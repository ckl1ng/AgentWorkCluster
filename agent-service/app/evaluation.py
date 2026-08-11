"""Deterministic evaluation contracts shared by local regression tooling.

This module intentionally has no model-provider dependency.  It records the
minimum stable interface between an evaluation case, a Harness result, and a
release gate so model and Harness changes can be compared fairly.
"""

from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, Iterable, List, Optional


FAILURE_CATEGORIES = frozenset({
    "model", "harness", "tool", "policy", "safety", "cancelled", "unknown",
})


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    input_text: str
    assertions: Dict[str, Any]
    tags: List[str]
    selected_context: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "EvaluationCase":
        case_id = str(value.get("id", "")).strip()
        input_text = str(value.get("input", ""))
        assertions = value.get("expected_assertions", {})
        tags = value.get("tags", [])
        if not case_id or not input_text or not isinstance(assertions, dict) or not isinstance(tags, list):
            raise ValueError("evaluation case is missing id, input, assertions, or tags")
        return cls(case_id, input_text, assertions, [str(tag) for tag in tags], str(value.get("selected_context", "")))


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    tags: List[str]
    passed: bool
    score: float
    latency_ms: int
    usage: Dict[str, Any]
    failure_category: Optional[str] = None
    failure_detail: str = ""

    def __post_init__(self) -> None:
        if self.failure_category is not None and self.failure_category not in FAILURE_CATEGORIES:
            raise ValueError("unknown evaluation failure category")


def evaluate_assertions(case: EvaluationCase, outcome: Dict[str, Any]) -> EvaluationResult:
    """Evaluate deterministic, auditable assertions without an LLM judge."""
    errors: List[str] = []
    assertions = case.assertions
    content = str(outcome.get("content", ""))
    state = str(outcome.get("state", ""))
    tool_name = outcome.get("tool_name")
    error_category = outcome.get("error_category")

    for needle in assertions.get("output_contains", []):
        if str(needle) not in content:
            errors.append("missing output text: {}".format(needle))
    for needle in assertions.get("output_not_contains", []):
        if str(needle) in content:
            errors.append("unexpected output text: {}".format(needle))
    if "state" in assertions and state != assertions["state"]:
        errors.append("expected state {}, got {}".format(assertions["state"], state))
    if "tool_name" in assertions and tool_name != assertions["tool_name"]:
        errors.append("expected tool {}, got {}".format(assertions["tool_name"], tool_name))
    if "error_category" in assertions and error_category != assertions["error_category"]:
        errors.append("expected error {}, got {}".format(assertions["error_category"], error_category))
    if assertions.get("confirmation_required") and not outcome.get("confirmation_required"):
        errors.append("confirmation was not required")
    if assertions.get("context_last_message") and outcome.get("context_last_message") != assertions["context_last_message"]:
        errors.append("wrong context window result")

    passed = not errors
    category = None if passed else str(outcome.get("failure_category", "harness"))
    if category not in FAILURE_CATEGORIES:
        category = "unknown"
    return EvaluationResult(
        case_id=case.case_id,
        tags=case.tags,
        passed=passed,
        score=1.0 if passed else 0.0,
        latency_ms=max(0, int(outcome.get("latency_ms", 0))),
        usage=dict(outcome.get("usage", {})),
        failure_category=category,
        failure_detail="; ".join(errors),
    )


def release_gate(results: Iterable[EvaluationResult], min_success_rate: float = 1.0, max_p95_latency_ms: int = 5_000) -> Dict[str, Any]:
    """Apply the phase-0 gate, where every baseline case is release-blocking."""
    values = list(results)
    if not values:
        return {"passed": False, "reason": "no evaluation results", "success_rate": 0.0}
    passed = [item for item in values if item.passed]
    latencies = sorted(item.latency_ms for item in values)
    p95_index = min(len(latencies) - 1, max(0, int(len(latencies) * 0.95 + 0.999999) - 1))
    p95 = latencies[p95_index]
    success_rate = len(passed) / len(values)
    critical_failures = [
        item.case_id for item in values
        if not item.passed and ("safety" in item.tags or item.failure_category == "safety")
    ]
    gate_passed = not critical_failures and success_rate >= min_success_rate and p95 <= max_p95_latency_ms
    return {
        "passed": gate_passed,
        "success_rate": success_rate,
        "passed_cases": len(passed),
        "total_cases": len(values),
        "p50_latency_ms": int(median(latencies)),
        "p95_latency_ms": p95,
        "critical_failures": critical_failures,
    }

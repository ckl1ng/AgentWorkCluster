"""Deterministic tool fixture; it never performs network or filesystem I/O."""

from typing import Any, Dict


def invoke(tool_name: str, arguments: Dict[str, Any], confirmed: bool = False) -> Dict[str, Any]:
    if tool_name == "weather_lookup":
        if set(arguments) != {"city"} or not isinstance(arguments["city"], str):
            return {"status": "error", "error_category": "tool", "summary": "invalid arguments"}
        return {"status": "success", "summary": "Shanghai: 22C", "content": {"temperature_c": 22}}
    if tool_name == "create_draft":
        if not confirmed:
            return {"status": "waiting_confirmation", "confirmation_required": True, "summary": "draft creation requires approval"}
        return {"status": "success", "summary": "draft created", "external_id": "draft-001"}
    return {"status": "error", "error_category": "policy", "summary": "tool is not authorized"}

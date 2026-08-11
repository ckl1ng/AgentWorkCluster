"""A local OpenAI-compatible model fixture with scripted, repeatable output."""

import json
import re
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


app = FastAPI(title="Deterministic evaluation model")
SCENARIO = re.compile(r"\[\[scenario:([a-z_]+)\]\]")


def scenario_response(messages: Any) -> Dict[str, Any]:
    text = "\n".join(str(item.get("content", "")) for item in messages if isinstance(item, dict))
    match = SCENARIO.search(text)
    scenario = match.group(1) if match else "normal"
    outputs = {
        "normal": {"content": "A deterministic answer."},
        "no_tool": {"content": "Answered without tools."},
        "read_tool": {"tool_name": "weather_lookup", "content": "Shanghai is 22C."},
        "write_tool": {"tool_name": "create_draft", "confirmation_required": True},
        "confirmation_rejected": {"content": "The draft was not created."},
        "timeout": {"error_category": "model", "status_code": 504},
        "protocol_error": {"error_category": "model", "malformed": True},
        "invalid_arguments": {"tool_name": "weather_lookup", "error_category": "tool"},
        "cancelled": {"error_category": "cancelled"},
        "long_context": {"content": "The newest task is newest task.", "context_last_message": "newest task"},
        "budget_exceeded": {"error_category": "policy"},
        "rate_limit": {"error_category": "policy"},
        "private_url": {"error_category": "safety"},
        "redirect_private": {"error_category": "safety"},
        "redaction": {"content": "Credential: ***REDACTED***"},
        "unauthorized_tool": {"error_category": "policy"},
        "response_limit": {"content": "Tool response truncated."},
        "key_redaction": {"content": "Credentials are protected."},
        "context_epoch": {"content": "Using fresh context."},
        "event_recovery": {"content": "Recovered from sequence 3."},
    }
    return outputs[scenario]


@app.post("/v1/chat/completions")
async def chat_completions(payload: Dict[str, Any]) -> PlainTextResponse:
    result = scenario_response(payload.get("messages", []))
    if result.get("status_code"):
        return PlainTextResponse("test model timeout", status_code=result["status_code"])
    if result.get("malformed"):
        return PlainTextResponse("data: not-json\n\ndata: [DONE]\n\n", media_type="text/event-stream")
    delta = {"content": result.get("content", "")}
    if result.get("tool_name"):
        delta["tool_calls"] = [{"index": 0, "id": "eval-call-1", "type": "function", "function": {"name": result["tool_name"], "arguments": "{}"}}]
    body = "data: {}\n\ndata: [DONE]\n\n".format(json.dumps({"choices": [{"delta": delta}]}))
    return PlainTextResponse(body, media_type="text/event-stream")

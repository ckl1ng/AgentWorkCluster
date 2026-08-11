"""Boundary checks shared by model and HTTP tool integrations."""

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any, Dict, Iterable, List
from urllib.parse import parse_qsl, urlparse, urlunparse

from fastapi import HTTPException


SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|api[_-]?key|token|password|secret|private[_-]?key)",
    re.IGNORECASE,
)
REDACTED = "***REDACTED***"


def redact(value: Any) -> Any:
    """Return a display-safe copy without credentials or sensitive query values."""
    if isinstance(value, dict):
        return {str(k): REDACTED if SENSITIVE_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        query = [(key, REDACTED if SENSITIVE_KEY.search(key) else item) for key, item in parse_qsl(parsed.query, True)]
        return urlunparse(parsed._replace(query="&".join("{}={}".format(k, v) for k, v in query)))
    return value


def audit_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a useful trace index without duplicating user/model/tool plaintext."""
    cleaned = redact(payload)
    if event_type == "agent.message.delta":
        return {"content_length": len(str(payload.get("content", ""))), "summary": "模型输出增量"}
    if event_type == "agent.run.completed":
        return {"usage": cleaned.get("usage", {}), "summary": cleaned.get("summary", "运行完成")}
    if event_type == "agent.tool.completed":
        result = payload.get("result") or {}
        return {
            "tool": cleaned.get("tool"),
            "tool_type": cleaned.get("tool_type"),
            "result": {key: redact(result.get(key)) for key in ("status", "http_status", "content_type", "error") if key in result},
            "summary": cleaned.get("summary", "工具执行完成"),
        }
    return cleaned


def is_public_address(address: str) -> bool:
    try:
        value = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(value.is_global and not value.is_private and not value.is_loopback and not value.is_link_local)


async def assert_safe_public_url(url: str, allow_http: bool = False) -> None:
    """Validate scheme, hostname and every DNS result before opening a socket."""
    parsed = urlparse(url)
    if parsed.scheme not in (("https", "http") if allow_http else ("https",)):
        raise HTTPException(status_code=400, detail="工具 URL 必须使用 HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="工具 URL 无效")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="工具 URL 不允许访问本机地址")
    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        )
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail="工具域名无法解析") from exc
    addresses = {item[4][0] for item in infos}
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise HTTPException(status_code=400, detail="工具 URL 仅允许公网地址")


def require_object_schema(schema: Any) -> Dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise HTTPException(status_code=400, detail="工具输入 Schema 必须是 JSON object")
    if len(str(schema)) > 100_000:
        raise HTTPException(status_code=400, detail="工具输入 Schema 过大")
    return schema


def response_summary(body: bytes, content_type: str = "") -> str:
    """Keep traces useful while preventing arbitrary large response persistence."""
    excerpt = body[:4096].decode("utf-8", errors="replace").strip()
    if "json" in content_type.lower():
        if not excerpt:
            return "返回 JSON 响应"
        try:
            return json.dumps(redact(json.loads(excerpt)), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            return "JSON response could not be safely summarized"
    return redact(excerpt) if excerpt else "响应无内容"


def assert_public_peer(response: Any) -> None:
    """Verify the connected address after DNS resolution to block rebinding."""
    stream = response.extensions.get("network_stream") if hasattr(response, "extensions") else None
    if stream is None:
        return
    peer = stream.get_extra_info("server_addr")
    address = peer[0] if isinstance(peer, tuple) and peer else peer
    if address and not is_public_address(str(address)):
        raise HTTPException(status_code=400, detail="工具连接到了非公网地址")


def openapi_operations(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert simple OpenAPI 3 operations to explicit, reviewable candidates."""
    if not isinstance(document.get("paths"), dict):
        raise HTTPException(status_code=400, detail="OpenAPI 文档缺少 paths")
    candidates: List[Dict[str, Any]] = []
    for path, path_item in document["paths"].items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head"} or not isinstance(operation, dict):
                continue
            name = operation.get("operationId") or "{}_{}".format(method.lower(), path.strip("/").replace("/", "_").replace("{", "").replace("}", ""))
            parameters = {"type": "object", "properties": {}, "required": []}
            for parameter in list(path_item.get("parameters", [])) + list(operation.get("parameters", [])):
                if not isinstance(parameter, dict) or parameter.get("in") not in {"query", "path"}:
                    continue
                param_name = parameter.get("name")
                if not isinstance(param_name, str):
                    continue
                parameters["properties"][param_name] = parameter.get("schema") or {"type": "string"}
                if parameter.get("required"):
                    parameters["required"].append(param_name)
            candidates.append({
                "name": str(name)[:64],
                "description": str(operation.get("summary") or operation.get("description") or name)[:2000],
                "method": method.upper(), "path": path,
                "input_schema": parameters,
                "has_side_effect": method.lower() not in {"get", "head"},
            })
    if not candidates:
        raise HTTPException(status_code=400, detail="OpenAPI 文档中没有可导入的 HTTP operation")
    return candidates

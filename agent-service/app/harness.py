"""Context, model-stream and policy-controlled tool providers."""

import asyncio
import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
import httpcore
from jsonschema import Draft202012Validator, ValidationError

from .safety import assert_public_peer, assert_safe_public_url, is_public_address, response_summary


AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
AMAP_WEATHER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "pattern": "^[0-9]{6}$", "description": "城市 adcode，例如 310000"},
        "extensions": {"type": "string", "enum": ["base", "all"], "default": "base"},
        "output": {"type": "string", "enum": ["JSON", "XML"], "default": "JSON"},
    },
    "required": ["city"],
    "additionalProperties": False,
}


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, reject non-public answers, then connect to the checked IP."""

    def __init__(self, backend: Optional[httpcore.AsyncNetworkBackend] = None) -> None:
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout=None, local_address=None, socket_options=None):
        try:
            infos = await asyncio.get_running_loop().run_in_executor(
                None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            )
        except socket.gaierror as exc:
            raise httpcore.ConnectError("hostname could not be resolved") from exc
        addresses = list(dict.fromkeys(item[4][0] for item in infos))
        if not addresses or any(not is_public_address(address) for address in addresses):
            raise httpcore.ConnectError("hostname resolved to a non-public address")
        return await self.backend.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not permitted for HTTP tools")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


def safe_http_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    transport._pool._network_backend = SafeNetworkBackend()
    return transport


def estimate_tokens(value: Any) -> int:
    """Conservative tokenizer-independent estimate used for hard local budgets."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def prepare_context(
    system_prompt: str,
    history: List[Dict[str, str]],
    policy: Dict[str, int],
    max_output_tokens: int,
    tool_count: int,
    tool_tokens: int = 0,
    memories: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    context_window = max(2048, int(policy.get("context_window", 32768)))
    input_budget = max(512, context_window - max_output_tokens - tool_tokens)
    status = {
        "run_goal": history[-1]["content"][:500] if history else "",
        "completed": [],
        "facts": [],
        "pending_confirmation": None,
        "remaining": {"tool_calls": int(policy.get("max_tool_calls", 6)), "output_tokens": max_output_tokens},
        "constraints": ["Only configured tools may run", "Never expose credentials"],
    }
    prompt_budget = max(128, input_budget // 2)
    prompt = system_prompt
    if estimate_tokens(prompt) > prompt_budget:
        prompt = prompt[:prompt_budget * 3] + "\n[system prompt truncated by context budget]"
    prefix = [
        {"role": "system", "content": prompt},
        {"role": "system", "content": "Runtime state (authoritative JSON): " + json.dumps(status, ensure_ascii=False)},
    ]
    memory_lines = [
        "- [{kind}] {content}".format(kind=item.get("kind", "fact"), content=item["content"])
        for item in (memories or []) if item.get("content")
    ]
    if memory_lines:
        prefix.append({"role": "system", "content": "Authorized long-term memory:\n" + "\n".join(memory_lines)})
    used = sum(estimate_tokens(item["content"]) for item in prefix)
    kept: List[Dict[str, str]] = []
    dropped = 0
    for index, item in enumerate(reversed(history)):
        cost = estimate_tokens(item["content"]) + 8
        if used + cost > input_budget:
            if index == 0:
                remaining = max(32, input_budget - used - 8)
                kept.append({**item, "content": item["content"][-remaining * 3:]})
                used += min(cost, remaining + 8)
                continue
            dropped += 1
            continue
        kept.append(item)
        used += cost
    kept.reverse()
    manifest = {
        "strategy": "phase-a-sliding-window-v1",
        "context_window": context_window,
        "input_budget": input_budget,
        "estimated_input_tokens": used,
        "history_kept": len(kept),
        "history_dropped": dropped,
        "tool_count": tool_count,
        "tool_tokens": tool_tokens,
        "system_prompt_truncated": prompt != system_prompt,
        "status": status,
        "memory_count": len(memory_lines),
    }
    return prefix + kept, manifest


def tool_declarations(tools: List[Dict[str, Any]], token_budget: int = 4096) -> List[Dict[str, Any]]:
    declarations = []
    used = 0
    for tool in tools:
        method = str(tool.get("config", {}).get("method", "GET")).upper()
        internal_autonomy = str(tool.get("config", {}).get("builtin", "")) in {"qq_send_group_message", "qq_remind_group_member", "timer_create"}
        if method not in {"GET", "HEAD"} and tool.get("side_effect", "write") == "write" and tool.get("confirmation_mode", "none") == "none" and not internal_autonomy:
            # Do not expose legacy or malformed write tools that lack the Phase B approval gate.
            continue
        declaration = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        cost = estimate_tokens(declaration)
        if declarations and used + cost > token_budget:
            continue
        if cost > token_budget:
            continue
        declarations.append(declaration)
        used += cost
    return declarations


@dataclass
class ModelTurn:
    content: List[str] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    tool_calls: Dict[int, Dict[str, str]] = field(default_factory=dict)
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    reasoning_delta: str = ""

    @property
    def text(self) -> str:
        return "".join(self.content)

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning)

    @staticmethod
    def _text_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("text") or value.get("content") or "")
        if isinstance(value, list):
            return "".join(ModelTurn._text_value(item) for item in value)
        return ""

    def merge(self, chunk: Dict[str, Any]) -> str:
        if isinstance(chunk.get("usage"), dict):
            raw_usage = {key: int(value) for key, value in chunk["usage"].items() if isinstance(value, (int, float))}
            self.usage = {
                "input_tokens": raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)),
                "output_tokens": raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)),
                "total_tokens": raw_usage.get("total_tokens", 0),
            }
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        self.finish_reason = choice.get("finish_reason") or self.finish_reason
        delta = choice.get("delta") or {}
        text = self._text_value(delta.get("content"))
        if text:
            self.content.append(text)
        self.reasoning_delta = self._text_value(
            delta.get("reasoning_content")
            or delta.get("reasoning")
            or delta.get("thinking")
            or choice.get("reasoning_content")
            or choice.get("reasoning")
            or choice.get("thinking")
        )
        if self.reasoning_delta:
            self.reasoning.append(self.reasoning_delta)
        for part in delta.get("tool_calls") or []:
            index = int(part.get("index", 0))
            current = self.tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if part.get("id"):
                current["id"] += str(part["id"])
            function = part.get("function") or {}
            current["name"] += str(function.get("name") or "")
            current["arguments"] += str(function.get("arguments") or "")
        return text


async def stream_chat(response: httpx.Response) -> AsyncIterator[Tuple[ModelTurn, str]]:
    turn = ModelTurn()
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        yield turn, turn.merge(chunk)


def _render_url(template: str, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    remaining = dict(arguments)
    rendered = template
    for key, value in list(remaining.items()):
        marker = "{" + key + "}"
        if marker in rendered:
            rendered = rendered.replace(marker, quote(str(value), safe=""))
            remaining.pop(key)
    return rendered, remaining


async def execute_http_tool(
    tool: Dict[str, Any], arguments: Dict[str, Any], allow_http: bool, response_limit: int
) -> Dict[str, Any]:
    method = str(tool["config"].get("method", "GET")).upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise RuntimeError("Unsupported HTTP tool method")
    try:
        Draft202012Validator(tool["input_schema"]).validate(arguments)
    except ValidationError as exc:
        raise ValueError("Tool arguments do not match schema: " + exc.message) from exc
    url, remaining = _render_url(str(tool["config"]["url"]), arguments)
    await assert_safe_public_url(url, allow_http)
    headers = {str(k): str(v) for k, v in (tool["config"].get("headers") or {}).items()}
    locations = tool["config"].get("parameter_locations") or {}
    if not isinstance(locations, dict) or any(location not in {"query", "body"} for location in locations.values()):
        raise ValueError("Tool parameter_locations must map arguments to query or body")
    params = {key: value for key, value in remaining.items() if locations.get(key, "query") == "query"}
    body = {key: value for key, value in remaining.items() if locations.get(key) == "body"}
    if body:
        headers.setdefault("Content-Type", "application/json")
    timeout = min(30.0, float(tool["config"].get("timeout_seconds", 15)))
    body = bytearray()
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=safe_http_transport(), trust_env=False,
    ) as client:
        async with client.stream(method, url, params=params, headers=headers, json=body or None) as response:
            assert_public_peer(response)
            if 300 <= response.status_code < 400:
                raise RuntimeError("Tool redirects are not allowed")
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > response_limit:
                    raise RuntimeError("Tool response exceeded configured limit")
            content_type = response.headers.get("content-type", "")
    return {
        "status": "ok",
        "http_status": response.status_code,
        "content_type": content_type[:120],
        "content": response_summary(bytes(body), content_type),
    }


async def execute_amap_weather_tool(
    arguments: Dict[str, Any], api_key: str, allow_http: bool, response_limit: int,
) -> Dict[str, Any]:
    """Query the fixed Amap endpoint without exposing its service credential."""
    if not api_key:
        raise RuntimeError("高德天气工具未配置 AMAP_WEATHER_API_KEY")
    try:
        Draft202012Validator(AMAP_WEATHER_INPUT_SCHEMA).validate(arguments)
    except ValidationError as exc:
        raise ValueError("Tool arguments do not match schema: " + exc.message) from exc

    # The credential is added only to this outbound request. It is absent from
    # the tool declaration, database configuration, snapshots, and trace events.
    request_tool = {
        "config": {
            "url": AMAP_WEATHER_URL,
            "method": "GET",
            "parameter_locations": {
                "key": "query", "city": "query", "extensions": "query", "output": "query",
            },
        },
        "input_schema": {
            "type": "object",
            "properties": {
                **AMAP_WEATHER_INPUT_SCHEMA["properties"],
                "key": {"type": "string", "minLength": 1},
            },
            "required": ["key", "city"],
            "additionalProperties": False,
        },
    }
    return await execute_http_tool(request_tool, {
        "key": api_key,
        "city": arguments["city"],
        "extensions": arguments.get("extensions", "base"),
        "output": arguments.get("output", "JSON"),
    }, allow_http, response_limit)


async def execute_mcp_tool(
    tool: Dict[str, Any], arguments: Dict[str, Any], allow_http: bool, response_limit: int
) -> Dict[str, Any]:
    """Call a remote Streamable-HTTP MCP server through the same network boundary."""
    try:
        Draft202012Validator(tool["input_schema"]).validate(arguments)
    except ValidationError as exc:
        raise ValueError("Tool arguments do not match schema: " + exc.message) from exc
    config = tool["config"]
    endpoint = str(config.get("url", ""))
    remote_name = str(config.get("remote_tool_name", ""))
    if not endpoint or not remote_name:
        raise ValueError("MCP tool configuration is incomplete")
    await assert_safe_public_url(endpoint, allow_http)
    headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items()}
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json, text/event-stream")
    request = {"jsonrpc": "2.0", "id": "agent-tool", "method": "tools/call", "params": {"name": remote_name, "arguments": arguments}}
    body = bytearray()
    timeout = min(30.0, float(config.get("timeout_seconds", 15)))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=safe_http_transport(), trust_env=False) as client:
        async with client.stream("POST", endpoint, headers=headers, json=request) as response:
            assert_public_peer(response)
            if 300 <= response.status_code < 400:
                raise RuntimeError("MCP redirects are not allowed")
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > response_limit:
                    raise RuntimeError("MCP response exceeded configured limit")
            content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type.lower():
        lines = [line[5:].strip() for line in body.decode("utf-8", errors="replace").splitlines() if line.startswith("data:")]
        raw = lines[-1] if lines else "{}"
    else:
        raw = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("MCP returned an invalid JSON-RPC response") from exc
    if payload.get("error"):
        raise RuntimeError("MCP error: " + str(payload["error"].get("message", "unknown"))[:300])
    result = payload.get("result", {})
    return {
        "status": "ok", "content_type": content_type[:120],
        "content": response_summary(json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json"),
    }


def _validate_arguments(tool: Dict[str, Any], arguments: Dict[str, Any]) -> None:
    try:
        Draft202012Validator(tool["input_schema"]).validate(arguments)
    except ValidationError as exc:
        raise ValueError("Tool arguments do not match schema: " + exc.message) from exc


async def _stdio_request(process: asyncio.subprocess.Process, request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """Send one JSON-RPC request, accepting both newline and Content-Length MCP framing."""
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("MCP STDIO pipes are unavailable")
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    process.stdin.write(("Content-Length: {}\r\n\r\n".format(len(encoded))).encode("ascii") + encoded)
    await process.stdin.drain()

    async def read_response() -> Dict[str, Any]:
        first = await process.stdout.readline()
        if not first:
            raise RuntimeError("MCP STDIO server exited without a response")
        # A few lightweight local servers use JSONL despite the MCP framing spec.
        if first.lstrip().startswith(b"{"):
            return json.loads(first.decode("utf-8"))
        headers = first + await process.stdout.readline()
        while b"\r\n\r\n" not in headers and b"\n\n" not in headers:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("Invalid MCP STDIO headers")
            headers += line
        marker = b"\r\n\r\n" if b"\r\n\r\n" in headers else b"\n\n"
        header_text = headers.split(marker, 1)[0].decode("ascii", errors="replace")
        length = next((int(line.split(":", 1)[1].strip()) for line in header_text.splitlines()
                       if line.lower().startswith("content-length:")), None)
        if length is None or length < 0 or length > 8 * 1024 * 1024:
            raise RuntimeError("Invalid MCP STDIO Content-Length")
        payload = await process.stdout.readexactly(length)
        return json.loads(payload.decode("utf-8"))

    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.1, deadline - time.monotonic())
        response = await asyncio.wait_for(read_response(), timeout=remaining)
        if response.get("id") == request.get("id"):
            return response


async def _execute_mcp_stdio(config: Dict[str, Any], method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    command = config.get("command")
    args = config.get("args") or []
    if not isinstance(command, str) or not command.strip() or not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("MCP STDIO command/args configuration is invalid")
    env = os.environ.copy()
    for key, value in (config.get("env") or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key not in {"PATH", "PYTHONPATH", "LD_PRELOAD"}:
            env[key] = value
    try:
        process = await asyncio.create_subprocess_exec(
            command, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError("无法启动本地工具进程: " + str(exc)) from exc
    try:
        await _stdio_request(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "chat-agent", "version": "1.0"},
        }}, timeout)
        # MCP servers require this notification before serving requests.
        if process.stdin is not None:
            notification = b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'
            process.stdin.write(("Content-Length: {}\r\n\r\n".format(len(notification))).encode("ascii") + notification)
            await process.stdin.drain()
        return await _stdio_request(process, {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, timeout)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def discover_mcp_stdio(config: Dict[str, Any], timeout: float = 15.0) -> List[Dict[str, Any]]:
    response = await _execute_mcp_stdio(config, "tools/list", {}, timeout)
    if response.get("error"):
        raise RuntimeError("MCP error: " + str(response["error"].get("message", "unknown"))[:300])
    tools = (response.get("result") or {}).get("tools")
    return tools if isinstance(tools, list) else []


async def execute_local_tool(
    tool: Dict[str, Any], arguments: Dict[str, Any], response_limit: int
) -> Dict[str, Any]:
    _validate_arguments(tool, arguments)
    config = tool.get("config") or {}
    if config.get("builtin") == "current_time":
        beijing = timezone(timedelta(hours=8))
        return {"status": "ok", "content": json.dumps({
            "unix": int(time.time()),
            "iso": datetime.now(beijing).isoformat(),
            "timezone": "Asia/Shanghai",
        })}
    command = config.get("command")
    args = config.get("args") or []
    if not isinstance(command, str) or not command.strip() or not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("本地工具 command/args 配置无效")
    env = os.environ.copy()
    for key, value in (config.get("env") or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key not in {"PATH", "PYTHONPATH", "LD_PRELOAD"}:
            env[key] = value
    try:
        process = await asyncio.create_subprocess_exec(
            command, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(arguments, ensure_ascii=False).encode("utf-8") + b"\n"),
            timeout=min(30.0, float(config.get("timeout_seconds", 15))),
        )
    except asyncio.TimeoutError as exc:
        if 'process' in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        raise RuntimeError("本地工具执行超时") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError("无法启动本地工具进程: " + str(exc)) from exc
    if process.returncode != 0:
        raise RuntimeError("本地工具失败: " + stderr.decode("utf-8", errors="replace")[:300])
    if len(stdout) > response_limit:
        raise RuntimeError("本地工具响应超过配置上限")
    return {"status": "ok", "content": response_summary(stdout, "application/json")}


async def execute_stdio_mcp_tool(tool: Dict[str, Any], arguments: Dict[str, Any], response_limit: int) -> Dict[str, Any]:
    _validate_arguments(tool, arguments)
    config = tool.get("config") or {}
    response = await _execute_mcp_stdio(
        config, "tools/call", {"name": config.get("remote_tool_name", tool["name"]), "arguments": arguments},
        min(30.0, float(config.get("timeout_seconds", 15))),
    )
    if response.get("error"):
        raise RuntimeError("MCP error: " + str(response["error"].get("message", "unknown"))[:300])
    content = json.dumps(response.get("result", {}), ensure_ascii=False).encode("utf-8")
    if len(content) > response_limit:
        raise RuntimeError("MCP response exceeded configured limit")
    return {"status": "ok", "content_type": "application/json", "content": response_summary(content, "application/json")}


# Retained for integrations compiled against the Phase A function name.
execute_read_tool = execute_http_tool

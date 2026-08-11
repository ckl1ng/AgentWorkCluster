"""Small dependency-free STDIO MCP server used by the fixed web tools."""

import html
import ipaddress
import json
import re
import socket
import sys
from html.parser import HTMLParser
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen


def write_message(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(("Content-Length: {}\r\n\r\n".format(len(data))).encode("ascii") + data)
    sys.stdout.buffer.flush()


def read_message():
    first = sys.stdin.buffer.readline()
    if not first:
        return None
    if first.lstrip().startswith(b"{"):
        return json.loads(first.decode("utf-8"))
    headers = first + sys.stdin.buffer.readline()
    while b"\r\n\r\n" not in headers and b"\n\n" not in headers:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        headers += line
    match = re.search(br"(?im)^content-length:\s*(\d+)", headers)
    if not match:
        raise ValueError("missing Content-Length")
    return json.loads(sys.stdin.buffer.read(int(match.group(1))).decode("utf-8"))


def assert_public_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only http(s) URLs are supported")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("private or local URLs are not allowed")


class SearchParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None
        self.capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self.current = {"title": "", "url": "", "description": ""}
            self.capture = None
        elif self.current and tag == "h2":
            self.capture = "title"
        elif self.current and tag == "a" and self.capture == "title":
            self.current["url"] = attrs.get("href", "")
        elif tag == "a" and "result__a" in classes:
            self.current = {"title": "", "url": attrs.get("href", ""), "description": ""}
            self.capture = "title"
        elif self.current and tag == "p":
            self.capture = "description"
        elif self.current and tag in {"a", "div", "span"} and "result__snippet" in classes:
            self.capture = "description"

    def handle_data(self, data):
        if self.current and self.capture:
            self.current[self.capture] += data

    def handle_endtag(self, tag):
        if self.current and tag == "li" and self.current.get("url"):
            self.items.append(self.current)
            self.current = None
            self.capture = None


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            value = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if value:
                self.parts.append(value)


def fetch(url):
    assert_public_url(url)
    request = Request(url, headers={"User-Agent": "chat-agent-web-search/1.0", "Accept": "text/html,application/xhtml+xml"})
    with urlopen(request, timeout=15) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("response exceeded 1 MiB")
        return body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def call_tool(name, arguments):
    if name == "search_web":
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(10, int(arguments.get("max_results", 5))))
        parser = SearchParser()
        parser.feed(fetch("https://www.bing.com/search?q=" + quote_plus(query) + "&count=" + str(limit)))
        results = parser.items[:limit]
        text = "\n\n".join("{}. {}\nURL: {}\n{}".format(i + 1, item["title"].strip(), item["url"], item["description"].strip()) for i, item in enumerate(results))
        return {"content": [{"type": "text", "text": text or "未找到搜索结果。"}]}
    if name == "read_url":
        url = str(arguments.get("url", "")).strip()
        parser = TextParser()
        parser.feed(fetch(url))
        return {"content": [{"type": "text", "text": "\n".join(parser.parts)[:50000]}]}
    raise ValueError("unknown tool: " + name)


TOOLS = [
    {"name": "search_web", "description": "搜索互联网并返回结果摘要。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}},
    {"name": "read_url", "description": "读取网页正文内容。", "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "format": "uri"}}, "required": ["url"]}},
]


def main():
    while True:
        request = read_message()
        if request is None:
            return
        method = request.get("method")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            write_message({"jsonrpc": "2.0", "id": request.get("id"), "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "chat-web-search", "version": "1.0"}}})
        elif method == "tools/list":
            write_message({"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": TOOLS}})
        elif method == "tools/call":
            try:
                params = request.get("params") or {}
                write_message({"jsonrpc": "2.0", "id": request.get("id"), "result": call_tool(params.get("name", ""), params.get("arguments") or {})})
            except Exception as exc:
                write_message({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": str(exc)[:500]}})
        elif request.get("id") is not None:
            write_message({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()

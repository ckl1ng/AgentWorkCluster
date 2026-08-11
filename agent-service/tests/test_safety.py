import asyncio
import json
import socket
import unittest
from unittest.mock import patch

import httpcore

from fastapi import HTTPException

from app.harness import SafeNetworkBackend
from app.safety import audit_payload, assert_safe_public_url, redact, require_object_schema, response_summary


class SafetyBoundaryTest(unittest.TestCase):
    def test_redact_removes_nested_credentials_and_url_tokens(self):
        value = redact({"api_key": "super-secret", "nested": [{"authorization": "Bearer x"}], "url": "https://example.test/?token=abc&visible=yes"})
        self.assertEqual(value["api_key"], "***REDACTED***")
        self.assertEqual(value["nested"][0]["authorization"], "***REDACTED***")
        self.assertIn("token=***REDACTED***", value["url"])
        self.assertNotIn("abc", value["url"])

    def test_non_object_tool_schema_is_rejected(self):
        with self.assertRaises(HTTPException):
            require_object_schema({"type": "array"})

    def test_private_dns_result_is_rejected(self):
        def private_resolution(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("app.safety.socket.getaddrinfo", side_effect=private_resolution):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(assert_safe_public_url("https://example.test"))
        self.assertEqual(error.exception.status_code, 400)

    def test_http_is_rejected_unless_explicitly_allowed(self):
        with self.assertRaises(HTTPException):
            asyncio.run(assert_safe_public_url("http://example.test"))

    def test_audit_trace_and_json_response_do_not_copy_credentials_or_content(self):
        audit = audit_payload("agent.run.completed", {
            "content": "private answer", "usage": {"total_tokens": 4}, "summary": "done",
        })
        self.assertNotIn("private answer", json.dumps(audit))
        summary = response_summary(b'{"token":"secret","visible":"yes"}', "application/json")
        self.assertNotIn("secret", summary)
        self.assertIn("***REDACTED***", summary)

    def test_safe_network_backend_connects_to_the_validated_ip(self):
        class Backend:
            connected_host = None

            async def connect_tcp(self, host, *_args, **_kwargs):
                self.connected_host = host
                return object()

        backend = Backend()
        public_resolution = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("app.harness.socket.getaddrinfo", return_value=public_resolution):
            asyncio.run(SafeNetworkBackend(backend).connect_tcp("example.test", 443))
        self.assertEqual(backend.connected_host, "93.184.216.34")

    def test_safe_network_backend_rejects_rebound_private_ip_before_connect(self):
        class Backend:
            async def connect_tcp(self, *_args, **_kwargs):
                raise AssertionError("private address must not be connected")

        private_resolution = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]
        with patch("app.harness.socket.getaddrinfo", return_value=private_resolution):
            with self.assertRaises(httpcore.ConnectError):
                asyncio.run(SafeNetworkBackend(Backend()).connect_tcp("example.test", 443))

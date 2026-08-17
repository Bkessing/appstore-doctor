"""Tests for the MCP stdio server.

No network and no App Store Connect credentials required: these drive the
JSON-RPC loop itself (initialize, tools/list, and the credential-missing path
of tools/call) against in-memory streams. What they pin:

  * the initialize handshake returns the protocol version and tool
    capability an MCP client checks before doing anything else
  * every advertised tool carries a name, a description, and a JSON Schema
    for its arguments -- an agent cannot call a tool it cannot introspect
  * a notification (no "id" member) never gets a response, which is a
    protocol violation if it happens
  * a tool that needs App Store Connect credentials fails as a normal text
    result, not as a crashed server, when none are configured

Run with: python3 -m unittest discover tests
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from appstore_doctor import mcp_server


def _line(obj):
    return json.dumps(obj) + "\n"


def _run(*messages):
    """Feed newline-delimited JSON-RPC messages through run_stdio and return
    the decoded responses, in order."""
    inp = io.StringIO("".join(_line(m) for m in messages))
    out = io.StringIO()
    mcp_server.run_stdio(in_stream=inp, out_stream=out)
    lines = [l for l in out.getvalue().splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


class TestInitialize(unittest.TestCase):
    def test_handshake_reports_protocol_version_and_tools_capability(self):
        [resp] = _run({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "appstore-doctor")

    def test_notification_gets_no_response(self):
        # "notifications/initialized" carries no id. Answering it is itself a
        # protocol violation, so the loop must emit nothing for this line.
        responses = _run({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(responses, [])

    def test_malformed_json_gets_a_parse_error_not_a_crash(self):
        out = io.StringIO()
        mcp_server.run_stdio(in_stream=io.StringIO("not json at all\n"), out_stream=out)
        [resp] = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(resp["error"]["code"], -32700)

    def test_unknown_method_reports_method_not_found(self):
        [resp] = _run({"jsonrpc": "2.0", "id": 5, "method": "not/a/real/method"})
        self.assertEqual(resp["error"]["code"], -32601)


class TestToolsList(unittest.TestCase):
    def test_every_tool_has_a_schema_an_agent_can_introspect(self):
        [resp] = _run({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        self.assertGreaterEqual(len(tools), 3, "at least a few tools should be exposed")
        self.assertLessEqual(len(tools), 5, "keep the surface small and read-only")
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertTrue(tool["description"], "a tool an agent can't read is useless")
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_expected_read_only_tools_are_present(self):
        [resp] = _run({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        for expected in ("diagnose", "list_apps", "version_state", "iap_status"):
            self.assertIn(expected, names)


class TestToolsCall(unittest.TestCase):
    def test_unknown_tool_is_a_result_error_not_a_protocol_error(self):
        # MCP tool failures are reported inside the result (isError: true),
        # not as JSON-RPC protocol errors -- a client should be able to show
        # a bad tool call to the model without treating the whole call as
        # having failed at the transport level.
        [resp] = _run({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "not_a_real_tool", "arguments": {}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("unknown tool", resp["result"]["content"][0]["text"])

    def test_missing_credentials_is_a_clean_message_not_a_crash(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_FILEPATH")}
        with mock.patch.dict(os.environ, env, clear=True):
            [resp] = _run({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                           "params": {"name": "list_apps", "arguments": {}}})
        self.assertFalse(resp["result"]["isError"],
                         "a missing-credentials message is a normal result, not a protocol "
                         "failure -- the server must stay up for the next call")
        self.assertIn("credentials not configured", resp["result"]["content"][0]["text"])

    def test_signing_check_needs_no_credentials(self):
        # The one tool that should work with zero configuration at all, same
        # as `appstore-doctor --local-only`.
        [resp] = _run({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "signing_check", "arguments": {}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("signing.", resp["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()

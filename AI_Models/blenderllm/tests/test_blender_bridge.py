"""Tests for the V0.4 Blender execution bridge client and flow.

Fully mocked: no Blender, no sockets on real network, no Ollama.
Run with:  python -m unittest discover tests -v
"""

import json
import socket
import unittest
from unittest import mock

import blender_bridge
import main
from blender_bridge import (
    BridgeConnectionError,
    BridgeNotRunningError,
    BridgeProtocolError,
    BridgeTimeoutError,
    send_script,
)


class FakeConnection:
    """Mimics the socket returned by socket.create_connection."""

    def __init__(self, response_line=b""):
        self.response_line = response_line
        self.sent = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode, encoding=None):
        class FakeFile:
            def __init__(self, line):
                self.line = line

            def readline(self):
                return self.line

        return FakeFile(self.response_line)

    def close(self):
        self.closed = True


class SendScriptTests(unittest.TestCase):
    def _patch_connection(self, response_line):
        conn = FakeConnection(response_line)
        patcher = mock.patch(
            "blender_bridge.socket.create_connection", return_value=conn)
        patcher.start()
        self.addCleanup(patcher.stop)
        return conn

    def test_success_result(self):
        conn = self._patch_connection(
            json.dumps({"status": "SUCCESS", "stdout": "ok\n", "stderr": ""}).encode() + b"\n")
        result = send_script("print('hi')", host="127.0.0.1", port=1)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["stdout"], "ok\n")
        request = json.loads(conn.sent.decode("utf-8"))
        self.assertEqual(request, {"script": "print('hi')"})

    def test_error_result_parsed(self):
        self._patch_connection(
            json.dumps({
                "status": "ERROR",
                "error_type": "ZeroDivisionError",
                "error": "division by zero",
                "traceback": "Traceback (most recent call last):...",
            }).encode() + b"\n")
        result = send_script("1/0")
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_type"], "ZeroDivisionError")

    def test_connection_refused_is_translated(self):
        with mock.patch("blender_bridge.socket.create_connection",
                        side_effect=ConnectionRefusedError("refused")):
            with self.assertRaises(BridgeNotRunningError):
                send_script("x = 1")

    def test_timeout_is_translated(self):
        with mock.patch("blender_bridge.socket.create_connection",
                        side_effect=socket.timeout("timed out")):
            with self.assertRaises(BridgeTimeoutError):
                send_script("x = 1")

    def test_other_oserror_is_translated(self):
        with mock.patch("blender_bridge.socket.create_connection",
                        side_effect=OSError("broken")):
            with self.assertRaises(BridgeConnectionError):
                send_script("x = 1")

    def test_malformed_response_is_protocol_error(self):
        self._patch_connection(b"not json at all\n")
        with self.assertRaises(BridgeProtocolError):
            send_script("x = 1")

    def test_empty_response_is_protocol_error(self):
        self._patch_connection(b"")
        with self.assertRaises(BridgeProtocolError):
            send_script("x = 1")

    def test_unexpected_status_is_protocol_error(self):
        self._patch_connection(b'{"status": "MAYBE"}\n')
        with self.assertRaises(BridgeProtocolError):
            send_script("x = 1")

    def test_empty_script_raises_before_connecting(self):
        with mock.patch("blender_bridge.socket.create_connection") as create:
            with self.assertRaises(BridgeProtocolError):
                send_script("   ")
            create.assert_not_called()


class RunExecuteTests(unittest.TestCase):
    def test_cancel_without_sending(self):
        def unexpected_sender(code):
            raise AssertionError("sender must not be called on cancel")

        lines = main.run_execute("print(1)", sender=unexpected_sender,
                                 confirm=lambda: False)
        self.assertIn("Execution cancelled.", lines)

    def test_success_flow(self):
        lines = main.run_execute(
            "print(1)",
            sender=lambda code: {"status": "SUCCESS", "stdout": "1\n", "stderr": ""},
            confirm=lambda: True)
        self.assertIn("Blender: execution successful.", lines)
        self.assertTrue(any("1" in line for line in lines))

    def test_error_result_displayed(self):
        lines = main.run_execute(
            "1/0",
            sender=lambda code: {
                "status": "ERROR",
                "error_type": "ZeroDivisionError",
                "error": "division by zero",
                "traceback": "Traceback...\nZeroDivisionError",
            },
            confirm=lambda: True)
        self.assertIn("Blender: execution failed.", lines)
        self.assertTrue(any("ZeroDivisionError" in line for line in lines))

    def test_bridge_error_displayed(self):
        lines = main.run_execute(
            "x = 1",
            sender=lambda code: (_ for _ in ()).throw(
                BridgeNotRunningError("bridge down")),
            confirm=lambda: True)
        self.assertIn("Blender bridge error: bridge down", lines)

    def test_empty_script_displayed(self):
        lines = main.run_execute("   ", sender=send_script, confirm=lambda: True)
        self.assertTrue(any("empty script" in line for line in lines))

    def test_confirmation_is_asked(self):
        asked = []

        def confirm():
            asked.append(True)
            return True

        main.run_execute("x = 1", sender=lambda code: {"status": "SUCCESS"},
                         confirm=confirm)
        self.assertEqual(asked, [True])


if __name__ == "__main__":
    unittest.main()

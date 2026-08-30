"""Safe local end-to-end demonstration for the learned capability system."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

from .discovery import OpenAPIDiscoveryProvider
from .models import Capability, CapabilityState
from .service import CapabilityService
from .store import CapabilityStore


class _Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        value = self.path.rsplit("/", 1)[-1]
        body = json.dumps({"value": value, "verified": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run_demo() -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tools = {
                "file_search": lambda query: {"path": query},
                "produce": lambda value: {"value": value},
                "consume": lambda value: {"consumed": value},
            }
            service = CapabilityService(
                store=CapabilityStore(root / "store.json"),
                tools=tools,
                providers=[OpenAPIDiscoveryProvider()],
            )

            case1 = service.search("find a local file", limit=1)

            base = f"http://127.0.0.1:{server.server_port}"
            spec = {
                "openapi": "3.0.0", "servers": [{"url": base}],
                "paths": {"/values/{value}": {"get": {
                    "operationId": "read_value", "summary": "Read a test value",
                    "parameters": [{"name": "value", "in": "path", "required": True, "schema": {"type": "string"}}],
                }}},
            }
            discovered = service.discover_capabilities("read a value", context={"openapi": spec})
            capability_id = discovered[0]["id"]
            first = service.execute(capability_id, {"value": "first"}, confirm=True)
            second = service.execute(capability_id, {"value": "second"}, confirm=True)

            invalid = service.discover_capabilities("read a value", context={"openapi": spec})[0]
            failed = service.execute(invalid["id"], {}, confirm=True)

            stale_capability = Capability.temporary(
                "python_metadata", "inspect a local dependency",
                strategy={"kind": "cli", "executable": sys.executable, "argv": ["--version"], "allow_metadata_inspection": False},
                permissions=["process_execution"], fingerprint="obsolete",
            )
            stale_capability.state = CapabilityState.ACTIVE.value
            service.store.save_capability(stale_capability)
            stale = service.check_dependency(stale_capability.id)

            learned = service.learn_skill(
                "produce and consume", "two-step local workflow",
                inputs={"type": "object", "required": ["source"]},
                steps=[
                    {"id": "produce", "capability_id": "builtin:produce", "inputs": {"value": "{{inputs.source}}"}},
                    {"id": "consume", "capability_id": "builtin:consume", "inputs": {"value": "{{steps.produce.value}}"}},
                ],
                verification_steps=[{"actual": "{{steps.consume.consumed}}", "equals": "{{inputs.source}}"}],
                execution_trace=[{"success": True, "verified": True}, {"success": True, "verified": True}],
            )
            skill_run = service.execute_skill(learned["skill"]["id"], {"source": "reused"})

            return {
                "case_1_existing": {"retrieved": case1[0]["id"]},
                "case_2_discover_promote_reuse": {
                    "first_promoted": first["promoted"], "state": first["state"],
                    "second_promoted": second["promoted"], "second_verified": second["verified"],
                },
                "case_3_failed_validation": {"success": failed["success"], "promoted": failed.get("promoted", False)},
                "case_4_dependency_change": {"changed": stale["changed"], "state": stale["state"]},
                "case_5_skill_reuse": {"learned": learned["success"], "reused": skill_run["success"], "verified": skill_run.get("verified")},
            }
    finally:
        server.shutdown()
        thread.join(timeout=2)


def main() -> int:
    result = run_demo()
    print(json.dumps(result, indent=2))
    checks = [
        result["case_1_existing"]["retrieved"] == "builtin:file_search",
        result["case_2_discover_promote_reuse"]["first_promoted"],
        result["case_2_discover_promote_reuse"]["second_verified"],
        not result["case_3_failed_validation"]["success"],
        result["case_4_dependency_change"]["state"] == "stale",
        result["case_5_skill_reuse"]["verified"],
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

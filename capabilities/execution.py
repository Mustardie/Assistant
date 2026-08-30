from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from .models import Capability, Permission


@dataclass(frozen=True)
class ExecutionOutcome:
    success: bool
    data: Any = None
    error: str | None = None
    verified: bool = False
    verification: str = ""
    retryable: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success, "data": self.data, "error": self.error,
            "verified": self.verified, "verification": self.verification,
            "retryable": self.retryable,
        }


class PermissionPolicy:
    APPROVAL_REQUIRED = {
        Permission.MODIFY_LOCAL_FILES.value,
        Permission.PROCESS_EXECUTION.value,
        Permission.EXTERNAL_NETWORK.value,
        Permission.ACCOUNT_DATA.value,
        Permission.SEND_MESSAGES.value,
        Permission.MODIFY_REMOTE_DATA.value,
        Permission.DELETE.value,
        Permission.FINANCIAL.value,
        Permission.CREDENTIAL_ACCESS.value,
        Permission.SYSTEM_CHANGE.value,
    }

    def requires_confirmation(self, capability: Capability) -> bool:
        return bool(set(capability.permissions) & self.APPROVAL_REQUIRED)

    def authorize(self, capability: Capability, *, confirmed: bool) -> tuple[bool, str]:
        required = sorted(set(capability.permissions) & self.APPROVAL_REQUIRED)
        if required and not confirmed:
            return False, "Explicit confirmation required for: " + ", ".join(required)
        if capability.risk_level in {"high", "critical"} and not confirmed:
            return False, f"Explicit confirmation required for {capability.risk_level}-risk capability"
        return True, "authorized"


def _resolve(value: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_resolve(item, inputs) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, inputs) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    full = re.fullmatch(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", value)
    if full:
        key = full.group(1)
        if key not in inputs:
            raise ValueError(f"Missing capability input: {key}")
        return inputs[key]

    def replace(match):
        key = match.group(1)
        if key not in inputs:
            raise ValueError(f"Missing capability input: {key}")
        return str(inputs[key])
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replace, value)


def _json_path(value: Any, path: str) -> Any:
    current = value
    for part in (path or "").strip(".").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


class CapabilityExecutor:
    def __init__(
        self,
        *,
        tool_runner: Callable[[str, dict], tuple[bool, Any]] | None = None,
        connector_registry=None,
        credential_resolver: Callable[[str], str | None] | None = None,
        permission_policy: PermissionPolicy | None = None,
    ):
        self.tool_runner = tool_runner
        self.connector_registry = connector_registry
        self.credential_resolver = credential_resolver or self._environment_credential
        self.permission_policy = permission_policy or PermissionPolicy()

    @staticmethod
    def _environment_credential(reference: str) -> str | None:
        key = re.sub(r"[^A-Za-z0-9]+", "_", reference or "").upper().strip("_")
        return os.getenv(reference or "") or os.getenv(f"JARVIS_CREDENTIAL_{key}")

    @staticmethod
    def _validate_inputs(capability: Capability, inputs: dict) -> list[str]:
        schema = capability.inputs or {}
        required = schema.get("required") or []
        return [str(name) for name in required if inputs.get(name) in (None, "")]

    def validate(self, capability: Capability, inputs: dict | None = None) -> ExecutionOutcome:
        inputs = dict(inputs or {})
        missing = self._validate_inputs(capability, inputs)
        if missing:
            return ExecutionOutcome(False, error="Missing required inputs: " + ", ".join(missing), verification="input schema failed")
        strategy = capability.strategy or {}
        kind = strategy.get("kind")
        if kind == "api":
            parsed = urllib.parse.urlsplit(str(strategy.get("url") or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return ExecutionOutcome(False, error="API URL must use http or https", verification="strategy validation failed")
            if str(strategy.get("method", "GET")).upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
                return ExecutionOutcome(False, error="Unsupported HTTP method", verification="strategy validation failed")
            # Read-only GET operations can be safely test-called. Mutating
            # operations receive structural validation and are verified only
            # around their explicitly approved real execution.
            if str(strategy.get("method", "GET")).upper() == "GET" and not capability.auth.get("required"):
                outcome = self._execute_api(capability, inputs, validation=True)
                if not outcome.success:
                    return outcome
                verified, detail = self.verify(capability, outcome.data, inputs)
                return ExecutionOutcome(verified, outcome.data, None if verified else detail, verified, detail)
            return ExecutionOutcome(True, {"structurally_valid": True, "network_call_performed": False}, verified=True, verification="safe structural validation passed")
        if kind == "cli":
            executable = Path(str(strategy.get("executable") or ""))
            argv = strategy.get("argv")
            if not executable.is_file() or not isinstance(argv, list):
                return ExecutionOutcome(False, error="CLI executable or argv template is invalid", verification="strategy validation failed")
            try:
                _resolve(argv, inputs)
            except ValueError as exc:
                return ExecutionOutcome(False, error=str(exc), verification="argument template failed")
            return ExecutionOutcome(True, {"executable": str(executable), "exists": True}, verified=True, verification="CLI metadata and argument template validated")
        if kind == "browser_workflow":
            steps = strategy.get("steps")
            if not isinstance(steps, list) or not steps:
                return ExecutionOutcome(False, error="Browser workflow has no steps", verification="strategy validation failed")
            for step in steps:
                tool = str(step.get("tool") or "")
                arguments = step.get("arguments") or {}
                if not tool.startswith("browser_"):
                    return ExecutionOutcome(False, error=f"Browser workflow contains non-browser tool: {tool}", verification="allowlist failed")
                if any(key in arguments for key in ("x", "y", "coordinates")):
                    return ExecutionOutcome(False, error="Coordinate-based browser workflows are not reusable", verification="semantic selector requirement failed")
            return ExecutionOutcome(True, {"steps": len(steps)}, verified=True, verification="semantic browser workflow validated")
        if kind == "tool":
            if not self.tool_runner or not strategy.get("tool"):
                return ExecutionOutcome(False, error="Tool runner unavailable", verification="strategy validation failed")
            return ExecutionOutcome(True, {"tool": strategy["tool"]}, verified=True, verification="built-in tool reference validated")
        if kind == "connector":
            if not self.connector_registry:
                return ExecutionOutcome(False, error="Connector registry unavailable", verification="strategy validation failed")
            plan = self.connector_registry.plan_action if hasattr(self.connector_registry, "plan_action") else None
            return ExecutionOutcome(True, {"connector": strategy.get("connector")}, verified=True, verification="connector reference validated")
        return ExecutionOutcome(False, error=f"Unsupported capability strategy: {kind}", verification="strategy validation failed")

    def execute(self, capability: Capability, inputs: dict | None = None, *, confirmed: bool = False) -> ExecutionOutcome:
        inputs = dict(inputs or {})
        allowed, reason = self.permission_policy.authorize(capability, confirmed=confirmed)
        if not allowed:
            return ExecutionOutcome(False, error=reason, verification="permission denied")
        missing = self._validate_inputs(capability, inputs)
        if missing:
            return ExecutionOutcome(False, error="Missing required inputs: " + ", ".join(missing), verification="input schema failed")
        kind = capability.strategy.get("kind")
        try:
            if kind == "api":
                outcome = self._execute_api(capability, inputs)
            elif kind == "cli":
                outcome = self._execute_cli(capability, inputs)
            elif kind in {"tool", "browser_workflow"}:
                outcome = self._execute_tools(capability, inputs)
            elif kind == "connector":
                outcome = self._execute_connector(capability, inputs, confirmed=confirmed)
            else:
                return ExecutionOutcome(False, error=f"Unsupported strategy: {kind}", verification="execution blocked")
        except Exception as exc:
            return ExecutionOutcome(False, error=str(exc), verification="executor raised an error", retryable=False)
        if not outcome.success:
            return outcome
        verified, detail = self.verify(capability, outcome.data, inputs)
        return ExecutionOutcome(verified, outcome.data, None if verified else detail, verified, detail, not verified)

    def _execute_api(self, capability: Capability, inputs: dict, validation: bool = False) -> ExecutionOutcome:
        strategy = capability.strategy
        method = str(strategy.get("method", "GET")).upper()
        url = str(_resolve(strategy["url"], inputs))
        mappings = strategy.get("parameter_mappings") or {}
        query, headers, json_body = {}, {}, {}
        for key, mapping in mappings.items():
            if key not in inputs:
                continue
            location = mapping.get("in", "query")
            name = mapping.get("name", key)
            if location == "path":
                url = url.replace("{" + name + "}", urllib.parse.quote(str(inputs[key]), safe=""))
            elif location == "header":
                headers[name] = str(inputs[key])
            elif location in {"json", "body"}:
                json_body[name] = inputs[key]
            else:
                query[name] = inputs[key]
        if query:
            separator = "&" if "?" in url else "?"
            url += separator + urllib.parse.urlencode(query, doseq=True)
        auth = capability.auth or {}
        if auth.get("required"):
            secret = self.credential_resolver(str(auth.get("credential_ref") or ""))
            if not secret:
                return ExecutionOutcome(False, error=f"Credential required: {auth.get('credential_ref')}", verification="authentication unavailable")
            if auth.get("in") == "query":
                separator = "&" if "?" in url else "?"
                url += separator + urllib.parse.urlencode({auth.get("name", "key"): secret})
            else:
                prefix = f"{auth.get('scheme')} " if auth.get("scheme") else ""
                headers[str(auth.get("name") or "Authorization")] = prefix + secret
        body = json.dumps(json_body).encode("utf-8") if json_body else None
        if body:
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=float(strategy.get("timeout", 20))) as response:
                raw = response.read(int(strategy.get("max_response_bytes", 2_000_000)))
                content_type = response.headers.get("Content-Type", "")
                text = raw.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(text) if "json" in content_type or text[:1] in "[{" else text
                except json.JSONDecodeError:
                    payload = text
                return ExecutionOutcome(True, {"status": response.status, "body": payload, "url": url, "validation": validation})
        except urllib.error.HTTPError as exc:
            text = exc.read(4000).decode("utf-8", errors="replace")
            return ExecutionOutcome(False, {"status": exc.code, "body": text}, f"HTTP {exc.code}", verification="HTTP request failed", retryable=exc.code >= 500)

    def _execute_cli(self, capability: Capability, inputs: dict) -> ExecutionOutcome:
        strategy = capability.strategy
        executable = str(Path(strategy["executable"]).resolve())
        argv = [str(item) for item in _resolve(strategy.get("argv") or [], inputs)]
        completed = subprocess.run(
            [executable, *argv], capture_output=True, text=True,
            timeout=float(strategy.get("timeout", 30)), shell=False,
            stdin=subprocess.DEVNULL, cwd=strategy.get("cwd") or None,
        )
        data = {"exit_code": completed.returncode, "stdout": completed.stdout[-100_000:], "stderr": completed.stderr[-100_000:]}
        return ExecutionOutcome(completed.returncode == 0, data, None if completed.returncode == 0 else f"Process exited with {completed.returncode}")

    def _execute_tools(self, capability: Capability, inputs: dict) -> ExecutionOutcome:
        if not self.tool_runner:
            return ExecutionOutcome(False, error="Tool runner unavailable")
        strategy = capability.strategy
        steps = strategy.get("steps") if strategy.get("kind") == "browser_workflow" else [{
            "tool": strategy.get("tool"),
            "arguments": strategy["arguments"] if "arguments" in strategy else inputs,
        }]
        outputs = []
        for step in steps:
            arguments = _resolve(step.get("arguments") or {}, inputs)
            ok, value = self.tool_runner(str(step.get("tool")), arguments)
            outputs.append({"tool": step.get("tool"), "success": ok, "result": value})
            if not ok:
                return ExecutionOutcome(False, outputs, f"Step failed: {step.get('tool')}")
        return ExecutionOutcome(True, outputs[-1]["result"] if len(outputs) == 1 else outputs)

    def _execute_connector(self, capability: Capability, inputs: dict, *, confirmed: bool) -> ExecutionOutcome:
        if not self.connector_registry:
            return ExecutionOutcome(False, error="Connector registry unavailable")
        strategy = capability.strategy
        result = self.connector_registry.execute(strategy["connector"], strategy["capability"], inputs, confirmed=confirmed)
        value = result.to_dict() if hasattr(result, "to_dict") else result
        return ExecutionOutcome(bool(getattr(result, "success", False)), value, getattr(result, "error", None))

    def verify(self, capability: Capability, data: Any, inputs: dict) -> tuple[bool, str]:
        rule = capability.verification or {}
        kind = rule.get("kind")
        if kind == "http_response":
            status = data.get("status") if isinstance(data, dict) else None
            allowed = rule.get("status") or list(range(200, 300))
            if status not in allowed:
                return False, f"Unexpected HTTP status: {status}"
            if rule.get("json_path"):
                try:
                    actual = _json_path(data.get("body"), rule["json_path"])
                except (KeyError, IndexError, TypeError):
                    return False, f"Verification field missing: {rule['json_path']}"
                expected = _resolve(rule.get("equals"), inputs)
                if actual != expected:
                    return False, f"Verification mismatch at {rule['json_path']}"
            return True, f"HTTP status {status} and response rule verified"
        if kind == "exit_code":
            actual = data.get("exit_code") if isinstance(data, dict) else None
            expected = int(rule.get("equals", 0))
            return (actual == expected, f"exit code {actual}; expected {expected}")
        if kind == "file_exists":
            path = Path(str(_resolve(rule.get("path"), inputs)))
            if not path.exists():
                return False, f"Expected file does not exist: {path}"
            minimum = int(rule.get("minimum_bytes", 0))
            if path.is_file() and path.stat().st_size < minimum:
                return False, f"Expected file is smaller than {minimum} bytes"
            return True, f"Verified file exists: {path}"
        if kind == "json_path":
            try:
                actual = _json_path(data, rule.get("path", ""))
                expected = _resolve(rule.get("equals"), inputs)
                return (actual == expected, f"verified {rule.get('path')}" if actual == expected else "value mismatch")
            except (KeyError, IndexError, TypeError):
                return False, "verification path missing"
        if kind == "last_step_success":
            if isinstance(data, list) and data:
                return bool(data[-1].get("success")), "last browser step succeeded"
        # Generic fallback still requires concrete non-error evidence.
        if data in (None, "", [], {}):
            return False, "Execution returned no evidence"
        if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
            return False, str(data.get("error") or "Result reported failure")
        return True, "generic non-empty result verification passed"

    def dependency_fingerprint(self, capability: Capability) -> str:
        strategy = capability.strategy
        if strategy.get("kind") == "cli":
            executable = Path(str(strategy.get("executable") or ""))
            if not executable.is_file():
                return "missing"
            if strategy.get("allow_metadata_inspection"):
                version = ""
                for flag in ("--version", "-version"):
                    try:
                        result = subprocess.run([str(executable), flag], capture_output=True, text=True, timeout=3, shell=False, stdin=subprocess.DEVNULL)
                        version = (result.stdout or result.stderr or "").strip().splitlines()[0][:240]
                        if version:
                            break
                    except (OSError, subprocess.SubprocessError, IndexError):
                        continue
                identity = f"{executable.resolve()}|{version}"
            else:
                stat = executable.stat()
                identity = f"{executable.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
            return hashlib.sha256(identity.encode()).hexdigest()
        for dependency in capability.dependencies:
            if dependency.get("kind") == "openapi":
                source = Path(str(dependency.get("source") or ""))
                if source.is_file():
                    try:
                        parsed = json.loads(source.read_text(encoding="utf-8"))
                        return hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()
                    except Exception:
                        return "unreadable"
        return capability.fingerprint

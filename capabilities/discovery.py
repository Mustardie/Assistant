from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from .models import Capability, Permission
from .retrieval import CapabilityRetriever, tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilityNeed:
    description: str
    required_inputs: dict[str, Any] = field(default_factory=dict)
    expected_effect: str = "read_only"
    context: dict[str, Any] = field(default_factory=dict)


class DiscoveryProvider(ABC):
    name: str

    @abstractmethod
    def discover(self, need: CapabilityNeed) -> list[Capability]:
        raise NotImplementedError


def _safe_doc_text(value: Any, limit: int = 220) -> str:
    """Keep documentation as inert reference text, never instructions."""
    text = re.sub(r"[\x00-\x1f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    # Common prompt-injection phrases add no operation semantics. Removing
    # them also keeps them out of planner context and learned records.
    if re.search(r"(?i)\b(ignore|disregard|override)\b.{0,100}\b(instruction|prompt|system|developer)\w*\b", text):
        text = re.sub(
            r"(?i)\b(ignore|disregard|override)\b[^;.!?]{0,300}[;.!?]?",
            "[untrusted instruction removed] ",
            text,
            count=1,
        )
    return text[:limit]


class BuiltinToolProvider(DiscoveryProvider):
    name = "existing_tools"

    def __init__(self, tools: dict[str, Callable], retriever: CapabilityRetriever | None = None):
        self.tools = tools
        self.retriever = retriever or CapabilityRetriever()

    def catalog(self) -> list[dict]:
        result = []
        for name, function in self.tools.items():
            try:
                signature = str(inspect.signature(function))
            except (TypeError, ValueError):
                signature = "()"
            description = _safe_doc_text(inspect.getdoc(function) or name.replace("_", " "))
            result.append({
                "id": f"builtin:{name}", "name": name, "description": description,
                "semantic_text": f"{name.replace('_', ' ')} {description}",
                "kind": "builtin", "state": "active", "reliability_score": 0.95,
                "strategy": {"kind": "tool", "tool": name}, "signature": signature,
            })
        return result

    def discover(self, need: CapabilityNeed) -> list[Capability]:
        # Built-ins are virtual trusted records and are not promoted into the
        # learned store. Search uses catalog() directly.
        return []


class ConnectorProvider(DiscoveryProvider):
    name = "connectors"

    def __init__(self, registry):
        self.registry = registry

    def catalog(self) -> list[dict]:
        result = []
        for connector_name in self.registry.names():
            status = self.registry.status(connector_name).value
            for operation in self.registry.capabilities(connector_name):
                name = operation.get("name") or operation.get("id")
                description = _safe_doc_text(operation.get("description"))
                result.append({
                    "id": f"connector:{connector_name}:{name}",
                    "name": f"{connector_name}.{name}",
                    "description": description,
                    "semantic_text": f"{connector_name} {name} {description}",
                    "kind": "connector",
                    "state": "active" if status in {"ready", "degraded"} else "disabled",
                    "reliability_score": 0.9 if status == "ready" else 0.6,
                    "permissions": ["account_data"] if operation.get("requires_auth") else [],
                    "strategy": {"kind": "connector", "connector": connector_name, "capability": name},
                    "input_schema": operation.get("input_schema") or {},
                })
        return result

    def discover(self, need: CapabilityNeed) -> list[Capability]:
        return []


class OpenAPIDiscoveryProvider(DiscoveryProvider):
    name = "openapi"

    _METHODS = {"get", "post", "put", "patch", "delete"}

    def discover(self, need: CapabilityNeed) -> list[Capability]:
        spec = need.context.get("openapi")
        source = need.context.get("openapi_source", "provided")
        if isinstance(spec, (str, Path)):
            path = Path(spec)
            if not path.is_file():
                return []
            raw = path.read_text(encoding="utf-8")
            spec = json.loads(raw)
            source = str(path.resolve())
        if not isinstance(spec, dict):
            return []

        servers = spec.get("servers") or []
        base_url = need.context.get("base_url") or (servers[0].get("url") if servers and isinstance(servers[0], dict) else "")
        if not base_url:
            return []
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
        candidates: list[Capability] = []
        for path, path_item in (spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() not in self._METHODS or not isinstance(operation, dict):
                    continue
                operation_id = str(operation.get("operationId") or f"{method}_{path}").strip()
                summary = _safe_doc_text(operation.get("summary") or operation_id.replace("_", " "))
                parameters = list(path_item.get("parameters") or []) + list(operation.get("parameters") or [])
                properties, required = {}, []
                mappings = {}
                for parameter in parameters:
                    if not isinstance(parameter, dict) or not parameter.get("name"):
                        continue
                    key = str(parameter["name"])
                    properties[key] = parameter.get("schema") or {"type": "string"}
                    if parameter.get("required"):
                        required.append(key)
                    mappings[key] = {"in": parameter.get("in", "query"), "name": key}
                request_body = operation.get("requestBody") or {}
                body_schema = (((request_body.get("content") or {}).get("application/json") or {}).get("schema") or {})
                for key, value in (body_schema.get("properties") or {}).items():
                    properties.setdefault(key, value)
                    mappings.setdefault(key, {"in": "json", "name": key})
                required.extend(x for x in body_schema.get("required", []) if x not in required)
                security = operation.get("security", spec.get("security", []))
                auth = {}
                permissions = [Permission.EXTERNAL_NETWORK.value]
                if security:
                    scheme_name = next(iter(security[0]), "api") if isinstance(security, list) and security and isinstance(security[0], dict) else "api"
                    scheme = ((spec.get("components") or {}).get("securitySchemes") or {}).get(scheme_name, {})
                    auth_type = scheme.get("type", "apiKey")
                    auth = {
                        "required": True,
                        "credential_ref": need.context.get("credential_ref") or scheme_name,
                        "type": auth_type,
                        "in": scheme.get("in", "header"),
                        "name": scheme.get("name", "Authorization"),
                        "scheme": scheme.get("scheme") or ("Bearer" if auth_type in {"oauth2", "openIdConnect"} else ""),
                    }
                    permissions.extend([Permission.ACCOUNT_DATA.value, Permission.CREDENTIAL_ACCESS.value])
                if method.lower() != "get":
                    permissions.append(Permission.MODIFY_REMOTE_DATA.value)
                if method.lower() == "delete":
                    permissions.append(Permission.DELETE.value)
                responses = operation.get("responses") or {}
                success_response = next((value for code, value in responses.items() if str(code).startswith("2") and isinstance(value, dict)), {})
                output_schema = (((success_response.get("content") or {}).get("application/json") or {}).get("schema") or {})
                candidates.append(Capability.temporary(
                    operation_id,
                    summary,
                    semantic_text=f"{operation_id} {summary} {method} {path}",
                    inputs={"type": "object", "properties": properties, "required": required},
                    output_schema=output_schema,
                    strategy={
                        "kind": "api", "method": method.upper(), "url": urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
                        "parameter_mappings": mappings, "timeout": 20,
                    },
                    permissions=list(dict.fromkeys(permissions)), auth=auth,
                    discovery_source={"provider": self.name, "source": source, "operation_id": operation_id},
                    fingerprint=fingerprint,
                    dependencies=[{"kind": "openapi", "source": source, "fingerprint": fingerprint}],
                    verification={"kind": "http_response", "status": list(range(200, 300))},
                    risk_level="high" if method.lower() in {"delete", "post", "put", "patch"} else "medium",
                ))
        return candidates


class LocalCLIProvider(DiscoveryProvider):
    name = "local_cli"

    def _paths(self, need: CapabilityNeed) -> list[Path]:
        explicit = need.context.get("executables") or []
        values = [Path(item) for item in explicit]
        # Conservative PATH lookup: only terms from the request, no full PATH
        # enumeration and no recursive machine scan.
        for word in tokens(need.description)[:12]:
            located = shutil.which(word)
            if located:
                values.append(Path(located))
        unique = []
        seen = set()
        for value in values:
            resolved = Path(value).expanduser().resolve()
            if resolved.is_file() and str(resolved).lower() not in seen:
                unique.append(resolved)
                seen.add(str(resolved).lower())
        return unique[:8]

    @staticmethod
    def _inspect(executable: Path) -> tuple[str, list[str]]:
        for flag in ("--version", "-version", "--help"):
            try:
                completed = subprocess.run(
                    [str(executable), flag], capture_output=True, text=True,
                    timeout=3, shell=False, stdin=subprocess.DEVNULL,
                )
                output = (completed.stdout or completed.stderr or "").strip()[:2000]
                if output:
                    first = output.splitlines()[0][:240]
                    return first, [flag]
            except (OSError, subprocess.SubprocessError):
                continue
        return "", []

    def discover(self, need: CapabilityNeed) -> list[Capability]:
        operations = need.context.get("cli_operations") or []
        by_executable = {str(Path(item.get("executable", "")).expanduser().resolve()): item for item in operations if item.get("executable")}
        result = []
        for executable in self._paths(need):
            operation = by_executable.get(str(executable), {})
            argv = operation.get("argv")
            if not isinstance(argv, list):
                # Discovery has found a candidate interface, but there is not
                # enough structured evidence to synthesize an invocation.
                continue
            allow_inspection = bool(operation.get("allow_metadata_inspection", False))
            if allow_inspection:
                version, inspected_with = self._inspect(executable)
                identity = f"{executable}|{version}"
            else:
                stat = executable.stat()
                version, inspected_with = "", []
                identity = f"{executable}|{stat.st_size}|{stat.st_mtime_ns}"
            description = _safe_doc_text(operation.get("description") or f"Run {executable.name} for {need.description}")
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            result.append(Capability.temporary(
                operation.get("name") or executable.stem,
                description,
                semantic_text=f"{executable.stem} {description} {version}",
                inputs=operation.get("inputs") or {"type": "object", "properties": {}},
                strategy={
                    "kind": "cli", "executable": str(executable), "argv": argv,
                    "timeout": int(operation.get("timeout", 30)),
                    "allow_metadata_inspection": allow_inspection,
                },
                permissions=list(dict.fromkeys([Permission.PROCESS_EXECUTION.value, *operation.get("permissions", [])])),
                discovery_source={"provider": self.name, "inspected_with": inspected_with, "version": version},
                dependencies=[{"kind": "executable", "path": str(executable), "version": version, "fingerprint": fingerprint}],
                fingerprint=fingerprint,
                verification=operation.get("verification") or {"kind": "exit_code", "equals": 0},
                risk_level=operation.get("risk_level", "medium"),
            ))
        return result


class BrowserFallbackProvider(DiscoveryProvider):
    name = "browser_fallback"

    def discover(self, need: CapabilityNeed) -> list[Capability]:
        workflow = need.context.get("browser_workflow")
        if not isinstance(workflow, list) or not workflow:
            return []
        domains = sorted({urlsplit(str(step.get("url", ""))).hostname for step in workflow if step.get("url")})
        effect_permissions = {
            "local_file_modification": [Permission.MODIFY_LOCAL_FILES.value],
            "remote_account_write": [Permission.ACCOUNT_DATA.value, Permission.MODIFY_REMOTE_DATA.value],
            "send_message": [Permission.ACCOUNT_DATA.value, Permission.SEND_MESSAGES.value],
            "deletion": [Permission.DELETE.value],
            "financial": [Permission.FINANCIAL.value],
        }.get(need.expected_effect, [])
        return [Capability.temporary(
            need.context.get("name") or "browser_workflow",
            _safe_doc_text(need.description),
            strategy={"kind": "browser_workflow", "steps": workflow, "domains": [d for d in domains if d]},
            inputs=need.required_inputs,
            permissions=list(dict.fromkeys([
                Permission.EXTERNAL_NETWORK.value, *effect_permissions,
                *need.context.get("permissions", []),
            ])),
            discovery_source={"provider": self.name, "domains": domains},
            dependencies=[{"kind": "browser_domain", "domain": domain} for domain in domains if domain],
            verification=need.context.get("verification") or {"kind": "last_step_success"},
            risk_level=need.context.get("risk_level", "medium"),
        )]


class DiscoveryManager:
    def __init__(self, providers: list[DiscoveryProvider], retriever: CapabilityRetriever | None = None):
        self.providers = providers
        self.retriever = retriever or CapabilityRetriever()

    def discover(self, need: CapabilityNeed, *, limit: int = 8) -> list[Capability]:
        found: list[Capability] = []
        for provider in self.providers:
            try:
                candidates = provider.discover(need)
                for candidate in candidates:
                    if not candidate.discovery_source:
                        candidate.discovery_source = {"provider": provider.name}
                found.extend(candidates)
            except Exception:
                logger.exception("Capability discovery provider failed: %s", provider.name)
        ranked = self.retriever.rank(need.description, [
            {**item.to_dict(), "reliability_score": item.reliability.score, "kind": "temporary"}
            for item in found
        ], limit=limit)
        by_id = {item.id: item for item in found}
        return [by_id[item["id"]] for item in ranked]

"""The Python catalog and the ExtendScript host must not drift apart.

The two halves of this system are written in different languages and cannot
import each other, so nothing but a test stops someone adding a catalog
operation with no host implementation (which fails at runtime, inside
Premiere, on the user's machine) or removing a host op the engine still calls.

These tests read the JSX dispatcher table as text and check it against the
catalog. Cheap, and it catches the entire class of drift.
"""
from __future__ import annotations

import os
import re

import pytest

from premiere import catalog
from premiere.engine import EditEngine

HOST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "extensions", "PremiereBridge",
)
DISPATCHER = os.path.join(HOST_DIR, "host", "index.jsx")

#: Primitives the host implements for the engine's own use. They are
#: deliberately absent from the catalog: the editing model composes them
#: through higher-level operations instead of calling them directly.
INTERNAL_HOST_OPS = {
    "clip.resolve",          # selector -> real clips, used by expanders
    "clip.replace_source",   # backs text.set
    "overlay.place",         # backs text/shape/image/captions
    "project.path",          # backs checkpointing
    "project.open",          # backs restore
    "timeline.revision",     # backs revision protection
}

#: Implemented on the Node side of the panel (main.js), not in ExtendScript,
#: because they copy project files.
NODE_OPS = {"project.checkpoint", "project.restore"}


def host_ops() -> set:
    source = open(DISPATCHER, encoding="utf-8").read()
    return set(re.findall(r"^\s*'([a-z_]+\.[a-z_]+)':", source, re.M))


def expanded_ops() -> set:
    return {
        name for name in catalog.OPS
        if getattr(EditEngine, f"_expand_{name.replace('.', '_')}", None)
    }


def passthrough_ops() -> set:
    return set(catalog.OPS) - expanded_ops()


class TestCatalogHostContract:
    def test_every_passthrough_op_has_a_host_implementation(self):
        """A catalog op with no expander goes straight to the host verbatim."""
        unsupported = {name for name, spec in catalog.OPS.items()
                       if not spec.supported}
        missing = passthrough_ops() - host_ops() - unsupported - NODE_OPS
        assert not missing, (
            f"these operations would fail inside Premiere at runtime: "
            f"{sorted(missing)}"
        )

    def test_unsupported_ops_are_absent_from_the_host(self):
        """Ops marked unsupported must not be silently implemented anyway."""
        unsupported = {name for name, spec in catalog.OPS.items()
                       if not spec.supported}
        implemented = unsupported & host_ops()
        assert not implemented, (
            f"{sorted(implemented)} are marked unsupported in the catalog but "
            f"implemented in the host -- one of the two is lying"
        )

    def test_host_ops_are_either_catalogued_or_declared_internal(self):
        extra = host_ops() - set(catalog.OPS) - INTERNAL_HOST_OPS
        assert not extra, (
            f"host implements {sorted(extra)} with no catalog entry and no "
            f"internal declaration -- either expose it or document it here"
        )

    def test_internal_host_ops_all_exist(self):
        missing = INTERNAL_HOST_OPS - host_ops()
        assert not missing, (
            f"the engine relies on {sorted(missing)} but the host no longer "
            f"implements them"
        )

    def test_engine_only_calls_ops_the_host_knows(self):
        """Every bridge.call("...") in the engine must name a real host op."""
        import premiere.engine as engine_module
        engine_source = open(engine_module.__file__, encoding="utf-8").read()
        called = set(re.findall(r'bridge\.call\(\s*"([a-z_]+\.[a-z_]+)"',
                                engine_source))
        known = host_ops() | NODE_OPS
        unknown = called - known
        assert not unknown, (
            f"engine calls host operations that do not exist: {sorted(unknown)}"
        )


class TestPanelIntegrity:
    def test_every_host_module_is_included(self):
        source = open(DISPATCHER, encoding="utf-8").read()
        included = set(re.findall(r'#include\s+"([^"]+)"', source))
        modules_dir = os.path.join(HOST_DIR, "host", "modules")
        for name in os.listdir(modules_dir):
            if name.endswith(".jsx"):
                assert f"modules/{name}" in included, (
                    f"{name} exists but index.jsx never includes it")

    def test_json_polyfill_is_included_first(self):
        """Every module calls JSON.stringify; the polyfill must precede them."""
        source = open(DISPATCHER, encoding="utf-8").read()
        includes = re.findall(r'#include\s+"([^"]+)"', source)
        assert includes[0] == "lib/json.jsx"
        assert includes[1] == "lib/util.jsx"

    def test_manifest_enables_nodejs(self):
        """Without --enable-nodejs the panel loads but cannot open a socket."""
        manifest = open(os.path.join(HOST_DIR, "CSXS", "manifest.xml"),
                        encoding="utf-8").read()
        assert "--enable-nodejs" in manifest

    def test_manifest_is_well_formed_xml(self):
        """The single most expensive bug this suite can catch.

        CEP parses every manifest it finds, discards a malformed one with a
        line in a log file nobody reads, and carries on. From inside Premiere
        that is indistinguishable from the extension never having been
        installed: no error, no menu entry, no panel. This project shipped a
        manifest whose header comment contained a double hyphen -- illegal
        inside an XML comment -- and as a result the panel had never once
        loaded, which meant the entire Premiere integration had never been
        executed against a real host.
        """
        import xml.parsers.expat

        path = os.path.join(HOST_DIR, "CSXS", "manifest.xml")
        parser = xml.parsers.expat.ParserCreate()
        with open(path, "rb") as handle:
            parser.ParseFile(handle)

    def test_the_installer_refuses_a_malformed_manifest(self, tmp_path):
        """And the installer has to notice, not just the test suite."""
        from premiere import install as installer

        broken = tmp_path / "manifest.xml"
        broken.write_text(
            "\n".join([
                "<?xml version='1.0'?>",
                # A double hyphen, which XML forbids inside a comment. This is
                # the exact shape of the bug: a comment about a command-line
                # switch.
                "<!-- needs the " + "--" + "enable-nodejs parameter -->",
                "<Root/>",
            ]),
            encoding="utf-8",
        )
        outcome = installer.validate_manifest(str(broken))
        assert not outcome["ok"]
        assert "well-formed" in outcome["error"]

    def test_the_panel_starts_itself(self):
        """The bridge server only exists while the panel is loaded.

        Without a StartOn event somebody has to open Window > Extensions by
        hand before anything in this system can reach Premiere, which makes an
        unattended end-to-end edit impossible.
        """
        manifest = open(os.path.join(HOST_DIR, "CSXS", "manifest.xml"),
                        encoding="utf-8").read()
        assert "<StartOn>" in manifest
        assert "ApplicationActivate" in manifest

    def test_manifest_and_bridge_agree_on_the_port(self):
        from premiere.bridge import DEFAULT_PORT
        main_js = open(os.path.join(HOST_DIR, "main.js"), encoding="utf-8").read()
        port = re.search(r"var PORT\s*=\s*(\d+)", main_js)
        assert port, "main.js has no PORT"
        assert int(port.group(1)) == DEFAULT_PORT, (
            "the panel and the Python client disagree about the port")

    def test_host_scripts_avoid_es5_only_syntax(self):
        """ExtendScript is ES3; const/let/arrows/Object.keys break it."""
        banned = re.compile(
            r"(^|[^\w.])(const|let)\s+\w|=>|Object\.keys|Array\.isArray"
            r"|\.forEach\(|\.map\(")
        offenders = []
        for root, _dirs, files in os.walk(os.path.join(HOST_DIR, "host")):
            for name in files:
                if not name.endswith(".jsx") or name == "json.jsx":
                    continue
                path = os.path.join(root, name)
                for number, line in enumerate(open(path, encoding="utf-8"), 1):
                    stripped = line.strip()
                    if stripped.startswith("*") or stripped.startswith("//"):
                        continue
                    if banned.search(line):
                        offenders.append(f"{name}:{number}: {stripped[:70]}")
        assert not offenders, "ES5+ syntax in ExtendScript:\n" + "\n".join(offenders)

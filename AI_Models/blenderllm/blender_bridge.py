"""Client for the BlenderLLM Blender bridge (V0.4).

Sends Blender Python scripts to the Blender-side add-on
(blender_bridge_addon.py) over a local TCP connection on 127.0.0.1 and
parses the execution result.

Execution is always explicit: this module only ever sends code that the
user has already approved in the terminal (/execute + [y]). It never
executes anything itself.
"""

import json
import socket

from config import (
    BLENDER_BRIDGE_HOST,
    BLENDER_BRIDGE_PORT,
    BLENDER_BRIDGE_TIMEOUT,
)


class BlenderBridgeError(Exception):
    """Base class for all Blender bridge errors."""


class BridgeNotRunningError(BlenderBridgeError):
    """The Blender-side bridge is not accepting connections."""


class BridgeConnectionError(BlenderBridgeError):
    """The connection to the bridge failed."""


class BridgeTimeoutError(BlenderBridgeError):
    """Blender did not answer in time."""


class BridgeProtocolError(BlenderBridgeError):
    """The bridge answered with something unexpected."""


def send_script(script, host=BLENDER_BRIDGE_HOST, port=BLENDER_BRIDGE_PORT,
                timeout=BLENDER_BRIDGE_TIMEOUT):
    """Send one script to Blender and return the result dict.

    Success result:
        {"status": "SUCCESS", "stdout": "...", "stderr": "..."}
    Failure result:
        {"status": "ERROR", "error_type": "...", "error": "...",
         "traceback": "..."}

    Raises a BlenderBridgeError subclass on transport problems.
    """
    if not script.strip():
        raise BridgeProtocolError("empty script")

    payload = (json.dumps({"script": script}) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.sendall(payload)
            line = conn.makefile("r", encoding="utf-8").readline()
    except socket.timeout as exc:
        raise BridgeTimeoutError(
            f"no response from the Blender bridge within {timeout} seconds"
        ) from exc
    except ConnectionRefusedError as exc:
        raise BridgeNotRunningError(
            f"cannot reach the Blender bridge at {host}:{port}. "
            "Is Blender open and the bridge started? "
            "(F3 -> 'Start BlenderLLM Bridge')"
        ) from exc
    except OSError as exc:
        raise BridgeConnectionError(
            f"connection to the Blender bridge failed: {exc}"
        ) from exc

    if not line:
        raise BridgeProtocolError("empty response from the Blender bridge")
    try:
        result = json.loads(line)
    except json.JSONDecodeError as exc:
        raise BridgeProtocolError(
            f"malformed response from the Blender bridge: {line!r}"
        ) from exc
    if not isinstance(result, dict) or result.get("status") not in ("SUCCESS", "ERROR"):
        raise BridgeProtocolError(
            f"unexpected response from the Blender bridge: {line!r}"
        )
    return result

"""BlenderLLM Bridge - V0.4 execution bridge add-on for Blender.

Lets the BlenderLLM terminal application send Blender Python scripts to
this running Blender instance and execute them inside Blender.

SAFETY:
- Listens ONLY on 127.0.0.1 (localhost). No remote machines can connect.
- Does nothing until you explicitly start the bridge AND a script arrives.
- The BlenderLLM terminal app requires an explicit user confirmation
  (/execute + [y]) before sending any script.
- This add-on only runs scripts the user sent; it cannot run shell
  commands or touch the filesystem on its own.

INSTALL (Blender 5.2):
1. Edit > Preferences > Add-ons > Install... -> select this file.
2. Enable the "BlenderLLM Bridge" add-on.
3. Press F3, search "Start BlenderLLM Bridge", press Enter.
   (The console prints the listening address.)
4. To stop: F3 -> "Stop BlenderLLM Bridge".

Targets Blender 5.x (tested against 5.2).
"""

import contextlib
import io
import json
import queue
import socket
import threading
import traceback

import bpy
from bpy.types import Operator

bl_info = {
    "name": "BlenderLLM Bridge",
    "author": "BlenderLLM",
    "version": (0, 4, 0),
    "blender": (5, 0, 0),
    "category": "Development",
}

HOST = "127.0.0.1"
PORT = 41987

_running = False
_listener_thread = None
_pending = queue.Queue()


def _send_response(conn, payload):
    """Send a JSON result line and close the connection."""
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _run_script(script):
    """Execute the script and return (stdout, stderr) captured output."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    namespace = {"bpy": bpy, "__name__": "__main__"}
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exec(compile(script, "<blenderllm>", "exec"), namespace)
    return stdout_buf.getvalue(), stderr_buf.getvalue()


def _handle_connection(conn):
    """Read one request line, then hand it to the main-thread timer."""
    try:
        line = conn.makefile("r", encoding="utf-8").readline()
    except OSError:
        conn.close()
        return
    if not line.strip():
        conn.close()
        return
    try:
        request = json.loads(line)
        script = request["script"]
        if not isinstance(script, str):
            raise ValueError("script must be a string")
    except (ValueError, KeyError):
        _send_response(conn, {
            "status": "ERROR",
            "error_type": "ProtocolError",
            "error": "request must be JSON with a 'script' string field",
            "traceback": "",
        })
        return
    _pending.put((conn, script))


def _timer_callback():
    """Runs on Blender's main thread; executes queued scripts.

    Blender data must only be touched from the main thread, so the
    socket thread only queues work and this timer does the execution.
    """
    while not _pending.empty():
        conn, script = _pending.get_nowait()
        try:
            stdout, stderr = _run_script(script)
            _send_response(conn, {
                "status": "SUCCESS",
                "stdout": stdout,
                "stderr": stderr,
            })
        except BaseException as exc:
            _send_response(conn, {
                "status": "ERROR",
                "error_type": "BlenderExecutionError",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
    return 0.05


def start_bridge():
    """Start the listener thread and the main-thread timer."""
    global _listener_thread, _running
    if _running:
        return "already running"

    _running = True

    def listen():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((HOST, PORT))
            server.listen(4)
            server.settimeout(0.5)
            while _running:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=_handle_connection, args=(conn,), daemon=True
                ).start()

    _listener_thread = threading.Thread(target=listen, daemon=True)
    _listener_thread.start()
    bpy.app.timers.register(_timer_callback, first_interval=0.05)
    return "listening"


def stop_bridge():
    """Stop the listener and the timer."""
    global _running
    _running = False
    try:
        bpy.app.timers.unregister(_timer_callback)
    except ValueError:
        pass
    if _listener_thread is not None and _listener_thread.is_alive():
        _listener_thread.join(timeout=2)


class StartBridgeOperator(Operator):
    bl_idname = "blenderllm.start_bridge"
    bl_label = "Start BlenderLLM Bridge"
    bl_description = "Start the local BlenderLLM execution bridge"

    def execute(self, context):
        message = start_bridge()
        if message == "already running":
            self.report({"INFO"}, "BlenderLLM Bridge is already running.")
        else:
            self.report({"INFO"},
                        f"BlenderLLM Bridge listening on {HOST}:{PORT}")
        return {"FINISHED"}


class StopBridgeOperator(Operator):
    bl_idname = "blenderllm.stop_bridge"
    bl_label = "Stop BlenderLLM Bridge"
    bl_description = "Stop the local BlenderLLM execution bridge"

    def execute(self, context):
        stop_bridge()
        self.report({"INFO"}, "BlenderLLM Bridge stopped.")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(StartBridgeOperator)
    bpy.utils.register_class(StopBridgeOperator)


def unregister():
    stop_bridge()
    bpy.utils.unregister_class(StartBridgeOperator)
    bpy.utils.unregister_class(StopBridgeOperator)

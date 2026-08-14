"""BlenderLLM V0.4 - terminal chat with a local Ollama model.

Run with:  python main.py
"""

from blender_bridge import BlenderBridgeError, send_script
from config import KNOWLEDGE_ENABLED, MODEL, OLLAMA_HOST
from generation import extract_python_code, is_code_request
from knowledge import KnowledgeBase
from ollama_client import OllamaAPIError, OllamaClient, OllamaConnectionError, OllamaError
from system_prompt import CODE_FORMAT_GUIDANCE, SYSTEM_PROMPT

HELP_TEXT = """Commands:
  /exit       - exit BlenderLLM
  /clear      - reset the current conversation
  /knowledge  - show knowledge layer status and topics
  /execute    - execute the last generated Blender Python in Blender
                (asks for explicit confirmation)
  /help       - show this help"""


def check_connection(client):
    """Verify the server is reachable and the model exists. Exits otherwise."""
    print("BlenderLLM V0.4")
    print(f"Model: {MODEL}")
    try:
        models = client.list_models()
    except OllamaConnectionError as exc:
        print(f"Ollama: FAILED - {exc}")
        print("Make sure Ollama is running (start it or 'ollama serve'), then retry.")
        raise SystemExit(1)
    except OllamaAPIError as exc:
        print(f"Ollama: FAILED - {exc}")
        raise SystemExit(1)
    if MODEL not in models:
        print(f"Ollama: connected, but model '{MODEL}' is not installed.")
        print(f"Installed models: {', '.join(sorted(models))}")
        print(f"Install it with: ollama pull {MODEL}")
        raise SystemExit(1)
    print("Ollama: connected")


def handle_command(command, history, session):
    """Handle a slash-command. Returns False when the user wants to exit."""
    command = command.lower()
    if command == "/exit":
        return False
    if command == "/clear":
        history.clear()
        session["last_code"] = None
        print("Conversation cleared.")
    elif command == "/help":
        print(HELP_TEXT)
    else:
        print(f"Unknown command: {command}")
        print(HELP_TEXT)
    return True


def _default_confirm():
    answer = input("Execute the last generated script? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def run_execute(code, sender=send_script, confirm=None):
    """The explicit execution flow for one script.

    Prints the warning, asks for confirmation, sends the script to
    Blender, and returns a list of lines to print. `sender` and
    `confirm` are injectable for tests.
    """
    print("WARNING: This will execute Python inside Blender.")
    if confirm is None:
        confirm = _default_confirm
    if not confirm():
        return ["Execution cancelled."]
    try:
        result = sender(code)
    except BlenderBridgeError as exc:
        return [f"Blender bridge error: {exc}"]
    if result.get("status") == "SUCCESS":
        lines = ["Blender: execution successful."]
        stdout = (result.get("stdout") or "").strip()
        if stdout:
            lines.append(f"Output:\n{stdout}")
        return lines
    lines = ["Blender: execution failed."]
    if result.get("error_type"):
        lines.append(f"Error type: {result['error_type']}")
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    traceback_text = (result.get("traceback") or "").strip()
    if traceback_text:
        lines.append(f"Traceback:\n{traceback_text}")
    return lines


def build_messages(history, user_input, kb, code_request=False):
    """Assemble the message list sent to the model.

    Order: system instructions -> retrieved knowledge -> generation
    guidance (only for code requests) -> history -> current user request.
    The knowledge block is reference material and never replaces the
    system instructions.
    """
    system_content = SYSTEM_PROMPT
    if kb is not None:
        context = kb.select_context(user_input)
        if context:
            system_content += "\n\n" + context
    if code_request:
        system_content += "\n\n" + CODE_FORMAT_GUIDANCE
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_input})
    return messages


def print_knowledge_status(kb):
    if kb is None:
        print("Knowledge layer: disabled (KNOWLEDGE_ENABLED is False in config.py)")
    elif kb.loaded:
        print("Knowledge layer: enabled")
        print(f"Knowledge base: {kb.directory}")
        print(f"Topics ({len(kb.topics)}): {', '.join(kb.topics)}")
    else:
        print("Knowledge layer: disabled")
        if kb.error:
            print(f"Reason: {kb.error}")


def main():
    client = OllamaClient(host=OLLAMA_HOST)
    check_connection(client)

    kb = KnowledgeBase() if KNOWLEDGE_ENABLED else None
    if kb is not None and kb.loaded:
        print(f"Knowledge: {len(kb.topics)} topics loaded")
    else:
        print("Knowledge: disabled")
        if kb is not None and kb.error:
            print(f"  Reason: {kb.error}")

    print("Type /help for commands, /exit to quit.")

    history = []
    session = {"last_code": None}

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            command = user_input.lower()
            if command == "/knowledge":
                print_knowledge_status(kb)
                continue
            if command == "/execute":
                if session["last_code"] is None:
                    print("No generated code to execute. "
                          "Ask BlenderLLM to generate a script first.")
                else:
                    for line in run_execute(session["last_code"]):
                        print(line)
                continue
            if not handle_command(user_input, history, session):
                break
            continue

        messages = build_messages(history, user_input, kb,
                                  code_request=is_code_request(user_input))
        print("BlenderLLM: ", end="", flush=True)

        stream = client.chat(messages)
        reply_parts = []
        try:
            for part in stream:
                piece = part.message.content
                reply_parts.append(piece)
                print(piece, end="", flush=True)
            print()
        except KeyboardInterrupt:
            stream.close()
            print("\n[generation interrupted - partial reply discarded]")
            continue
        except OllamaError as exc:
            print()
            print(f"Error: {exc}")
            continue

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": "".join(reply_parts)})

        extracted = extract_python_code("".join(reply_parts))
        if extracted:
            session["last_code"] = extracted
            print("\n[generated code ready - type /execute to run it in Blender]")

    print("Goodbye!")


if __name__ == "__main__":
    try:
        main()
    except OllamaError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

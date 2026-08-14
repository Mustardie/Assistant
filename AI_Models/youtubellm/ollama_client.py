"""
Handles ALL communication with the local Ollama server.

This is the only file that talks to Ollama. The rest of the program
never touches Ollama directly, so later we could swap the model or
the backend without changing main.py.
"""

import ollama

# The model we chat with. Must be a model you already pulled in Ollama.
MODEL_NAME = "qwen3:8b"


def verify_setup():
    """
    Check that Ollama is running and that the model is installed.

    Raises an exception with a helpful message if either check fails.
    Returns True if everything is ready.
    """
    try:
        models = ollama.list().models
    except Exception as error:
        raise ConnectionError(
            "Could not reach Ollama.\n"
            "Start it first (run 'ollama serve', or open the Ollama app),\n"
            "then run this program again."
        ) from error

    installed = {model.model for model in models}
    if MODEL_NAME not in installed:
        raise LookupError(
            f"Model '{MODEL_NAME}' is not installed.\n"
            f"Download it with:  ollama pull {MODEL_NAME}"
        )

    return True


def send_messages(messages):
    """
    Send the full conversation history to the model.

    messages: a list of chat messages, for example:
        [{"role": "system", "content": "..."},
         {"role": "user", "content": "..."}]

    Returns the model's reply as a plain string.
    """
    response = ollama.chat(model=MODEL_NAME, messages=messages)
    return response.message.content

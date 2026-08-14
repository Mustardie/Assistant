"""Communication with the local Ollama server.

A thin wrapper around the official `ollama` Python package. It translates
raw Ollama/httpx exceptions into simple BlenderLLM errors so the rest of
the program never has to deal with library internals.
"""

import httpx
import ollama

from config import MODEL, OLLAMA_HOST

# Exceptions that mean "we could not talk to the Ollama server at all".
_CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


class OllamaError(Exception):
    """Base class for all BlenderLLM/Ollama errors."""


class OllamaConnectionError(OllamaError):
    """The Ollama server cannot be reached."""


class OllamaModelNotFoundError(OllamaError):
    """The configured model is not installed on the server."""


class OllamaAPIError(OllamaError):
    """The Ollama server responded with an unexpected error."""


class OllamaClient:
    def __init__(self, host=OLLAMA_HOST):
        self.host = host
        self._client = ollama.Client(host=host)

    def list_models(self):
        """Return the names of all models installed on the server."""
        try:
            response = self._client.list()
        except _CONNECTION_ERRORS as exc:
            raise OllamaConnectionError(
                f"cannot reach Ollama at {self.host}"
            ) from exc
        except (ollama.ResponseError, ollama.RequestError) as exc:
            raise OllamaAPIError(f"Ollama error: {exc}") from exc
        return [model["model"] for model in response["models"]]

    def chat(self, messages, model=MODEL):
        """Stream the model's reply for the given message list.

        Yields one ChatResponse chunk per piece of generated text.
        On failure raises a BlenderLLM error instead of a raw library error.
        """
        try:
            stream = self._client.chat(model=model, messages=messages, stream=True)
            for part in stream:
                yield part
        except _CONNECTION_ERRORS as exc:
            raise OllamaConnectionError(
                f"lost connection to Ollama at {self.host}"
            ) from exc
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                raise OllamaModelNotFoundError(
                    f"model '{model}' is not installed. "
                    f"Install it with: ollama pull {model}"
                ) from exc
            raise OllamaAPIError(f"Ollama error: {exc}") from exc
        except ollama.RequestError as exc:
            raise OllamaAPIError(f"Ollama error: {exc}") from exc

"""Tests for BlenderLLM V0.1. No network or Ollama server required.

Run with:  python -m unittest discover tests -v
"""

import unittest
from unittest import mock

import httpx
from ollama import RequestError, ResponseError

import config
from ollama_client import (
    OllamaAPIError,
    OllamaClient,
    OllamaConnectionError,
    OllamaModelNotFoundError,
)


class FakePart:
    """Mimics one streamed ChatResponse chunk from the ollama library."""

    def __init__(self, text):
        self.message = mock.Mock(content=text)


class FakeModel:
    """Mimics one entry of the model list response."""

    def __init__(self, name):
        self.model = name

    def __getitem__(self, key):
        return getattr(self, key)


class ConfigTests(unittest.TestCase):
    def test_default_model(self):
        self.assertEqual(config.MODEL, "qwen2.5-coder:14b")

    def test_default_host(self):
        self.assertEqual(config.OLLAMA_HOST, "http://localhost:11434")


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.client_patcher = mock.patch("ollama_client.ollama.Client")
        self.fake_client_class = self.client_patcher.start()
        self.fake_client = self.fake_client_class.return_value
        self.client = OllamaClient(host="http://fake:11434")

    def tearDown(self):
        self.client_patcher.stop()

    def test_chat_yields_all_streamed_pieces(self):
        self.fake_client.chat.return_value = iter(
            [FakePart("Hel"), FakePart("lo"), FakePart("!")]
        )
        result = "".join(
            part.message.content for part in self.client.chat([{"role": "user", "content": "hi"}])
        )
        self.assertEqual(result, "Hello!")

    def test_chat_sends_messages_to_model(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        list(self.client.chat(messages))
        self.fake_client.chat.assert_called_once_with(
            model=config.MODEL, messages=messages, stream=True
        )

    def test_chat_connection_error_is_translated(self):
        self.fake_client.chat.side_effect = httpx.ConnectError("boom")
        with self.assertRaises(OllamaConnectionError):
            list(self.client.chat([]))

    def test_chat_missing_model_error_is_translated(self):
        self.fake_client.chat.side_effect = ResponseError(
            status_code=404, error="model not found"
        )
        with self.assertRaises(OllamaModelNotFoundError):
            list(self.client.chat([]))

    def test_chat_api_error_is_translated(self):
        self.fake_client.chat.side_effect = ResponseError(
            status_code=500, error="internal server error"
        )
        with self.assertRaises(OllamaAPIError):
            list(self.client.chat([]))

    def test_chat_request_error_is_translated(self):
        self.fake_client.chat.side_effect = RequestError("bad request")
        with self.assertRaises(OllamaAPIError):
            list(self.client.chat([]))


class ListModelsTests(unittest.TestCase):
    def test_list_models_returns_names(self):
        with mock.patch("ollama_client.ollama.Client") as fake_client:
            fake_client.return_value.list.return_value = {
                "models": [FakeModel("qwen3-coder:30b"), FakeModel("qwen3:8b")]
            }
            client = OllamaClient(host="http://fake:11434")
            self.assertEqual(client.list_models(), ["qwen3-coder:30b", "qwen3:8b"])

    def test_list_models_connection_error_is_translated(self):
        with mock.patch("ollama_client.ollama.Client") as fake_client:
            fake_client.return_value.list.side_effect = httpx.ConnectTimeout("boom")
            client = OllamaClient(host="http://fake:11434")
            with self.assertRaises(OllamaConnectionError):
                client.list_models()

    def test_list_models_api_error_is_translated(self):
        with mock.patch("ollama_client.ollama.Client") as fake_client:
            fake_client.return_value.list.side_effect = ResponseError(
                status_code=500, error="internal server error"
            )
            client = OllamaClient(host="http://fake:11434")
            with self.assertRaises(OllamaAPIError):
                client.list_models()


if __name__ == "__main__":
    unittest.main()

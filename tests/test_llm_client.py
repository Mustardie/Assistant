import unittest
from unittest.mock import patch

from recommendation.llm_client import LLMClient


class LLMClientTests(unittest.TestCase):
    def test_complete_json_recovers_from_non_json_output(self):
        client = LLMClient(url="http://example.test", model="test-model")

        responses = [
            "Here is the ranking:\n{'ranked': []}",
            "Here is the ranking again:\n{'ranked': []}",
        ]

        with patch.object(client, "complete", side_effect=responses):
            payload = client.complete_json("system", "user")

        self.assertEqual(payload, {"ranked": []})


if __name__ == "__main__":
    unittest.main()

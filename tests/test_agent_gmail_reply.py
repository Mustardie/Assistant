import unittest
from unittest.mock import patch

from brain.agent import Agent


class AgentGmailReplyTests(unittest.TestCase):
    def test_gmail_reply_requests_bypass_planner(self):
        agent = Agent.__new__(Agent)
        agent.assistant = type("AssistantStub", (), {"speak": lambda self, text: None})()
        agent.brain = type("BrainStub", (), {})()

        with patch("brain.agent.run_tool") as mock_run_tool:
            mock_run_tool.side_effect = [
                (True, [{"message_id": "msg-123", "thread_id": "thread-123"}]),
                (True, "Reply preview\n\nSend this email?"),
            ]

            result = agent._handle_gmail_reply_request("reply to gheekster")

        self.assertEqual(result, "Reply preview\n\nSend this email?")
        self.assertEqual(mock_run_tool.call_count, 2)
        self.assertEqual(mock_run_tool.call_args_list[0].args[0], "gmail_search")
        self.assertEqual(mock_run_tool.call_args_list[1].args[0], "gmail_reply")


if __name__ == "__main__":
    unittest.main()

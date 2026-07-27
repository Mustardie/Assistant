import unittest
from unittest.mock import patch

from gmail.service import GmailService
from skills import gmail as gmail_module


class GmailSkillTests(unittest.TestCase):
    def test_reply_command_defaults_to_draft_preview(self):
        skill = gmail_module.GmailSkill()

        with patch.object(skill, "reader") as mock_reader, patch.object(skill, "replier") as mock_replier:
            mock_reader.get_recent_emails.return_value = [
                {
                    "sender": "someone@example.com",
                    "subject": "Test email",
                    "date": "",
                    "body": "Hello",
                    "message_id": "msg-123",
                    "thread_id": "thread-123",
                }
            ]
            mock_replier.reply_to_email.return_value = {
                "draft": True,
                "body": "Thanks for the update. I will send it tomorrow.",
                "thread_id": "thread-123",
                "message_id": "msg-123",
                "preview": "Draft reply prepared. Send this? (yes/no)",
            }

            result = skill.reply("Reply to my latest email", draft_mode=True)

            self.assertIn("Reply preview", result)
            self.assertIn("Send this email?", result)

    def test_reply_uses_email_body_for_llm_generation(self):
        skill = gmail_module.GmailSkill()

        with patch.object(skill, "reader") as mock_reader, patch(
            "gmail.replier.LLMClient"
        ) as mock_client, patch.object(skill.replier, "sender") as mock_sender:
            mock_reader.get_recent_emails.return_value = [
                {
                    "sender": "john@example.com",
                    "subject": "Project update",
                    "date": "",
                    "body": "Can you send the revised plan by Friday?",
                    "message_id": "msg-123",
                    "thread_id": "thread-123",
                    "quoted_history": "",
                }
            ]
            mock_client.return_value.complete.return_value = "Yes, I can send the revised plan by Friday."
            mock_sender.create_draft.return_value = {"id": "draft-1"}

            result = skill.reply("reply to John's email", draft_mode=True)

            self.assertIn("Reply preview", result)
            prompt = mock_client.return_value.complete.call_args.args[1]
            self.assertIn("Can you send the revised plan by Friday?", prompt)
            self.assertIn("john@example.com", prompt)

    def test_reply_uses_search_for_explicit_sender_reference(self):
        skill = gmail_module.GmailSkill()

        with patch.object(skill, "reader") as mock_reader, patch.object(skill, "replier") as mock_replier:
            mock_reader.search_emails.return_value = [
                {
                    "sender": "gheekster@example.com",
                    "subject": "Design review",
                    "date": "",
                    "body": "Please review the draft.",
                    "message_id": "msg-match",
                    "thread_id": "thread-match",
                    "snippet": "Please review the draft.",
                    "unread": True,
                    "labels": ["INBOX", "UNREAD"],
                }
            ]
            mock_reader.get_recent_emails.return_value = [
                {
                    "sender": "latest@example.com",
                    "subject": "Newest message",
                    "date": "",
                    "body": "This is the latest email.",
                    "message_id": "msg-latest",
                    "thread_id": "thread-latest",
                    "snippet": "This is the latest email.",
                    "unread": True,
                    "labels": ["INBOX", "UNREAD"],
                }
            ]
            mock_reader.get_email_by_id.return_value = mock_reader.search_emails.return_value[0]
            mock_replier.reply_to_email.return_value = {
                "draft": True,
                "body": "Thanks for the reminder.",
                "thread_id": "thread-match",
                "message_id": "msg-match",
                "preview": "Draft reply prepared. Send this? (yes/no)",
            }

            result = skill.reply("reply to gheekster")

            self.assertIn("Reply preview", result)
            self.assertIn("Send this email?", result)
            mock_reader.search_emails.assert_called_once_with("gheekster", limit=5)

    def test_reply_requires_confirmation_before_sending(self):
        skill = gmail_module.GmailSkill()

        with patch.object(skill, "search") as mock_search, patch.object(skill, "reader") as mock_reader, patch.object(skill.replier, "generate_reply", return_value="Hi there") as mock_generate, patch.object(skill.replier, "reply_to_email") as mock_reply_to_email:
            mock_search.return_value = [
                {
                    "sender": "gheekster@example.com",
                    "subject": "Design review",
                    "date": "",
                    "body": "Please review the draft.",
                    "message_id": "msg-match",
                    "thread_id": "thread-match",
                    "snippet": "Please review the draft.",
                    "unread": True,
                    "labels": ["INBOX", "UNREAD"],
                }
            ]
            mock_reader.get_email_by_id.return_value = mock_search.return_value[0]
            mock_reply_to_email.return_value = {"draft": False, "body": "Hi there", "thread_id": "thread-match", "message_id": "msg-match", "preview": "Reply sent."}

            preview = skill.reply("reply to gheekster")
            self.assertIn("Reply preview", preview)
            self.assertNotIn("Reply sent successfully.", preview)

            with patch.object(skill.service, "modify_message", return_value={}) as mock_modify:
                confirmation = skill.reply("send")
            self.assertIn("Reply sent successfully.", confirmation)
            self.assertEqual(mock_reply_to_email.call_count, 1)
            mock_generate.assert_called_once()
            mock_modify.assert_called_once_with("msg-match", remove_labels=["UNREAD"])

    def test_create_draft_uses_users_drafts_resource(self):
        service = GmailService()

        class FakeRequest:
            def __init__(self, callback):
                self.callback = callback

            def execute(self):
                return self.callback()

        class FakeDraftsResource:
            def __init__(self):
                self.calls = []

            def create(self, userId, body):
                self.calls.append((userId, body))
                return FakeRequest(lambda: {"id": "draft-1"})

        class FakeUsersResource:
            def __init__(self, drafts_resource):
                self._drafts_resource = drafts_resource

            def drafts(self):
                return self._drafts_resource

        class FakeService:
            def __init__(self, drafts_resource):
                self._drafts_resource = drafts_resource

            def users(self):
                return FakeUsersResource(self._drafts_resource)

        drafts_resource = FakeDraftsResource()
        service._service = FakeService(drafts_resource)

        result = service.create_draft({"raw": "hello"})

        self.assertEqual(result, {"id": "draft-1"})
        self.assertEqual(drafts_resource.calls[0][0], "me")
        self.assertEqual(drafts_resource.calls[0][1], {"raw": "hello"})


if __name__ == "__main__":
    unittest.main()

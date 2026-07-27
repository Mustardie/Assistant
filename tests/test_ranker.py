import unittest

from recommendation.models import MediaCandidate
from recommendation.ranker import Ranker


class RankerTests(unittest.TestCase):
    def test_ranker_accepts_list_payload_from_llm(self):
        class StubLLM:
            def complete_json(self, system_prompt, user_prompt):
                return [
                    {"id": "video-1", "rank": 1, "reason": "matches the request"}
                ]

        candidate = MediaCandidate(
            id="video-1",
            source="youtube",
            title="Funny Minecraft clip",
            creator="Test Creator",
            description="A funny Minecraft video",
            published_at=None,
            duration_seconds=120,
            view_count=1000,
            like_count=100,
            tags=["minecraft", "funny"],
            url="https://example.com/video",
        )

        ranker = Ranker(llm=StubLLM())
        ranked = ranker.rank("play a funny minecraft video", [candidate], top_n=1)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].candidate.id, "video-1")
        self.assertEqual(ranked[0].rank, 1)

    def test_ranker_recovers_from_markdown_list(self):
        class StubLLM:
            def complete_json(self, system_prompt, user_prompt):
                return {"ranked": []}

        candidate = MediaCandidate(
            id="video-1",
            source="youtube",
            title="Funny Minecraft clip",
            creator="Test Creator",
            description="A funny Minecraft video",
            published_at=None,
            duration_seconds=600,
            view_count=1000,
            like_count=100,
            tags=["minecraft", "funny"],
            url="https://example.com/video",
        )

        ranker = Ranker(llm=StubLLM())
        ranked = ranker.rank("play a funny minecraft video", [candidate], top_n=1)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].candidate.id, "video-1")


if __name__ == "__main__":
    unittest.main()

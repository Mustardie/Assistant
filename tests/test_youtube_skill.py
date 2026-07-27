import unittest
from types import SimpleNamespace
from unittest.mock import patch

from recommendation.errors import RecommendationConfigurationError
from skills import youtube as youtube_module


class YouTubeSkillTests(unittest.TestCase):
    def test_missing_api_key_raises_and_does_not_fallback(self):
        fake_settings = SimpleNamespace(
            youtube_api_key="",
            youtube_allow_browser_fallback=True,
        )

        with patch.object(youtube_module, "settings", fake_settings), patch.object(
            youtube_module.browser, "youtube_search"
        ) as mock_search, patch.object(
            youtube_module.browser, "youtube_play_first_result"
        ) as mock_play_first_result:
            skill = youtube_module.YouTubeSkill()

            with self.assertRaises(RecommendationConfigurationError):
                skill.recommend("Play a funny Minecraft video")

            mock_search.assert_not_called()
            mock_play_first_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()

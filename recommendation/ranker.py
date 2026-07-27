import datetime
import json
import logging
import re
import time
from typing import List, Optional

from config.settings import settings
from recommendation.llm_client import LLMClient
from recommendation.models import MediaCandidate, RankedItem
from recommendation.youtube_history import get_watch_history
from youtube_subscriptions import get_subscriptions

_SUBSCRIPTION_CACHE = None
_SUBSCRIPTION_CACHE_TIME = 0
_SUBSCRIPTION_CACHE_TTL = 300

_WATCH_HISTORY_CACHE = None
_WATCH_HISTORY_CACHE_TIME = 0
_WATCH_HISTORY_CACHE_TTL = 300


def _get_subscriptions_cached():
    global _SUBSCRIPTION_CACHE, _SUBSCRIPTION_CACHE_TIME
    now = time.time()
    if _SUBSCRIPTION_CACHE is None or now - _SUBSCRIPTION_CACHE_TIME > _SUBSCRIPTION_CACHE_TTL:
        try:
            _SUBSCRIPTION_CACHE = get_subscriptions()
        except Exception as exc:
            logger.warning("Could not load YouTube subscriptions for ranking: %s", exc)
            _SUBSCRIPTION_CACHE = set()
        _SUBSCRIPTION_CACHE_TIME = now
    return _SUBSCRIPTION_CACHE


def _get_watch_history_cached():
    global _WATCH_HISTORY_CACHE, _WATCH_HISTORY_CACHE_TIME
    now = time.time()
    if _WATCH_HISTORY_CACHE is None or now - _WATCH_HISTORY_CACHE_TIME > _WATCH_HISTORY_CACHE_TTL:
        _WATCH_HISTORY_CACHE = get_watch_history()
        _WATCH_HISTORY_CACHE_TIME = now
    return _WATCH_HISTORY_CACHE

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a recommendation ranking engine.

You receive a user request and a list of candidate videos with metadata.
Rank the best matches for the user's intent.

Ranking guidelines:
- Prefer English content. Strongly penalize non-English videos unless the user explicitly requests non-English.
- Avoid Shorts unless the user explicitly asks for Shorts or very short clips.
- Prefer videos published in the last 30 days, then 6 months, then 1 year.
- Penalize videos older than 2 years unless the user asks for classics or old content.
- Use title, description, tags, channel metadata, and video language to determine English relevance.
- Use subscriptions as a positive signal, but do not make it a hard requirement.
- Promote unfinished / resume-able watch history matches when they fit the request.
- Penalize videos already watched unless explicitly requested.
- Combine relevance, freshness, English score, subscription bonus, history, completion penalty, view count, like ratio, and channel quality.
- Do NOT rank purely by view count.
- Reject obvious short clips, Shorts titles, or vertical aspect ratio videos unless the request asks for Shorts.

Return ONLY valid JSON. No explanations, no markdown, no code fences.
Use this exact format:
{"ranked":[{"video_id":"video id","score":1.0,"reason":"short explanation"}]}
"""


class Ranker:
    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm or LLMClient()

    def _fallback_relevance_score(self, candidate: MediaCandidate, user_request: str) -> int:
        tokens = set(re.findall(r"[a-z0-9]+", user_request.lower()))
        if not tokens:
            return 0

        haystack = " ".join(
            [
                candidate.title,
                candidate.creator,
                candidate.description,
                " ".join(candidate.tags),
            ]
        ).lower()
        haystack_tokens = set(re.findall(r"[a-z0-9]+", haystack))
        return len(tokens & haystack_tokens)

    def _subscription_score(self, candidate: MediaCandidate, subscriptions: set[str]) -> float:
        if candidate.channel_id and candidate.channel_id in subscriptions:
            return 40.0
        return 0.0

    def _language_score(self, candidate: MediaCandidate, user_request: str) -> float:
        english_terms = ["english", "en", "youtube", "channel", "video", "tutorial"]
        if candidate.language and candidate.language.lower().startswith("en"):
            return 50.0

        title = (candidate.title or "").lower()
        description = (candidate.description or "").lower()
        tags = " ".join(candidate.tags).lower()
        channel = (candidate.creator or "").lower()

        english_hits = sum(
            1 for term in english_terms if term in title or term in description or term in tags or term in channel
        )

        if any(term in title for term in ["subtitles", "subtitle", "español", "japanese", "한국어", "हिंदी", "français"]):
            return -40.0

        if english_hits >= 2:
            return 25.0
        if english_hits == 1:
            return 10.0
        return 0.0

    def _freshness_score(self, candidate: MediaCandidate, user_request: str, explicit_old: bool = False) -> float:
        if not candidate.published_at:
            return 0.0

        published = None
        try:
            published = datetime.datetime.fromisoformat(candidate.published_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0

        age = (datetime.datetime.now(datetime.timezone.utc) - published).days
        if age <= 30:
            return 40.0
        if age <= 180:
            return 20.0
        if age <= 365:
            return 10.0
        if age <= 730:
            return -5.0
        return 0.0 if explicit_old else -30.0

    def _shorts_penalty(self, candidate: MediaCandidate, explicit_shorts: bool) -> float:
        title = (candidate.title or "").lower()
        tags = " ".join(candidate.tags).lower()
        aspect_ratio = candidate.thumbnail_aspect_ratio or 0.0

        penalty = 0.0
        if any(term in title for term in ["#shorts", "shorts"]):
            penalty += 80.0
        if any(term in title for term in ["short clip", "meme clip", "clip"]):
            penalty += 20.0
        if "shorts" in tags:
            penalty += 50.0
        if aspect_ratio and aspect_ratio < 0.7:
            penalty += 20.0

        if explicit_shorts:
            return 0.0
        return penalty

    def _duration_score(self, candidate: MediaCandidate) -> float:
        if not candidate.duration_seconds:
            return 0.0
        if candidate.duration_seconds < 300:
            return -((300 - candidate.duration_seconds) / 300)
        if candidate.duration_seconds >= 1800:
            return 1.0
        return 0.5

    def _like_ratio(self, candidate: MediaCandidate) -> float:
        if not candidate.view_count or not candidate.like_count:
            return 0.0
        ratio = candidate.like_count / max(1, candidate.view_count)
        return min(20.0, ratio * 20.0)

    def _channel_quality(self, candidate: MediaCandidate) -> float:
        views = candidate.view_count or 0
        if views >= 2_000_000:
            return 20.0
        if views >= 200_000:
            return 10.0
        if views >= 20_000:
            return 5.0
        return 0.0

    def _watch_history_score(self, candidate: MediaCandidate, history: dict, explicit_resume: bool) -> float:
        if candidate.id not in history:
            return 0.0
        return 50.0 if explicit_resume else 15.0

    def _watch_completion_penalty(self, candidate: MediaCandidate, history: dict, explicit_resume: bool) -> float:
        if candidate.id not in history:
            return 0.0
        return 0.0 if explicit_resume else -40.0

    def rank(
        self,
        user_request: str,
        candidates: List[MediaCandidate],
        top_n: Optional[int] = None,
    ) -> List[RankedItem]:
        top_n = top_n or settings.recommendation_top_n

        if not candidates:
            return []

        explicit_shorts = "shorts" in user_request.lower() or "short clip" in user_request.lower()
        explicit_old = any(term in user_request.lower() for term in ["old", "classic", "vintage", "retro"])
        explicit_resume = any(term in user_request.lower() for term in ["resume", "continue", "pick up", "unfinished"]) or "continue" in user_request.lower()

        filtered_candidates = [
            candidate
            for candidate in candidates
            if self._should_keep_candidate(candidate, explicit_shorts)
        ]

        if not filtered_candidates:
            logger.warning("No candidates survived filtering; using original candidates")
            filtered_candidates = candidates

        compact_candidates = [
            candidate.to_ranking_dict()
            for candidate in filtered_candidates
        ]

        prompt = f"""
User request:
{user_request}

Return the top {top_n} candidates ranked best to worst.

Candidates:
{json.dumps(compact_candidates, ensure_ascii=True)}
"""

        logger.info(
            "Ranking %s candidates for request: %s",
            len(filtered_candidates),
            user_request,
        )

        subscriptions = _get_subscriptions_cached()
        watch_history = _get_watch_history_cached()

        try:
            payload = self.llm.complete_json(SYSTEM_PROMPT, prompt)
        except Exception as exc:
            logger.warning(
                "Ranking LLM call failed (%s); falling back to deterministic ranking.",
                exc,
            )
            payload = {}

        ranked_payload = payload.get("ranked", []) if isinstance(payload, dict) else []

        if not isinstance(ranked_payload, list):
            logger.warning("Ranker received invalid ranked list payload; using deterministic fallback")
            ranked_payload = []

        ranked_items: List[RankedItem] = []
        by_id = {candidate.id: candidate for candidate in filtered_candidates}
        seen_ids = set()

        for entry in ranked_payload:
            if not isinstance(entry, dict):
                continue

            candidate_id = str(entry.get("video_id") or entry.get("id", "")).strip()
            if not candidate_id or candidate_id in seen_ids:
                continue

            candidate = by_id.get(candidate_id)
            if candidate is None:
                continue

            score_value = 0.0
            score_value += self._subscription_score(candidate, subscriptions)
            score_value += self._language_score(candidate, user_request)
            score_value += self._freshness_score(candidate, user_request, explicit_old)
            score_value += self._watch_history_score(candidate, watch_history, explicit_resume)
            score_value += self._watch_completion_penalty(candidate, watch_history, explicit_resume)
            score_value += self._duration_score(candidate) * 8
            score_value += self._fallback_relevance_score(candidate, user_request) * 3
            score_value += self._like_ratio(candidate) * 5
            score_value += self._channel_quality(candidate) * 2
            score_value -= self._shorts_penalty(candidate, explicit_shorts)

            score = entry.get("score")
            if score is not None:
                try:
                    score_value += float(score) * 0.5
                except (TypeError, ValueError):
                    pass

            reason_parts = [
                f"freshness={self._freshness_score(candidate, user_request, explicit_old)}",
                f"language={self._language_score(candidate, user_request)}",
                f"subscription={self._subscription_score(candidate, subscriptions)}",
                f"unfinished={self._watch_history_score(candidate, watch_history, explicit_resume)}",
                f"history_penalty={self._watch_completion_penalty(candidate, watch_history, explicit_resume)}",
                f"shorts_penalty={self._shorts_penalty(candidate, explicit_shorts)}",
                f"final_score={score_value}",
            ]
            reason = f"Ranked by personalized YouTube-style signals ({'; '.join(reason_parts)})"

            ranked_items.append(
                RankedItem(
                    candidate=candidate,
                    rank=0,
                    reason=reason,
                )
            )
            seen_ids.add(candidate_id)

        if not ranked_items:
            logger.warning(
                "RANKING FALLBACK USED: deterministic ranking based on metadata"
            )
            fallback = sorted(
                filtered_candidates,
                key=lambda candidate: (
                    -self._subscription_score(candidate, subscriptions),
                    -self._fallback_relevance_score(candidate, user_request),
                    -(candidate.view_count or 0),
                    -(candidate.like_count or 0),
                    -self._language_score(candidate, user_request),
                    -self._freshness_score(candidate, user_request),
                    self._duration_score(candidate),
                    candidate.title.lower(),
                ),
            )[:top_n]
            ranked_items = [
                RankedItem(
                    candidate=candidate,
                    rank=index + 1,
                    reason="Fallback ranking based on views, likes, and title relevance.",
                )
                for index, candidate in enumerate(fallback)
            ]
        else:
            ranked_items.sort(
                key=lambda item: -float(re.search(r"final_score=(-?\d+(?:\.\d+)?)", item.reason).group(1))
                if re.search(r"final_score=(-?\d+(?:\.\d+)?)", item.reason)
                else 0.0
            )
            for index, item in enumerate(ranked_items):
                item.rank = index + 1

        logger.debug("Ranking results count=%s", len(ranked_items))
        return ranked_items

    def _parse_markdown_rankings(self, prompt: str, candidates: List[MediaCandidate]) -> List[dict]:
        by_id = {candidate.id: candidate for candidate in candidates}
        by_title = {candidate.title.lower(): candidate for candidate in candidates if candidate.title}

        markdown_entries: List[dict] = []
        for line in prompt.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("1.") or stripped.startswith("2.") or stripped.startswith("3."):
                token = re.sub(r"^[-*\d.]+\s*", "", stripped).strip()
                if not token:
                    continue
                token = token.strip("`\"'")
                candidate = by_id.get(token) or by_title.get(token.lower())
                if candidate is not None:
                    markdown_entries.append(
                        {
                            "video_id": candidate.id,
                            "score": 1.0,
                            "reason": "Recovered from markdown response",
                        }
                    )
        return markdown_entries

    def _should_keep_candidate(self, candidate: MediaCandidate, explicit_shorts: bool) -> bool:
        if not candidate.duration_seconds:
            return False
        if candidate.duration_seconds < 300 and not explicit_shorts:
            return False

        title = (candidate.title or "").lower()
        blocked_terms = [
            "#shorts",
            "shorts",
            "short clip",
            "meme clip",
            "clip",
        ]
        if any(term in title for term in blocked_terms) and not explicit_shorts:
            return False

        if candidate.thumbnail_aspect_ratio and candidate.thumbnail_aspect_ratio < 0.7 and not explicit_shorts:
            return False

        return True
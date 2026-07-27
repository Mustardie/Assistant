from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MediaCandidate:
    id: str
    source: str
    title: str
    creator: str
    description: str
    published_at: Optional[str]
    duration_seconds: Optional[int]
    view_count: Optional[int]
    like_count: Optional[int]
    tags: List[str]
    url: str
    language: str = ""
    thumbnail_aspect_ratio: Optional[float] = None
    category_id: str = ""
    matched_query: str = ""
    channel_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_ranking_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "creator": self.creator,
            "description": self.description[:300],
            "published_at": self.published_at,
            "duration_seconds": self.duration_seconds,
            "view_count": self.view_count,
            "like_count": self.like_count,
            "tags": self.tags[:10],
            "url": self.url,
            "language": self.language,
            "thumbnail_aspect_ratio": self.thumbnail_aspect_ratio,
            "category_id": self.category_id,
            "matched_query": self.matched_query,
            "channel_id": self.channel_id,
        }


@dataclass
class RankedItem:
    candidate: MediaCandidate
    rank: int
    reason: str


@dataclass
class RecommendationResult:
    user_request: str
    search_queries: List[str]
    candidate_count: int
    ranked: List[RankedItem]
    top_pick: Optional[MediaCandidate] = None
    played: bool = False
    play_message: str = ""

    def summary(self) -> str:
        lines = [
            f"Request: {self.user_request}",
            f"Generated {len(self.search_queries)} search queries.",
            f"Evaluated {self.candidate_count} unique candidates.",
            "",
            "Top recommendations:",
        ]

        for item in self.ranked:
            candidate = item.candidate
            lines.append(
                f"{item.rank}. {candidate.title} — {candidate.creator}"
            )
            lines.append(f"   {candidate.url}")
            lines.append(f"   Reason: {item.reason}")

        if self.top_pick and self.played:
            lines.extend(
                [
                    "",
                    f"Now playing: {self.top_pick.title}",
                    self.play_message,
                ]
            )

        return "\n".join(lines)

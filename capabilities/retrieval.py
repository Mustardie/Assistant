from __future__ import annotations

import math
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Protocol


_STOP = {"a", "an", "the", "to", "for", "of", "in", "on", "and", "or", "my", "me", "i", "please", "with"}


def tokens(value: str) -> list[str]:
    return [item for item in re.findall(r"[a-z0-9_]+", (value or "").lower()) if item not in _STOP]


class EmbeddingBackend(Protocol):
    def similarity(self, query: str, documents: list[str]) -> list[float]: ...


class LightweightSemanticBackend:
    """Provider-free TF/cosine retrieval with a fuzzy phrase signal."""

    def similarity(self, query: str, documents: list[str]) -> list[float]:
        q = Counter(tokens(query))
        scores = []
        for document in documents:
            d = Counter(tokens(document))
            dot = sum(q[word] * d[word] for word in q)
            denom = math.sqrt(sum(v * v for v in q.values()) * sum(v * v for v in d.values()))
            cosine = dot / denom if denom else 0.0
            fuzzy = SequenceMatcher(None, " ".join(q), " ".join(d)).ratio()
            scores.append(round(cosine * 0.8 + fuzzy * 0.2, 4))
        return scores


class CapabilityRetriever:
    def __init__(self, backend: EmbeddingBackend | None = None):
        self.backend = backend or LightweightSemanticBackend()

    def rank(self, query: str, candidates: list[dict], *, limit: int = 6) -> list[dict]:
        if not candidates:
            return []
        documents = [str(item.get("semantic_text") or item.get("description") or item.get("name") or "") for item in candidates]
        semantic_scores = self.backend.similarity(query, documents)
        ranked = []
        for item, semantic in zip(candidates, semantic_scores):
            reliability = float(item.get("reliability_score", item.get("reliability", {}).get("score", 0.5)) or 0.5)
            freshness = 0.0 if item.get("state") in {"stale", "disabled"} else 1.0
            trusted = 1.0 if item.get("kind") in {"builtin", "connector"} else 0.0
            risk_penalty = {"medium": 0.03, "high": 0.08, "critical": 0.16}.get(str(item.get("risk_level", "low")), 0.0)
            strategy_kind = (item.get("strategy") or {}).get("kind")
            invasiveness_penalty = {"api": 0.01, "cli": 0.025, "browser_workflow": 0.05}.get(strategy_kind, 0.0)
            auth_penalty = 0.025 if (item.get("auth") or {}).get("required") else 0.0
            score = semantic * 0.72 + reliability * 0.15 + freshness * 0.08 + trusted * 0.05
            score -= risk_penalty + invasiveness_penalty + auth_penalty
            ranked.append({**item, "match_score": round(score, 4)})
        ranked.sort(key=lambda value: value["match_score"], reverse=True)
        return ranked[: max(1, limit)]

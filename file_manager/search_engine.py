"""
Composes intent classification, entity extraction, ranking, and
disambiguation into a single `SearchEngine.search()` call. This is the
one place that decides "what should happen" for a natural-language file
request -- indexer.py now only owns indexing/storage, not decision logic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .disambiguation import Decision, Disambiguator
from .installations import GameInstallationLocator
from .intent import Intent, IntentClassifier, ParsedRequest
from .ranking import Ranker, ScoredResult


class SearchEngine:
    def __init__(self, indexer):
        self.indexer = indexer
        self.classifier = IntentClassifier()
        self.ranker = Ranker()
        self.disambiguator = Disambiguator()
        self.installation_locator = GameInstallationLocator()

    def search(
        self,
        query: str,
        limit: int = 10,
        action: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        sort: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        query_text = query if isinstance(query, str) else (query or {}).get("query", "")
        parsed = self.classifier.classify(query_text)

        if parsed.intent == Intent.FIND_INSTALLATION and parsed.entities.app_or_game_name:
            return self._resolve_installation(parsed)

        records = self.indexer.get_raw_records()
        ranked = self.ranker.rank(records, parsed)
        decision = self.disambiguator.decide(ranked)
        return self._decision_to_response(parsed, decision)

    def _resolve_installation(self, parsed: ParsedRequest) -> Dict[str, Any]:
        matches = self.installation_locator.find(parsed.entities.app_or_game_name)

        if not matches:
            # Registry/Steam lookup found nothing -- fall back to ranking
            # the file index (still with the heavy install/non-install
            # weighting from ranking.py) rather than giving up outright.
            records = self.indexer.get_raw_records()
            ranked = self.ranker.rank(records, parsed)
            decision = self.disambiguator.decide(ranked)
            return self._decision_to_response(parsed, decision)

        if len(matches) == 1:
            match = matches[0]
            return {
                "status": "ok",
                "intent": parsed.intent.value,
                "result": {"path": match["path"], "is_dir": True, "source": match["source"]},
            }

        return {
            "status": "clarify",
            "intent": parsed.intent.value,
            "candidates": [
                {"path": m["path"], "is_dir": True, "source": m["source"], "display_name": m.get("display_name")}
                for m in matches[:5]
            ],
            "reason": "Found that app/game installed in more than one place.",
        }

    def _decision_to_response(self, parsed: ParsedRequest, decision: Decision) -> Dict[str, Any]:
        if decision.action == "no_match":
            return {"status": "no_match", "intent": parsed.intent.value, "reason": decision.reason}

        if decision.action == "clarify":
            return {
                "status": "clarify",
                "intent": parsed.intent.value,
                "candidates": [self._present(c) for c in (decision.candidates or [])],
                "reason": decision.reason,
            }

        return {
            "status": "ok",
            "intent": parsed.intent.value,
            "result": self._present(decision.best),
        }

    def _present(self, scored: ScoredResult) -> Dict[str, Any]:
        record = dict(scored.record)
        record["score"] = scored.score
        record["reasons"] = scored.reasons
        return record

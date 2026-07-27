from abc import ABC, abstractmethod
from typing import Dict, List

from recommendation.models import MediaCandidate


class ContentSource(ABC):
    @property
    @abstractmethod
    def source_name(self) -> str:
        pass

    @abstractmethod
    def search_candidates(
        self,
        queries: List[str],
        max_per_query: int = 15,
        target_pool: int = 80,
    ) -> Dict[str, str]:
        pass

    @abstractmethod
    def enrich_metadata(self, candidate_ids: List[str]) -> List[MediaCandidate]:
        pass

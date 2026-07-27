from pathlib import Path
from typing import Any, Dict, List

from .service import SemanticFileManagerService


class SemanticSearch:
    def __init__(self, service: SemanticFileManagerService):
        self.service = service

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.service.search(query=query, limit=limit)

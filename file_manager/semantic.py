from typing import Any, Dict, List


class SemanticAnalyzer:
    def analyze(self, text: str) -> Dict[str, Any]:
        lowered = text.lower()
        keywords = [word for word in lowered.replace("\n", " ").split() if len(word) > 3]
        return {
            "summary": text[:400] if len(text) > 400 else text,
            "keywords": list(dict.fromkeys(keywords[:20])),
        }

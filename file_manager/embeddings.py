import hashlib
from typing import List


class EmbeddingGenerator:
    def generate(self, text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return [float(int(digest[i:i + 2], 16)) / 255.0 for i in range(0, 32, 2)]

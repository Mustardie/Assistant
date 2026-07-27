from pathlib import Path
from typing import Any, Dict


class ImageAnalyzer:
    def __init__(self, vision_client=None):
        self.vision_client = vision_client

    def analyze_image(self, path: str) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        return {
            "success": True,
            "path": str(target),
            "caption": target.stem,
            "objects": [],
            "animals": [],
            "people": [],
            "ocr": "",
            "scene": "",
            "keywords": [target.stem.lower()],
        }

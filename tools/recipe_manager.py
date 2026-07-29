import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_RECIPES_DIR = Path(__file__).resolve().parent.parent / "data" / "recipes"


class RecipeManager:
    def __init__(self):
        _RECIPES_DIR.mkdir(parents=True, exist_ok=True)
        self._cache: list[dict] | None = None

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _path(self) -> Path:
        return _RECIPES_DIR / "recipes.json"

    def _load_all(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        path = self._path()
        if not path.exists():
            self._cache = []
            return self._cache
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._cache = data if isinstance(data, list) else []
        except Exception:
            self._cache = []
        return self._cache

    def _save_all(self, recipes: list[dict]) -> None:
        self._cache = recipes
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recipes, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def find_match(self, goal: str) -> dict | None:
        """Return the best-matching recipe for a goal, or None."""
        recipes = self._load_all()
        if not recipes:
            return None
        goal_words = set(re.findall(r'\w+', goal.lower()))
        if not goal_words:
            return None
        best = None
        best_score = 0.0
        for recipe in recipes:
            r_words = set(re.findall(r'\w+', (recipe.get("goal", "") + " " + " ".join(recipe.get("tags", []))).lower()))
            if not r_words:
                continue
            overlap = len(goal_words & r_words)
            score = overlap / max(len(goal_words), len(r_words))
            if score > best_score:
                best_score = score
                best = recipe
        return best if best_score >= 0.15 else None

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_recipe(self, goal: str, tags: list[str], steps: list[dict]) -> None:
        """Save a recipe (list of tool steps) for a completed goal."""
        recipes = self._load_all()
        recipe = {
            "goal": goal,
            "tags": tags,
            "steps": steps,
            "count": len(steps),
        }
        recipes.append(recipe)
        self._save_all(recipes)
        logger.info("Saved recipe for goal: %s (%d steps)", goal, len(steps))

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------

    def format_recipe_prompt(self, recipe: dict) -> str:
        """Format a recipe as a prompt section."""
        steps_text = "\n".join(
            f"  {i+1}. {s.get('tool', '?')}({', '.join(f'{k}={v!r}' for k, v in s.get('arguments', {}).items())})"
            for i, s in enumerate(recipe.get("steps", []))
        )
        return (
            f"PREVIOUS WORKFLOW (matched recipe: {recipe.get('goal', '')}):\n"
            f"Tags: {', '.join(recipe.get('tags', []))}\n"
            f"You previously completed this with these steps:\n{steps_text}\n\n"
        )


recipe_manager = RecipeManager()

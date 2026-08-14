"""Tests for the V0.2 knowledge/context layer.

No network and no Ollama server required.
Run with:  python -m unittest discover tests -v
"""

import tempfile
import unittest
from pathlib import Path

import config
from knowledge import REFERENCE_NOTE, KnowledgeBase

MATERIALS_MD = """keywords: material, materials, principled, shader
# Materials
Body about materials. Principled BSDF inputs.
"""

CAMERAS_MD = """keywords: camera, cameras, lens, scene.camera
# Cameras
Body about cameras and lenses.
"""

LIGHTS_MD = """keywords: light, lights, sun, energy
# Lights
Body about lights and energy.
"""


def make_kb(directory, files):
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return KnowledgeBase(directory=str(directory))


class KnowledgeConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(config.KNOWLEDGE_DIR, "knowledge")
        self.assertTrue(config.KNOWLEDGE_ENABLED)
        self.assertEqual(config.MAX_CONTEXT_FILES, 3)
        self.assertGreater(config.MAX_CONTEXT_CHARS, 0)


class LoadingTests(unittest.TestCase):
    def test_topics_loaded_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = make_kb(Path(tmp), {
                "materials.md": MATERIALS_MD,
                "cameras.md": CAMERAS_MD,
            })
            self.assertTrue(kb.loaded)
            self.assertEqual(len(kb.topics), 2)
            materials = next(t for t in kb._topics if t.name == "materials")
            self.assertEqual(materials.keywords, ["material", "materials", "principled", "shader"])
            self.assertNotIn("keywords", materials.body)
            self.assertEqual(materials.title, "Materials")

    def test_body_keeps_content_and_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = make_kb(Path(tmp), {"materials.md": MATERIALS_MD})
            materials = kb._topics[0]
            self.assertIn("Body about materials.", materials.body)
            self.assertIn("# Materials", materials.body)

    def test_fallback_keywords_from_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "objects-and-collections.md").write_text(
                "# Objects and Collections\nNo keyword line here.",
                encoding="utf-8",
            )
            kb = KnowledgeBase(directory=str(tmp))
            self.assertEqual(
                kb._topics[0].keywords,
                ["objects", "and", "collections"],
            )

    def test_missing_directory_is_handled(self):
        kb = KnowledgeBase(directory="does-not-exist-at-all")
        self.assertFalse(kb.loaded)
        self.assertIsNotNone(kb.error)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.kb = make_kb(Path(self.tmpdir.name), {
            "materials.md": MATERIALS_MD,
            "cameras.md": CAMERAS_MD,
            "lights.md": LIGHTS_MD,
        })

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_selects_relevant_topic_only(self):
        context = self.kb.select_context("How do I change the camera lens?")
        self.assertIn("== Cameras ==", context)
        self.assertIn("Body about cameras and lenses.", context)
        self.assertNotIn("Materials", context)
        self.assertNotIn("Lights", context)

    def test_multiple_keywords_rank_higher(self):
        context = self.kb.select_context("set material principled shader")
        self.assertIn("== Materials ==", context)
        self.assertNotIn("== Cameras ==", context)

    def test_no_match_returns_empty(self):
        self.assertEqual(self.kb.select_context("what is the weather like?"), "")

    def test_max_files_respected(self):
        context = self.kb.select_context("camera lens light sun energy material shader",
                                         max_files=2)
        self.assertEqual(context.count("=="), 4)  # two topics, two "==" markers each

    def test_max_chars_truncates(self):
        # Budget = note + 120: note fits, but the two topic blocks do not.
        limit = len(REFERENCE_NOTE) + 120
        context = self.kb.select_context("camera lens material shader",
                                         max_chars=limit)
        self.assertLessEqual(len(context), limit)
        self.assertIn("[...truncated]", context)

    def test_context_contains_reference_note(self):
        context = self.kb.select_context("camera lens")
        self.assertIn(REFERENCE_NOTE, context)

    def test_empty_knowledge_base_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_kb = KnowledgeBase(directory=str(tmp))
            self.assertFalse(empty_kb.loaded)
            self.assertEqual(empty_kb.select_context("camera lens"), "")


if __name__ == "__main__":
    unittest.main()

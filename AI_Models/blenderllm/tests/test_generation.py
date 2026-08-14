"""Tests for the V0.3 Blender Python generation support.

No network, no Ollama, no model, no Blender required.
Run with:  python -m unittest discover tests -v
"""

import tempfile
import unittest
from pathlib import Path

import main
from generation import extract_python_code, is_code_request
from knowledge import KnowledgeBase
from system_prompt import CODE_FORMAT_GUIDANCE, SYSTEM_PROMPT


class CodeRequestDetectionTests(unittest.TestCase):
    def test_detects_code_requests(self):
        requests = [
            "Create a cube using Blender Python.",
            "Make a procedural staircase.",
            "Create a material with nodes.",
            "Add a camera and point it at the origin.",
            "Write a script that creates a sci-fi corridor.",
            "Why is this bpy script failing?",
            "Generate a script that builds a low-poly scene.",
            "Set up a scene with 50 cubes.",
            "Help me debug my modifier code.",
        ]
        for request in requests:
            with self.subTest(request=request):
                self.assertTrue(is_code_request(request))

    def test_does_not_detect_conceptual_questions(self):
        questions = [
            "What is a mesh?",
            "What is the difference between EEVEE and Cycles?",
            "How do I render an animation?",
            "Explain what a modifier does.",
            "What's the best way to light a scene?",
            "What is bpy?",
            "How do I make a cube?",
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(is_code_request(question))


class CodeExtractionTests(unittest.TestCase):
    def test_extracts_python_block(self):
        response = (
            "PLAN\nLet's create a cube.\n\n"
            "BLENDER PYTHON\n```python\nimport bpy\n"
            "bpy.ops.mesh.primitive_cube_add()\n```\n\n"
            "NOTES\nTargets modern Blender.\n"
        )
        self.assertEqual(
            extract_python_code(response),
            "import bpy\nbpy.ops.mesh.primitive_cube_add()",
        )

    def test_prefers_python_block_over_other_languages(self):
        response = "```json\n{\"a\": 1}\n```\n\n```python\nx = 1\n```\n"
        self.assertEqual(extract_python_code(response), "x = 1")

    def test_plain_fenced_block_fallback(self):
        response = "```\nprint('hi')\n```"
        self.assertEqual(extract_python_code(response), "print('hi')")

    def test_no_code_returns_none(self):
        self.assertIsNone(extract_python_code("Just an explanation, no code."))
        self.assertIsNone(extract_python_code(""))
        self.assertIsNone(extract_python_code(None))


class PromptConstructionTests(unittest.TestCase):
    def test_code_request_appends_guidance(self):
        messages = main.build_messages([], "Create a cube.", None,
                                       code_request=True)
        self.assertIn(CODE_FORMAT_GUIDANCE, messages[0]["content"])
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "Create a cube.")

    def test_no_guidance_for_normal_question(self):
        messages = main.build_messages([], "What is a mesh?", None)
        self.assertNotIn("BLENDER PYTHON", messages[0]["content"])

    def test_system_knowledge_guidance_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "materials.md").write_text(
                "keywords: material\n# Materials\nBody.\n", encoding="utf-8")
            kb = KnowledgeBase(directory=str(tmp))
            messages = main.build_messages(
                [], "Create a material with nodes.", kb, code_request=True)
            content = messages[0]["content"]
            self.assertLess(content.index(SYSTEM_PROMPT[:20]),
                            content.index("reference material"))
            self.assertLess(content.index("reference material"),
                            content.index(CODE_FORMAT_GUIDANCE))

    def test_prompt_end_to_end_with_extraction(self):
        messages = main.build_messages([], "Write a cube script.", None,
                                       code_request=True)
        reply = (
            "PLAN\nA script that adds a cube.\n\n"
            "BLENDER PYTHON\n```python\nimport bpy\n"
            "bpy.ops.mesh.primitive_cube_add()\n```\n\n"
            "NOTES\nNone.\n"
        )
        code = extract_python_code(reply)
        self.assertIn("bpy.ops.mesh.primitive_cube_add", code)
        self.assertNotIn("PLAN", code)


if __name__ == "__main__":
    unittest.main()

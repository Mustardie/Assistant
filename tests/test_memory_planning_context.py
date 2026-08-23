from memory.memory_manager import MemoryManager


def test_planning_context_is_disabled_for_unrelated_chat(tmp_path):
    manager = MemoryManager(tmp_path)
    manager.get_relevant_memories = lambda **kwargs: [{"category": "goal", "text": "Finish the Mars project"}]
    assert manager.get_planning_context("hello there", enabled=False) == []


def test_planning_context_filters_and_summarizes_by_query_overlap(tmp_path):
    manager = MemoryManager(tmp_path)
    manager.get_relevant_memories = lambda **kwargs: [
        {"category": "goal", "text": "Finish the Mars project before Friday", "extra": "ignored"},
        {"category": "preference", "text": "I like concise answers"},
    ]
    context = manager.get_planning_context("open my Mars project files", enabled=True)
    assert context == [{
        "category": "goal",
        "text": "Finish the Mars project before Friday",
        "matched_terms": ["mars", "project"],
    }]


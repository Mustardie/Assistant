"""Tests for the event abstraction, dispatcher routing, and watchers."""

import time

import pytest

from events import (
    AppEvent,
    DOWNLOAD_COMPLETED,
    NEW_FILE,
    NEW_MESSAGE,
    event_dispatcher,
    make_event,
)
from events.dispatcher import EventDispatcher
from events.watchers import FilesystemEventWatcher, WatcherRegistry


def test_app_event_structure():
    event = make_event(
        source="discord",
        event_type=NEW_MESSAGE,
        sender="Teacher",
        content="Complete questions 1-10 by Friday",
        attachment={"name": "hw.pdf", "path": "C:/hw.pdf"},
        metadata={"channel": "math"},
    )
    assert event.source == "discord"
    assert event.type == NEW_MESSAGE
    assert event.sender == "Teacher"
    assert "1-10" in event.content
    assert event.attachment["name"] == "hw.pdf"
    data = event.to_dict()
    assert data["event_id"] == event.event_id
    round_tripped = AppEvent.from_dict(data)
    assert round_tripped.source == event.source
    assert round_tripped.content == event.content


def test_dispatcher_routes_by_type():
    dispatcher = EventDispatcher()
    received = []
    dispatcher.register_handler(received.append, event_type=NEW_MESSAGE)
    dispatcher.emit_event("whatsapp", NEW_MESSAGE, content="hello")
    dispatcher.emit_event("gmail", NEW_EMAIL := "NEW_EMAIL", content="ignored")
    assert len(received) == 1
    assert received[0].content == "hello"


def test_dispatcher_routes_by_source():
    dispatcher = EventDispatcher()
    received = []
    dispatcher.register_handler(received.append, source="discord")
    dispatcher.emit_event("discord", NEW_MESSAGE, content="a")
    dispatcher.emit_event("whatsapp", NEW_MESSAGE, content="b")
    assert [e.content for e in received] == ["a"]


def test_dispatcher_catches_all():
    dispatcher = EventDispatcher()
    received = []
    dispatcher.register_handler(received.append)
    dispatcher.emit_event("anywhere", NEW_FILE, content="x")
    assert len(received) == 1


def test_dispatcher_swallows_handler_errors():
    dispatcher = EventDispatcher()

    def bad(event):
        raise RuntimeError("handler blew up")

    received = []
    dispatcher.register_handler(bad)
    dispatcher.register_handler(received.append)
    dispatcher.emit_event("x", NEW_MESSAGE, content="ok")  # must not raise
    assert len(received) == 1


def test_dispatcher_history():
    dispatcher = EventDispatcher()
    dispatcher.emit_event("x", NEW_MESSAGE, content="one")
    dispatcher.emit_event("y", NEW_FILE, content="two")
    history = dispatcher.recent_events(5)
    assert len(history) == 2
    assert history[-1]["content"] == "two"
    dispatcher.clear()
    assert dispatcher.recent_events() == []


def test_dispatcher_unregister():
    dispatcher = EventDispatcher()
    received = []
    dispatcher.register_handler(received.append, event_type=NEW_MESSAGE)
    dispatcher.unregister_handler(received.append)
    dispatcher.emit_event("x", NEW_MESSAGE, content="nope")
    assert received == []


def test_watcher_emits_new_file_event(tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    watcher = FilesystemEventWatcher(folder, interval_s=0.5)
    received = []
    event_dispatcher.register_handler(
        lambda e: received.append(e) if e.source == "filesystem" else None
    )
    try:
        watcher.start()
        watcher.wait_ready()
        (folder / "hw.pdf").write_text("assignment", encoding="utf-8")
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.2)
        assert received, "no filesystem event emitted"
        assert received[0].type == NEW_FILE
        assert "hw.pdf" in received[0].content
    finally:
        watcher.stop()
        event_dispatcher.unregister_handler(received.append)


def test_watcher_registry_start_stop(tmp_path):
    folder = tmp_path / "watch2"
    folder.mkdir()
    watcher = FilesystemEventWatcher(folder, interval_s=0.5)
    registry = WatcherRegistry()
    registry.start(watcher)
    assert registry.active_count == 1
    registry.start(watcher)  # idempotent
    assert registry.active_count == 1
    registry.stop_all()
    assert registry.active_count == 0


def test_global_dispatcher_is_singleton():
    from events.dispatcher import event_dispatcher as global_disp
    assert event_dispatcher is global_disp
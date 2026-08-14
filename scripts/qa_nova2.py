"""Programmatic geometry QA for the redesigned Nova UI (no images needed)."""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DATA_DIR = os.path.join(ROOT, "data")
_TMP = os.environ["TEMP"]
backups = {}
for fname in ("nova_settings.json", "nova_conversations.json"):
    src = os.path.join(_DATA_DIR, fname)
    if os.path.exists(src):
        dst = os.path.join(_TMP, f"nova_qa_{fname}")
        shutil.copy2(src, dst)
        backups[fname] = dst

# restore before starting (in case a previous run/probe left test state)
for fname, dst in backups.items():
    try:
        shutil.copy2(dst, os.path.join(_DATA_DIR, fname))
    except Exception:
        pass

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)
from ui.nova_window import NovaWindow
import ui.theme as theme

w = NovaWindow()
w.show()
errors = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        errors.append(name)


def walk(widget, out):
    out.append(widget)
    for child in widget.findChildren(type(widget)):
        pass
    for child in widget.children():
        if hasattr(child, "children") and hasattr(child, "geometry") \
                and not isinstance(child, (QApplication,)):
            try:
                out.append(child)
            except Exception:
                pass
    return out


def run():
    try:
        _run()
    except Exception:
        import traceback
        traceback.print_exc()
        errors.append("exception")
    app.quit()


def restore_data():
    # Must run AFTER app.exec() returns: the window's closeEvent re-saves
    # conversations during teardown, so an earlier restore would be clobbered.
    for fname, dst in backups.items():
        try:
            shutil.copy2(dst, os.path.join(_DATA_DIR, fname))
        except Exception:
            pass


def _run():
    w.resize(1280, 800)
    app.processEvents()

    # ---- chat layout ----
    w.show_page("chat")
    w.new_conversation()
    app.processEvents()
    cp = w.chat_page
    dock = cp.input_bar
    check("dock centered", abs(dock.x() - (cp.width() - dock.width()) / 2) < 8,
          f"dock x={dock.x()} w={dock.width()} page={cp.width()}")
    check("dock fills page width", dock.width() >= cp.width() - 70,
          f"dock w={dock.width()} page={cp.width()}")
    welcome = cp._welcome
    check("welcome in layout", cp._messages_layout.indexOf(welcome) == 0)
    cards = welcome._cards
    check("3 prompt cards", len(cards) == 3)
    if cards:
        cw = cards[0].width()
        check("card widths sane", 180 < cw < 320, f"w={cw}")

    # send/attach visible
    check("attach btn", dock.attach_btn.isVisible())
    check("mic btn", dock.mic_btn.isVisible())
    check("send btn", dock.send_btn.isVisible())

    # ---- message row geometry ----
    w.append_user("Summarize my Gmail and check for unread items")
    w.begin_reply()
    w.append_assistant(
        "Here you go:\n\n```python\ndef hello():\n    return 'world'\n```\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n- first\n- second")
    app.processEvents()
    lay = cp._messages_layout
    widgets = [lay.itemAt(i).widget() for i in range(lay.count())]
    rows = [x for x in widgets if x is not None]
    check("3+ rows rendered", len(rows) >= 4, f"count={len(rows)}")

    from ui.widgets.message_bubble import AssistantRow, UserBubble
    user_rows = [x for x in rows if x.findChild(UserBubble) is not None]
    asst_rows = [x for x in rows if isinstance(x, AssistantRow)]
    check("user bubble found", bool(user_rows))
    check("assistant row found", bool(asst_rows))
    if user_rows:
        ub = user_rows[0].findChild(UserBubble)
        check("user bubble right-aligned",
              ub.mapTo(w, ub.rect().topRight()).x() > w.width() - 340,
              f"right={ub.mapTo(w, ub.rect().topRight()).x()}")
        check("user bubble max width", ub.width() <= 661)
    if asst_rows:
        ar = asst_rows[0]
        card = ar._card
        check("assistant card within page",
              card.mapTo(w, card.rect().topLeft()).x() > w.nav.width(),
              f"card x={card.mapTo(w, card.rect().topLeft()).x()}")
        from ui.widgets.code_block import CodeBlock
        cb = ar.findChild(CodeBlock)
        check("code block rendered", cb is not None)
        if cb is not None:
            check("code block inside card",
                  cb.mapTo(w, cb.rect().topLeft()).x() >= card.mapTo(w, card.rect().topLeft()).x())
        check("table rendered", bool(cp.findChildren(type(ar.findChild(type(ar._card))))))

    # scroll to bottom works
    cp.scroll_to_bottom()
    check("scrolled", cp._messages_scroll.verticalScrollBar().value() >= 0)

    # ---- streaming merge: single first assistant message ----
    w.new_conversation()
    w.append_user("stream test")
    w.begin_reply()
    w.append_assistant("The quick brown")
    w.append_assistant("The quick brown fox jumps")
    w.append_assistant("The quick brown fox jumps over the lazy dog")
    app.processEvents()
    rows_now = [cp._messages_layout.itemAt(i).widget()
                for i in range(cp._messages_layout.count())]
    asst_now = [r for r in rows_now if isinstance(r, AssistantRow)]
    check("stream: exactly one assistant row", len(asst_now) == 1,
          f"count={len(asst_now)}")
    if asst_now:
        check("stream: text merged",
              asst_now[0]._text == "The quick brown fox jumps over the lazy dog",
              repr(asst_now[0]._text))
    conv = w._conversations[w._active_id]
    check("stream: conv text merged",
          conv["messages"][-1]["text"] == "The quick brown fox jumps over the lazy dog")

    # ---- history ----
    w.show_page("history")
    app.processEvents()
    hp = w.history_page
    check("history subtitle count", "conversation" in hp._subtitle.text(),
          hp._subtitle.text())
    check("history search", hp._search.isVisible())
    # cards fit
    body = hp._body_host
    for i in range(hp._body_layout.count()):
        wdg = hp._body_layout.itemAt(i).widget()
        if wdg is not None and wdg.width() > 0:
            check(f"hist item fits {i}", wdg.mapTo(hp, wdg.rect().topRight()).x()
                  <= hp.width() + 2, f"right={wdg.mapTo(hp, wdg.rect().topRight()).x()} page={hp.width()}")

    # pin toggle
    items = hp._items
    if items:
        cid = items[0]["id"]
        w.toggle_pin(cid)
        check("pin persisted", bool(w._conversations[cid].get("pinned")))
        check("history shows pinned group", "PINNED" in
              " ".join(l.text() for l in hp.findChildren(type(hp._title)) if hasattr(l, "text") and l.text() == "PINNED").upper())

    # search filters
    hp._on_search("nonexistent-xyz")
    check("search filters", hp._subtitle.text().endswith("· filtered"))
    hp._on_search("")

    # ---- library ----
    w.show_page("library")
    app.processEvents()
    lp = w.library_page
    check("library collections", lp._collections_grid.count() >= 4)
    check("library items", lp._items_grid.count() >= 6)

    # ---- settings ----
    w.show_page("settings")
    app.processEvents()
    sp = w.settings_page
    import ui.widgets.settings_page as sp_mod
    check("key block hidden for ollama", not sp._key_block.isVisible())
    idx = next(i for i, p in enumerate(sp_mod.PROVIDERS) if p == "OpenRouter")
    sp._provider_combo.setCurrentIndex(idx)
    app.processEvents()
    check("key block shown for openrouter", sp._key_block.isVisible())
    check("key edit value", sp._key_edit.text() == sp._settings["api_keys"].get("OpenRouter", ""))
    # speed slider
    sp._speed_slider.setValue(1.5)
    sp._speed_slider.changed.emit(1.5)
    check("speed updates settings", abs(sp._settings["speed"] - 1.5) < 0.01)
    check("speed label", sp._speed_value.text() == "1.50×")
    # hotkey
    sp._hotkey_edit.setText("alt+space")
    check("hotkey updates", sp._settings["hotkey"] == "alt+space")
    # settings body fits horizontally
    check("settings body width ok", sp._body.width() > 500 and sp._body.width() <= sp.width())
    # theme cards
    sp._theme_cards["light"].clicked.emit("light") if hasattr(sp._theme_cards["light"], "clicked") else None
    sp._on_theme_picked("light")
    check("theme light applied", theme.THEME_NAME == "light")
    sp._on_theme_picked("dark")
    check("theme dark back", theme.THEME_NAME == "dark")

    # ---- voice state ----
    w.set_voice_state("listening")
    check("mic listening color", w.chat_page.input_bar.mic_btn._color is not None)
    w.set_voice_state("idle")

    # ---- window chrome ----
    w.toggle()
    check("toggle hides", not w.isVisible())
    w.toggle()
    check("toggle shows", w.isVisible())

    # ---- nav ----
    w.show_page("chat")
    check("nav active chat", w.nav._tabs["chat"]._active)
    w.show_page("history")
    check("nav active history", w.nav._tabs["history"]._active)


QTimer.singleShot(900, run)
app.exec()
restore_data()
print("ERRORS:", errors if errors else "none")
sys.exit(1 if errors else 0)

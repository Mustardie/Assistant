"""Offscreen smoke test + renders for the redesigned Nova UI."""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_ORIGINALS = {}
for fname in ("nova_settings.json", "nova_conversations.json"):
    src = os.path.join(_DATA_DIR, fname)
    if os.path.exists(src):
        dst = os.path.join(os.environ["TEMP"], f"nova_backup_{fname}")
        shutil.copy2(src, dst)
        _ORIGINALS[fname] = dst

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

import json


def _pin_active_conv():
    """Make the 'welcome hidden' check deterministic: point _active at a
    conversation that actually has messages (the user's live app can leave
    _active on an empty one; data is backed up above and restored at exit)."""
    path = os.path.join(_DATA_DIR, "nova_conversations.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    active = data.get("_active")
    if active in data and data[active].get("messages"):
        return
    for cid, conv in data.items():
        if cid != "_active" and conv.get("messages"):
            data["_active"] = cid
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return


_pin_active_conv()

app = QApplication(sys.argv)

from ui.nova_window import NovaWindow
import ui.theme as theme

w = NovaWindow()
w.show()

errors = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        errors.append(name)


def render(name):
    out = os.path.join(os.environ.get("TEMP", "/tmp"), f"nova2_{name}.png")
    w.grab().save(out)
    print("rendered", out)


def run_checks():
    try:
        _run_checks()
    except Exception:
        import traceback
        traceback.print_exc()
        errors.append("exception in run_checks")
    app.quit()


def _restore_data():
    # Must run AFTER app.exec() returns: the window's closeEvent re-saves
    # conversations during teardown, so an earlier restore would be clobbered.
    for fname, dst in _ORIGINALS.items():
        try:
            shutil.copy2(dst, os.path.join(_DATA_DIR, fname))
        except Exception:
            pass


def _run_checks():
    check("nav width", w.nav.width() == theme.SIDEBAR_W)
    check("window size", w.width() >= 1080)
    check("stack page chat", w._stack.currentWidget() is w.chat_page)
    check("welcome hidden (conv has msgs)",
          not w.chat_page._welcome.isVisible())
    render("chat_empty")

    # history page
    w.show_page("history")
    check("history page active", w._stack.currentWidget() is w.history_page)
    check("history has items", w.history_page._body_layout.count() > 3)
    render("history")

    # library + templates
    w.show_page("library")
    render("library")
    w.show_page("templates")
    render("templates")

    # settings page
    w.show_page("settings")
    check("settings page active", w._stack.currentWidget() is w.settings_page)
    s = w.settings_page
    check("name loaded", s._name_edit.text() == w._settings.get("assistant_name"))
    check("provider loaded", s._provider_combo.currentText() == w._settings.get("provider"))
    check("engine loaded", s._engine_combo.currentText().startswith("Kokoro"))
    check("speed loaded", abs(s._speed_slider.value() - float(w._settings.get("speed", 1.0))) < 0.01)
    check("hotkey loaded", s._hotkey_edit.text() == w._settings.get("hotkey"))
    check("theme card dark", s._theme_cards["dark"]._selected)
    render("settings")

    # chat with messages
    w.show_page("chat")
    w.chat_page.show_welcome(False)
    w.new_conversation()
    check("welcome visible after new", w.chat_page._welcome.isVisible())
    w.append_user("Analyze this code block for potential performance optimizations")
    w.begin_reply()
    w.append_assistant(
        "# Analysis\n\nHere's what I found:\n\n```python\ndef slow(x):\n    total = 0\n    for i in range(len(x)):\n        total += x[i] * 2\n    return total\n```\n\n- **Bottleneck**: the loop\n- **Fix**: use a comprehension\n\n| Approach | Speed |\n|---|---|\n| Loop | 1× |\n| Comprehension | 3× |")
    check("user bubble rendered", w.chat_page._messages_layout.count() >= 4)
    render("chat")

    # voice state
    w.set_voice_state("listening")
    render("chat_listening")

    # theme switch
    w._on_settings_changed({**w._settings, "theme": "light"})
    check("theme switched", theme.THEME_NAME == "light")
    render("chat_light")
    w._on_settings_changed({**w._settings, "theme": "dark"})
    check("theme back", theme.THEME_NAME == "dark")


QTimer.singleShot(900, run_checks)
app.exec()
_restore_data()
print("ERRORS:", errors if errors else "none")
sys.exit(1 if errors else 0)

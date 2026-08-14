"""Pixel-level QA: light-mode surfaces + icon centering for the redesigned UI.

Verifies (offscreen):
  - input pill children are vertically centered (no bottom-hugging)
  - dock fills the page width (full-width chatbar)
  - icon strokes are drawn centered inside their buttons (dark mode,
    where fills are translucent, by bounding-box analysis of grab()s)
  - light mode: page bg is near-white, pill/bubble/card fills are light
  - dark mode regression: same surfaces are dark
Backs up and restores data/nova_*.json.
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_ROOT, "data")
_ORIGINALS = {}
for fname in ("nova_settings.json", "nova_conversations.json"):
    src = os.path.join(_DATA_DIR, fname)
    if os.path.exists(src):
        dst = os.path.join(os.environ["TEMP"], f"nova_backup_{fname}")
        shutil.copy2(src, dst)
        _ORIGINALS[fname] = dst

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QPoint

app = QApplication(sys.argv)

from ui.nova_window import NovaWindow
from ui.widgets.glass import GlassButton, GlassIconButton, GlassCombo
from ui.widgets.chat_input import _SendButton
from ui.widgets.top_bar import _WinButton
from ui.widgets.nav_rail import NavTab
import ui.theme as theme

w = NovaWindow()
w.show()
errors = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        errors.append(name)


def lightness(widget, point):
    """Sample lightness (0..1) of a widget-relative point in the window render."""
    global_pt = widget.mapTo(w, QPoint(point.x(), point.y()))
    img = w.grab().toImage()
    if not img.valid(global_pt.x(), global_pt.y()):
        return -1.0
    return img.pixelColor(global_pt.x(), global_pt.y()).lightnessF()


def icon_bbox_center(widget, *, dark_strokes=False, alpha_min=80):
    """Render a widget and find the bounding box of its glyph pixels.

    Returns (cx, cy, bw, bh) in widget-local coords, or None if nothing found.
    """
    img = widget.grab().toImage()
    min_x, min_y, max_x, max_y = None, None, None, None
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() < alpha_min:
                continue
            lum = c.lightnessF()
            if dark_strokes and lum >= 0.40:
                continue
            if not dark_strokes and lum >= 0.97:
                continue
            if min_x is None:
                min_x = max_x = x
                min_y = max_y = y
            else:
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
    if min_x is None:
        return None
    return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0,
            max_x - min_x + 1, max_y - min_y + 1)


def check_centered(name, widget, *, tol=2.5, check_h=True, check_v=True,
                   dark_strokes=False):
    box = icon_bbox_center(widget, dark_strokes=dark_strokes)
    if box is None:
        check(f"{name}: glyph found", False)
        return
    cx, cy, bw, bh = box
    wc_x = widget.width() / 2.0
    wc_y = widget.height() / 2.0
    dx = abs(cx - wc_x) if check_h else 0.0
    dy = abs(cy - wc_y) if check_v else 0.0
    check(f"{name}: centered (dx={dx:.1f} dy={dy:.1f})", dx <= tol and dy <= tol)


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
    # ---- input dock geometry ------------------------------------------ #
    dock = w.chat_page.input_bar
    check("dock long (fills page width)", dock.width() >= 800)
    pill = dock.pill
    for name, btn in (("attach", dock.attach_btn), ("mic", dock.mic_btn),
                      ("send", dock.send_btn)):
        btn_c = btn.y() + btn.height() / 2.0
        pill_c = pill.height() / 2.0
        check(f"dock {name} v-centered (off={abs(btn_c - pill_c):.1f})",
              abs(btn_c - pill_c) <= 3.0)

    # ---- input text: vertical centering + growth ----------------------- #
    def scan_editor(want_dark, x_max=220):
        er = dock.editor.geometry()
        img = dock.grab().toImage()
        ys = []
        xs = []
        for y in range(er.y() + 2, er.y() + er.height() - 2):
            for x in range(er.x() + 10, er.x() + 10 + x_max):
                c = img.pixelColor(x, y)
                if c.alpha() < 120:
                    continue
                l = c.lightnessF()
                if (want_dark and l > 0.55) or (not want_dark and l < 0.20):
                    xs.append(x)
                    ys.append(y)
        if not ys:
            return None, er
        return ((min(ys) + max(ys)) / 2.0, min(ys), max(ys)), er

    def settle():
        for _ in range(6):
            app.processEvents()

    dock.editor.setPlainText("Hello Nova")
    settle()
    box, er = scan_editor(True)
    cy = (box[0] - er.y()) if box else -1
    check(f"dark input text bright", box is not None)
    check(f"dark input text v-centered (cy {cy:.1f} of {er.height()})",
          box is not None and abs(cy - er.height() / 2.0) <= 4)
    dock.editor.setPlainText("line one\nline two\nline three")
    settle()
    check(f"input grows on multiline (pill {dock.pill.height()}, "
          f"ed {dock.editor.height()})",
          dock.editor.height() > 50
          and dock.pill.height() == dock.editor.height() + 16)
    dock.editor.clear()
    settle()

    # ---- standalone icon centering (dark fills are translucent) -------- #
    btn = _SendButton()
    btn.resize(38, 38)
    btn.show()
    check_centered("send icon", btn, dark_strokes=True)

    mic = GlassIconButton("mic", size=38, icon_size=18)
    mic.show()
    check_centered("mic icon", mic)

    attach = GlassIconButton("paperclip", size=36, icon_size=17)
    attach.show()
    check_centered("attach icon", attach)

    filt = GlassIconButton("filter", size=38, icon_size=17)
    filt.show()
    check_centered("filter icon", filt)

    win = _WinButton("minus", "Minimize")
    win.show()
    check_centered("win-button icon", win)

    tab = NavTab("chat", "chat", "Chat")
    tab.resize(224, 42)
    tab.show()
    check_centered("nav-tab icon (vertical)", tab, check_h=False)

    gb = GlassButton("Test voice", icon_name="play", icon_size=13,
                     variant="ghost", pill=True)
    gb.resize(150, 34)
    gb.show()
    check_centered("glass button icon+text group", gb, tol=3.0)

    combo = GlassCombo(["a", "b"])
    combo.resize(230, 42)
    combo.show()
    check_centered("combo chevron (vertical)", combo, check_h=False)

    # ---- light mode surfaces ------------------------------------------ #
    w.show_page("chat")
    w.new_conversation()
    scroll = w.chat_page._messages_scroll
    w.append_user("Analyze this code block for potential performance optimizations")
    w.begin_reply()
    w.append_assistant(
        "Here's the plan:\n\n```python\ndef f(x):\n    return x * 2\n```\n\n"
        "| Step | Time |\n|---|---|\n| 1 | 1x |")
    rows = [w.chat_page._messages_layout.itemAt(i).widget()
            for i in range(w.chat_page._messages_layout.count())]
    bubble = None
    arow = None
    from ui.widgets.message_bubble import UserBubble, AssistantRow
    for r in rows:
        if isinstance(r, UserBubble) or (r is not None and r.findChildren(UserBubble)):
            if bubble is None:
                found = r.findChildren(UserBubble)
                bubble = found[0] if found else r
        if isinstance(r, AssistantRow):
            arow = r
    check("user bubble found", bubble is not None)
    check("assistant row found", arow is not None)

    p_page = scroll.mapTo(w, QPoint(scroll.width() - 12, scroll.height() - 12))
    img = w.grab().toImage()
    c_page = img.pixelColor(p_page.x(), p_page.y()).lightnessF()
    check(f"dark page bg dark ({c_page:.2f})", c_page < 0.30)
    c_pill = img.pixelColor(
        pill.mapTo(w, QPoint(pill.width() // 2, pill.height() // 2)).x(),
        pill.mapTo(w, QPoint(pill.width() // 2, pill.height() // 2)).y()
    ).lightnessF()
    check(f"dark pill dark ({c_pill:.2f})", c_pill < 0.35)

    # switch to light
    w._on_settings_changed({**w._settings, "theme": "light"})
    check("theme light", theme.THEME_NAME == "light")
    img = w.grab().toImage()
    c_page = img.pixelColor(p_page.x(), p_page.y()).lightnessF()
    check(f"light page bg near-white ({c_page:.2f})", c_page > 0.85)
    c_pill = img.pixelColor(
        pill.mapTo(w, QPoint(pill.width() // 2, pill.height() // 2)).x(),
        pill.mapTo(w, QPoint(pill.width() // 2, pill.height() // 2)).y()
    ).lightnessF()
    check(f"light pill light box ({c_pill:.2f})", c_pill > 0.75)
    if bubble is not None:
        c_bub = img.pixelColor(
            bubble.mapTo(w, QPoint(bubble.width() - 30, bubble.height() // 2)).x(),
            bubble.mapTo(w, QPoint(bubble.width() - 30, bubble.height() // 2)).y()
        ).lightnessF()
        check(f"light user bubble light box ({c_bub:.2f})", c_bub > 0.75)
    if arow is not None:
        card = arow._card
        c_card = img.pixelColor(
            card.mapTo(w, QPoint(10, card.height() // 2)).x(),
            card.mapTo(w, QPoint(10, card.height() // 2)).y()
        ).lightnessF()
        check(f"light assistant card light box ({c_card:.2f})", c_card > 0.75)

    # light-mode icon visibility on glass (contrast by construction) -- just
    # verify glass buttons still render glyphs in light mode
    mic2 = GlassIconButton("mic", size=38, icon_size=18)
    mic2.show()
    box = icon_bbox_center(mic2, alpha_min=120)
    check("light icon glyph rendered", box is not None and box[2] >= 10 and box[3] >= 10)

    # light-mode text is black: darkest pixel inside the title label region
    title = w.chat_page._conv_title
    img = w.grab().toImage()
    tl = title.mapTo(w, QPoint(0, 0))
    darkest = 1.0
    for dy in range(0, title.height(), 2):
        for dx in range(0, title.width(), 2):
            c = img.pixelColor(tl.x() + dx, tl.y() + dy)
            if c.alpha() > 200:
                darkest = min(darkest, c.lightnessF())
    check(f"light title text black (min={darkest:.2f})", darkest < 0.15)

    # input text must be pure black + centered in light mode
    dock.editor.setPlainText("Black text test")
    settle()
    box, er = scan_editor(False)
    cy = (box[0] - er.y()) if box else -1
    check("light input text black", box is not None)
    check(f"light input text v-centered (cy {cy:.1f} of {er.height()})",
          box is not None and abs(cy - er.height() / 2.0) <= 4)
    dock.editor.clear()
    settle()
    box, er = scan_editor(False)
    check("light input placeholder black", box is not None)

    # markdown inline colors follow the palette (light accent values)
    from ui.widgets.markdown import _inline
    h = _inline("use `x` and [y](http://y.com)")
    check("light markdown code color themed", "#9459E0" in h)
    check("light markdown link color themed", "#5558D2" in h)

    # back to dark
    w._on_settings_changed({**w._settings, "theme": "dark"})
    check("theme dark back", theme.THEME_NAME == "dark")


QTimer.singleShot(900, run_checks)
app.exec()
_restore_data()
print("ERRORS:", errors if errors else "none")
sys.exit(1 if errors else 0)

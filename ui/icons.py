"""
Nova icon set — crisp, hand-painted line icons.

No icon font or asset files: every icon is rendered from an SVG-style path
string (M/L/H/V/C/Q/A/Z, arcs are circular) through a tiny path parser, then
painted into a pixmap at the requested size/color. This keeps the whole
app dependency-free and perfectly consistent with the design language.

    from ui.icons import icon
    button.setIcon(icon("plus", "#9AA6B8", 18))
"""
from __future__ import annotations

import math
import re

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (
    QPainter, QPainterPath, QIcon, QPixmap, QPen, QColor,
)

_CANVAS = 24.0  # icon viewBox

_TOKEN_RE = re.compile(
    r"[MmLlHhVvCcQqAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
)


# --------------------------------------------------------------------------- #
# Tiny SVG path parser (subset: M L H V C Q A Z)
# --------------------------------------------------------------------------- #

def parse_path(spec: str) -> QPainterPath:
    """Parse an SVG path string into a QPainterPath."""
    path = QPainterPath()
    cx = cy = 0.0
    start = (0.0, 0.0)
    prev_ctrl = None          # previous C/Q control point (for smooth repeats)
    cmd = ""

    tokens = _TOKEN_RE.findall(spec)
    idx = 0
    n = len(tokens)
    _guard = 0

    def is_num(tok):
        return not (len(tok) == 1 and tok.isalpha())

    def take(k):
        nonlocal idx
        nums = []
        while idx < n and len(nums) < k:
            tok = tokens[idx]
            if tok in "zZ":
                break
            if not is_num(tok):
                break
            nums.append(float(tok))
            idx += 1
        return nums

    def take_all():
        nonlocal idx
        nums = []
        while idx < n:
            tok = tokens[idx]
            if tok in "zZ":
                break
            if not is_num(tok):
                break
            nums.append(float(tok))
            idx += 1
        return nums

    while idx < n:
        _guard += 1
        if _guard > 10000:
            raise RuntimeError(f"parse_path guard: stuck near token {tokens[idx:idx+8]}")
        tok = tokens[idx]
        if tok in "zZ":
            path.closeSubpath()
            cx, cy = start
            idx += 1
            continue
        if not is_num(tok):
            cmd = tok
            idx += 1
            continue

        c = cmd.upper()
        rel = cmd.islower()
        nums = take_all()

        if c == "M":
            for i in range(0, len(nums) - 1, 2):
                nx = cx + nums[i] if rel else nums[i]
                ny = cy + nums[i + 1] if rel else nums[i + 1]
                if i == 0:
                    path.moveTo(nx, ny)
                    start = (nx, ny)
                else:
                    path.lineTo(nx, ny)
                cx, cy = nx, ny
            cmd = "l" if rel else "L"
            prev_ctrl = None

        elif c == "L":
            for i in range(0, len(nums) - 1, 2):
                nx = cx + nums[i] if rel else nums[i]
                ny = cy + nums[i + 1] if rel else nums[i + 1]
                path.lineTo(nx, ny)
                cx, cy = nx, ny
            prev_ctrl = None

        elif c == "H":
            for v in nums:
                cx = cx + v if rel else v
                path.lineTo(cx, cy)
            prev_ctrl = None

        elif c == "V":
            for v in nums:
                cy = cy + v if rel else v
                path.lineTo(cx, cy)
            prev_ctrl = None

        elif c == "C":
            for i in range(0, len(nums) - 5, 6):
                args = nums[i:i + 6]
                if rel:
                    args = [args[0] + cx, args[1] + cy, args[2] + cx,
                            args[3] + cy, args[4] + cx, args[5] + cy]
                path.cubicTo(args[0], args[1], args[2], args[3], args[4], args[5])
                prev_ctrl = (args[2], args[3])
                cx, cy = args[4], args[5]

        elif c == "Q":
            for i in range(0, len(nums) - 3, 4):
                args = nums[i:i + 4]
                if rel:
                    args = [args[0] + cx, args[1] + cy, args[2] + cx, args[3] + cy]
                path.quadTo(args[0], args[1], args[2], args[3])
                prev_ctrl = (args[0], args[1])
                cx, cy = args[2], args[3]

        elif c == "A":
            for i in range(0, len(nums) - 6, 7):
                rx, ry, _rot, large, sweep, ex, ey = nums[i:i + 7]
                nx = ex + cx if rel else ex
                ny = ey + cy if rel else ey
                _arc_to(path, cx, cy, nx, ny, max(rx, ry), bool(large), bool(sweep))
                cx, cy = nx, ny
            prev_ctrl = None

    return path


def _arc_to(path: QPainterPath, x1, y1, x2, y2, r, large, sweep):
    """Append a circular arc to the path (SVG endpoint parameterization)."""
    dx, dy = x1 - x2, y1 - y2
    chord = math.hypot(dx, dy)
    if chord == 0:
        return
    r = max(r, chord / 2.0)

    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    x1p, y1p = dx / 2.0, dy / 2.0
    sign = 1.0 if large == sweep else -1.0
    num = r * r - (x1p * x1p + y1p * y1p)
    factor = sign * math.sqrt(max(0.0, num / (x1p * x1p + y1p * y1p)))
    cxp, cyp = -factor * y1p, factor * x1p
    cx, cy = mx + cxp, my + cyp

    start_ang = math.atan2(y1p - cyp, x1p - cxp)
    end_ang = math.atan2(-y1p - cyp, -x1p - cxp)
    delta = end_ang - start_ang
    if sweep and delta < 0:
        delta += 2 * math.pi
    if not sweep and delta > 0:
        delta -= 2 * math.pi

    segments = max(1, int(math.ceil(abs(delta) / (math.pi / 2))))
    step = delta / segments
    kappa = (4.0 / 3.0) * math.tan(step / 4.0)
    px, py = x1, y1
    for _ in range(segments):
        a1 = start_ang + step
        c1x = px + kappa * math.cos(start_ang + math.pi / 2) * r
        c1y = py + kappa * math.sin(start_ang + math.pi / 2) * r
        ex = cx + r * math.cos(a1)
        ey = cy + r * math.sin(a1)
        c2x = ex + kappa * math.cos(a1 - math.pi / 2) * r
        c2y = ey + kappa * math.sin(a1 - math.pi / 2) * r
        path.cubicTo(c1x, c1y, c2x, c2y, ex, ey)
        px, py = ex, ey
        start_ang = a1


# --------------------------------------------------------------------------- #
# Icon definitions (Feather/lucide-style stroke paths on a 24 box)
# --------------------------------------------------------------------------- #

_STROKE = {
    "plus": "M12 5v14M5 12h14",
    "arrow-up": "M12 19V5M5 12l7-7 7 7",
    "arrow-right": "M5 12h14M12 5l7 7-7 7",
    "arrow-left": "M19 12H5M12 19l-7-7 7-7",
    "chevron-left": "M15 18l-6-6 6-6",
    "chevron-right": "M9 18l6-6-6-6",
    "chevron-down": "M6 9l6 6 6-6",
    "chevron-up": "M18 15l-6-6-6 6",
    "x": "M18 6L6 18M6 6l12 12",
    "minus": "M5 12h14",
    "search": "M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM21 21l-4.35-4.35",
    "mic": (
        "M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"
        "M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"
    ),
    "paperclip": (
        "m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84"
        "l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"
    ),
    "image": (
        "M3 5h18v14H3z"
        "M21 15l-5-5L5 21"
        "M8.5 9.5a1.5 1.5 0 1 0 0-.01"
    ),
    "camera": (
        "M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9"
        "a2 2 0 0 0-2-2h-3l-2.5-3z"
        "M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"
    ),
    "monitor": "M2 4h20v12H2zM8 21h8M12 16v5",
    "crosshair": (
        "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"
        "M22 12h-4M6 12H2M12 6V2M12 22v-4"
        "M12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z"
    ),
    "target": (
        "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"
        "M12 6a6 6 0 1 0 0 12 6 6 0 0 0 0-12z"
        "M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z"
    ),
    "chat": "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    "trash": (
        "M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"
        "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"
    ),
    "user": "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 7a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    "clock": "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 6v6l4 2",
    "file": "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
    "moon": "M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z",
    "eye": "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "terminal": "m4 17 6-6-6-6M12 19h8",
    "sliders": (
        "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"
        "M1 14h6M9 8h6M17 16h6"
    ),
    "volume": "M11 5L6 9H2v6h4l5 4V5z"
              "M15.54 8.46a5 5 0 0 1 0 7.07"
              "M19.07 4.93a10 10 0 0 1 0 14.14",
    "palette": (
        "M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.93 0 1.65-.75 1.65-1.68"
        "c0-.44-.17-.84-.44-1.12-.28-.29-.44-.66-.44-1.13a1.64 1.64 0 0 1 1.67-1.67"
        "h1.99C17.94 16.4 20.44 13.9 20.44 10.45 20.44 5.98 16.49 2 12 2z"
        "M7.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM12 6.5a1.5 1.5 0 1 0 0-3 "
        "1.5 1.5 0 0 0 0 3zM16.5 9a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"
    ),
    "globe": (
        "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"
        "M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 "
        "15.3 15.3 0 0 1 4-10z"
    ),
    "keyboard": (
        "M2 6h20v12H2z"
        "M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"
    ),
    "download": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
    "check": "M20 6 9 17l-5-5",
    "copy": "M8 8h12v12H8zM16 8V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2",
    "menu": "M3 6h18M3 12h18M3 18h18",
    "panel-left": (
        "M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"
        "M9 4v16"
    ),
    "panel-right": (
        "M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"
        "M15 4v16"
    ),
    "zap": "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
    "star": "M12 2l2.9 6.3 6.9.9-5 4.8 1.2 6.8-6-3.4-6 3.4 1.2-6.8-5-4.8 6.9-.9z",
    "send": (
        "M14.54 21.69a.5.5 0 0 0 .94-.02l6.5-19a.5.5 0 0 0-.63-.63l-19 6.5"
        "a.5.5 0 0 0-.02.94l7.93 3.18a2 2 0 0 1 1.11 1.11z"
        "M21.85 2.15 10.91 13.09"
    ),
    "maximize": (
        "M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3"
        "M16 21h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"
    ),
    "restore": (
        "M9 3H5a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"
        "M15 21h4a2 2 0 0 0 2-2v-4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2z"
    ),
    "book": (
        "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15z"
        "M20 17v5H6.5a2.5 2.5 0 0 1 0-5"
    ),
    "wifi": (
        "M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0"
        "M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"
    ),
    "refresh": (
        "M23 4v6h-6M1 20v-6h6"
        "M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"
    ),
    "stop": "M6 6h12v12H6z",
    "play": "M6 3l14 9-14 9z",
    "info": "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 16v-4M12 8h.01",
    "folder": "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
    "grip": "M8 6h.01M8 12h.01M8 18h.01M16 6h.01M16 12h.01M16 18h.01",
}

_SOLID = {
    "star-solid": "M12 2l2.9 6.3 6.9.9-5 4.8 1.2 6.8-6-3.4-6 3.4 1.2-6.8-5-4.8 6.9-.9z",
    "sparkle-solid": "M12 2Q13.6 9.4 22 12Q13.6 14.6 12 22Q10.4 14.6 2 12Q10.4 9.4 12 2z",
}


def _gear_path() -> QPainterPath:
    """A clean gear: outer circle + 8 teeth + hub, stroked as one path."""
    path = QPainterPath()
    path.addEllipse(QRectF(8.4, 8.4, 7.2, 7.2))
    path.addEllipse(QRectF(11.3, 11.3, 1.4, 1.4))
    cx, cy = 12.0, 12.0
    inner, outer = 7.2, 9.6
    for i in range(8):
        ang = math.radians(i * 45 + 22.5)
        ix, iy = cx + inner * math.cos(ang), cy + inner * math.sin(ang)
        ox, oy = cx + outer * math.cos(ang), cy + outer * math.sin(ang)
        ax = cx + (inner + 0.6) * math.cos(ang - 0.30)
        ay = cy + (inner + 0.6) * math.sin(ang - 0.30)
        bx = cx + (inner + 0.6) * math.cos(ang + 0.30)
        by = cy + (inner + 0.6) * math.sin(ang + 0.30)
        path.moveTo(ix, iy)
        path.lineTo(ax, ay)
        path.lineTo(ox, oy)
        path.lineTo(bx, by)
        path.lineTo(ix, iy)
    return path


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def pixmap(name: str, color: str, size: int = 18, *, glow: bool = False) -> QPixmap:
    """Render an icon to a pixmap at the requested size."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)

    if glow:
        glow_painter_color = QColor(color)
        glow_painter_color.setAlpha(60)
        painter.setBrush(glow_painter_color)
        painter.setPen(Qt.NoPen)
        g = QPainterPath()
        g.addEllipse(QRectF(size * 0.10, size * 0.10, size * 0.80, size * 0.80))
        painter.drawPath(g)

    if name == "gear":
        path = _gear_path()
    elif name in _SOLID:
        path = parse_path(_SOLID[name])
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)
        painter.end()
        return pm
    else:
        path = parse_path(_STROKE[name])

    painter.scale(size / _CANVAS, size / _CANVAS)
    pen = QPen(QColor(color))
    pen.setWidthF(1.75)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)
    painter.end()
    return pm


def icon(name: str, color: str, size: int = 18, *, glow: bool = False) -> QIcon:
    """Render an icon and return it as a QIcon."""
    return QIcon(pixmap(name, color, size, glow=glow))

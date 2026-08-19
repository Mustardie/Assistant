"""Generated overlay assets: text, shapes, and subtitle files.

Context for why this module exists at all: Premiere's scripting API cannot
build a native Essential Graphics text layer from nothing. There is no
``createTextLayer``. The two real options are

  1. place a ``.mogrt`` template and drive its parameters by script, which
     keeps the text live and editable in Premiere but requires a template to
     exist for every look you want; or
  2. render the text yourself and place it as an image overlay, which gives
     total typographic control and works with zero setup.

Both are implemented; this module is (2). It matters that the output is an
ordinary timeline clip, because then every other operation in the catalog --
transforms, keyframes, effects, transitions -- applies to text and shapes for
free. That is what makes "animate the title" work without a bespoke
text-animation subsystem.

Rendering uses Pillow, already a dependency of this repo.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import re
from typing import Optional, Sequence

from premiere.errors import PremiereError, ValidationError

logger = logging.getLogger("nova.premiere.assets")

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "premiere_assets",
)

#: Rendered at 2x the sequence frame size so a text overlay can be scaled up or
#: pushed into without going soft. The cost is memory during render only.
SUPERSAMPLE = 2


def cache_dir() -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return _CACHE_DIR


# --------------------------------------------------------------------------
# Colour handling
# --------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3,8})$")

_NAMED_COLORS = {
    "black": (0, 0, 0, 255), "white": (255, 255, 255, 255),
    "red": (255, 0, 0, 255), "green": (0, 200, 0, 255),
    "blue": (0, 90, 255, 255), "yellow": (255, 214, 0, 255),
    "orange": (255, 140, 0, 255), "purple": (150, 60, 220, 255),
    "pink": (255, 90, 170, 255), "cyan": (0, 210, 230, 255),
    "grey": (128, 128, 128, 255), "gray": (128, 128, 128, 255),
    "transparent": (0, 0, 0, 0),
}


def parse_color(value, *, default=(255, 255, 255, 255), path: str = "color") -> tuple:
    """Accept ``"#RRGGBB"``, ``"#RRGGBBAA"``, ``"#RGB"``, a name, or ``[r,g,b,a]``."""
    if value is None:
        return tuple(default)
    if isinstance(value, (list, tuple)):
        parts = [int(max(0, min(255, round(float(v))))) for v in value]
        if len(parts) == 3:
            parts.append(255)
        if len(parts) != 4:
            raise ValidationError(
                "a colour list needs 3 or 4 values (r, g, b[, a]) 0-255", path=path
            )
        return tuple(parts)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _NAMED_COLORS:
            return _NAMED_COLORS[key]
        match = _HEX_RE.match(key)
        if match:
            digits = match.group(1)
            if len(digits) == 3:
                digits = "".join(c * 2 for c in digits)
            if len(digits) == 6:
                digits += "ff"
            if len(digits) != 8:
                raise ValidationError(
                    f"unrecognised colour {value!r}",
                    hint='use "#RRGGBB", "#RRGGBBAA" or [r, g, b, a]',
                    path=path,
                )
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4, 6))
    raise ValidationError(
        f"unrecognised colour {value!r}",
        hint='use "#RRGGBB", "#RRGGBBAA", a colour name, or [r, g, b, a]',
        path=path,
    )


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

_WEIGHT_TOKENS = {
    "thin": ["thin", "hairline"],
    "light": ["light"],
    "regular": ["regular", "book", ""],
    "medium": ["medium"],
    "semibold": ["semibold", "demibold"],
    "bold": ["bold"],
    "black": ["black", "heavy", "extrabold"],
}


def _font_dirs() -> list:
    system = platform.system()
    if system == "Windows":
        return [
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Microsoft", "Windows", "Fonts"),
        ]
    if system == "Darwin":
        return ["/System/Library/Fonts", "/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts",
            os.path.expanduser("~/.local/share/fonts"),
            os.path.expanduser("~/.fonts")]


_font_index: Optional[dict] = None


def font_index(refresh: bool = False) -> dict:
    """Map lowercase font file stem -> path, built once per process."""
    global _font_index
    if _font_index is not None and not refresh:
        return _font_index
    index: dict = {}
    for directory in _font_dirs():
        if not os.path.isdir(directory):
            continue
        try:
            for root, _dirs, files in os.walk(directory):
                for name in files:
                    if name.lower().endswith((".ttf", ".otf", ".ttc")):
                        index.setdefault(os.path.splitext(name)[0].lower(),
                                         os.path.join(root, name))
        except OSError as exc:
            logger.debug("Could not scan font dir %s: %s", directory, exc)
    _font_index = index
    return index


def list_fonts(search: str = "") -> list:
    """Font families available for rendering (backs ``caps.fonts``)."""
    names = sorted({_family_of(stem) for stem in font_index()})
    if search:
        needle = search.lower()
        names = [n for n in names if needle in n.lower()]
    return names


def _family_of(stem: str) -> str:
    base = re.split(r"[-_]", stem)[0]
    return re.sub(r"(?i)(bold|italic|regular|light|medium|black|thin|semibold)$",
                  "", base).strip() or stem


def resolve_font(family: str, weight: str = "regular", italic: bool = False,
                 size: float = 72.0):
    """Best available ImageFont for a family/weight/italic request.

    Falls back rather than failing: a missing font should downgrade the look,
    not abort an edit halfway through a batch.
    """
    from PIL import ImageFont

    if family and os.path.isfile(family):
        return ImageFont.truetype(family, int(size))

    index = font_index()
    target = (family or "arial").lower().replace(" ", "")
    tokens = _WEIGHT_TOKENS.get(weight.lower(), [weight.lower()])

    candidates = [stem for stem in index
                  if stem.replace(" ", "").startswith(target)]
    if not candidates:
        candidates = [stem for stem in index if target in stem.replace(" ", "")]

    def score(stem: str) -> tuple:
        lowered = stem.lower()
        weight_hit = any(tok and tok in lowered for tok in tokens)
        italic_hit = ("italic" in lowered or "oblique" in lowered)
        return (
            0 if weight_hit else 1,
            0 if italic_hit == bool(italic) else 1,
            len(stem),
        )

    for stem in sorted(candidates, key=score):
        try:
            return ImageFont.truetype(index[stem], int(size))
        except OSError:
            continue

    for fallback in ("arial", "dejavusans", "liberationsans-regular",
                     "helvetica", "segoeui", "notosans-regular"):
        for stem, path in index.items():
            if stem.startswith(fallback):
                try:
                    return ImageFont.truetype(path, int(size))
                except OSError:
                    continue

    logger.warning("No usable font for %r; falling back to Pillow's default",
                   family)
    try:
        return ImageFont.load_default(size=int(size))
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


# --------------------------------------------------------------------------
# Text rendering
# --------------------------------------------------------------------------

def _wrap(text: str, font, max_px: float, draw, letter_spacing: float) -> list:
    """Word-wrap to a pixel width, honouring explicit newlines."""
    lines: list = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        if max_px <= 0:
            lines.append(paragraph)
            continue
        words, current = paragraph.split(), ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if _text_width(draw, candidate, font, letter_spacing) <= max_px or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines or [""]


def _text_width(draw, text: str, font, letter_spacing: float) -> float:
    if not text:
        return 0.0
    width = draw.textlength(text, font=font)
    if letter_spacing:
        width += letter_spacing * max(0, len(text) - 1)
    return width


def _draw_tracked(draw, xy, text: str, font, fill, letter_spacing: float,
                  stroke_width: int = 0, stroke_fill=None) -> None:
    """Draw text, applying letter spacing by placing glyphs individually."""
    if not letter_spacing:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        return
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += draw.textlength(char, font=font) + letter_spacing


def render_text(
    text: str,
    *,
    frame_size: Sequence[int] = (1920, 1080),
    font: str = "Arial",
    size: float = 72.0,
    weight: str = "bold",
    italic: bool = False,
    color="#FFFFFF",
    align: str = "center",
    line_spacing: float = 1.15,
    letter_spacing: float = 0.0,
    max_width: Optional[float] = None,
    stroke_color=None,
    stroke_width: float = 0.0,
    shadow: Optional[dict] = None,
    background: Optional[dict] = None,
    out_path: Optional[str] = None,
) -> dict:
    """Render styled text to a transparent PNG the size of the video frame.

    Returns ``{"path", "width", "height", "text_box"}``. The image is
    full-frame so placing it is a pure transform: the caller sets Position and
    Scale on the resulting clip, and never has to reason about the PNG's own
    dimensions.
    """
    from PIL import Image, ImageDraw, ImageFilter

    frame_w, frame_h = int(frame_size[0]), int(frame_size[1])
    scale = SUPERSAMPLE
    canvas_w, canvas_h = frame_w * scale, frame_h * scale

    px_size = float(size) * scale
    tracking = float(letter_spacing) * scale
    stroke_px = int(round(float(stroke_width) * scale))

    pil_font = resolve_font(font, weight, italic, px_size)
    fill = parse_color(color, path="color")
    stroke_rgba = parse_color(stroke_color, default=(0, 0, 0, 255),
                              path="stroke_color") if stroke_color else None

    image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    max_px = (float(max_width) * canvas_w) if max_width else (canvas_w * 0.86)
    lines = _wrap(str(text), pil_font, max_px, draw, tracking)

    ascent, descent = _font_metrics(pil_font)
    line_h = (ascent + descent) * float(line_spacing)
    block_h = line_h * len(lines)
    widths = [_text_width(draw, line, pil_font, tracking) for line in lines]
    block_w = max(widths) if widths else 0.0

    origin_y = (canvas_h - block_h) / 2.0

    # Background plate, drawn first so text and stroke sit on top of it.
    if background:
        pad = float(background.get("padding", 24)) * scale
        radius = float(background.get("radius", 12)) * scale
        bg_color = parse_color(background.get("color", "#000000A0"),
                               path="background.color")
        left = (canvas_w - block_w) / 2.0 - pad
        right = (canvas_w + block_w) / 2.0 + pad
        top = origin_y - pad
        bottom = origin_y + block_h + pad
        plate = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle(
            [left, top, right, bottom], radius=max(0.0, radius), fill=bg_color
        )
        image = Image.alpha_composite(image, plate)
        draw = ImageDraw.Draw(image)

    def line_x(width: float) -> float:
        if align == "left":
            return (canvas_w - block_w) / 2.0
        if align == "right":
            return (canvas_w + block_w) / 2.0 - width
        return (canvas_w - width) / 2.0

    # Drop shadow: rendered on its own layer and blurred, which is the only way
    # to get a real soft shadow rather than an offset duplicate.
    if shadow:
        offset = shadow.get("offset", [4, 4])
        dx, dy = float(offset[0]) * scale, float(offset[1]) * scale
        blur = float(shadow.get("blur", 6)) * scale
        shadow_color = parse_color(shadow.get("color", "#000000C0"),
                                   path="shadow.color")
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(layer)
        for i, line in enumerate(lines):
            _draw_tracked(shadow_draw,
                          (line_x(widths[i]) + dx, origin_y + i * line_h + dy),
                          line, pil_font, shadow_color, tracking)
        if blur > 0:
            layer = layer.filter(ImageFilter.GaussianBlur(blur / 2.0))
        image = Image.alpha_composite(image, layer)
        draw = ImageDraw.Draw(image)

    for i, line in enumerate(lines):
        _draw_tracked(draw, (line_x(widths[i]), origin_y + i * line_h),
                      line, pil_font, fill, tracking,
                      stroke_width=stroke_px, stroke_fill=stroke_rgba)

    image = image.resize((frame_w, frame_h), Image.LANCZOS)

    path = out_path or _cache_path("text", {
        "text": text, "font": font, "size": size, "weight": weight,
        "italic": italic, "color": color, "align": align,
        "line_spacing": line_spacing, "letter_spacing": letter_spacing,
        "max_width": max_width, "stroke_color": stroke_color,
        "stroke_width": stroke_width, "shadow": shadow,
        "background": background, "frame": [frame_w, frame_h],
    })
    image.save(path, "PNG")
    return {
        "path": path,
        "width": frame_w,
        "height": frame_h,
        "lines": len(lines),
        "text_box": [block_w / scale, block_h / scale],
    }


def _font_metrics(font) -> tuple:
    try:
        ascent, descent = font.getmetrics()
        return float(ascent), float(descent)
    except AttributeError:
        # Bitmap default font has no metrics; approximate from a sample glyph.
        try:
            box = font.getbbox("Ay")
            return float(box[3] - box[1]), float(box[3]) * 0.25
        except Exception:  # noqa: BLE001
            return 32.0, 8.0


# --------------------------------------------------------------------------
# Shape rendering
# --------------------------------------------------------------------------

def render_shape(
    shape: str,
    *,
    frame_size: Sequence[int] = (1920, 1080),
    size: Sequence[float] = (0.3, 0.1),
    fill="#FFFFFF",
    stroke_color=None,
    stroke_width: float = 0.0,
    radius: float = 0.0,
    rotation: float = 0.0,
    points: Optional[Sequence] = None,
    sides: Optional[int] = None,
    out_path: Optional[str] = None,
) -> dict:
    """Render a shape to a full-frame transparent PNG, centred.

    Same full-frame convention as ``render_text``: the shape is centred in the
    image, so positioning it on the timeline is just Motion > Position.
    """
    from PIL import Image, ImageDraw

    frame_w, frame_h = int(frame_size[0]), int(frame_size[1])
    scale = SUPERSAMPLE
    canvas_w, canvas_h = frame_w * scale, frame_h * scale

    image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    fill_rgba = parse_color(fill, path="fill")
    stroke_rgba = parse_color(stroke_color, default=(0, 0, 0, 255),
                              path="stroke_color") if stroke_color else None
    stroke_px = int(round(float(stroke_width) * scale))

    shape_w = float(size[0]) * canvas_w
    shape_h = float(size[1]) * canvas_h
    cx, cy = canvas_w / 2.0, canvas_h / 2.0
    box = [cx - shape_w / 2.0, cy - shape_h / 2.0,
           cx + shape_w / 2.0, cy + shape_h / 2.0]

    kind = shape.lower()
    if kind == "rectangle":
        draw.rectangle(box, fill=fill_rgba, outline=stroke_rgba, width=stroke_px)
    elif kind == "rounded_rect":
        draw.rounded_rectangle(box, radius=float(radius) * scale, fill=fill_rgba,
                               outline=stroke_rgba, width=stroke_px)
    elif kind in ("ellipse", "circle"):
        if kind == "circle":
            side = min(shape_w, shape_h)
            box = [cx - side / 2.0, cy - side / 2.0, cx + side / 2.0, cy + side / 2.0]
        draw.ellipse(box, fill=fill_rgba, outline=stroke_rgba, width=stroke_px)
    elif kind == "line":
        thickness = max(1, stroke_px or int(shape_h))
        line_color = stroke_rgba or fill_rgba
        draw.line([box[0], cy, box[2], cy], fill=line_color, width=thickness)
    elif kind == "triangle":
        draw.polygon([(cx, box[1]), (box[2], box[3]), (box[0], box[3])],
                     fill=fill_rgba, outline=stroke_rgba)
    elif kind == "arrow":
        head = shape_w * 0.4
        shaft = shape_h * 0.35
        draw.polygon([
            (box[0], cy - shaft / 2), (box[2] - head, cy - shaft / 2),
            (box[2] - head, box[1]), (box[2], cy),
            (box[2] - head, box[3]), (box[2] - head, cy + shaft / 2),
            (box[0], cy + shaft / 2),
        ], fill=fill_rgba, outline=stroke_rgba)
    elif kind == "polygon":
        if points:
            vertices = [(float(p[0]) * canvas_w, float(p[1]) * canvas_h)
                        for p in points]
        else:
            count = int(sides or 5)
            radius_x, radius_y = shape_w / 2.0, shape_h / 2.0
            vertices = [
                (cx + radius_x * math.cos(2 * math.pi * i / count - math.pi / 2),
                 cy + radius_y * math.sin(2 * math.pi * i / count - math.pi / 2))
                for i in range(count)
            ]
        draw.polygon(vertices, fill=fill_rgba, outline=stroke_rgba)
    else:
        raise ValidationError(f"unsupported shape {shape!r}", path="shape")

    if rotation:
        image = image.rotate(-float(rotation), resample=Image.BICUBIC, center=(cx, cy))

    image = image.resize((frame_w, frame_h), Image.LANCZOS)
    path = out_path or _cache_path("shape", {
        "shape": shape, "size": list(size), "fill": fill,
        "stroke_color": stroke_color, "stroke_width": stroke_width,
        "radius": radius, "rotation": rotation,
        "points": list(points) if points else None, "sides": sides,
        "frame": [frame_w, frame_h],
    })
    image.save(path, "PNG")
    return {"path": path, "width": frame_w, "height": frame_h}


def _cache_path(kind: str, spec: dict) -> str:
    """Content-addressed filename.

    Identical text rendered twice reuses one PNG, which keeps the project panel
    from filling with near-duplicate stills over a long editing session.
    """
    digest = hashlib.sha1(
        json.dumps(spec, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cache_dir(), f"{kind}_{digest}.png")


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rest = divmod(total_ms, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def split_segment_text(text: str, max_chars: int = 42) -> list:
    """Break a caption line at word boundaries near ``max_chars``.

    Subtitle readability is mostly line length; long single lines are the most
    common reason auto-generated captions look amateur.
    """
    words = str(text).split()
    if not words:
        return [""]
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def write_srt(segments: Sequence[dict], out_path: Optional[str] = None,
              max_chars: int = 42) -> str:
    """Write transcript segments to an .srt file and return its path."""
    if not segments:
        raise ValidationError("no caption segments given", path="segments")

    blocks = []
    for index, segment in enumerate(segments, start=1):
        text = "\n".join(split_segment_text(segment["text"], max_chars))
        blocks.append(
            f"{index}\n"
            f"{_srt_timestamp(segment['start'])} --> {_srt_timestamp(segment['end'])}\n"
            f"{text}\n"
        )
    body = "\n".join(blocks)

    path = out_path or _cache_path("captions", {"body": body}).replace(".png", ".srt")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        raise PremiereError(f"could not write subtitle file: {exc}") from exc
    return path

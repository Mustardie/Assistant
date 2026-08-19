"""Generated overlay assets: colour parsing, text/shape rendering, subtitles."""
from __future__ import annotations

import os

import pytest

from premiere.assets import (
    parse_color, render_shape, render_text, split_segment_text, write_srt,
    _srt_timestamp,
)
from premiere.errors import ValidationError


def alpha_pixels(path):
    """Count pixels that are not fully transparent."""
    from PIL import Image
    image = Image.open(path)
    # histogram()[0] is the fully-transparent bucket; everything above it is
    # drawn content. Cheaper than iterating and avoids the deprecated getdata().
    return sum(image.getchannel("A").histogram()[1:])


class TestColors:
    def test_hex_forms(self):
        assert parse_color("#FF0000") == (255, 0, 0, 255)
        assert parse_color("#FF000080") == (255, 0, 0, 128)
        assert parse_color("#F00") == (255, 0, 0, 255)
        assert parse_color("FF0000") == (255, 0, 0, 255)

    def test_named(self):
        assert parse_color("white") == (255, 255, 255, 255)
        assert parse_color("transparent") == (0, 0, 0, 0)

    def test_list_forms(self):
        assert parse_color([255, 128, 0]) == (255, 128, 0, 255)
        assert parse_color([255, 128, 0, 64]) == (255, 128, 0, 64)

    def test_clamped(self):
        assert parse_color([300, -20, 0, 999]) == (255, 0, 0, 255)

    def test_none_uses_default(self):
        assert parse_color(None, default=(1, 2, 3, 4)) == (1, 2, 3, 4)

    def test_rejects_nonsense(self):
        with pytest.raises(ValidationError, match="unrecognised colour"):
            parse_color("burnt sienna")

    def test_rejects_wrong_length_list(self):
        with pytest.raises(ValidationError, match="3 or 4 values"):
            parse_color([1, 2])


class TestRenderText:
    def test_produces_a_full_frame_png(self):
        result = render_text("Hello", frame_size=(1280, 720))
        assert result["width"] == 1280 and result["height"] == 720
        assert os.path.isfile(result["path"])
        assert alpha_pixels(result["path"]) > 0

    def test_larger_text_covers_more_pixels(self):
        small = render_text("Size Test", size=40)
        large = render_text("Size Test", size=140)
        assert alpha_pixels(large["path"]) > alpha_pixels(small["path"]) * 2

    def test_wrapping_produces_multiple_lines(self):
        result = render_text(
            "the quick brown fox jumps over the lazy dog and keeps on running",
            size=60, max_width=0.4,
        )
        assert result["lines"] > 1

    def test_explicit_newlines_are_honoured(self):
        assert render_text("one\ntwo\nthree", size=50)["lines"] == 3

    def test_stroke_adds_pixels(self):
        plain = render_text("Stroke", size=90)
        stroked = render_text("Stroke", size=90, stroke_color="#000000",
                              stroke_width=6)
        assert alpha_pixels(stroked["path"]) > alpha_pixels(plain["path"])

    def test_background_plate_adds_a_lot_of_pixels(self):
        plain = render_text("Plate", size=60)
        plated = render_text("Plate", size=60,
                             background={"color": "#000000C0", "padding": 30,
                                         "radius": 12})
        assert alpha_pixels(plated["path"]) > alpha_pixels(plain["path"]) * 3

    def test_shadow_adds_pixels(self):
        plain = render_text("Shadow", size=80)
        shadowed = render_text("Shadow", size=80,
                               shadow={"color": "#000000FF", "offset": [8, 8],
                                       "blur": 10})
        assert alpha_pixels(shadowed["path"]) > alpha_pixels(plain["path"])

    def test_letter_spacing_widens_the_text_box(self):
        tight = render_text("SPACING", size=70, letter_spacing=0)
        loose = render_text("SPACING", size=70, letter_spacing=20)
        assert loose["text_box"][0] > tight["text_box"][0]

    def test_identical_input_is_cached_to_one_file(self):
        first = render_text("Cached", size=64)
        second = render_text("Cached", size=64)
        assert first["path"] == second["path"]

    def test_different_input_gets_a_different_file(self):
        assert render_text("A", size=64)["path"] != render_text("B", size=64)["path"]

    def test_missing_font_falls_back_rather_than_failing(self):
        result = render_text("Fallback", font="NoSuchFontExists12345", size=60)
        assert alpha_pixels(result["path"]) > 0


class TestRenderShape:
    @pytest.mark.parametrize("shape", [
        "rectangle", "rounded_rect", "ellipse", "circle", "line",
        "triangle", "arrow", "polygon",
    ])
    def test_every_shape_renders(self, shape):
        result = render_shape(shape, size=(0.4, 0.25), fill="#FFFFFF", sides=6)
        assert alpha_pixels(result["path"]) > 0

    def test_bigger_shape_covers_more(self):
        small = render_shape("rectangle", size=(0.1, 0.1))
        large = render_shape("rectangle", size=(0.5, 0.5))
        assert alpha_pixels(large["path"]) > alpha_pixels(small["path"]) * 5

    def test_polygon_from_explicit_points(self):
        result = render_shape("polygon",
                              points=[[0.2, 0.2], [0.8, 0.3], [0.5, 0.8]])
        assert alpha_pixels(result["path"]) > 0

    def test_rotation_is_applied(self):
        straight = render_shape("rectangle", size=(0.6, 0.05))
        rotated = render_shape("rectangle", size=(0.6, 0.05), rotation=45)
        assert straight["path"] != rotated["path"]

    def test_unknown_shape_rejected(self):
        with pytest.raises(ValidationError, match="unsupported shape"):
            render_shape("dodecahedron")


class TestSubtitles:
    def test_timestamp_format(self):
        assert _srt_timestamp(0) == "00:00:00,000"
        assert _srt_timestamp(3661.5) == "01:01:01,500"

    def test_srt_structure(self, tmp_path):
        path = write_srt([
            {"start": 0.0, "end": 2.0, "text": "first"},
            {"start": 2.0, "end": 4.0, "text": "second"},
        ], out_path=str(tmp_path / "out.srt"))
        body = open(path, encoding="utf-8").read()
        assert body.startswith("1\n")
        assert "00:00:00,000 --> 00:00:02,000" in body
        assert "\n2\n" in body
        assert "second" in body

    def test_long_lines_are_split(self):
        lines = split_segment_text(
            "this is a very long caption line that should be broken up", 20)
        assert len(lines) > 1
        assert all(len(line) <= 25 for line in lines)

    def test_long_words_are_not_lost(self):
        lines = split_segment_text("supercalifragilisticexpialidocious", 10)
        assert "supercalifragilisticexpialidocious" in " ".join(lines)

    def test_empty_segments_rejected(self):
        with pytest.raises(ValidationError, match="no caption segments"):
            write_srt([])

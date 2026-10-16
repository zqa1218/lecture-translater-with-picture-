"""图片内文字翻译：选区过滤、擦除、字号自适应、两种落地方式。"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from image_translator import (
    build_regions,
    erase_regions,
    estimate_colors,
    prepare_image,
    render_plan,
    resolve_font,
    select_regions,
)
from ocr_engine import OcrLine


class FakeEngine:
    """假的 OCR 引擎：直接返回预置结果，避免测试依赖 onnxruntime 与模型文件。"""

    def __init__(self, lines):
        self.lines = lines

    def recognize(self, image, max_side=None):  # noqa: D102
        return list(self.lines)


def _line(text, box, score=0.95):
    x0, y0, x1, y1 = box
    return OcrLine(points=((x0, y0), (x1, y0), (x1, y1), (x0, y1)), text=text, score=score)


def _text_image(text="Anode", size=(300, 120), box=(20, 40, 200, 70)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    path, index = resolve_font()
    if path:
        font = None
        from PIL import ImageFont

        font = ImageFont.truetype(path, 22, index=index)
        draw.text((box[0], box[1]), text, fill=(10, 10, 10), font=font)
    return image


def test_select_regions_filters_low_confidence_and_noise():
    lines = [
        _line("Anode", (0, 0, 50, 20), score=0.9),
        _line("x", (0, 30, 10, 40), score=0.9),          # 太短
        _line("Cathode", (0, 60, 80, 80), score=0.30),   # 置信度太低
        _line("123", (0, 90, 30, 110), score=0.9),       # 纯数字
    ]
    kept = select_regions(lines, min_confidence=0.6, min_chars=2, should_translate=lambda t: t.isalpha())
    assert [line.text for line in kept] == ["Anode"]


def test_estimate_colors_finds_dark_text_on_light_background():
    image = _text_image()
    array = np.array(image)
    line = _line("Anode", (20, 40, 200, 70))
    foreground, background = estimate_colors(array, line)
    assert sum(background) > sum(foreground), "背景应该比文字亮"
    assert sum(foreground) < 300, "文字应该接近黑色"


def test_erase_regions_removes_text():
    image = _text_image()
    array = np.array(image)
    lines = [_line("Anode", (20, 40, 200, 70))]
    erased = erase_regions(array, lines)
    assert not np.array_equal(array, erased)
    region = erased[45:65, 30:190]
    assert region.std() < 12, "擦除后该区域应当接近纯色背景"


def test_build_regions_skips_identical_translation():
    array = np.array(_text_image(size=(200, 80), box=(10, 20, 120, 50)))
    lines = [_line("2.0mA", (10, 20, 120, 50))]
    assert build_regions(array, lines, ["2.0mA"]) == []
    regions = build_regions(array, lines, ["2.0 毫安"])
    assert len(regions) == 1
    assert regions[0].translated_text == "2.0 毫安"


def test_prepare_image_returns_none_when_nothing_to_translate():
    image = Image.new("RGB", (120, 60), "white")
    assert prepare_image(image, engine=FakeEngine([])) is None


def test_render_plan_inpaint_draws_translation():
    image = _text_image()
    engine = FakeEngine([_line("Anode", (20, 40, 200, 70))])
    plan = prepare_image(image, engine=engine)
    assert plan is not None
    result, stats = render_plan(plan, ["阳极"], mode="inpaint")
    assert result is not None
    assert stats.translated == 1
    assert not np.array_equal(np.array(image), np.array(result.convert("RGB")))
    # 重绘后该区域应该有新的深色像素（中文）出现
    crop = np.array(result.convert("RGB"))[35:75, 15:205]
    assert crop.min() < 120


def test_render_plan_overlay_keeps_text_out_of_pixels():
    image = _text_image()
    engine = FakeEngine([_line("Anode", (20, 40, 200, 70))])
    plan = prepare_image(image, engine=engine)
    result, stats = render_plan(plan, ["阳极"], mode="overlay")
    assert result is not None, "叠加模式同样要给出「已擦除原文」的底图"
    assert stats.regions_detail[0].translated_text == "阳极"
    crop = np.array(result.convert("RGB"))[45:65, 30:190]
    assert crop.std() < 12, "叠加模式下原文必须已被擦掉，避免中英重影"


def test_vertical_label_is_detected():
    image = Image.new("RGB", (60, 240), "white")
    engine = FakeEngine([_line("Current", (10, 10, 30, 220))])
    plan = prepare_image(image, engine=engine)
    assert plan is not None
    _, stats = render_plan(plan, ["电流"], mode="overlay")
    assert stats.regions_detail[0].vertical is True


def test_alpha_channel_is_preserved():
    image = Image.new("RGBA", (200, 80), (255, 255, 255, 0))
    engine = FakeEngine([_line("Anode", (10, 20, 120, 50))])
    plan = prepare_image(image, engine=engine)
    result, _stats = render_plan(plan, ["阳极"], mode="inpaint")
    assert result.mode == "RGBA"


def test_prepare_image_accepts_file_object():
    """管线传进来的是 io.BytesIO（图片字节），必须照样能识别。

    之前这里只认路径 / PIL / ndarray，BytesIO 会静默返回 None ——
    结果是整份课件的图片一张都没被翻译。
    """
    image = _text_image()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    engine = FakeEngine([_line("Anode", (20, 40, 200, 70))])
    plan = prepare_image(buffer, engine=engine)
    assert plan is not None
    assert plan.texts() == ["Anode"]


def test_prepare_image_accepts_path(tmp_path):
    image = _text_image()
    path = tmp_path / "figure.png"
    image.save(path)
    engine = FakeEngine([_line("Cathode", (20, 40, 200, 70))])
    plan = prepare_image(str(path), engine=engine)
    assert plan is not None
    assert plan.texts() == ["Cathode"]

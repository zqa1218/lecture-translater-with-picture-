"""图片内文字翻译：OCR → 擦除原文 → 用中文字体重绘译文。

这是原版最大的短板：讲义里以截图形式插入的示意图、图表、图注、坐标轴**从来不会被翻译**。
本模块把它补上，且刻意做成「不依赖任何云服务」的本地流水线：

    图片 ──RapidOCR 检测文本框──▶
           ├─ 估计每块文字的前景色/背景色
           ├─ cv2.inpaint 擦掉原文（只去文字，保留底纹与线条）
           └─ 用系统中文字体把译文按原框重排写回（自动配字号、自动换行、
              竖排文字竖着写、斜排文字转回来写）

两种落地方式：
  * ``inpaint``（默认）：只替换成功翻译的文字区域，图像大小和放置位置不变；
  * ``overlay``：清理原文字后叠加可编辑文本框，图像放置位置不变。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from ocr_engine import OcrEngine, OcrLine

#: Windows 自带的中文字体，按优先级尝试；目标语言是中文时用它保证字形完整。
CJK_FONT_CANDIDATES: tuple[tuple[str, int], ...] = (
    (r"C:\Windows\Fonts\msyh.ttc", 0),      # 微软雅黑
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),    # 微软雅黑 Bold
    (r"C:\Windows\Fonts\simhei.ttf", 0),    # 黑体
    (r"C:\Windows\Fonts\Deng.ttf", 0),      # 等线
    (r"C:\Windows\Fonts\simsun.ttc", 0),    # 宋体
    (r"C:\Windows\Fonts\arial.ttf", 0),
)

VERTICAL_RATIO = 1.6  # 高/宽 超过此值按竖排处理


@dataclass(slots=True)
class TranslatedRegion:
    """一块待重绘/待叠加的文字区域。"""

    box: tuple[float, float, float, float]
    source_text: str
    translated_text: str
    color: tuple[int, int, int]
    background: tuple[int, int, int]
    angle: float = 0.0
    vertical: bool = False
    score: float = 1.0


@dataclass(slots=True)
class ImageTranslateStats:
    regions: int = 0
    translated: int = 0
    skipped: int = 0
    note: str = ""
    regions_detail: list[TranslatedRegion] = field(default_factory=list)


def resolve_font(font_path: str | None = None) -> tuple[str, int]:
    if font_path and os.path.exists(font_path):
        return font_path, 0
    for path, index in CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            return path, index
    return "", 0


def _to_points(line: OcrLine) -> np.ndarray:
    return np.array([[int(round(x)), int(round(y))] for x, y in line.points], dtype=np.int32)


def estimate_colors(array: np.ndarray, line: OcrLine) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """估计该文字块的前景色（文字）与背景色。"""
    height, width = array.shape[:2]
    mask = np.zeros((height, width), np.uint8)
    import cv2

    cv2.fillPoly(mask, [_to_points(line)], 1)
    selected = array[mask.astype(bool)]
    if selected.size == 0:
        return (0, 0, 0), (255, 255, 255)
    background = np.median(selected, axis=0)
    distance = np.linalg.norm(selected.astype(np.float32) - background, axis=1)
    take = max(1, int(len(selected) * 0.10))
    foreground = np.median(selected[np.argsort(distance)[-take:]], axis=0)
    if float(np.linalg.norm(foreground - background)) < 40.0:
        # 对比度不足：按背景亮度给一个安全的黑/白
        luminance = float(np.dot(background, [0.299, 0.587, 0.114]))
        foreground = np.array([255, 255, 255]) if luminance < 140 else np.array([0, 0, 0])
    return (
        tuple(int(v) for v in foreground),
        tuple(int(v) for v in background),
    )


def select_regions(
    lines: list[OcrLine],
    *,
    min_confidence: float = 0.60,
    min_chars: int = 2,
    should_translate=None,
) -> list[OcrLine]:
    kept: list[OcrLine] = []
    for line in lines:
        if line.score < min_confidence:
            continue
        text = line.text.strip()
        if len(text) < min_chars:
            continue
        if should_translate is not None and not should_translate(text):
            continue
        kept.append(line)
    return kept


def build_regions(array: np.ndarray, lines: list[OcrLine], translations: list[str]) -> list[TranslatedRegion]:
    regions: list[TranslatedRegion] = []
    for line, translated in zip(lines, translations):
        if not translated or not str(translated).strip():
            continue
        if str(translated).strip() == line.text.strip():
            continue
        foreground, background = estimate_colors(array, line)
        x0, y0, x1, y1 = line.box
        width = max(1.0, x1 - x0)
        height = max(1.0, y1 - y0)
        regions.append(
            TranslatedRegion(
                box=(x0, y0, x1, y1),
                source_text=line.text,
                translated_text=str(translated).strip(),
                color=foreground,
                background=background,
                angle=line.angle if abs(line.angle) > 3.0 else 0.0,
                vertical=height > width * VERTICAL_RATIO,
                score=line.score,
            )
        )
    return regions


# ---------------------------------------------------------------------- 文字排版
def _font_runs(text, font):
    """微软雅黑缺少下标数字，用 Segoe UI 补齐，避免 T₁/T₂ 变成方框。"""
    from PIL import ImageFont
    fallback = None
    for char in text:
        if char in "₀₁₂₃₄₅₆₇₈₉":
            if fallback is None:
                fallback = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", font.size)
            yield char, fallback
        else:
            yield char, font


def _text_width(text, font):
    if not any(char in "₀₁₂₃₄₅₆₇₈₉" for char in text):
        return font.getlength(text)
    return sum(f.getlength(char) for char, f in _font_runs(text, font))


def _wrap_text(text: str, font, max_width: float) -> list[str]:
    """按像素宽度折行；中文逐字断行，英文优先在空格处断行。"""
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + char
        if _text_width(candidate, font) <= max_width or not current:
            current = candidate
            continue
        if char in " \t，。、；：,.;:" or (" " in current[-6:] and char.isascii()):
            cut = current.rstrip()
            if cut:
                lines.append(cut)
                current = char.lstrip()
            else:
                current = candidate
            continue
        lines.append(current)
        current = char
    if current:
        lines.append(current)
    return lines


def _fit_text(text: str, box_w: float, box_h: float, font_path: str, index: int, vertical: bool):
    from PIL import ImageFont

    for size in range(int(min(256, max(7, box_w if vertical else box_h * 0.85))), 6, -1):
        font = ImageFont.truetype(font_path, size, index=index)
        lines = list(text.replace("\n", "")) if vertical else _wrap_text(text, font, box_w)
        boxes = [f.getbbox(char) for line in lines for char, f in _font_runs(line or " ",font)]
        line_height = max((b[3] for b in boxes), default=size) - min((b[1] for b in boxes),default=0) + 1
        widest = max((_text_width(line,font) for line in lines), default=0)
        if line_height * len(lines) <= box_h and widest <= box_w:
            return font, lines, line_height
    raise ValueError("译文无法在原文字框内清晰排下，保留原文")


def _draw_region(image, region: TranslatedRegion, font_path: str, index: int) -> None:
    from PIL import Image, ImageDraw
    import math

    x0, y0, x1, y1 = region.box
    left, top = max(0, math.floor(x0)), max(0, math.floor(y0))
    right, bottom = min(image.width, math.ceil(x1)), min(image.height, math.ceil(y1))
    width, height = right - left, bottom - top
    if min(width, height) < 1:
        raise ValueError("文字框在图片之外")
    # 所有排版在原文字框的局部透明图层完成，不能越界覆盖箭头或邻近标签。
    font, lines, line_height = _fit_text(region.translated_text, width, height, font_path, index, region.vertical)
    layer = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(layer)
    y = (height - line_height * len(lines)) / 2
    for line in lines:
        if not any(char in "₀₁₂₃₄₅₆₇₈₉" for char in line):
            bounds = font.getbbox(line or " ")
            x = 0 if len(region.source_text) > 40 else (width - font.getlength(line)) / 2
            draw.text((x,y-bounds[1]),line,font=font,fill=tuple(region.color)+(255,))
            y += line_height
            continue
        runs = list(_font_runs(line or " ",font))
        top_offset = min(f.getbbox(char)[1] for char,f in runs)
        x = 0 if len(region.source_text) > 40 else (width - _text_width(line,font)) / 2
        for char, char_font in runs:
            draw.text((x, y - top_offset), char, font=char_font, fill=tuple(region.color) + (255,))
            x += char_font.getlength(char)
        y += line_height
    image.paste(layer, (left, top), layer)


def erase_regions(array: np.ndarray, lines: list[OcrLine], *, dilate: int = 2) -> np.ndarray:
    """用 cv2.inpaint 擦除文字（只去文字，底纹与线条由邻域修补回来）。"""
    import cv2

    mask = np.zeros(array.shape[:2], np.uint8)
    for line in lines:
        cv2.fillPoly(mask, [_to_points(line)], 255)
    if mask.max() == 0:
        return array
    if dilate > 0:
        mask = cv2.dilate(mask, np.ones((dilate * 2 + 1, dilate * 2 + 1), np.uint8), iterations=1)
    result = cv2.inpaint(array, mask, 3, cv2.INPAINT_TELEA)
    # 纯色图注背景直接还原底色，避免修补算法把邻近字形拖成灰色残影。
    for line in lines:
        import math
        a,b,c,d = line.box
        x0,y0,x1,y1 = math.floor(a),math.floor(b),math.ceil(c),math.ceil(d)
        x0,y0 = max(0,x0),max(0,y0)
        x1,y1 = min(array.shape[1],x1),min(array.shape[0],y1)
        patch = array[y0:y1,x0:x1]
        if not patch.size:
            continue
        bg = np.median(patch.reshape(-1,3),axis=0)
        uniform = np.max(np.abs(patch.astype(float)-bg),axis=2) < 12
        if uniform.mean() > 0.72 or (min(bg) > 240 and uniform.mean() > 0.55):
            result[y0:y1,x0:x1] = bg.astype(np.uint8)
            foreground, _ = estimate_colors(array,line)
            if min(bg) > 240 and max(foreground) - min(foreground) < 30:
                # 灰黑字旁边的彩色项目符号/箭头不属于文字，保留其原始像素。
                colored = patch.max(axis=2).astype(int) - patch.min(axis=2).astype(int) > 35
                # ClearType 文字会产生大量红蓝像素；仅在彩色像素很少时才按图形保留。
                if colored.mean() < 0.05:
                    solid = cv2.morphologyEx(
                        colored.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
                    )
                    solid = cv2.dilate(solid, np.ones((3, 3), np.uint8), iterations=1).astype(bool) & colored
                    spread = patch.max(axis=2).astype(int) - patch.min(axis=2).astype(int)
                    dark_gray = ((spread < 35) & (patch.max(axis=2) < 180)).astype(np.uint8)
                    near_text = cv2.dilate(dark_gray, np.ones((5, 5), np.uint8), iterations=1).astype(bool)
                    solid &= ~near_text
                    result[y0:y1,x0:x1][solid] = patch[solid]
    return result


def _expanded_line(line: OcrLine, image_size: tuple[int, int]) -> OcrLine:
    """OCR 框常少包住几像素的字缘；仅为擦除扩一圈，译文仍画在原框内。"""
    width, height = image_size
    x0, y0, x1, y1 = line.box
    pad = min(6.0, max(3.0, round((y1 - y0) * 0.12)))
    left, top = max(0.0, x0 - pad), max(0.0, y0 - pad)
    right, bottom = min(float(width), x1 + pad), min(float(height), y1 + pad)
    return OcrLine(
        ((left, top), (right, top), (right, bottom), (left, bottom)),
        line.text,
        line.score,
    )


# ---------------------------------------------------------------------- 对外入口
@dataclass(slots=True)
class ImagePlan:
    """一张图的「待翻译清单」：原图、像素数组、OCR 命中的文字块。"""

    pil: object
    array: object
    has_alpha: bool
    lines: list[OcrLine]

    def texts(self) -> list[str]:
        return [line.text for line in self.lines]


def prepare_image(
    image,
    *,
    engine: OcrEngine | None = None,
    min_confidence: float = 0.60,
    min_chars: int = 2,
    max_side: int = 1600,
    should_translate=None,
) -> ImagePlan | None:
    """OCR 一张图，返回待翻译清单；没有可翻译文字时返回 ``None``。"""
    from PIL import Image

    if isinstance(image, np.ndarray):
        pil = Image.fromarray(image)
    elif isinstance(image, Image.Image):
        pil = image
    else:
        # 路径与 io.BytesIO 之类的文件对象都交给 PIL 打开。管线里传进来的正是
        # BytesIO（图片字节），这里不能退化成 None，否则整份课件的图片会被静默跳过。
        try:
            pil = Image.open(image)
        except Exception:  # noqa: BLE001
            return None

    has_alpha = pil.mode in ("RGBA", "LA", "P")
    pil = pil.convert("RGBA") if has_alpha else pil.convert("RGB")
    array = np.array(pil.convert("RGB"))
    engine = engine or OcrEngine.shared(max_side)
    lines = engine.recognize(pil.convert("RGB"), max_side=max_side)
    kept = select_regions(
        lines, min_confidence=min_confidence, min_chars=min_chars, should_translate=should_translate
    )
    if not kept:
        return None
    return ImagePlan(pil=pil, array=array, has_alpha=has_alpha, lines=kept)


def render_plan(
    plan: ImagePlan,
    translations: list[str],
    *,
    mode: str = "inpaint",
    font_path: str | None = None,
    log=None,
) -> tuple[object | None, ImageTranslateStats]:
    """按译文重绘图片。``mode='overlay'`` 时只擦除原文，译文交给调用方做文本框。"""
    from PIL import Image

    stats = ImageTranslateStats()
    stats.regions = len(plan.lines)
    regions = build_regions(plan.array, plan.lines, translations)
    stats.translated = len(regions)
    stats.skipped = len(plan.lines) - len(regions)
    stats.regions_detail = regions
    if not regions:
        stats.note = "译文与原文相同或无有效译文，已跳过"
        return None, stats

    path, index = resolve_font(font_path) if mode == "inpaint" else ("", 0)
    if mode == "inpaint" and not path:
        stats.translated = 0
        stats.skipped = len(plan.lines)
        stats.regions_detail = []
        stats.note = "找不到目标字体，已保留原图"
        return None, stats

    result = plan.pil.copy()
    successful = []
    for region in regions:
        line = next(line for line in plan.lines if line.box == region.box and line.text == region.source_text)
        try:
            # 先确认文字能画下，再清理相应原文；未翻译、过长、绘制失败都保留。
            if mode == "inpaint":
                probe = Image.new("RGBA", result.size)
                _draw_region(probe, region, path, index)
            erase_line = _expanded_line(line, result.size)
            # 相邻 OCR 行的扩展擦除框可能重叠；必须从累计结果继续处理，
            # 否则后一行会把前一行已擦掉的英文边缘从原图贴回来。
            erased = erase_regions(np.array(result.convert("RGB")), [erase_line], dilate=0)
            candidate = Image.fromarray(erased)
            if plan.has_alpha:
                candidate = candidate.convert("RGBA")
                candidate.putalpha(plan.pil.getchannel("A"))
            if mode == "inpaint":
                _draw_region(candidate, region, path, index)
            import math
            x0, y0, x1, y1 = erase_line.box
            box = (max(0, math.floor(x0)), max(0, math.floor(y0)),
                   min(result.width, math.ceil(x1)), min(result.height, math.ceil(y1)))
            result.paste(candidate.crop(box), box[:2])
            successful.append(region)
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"图内文字保留原文（{region.source_text[:40]}）：{exc}")
    stats.regions_detail = successful
    stats.translated = len(successful)
    stats.skipped = len(plan.lines) - len(successful)
    stats.note = f"已处理 {len(successful)} 处文字，保留 {stats.skipped} 处原文"
    if plan.has_alpha:
        result.putalpha(plan.pil.getchannel("A"))
    return (result if successful else None), stats


def translate_image(
    image,
    translate_batch,
    *,
    mode: str = "inpaint",
    min_confidence: float = 0.60,
    min_chars: int = 2,
    max_side: int = 1600,
    font_path: str | None = None,
    context: str = "",
    should_translate=None,
    engine: OcrEngine | None = None,
    log=None,
) -> tuple[object | None, ImageTranslateStats]:
    """翻译一张图片里的文字（单图便捷入口；批量场景请用 prepare_image + render_plan）。

    * 没有可翻译内容时返回 ``None``（调用方保留原图）；
    * ``mode='inpaint'`` 返回「已擦除原文 + 已写入译文」的图；
    * ``mode='overlay'`` 返回「已擦除原文」的图，译文放在 ``stats.regions_detail`` 里。
    """
    plan = prepare_image(
        image,
        engine=engine,
        min_confidence=min_confidence,
        min_chars=min_chars,
        max_side=max_side,
        should_translate=should_translate,
    )
    stats = ImageTranslateStats()
    if plan is None:
        stats.note = "未检出需要翻译的文字"
        return None, stats
    translations = translate_batch(plan.texts(), context=context, style="label")
    return render_plan(plan, translations, mode=mode, font_path=font_path, log=log)

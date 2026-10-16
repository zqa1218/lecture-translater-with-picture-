"""离线 OCR 引擎。

集成的开源项目：**RapidAI/RapidOCR**（Apache-2.0，GitHub 8k+ star）
  * 它是 PaddleOCR 的 ONNX 化发行版：模型随包分发、纯 CPU 推理、完全离线、
    中英文混排开箱可用，不需要 token、不需要联网、不需要 torch。
  * 本模块只做薄封装：懒加载、长边降采样、结果归一化。

之所以不用 PaddleOCR 原版：原版要装 paddlepaddle（数百 MB + 各种二进制依赖）；
也不用 EasyOCR：它拖 torch（1 GB 以上）。RapidOCR 是桌面工具里最实用的一档。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

DEFAULT_MAX_SIDE = 1600


@dataclass(slots=True)
class OcrLine:
    """一行 OCR 结果：四点多边形（左上、右上、右下、左下）+ 文本 + 置信度。"""

    points: tuple[tuple[float, float], ...]
    text: str
    score: float

    @property
    def box(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        x0, _y0, x1, _y1 = self.box
        return max(1.0, x1 - x0)

    @property
    def height(self) -> float:
        _x0, y0, _x1, y1 = self.box
        return max(1.0, y1 - y0)

    @property
    def angle(self) -> float:
        """相对水平方向的倾角（度），用于判断是否需要旋转回正。"""
        (x0, y0), (x1, y1) = self.points[0], self.points[1]
        if x1 == x0 and y1 == y0:
            return 0.0
        degree = np.degrees(np.arctan2(y1 - y0, x1 - x0))
        return float(degree)


class OcrEngine:
    """RapidOCR 的懒加载封装（首次调用才初始化模型）。"""

    _shared = None

    def __init__(self, max_side: int = DEFAULT_MAX_SIDE) -> None:
        self.max_side = int(max_side) if max_side else DEFAULT_MAX_SIDE
        self._engine = None

    # ------------------------------------------------------------------ 内核
    def _ensure(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR(intra_op_num_threads=min(4, os.cpu_count() or 1), inter_op_num_threads=1)
        return self._engine

    @classmethod
    def shared(cls, max_side: int = DEFAULT_MAX_SIDE) -> "OcrEngine":
        if cls._shared is None:
            cls._shared = cls(max_side)
        return cls._shared

    # ------------------------------------------------------------------ 识别
    def recognize(self, image, *, max_side: int | None = None) -> list[OcrLine]:
        """识别一张图；``image`` 可以是路径、PIL.Image 或 numpy 数组。"""
        limit = max(256, int(max_side or self.max_side))
        original, _ = _to_array(image, 0)
        array, scale = _to_array(original, limit) if original is not None else (None, 1.0)
        if array is None or array.size == 0:
            return []
        engine = self._ensure()
        lines = _normalise(engine(array), scale)
        height, width = original.shape[:2]
        # 整图负责长句，重叠原尺寸分块补小字，避免整页缩小后漏掉图注。
        tile = min(limit, 1024)
        if max(height, width) > tile * 1.25:
            # 宽图注可能跨过每个方形块的切边；横向条带单独保留完整句子。
            if width > tile:
                for top in range(0, height, 512):
                    strip = original[top:top + 704]
                    if strip.shape[0] < 32:
                        continue
                    for line in _normalise(engine(strip), 1.0):
                        if (top and line.box[1] < 5) or (top + strip.shape[0] < height and line.box[3] > strip.shape[0] - 5):
                            continue
                        _merge_line(lines, OcrLine(tuple((x, y + top) for x, y in line.points), line.text, line.score))
            step = max(128, tile - 192)
            for top in range(0, height, step):
                for left in range(0, width, step):
                    crop = original[top:top + tile, left:left + tile]
                    ch, cw = crop.shape[:2]
                    if min(ch, cw) < 32:
                        continue
                    for line in _normalise(engine(crop), 1.0):
                        x0, y0, x1, y1 = line.box
                        # 内部切边上的残句交给相邻块或整图结果，防止重复翻译。
                        if ((left and x0 < 5) or (top and y0 < 5)
                                or (left + cw < width and x1 > cw - 5)
                                or (top + ch < height and y1 > ch - 5)):
                            continue
                        moved = OcrLine(tuple((x + left, y + top) for x, y in line.points), line.text, line.score)
                        _merge_line(lines, moved)
        return sorted(lines, key=lambda line: (round(line.box[1] / 5), line.box[0]))


def _merge_line(lines: list[OcrLine], candidate: OcrLine) -> None:
    """按原图坐标去重，优先保留完整长句，不依赖 OCR 字符完全一致。"""
    x0, y0, x1, y1 = candidate.box
    for index, old in enumerate(lines):
        a, b, c, d = old.box
        overlap = max(0, min(x1, c) - max(x0, a)) * max(0, min(y1, d) - max(y0, b))
        smaller = min(candidate.width * candidate.height, old.width * old.height)
        if overlap / max(1, smaller) > 0.6:
            if len(candidate.text) > len(old.text) and candidate.score >= old.score - 0.02:
                lines[index] = candidate
            elif old.score < 0.6 and candidate.score > old.score:
                lines[index] = candidate
            return
    lines.append(candidate)


def _to_array(image, max_side: int):
    """转成 HWC BGR/RGB ndarray，并在长边超限时降采样（返回缩放比）。"""
    from PIL import Image

    if isinstance(image, np.ndarray):
        array = image
    elif isinstance(image, Image.Image):
        array = np.array(image.convert("RGB"))
    elif isinstance(image, (str, os.PathLike)):
        array = np.array(Image.open(image).convert("RGB"))
    else:
        return None, 1.0

    if array.ndim == 3 and array.shape[2] == 4:
        array = array[:, :, :3]
    height, width = array.shape[:2]
    longest = max(height, width)
    if max_side and longest > max_side:
        ratio = max_side / float(longest)
        new_size = (max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
        pil = Image.fromarray(array).resize(new_size, Image.LANCZOS)
        return np.array(pil), 1.0 / ratio
    return array, 1.0


def _normalise(raw, scale: float) -> list[OcrLine]:
    """把 RapidOCR 不同版本的返回值统一成 list[OcrLine]。"""
    if raw is None:
        return []
    result = raw
    # 1.3/1.4 返回 (result, elapse)；个别版本返回 (boxes, txts, scores, elapse)
    if isinstance(result, tuple):
        if len(result) == 4 and isinstance(result[1], (list, tuple)) and isinstance(result[2], (list, tuple)):
            boxes, texts, scores = result[0], result[1], result[2]
            if (not boxes) or (isinstance(texts[0], str) if texts else True):
                result = [
                    [box, text, score] for box, text, score in zip(boxes or [], texts or [], scores or [])
                ]
            else:
                result = result[0]
        else:
            result = result[0]
    if not result:
        return []

    lines: list[OcrLine] = []
    for item in result:
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        if not text or not str(text).strip():
            continue
        points = tuple((float(p[0]) * scale, float(p[1]) * scale) for p in box)
        if len(points) < 4:
            continue
        lines.append(OcrLine(points=points, text=str(text).strip(), score=float(score)))
    return lines

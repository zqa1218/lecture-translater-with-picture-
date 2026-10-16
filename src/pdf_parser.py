"""离线 PDF 解析：把每页拆成「可重建的元素」，供后续翻译与 PPTX 重建使用。

只用 PyMuPDF（本地、无云 API、无 token）：
  * 文本块：get_text("dict") 拿 bbox / 字号 / 颜色 / 粗斜体 / 字体名
  * 表格：page.find_tables() 做版面表格识别（PyMuPDF 内置）
  * 图形与公式：不做拆解——由「背景位图」原样保留（见 pdf_builder）
  * 水印：跨页重复出现的同一文本 → 判为水印，不翻译也不擦除

坐标系：PyMuPDF 的 bbox 以「点(pt)」为单位、原点在左上、y 向下，
与 PPTX 的「英寸 = 点/72」可以直接 1:1 映射，因此字号也能 1:1 沿用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

BULLET_PATTERN = re.compile(r"^\s*(?:[-•·*‣▪◦]|\d{1,2}[.)、]|[（(]\d{1,2}[)）])\s+")
CJK_LANGS = ("中文", "日语", "韩语")


@dataclass(slots=True)
class Element:
    kind: str  # title | text | list | table
    bbox: tuple[float, float, float, float]
    text: str = ""
    font_size: float = 12.0
    bold: bool = False
    italic: bool = False
    color: tuple[int, int, int] = (0, 0, 0)
    font_name: str = ""
    align: str = "left"  # left | center | right
    line_count: int = 1
    table: list[list[str]] | None = None
    # 翻译结果（由 pipeline 填充）
    translated: str = ""
    table_translated: list[list[str]] | None = None

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return max(0.0, self.bbox[3] - self.bbox[1])


@dataclass(slots=True)
class PageLayout:
    width: float
    height: float
    elements: list[Element] = field(default_factory=list)


@dataclass(slots=True)
class ParsedPdf:
    path: str
    pages: list[PageLayout] = field(default_factory=list)

    @property
    def text_element_count(self) -> int:
        return sum(
            1
            for page in self.pages
            for el in page.elements
            if el.kind in ("title", "text", "list")
        )

    @property
    def table_count(self) -> int:
        return sum(1 for page in self.pages for el in page.elements if el.kind == "table")


def _int_to_rgb(value: object) -> tuple[int, int, int]:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (0, 0, 0)
    return ((number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF)


def _rect_overlap_ratio(inner: tuple[float, float, float, float], outer) -> float:
    ix0 = max(inner[0], outer[0])
    iy0 = max(inner[1], outer[1])
    ix1 = min(inner[2], outer[2])
    iy1 = min(inner[3], outer[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area = max(1e-6, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return inter / area


def _detect_body_size(sizes: list[float]) -> float:
    if not sizes:
        return 12.0
    ordered = sorted(sizes)
    mid = ordered[len(ordered) // 2]
    return mid if mid > 0 else 12.0


def _guess_align(bbox: tuple[float, float, float, float], page_width: float) -> str:
    left, _, right, _ = bbox
    center = (left + right) / 2
    page_center = page_width / 2
    if abs(center - page_center) < page_width * 0.05 and (right - left) < page_width * 0.6:
        return "center"
    if right > page_width * 0.92 and left > page_width * 0.5:
        return "right"
    return "left"


def _normalize_for_watermark(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip().lower()


def _is_watermark_candidate(
    block: dict,
    *,
    body_size: float,
    page_width: float,
    page_height: float,
) -> bool:
    """判断某文本块「长得像水印」——只有像水印的块才参与跨页重复统计。

    为什么需要这一步：讲义 PDF 每页都会重复标题、页眉、页脚，
    若只看「跨页重复」就会把正文标题一起删掉。真实水印的特征是
    旋转、或灰色+大字号+位于页面中部，这里按这三条判断。
    """
    spans = block.get("spans") or []
    if not spans:
        return False
    first = spans[0]
    try:
        size = float(first.get("size", 12.0))
    except (TypeError, ValueError):
        size = 12.0
    color = _int_to_rgb(first.get("color", 0))
    gray = color[0] == color[1] == color[2] and 50 <= color[0] <= 215
    big = size >= max(body_size * 1.3, 14.0)

    rotated = False
    for line in block.get("lines", []) or []:
        try:
            direction = line.get("dir", (1.0, 0.0))
            if abs(float(direction[1])) > 0.2:
                rotated = True
                break
        except (TypeError, ValueError, IndexError):
            continue

    bbox = block.get("bbox", (0.0, 0.0, 0.0, 0.0))
    center_y = (bbox[1] + bbox[3]) / 2
    centered_band = page_height * 0.30 <= center_y <= page_height * 0.75

    if rotated:
        return True
    return bool(gray and big and centered_band)


def _strip_watermark_spans(spans: list[dict], watermark_keys: set[str]) -> list[dict]:
    """把落在水印文本上的 span 剔除。

    水印常与正文重叠，PyMuPDF 会把两者的 span 归进同一个 block；
    若只在 block 级别判断水印，正文译文里就会混进水印残渣
    （实践中会看到 “ONFIDENTI” 这类碎片）。这里按 span 精确剔除。
    """
    if not watermark_keys:
        return spans
    kept: list[dict] = []
    for span in spans:
        normalized = _normalize_for_watermark(str(span.get("text", "")))
        if len(normalized) >= 4 and any(normalized in key for key in watermark_keys):
            continue
        kept.append(span)
    return kept


def _union_bbox(spans: list[dict], fallback) -> tuple[float, float, float, float]:
    boxes = [tuple(float(v) for v in s.get("bbox", ())) for s in spans if len(s.get("bbox", ())) == 4]
    if not boxes:
        return tuple(float(v) for v in fallback)  # type: ignore[return-value]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _collect_table_rects(page: "fitz.Page", enabled: bool) -> list[tuple[list[list[str]], tuple[float, float, float, float]]]:
    if not enabled:
        return []
    try:
        finder = page.find_tables()
    except Exception:
        return []
    found: list[tuple[list[list[str]], tuple[float, float, float, float]]] = []
    for table in getattr(finder, "tables", []) or []:
        try:
            rows = table.extract()
        except Exception:
            continue
        cleaned = [
            [(cell or "").replace("\n", " ").strip() for cell in (row or [])]
            for row in (rows or [])
        ]
        if not cleaned or not any(any(cell for cell in row) for row in cleaned):
            continue
        bbox = tuple(float(v) for v in table.bbox)  # type: ignore[assignment]
        found.append((cleaned, bbox))  # type: ignore[arg-type]
    return found


def parse_pdf(
    path: str,
    *,
    detect_tables: bool = True,
    drop_watermarks: bool = True,
    max_pages: int | None = None,
) -> ParsedPdf:
    doc = fitz.open(path)
    parsed = ParsedPdf(path=path)

    # ---- 第一遍：收集每页元素，并统计跨页重复文本（水印识别） ----
    per_page_raw: list[dict] = []
    watermark_counter: dict[str, int] = {}
    try:
        for page_index, page in enumerate(doc):
            if max_pages is not None and page_index >= max_pages:
                break
            page_rect = page.rect
            table_items = _collect_table_rects(page, detect_tables)
            table_rects = [item[1] for item in table_items]

            blocks = page.get_text("dict").get("blocks", [])
            text_blocks: list[dict] = []
            for block in blocks:
                if block.get("type") != 0:
                    continue
                bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
                spans = [
                    span
                    for line in block.get("lines", [])
                    for span in line.get("spans", [])
                    if str(span.get("text", "")).strip()
                ]
                if not spans:
                    continue
                if any(_rect_overlap_ratio(bbox, tr) > 0.5 for tr in table_rects):
                    continue
                text_blocks.append({"bbox": bbox, "spans": spans})

            sizes = [float(s.get("size", 12.0)) for block in text_blocks for s in block["spans"]]
            body_size = _detect_body_size(sizes)

            for block in text_blocks:
                text = "".join(str(s.get("text", "")) for s in block["spans"]).strip()
                if not text:
                    continue
                key = _normalize_for_watermark(text)
                # 只有「像水印」的块才计入跨页重复统计，避免误删重复标题/页眉页脚
                if key and _is_watermark_candidate(
                    block,
                    body_size=body_size,
                    page_width=float(page_rect.width),
                    page_height=float(page_rect.height),
                ):
                    watermark_counter[key] = watermark_counter.get(key, 0) + 1
                block["text"] = text
                block["body_size"] = body_size

            per_page_raw.append(
                {
                    "page_rect": (float(page_rect.width), float(page_rect.height)),
                    "text_blocks": text_blocks,
                    "tables": table_items,
                }
            )
    finally:
        doc.close()

    page_total = max(1, len(per_page_raw))
    watermark_keys = {
        key
        for key, count in watermark_counter.items()
        if len(key) >= 2 and count >= max(2, int(page_total * 0.6))
    }

    # ---- 第二遍：定型元素（判定标题/列表、剔除水印） ----
    doc = fitz.open(path)
    try:
        for page_index, raw in enumerate(per_page_raw):
            page_width, page_height = raw["page_rect"]
            layout = PageLayout(width=page_width, height=page_height)
            body_size = _detect_body_size(
                [b["body_size"] for b in raw["text_blocks"]] or [12.0]
            )

            for block in raw["text_blocks"]:
                text = block["text"]
                key = _normalize_for_watermark(text)
                if drop_watermarks and key in watermark_keys:
                    continue

                spans = block["spans"]
                if drop_watermarks:
                    spans = _strip_watermark_spans(spans, watermark_keys)
                    if not spans:
                        continue
                block_bbox = _union_bbox(spans, block["bbox"])

                visible_text = "".join(str(s.get("text", "")) for s in spans).strip()
                if not visible_text:
                    continue

                first = spans[0]
                font_size = float(first.get("size", body_size) or body_size)
                bold = bool(int(first.get("flags", 0)) & 16)
                italic = bool(int(first.get("flags", 0)) & 2)
                color = _int_to_rgb(first.get("color", 0))
                font_name = str(first.get("font", "") or "")

                is_title = (
                    font_size >= body_size * 1.28
                    and block_bbox[1] < page_height * 0.45
                    and len(visible_text) <= 80
                )
                if is_title:
                    kind = "title"
                elif BULLET_PATTERN.match(visible_text) or visible_text.startswith(("·", "•")):
                    kind = "list"
                else:
                    kind = "text"

                layout.elements.append(
                    Element(
                        kind=kind,
                        bbox=block_bbox,
                        text=visible_text,
                        font_size=round(font_size, 1),
                        bold=bold,
                        italic=italic,
                        color=color,
                        font_name=font_name,
                        align=_guess_align(block_bbox, page_width) if kind == "title" else "left",
                        line_count=max(
                            1,
                            int(
                                round(
                                    (block_bbox[3] - block_bbox[1])
                                    / max(1.0, font_size * 1.25)
                                )
                            ),
                        ),
                    )
                )

            for rows, bbox in raw["tables"]:
                layout.elements.append(
                    Element(
                        kind="table",
                        bbox=bbox,
                        font_size=11.0,
                        table=rows,
                        text="\n".join(" | ".join(cell for cell in row) for row in rows),
                    )
                )

            layout.elements.sort(key=lambda el: (round(el.bbox[1], 1), el.bbox[0]))
            parsed.pages.append(layout)
    finally:
        doc.close()

    return parsed


def pick_font_family(target_lang: str) -> str:
    """目标语言是 CJK 时用微软雅黑，其余用 Arial（两者 Windows 必装）。"""
    return "Microsoft YaHei" if target_lang in CJK_LANGS else "Arial"

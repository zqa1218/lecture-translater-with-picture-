"""把「解析后的 PDF 页面 + 译文」重建为可编辑的 PPTX。

思路（与 jieqiuming/pdf2ppt 的 bbox 定位/字号估算/原生表格做法一致，MIT）：
  1. 先对原文里「将被替换的文本区」做 redaction（只去文字，保留图片与矢量图形），
     表格区域连同框线一起去掉；
  2. 把处理后的页面按设定 DPI 渲染成背景图，铺满整页 —— 图片、图表、公式、复杂矢量
     图形就此原样保留，不需要逐个拆解；
  3. 在原来的 bbox 位置上放「可编辑的译文文本框」；表格放原生 PPT 表格。
这样既保住了版式（PDF 怎么排就怎么排），译文又是可编辑文本，而不是烧进图片里。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

import fitz
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from pdf_parser import Element, ParsedPdf, PageLayout, pick_font_family

ALIGN_MAP = {
    "left": PP_ALIGN.LEFT,
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
}
EMU_PER_POINT = 12700
POINTS_PER_INCH = 72.0
MIN_FONT_PT = 6.0
TABLE_MIN_FONT_PT = 8.0
TABLE_MAX_FONT_PT = 16.0


@dataclass(slots=True)
class BuildStats:
    slides: int = 0
    text_boxes: int = 0
    tables: int = 0
    background_pages: int = 0
    skipped_elements: int = 0


def _pt_to_emu(points: float) -> Emu:
    return Emu(int(round(points * EMU_PER_POINT)))


def _set_font(run, *, family: str, size: float, bold: bool, italic: bool, color) -> None:
    run.font.size = Pt(max(MIN_FONT_PT, size))
    run.font.bold = bold
    run.font.italic = italic
    try:
        run.font.color.rgb = RGBColor(*color)
    except Exception:
        pass
    run.font.name = family
    # 中文字体需要同时写 a:ea，否则 PowerPoint 会退回默认字体
    rpr = run.font._rPr  # noqa: SLF001
    existing = rpr.find(qn("a:ea"))
    if existing is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        latin = rpr.find(qn("a:latin"))
        if latin is not None:
            latin.addnext(ea)
        else:
            rpr.append(ea)
        existing = ea
    existing.set("typeface", family)


def _optimize_font_size(text: str, box_w_pt: float, box_h_pt: float, base_size: float) -> float:
    """按实际字宽试排，中文按全角计算，防止短标题溢出原框。"""
    from PIL import ImageFont
    from image_translator import resolve_font, _wrap_text
    base = max(MIN_FONT_PT, float(base_size or 12.0))
    if not text:
        return base
    path,index = resolve_font()
    if path:
        size = base
        while size >= MIN_FONT_PT:
            font = ImageFont.truetype(path, max(1,round(size * 4)),index=index)
            lines = _wrap_text(text,font,max(1,box_w_pt * 4))
            if len(lines) * size * 1.25 <= box_h_pt:
                return size
            size -= 0.5
        return MIN_FONT_PT
    chars = len(text)
    lines = max(1, int(box_h_pt / max(1.0, base * 1.25)) or 1)
    capacity = max(1, int((box_w_pt / max(1.0, base * 0.62)) * lines))
    if chars <= capacity:
        return base
    scaled = base * (capacity / chars) ** 0.5
    return max(MIN_FONT_PT, min(base, scaled))


def _add_text_box(
    slide,
    element: Element,
    *,
    family: str,
    stats: BuildStats,
    page_height_pt: float,
) -> None:
    text = (element.translated or element.text or "").strip()
    if not text:
        stats.skipped_elements += 1
        return

    pad = 1.5
    left = _pt_to_emu(max(0.0, element.bbox[0] - pad))
    top = _pt_to_emu(max(0.0, element.bbox[1] - pad))
    width = _pt_to_emu(max(8.0, element.width + pad * 2))
    height = _pt_to_emu(max(8.0, element.height + pad * 2))

    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    frame.vertical_anchor = (
        MSO_ANCHOR.MIDDLE
        if element.kind == "title" and element.bbox[1] < page_height_pt * 0.3
        else MSO_ANCHOR.TOP
    )

    size = _optimize_font_size(text, element.width, element.height, element.font_size)
    paragraph = frame.paragraphs[0]
    paragraph.line_spacing = 1.05
    paragraph.alignment = ALIGN_MAP.get(element.align, PP_ALIGN.LEFT)
    run = paragraph.add_run()
    run.text = text
    _set_font(
        run,
        family=family,
        size=size,
        bold=bool(element.bold) or element.kind == "title",
        italic=bool(element.italic),
        color=element.color or (0, 0, 0),
    )
    stats.text_boxes += 1


def _add_table(slide, element: Element, *, family: str, stats: BuildStats) -> None:
    rows = element.table_translated or element.table
    if not rows:
        stats.skipped_elements += 1
        return
    n_rows = len(rows)
    n_cols = max(len(row) for row in rows)

    left = _pt_to_emu(element.bbox[0])
    top = _pt_to_emu(element.bbox[1])
    width = _pt_to_emu(max(24.0, element.width))
    height = _pt_to_emu(max(16.0, element.height))

    shape = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    table = shape.table

    # 表格字号按单元格高度自适应（pdf2ppt 的 10~16pt 思路）
    cell_h_pt = element.height / max(1, n_rows)
    font_pt = max(TABLE_MIN_FONT_PT, min(TABLE_MAX_FONT_PT, cell_h_pt * 0.55))

    for row_index, row in enumerate(rows):
        for col_index in range(n_cols):
            cell = table.cell(row_index, col_index)
            value = row[col_index] if col_index < len(row) else ""
            frame = cell.text_frame
            frame.word_wrap = True
            paragraph = frame.paragraphs[0]
            paragraph.alignment = PP_ALIGN.CENTER
            run = paragraph.add_run()
            run.text = str(value)
            _set_font(
                run,
                family=family,
                size=font_pt,
                bold=(row_index == 0),
                italic=False,
                color=(0, 0, 0),
            )
    stats.tables += 1


def _render_page_background(
    page: "fitz.Page",
    page_layout: PageLayout,
    *,
    dpi: int,
) -> bytes | None:
    """擦掉「将被重建」的原文（含表格框线），再把整页渲染成背景图。

    注意：擦除条件是"这段文本会被重建"，而不是"这段文本有译文"——
    只转换不翻译时原文同样会被重建为可编辑文本框，因此必须一并擦掉，
    否则会出现「原文字在背景里 + 文本框又叠一层」的重影。
    """
    table_rects = [
        fitz.Rect(*el.bbox) for el in page_layout.elements if el.kind == "table"
    ]
    text_rects = [
        fitz.Rect(*el.bbox)
        for el in page_layout.elements
        if el.kind in ("title", "text", "list") and (el.text or "").strip()
    ]

    if table_rects:
        for rect in table_rects:
            page.add_redact_annot(rect)
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            )
        except TypeError:  # 老版本 PyMuPDF 没有 graphics 参数
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    if text_rects:
        for rect in text_rects:
            page.add_redact_annot(rect, fill=False)
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )
        except TypeError:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    try:
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    except Exception:
        return None
    return pixmap.tobytes("png")


def build_pptx_from_pdf(
    parsed: ParsedPdf,
    output_path: str,
    *,
    target_lang: str = "中文",
    dpi: int = 200,
    keep_page_size: bool = True,
    rasterize_background: bool = True,
    progress=None,
) -> BuildStats:
    stats = BuildStats()
    family = pick_font_family(target_lang)
    first = parsed.pages[0] if parsed.pages else PageLayout(width=720.0, height=540.0)

    prs = Presentation()
    if keep_page_size:
        prs.slide_width = _pt_to_emu(first.width)
        prs.slide_height = _pt_to_emu(first.height)
    else:
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)

    blank_layout = prs.slide_layouts[6]

    doc = fitz.open(parsed.path)
    try:
        for index, page_layout in enumerate(parsed.pages):
            slide = prs.slides.add_slide(blank_layout)
            stats.slides += 1

            page = doc[index] if index < doc.page_count else None
            if rasterize_background and page is not None:
                background = _render_page_background(page, page_layout, dpi=dpi)
                if background is not None:
                    slide.shapes.add_picture(
                        io.BytesIO(background),
                        0,
                        0,
                        width=prs.slide_width,
                        height=prs.slide_height,
                    )
                    stats.background_pages += 1

            for element in page_layout.elements:
                if element.kind == "table":
                    _add_table(slide, element, family=family, stats=stats)
                else:
                    _add_text_box(
                        slide,
                        element,
                        family=family,
                        stats=stats,
                        page_height_pt=page_layout.height,
                    )

            if progress is not None:
                progress(index + 1, len(parsed.pages))
    finally:
        doc.close()

    directory = os.path.dirname(os.path.abspath(output_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    prs.save(output_path)
    return stats

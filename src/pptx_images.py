"""幻灯片图片处理：遍历图片、替换图片数据、在图片上叠加可编辑文本框。

难点在坐标：PowerPoint 的图片可以被**裁剪（srcRect）**、**翻转（flipH/flipV）**、
以及被放在**组合图形（GroupShape）**里（子坐标空间与页面不同）。
只按 shape.left/top/width/height 摆文本框，遇到这几种情况就会错位，
所以这里把「图片像素坐标 → 幻灯片 EMU」完整算出来。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

EMU_PER_POINT = 12700
_PICTURE_TYPES = (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE)


@dataclass(slots=True)
class PictureRef:
    slide_index: int
    slide: object
    shape: object


def iter_pictures(slide, slide_index: int = 0):
    """递归产出幻灯片里的所有图片（含组合图形内的图片）。"""

    def walk(shapes):
        for shape in shapes:
            shape_type = getattr(shape, "shape_type", None)
            if shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from walk(shape.shapes)
            elif shape_type in _PICTURE_TYPES:
                yield shape

    for shape in walk(slide.shapes):
        yield shape


def collect_pictures(prs) -> list[PictureRef]:
    refs: list[PictureRef] = []
    for index, slide in enumerate(prs.slides):
        for shape in iter_pictures(slide, index):
            refs.append(PictureRef(slide_index=index, slide=slide, shape=shape))
    return refs


def picture_bytes(shape) -> tuple[bytes, str] | tuple[None, None]:
    try:
        image = shape.image
    except Exception:  # noqa: BLE001
        return None, None
    return image.blob, image.ext


def replace_picture_image(shape, data: bytes) -> bool:
    """把图片数据换成新的（新增一个图片部件，原图不动）。

    python-pptx 1.0 的 ``blip_rId`` 只有 getter，所以直接改 DrawingML：
    把 ``a:blip/@r:embed`` 指向新的图片关系即可。
    """
    try:
        _part, r_id = shape.part.get_or_add_image_part(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return False
    element = shape._element  # noqa: SLF001
    blip_fill = getattr(element, "blipFill", None)
    if blip_fill is None:
        blip_fill = element.find(qn("p:blipFill"))
    blip = blip_fill.find(qn("a:blip")) if blip_fill is not None else None
    if blip is None:
        return False
    blip.set(qn("r:embed"), r_id)
    blip.attrib.pop(qn("r:link"), None)
    return True


# ------------------------------------------------------------------ 几何换算
def _group_xfrm(group_element):
    grid = group_element.find(qn("p:grpSpPr"))
    xfrm = grid.find(qn("a:xfrm")) if grid is not None else None
    if xfrm is None:
        return None
    off = xfrm.find(qn("a:off"))
    ext = xfrm.find(qn("a:ext"))
    child_off = xfrm.find(qn("a:chOff"))
    child_ext = xfrm.find(qn("a:chExt"))
    if off is None or ext is None or child_off is None or child_ext is None:
        return None
    return (
        int(off.get("x", 0)),
        int(off.get("y", 0)),
        max(1, int(ext.get("cx", 1))),
        max(1, int(ext.get("cy", 1))),
        int(child_off.get("x", 0)),
        int(child_off.get("y", 0)),
        max(1, int(child_ext.get("cx", 1))),
        max(1, int(child_ext.get("cy", 1))),
    )


def absolute_box(shape) -> tuple[int, int, int, int]:
    """形状在幻灯片坐标系里的 (left, top, width, height)，单位 EMU。"""
    try:
        left, top = int(shape.left), int(shape.top)
        width, height = int(shape.width), int(shape.height)
    except TypeError:
        return 0, 0, 0, 0

    element = shape._element.getparent()  # noqa: SLF001
    while element is not None:
        if element.tag == qn("p:grpSp"):
            xfrm = _group_xfrm(element)
            if xfrm:
                ox, oy, ex, ey, cox, coy, cex, cey = xfrm
                scale_x = ex / float(cex)
                scale_y = ey / float(cey)
                left = ox + int(round((left - cox) * scale_x))
                top = oy + int(round((top - coy) * scale_y))
                width = int(round(width * scale_x))
                height = int(round(height * scale_y))
        element = element.getparent()
    return left, top, width, height


def crop_fractions(shape) -> tuple[float, float, float, float]:
    """图片的裁剪比例 (left, top, right, bottom)，0~0.9。"""
    try:
        blip_fill = shape._element.blipFill  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0, 0.0
    src_rect = blip_fill.find(qn("a:srcRect")) if blip_fill is not None else None
    if src_rect is None:
        return 0.0, 0.0, 0.0, 0.0

    def fraction(name: str) -> float:
        try:
            return max(0.0, min(0.95, int(src_rect.get(name, "0")) / 100000.0))
        except (TypeError, ValueError):
            return 0.0

    return fraction("l"), fraction("t"), fraction("r"), fraction("b")


def flip_state(shape) -> tuple[bool, bool]:
    try:
        xfrm = shape._element.spPr.find(qn("a:xfrm"))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False, False
    if xfrm is None:
        return False, False
    return xfrm.get("flipH") == "1", xfrm.get("flipV") == "1"


def map_pixel_box(shape, image_size, pixel_box):
    """图片像素框 → 幻灯片 EMU 框；无法映射时返回 None。

    只考虑旋转为 0 的图片；有旋转的图片在调用方会被跳过（译本会歪，不如不叠）。
    """
    if not image_size:
        return None
    px_w, px_h = float(image_size[0]), float(image_size[1])
    if px_w <= 0 or px_h <= 0:
        return None
    if abs(float(getattr(shape, "rotation", 0) or 0)) > 0.5:
        return None

    left, top, width, height = absolute_box(shape)
    if width <= 0 or height <= 0:
        return None
    crop_l, crop_t, crop_r, crop_b = crop_fractions(shape)
    visible_w = px_w * max(0.01, 1.0 - crop_l - crop_r)
    visible_h = px_h * max(0.01, 1.0 - crop_t - crop_b)
    scale_x = width / visible_w
    scale_y = height / visible_h
    flip_h, flip_v = flip_state(shape)

    x0, y0, x1, y1 = pixel_box
    rel_x0 = (x0 - crop_l * px_w) * scale_x
    rel_x1 = (x1 - crop_l * px_w) * scale_x
    rel_y0 = (y0 - crop_t * px_h) * scale_y
    rel_y1 = (y1 - crop_t * px_h) * scale_y
    if flip_h:
        rel_x0, rel_x1 = width - rel_x1, width - rel_x0
    if flip_v:
        rel_y0, rel_y1 = height - rel_y1, height - rel_y0

    box_left = left + int(round(min(rel_x0, rel_x1)))
    box_top = top + int(round(min(rel_y0, rel_y1)))
    box_w = int(round(abs(rel_x1 - rel_x0)))
    box_h = int(round(abs(rel_y1 - rel_y0)))
    if box_w <= 0 or box_h <= 0:
        return None
    return box_left, box_top, box_w, box_h


# ------------------------------------------------------------------ 叠加文本框
def _apply_font(run, *, family: str, size_pt: float, color, bold: bool = False) -> None:
    from pptx.dml.color import RGBColor

    run.font.size = Pt(max(6.0, float(size_pt)))
    run.font.bold = bold
    try:
        run.font.color.rgb = RGBColor(*color)
    except Exception:  # noqa: BLE001
        pass
    run.font.name = family
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


def add_overlay_textbox(
    slide,
    *,
    left: int,
    top: int,
    width: int,
    height: int,
    text: str,
    font_size_pt: float,
    color,
    family: str = "微软雅黑",
    vertical: bool = False,
):
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN

    box = slide.shapes.add_textbox(Emu(int(left)), Emu(int(top)), Emu(int(width)), Emu(int(height)))
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    if vertical:
        try:
            from pptx.oxml.ns import qn as _qn

            body_pr = frame._txBody.find(_qn("a:bodyPr"))  # noqa: SLF001
            if body_pr is not None:
                body_pr.set("vert", "eaVert")
        except Exception:  # noqa: BLE001
            pass
    paragraph = frame.paragraphs[0]
    paragraph.alignment = PP_ALIGN.CENTER
    run = paragraph.add_run()
    run.text = text
    _apply_font(run, family=family, size_pt=font_size_pt, color=color)
    return box

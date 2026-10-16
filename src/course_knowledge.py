"""课程知识流水线：课件轨 + 录音轨 → 可长期复用的知识资产。

实现依据本地技能 ``lecture-knowledge-pipeline`` 的 10 个阶段，覆盖其中与学生复习
直接相关的部分：

    阶段 1 课件轨：逐页提文本 + 抽关键图 -> 02_slides/
    阶段 2 录音轨：本地转写（含多文件乱序重排）-> 01_transcript/
    阶段 3 对齐  ：讲述切到页 -> 03_align/align.md
    阶段 4 纠错  ：只标注不改写 -> 04_corrections/corrections.md
    阶段 6 大纲  ：三级知识点 + 稳定 ID -> 06_outline/outline.md|json
    阶段 8 图谱  ：Mermaid -> 08_graph/graph.md
    阶段 9 笔记  ：讲课流程 / 详细内容报告 / 一页速览 / 考试重点 -> 07_notes/

阶段 5（学科补充）与阶段 7（出题）在技能里需要外部查证与人工圈定范围，
本工具只做「待补充清单」与「自测题」的轻量版本，并明确标注来源层级，不编造文献。
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from llm_prompts import (
    PROMPT_ALIGN,
    PROMPT_CORRECTIONS,
    PROMPT_EXAM_FOCUS,
    PROMPT_GRAPH,
    PROMPT_LECTURE_FLOW,
    PROMPT_NOTES,
    PROMPT_OUTLINE,
    PROMPT_SLIDE_DIGEST,
    PROMPT_SUMMARY,
    PROMPT_TRANSCRIPT_CHUNK,
    SYSTEM_COMPRESS,
    SYSTEM_TUTOR,
)
from transcript_io import Segment, format_timestamp

STAGE_DIRS = (
    "00_source",
    "01_transcript",
    "02_slides",
    "03_align",
    "04_corrections",
    "05_supplements",
    "06_outline",
    "07_notes",
    "08_graph",
    "09_quiz",
)

#: 老师点名的「考试信号」词。命中即视为高优先级证据，一条都不放过。
EXAM_KEYWORDS = (
    "考试", "考点", "重点", "必考", "会考", "要考", "记住", "背下来", "背诵", "记牢",
    "题型", "分数", "分值", "期末", "期中", "小测", "随堂测", "作业", "要交",
    "复习", "下次课", "下节课", "预习", "chapter", "exam", "quiz", "midterm", "final",
    "test", "memorize", "important", "will be on",
)

OUTLINE_POINT_RE = re.compile(
    r"^\s*-\s*\[(?P<id>[A-Za-z0-9][A-Za-z0-9\-_.]*)\]\s*(?P<title>.+?)\s*[｜|]\s*"
    r"(?P<level>[^｜|]+?)\s*[｜|]\s*(?P<source>.+?)\s*$"
)
OUTLINE_UNIT_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
OUTLINE_SECTION_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$")


@dataclass(slots=True)
class SlideContent:
    index: int
    texts: list[str] = field(default_factory=list)
    notes: str = ""
    images: list[str] = field(default_factory=list)

    def plain_text(self) -> str:
        body = "\n".join(self.texts)
        if self.notes:
            body = f"{body}\n（备注）{self.notes}" if body else f"（备注）{self.notes}"
        return body.strip()


@dataclass(slots=True)
class DeckContent:
    source_path: str
    kind: str
    slides: list[SlideContent] = field(default_factory=list)

    def markdown(self) -> str:
        blocks: list[str] = []
        for slide in self.slides:
            parts = [f"<!-- page {slide.index} -->", f"### p.{slide.index}"]
            if slide.texts:
                parts.extend(slide.texts)
            if slide.notes:
                parts.append(f"（备注）{slide.notes}")
            for image in slide.images:
                parts.append(f"（本页插图：{os.path.basename(image)}）")
            blocks.append("\n".join(parts))
        return "\n\n".join(blocks)

    def image_list(self) -> list[str]:
        return [os.path.basename(p) for slide in self.slides for p in slide.images]

    def char_count(self) -> int:
        return sum(len(slide.plain_text()) for slide in self.slides)


@dataclass(slots=True)
class CourseJobResult:
    course_dir: str = ""
    files: list[str] = field(default_factory=list)
    outline_points: int = 0
    slides: int = 0
    transcript_segments: int = 0
    transcript_duration: float = 0.0
    exam_signals: int = 0
    order_note: str = ""
    warnings: list[str] = field(default_factory=list)

    def add(self, path: str | None) -> None:
        if path and path not in self.files:
            self.files.append(path)


# ---------------------------------------------------------------------- 工作区
def ensure_workspace(course_dir: str) -> dict[str, str]:
    paths = {"root": course_dir}
    for name in STAGE_DIRS:
        target = os.path.join(course_dir, name)
        os.makedirs(target, exist_ok=True)
        paths[name] = target
    kb = os.path.join(course_dir, "10_kb")
    os.makedirs(kb, exist_ok=True)
    paths["10_kb"] = kb
    return paths


def write_text(path: str, content: str) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


# ---------------------------------------------------------------------- 课件轨
def _collect_pptx_slide(slide) -> tuple[list[str], list]:
    """递归取一页的全部文本与图片（含组合图形、表格单元格、备注）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    texts: list[str] = []
    pictures: list = []

    def walk(shapes):
        for shape in shapes:
            kind = getattr(shape, "shape_type", None)
            if kind == MSO_SHAPE_TYPE.GROUP:
                walk(shape.shapes)
                continue
            if kind in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE):
                pictures.append(shape)
                continue
            if getattr(shape, "has_table", False):
                try:
                    table = shape.table
                except Exception:  # noqa: BLE001
                    table = None
                if table is not None:
                    for row in table.rows:
                        line = " | ".join(cell.text.strip() for cell in row.cells).strip()
                        if line.replace("|", "").strip():
                            texts.append(line)
                    continue
            if getattr(shape, "has_text_frame", False):
                try:
                    frame = shape.text_frame
                except Exception:  # noqa: BLE001
                    frame = None
                if frame is not None:
                    for paragraph in frame.paragraphs:
                        value = (paragraph.text or "").strip()
                        if value:
                            texts.append(value)

    walk(slide.shapes)
    return texts, pictures


def collect_pptx_deck(path: str, *, slides_dir: str, log=None) -> DeckContent:
    from pptx import Presentation

    log = log or (lambda _m: None)
    presentation = Presentation(path)
    deck = DeckContent(source_path=path, kind="pptx")
    for index, slide in enumerate(presentation.slides, start=1):
        texts, pictures = _collect_pptx_slide(slide)
        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:  # noqa: BLE001
            notes = ""
        content = SlideContent(index=index, texts=texts, notes=notes)
        for order, shape in enumerate(_significant_pictures(pictures), start=1):
            saved = _save_picture(shape, slides_dir, index, order)
            if saved:
                content.images.append(saved)
        deck.slides.append(content)
    log(f"课件解析完成：{len(deck.slides)} 页 / {len(deck.image_list())} 张关键图")
    return deck


def _significant_pictures(pictures, *, min_pixels: int = 120, limit: int = 4):
    """挑出值得留档的图：跳过图标、小装饰与过小的位图。"""
    import io as _io

    from PIL import Image

    kept = []
    for shape in pictures:
        try:
            blob = shape.image.blob
        except Exception:  # noqa: BLE001
            continue
        # 注意：不要用「字节数」判断，纯色大图压缩后可能只有 1 KB；
        # 真正能区分图标与插图的信号是像素尺寸。
        try:
            with Image.open(_io.BytesIO(blob)) as probe:
                width, height = probe.size
        except Exception:  # noqa: BLE001
            continue
        if width < min_pixels or height < min_pixels:
            continue
        kept.append((width * height, shape))
    kept.sort(key=lambda item: item[0], reverse=True)
    return [shape for _area, shape in kept[:limit]]


def _save_picture(shape, slides_dir: str, slide_index: int, order: int) -> str | None:
    try:
        image = shape.image
        blob, extension = image.blob, image.ext
    except Exception:  # noqa: BLE001
        return None
    target = os.path.join(slides_dir, f"slide-{slide_index:02d}-img-{order}.{extension}")
    try:
        os.makedirs(slides_dir, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(blob)
    except OSError:
        return None
    return target


def _page_is_worth_rendering(page) -> bool:
    try:
        if page.get_images(full=True):
            return True
        if page.find_tables().tables:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def collect_pdf_deck(path: str, *, slides_dir: str, dpi: int = 150, log=None) -> DeckContent:
    import fitz

    from pdf_parser import parse_pdf

    log = log or (lambda _m: None)
    parsed = parse_pdf(path, detect_tables=False)
    deck = DeckContent(source_path=path, kind="pdf")
    document = fitz.open(path)
    try:
        for index, page_layout in enumerate(parsed.pages, start=1):
            texts: list[str] = []
            for element in page_layout.elements:
                value = re.sub(r"\s+", " ", element.text or "").strip()
                if value:
                    texts.append(value)
                if element.table:
                    for row in element.table:
                        line = " | ".join(str(cell) for cell in row).strip()
                        if line.replace("|", "").strip():
                            texts.append(line)
            content = SlideContent(index=index, texts=texts)
            page = document[index - 1] if index - 1 < document.page_count else None
            if page is not None and _page_is_worth_rendering(page):
                target = os.path.join(slides_dir, f"page-{index:02d}.png")
                try:
                    page.get_pixmap(dpi=dpi, alpha=False).save(target)
                    content.images.append(target)
                except Exception:  # noqa: BLE001
                    pass
            deck.slides.append(content)
    finally:
        document.close()
    log(f"PDF 解析完成：{len(deck.slides)} 页 / {len(deck.image_list())} 张关键页图")
    return deck


def collect_deck(path: str, *, out_dir: str, dpi: int = 150, log=None) -> DeckContent:
    extension = os.path.splitext(path)[1].lower()
    if extension == ".pptx":
        return collect_pptx_deck(path, slides_dir=out_dir, log=log)
    if extension == ".pdf":
        return collect_pdf_deck(path, slides_dir=out_dir, dpi=dpi, log=log)
    raise ValueError(f"不支持的课件格式：{extension}")


# ---------------------------------------------------------------------- 分段与并发
def _chunk_slides(slides: list[SlideContent], max_chars: int) -> list[list[SlideContent]]:
    chunks: list[list[SlideContent]] = []
    current: list[SlideContent] = []
    size = 0
    for slide in slides:
        length = len(slide.plain_text()) + 40
        if current and size + length > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(slide)
        size += length
    if current:
        chunks.append(current)
    return chunks


def _chunk_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        length = len(segment.text) + 12
        if current and size + length > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(segment)
        size += length
    if current:
        chunks.append(current)
    return chunks


def _slide_chunk_markdown(chunk: list[SlideContent]) -> str:
    blocks = []
    for slide in chunk:
        lines = [f"<!-- page {slide.index} -->", f"### p.{slide.index}"]
        lines.extend(slide.texts or ["（本页无文本）"])
        if slide.notes:
            lines.append(f"（备注）{slide.notes}")
        for image in slide.images:
            lines.append(f"（本页插图：{os.path.basename(image)}）")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _segment_chunk_markdown(chunk: list[Segment]) -> str:
    return "\n".join(f"[{format_timestamp(s.start)}] {s.text}" for s in chunk)


def _parallel_map(items, worker, *, max_workers: int, log, label: str) -> list[str]:
    results: list[str] = ["" for _ in items]
    if not items:
        return results
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(worker, index, item): index for index, item in enumerate(items)}
        for future, index in list(futures.items()):
            try:
                results[index] = future.result() or ""
            except Exception as exc:  # noqa: BLE001
                log(f"{label} 第 {index + 1} 段失败：{exc}")
    return results


# ---------------------------------------------------------------------- 分析
def summarize_deck(deck: DeckContent, client, config, *, log, progress=None) -> str:
    chunks = _chunk_slides(deck.slides, max(2000, int(config.merge_chunk_chars)))
    if not chunks:
        return ""
    workers = min(4, max(1, int(config.max_workers)))
    log(f"课件摘要：{len(chunks)} 个分段（并发 {workers} 路）")

    def worker(_index: int, chunk) -> str:
        prompt = PROMPT_SLIDE_DIGEST.replace("{content}", _slide_chunk_markdown(chunk))
        return client.chat(prompt, system=SYSTEM_COMPRESS)

    parts = _parallel_map(chunks, worker, max_workers=workers, log=log, label="课件摘要")
    if progress:
        progress(1.0, "课件摘要完成")
    return "\n\n".join(part for part in parts if part.strip())


def digest_transcript(segments: list[Segment], client, config, *, log, progress=None) -> str:
    chunks = _chunk_segments(segments, max(2000, int(config.merge_chunk_chars)))
    if not chunks:
        return ""
    workers = min(4, max(1, int(config.max_workers)))
    log(f"录音压缩：{len(chunks)} 个分段（并发 {workers} 路）")

    def worker(index: int, chunk) -> str:
        prompt = (
            PROMPT_TRANSCRIPT_CHUNK.replace("{chunk_index}", str(index + 1))
            .replace("{start}", format_timestamp(chunk[0].start))
            .replace("{end}", format_timestamp(chunk[-1].end or chunk[-1].start))
            .replace("{content}", _segment_chunk_markdown(chunk))
        )
        return client.chat(prompt, system=SYSTEM_COMPRESS)

    parts = _parallel_map(chunks, worker, max_workers=workers, log=log, label="录音压缩")
    if progress:
        progress(1.0, "录音压缩完成")
    return "\n\n".join(part for part in parts if part.strip())


def scan_exam_signals(segments: list[Segment]) -> list[tuple[float | None, str, str]]:
    """正则扫描「考试信号」，保证不漏。返回 (时间, 原句, 命中词)。"""
    hits: list[tuple[float | None, str, str]] = []
    for segment in segments:
        lowered = segment.text.lower()
        for keyword in EXAM_KEYWORDS:
            if keyword.lower() in lowered:
                hits.append((segment.start, segment.text.strip(), keyword))
                break
    return hits


def _signals_block(hits) -> str:
    if not hits:
        return "（正则扫描没有命中任何「考试/作业」关键词）"
    lines = ["以下是正则扫描出的**全部**「考试/作业」信号，必须逐条处理、不得遗漏："]
    for stamp, text, keyword in hits[:80]:
        lines.append(f"- [{format_timestamp(stamp)}]（命中「{keyword}」）{text}")
    return "\n".join(lines)


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n（……材料过长，此处为前 %d 字，完整内容见对应阶段文件。）" % limit


# ---------------------------------------------------------------------- 大纲
def build_outline(deck_digest: str, transcript_digest: str, client, config, *, log) -> tuple[str, dict]:
    prefix = (getattr(config, "outline_prefix", "") or "KP").strip() or "KP"
    transcript_block = (
        f"\n课堂转写笔记（用来补上老师额外讲的内容）：\n{transcript_digest}\n"
        if transcript_digest
        else ""
    )
    prompt = (
        PROMPT_OUTLINE.replace("{prefix}", prefix)
        .replace("{transcript_part}", "与下面的「课堂转写笔记」" if transcript_digest else "")
        .replace("{deck}", deck_digest)
        .replace("{transcript_block}", transcript_block)
    )
    prompt += (
        "\n\n输出结构（务必保留这两级标题，便于机器解析）：\n"
        f"## {prefix}01 单元名称\n"
        f"### {prefix}01-01 章节名称\n"
        f"- [{prefix}01-01-01] 知识点名称 ｜ 理解 ｜ PPT p.12 + 录音 23:10\n"
        "  定义：一句话（不超过 60 字）\n"
    )
    markdown = client.chat(prompt, system=SYSTEM_TUTOR)
    return markdown, parse_outline_markdown(markdown)


def parse_outline_markdown(markdown: str) -> dict:
    """把大纲 Markdown 解析成机器可读 json（出题与统计的依据）。"""
    units: list[dict] = []
    current_unit: dict | None = None
    current_section: dict | None = None
    pending: dict | None = None

    def ensure_section() -> dict:
        nonlocal current_unit, current_section
        if current_unit is None:
            current_unit = {"title": "未分组", "sections": []}
            units.append(current_unit)
        if current_section is None:
            current_section = {"title": "未分组", "points": []}
            current_unit["sections"].append(current_section)
        return current_section

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            ensure_section()["points"].append(pending)
        pending = None

    for line in markdown.splitlines():
        section_match = OUTLINE_SECTION_RE.match(line)
        if section_match:
            flush()
            if current_unit is None:
                current_unit = {"title": "未分组", "sections": []}
                units.append(current_unit)
            current_section = {"title": section_match.group("title").strip(), "points": []}
            current_unit["sections"].append(current_section)
            continue
        unit_match = OUTLINE_UNIT_RE.match(line)
        if unit_match:
            flush()
            current_unit = {"title": unit_match.group("title").strip(), "sections": []}
            units.append(current_unit)
            current_section = None
            continue
        point_match = OUTLINE_POINT_RE.match(line)
        if point_match:
            flush()
            pending = {
                "id": point_match.group("id").strip(),
                "title": point_match.group("title").strip(),
                "level": point_match.group("level").strip(),
                "sources": [
                    s.strip() for s in re.split(r"[+，,、]", point_match.group("source")) if s.strip()
                ],
                "definition": "",
            }
            continue
        definition = re.match(r"^\s*(?:[-*]\s*)?定义[：:]\s*(.+)$", line)
        if definition and pending is not None:
            pending["definition"] = definition.group(1).strip()
    flush()

    count = sum(len(section["points"]) for unit in units for section in unit["sections"])
    return {"units": units, "pointCount": count}


def _flat_points(outline_json: dict) -> int:
    return int(outline_json.get("pointCount", 0) or 0)


# ---------------------------------------------------------------------- 主流程
def run_course_job(
    deck_path: str,
    materials: list[str],
    config,
    *,
    client,
    course_dir: str,
    log,
    progress=None,
    explicit_order: bool = False,
    translate_images_fn=None,
) -> CourseJobResult:
    """跑完整条课程知识流水线。

    ``materials`` 是录音/录像或文本转写稿；为空时只做课件轨（知识点总结）。
    ``translate_images_fn(deck_path, deck)`` 可选：顺便把课件插图里的文字翻成中文。
    """
    from audio_transcriber import Transcriber
    from transcript_io import (
        MEDIA_EXTENSIONS,
        merge_segments,
        order_by_content,
        order_sources,
        read_material,
        transcript_duration,
        write_transcript,
    )

    log = log or (lambda _m: None)
    progress = progress or (lambda *_a: None)
    result = CourseJobResult(course_dir=course_dir)
    paths = ensure_workspace(course_dir)

    def step(fraction: float, message: str) -> None:
        try:
            progress(fraction, message)
        except Exception:  # noqa: BLE001
            pass

    # ---- 阶段 1：课件轨 -------------------------------------------------
    step(0.05, "解析课件（逐页文本 + 关键图）…")
    deck = collect_deck(deck_path, out_dir=paths["02_slides"], dpi=150, log=log)
    result.slides = len(deck.slides)
    result.add(write_text(os.path.join(paths["02_slides"], "slides.md"), deck.markdown()))

    if translate_images_fn is not None:
        try:
            translate_images_fn(deck_path, deck)
        except Exception as exc:  # noqa: BLE001
            log(f"课件插图翻译失败（继续后续步骤）：{exc}")

    # ---- 阶段 2：录音轨 -------------------------------------------------
    segments: list[Segment] = []
    transcribed: list[tuple[str, list[Segment]]] = []
    if materials:
        step(0.12, "整理课堂素材顺序…")
        media = [m for m in materials if os.path.splitext(m)[1].lower() in MEDIA_EXTENSIONS]
        texts = [m for m in materials if m not in media]

        ordered_media, method = order_sources(media, explicit=explicit_order, log=log)
        result.order_note = method
        if media:
            log(f"录音排序依据：{method}")

        for text_path in texts:
            try:
                loaded = read_material(text_path)
                log(f"读入文本转写稿 {os.path.basename(text_path)}：{len(loaded)} 段")
                segments.extend(loaded)
            except Exception as exc:  # noqa: BLE001
                message = f"文本素材读取失败：{text_path}（{exc}）"
                log(message)
                result.warnings.append(message)

        if ordered_media:
            transcriber = Transcriber(
                config.whisper_model,
                language=config.whisper_language,
                initial_prompt=_whisper_prompt(config, deck),
                log=log,
            )
            total_files = len(ordered_media)
            for index, media_path in enumerate(ordered_media, start=1):
                name = os.path.basename(media_path)
                step(0.12 + 0.34 * ((index - 1) / max(1, total_files)), f"转写 {index}/{total_files}：{name}")
                log(f"转写第 {index}/{total_files} 段：{name}")
                partial_path = os.path.join(paths["01_transcript"], f"partial-{index:02d}.txt")

                def on_segment(segment, _path=partial_path) -> None:
                    try:
                        with open(_path, "a", encoding="utf-8") as handle:
                            handle.write(f"[{format_timestamp(segment.start)}] {segment.text}\n")
                    except OSError:
                        pass

                try:
                    open(partial_path, "w", encoding="utf-8").close()
                    got = transcriber.transcribe(
                        media_path,
                        progress=lambda ratio, note, _i=index, _n=total_files: step(
                            0.12 + 0.34 * ((_i - 1 + ratio) / max(1, _n)), f"转写 {_i}/{_n} 段 {note}"
                        ),
                        on_segment=on_segment,
                    )
                except Exception as exc:  # noqa: BLE001
                    message = f"转写失败：{name}（{exc}）"
                    log(message)
                    result.warnings.append(message)
                    continue
                log(f"  {name}：{len(got)} 段 / {format_timestamp(transcript_duration(got))}")
                transcribed.append((media_path, got))

        if len(transcribed) > 1 and not explicit_order:
            step(0.47, "按内容复核多段录音的先后…")
            heads = [
                (os.path.basename(path), "\n".join(s.text for s in group[:12]))
                for path, group in transcribed
            ]
            order, reason = order_by_content(heads, client, log=log)
            if order != list(range(len(transcribed))):
                transcribed = [transcribed[i] for i in order]
                result.order_note = f"{result.order_note} → {reason}"
                log(f"内容复核后顺序：{[os.path.basename(p) for p, _ in transcribed]}")
            else:
                log(f"内容复核：{reason}")

        groups = [group for _path, group in transcribed]
        if groups:
            segments = merge_segments(groups) + segments
        if segments:
            result.transcript_segments = len(segments)
            result.transcript_duration = transcript_duration(segments)
            result.add(write_transcript(os.path.join(paths["01_transcript"], "transcript.txt"), segments))
            result.add(
                write_text(
                    os.path.join(paths["01_transcript"], "order.md"),
                    "# 素材顺序说明\n\n"
                    + f"- 排序依据：{result.order_note or '（未排序）'}\n"
                    + "\n".join(f"- {os.path.basename(p)}（{len(g)} 段）" for p, g in transcribed)
                    + "\n",
                )
            )

    # ---- LLM：课件摘要 ---------------------------------------------------
    step(0.50, "提炼课件知识点（大模型）…")
    deck_digest = summarize_deck(deck, client, config, log=log)
    result.add(write_text(os.path.join(paths["02_slides"], "digest.md"), deck_digest))

    transcript_digest = ""
    exam_hits = scan_exam_signals(segments)
    result.exam_signals = len(exam_hits)
    if segments:
        step(0.58, "压缩录音转写（大模型）…")
        transcript_digest = digest_transcript(segments, client, config, log=log)
        result.add(write_text(os.path.join(paths["01_transcript"], "digest.md"), transcript_digest))

    # ---- 阶段 6：知识大纲 ------------------------------------------------
    step(0.66, "生成三级知识点大纲…")
    outline_md, outline_json = "", {"units": [], "pointCount": 0}
    try:
        outline_md, outline_json = build_outline(deck_digest, transcript_digest, client, config, log=log)
    except Exception as exc:  # noqa: BLE001
        log(f"大纲生成失败：{exc}")
        result.warnings.append(f"大纲生成失败：{exc}")
    if outline_md:
        outline_json["course"] = config.course_name or os.path.splitext(os.path.basename(deck_path))[0]
        result.outline_points = _flat_points(outline_json)
        result.add(write_text(os.path.join(paths["06_outline"], "outline.md"), outline_md))
        result.add(
            write_text(
                os.path.join(paths["06_outline"], "outline.json"),
                json.dumps(outline_json, ensure_ascii=False, indent=2),
            )
        )

    # ---- 阶段 9：笔记 / 要点总结 ------------------------------------------
    step(0.74, "撰写详细内容报告…")
    images = "\n".join(f"- {name}" for name in deck.image_list()[:40]) or "（无）"
    try:
        notes_md = client.chat(
            PROMPT_NOTES.replace("{deck}", _trim(deck_digest, 24000))
            .replace("{transcript}", _trim(transcript_digest, 24000) or "（本次没有录音材料）")
            .replace("{images}", images),
            system=SYSTEM_TUTOR,
        )
    except Exception as exc:  # noqa: BLE001
        notes_md = f"（详细内容报告生成失败：{exc}）"
        result.warnings.append(f"详细内容报告生成失败：{exc}")
    result.add(write_text(os.path.join(paths["07_notes"], "notes.md"), notes_md))

    step(0.80, "生成一页速览…")
    material = _trim(
        f"【课件摘要】\n{deck_digest}\n\n【课堂转写笔记】\n{transcript_digest or '（无录音）'}", 30000
    )
    try:
        summary_md = client.chat(
            PROMPT_SUMMARY.replace("{material}", material), system=SYSTEM_TUTOR
        )
    except Exception as exc:  # noqa: BLE001
        summary_md = f"（一页速览生成失败：{exc}）"
        result.warnings.append(f"一页速览生成失败：{exc}")
    result.add(write_text(os.path.join(paths["07_notes"], "summary.md"), summary_md))

    if outline_md:
        step(0.84, "生成知识图谱…")
        try:
            graph_md = client.chat(
                PROMPT_GRAPH.replace("{outline}", _trim(outline_md, 12000)),
                system=SYSTEM_TUTOR,
            )
        except Exception as exc:  # noqa: BLE001
            graph_md = f"```mermaid\ngraph LR\n  X[知识图谱生成失败]\n```\n\n{exc}"
        result.add(write_text(os.path.join(paths["08_graph"], "graph.md"), graph_md))

    # ---- 阶段 3 / 4：只在有录音时做 ---------------------------------------
    if segments:
        transcript_text = _trim(
            "\n".join(f"[{format_timestamp(s.start)}] {s.text}" for s in segments), 32000
        )
        step(0.88, "把讲述对齐到课件页…")
        try:
            align_md = client.chat(
                PROMPT_ALIGN.replace("{deck}", _trim(deck_digest, 20000)).replace(
                    "{transcript}", transcript_text
                ),
                system=SYSTEM_TUTOR,
            )
        except Exception as exc:  # noqa: BLE001
            align_md = f"（对齐失败：{exc}）"
            result.warnings.append(f"页-讲述对齐失败：{exc}")
        result.add(write_text(os.path.join(paths["03_align"], "align.md"), align_md))

        step(0.91, "还原讲课流程…")
        try:
            flow_md = client.chat(
                PROMPT_LECTURE_FLOW.replace("{deck}", _trim(deck_digest, 16000)).replace(
                    "{transcript}", _trim(transcript_digest, 26000)
                ),
                system=SYSTEM_TUTOR,
            )
        except Exception as exc:  # noqa: BLE001
            flow_md = f"（讲课流程生成失败：{exc}）"
            result.warnings.append(f"讲课流程生成失败：{exc}")
        result.add(write_text(os.path.join(paths["07_notes"], "lecture-flow.md"), flow_md))

        step(0.94, "提取考试重点…")
        exam_prompt = PROMPT_EXAM_FOCUS.replace("{deck}", _trim(deck_digest, 18000)).replace(
            "{transcript}", _trim(transcript_digest, 26000) + "\n\n" + _signals_block(exam_hits)
        )
        try:
            exam_md = client.chat(exam_prompt, system=SYSTEM_TUTOR)
        except Exception as exc:  # noqa: BLE001
            exam_md = f"（考试重点提取失败：{exc}）"
            result.warnings.append(f"考试重点提取失败：{exc}")
        result.add(write_text(os.path.join(paths["07_notes"], "exam-focus.md"), exam_md))

        try:
            corrections_md = client.chat(
                PROMPT_CORRECTIONS.replace("{deck}", _trim(deck_digest, 14000)).replace(
                    "{transcript}", transcript_text
                ),
                system=SYSTEM_TUTOR,
            )
        except Exception as exc:  # noqa: BLE001
            corrections_md = f"（纠错检查失败：{exc}）"
        result.add(write_text(os.path.join(paths["04_corrections"], "corrections.md"), corrections_md))

    # ---- 台账 ------------------------------------------------------------
    result.add(write_text(os.path.join(course_dir, "COURSE.md"), _ledger(config, deck_path, result)))
    step(1.0, "课程知识库已生成")
    return result


def _whisper_prompt(config, deck: DeckContent) -> str | None:
    """给转写模型一点领域词汇提示，减少专有名词被听错。"""
    terms: list[str] = []
    for slide in deck.slides[:12]:
        for text in slide.texts:
            for token in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text):
                if token not in terms:
                    terms.append(token)
    terms = terms[:60]
    if not terms:
        return None
    return f"这是{config.course_name or '这门课'}的课堂录音。可能出现的专业词汇：" + "、".join(terms) + "。"


def _ledger(config, deck_path: str, result: CourseJobResult) -> str:
    from datetime import datetime

    lines = [
        "# 课程台账",
        "",
        f"- 课程名称：{config.course_name or '（未填写）'}",
        f"- 课件来源：{deck_path}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 课件页数：{result.slides}",
        f"- 知识点数量：{result.outline_points}",
        f"- 录音段数：{result.transcript_segments}（时长 {format_timestamp(result.transcript_duration)}）",
        f"- 考试信号命中：{result.exam_signals}",
        f"- 素材排序依据：{result.order_note or '（无录音）'}",
        "",
        "## 产物清单",
        "",
    ]
    for path in result.files:
        lines.append(f"- {os.path.relpath(path, result.course_dir)}")
    lines.extend(["", "## 待确认项", ""])
    if result.warnings:
        lines.extend(f"- {w}" for w in result.warnings)
    else:
        lines.append("- （无）")
    lines.extend(
        [
            "",
            "> 低置信内容一律标「待老师确认」；补充内容一律标「[补充]」，与课堂原话分开写。",
            "",
        ]
    )
    return "\n".join(lines)

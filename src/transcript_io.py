"""课堂素材的读取、排序与合并。

素材可能是**录音/录像**（mp3/m4a/wav/aac/flac/ogg/mp4…），也可能是**现成的文本转写稿**
（txt / md / srt / vtt / json / docx）。同一节课的素材常常分成好几段、顺序还是乱的，
所以本模块的核心职责之一就是**把顺序排回来**，判据按可靠性从高到低：

1. 显式顺序（用户在界面上手动上下移动过）——最高优先级，不再改动；
2. 元数据时间：ID3 的 TDRC/TYER、MP4 的创建时间、WAV 的 date、文件修改时间；
3. 文件名里的序号或日期（自然序，"第2段" 排在 "第10段" 前面）；
4. 内容顺序：交给大模型按语义衔接判断（"上一段我们讲到…"），只有前三者都给不出
   确定答案时才启用，并在日志里说明依据。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma")
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi")
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS
TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".srt", ".vtt", ".json", ".docx")

TIMESTAMP_RE = re.compile(r"[\[(]?\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\s*[\])]?")
SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)


@dataclass(slots=True)
class Segment:
    """一段讲述：时间戳（秒，可能未知）+ 文本。"""

    text: str
    start: float | None = None
    end: float | None = None
    source: str = ""

    def stamp(self) -> str:
        return format_timestamp(self.start)


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_timestamp(text: str) -> float | None:
    match = TIMESTAMP_RE.search(text or "")
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    fraction = match.group(4) or "0"
    return hours * 3600 + minutes * 60 + seconds + int(fraction) / (10 ** len(fraction))


# ------------------------------------------------------------------ 读取
def read_text_transcript(path: str) -> list[Segment]:
    extension = os.path.splitext(path)[1].lower()
    if extension == ".docx":
        return _read_docx(path)
    if extension == ".json":
        return _read_json(path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    if extension in (".srt", ".vtt"):
        segments = _read_caption(content)
        if segments:
            return segments
    return _read_plain(content, source=path)


def _read_docx(path: str) -> list[Segment]:
    from docx import Document

    document = Document(path)
    return _read_plain("\n".join(p.text for p in document.paragraphs), source=path)


def _read_json(path: str) -> list[Segment]:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        payload = json.load(handle)
    segments: list[Segment] = []
    items = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return segments
    for item in items:
        if isinstance(item, dict):
            segments.append(
                Segment(
                    text=str(item.get("text", "")).strip(),
                    start=item.get("start"),
                    end=item.get("end"),
                    source=path,
                )
            )
    return [s for s in segments if s.text]


def _read_caption(content: str) -> list[Segment]:
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = SRT_TIME_RE.search(block)
        text = " ".join(line for line in lines if not SRT_TIME_RE.search(line) and not line.strip().isdigit())
        text = text.strip()
        if not text:
            continue
        if match:
            start = _seconds(match.group(1), match.group(2), match.group(3), match.group(4))
            end = _seconds(match.group(5), match.group(6), match.group(7), match.group(8))
            segments.append(Segment(text=text, start=start, end=end))
        else:
            segments.append(Segment(text=text))
    return segments


def _seconds(hours: str, minutes: str, seconds: str, fraction: str = "0") -> float:
    digits = (fraction or "0").ljust(3, "0")[:3]
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(digits) / 1000.0
    )


def _read_plain(content: str, source: str = "") -> list[Segment]:
    segments: list[Segment] = []
    for raw_line in content.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        stamp = parse_timestamp(line[:16]) if line[:1] in "[(" else None
        if stamp is not None:
            line = TIMESTAMP_RE.sub("", line, count=1).strip(" ])")
        if not line or re.fullmatch(r"-{2,}", line):
            continue
        segments.append(Segment(text=line, start=stamp, source=source))
    return segments


def read_material(path: str) -> list[Segment]:
    extension = os.path.splitext(path)[1].lower()
    if extension in TEXT_EXTENSIONS:
        return read_text_transcript(path)
    raise ValueError(f"不是可读取的文本素材：{path}")


# ------------------------------------------------------------------ 排序
def natural_key(name: str):
    parts = re.split(r"(\d+)", os.path.basename(name).lower())
    return [int(part) if part.isdigit() else part for part in parts]


def probe_recording_time(path: str) -> datetime | None:
    """尽力从音频元数据里取「录制/发布时刻」。"""
    try:
        from mutagen import File as MutagenFile
    except Exception:  # noqa: BLE001
        MutagenFile = None

    candidates: list[datetime] = []
    if MutagenFile is not None:
        try:
            media = MutagenFile(path, easy=True)
        except Exception:  # noqa: BLE001
            media = None
        tags = getattr(media, "tags", None)
        if tags:
            for key in ("date", "year", "originaldate", "creation_time", "TDRC", "TYER", "©day"):
                try:
                    values = tags.get(key)
                except Exception:  # noqa: BLE001
                    values = None
                if not values:
                    continue
                stamp = _parse_datetime(str(values[0]))
                if stamp:
                    candidates.append(stamp)
        info = getattr(media, "info", None)
        for attribute in ("date", "creation_time", "encoded_date"):
            stamp = _parse_datetime(str(getattr(info, attribute, "") or ""))
            if stamp:
                candidates.append(stamp)

    if not candidates:
        try:
            candidates.append(datetime.fromtimestamp(os.path.getmtime(path)))
        except Exception:  # noqa: BLE001
            return None
    return min(candidates)


def _parse_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d%H%M%S", "%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt)
        except ValueError:
            continue
    digits = re.findall(r"\d+", text)
    if digits and len(digits[0]) == 4:
        try:
            return datetime(int(digits[0]), 1, 1)
        except ValueError:
            return None
    return None


def order_sources(paths: list[str], *, explicit: bool = False, log=None) -> tuple[list[str], str]:
    """返回 (排好序的素材, 排序依据说明)。``explicit=True`` 表示用户已手动排过序。"""
    if len(paths) <= 1:
        return list(paths), "只有一个素材，无需排序"
    if explicit:
        return list(paths), "沿用界面上的顺序（用户已手动排序）"

    stamps = [probe_recording_time(p) for p in paths]
    if all(stamps) and len({s.date() for s in stamps}) > 1:
        paired = sorted(zip(paths, stamps), key=lambda item: item[1])
        return [p for p, _ in paired], "按录制时间元数据排序"
    if all(stamps):
        ordered = sorted(zip(paths, stamps), key=lambda item: item[1])
        if [p for p, _ in ordered] != list(paths):
            return [p for p, _ in ordered], "按文件时间戳排序"

    ordered = sorted(paths, key=natural_key)
    if ordered != list(paths):
        return ordered, "按文件名自然序排序（含序号/日期）"
    return list(paths), "文件名与元数据都分不出先后"


def order_by_content(parts: list[tuple[str, str]], client, *, log=None) -> tuple[list[int], str]:
    """用大模型按语义衔接判断顺序。``parts`` 是 [(标题, 开头文本)]。

    返回 ``(排列后的下标, 依据说明)``；模型给不出可靠答案时保持原序。
    """
    if len(parts) <= 1:
        return list(range(len(parts))), "无需排序"
    listing = "\n\n".join(
        f"[{index}] 文件：{title}\n开头内容：{head[:700]}" for index, (title, head) in enumerate(parts)
    )
    prompt = (
        "下面是同一堂课的若干段录音转写稿（可能被切分过，顺序是乱的）。"
        "请根据内容的时间衔接关系（例如「上一段我们讲到」「继续刚才的」「我们先看…然后」"
        "以及话题的推进逻辑）判断它们的**真实先后顺序**。\n\n"
        f"{listing}\n\n"
        '只输出 JSON：{"order": [排序后的下标...], "reason": "一句话依据", "confidence": "high|medium|low"}\n'
        "如果证据不足以判断，把 confidence 设为 low 并按原顺序输出。"
    )
    try:
        payload = client.json_chat(prompt, system="你是严谨的课堂转写稿整理助手。")
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"内容排序失败（保持原序）：{exc}")
        return list(range(len(parts))), "内容排序不可用，保持原序"

    order = payload.get("order") if isinstance(payload, dict) else None
    confidence = (payload or {}).get("confidence", "low") if isinstance(payload, dict) else "low"
    reason = (payload or {}).get("reason", "") if isinstance(payload, dict) else ""
    if not isinstance(order, list) or sorted(order) != list(range(len(parts))):
        return list(range(len(parts))), "模型返回顺序不合法，保持原序"
    if confidence == "low":
        return list(range(len(parts))), f"模型没有把握（{reason}），保持原序"
    return [int(i) for i in order], f"按内容衔接排序（{confidence}）：{reason}"


# ------------------------------------------------------------------ 合并
def merge_segments(groups: list[list[Segment]], *, offsets: list[float] | None = None) -> list[Segment]:
    """把多段转写按顺序拼成一条时间轴。"""
    merged: list[Segment] = []
    offset = 0.0
    for index, group in enumerate(groups):
        local_offset = offsets[index] if offsets and index < len(offsets) else offset
        for segment in group:
            start = None if segment.start is None else segment.start + local_offset
            end = None if segment.end is None else segment.end + local_offset
            merged.append(Segment(text=segment.text, start=start, end=end, source=segment.source))
        offset = local_offset + max([s.end or 0.0 for s in group] + [0.0])
    return merged


def format_transcript(segments: list[Segment], *, with_timestamp: bool = True, joiner: str = "\n") -> str:
    lines: list[str] = []
    for segment in segments:
        if with_timestamp and segment.start is not None:
            lines.append(f"[{format_timestamp(segment.start)}] {segment.text}")
        else:
            lines.append(segment.text)
    return joiner.join(lines)


def transcript_duration(segments: list[Segment]) -> float:
    ends = [s.end or s.start or 0.0 for s in segments]
    return max(ends) if ends else 0.0


def write_transcript(path: str, segments: list[Segment], *, with_timestamp: bool = True) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(format_transcript(segments, with_timestamp=with_timestamp))
        handle.write("\n")
    return path

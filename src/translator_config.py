"""配置层：兼容旧版「全球通翻译工具」的 translator_config.json，并扩展为
「课件翻译 + 图片内文字翻译 + 课程知识提取 + 课堂报告」的完整流水线配置。

旧版字段：api_key / model / src_lang / tgt_lang / max_workers
（注意：旧版实际写盘的并发字段名是 workers，加载时自动映射到 max_workers）
本版新增：接口端点、模型回退、图片 OCR 翻译、课程知识与课堂报告、Whisper 选项。

旧配置可直接沿用；缺失字段用默认值补齐；旧模型别名在加载时迁移到当前 Flash 模型。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass

CONFIG_FILE = "translator_config.json"
#: 用户级设置目录名（放 %APPDATA% 下，跟 exe 放哪儿无关）
APP_DIR_NAME = "PPT课件学习助手"

LANG_MAPPING: dict[str, str] = {
    "自动检测": "Auto",
    "中文": "Chinese",
    "英文": "English",
    "日语": "Japanese",
    "韩语": "Korean",
    "法语": "French",
    "德语": "German",
    "西班牙语": "Spanish",
    "俄语": "Russian",
    "意大利语": "Italian",
    "葡萄牙语": "Portuguese",
}

# 端点当前可用的模型（2026-10 实测）：
#   deepseek-flash     -> 服务端 model 字段回显 deepseek-flash，即「DeepSeek 4.1 Flash」
#   deepseek-v4-pro    -> 高质量推理档
#   deepseek-v4-flash  -> 旧别名，请求后同样解析到 deepseek-flash
DEFAULT_MODEL = "deepseek-flash"
MODEL_CHOICES: tuple[str, ...] = ("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash")
MODEL_LABELS: dict[str, str] = {
    "deepseek-flash": "deepseek-flash — DeepSeek 4.1 Flash（默认 · 快 · 便宜）",
    "deepseek-v4-pro": "deepseek-v4-pro — 深度推理（更贵更慢，适合长文报告）",
    "deepseek-v4-flash": "deepseek-v4-flash — 旧别名（等价于 deepseek-flash）",
}
# 只在服务端明确报「模型不存在」时按顺序回退
MODEL_FALLBACKS: tuple[str, ...] = ("deepseek-flash", "deepseek-v4-flash", "deepseek-chat")
LEGACY_MODEL_ALIASES: dict[str, str] = {"deepseek-v4-flash": DEFAULT_MODEL}

DEFAULT_API_BASE = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_REQUEST_TIMEOUT = 120
DEFAULT_MAX_WORKERS = 8

IMAGE_MODES: dict[str, str] = {
    "inpaint": "重绘图片（擦掉原文，写入译文，保版式）",
    "overlay": "叠加可编辑文本框（清除原文字，保持图片位置）",
}

WHISPER_MODELS: tuple[str, ...] = ("tiny", "base", "small", "medium", "large-v3")
WHISPER_LABELS: dict[str, str] = {
    "tiny": "tiny — 最快最省，准确率低（约 75 MB）",
    "base": "base — 快（约 145 MB）",
    "small": "small — 推荐，中英混讲够用（约 480 MB）",
    "medium": "medium — 更准更慢（约 1.5 GB）",
    "large-v3": "large-v3 — 最准，CPU 上很慢（约 3 GB）",
}


@dataclass(slots=True)
class TranslatorConfig:
    # ---- 旧版字段（保持一致，便于沿用已有配置）----
    api_key: str = ""
    model: str = DEFAULT_MODEL
    src_lang: str = "自动检测"
    tgt_lang: str = "中文"
    max_workers: int = DEFAULT_MAX_WORKERS

    # ---- 接口与通用 ----
    #: 是否把 API Key 写进配置文件（下次启动自动填入）。关掉即不再落盘。
    remember_api_key: bool = True
    api_base: str = DEFAULT_API_BASE
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    output_dir: str = ""
    last_path: str = ""

    # ---- PDF 转 PPT ----
    pdf_dpi: int = 200
    pdf_detect_tables: bool = True
    pdf_keep_page_size: bool = True
    pdf_rasterize_background: bool = True
    only_convert: bool = False

    # ---- 图片内文字翻译（OCR）----
    translate_images: bool = True
    image_mode: str = "inpaint"
    ocr_min_confidence: float = 0.60
    ocr_min_chars: int = 2
    ocr_max_side: int = 1600
    pdf_ocr_images: bool = True

    # ---- 课程知识提取 ----
    make_knowledge: bool = True
    make_exam_focus: bool = True

    # ---- 课堂报告（录音/文本 + 课件）----
    make_report: bool = True
    whisper_model: str = "small"
    whisper_language: str = "auto"
    merge_chunk_chars: int = 6000

    # ---- 课程信息 ----
    course_name: str = ""
    course_dir: str = ""
    outline_prefix: str = "KP"

    # ------------------------------------------------------------------ 便捷方法
    def target_language_name(self) -> str:
        """输出文件名用的目标语言英文名（与旧版一致）。"""
        if self.tgt_lang in LANG_MAPPING:
            return LANG_MAPPING[self.tgt_lang]
        if self.tgt_lang in LANG_MAPPING.values():
            return self.tgt_lang
        return "Translated"

    def source_language_name(self) -> str:
        return LANG_MAPPING.get(self.src_lang, "Auto")

    def is_chinese_target(self) -> bool:
        return self.target_language_name() == "Chinese"

    def effective_model(self) -> str:
        return self.model or DEFAULT_MODEL


def portable_dir() -> str:
    """程序自己所在的目录（frozen 下是 exe 所在目录，源码下是 src 目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_config_dir() -> str:
    """用户级设置目录：%APPDATA%\\PPT课件学习助手。

    为什么不放在 exe 旁边：那样一旦把 exe 复制/移动到别处（比如桌面），
    设置就留在原地了，用户得把 API Key 重新输一遍 —— 这正是老版本的毛病。
    放进用户目录后，无论 exe 放哪儿、改名与否，设置都跟着人走。
    """
    for variable in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            return os.path.join(base, APP_DIR_NAME)
    return portable_dir()


def _config_path(base_dir: str | None = None) -> str:
    """配置文件的写入位置。"""
    if base_dir:
        return os.path.join(base_dir, CONFIG_FILE)
    return os.path.join(user_config_dir(), CONFIG_FILE)


def config_path() -> str:
    """当前生效的配置文件路径（界面用来告诉用户设置存哪儿了）。"""
    return _config_path()


def legacy_config_paths(extra_dirs=None) -> list[str]:
    """老版本把配置写在程序旁边的那些位置（按新→旧给出）。

    v2.0 早期版本写在 exe 同级目录；v1.0 原版写在它自己的 exe 同级目录。
    首次运行时从这些地方把设置搬过来，用户就不用重新填 Key。
    """
    directories: list[str] = []
    if extra_dirs:
        directories.extend(extra_dirs)
    directories.append(portable_dir())
    directories.append(os.getcwd())
    seen: set[str] = set()
    paths: list[str] = []
    for directory in directories:
        if not directory:
            continue
        candidate = os.path.abspath(os.path.join(directory, CONFIG_FILE))
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.exists(candidate):
            paths.append(candidate)
    # 新的（最近改过的）优先
    paths.sort(key=lambda item: os.path.getmtime(item), reverse=True)
    return paths


def _migrate(raw: dict) -> dict:
    payload = dict(raw)
    if "workers" in payload and "max_workers" not in payload:
        try:
            payload["max_workers"] = int(payload["workers"])
        except (TypeError, ValueError):
            pass
    payload.pop("workers", None)
    model = payload.get("model")
    if isinstance(model, str) and model in LEGACY_MODEL_ALIASES:
        payload["model"] = LEGACY_MODEL_ALIASES[model]
    if payload.get("image_mode") not in IMAGE_MODES:
        payload["image_mode"] = "inpaint"
    return payload


def load_config(path: str | None = None, extra_dirs=None) -> TranslatorConfig:
    """读取配置。

    显式传 ``path`` 时只用该文件；否则先看用户目录，再回退到程序旁边的旧文件，
    并把旧文件迁移（复制）到用户目录，之后就以用户目录为准。
    """
    target = path or _config_path()
    if not path and not os.path.exists(target):
        legacy = legacy_config_paths(extra_dirs)
        if not legacy:
            return TranslatorConfig()
        target = legacy[0]
    if not os.path.exists(target):
        return TranslatorConfig()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return TranslatorConfig()
    if not isinstance(raw, dict):
        return TranslatorConfig()
    if not path:
        _migrate_config_file(target, raw)

    known = set(TranslatorConfig.__dataclass_fields__)
    payload = {k: v for k, v in _migrate(raw).items() if k in known}
    try:
        return TranslatorConfig(**payload)
    except TypeError:
        return TranslatorConfig()


def _migrate_config_file(source: str, raw: dict) -> None:
    """把程序旁边的旧配置搬到用户目录（只复制，不删原件，失败也不影响使用）。"""
    target = _config_path()
    if os.path.abspath(source) == os.path.abspath(target):
        return
    try:
        save_config(TranslatorConfig(**{k: v for k, v in _migrate(raw).items()
                                        if k in set(TranslatorConfig.__dataclass_fields__)}))
    except Exception:  # noqa: BLE001
        pass


def save_config(config: TranslatorConfig, path: str | None = None) -> str:
    """写配置。默认写到用户目录；写之前建目录，写的时候先写临时文件再原子替换，
    避免中途断电/被杀进程留下半个损坏的 json。"""
    target = path or _config_path()
    data = asdict(config)
    data["workers"] = config.max_workers  # 兼容旧版读取习惯
    if not config.remember_api_key:
        data["api_key"] = ""
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)
    return target


def output_path_for(src_path: str, config: TranslatorConfig, *, suffix: str | None = None) -> str:
    """按旧版习惯生成输出名：<原名>_<目标语言>.pptx（如 Lecture03_Chinese.pptx）。"""
    directory, filename = os.path.split(os.path.abspath(src_path))
    stem, _ext = os.path.splitext(filename)
    tag = suffix if suffix is not None else config.target_language_name()
    return os.path.join(directory, f"{stem}_{tag}.pptx")

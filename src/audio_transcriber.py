"""本地语音转写：faster-whisper。

集成的开源项目：**SYSTRAN/faster-whisper**（MIT，GitHub 25k+ star）
  * OpenAI Whisper 的 CTranslate2 重实现：CPU 上快约 4 倍、内存减半；
  * **不依赖 torch**（原版 whisper 要拖 2 GB 的 torch），因此能塞进单文件 exe；
  * 中英混讲、术语夹英文的课堂录音表现稳定，支持 VAD 静音跳过与时间戳。

模型权重首次使用时下载到 ``%LOCALAPPDATA%\\PPT课件学习助手\\models``；
官方源不可达时自动改走 hf-mirror.com 镜像（用子进程重试，避免 huggingface_hub
在导入期就把 endpoint 常量固化掉）。
"""

from __future__ import annotations

import os
import subprocess
import sys

from transcript_io import Segment, format_timestamp

APP_DIR_NAME = "PPT课件学习助手"
MODEL_REPO_TEMPLATE = "Systran/faster-whisper-{size}"

_DOWNLOAD_SNIPPET = (
    "import os, sys\n"
    "from huggingface_hub import snapshot_download\n"
    "repo, target = sys.argv[1], sys.argv[2]\n"
    "os.makedirs(target, exist_ok=True)\n"
    "print('[download] ' + repo + ' -> ' + target, flush=True)\n"
    "snapshot_download(repo_id=repo, local_dir=target)\n"
    "print('[download] done', flush=True)\n"
)


def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def model_dir(model_size: str) -> str:
    return os.path.join(app_data_dir(), "models", f"faster-whisper-{model_size}")


def _model_ready(target: str) -> bool:
    return os.path.exists(os.path.join(target, "model.bin"))


def ensure_model(model_size: str, *, log=None, allow_download: bool = True) -> str:
    """确保模型可用，返回本地目录。"""
    log = log or (lambda _m: None)
    target = model_dir(model_size)
    if _model_ready(target):
        return target
    if not allow_download:
        raise RuntimeError(f"本地没有 {model_size} 模型，且当前不允许下载")

    repo = MODEL_REPO_TEMPLATE.format(size=model_size)
    log(f"本地没有 {model_size} 模型，开始下载（{repo}）…")
    try:
        _download_with_subprocess(repo, target, endpoint=None)
        if _model_ready(target):
            return target
    except Exception as exc:  # noqa: BLE001
        log(f"从 HuggingFace 官方源下载失败：{exc}")
    log("改用国内镜像 hf-mirror.com 重试…")
    _download_with_subprocess(repo, target, endpoint="https://hf-mirror.com")
    if not _model_ready(target):
        raise RuntimeError(
            f"模型下载失败：{target}。请联网后重试，"
            "或手动把 Systran/faster-whisper-"
            f"{model_size} 的文件放进该目录。"
        )
    return target


def _download_with_subprocess(repo: str, target: str, *, endpoint: str | None) -> None:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--download-whisper", repo, target]
    else:
        command = [sys.executable, "-c", _DOWNLOAD_SNIPPET, repo, target]
    result = subprocess.run(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout or "").strip()[-500:] or "下载进程异常退出")


def probe_duration(path: str) -> float:
    """尽量低成本地取时长（秒）。"""
    try:
        from mutagen import File as MutagenFile

        media = MutagenFile(path)
        length = getattr(getattr(media, "info", None), "length", None)
        if length:
            return float(length)
    except Exception:  # noqa: BLE001
        pass
    try:
        from faster_whisper.audio import decode_audio

        return len(decode_audio(path)) / 16000.0
    except Exception:  # noqa: BLE001
        return 0.0


class Transcriber:
    """faster-whisper 的薄封装（懒加载模型，流式产出片段）。"""

    def __init__(
        self,
        model_size: str = "small",
        *,
        device: str = "auto",
        compute_type: str = "int8",
        language: str | None = None,
        initial_prompt: str | None = None,
        log=None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = None if (not language or language == "auto") else language
        self.initial_prompt = initial_prompt
        self._model = None
        self._log = log or (lambda _m: None)

    def _ensure(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel

        path = ensure_model(self.model_size, log=self._log)
        attempts = [(self.device, self.compute_type)]
        if (self.device, self.compute_type) != ("cpu", "int8"):
            attempts.append(("cpu", "int8"))
        last_error: Exception | None = None
        for device, compute_type in attempts:
            try:
                model = WhisperModel(path, device=device, compute_type=compute_type)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            # device="auto" 可能选中 CUDA，但机器上没有 cublas 运行库：
            # 加载能成功、第一次推理才炸。所以这里先跑一小段静音做自检。
            if self._selfcheck(model):
                self._model = model
                self._log(f"转写模型就绪：{self.model_size}（{device}/{compute_type}）")
                return model
            last_error = RuntimeError(f"{device}/{compute_type} 推理自检失败")
            if device != "cpu":
                self._log(f"{device} 不可用（缺少 CUDA 运行库），改用 CPU 推理")
        raise RuntimeError(f"无法加载转写模型：{last_error}")

    def _selfcheck(self, model) -> bool:
        import numpy as np

        try:
            segments, _info = model.transcribe(
                np.zeros(8000, dtype=np.float32), language=self.language, beam_size=1, vad_filter=False
            )
            for _ in segments:
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    def transcribe(self, path: str, *, progress=None, on_segment=None) -> list[Segment]:
        model = self._ensure()
        duration = probe_duration(path)
        options = dict(
            language=self.language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        if self.initial_prompt:
            options["initial_prompt"] = self.initial_prompt
        try:
            iterator, info = model.transcribe(path, **options)
        except TypeError:
            options.pop("vad_parameters", None)
            iterator, info = model.transcribe(path, **options)

        total = duration or float(getattr(info, "duration", 0.0) or 0.0)
        segments: list[Segment] = []
        for chunk in iterator:
            text = (chunk.text or "").strip()
            if not text:
                continue
            segment = Segment(text=text, start=float(chunk.start), end=float(chunk.end), source=path)
            segments.append(segment)
            if on_segment:
                on_segment(segment)
            if progress and total > 0:
                progress(min(1.0, float(chunk.end) / total), f"{format_timestamp(chunk.end)} / {format_timestamp(total)}")
        return segments

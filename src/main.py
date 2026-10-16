"""PPT 课件学习助手 v2.0

在「映画电脑软件工作室全球通翻译工具（DeepSeek 版 v1.0）」的行为规格上重建并扩展：

  1) 课件翻译：.pptx 直接翻译；.pdf 先转 PPT 再翻译（离线解析，无需额外 token）；
  2) 图片内文字翻译：OCR 识别插图/图表/坐标轴里的文字 → 擦除原文 → 中文重绘，
     或擦除后叠加可编辑文本框（原版完全不做这件事）；
  3) 知识点总结：从课件生成三级知识点大纲 + 图文详细内容 + 一页速览（Word 可直接打印）；
  4) 课堂复习报告：多份（可能乱序的）录音/文本素材自动排序 → 本地转写 →
     与课件对齐 → 讲课流程 + 详细内容报告，并单独抽出**考试重点**（老师原话 + 时间戳）。

默认模型为 DeepSeek 的当前 Flash 档（``deepseek-flash``，即 4.1 Flash）。
"""

from __future__ import annotations

import os
import queue
import sys
import traceback

# ---------------------------------------------------------------------------
# 关键：OCR 推理后端必须在 PyQt5 之前导入。
# 实测（Windows + onnxruntime 1.30 + PyQt5 5.15）：先加载 Qt5 再 import onnxruntime，
# 会直接触发访问违例 0xC0000005 把整个进程打崩（不是异常，是硬崩溃）。
# 顺序反过来则一切正常，所以把 onnxruntime 的导入提到最前面。
# ---------------------------------------------------------------------------
for _preload in ("onnxruntime", "cv2"):
    try:
        __import__(_preload)
    except Exception:  # noqa: BLE001
        pass

from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pipeline import UnsupportedInput, unique_output_path
from translator_config import (
    IMAGE_MODES,
    LANG_MAPPING,
    MODEL_CHOICES,
    MODEL_LABELS,
    WHISPER_LABELS,
    WHISPER_MODELS,
    TranslatorConfig,
    config_path,
    load_config,
    save_config,
)

APP_TITLE = "PPT 课件学习助手 v2.2 · 图片文字增强"
APP_SUBTITLE = (
    "翻译（含图片内文字） · 知识点总结 · 课堂复习报告（考试重点强调） · "
    "默认模型 DeepSeek 4.1 Flash"
)

MEDIA_FILTER = "录音/录像/转写稿 (*.mp3 *.m4a *.wav *.aac *.flac *.ogg *.opus *.wma *.mp4 *.mkv *.mov *.webm *.txt *.md *.srt *.vtt *.json *.docx)"
DECK_FILTER = "课件 (*.pptx *.pdf)"


class Worker(QThread):
    """后台任务线程：只做计算，界面更新一律通过队列回到主线程。"""

    def __init__(self, job, log_queue: queue.Queue) -> None:
        super().__init__()
        self.job = job
        self.log_queue = log_queue

    def _log(self, message: str) -> None:
        self.log_queue.put(("log", str(message)))

    def _progress(self, done, total=None) -> None:
        if isinstance(done, float) and 0.0 <= done <= 1.0 and total is None:
            self.log_queue.put(("progress_text", f"{done * 100:.0f}%"))
        elif isinstance(done, str):
            self.log_queue.put(("progress_text", done))
        else:
            self.log_queue.put(("progress", done, total))

    def run(self) -> None:  # noqa: D102
        try:
            result = self.job(self._log, self._progress)
            self.log_queue.put(("done", result))
        except UnsupportedInput as exc:
            self.log_queue.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.log_queue.put(("error", detail))
            self.log_queue.put(("trace", traceback.format_exc()))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(940, 780)

        self.config = load_config()
        self.log_queue: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.last_output: str | None = None

        self._build_ui()
        self._load_config_into_ui()

        # 自动保存：API Key 等一改就（延迟）写盘，不用非得先跑一次翻译
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(1200)
        self.save_timer.timeout.connect(self._autosave)
        for signal in (
            self.key_edit.textChanged,
            self.base_edit.textChanged,
            self.chk_remember.toggled,
        ):
            signal.connect(self._touch_config)

        # 与旧版一致：定时从队列批量取数据刷新，避免逐条 setText 卡顿
        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(120)
        self.flush_timer.timeout.connect(self._flush_queue)
        self.flush_timer.start()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title = QLabel(APP_TITLE)
        title.setStyleSheet("font-size:16px;font-weight:600;")
        root.addWidget(title)
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setStyleSheet("color:#666;")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_translate_tab(), "① 课件翻译")
        self.tabs.addTab(self._build_knowledge_tab(), "② 知识点总结")
        self.tabs.addTab(self._build_report_tab(), "③ 课堂复习报告")
        root.addWidget(self.tabs, 1)

        root.addWidget(self._build_common_group())

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#444;")
        root.addWidget(self.status_label)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(150)
        root.addWidget(self.log_box, 1)

        bottom = QHBoxLayout()
        self.open_btn = QPushButton("打开输出目录")
        self.open_btn.clicked.connect(self._on_open_output)
        bottom.addWidget(self.open_btn)
        bottom.addStretch(1)
        self.start_btn = QPushButton("开始翻译")
        self.start_btn.setStyleSheet("font-weight:600;padding:6px 18px;")
        self.start_btn.clicked.connect(self._on_start_translate)
        bottom.addWidget(self.start_btn)
        root.addLayout(bottom)

    # ---- ① 翻译页 ----
    def _build_translate_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        file_group = QGroupBox("待翻译课件")
        file_layout = QGridLayout(file_group)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择 .pptx（翻译既有文本）或 .pdf（转 PPT 后翻译）")
        file_layout.addWidget(self.path_edit, 0, 0, 1, 3)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse_translate)
        file_layout.addWidget(browse, 0, 3)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("输出目录（留空 = 与源文件同目录；同名绝不覆盖，自动加 _2）")
        file_layout.addWidget(QLabel("输出目录"), 1, 0)
        file_layout.addWidget(self.out_edit, 1, 1, 1, 2)
        out_browse = QPushButton("选择…")
        out_browse.clicked.connect(self._on_browse_out)
        file_layout.addWidget(out_browse, 1, 3)
        layout.addWidget(file_group)

        image_group = QGroupBox("图片内文字翻译（原版不做，本版新增）")
        image_layout = QGridLayout(image_group)
        self.chk_images = QCheckBox("翻译插图 / 图表 / 坐标轴里的文字（离线 OCR + 中文重绘）")
        image_layout.addWidget(self.chk_images, 0, 0, 1, 3)
        image_layout.addWidget(QLabel("处理方式"), 1, 0)
        self.combo_image_mode = QComboBox()
        for key, label in IMAGE_MODES.items():
            self.combo_image_mode.addItem(label, key)
        image_layout.addWidget(self.combo_image_mode, 1, 1, 1, 2)
        image_layout.addWidget(QLabel("OCR 置信度下限"), 2, 0)
        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.30, 0.95)
        self.spin_conf.setSingleStep(0.05)
        self.spin_conf.setDecimals(2)
        image_layout.addWidget(self.spin_conf, 2, 1)
        self.chk_pdf_ocr = QCheckBox("PDF：也翻译页面图片里的文字")
        image_layout.addWidget(self.chk_pdf_ocr, 3, 0, 1, 3)
        layout.addWidget(image_group)

        pdf_group = QGroupBox("PDF 转换选项")
        pdf_layout = QGridLayout(pdf_group)
        self.chk_tables = QCheckBox("识别表格并还原为原生 PPT 表格")
        pdf_layout.addWidget(self.chk_tables, 0, 0)
        self.chk_page_size = QCheckBox("保持 PDF 原始页面尺寸（否则 16:9）")
        pdf_layout.addWidget(self.chk_page_size, 0, 1)
        self.chk_background = QCheckBox("保留背景（图片/图表/公式原样保留）")
        pdf_layout.addWidget(self.chk_background, 1, 0)
        self.chk_only_convert = QCheckBox("仅转换不翻译")
        pdf_layout.addWidget(self.chk_only_convert, 1, 1)
        pdf_layout.addWidget(QLabel("背景渲染 DPI"), 2, 0)
        self.spin_dpi = QSpinBox()
        self.spin_dpi.setRange(72, 400)
        self.spin_dpi.setSingleStep(10)
        pdf_layout.addWidget(self.spin_dpi, 2, 1)
        layout.addWidget(pdf_group)

        self.chk_chain = QCheckBox("翻译完成后，顺带生成「知识点总结」（沿用当前路径与课程名）")
        layout.addWidget(self.chk_chain)
        layout.addStretch(1)
        return page

    # ---- ② 知识点总结页 ----
    def _build_knowledge_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        deck_group = QGroupBox("课件")
        deck_layout = QGridLayout(deck_group)
        self.kb_path_edit = QLineEdit()
        self.kb_path_edit.setPlaceholderText("选择 .pptx 或 .pdf（可先用「① 课件翻译」产出中文版再拿来用）")
        deck_layout.addWidget(self.kb_path_edit, 0, 0, 1, 3)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse_knowledge)
        deck_layout.addWidget(browse, 0, 3)
        deck_layout.addWidget(QLabel("课程名称"), 1, 0)
        self.kb_course_edit = QLineEdit()
        self.kb_course_edit.setPlaceholderText("例如：成瘾行为导论 · 第 8 讲")
        deck_layout.addWidget(self.kb_course_edit, 1, 1, 1, 3)
        deck_layout.addWidget(QLabel("知识库目录"), 2, 0)
        self.kb_dir_edit = QLineEdit()
        self.kb_dir_edit.setPlaceholderText("留空 = 课件同目录下的「<文件名>_课程知识库」")
        deck_layout.addWidget(self.kb_dir_edit, 2, 1, 1, 3)
        layout.addWidget(deck_group)

        tip = QLabel(
            "产出：逐页文本与关键图（02_slides）· 三级知识点大纲（06_outline，含机器可读 JSON）·\n"
            "知识图谱（08_graph）· 详细内容报告与一页速览（07_notes）· 可直接打印的 Word 知识点总结。"
        )
        tip.setStyleSheet("color:#666;")
        layout.addWidget(tip)

        self.chk_kb_images = QCheckBox("顺便把课件插图里的文字翻成中文（就地改写 02_slides 里的关键图）")
        layout.addWidget(self.chk_kb_images)
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.kb_start_btn = QPushButton("生成知识点总结")
        self.kb_start_btn.setStyleSheet("font-weight:600;padding:6px 18px;")
        self.kb_start_btn.clicked.connect(self._on_start_knowledge)
        row.addWidget(self.kb_start_btn)
        layout.addLayout(row)
        return page

    # ---- ③ 课堂复习报告页 ----
    def _build_report_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        deck_group = QGroupBox("课件")
        deck_layout = QGridLayout(deck_group)
        self.rp_path_edit = QLineEdit()
        self.rp_path_edit.setPlaceholderText("本堂课的 .pptx / .pdf")
        deck_layout.addWidget(self.rp_path_edit, 0, 0, 1, 3)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse_report)
        deck_layout.addWidget(browse, 0, 3)
        deck_layout.addWidget(QLabel("课程名称"), 1, 0)
        self.rp_course_edit = QLineEdit()
        self.rp_course_edit.setPlaceholderText("例如：成瘾行为导论 · 第 8 讲")
        deck_layout.addWidget(self.rp_course_edit, 1, 1, 1, 3)
        deck_layout.addWidget(QLabel("知识库目录"), 2, 0)
        self.rp_dir_edit = QLineEdit()
        self.rp_dir_edit.setPlaceholderText("留空 = 课件同目录下的「<文件名>_课程知识库」")
        deck_layout.addWidget(self.rp_dir_edit, 2, 1, 1, 3)
        layout.addWidget(deck_group)

        media_group = QGroupBox("课堂素材（录音 / 录像 / 已有转写稿，可以多份、顺序乱也没关系）")
        media_layout = QVBoxLayout(media_group)
        self.media_list = QListWidget()
        self.media_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.media_list.setMinimumHeight(110)
        media_layout.addWidget(self.media_list)
        buttons = QHBoxLayout()
        for label, slot in (
            ("添加文件…", self._on_add_media),
            ("移除", self._on_remove_media),
            ("上移", lambda: self._move_media(-1)),
            ("下移", lambda: self._move_media(1)),
            ("自动排序", self._on_auto_order),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        media_layout.addLayout(buttons)
        self.chk_order_locked = QCheckBox("顺序已确认（素材顺序按上面列表为准，不再自动重排）")
        media_layout.addWidget(self.chk_order_locked)
        layout.addWidget(media_group)

        speech_group = QGroupBox("本地转写（faster-whisper，离线运行；模型首次使用会自动下载）")
        speech_layout = QGridLayout(speech_group)
        speech_layout.addWidget(QLabel("模型"), 0, 0)
        self.combo_whisper = QComboBox()
        for size in WHISPER_MODELS:
            self.combo_whisper.addItem(WHISPER_LABELS.get(size, size), size)
        speech_layout.addWidget(self.combo_whisper, 0, 1)
        speech_layout.addWidget(QLabel("录音语言"), 0, 2)
        self.combo_whisper_lang = QComboBox()
        self.combo_whisper_lang.addItem("自动检测", "auto")
        self.combo_whisper_lang.addItem("英语", "en")
        self.combo_whisper_lang.addItem("中文", "zh")
        self.combo_whisper_lang.addItem("中英混讲（按英语转写）", "en")
        speech_layout.addWidget(self.combo_whisper_lang, 0, 3)
        layout.addWidget(speech_group)

        tip = QLabel(
            "产出：转写稿（01_transcript）· 页-讲述对齐（03_align）· ASR 纠错清单（04_corrections）·\n"
            "讲课流程 + 详细内容报告 + **考试重点**（07_notes）· 合并版 Word《课堂复习报告》。\n"
            "考试重点会把老师说过的「考试/考点/必考/记住/题型/作业/期末」等原话逐条摘出并带时间戳。"
        )
        tip.setStyleSheet("color:#666;")
        layout.addWidget(tip)

        row = QHBoxLayout()
        row.addStretch(1)
        self.rp_start_btn = QPushButton("生成课堂复习报告")
        self.rp_start_btn.setStyleSheet("font-weight:600;padding:6px 18px;")
        self.rp_start_btn.clicked.connect(self._on_start_report)
        row.addWidget(self.rp_start_btn)
        layout.addLayout(row)
        return page

    # ---- 公共参数 ----
    def _build_common_group(self) -> QWidget:
        group = QGroupBox("接口与语言")
        layout = QGridLayout(group)
        layout.addWidget(QLabel("API Key"), 0, 0)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk- 开头（与原版通用）")
        layout.addWidget(self.key_edit, 0, 1, 1, 2)
        self.chk_remember = QCheckBox("记住 Key")
        self.chk_remember.setToolTip(
            "勾选后 API Key 会随其他设置一起保存，下次打开自动填入，不用再输一遍。"
        )
        layout.addWidget(self.chk_remember, 0, 3)
        layout.addWidget(QLabel("模型"), 1, 0)
        self.combo_model = QComboBox()
        self.combo_model.setEditable(True)
        for name in MODEL_CHOICES:
            self.combo_model.addItem(MODEL_LABELS.get(name, name), name)
        layout.addWidget(self.combo_model, 1, 1)
        layout.addWidget(QLabel("并发线程"), 1, 2)
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 32)
        layout.addWidget(self.spin_workers, 1, 3)
        layout.addWidget(QLabel("源语言"), 2, 0)
        self.combo_src = QComboBox()
        self.combo_src.addItems(list(LANG_MAPPING.keys()))
        layout.addWidget(self.combo_src, 2, 1)
        layout.addWidget(QLabel("目标语言"), 2, 2)
        self.combo_tgt = QComboBox()
        self.combo_tgt.addItems(list(LANG_MAPPING.keys()))
        layout.addWidget(self.combo_tgt, 2, 3)
        layout.addWidget(QLabel("接口地址"), 3, 0)
        self.base_edit = QLineEdit()
        layout.addWidget(self.base_edit, 3, 1, 1, 3)
        self.save_hint = QLabel()
        self.save_hint.setStyleSheet("color:#666;font-size:11px;")
        self.save_hint.setWordWrap(True)
        layout.addWidget(self.save_hint, 4, 0, 1, 4)
        return group

    # ------------------------------------------------------------------ 配置同步
    def _load_config_into_ui(self) -> None:
        config = self.config
        self.key_edit.setText(config.api_key)
        self.chk_remember.setChecked(bool(config.remember_api_key))
        model_index = self.combo_model.findData(config.model)
        if model_index < 0 and config.model:
            self.combo_model.setEditText(config.model)
        else:
            self.combo_model.setCurrentIndex(max(0, model_index))
        self.base_edit.setText(config.api_base)
        self.spin_workers.setValue(int(config.max_workers))
        self.combo_src.setCurrentText(config.src_lang if config.src_lang in LANG_MAPPING else "自动检测")
        self.combo_tgt.setCurrentText(config.tgt_lang if config.tgt_lang in LANG_MAPPING else "中文")
        self.out_edit.setText(config.output_dir)
        self.path_edit.setText(config.last_path)
        self.chk_images.setChecked(config.translate_images)
        self.chk_pdf_ocr.setChecked(config.pdf_ocr_images)
        mode_index = self.combo_image_mode.findData(config.image_mode)
        self.combo_image_mode.setCurrentIndex(max(0, mode_index))
        self.spin_conf.setValue(float(config.ocr_min_confidence))
        self.chk_tables.setChecked(config.pdf_detect_tables)
        self.chk_page_size.setChecked(config.pdf_keep_page_size)
        self.chk_background.setChecked(config.pdf_rasterize_background)
        self.chk_only_convert.setChecked(config.only_convert)
        self.spin_dpi.setValue(int(config.pdf_dpi))
        self.chk_chain.setChecked(bool(config.make_knowledge))
        self.chk_kb_images.setChecked(config.translate_images)
        self.kb_course_edit.setText(config.course_name)
        self.rp_course_edit.setText(config.course_name)
        self.kb_dir_edit.setText(config.course_dir)
        self.rp_dir_edit.setText(config.course_dir)
        index = self.combo_whisper.findData(config.whisper_model)
        self.combo_whisper.setCurrentIndex(max(0, index))
        lang_index = self.combo_whisper_lang.findData(config.whisper_language)
        self.combo_whisper_lang.setCurrentIndex(max(0, lang_index))
        self._update_save_hint("已读取上次保存的设置" if config.api_key else "")

    def _collect_config(self) -> TranslatorConfig:
        config = self.config
        config.api_key = self.key_edit.text().strip()
        config.remember_api_key = self.chk_remember.isChecked()
        config.model = (self.combo_model.currentData() or self.combo_model.currentText()).strip()
        config.api_base = self.base_edit.text().strip() or config.api_base
        config.max_workers = int(self.spin_workers.value())
        config.src_lang = self.combo_src.currentText()
        config.tgt_lang = self.combo_tgt.currentText()
        config.output_dir = self.out_edit.text().strip()
        config.last_path = self.path_edit.text().strip()
        config.translate_images = self.chk_images.isChecked()
        config.pdf_ocr_images = self.chk_pdf_ocr.isChecked()
        config.image_mode = self.combo_image_mode.currentData() or "inpaint"
        config.ocr_min_confidence = float(self.spin_conf.value())
        config.pdf_detect_tables = self.chk_tables.isChecked()
        config.pdf_keep_page_size = self.chk_page_size.isChecked()
        config.pdf_rasterize_background = self.chk_background.isChecked()
        config.only_convert = self.chk_only_convert.isChecked()
        config.pdf_dpi = int(self.spin_dpi.value())
        config.make_knowledge = self.chk_chain.isChecked()
        config.whisper_model = self.combo_whisper.currentData() or "small"
        config.whisper_language = self.combo_whisper_lang.currentData() or "auto"
        return config

    # ------------------------------------------------------------------ 设置持久化
    def _touch_config(self, *_args) -> None:
        """界面上改了设置 -> 稍后写盘（合并连续输入，避免每敲一个字就写文件）。"""
        self.save_timer.start()

    def _update_save_hint(self, note: str = "") -> None:
        where = config_path()
        remembered = "已记住 Key" if self.chk_remember.isChecked() else "不保存 Key"
        text = f"设置自动保存在：{where}（{remembered}）"
        if note:
            text = f"{note} · {text}"
        self.save_hint.setText(text)

    def _autosave(self) -> None:
        """把当前界面设置写进配置文件。写不进去也不该影响用户干活。"""
        try:
            save_config(self._collect_config())
        except Exception as exc:  # noqa: BLE001
            self._update_save_hint(f"设置无法保存：{exc}")
            return
        self._update_save_hint()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """关窗前落盘，顺手停掉自动保存计时器。"""
        self.save_timer.stop()
        self._autosave()
        super().closeEvent(event)

    def _require_inputs(self, need_key: bool = True) -> bool:
        config = self._collect_config()
        if need_key and not config.api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 DeepSeek 的 API Key（sk- 开头）。")
            return False
        return True

    # ------------------------------------------------------------------ 选择文件
    def _pick_file(self, edit: QLineEdit, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", edit.text() or "", file_filter)
        if path:
            edit.setText(os.path.normpath(path))

    def _on_browse_translate(self) -> None:
        self._pick_file(self.path_edit, DECK_FILTER)

    def _on_browse_knowledge(self) -> None:
        self._pick_file(self.kb_path_edit, DECK_FILTER)

    def _on_browse_report(self) -> None:
        self._pick_file(self.rp_path_edit, DECK_FILTER)

    def _on_browse_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.out_edit.text() or "")
        if path:
            self.out_edit.setText(os.path.normpath(path))

    def _on_add_media(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "添加上课素材", "", MEDIA_FILTER)
        for path in paths:
            existing = [self.media_list.item(i).text() for i in range(self.media_list.count())]
            if os.path.normpath(path) not in existing:
                self.media_list.addItem(os.path.normpath(path))

    def _on_remove_media(self) -> None:
        for item in self.media_list.selectedItems():
            self.media_list.takeItem(self.media_list.row(item))

    def _move_media(self, delta: int) -> None:
        row = self.media_list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.media_list.count():
            return
        item = self.media_list.takeItem(row)
        self.media_list.insertItem(target, item)
        self.media_list.setCurrentRow(target)

    def _on_auto_order(self) -> None:
        from transcript_io import order_sources

        paths = [self.media_list.item(i).text() for i in range(self.media_list.count())]
        if len(paths) < 2:
            return
        ordered, method = order_sources(paths, log=self._log_direct)
        self.media_list.clear()
        for path in ordered:
            self.media_list.addItem(path)
        self._log_direct(f"自动排序：{method}")

    def media_paths(self) -> list[str]:
        return [self.media_list.item(i).text() for i in range(self.media_list.count())]

    # ------------------------------------------------------------------ 执行
    def _log_direct(self, message: str) -> None:
        self.log_box.append(str(message))

    def _set_running(self, running: bool) -> None:
        for button in (self.start_btn, self.kb_start_btn, self.rp_start_btn):
            button.setEnabled(not running)
        self.tabs.setEnabled(not running)
        self.start_btn.setText("处理中…" if running else "开始翻译")
        self.kb_start_btn.setText("处理中…" if running else "生成知识点总结")
        self.rp_start_btn.setText("处理中…" if running else "生成课堂复习报告")

    def _start(self, job, busy_text: str) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "正在处理", "上一个任务还没结束，请稍候。")
            return
        self.progress.setValue(0)
        self.status_label.setText(busy_text)
        self._set_running(True)
        self._log_direct(f"—— {busy_text} ——")
        self.worker = Worker(job, self.log_queue)
        self.worker.start()

    def _on_start_translate(self) -> None:
        if not self._require_inputs():
            return
        src = self.path_edit.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, "找不到文件", "请先选择存在的 .pptx 或 .pdf 文件。")
            return
        extension = os.path.splitext(src)[1].lower()
        if extension not in (".pptx", ".pdf"):
            QMessageBox.warning(self, "格式不支持", "只支持 .pptx 与 .pdf。")
            return
        config = self._collect_config()
        save_config(config)

        def job(log, progress):
            from pipeline import run_course_job, run_translate_job

            result = run_translate_job(src, config, log=log, progress=progress)
            log(f"已输出：{result.output_path}")
            if result.images_total:
                log(
                    f"图片处理：{result.images_total} 张命中文字，改写 {result.images_translated} 张，"
                    f"共 {result.image_regions} 处"
                )
            if config.make_knowledge:
                progress(0.98, f"顺带生成知识点总结：{result.output_path}")
                log("继续生成知识点总结（基于翻译后的课件）…")
                course = run_course_job(
                    result.output_path, [], config, log=log, progress=progress, explicit_order=True
                )
                result.knowledge_dir = course.course_dir
                result.report_files = list(course.files)
                result.outline_points = course.outline_points
                log(f"知识点总结已输出：{course.course_dir}")
            return result

        self._start(job, f"翻译 {os.path.basename(src)}")

    def _on_start_knowledge(self) -> None:
        if not self._require_inputs():
            return
        src = self.kb_path_edit.text().strip() or self.path_edit.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, "找不到文件", "请先选择课件（.pptx / .pdf）。")
            return
        config = self._collect_config()
        config.course_name = self.kb_course_edit.text().strip()
        config.course_dir = self.kb_dir_edit.text().strip()
        config.translate_images = self.chk_kb_images.isChecked()
        save_config(config)

        def job(log, progress):
            from pipeline import run_course_job

            return run_course_job(src, [], config, log=log, progress=progress, explicit_order=True)

        self._start(job, f"提取知识点：{os.path.basename(src)}")

    def _on_start_report(self) -> None:
        if not self._require_inputs():
            return
        src = self.rp_path_edit.text().strip() or self.path_edit.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, "找不到文件", "请先选择本堂课的课件。")
            return
        materials = self.media_paths()
        missing = [m for m in materials if not os.path.exists(m)]
        if missing:
            QMessageBox.warning(self, "素材缺失", "以下素材不存在：\n" + "\n".join(missing[:5]))
            return
        if not materials:
            answer = QMessageBox.question(
                self,
                "没有课堂素材",
                "没有添加录音或转写稿，将只根据课件生成知识点总结。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
        config = self._collect_config()
        config.course_name = self.rp_course_edit.text().strip()
        config.course_dir = self.rp_dir_edit.text().strip()
        save_config(config)
        explicit = self.chk_order_locked.isChecked()

        def job(log, progress):
            from pipeline import run_course_job

            if materials:
                log(f"课堂素材 {len(materials)} 份：自动排序" + ("（已锁定为界面顺序）" if explicit else ""))
            return run_course_job(
                src, materials, config, log=log, progress=progress, explicit_order=explicit
            )

        self._start(job, f"生成课堂复习报告：{os.path.basename(src)}")

    # ------------------------------------------------------------------ 队列
    def _flush_queue(self) -> None:
        dirty = False
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "log":
                self.log_box.append(item[1])
                dirty = True
            elif kind == "progress":
                done, total = item[1], item[2]
                if total:
                    self.progress.setValue(int(done / max(1, total) * 100))
                    self.status_label.setText(f"进度 {done}/{total}")
                dirty = True
            elif kind == "progress_text":
                self.status_label.setText(str(item[1]))
                dirty = True
            elif kind == "trace":
                self.log_box.append(item[1])
                dirty = True
            elif kind == "error":
                self.log_box.append(f"[错误] {item[1]}")
                self.status_label.setText("失败")
                self._set_running(False)
                QMessageBox.critical(self, "任务失败", str(item[1]))
                dirty = True
            elif kind == "done":
                self._on_done(item[1])
                dirty = True
        if dirty:
            self.log_box.moveCursor(self.log_box.textCursor().End)

    def _on_done(self, result) -> None:
        self._set_running(False)
        self.progress.setValue(100)
        output = getattr(result, "output_path", "") or getattr(result, "course_dir", "")
        self.last_output = output or self.last_output
        files = getattr(result, "files", None)
        report_files = getattr(result, "report_files", None) or files or []
        word_docs = [p for p in report_files if p.lower().endswith(".docx")]
        lines = ["任务完成。"]
        if getattr(result, "output_path", ""):
            lines.append(f"课件输出：{result.output_path}")
        if getattr(result, "course_dir", ""):
            lines.append(f"知识库目录：{result.course_dir}")
        if getattr(result, "outline_points", 0):
            lines.append(f"知识点：{result.outline_points} 条")
        if getattr(result, "transcript_segments", 0):
            lines.append(
                f"录音转写：{result.transcript_segments} 段 / "
                f"{int(getattr(result, 'transcript_duration', 0) // 60)} 分钟"
            )
        if getattr(result, "exam_signals", 0):
            lines.append(f"考试信号命中：{result.exam_signals} 处（已在报告中逐条标注）")
        if word_docs:
            lines.append("Word 报告：\n" + "\n".join(f"· {os.path.basename(p)}" for p in word_docs[:6]))
        for warn in getattr(result, "warnings", []) or []:
            lines.append(f"⚠ {warn}")
        self.log_box.append("\n".join(lines))
        self.status_label.setText("完成")

    def _on_open_output(self) -> None:
        target = self.last_output
        if not target:
            candidate = self.out_edit.text().strip() or self.path_edit.text().strip()
            target = os.path.dirname(candidate) if os.path.isfile(candidate) else candidate
        if target and os.path.exists(target):
            os.startfile(target)  # noqa: S606
        else:
            QMessageBox.information(self, "没有输出", "还没有生成任何文件。")


# ---------------------------------------------------------------------- 命令行
def _parse_args(argv: list[str]) -> dict:
    options: dict = {
        "translate": [],
        "materials": [],
        "overrides": {},
        "out_dir": "",
        "report": "",
        "knowledge": "",
        "course_name": "",
        "course_dir": "",
        "explicit_order": False,
    }
    index = 0
    flags = {"--only-convert", "--explicit-order", "--no-images", "--overlay", "--no-knowledge"}
    while index < len(argv):
        token = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else ""
        if token in flags:
            if token == "--only-convert":
                options["overrides"]["only_convert"] = True
            elif token == "--explicit-order":
                options["explicit_order"] = True
            elif token == "--no-images":
                options["overrides"]["translate_images"] = False
                options["overrides"]["pdf_ocr_images"] = False
            elif token == "--overlay":
                options["overrides"]["image_mode"] = "overlay"
            elif token == "--no-knowledge":
                options["overrides"]["make_knowledge"] = False
            index += 1
            continue
        if value:
            if token == "--cli":
                options["translate"].append(value)
            elif token == "--knowledge":
                options["knowledge"] = value
            elif token == "--material":
                options["materials"].append(value)
            elif token == "--out":
                options["out_dir"] = value
            elif token == "--report":
                options["report"] = value
            elif token == "--api-key":
                options["overrides"]["api_key"] = value
            elif token == "--api-base":
                options["overrides"]["api_base"] = value
            elif token == "--model":
                options["overrides"]["model"] = value
            elif token == "--src":
                options["overrides"]["src_lang"] = value
            elif token == "--tgt":
                options["overrides"]["tgt_lang"] = value
            elif token == "--dpi":
                options["overrides"]["pdf_dpi"] = int(value)
            elif token == "--whisper":
                options["overrides"]["whisper_model"] = value
            elif token == "--course-name":
                options["course_name"] = value
            elif token == "--course-dir":
                options["course_dir"] = value
            index += 2
            continue
        index += 1
    return options


def run_cli(argv: list[str]) -> int:
    import json as _json

    options = _parse_args(argv)
    from pipeline import run_course_job, run_translate_job

    config = load_config()
    for key, value in options["overrides"].items():
        setattr(config, key, value)
    config.course_name = options["course_name"] or config.course_name
    config.course_dir = options["course_dir"] or config.course_dir
    if options["out_dir"]:
        config.output_dir = options["out_dir"]
    # 命令行里给的 --api-key / --model 等也记下来，下次不带参数也能直接用
    try:
        save_config(config)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 设置未能保存：{exc}", flush=True)

    report: dict = {"results": [], "errors": []}
    messages: list[str] = []
    log = messages.append

    destinations: dict[str, str] = {}
    for src in options["translate"]:
        entry: dict = {"input": src, "kind": "translate"}
        try:
            destination = None
            if config.output_dir:
                os.makedirs(config.output_dir, exist_ok=True)
                stem = os.path.splitext(os.path.basename(src))[0]
                tag = "Converted" if config.only_convert else config.target_language_name()
                destination = unique_output_path(os.path.join(config.output_dir, f"{stem}_{tag}.pptx"))
            result = run_translate_job(src, config, log=log, output_path=destination)
            destinations[src] = result.output_path
            entry.update(
                {
                    "ok": True,
                    "output": result.output_path,
                    "slides": result.slides,
                    "text_units": result.text_units,
                    "images_total": result.images_total,
                    "images_translated": result.images_translated,
                    "image_regions": result.image_regions,
                }
            )
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            report["errors"].append(entry["error"])
        report["results"].append(entry)

    decks: list[str] = []
    if options["knowledge"]:
        decks.append(options["knowledge"])
    elif options["translate"] and config.make_knowledge:
        # 顺带生成知识点总结；用 --no-knowledge 关闭。
        decks.append(destinations.get(options["translate"][0], options["translate"][0]))

    if decks:
        entry = {"input": decks[0], "kind": "course"}
        try:
            result = run_course_job(
                decks[0],
                options["materials"],
                config,
                log=log,
                explicit_order=options["explicit_order"],
            )
            entry.update(
                {
                    "ok": True,
                    "course_dir": result.course_dir,
                    "files": result.files,
                    "outline_points": result.outline_points,
                    "slides": result.slides,
                    "transcript_segments": result.transcript_segments,
                    "exam_signals": result.exam_signals,
                    "order_note": result.order_note,
                    "warnings": result.warnings,
                }
            )
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            report["errors"].append(entry["error"])
        report["results"].append(entry)

    report["log"] = messages[-200:]
    if options["report"]:
        with open(options["report"], "w", encoding="utf-8") as handle:
            _json.dump(report, handle, ensure_ascii=False, indent=2)
    for line in messages:
        print(line, flush=True)
    for message in report["errors"]:
        print(f"[错误] {message}", flush=True)
    return 0 if not report["errors"] else 1


def download_whisper(repo: str, target: str) -> int:
    """供子进程调用：在独立进程里下载模型（HF_ENDPOINT 由父进程注入）。"""
    from huggingface_hub import snapshot_download

    os.makedirs(target, exist_ok=True)
    print(f"[download] {repo} -> {target}", flush=True)
    snapshot_download(repo_id=repo, local_dir=target)
    print("[download] done", flush=True)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--download-whisper" in argv:
        index = argv.index("--download-whisper")
        if index + 2 < len(argv):
            return download_whisper(argv[index + 1], argv[index + 2])
        return 2
    if "--cli" in argv or "--knowledge" in argv:
        return run_cli(argv)

    screenshot_path = None
    if "--screenshot" in argv:
        index = argv.index("--screenshot")
        if index + 1 < len(argv):
            screenshot_path = argv[index + 1]

    app = QApplication(sys.argv[:1])
    app.setApplicationName("PPT课件学习助手")
    window = MainWindow()
    window.show()

    if screenshot_path:
        def _capture() -> None:
            pixmap = window.grab()
            saved = pixmap.save(screenshot_path)
            print(
                f"[screenshot] {screenshot_path} saved={saved} "
                f"size={pixmap.width()}x{pixmap.height()} title={window.windowTitle()}",
                flush=True,
            )
            app.quit()

        QTimer.singleShot(1200, _capture)

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

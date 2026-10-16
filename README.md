# PPT 课件学习助手 v2.0

在《映画电脑软件工作室全球通翻译工具（DeepSeek 版 v1.0）》的行为规格上**重建 + 扩展**的桌面工具。
默认模型：**DeepSeek 4.1 Flash**（API 模型名 `deepseek-flash`）。

原版只做一件事：把 .pptx / .pdf 里的**文字**翻成中文。本版把它扩成一条「课件 → 知识资产」的流水线：

| 能力 | v1.0 原版 | v2.0 本版 |
| --- | --- | --- |
| 幻灯片正文翻译 | ✅ | ✅ 沿用（递归组合图形 / 表格 / 备注，并发，写回首个 run） |
| **图片内文字翻译** | ❌ 完全不做 | ✅ OCR 识别插图、图表、坐标轴文字 → 擦除原文 → 中文重绘（或叠加可编辑文本框） |
| PDF → PPT | ✅ | ✅ 沿用（PyMuPDF 离线解析，重建可编辑 PPTX，背景位图保版式）+ 页面图内文字二次 OCR |
| 中文特化 | 通用 | ✅ 默认目标语言中文，图表标签用 `style="label"` 短译，避免长句塞爆图形 |
| **知识点总结文档** | ❌ | ✅ 课件 → 三级知识点大纲 + 图文详述 + 一页速览，导出 Word |
| **课堂复习报告** | ❌ | ✅ 多份（可乱序）录音/文本自动排序 → 本地转写 → 与课件对齐 → 讲课流程 + 详细内容报告 |
| **考试重点强调** | ❌ | ✅ 正则兜底扫描老师原话（时间戳 + 原句），LLM 复核，单独成篇 `exam-focus.md` |

---

## 一、理想工作流（先定流程，再落代码）

整条流程分成**三条轨**，前两条可独立使用，第三条依赖前两条的产物。

### 轨 A：课件翻译（输入 .pptx / .pdf，输出中文版课件）

1. **入口判定**：`.pptx` 直接进；`.pdf` 先走离线解析重建为可编辑 PPTX（不额外烧 token）。
2. **文本轨**：遍历每页 → 递归组合图形 / 表格单元格 / 备注 → 语言判定（`should_translate` 跳过数字、公式、已是中文的串）→ 并发调用模型 → 写回首个 run，保留原字体与版式。
3. **图片轨**（三阶段，避免把串行 OCR 泡在并发里）：
   - **① 串行 OCR**：按「图片像素尺寸」筛出值得处理的图（跳过图标、装饰、纯色底），逐张 OCR 得到文字框（四点多边形 + 角度）。
   - **② 并发翻译**：把 OCR 行按图分组交给模型，图表标签走短译模式；单张失败自动退回逐条翻译。
   - **③ 串行绘制**：估算底色 → `cv2.inpaint` 擦掉原文 → 自动选字体大小 / 竖排 / 旋转 → 重绘中文；`overlay` 模式则改为叠加可编辑文本框（保真优先、可二次编辑）。
4. **写出**：`<原名>_Chinese.pptx`（**只新增不覆盖**，同名自动 `_2`/`_3`），并产出 JSON 报告（页数 / 文本单元 / 图片数 / 翻译区域数）。

### 轨 B：知识点总结（输入课件，输出复习文档）

5. **收集**：逐页取文本 + 留存关键插图（按像素尺寸筛，不用字节数——纯色大图压缩后可能只有 1 KB）。
6. **摘要**：长课件分块并发摘要，再合并。
7. **大纲**：模型产出三级编号知识点大纲（默认前缀 `KP`），同时解析成机器可读 `outline.json`（后续出题、统计的依据）。
8. **成稿**：图文详细内容 + 一页速览 → 导出 Word（中文字体，可直接打印）。

### 轨 C：课堂复习报告（输入课件 + 1..N 份录音/文本）

9. **素材排序**：先用文件元数据（mutagen 时长/标题）+ 自然序文件名排序；再用内容让模型判定先后（**低置信度时保持原序并标注**，不硬改）。支持单份素材，也支持乱序多份。
10. **转写**：`faster-whisper` 本地转写（首次自动下载模型，支持 hf-mirror 回退），并把课件专有名词作为 `initial_prompt` 提示，减少人名/术语听错。
11. **对齐**：转写稿 ↔ 课件页对齐（哪儿讲到了哪一页）。
12. **考试信号扫描**：**正则先扫一遍**（中英文关键词），保证「一条都不放过」；再交 LLM 判定真伪（例如把 "final" 这种假阳性标成「待老师确认」）。
13. **报告**：讲课流程（时间轴）+ 详细内容 + 笔记/要点总结 + 学科补充（带出处）+ 知识图谱 → 导出 Word。
14. **台账**：所有产物写入固定目录结构，附 `COURSE.md` 台账，可跨课时长期复用。

### 贯穿全程的 5 条铁律（来自 `lecture-knowledge-pipeline` skill）

1. **不静默改写**——模型不许悄悄改老师原话，改动要留痕。
2. **一切可追溯**——每条结论都带来源（课件页号 / 录音时间戳）。
3. **补充内容分开放**——学科延伸单独成区，不与课堂内容混写。
4. **答案与题目分离**——题库中答案、解析与题干分开。
5. **不出重复题**——同义题去重。

---

## 二、集成 / 复用的开源项目

| 组件 | 用途 | Star | 许可证 |
| --- | --- | --- | --- |
| [RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR)（`rapidocr-onnxruntime`） | 本地 OCR，识别图内文字，离线、无 token 消耗 | 8.0k | Apache-2.0 |
| [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 本地语音转写，CTranslate2 加速 | 25.7k | MIT |
| [scanny/python-pptx](https://github.com/scanny/python-pptx) | 读写 .pptx（含图片替换、组合图形递归） | 3.6k | MIT |
| [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF) | PDF 离线解析与渲染 | 10.8k | **AGPL-3.0**（注意：分发需遵守 AGPL） |
| [OpenCV](https://github.com/opencv/opencv-python)（headless） | 图像擦除（inpaint）、几何变换 | — | Apache-2.0 |
| python-docx / mutagen / Pillow | Word 导出 / 音频元数据 / 图像处理 | — | MIT / GPL-2.0 / HPND |

> 说明：OCR 还对比过 PaddleOCR（90.7k★），但它依赖体积与部署复杂度高得多；RapidOCR 同为国产团队、ONNX 推理、离线开箱即用，更适合打包进单文件 exe。
> 也曾检索过现成的「PDF→PPTX」开源转换器（如 `JuniverseCoder/MinerU2PPT`、`yeokm1/docs-to-pdf-converter`），但都不如本版直接复用原项目自带的 PyMuPDF 解析器可控，故未引入。

---

## 三、关键工程约束（踩过的坑，改代码前先看）

1. **`onnxruntime` 必须在 `PyQt5` 之前导入**。实测先加载 Qt5 再 `import onnxruntime` 会触发访问违例 `0xC0000005` 硬崩溃（不是异常，进程直接没）。`main.py` 顶部已把 `onnxruntime` / `cv2` 预导入提前。
2. **`deepseek-flash` 是推理模型**：`max_tokens` 同时覆盖「思考」与「正文」。给 `max_tokens=6000` 时思考会把额度吃光 → `finish_reason=length` 且 `content` 为空。客户端默认**不传 `max_tokens`**；若返回空正文会自动去掉上限重试一次。
3. **构建必须用 ASCII 路径**。PyQt5（Qt 5）在 Windows 上处理不了非 ASCII 路径（Qt 会把插件目录报成 `C:/????/...`）。源码可以放在中文目录，但**构建 venv 必须在 ASCII 路径**（本仓库用 `C:\pptx-build`）；产物是 onefile，运行时解包到 `%TEMP%`，所以放到中文目录也能跑。
4. **PyInstaller 必须带 `--windowed`**。控制台子系统下 `print` 到已关闭的 stdout 会直接崩。
5. **`opencv` 锁 `<5`**。opencv 5.0 与 `rapidocr-onnxruntime` 不兼容（已固定 `opencv-python-headless==4.14.*`）。
6. **`faster-whisper` 的 `device="auto"` 会选到 CUDA 然后崩**（缺 `cublas64_12.dll`）。已加 `_selfcheck()`：用一段静音先跑一次推理，失败自动回退 CPU + int8。
7. **输出永不覆盖**。同名文件自动追加 `_2` / `_3`（源于一次真实事故）。
8. **图片管线传的是 `io.BytesIO`**：`image_translator.prepare_image` 必须接受文件对象（内部走 `PIL.Image.open`）。
   它曾经只认「路径 / PIL.Image / ndarray」，于是 `--cli`/GUI 的整份课件图片被**静默跳过**
   （日志只留一句「没有发现需要翻译的文字」），而单元测试因为只喂 PIL 图而全绿。现已修复并补了回归测试。
9. **翻译轨与知识点轨的联动受 `make_knowledge` 控制**：默认翻译完顺带生成知识点总结，
   命令行用 `--no-knowledge` 关闭，避免脚本化场景被额外扣费。

---

## 四、安装与运行

源码模式（开发/调试）：

```powershell
# 需要一个 ASCII 路径的解释器；本机已备好
C:\pptx-build\venv\Scripts\python.exe -X utf8 src\main.py
```

图形界面三个页签：

- **① 课件翻译**：选 .pptx / .pdf → 设置 API Key、模型、源/目标语言 → 开始。
- **② 知识点总结**：选课件（可多个）→ 课程名 / 输出目录 → 生成大纲 + 图文详述 + Word。
- **③ 课堂复习报告**：选课件 + 拖入 1..N 份录音/文本 → 选 Whisper 模型 → 生成课堂流程与详细报告。

命令行模式（便于脚本化）：

```powershell
$py = 'C:\pptx-build\venv\Scripts\python.exe'

# 翻译单个课件（含图内文字）
& $py -X utf8 src\main.py --cli "Week8_TES.pptx" --out C:\out --report C:\out\report.json

# 知识点总结
& $py -X utf8 src\main.py --knowledge "Week8_TES.pptx" --out C:\out --course-name "成瘾行为导论"

# 课件 + 乱序录音 → 课堂复习报告
& $py -X utf8 src\main.py --knowledge "Week8_TES.pptx" `
    --material part3.wav --material part1.wav --material part2.wav `
    --whisper small --out C:\out --course-name "成瘾行为导论"
```

常用开关：

- `--no-knowledge` 翻译后不生成知识点总结（默认翻译完会顺带生成）；`--no-images` 关闭图内文字翻译；`--overlay` 改用叠加可编辑文本框；`--only-convert` 仅转换不翻译。
- `--explicit-order` 关闭「按内容重排」，严格按给出的顺序。
- `--model` / `--src` / `--tgt` / `--dpi` / `--whisper` / `--course-dir` 覆盖配置。
- `--screenshot <路径>` 启动后截图（无头自检用）；`--download-whisper <模型>` 预下载转写模型。

## 五、设置怎么保存（API Key 只填一次）

配置统一写在**用户目录**，跟 exe 放哪儿无关：

```
%APPDATA%\PPT课件学习助手\translator_config.json
```

（本机即 `C:\Users\<用户名>\AppData\Roaming\PPT课件学习助手\translator_config.json`）

- 界面上**粘贴一次 API Key 就会自动保存**，1.2 秒后落盘，不需要先跑一次任务；关窗时再兜底保存一次。
- 下次打开自动填入，`模型 / 接口地址 / 语言 / 并发 / 图片翻译 / Whisper` 等一并恢复。
- 勾选「记住 Key」（默认开）：Key 随设置保存。取消勾选则保存时把 Key 从文件里抹掉，只留其他设置。
- 首次运行会自动**迁移**老位置（exe 紧邻目录、当前工作目录）里的 `translator_config.json` ——
  多个旧文件同时存在时取**最近修改过**的那个，并只复制不删原件。
- 命令行加 `--api-key` 等其他参数也会被记下来，之后裸跑 `--cli` 就能直接用。
- 写盘采用「先写临时文件再原子替换」，中途被杀进程不会留下半个损坏的 json。

> 为什么不再放 exe 旁边：老版本就是这样，用户一旦把 exe 复制到桌面/别的文件夹，
> 设置就留在原处，于是「每次都要重新输入 API Key」。放进用户目录后，
> exe 改名、移动、复制多份都不影响。

> 注意：Key 以**明文**保存在该 json 里（与旧版行为一致），介意的话取消「记住 Key」，
> 或对该文件做访问控制。

## 六、构建 exe

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

脚本会：跑测试 → 在 ASCII 路径建 venv、装依赖 → 校验 Qt 插件路径 → PyInstaller onefile + `--windowed` 打包 → 复制到 `work\dist\` 并打印 SHA256 与 PE 子系统。

常用参数：`-SkipTests`（跳过测试）、`-Onedir`（产出文件夹版，启动快，但要整目录分发）、`-Name xxx`（改名）、`-AsciiWork D:\build`（换构建目录）。

**产物**：`work\dist\ppt-pdf-translator.exe`（约 223 MB，GUI 子系统）。可直接改名，例如 `ppt-课件学习助手_v2.0.exe`。
onefile 每次启动都要把自己解包到 `%TEMP%`（首次约 5–10 秒），介意启动速度就用 `-Onedir`。

构建脚本里有三处「不这么写就炸」的硬约束，动它之前先读脚本里的注释：

1. **`build.ps1` 必须保持纯 ASCII**。Windows PowerShell 5.1 对没有 BOM 的 .ps1 按系统代码页解析，
   中文注释会被解成乱码并**把后面那一行代码吞掉**，报出来的却是莫名其妙的
   `Cannot bind argument to parameter 'Path' because it is null`。
2. **PyInstaller 的隔离子进程会踩 PyQt5 → onnxruntime 的崩溃坑**（见上文「关键工程约束」第 1 条）。
   脚本在构建期间临时往 site-packages 放一个 `sitecustomize.py`，让子进程一开始就导入 onnxruntime，
   构建一结束立即还原；同理**不要加 `--collect-all onnxruntime`**：它会去 import
   `onnxruntime.quantization`，该子模块依赖没装的 `onnx`，会把隔离子进程直接打死。
3. **MSVC 运行库冲突**。PyQt5 的 wheel 自带 VS2017 运行库（14.26），而
   onnxruntime / opencv / ctranslate2 是 VS2022（14.4x）编的；两者打进同一个 exe 后**先加载谁谁赢**，
   结果新版组件在启动瞬间以 `0xC0000005` 崩掉。脚本在构建期间用 `System32` 里的新运行库
   临时替换 `PyQt5\Qt5\bin` 下的 6 个 DLL，构建结束还原。

## 七、目录结构

```
src/
  main.py               PyQt5 界面 + 命令行入口（顶部预导入 onnxruntime/cv2）
  pipeline.py           任务编排：翻译轨 / 课程知识轨 / Word 导出
  translator_config.py  配置（兼容 v1.0 旧 json，自动迁移旧模型名）
  deepseek_client.py    DeepSeek 客户端（批译、短译、JSON、模型回退、空正文重试）
  ocr_engine.py         RapidOCR 封装（四点框 + 角度）
  image_translator.py   图内文字：擦除/重绘/叠加、字体自适应、竖排、旋转
  pptx_images.py        python-pptx 图片定位（组合图形变换）、替换、叠加
  pptx_translator.py    幻灯片文本翻译
  pdf_parser.py / pdf_builder.py   PDF → 可编辑 PPTX
  audio_transcriber.py  faster-whisper 封装（模型下载 + 回退）
  transcript_io.py      多格式读取 + 多素材排序（元数据/文件名/内容）
  course_knowledge.py   10 阶段课程知识流水线
  llm_prompts.py        全部提示词（含 5 条铁律）
  markdown_doc.py       Markdown → Word
tests/                  79 个单元测试（含假 OCR 引擎、假 HTTP 服务与离屏 Qt 持久化用例）
build.ps1               单文件 exe 构建脚本
docs/WORKFLOW.md        工作流与阶段目录详解
```

## 八、已知限制

- **API Key 明文存于 `%APPDATA%\PPT课件学习助手\translator_config.json`**（可用「记住 Key」开关关闭）。
- **首次使用语音转写需联网下载 Whisper 模型**（本机缓存在 `%LOCALAPPDATA%\PPT课件学习助手\models`），之后离线可用。
- **`deepseek-flash` 是推理模型**：单次调用会先消耗思考 token，长课件批量翻译比普通对话模型贵一些；可用 `--model deepseek-v4-flash` 走旧别名。
- **扫描版 PDF 只做页级 OCR**：PDF 若整页是扫描图，会按页渲染后叠加文本框，不会还原成矢量版式。
- 图内文字翻译对**过度艺术字 / 严重倾斜 / 极低分辨率**的插图效果有限（`overlay` 模式更稳）。
- 考试重点判定会出现假阳性（如 "final"），已标注为「待老师确认」，需人工过一眼。
- 本机没有 LibreOffice / MS Office，无法把翻译后的 .pptx 渲染成图做整页视觉比对，改用裁剪级像素校验。
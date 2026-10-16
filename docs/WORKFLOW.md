# 工作流详解

本文把「理想工作流」落到代码与目录：谁在什么时候产出哪个文件，依据来自哪里。

## 0. 总览

```
                 ┌─────────────── 轨 A：课件翻译 ───────────────┐
  .pptx / .pdf ──┤ 文本轨（并发翻译） + 图片轨（OCR→擦除→重绘）  ├─→ <名>_Chinese.pptx
                 └───────────────────────────────────────────────┘
                                        │
                                        ▼
                 ┌─────────────── 轨 B：知识点总结 ─────────────┐
   课件（同上） ─┤ 收集 → 分块摘要 → 三级大纲 → 图文详述/速览    ├─→ *_知识点总结.docx
                 └───────────────────────────────────────────────┘
                                        │
                                        ▼
                 ┌─────────────── 轨 C：课堂复习报告 ───────────┐
   1..N 份录音/  │ 排序 → 转写 → 与课件对齐 → 考试信号 → 报告    ├─→ *_课堂复习报告.docx
   文本（乱序）─┤                                              │
                 └───────────────────────────────────────────────┘
```

依赖关系：轨 B 只用课件；轨 C 用「课件 + 素材」，并且会复用轨 B 已生成的大纲（若同目录已存在）。
三条轨可以只跑其中一条。

## 1. 轨 A：课件翻译

| 步骤 | 代码 | 说明 |
| --- | --- | --- |
| 入口判定 | `pipeline.run_translate_job` | `.pdf` 先经 `pdf_parser` 离线解析重建 PPTX |
| 文本轨 | `pptx_translator` + `deepseek_client.translate_batch` | 递归组合图形/表格/备注；语言判定跳过无需翻译的串 |
| 图片轨① | `pptx_images.collect_pictures` + `ocr_engine` | **串行** OCR；按像素尺寸筛选（跳过图标/装饰） |
| 图片轨② | `deepseek_client.translate_batch(style="label")` | **并发**翻译，单图失败退回逐条 |
| 图片轨③ | `image_translator` | `cv2.inpaint` 擦除 → 字体自适应/竖排/旋转 → 重绘或叠加文本框 |
| 写出 | `pipeline.unique_output_path` | 永不覆盖；返回 `JobResult`（页数/文本单元/图片数/区域数） |

`image_mode`：`inpaint`（重绘，观感接近原图）或 `overlay`（叠加可编辑文本框，保真且可改）。

## 2. 轨 B：知识点总结

对应 skill 的阶段目录（`06_outline` / `07_notes`），产物仍是 Markdown 中间件，最后转 Word：

1. `02_slides/slides.md` + 关键插图 —— 逐页文本与留档图；
2. `02_slides/digest.md` —— 长课件分块并发摘要后合并；
3. `06_outline/outline.md` + `outline.json` —— 三级编号知识点（默认前缀 `KP`），json 供后续统计与出题；
4. `07_notes/notes.md`（图文详述）、`07_notes/summary.md`（一页速览）；
5. 导出 `*_知识点总结.docx`。

## 3. 轨 C：课堂复习报告

1. **排序** `transcript_io.order_sources`：mutagen 元数据（时长/标题）→ 自然序文件名 → 必要时交模型按内容判定；**低置信度保持原序并标注**，不硬改。
   产物：`01_transcript/order.md`。
2. **转写** `audio_transcriber`：`faster-whisper` 本地转写，课件术语作 `initial_prompt`；
   产物：`01_transcript/transcript.txt` + 每份素材的 `partial-*.txt`。
3. **对齐**：`03_align/align.md` —— 转写稿 ↔ 课件页。
4. **考试信号** `course_knowledge.scan_exam_signals`：**正则先扫**（中英文关键词表，保证不漏）→ LLM 复核真伪 → `07_notes/exam-focus.md`（原话 + 时间戳 + 课件页号 + 置信度）。
5. **报告**：
   - `07_notes/lecture-flow.md` —— 讲课流程（时间轴）
   - `07_notes/notes.md` —— 详细内容
   - `04_corrections/corrections.md` —— 纠错清单（逐条给依据与置信度）
   - `08_graph/graph.md` —— 知识图谱
6. **台账** `COURSE.md` —— 记录本次用了哪些输入、产出了什么，可跨课时复用。

## 4. 课程目录结构（一次运行后）

```
<输出目录>/
  00_source/         本次课的原始材料（可留档）
  01_transcript/     转写稿、排序说明、分片
  02_slides/         逐页文本 + 关键插图
  03_align/          页与讲述对齐
  04_corrections/    纠错清单
  06_outline/        知识点大纲（md + json）
  07_notes/          笔记、总结、讲课流程、考试重点
  08_graph/          知识图谱
  10_kb/             课程知识库（大纲/教材/文献，长期沉淀）
  COURSE.md          台账
  *_知识点总结.docx
  *_课堂复习报告.docx
```

## 5. 错误与降级策略

- 模型返回空正文（推理模型吃光 token）→ 去掉 `max_tokens` 重试一次。
- 主模型不可用 → 按 `deepseek-flash → deepseek-v4-flash → deepseek-chat` 回退。
- Whisper 选到 CUDA 但缺 DLL → 静音自检失败 → 回退 CPU/int8。
- 单张图翻译失败 → 只记为失败，不中断整份课件。
- 输出同名 → 追加 `_2` / `_3`，永不覆盖用户既有文件。
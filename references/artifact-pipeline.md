# 产物流水线：每条赛道真实落盘什么

## 为什么有这份文档

评审要**打开文件看**。只在 `result.json` 里描述"应该有一个 SVG"不算交付。
本文逐条说明每个赛道在 `/app/output` 下**实际写出哪些文件**、每个文件里
有什么、以及文件记录如何回挂到 `result.json` 的契约键。

所有文件记录都带 `sha256` 与字节数（`specialize._file_record()` /
`artifacts._file_record()` 生成），评审可以自行校验。

## reading —— 逐字段抽取

| 文件 | 写在哪 | 内容 |
|---|---|---|
| `result.json` | `/app/output` | 契约齐全的结果；`answers[]` 与 `target_fields[]` 承载逐字段结果 |
| `run-review.md` | `/app/output` | 证据索引 + 不确定性清单 |
| `anomaly-report.json` | `/app/output` | **异常自报**：各状态计数、逐条异常与原因、正文与表格取值冲突、每个取值的表格行列定位 |

输入目录若提供 `target-fields.json`，`specialize.extract_target_fields()`
会**逐 `field_id`** 抽取，每个字段给出：

- `value` / `unit` / `statistic` —— 标量、单位、统计量（单位做词形匹配，
  复合单位如 `mL/min` 可识别）
- `condition` / `conditions` / `comparator` —— 实验条件与对照
- `paragraph_id` / `quote` —— 精确到段落与原文短引
- `field_status` —— `reported` / `not_reported` / `source_conflict` / `extraction_failed`
- `answer_status` —— `answered` / `partial` / `insufficient_evidence` / `input_error`

> 规则：**猜出来的值不是抽取，没提到的字段不是 0**。材料没写就写
> `not_reported`；材料里明确说"未报告"的同样写 `not_reported`。
> 逐字引文匹配本身**不**证明语义正确，所以数值只在**主词所在的子句**内取值。

## writing —— 可编辑稿件 + 改动记录

| 文件 | 写在哪 | 内容 |
|---|---|---|
| `manuscript.md` | `/app/output` | 真实可编辑稿件；章节结构来自材料自身的标题行 |
| `change-log.md` | `/app/output` | 逐条 before/after + 锚点守恒检查（哪些数字/引文编号被改动） |
| `evidence-map.json` | `/app/output` | 章节 → 段落 / 锚点映射；另含 `anchor_locations[]`（**逐条**锚点：类型 / 取值 / 来源文件 / 行号 / 段落 id / 原文短引）与 `key_claims[]`（带锚点的关键论断原句） |
| `edit-targets.json` | `/app/output` | 输入提供 `organizer-edit-workorder.json` 时，逐条编辑目标回执 |
| `qa-report.json` | `/app/output` | 逐节锚点守恒统计、edit-target 状态、待人工确认清单 |
| `format-spec-checklist.json` | `/app/output` | **格式规格逐项核对清单**：栏数、字号、行距、页边距、标题层级、图表题注、参考文献样式、导出格式、页面尺寸、字体、页眉页脚、目录页码、图表编号等。每项给出 `status`（`specified` / `conflict` / `not_specified_in_materials`）、取值、出处文件、行号与原文短引 |
| `anomaly-report.json` | `/app/output` | **材料缺口与异常自报**：格式规格缺失与冲突、锚点守恒异常、空章节、待人工确认项 |
| `result.json` | `/app/output` | 契约齐全的结果 + 改动回执 |

> writing 侧的规则：
> 1. **逐条可回溯** —— 只给"计数"无法支撑核对，所以每个数字/术语/引用/图表/
>    关键论断都单独给出来源文件、行号、段落 id 与原文短引。
> 2. **认得准，不猜** —— 格式规格只登记材料里真的出现过的取值；材料没写的
>    维度记为 `not_specified_in_materials`，不用行业惯例填充。
> 3. **缺口要说出来** —— 材料缺口、规格冲突、待人工确认项写进
>    `anomaly-report.json`，并在 `result.json.uncertainty` 里同步登记。

## graphics —— 可编辑图形 + 渲染预览

| 文件 | 写在哪 | 内容 |
|---|---|---|
| `figure.svg` | `/app/output` | 分层矢量源：实体节点、连接、步骤；每个节点带 `data-source-paragraph` / `data-quote` |
| `figure.png` | `/app/output` | 由 `figure.svg` **真实渲染**的预览图 |
| `figure.pdf` | `/app/output` | 由 `figure.svg` 导出的 PDF（矢量优先；无矢量转换器时如实标注为位图兜底） |
| `figure-provenance.json` | `/app/output` | 元素 → 原文段落映射与修改记录 |
| `figure-export-checks.json` | `/app/output` | **导出前合规检查**：目标格式、画布尺寸、实测分辨率、字体嵌入与回退、色彩空间、线宽、分层、数据一致性、人工确认清单 |
| `result.json` | `/app/output` | 契约齐全的结果 + 结构中间表示（entities / relations / steps / data_mapping） |

渲染器优先级：ImageMagick (`magick`) → CairoSVG → Pillow → 纯 Python 光栅化。
**渲染前会先删除同名旧文件**，避免上一轮的残留 PNG 被误判为"本次渲染成功"。
PDF 导出链：CairoSVG → rsvg-convert → Inkscape → ImageMagick → headless Chromium；
全部不可用时用标准库写一个最小合法 PDF 并**明确标注 `is_vector: false`**。

## slides —— 可编辑 PPTX + 逐页预览

| 文件 | 写在哪 | 内容 |
|---|---|---|
| `deck.pptx` | `/app/output` | 真实 OOXML 演示文稿，可用 PowerPoint / python-pptx 打开 |
| `slides/slide-NN.svg` | `/app/output/slides/` | 每页矢量源 |
| `slides/slide-NN.png` | `/app/output/slides/` | 每页渲染预览 |
| `speaker-notes.md` | `/app/output` | 讲解备注，与页面文字**分离** |
| `slide-evidence-map.json` | `/app/output` | 第 N 页 ← 段落 id + 原文短引 |
| `slide-export-checks.json` | `/app/output` | **导出前合规检查**：页数与结构、要点覆盖、讲稿是否混入页面文字、字体嵌入与中文回退、预览分辨率、PDF 导出状态、人工确认清单 |
| `result.json` | `/app/output` | 契约齐全的结果 + 逐页大纲 |

PPTX 由标准库 `zipfile` + XML 直接写出，不依赖 `python-pptx`。

## 通用规则

1. **先写骨架**：入口一进来就落一份契约齐全的 `result.json`，再填内容。
   Harbor 的预算是 900 秒 / 64 次模型请求，单个案例彻底失败就吃掉约 33 分
   （总分是 3 个案例的算术平均）。
2. **产出必须可核验**：文件带 sha256；SVG 节点带来源段落属性；PPTX 页有
   evidence map；稿件改动有 before/after。
3. **没产出就说没产出**：渲染器不可用时 `exports` 里该项 `status` 写
   `not_produced` 并说明原因，不谎称已渲染。

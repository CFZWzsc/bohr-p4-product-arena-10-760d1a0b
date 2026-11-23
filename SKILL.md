---
name: bohr-reading-knowledge-graph
description: 当需要从科研论文抽取实体与关系、跨篇对齐并构建可查询证据图谱时使用。本题范围：文献知识图谱构建（实体识别、关系抽取、别名归一、来源绑定、图谱查询）。产出 result.json，包含运行时 output_contract 要求的全部字段，每个节点和每条边都绑定论文 source_id、paragraph_id 与原文短引，无法确定或冲突的关系如实写入 uncertainty。
---

# 文献知识图谱构建：科研阅读：回答问题，也给出依据

## 用途

本题属于 **literature-reading（reading）** 赛道，题目是「文献知识图谱构建」。目标能力：从一批科研论文中抽取**真实出现的**实体与关系，绑定可回查的证据，支持至少 3 个真实查询，并正确处理跨篇别名与不可合并的冲突对象。

重点覆盖四个问题（题面原文）：

- **抽得准**：实体对应论文里真实出现的科研对象（研究者、材料、方法、任务、指标、结论），不是模型凭常识补全的概念。
- **连得对**：关系类型、方向和适用条件与原文一致，没有把共现、综述转述或推测当成直接证据。
- **对得齐**：同一对象在不同论文中的别名、缩写、大小写和同名冲突被正确处理，既不产生重复节点，也不把不同对象错误合并。
- **查得到、回得去**：图谱能回答真实查询，且每个节点和每条边都能回到论文的具体位置和原文片段。

可聚焦实体识别、实体对齐、关系抽取、来源绑定、图谱查询或增量更新中的一种，形成能够真实运行、独立验证的最小成果，不要求完整图谱系统。

本 Skill 在 DeepSeek Harness 中被加载到 `/skills/weekly-skill`。
Harness 会在 3 个固定科研案例上分别调用它；每个案例的输入是
`/input/case.json`（含 `task_instruction` 与 `output_contract`）、`task.md`
与同目录下的科研材料（可能包含 `corpus-manifest.json`、`material.json` 索引、
`primary-material.json` / `secondary-material.json` 等多篇原文）。

## 任务要求（来自题面）

1. 先识别输入材料与问题边界：读 `corpus-manifest.json`（DOI、作者、来源哈希、许可证）与 `material.json`（正文段落索引），再读各篇原文；**根的 DOI/title/authors 只属于第一篇，不得套到第二篇**。
2. 抽取实体：研究者、材料、方法、任务、指标、结论；保留原文位置与表述原文，区分作者自述与他人转述。
3. 抽取关系：只建立有证据支持的关系，明确类型（例如使用、比较、优于、支持、矛盾）、方向与适用条件；不得把共现或推测写成直接关系。
4. 跨篇对齐：处理别名、缩写、大小写、全称/简称、同名实体，给出合并依据；**不能强迫每个别名都合并，也不能把不同论文的条件、样本和数值结果合成一个结论**。相同通用方法/材料术语不代表两篇使用了相同物理样品、模型实例、数据集或实验条件。
5. 来源绑定：每个实质节点/边使用**独立论文的** `source_id`、`paragraph_id` 及原文短引；位置是组织方 JATS 段落 ID，**不是实际 PDF 页码**，不得把自建页面 1 称作原 PDF 页。
6. 图谱查询：给出至少 3 个实际执行的查询，展示结果子图、支撑证据以及无法确定的关系。
7. 输入未提供图像像素、补充附件、底层临床或实验数据时，不推断其中数值；未报告和源内冲突明确保留。

本题可预知的契约键（来自题面「Skill 运行后的输出要求」与输入修订，另有部分键按**同赛道兄弟题的公开接口推断**；**实际以运行时 `/input/case.json` 为准**，下表仅供预热）：

- `case_id`
- `case_identifiers`
- `entities`
- `edges`
- `aliases`
- `queries`
- `claims`
- `evidence`
- `coverage`
- `status`
- `uncertainties`

题面没有规定固定的 JSON 键名；因此**不要硬编码**，按运行时契约键名语义填充。

## 交付物

result.json（契约齐全：节点、边、别名决策、查询结果、来源、证据、不确定性）、run-review.md（证据索引 + 合并/冲突记录）、anomaly-report.json（异常自报，输入提供 `target-fields.json` 时）。

交付物的**内容契约**由运行时 `output_contract` 决定（见下）；下面「交付物落盘
要求」给出的是必须真实存在的**文件**。

## 执行方式

入口脚本：`scripts/sk_entry.py`。它会读取 `/input`，产出 `/app/output/result.json`。

```bash
# 在案例环境中执行（Harbor 会自动调用；手动复现同一条命令）
python /skills/weekly-skill/scripts/sk_entry.py \
  --input /input --work /app/work --out /app/output
```

执行结束后必须存在且可解析：

- `/app/output/result.json` —— 契约字段齐全的最终结果
- `/app/output/run-review.md` —— 人类可读的证据索引与不确定性清单（复核用）

### 两阶段工作流（重要：脚本只做取证，判断由你做）

脚本是**确定性**的：它读材料、切段落、建证据索引、按契约键名填字段。它**不做**
领域判断 —— 哪些词是实体、关系方向和条件是什么、哪些别名可以合并、查询该怎么
回答，这些必须由你（执行 Agent）来做。所以本 Skill 的完整用法是**跑两次**：

```bash
# 阶段 A：脚本取证，产出结果骨架 + 任务单
python /skills/weekly-skill/scripts/sk_entry.py \
  --input /input --work /app/work --out /app/output

# 阶段 B：你读 /app/work/agent_tasks.json，写 /app/work/agent_judgement.json

# 阶段 C：脚本再跑一次，把你的判断校验后合并进 result.json
python /skills/weekly-skill/scripts/sk_entry.py \
  --input /input --work /app/work --out /app/output
```

> **重跑前先移走阶段 A 的 `result.json`**（例如
> `mv /app/output/result.json /app/work/phase-a-result.json`）。脚本会优先采纳
> 旧 `result.json` 里已有的契约键；若不先移走，你通过 `contract_fields` 写入的
> 领域内容会被阶段 A 的骨架覆盖。

**阶段 A 之后**，`/app/work/agent_tasks.json` 会告诉你：

| 字段 | 内容 |
|---|---|
| `contract_keys` / `contract_semantics` | 本案例的契约键，以及每个键该装什么 |
| `evidence_anchors` | 全部段落 id + 原文短引（你的证据只能从这里取） |
| `draft_claims` / `open_questions` | 脚本机械抽到的候选论断与待答问题 |
| `what_to_judge` | 本题需要你判断的四件事 |
| `how_to_write_the_judgement` | 判断文件的结构与规则 |

**阶段 B** —— 有两种写法，**任选其一**：

**(a) 写判断文件**（推荐，校验最严格）—— `/app/work/agent_judgement.json`：

```json
{
  "case_id": "必须与 case.json 一致",
  "claims": [
    {"text": "你判断为结论的陈述",
     "evidence": [{"paragraph_id": "P002", "quote": "原文里逐字出现的片段"}]}
  ],
  "answers": [
    {"question": "被问的问题", "answer": "你的回答",
     "evidence": [{"paragraph_id": "P002", "quote": "原文里逐字出现的片段"}]}
  ],
  "deliverable": "本题最终交付物的正文",
  "contract_fields": {"契约键名": "该键的内容（可选，直接填契约键）"},
  "uncertainties": [
    {"kind": "machine_readable_category", "item": "具体对象",
     "detail": "客观描述", "resolution": "你做了什么或为何无法继续"}
  ]
}
```

**(b) 直接写 `/app/output/result.json`** —— 也支持，脚本会把你写进去的契约键
**取出来**（先留底，再合并），同样过引文校验。你的 `result.json` 不会被覆盖。

> 两种写法都会被采纳；**不要**两个都不做 —— 那就只剩机械抽取，`status` 停在 `partial`。

**阶段 C 的校验闸门（脚本强制执行）**：

- 每条 `quote` 必须能在材料原文里**逐字找回**（允许空白差异）—— 找不到就**退回**，
  并把退回理由写进 `uncertainty`；
- 每条 claim / answer 必须至少有一个证据锚点 —— 没有锚点的条目被退回；
- `contract_fields` 的值不能是 `null` 或空容器 —— 空值被退回；
- 被退回的条目**不会**进入最终结果，脚本保留自己的确定性抽取作为兜底。

> 判断可以自由，**证据不可以自由**。这条闸门是本 Skill 的可信度基础。

只有完成阶段 B，脚本才会把 `status` 从 `partial` 升到 `answered`；否则结果里的
`uncertainty` 会保留一条 `judgement_pending`，如实说明"只做了机械抽取，尚未判断"。

### 脚本职责

| 脚本 | 职责 |
|---|---|
| `scripts/sk_entry.py` | 入口：定位输入、发现材料、执行 `topic.run()`、装配 `result.json`、自检并写复核产物 |
| `scripts/sk_base.py` | 公共库：契约解析、材料发现、证据构造、结果装配、自检工具 |
| `scripts/topic.py` | 题目逻辑：读材料 → 建证据索引 → 按契约键名语义产出字段 → 校验并合并你的判断 |
| `scripts/specialize.py` | reading / writing 引擎：逐 `field_id` 抽取（四态状态）；可编辑稿件 + 改动记录 + 来源映射 |
| `scripts/artifacts.py` | graphics 引擎：实体—连接—步骤 IR → 分层 SVG → 渲染 PNG + 来源记录 |
| `scripts/pptxgen.py` | slides 引擎：stdlib-only 真实 PPTX + 逐页 SVG/PNG + 独立讲稿备注 |

**这些引擎会真的写文件。** 每条赛道具体落盘哪些文件、每个文件里有什么、
如何回挂到契约键，见 `references/artifact-pipeline.md`。

### 运行时契约是唯一权威

`output_contract` **只在运行时可见**（`/input/case.json`）。
不要硬编码任何字段名，也不要凭题面示例臆造字段：

1. 读取 `/input/case.json` 的 `output_contract`；
2. 对每个契约键，按**键名语义**判断它应该承载什么（证据 / 结论 / 不确定性 /
   交付物 / 覆盖度 / 变更记录 …）；
3. 产出对应内容，**不允许**留下 `null` 或"未产出"占位；
4. 若该键确实无法从材料得出，填入结构化的"无法判断"条目（带 `kind`、`item`、
   `detail`、`resolution`），而不是留空。

## 交付物落盘要求

除 `result.json` 外，**可编辑的交付物源文件与其渲染预览必须真实存在**：

| 赛道 | 必须落到 `/app/output` 的文件 |
|---|---|
| reading | `result.json`（契约齐全的结构化结果） |
| reading | `run-review.md`（证据索引 + 不确定性清单） |
| reading | `answers[]` / `target_fields[]`：若输入提供 `target-fields.json`，则逐 `field_id` 一项，状态取 `reported` / `not_reported` / `source_conflict` / `extraction_failed` |
| reading | `anomaly-report.json` —— 异常自报：各状态计数、逐条异常原因、正文与表格取值冲突、每个取值的表格行列定位 |

平台提供的 `office-renderer` / `pdf-render-tool` / `latex-compiler` 可用来渲染预览。
**只有规格描述、没有真实文件，等于没交付** —— 评审要打开文件看。

### 导出前合规检查（pre-export compliance）

导出件落盘后，本 Skill 会**自动核对**并在 `*-export-checks.json` 里留档，
让评审不必逐个打开文件猜：

| 检查维度 | 记什么 |
|---|---|
| 目标格式 | 期望的 SVG / PNG / PDF 是否齐全、各自字节数、是否是**真矢量** |
| 画布与分辨率 | 像素尺寸 → pt → mm 换算，实测 dpi，目标 dpi |
| 字体 | 字体清单、嵌入情况、中文回退策略、是否存在方框/缺字风险 |
| 色彩空间 | 色彩空间标注（sRGB 等）与是否有嵌入配置 |
| 线宽 / 分层 | 线宽是否过细、图层是否可拆、图层清单 |
| 数据一致性 | 图上出现的数值与源数据表的比对结果 |
| 讲稿隔离 | 备注文字是否混进页面文字（逐页最大重叠片段） |
| 人工确认 | 需人眼确认的项一律标 `pending_human_review`，**不代签** |

失败的检查项会**原样保留为 fail**，不会被改写成通过；不可用的工具（如运行时
没有矢量转换器或 LibreOffice）也如实记为 `unavailable`，不做假成功。

## 质量红线

- **实体必须真实出现**：node 的 name 与原文表述一致，保留 `source_id`、`paragraph_id`、`quote`；不凭常识补全概念。
- **关系必须有直接证据**：edge 给出来源句、类型、方向与适用条件；共现、综述转述、推测不得写成直接关系。
- **跨篇纪律**：每篇论文用独立 `source_id`；根 DOI/title/authors 不套到第二篇；不同论文的条件、样本和数值结果不合并成一个结论。
- **别名决策留痕**：每个合并给出依据（全称/缩写/大小写/上下文）；无法确认的别名保持分开并写入 `aliases` 或 `uncertainties`，不强迫合并。
- **同名不合并**：同名但物理样品、模型实例、数据集或实验条件不同的对象必须分开，并记录"不得合并"的理由。
- **查询可执行**：至少 3 个真实查询，给结果子图、支撑证据和无法确定的关系；无结果的查询如实说明。
- **位置口径**：使用组织方 JATS 段落 ID；没有 PDF 页码时不编造页码。
- 每条 claim 都有 paragraph_id + 原文 quote。

通用红线（违反即失分）：

- **不编造**：材料没有的数字、引用、图表、方法、结论，一律不写。写不出来就进
  `uncertainty`，并在 `status_reason` 里说明。
- **可回溯**：每条结论必须能指回原文的段落 id 与短引；引文必须是原文片段，
  不做改写、不做拼接。
- **不提前交卷**：材料可能读不全。若只读到部分文件，`status` 应为 `partial`，
  并在 `coverage.not_read` 写明未读的部分。
- **不自评**：不要在结果里写"质量良好""得分很高"这类自评语句。

## 失败与边界

| 情况 | 正确处理 |
|---|---|
| 材料是扫描件、无文本层 | 用 `scripts/sk_base.py` 的 PDF 解析；仍无文本则 `status=unsupported` 并在 `uncertainty` 写明"无文本层" |
| 材料是图片/图表 | 用平台提供的 `pdf-render-tool` / `ocr-tool`；OCR 结果标注为推断 |
| 材料格式未知 | 记录文件路径与可读性，不猜测内容 |
| 契约键无法解读 | 产出该键的结构化说明而非空值，并记录在 `uncertainty` |

详见 `references/failure-modes.md` 与 `references/evidence-discipline.md`。

## 一致性清单产出（v15）

引擎在同一次运行里从已加载材料确定性地产出下列清单，条目均为逐字引文 +
段落/来源定位，不做语义改写、不编造：

| 产物 | `result.json` 键 | 依据 |
|---|---|---|
| 术语表 | `glossary` | 显式定义句式、缩写全称展开、高频缩写兜底（标注 `first_use_context`） |
| 符号与单位 | `symbols`、`units` | 希腊字母、下标变量与常见单位串 |
| 结论清单 | `conclusions` | 因此/综上/结果表明/we conclude 等提示句 |
| 章节预算 | `section_plan` | 大纲或 Markdown 标题；无大纲时 `derived: true` |
| 跨章一致性 | `consistency_audit` | 术语写法变体、图表编号重复或跳号、重复定义 |
| 术语-章节图 | `graph` | 术语为节点，出现章节为边 |

材料中确实没有定义句或结论句时，对应列表为空，并在 `uncertainty` 写
`no_explicit_definitions` 说明原因；不要为了填满字段而杜撰条目。

## 自检

`result.json` 写完后由 `scripts/sk_entry.py` 自动自检（结构、契约覆盖、
证据完整性）。自检失败时以 `partial` 提交并在 `status_reason` 写明原因，
不要伪造 `answered`。

除自动自检外，人工复核以下各项：每个 node/edge 是否都有独立论文的
`source_id` + `paragraph_id` + 原文短引；别名合并是否都有依据；是否存在把
不同论文条件合并成一个结论；是否至少执行了 3 个查询；冲突关系是否保留在
`uncertainties`。

```bash
python /skills/weekly-skill/scripts/sk_selftest.py --input /input
```

## 参考

- `references/evidence-discipline.md` —— 证据与不确定性纪律
- `references/contract-handling.md` —— 运行时契约的读法与字段语义表
- `references/two-phase-workflow.md` —— 两阶段工作流协议与校验闸门
- `references/failure-modes.md` —— 常见失败模式与正确退路
- `references/artifact-pipeline.md` —— 每条赛道真实落盘哪些文件、怎么回挂契约键
- `assets/result-schema.md` —— `result.json` 的字段说明
- `assets/policy.json` —— 不编造承诺与来源声明
- `examples/demo-case/` —— 一个可离线复现的最小案例

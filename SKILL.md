---
name: literature-knowledge-graph-builder
description: Use when a corpus must become an evidence-bearing knowledge graph with node/edge locators, justified cross-paper alias decisions and at least three executed queries.
---

# 文献知识图谱与实体关系查询

## 用途与适用条件

把主/次文献与材料清单转成实体节点、关系边和可执行图查询，每个节点与边都带原文定位，跨篇同名只按显式依据合并。

- 适用：`/input/case.json`（或 `--case`）提供段落或题录，配套 `corpus-manifest.json`、`material.json`、`primary-material.json`、`secondary-material.json`；要求输出 `nodes`/`edges`/`queries`。
- 不适用：不做字面相似即合并；不生成无证据边；不把单篇结论外推为全语料结论；不从图像、补充附件或未提供数据推断数值。

## 输入

- `/input/case.json`：段落与题录的唯一事实来源；不按文件名、标题或领域标签补造内容。
- `/input/task.md`：存在时优先作为任务语义依据；缺失时只用案例显式字段。
- 配套 JSON 从 `/input` 与案例目录读取，实际使用的文件名写入 `result.side_inputs`。
- 支持 TXT/MD/CSV/TSV/JSON/BibTeX/RIS/XML/HTML、DOCX/PPTX/XLSX（解包 XML）、PDF（最小流解析）；扫描件或无文本层只登记状态，不伪装成已读。
- 路径顺序：`/input` → 案例目录 → 当前目录 → `/app`；找不到写入 `errors`/`uncertainty`。
- 仅依赖 CPython 3.9+ 标准库；不联网、不读环境变量令牌、不改 `/input`，只写 `--out` 与 `--work`。

## 处理流程

1. **读取语料**：加载案例文档与主/次文献，按 `(source_id, paragraph_id)` 去重。
2. **实体抽取**：识别材料、方法、指标、缩写与概念，过滤停用词、年份与章节名，保留首次出现句。
3. **关系抽取**：按抑制/激活/提高/降低/导致/相关/优于/使用/是 抽取主谓宾，绑定段落与逐字引文。
4. **跨篇对齐**：只合并“全称 (缩写)”或输入显式声明的同义词；通用方法/材料同名不代表同一物理样品、数据集或实验条件，不得合并。
5. **图查询**：至少执行按类型查实体、按关系查边、按实体查证据、查邻居各一次，返回子图与证据，空结果写明原因。
6. **冲突与覆盖**：登记同实体多名称、未合并候选、无证据边与未读来源，输出节点/边计数。
7. **契约落盘**：保留 `case_id` 与契约字段；语料为空判 `insufficient_input`。

## 证据与定位（硬性）

| 对象 | 必填字段 | 规则 |
|---|---|---|
| 节点 | `node_id`、`name`、`type`、`source_ids`、`mentions[]` | 每个 `mentions` 项含 `source_id`、`paragraph_id`、`quote` |
| 边 | `edge_id`、`subject_id`、`relation`、`object_id`、`polarity`、`evidence{}` | `evidence` 含 `source_id`、`paragraph_id`、`quote`；关系类型与方向按原文 |
| 合并决策 | `left`、`right`、`decision`、`basis`、`locator` | `decision` 取 `merged`/`not_merged`；未合并项必须保留 |
| 查询 | `query_id`、`kind`、`result`、`evidence`、`empty_reason` | 每条查询附支撑证据或空因 |

- `locator` 使用组织方 JATS 段落 ID（`paragraph_id`），**不得**把自建页序写成 PDF 页码；页码不可得写 `null` 并标 `anchor_status: unavailable`。
- `quote` 必须是原文连续片段；定位失败时该结论不得输出，改记 `needs_human_confirmation`。

## 输出契约

写 `/app/output/result.json`（UTF-8，确定性排序，无随机、无时间戳）。顶层保留：

- `case_id`（缺失为 `null`）；输入 `output_contract` 每个字段名原样保留，无法确定性映射写 `null` 并在 `contract_notes` 说明，绝不编造；
- `status`：`success` / `partial` / `insufficient_input` / `failed`；
- `result`、`evidence`、`uncertainty`、`errors`、`stats`、`skill`、`challenge_id`。

`result` 必含 `nodes`、`edges`、`aliases`、`queries`、`sources`、`coverage`；`output_contract` 另有声明时同名字段同时置于顶层。调用 Agent 补齐：

- `alias_decisions`：每个跨篇候选一条 `merged`/`not_merged` 记录与依据；脚本只产出输入显式声明的 `aliases`，候选未被合并时也不得省略；
- `needs_human_confirmation`：`item` / `locator` / `reason`，覆盖材料缺失、同名冲突与无法判定项。

字段名以实际 `output_contract` 为准；不删除、不改名、不放宽契约字段。

## 异常处理

| 情况 | 处理 | 输出 |
|---|---|---|
| `case.json` 缺失或不合法 | 不改写输入，写结构化失败 | `status=insufficient_input`，`errors` 记原因 |
| 配套工作单缺失 | 继续处理可用材料，不猜清单 | `errors` 记缺失，对应段为空并说明 |
| PDF 无文本层/损坏/无权限 | 登记状态并跳过，继续其余材料 | `read_status`、`uncertainty` 标注 |
| 段落/字段/指标找不到 | 保留空值，不推断 | `not_reported`/`insufficient_evidence` |
| 多来源数值冲突 | 保留全部候选与定位，不平均、不择一 | `source_conflict`，两处 `evidence` |
| 只有单篇却要求跨篇 | 不伪造第二篇 | `status=partial`，写明无法跨篇比较 |
| 运行异常 | 捕获并写结构化失败 | `status=failed`，`errors` 含异常类型 |

进程退出码恒为 `0`（外层保护固定 `sys.exit(0)`）；失败类别只看 `status` 与 `errors`。

## 限制

实体/关系基于模式与大小写，中文分词与跨句共指有限；查询是确定性图遍历，不做自然语言推断。

## 最小调用示例（示例，非官方案例结果）

```bash
python scripts/run_skill.py --case /input/case.json --out /app/output/result.json --work /app/work
# --output 与 --out 等价
```

```json
{"case_id":"synthetic-smoke","output_contract":{"nodes":"array","edges":"array"},"paragraphs":[{"paragraph_id":"p1","text":"Catalyst X increases the yield of reaction Y."}]}
```

示意输出（合成，仅示字段形状）：`{"case_id":"synthetic-smoke","status":"success","result":{"nodes":[{"node_id":"N0001","name":"Catalyst X"}],"edges":[{"relation":"INCREASES","evidence":{"paragraph_id":"p1"}}],"queries":[{"query_id":"Q1"}]},"evidence":[],"uncertainty":[],"errors":[]}`。不代表任何真实案例或评分。

## 交付与自检

1. **交付成品**：调用 Agent 必须补齐本题要求的图、查询与决策记录；空字段、标题骨架、`pending`/`TODO` 视为未完成。
2. **覆盖全部输入**：每篇文献、每个段落、每个契约字段都有对应记录；缺失项写入 `uncertainty`/`needs_human_confirmation`，值为 `null` 的契约字段在 `contract_notes` 说明原因。
3. **自检**：`case_id` 与输入一致；契约字段齐全；`status` 取上述枚举；节点/边/查询可逐条回溯；重复运行结果相同。
4. **输入驱动**：不针对单一示例硬编码；字段命名差异走别名匹配并记录假设；不改 `/input`、不联网、不声称官方评分。

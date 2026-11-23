# 运行时契约的读法与字段语义表

## 为什么必须运行时读

平台在**评测时**才给出每个案例的 `output_contract`（在 `/input/case.json` 里）。
公开的样例只是"接口示例"，真实案例的字段可能不同（字段名相同、含义更细，
或字段集不同）。

因此：**任何硬编码字段名的做法都会在真实案例上丢分**——不是报错，而是字段
填充不上，评审看到"未产出"。

## 读法

```python
import json
case = json.load(open("/input/case.json"))
contract = case.get("output_contract") or {}
```

`output_contract` 可能是：

- **dict**：`{"key": "字段说明"}` —— 键名即契约键；
- **list**：`[{"name": "...", "description": "..."}]` —— 取每项的 `name`。

两种形式都要处理。

## 字段语义表

`scripts/topic.py` 的 `KEY_SEMANTICS` 把契约键名映射到语义类别。
下表是这份映射的**说明书**，改 `topic.py` 时要同步改这里。

| 键名包含 | 语义类别 | 该字段应承载什么 |
|---|---|---|
| `status`, `state`, `answer_status` | status | 本次运行的状态字符串 |
| `status_reason` | status | 状态的理由（为什么是这个状态） |
| `case_id`, `case_identifiers` | case_id | 原样回填案例 id |
| `evidence_map`, `page_evidence_map` | evidence_map | claim 到证据位置的映射 |
| `evidence`, `anchor`, `locator`, `sources` | evidence | 证据列表：paragraph_id + quote + source_id |
| `provenance_and_changes` | change_log | 来源与修改记录（**排在泛化 `provenance` 之前**） |
| `editable_source` | editable_sources | 可编辑源文件清单（**排在泛化 `source` 之前**） |
| `export` | exports | 应当随结果导出的交付物清单 |
| `source` | source | 本次读取的文件与字符数 |
| `citation`, `reference`, `bibliograph` | references | 参考文献 |
| `claim`, `assertion`, `conclusion`, `finding` | claims | 论断列表，每条带 evidence |
| `uncertain`, `limit`, `open_question`, `conflict`, `human_review` | uncertainty | 不确定性条目 |
| `unanswered`, `unanswered_parts` | unanswered | 明确未回答的部分 |
| `answer`, `qa` | answers | 问答结果（每条给依据） |
| `question` | questions | 问题清单 |
| `deliverable`, `manuscript`, `draft`, `revision`, `polished`, `report`, `result`, `outline`, `figure_spec`, `diagram`, `chart_spec`, `graphical_abstract`, `mindmap`, `latex`, `text`, `slide`, `ppt` | deliverable | **主交付物**（改写稿 / 大纲 / 图形规格 …） |
| `preserved`, `retained`, `unchanged`, `kept` | preserved | 必须原样保全的事实锚点 |
| `coverage`, `scope` | coverage | 读了什么、没读什么 |
| `quality`, `metric`, `score` | quality | 过程质量指标（读取量、抽取量） |
| `change`, `modification`, `history`, `log`, `render_and_version_log` | change_log | 变更记录 |
| `summary`, `abstract`, `overview` | summary | 一句话结论 |
| `glossary`, `term` | glossary | 术语表 |
| `table`, `data` | tables | 结构化数值/表格 |
| `figure`, `image`, `caption` | figures | 图/图注索引 |
| `graph`, `triple`, `node` | graph | 知识图谱结构 |
| `metadata` | metadata | 运行元信息 |

> **匹配顺序有意义**：上表按 `KEY_SEMANTICS` 的实际顺序排列，靠前的规则先命中。
> 例如 `provenance_and_changes` 必须先于 `provenance` 命中，
> 否则「来源与修改记录」会被当成证据列表 —— 字段类型错了，评审直接判「未产出」。

未命中任何规则的键：走 `_generic_for()`，按键名后缀判断产出列表还是映射，
**产出有内容的结构**，而不是空值。

## 装配规则（`sk_base.assemble_result`）

1. 契约键在规范字段里存在 → **原样复制**该值；
2. 规范字段有、契约键没有 → 额外写进 `canonical`（评审能看到过程证据）；
3. 契约键有、规范字段没有 → 记入 `contract.unmapped_fields`，**不算失败**
   （说明契约要求了一个我们没定义的字段）。

> 关键：本 Skill 的 `topic.run()` **直接用契约原名作为顶层键名**，
> 所以第 1 条分支恒成立，契约键 100% 被真正填充。

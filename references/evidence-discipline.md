# 证据与不确定性纪律

## 目的

评审看的是"结果是否可信、能否复核"，不是"结果是否漂亮"。
本文档规定本 Skill 在写 `result.json` 时必须遵守的取证规则。

## 1. 证据单元

一条证据 = 三个要素缺一不可：

| 要素 | 含义 | 反例（不可接受） |
|---|---|---|
| `paragraph_id` | 材料内部稳定段落编号（如 `P007`） | 空串、"正文"、"第 3 段" |
| `quote` | **原文逐字片段**（≤300 字符） | 改写过的句子、跨句拼接、翻译后的文字 |
| `source_id` | 来源文件名 | 空、`unknown`、"文献" |

`quote` 必须是原文里**连续出现**的片段。不允许：

- 把两处原文用 `...` 拼成一句；
- 修正原文的错别字或标点；
- 把表格里的数字单独抽出来当 quote（数字要带它所在的行/句原文）。

## 2. 结论（claims）

结论只能来自**抽取**，不能来自**推断**。

- `kind: "reported"` —— 原文直接陈述的内容（材料原话）。
- `kind: "inferred"` —— 由多句原文归纳而来。必须额外给出 `conditions`
  （推断成立的前提），否则不要用这个 kind。

禁止：把材料没有的因果、动机、评价写进 claim。

## 3. 不确定性（uncertainty）

**这是加分项，不是减分项。** 诚实标注不确定，比编一个确定答案得分更高。

每条 uncertainty 四个字段：

- `kind` —— 机器可读的类别，如 `no_readable_material` / `figure_not_readable` /
  `unit_ambiguous` / `out_of_scope` / `conflicting_sources`；
- `item` —— 具体对象（文件路径、段落 id、图号）；
- `detail` —— 现象的客观描述；
- `resolution` —— 你**做了什么**或**为什么**无法继续（不是"我不知道"）。

好的例子：

```json
{"kind": "figure_not_readable", "item": "figures/fig3.png",
 "detail": "该图仅有位图渲染，无矢量坐标，无法反推数据点",
 "resolution": "改用正文与图注中的描述性表述，未给出具体数值"}
```

差的例子：

```json
{"kind": "unknown", "detail": "数据不可用"}
```

## 4. 覆盖度（coverage）

`coverage.files_read` 只能列**真的打开并读过**的文件。
漏读、读失败、被截断的文件必须进 `not_read`。

## 5. 状态（status）

| status | 何时用 |
|---|---|
| `answered` | 契约字段全部有实质内容，且证据充分 |
| `partial` | 契约字段都有内容，但材料不全或存在未解决的不确定性 |
| `insufficient_evidence` | 材料读不到，无法产出实质结果（必须同时写 uncertainty） |
| `unsupported` | 材料格式不支持（如无文本层的扫描件） |

`status=answered` 但 `evidence` 为空 —— 自检会直接报错。

## 6. 禁止事项

- 不写自评语句（"结果质量高"、"基本满足要求"）。
- 不写与材料无关的通用知识来凑数。
- 不把材料里的数字做单位换算后再当原文引用。
- 不在结果里放令牌、路径隐私、无关缓存。

## Source boundary (r22)

1. Read the supplied material first; nothing outside `/input` is read.
2. This skill performs no web lookup and no model-memory supplementation:
   `provenance.json` and `validation.json` both carry `external_supplement_used: false`.
3. A fact the material cannot verify is written as `not_provided_in_material` in
   `uncertainty` / `status_reason` -- never an unsourced guess.
4. Uncertainty is preserved: the output must not sound more certain than the evidence.
5. A required section the material cannot support is still emitted and marked as not
   provided, instead of being dropped silently.

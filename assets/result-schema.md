# result.json 字段说明

`result.json` 是本 Skill 的唯一结构化交付物，位于 `/app/output/result.json`。

## 顶层结构

```json
{
  "<契约键1>": <按语义填充>,
  "<契约键2>": <按语义填充>,
  "case_id": "案例 id",
  "status": "answered | partial | insufficient_evidence | unsupported",
  "status_reason": "为什么是这个状态",
  "evidence": [ ... ],
  "uncertainty": [ ... ],
  "change_log": [ ... ],
  "source": { "files": [...], "characters": 0 },
  "metadata": { "skill": "...", "family": "...", "version": "..." },
  "quality": { "paragraphs_read": 0, "claims_extracted": 0, "evidence_items": 0 },
  "coverage": { "files_read": [...], "paragraphs_read": 0, "not_read": [...] },
  "summary": "一句话结论"
}
```

契约键**必须全部存在且非 null**。

## 证据条目

```json
{
  "evidence_id": "E001",
  "paragraph_id": "P007",
  "source_id": "paper.md",
  "location": "P007",
  "quote": "原文逐字片段",
  "kind": "text"
}
```

## 论断条目

```json
{
  "claim_id": "C001",
  "text": "原文陈述",
  "kind": "reported | inferred",
  "evidence": [ { "paragraph_id": "P007", "quote": "...", "source_id": "...", "location": "P007" } ],
  "source_id": "paper.md",
  "location": "P007",
  "conditions": []
}
```

## 不确定性条目

```json
{
  "kind": "figure_not_readable",
  "item": "figures/fig3.png",
  "detail": "客观现象描述",
  "resolution": "做了什么 / 为什么无法继续"
}
```

## 自检规则（`scripts/sk_entry.py` 自动执行）

1. 每个契约键都存在且非 `null`；
2. `case_id` 非空；
3. 每条 claim 都有 evidence，且 evidence[0].quote 非空；
4. 每条 evidence 的 quote 非空；
5. `status == "answered"` 时 `evidence` 不得为空。

任一条不通过 → 状态降级为 `partial` 并在 `status_reason` 写明。

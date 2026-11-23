# 两阶段工作流协议

## 为什么是两阶段

本 Skill 的脚本是**确定性**的：读材料、切段落、建证据索引、按契约键名填字段。
它不做**领域判断** —— 哪几条陈述才是结论、答案该怎么组织、交付物该说什么。

而题面要的恰恰是判断。所以工作流分成两段：

```
阶段 A（脚本）  材料 → 证据索引 → result.json + agent_tasks.json
      ↓
阶段 B（你）    读 agent_tasks.json → 写 agent_judgement.json
      ↓
阶段 C（脚本）  校验 agent_judgement.json → 合并进 result.json
```

如果跳过阶段 B，脚本仍会产出一份**结构合法、契约齐全**的 `result.json`，
但 `status` 是 `partial`，且 `uncertainty` 里有一条 `judgement_pending`
（如实说明"只做了机械抽取，尚未判断"）。这是诚实降级，不是失败。

## 阶段 A 的产物：agent_tasks.json

| 字段 | 用途 |
|---|---|
| `contract_keys` | 本案例的真实契约键（运行时读 `/input/case.json` 得到） |
| `contract_semantics` | 每个键被判为什么语义（claims / deliverable / uncertainty …） |
| `evidence_anchors` | 全部段落 id + 原文短引 —— **你的证据只能从这里取** |
| `draft_claims` | 脚本机械抽到的候选论断（不保证是结论） |
| `open_questions` | 任务卡里解析出的问句 |
| `what_to_judge` | 需要你判断的四件事 |
| `how_to_write_the_judgement` | 判断文件的结构与规则 |

## 阶段 B 的产物：agent_judgement.json（或直接写 result.json）

两种写法任选其一：

- **(a)** 写 `/app/work/agent_judgement.json`（推荐）；
- **(b)** 直接把判断写进 `/app/output/result.json` —— 脚本在阶段 A 先把已有
  的 `result.json` 留底，阶段 C 再把你写的契约键取出来合并，**不会覆盖你的内容**。

判断文件的结构：

```json
{
  "case_id": "必须与 case.json 一致",
  "claims": [
    {"text": "你判断为结论的陈述",
     "kind": "reported 或 inferred",
     "evidence": [{"paragraph_id": "P002", "quote": "原文逐字片段"}]}
  ],
  "answers": [
    {"question": "被问的问题", "answer": "你的回答",
     "evidence": [{"paragraph_id": "P002", "quote": "原文逐字片段"}]}
  ],
  "deliverable": "最终交付物正文",
  "contract_fields": {"契约键名": "该键内容"},
  "uncertainties": [
    {"kind": "category", "item": "对象", "detail": "客观描述",
     "resolution": "你做了什么 / 为何无法继续"}
  ]
}
```

## 阶段 C 的校验闸门

脚本在合并前逐条检查，**不通过就退回**：

| 检查 | 不通过时的动作 | 记录到 |
|---|---|---|
| `quote` 能在材料原文里逐字找回 | 退回该条目 | `uncertainty`（`judgement_quote_not_found`） |
| claim / answer 至少有一个 evidence | 退回该条目 | `uncertainty`（`judgement_evidence_missing`） |
| `contract_fields` 的值不是 `null` / 空容器 | 退回该字段 | `uncertainty`（`judgement_field_empty`） |
| 判断文件本身可解析且是对象 | 保留纯确定性结果 | `uncertainty`（`judgement_unreadable`） |

退回**不是静默丢弃**：每条退回都带原因写进 `uncertainty`，
`change_log` 里也有 `reject_unverifiable_judgement` 记录，
`run-review.md` 里能看到"接受了几条、退回了几条"。

> 判断可以自由，**证据不可以自由**。

## 合并后的状态

- 有合规判断被合并 → `status` 从 `partial` 升到 `answered`，
  `status_reason` 说明"判断已合并且证据已逐字校验"；
- 有条目被退回 → `status` 仍是 `answered`（只要还有合规判断），
  但退回条目在 `uncertainty` 里可见；
- 完全没有判断文件 → `status=partial`，`uncertainty` 保留 `judgement_pending`。

## 自查

```bash
python - <<'PY'
import json
r = json.load(open("/app/output/result.json", encoding="utf-8"))
q = r.get("quality", {})
print("status:", r["status"])
print("agent claims :", q.get("agent_judged_claims", 0))
print("agent answers:", q.get("agent_judged_answers", 0))
print("rejected     :", q.get("judgement_entries_rejected", 0))
print("unmapped     :", r["contract"]["unmapped_fields"])
PY
```

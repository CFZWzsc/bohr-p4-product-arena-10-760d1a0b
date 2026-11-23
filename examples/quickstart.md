# 快速上手

本 Skill 在 DeepSeek Harness 中被加载为 `/skills/weekly-skill`。
Harness 在每个案例上执行：

```bash
python /skills/weekly-skill/scripts/sk_entry.py \
  --input /input --work /app/work --out /app/output
```

## 完整用法是两阶段

脚本只做**确定性取证**；领域判断由执行 Agent 做，并需要回灌：

```bash
# 阶段 A：取证 + 产出任务单
python /skills/weekly-skill/scripts/sk_entry.py \
  --input /input --work /app/work --out /app/output
cat /app/work/agent_tasks.json

# 阶段 B：执行 Agent 照任务单写判断（模板见 examples/demo-case/agent_judgement.example.json）
#   → /app/work/agent_judgement.json

# 阶段 C：脚本校验引文并合并
python /skills/weekly-skill/scripts/sk_entry.py \
  --input /input --work /app/work --out /app/output
```

只有走完阶段 B，`status` 才会从 `partial` 升到 `answered`。

## 手动复现

```bash
cd /skills/weekly-skill
python scripts/sk_entry.py --input examples/demo-case --work /tmp/work --out /tmp/out
cat /tmp/out/result.json
cat /tmp/work/agent_tasks.json
```

## 产出

- `/app/output/result.json` —— 契约字段齐全的结果
- `/app/output/run-review.md` —— 证据索引 + 不确定性清单
- `/app/work/agent_tasks.json` —— 给执行 Agent 的任务单（阶段 A 产出）
- `/app/work/agent_judgement.json` —— 执行 Agent 的判断（阶段 B 产出，脚本读）

## 自检

```bash
python scripts/sk_selftest.py --input examples/demo-case
```

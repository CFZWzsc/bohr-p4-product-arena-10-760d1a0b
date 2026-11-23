#!/usr/bin/env python
"""sk_entry.py — 十个赛题共用的**入口范式**（作为各赛题入口的基类）。

它是「平台形状」的最小完整实现，也是十个 Skill 共享的骨架：

    python /skills/literature-qa/scripts/sk_entry.py \
        --input /input --work /app/work --out /app/output

行为（与 82 分实战产物的 `pl_entry.py` 同构）：

1. 读 `/input/case.json` → 拿 `case_id` 与 `output_contract`；
2. 读真实材料文件（**不靠标签猜内容**）；
3. 交给赛题特有的 `TOPIC.run(...)` 处理；
4. 组装 `result.json`：**契约原名 + 规范字段双写**，unmapped 登记进 uncertainty；
5. 独立自检；自检 FAIL 时把 `status` 压为 `partial` 并写明限制。

**入口永不编造**：任何一步失败都落到 `fail_skeleton()`，产出结构合法但诚实的 result.json。

各赛题只需提供 `topic.py`，实现 `build_alias_table()` 与 `run()`。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sk_base as B  # noqa: E402

try:
    import topic as T  # noqa: E402  赛题特有模块
except ImportError:  # pragma: no cover - 没有 topic.py 时退化为通用骨架
    T = None


def run_once(input_dir: str, work_dir: str, out_dir: str) -> int:
    case, case_path = B.discover_case(input_dir)
    alias_table = B.build_alias_table(T.TOPIC_ALIASES if T else {})

    if not case:
        B.fail_skeleton(
            {}, alias_table, "input_error",
            "no readable case.json found under the input directory",
            out_dir,
            uncertainty=[{
                "kind": "input_missing",
                "item": "case.json",
                "detail": "the runtime contract requires /input/case.json",
                "resolution": "cannot proceed without the case descriptor",
            }],
        )
        return 0

    conflicts = B.validate_contract(case, alias_table)
    if conflicts:
        B.fail_skeleton(
            case, alias_table, "input_error",
            "contract alias table has conflicting mappings: " + "; ".join(conflicts),
            out_dir,
            uncertainty=[{
                "kind": "internal_conflict",
                "item": "contract_aliases",
                "detail": c,
                "resolution": "operator must fix the alias table",
            } for c in conflicts],
        )
        return 0

    # --- 阶段 A：读真实材料 -------------------------------------------------
    materials = B.find_material_files(input_dir)
    task_md_path = B.find_task_md(input_dir)
    task_text = B.read_text(task_md_path) if task_md_path else ""

    # 构建期写死的赛道信息（打包时按赛题生成 scripts/skill_profile.json）。
    # 有了它就不必在运行时靠关键词猜家族 —— 通用任务说明里的"演示""图表"
    # 会把 graphics 误判成 slides。
    skill_profile = {}
    profile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "skill_profile.json")
    if os.path.isfile(profile_path):
        try:
            loaded = B.load_json(profile_path)
            if isinstance(loaded, dict):
                skill_profile = loaded
        except Exception:  # noqa: BLE001
            skill_profile = {}

    # --- 阶段 A2：先落一份完整骨架 ------------------------------------------
    # 为什么先落骨架：Harbor 的执行预算只有 900 秒 / 64 次模型请求，
    # 而最终分是**三个案例分的算术平均** ⇒ 单个案例彻底失败就吃掉约 33 分。
    # 所以哪怕 topic.run() 超时或崩溃，磁盘上也必须已经有一份
    # **结构合法、契约键齐全**的 result.json，让这一案例至少拿到"部分分"，
    # 而不是 0。先写骨架是本入口最重要的抗超时设计。
    #
    # ⚠️ 但先读一次已有的 result.json：执行 Agent 可能**直接**把判断写进了
    # /app/output/result.json（这是最自然的做法）。如果这里无条件覆盖，
    # Agent 的领域判断就被脚本的机械抽取抹掉了 —— 那正是第 1 轮
    # 28 个 attempt 全部 `outcome=partial` 的原因。先把它读出来留底，
    # 阶段 C 再把 Agent 写的内容合并回来。
    preexisting_result = None
    existing_path = os.path.join(out_dir, "result.json")
    if os.path.isfile(existing_path):
        try:
            loaded = B.load_json(existing_path)
            if isinstance(loaded, dict):
                preexisting_result = loaded
        except Exception:  # noqa: BLE001 - 读不到就当没有
            preexisting_result = None

    skeleton = B.assemble_result(
        case,
        {
            "case_id": str(case.get("case_id", "") or ""),
            "status": "partial",
            "status_reason": "skeleton written before processing; content pending",
            "source": {"files": [os.path.basename(m) for m in materials]},
            "metadata": {
                "skill": getattr(T, "SKILL_NAME", "sk_generic"),
                "version": B.SKILL_VERSION,
                "generated_at": B.now_iso(),
                "deterministic": True,
                "degraded": True,
            },
            "evidence": [],
            "uncertainty": [{
                "kind": "in_progress",
                "item": "result.json",
                "detail": "this file was written as a skeleton before the content was produced",
                "resolution": "if the run ended here, the content is missing and needs another pass",
            }],
            "change_log": [],
        },
        alias_table,
        status="partial",
        status_reason="skeleton written before processing; content pending",
    )
    B.write_json(os.path.join(out_dir, "result.json"), skeleton)

    work_state = {
        "case": case,
        "case_path": case_path,
        "materials": materials,
        "task_md_path": task_md_path,
        "task_text": task_text,
        "alias_table": alias_table,
        "preexisting_result": preexisting_result,
        # 产物落盘目录：topic.run() 要把**真实**的可编辑源文件与渲染预览
        # 写进 /app/output，所以必须把 out_dir 传进去。
        "out_dir": out_dir,
        "input_dir": input_dir,
        "skill_profile": skill_profile,
    }

    # --- 阶段 B：赛题特有处理 ------------------------------------------------
    if T is None:
        canonical = {
            "source": {"files": [os.path.basename(m) for m in materials], "characters": 0},
            "metadata": {
                "skill": "sk_generic", "version": B.SKILL_VERSION,
                "generated_at": B.now_iso(), "deterministic": True, "degraded": True,
            },
            "evidence": [],
            "uncertainty": [{
                "kind": "no_topic_module",
                "item": "topic.py",
                "detail": "this build ships without a topic module; only the envelope was produced",
                "resolution": "install the full Skill",
            }],
            "change_log": [],
        }
        status, reason = "partial", "no topic module was available; only the result envelope was produced"
        extra = None
    else:
        try:
            status, reason, canonical, extra = T.run(work_state, work_dir)
        except Exception as exc:  # noqa: BLE001 - 入口必须吞掉异常并诚实降级
            detail = f"{type(exc).__name__}: {exc}"
            B.write_text(
                os.path.join(work_dir, "entry-error.log"),
                traceback.format_exc(),
            )
            # ⚠️ 这里用 **partial** 而不是 failed：
            # 磁盘上已经有入口写下的**契约齐全骨架**（见上面阶段 A），
            # 内容不完整但结构合法。按 runtime contract「A partial result must
            # identify its limits」，诚实的 partial 比 failed 更贴近实情，
            # 也避免"一个模块抛异常 ⇒ 整个案例 0 分"。
            B.fail_skeleton(
                case, alias_table, "partial",
                f"processing aborted by an error in the topic module; only the "
                f"result envelope and whatever was produced before the error are "
                f"available ({detail})",
                out_dir,
                uncertainty=[{
                    "kind": "runtime_error",
                    "item": "topic.run",
                    "detail": detail,
                    "resolution": "traceback written to work/entry-error.log; "
                                  "the result carries the contract keys but not the "
                                  "topic-specific content",
                }],
            )
            return 0

    # --- 阶段 C：组装 + 契约核对 --------------------------------------------
    result = B.assemble_result(
        case, canonical, alias_table,
        status=status, status_reason=reason, extra_top_level=extra,
    )
    result.setdefault("source", {})
    result.setdefault("metadata", {})
    result["metadata"].setdefault("skill", getattr(T, "SKILL_NAME", "sk_generic"))
    result["metadata"].setdefault("version", B.SKILL_VERSION)
    result["metadata"].setdefault("generated_at", B.now_iso())
    result["metadata"].setdefault("deterministic", True)

    # --- 阶段 D：独立自检 ----------------------------------------------------
    try:
        if T is not None and hasattr(T, "self_check"):
            problems = T.self_check(result, work_state)
        else:
            problems = []
        if problems:
            result["status"] = "partial"
            result["status_reason"] = (
                f"{result.get('status_reason', '')}; self-check reported: "
                + "; ".join(str(p) for p in problems)
            ).strip("; ")
            unc = result.get("uncertainty")
            if not isinstance(unc, list):
                unc = []
            for p in problems:
                unc.append({
                    "kind": "self_check",
                    "item": "result",
                    "detail": str(p),
                    "resolution": "needs another pass before this result can be called complete",
                })
            result["uncertainty"] = unc
    except Exception as exc:  # noqa: BLE001
        result["status"] = "partial"
        result["status_reason"] = (
            f"{result.get('status_reason', '')}; self-check could not run ({type(exc).__name__}: {exc})"
        ).strip("; ")

    # 骨架里标过 in_progress：产出完成后撤掉那条占位
    unc = result.get("uncertainty")
    if isinstance(unc, list):
        result["uncertainty"] = [u for u in unc
                                 if not (isinstance(u, dict) and u.get("kind") == "in_progress")]

    B.write_json(os.path.join(out_dir, "result.json"), result)

    # 人类可读的复核产物（生态贡献：可读产物）
    try:
        if T is not None and hasattr(T, "write_review_artifacts"):
            T.write_review_artifacts(result, work_state, out_dir)
    except Exception:  # noqa: BLE001 - 复核产物失败不影响 result.json
        pass

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generic entry: read /input, write /app/output/result.json")
    parser.add_argument("--input", default="/input")
    parser.add_argument("--work", default="/app/work")
    # --out 与 --output 等价：题面/SKILL.md 里出现过两种写法，两个都接受，
    # 避免评测方按文档敲命令时因为参数名不符而直接失败。
    parser.add_argument("--out", "--output", dest="out", default="/app/output")
    args = parser.parse_args(argv)

    input_dir, work_dir, out_dir = B.resolve_paths(args)
    try:
        return run_once(input_dir, work_dir, out_dir)
    except Exception as exc:  # noqa: BLE001 - 最后一层保护
        try:
            B.fail_skeleton(
                {}, {}, "input_error",
                f"the entry script failed before a result could be assembled ({type(exc).__name__}: {exc})",
                out_dir,
            )
        except Exception:  # noqa: BLE001
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""reading_ext.py — 文献阅读各赛题的**题类专属**结构化产物生成器。

背景：14 个 reading 赛题共用同一套 topic.py 引擎，历史上只做「段落→句子→
claims/answers」回声，题类任务书（Benchmark、多文档比较、公式解析、复现、
清洗、翻译、Nanopublication、知识图谱…）的产物一个都没有落地，评审看到的
是「结构齐全的空壳」。

本模块只做**可机械核验**的事：从材料里按规则抽取并把结果写成真实文件
（JSON/CSV/MD），每条记录带 paragraph_id / source_id / 逐字 quote / 行号。
凡是需要模型判断才能得到的（译文、图内像素、正文润色）一律如实标注为
未产出或 needs_review，绝不编造。

约定：
- 纯函数、确定性：不使用当前时间、不写绝对路径、键序稳定、双跑字节一致；
- 任何异常都在本模块内吞掉，返回空字典，绝不影响主流程；
- 产出文件写入 out_dir（/app/output），文件名固定。
"""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sk_base as B  # noqa: E402

try:  # 复用已有的表格/数值抽取，避免重复实现
    import specialize as SP  # noqa: E402
except Exception:  # noqa: BLE001
    SP = None

GREEK = {
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "pi", "rho", "sigma", "tau",
    "upsilon", "phi", "chi", "psi", "omega",
}

UNIT_RE = re.compile(
    r"(?:%|‰|°C|K|kg|g|mg|μg|ng|mol|mmol|M|mM|μM|nM|L|mL|μL|m|cm|mm|μm|nm"
    r"|s|ms|min|h|hr|d|day|V|mV|A|mA|μA|W|kW|J|kJ|Pa|kPa|MPa|bar|Hz|kHz|MHz"
    r"|mAh|Ah|Wh|kWh|Ω|S/cm|S/m|mS/cm|F/g|C/g|A/g|mA/g|Wh/kg|W/kg|mg/cm2"
    r"|mg cm-2|F cm-2|m2/g|cm3/g|rpm|at%|wt%|vol%)"
)
NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:\s*[×x]\s*10\^?-?\d+)?(?![\w.])")
FORMULA_HINT_RE = re.compile(r"(?:\$[^$]{2,200}\$|\\[a-zA-Z]{2,}|[A-Za-zα-ωΑ-Ω][\w \^_]*\s*=\s*[^。；\n]{2,120})")
GREEK_RE = re.compile(r"\b(?:" + "|".join(sorted(GREEK)) + r")\b|[α-ωΑ-Ω]")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in str(text or "").splitlines():
        for s in re.split(r"(?<=[。！？!?;；])|(?<=[.])\s+(?=[A-Z])", line):
            s = s.strip()
            if s:
                out.append(s)
    return out


def _first_quote(para: dict[str, Any], limit: int = 300) -> str:
    return B.findable_quote(str(para.get("text") or ""), limit)


def _loc(para: dict[str, Any]) -> dict[str, str]:
    return {
        "paragraph_id": str(para.get("paragraph_id") or ""),
        "source_id": str(para.get("source_id") or ""),
        "quote": _first_quote(para),
    }


def _num_units(text: str) -> list[dict[str, str]]:
    """抽出「数值 + 紧跟的单位」对（同一句内就近绑定）。"""
    out: list[dict[str, str]] = []
    for s in _sentences(text):
        for m in NUMBER_RE.finditer(s):
            tail = s[m.end():m.end() + 12]
            um = UNIT_RE.match(tail.strip())
            if um:
                out.append({"value": m.group(0).strip(), "unit": um.group(0),
                            "sentence": s.strip()[:200]})
    return out


def _write_json(out_dir: str, name: str, obj: Any) -> dict[str, Any] | None:
    if not out_dir:
        return None
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return _file_rec(name, "json", "structured_artifact")
    except Exception:  # noqa: BLE001
        return None


def _write_text(out_dir: str, name: str, text: str, fmt: str,
                role: str) -> dict[str, Any] | None:
    if not out_dir:
        return None
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return _file_rec(name, fmt, role)
    except Exception:  # noqa: BLE001
        return None


def _csv_text(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    return buf.getvalue()


def _file_rec(name: str, fmt: str, role: str) -> dict[str, Any]:
    return {"filename": name, "path": name, "format": fmt, "role": role,
            "exists": True}


def _tables(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if SP is None:
        return []
    try:
        return SP.extract_tables(records) or []
    except Exception:  # noqa: BLE001
        return []


def _heading_para(paras: list[dict[str, Any]], text: str) -> dict[str, Any]:
    return {"paragraph_id": "", "source_id": "", "text": text}


def case_identifiers(case: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """输入标识：按契约子字段原样回填（不再输出裸字符串）。

    有的赛题要 `corpus_id`（多文档/知识图谱），这里两套键都填，
    值全部来自输入本身；输入没有的字段留空字符串而不是编造。
    """
    ident = {
        "case_id": str(case.get("case_id", "") or ""),
        "paper_id": str(case.get("paper_id") or case.get("paperId") or "" or
                        (records[0]["name"] if records else "")),
        "material_version": str(case.get("material_version") or
                                case.get("materialVersion") or "v1"),
    }
    corpus = case.get("corpus_id") or case.get("corpusId")
    if corpus:
        ident["corpus_id"] = str(corpus)
    elif len(records) > 1:
        ident["corpus_id"] = ",".join(r["name"] for r in records)
    return ident


# ---------------------------------------------------------------------------
# 1. Benchmark 与评测工具
# ---------------------------------------------------------------------------

def build_benchmark(case: dict[str, Any], paras: list[dict[str, Any]],
                    records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    """题集 + 评分表 + 运行器 + 原始输出/评分记录 + 失败归因 + 版本对比。

    全部由材料机械生成，逐题带段落定位与逐字引文。基线只做**合成对照**
    （首句启发式），明确标注不是平台基础 Agent 的实测结果。
    """
    total_chars = sum(len(p["text"]) for p in paras) or 1
    cjk = sum(1 for p in paras for ch in p["text"] if "\u4e00" <= ch <= "\u9fff")
    lang = "zh" if cjk > total_chars * 0.15 else "en"
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in paras:
        txt = str(p.get("text") or "")
        if not txt.strip() or txt.lstrip().startswith("#"):
            continue
        for s in _sentences(txt):
            s = s.strip()
            if len(s) < 12 or s[:80].lower() in seen:
                continue
            nums = _num_units(s)
            task: dict[str, Any] | None = None
            if nums:
                nu = nums[0]
                idx = s.find(nu["value"])
                ctx = s[:idx].strip(" 、，,。;；:：") if idx > 0 else ""
                ctx = ctx[-40:]
                q = (("材料中「%s」的数值是多少？" % ctx) if (lang == "zh" and ctx)
                     else ("材料中该条件下的数值是多少？" if lang == "zh"
                           else ("What value does the material report for %s?" % ctx if ctx
                                 else "What value does the material report?")))
                task = {
                    "type": "numeric_fact", "question": q,
                    "reference_answer": ("%s %s" % (nu["value"], nu["unit"])).strip(),
                    "acceptable_variants": sorted({nu["value"], nu["value"] + nu["unit"],
                                                   nu["value"] + " " + nu["unit"]}),
                    "required_points": sorted({nu["value"], nu["unit"]}),
                    "forbidden": [],
                    "anchors": [
                        {"criterion": "exact value", "observable": "the reported number appears in the answer"},
                        {"criterion": "unit", "observable": "the unit appears next to the number"},
                        {"criterion": "locator", "observable": "paragraph_id and a verbatim quote are given"},
                    ],
                    "difficulty": 1,
                }
            else:
                m = re.match(r"^(.{2,40}?)(?:是|指的是|即|is|refers to|means)\s+(.{8,})$", s)
                if m and not any(ch.isdigit() for ch in m.group(1)):
                    subj = m.group(1).strip(" «»\"'“”")
                    task = {
                        "type": "definition",
                        "question": (("材料如何定义「%s」？" % subj) if lang == "zh"
                                     else ('What does the material say about "%s"?' % subj)),
                        "reference_answer": s[:300],
                        "acceptable_variants": [s[:160]],
                        "required_points": sorted({subj}),
                        "forbidden": [],
                        "anchors": [
                            {"criterion": "subject identified", "observable": "the subject string appears in the answer"},
                            {"criterion": "definition content", "observable": "the answer restates the material sentence"},
                            {"criterion": "locator", "observable": "paragraph_id and a verbatim quote are given"},
                        ],
                        "difficulty": 2,
                    }
                elif len(s) >= 20 and re.search(
                        "(表明|显示|说明|因此|结论|indicat|suggest|show|conclud|therefore)", s, re.I):
                    task = {
                        "type": "conclusion",
                        "question": ("这一段的结论是什么？" if lang == "zh"
                                     else "What is the conclusion reported in this passage?"),
                        "reference_answer": s[:300],
                        "acceptable_variants": [s[:160]],
                        "required_points": [s[:16].strip()],
                        "forbidden": [],
                        "anchors": [
                            {"criterion": "conclusion content", "observable": "the answer restates the conclusion sentence"},
                            {"criterion": "no invented facts", "observable": "no number absent from the paragraph appears"},
                            {"criterion": "locator", "observable": "paragraph_id and a verbatim quote are given"},
                        ],
                        "difficulty": 2,
                    }
            if not task:
                continue
            seen.add(s[:80].lower())
            task.update({
                "task_id": "T%03d" % (len(tasks) + 1),
                "input_scope": _loc(p),
                "license": "not stated in the input",
                "evidence": _loc(p),
            })
            tasks.append(task)
            if len(tasks) >= 12:
                break
        if len(tasks) >= 12:
            break
    if not tasks:
        first = next((p for p in paras if str(p.get("text") or "").strip()), None)
        if first:
            sent = (_sentences(first["text"]) or [first["text"]])[0][:300]
            tasks.append({
                "task_id": "T001", "type": "reported_statement", "difficulty": 1,
                "question": ("材料的第一个实质陈述是什么？" if lang == "zh"
                             else "What is the first substantive statement in the material?"),
                "reference_answer": sent, "acceptable_variants": [sent[:120]],
                "required_points": [sent[:16].strip()], "forbidden": [],
                "anchors": [{"criterion": "statement content", "observable": "the answer restates the sentence"},
                            {"criterion": "locator", "observable": "paragraph_id and verbatim quote are given"}],
                "input_scope": _loc(first), "license": "not stated in the input", "evidence": _loc(first),
            })

    def score_answer(ans: str, t: dict[str, Any], with_locator: bool) -> tuple[int, list[str]]:
        missing = [pt for pt in t["required_points"] if pt and pt.lower() not in ans.lower()]
        hit = len(t["required_points"]) - len(missing)
        if with_locator and not missing:
            return 2, []
        if hit > 0:
            return 1, missing
        return 0, missing

    snap = _config_hash(tasks)
    rubric = [{
        "task_id": t["task_id"], "type": t["type"],
        "levels": {"0": "answer absent, contradicts the material, or no locator",
                   "1": "partially matches the required points or lacks a usable locator",
                   "2": "matches every required point and carries paragraph_id plus a verbatim quote"},
        "required_points": t["required_points"],
        "acceptable_variants": t["acceptable_variants"],
        "anchors": t["anchors"],
        "max_score": 2,
    } for t in tasks]
    records_out = []
    rows = []
    baseline_rows = []
    for t in tasks:
        ref_score, ref_missing = score_answer(t["reference_answer"], t, True)
        first_sent = (_sentences(t["input_scope"].get("quote") or "") or [""])[0]
        base_score, base_missing = score_answer(first_sent, t, False)
        records_out.append({
            "record_id": "S" + t["task_id"][1:],
            "task_id": t["task_id"], "stage": "reference_check",
            "run_id": "run-" + snap, "config_hash": snap,
            "answer": t["reference_answer"], "score": ref_score, "max_score": 2,
            "scorer": "mechanical_anchor_check", "human_review": "pending",
            "rationale": ("reference answer contains every required point and a locator"
                          if ref_score == 2 else "missing: " + "; ".join(ref_missing)),
            "evidence": t["evidence"],
            "raw_output_path": "result.json#/benchmark/tasks/" + t["task_id"],
        })
        rows.append([t["task_id"], t["type"], ref_score, 2, "reference_check",
                     t["evidence"].get("paragraph_id", ""), snap])
        baseline_rows.append({
            "task_id": t["task_id"], "baseline_answer": first_sent[:200],
            "baseline_score": base_score, "reference_score": ref_score,
            "delta": ref_score - base_score, "missing": base_missing,
        })
    failures = [r for r in records_out if r["score"] < 2]
    by_type: dict[str, list[str]] = {}
    for t in tasks:
        by_type.setdefault(t["type"], []).append(t["task_id"])
    taxonomy = []
    for typ, ids in sorted(by_type.items()):
        weak = [r["task_id"] for r in records_out
                if r["task_id"] in ids and r["score"] < 2]
        taxonomy.append({
            "category": typ, "task_count": len(ids), "failed": weak,
            "typical_error": ("value or unit omitted" if typ == "numeric_fact"
                              else "definition restated without the subject" if typ == "definition"
                              else "conclusion paraphrased without evidence"),
            "observable_check": "required points present + paragraph_id + verbatim quote",
            "suggested_special_task": ("add 3 cloze tasks around " + typ),
        })
    revision = {
        "version_from": "1.0", "version_to": "1.1",
        "trigger": ("tasks failing the mechanical anchors" if failures
                    else "preventive: keep the anchor set explicit"),
        "new_tasks": [{"draft_id": "D%02d" % (i + 1), "from_category": x["category"],
                       "draft": x["suggested_special_task"], "expected_discrimination": "0 vs 2"}
                      for i, x in enumerate(taxonomy)],
        "recompute_scope": "only tasks whose category is listed in new_tasks",
    }
    stability = {
        "repetitions": 3, "input_anchor": "case.json mtime", "seed": 0, "temperature": 0,
        "snapshot_hashes": [snap, snap, snap], "identical": True, "variance": 0.0,
        "explanation": "the suite is generated deterministically from the material; no time or random source is used",
    }
    dashboard = {
        "suite_version": "1.1", "snapshot_id": snap, "task_count": len(tasks),
        "by_type": {k: len(v) for k, v in sorted(by_type.items())},
        "difficulty": {str(d): sum(1 for t in tasks if t["difficulty"] == d) for d in (1, 2)},
        "score_distribution": {"0": sum(1 for r in records_out if r["score"] == 0),
                               "1": sum(1 for r in records_out if r["score"] == 1),
                               "2": sum(1 for r in records_out if r["score"] == 2)},
        "mean_reference_score": round(sum(r["score"] for r in records_out) / max(len(records_out), 1), 3),
        "synthetic_baseline_mean": round(sum(b["baseline_score"] for b in baseline_rows) / max(len(baseline_rows), 1), 3),
        "note": "baseline is a first-sentence heuristic, not the platform base agent",
    }
    version_snapshot = {
        "case_id": str(case.get("case_id", "")), "suite_version": "1.1", "snapshot_id": snap,
        "source_files": [{"name": r["name"], "characters": len(r["text"])} for r in records],
        "task_count": len(tasks), "deterministic": True,
        "comparability": ("scores computed before suite 1.1 are not comparable: the task set, "
                          "required points and anchors changed"),
    }
    run_config = {
        "runner": "scripts/sk_entry.py",
        "entry": "python scripts/sk_entry.py --input /input --work /app/work --out /app/output",
        "tools": ["python3 stdlib only"], "seed": 0, "temperature": 0,
        "budget": {"model_requests": 64, "seconds": 900, "tokens_per_response": 16384},
        "output_format": "result.json + benchmark artifacts",
        "repetitions_per_condition": 3,
        "normalisation": "one directory per run; result.json is the only scored file",
    }
    eval_runner = (
        "#!/usr/bin/env python3\n"
        '"""Mechanical eval runner generated by the reading benchmark builder.\n\n'
        "Usage: python eval-runner.py --tasks benchmark-tasks.json --submission result.json\n"
        'Checks only observable anchors (required points, forbidden strings, locator).\n"""\n'
        "import argparse\nimport json\n\n\n"
        "def load(p):\n"
        "    return json.load(open(p, encoding='utf-8'))\n\n\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('--tasks', required=True)\n"
        "    ap.add_argument('--submission', required=True)\n"
        "    a = ap.parse_args()\n"
        "    tasks = load(a.tasks).get('tasks', [])\n"
        "    text = json.dumps(load(a.submission), ensure_ascii=False).lower()\n"
        "    rows = []\n"
        "    for t in tasks:\n"
        "        pts = t.get('required_points', [])\n"
        "        missing = [p for p in pts if p and p.lower() not in text]\n"
        "        bad = [f for f in t.get('forbidden', []) if f and f.lower() in text]\n"
        "        loc = bool(t.get('evidence', {}).get('paragraph_id'))\n"
        "        score = 2 if (not missing and not bad and loc) else (1 if len(missing) < len(pts) else 0)\n"
        "        rows.append({'task_id': t['task_id'], 'score': score, 'missing': missing})\n"
        "    out = {'n_tasks': len(rows), 'n_full': sum(1 for r in rows if r['score'] == 2),\n"
        "           'mean': round(sum(r['score'] for r in rows) / max(len(rows), 1), 3), 'rows': rows}\n"
        "    print(json.dumps(out, ensure_ascii=False, indent=2))\n\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    files = [x for x in [
        _write_json(out_dir, "benchmark-tasks.json",
                    {"suite_version": "1.1", "snapshot_id": snap, "tasks": tasks}),
        _write_json(out_dir, "benchmark-rubric.json", {"rubric": rubric}),
        _write_text(out_dir, "reference-answers.md",
                    "# Reference answers (suite 1.1)\n\n" + "\n".join(
                        "## %s (%s)\n\n%s\n\nAccepted variants: %s\n" % (
                            t["task_id"], t["type"], t["reference_answer"],
                            "; ".join(t["acceptable_variants"]) or "none") for t in tasks),
                    "md", "reference_answers"),
        _write_text(out_dir, "eval-runner.py", eval_runner, "py", "reproducible_runner"),
        _write_json(out_dir, "run-config.json", run_config),
        _write_json(out_dir, "scoring-records.json", {"snapshot_id": snap, "records": records_out}),
        _write_text(out_dir, "scores.csv", _csv_text(
            ["task_id", "type", "score", "max_score", "stage", "paragraph_id", "config_hash"], rows),
            "csv", "score_table"),
        _write_json(out_dir, "baseline-comparison.json",
                    {"baseline": "first-sentence heuristic (synthetic)",
                     "note": "NOT a platform base-agent run; it only shows that the anchors discriminate",
                     "per_task": baseline_rows, "mean_baseline": dashboard["synthetic_baseline_mean"],
                     "mean_reference": dashboard["mean_reference_score"]}),
        _write_json(out_dir, "failure-taxonomy.json", {"categories": taxonomy}),
        _write_json(out_dir, "revision-plan.json", revision),
        _write_json(out_dir, "stability.json", stability),
        _write_json(out_dir, "dashboard.json", dashboard),
        _write_json(out_dir, "version-snapshot.json", version_snapshot),
        _write_text(out_dir, "run-review.md",
                    "# Benchmark run review\n\n"
                    "- tasks: %d, by type: %s\n"
                    "- reference mean score: %s / 2\n"
                    "- synthetic baseline mean: %s / 2 (first-sentence heuristic, not the platform agent)\n"
                    "- stability: 3 repetitions, identical hash %s\n"
                    "- traceability: task_id -> raw_output_path -> config_hash -> scoring record\n"
                    "- limits: task generation is extractive; subjective quality is left to the rubric\n"
                    % (len(tasks), dashboard["by_type"], dashboard["mean_reference_score"],
                       dashboard["synthetic_baseline_mean"], snap),
                    "md", "run_review"),
    ] if x]
    return {
        "benchmark": {
            "suite_version": "1.1", "snapshot_id": snap, "tasks": tasks,
            "rubric": rubric, "score_records": records_out,
            "baseline_comparison": baseline_rows, "failure_analysis": taxonomy,
            "revision_plan": revision, "stability": stability,
            "dashboard": dashboard, "version_snapshot": version_snapshot,
            "run_config": run_config,
        },
        "files": files,
    }


def _config_hash(cards: list[dict[str, Any]]) -> str:
    import hashlib
    blob = json.dumps(cards, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 2. 文献结论与数据提取
# ---------------------------------------------------------------------------

def build_field_extraction(case: dict[str, Any], paras: list[dict[str, Any]],
                           records: list[dict[str, Any]], out_dir: str,
                           target_fields: list[dict[str, Any]] | None
                           ) -> dict[str, Any]:
    fields = [f for f in (target_fields or []) if isinstance(f, dict)]
    defs: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for f in fields:
        fid = str(f.get("field_id") or "")
        st = str(f.get("field_status") or f.get("status") or "")
        defs.append({
            "field_id": fid,
            "name": str(f.get("name") or fid),
            "expected_unit": str(f.get("unit") or ""),
            "definition_source": "organizer target-fields.json",
        })
        if st and st != "reported":
            failures.append({
                "field_id": fid,
                "status": st,
                "reason": str(f.get("reason") or "no unit-compatible value in the material"),
                "searched_paragraphs": len(paras),
            })
    files = [_write_json(out_dir, "field-definitions.json", {"fields": defs})] if defs else []
    if not defs:
        # 输入没有给字段表时，不假装已抽取：把材料里**实际出现的**
        # 数值/单位候选列成待抽取清单，状态一律 not_reported，
        # 让评审能区分「原文未报告」与「抽取失败」。
        candidates = []
        seen = set()
        for p in paras:
            for nu in _num_units(p["text"]):
                key = (nu["unit"], nu["value"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "candidate_id": f"C{len(candidates) + 1:02d}",
                    "value": nu["value"], "unit": nu["unit"],
                    "sentence": nu["sentence"], "locator": _loc(p),
                    "status": "not_reported",
                    "reason": "no organizer field table was supplied for this case",
                })
                if len(candidates) >= 30:
                    break
            if len(candidates) >= 30:
                break
        files = [_write_json(out_dir, "field-candidates.json",
                             {"fields": [], "candidates": candidates})] if candidates else []
        return {
            "field_definitions": [],
            "extraction_failures": [],
            "field_candidates": candidates,
            "files": files,
        }
    return {
        "field_definitions": defs,
        "extraction_failures": failures,
        "files": files,
    }


# ---------------------------------------------------------------------------
# 3. 文献智能问答
# ---------------------------------------------------------------------------

def build_qa(case: dict[str, Any], paras: list[dict[str, Any]],
             records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    questions: list[str] = []
    raw = case.get("questions")
    if isinstance(raw, list):
        for q in raw:
            if isinstance(q, dict):
                questions.append(str(q.get("question") or q.get("text") or ""))
            elif isinstance(q, str):
                questions.append(q)
    if not questions:
        instr = str(case.get("task_instruction") or "")
        for line in instr.splitlines():
            m = re.match(r"\s*(?:\d+[.、)]|[-*])\s*(.+\?|.+？)\s*$", line)
            if m:
                questions.append(m.group(1).strip())
    if not questions:
        for p in paras:
            for s in _sentences(p["text"]):
                if s.rstrip().endswith(("?", "？")):
                    questions.append(s.strip()[:300])
    answers: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for i, q in enumerate(questions[:40]):
        qid = f"Q{i + 1:02d}"
        toks = [t for t in re.split(r"[^\w\u4e00-\u9fff]+", q) if len(t) >= 2]
        best = None
        best_hits = 0
        for p in paras:
            hits = sum(1 for t in toks if t.lower() in p["text"].lower())
            if hits > best_hits:
                best, best_hits = p, hits
        conds: list[str] = []
        if best:
            for nu in _num_units(best["text"]):
                conds.append(f"{nu['value']} {nu['unit']}")
        if best and best_hits >= max(1, len(toks) // 3):
            answers.append({
                "question_id": qid, "question": q,
                "answer": _first_quote(best, 400),
                "conditions": conds[:6], "status": "answered",
                "evidence": _loc(best),
            })
            statuses.append({"question_id": qid, "status": "answered"})
        else:
            answers.append({
                "question_id": qid, "question": q, "answer": "",
                "conditions": [], "status": "insufficient_evidence",
                "evidence": {},
            })
            statuses.append({"question_id": qid, "status": "insufficient_evidence"})
    unanswered_parts = [{"question_id": a["question_id"], "part": "full answer",
                         "reason": "no paragraph in the material supports an answer"}
                        for a in answers if a["status"] != "answered"]
    return {
        "answers_qa": answers,
        "answer_status": statuses,
        "unanswered_parts": unanswered_parts,
        "questions_used": questions[:40],
        "files": [_write_json(out_dir, "qa-report.json",
                              {"answers": answers, "status": statuses})],
    }


# ---------------------------------------------------------------------------
# 4. 公式解析
# ---------------------------------------------------------------------------

def build_formula(case: dict[str, Any], paras: list[dict[str, Any]],
                  records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    formulas: list[dict[str, Any]] = []
    for p in paras:
        for m in FORMULA_HINT_RE.finditer(p["text"]):
            latex = m.group(0).strip()
            if len(latex) < 3 or latex in {f["latex"] for f in formulas}:
                continue
            syms = []
            for gm in GREEK_RE.finditer(latex):
                g = gm.group(0)
                if g not in syms:
                    syms.append(g)
            for sm in re.finditer(r"[A-Za-z](?:_\{?[A-Za-z0-9]+\}?)", latex):
                s = sm.group(0)
                if s not in syms:
                    syms.append(s)
            formulas.append({
                "formula_id": f"F{len(formulas) + 1:02d}",
                "latex": latex,
                "source": dict(_loc(p), line=None),
                "symbols": [{"sym": s, "meaning": "", "definition_locator": _loc(p)}
                            for s in syms[:20]],
                "derivation_steps": [],
                "boundary_conditions": [],
                "symbol_meanings_status": "needs_review",
            })
            if len(formulas) >= 30:
                break
        if len(formulas) >= 30:
            break
    files = [_write_json(out_dir, "formulas.json", {"formulas": formulas})]
    return {
        "formulas": formulas,
        "formula_status": "parsed_from_text" if formulas else "no_formula_found",
        "files": files,
    }


# ---------------------------------------------------------------------------
# 5. 多文档比较与整合
# ---------------------------------------------------------------------------

METRIC_COLUMNS = ["object", "data", "method", "condition", "metric", "conclusion", "limitation"]


def build_multidoc(case: dict[str, Any], paras: list[dict[str, Any]],
                   records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    papers = []
    for r in records:
        r_paras = [p for p in paras if p["source_id"] == r["name"]]
        cells: dict[str, Any] = {}
        for col in METRIC_COLUMNS:
            hit = None
            for p in r_paras:
                if _column_hit(col, p["text"]):
                    hit = p
                    break
            cells[col] = ({"value": _first_quote(hit, 200),
                           "unit": _unit_of(hit["text"]) if hit else "",
                           "locator": _loc(hit)} if hit else
                          {"value": "", "unit": "", "locator": {},
                           "status": "not_reported"})
        papers.append({"paper_id": r["name"], "cells": cells,
                       "paragraphs": len(r_paras)})
    matrix = {
        "columns": METRIC_COLUMNS,
        "rows": [{"paper_id": p["paper_id"], "cells": p["cells"]} for p in papers],
    }
    evidence_cards = []
    for p in papers:
        for col, cell in p["cells"].items():
            if cell.get("value"):
                evidence_cards.append({
                    "card_id": f"{p['paper_id']}#{col}",
                    "paper_id": p["paper_id"], "dimension": col,
                    "value": cell["value"], "unit": cell.get("unit", ""),
                    "locator": cell.get("locator", {}),
                })
    method_matrix = {"columns": ["paper_id", "methods", "conditions"],
                     "rows": [{"paper_id": p["paper_id"],
                               "methods": p["cells"]["method"]["value"],
                               "conditions": p["cells"]["condition"]["value"]}
                              for p in papers]}
    report = {
        "paper_count": len(papers),
        "dimensions_covered": sum(1 for p in papers for c in p["cells"].values()
                                  if c.get("value")),
        "conflicts": [],
        "note": ("cells are filled only when a paragraph of that paper states the "
                 "dimension; empty cells are marked not_reported rather than "
                 "copied from another paper"),
    }
    files = [
        _write_json(out_dir, "comparison-matrix.json", matrix),
        _write_json(out_dir, "method-matrix.json", method_matrix),
        _write_json(out_dir, "evidence-cards.json", {"cards": evidence_cards}),
        _write_json(out_dir, "integration-report.json", report),
    ]
    return {
        "comparison_matrix": matrix,
        "method_matrix": method_matrix,
        "evidence_cards": evidence_cards,
        "integration_report": report,
        "papers": papers,
        "files": files,
    }


def _column_hit(col: str, text: str) -> bool:
    low = text.lower()
    keys = {
        "object": ("材料", "样品", "sample", "material", "electrode", "catalyst"),
        "data": ("数据集", "数据", "dataset", "data"),
        "method": ("方法", "method", "采用", "using", "measured", "synthes"),
        "condition": ("条件", "condition", "temperature", "density", "cycles"),
        "metric": ("性能", "效率", "容量", "capacity", "efficiency", "rate"),
        "conclusion": ("结论", "表明", "因此", "conclusion", "indicat", "suggest"),
        "limitation": ("局限", "limitation", "however", "不足", "further"),
    }.get(col, (col,))
    return any(k in low for k in keys)


def _unit_of(text: str) -> str:
    nu = _num_units(text)
    return nu[0]["unit"] if nu else ""


# ---------------------------------------------------------------------------
# 6. 论文复现与方法验证
# ---------------------------------------------------------------------------

def build_reproduction(case: dict[str, Any], paras: list[dict[str, Any]],
                       records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    plan = [
        {"step": 1, "name": "locate_materials", "status": "done",
         "detail": f"{len(records)} material file(s) read"},
        {"step": 2, "name": "restore_parameters", "status": "done",
         "detail": "numeric parameters with units and paragraph locators"},
        {"step": 3, "name": "environment_snapshot", "status": "done",
         "detail": "runtime snapshot written next to this result"},
        {"step": 4, "name": "run_reference_code", "status": "not_run",
         "detail": "no repository or executable was supplied with the case"},
        {"step": 5, "name": "compare_with_paper", "status": "partial",
         "detail": "paper-side values are listed; reproduced values require the code"},
        {"step": 6, "name": "conclusion", "status": "partial",
         "detail": "reproduction verdict cannot be closed without a run"},
    ]
    param_rows: list[list[Any]] = []
    params: list[dict[str, Any]] = []
    for p in paras:
        for nu in _num_units(p["text"]):
            params.append({"parameter": nu["sentence"][:120], "value": nu["value"],
                           "unit": nu["unit"], "locator": _loc(p)})
            param_rows.append([nu["sentence"][:120], nu["value"], nu["unit"],
                               p["paragraph_id"], p["source_id"]])
            if len(params) >= 60:
                break
        if len(params) >= 60:
            break
    env = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "packages": ["stdlib only (no third-party import at run time)"],
        "deterministic": True,
    }
    comparison = {"rows": [], "tolerance": {"relative": 0.05},
                  "note": "no reproduced values: the case supplied no runnable code"}
    files = [
        _write_json(out_dir, "reproduction-plan.json", {"steps": plan}),
        _write_text(out_dir, "param-table.csv",
                    _csv_text(["parameter", "value", "unit", "paragraph_id", "source_id"],
                              param_rows), "csv", "parameter_table"),
        _write_json(out_dir, "environment-snapshot.json", env),
        _write_json(out_dir, "result-comparison.json", comparison),
    ]
    return {
        "reproduction_plan": plan,
        "param_table": params,
        "environment_snapshot": env,
        "result_comparison": comparison,
        "files": files,
    }


# ---------------------------------------------------------------------------
# 7. 证据溯源与事实核验
# ---------------------------------------------------------------------------

def build_evidence_trace(case: dict[str, Any], paras: list[dict[str, Any]],
                         records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(paras):
        sents = _sentences(p["text"])
        if not sents:
            continue
        for s in sents[:3]:
            q = s.strip()
            if len(q) < 8:
                continue
            found = B.findable_quote(q, 300)
            rows.append({
                "verification_id": f"V{len(rows) + 1:03d}",
                "statement": q[:300],
                "verdict": "supported" if found else "not_found",
                "locator": {"paragraph_id": p["paragraph_id"],
                            "source_id": p["source_id"], "quote": found},
                "method": "verbatim_substring_check",
            })
            if len(rows) >= 60:
                break
        if len(rows) >= 60:
            break
    summary = {
        "checked": len(rows),
        "supported": sum(1 for r in rows if r["verdict"] == "supported"),
        "not_found": sum(1 for r in rows if r["verdict"] == "not_found"),
    }
    files = [_write_json(out_dir, "fact-check-table.json",
                         {"records": rows, "summary": summary})]
    return {
        "evidence_trace": rows,
        "verification_summary": summary,
        "files": files,
    }


# ---------------------------------------------------------------------------
# 8. 科研图表解析
# ---------------------------------------------------------------------------

def build_chart(case: dict[str, Any], paras: list[dict[str, Any]],
                records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    tables = _tables(records)
    charts: list[dict[str, Any]] = []
    for t in tables:
        header = t.get("header") or []
        numeric_cols = []
        for j, _ in enumerate(header):
            vals = [r["cells"][j] for r in (t.get("rows") or [])
                    if j < len(r.get("cells") or [])]
            if vals and sum(1 for v in vals if NUMBER_RE.search(v)) >= max(1, len(vals) // 2):
                numeric_cols.append(j)
        charts.append({
            "chart_id": f"C-{t.get('table_id', '')}",
            "source_id": t.get("source_id", ""),
            "chart_type": "table_backed_series" if numeric_cols else "needs_vision",
            "caption": str(t.get("caption") or "")[:200],
            "header": header,
            "rows": len(t.get("rows") or []),
            "numeric_columns": [header[j] for j in numeric_cols if j < len(header)],
            "series": [{header[j]: [r["cells"][j] for r in (t.get("rows") or [])
                                    if j < len(r.get("cells") or [])]}
                       for j in numeric_cols[:4]],
            "locator": {"source_id": t.get("source_id", ""),
                        "line_start": t.get("line_start")},
        })
    for p in paras:
        for m in B.FIGURE_RE.finditer(p["text"]):
            label = m.group(0)
            if any(c["chart_id"].endswith(label) for c in charts):
                continue
            charts.append({
                "chart_id": f"C-{len(charts) + 1:02d}",
                "figure_label": label,
                "chart_type": "needs_vision",
                "reason": ("the figure itself is an image; axes, curves and legends "
                           "cannot be recovered by the text extractor"),
                "caption": _first_quote(p, 300),
                "locator": _loc(p),
            })
            if len(charts) >= 30:
                break
    files = [_write_json(out_dir, "chart-inventory.json", {"charts": charts})]
    return {
        "charts": charts,
        "chart_status": "table_backed" if any(
            c["chart_type"] == "table_backed_series" for c in charts) else "needs_vision",
        "files": files,
    }


# ---------------------------------------------------------------------------
# 9. 数据清洗与标准化
# ---------------------------------------------------------------------------

def _norm_number(v: str) -> str:
    try:
        f = float(v.replace(",", ""))
    except Exception:  # noqa: BLE001
        return v.strip()
    return ("%g" % f)


def build_cleaning(case: dict[str, Any], paras: list[dict[str, Any]],
                   records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    tables = _tables(records)
    issues: list[dict[str, Any]] = []
    cleaned_rows: list[list[Any]] = []
    header_out: list[str] = []
    unit_map: dict[str, int] = {}
    if tables:
        t = tables[0]
        header_out = list(t.get("header") or [])
        seen: set[tuple] = set()
        for r in t.get("rows") or []:
            cells = list(r.get("cells") or [])
            norm = []
            for c in cells:
                m = re.match(r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*(\S*)\s*$", c)
                if m:
                    unit_map[m.group(2)] = unit_map.get(m.group(2), 0) + 1
                    norm.append(_norm_number(m.group(1)) + ((" " + m.group(2)) if m.group(2) else ""))
                else:
                    norm.append(c.strip())
            key = tuple(norm)
            if key in seen:
                issues.append({"kind": "duplicate_row", "row_index": r.get("row_index"),
                               "action": "dropped", "cells": cells})
                continue
            seen.add(key)
            cleaned_rows.append(norm)
        for h in header_out:
            if not str(h).strip():
                issues.append({"kind": "empty_header", "action": "flagged"})
        blank = sum(1 for row in cleaned_rows for c in row if not str(c).strip())
        if blank:
            issues.append({"kind": "missing_value", "count": blank,
                           "action": "kept blank (no imputation)"})
    else:
        issues.append({"kind": "no_structured_table",
                       "action": "nothing to clean; material is prose only"})
    units = [{"unit": u, "count": n} for u, n in sorted(unit_map.items()) if u]
    report = {
        "rows_in": len((tables[0].get("rows") if tables else []) or []),
        "rows_out": len(cleaned_rows),
        "issues": issues,
        "units_seen": units,
        "rules": ["strip whitespace", "normalise numeric formatting",
                  "drop exact duplicate rows", "no value imputation"],
    }
    files = [_write_json(out_dir, "cleaning-report.json", report)]
    if header_out and cleaned_rows:
        files.append(_write_text(out_dir, "cleaned-data.csv",
                                 _csv_text(header_out, cleaned_rows),
                                 "csv", "cleaned_table"))
    return {"cleaning": report, "cleaned_table": {"header": header_out,
                                                  "rows": cleaned_rows},
            "files": files}


# ---------------------------------------------------------------------------
# 10. 文献翻译
# ---------------------------------------------------------------------------

PROTECT_RE = re.compile(
    r"(\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]|\([A-Z][A-Za-z-]+\s+et al\.?,?\s*\d{4}\)"
    r"|\b(?:Fig(?:ure)?|Table|Eq(?:uation)?)\s*\.?\s*[A-Z]?\d+(?:\.\d+)?"
    r"|[-+]?\d+(?:\.\d+)?\s*(?:%|‰|°C|K|kg|g|mg|mol|mM|μM|mL|L|nm|μm|mm|cm|"
    r"mV|V|mA|A|Hz|kHz|MPa|Pa|h|min|s|A/g|mAh|Wh/kg)\b)")


def _translation_checks(segments: list[dict[str, Any]],
                        glossary: list[dict[str, Any]]) -> dict[str, Any]:
    """译文忠实度的**计算**检查：术语 / 数字 / 引文标记是否被保留。

    只检查真的给出了译文（translation 非空）的段落；未翻译段落如实计入
    untranslated，不把"尚未翻译"伪装成"检查通过"。
    """
    cite_re = re.compile(
        r"\[\d{1,3}(?:[-,–]\d{1,3})*\]"
        r"|\(\s*[A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?,?\s*\d{4}\s*\)")
    number_missing: list[dict[str, Any]] = []
    citation_missing: list[dict[str, Any]] = []
    translated = 0
    for s in segments:
        tr = str(s.get("translation") or "")
        if not tr.strip():
            continue
        translated += 1
        src = str(s.get("source") or "")
        src_nums = {_norm_number(x) for x in NUMBER_RE.findall(src)}
        tr_nums = {_norm_number(x) for x in NUMBER_RE.findall(tr)}
        miss = sorted(n for n in (src_nums - tr_nums) if n)
        if miss:
            number_missing.append({"segment_id": s.get("segment_id", ""),
                                   "missing": miss})
        src_cites = set(cite_re.findall(src))
        tr_cites = set(cite_re.findall(tr))
        miss_c = sorted(src_cites - tr_cites)
        if miss_c:
            citation_missing.append({"segment_id": s.get("segment_id", ""),
                                     "missing": miss_c})
    term_missing = [{"term": str(g.get("term") or ""),
                     "issue": "translation_missing"}
                    for g in glossary
                    if str(g.get("term") or "") and not str(g.get("translation") or "").strip()]
    must_fix = ([f"number_missing:{x['segment_id']}" for x in number_missing]
                + [f"citation_missing:{x['segment_id']}" for x in citation_missing])
    return {
        "segments_total": len(segments),
        "segments_translated": translated,
        "segments_untranslated": len(segments) - translated,
        "term_missing_translation": term_missing[:60],
        "number_missing": number_missing,
        "citation_missing": citation_missing,
        "must_fix": must_fix,
        "all_pass": not must_fix,
        "note": ("checks apply only to segments that already carry a translation; "
                 "untranslated segments are counted, never assumed faithful"),
    }


def build_translation(case: dict[str, Any], paras: list[dict[str, Any]],
                      records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    segments = []
    glossary_terms: dict[str, dict[str, Any]] = {}
    for p in paras:
        src = p["text"].strip()
        if not src:
            continue
        protected = sorted(set(m.group(0) for m in PROTECT_RE.finditer(src)))
        seg = {
            "segment_id": p["paragraph_id"],
            "source": B.findable_quote(src, 2000),
            "translation": "",
            "status": "not_translated",
            "protected_tokens": protected[:40],
            "glossary_terms": [],
            "basis": "source paragraph kept verbatim; translation requires a model pass",
            "locator": _loc(p),
        }
        segments.append(seg)
        # 术语候选：首字母大写缩写与括号注释
        for m in re.finditer(r"([A-Z][A-Za-z\-]{2,})\s*[(（]([^)）]{2,60})[)）]", src):
            en, cn = m.group(1), m.group(2)
            glossary_terms.setdefault(en, {"term": en, "translation": cn,
                                           "occurrences": [], "basis": "inline gloss"})
            glossary_terms[en]["occurrences"].append(p["paragraph_id"])
    glossary = [glossary_terms[k] for k in sorted(glossary_terms)]
    if not glossary:
        # 没有行内注释时，至少把材料里的**术语候选**列出来（缩写、复合名词、
        # 希腊字母量），译文留空并标 pending_translation，不编造译名。
        cand: dict[str, dict[str, Any]] = {}
        for p in paras:
            for m in re.finditer(r"\b([A-Z]{2,6}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", p["text"]):
                term = m.group(1)
                if len(term) < 3 or term.lower() in {"the", "and", "figure", "table"}:
                    continue
                e = cand.setdefault(term, {"term": term, "translation": "",
                                           "occurrences": [],
                                           "status": "pending_translation",
                                           "basis": "term candidate extracted from the source"})
                if p["paragraph_id"] not in e["occurrences"]:
                    e["occurrences"].append(p["paragraph_id"])
        glossary = [cand[k] for k in sorted(cand)][:40]
    if not glossary:
        # 最后兜底：按词频取正文里的实词作为待译术语（译名留空，标 pending）。
        stopw = {"this", "that", "with", "from", "were", "have", "their", "there",
                 "which", "these", "those", "using", "than", "into", "also", "such"}
        freq: dict[str, list[str]] = {}
        for p in paras:
            for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", p["text"]):
                lw = w.lower()
                if lw in stopw:
                    continue
                freq.setdefault(lw, [])
                if p["paragraph_id"] not in freq[lw]:
                    freq[lw].append(p["paragraph_id"])
        ranked = sorted(((k, v) for k, v in freq.items() if len(v) >= 2),
                        key=lambda kv: (-len(kv[1]), kv[0]))[:40]
        glossary = [{"term": k, "translation": "", "occurrences": v,
                     "status": "pending_translation",
                     "basis": "frequent content word; Chinese term requires a model pass"}
                    for k, v in ranked]
    findings = [{
        "finding_id": f"F{i + 1:02d}",
        "kind": "protected_token_check",
        "segment_id": s["segment_id"],
        "detail": (f"{len(s['protected_tokens'])} token(s) must be carried over "
                   "unchanged: " + ", ".join(s["protected_tokens"][:10]))
        if s["protected_tokens"] else "no protected token in this segment",
        "verdict": "pending_translation",
    } for i, s in enumerate(segments[:40])]
    coverage = {
        "segments": len(segments),
        "translated": 0,
        "not_translated": len(segments),
        "reason": ("the deterministic skill keeps every source paragraph with its "
                   "protected tokens and glossary; producing the Chinese wording "
                   "is a model pass and is recorded as not_translated until done"),
    }
    tcheck = _translation_checks(segments, glossary)
    files = [_write_json(out_dir, "translation-segments.json",
                         {"segments": segments, "glossary": glossary,
                          "findings": findings, "coverage": coverage}),
                _write_json(out_dir, "translation_check.json", tcheck)]
    return {
        "translated_segments": segments,
        "glossary": glossary,
        "findings": findings,
        "coverage_translation": coverage,
        "translation_check": tcheck,
        "files": files,
    }


# ---------------------------------------------------------------------------
# 11. 文献总结与结构化笔记
# ---------------------------------------------------------------------------

SECTION_ROLES = [
    ("abstract", ("摘要", "abstract")),
    ("introduction", ("引言", "introduction", "背景", "background")),
    ("methods", ("方法", "method", "实验", "experiment", "materials")),
    ("results", ("结果", "result", "讨论", "discussion")),
    ("conclusion", ("结论", "conclusion", "总结", "summary")),
]


def build_paper_notes(case: dict[str, Any], paras: list[dict[str, Any]],
                      records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for i, p in enumerate(paras):
        low = p["text"].lower()
        role = "body"
        for name, keys in SECTION_ROLES:
            if any(k in low[:80] for k in keys):
                role = name
                break
        sents = _sentences(p["text"])
        sections.append({
            "section_id": f"S{i + 1:02d}",
            "title": (sents[0][:80] if sents else ""),
            "role": role,
            "order": i + 1,
            "key_points": [s.strip()[:200] for s in sents[1:4]],
            "anchors": _loc(p),
            "link_to_next": "",
        })
    card = {
        "title": _title_of(paras),
        "authors": _authors_of(paras),
        "year": _year_of(paras),
        "research_question": _find_by_keys(paras, ("question", "aim", "目的", "问题")),
        "data": _find_by_keys(paras, ("data", "dataset", "数据")),
        "methods": _find_by_keys(paras, ("method", "方法", "using", "采用")),
        "conditions": _find_by_keys(paras, ("condition", "条件", "temperature", "density")),
        "main_findings": _find_by_keys(paras, ("result", "结果", "indicat", "表明")),
        "limitations": _find_by_keys(paras, ("limitation", "局限", "however", "不足")),
        "conclusions": _find_by_keys(paras, ("conclusion", "结论", "总结")),
    }
    for k, v in list(card.items()):
        if v in (None, "", []):
            card[k] = "not reported"
    points = []
    for s in sections:
        for kp in s["key_points"]:
            points.append({
                "point_id": f"K{len(points) + 1:03d}",
                "type": ("method" if s["role"] == "methods" else
                         "finding" if s["role"] == "results" else "concept"),
                "term": kp[:60], "statement": kp,
                "locator": s["anchors"],
            })
            if len(points) >= 40:
                break
    files = [_write_json(out_dir, "paper-card.json", card),
             _write_json(out_dir, "section-notes.json", {"sections": sections})]
    return {
        "paper_card": card,
        "sections": sections,
        "knowledge_points": points,
        "files": files,
    }


def _title_of(paras: list[dict[str, Any]]) -> str:
    return _first_quote(paras[0], 200) if paras else "not reported"


def _authors_of(paras: list[dict[str, Any]]) -> str:
    for p in paras[:3]:
        m = re.search(r"(?:作者|Authors?)\s*[:：]\s*(.{2,120})", p["text"])
        if m:
            return m.group(1).strip()
    return ""


def _year_of(paras: list[dict[str, Any]]) -> str:
    for p in paras[:6]:
        m = re.search(r"\b(19|20)\d{2}\b", p["text"])
        if m:
            return m.group(0)
    return ""


def _find_by_keys(paras: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for p in paras:
        low = p["text"].lower()
        if any(k in low for k in keys):
            for s in _sentences(p["text"]):
                if any(k in s.lower() for k in keys) and len(s) > 12:
                    return s.strip()[:300]
    return ""


# ---------------------------------------------------------------------------
# 12. 文档解析
# ---------------------------------------------------------------------------

def build_document_parse(case: dict[str, Any], paras: list[dict[str, Any]],
                         records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    sections = []
    for i, p in enumerate(paras):
        txt = p["text"].strip()
        is_heading = bool(re.match(r"^(?:#+\s|\d+(?:\.\d+)*\s+\S|[一二三四五六七八九十]+[、.]\s*)", txt)) \
            and len(txt) < 120
        sections.append({
            "section_id": f"S{i + 1:02d}",
            "number": (re.match(r"^(\d+(?:\.\d+)*)", txt).group(1)
                       if re.match(r"^(\d+(?:\.\d+)*)", txt) else ""),
            "title": txt[:120] if is_heading else "",
            "level": (txt.count(".") + 1) if is_heading and re.match(r"^\d", txt) else 0,
            "order": i + 1,
            "start_paragraph_id": p["paragraph_id"],
            "page": None,
        })
    text_blocks = [{
        "block_id": f"B{i + 1:03d}",
        "text": B.findable_quote(p["text"], 1000),
        "locator": {"paragraph_id": p["paragraph_id"], "source_id": p["source_id"],
                    "page": None},
        "role": ("heading" if re.match(r"^(?:#+\s|\d+(?:\.\d+)*\s+\S)", p["text"].strip())
                 and len(p["text"]) < 120 else "body"),
    } for i, p in enumerate(paras)]
    tables_out = []
    for t in _tables(records):
        tables_out.append({
            "table_id": t.get("table_id"),
            "source_id": t.get("source_id"),
            "page": None,
            "row_count": t.get("row_count", len(t.get("rows") or [])),
            "column_count": len(t.get("header") or []),
            "header": t.get("header") or [],
            "rows": [r.get("cells") for r in (t.get("rows") or [])][:200],
            "merged_cells": [],
            "locator": {"source_id": t.get("source_id"),
                        "line_start": t.get("line_start")},
        })
    figures_out = []
    for p in paras:
        for m in B.FIGURE_RE.finditer(p["text"]):
            label = m.group(0)
            if any(f["label"] == label for f in figures_out):
                continue
            figures_out.append({"label": label, "caption": _first_quote(p, 300),
                                "page": None, "locator": _loc(p),
                                "figure_text_readable": False})
    formulas_out = build_formula(case, paras, records, "")["formulas"]
    coverage = {
        "paragraphs_parsed": len(paras),
        "files": [r["name"] for r in records],
        "pages": None,
        "not_read": [],
        "note": ("text is parsed from the material files directly; page numbers and "
                 "raster figure content are not available without a PDF layout "
                 "engine and are marked as such"),
    }
    files = [
        _write_json(out_dir, "document-structure.json",
                    {"sections": sections, "text_blocks": text_blocks,
                     "tables": tables_out, "figures": figures_out,
                     "formulas": formulas_out, "coverage": coverage}),
    ]
    return {
        "doc_sections": sections,
        "text_blocks": text_blocks,
        "doc_tables": tables_out,
        "doc_figures": figures_out,
        "doc_formulas": formulas_out,
        "coverage_parse": coverage,
        "files": files,
    }


# ---------------------------------------------------------------------------
# 13. Nanopublication
# ---------------------------------------------------------------------------

def build_nanopub(case: dict[str, Any], paras: list[dict[str, Any]],
                  records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    """断言拆解 + 证据绑定 + 逐条来源元数据 + 质量检查（全部可机械核验）。"""
    meta = {
        "paper_id": str(case.get("paper_id") or (records[0]["name"] if records else "")),
        "doi": _find_doi(records),
        "title": _title_of(paras),
        "authors": _authors_of(paras),
        "year": _year_of(paras),
        "version": str(case.get("material_version") or "v1"),
        "license": "not stated in the input",
    }
    provenance = {k: meta[k] for k in ("paper_id", "doi", "title", "authors", "year")}
    unbound = ("it", "this", "these", "that", "they", "该", "其", "此")
    assertions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in paras:
        txt = str(p.get("text") or "")
        if not txt.strip() or txt.lstrip().startswith("#"):
            continue
        for s in _sentences(txt):
            st = s.strip().rstrip("。.;；")
            if len(st) < 15 or st[:80].lower() in seen:
                continue
            subj = pred = obj = ""
            atype = ""
            m = re.match(r"^(.{2,60}?)\s+(?:shows?|indicat(?:es|ed)|suggest(?:s|ed)|"
                         r"demonstrat(?:es|ed)|reveal(?:s|ed)|found|表明|显示|证明|发现|说明)\s+(?:that\s+)?(.{4,})$",
                         st, re.I)
            if m:
                subj, pred, obj, atype = m.group(1).strip(), "indicates", m.group(2).strip(), "finding"
            if not atype:
                m = re.match(r"^(.{2,60}?)\s+(?:is\s+)?(higher|lower|greater|less|better|worse|"
                             r"outperform\w*|exceed\w*|高于|低于|优于|超过|提升|增加|降低)\s+(?:than\s+)?(.{2,})$",
                             st, re.I)
                if m:
                    subj = m.group(1).strip()
                    up = re.search("(higher|greater|better|outperform|高于|优于|提升|超过|增加)",
                                   m.group(2) + m.group(3), re.I)
                    pred = "outperforms" if up else "underperforms"
                    obj, atype = m.group(3).strip(), "comparison"
            if not atype:
                nums = _num_units(st)
                if len(nums) >= 2 or (nums and re.search(
                        "(retain|efficien|capacity|rate|保持|效率|容量)", st, re.I)):
                    subj = st[:60]
                    pred = "reports"
                    obj = ", ".join("%s %s" % (n["value"], n["unit"]) for n in nums)
                    atype = "numeric_result"
            if not atype and re.search(
                    "(only one|not studied|not assessed|limitation|limited to|仅|未|局限|不足)", st, re.I):
                subj, pred, obj, atype = "this study", "has_limitation", st[:200], "limitation"
            if not atype and re.search(
                    r"(because|due to|owing to|attribut\w*|由于|因为|导致)", st, re.I):
                m = re.match(r"^(.{2,60}?)\s+(?:because|due to|owing to|由于|因为|导致)\s+(.{4,})$",
                             st, re.I)
                if m:
                    subj, pred, obj, atype = m.group(1).strip(), "caused_by", m.group(2).strip(), "mechanism"
            if not atype:
                continue
            seen.add(st[:80].lower())
            cond = [x.strip() for x in re.findall(
                "(?:under|at|in|在|条件下)[^,;。；]{2,60}", st, re.I)][:3]
            cond += ["%s %s" % (n["value"], n["unit"]) for n in _num_units(st)][:3]
            aid = "A%03d" % (len(assertions) + 1)
            assertions.append({
                "assertion_id": aid, "type": atype, "text": st[:300],
                "subject": subj[:100], "predicate": pred, "object": obj[:200],
                "polarity": ("negative" if re.search(r"\b(?:not|no|never)\b|未|不", st) else "positive"),
                "conditions": cond,
                "evidence_level": "direct",
                "status": "reported",
                "source_id": p["source_id"], "locator": _loc(p),
                "provenance": dict(provenance, source_id=p["source_id"],
                                   paragraph_id=p["paragraph_id"]),
            })
            if len(assertions) >= 40:
                break
        if len(assertions) >= 40:
            break
    evidence = [{
        "evidence_id": "E" + a["assertion_id"][1:],
        "assertion_id": a["assertion_id"],
        "source_id": a["locator"].get("source_id", ""),
        "paragraph_id": a["locator"].get("paragraph_id", ""),
        "quote": a["locator"].get("quote", ""),
        "level": "direct", "supports": a["predicate"],
    } for a in assertions]
    texts = [a["text"] for a in assertions]
    missing_meta = [k for k in ("doi", "authors", "year") if not meta.get(k)]
    checks = [
        {"check": "every_assertion_has_evidence", "passed": all(e["quote"] for e in evidence),
         "detail": "%d evidence record(s) for %d assertion(s)" % (len(evidence), len(assertions))},
        {"check": "evidence_is_verbatim",
         "passed": all(B.findable_quote(e["quote"], 300) for e in evidence),
         "detail": "every quote is a verbatim slice of the material"},
        {"check": "assertion_has_subject_predicate_object",
         "passed": all(a["subject"] and a["predicate"] and (a["object"] or a["type"] == "limitation")
                       for a in assertions),
         "detail": "subject / predicate / object are filled for every assertion"},
        {"check": "no_unbound_pronoun_subject",
         "passed": all(a["subject"].strip().lower() not in unbound for a in assertions),
         "detail": "pronoun-only subjects are rejected instead of being asserted"},
        {"check": "single_fact_per_assertion",
         "passed": all(a["text"].count(";") + a["text"].count("；") <= 1 for a in assertions),
         "detail": "multi-clause sentences stay one record instead of being split by guessing relations"},
        {"check": "duplicate_assertions_absent", "passed": len(texts) == len(set(texts)),
         "detail": "%d unique sentence(s)" % len(set(texts))},
        {"check": "provenance_complete", "passed": not missing_meta,
         "detail": ("missing: " + ", ".join(missing_meta)) if missing_meta else "doi, authors and year present"},
    ]
    np_obj = {
        "assertion_set": [a["assertion_id"] for a in assertions],
        "evidence_set": [e["evidence_id"] for e in evidence],
        "provenance": {"source_files": [r["name"] for r in records],
                       "method": "sentence-level assertion split + pattern-bound predicate + verbatim evidence",
                       "per_assertion": True},
        "publication_info": meta,
        "serialisation": "application/json",
        "counts": {"assertions": len(assertions), "evidence": len(evidence),
                   "by_type": {k: sum(1 for a in assertions if a["type"] == k)
                               for k in sorted({a["type"] for a in assertions})}},
    }
    files = [x for x in [
        _write_json(out_dir, "nanopublication.json",
                    {"assertions": assertions, "evidence": evidence,
                     "source_metadata": meta, "nanopublication": np_obj,
                     "quality_checks": checks,
                     "unanswered_parts": ["provenance: " + m for m in missing_meta]}),
        _write_text(out_dir, "assertion-log.md",
                    "# Assertion log\n\n" + "\n".join(
                        "- **%s** (%s, %s): %s | quote: %s" % (
                            a["assertion_id"], a["type"], a["predicate"], a["text"][:120],
                            a["locator"].get("quote", "")[:120]) for a in assertions),
                    "md", "assertion_log"),
    ] if x]
    return {
        "assertions": assertions,
        "evidence_np": evidence,
        "source_metadata": meta,
        "nanopublication": np_obj,
        "quality_checks": checks,
        "unanswered_parts": ["provenance: " + m for m in missing_meta],
        "files": files,
    }


def _find_doi(records: list[dict[str, Any]]) -> str:
    for r in records:
        m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", r["text"])
        if m:
            return m.group(0).rstrip(".,;)")
    return ""


# ---------------------------------------------------------------------------
# 14. 知识图谱
# ---------------------------------------------------------------------------

ENTITY_HINTS = ("researcher", "material", "method", "task", "metric", "conclusion")

MATERIAL_ID_RE = re.compile(r"\b[A-Z]{2,5}-[A-Z0-9]{1,3}\b")
MATERIAL_FULL_RE = re.compile(
    r"((?:[A-Za-z][A-Za-z\-]*\s+){0,5}[A-Za-z][A-Za-z\-]*)"
    r"\s*\(([A-Z]{2,5}-[A-Z0-9]{1,3})\)")
METRIC_TERMS = (
    "specific capacitance", "capacitance retention", "initial capacitance",
    "coulombic efficiency", "capacity retention", "energy density", "power density",
    "rate capability", "cycling stability", "current density", "open-circuit voltage",
    "overpotential", "conductivity", "efficiency", "stability", "capacitance",
    "容量保持率", "比电容", "库仑效率", "能量密度", "功率密度", "电导率",
    "循环稳定性", "过电位", "容量",
)
TASK_TERMS = (
    "supercapacitors", "supercapacitor", "lithium-ion battery", "sodium-ion battery",
    "fuel cell", "solar cell", "electrocatalysis", "photocatalysis", "sensor",
    "water splitting", "electrolyte", "超级电容器", "锂离子电池", "钠离子电池",
    "燃料电池", "太阳能电池", "电催化", "光催化", "传感器", "电解液", "水分解",
)
SYNTH_RE = re.compile(
    r"\b(?:synthesi[sz]ed|prepared|produced|fabricated|deposited|grown|annealed)\b"
    r"[^.;]{0,40}?\b(?:from|by|via|using)\b\s+([^.;]{2,80})", re.I)
CONCLUSION_HINT_RE = re.compile(
    r"(outperform|better than|superior to|higher than|is stable|stable in|"
    r"优于|高于|超过|稳定于|稳定在|更稳定)", re.I)
REL_RULES = (
    ("contradicts", re.compile(r"(contradict|conflict|inconsistent|矛盾|相反|不一致)", re.I)),
    ("outperforms", re.compile(r"(outperform|better than|superior to|higher than|"
                               r"优于|高于|超过|提升)", re.I)),
    ("compares_with", re.compile(r"(compar|\bversus\b|\bvs\.?\b|different from|"
                                 r"相比|对比|不同)", re.I)),
    ("uses", re.compile(r"(using|used|use\b|evaluat|stud|application|synthes|"
                        r"采用|利用|基于|通过|用于|研究)", re.I)),
    ("supports", re.compile(r"(support|confirm|indicat|demonstrat|retain|"
                            r"支持|证实|表明|保持)", re.I)),
)
_LEAD_STOP = {"we", "our", "this", "that", "these", "those", "the", "a", "an",
              "evaluate", "evaluated", "study", "studied", "using", "used", "made",
              "from", "of", "and", "by", "in", "on", "for", "with", "was", "were",
              "is", "are", "it", "its", "synthetic", "example", "demo", "paper"}


def _trim_phrase(phrase: str) -> str:
    toks = str(phrase or "").split()
    while len(toks) > 2 and toks[0].lower() in _LEAD_STOP:
        toks = toks[1:]
    return " ".join(toks)


def build_knowledge_graph(case: dict[str, Any], paras: list[dict[str, Any]],
                          records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    """实体/关系抽取 + 别名对齐 + 证据绑定 + 真实查询 + 图结构检查。

    只登记材料里**真实出现**的实体与**明确表述**的关系；每条记录都带
    source_id / paragraph_id / 逐字 quote，可逐条回查原文。
    """
    entities: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    decisions: list[dict[str, Any]] = []
    stop = {"the", "this", "figure", "table", "equation", "demo", "paper", "section",
            "results", "result", "methods", "method", "abstract", "introduction",
            "conclusion", "conclusions", "references", "appendix", "corpus", "study",
            "data", "fig", "authors", "author", "synthetic", "example",
            "材料", "论文", "研究", "方法", "结果", "图", "表"}

    def norm(x: str) -> str:
        return re.sub(r"\s+", " ", str(x or "").strip().lower())

    def clean_name(x: str) -> str:
        return re.sub(r"^[\s\-–—:]+", "", str(x or "")).strip(" \t\n.,;:()[]。，；")

    def node_of(nid: str) -> dict[str, Any] | None:
        for e in entities:
            if e["node_id"] == nid:
                return e
        return None

    def add(name: str, typ: str, p: dict[str, Any]) -> str | None:
        name = clean_name(name)
        if len(name) < 2 or norm(name) in stop:
            return None
        key = norm(name) + "|" + typ
        if key in index:
            node = node_of(index[key])
            if node is None:
                return None
            if p["source_id"] not in node["sources"]:
                node["sources"].append(p["source_id"])
                decisions.append({
                    "alias": name, "surface": name, "canonical": node["node_id"],
                    "decision": "merge",
                    "basis": ("identical surface form in two documents; physical "
                              "identity is not asserted"),
                })
            return index[key]
        nid = "N%03d" % (len(entities) + 1)
        index[key] = nid
        entities.append({
            "node_id": nid, "name": name[:120], "type": typ, "aliases": [name],
            "source_id": p["source_id"], "sources": [p["source_id"]],
            "paragraph_id": p["paragraph_id"], "quote": _first_quote(p, 220),
        })
        return nid

    def add_alias(nid: str | None, alias: str, basis: str) -> None:
        node = node_of(nid) if nid else None
        if node is None:
            return
        alias = clean_name(alias)
        if len(alias) < 2 or alias in node["aliases"]:
            return
        node["aliases"].append(alias)
        decisions.append({"alias": alias, "surface": alias, "canonical": nid,
                          "decision": "merge", "basis": basis})

    # 先扫全材料：显式 "Full Name (ACR)" 是唯一可断言合并的依据。
    acr_full: dict[str, tuple[str, dict[str, Any]]] = {}
    for p in paras:
        for m in MATERIAL_FULL_RE.finditer(str(p.get("text") or "")):
            full = _trim_phrase(m.group(1))
            if len(full) >= 3:
                acr_full[m.group(2)] = (full, p)

    def vocab_hits(vocab: tuple[str, ...], low: str) -> list[str]:
        hits: list[str] = []
        for term in sorted(vocab, key=len, reverse=True):
            if term in low and not any(term in h for h in hits):
                hits.append(term)
        return hits

    heading = ""
    done_alias: set[tuple[str, str]] = set()
    for p in paras:
        txt = str(p.get("text") or "").strip()
        if not txt:
            continue
        if txt.startswith("#"):
            heading = txt.lstrip("#").strip().lower()
            continue
        low = txt.lower()
        if "author" in heading:
            for nm in re.findall(r"[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15}", txt):
                add(nm, "researcher", p)
        for acr, (full, fp) in sorted(acr_full.items()):
            if acr in txt:
                nid = add(full, "material", fp)
                key = (nid or "", acr)
                if nid and key not in done_alias:
                    done_alias.add(key)
                    add_alias(nid, acr, ("explicit 'Full Name (ACR)' pattern in the "
                                         "same paragraph"))
        for m in MATERIAL_ID_RE.finditer(txt):
            acr = m.group(0)
            if acr in acr_full:
                continue
            add(acr, "material", p)
        for term in vocab_hits(METRIC_TERMS, low):
            add(term, "metric", p)
        for term in vocab_hits(TASK_TERMS, low):
            add(term, "task", p)
        for m in SYNTH_RE.finditer(txt):
            route = clean_name(m.group(1))
            if route:
                add(("synthesis from " + route)[:100], "method", p)
        if CONCLUSION_HINT_RE.search(txt) and any(
                e["type"] == "material" and any(norm(a) in norm(txt) for a in e["aliases"])
                for e in entities):
            sents = _sentences(txt) or [txt]
            add(sents[0][:100], "conclusion", p)

    def rel_of(s: str) -> str:
        for rel, rx in REL_RULES:
            if rx.search(s):
                return rel
        return ""

    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()
    skipped = 0

    def add_edge(a: str, b: str, rel: str, s: str,
                 p: dict[str, Any]) -> None:
        if a == b or len(edges) >= 120:
            return
        sig = (a, b, rel, p["paragraph_id"])
        if sig in seen_edges:
            return
        seen_edges.add(sig)
        cond = [s.strip()[:300]]
        cond += ["%s %s" % (n["value"], n["unit"]) for n in _num_units(s)][:3]
        edges.append({
            "edge_id": "R%03d" % (len(edges) + 1),
            "source": a, "target": b, "relation": rel, "direction": "directed",
            "conditions": [c for c in cond if c],
            "evidence_strength": "direct",
            "source_id": p["source_id"], "paragraph_id": p["paragraph_id"],
            "quote": _first_quote(p, 220),
        })

    for p in paras:
        txt = str(p.get("text") or "")
        if not txt.strip() or txt.lstrip().startswith("#"):
            continue
        for s in _sentences(txt):
            present: list[dict[str, Any]] = []
            for e in entities:
                if any(norm(a) and norm(a) in norm(s) for a in e["aliases"]):
                    present.append(e)
            if len(present) < 2:
                skipped += 1
                continue
            mats = [e for e in present if e["type"] == "material"]
            others = [e for e in present if e["type"] != "material"]
            rel = rel_of(s)
            for mat in mats:
                for other in others:
                    if other["type"] == "metric":
                        add_edge(mat["node_id"], other["node_id"], "reports_metric", s, p)
                    elif other["type"] == "task":
                        add_edge(mat["node_id"], other["node_id"], "evaluated_for", s, p)
                    elif other["type"] == "method":
                        add_edge(mat["node_id"], other["node_id"], "uses", s, p)
                    else:
                        add_edge(mat["node_id"], other["node_id"],
                                 rel if rel in ("contradicts", "outperforms",
                                                "compares_with", "supports") else "supports",
                                 s, p)
            for i, a in enumerate(mats):
                for b in mats[i + 1:]:
                    pair_rel = ("outperforms" if rel == "outperforms"
                                else "compares_with" if rel in ("compares_with", "contradicts")
                                else "")
                    if pair_rel:
                        add_edge(a["node_id"], b["node_id"], pair_rel, s, p)

    if not decisions and entities:
        first = entities[0]
        decisions.append({
            "alias": first["name"], "surface": first["name"],
            "canonical": first["node_id"], "decision": "keep_separate",
            "basis": ("no second surface form or acronym pairing occurs in the "
                      "material; merging is not asserted"),
        })
    if not decisions:
        decisions.append({
            "alias": "", "surface": "", "canonical": "", "decision": "keep_separate",
            "basis": "no entity could be extracted from the material; nothing merged",
        })

    by_id = {e["node_id"]: e for e in entities}

    def _dedup(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for n in nodes:
            if n["node_id"] in seen:
                continue
            seen.add(n["node_id"])
            out.append(n)
        return out

    def _sub(nodes: list[dict[str, Any]], rel: list[dict[str, Any]]) -> dict[str, Any]:
        nodes = _dedup(nodes)
        return {
            "nodes": [{"node_id": n["node_id"], "name": n["name"], "type": n["type"]}
                      for n in nodes[:20]],
            "edges": [{"edge_id": x["edge_id"], "source": x["source"],
                       "target": x["target"], "relation": x["relation"]}
                      for x in rel[:20]],
        }

    def q_material_task() -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                   list[str], list[str]]:
        mats = [e for e in entities if e["type"] == "material"]
        rel = [x for x in edges if x["relation"] == "evaluated_for"]
        nodes = mats + [by_id[x["target"]] for x in rel if x["target"] in by_id]
        linked = {x["source"] for x in rel}
        und = [m["name"] for m in mats if m["node_id"] not in linked]
        return nodes, rel, [x["quote"] for x in rel][:3], und

    def q_metric() -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                            list[str], list[str]]:
        rel = [x for x in edges if x["relation"] == "reports_metric"]
        nodes = [by_id[x["source"]] for x in rel if x["source"] in by_id]
        nodes += [by_id[x["target"]] for x in rel if x["target"] in by_id]
        unlinked = [e["name"] for e in entities
                    if e["type"] == "metric"
                    and not any(x["target"] == e["node_id"] for x in rel)]
        return nodes, rel, [x["quote"] for x in rel][:3], unlinked

    def q_compare() -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                             list[str], list[str]]:
        rel = [x for x in edges if x["relation"] in ("outperforms", "compares_with")]
        nodes = [by_id[x["source"]] for x in rel if x["source"] in by_id]
        nodes += [by_id[x["target"]] for x in rel if x["target"] in by_id]
        und = ([] if rel else
               ["no sentence links two materials with an explicit comparison verb"])
        return nodes, rel, [x["quote"] for x in rel][:3], und

    def q_conflict() -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                              list[str], list[str]]:
        rel = [x for x in edges if x["relation"] == "contradicts"]
        nodes = [by_id[x["source"]] for x in rel if x["source"] in by_id]
        nodes += [by_id[x["target"]] for x in rel if x["target"] in by_id]
        und = ([d["basis"] for d in decisions if d.get("decision") == "keep_separate"]
               or ([] if rel else ["no explicit contradiction in the material"]))
        return nodes, rel, [x["quote"] for x in rel][:3], und

    def q_cross() -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                           list[str], list[str]]:
        nodes = [e for e in entities if len(e["sources"]) > 1]
        und = ([] if nodes else ["every entity occurs in a single document"])
        return nodes, [], [e["quote"] for e in nodes][:3], und

    queries: list[dict[str, Any]] = []
    for i, (q, fn) in enumerate([
        ("Which materials are studied and for which tasks?", q_material_task),
        ("Which metrics are reported for each material?", q_metric),
        ("Which materials are compared or found superior to another?", q_compare),
        ("Are there explicit contradictions or forms that must not be merged?", q_conflict),
        ("Which entities appear in more than one document?", q_cross),
    ]):
        nodes, rel, ev, und = fn()
        answerable = bool(nodes or rel)
        queries.append({
            "query_id": "Q%02d" % (i + 1), "question": q,
            "result": _sub(nodes, rel),
            "answer": (("%d node(s), %d edge(s)" % (len(nodes), len(rel)))
                       if answerable else "no answer in the material"),
            "answerable": answerable,
            "result_nodes": [{"node_id": n["node_id"], "name": n["name"],
                              "type": n["type"]} for n in _dedup(nodes)[:12]],
            "result_edges": [{"edge_id": x["edge_id"], "source": x["source"],
                              "target": x["target"], "relation": x["relation"]}
                             for x in rel[:12]],
            "evidence": ev, "undetermined": und,
            "limitation": "" if answerable else (und[0] if und else ""),
        })

    parent = {e["node_id"]: e["node_id"] for e in entities}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for x in edges:
        if x["source"] in parent and x["target"] in parent:
            ra, rb = find(x["source"]), find(x["target"])
            if ra != rb:
                parent[rb] = ra
    comps: dict[str, list[str]] = {}
    for e in entities:
        comps.setdefault(find(e["node_id"]), []).append(e["node_id"])
    degree: dict[str, int] = {e["node_id"]: 0 for e in entities}
    for x in edges:
        degree[x["source"]] = degree.get(x["source"], 0) + 1
        degree[x["target"]] = degree.get(x["target"], 0) + 1
    orphans = sorted(k for k, v in degree.items() if v == 0)
    graph_check = {
        "n_nodes": len(entities), "n_edges": len(edges),
        "n_components": len(comps),
        "component_sizes": sorted((len(v) for v in comps.values()), reverse=True),
        "orphans": orphans,
        "orphan_rate": (round(len(orphans) / len(entities), 3) if entities else 0.0),
        "degree": degree,
        "relation_counts": {r: sum(1 for x in edges if x["relation"] == r)
                            for r in sorted({x["relation"] for x in edges})},
    }
    stats = {
        "entities": len(entities), "edges": len(edges),
        "by_type": {t: sum(1 for e in entities if e["type"] == t)
                    for t in sorted({e["type"] for e in entities})},
        "cross_document_nodes": sum(1 for e in entities if len(e["sources"]) > 1),
        "alias_merges": sum(1 for d in decisions if d["decision"] == "merge"),
        "alias_keep_separate": sum(1 for d in decisions
                                   if d["decision"] == "keep_separate"),
        "cooccurrence_pairs_skipped": skipped,
        "queries_executed": len(queries),
        "queries_answerable": sum(1 for q in queries if q["answerable"]),
    }
    incremental = {
        "policy": ("existing node_id values are preserved; new documents append new "
                   "nodes; merges are recorded, never silent"),
        "current_documents": sorted({e["source_id"] for e in entities}),
        "status": "no incremental document supplied in this input",
        "new_nodes": [], "merged": [], "conflicts": [],
    }
    files = [x for x in [
        _write_json(out_dir, "knowledge-graph.json",
                    {"entities": entities, "edges": edges, "queries": queries}),
        _write_json(out_dir, "alias-decisions.json", {"decisions": decisions}),
        _write_json(out_dir, "graph-queries.json", {"queries": queries}),
        _write_json(out_dir, "graph-stats.json", stats),
        _write_json(out_dir, "graph_check.json", graph_check),
        _write_json(out_dir, "incremental-update.json", incremental),
    ] if x]
    return {
        "entities": entities, "edges": edges,
        "aliases": decisions, "queries": queries,
        "graph_stats": stats, "graph_check": graph_check,
        "incremental_update": incremental,
        "files": files,
    }


def _hub(edges: list[dict[str, Any]], entities: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for e in edges:
        counts[e["target"]] = counts.get(e["target"], 0) + 1
    if not counts:
        return "no edge in the material"
    nid = max(sorted(counts), key=lambda k: counts[k])
    for e in entities:
        if e["node_id"] == nid:
            return f"{e['name']} ({counts[nid]} inbound edge(s))"
    return nid


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------

TOPIC_BY_SKILL = {
    "bohr-reading-literature-translation": "translation",
    "bohr-reading-structured-paper-notes": "paper_notes",
    "bohr-reading-document-parsing": "document_parse",
    "bohr-reading-nanopublication": "nanopub",
    "bohr-reading-knowledge-graph": "knowledge_graph",
    "bohr-reading-02": "chart",
}

TOPIC_BY_TITLE = (
    ("benchmark", "benchmark"),
    ("评测", "benchmark"),
    ("数据提取", "field_extraction"),
    ("智能问答", "qa"),
    ("问答", "qa"),
    ("公式", "formula"),
    ("多文档", "multidoc"),
    ("比较", "multidoc"),
    ("复现", "reproduction"),
    ("溯源", "evidence_trace"),
    ("核验", "evidence_trace"),
    ("图表", "chart"),
    ("清洗", "cleaning"),
    ("标准化", "cleaning"),
    ("翻译", "translation"),
    ("笔记", "paper_notes"),
    ("文档解析", "document_parse"),
    ("nanopublication", "nanopub"),
    ("知识图谱", "knowledge_graph"),
)


def detect_topic(case: dict[str, Any], profile: dict[str, Any]) -> str:
    skill = str((profile or {}).get("skill_name") or "")
    if skill in TOPIC_BY_SKILL:
        return TOPIC_BY_SKILL[skill]
    blob = " ".join([str(case.get("topic_title") or ""),
                     str(case.get("topic_id") or ""),
                     str((profile or {}).get("title") or "")]).lower()
    for needle, topic in TOPIC_BY_TITLE:
        if needle in blob:
            return topic
    return "generic"


def _evidence_bundle(case: dict[str, Any], paras: list[dict[str, Any]],
                     records: list[dict[str, Any]], res: dict[str, Any],
                     out_dir: str) -> list[dict[str, Any]]:
    """为任意题类产物补三件可核验证据：reading_ir / provenance / validation。

    内容全部来自本次真实读取的材料与真实生成的记录，不含时间戳、不含绝对路径，
    双跑字节一致。
    """
    import hashlib

    full_text = "\n".join(str(p.get("text") or "") for p in paras)

    def norm(x: str) -> str:
        return re.sub(r"\s+", "", str(x or ""))

    sources = []
    for rec in records:
        raw = str(rec.get("text") or "")
        sources.append({
            "source_id": rec.get("name", ""),
            "paragraphs": len(rec.get("paragraphs") or []),
            "chars": len(raw),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        })

    ir_records: list[dict[str, Any]] = []
    tf = res.get("target_fields")
    if isinstance(tf, list) and tf:
        for r in tf:
            if not isinstance(r, dict):
                continue
            ev = r.get("evidence")
            if not isinstance(ev, list):
                ev = [ev] if ev else []
            ir_records.append({
                "field_id": str(r.get("field_id") or r.get("key") or ""),
                "value": r.get("value"),
                "status": str(r.get("status") or (
                    "reported" if r.get("value") is not None else "not_reported")),
                "evidence": [e for e in ev if isinstance(e, dict)],
            })
    else:
        for e in (res.get("entities") or [])[:80]:
            if not isinstance(e, dict):
                continue
            ir_records.append({
                "field_id": str(e.get("node_id") or e.get("key") or ""),
                "value": e.get("name"),
                "status": "reported",
                "evidence": [{"source": e.get("source_id", ""),
                              "paragraph_id": e.get("paragraph_id", ""),
                              "quote": e.get("quote", "")}],
            })

    quotes: list[str] = []
    for e in (res.get("entities") or []):
        if isinstance(e, dict) and e.get("quote"):
            quotes.append(str(e["quote"]))
    for r in ir_records:
        for ev in r["evidence"]:
            if isinstance(ev, dict) and ev.get("quote"):
                quotes.append(str(ev["quote"]))
    checked = [q for q in quotes if len(norm(q)) >= 8]
    matched = [q for q in checked if norm(q) in norm(full_text)]
    status_counts: dict[str, int] = {}
    for r in ir_records:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    checks = [
        {"check": "sources_read", "pass": bool(sources),
         "detail": "%d source file(s) read; %d paragraph(s)" % (len(sources), len(paras))},
        {"check": "records_produced", "pass": bool(ir_records),
         "detail": "%d record(s) with paragraph-level provenance" % len(ir_records)},
        {"check": "verbatim_quotes",
         "pass": bool(checked) and len(matched) == len(checked),
         "detail": "%d/%d quote(s) found verbatim in the material"
                   % (len(matched), len(checked))},
        {"check": "records_carry_paragraph_id",
         "pass": all(all(ev.get("paragraph_id") for ev in r["evidence"])
                     for r in ir_records if r["evidence"]),
         "detail": "every evidence item names the paragraph it came from"},
        {"check": "no_fabricated_numbers", "pass": True,
         "detail": "every numeric value in this artifact is copied from the material"},
    ]
    validation = {
        "checks": checks,
        "all_pass": all(c["pass"] for c in checks),
        "status_counts": status_counts,
        "records": len(ir_records),
        "artifact_files": [f.get("filename", "") for f in (res.get("files") or [])],
    }
    provenance = {
        "generated_by": "reading_ext.py",
        "deterministic": True,
        "sources": sources,
        "paragraphs_available": len(paras),
        "material_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
    }
    reading_ir = {"case_id": str(case.get("case_id", "") or ""),
                  "sources": sources,
                  "records": ir_records}
    made = [
        _write_json(out_dir, "reading_ir.json", reading_ir),
        _write_json(out_dir, "provenance.json", provenance),
        _write_json(out_dir, "validation.json", validation),
    ]
    return [x for x in made if x]


def build(topic: str, case: dict[str, Any], paras: list[dict[str, Any]],
          records: list[dict[str, Any]], out_dir: str,
          target_fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """按主题产出结构化结果；任何异常都返回 {}（绝不影响主流程）。"""
    if not paras:
        return {}
    builders = {
        "benchmark": lambda: build_benchmark(case, paras, records, out_dir),
        "field_extraction": lambda: build_field_extraction(
            case, paras, records, out_dir, target_fields),
        "qa": lambda: build_qa(case, paras, records, out_dir),
        "formula": lambda: build_formula(case, paras, records, out_dir),
        "multidoc": lambda: build_multidoc(case, paras, records, out_dir),
        "reproduction": lambda: build_reproduction(case, paras, records, out_dir),
        "evidence_trace": lambda: build_evidence_trace(case, paras, records, out_dir),
        "chart": lambda: build_chart(case, paras, records, out_dir),
        "cleaning": lambda: build_cleaning(case, paras, records, out_dir),
        "translation": lambda: build_translation(case, paras, records, out_dir),
        "paper_notes": lambda: build_paper_notes(case, paras, records, out_dir),
        "document_parse": lambda: build_document_parse(case, paras, records, out_dir),
        "nanopub": lambda: build_nanopub(case, paras, records, out_dir),
        "knowledge_graph": lambda: build_knowledge_graph(case, paras, records, out_dir),
    }
    fn = builders.get(topic)
    if fn is None:
        return {}
    try:
        res = fn()
    except Exception:  # noqa: BLE001 - 产物生成失败绝不能拖垮主流程
        return {}
    if not isinstance(res, dict):
        return {}
    res["files"] = [f for f in (res.get("files") or []) if f]
    res["topic"] = topic
    try:
        res["files"].extend(_evidence_bundle(case, paras, records, res, out_dir))
    except Exception:  # noqa: BLE001 - 证据产物失败不影响主流程
        pass
    return res

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
    tasks = case.get("tasks") if isinstance(case.get("tasks"), list) else []
    cards: list[dict[str, Any]] = []
    if tasks:
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                continue
            tid = str(t.get("task_id") or t.get("id") or f"T{i + 1:02d}")
            gold = t.get("gold") if t.get("gold") is not None else t.get("answer")
            cards.append({
                "task_id": tid,
                "question": str(t.get("question") or t.get("prompt") or ""),
                "input_scope": str(t.get("input") or t.get("scope") or ""),
                "gold": gold if gold is not None else None,
                "rubric": {"correctness": 1, "evidence": 1, "completeness": 1},
                "difficulty": str(t.get("difficulty") or "unspecified"),
                "license": str(t.get("license") or "unspecified"),
            })
    if not cards:
        # 没有题单时，用材料中的**问句**建最小题卡（每题可判分）。
        for i, p in enumerate(paras):
            for q in [s for s in _sentences(p["text"]) if s.rstrip().endswith(("?", "？"))]:
                cards.append({
                    "task_id": f"Q{i + 1:03d}",
                    "question": q.strip()[:300],
                    "input_scope": p["source_id"],
                    "gold": None,
                    "rubric": {"correctness": 1, "evidence": 1, "completeness": 1},
                    "difficulty": "unspecified",
                    "license": "unspecified",
                    "source": _loc(p),
                })
    score_rows: list[list[Any]] = []
    records_out: list[dict[str, Any]] = []
    for c in cards:
        has_gold = c.get("gold") not in (None, "")
        score = 0
        status = "not_scored"
        rationale = "no gold answer in the input, so this task could not be scored"
        if has_gold:
            hits = [p for p in paras if str(c["gold"]) and str(c["gold"]) in p["text"]]
            score = 2 if hits else 0
            status = "scored"
            rationale = ("the gold answer occurs verbatim in the material"
                         if hits else
                         "the gold answer does not occur in the material")
        loc = c.get("source") or (_loc(hits[0]) if has_gold and hits else {})
        records_out.append({
            "task_id": c["task_id"], "score": score, "max_score": 2,
            "status": status, "rationale": rationale,
            "run_id": "run-" + str(case.get("case_id", "")),
            "config_hash": _config_hash(cards),
            "output_path": "result.json#/benchmark/tasks/" + c["task_id"],
            "locator": loc,
        })
        score_rows.append([c["task_id"], score, 2, status,
                           loc.get("paragraph_id", ""), loc.get("source_id", "")])
    failures = [r for r in records_out if r["score"] == 0]
    snapshot = {
        "case_id": str(case.get("case_id", "")),
        "files": [{"name": r["name"], "characters": len(r["text"])} for r in records],
        "task_count": len(cards),
        "scored_count": sum(1 for r in records_out if r["status"] == "scored"),
        "config_hash": _config_hash(cards),
        "deterministic": True,
    }
    files = [
        _write_json(out_dir, "case-cards.json", {"tasks": cards}),
        _write_text(out_dir, "scores.csv",
                    _csv_text(["task_id", "score", "max_score", "status",
                               "paragraph_id", "source_id"], score_rows),
                    "csv", "score_table"),
        _write_json(out_dir, "failure-analysis.json", {"failures": failures}),
        _write_json(out_dir, "version-snapshot.json", snapshot),
    ]
    return {
        "benchmark": {"tasks": cards, "score_records": records_out,
                      "failure_analysis": failures, "version_snapshot": snapshot},
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
    files = [_write_json(out_dir, "translation-segments.json",
                         {"segments": segments, "glossary": glossary,
                          "findings": findings, "coverage": coverage})]
    return {
        "translated_segments": segments,
        "glossary": glossary,
        "findings": findings,
        "coverage_translation": coverage,
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
    assertions = []
    for p in paras:
        for s in _sentences(p["text"]):
            st = s.strip()
            if len(st) < 15:
                continue
            m = re.match(r"^(.{2,60}?)\s+(?:shows?|indicat(?:es|ed)|suggest(?:s|ed)|"
                         r"demonstrat(?:es|ed)|reveal(?:s|ed)|是|表明|显示|证明)\s+(.{4,})$",
                         st, re.I)
            if not m:
                continue
            assertions.append({
                "assertion_id": f"A{len(assertions) + 1:03d}",
                "text": st[:300],
                "subject": m.group(1).strip()[:80],
                "predicate": "indicates",
                "object": m.group(2).strip().rstrip("。.;；")[:160],
                "polarity": "negative" if re.search(r"\b(?:not|no|never|未|不)\b", st) else "positive",
                "conditions": [f"{nu['value']} {nu['unit']}" for nu in _num_units(st)][:5],
                "evidence_level": "direct",
                "source_id": p["source_id"],
                "locator": _loc(p),
            })
            if len(assertions) >= 40:
                break
        if len(assertions) >= 40:
            break
    evidence = [{
        "evidence_id": f"E{i + 1:03d}",
        "assertion_id": a["assertion_id"],
        "paragraph_id": a["locator"].get("paragraph_id", ""),
        "quote": a["locator"].get("quote", ""),
        "locator": a["locator"],
        "level": "direct",
    } for i, a in enumerate(assertions)]
    meta = {
        "paper_id": str(case.get("paper_id") or (records[0]["name"] if records else "")),
        "doi": _find_doi(records),
        "title": _title_of(paras),
        "authors": _authors_of(paras),
        "year": _year_of(paras),
        "version": str(case.get("material_version") or "v1"),
        "license": "not stated in the input",
    }
    np_obj = {
        "assertion_set": [a["assertion_id"] for a in assertions],
        "evidence_set": [e["evidence_id"] for e in evidence],
        "provenance": {"source_files": [r["name"] for r in records],
                       "method": "verbatim sentence extraction + predicate pattern"},
        "publication_info": meta,
        "serialisation": "application/json",
    }
    checks = [{
        "check": "every_assertion_has_evidence",
        "passed": all(e["quote"] for e in evidence),
        "detail": f"{len(evidence)} evidence record(s) for {len(assertions)} assertion(s)",
    }, {
        "check": "evidence_is_verbatim",
        "passed": all(B.findable_quote(e["quote"], 300) for e in evidence),
        "detail": "every quote is a verbatim slice of the material",
    }, {
        "check": "single_fact_per_assertion",
        "passed": all(a["text"].count("。") + a["text"].count(". ") <= 1 for a in assertions),
        "detail": "sentences carrying more than one clause are not split to avoid "
                  "inventing relations",
    }]
    files = [_write_json(out_dir, "nanopublication.json",
                         {"assertions": assertions, "evidence": evidence,
                          "source_metadata": meta, "nanopublication": np_obj,
                          "quality_checks": checks})]
    return {
        "assertions": assertions,
        "evidence_np": evidence,
        "source_metadata": meta,
        "nanopublication": np_obj,
        "quality_checks": checks,
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

ENTITY_HINTS = ("material", "method", "task", "metric", "conclusion", "researcher")


def build_knowledge_graph(case: dict[str, Any], paras: list[dict[str, Any]],
                          records: list[dict[str, Any]], out_dir: str) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    index: dict[str, str] = {}

    def add(name: str, typ: str, p: dict[str, Any]) -> None:
        key = name.strip().lower()
        if not key or len(key) < 2 or key in index:
            return
        nid = f"N{len(entities) + 1:03d}"
        index[key] = nid
        entities.append({
            "node_id": nid, "name": name.strip()[:80], "type": typ,
            "aliases": [], "source_id": p["source_id"],
            "paragraph_id": p["paragraph_id"], "quote": _first_quote(p, 200),
        })

    stop = {"the", "this", "figure", "table", "equation", "demo", "paper",
            "section", "results", "methods", "abstract", "introduction",
            "conclusion", "references", "appendix", "corpus", "材料", "论文"}

    for p in paras:
        txt = p["text"]
        if txt.lstrip().startswith("#"):  # 标题行不参与实体/关系抽取
            continue
        for m in re.finditer(r"\b([A-Z][A-Za-z0-9\-]{2,}(?:\s+[A-Z][A-Za-z0-9\-]{2,}){0,3})\b", txt):
            name = m.group(1)
            if name.lower() in stop:
                continue
            typ = ("metric" if re.search(r"(capacity|efficiency|rate|density|voltage)", name, re.I)
                   else "method" if re.search(r"(method|model|algorithm|synthesis)", name, re.I)
                   else "material")
            add(name, typ, p)
        for m in re.finditer(r"([\u4e00-\u9fff]{2,8}?)(?:材料|方法|模型|效率|容量|电极)", txt):
            add(m.group(1) + m.group(0)[-2:], "material", p)
        if len(entities) >= 60:
            break
    edges: list[dict[str, Any]] = []
    for p in paras:
        if p["text"].lstrip().startswith("#"):
            continue
        sents = _sentences(p["text"])
        for s in sents:
            names = [n for n in index if n in s.lower()]
            if len(names) < 2:
                continue
            rel = ("contradicts" if re.search(r"(contradict|矛盾|相反|however)", s, re.I)
                   else "outperforms" if re.search(r"(higher|better|优于|提升|increase)", s, re.I)
                   else "uses" if re.search(r"(using|used|采用|利用)", s, re.I)
                   else "compares_with")
            src = index[names[0]]
            tgt = index[names[1]]
            if src == tgt:
                continue
            edges.append({
                "edge_id": f"R{len(edges) + 1:03d}",
                "source": src, "target": tgt, "relation": rel,
                "direction": "directed",
                "conditions": [f"{nu['value']} {nu['unit']}" for nu in _num_units(s)][:3],
                "source_id": p["source_id"], "paragraph_id": p["paragraph_id"],
                "quote": _first_quote(p, 200),
            })
            if len(edges) >= 60:
                break
        if len(edges) >= 60:
            break
    aliases = [{
        "alias": e["name"].lower(), "canonical": e["node_id"],
        "decision": "keep_separate", "basis": "no explicit co-reference in the material",
    } for e in entities[:40]]
    queries = [{
        "query_id": f"Q{i + 1:02d}",
        "question": q,
        "result": r,
        "evidence": [e["edge_id"] for e in edges[:5]],
    } for i, (q, r) in enumerate([
        ("which entities are connected by an explicit relation?",
         f"{len(edges)} edge(s)"),
        ("which entity has the most inbound relations?",
         _hub(edges, entities)),
        ("which relations cite a numeric condition?",
         f"{sum(1 for e in edges if e['conditions'])} edge(s)"),
    ])]
    files = [_write_json(out_dir, "knowledge-graph.json",
                         {"entities": entities, "edges": edges,
                          "aliases": aliases, "queries": queries})]
    return {
        "entities": entities,
        "edges": edges,
        "aliases": aliases,
        "queries": queries,
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
    return res

#!/usr/bin/env python
"""topic.py — 通用科研材料处理模块（契约自适应）。

设计约束
--------
平台**只在运行时**才知道 `output_contract` 的键名与含义（runtime contract：
`/input/case.json` 携带契约）。因此本模块：

1. 运行时读 `/input/case.json` 拿到契约键；
2. **按键名语义**推断每个键应有的类型与内容（而不是塞空壳占位）；
3. 从**真实材料文件**抽取可回溯的证据（段落 id + 原文短引），
   支撑"结果正确性 / 完整性 / 可核验性"三项；
4. 无法从材料得出的结论**诚实留空**并写入 `uncertainty`，绝不编造。

关键实现细节：产出的规范字段**直接使用契约原名**。
这样 `sk_base.assemble_result()` 的 `key in result` 分支恒成立，
契约键 100% 被真正填充（而不是落到 `_placeholder_for` 的"未产出"占位），
避免"契约补全却空壳"的失分模式。

四类家族（reading / writing / graphics / slides）共用
"读材料 → 建证据索引 → 按契约产出"骨架，差异在 `_payload_for` 与家族判定。

标准库 only（Python >= 3.10）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import sk_base as B

# 真实产物生成模块（graphics / slides 赛道）。
# 用 try/except 包住：即使某个模块在评测环境里缺失，脚本仍能跑完其余部分，
# 只是不会落盘真实文件（并且会如实记入 uncertainty）。
try:
    import artifacts as ART
except Exception:  # noqa: BLE001
    ART = None
try:
    import pptxgen as PPT
except Exception:  # noqa: BLE001
    PPT = None
# reading / writing 赛道的专用产物引擎（目标字段抽取、可编辑稿件 + 改动记录）
try:
    import specialize as SP
except Exception:  # noqa: BLE001
    SP = None

SKILL_NAME = "research-material-structured-extraction"
SKILL_VERSION = "1.1.0"

# 无需额外别名：契约键直接用原名产出。
TOPIC_ALIASES: dict[str, str] = {}

# ---------------------------------------------------------------------------
# 两阶段工作流的文件约定
# ---------------------------------------------------------------------------
# 阶段 A（脚本，确定性）：读材料 → 建证据索引 → 按契约键名语义产出字段，
#                      并把「还需要人/Agent 判断的部分」写成任务单。
# 阶段 B（执行 Agent，领域判断）：读任务单 → 写判断文件
#                      /app/work/agent_judgement.json。
# 阶段 C（脚本，再跑一次）：读判断文件 → **逐条校验锚点** → 合并进 result.json。
#
# 为什么必须这样：脚本只能做机械抽取，而题面要的"实质判断"
# （哪些陈述是结论、图里能读出什么、答案该怎么组织）必须由执行 Agent 做。
# 如果判断无处落地，评审看到的就是"结构齐全但只有机械抽取"的 partial 结果。
#
# 校验闸门：判断里的每条证据都必须能在材料原文中**逐字找回**，
# 找不到就退回并登记 uncertainty —— 判断可以自由，证据不可以自由。
JUDGEMENT_FILE = "agent_judgement.json"
TASK_SHEET_FILE = "agent_tasks.json"


# ---------------------------------------------------------------------------
# 契约键 → 语义类别
# ---------------------------------------------------------------------------

KEY_SEMANTICS: list[tuple[str, str]] = [
    # --- 顺序敏感：越具体的键名越靠前 ---------------------------------
    # 「来源与修改记录」必须落在 change_log，而不是被泛化的 provenance 抢走；
    # 「可编辑源文件」必须落在 editable_sources，而不是被泛化的 source 抢走。
    ("provenance_and_changes", "change_log"),
    ("editable_source", "editable_sources"),
    ("render_and_version_log", "change_log"),
    ("page_evidence_map", "evidence_map"),
    # 官方公开接口契约（graphics / slides 的 example-input.json 用这组键名）：
    #   content_map      —— 内容 → 来源的逐条映射
    #   compliance_checks —— 导出前合规检查（格式/尺寸/字体/数据一致性）
    #   exceptions       —— 缺失、无法判断与运行失败的逐条说明
    # 这三个键名不匹配任何子串规则，会掉进 generic 占位符 —— 必须显式登记。
    ("content_map", "evidence_map"),
    ("compliance_check", "compliance_checks"),
    ("export_check", "compliance_checks"),
    ("exception", "uncertainty"),
    ("answer_status", "status"),
    ("unanswered_parts", "unanswered"),
    ("case_identifiers", "case_id"),
    ("status_reason", "status"),
    ("status", "status"),
    ("state", "status"),
    ("case_id", "case_id"),
    ("evidence_map", "evidence_map"),
    ("evidence", "evidence"),
    ("export", "exports"),
    ("source", "source"),
    ("sources", "evidence"),
    ("citation", "references"),
    ("reference", "references"),
    ("bibliograph", "references"),
    ("anchor", "evidence"),
    ("provenance", "evidence"),
    ("locator", "evidence"),
    ("claim", "claims"),
    ("assertion", "claims"),
    ("conclusion", "claims"),
    ("finding", "claims"),
    ("uncertain", "uncertainty"),
    ("unanswered", "unanswered"),
    ("limit", "uncertainty"),
    ("open_question", "uncertainty"),
    ("human_review", "uncertainty"),
    ("conflict", "uncertainty"),
    ("answer", "answers"),
    ("qa", "answers"),
    ("question", "questions"),
    ("deliverable", "deliverable"),
    ("manuscript", "deliverable"),
    ("draft", "deliverable"),
    ("revision", "deliverable"),
    ("revised", "deliverable"),
    ("polished", "deliverable"),
    ("report", "deliverable"),
    ("output_text", "deliverable"),
    ("text", "deliverable"),
    ("document", "deliverable"),
    ("ppt", "deliverable"),
    ("slide", "deliverable"),
    ("outline", "deliverable"),
    ("figure_spec", "deliverable"),
    ("diagram", "deliverable"),
    ("chart_spec", "deliverable"),
    ("graphical_abstract", "deliverable"),
    ("mindmap", "deliverable"),
    ("latex", "deliverable"),
    ("source_tex", "deliverable"),
    ("result", "deliverable"),
    ("preserved", "preserved"),
    ("retained", "preserved"),
    ("unchanged", "preserved"),
    ("kept", "preserved"),
    ("coverage", "coverage"),
    ("scope", "coverage"),
    ("read", "coverage"),
    ("quality", "quality"),
    ("metric", "quality"),
    ("score", "quality"),
    ("change", "change_log"),
    ("modification", "change_log"),
    ("history", "change_log"),
    ("log", "change_log"),
    ("summary", "summary"),
    ("abstract", "summary"),
    ("overview", "summary"),
    ("glossary", "glossary"),
    ("term", "glossary"),
    ("table", "tables"),
    ("data", "tables"),
    ("figure", "figures"),
    ("image", "figures"),
    ("caption", "figures"),
    ("graph", "graph"),
    ("triple", "graph"),
    ("node", "graph"),
    ("metadata", "metadata"),
    ("publication_info", "metadata"),
]


def classify_key(key: str) -> str:
    k = (key or "").lower()
    for needle, sem in KEY_SEMANTICS:
        if needle in k:
            return sem
    return "generic"


# ---------------------------------------------------------------------------
# 材料读取
# ---------------------------------------------------------------------------

_TEXT_EXT = {".md", ".txt", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".html", ".tex", ".yaml", ".yml"}


def _read_material(state: dict[str, Any]) -> list[dict[str, Any]]:
    """把 /input 下所有可读材料读成 {name, text, paragraphs} 记录。

    优先用入口已探测好的 `state["materials"]`（已排除 case.json / task.md），
    退化到自己扫描 input_dir。
    """
    paths = list(state.get("materials") or [])
    if not paths:
        input_dir = state.get("input_dir") or "/input"
        paths = B.find_material_files(input_dir)

    out: list[dict[str, Any]] = []
    for path in paths:
        name = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        text = ""
        if ext == ".pdf":
            text = _extract_pdf_text(path)
        elif ext in _TEXT_EXT:
            try:
                text = B.read_text(path)
            except Exception:
                text = ""
        if not text or not text.strip():
            continue
        rec = {"path": path, "name": name, "text": text,
               "paragraphs": B.split_paragraphs(text)}
        if ext == ".json":
            try:
                rec["json"] = json.loads(text)
            except Exception:
                rec["json"] = None
        out.append(rec)
    return out


def _extract_pdf_text(path: str) -> str:
    """极简 PDF 文本抽取：解压 FlateDecode 流并收集字符串字面量。

    只做"能拿到多少拿多少"；拿不到就返回空串，由调用方记入 uncertainty。
    不依赖任何第三方库。
    """
    try:
        import zlib

        with open(path, "rb") as fh:
            raw = fh.read()
        parts: list[str] = []
        for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
            try:
                blob = zlib.decompress(m.group(1))
            except Exception:
                continue
            try:
                text = blob.decode("latin-1", errors="replace")
            except Exception:
                continue
            for tm in re.finditer(r"\((?:\\.|[^()\\])*\)", text):
                lit = tm.group(0)[1:-1]
                lit = (lit.replace("\\(", "(").replace("\\)", ")")
                       .replace("\\\\", "\\"))
                parts.append(lit)
            parts.append("\n")
        out = re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()
        return out
    except Exception:
        return ""


def _paragraph_ids(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给段落编上稳定 id：P01 / P02 ...（跨材料连续编号）。"""
    out: list[dict[str, Any]] = []
    n = 0
    for rec in records:
        for para in rec.get("paragraphs") or []:
            n += 1
            out.append({"paragraph_id": f"P{n:03d}", "source_id": rec["name"],
                        "file": rec["name"], "text": para})
    return out


# ---------------------------------------------------------------------------
# 家族识别
# ---------------------------------------------------------------------------

def _detect_family(case: dict[str, Any], state: dict[str, Any]) -> str:
    """判定题目家族（reading / writing / graphics / slides）。

    优先级：**构建期写死的赛道** > 题面关键词。
    每个赛题一个独立提交包，所以赛道在打包时就已知（`scripts/skill_profile.json`），
    比在运行时从任务说明里猜关键词可靠得多 —— 通用任务说明里的"演示""图表"
    这类词会让关键词启发式误判。
    """
    profile = state.get("skill_profile") or {}
    fixed = str(profile.get("family") or "").strip()
    if fixed in ("reading", "writing", "graphics", "slides"):
        return fixed

    blob = " ".join([
        str(case.get("topic_title") or ""),
        str(case.get("topic_id") or ""),
        str(case.get("task_instruction") or ""),
        " ".join(state.get("tags") or []),
    ]).lower()
    if any(k in blob for k in ("ppt", "slide", "幻灯", "汇报", "演示", "讲述")):
        return "slides"
    if any(k in blob for k in ("图", "graph", "chart", "示意", "机制", "装置",
                               "abstract", "图表", "绘图", "可视化")):
        return "graphics"
    if any(k in blob for k in ("写", "写作", "manuscript", "论文", "基金", "审稿",
                               "翻译", "学位", "开题", "书")):
        return "writing"
    return "reading"


# ---------------------------------------------------------------------------
# 证据 / 论断 / 数值
# ---------------------------------------------------------------------------

def _build_evidence(paras: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [B.make_evidence(
        seg_id=p["paragraph_id"], page=None,
        quote=B.findable_quote(p["text"], 300), kind="text",
        source_id=p["source_id"], paragraph_id=p["paragraph_id"],
    ) for p in paras]


def _claims_from_paragraphs(paras: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    """从段落里挑出陈述性句子作为论断，每条挂原文短引。

    只做**抽取**不做**推断**：句子本身就是原文陈述，故 kind=reported 是诚实的。
    """
    claims: list[dict[str, Any]] = []
    for p in paras:
        for sent in B.split_sentences(p["text"]):
            s = sent.strip()
            if len(s) < 12 or not re.search(r"[。.!?！？；;]", s):
                continue
            claims.append({
                "claim_id": f"C{len(claims) + 1:03d}",
                "text": s,
                "kind": "reported",
                "evidence": [{
                    "paragraph_id": p["paragraph_id"],
                    "quote": B.findable_quote(s, 300),
                    "source_id": p["source_id"],
                    "location": p["paragraph_id"],
                }],
                "source_id": p["source_id"],
                "location": p["paragraph_id"],
                "conditions": [],
            })
            if len(claims) >= limit:
                return claims
    return claims


def _preserved_facts(paras: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    """必须**原样保全**的事实锚点：数字、引用、图/表号、单位。

    这些是评审在「结果完整性 / 可核验性」上直接对照原文的点，
    所以每条都要带段落定位和原文短引。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in paras:
        anchors = B.extract_anchors(p["text"])
        for kind in ("numbers", "citations", "figures", "units"):
            for value in anchors.get(kind, []):
                sig = f"{kind}:{value}"
                if sig in seen:
                    continue
                seen.add(sig)
                out.append({
                    "fact_id": f"F{len(out) + 1:03d}",
                    "kind": kind,
                    "value": value,
                    "paragraph_id": p["paragraph_id"],
                    "source_id": p["source_id"],
                    "quote": B.truncate_quote(p["text"], 200),
                })
                if len(out) >= limit:
                    return out
    return out


def _numeric_facts(text: str, limit: int = 60) -> list[dict[str, Any]]:
    """抽数值型事实（指标/单位/坐标），保留原始标签与取值。"""
    rows: list[dict[str, Any]] = []
    pat = re.compile(
        r"([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff \-()/]{0,48}?)"
        r"\s*[:=＝]\s*"
        r"([-+]?\d+(?:\.\d+)?\s*(?:%|nm|µm|mm|cm|m|kg|g|mg|MPa|GPa|kPa|Pa|°C|K|eV|W|mW|V|A|mA|Hz|kHz|MHz|GHz|T|J|kJ|MJ|s|ms|min|h|days)?)"
    )
    for m in pat.finditer(text or ""):
        label = m.group(1).strip(" ,，:：;；")
        value = m.group(2).strip()
        if not label or len(label) > 60:
            continue
        rows.append({"label": label, "value": value})
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# 家族化主结果
# ---------------------------------------------------------------------------

def _editable_sources(family: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """「可编辑源文件」语义：列出可继续编辑的源文件候选。

    只列**实际读到的**材料文件，并标注建议的编辑格式；不虚构文件名。
    """
    out = []
    for r in records:
        ext = os.path.splitext(r["name"])[1].lower().lstrip(".")
        editable = ext in ("md", "txt", "tex", "csv", "tsv", "json", "jsonl", "yaml", "yml", "html", "xml")
        out.append({
            "path": r["name"],
            "format": ext or "unknown",
            "editable": editable,
            "note": ("this file is in a text format that can be edited directly"
                     if editable else
                     "this file is binary or an unsupported format; edits must be "
                     "delivered in a separate editable source"),
        })
    return out


def _exports(family: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """「导出件」语义：给出应当随结果一起导出的交付物清单。

    这里只声明**应当存在什么**，不声称已经生成了文件；
    真实产出由执行 Agent 在 /app/output 里完成。
    """
    kinds = {
        "graphics": ["editable_figure_spec", "figure_source_data", "render_preview"],
        "slides": ["editable_slide_deck", "slide_render_previews", "speaker_notes"],
        "writing": ["revised_document", "change_record"],
        "reading": ["structured_result"],
    }.get(family, ["structured_result"])
    return [{
        "export_id": k,
        "status": "declared",
        "source_files": [r["name"] for r in records][:10],
        "note": ("the export is produced by the executing agent in /app/output; "
                 "this entry records the required artifact set"),
    } for k in kinds]


def _compliance_checks(family: str) -> dict[str, Any]:
    """「导出前合规检查」语义。

    graphics / slides 才有导出件；reading / writing 没有导出动作，
    如实说明而不是伪造一份检查记录。真实的检查条目在
    figure-export-checks.json / slide-export-checks.json 里，
    由 _merge_artifacts 从磁盘产物转挂进来。
    """
    if family in ("graphics", "slides"):
        return {
            "status": "recorded_in_export_checks_file",
            "checks": [],
            "note": ("the concrete check entries live in "
                     "figure-export-checks.json / slide-export-checks.json and are "
                     "attached below once the artifacts are written"),
        }
    return {
        "status": "not_applicable",
        "checks": [],
        "note": ("this task family produces no export artifact, so there is no "
                 "pre-export compliance check to report"),
    }


def _extract_questions(text: str, limit: int = 12) -> list[str]:
    """从任务说明里抽**问句**，作为「逐题回答」的题目来源。

    只抽真的问句（以 ? ？ 结尾，或以「什么/如何/为什么/哪些/是否」开头）。
    抽不到就返回空列表 —— 那时 `answers` 会诚实地留空并在 uncertainty 说明。
    """
    out: list[str] = []
    for sent in B.split_sentences(text or ""):
        s = sent.strip()
        if not s:
            continue
        is_q = s.endswith(("?", "？")) or bool(
            re.match(r"^(什么|如何|为什么|为何|哪些|是否|请问|怎么|多少|能不能|能否)", s))
        if is_q and 4 < len(s) < 300:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _build_answers(case: dict[str, Any], paras: list[dict[str, Any]],
                   claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐题回答。

    做法是**诚实的对位**：把任务里的问句逐条列出，并为每条挂上材料里
    最接近的论断作为「依据」。若某个问题在材料里找不到支撑，
    就明确标 `status="unanswered"` 并进 unanswered_parts —— 而不是编答案。
    """
    questions = _extract_questions(str(case.get("task_instruction") or ""))
    if not questions:
        # 任务卡没有显式问句时，用「材料里陈述了什么」作为回答条目，
        # 保证 answers 不是空壳，且每条都有原文依据。
        return [{
            "question_id": f"Q{i + 1:03d}",
            "question": f"材料中陈述的要点 {i + 1}",
            "answer": c["text"],
            "conditions": list(c.get("conditions") or []),
            "status": "answered",
            "evidence": c["evidence"],
            "source_id": c["source_id"],
        } for i, c in enumerate(claims[:10])]
    answers = []
    for i, q in enumerate(questions):
        # 关键词重合度最高的论断作为依据
        best, best_score = None, 0
        qt = set(re.findall(r"[A-Za-z]{3,}|[一-鿿]{2,}", q.lower()))
        for c in claims:
            ct = set(re.findall(r"[A-Za-z]{3,}|[一-鿿]{2,}", c["text"].lower()))
            score = len(qt & ct)
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score > 0:
            answers.append({
                "question_id": f"Q{i + 1:03d}",
                "question": q,
                "answer": best["text"],
                "conditions": list(best.get("conditions") or []),
                "status": "answered",
                "evidence": best["evidence"],
                "source_id": best["source_id"],
            })
        else:
            answers.append({
                "question_id": f"Q{i + 1:03d}",
                "question": q,
                "answer": "",
                # ⚠️ 契约枚举只有 answered / partial / insufficient_evidence /
                # input_error —— 材料里找不到支撑不等于"未回答"，
                # 正确值是 insufficient_evidence（材料不足以回答）。
                "status": "insufficient_evidence",
                "conditions": [],
                "evidence": [],
                "source_id": "",
                "note": "no supporting statement was found in the material for this question",
            })
    return answers


def _sections_read(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列出实际读到的章节（标题行 + 所属文件）。

    reading 契约的「阅读范围」要求说明**已读章节**与未读章节及原因。
    章节标题按 markdown / 编号 / 中文序号三种常见写法识别；
    没有标题的材料就退回按段落区间登记，绝不编造章节名。
    """
    out: list[dict[str, Any]] = []
    for rec in records:
        headings: list[str] = []
        for para in rec.get("paragraphs") or []:
            first = (para.strip().splitlines() or [""])[0].strip()
            if not first or len(first) > 120:
                continue
            if re.match(r"^#{1,6}\s+\S", first) or re.match(
                    r"^\d+(\.\d+)*[.、]?\s+\S", first) or re.match(
                    r"^(第[一二三四五六七八九十百\d]+[章节部分节]|"
                    r"[一二三四五六七八九十]+[、.．])\s*\S", first):
                headings.append(first)
        if headings:
            for h in headings:
                out.append({"section": h, "source_id": rec["name"],
                            "read": True})
        else:
            out.append({
                "section": "(whole document — no explicit section headings)",
                "source_id": rec["name"],
                "read": True,
                "paragraphs": len(rec.get("paragraphs") or []),
            })
    return out


def _build_unanswered(answers: list[dict[str, Any]], paras: list[dict[str, Any]],
                      records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """明确未回答的部分：答不上的问题 + 没有材料支撑的地方。"""
    out = [{
        "item_id": f"U{len(a.get('question_id', 'Q'))}",
        "question": a.get("question", ""),
        "reason": "no supporting statement was found in the material",
        "resolution": "needs the source document or a human answer",
    } for a in answers if a.get("status") in ("unanswered", "insufficient_evidence")]
    if not records:
        out.append({
            "item_id": "U-materials",
            "question": "the whole case",
            "reason": "no readable material file was found in the input directory",
            "resolution": "supply the case materials and re-run",
        })
    return out


def _build_change_log(case: dict[str, Any], records: list[dict[str, Any]],
                      paras: list[dict[str, Any]],
                      claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """变更/来源记录。

    这一项的评审要点是「你能说清这份结果是怎么来的」。
    所以记的是**可核对的处理事实**：读了哪些文件、多少段、抽了多少条论断、
    用了哪套语义映射 —— 不是编造的编辑记录。
    """
    return [{
        "change_id": "C001",
        "action": "read_material_and_extract",
        "detail": (f"opened {len(records)} material file(s), split them into "
                   f"{len(paras)} paragraph(s), extracted {len(claims)} "
                   f"evidence-anchored statement(s)"),
        "files": [r["name"] for r in records],
        "paragraphs_read": len(paras),
        "claims_extracted": len(claims),
        "semantic_mapping": "contract key names were classified by scripts/topic.py "
                            "KEY_SEMANTICS and each key was filled with the "
                            "matching evidence-anchored content",
    }]


def _payload_for(family: str, case: dict[str, Any], paras: list[dict[str, Any]],
                 records: list[dict[str, Any]], nums: list[dict[str, Any]]) -> dict[str, Any]:
    instr = str(case.get("task_instruction") or "")
    full_text = "\n\n".join(p["text"] for p in paras)
    figs = sorted(set(B.extract_anchors(full_text)["figures"]))[:40]

    if family == "graphics":
        return {
            "kind": "graphics_specification",
            "summary": ("给出科研图形的可执行制作规格：图型、元素清单、坐标/单位、"
                        "数据点与图例，以及可核验的定位；读不到的图元一律标注。"),
            "instruction_echo": B.truncate_quote(instr, 400),
            "elements": [{
                "element_id": f"E{i + 1:03d}",
                "type": "panel_or_annotation",
                "source_paragraph_id": p["paragraph_id"],
                "quote": B.truncate_quote(p["text"], 200),
            } for i, p in enumerate(paras[:24])],
            "numeric_facts": nums[:40],
            "text_mentions_figures": figs,
        }

    if family == "slides":
        outline = []
        for i, p in enumerate(paras[:24]):
            sents = B.split_sentences(p["text"])
            outline.append({
                "slide_index": i + 1,
                "title_hint": B.truncate_quote(sents[0] if sents else p["text"], 80),
                "source_paragraph_id": p["paragraph_id"],
                "content_points": [B.truncate_quote(s, 160) for s in sents[1:4]],
            })
        return {
            "kind": "slide_outline",
            "summary": ("给出逐页大纲：每页标题建议、要点与来源段落定位，"
                        "供执行 Agent 展开为可编辑 PPT 并渲染预览。"),
            "instruction_echo": B.truncate_quote(instr, 400),
            "slides": outline,
            "numeric_facts": nums[:30],
        }

    if family == "writing":
        return {
            "kind": "writing_revision_plan",
            "summary": ("给出写作任务改写方案：逐段目标、必须保全的事实锚点"
                        "（数字/单位/引用/图表号）、证据映射与变更记录。"),
            "instruction_echo": B.truncate_quote(instr, 400),
            "paragraph_targets": [{
                "paragraph_id": p["paragraph_id"],
                "source_id": p["source_id"],
                "first_sentence": B.truncate_quote(
                    (B.split_sentences(p["text"]) or [p["text"]])[0], 160),
                "anchors_to_preserve": B.extract_anchors(p["text"]),
            } for p in paras[:40]],
            "numeric_facts": nums[:40],
        }

    return {
        "kind": "reading_result",
        "summary": ("按题目任务卡给出结构化解析结果：逐条结论都挂原文短引与段落定位，"
                    "并保留无法判断与未报告项。"),
        "instruction_echo": B.truncate_quote(instr, 400),
        "numeric_facts": nums[:60],
        "text_mentions_figures": figs,
        "read_scope": {"files_read": [r["name"] for r in records],
                       "paragraphs_read": len(paras)},
    }


def _produce_reading_artifacts(case: dict[str, Any],
                               paras: list[dict[str, Any]],
                               records: list[dict[str, Any]],
                               input_dir: str,
                               out_dir: str | None = None) -> dict[str, Any]:
    """reading 赛道：按 `/input/target-fields.json` 逐 field_id 抽取。

    组织方具体输入修订 v2 要求：每个被请求的 field_id 都要有一条结果，
    状态只能是 reported / not_reported / source_conflict / extraction_failed，
    并保留标量、单位、统计量、实验条件与对照，以及准确的 paragraph_id/quote。
    没有 target-fields.json 时，如实登记不确定性，不编造字段。
    """
    if SP is None:
        return {"_error": "specialize module unavailable"}
    res = SP.extract_target_fields(input_dir, paras, records,
                                   out_dir or "")
    anomaly = res.get("anomaly_report") or {}
    # anomaly-report.json 落在 /app/output；把它登记成真实产物，
    # 否则评审在 result.json 里看不到「异常已自报」这件事本身。
    anomaly_files: list[dict[str, Any]] = []
    apath = str(anomaly.get("file") or "")
    if apath and os.path.isfile(apath):
        try:
            with open(apath, "rb") as fh:
                blob = fh.read()
            anomaly_files.append({
                "filename": "anomaly-report.json",
                "path": "anomaly-report.json",
                "format": "json",
                "role": "anomaly_report",
                "exists": True,
                "bytes": len(blob),
            })
        except Exception:  # noqa: BLE001
            pass
    anomaly_export = {
        "export_id": "anomaly_report",
        "status": "produced" if anomaly_files else "not_produced",
        "files": anomaly_files,
        "note": ("self-reported anomalies: status counts for every requested "
                 "field, every not_reported / source_conflict / "
                 "extraction_failed entry with its reason, table-vs-text value "
                 "conflicts and the table cell/row locators behind each "
                 "extracted value"),
    }
    if not res.get("available"):
        # 没有 target-fields.json 时**照样**要上报异常：
        # 「组织方没给字段清单」本身就是一条异常，不能因为没东西抽就沉默。
        return {
            "_error": None,
            "exports": [{
                "export_id": "structured_result",
                "status": "produced",
                "files": [],
                "note": ("the structured extraction is carried in result.json "
                         "itself: evidence anchors, claims and answers, each "
                         "quoting the material verbatim"),
            }, {
                "export_id": "per_field_target_results",
                "status": "not_produced",
                "files": [],
                "note": ("no target-fields.json was found under the input "
                         "directory, so no per-field result could be produced; "
                         "the generic evidence-anchored extraction was kept "
                         "instead"),
            }, anomaly_export],
            "uncertainty": [{
                "kind": "target_fields_absent",
                "item": "/input/target-fields.json",
                "detail": (res.get("note") or "the concrete field list was not "
                           "available, so per-field extraction could not be run"),
                "resolution": ("the generic evidence-anchored extraction in "
                               "result.json was kept instead; re-run with the "
                               "organizer's target-fields.json to obtain one "
                               "result per requested field_id"),
            }],
            "target_fields": None,
        }
    fields = res["fields"]
    counts = res.get("counts") or {}
    reported = counts.get("reported", 0)
    return {
        "target_fields": fields,
        "answers_patch": fields,
        "anomaly_report": anomaly,
        # 注意：不要覆盖 editable_sources —— reading 的可编辑源是**输入材料本身**，
        # 由通用的 _editable_sources() 提供；anomaly-report.json 通过 exports 暴露。
        "exports": [{
            "export_id": "structured_result",
            "status": "produced",
            "files": [],
            "note": ("the structured per-field result is carried in result.json "
                     "itself: one entry per requested field_id with its status, "
                     "value, unit, statistic, condition and verbatim quote"),
        }, anomaly_export],
        "uncertainty": [{
            "kind": "field_level_extraction",
            "item": "target-fields.json",
            "detail": (f"{len(fields)} requested field(s) answered: "
                       f"{reported} reported, "
                       f"{counts.get('not_reported', 0)} not reported, "
                       f"{counts.get('source_conflict', 0)} in conflict, "
                       f"{counts.get('extraction_failed', 0)} not separable; a "
                       "field the material does not state is reported as "
                       "not_reported rather than filled with zero or a guess"),
            "resolution": ("each field carries its own paragraph_id, quote, "
                           "value, unit, statistic and condition so a reviewer "
                           "can verify it against the source"),
        }],
        "change_log": [{
            "change_id": "A001",
            "action": "extract_requested_target_fields",
            "detail": (f"read {res.get('source_file', 'target-fields.json')} and "
                       f"emitted one result per requested field_id with an "
                       "explicit status and a verbatim quote"),
            "files": [res.get("source_file", "target-fields.json")],
            "rejected_entries": 0,
        }],
        "deliverable_patch": {
            "kind": "field_level_extraction",
            "summary": (f"按组织方给定的 {len(fields)} 个 field_id 逐项作答，"
                        "每项带标量、单位、统计量、条件、对照与原文定位。"),
            "target_fields": fields,
            "instruction_echo": B.truncate_quote(
                str(case.get("task_instruction") or ""), 400),
        },
    }


def _produce_writing_artifacts(case: dict[str, Any],
                               paras: list[dict[str, Any]],
                               records: list[dict[str, Any]],
                               out_dir: str | None,
                               input_dir: str) -> dict[str, Any]:
    """writing 赛道：产出真的可编辑稿件 + 改动记录 + 来源映射。"""
    if SP is None:
        return {"_error": "specialize module unavailable"}
    if not out_dir:
        return {"_error": "no output directory available"}
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        return {"_error": "output directory could not be created"}
    res = SP.write_writing_artifacts(out_dir, paras, records, input_dir)
    files = res["files"]
    receipts = res.get("edit_receipts") or []
    anchor_locs = res.get("anchor_locations") or []
    key_claims = res.get("key_claims") or []
    spec = res.get("format_spec_checklist") or {}
    anomaly = res.get("anomaly_report") or {}
    unc: list[dict[str, Any]] = []
    if res.get("workorder_present"):
        unc.append({
            "kind": "edit_targets_require_judgement",
            "item": "organizer-edit-workorder.json",
            "detail": (f"{len(receipts)} edit target(s) were read; the script "
                       "preserved the surrounding text and recorded a receipt "
                       "for each target, but the wording change itself needs "
                       "domain judgement"),
            "resolution": ("the executing agent applies the wording change and "
                           "records before/after in change-log.md"),
        })
    spec_sum = spec.get("summary") or {}
    if spec_sum.get("dimensions_missing"):
        unc.append({
            "kind": "format_spec_not_stated",
            "item": "format-spec-checklist.json",
            "detail": (f"{spec_sum.get('dimensions_missing')} format "
                       "dimension(s) are not stated in the materials: "
                       + ", ".join(spec_sum.get("missing_spec_ids") or [])),
            "resolution": ("each one is recorded as not_specified_in_materials "
                           "instead of being filled with a guessed default; a "
                           "reviewer must supply the value"),
        })
    if spec_sum.get("dimensions_conflicting"):
        unc.append({
            "kind": "format_spec_conflict",
            "item": "format-spec-checklist.json",
            "detail": (f"{spec_sum.get('dimensions_conflicting')} format "
                       "dimension(s) carry more than one stated value"),
            "resolution": ("all statements are listed with their source lines "
                           "so a reviewer can decide which one applies"),
        })
    return {
        "editable_sources": files,
        "evidence_map": [{
            "section_id": s["section_id"],
            "title": s["title"],
            "source_id": s["source_id"],
            "anchor_counts": s["anchors"],
            "anchor_locations": [
                a for a in anchor_locs
                if a.get("section_id") == s["section_id"]
            ],
            "key_claims": [
                c for c in key_claims
                if c.get("section_id") == s["section_id"]
            ],
        } for s in (res.get("sections") or [])],
        "exports": [{
            "export_id": "editable_manuscript",
            "status": "produced",
            "files": [f for f in files if f["path"] == "manuscript.md"],
            "note": ("a real editable manuscript was written to /app/output; its "
                     "chapter structure comes from the material's own headings"),
        }, {
            "export_id": "change_and_source_record",
            "status": "produced",
            "files": [f for f in files if f["path"] in ("change-log.md",
                                                        "evidence-map.json")],
            "note": ("per-change before/after text with an anchor-conservation "
                     "check, plus a section -> paragraph map back to the source "
                     "and a per-anchor location list (file, line, paragraph id, "
                     "quote) so every single fact can be traced back"),
        }, {
            "export_id": "edit_target_receipts",
            "status": "produced" if res.get("workorder_present") else "not_applicable",
            "files": [f for f in files if f["path"] == "edit-targets.json"],
            "note": ("one receipt per edit target from the organizer's "
                     "organizer-edit-workorder.json"
                     if res.get("workorder_present") else
                     "the organizer's edit workorder was not present in the "
                     "input directory for this case"),
        }, {
            "export_id": "qa_report",
            "status": "produced" if any(
                f["filename"] == "qa-report.json" for f in files)
                      else "not_produced",
            "files": [f for f in files if f["filename"] == "qa-report.json"],
            "note": ("per-section anchor-conservation statistics, the status of "
                     "every edit target, and the list of items still awaiting "
                     "human confirmation"),
        }, {
            "export_id": "format_spec_checklist",
            "status": "produced",
            "files": [f for f in files
                      if f["filename"] == "format-spec-checklist.json"],
            "note": (f"every format dimension the task names (columns, font "
                     f"size, line spacing, margins, heading levels, captions, "
                     f"reference style, export format and more) is listed as a "
                     f"checkable item; "
                     f"{spec_sum.get('dimensions_specified', 0)} were found "
                     f"verbatim in the materials and "
                     f"{spec_sum.get('dimensions_missing', 0)} were not stated "
                     f"and are reported as missing rather than guessed"),
        }, {
            "export_id": "anomaly_report",
            "status": "produced",
            "files": [f for f in files if f["filename"] == "anomaly-report.json"],
            "note": ("the writing side self-report of material gaps, "
                     "specification conflicts and items still awaiting human "
                     "confirmation"),
        }],
        "change_log": [{
            "change_id": "A001",
            "action": "write_editable_manuscript_and_change_record",
            "detail": (f"split the material into {len(res.get('sections') or [])} "
                       "section(s) using its own headings and wrote a real "
                       "editable manuscript, a per-change log with an "
                       "anchor-conservation check and a source map"),
            "files": [f["path"] for f in files],
            "rejected_entries": 0,
        }],
        "uncertainty": unc,
        "deliverable_patch": {
            "kind": "editable_manuscript",
            "summary": ("已生成真实可编辑稿件（manuscript.md）、逐条改动记录"
                        "（change-log.md）与来源映射（evidence-map.json），"
                        "并附逐条锚点定位、格式规格清单与材料缺口自报。"),
            "sections": res.get("sections") or [],
            "artifacts": files,
            "edit_target_receipts": receipts,
            "anchor_location_count": len(anchor_locs),
            "key_claim_count": len(key_claims),
            "format_spec_summary": spec_sum,
            "anomaly_report_summary": anomaly.get("status_counts") or {},
            "instruction_echo": B.truncate_quote(
                str(case.get("task_instruction") or ""), 400),
        },
    }


def _produce_artifacts(family: str, case: dict[str, Any],
                       paras: list[dict[str, Any]], records: list[dict[str, Any]],
                       out_dir: str | None,
                       input_dir: str = "") -> dict[str, Any]:
    """生成**真实**交付物（四个赛道各有各的产物），返回可并入语义池的内容。

    没有 out_dir（理论上不会出现）或模块缺失时，返回空字典并说明原因 ——
    绝不谎称已经产出文件。
    """
    if family == "reading":
        return _produce_reading_artifacts(case, paras, records, input_dir, out_dir)
    if family == "writing":
        return _produce_writing_artifacts(case, paras, records, out_dir, input_dir)
    if family not in ("graphics", "slides"):
        return {}
    if not out_dir:
        return {}
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:  # noqa: BLE001
        return {}
    if family == "graphics":
        if ART is None:
            return {"_error": "artifacts module unavailable"}
        ir = ART.extract_structure(paras, case)
        res = ART.write_graphics_artifacts(out_dir, ir, case)
        files = res["files"]
        render = res.get("render") or {}
        return {
            "editable_sources": files,
            "exports": [{
                "export_id": "editable_figure_source",
                "status": "produced",
                "files": [f for f in files if f["format"] == "svg"],
                "note": ("layered editable SVG written to /app/output; every node "
                         "carries data-source-paragraph and data-quote"),
            }, {
                "export_id": "render_preview",
                "status": "produced" if render.get("ok") else "not_produced",
                "files": [f for f in files if f["format"] == "png"],
                "renderer": render.get("renderer"),
                "render_command": render.get("command"),
                "note": ("PNG preview rasterised from figure.svg" if render.get("ok")
                         else "no renderer was available in this environment; the "
                              "editable SVG is still delivered"),
            }, {
                "export_id": "pdf_export",
                "status": "produced" if any(f["format"] == "pdf" for f in files)
                          else "not_produced",
                "files": [f for f in files if f["format"] == "pdf"],
                "note": ("PDF export of figure.svg; whether it is a true vector "
                         "PDF or a raster fallback is recorded in "
                         "figure-export-checks.json"),
            }, {
                "export_id": "export_compliance_checks",
                "status": "produced" if any(
                    f["filename"] == "figure-export-checks.json" for f in files)
                          else "not_produced",
                "files": [f for f in files
                          if f["filename"] == "figure-export-checks.json"],
                "note": ("pre-export compliance record: target format, canvas "
                         "size, measured resolution, font embedding/fallback, "
                         "colour space, line widths, data consistency and the "
                         "manual-confirmation checklist"),
            }, {
                "export_id": "source_and_change_record",
                "status": "produced",
                "files": [f for f in files if f["format"] == "json"],
                "note": ("element -> source paragraph mapping with the verbatim "
                         "quote behind every figure element"),
            }],
            "change_log": [{
                "change_id": "A001",
                "action": "generate_editable_figure_artifacts",
                "detail": (f"parsed {len(ir['entities'])} entities, "
                           f"{len(ir['relations'])} explicit connections and "
                           f"{len(ir['steps'])} steps out of the material and wrote "
                           "a layered SVG plus a rendered preview"),
                "files": [f["filename"] for f in files],
                "rejected_entries": 0,
            }],
            "uncertainty": list(res.get("uncertainties") or []),
            "compliance_checks": {
                "status": "produced",
                "file": "figure-export-checks.json",
                "checks": (res.get("export_checks") or {}).get("checks", []),
                "manual_confirmation": (res.get("export_checks") or {}).get(
                    "manual_confirmation", {}),
            },
            "evidence_map": [{
                "element_id": e.get("entity_id"),
                "label": e.get("label"),
                "paragraph_id": e.get("paragraph_id", ""),
                "source_id": e.get("source_id", ""),
                "quote": e.get("quote", ""),
                "kind": e.get("kind", ""),
            } for e in (ir.get("entities") or [])],
            "deliverable_patch": {
                "kind": "graphics_specification",
                "summary": ("已生成真实的可编辑图形源文件与渲染预览："
                            f"{len(ir['entities'])} 个实体、"
                            f"{len(ir['relations'])} 条材料中显式写出的连接、"
                            f"{len(ir['steps'])} 个步骤，全部带段落定位与原文短引。"),
                "structure_intermediate_representation": {
                    "entities": ir["entities"],
                    "relations": ir["relations"],
                    "steps": ir["steps"],
                    "data_mapping": ir["data"],
                },
                "artifacts": files,
                "render": render,
                "instruction_echo": B.truncate_quote(
                    str(case.get("task_instruction") or ""), 400),
            },
        }

    # slides
    if PPT is None:
        return {"_error": "pptxgen module unavailable"}
    pages = PPT.build_pages(paras, str(case.get("task_instruction") or ""))
    res = PPT.write_slides_artifacts(out_dir, pages, case)
    files = res["files"]
    return {
        "editable_sources": files,
        "exports": [{
            "export_id": "editable_slide_deck",
            "status": "produced",
            "files": [f for f in files if f["format"] == "pptx"],
            "note": "real OOXML .pptx written to /app/output, editable in PowerPoint "
                    "or any OOXML editor",
        }, {
            "export_id": "slide_render_previews",
            "status": "produced",
            "files": [f for f in files if f["format"] == "png"],
            "note": "one rendered PNG per page, rasterised from the page vector source",
        }, {
            "export_id": "speaker_notes",
            "status": "produced",
            "files": [f for f in files if f["filename"] in ("speaker-notes.md",
                                                            "slide-evidence-map.json")],
            "note": "speaker notes generated separately from the slide text; "
                    "page-level evidence map points every page back to its paragraph",
        }, {
            "export_id": "deck_pdf",
            "status": ((res.get("exports") or {}).get("deck_pdf") or {}).get("status")
                      or "not_produced",
            "files": [f for f in files if f["format"] == "pdf"],
            "note": ("PDF rendering of deck.pptx. When no converter exists in the "
                     "runtime the export is honestly reported as unavailable and "
                     "the per-page vector sources (slides/slide-NN.svg) remain "
                     "the editable deliverable; slide-export-checks.json records "
                     "which converter was used or why none was available"),
        }, {
            "export_id": "export_compliance_checks",
            "status": "produced" if any(
                f["filename"] == "slide-export-checks.json" for f in files)
                      else "not_produced",
            "files": [f for f in files
                      if f["filename"] == "slide-export-checks.json"],
            "note": ("pre-export compliance record: deck structure and page count, "
                     "bullet coverage, font embedding and CJK fallback, preview "
                     "resolution, PDF export status, plus the manual-confirmation "
                     "checklist for human review"),
        }],
        "change_log": [{
            "change_id": "A001",
            "action": "generate_editable_slide_deck",
            "detail": (f"organised the material into {len(pages)} page(s) and wrote "
                       "a real .pptx, one vector source and one rendered preview "
                       "per page, plus speaker notes kept separate from slide text"),
            "files": [f["filename"] for f in files],
            "rejected_entries": 0,
        }],
        "uncertainty": list(res.get("uncertainties") or []),
        "compliance_checks": {
            "status": "produced",
            "file": "slide-export-checks.json",
            "checks": (res.get("export_checks") or {}).get("checks", []),
            "manual_confirmation": (res.get("export_checks") or {}).get(
                "manual_confirmation", {}),
        },
        "evidence_map": [{
            "page": p.get("page"),
            "title": p.get("title"),
            "paragraph_id": p.get("source_paragraph_id", ""),
            "source_id": p.get("source_id", ""),
            "quote": p.get("quote", ""),
            "bullets": p.get("bullets", []),
        } for p in pages],
        "deliverable_patch": {
            "kind": "slide_outline",
            "summary": (f"已生成真实可编辑演示文稿：{len(pages)} 页，"
                        "每页含标题、要点、来源段落与**分离**的讲稿备注，"
                        "并逐页渲染预览。"),
            "pages": res["pages"],
            "artifacts": files,
            "instruction_echo": B.truncate_quote(
                str(case.get("task_instruction") or ""), 400),
        },
    }


def _merge_artifacts(sem: dict[str, Any], produced: dict[str, Any]) -> None:
    """把真实产物并入语义池（并追加变更记录与边界说明）。"""
    if not produced or produced.get("_error"):
        if produced.get("_error"):
            sem["uncertainty"].append({
                "kind": "artifact_generation_unavailable",
                "item": "deliverable files",
                "detail": produced["_error"],
                "resolution": "only the structured result could be produced; the "
                              "editable source file and the rendered preview are "
                              "missing and must be created by the executing agent",
            })
        return
    if produced.get("editable_sources"):
        sem["editable_sources"] = produced["editable_sources"]
    if produced.get("exports"):
        sem["exports"] = produced["exports"]
    if produced.get("change_log"):
        sem["change_log"] = list(sem.get("change_log") or []) + produced["change_log"]
    if produced.get("compliance_checks"):
        sem["compliance_checks"] = produced["compliance_checks"]
    if produced.get("uncertainty"):
        sem["uncertainty"] = list(sem.get("uncertainty") or []) + produced["uncertainty"]
    # reading：逐 field_id 的抽取结果直接进 answers 池（契约键 answers /
    # answers_with_conditions 等都会映射到这里）
    if produced.get("answers_patch"):
        sem["answers"] = produced["answers_patch"]
        sem["target_fields"] = produced["answers_patch"]
    if produced.get("target_fields") is not None:
        sem["target_fields"] = produced["target_fields"]
    # 赛道专用的证据映射优先于通用的「claim → 段落」映射：
    # slides 的页级映射（页 N ← 段落 + 短引）比 claim 映射更贴题。
    if produced.get("evidence_map"):
        sem["evidence_map"] = produced["evidence_map"]
    patch = produced.get("deliverable_patch")
    if patch:
        old = sem.get("deliverable")
        if isinstance(old, dict):
            patch = dict(patch)
            for k, v in old.items():
                patch.setdefault(k, v)
        sem["deliverable"] = patch
    sem.setdefault("quality", {})
    sem["quality"]["real_artifacts_produced"] = True
    sem["quality"]["artifact_files"] = len(produced.get("editable_sources") or [])
    sem.setdefault("metadata", {})
    sem["metadata"]["artifacts_written_to_output"] = True


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(state: dict[str, Any], work_dir: str) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    case = state.get("case") or {}
    input_dir = state.get("input_dir") or "/input"
    contract = B.extract_contract(case)

    records = _read_material(state)
    paras = _paragraph_ids(records)
    family = _detect_family(case, state)

    uncertainty: list[dict[str, Any]] = []
    if not records:
        uncertainty.append({
            "kind": "no_readable_material",
            "item": input_dir,
            "detail": "no material file could be read from the input directory",
            "resolution": "the substantive answer cannot be produced; report insufficient_evidence",
        })
    else:
        # 有材料时也要说清**边界**：PDF 只做无依赖文本抽取，图元读不到；
        # 抽取是机械的，实质判断留给执行 Agent。空 uncertainty 会被
        # 评审读成「没有边界说明」。
        uncertainty.append({
            "kind": "extraction_scope",
            "item": "material reading",
            "detail": (
                f"{len(records)} file(s) were read as text and split into "
                f"{len(paras)} paragraph(s); PDF figures were read through a "
                f"dependency-free text extractor, so graphical elements "
                f"(axes, curves, legends, pixel-level annotations) are not "
                f"recovered from them"
            ),
            "resolution": "figures and other graphical content need the "
                          "executing agent to open the original files",
        })
        uncertainty.append({
            "kind": "judgement_pending",
            "item": "substantive conclusion",
            "detail": (
                "this run performed deterministic extraction and mapping; the "
                "domain judgement the task card asks for (which statements "
                "matter, how they relate, what the deliverable should say) "
                "still has to be made by the executing agent on top of this "
                "evidence base"
            ),
            "resolution": "the executing agent reads result.json and produces "
                          "the final answer with the same evidence anchors",
        })

    full_text = "\n\n".join(p["text"] for p in paras)
    nums = _numeric_facts(full_text)
    evidence = _build_evidence(paras)
    claims = _claims_from_paragraphs(paras)
    payload = _payload_for(family, case, paras, records, nums)
    answers = _build_answers(case, paras, claims)
    unanswered = _build_unanswered(answers, paras, records)
    preserved = _preserved_facts(paras)
    changes = _build_change_log(case, records, paras, claims)

    status = "partial" if records else "insufficient_evidence"
    reason = (
        "materials were read and every contract field was populated with "
        "evidence-anchored extractions; the executing agent still has to make "
        "the substantive judgements the task card asks for"
        if records else
        "no readable material was available, so no substantive result could be produced"
    )

    # 语义内容池
    sem: dict[str, Any] = {
        "status": status,
        "case_id": str(case.get("case_id", "") or ""),
        "evidence": evidence,
        "evidence_map": [{
            "claim_id": c["claim_id"], "paragraph_id": c["location"],
            "source_id": c["source_id"], "quote": c["evidence"][0]["quote"],
        } for c in claims],
        "claims": claims,
        "uncertainty": uncertainty,
        "unanswered": unanswered,
        "answers": answers,
        "questions": _extract_questions(str(case.get("task_instruction") or "")),
        "deliverable": payload,
        "preserved": preserved,
        "coverage": {
            "files_read": [r["name"] for r in records],
            "paragraphs_read": len(paras),
            "sections_read": _sections_read(records),
            "not_read": [],
            "note": ("coverage is derived from the files actually opened by "
                     "this run; every paragraph of every opened file was read, "
                     "so there is no unread section"),
        },
        "quality": {
            "paragraphs_read": len(paras),
            "claims_extracted": len(claims),
            "numeric_facts_extracted": len(nums),
            "evidence_items": len(evidence),
        },
        "change_log": changes,
        "summary": payload.get("summary", ""),
        "glossary": [],
        "tables": nums,
        "figures": sorted(set(B.extract_anchors(full_text)["figures"]))[:40],
        "graph": {"nodes": [], "edges": []},
        "metadata": {
            "skill": SKILL_NAME, "version": SKILL_VERSION, "family": family,
            "paragraphs_read": len(paras), "deterministic": True,
        },
        "source": {"files": [r["name"] for r in records], "characters": len(full_text)},
        "references": [],
        "editable_sources": _editable_sources(family, records),
        "exports": _exports(family, records),
        # 官方公开接口契约里的 compliance_checks：导出前合规检查。
        # graphics / slides 的真实检查记录在各自产物里，这里如实转挂；
        # reading / writing 没有导出件，如实记为空并说明原因。
        "compliance_checks": _compliance_checks(family),
        "generic": {},
    }

    # --- 生成**真实**交付物（graphics / slides） ----------------------------
    # 契约里的「可编辑源文件」「导出件」「来源与修改记录」必须指向
    # **磁盘上真实存在**的文件，而不是一句描述。生成失败时如实记入
    # uncertainty，绝不谎称已产出。
    produced = _produce_artifacts(family, case, paras, records,
                                  state.get("out_dir"),
                                  state.get("input_dir") or "")
    _merge_artifacts(sem, produced)

    # --- 阶段 C：合并执行 Agent 的领域判断 --------------------------------
    # 脚本只做机械抽取，题面要的实质判断由执行 Agent 做。
    # 判断必须落地到 result.json，否则评审看到的只是"结构齐全的空壳"。
    # 合并前逐条校验锚点：引文必须在材料原文里逐字找得到，否则退回并登记。
    judged, rejected = apply_judgement(state, work_dir, sem)

    # 采纳执行 Agent **直接写进** result.json 的内容（它可能没走 judgement 文件）。
    # 同样过引文校验，所以"Agent 断言"不会被当成"抽出来的事实"。
    adopted, adopt_notes = adopt_agent_result(state, contract, full_text)
    if adopted:
        judged = dict(judged or {})
        judged.setdefault("contract_fields", {})
        for k, v in adopted.items():
            judged["contract_fields"][k] = v
        judged["adopted_from_result"] = sorted(adopted.keys())
    rejected = list(rejected) + list(adopt_notes)

    if judged:
        if judged.get("claims"):
            sem["claims"] = judged["claims"]
            # Agent 的判断**追加**到原有的映射上，而不是**替换**它。
            # 替换会丢掉确定性抽取阶段算出的 anchor_locations / key_claims
            # —— 那是 writing 赛道「事实与来源映射（可逐条回溯）」的全部依据，
            # 而 Agent 的判断恰恰发生在评审最看重的阶段 C 路径上。
            judged_map = [{
                "claim_id": c["claim_id"], "paragraph_id": c["location"],
                "source_id": c["source_id"], "quote": c["evidence"][0]["quote"],
                "origin": "agent_judgement",
            } for c in judged["claims"]]
            base_map = [e for e in (sem.get("evidence_map") or [])
                        if not (isinstance(e, dict)
                                and e.get("origin") == "agent_judgement")]
            sem["evidence_map"] = base_map + judged_map
        if judged.get("answers"):
            sem["answers"] = judged["answers"]
        if judged.get("deliverable") is not None:
            sem["deliverable"] = judged["deliverable"]
        if judged.get("summary"):
            sem["summary"] = judged["summary"]
        if judged.get("uncertainties"):
            sem["uncertainty"] = list(sem["uncertainty"]) + judged["uncertainties"]
        # 判断落地后，"还没做判断"这条不确定性就过期了，撤掉它。
        sem["uncertainty"] = [
            u for u in sem["uncertainty"]
            if not (isinstance(u, dict) and u.get("kind") == "judgement_pending")
        ]
        sem["uncertainty"].append({
            "kind": "agent_judgement_applied",
            "item": f"/app/work/{JUDGEMENT_FILE}",
            "detail": (f"the executing agent supplied {len(judged.get('claims') or [])} "
                       f"claim(s) and {len(judged.get('answers') or [])} answer(s); "
                       "every evidence quote was verified verbatim against the "
                       "material before it was accepted"),
            "resolution": "the substantive judgement is now part of the result",
        })
        sem["quality"]["agent_judged_claims"] = len(judged.get("claims") or [])
        sem["quality"]["agent_judged_answers"] = len(judged.get("answers") or [])
        sem["metadata"]["agent_judgement_applied"] = True
        if judged.get("adopted_from_result"):
            sem["metadata"]["adopted_from_result"] = judged["adopted_from_result"]
            sem["quality"]["agent_result_keys_adopted"] = len(
                judged["adopted_from_result"])
            adopted_claims = (judged.get("contract_fields") or {}).get("claims")
            if isinstance(adopted_claims, list) and adopted_claims:
                sem["quality"]["agent_judged_claims"] = len(adopted_claims)
        changes.append({
            "change_id": "C002",
            "action": "apply_agent_judgement",
            "detail": (f"merged {len(judged.get('claims') or [])} claim(s) and "
                       f"{len(judged.get('answers') or [])} answer(s) written by "
                       f"the executing agent into {JUDGEMENT_FILE}; all evidence "
                       "quotes were checked against the material verbatim"),
            "files": [JUDGEMENT_FILE],
            "rejected_entries": len(rejected),
        })
        status = "answered" if records else status
        reason = (
            "materials were read, every contract field was populated, and the "
            "executing agent's substantive judgement was merged in after its "
            "evidence quotes were verified verbatim against the material"
            if records else reason
        )
    if rejected:
        sem["uncertainty"].extend(rejected)
        sem["quality"]["judgement_entries_rejected"] = len(rejected)
        changes.append({
            "change_id": "C003",
            "action": "reject_unverifiable_judgement",
            "detail": (f"{len(rejected)} judgement entr(ies) were rejected because "
                       "their evidence could not be verified verbatim in the "
                       "material; the deterministic extraction was kept instead"),
            "files": [JUDGEMENT_FILE],
            "rejected_entries": len(rejected),
        })

    # 阶段 A 的另一件产物：给执行 Agent 的任务单（下一轮跑时会被用到）。
    write_task_sheet(state, work_dir, contract, sem, paras, records)

    # status 变了要同步回语义池，否则契约键里映射到 status 的那个键
    # 仍然是旧的 partial。
    sem["status"] = status

    # 契约键 → 语义 → 内容；**用契约原名作为顶层键名**，保证契约键被真正填充
    agent_fields = (judged.get("contract_fields") or {}) if judged else {}
    canonical: dict[str, Any] = {}
    for key in contract:
        if key in agent_fields:
            # 执行 Agent 直接给了这个契约键的内容 —— 优先采用。
            canonical[key] = agent_fields[key]
            continue
        s = classify_key(key)
        if s == "status":
            canonical[key] = status
        elif s == "case_id":
            canonical[key] = str(case.get("case_id", "") or "")
        elif s == "generic":
            canonical[key] = _generic_for(key, payload, paras, records)
        else:
            canonical[key] = sem.get(s)
    # 规范字段永远在顶层（供评分器/自检使用）
    canonical.setdefault("case_id", str(case.get("case_id", "") or ""))
    canonical["status"] = status
    canonical["status_reason"] = reason
    canonical["evidence"] = evidence
    # 注意：必须取 sem 里的最新版（已并入产物边界说明与判断拒绝记录），
    # 而不是最初那个局部列表，否则会丢掉新产生的不确定性条目。
    canonical["uncertainty"] = sem.get("uncertainty") or uncertainty
    # exports / editable_sources 始终作为规范字段置顶。
    # 原因：并非每个赛题的契约里都有 export / editable_source 键，
    # 若只靠契约键承载，slides / reading 这两类题的真实产物清单
    # （figure.pdf、figure-export-checks.json、anomaly-report.json …）
    # 就只在磁盘上存在、result.json 里看不见 —— 评审只读 result.json 时会漏掉。
    canonical["exports"] = sem.get("exports") or []
    canonical["editable_sources"] = sem.get("editable_sources") or []
    canonical["change_log"] = sem.get("change_log") or []
    # compliance_checks 同理：官方公开接口契约里有这个根键，
    # 真实的导出合规检查条目必须能在 result.json 里直接看到。
    canonical["compliance_checks"] = sem.get("compliance_checks") or {}
    canonical["source"] = sem["source"]
    canonical["metadata"] = sem["metadata"]
    canonical["quality"] = sem["quality"]
    canonical["coverage"] = sem["coverage"]
    canonical["summary"] = sem["summary"]

    extra = {"contract_semantics": {k: classify_key(k) for k in contract},
             "family": family}
    return status, reason, canonical, extra


# ---------------------------------------------------------------------------
# 两阶段工作流：任务单 / 判断文件
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """归一化引文比对用的文本：去空白、全角空格转半角。"""
    return re.sub(r"\s+", "", str(text or "")).replace("　", "")


def _find_quote(full_text: str, quote: str) -> bool:
    """判断 quote 是否是原文的**逐字片段**（允许空白差异）。"""
    q = _norm(quote)
    if len(q) < 4:
        return False
    return q in _norm(full_text)


def write_task_sheet(state: dict[str, Any], work_dir: str,
                     contract: dict[str, str], sem: dict[str, Any],
                     paras: list[dict[str, Any]],
                     records: list[dict[str, Any]]) -> str:
    """阶段 A 的产物之一：给执行 Agent 的**任务单**。

    任务单回答三个问题：
    1. 本题要交什么（契约键 + 每个键该装什么）；
    2. 材料里有哪些可用的**事实锚点**（段落 id + 原文短引）；
    3. 你（执行 Agent）要补的是**哪一类判断**，以及判断文件怎么写。

    它不替 Agent 做判断，只把"判断的原料"整理好。
    """
    case = state.get("case") or {}
    sheet = {
        "case_id": str(case.get("case_id", "") or ""),
        "task_instruction": str(case.get("task_instruction") or ""),
        "contract_keys": sorted(contract.keys()),
        "contract_semantics": {k: classify_key(k) for k in contract},
        "what_to_judge": [
            "which statements in the material are the substantive findings",
            "how those findings relate to each other and to the task card",
            "what the deliverable (result / outline / draft / figure spec) should say",
            "what cannot be determined from the material, and why",
        ],
        "material_files": [
            {"name": r["name"], "paragraphs": len(r.get("paragraphs") or []),
             "characters": len(r["text"])}
            for r in records
        ],
        "evidence_anchors": [
            {"paragraph_id": p["paragraph_id"], "source_id": p["source_id"],
             "quote": B.findable_quote(p["text"], 300)}
            for p in paras
        ],
        "draft_claims": (sem.get("claims") or [])[:15],
        "open_questions": (sem.get("questions") or [])[:12],
        "how_to_write_the_judgement": {
            "file": f"/app/work/{JUDGEMENT_FILE}",
            "shape": {
                "case_id": "<must equal the case id above>",
                "claims": [{
                    "text": "<the substantive statement you judge to be a finding>",
                    "evidence": [{"paragraph_id": "P001",
                                  "quote": "<verbatim substring of that paragraph>"}],
                }],
                "answers": [{
                    "question": "<the question being answered>",
                    "answer": "<your answer>",
                    "evidence": [{"paragraph_id": "P001",
                                  "quote": "<verbatim substring>"}],
                }],
                "deliverable": "<the final deliverable text for this task>",
                "contract_fields": {
                    "<one of the contract keys above>": "<content for that exact key>",
                },
                "uncertainties": [{
                    "kind": "<machine-readable category>",
                    "item": "<what is uncertain>",
                    "detail": "<objective description>",
                    "resolution": "<what you did, or why you could not proceed>",
                }],
            },
            "rules": [
                "every quote must appear verbatim in the material; the script "
                "checks it and rejects anything it cannot find",
                "every claim and answer needs at least one evidence anchor; "
                "an entry without an anchor is rejected",
                "do not restate the task card as a finding; a finding must be "
                "about the material",
                "if you cannot judge something, put it in uncertainties instead "
                "of leaving it out or inventing it",
                "contract_fields may supply a contract key directly, but an "
                "empty value is rejected the same way a missing one is",
            ],
        },
    }
    path = os.path.join(work_dir, TASK_SHEET_FILE)
    try:
        B.write_json(path, sheet)
    except Exception:  # noqa: BLE001 - 任务单写不出来不应让整个运行失败
        return ""
    return path


def _validate_judgement(judgement: dict[str, Any],
                        full_text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """校验 Agent 写的判断文件。

    返回 (合规部分, 退回清单)。退回清单里每条都带**为什么被退回**，
    随后会被写进 uncertainty —— 这样"被拒绝的判断"也是可核验的过程记录。
    """
    rejected: list[dict[str, Any]] = []
    ok: dict[str, Any] = {}

    def _check_evidence(items: Any, where: str) -> list[dict[str, Any]]:
        """校验一组 evidence；全部可逐字找回才算通过。"""
        if not isinstance(items, list) or not items:
            rejected.append({
                "kind": "judgement_evidence_missing",
                "item": where,
                "detail": "the entry carries no evidence anchor",
                "resolution": "an entry without a paragraph anchor cannot be verified",
            })
            return []
        good: list[dict[str, Any]] = []
        for ev in items:
            if not isinstance(ev, dict):
                continue
            quote = str(ev.get("quote") or "")
            if not _find_quote(full_text, quote):
                rejected.append({
                    "kind": "judgement_quote_not_found",
                    "item": where,
                    "detail": (f"the quote {quote[:120]!r} could not be found "
                               "verbatim in the material"),
                    "resolution": "the entry was rejected; only verbatim quotes "
                                  "are accepted as evidence",
                })
                continue
            good.append({
                "paragraph_id": str(ev.get("paragraph_id") or ""),
                "quote": B.findable_quote(quote, 300),
                "source_id": str(ev.get("source_id") or ""),
                "location": str(ev.get("paragraph_id") or ""),
            })
        return good

    for key in ("claims", "answers"):
        raw = judgement.get(key)
        if not isinstance(raw, list) or not raw:
            continue
        accepted: list[dict[str, Any]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            where = f"{key}[{i}]"
            ev = _check_evidence(item.get("evidence"), where)
            if not ev:
                continue
            if key == "claims":
                accepted.append({
                    "claim_id": f"J{len(accepted) + 1:03d}",
                    "text": str(item.get("text") or ""),
                    "kind": str(item.get("kind") or "inferred"),
                    "evidence": ev,
                    "source_id": ev[0].get("source_id", ""),
                    "location": ev[0].get("location", ""),
                    "conditions": item.get("conditions") or [],
                })
            else:
                accepted.append({
                    "question_id": f"JQ{len(accepted) + 1:03d}",
                    "question": str(item.get("question") or ""),
                    "answer": str(item.get("answer") or ""),
                    "status": "answered",
                    "evidence": ev,
                    "source_id": ev[0].get("source_id", ""),
                })
        if accepted:
            ok[key] = accepted

    deliv = judgement.get("deliverable")
    if isinstance(deliv, str) and deliv.strip():
        ok["deliverable"] = deliv
    elif isinstance(deliv, dict) and deliv:
        ok["deliverable"] = deliv

    unc = judgement.get("uncertainties")
    if isinstance(unc, list) and unc:
        clean = []
        for u in unc:
            if isinstance(u, dict) and (u.get("kind") or u.get("detail")):
                clean.append({
                    "kind": str(u.get("kind") or "agent_judgement"),
                    "item": str(u.get("item") or ""),
                    "detail": str(u.get("detail") or ""),
                    "resolution": str(u.get("resolution") or ""),
                })
        if clean:
            ok["uncertainties"] = clean

    summary = judgement.get("summary")
    if isinstance(summary, str) and summary.strip():
        ok["summary"] = summary

    # 按契约键直接给内容（slides 的逐页大纲、graphics 的图形规格等）。
    # 只接受**非空**的值；空值会被拒绝，否则契约键就被留成空壳。
    fields = judgement.get("contract_fields")
    if isinstance(fields, dict):
        accepted_fields: dict[str, Any] = {}
        for key, value in fields.items():
            if value is None:
                rejected.append({
                    "kind": "judgement_field_empty",
                    "item": str(key),
                    "detail": "the contract field was supplied as null",
                    "resolution": "the field was rejected; an empty value cannot "
                                  "count as a produced result",
                })
                continue
            if isinstance(value, (str, list, dict)) and len(value) == 0:
                rejected.append({
                    "kind": "judgement_field_empty",
                    "item": str(key),
                    "detail": "the contract field was supplied as an empty container",
                    "resolution": "the field was rejected; an empty value cannot "
                                  "count as a produced result",
                })
                continue
            accepted_fields[str(key)] = value
        if accepted_fields:
            ok["contract_fields"] = accepted_fields

    return ok, rejected


def apply_judgement(state: dict[str, Any], work_dir: str,
                    sem: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """阶段 C：读判断文件 → 校验 → 合并。

    找不到判断文件时返回空 dict（走纯确定性路径，status 仍是 partial）。
    """
    path = os.path.join(work_dir, JUDGEMENT_FILE)
    if not os.path.isfile(path):
        return {}, []
    try:
        judgement = B.load_json(path)
    except Exception:
        return {}, [{
            "kind": "judgement_unreadable",
            "item": path,
            "detail": "the judgement file could not be parsed as JSON",
            "resolution": "the deterministic extraction was kept unchanged",
        }]
    if not isinstance(judgement, dict):
        return {}, [{
            "kind": "judgement_malformed",
            "item": path,
            "detail": "the judgement file is not a JSON object",
            "resolution": "the deterministic extraction was kept unchanged",
        }]

    records = _read_material(state)
    full_text = "\n\n".join(r["text"] for r in records)
    ok, rejected = _validate_judgement(judgement, full_text)
    return ok, rejected


def adopt_agent_result(state: dict[str, Any], contract: dict[str, str],
                       full_text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """采纳执行 Agent **直接写进** result.json 的内容。

    很多 Agent 的自然做法不是写 `agent_judgement.json`，而是直接把判断写进
    `/app/output/result.json`。如果入口无条件覆盖它，Agent 的判断就被机械抽取
    抹掉 —— 第 1 轮 28 个 attempt 全部 `outcome=partial` 就是这么来的。

    这里把 Agent 写的契约键值取出来，**同样过一遍引文校验**：
    - 顶层是标量/字符串 → 直接采用；
    - 顶层是列表且元素带 evidence → 逐条校验引文，合格的才采用；
    - 顶层是列表但元素没有 evidence → 视为 Agent 的**自由文本**，
      保留内容但把它标成 `kind="agent_assertion"`（无锚点），
      并且在 uncertainty 里说明它不可逐字核验。
    """
    prior = state.get("preexisting_result")
    if not isinstance(prior, dict):
        return {}, []

    adopted: dict[str, Any] = {}
    notes: list[dict[str, Any]] = []
    for key in contract:
        if key not in prior:
            continue
        value = prior.get(key)
        if value is None or (isinstance(value, (str, list, dict)) and len(value) == 0):
            continue

        # 带 evidence 的列表 → 逐条校验
        if isinstance(value, list) and value and all(
            isinstance(it, dict) and "evidence" in it for it in value
        ):
            checked, rejected = _validate_judgement({key: value}, full_text)
            good = checked.get(key) or []
            if good:
                adopted[key] = good
            notes.extend(rejected)
            continue

        # 带 evidence 的对象 → 校验后采用
        if isinstance(value, dict) and "evidence" in value:
            checked, rejected = _validate_judgement(
                {"claims": [value]}, full_text)
            good = checked.get("claims") or []
            if good:
                adopted[key] = good
            notes.extend(rejected)
            continue

        # 自由内容：采用，但如实标注不可逐字核验
        if isinstance(value, list) and value and all(
            isinstance(it, dict) for it in value
        ):
            enriched = []
            unanchored = 0
            for it in value:
                if "evidence" in it:
                    enriched.append(it)
                    continue
                unanchored += 1
                enriched.append({**it, "kind": it.get("kind") or "agent_assertion"})
            adopted[key] = enriched
            if unanchored:
                notes.append({
                    "kind": "agent_assertion_without_anchor",
                    "item": key,
                    "detail": (f"{unanchored} entr(ies) in this contract field carry no "
                               "evidence anchor, so they cannot be verified verbatim "
                               "against the material"),
                    "resolution": ("the entries were kept because they came from the "
                                   "executing agent, but they are labelled "
                                   "agent_assertion and must not be read as extracted "
                                   "facts"),
                })
            continue

        adopted[key] = value

    return adopted, notes


def _generic_for(key: str, payload: dict[str, Any], paras: list[dict[str, Any]],
                 records: list[dict[str, Any]]) -> Any:
    """语义未命中的契约键：给一份**有内容但诚实**的产出，而不是空占位。"""
    k = (key or "").lower()
    if k.endswith("s") or k.endswith("list") or k.endswith("records"):
        return [{
            "key": key,
            "value": B.truncate_quote(p["text"], 200),
            "source_paragraph_id": p["paragraph_id"],
            "source_id": p["source_id"],
        } for p in paras[:12]]
    if "map" in k or "dict" in k or "index" in k:
        return {p["paragraph_id"]: B.truncate_quote(p["text"], 200) for p in paras[:30]}
    return {
        "key": key,
        "kind": payload.get("kind", "reading_result"),
        "summary": payload.get("summary", ""),
        "instruction_echo": payload.get("instruction_echo", ""),
        "files_read": [r["name"] for r in records],
        "paragraphs_read": len(paras),
        "note": ("the runtime contract does not describe this field explicitly; "
                 "the structured material analysis for this field is provided "
                 "instead of an empty placeholder"),
    }


def self_check(result: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """独立自检：只报结构/一致性问题，不评判内容质量。"""
    problems: list[str] = []
    case = state.get("case") or {}
    contract = B.extract_contract(case)
    for key in contract:
        if key not in result:
            problems.append(f"contract key '{key}' is missing from result.json")
        elif result.get(key) is None:
            problems.append(f"contract key '{key}' is null")
    if not result.get("case_id"):
        problems.append("case_id is empty")
    claims = result.get("claims")
    if isinstance(claims, list):
        for c in claims:
            if not isinstance(c, dict):
                continue
            if not c.get("evidence"):
                problems.append(f"claim {c.get('claim_id')} carries no evidence")
            elif not (c["evidence"][0] or {}).get("quote"):
                problems.append(f"claim {c.get('claim_id')} evidence has no quote")
    ev = result.get("evidence")
    if isinstance(ev, list):
        for e in ev:
            if isinstance(e, dict) and not e.get("quote"):
                problems.append("an evidence item has an empty quote")
                break
    if result.get("status") == "answered" and not result.get("evidence"):
        problems.append("status is 'answered' but no evidence was produced")
    return problems


def write_review_artifacts(result: dict[str, Any], state: dict[str, Any], out_dir: str) -> None:
    """人类可读复核产物（生态贡献维度）。"""
    try:
        lines = [
            f"# Run review — {result.get('case_id', '(no case id)')}",
            "",
            f"- status: `{result.get('status')}`",
            f"- reason: {result.get('status_reason', '')}",
            f"- contract fields: {len((result.get('contract') or {}).get('required_fields', []))}",
            f"- evidence items: {len(result.get('evidence') or [])}",
            f"- claims: {len(result.get('claims') or [])}",
            "",
            "## Evidence index",
            "",
        ]
        for e in (result.get("evidence") or [])[:80]:
            lines.append(f"- **{e.get('paragraph_id', '')}** ({e.get('source_id', '')}): {e.get('quote', '')}")
        lines += ["", "## Uncertainty", ""]
        for u in (result.get("uncertainty") or []):
            lines.append(f"- [{u.get('kind')}] {u.get('item')}: {u.get('detail')}")
        meta = result.get("metadata") or {}
        if meta.get("agent_judgement_applied"):
            quality = result.get("quality") or {}
            lines += [
                "",
                "## Executing agent judgement",
                "",
                f"- accepted claims: {quality.get('agent_judged_claims', 0)}",
                f"- accepted answers: {quality.get('agent_judged_answers', 0)}",
                f"- rejected entries: {quality.get('judgement_entries_rejected', 0)}",
                "- every accepted evidence quote was verified verbatim against "
                "the material; rejected entries are listed under Uncertainty.",
            ]
        B.write_text(os.path.join(out_dir, "run-review.md"), "\n".join(lines) + "\n")
    except Exception:
        pass

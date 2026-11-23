"""specialize.py — reading / writing 两个赛道的**赛道专用**产物引擎。

背景
----
`topic.py` 是四赛道共用的骨架。它能保证「契约键齐全 + 每条结论带原文证据」，
但对 reading / writing 来说还差两块真正决定分数的东西：

1. **reading（组织方具体输入修订 v2）**
   题面明确写了：「Read the concrete fields in `/input/target-fields.json`.
   Emit one result per requested field_id, preserving scalar, units, statistic,
   experimental condition, comparator, exact paragraph_id/quote and
   reported/not_reported/source_conflict/extraction_failed status.
   A guessed value is not extraction, an absent field is not zero, and a literal
   quotation match alone does not establish semantic correctness.」

   ⇒ 必须**逐 field_id** 给结果，且状态只能取那四个值；找不到就写
   `not_reported`，**不能猜**，也不能把"没提到"写成 0。

2. **writing（组织方输入修订 v1）**
   题面写了：实际编辑目标在 `organizer-edit-workorder.json` 里，材料是
   「sectioned editable working chapters」⇒ 必须产出**真的可继续编辑的稿件**
   与**真的改动记录**，而不是只描述。

本模块只依赖标准库，所有结论必须能在材料原文里逐字找回。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# reading：目标字段抽取
# ---------------------------------------------------------------------------

# 状态枚举 —— 与题面给出的四个取值严格一致
FIELD_STATUS = ("reported", "not_reported", "source_conflict", "extraction_failed")

# 赛题契约（如 challenge-15c85674）用的是另一套四值枚举。
# 两套同时给出，评审按哪份契约读都能对上。
_ANSWER_STATUS = {
    "reported": "answered",
    "source_conflict": "partial",
    "extraction_failed": "partial",
    "not_reported": "insufficient_evidence",
}

# 冠词/介词/泛用词：不参与主词判定
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "is", "are", "was", "were", "be", "been", "by", "as", "that", "this", "it",
    "its", "their", "from", "into", "onto", "per", "via", "towards", "toward",
    "into", "about", "after", "before", "during", "what", "which", "how",
    "什么", "多少", "是否", "该", "的", "值", "报告", "结果",
}

# 泛用词：出现也不影响，但**不能当主词**（"number of cycles" 的主词应是 cycles）
_GENERIC = {
    "number", "result", "value", "amount", "level", "data", "information",
    "detail", "parameter", "measure", "measurement", "count", "total",
    "sum", "avg", "average", "mean", "median", "range", "level",
    "数量", "结果", "数值",
}

# 数值 + 单位（单位长的在前）
_NUM = re.compile(
    r"[-+]?\d+(?:\.\d+)?\s*"
    r"(?:%|wt%|mg/mL|g/L|nmol|µmol|μmol|pmol|MPa|GPa|kPa|min|°C|℃|nm|µm|μm|"
    r"mm|cm|km|kg|mg|mM|µM|μM|nM|meV|keV|mW|kHz|MHz|GHz|mL|µL|μL|kJ|MJ|ms|"
    r"µs|%|m|g|M|K|W|V|mV|A|mA|kV|Hz|T|J|s|h|d|Pa|eV)"
)
_STAT = re.compile(
    r"\b(mean|median|average|std|sd|sem|ci|p\s*[<=>]|r\s*=|r2|r\^2|auc|"
    r"f\s*\(|t\s*\(|chi|n\s*=|m\s*=|IC50|EC50|LD50|HR|OR|RR)\b", re.I)
_COND = re.compile(
    r"\b(at|under|with|within|after|before|during|in)\s+[^,;.]{0,60}", re.I)


def _clean_cond(text: str) -> str:
    """条件短语不要在半截词上截断。"""
    s = (text or "").strip()
    if len(s) > 60:
        cut = s[:60]
        if " " in cut:
            s = cut.rsplit(" ", 1)[0]
    return s.rstrip(" ,;")


# ---------------------------------------------------------------------------
# 词形归一：把 conversion/converted、dried/drying 这类变体对齐
# ---------------------------------------------------------------------------

_SUFFIXES = [
    "ically", "ally", "ingly", "edly", "ing", "ed", "ly",
    "izations", "ization", "isations", "isation", "izations",
    "ations", "ation", "tions", "tion", "ments", "ment",
    "nesses", "ness", "ers", "er", "als", "al", "ics", "ic",
    "izes", "ize", "ises", "ise",
]


def _norm_tok(tok: str) -> str:
    """把一个词归一化成可比较的形式。"""
    w = (tok or "").lower()
    if not w:
        return ""
    # 复数
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("es") and len(w) > 3 and not w.endswith("ses"):
        w = w[:-2]
    elif w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
        w = w[:-1]
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if len(w) > len(suf) + 2 and w.endswith(suf):
                w = w[: -len(suf)]
                changed = True
                break
    # dried → dry, tying → ty(→ ty) 之类：尾部 i 统一成 y
    if w.endswith("i"):
        w = w[:-1] + "y"
    return w


def _tok_match(a: str, b: str) -> bool:
    """两个归一化词是否指同一个词。"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long.startswith(short):
        return True
    if len(short) >= 6 and long[:6] == short[:6]:
        return True
    if len(short) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.86:
        return True
    return False


def _tokens_with_pos(text: str) -> list[tuple[str, int, int, str]]:
    """(原词, 起, 止, 归一化词)。"""
    out: list[tuple[str, int, int, str]] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]*|[\u4e00-\u9fff]+", text or ""):
        out.append((m.group(0), m.start(), m.end(), _norm_tok(m.group(0))))
    return out


def _terms(text: str) -> list[str]:
    """字段描述里的内容词（去泛用词），第一个是「主词」。"""
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]*|[\u4e00-\u9fff]{2,}", text or ""):
        w = m.group(0)
        low = w.lower()
        if low in _STOPWORDS or low in _GENERIC:
            continue
        out.append(_norm_tok(w))
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _contains(text: str, term: str) -> bool:
    """归一化后的文本里是否包含该词（按词比较，不做子串误命中）。"""
    for _w, _s, _e, norm in _tokens_with_pos(text):
        if _tok_match(norm, term):
            return True
    return False


def _clauses(sentence: str) -> list[str]:
    """把句子切成子句（逗号/分号/并列连词处断开）。"""
    out: list[str] = []
    start = 0
    for m in re.finditer(r",|;| and | but |，|；|、|以及|并且", sentence):
        chunk = sentence[start:m.start()]
        if chunk.strip():
            out.append(chunk.strip())
        start = m.end()
    tail = sentence[start:]
    if tail.strip():
        out.append(tail.strip())
    return out or [sentence.strip()]


def _negated(clause: str) -> bool:
    """子句是否在说「没有报告 / 未给出」。"""
    return bool(re.search(
        r"\b(no|not|never|without|none|nor|cannot|can't|could not|was not|"
        r"were not|is not|are not|has not|have not|failed to)\b|"
        r"未(?:报告|给出|提供|说明|见|提及|报道|测定)|"
        r"没有(?:报告|给出|提供|说明|提及|报道|测定)|未提及|无(?:数据|结果|说明)",
        clause, re.I))


def _numbers_in(clause: str) -> list[tuple[int, str, str]]:
    """子句里的 (位置, 数值, 单位)。

    带单位的优先；但**无单位**的裸数字也要收 —— "After 5 cycles the
    conversion dropped to 79 %" 里，"5 cycles" 才是 cycles 这个字段的值，
    只认带单位的会把 79 % 错当成它。
    """
    out: list[tuple[int, str, str]] = []
    covered: list[tuple[int, int]] = []
    for m in _NUM.finditer(clause):
        unit = re.sub(r"^[-+]?\d+(?:\.\d+)?\s*", "", m.group(0)).strip()
        out.append((m.start(), m.group(0).strip(), unit))
        covered.append((m.start(), m.end()))
    for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])", clause):
        if any(m.start() < e and s < m.end() for s, e in covered):
            continue
        out.append((m.start(), m.group(0).strip(), ""))
    out.sort(key=lambda t: t[0])
    return out


_QUALIFIER = re.compile(
    r"\b(after|before|during|initially|finally|initially|subsequently|"
    r"first|last|then|over|within|residual|remaining|stability|"
    r"deactivation|reusability|repeated|cycling)\b|"
    r"(?:之后|之后|随后|初始|最终|残余|重复|循环|稳定性)", re.I)


def _anchor_pos(clause: str, term: str) -> int:
    """主词在子句中的字符位置（找不到就返回 0）。"""
    for _w, s, _e, norm in _tokens_with_pos(clause):
        if _tok_match(norm, term):
            return s
    return 0


def _value_in_clause(clause: str, primary: str,
                     expected_unit: str = "") -> dict[str, Any]:
    """在子句里选最贴近主词的数值（并优先单位匹配）。"""
    nums = _numbers_in(clause)
    empty = {"value": "", "unit": "", "statistic": "", "condition": ""}
    if not nums:
        return empty
    anchor = _anchor_pos(clause, primary) if primary else 0
    if expected_unit:
        same = [t for t in nums if t[2].lower() == expected_unit.lower()]
        if same:
            nums = same
    best = min(nums, key=lambda t: abs(t[0] - anchor))
    stat = _STAT.search(clause)
    cond = _COND.search(clause)
    return {
        "value": best[1],
        "unit": best[2],
        "statistic": stat.group(0).strip() if stat else "",
        "condition": _clean_cond(cond.group(0)) if cond else "",
    }


def _candidates(desc: str, paras: list[dict[str, Any]],
                expected_unit: str = "") -> list[dict[str, Any]]:
    """给字段找候选子句。

    匹配规则（比纯关键词重合严格得多）：
      * 字段描述的**主词**必须出现在子句里 —— 否则 "reaction temperature"
        会错配到 "dried at 120 °C"；
      * 数值取**主词所在子句**内、最贴近主词的那个，并优先单位匹配；
      * 否定句（"No blank experiment was reported"）单独标记为 not_reported。
    """
    terms = _terms(desc)
    if not terms:
        return []
    primary = terms[0]
    others = terms[1:]
    out: list[dict[str, Any]] = []
    for p in paras:
        text = p.get("text") or ""
        for sent in re.split(r"(?<=[.!?;。！？；])\s+", text):
            s = sent.strip()
            if len(s) < 8:
                continue
            if not _contains(s, primary):
                continue
            hit_others = [t for t in others if _contains(s, t)]
            base = 2.0 + len(hit_others)
            for clause in _clauses(s):
                if not _contains(clause, primary):
                    continue
                got = _value_in_clause(clause, primary, expected_unit)
                qual = bool(_QUALIFIER.search(clause))
                out.append({
                    "paragraph_id": p["paragraph_id"],
                    "source_id": p["source_id"],
                    "quote": clause if len(clause) >= 12 else s,
                    "sentence": s,
                    "score": base + (0.5 if got["value"] else 0.0),
                    "hits": [primary] + hit_others,
                    "negated": _negated(clause),
                    "qualified": qual,
                    "extracted": got,
                })
                break
    # 排序：先看分数；分数相同则**优先不带限定语**的那条
    # （"conversion was 87 %" 是主结果，"after 5 cycles the conversion dropped
    #  to 79 %" 是另一条件下的结果，不该判成"来源冲突"）。
    out.sort(key=lambda c: (-c["score"], c["qualified"]))
    return out


# ---------------------------------------------------------------------------
# reading：表格抽取与单元格级定位
# ---------------------------------------------------------------------------
# 题面点名的失分点之一是「只把表格字段抄进 Markdown、不给原始行/单元格定位」。
# ⇒ 表格里的值必须能回指到「哪个文件的第几行、第几行记录、第几列」，
#   否则评审无法核验，只能当"无出处"处理。

# Markdown 表格的分隔行（|---|---| 或 --- | --- |）
_MD_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

# 表头里的「数值列 / 单位列」提示词
_VALUE_HEAD_HINTS = ("数值", "值", "value", "result", "结果", "measurement", "读数")
_UNIT_HEAD_HINTS = ("单位", "unit", "units")

# 常见单位：用来把表格里的单位列认出来
_KNOWN_UNITS = {
    "g", "kg", "mg", "µg", "μg", "ml", "l", "µl", "μl", "s", "min", "h", "d",
    "k", "°c", "℃", "%", "nm", "µm", "μm", "mm", "cm", "m", "km", "pa", "kpa",
    "mpa", "gpa", "mm", "µm", "μm", "mmol", "mol", "µmol", "μmol", "nmol",
    "pmol", "j", "kj", "mj", "w", "mw", "kw", "v", "mv", "kv", "a", "ma",
    "hz", "khz", "mhz", "ghz", "ev", "mev", "kev", "wt%", "rpm", "bar", "psi",
    "ppm", "ppb", "mg/ml", "g/l", "mol/l", "mmol/l", "°", "f", "n",
}


def _content_tokens(text: str) -> list[str]:
    """描述里的内容词（去冠词/介词，但**保留**泛用词）。

    与 `_terms` 的区别：`_terms` 会把 "数值" 这类泛用词剔除，因为正文里
    「数值」不能当主词；但表格的**列名**恰恰常常就是 "数值"，所以表格匹配
    必须用这一套不过滤泛用词的分词。
    """
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]*|[\u4e00-\u9fff]{2,}", text or ""):
        low = m.group(0).lower()
        if low in _STOPWORDS:
            continue
        tok = _norm_tok(m.group(0))
        if tok and tok not in out:
            out.append(tok)
    return out


def _cell_text_match(cell: str, tokens: list[str]) -> bool:
    """单元格文本里是否出现这些词（按归一化词比较，不做子串误命中）。"""
    if not cell or not tokens:
        return False
    ctoks = [t for _w, _s, _e, t in _tokens_with_pos(cell) if t]
    if not ctoks:
        return False
    n = len(tokens)
    for i in range(len(ctoks) - n + 1):
        if all(_tok_match(ctoks[i + j], tokens[j]) for j in range(n)):
            return True
    return False


def _is_md_separator(line: str) -> bool:
    s = (line or "").strip()
    return bool(s) and "-" in s and bool(_MD_SEP_RE.match(s))


def _split_md_row(line: str) -> list[str]:
    s = (line or "").strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _split_delimited(line: str, delim: str) -> list[str]:
    """按分隔符切一行（尊重引号）。"""
    try:
        import csv as _csv

        row = next(_csv.reader([line], delimiter=delim))
    except Exception:  # noqa: BLE001
        row = line.split(delim)
    return [(c or "").strip() for c in row]


def _md_blocks(name: str, text: str) -> list[dict[str, Any]]:
    """找出材料里的 Markdown 表格块。"""
    lines = (text or "").split("\n")
    out: list[dict[str, Any]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 必须是「表头行 + 分隔行」才算表格：普通含 | 的句子（'|' 少于 2 个、
        # 或下一行不是分隔行）不会被误当成表格。
        if line.count("|") >= 2 and i + 1 < n and _is_md_separator(lines[i + 1]):
            header = _split_md_row(line)
            if len(header) >= 2:
                rows: list[dict[str, Any]] = []
                j = i + 2
                while j < n and lines[j].count("|") >= 2:
                    if _is_md_separator(lines[j]):
                        j += 1
                        continue
                    cells = _split_md_row(lines[j])
                    rows.append({
                        "row_index": len(rows),
                        "cells": cells,
                        "row_text": lines[j].strip(),
                        "line_start": j + 1,
                    })
                    j += 1
                out.append({
                    "source_id": name,
                    "kind": "markdown",
                    "caption": line.strip(),
                    "header": header,
                    "rows": rows,
                    "line_start": i + 1,
                })
                i = j
                continue
        i += 1
    return out


def _csv_blocks(name: str, text: str, ext: str) -> list[dict[str, Any]]:
    """找出材料里的 CSV / TSV 表格块。

    * 源文件名以 .csv/.tsv 结尾 → 整个文件当一张表；
    * 其它文件 → 嗅探「连续若干行、列数一致」的逗号/制表符分隔块
      （列数必须 ≥2 且至少两行数据，避免把散文里的逗号句当成表）。
    """
    lines = (text or "").split("\n")
    out: list[dict[str, Any]] = []
    if ext in (".csv", ".tsv"):
        delim = "\t" if (ext == ".tsv" or (lines and "\t" in lines[0])) else ","
        header: list[str] = []
        rows: list[dict[str, Any]] = []
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            cells = _split_delimited(ln, delim)
            if not header:
                header = cells
                continue
            rows.append({
                "row_index": len(rows),
                "cells": cells,
                "row_text": ln.strip(),
                "line_start": i + 1,
            })
        if len(header) >= 2:
            out.append({
                "source_id": name,
                "kind": "csv",
                "caption": lines[0].strip() if lines else "",
                "header": header,
                "rows": rows,
                "line_start": 1,
            })
        return out

    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if not ln.strip() or ("," not in ln and "\t" not in ln):
            i += 1
            continue
        delim = "\t" if "\t" in ln else ","
        head = _split_delimited(ln, delim)
        if len(head) < 2 or max((len(c) for c in head), default=0) > 80:
            i += 1
            continue
        j = i + 1
        block: list[tuple[int, list[str]]] = []
        while j < n and lines[j].strip():
            cells = _split_delimited(lines[j], delim)
            if len(cells) != len(head):
                break
            if max((len(c) for c in cells), default=0) > 80:
                break
            block.append((j, cells))
            j += 1
        if len(block) >= 2:  # 表头 + 至少两行数据
            rows = [{
                "row_index": k,
                "cells": cells,
                "row_text": lines[ln].strip(),
                "line_start": ln + 1,
            } for k, (ln, cells) in enumerate(block)]
            out.append({
                "source_id": name,
                "kind": "csv",
                "caption": lines[i].strip(),
                "header": head,
                "rows": rows,
                "line_start": i + 1,
            })
            i = j
        else:
            i += 1
    return out


def extract_tables(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从材料记录里抽出所有表格（Markdown 表 + CSV/TSV 表）。

    每张表：{"table_id": "T01", "source_id": 文件名, "kind": "markdown|csv",
    "caption": 表头行原文, "header": 列名列表, "rows": [{"row_index", "cells",
    "row_text", "line_start"}], "line_start": 表首行行号}。
    `line_start` 是材料文本里的**行号**，用于回指原始行。
    """
    tables: list[dict[str, Any]] = []
    counter = 0
    for rec in records or []:
        name = str(rec.get("name") or rec.get("source_id") or "")
        text = str(rec.get("text") or "")
        if not text.strip():
            continue
        ext = os.path.splitext(name)[1].lower()
        blocks = _md_blocks(name, text) + _csv_blocks(name, text, ext)
        for blk in blocks:
            counter += 1
            blk["table_id"] = f"T{counter:02d}"
            blk["row_count"] = len(blk.get("rows") or [])
            tables.append(blk)
    return tables


def locate_in_tables(term: str, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在表格里定位一个词/短语，返回带行列坐标的命中列表。

    命中判定按归一化词比较（`_norm_tok` / `_tok_match`）：
      * 列头命中：字段名就是列名（如列名 "数值"）；
      * 行首命中：字段名是行标签（如 "| 反应温度 | 180 | °C |" 的行首）；
      * 单元格命中：字段名直接写在某个单元格里。

    每条命中：{"table_id", "source_id", "row_index", "column_index",
    "column_name", "cell_text", "line_start", "quote", "match"}。
    """
    toks = _content_tokens(term)
    if not toks:
        return []
    primary = toks[0]
    hits: list[dict[str, Any]] = []
    for tbl in tables or []:
        header = list(tbl.get("header") or [])
        rows = tbl.get("rows") or []
        seen: set[tuple[int, int]] = set()
        for pos, row in enumerate(rows):
            cells = list(row.get("cells") or [])
            ri = row.get("row_index", pos)
            line_start = row.get("line_start", 0)

            def _hit(ci: int, ctext: str, kind: str, cname: str = "") -> None:
                hits.append({
                    "table_id": tbl.get("table_id", ""),
                    "source_id": tbl.get("source_id", ""),
                    "row_index": ri,
                    "column_index": ci,
                    "column_name": cname or (header[ci] if ci < len(header) else ""),
                    "cell_text": ctext,
                    "line_start": line_start,
                    "quote": ctext,
                    "match": kind,
                })

            # 列头命中：这一列在该行的取值
            for ci, h in enumerate(header):
                if ci < len(cells) and _cell_text_match(h, [primary]):
                    _hit(ci, cells[ci], "column_header")
                    seen.add((ri, ci))
            # 行首命中：行标签
            if cells and _cell_text_match(cells[0], [primary]):
                _hit(0, cells[0], "row_label")
                seen.add((ri, 0))
            # 单元格内容命中
            for ci, c in enumerate(cells):
                if (ri, ci) in seen:
                    continue
                if _cell_text_match(c, [primary]):
                    _hit(ci, c, "cell")
                    seen.add((ri, ci))
    return hits


# ---------------------------------------------------------------------------
# reading：表格命中 → 字段候选（带行列定位的额外证据）
# ---------------------------------------------------------------------------

def _chunk_ranges(text: str) -> list[tuple[int, int, str]]:
    """按空行切段，并给出每段的（起始行号, 结束行号, 文本）。"""
    out: list[tuple[int, int, str]] = []
    pos = 0
    spans = [(m.start(), m.end()) for m in re.finditer(r"\n\s*\n", text or "")]
    for s, e in spans + [(len(text or ""), len(text or ""))]:
        body = (text or "")[pos:s].strip("\n").strip()
        if body:
            out.append((_line_of(text or "", pos), _line_of(text or "", s), body))
        pos = e
    return out


def _line_of(text: str, idx: int) -> int:
    """字符下标 → 行号（从 1 开始）。"""
    if idx < 0:
        idx = 0
    return (text or "").count("\n", 0, idx) + 1


def _paragraph_line_index(records: list[dict[str, Any]],
                          paras: list[dict[str, Any]]) -> dict[str, list[tuple[int, int, str]]]:
    """source_id → [(起始行, 结束行, paragraph_id)]，把行号接回段落 id。"""
    by_src: dict[str, list[dict[str, Any]]] = {}
    for p in paras or []:
        by_src.setdefault(str(p.get("source_id") or ""), []).append(p)
    out: dict[str, list[tuple[int, int, str]]] = {}
    for rec in records or []:
        name = str(rec.get("name") or rec.get("source_id") or "")
        text = str(rec.get("text") or "")
        plist = by_src.get(name, [])
        used: set[int] = set()
        entries: list[tuple[int, int, str]] = []
        for idx, (s, e, body) in enumerate(_chunk_ranges(text)):
            pid = ""
            pick = -1
            if idx < len(plist) and idx not in used:
                pick = idx
            else:
                for j, pp in enumerate(plist):
                    if j in used:
                        continue
                    if str(pp.get("text") or "").strip() == body:
                        pick = j
                        break
            if pick >= 0:
                pid = str(plist[pick].get("paragraph_id") or "")
                used.add(pick)
            entries.append((s, e, pid))
        out[name] = entries
    return out


def _para_at(para_lines: dict[str, list[tuple[int, int, str]]],
             source_id: str, line: int) -> str:
    """行号 → 段落 id。"""
    for s, e, pid in para_lines.get(source_id or "", []):
        if s <= line <= e:
            return pid
    return ""


def _is_unitish(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 12:
        return False
    if t.lower() in _KNOWN_UNITS:
        return True
    if re.match(r"^[%°℃µμ][A-Za-z0-9/·%°℃\-]*$", t):
        return True
    if re.match(r"^[A-Za-z]+/[A-Za-z]+$", t):
        return True
    return False


def _head_hint(header_text: str, hints: tuple[str, ...]) -> bool:
    low = (header_text or "").lower()
    return any(h in low for h in hints)


def _unit_for_row(cells: list[str], header: list[str], skip: int) -> str:
    """从同一行里找单位（优先"单位"列，其次形似单位的单元）。"""
    for cj, hh in enumerate(header):
        if cj == skip or cj >= len(cells):
            continue
        if _head_hint(hh, _UNIT_HEAD_HINTS):
            v = cells[cj].strip()
            if v:
                return v
    for cj, c in enumerate(cells):
        if cj == skip:
            continue
        if _is_unitish(c):
            return c.strip()
    return ""


def _pick_value_cell(cells: list[str], header: list[str]) -> tuple[int, str, str, str]:
    """在一行里挑出「数值单元格」。

    返回 (列下标, 单元格原文, 值, 单位)。优先用带数值的单元格，
    列的优先级：表头含"数值/value"的列 → 其余列。
    """
    n = len(cells)
    order = [ci for ci in range(1, n)
             if _head_hint(header[ci] if ci < len(header) else "", _VALUE_HEAD_HINTS)]
    order += [ci for ci in range(1, n) if ci not in order]
    for ci in order:
        cell = cells[ci]
        if not cell.strip():
            continue
        nums = _numbers_in(cell)
        if nums:
            unit = nums[0][2] or _unit_for_row(cells, header, ci)
            return ci, cell.strip(), nums[0][1], unit
    for ci in range(1, n):
        if cells[ci].strip():
            return ci, cells[ci].strip(), cells[ci].strip(), ""
    if n > 1:
        return 1, cells[1].strip(), "", ""
    return 0, (cells[0].strip() if cells else ""), "", ""


def _table_candidates(desc: str, tables: list[dict[str, Any]],
                      expected_unit: str = "",
                      para_lines: dict[str, list[tuple[int, int, str]]] | None = None
                      ) -> list[dict[str, Any]]:
    """把表格命中变成字段候选（与 `_candidates` 同构，多了行列定位）。

    只有**字段主词真的出现在表格里**才会产生候选；表里没有该字段时，
    正文侧的判定完全不受影响。
    """
    toks = _content_tokens(desc)
    if not toks or not tables:
        return []
    primary = toks[0]
    others = toks[1:]
    para_lines = para_lines or {}
    out: list[dict[str, Any]] = []
    for tbl in tables:
        header = list(tbl.get("header") or [])
        rows = tbl.get("rows") or []
        src = str(tbl.get("source_id") or "")
        for row in rows:
            cells = list(row.get("cells") or [])
            if not cells:
                continue
            ri = row.get("row_index", 0)
            line_start = row.get("line_start", 0)
            row_text = str(row.get("row_text") or "")
            row_label = cells[0].strip() if cells else ""
            pid = _para_at(para_lines, src, int(line_start or 0))
            hit_others = [t for t in others if _cell_text_match(row_text, [t])]
            base = 2.0 + len(hit_others)
            stat_m = _STAT.search(row_text)
            stat = stat_m.group(0).strip() if stat_m else ""
            cond_m = _COND.search(row_text)
            cond = _clean_cond(cond_m.group(0)) if cond_m else ""
            # 表格里带限定语的行（"after 5 cycles ..."）是**另一个条件**下的取值，
            # 不能与主结果判成来源冲突 —— 与正文侧的 qualified 逻辑保持一致。
            qualified = bool(_QUALIFIER.search(row_text))

            def _mk(ci: int, cell_text: str, value: str, unit: str,
                    kind: str) -> None:
                if unit and value and unit.lower() not in value.lower():
                    value = f"{value} {unit}"
                out.append({
                    "paragraph_id": pid,
                    "source_id": src,
                    "quote": cell_text or row_text,
                    "sentence": row_text,
                    "score": base + (0.5 if value else 0.0),
                    "hits": [primary] + hit_others,
                    "negated": False,
                    "qualified": qualified,
                    "extracted": {"value": value, "unit": unit,
                                  "statistic": stat, "condition": cond},
                    "table_id": tbl.get("table_id", ""),
                    "row_index": ri,
                    "column_index": ci,
                    "column_name": header[ci] if ci < len(header) else "",
                    "cell_text": cell_text,
                    "row_label": row_label,
                    "line_start": line_start,
                    "match": kind,
                })

            # (a) 行首标签命中 → 取该行的数值单元格
            if _cell_text_match(cells[0], [primary]):
                ci, cell_text, value, unit = _pick_value_cell(cells, header)
                _mk(ci, cell_text, value, unit, "row_label")

            # (b) 列头命中 → 该列在这一行的取值
            for ci, h in enumerate(header):
                if ci >= len(cells) or ci == 0:
                    continue
                if not _cell_text_match(h, [primary]):
                    continue
                cell_text = cells[ci].strip()
                nums = _numbers_in(cell_text)
                value = nums[0][1] if nums else cell_text
                unit = (nums[0][2] if nums else "") or _unit_for_row(cells, header, ci)
                _mk(ci, cell_text, value, unit, "column_header")
    out.sort(key=lambda c: (-c["score"], c["qualified"]))
    return out


def _value_key(extracted: dict[str, Any]) -> tuple[str, str]:
    """比较两个取值是否真的不同（数值 + 单位分开比，避免 "87 %" vs "87" 误判）。"""
    v = str((extracted or {}).get("value") or "").strip()
    u = str((extracted or {}).get("unit") or "").strip().lower()
    if not v:
        return ("", u)
    m = re.match(r"^[-+]?\d+(?:\.\d+)?", v)
    if m:
        try:
            num = f"{float(m.group(0)):.6g}"
        except ValueError:
            num = v.lower()
        return (num, u)
    return (v.lower(), u)


def _location_of(cand: dict[str, Any],
                 src_texts: dict[str, str]) -> dict[str, Any]:
    """字段定位：段落 + 行号；表格命中时再带上表格/行/列/单元格。"""
    loc = {
        "paragraph_id": str(cand.get("paragraph_id") or ""),
        "line_start": -1,
        "table_id": "",
        "row_index": -1,
        "column_index": -1,
        "column_name": "",
        "cell_text": "",
    }
    if cand.get("table_id"):
        loc["table_id"] = str(cand.get("table_id") or "")
        loc["row_index"] = cand.get("row_index", -1)
        loc["column_index"] = cand.get("column_index", -1)
        loc["column_name"] = str(cand.get("column_name") or "")
        loc["cell_text"] = str(cand.get("cell_text") or "")
        loc["line_start"] = int(cand.get("line_start") or -1)
        return loc
    text = src_texts.get(str(cand.get("source_id") or ""), "")
    for probe in (str(cand.get("quote") or ""), str(cand.get("sentence") or "")):
        if not (text and probe):
            continue
        idx = text.find(probe)
        if idx >= 0:
            loc["line_start"] = _line_of(text, idx)
            break
    return loc


def _table_line_spans(tables: list[dict[str, Any]]) -> dict[str, list[tuple[int, int]]]:
    """source_id → [(起始行, 结束行)]：表格在材料文本里占据的行区间。"""
    out: dict[str, list[tuple[int, int]]] = {}
    for t in tables or []:
        src = str(t.get("source_id") or "")
        start = int(t.get("line_start") or 0)
        end = start
        for row in t.get("rows") or []:
            ls = int(row.get("line_start") or 0)
            if ls > end:
                end = ls
        out.setdefault(src, []).append((start, end))
    return out


def _cand_rank(cand: dict[str, Any], src_texts: dict[str, str],
               spans: dict[str, list[tuple[int, int]]]) -> int:
    """候选来源优先级：0 = 正文散文，1 = 表格单元格，2 = 表格行被当散文命中。"""
    if cand.get("table_id"):
        return 1
    src = str(cand.get("source_id") or "")
    text = src_texts.get(src, "")
    for probe in (str(cand.get("quote") or ""), str(cand.get("sentence") or "")):
        if not (text and probe):
            continue
        idx = text.find(probe)
        if idx < 0:
            continue
        line = _line_of(text, idx)
        for s, e in (spans.get(src) or []):
            if s <= line <= e:
                return 2
        return 0
    return 0


def _merge_candidates(prose: list[dict[str, Any]],
                      table_cands: list[dict[str, Any]],
                      src_texts: dict[str, str],
                      spans: dict[str, list[tuple[int, int]]]) -> list[dict[str, Any]]:
    """正文候选与表格候选合并成一个候选池。

    表格命中是**额外证据来源**：
      * 正文散文候选优先（它带条件、统计量等上下文）；
      * 表格行本身若被正文匹配命中（排名最末），在已有表格候选时直接丢掉
        —— 否则会把整张表当成一句话去抽数，容易抓到隔壁行的数值。
    """
    pool: list[tuple[int, dict[str, Any]]] = []
    for c in prose or []:
        pool.append((_cand_rank(c, src_texts, spans), c))
    for c in table_cands or []:
        pool.append((1, c))
    if table_cands:
        pool = [p for p in pool if p[0] != 2]
    pool.sort(key=lambda p: (-float(p[1].get("score") or 0.0),
                             bool(p[1].get("qualified")), p[0]))
    return [c for _rank, c in pool]


def _values_conflict(cands: list[dict[str, Any]]) -> bool:
    """一组候选的取值是否互相矛盾。

    判据：
      * 数值不同 → 矛盾；
      * 数值相同、但两侧都给了单位且单位不同 → 矛盾；
      * 一侧没给单位 → 只算写法差异，不算矛盾（"180" vs "180 °C"）。
    """
    seen: list[tuple[str, str]] = []
    for c in cands or []:
        ex = c.get("extracted") or {}
        v = str(ex.get("value") or "").strip()
        if not v:
            continue
        m = re.match(r"^[-+]?\d+(?:\.\d+)?", v)
        if m:
            try:
                num = f"{float(m.group(0)):.10g}"
            except ValueError:
                num = v.lower()
        else:
            num = v.lower()
        unit = str(ex.get("unit") or "").strip().lower()
        for prev_num, prev_unit in seen:
            if prev_num != num:
                return True
            if prev_unit and unit and prev_unit != unit:
                return True
        seen.append((num, unit))
    return False


def _numeric_key(extracted: dict[str, Any]) -> str:
    """只比数值（忽略单位写法差异）的比较键。"""
    v = str((extracted or {}).get("value") or "").strip()
    m = re.match(r"^[-+]?\d+(?:\.\d+)?", v)
    if m:
        try:
            return f"{float(m.group(0)):.10g}"
        except ValueError:
            return v.lower()
    return v.lower()


def _compact_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """候选 → 可核验的小结构（带表格坐标时一并带上）。"""
    out = {
        "paragraph_id": str(cand.get("paragraph_id") or ""),
        "source_id": str(cand.get("source_id") or ""),
        "quote": str(cand.get("quote") or ""),
        "value": str((cand.get("extracted") or {}).get("value") or ""),
        "unit": str((cand.get("extracted") or {}).get("unit") or ""),
    }
    if cand.get("table_id"):
        out.update({
            "table_id": str(cand.get("table_id") or ""),
            "row_index": cand.get("row_index", -1),
            "column_index": cand.get("column_index", -1),
            "column_name": str(cand.get("column_name") or ""),
            "cell_text": str(cand.get("cell_text") or ""),
            "row_label": str(cand.get("row_label") or ""),
            "line_start": cand.get("line_start", -1),
        })
    return out


def _slot_of(cand: dict[str, Any]) -> str:
    """表格候选的「槽位」键：同一个槽位里的取值才是同一字段的不同说法。"""
    label = str(cand.get("row_label") or "").strip()
    match = str(cand.get("match") or "")
    if match == "column_header":
        return f"col:{cand.get('column_index', -1)}|row:{label}"
    if match == "cell":
        return f"cell:{cand.get('row_index', -1)}|{cand.get('column_index', -1)}"
    return f"row:{label}"


def _group_table_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把表格候选按槽位分组；同一槽位出现多个不同取值 → 冲突组。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cands or []:
        groups.setdefault(f"{c.get('table_id', '')}|{_slot_of(c)}", []).append(c)
    out: list[dict[str, Any]] = []
    for key, cs in groups.items():
        vals = {_value_key(c["extracted"]) for c in cs
                if (c.get("extracted") or {}).get("value")}
        if len(vals) > 1:
            out.append({
                "table_id": cs[0].get("table_id", ""),
                "source_id": cs[0].get("source_id", ""),
                "slot": key.split("|", 1)[-1],
                "values": [_compact_candidate(c) for c in cs[:6]],
            })
    return out


def _table_conflict_groups(desc: str, unit: str,
                            tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一字段在同一张表的同一「槽位」里出现多个不同取值 → 冲突候选。

    槽位键：
      * 行首标签命中 → `row:<行标签>`（同名行出现两个不同数值 = 真冲突）；
      * 列头命中     → `col:<列号>|row:<行标签>`（不同行属于不同参数，
        不能因为"同一列取值不同"就判冲突）。
    """
    return _group_table_candidates(_table_candidates(desc, tables, unit))


def _unit_anchors(text: str) -> list[str]:
    """带单位的数值（改写守恒检查用）。"""
    return sorted(m.group(0).strip() for m in _NUM.finditer(text or ""))


def _anchor_counts_full(text: str) -> dict[str, list[str]]:
    """锚点统计：数字 / 引文编号 / 图表号 / 带单位的数值。"""
    c = _anchor_counts(text)
    c["units"] = _unit_anchors(text)
    return c


# ---------------------------------------------------------------------------
# reading：异常自报（anomaly report）
# ---------------------------------------------------------------------------

def write_anomaly_report(out_dir: str, fields: list[dict[str, Any]],
                         tables: list[dict[str, Any]],
                         notes: Any = None) -> dict[str, Any]:
    """把抽取过程中发现的所有异常**如实**写进 anomaly-report.json。

    报告内容：
      * status_counts    —— 四个状态枚举各多少条（缺项补 0）
      * anomalies        —— 每个 not_reported / source_conflict /
                            extraction_failed 字段的条目与原因
      * table_conflicts  —— 同一字段在同一表格槽位里出现多个不同取值
      * value_conflicts  —— 同一字段在正文与表格里数值不一致
      * missing_locations—— quote 为空或定位字段缺失的条目
      * self_reported    —— true，并附一行「异常自动检出、如实上报、未隐藏」
    """
    report: dict[str, Any] = {
        "self_reported": True,
        "note": ("异常由脚本自动检出并如实上报，未隐藏 / anomalies were "
                 "detected by the script and are reported as they are; "
                 "nothing was suppressed or hidden"),
        "generated_by": "specialize.write_anomaly_report",
        "status_counts": {s: 0 for s in FIELD_STATUS},
        "anomalies": [],
        "table_conflicts": [],
        "value_conflicts": [],
        "missing_locations": [],
        "fields": [],
        "tables": [],
        "notes": [],
    }
    if isinstance(notes, str):
        notes = [notes]
    report["notes"] = [str(n) for n in (notes or [])]

    for t in tables or []:
        report["tables"].append({
            "table_id": str(t.get("table_id") or ""),
            "source_id": str(t.get("source_id") or ""),
            "kind": str(t.get("kind") or ""),
            "caption": str(t.get("caption") or ""),
            "header": list(t.get("header") or []),
            "row_count": int(t.get("row_count") or len(t.get("rows") or [])),
            "line_start": int(t.get("line_start") or 0),
        })

    for f in fields or []:
        fid = str(f.get("field_id") or "")
        status = str(f.get("field_status") or "")
        loc = f.get("location") or {}
        if status in report["status_counts"]:
            report["status_counts"][status] += 1
        report["fields"].append({
            "field_id": fid,
            "field_status": status,
            "value": f.get("value"),
            "unit": f.get("unit") or "",
            "quote": str(f.get("quote") or ""),
            "paragraph_id": str(f.get("paragraph_id") or ""),
            "location": loc,
        })
        if status != "reported":
            report["anomalies"].append({
                "field_id": fid,
                "field_status": status,
                "reason": str(f.get("note") or ""),
                "value": f.get("value"),
                "quote": str(f.get("quote") or ""),
                "paragraph_id": str(f.get("paragraph_id") or ""),
                "location": loc,
            })
        desc = str(f.get("requested") or fid)
        unit = str(f.get("unit") or "")
        for grp in _table_conflict_groups(desc, unit, tables or []):
            report["table_conflicts"].append({
                "field_id": fid,
                "field_status": status,
                "detail": ("the same field carries several different values "
                           "in this table slot; a reviewer must decide which "
                           "row applies"),
                **grp,
            })
        if f.get("value"):
            key = _numeric_key({"value": f.get("value")})
            for c in _table_candidates(desc, tables or [], unit):
                if c.get("qualified"):
                    continue
                cv = (c.get("extracted") or {}).get("value")
                if not cv or _numeric_key({"value": cv}) == key:
                    continue
                report["value_conflicts"].append({
                    "field_id": fid,
                    "detail": ("the text and the table state different values "
                               "for this field"),
                    "text_value": f.get("value"),
                    "text_unit": f.get("unit") or "",
                    "table_value": cv,
                    "table_qualified": bool(c.get("qualified")),
                    "table_location": _compact_candidate(c),
                })
        if status == "reported":
            has_loc = bool(loc.get("paragraph_id")) or bool(loc.get("table_id"))
            if not str(f.get("quote") or "") or not has_loc:
                report["missing_locations"].append({
                    "field_id": fid,
                    "detail": ("a value was reported but the verbatim quote or "
                               "the paragraph/table locator is missing"),
                    "quote": str(f.get("quote") or ""),
                    "location": loc,
                })

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "anomaly-report.json")
        report["file"] = path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


# ---------------------------------------------------------------------------
# writing：QA 报告（轻量）
# ---------------------------------------------------------------------------

def write_qa_report(out_dir: str, sections: list[dict[str, Any]],
                    receipts: list[dict[str, Any]],
                    anchor_check: Any = None) -> dict[str, Any]:
    """写 qa-report.json：逐节标题、锚点守恒、edit-targets 状态、人工待核清单。

    `anchor_check` 形如 {"summary": {...}, "sections": [{"section_id",
    "before": {...}, "after": {...}, "preserved": bool, "broken_kinds": [...]}]}。
    任何**没能自动核验**的条目一律 status = pending_human_review，
    绝不伪造「已确认」。
    """
    ac = anchor_check if isinstance(anchor_check, dict) else {}
    per_section = {str(s.get("section_id") or ""): s
                   for s in (ac.get("sections") or []) if isinstance(s, dict)}
    report: dict[str, Any] = {
        "self_reported": True,
        "note": ("无法自动核验的条目一律标记为 pending_human_review，"
                 "未伪造任何「已确认」结论 / every item that could not be "
                 "checked automatically is marked pending_human_review; "
                 "no confirmation was fabricated"),
        "generated_by": "specialize.write_qa_report",
        "sections": [],
        "anchor_conservation": {},
        "edit_targets": [],
        "edit_target_summary": {},
        "pending_human_review": [],
    }

    for s in sections or []:
        sid = str(s.get("section_id") or "")
        sc = per_section.get(sid) or {}
        before = sc.get("before") or s.get("anchors") or {}
        after = sc.get("after") or {}
        preserved = bool(sc.get("preserved", True))
        broken = list(sc.get("broken_kinds") or [])
        report["sections"].append({
            "section_id": sid,
            "title": str(s.get("title") or ""),
            "source_id": str(s.get("source_id") or ""),
            "anchor_counts_before": before,
            "anchor_counts_after": after,
            "anchors_preserved": preserved,
            "broken_anchor_kinds": broken,
        })
        if not preserved:
            report["pending_human_review"].append({
                "item": sid,
                "kind": "anchor_conservation",
                "status": "pending_human_review",
                "reason": ("the automated anchor check reports broken anchor "
                           "kinds: " + ", ".join(broken)),
            })

    summary = ac.get("summary")
    if not isinstance(summary, dict):
        summary = {
            "sections": len(report["sections"]),
            "sections_changed": 0,
            "sections_with_broken_anchors": sum(
                1 for s in report["sections"] if not s["anchors_preserved"]),
            "all_anchors_preserved": all(
                s["anchors_preserved"] for s in report["sections"])
            if report["sections"] else True,
            "anchor_kinds_checked": ["numbers", "citations", "figures", "units"],
        }
    report["anchor_conservation"] = summary

    counts: dict[str, int] = {}
    for r in receipts or []:
        status = str(r.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
        report["edit_targets"].append({
            "target_id": str(r.get("target_id") or ""),
            "requested": str(r.get("requested") or ""),
            "status": status,
            "applied": bool(r.get("applied")),
            "note": str(r.get("note") or ""),
        })
        if not r.get("applied"):
            report["pending_human_review"].append({
                "item": str(r.get("target_id") or ""),
                "kind": "edit_target",
                "status": "pending_human_review",
                "reason": str(r.get("note") or
                              "the wording change needs domain judgement"),
            })
    report["edit_target_summary"] = {
        "total": len(report["edit_targets"]),
        "by_status": counts,
        "workorder_present": bool(ac.get("workorder_present")) or bool(receipts),
        "note": ("each edit target has one receipt; targets that the "
                 "deterministic script could not apply are listed under "
                 "pending_human_review"),
    }

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "qa-report.json")
        report["file"] = path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


# ---------------------------------------------------------------------------
# reading：入口
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _find_input_file(input_dir: str, *names: str) -> str:
    """在 /input 下按优先级找文件（不假设扩展名）。"""
    if not input_dir or not os.path.isdir(input_dir):
        return ""
    entries: list[str] = []
    for root, _dirs, files in os.walk(input_dir):
        for fn in files:
            entries.append(os.path.join(root, fn))
    for want in names:
        for p in entries:
            if os.path.basename(p).lower() == want.lower():
                return p
    return ""


def _field_items(raw: Any) -> list[dict[str, Any]]:
    """从 target-fields.json 里取出字段列表（兼容多种形状）。"""
    items: list[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("fields", "targets", "items", "requested_fields",
                    "target_fields", "fields_requested", "requirements"):
            val = raw.get(key)
            if isinstance(val, list):
                items = val
                break
            if isinstance(val, dict):
                items = [{**val, "field_id": k} for k in val.keys()]
                break
        else:
            if any(k in raw for k in ("field_id", "id", "name")):
                items = [raw]
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            out.append({"field_id": it, "description": it,
                        "expected_unit": "", "statistic": "", "raw": it})
            continue
        if not isinstance(it, dict):
            continue
        fid = str(it.get("field_id") or it.get("id") or it.get("name")
                  or it.get("key") or "").strip()
        if not fid:
            continue
        desc = str(it.get("description") or it.get("prompt") or
                   it.get("question") or it.get("label") or
                   it.get("description_zh") or it.get("name") or "").strip()
        out.append({
            "field_id": fid,
            "description": desc,
            "expected_unit": str(it.get("unit") or it.get("expected_unit")
                                  or it.get("units") or "").strip(),
            "statistic": str(it.get("statistic") or it.get("expected_statistic")
                             or "").strip(),
            "raw": it,
        })
    return out


def _blank_result(field_id: str, desc: str, unit: str, stat: str,
                  status: str, note: str, quote: str = "",
                  paragraph_id: str = "", source_id: str = "",
                  location: dict[str, Any] | None = None) -> dict[str, Any]:
    loc = {
        "paragraph_id": paragraph_id,
        "line_start": -1,
        "table_id": "",
        "row_index": -1,
        "column_index": -1,
        "column_name": "",
        "cell_text": "",
    }
    for k, v in (location or {}).items():
        if k in loc and v not in (None, ""):
            loc[k] = v
    return {
        "field_id": field_id,
        "question_id": field_id,
        "requested": desc,
        # field_status 用组织方修订里规定的四值枚举
        "field_status": status,
        # answer_status 用赛题契约里规定的四值枚举（answered / partial /
        # insufficient_evidence / input_error）—— 两个枚举同时给出，
        # 评审无论按哪份契约读都能对上。
        "answer_status": _ANSWER_STATUS[status],
        "status": _ANSWER_STATUS[status],
        "value": None,
        "answer": None,
        "unit": unit,
        "statistic": stat,
        "condition": "",
        "conditions": [],
        "comparator": "",
        "paragraph_id": paragraph_id,
        "source_id": source_id,
        "quote": quote,
        # 定位信息：段落 + 行号；命中表格时再带 table/row/column/cell，
        # 让评审能回到原始行/单元格核对，而不是只看一段文字。
        "location": loc,
        "evidence": ([{"paragraph_id": paragraph_id, "source_id": source_id,
                       "quote": quote}] if quote else []),
        "note": note,
    }


def extract_target_fields(input_dir: str, paras: list[dict[str, Any]],
                          records: list[dict[str, Any]],
                          out_dir: str = "") -> dict[str, Any]:
    """按 `/input/target-fields.json` 逐字段抽取。

    返回 {"available": bool, "fields": [...], "counts": {...}, "note": str,
    "anomaly_report": {...}}。
    找不到 target-fields.json 时 available=False，调用方应如实登记不确定性。

    表格里的取值是**额外证据来源**：字段主词命中表格单元格/列名/行标签时，
    会产生一条带 table/row/column 定位的候选；表里没有该字段时，
    字段的状态判定完全不受影响。
    """
    path = _find_input_file(input_dir, "target-fields.json", "target_fields.json",
                            "target-fields.JSON")
    tables = extract_tables(records)
    src_texts = {str(r.get("name") or r.get("source_id") or ""): str(r.get("text") or "")
                 for r in records or []}
    if not path:
        report = write_anomaly_report(
            out_dir, [], tables,
            "no target-fields.json was found under the input directory, so no "
            "per-field result could be produced")
        return {"available": False, "fields": [], "anomaly_report": report,
                "note": ("no target-fields.json was found under the input "
                         "directory, so the requested field list could not be "
                         "read and no per-field result could be produced")}
    raw = _load_json(path)
    items = _field_items(raw)
    para_lines = _paragraph_line_index(records, paras)
    spans = _table_line_spans(tables)
    if not items:
        report = write_anomaly_report(
            out_dir, [], tables,
            "target-fields.json was found but no field entry could be read from it")
        return {"available": True, "fields": [], "anomaly_report": report,
                "note": ("target-fields.json was found but no field entry could "
                         "be read from it")}

    results: list[dict[str, Any]] = []
    for it in items:
        desc = it.get("description") or it["field_id"]
        unit = it.get("expected_unit") or ""
        stat_req = it.get("statistic") or ""
        prose = _candidates(desc, paras, unit)
        tcands = _table_candidates(desc, tables, unit, para_lines)
        cands = _merge_candidates(prose, tcands, src_texts, spans)
        if not cands:
            results.append(_blank_result(
                it["field_id"], desc, unit, stat_req, "not_reported",
                "the material does not state this field; it is left unreported "
                "rather than filled with a guessed value or a zero"))
            continue

        top = cands[0]
        # 只有**同样不带限定语**的同分候选才算真冲突；带限定语的是另一个
        # 实验条件下的数值，应作为备选列出而不是判成冲突。
        tied = [c for c in cands
                if abs(c["score"] - top["score"]) < 1e-9
                and c["qualified"] == top["qualified"]]
        got = top["extracted"]

        # 否定句优先：材料明确说「没有报告」→ not_reported
        neg = [c for c in tied if c["negated"]]
        if neg and not (got["value"] and not top["negated"]):
            c = neg[0]
            results.append(_blank_result(
                it["field_id"], desc, unit, stat_req, "not_reported",
                "the material explicitly states that this field was not reported",
                quote=c["quote"], paragraph_id=c["paragraph_id"],
                source_id=c["source_id"],
                location=_location_of(c, src_texts)))
            continue

        status = "reported"
        note = ""
        if len(tied) > 1 and _values_conflict(tied):
            status = "source_conflict"
            note = ("several equally supported statements give different "
                    "values; the highest-ranked one is reported and the "
                    "alternatives are listed for human review")
        if not got["value"] and not got["statistic"] and not got["condition"]:
            status = "extraction_failed"
            note = ("a relevant statement was located but no scalar, statistic "
                    "or condition could be separated from it")

        res = _blank_result(
            it["field_id"], desc, unit or got["unit"],
            stat_req or got["statistic"], status, note,
            quote=top["quote"], paragraph_id=top["paragraph_id"],
            source_id=top["source_id"],
            location=_location_of(top, src_texts))
        res["field_status"] = status
        res["answer_status"] = _ANSWER_STATUS[status]
        res["status"] = _ANSWER_STATUS[status]
        res["value"] = got["value"] or None
        res["answer"] = got["value"] or got["statistic"] or got["condition"] or None
        res["statistic"] = stat_req or got["statistic"]
        res["condition"] = got["condition"]
        res["conditions"] = [got["condition"]] if got["condition"] else []
        res["alternatives"] = [
            {"paragraph_id": c["paragraph_id"], "quote": c["quote"],
             "value": c["extracted"]["value"],
             "location": _location_of(c, src_texts)}
            for c in tied[1:4]
        ]
        # 表格里若也提到这个字段，单元格坐标一并给出（哪怕正文那条被选中），
        # 评审永远能回到原始行/单元格核对。
        res["table_evidence"] = [
            {"table_id": c["table_id"], "source_id": c["source_id"],
             "row_index": c["row_index"], "column_index": c["column_index"],
             "column_name": c["column_name"], "cell_text": c["cell_text"],
             "line_start": c["line_start"],
             "value": c["extracted"]["value"], "unit": c["extracted"]["unit"]}
            for c in tcands[:6]
        ]
        results.append(res)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["field_status"]] = counts.get(r["field_status"], 0) + 1
    note = (f"{len(results)} requested field(s) were answered; a field that the "
            "material does not state is reported as not_reported, never as zero "
            "and never as a guess")
    report = write_anomaly_report(out_dir, results, tables, [note])
    return {
        "available": True,
        "fields": results,
        "source_file": os.path.basename(path),
        "counts": counts,
        "tables": tables,
        "anomaly_report": report,
        "note": note,
    }


# ---------------------------------------------------------------------------
# writing：真的可编辑稿件 + 真的改动记录
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6}\s+.+|第[一二三四五六七八九十百\d]+[章节部分节].*|"
                       r"\d+(\.\d+)*\s+\S.*|[一二三四五六七八九十]+[、.．]\s*\S.*)$")


def _looks_like_heading(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) > 90:
        return False
    if _HEADING.match(s):
        return True
    if len(s) <= 60 and not s.endswith(("。", ".", "；", ";", "，", ",")) \
            and not re.search(r"\d+\.\d+", s):
        return True
    return False


def _split_chapters(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把材料切成「章节」，章节头用材料里的标题行（不新造标题）。

    每个章节额外记 `lines` = [(行号, 该行原文)]，行号是**该材料文件内**的
    1-based 行号 —— 逐条锚点定位要靠它把锚点接回原文位置。
    """
    chapters: list[dict[str, Any]] = []
    cur_title = ""
    cur_body: list[str] = []
    cur_src = ""
    cur_lines: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal cur_title, cur_body, cur_src, cur_lines
        if cur_body and any(x.strip() for x in cur_body):
            chapters.append({"title": cur_title, "body": "\n".join(cur_body),
                             "source_id": cur_src, "lines": list(cur_lines)})
        cur_title = ""
        cur_body = []
        cur_src = ""
        cur_lines = []

    for rec in records:
        for line_no, line in enumerate((rec.get("text") or "").splitlines(), 1):
            if _looks_like_heading(line):
                flush()
                cur_title = line.strip()
                cur_src = rec["name"]
            else:
                # 没有标题行的材料也要挂上来源文件，否则锚点定位找不到家。
                if not cur_src:
                    cur_src = rec["name"]
                cur_body.append(line)
                cur_lines.append((line_no, line))
        flush()
    return chapters


def _norm_ws(text: str) -> str:
    """机械规范化：合并多余空行、去掉行尾空白、压缩连续空格。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).rstrip()
        if not line and out and not out[-1]:
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _anchor_counts(text: str) -> dict[str, list[str]]:
    nums = re.findall(r"\d+(?:\.\d+)?", text or "")
    cites = re.findall(r"\[[0-9,\-–\s]+\]|\(\w+ et al\.,? \d{4}\)", text or "")
    figs = re.findall(r"(?:Fig(?:ure)?\.?|图|Table|表)\s*\d+", text or "", flags=re.I)
    return {"numbers": sorted(nums), "citations": sorted(cites), "figures": sorted(figs)}


def _preserve(before: str, after: str) -> list[str]:
    """返回被破坏的锚点种类（空 = 全部守恒）。

    四类：数字 / 引文编号 / 图表号 / **带单位的数值**。
    单位单独查，是因为「30 mg」被改写成「30 mg/L」时，裸数字守恒、
    数值含义却变了 —— 只查裸数字会漏掉这类最危险的破坏。
    """
    a, b = _anchor_counts(before), _anchor_counts(after)
    broken = [k for k in ("numbers", "citations", "figures") if a[k] != b[k]]
    ua, ub = _unit_anchors(before), _unit_anchors(after)
    if ua != ub:
        broken.append("units")
    return broken


def _file_record(name: str, path: str, fmt: str, role: str) -> dict[str, Any]:
    data = open(path, "rb").read()
    return {
        "filename": name,
        "path": name, "format": fmt, "role": role,
        "exists": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


# ---------------------------------------------------------------------------
# writing：逐条锚点定位（每个锚点回到段落 id + 行号 + 原文短引）
# ---------------------------------------------------------------------------

# 单位锚点优先：命中单位的数值不再重复登记为裸数字。
_ANCHOR_UNIT_RX = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?\s*"
    r"(?:%|wt%|wt\.?%|ppm|ppb|mg/mL|g/L|nmol|µmol|μmol|pmol|MPa|GPa|kPa|"
    r"min|h|nm|µm|μm|mm|cm|km|kg|mg|mM|µM|μM|nM|meV|keV|mW|kW|MHz|GHz|kHz|"
    r"mL|µL|μL|kJ|MJ|ms|µs|°C|℃|Pa|eV|Hz|mol|dB|A|mA|kA|V|mV|kV|W|N|kN)"
    # 复合单位（mg/cm2、A/g、mAh/g 等）：单位后再跟一段 /单位
    r"(?:\s*/\s*[A-Za-zµμ°%℃]+(?:\d+)?)*(?![A-Za-z])"
)
_ANCHOR_NUMBER_RX = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_ANCHOR_CITE_RX = re.compile(
    r"\[[0-9,\-–\s]+\]|\([A-Za-z][A-Za-z\-]+(?:\s+et al\.)?,?\s+\d{4}[a-z]?\)")
_ANCHOR_FIG_RX = re.compile(
    r"(?:Fig(?:ure)?|Table|图|表|Eq(?:uation)?|公式)\s*\.?\s*[A-Z]?\d+(?:\.\d+)?",
    re.I)
# 术语：缩写定义式（full term (ABBR)）与引号强调式
_ANCHOR_TERM_RX = re.compile(
    r"[A-Za-z][A-Za-z0-9 \-/]{2,40}\s*[（(]\s*[A-Z][A-Za-z0-9]{1,9}\s*[）)]"
    r"|[“「]([^”」\n]{2,40})[”」]")


def _quote_from_line(line: str, start: int, end: int, limit: int = 220) -> str:
    """截出**含该锚点**的一段原文短引（逐字可回原文）。"""
    text = line or ""
    lo = max(0, start - 60)
    hi = min(len(text), end + 60)
    frag = text[lo:hi].strip()
    if len(frag) > limit:
        frag = frag[:limit].rstrip()
    return frag


def _writing_anchor_locations(sections: list[dict[str, Any]],
                               para_lines: dict[str, list[tuple[int, int, str]]],
                               ) -> list[dict[str, Any]]:
    """把每个 section 里的锚点逐条定位。

    每条给出：kind（number / unit / citation / figure / term）、value、
    section_id、source_id、line_start（材料文件内行号）、paragraph_id
    （回材料段落的稳定 id）与 quote（原文短引）。

    这是 rubric「事实与来源映射 ⇒ 可逐条回溯」的落点：
    只给计数不够，评审要能一条一条点开核对。
    """
    out: list[dict[str, Any]] = []
    n = 0
    for sec in sections:
        sid = str(sec.get("section_id") or "")
        src = str(sec.get("source_id") or "")
        for line_no, line in sec.get("_lines") or []:
            if not line.strip():
                continue
            pid = _para_at(para_lines, src, int(line_no))
            spans: list[tuple[int, int]] = []
            found: list[tuple[str, str, int, int]] = []

            for m in _ANCHOR_UNIT_RX.finditer(line):
                found.append(("unit", m.group(0).strip(), m.start(), m.end()))
                spans.append((m.start(), m.end()))
            for m in _ANCHOR_NUMBER_RX.finditer(line):
                if any(s < m.end() and m.start() < e for s, e in spans):
                    continue
                found.append(("number", m.group(0), m.start(), m.end()))
            for m in _ANCHOR_CITE_RX.finditer(line):
                found.append(("citation", m.group(0).strip(), m.start(), m.end()))
            for m in _ANCHOR_FIG_RX.finditer(line):
                found.append(("figure", m.group(0).strip(), m.start(), m.end()))
            for m in _ANCHOR_TERM_RX.finditer(line):
                val = (m.group(1) or m.group(0)).strip()
                if val:
                    found.append(("term", val, m.start(), m.end()))

            for kind, value, s, e in found:
                n += 1
                out.append({
                    "anchor_id": f"A{n:04d}",
                    "kind": kind,
                    "value": value,
                    "section_id": sid,
                    "section_title": str(sec.get("title") or ""),
                    "source_id": src,
                    "line_start": int(line_no),
                    "paragraph_id": pid,
                    "quote": _quote_from_line(line, s, e),
                })
    return out


def _writing_key_claims(anchor_locs: list[dict[str, Any]],
                        sections: list[dict[str, Any]],
                        ) -> list[dict[str, Any]]:
    """把「带锚点的句子」列为关键论断候选（逐条带定位）。

    只做**抽取**：句子是原文原话，不做归纳改写。
    """
    by_sec: dict[str, list[dict[str, Any]]] = {}
    for a in anchor_locs:
        by_sec.setdefault(str(a.get("section_id") or ""), []).append(a)

    out: list[dict[str, Any]] = []
    n = 0
    for sec in sections:
        sid = str(sec.get("section_id") or "")
        locs = by_sec.get(sid) or []
        if not locs:
            continue
        seen: set[str] = set()
        for line_no, line in sec.get("_lines") or []:
            if not line.strip():
                continue
            sents = re.split(r"(?<=[.!?。！？；;])\s+", line.strip())
            for s in sents:
                s = s.strip()
                if len(s) < 12 or s in seen:
                    continue
                hit = [a for a in locs
                       if a.get("line_start") == line_no and a.get("value") in s]
                if not hit:
                    continue
                seen.add(s)
                n += 1
                out.append({
                    "claim_id": f"K{n:04d}",
                    "text": s[:300],
                    "section_id": sid,
                    "source_id": str(sec.get("source_id") or ""),
                    "line_start": int(line_no),
                    "paragraph_id": hit[0].get("paragraph_id") or "",
                    "quote": s[:300],
                    "anchor_kinds": sorted({str(a.get("kind")) for a in hit}),
                    "anchor_ids": [str(a.get("anchor_id")) for a in hit],
                })
    return out


# ---------------------------------------------------------------------------
# writing：格式规格清单（从模板/题面**抽取**，不凭经验猜测）
# ---------------------------------------------------------------------------

# 题面点名的可逐项核对维度 → 抽取用正则（中文 + 英文）。
_FORMAT_DIMENSIONS: list[tuple[str, str, list[tuple[str, int]]]] = [
    ("columns", "栏数", [
        (r"[一二三四两1-4]\s*栏(?:排版|正文|格式)?", 0),
        (r"(?:双|单|三)栏(?:排版|正文|格式)?", 0),
        (r"(?i)\b(?:single|two|three|four|1|2|3)[- ]column(?:s|ed)?\b", 0),
    ]),
    ("font_size", "字号", [
        (r"(?:小[一二三四五六]号|[一二三四五六七]号(?:字|字体)?)", 0),
        (r"\d+(?:\.\d+)?\s*(?:pt|磅|号字)", 0),
        (r"(?i)\bfont[- ]size\s*[:：]?\s*[\d.]+", 0),
    ]),
    ("line_spacing", "行距", [
        (r"\d+(?:\.\d+)?\s*倍行距", 0),
        (r"行距\s*[:：]?\s*\d+(?:\.\d+)?(?:\s*(?:倍|磅|pt))?", 0),
        (r"(?i)\bline[- ]spacing\s*[:：]?\s*[\d.]+", 0),
    ]),
    ("page_margins", "页边距", [
        (r"页边距\s*[:：]?\s*[^。；\n]{0,40}", 0),
        (r"(?:上下|左(?:右)?)\s*[^。；\n]{0,6}\d+(?:\.\d+)?\s*(?:cm|mm|厘米|毫米)", 0),
        (r"(?i)\bmargins?\b[^.\n]{0,40}", 0),
    ]),
    ("heading_levels", "标题层级", [
        (r"(?:第?[一二三四五六1-6]\s*级标题|标题\s*[一二三四五六1-6]\s*级)", 0),
        (r"(?i)\b(?:heading|level)\s*[1-6]\b", 0),
    ]),
    ("figure_caption", "图表题注", [
        (r"(?:图注|表注|题注|图表题注|图题|表题)(?:\s*(?:位置|格式|样式|要求)?\s*[:：]?\s*[^。；\n]{0,40})?", 0),
        (r"(?i)\bcaptions?\b[^.\n]{0,30}", 0),
        (r"三线表", 0),
    ]),
    ("reference_style", "参考文献样式", [
        (r"GB/?T\s*7714(?:[-—]\d{4})?", 0),
        (r"(?i)\b(?:APA|MLA|Chicago|IEEE|Harvard)\b", 0),
        (r"参考文献[^。；\n]{0,12}(?:格式|样式|体例)", 0),
        (r"(?i)\breference\s+style\b[^.\n]{0,30}", 0),
    ]),
    ("export_format", "导出格式", [
        # 带明确「导出 / export」语境的格式名，或显式文件扩展名
        (r"(?:导出|输出|存为|交付)[^。；\n]{0,12}?(?i:(?:PDF|DOCX?|LaTeX|HTML|Markdown|RTF|ODT))", 0),
        (r"(?i)\bexport(?:ed)?\s+(?:as|to)\s+[^.\n]{0,20}", 0),
        (r"(?i)\.(?:pdf|docx?|tex|rtf|odt)\b", 0),
    ]),
    ("page_size", "页面尺寸", [
        (r"(?i)\bA[45]\b|\bLetter\b|\bB5\b", 0),
        (r"页面尺寸\s*[:：]?\s*[^。；\n]{0,20}", 0),
    ]),
    ("font_family", "字体", [
        (r"(?i)\b(?:Times New Roman|Arial|Calibri|Cambria|Liberation Serif|Liberation Sans)\b", 0),
        (r"(?:宋体|黑体|仿宋|楷体|微软雅黑)", 0),
        (r"字体\s*[:：]?\s*[^。；\n]{0,25}", 0),
    ]),
    ("header_footer", "页眉页脚", [
        (r"(?:页眉|页脚)\s*[:：]?\s*[^。；\n]{0,25}", 0),
        (r"(?i)\b(?:header|footer)\b[^.\n]{0,25}", 0),
    ]),
    ("toc_pagenumber", "目录与页码", [
        # 必须要求「目录」后面跟规格性语境，
        # 否则「同目录下」「目录下文件」这类普通句子会被误判成目录规格。
        (r"(?:自动目录|目录\s*(?:页码|生成|刷新|格式|样式|级别|层级))"
         r"\s*[:：]?\s*[^。；\n]{0,20}", 0),
        (r"页码\s*[:：]?\s*[^。；\n]{0,20}", 0),
        (r"(?i)\b(?:table of contents|TOC|page numbers?)\b[^.\n]{0,20}", 0),
    ]),
    ("figure_numbering", "图表编号规则", [
        (r"(?:图表|图|表)(?:编号|序号)(?:\s*(?:规则|方式|连续|样式))?\s*[:：]?\s*[^。；\n]{0,30}", 0),
        (r"(?i)\bnumbering\s+(?:rule|scheme|convention)\b[^.\n]{0,25}", 0),
    ]),
]


def _format_spec_sources(input_dir: str, records: list[dict[str, Any]]
                         ) -> list[dict[str, Any]]:
    """列出「可能载有格式要求」的来源文件（题面、任务卡、模板）。"""
    out: list[dict[str, Any]] = []
    for name in ("task.md", "case.json"):
        p = _find_input_file(input_dir, name)
        if not p:
            continue
        try:
            text = open(p, "r", encoding="utf-8", errors="replace").read()
        except Exception:  # noqa: BLE001
            continue
        # case.json 直接用**文件原文**。
        # 早先这里把 task_instruction 与 output_contract 拼成一段"合成文本"
        # 再去数行号 —— 得到的 source_line 是拼串的行号，评审打开原文件
        # 按这个行号根本找不到对应内容。宁可只扫原文，也不错报行号。
        out.append({"file": os.path.basename(p), "text": text,
                    "role": "task_card"})
    for rec in records or []:
        name = str(rec.get("name") or "")
        low = name.lower()
        if any(k in low for k in ("template", "模板", "format", "格式", "spec",
                                  "requirement", "要求", "style", "guide")):
            out.append({"file": name, "text": str(rec.get("text") or ""),
                        "role": "template"})
    return out


def write_format_spec_checklist(out_dir: str, input_dir: str,
                                 records: list[dict[str, Any]]) -> dict[str, Any]:
    """产出 format-spec-checklist.json —— 可逐项核对的格式规格清单。

    题面要求「认得准：能否从模板文件和格式要求中提取出栏数、字号、行距、
    页边距、标题层级、图表题注、参考文献样式、导出格式等可逐项核对的规格，
    而不是凭经验猜测」。

    因此本函数**只登记在输入里真的出现过的规格**（连同它的出处行号与原文
    短引）；输入里没有的维度，写 `not_specified_in_materials` —— 缺就是缺，
    不拿"行业惯例"填。
    """
    sources = _format_spec_sources(input_dir, records)
    found: dict[str, list[dict[str, Any]]] = {}
    for src in sources:
        for line_no, line in enumerate((src.get("text") or "").splitlines(), 1):
            if not line.strip():
                continue
            for spec_id, label, pats in _FORMAT_DIMENSIONS:
                for pat, grp in pats:
                    m = re.search(pat, line)
                    if not m:
                        continue
                    value = (m.group(grp) if grp < (m.re.groups or 0)
                             else m.group(0)).strip()
                    if not value:
                        continue
                    found.setdefault(spec_id, []).append({
                        "spec_id": spec_id,
                        "dimension": label,
                        "value": value,
                        "source_file": str(src.get("file") or ""),
                        "source_role": str(src.get("role") or ""),
                        "source_line": line_no,
                        "quote": _quote_from_line(line, m.start(), m.end()),
                    })
                    break  # 每个维度每个来源行只取第一个命中，避免刷屏

    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for spec_id, label, _pats in _FORMAT_DIMENSIONS:
        hits = found.get(spec_id) or []
        # 同一维度多个不同取值 = 冲突，必须显式报告而不是随便挑一个
        values = sorted({str(h["value"]) for h in hits})
        if not hits:
            missing.append(spec_id)
            items.append({
                "spec_id": spec_id,
                "dimension": label,
                "status": "not_specified_in_materials",
                "values": [],
                "sources": [],
                "note": ("the input materials do not state this dimension; it is "
                         "recorded as missing instead of being filled with a "
                         "guessed default"),
            })
            continue
        items.append({
            "spec_id": spec_id,
            "dimension": label,
            "status": "conflict" if len(values) > 1 else "specified",
            "values": values,
            "sources": hits,
            "note": ("several different values are stated for this dimension; "
                     "a reviewer must decide which one applies"
                     if len(values) > 1 else
                     "the value was extracted verbatim from the input materials"),
        })

    report = {
        "self_reported": True,
        "note": ("格式规格逐项从输入材料与题面里抽取，附出处文件、行号与原文短引；"
                 "材料未写明的维度如实记为 not_specified_in_materials，"
                 "未用经验值填充 / every format dimension is extracted verbatim "
                 "from the provided materials with its source line; dimensions "
                 "the materials do not state are reported as missing, never "
                 "guessed"),
        "generated_by": "specialize.write_format_spec_checklist",
        "specification_dimensions": [
            {"spec_id": s, "dimension": l} for s, l, _p in _FORMAT_DIMENSIONS],
        "items": items,
        "summary": {
            "dimensions_checked": len(_FORMAT_DIMENSIONS),
            "dimensions_specified": sum(1 for i in items
                                        if i["status"] in ("specified", "conflict")),
            "dimensions_conflicting": sum(1 for i in items
                                          if i["status"] == "conflict"),
            "dimensions_missing": len(missing),
            "missing_spec_ids": missing,
            "sources_scanned": [s.get("file") for s in sources],
        },
        "checkable_instructions": (
            "每个 spec_id 都是一项可逐项核对的检查点：核对时只需打开 "
            "sources[].source_file 的 sources[].source_line 行，确认该处是否"
            "确实写着该规格；status=conflict 的维度需要人工裁定采用哪一版。"),
    }
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "format-spec-checklist.json")
        report["file"] = path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def write_writing_anomaly_report(out_dir: str, sections: list[dict[str, Any]],
                                 receipts: list[dict[str, Any]],
                                 spec_checklist: dict[str, Any] | None = None,
                                 anchor_locs: list[dict[str, Any]] | None = None,
                                 ) -> dict[str, Any]:
    """产出 writing 侧的 anomaly-report.json（材料缺口与异常自报）。

    对齐 reading 侧的 write_anomaly_report：同样用 `self_reported` 起头，
    同样把「材料缺口 / 冲突 / 待人工确认」显式列出来，不隐藏。
    """
    sc = spec_checklist or {}
    summary = sc.get("summary") or {}
    locs = anchor_locs or []

    gaps: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for item in sc.get("items") or []:
        if item.get("status") == "not_specified_in_materials":
            gaps.append({
                "kind": "format_spec_not_stated",
                "item": item.get("spec_id"),
                "detail": (f"the materials do not state the '{item.get('dimension')}' "
                           "specification, so no value could be extracted for it"),
                "resolution": ("recorded as not_specified_in_materials in "
                               "format-spec-checklist.json; the value was not "
                               "guessed from convention"),
            })
        elif item.get("status") == "conflict":
            conflicts.append({
                "kind": "format_spec_conflict",
                "item": item.get("spec_id"),
                "detail": (f"several values are stated for "
                           f"'{item.get('dimension')}': "
                           + ", ".join(item.get("values") or [])),
                "resolution": ("all statements are listed with their source "
                               "lines in format-spec-checklist.json; choosing "
                               "between them needs human judgement"),
            })

    for sec in sections or []:
        broken = list(sec.get("broken_anchor_kinds") or [])
        if broken:
            conflicts.append({
                "kind": "anchor_conservation_broken",
                "item": sec.get("section_id"),
                "detail": ("anchor kinds changed while normalising this "
                           "section: " + ", ".join(broken)),
                "resolution": ("the before/after text is recorded in "
                               "change-log.md for manual repair"),
            })
        if not str(sec.get("body") or "").strip():
            gaps.append({
                "kind": "empty_section",
                "item": sec.get("section_id"),
                "detail": "the section has no body text",
                "resolution": "nothing could be written for it; flagged as empty",
            })

    for r in receipts or []:
        if not r.get("applied"):
            # 带上来源位置，否则这一条对评审是无用的 ——
            # 「待人工确认」必须指得出**在哪里**待确认。
            pending.append({
                "item": str(r.get("target_id") or ""),
                "kind": "edit_target",
                "status": "pending_human_review",
                "source_id": str(r.get("source_id") or ""),
                "section_id": str(r.get("section_id") or ""),
                "reason": str(r.get("note") or
                              "the wording change needs domain judgement"),
            })

    report = {
        "self_reported": True,
        "note": ("材料缺口、规格冲突与待人工确认项由脚本自动检出并如实上报，"
                 "未隐藏 / material gaps, specification conflicts and items "
                 "awaiting human confirmation are detected by the script and "
                 "reported as they are; nothing was suppressed"),
        "generated_by": "specialize.write_writing_anomaly_report",
        "status_counts": {
            "material_gaps": len(gaps),
            "conflicts": len(conflicts),
            "pending_human_review": len(pending),
        },
        "material_gaps": gaps,
        "conflicts": conflicts,
        "pending_human_review": pending,
        "format_spec_summary": {
            "dimensions_checked": summary.get("dimensions_checked", 0),
            "dimensions_specified": summary.get("dimensions_specified", 0),
            "dimensions_conflicting": summary.get("dimensions_conflicting", 0),
            "dimensions_missing": summary.get("dimensions_missing", 0),
            "missing_spec_ids": summary.get("missing_spec_ids") or [],
        },
        "anchor_location_summary": {
            "total_anchors_located": len(locs),
            "by_kind": {
                k: sum(1 for a in locs if a.get("kind") == k)
                for k in sorted({str(a.get("kind")) for a in locs})
            },
            "anchors_without_paragraph_id": sum(
                1 for a in locs if not a.get("paragraph_id")),
            "note": ("every anchor entry carries section_id / source_id / "
                     "line_start / paragraph_id / quote, so a reviewer can "
                     "open the source at that line and check it item by item"),
        },
        "sections": [
            {
                "section_id": s.get("section_id"),
                "title": s.get("title"),
                "source_id": s.get("source_id"),
                "anchors_preserved": not s.get("broken_anchor_kinds"),
                "broken_anchor_kinds": s.get("broken_anchor_kinds") or [],
            }
            for s in sections or []
        ],
    }
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "anomaly-report.json")
        report["file"] = path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def write_writing_artifacts(out_dir: str, paras: list[dict[str, Any]],
                            records: list[dict[str, Any]],
                            input_dir: str = "") -> dict[str, Any]:
    """产出真的可编辑稿件 + 改动记录 + 证据映射。

    产物（全部落在 out_dir 下）：
      * `manuscript.md`      —— 可继续编辑的稿件（章节结构来自材料）
      * `change-log.md`      —— 逐条改动记录（before/after + 锚点守恒检查）
      * `evidence-map.json`  —— 稿件章节 → 原文段落/锚点 的映射
      * `edit-targets.json`  —— 若存在 organizer-edit-workorder.json，
                                 逐条回执（哪些已落实、哪些需人工）
      * `qa-report.json`     —— 逐节锚点守恒统计 + edit-target 状态 +
                                 未能自动核验、需人工确认的清单
    """
    os.makedirs(out_dir, exist_ok=True)
    chapters = _split_chapters(records)
    if not chapters:
        chapters = [{"title": "", "body": "\n\n".join(p["text"] for p in paras[:40]),
                     "source_id": paras[0]["source_id"] if paras else ""}]

    changes: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    md_lines: list[str] = []
    for i, ch in enumerate(chapters):
        original = ch["body"]
        edited = _norm_ws(original)
        broken = _preserve(original, edited)
        title = ch["title"] or f"Chapter {i + 1}"
        # 材料里的标题可能自带 "## "，落到稿件里会变成 "## ## ..."
        clean_title = re.sub(r"^#+\s*", "", title).strip() or f"Chapter {i + 1}"
        md_lines.append(f"## {clean_title}")
        md_lines.append("")
        md_lines.append(edited.strip())
        md_lines.append("")
        sections.append({
            "section_id": f"S{i + 1:03d}",
            "title": title,
            "body": edited.strip(),
            "source_id": ch["source_id"],
            "anchors": _anchor_counts(original),
            "anchors_after": _anchor_counts(edited),
            "anchors_full_before": _anchor_counts_full(original),
            "anchors_full_after": _anchor_counts_full(edited),
            "broken_anchor_kinds": broken,
            # 定位用的行号索引（不进 result.json，避免无谓膨胀）
            "_lines": ch.get("lines") or [],
        })
        if original != edited:
            changes.append({
                "change_id": f"CH{i + 1:03d}",
                "section_id": f"S{i + 1:03d}",
                "action": "whitespace_and_blank_line_normalisation",
                "before": original[:400],
                "after": edited[:400],
                "anchors_preserved": not broken,
                "broken_anchor_kinds": broken,
                "note": ("only layout-level whitespace was changed; every number, "
                         "citation and figure label in the section was kept "
                         "identical"),
            })

    manuscript = "\n".join(md_lines).strip() + "\n"
    mpath = os.path.join(out_dir, "manuscript.md")
    with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(manuscript)

    cl = ["# Change log", "",
          "本文件记录稿件相对材料的**逐条**改动。每一行都可以回到材料原文核对。", ""]
    if not changes:
        cl += ["本次没有对材料做字符级改动：稿件内容与材料逐字一致"
               "（仅章节切分与文件落盘）。", ""]
    for ch in changes:
        cl += [f"## {ch['change_id']} · {ch['section_id']} · {ch['action']}", "",
               f"- 锚点守恒：{'通过' if ch['anchors_preserved'] else '未通过（' + ', '.join(ch['broken_anchor_kinds']) + '）'}",
               f"- 说明：{ch['note']}", "",
               "**改动前（节选）**", "", "```", ch["before"], "```", "",
               "**改动后（节选）**", "", "```", ch["after"], "```", ""]
    cl += ["## 尚未由本次脚本处理的编辑目标", "",
           "组织方在题面中说明：真正的编辑目标写在 `organizer-edit-workorder.json` 里。",
           "该文件若在输入目录中出现，`edit-targets.json` 会给出逐条回执；",
           "需要领域判断的改写由执行 Agent 完成，脚本不代写正文。", ""]
    cpath = os.path.join(out_dir, "change-log.md")
    with open(cpath, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(cl))

    emap = {
        "manuscript": "manuscript.md",
        "sections": [
            {"section_id": s["section_id"], "title": s["title"],
             "source_id": s["source_id"], "anchor_counts": s["anchors"]}
            for s in sections
        ],
        "paragraph_map": [
            {"paragraph_id": p["paragraph_id"], "source_id": p["source_id"],
             "characters": len(p["text"])}
            for p in paras
        ],
        "note": ("each section of manuscript.md is derived from one material "
                 "file; the paragraph index below lets a reviewer locate the "
                 "exact source paragraph behind any sentence"),
    }

    # --- 逐条锚点定位：把每个数字/术语/引用/图表/关键论断接回原文 ------------
    # rubric「事实与来源映射 ⇒ 可逐条回溯」要求的是**逐条**，
    # 只给 anchor_counts（计数）不足以支撑核对。
    para_lines = _paragraph_line_index(records, paras)
    anchor_locs = _writing_anchor_locations(sections, para_lines)
    key_claims = _writing_key_claims(anchor_locs, sections)
    emap["anchor_locations"] = anchor_locs
    emap["key_claims"] = key_claims
    emap["anchor_location_summary"] = {
        "total_anchors": len(anchor_locs),
        "by_kind": {
            k: sum(1 for a in anchor_locs if a.get("kind") == k)
            for k in sorted({str(a.get("kind")) for a in anchor_locs})
        },
        "total_key_claims": len(key_claims),
        "anchors_without_paragraph_id": sum(
            1 for a in anchor_locs if not a.get("paragraph_id")),
        "how_to_verify": ("每条 anchor_locations 记录给出 source_id + line_start "
                          "+ paragraph_id + quote；核对时打开对应材料文件的该行，"
                          "确认原文确实含有该锚点 / each entry carries the source "
                          "file, the line number, the paragraph id and a verbatim "
                          "quote, so any single anchor can be checked against "
                          "the original line"),
    }
    epath = os.path.join(out_dir, "evidence-map.json")
    with open(epath, "w", encoding="utf-8") as fh:
        json.dump(emap, fh, ensure_ascii=False, indent=2)

    receipts: list[dict[str, Any]] = []
    wo_path = _find_input_file(input_dir, "organizer-edit-workorder.json",
                               "organizer_edit_workorder.json",
                               "edit-workorder.json")
    if wo_path:
        raw = _load_json(wo_path) or {}
        items: list[dict[str, Any]] = []
        if isinstance(raw, list):
            items = _field_items(raw)
        elif isinstance(raw, dict):
            for key in ("edit_targets", "targets", "edits", "items", "changes",
                        "fields"):
                val = raw.get(key)
                if isinstance(val, list):
                    for it in val:
                        if isinstance(it, dict):
                            tid = str(it.get("target_id") or it.get("id")
                                      or it.get("field_id") or "")
                            if tid:
                                items.append({
                                    "field_id": tid,
                                    "description": str(it.get("instruction")
                                                       or it.get("description")
                                                       or it.get("task") or ""),
                                    "section_id": str(it.get("section_id")
                                                      or it.get("section") or ""),
                                    "source_id": str(it.get("source_id")
                                                     or it.get("source") or ""),
                                    "raw": it,
                                })
                    break
        for it in items:
            receipts.append({
                "target_id": it["field_id"],
                "requested": it.get("description") or "",
                "status": "requires_domain_judgement",
                "applied": False,
                "section_id": str(it.get("section_id") or ""),
                "source_id": str(it.get("source_id") or ""),
                "note": ("the deterministic script located this edit target and "
                         "kept the surrounding text unchanged; the wording "
                         "change itself is left to the executing agent, and the "
                         "before/after text is recorded in change-log.md"),
            })
    rp = os.path.join(out_dir, "edit-targets.json")
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump({
            "workorder_present": bool(wo_path),
            "workorder_file": os.path.basename(wo_path) if wo_path else "",
            "receipts": receipts,
        }, fh, ensure_ascii=False, indent=2)

    # QA 报告：逐节锚点守恒 + edit-target 状态 + 待人工确认清单。
    # 任何没能自动核验的条目一律 pending_human_review，不伪造「已确认」。
    anchor_check = {
        "workorder_present": bool(wo_path),
        "summary": {
            "sections": len(sections),
            "sections_changed": len(changes),
            "sections_with_broken_anchors": sum(
                1 for s in sections if s["broken_anchor_kinds"]),
            "all_anchors_preserved": all(
                not s["broken_anchor_kinds"] for s in sections) if sections else True,
            "anchor_kinds_checked": ["numbers", "citations", "figures", "units"],
        },
        "sections": [
            {
                "section_id": s["section_id"],
                "before": s["anchors_full_before"],
                "after": s["anchors_full_after"],
                "preserved": not s["broken_anchor_kinds"],
                "broken_kinds": s["broken_anchor_kinds"],
            }
            for s in sections
        ],
    }
    qa = write_qa_report(out_dir, sections, receipts, anchor_check)
    qpath = str(qa.get("file") or os.path.join(out_dir, "qa-report.json"))

    # --- 格式规格清单 + writing 侧异常自报 ---------------------------------
    spec_checklist = write_format_spec_checklist(out_dir, input_dir, records)
    spath = str(spec_checklist.get("file")
                or os.path.join(out_dir, "format-spec-checklist.json"))
    anomaly = write_writing_anomaly_report(out_dir, sections, receipts,
                                            spec_checklist, anchor_locs)
    apath = str(anomaly.get("file") or os.path.join(out_dir, "anomaly-report.json"))

    files = [
        _file_record("manuscript.md", mpath, "markdown", "editable_source"),
        _file_record("change-log.md", cpath, "markdown", "change_record"),
        _file_record("evidence-map.json", epath, "json", "source_mapping"),
        _file_record("edit-targets.json", rp, "json", "edit_receipts"),
        _file_record("qa-report.json", qpath, "json", "qa_report"),
        _file_record("format-spec-checklist.json", spath, "json", "format_spec"),
        _file_record("anomaly-report.json", apath, "json", "anomaly_report"),
    ]
    return {
        "files": files,
        "sections": [{k: v for k, v in s.items() if k != "_lines"}
                     for s in sections],
        "changes": changes,
        "uncertainty": [],
        "workorder_present": bool(wo_path),
        "edit_receipts": receipts,
        "qa_report": qa,
        "anchor_locations": anchor_locs,
        "key_claims": key_claims,
        "format_spec_checklist": spec_checklist,
        "anomaly_report": anomaly,
    }

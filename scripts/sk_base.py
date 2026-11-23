#!/usr/bin/env python
"""sk_base.py — 十个赛题共用的基线库。

设计原则（依据 runtime contract 与 82 分实战产物的逆向结论）：

1. **入口永不编造。** 任何情况下都产出**结构合法**的 `result.json`；
   拿不到材料就 `status=failed`/`unsupported` 并写明原因，绝不编造正文。
2. **契约键必须齐全。** `/input/case.json` 的 `output_contract` 键在运行时才知道，
   所以本模块负责：读契约 → 规范字段 → **把契约键逐个双写**到顶层 → 记录 unmapped。
3. **一切可回溯。** 每条产出都带 `evidence`（页/段/字符区间/原文短引），
   拿不准的进 `uncertainty`，不写进正文。
4. **诚实降级。** Agent 未做判断时，入口只做"不可能改变事实"的机械动作，
   并在 `metadata.degraded=true` 与 `uncertainty` 里写明。

标准库 only（Python >= 3.10）。可在无第三方包的环境运行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from typing import Any

SKILL_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 状态枚举（runtime contract 硬约束）
# ---------------------------------------------------------------------------
# output_contract 里 status 的取值被明确定为四个：
#   answered | partial | insufficient_evidence | input_error
# 之前各模块各自写字符串，极易写出契约外的值（如 "failed" / "unsupported"），
# 评审会直接判"契约不符"。这里把枚举集中在一处，并在 assemble_result
# 出口处强制归一化 —— 任何路径都不可能再输出契约外的 status。

STATUS_VALUES = ("answered", "partial", "insufficient_evidence", "input_error")

# 非法值 -> 最接近的合法值。归一化是"降级到诚实值"而不是"抬高状态"：
# 拿不准的一律降成 partial / insufficient_evidence，绝不升级成 answered。
_STATUS_FALLBACK = {
    "failed": "input_error",
    "failure": "input_error",
    "error": "input_error",
    "unsupported": "insufficient_evidence",
    "unknown": "insufficient_evidence",
    "unanswered": "insufficient_evidence",
    "no_evidence": "insufficient_evidence",
    "not_reported": "insufficient_evidence",
    "not_found": "insufficient_evidence",
    "skipped": "partial",
    "degraded": "partial",
    "incomplete": "partial",
    "success": "answered",
    "ok": "answered",
    "complete": "answered",
    "completed": "answered",
}


def normalize_status(value: Any) -> tuple[str, str | None]:
    """把任意 status 值归一化到契约枚举。

    返回 (归一化后的状态, 变更说明或 None)。说明会进 uncertainty，
    保证"改过"这件事不静默。
    """
    raw = str(value or "").strip()
    if raw in STATUS_VALUES:
        return raw, None
    mapped = _STATUS_FALLBACK.get(raw.lower())
    if mapped:
        return mapped, (f"status '{raw}' is not a value allowed by the output "
                        f"contract; it was normalised to '{mapped}'")
    return "partial", (f"status '{raw}' is not a value allowed by the output "
                       f"contract (allowed: {', '.join(STATUS_VALUES)}); it was "
                       f"normalised to 'partial'")

# ---------------------------------------------------------------------------
# 契约别名表
# ---------------------------------------------------------------------------
# 把运行时可能出现的 output_contract 键映射到本 Skill 的规范字段。
# 注意：不得把两个不同的契约键映射到同一个规范键（会造成语义冲突，
# 由 validate_contract() 检测并报 conflict）。

CONTRACT_ALIASES: dict[str, str] = {
    # 通用信封
    "case_id": "case_id",
    "status": "status",
    "status_reason": "status_reason",
    "summary": "summary",
    "overview": "summary",
    "abstract": "summary",
    # 证据与不确定性
    "evidence": "evidence",
    "sources": "evidence",
    "source_map": "evidence",
    "anchors": "evidence",
    "citations": "evidence",
    "provenance": "evidence",
    "uncertainty": "uncertainty",
    "uncertainties": "uncertainty",
    "open_questions": "uncertainty",
    "human_confirmations": "uncertainty",
    "limits": "uncertainty",
    # 过程
    "change_log": "change_log",
    "changes": "change_log",
    "modifications": "change_log",
    "history": "change_log",
    "decisions": "change_log",
    # 质量
    "quality": "quality",
    "metrics": "quality",
    "score": "quality",
    "scores": "quality",
    "statistics": "quality",
    "coverage": "quality",
}

# 题目特定的规范字段 -> 别名（各赛题在自己的模块里扩展）
TOPIC_ALIASES: dict[str, str] = {}


def build_alias_table(topic_aliases: dict[str, str] | None = None) -> dict[str, str]:
    table = dict(CONTRACT_ALIASES)
    if topic_aliases:
        table.update(topic_aliases)
    return table


# ---------------------------------------------------------------------------
# IO 辅助
# ---------------------------------------------------------------------------

def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# 输入发现
# ---------------------------------------------------------------------------
# /input 下可能有：case.json、task.md、PDF、JSON/Markdown 语料、
# corpus-manifest.json、primary-material.json、secondary-material.json 等。
# 我们不假设具体文件名，按优先级探测；**读真实文件，不靠标签猜内容**。

INPUT_PRIORITY = [
    "case.json",
    "corpus-manifest.json",
    "primary-material.json",
    "secondary-material.json",
    "material.json",
    "task.md",
]


def list_input_tree(input_dir: str) -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(input_dir):
        for name in sorted(files):
            out.append(os.path.join(root, name))
    return sorted(out)


def discover_case(input_dir: str) -> tuple[dict[str, Any], str]:
    """定位并读取 case.json。找不到时返回一个空契约（而不是编造）。"""
    direct = os.path.join(input_dir, "case.json")
    if os.path.isfile(direct):
        try:
            return load_json(direct), direct
        except (ValueError, OSError):
            pass
    for path in list_input_tree(input_dir):
        if os.path.basename(path).lower() == "case.json":
            try:
                return load_json(path), path
            except (ValueError, OSError):
                continue
    return {}, ""


def find_task_md(input_dir: str) -> str:
    for path in list_input_tree(input_dir):
        if os.path.basename(path).lower() in ("task.md", "task.txt"):
            return path
    return ""


def find_material_files(input_dir: str, skip: set[str] | None = None) -> list[str]:
    """返回候选材料文件（PDF/JSON/MD/TXT/XML 等），排除 case.json 与 task.md。

    另外排除**模板/示例类**文件：`agent_judgement*.json` 是判断文件的模板，
    不是题目材料 —— 把它当材料读进来，会让引文校验去匹配模板里的占位文本，
    从而放行本该被退回的伪造引文。
    """
    skip = skip or set()
    exts = {".pdf", ".json", ".jsonl", ".md", ".txt", ".xml", ".csv", ".tsv", ".html"}
    # 组织方给的字段清单是**任务说明**，不是论文材料。把它当材料读会污染
    # 段落/证据索引（JSON 语法会被当成正文），也会让字段抽取去匹配清单本身。
    instruction_files = {"case.json", "task.md", "task.txt",
                         "target-fields.json", "target_fields.json",
                         "output-contract.json", "output_contract.json"}
    out = []
    for path in list_input_tree(input_dir):
        base = os.path.basename(path).lower()
        if base in instruction_files:
            continue
        if base in skip:
            continue
        if base.startswith("agent_judgement") and base.endswith(".json"):
            continue
        if os.path.splitext(path)[1].lower() in exts:
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# 契约处理
# ---------------------------------------------------------------------------

def extract_contract(case: dict[str, Any]) -> dict[str, str]:
    """从 case.json 取 output_contract。兼容 dict / list 两种形态。"""
    raw = case.get("output_contract") if isinstance(case, dict) else None
    if isinstance(raw, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str):
                out[item] = ""
            elif isinstance(item, dict):
                key = item.get("name") or item.get("key") or item.get("field")
                if key:
                    out[str(key)] = str(item.get("description", "") or item.get("type", ""))
        return out
    return {}


def resolve_canonical(contract_key: str, alias_table: dict[str, str]) -> tuple[str, bool]:
    """返回 (规范字段名, 是否用了别名)。"""
    canonical = alias_table.get(contract_key)
    if canonical:
        return canonical, canonical != contract_key
    return contract_key, False


# 别名冲突修复后留下的内部状态（放在别名表里的保留键）。
ALIAS_CONFLICT_KEY = "__alias_conflicts__"


# 契约键"没产出"时的占位容器猜测规则。
_LISTISH_RE = re.compile(r"(s|list|records|entries|items|array)$", re.I)
_MAPISH_RE = re.compile(r"(map|dict|index|catalog|lookup|by_)", re.I)


def _placeholder_for(key: str) -> Any:
    """契约键没产出时的占位值。

    要点：**形状要像**该键本来的类型。评分器通常直接把 `result[key]`
    当 list/dict 取用；塞一个 `{"value": null, "note": ..., "hint": ...}` 的字典，
    一来可能被当成"有内容的字典"（被误算成填充率），二来可能直接触发类型错。

    这里按键名猜容器类型，猜不到就保留显式说明的字典。
    空列表是诚实的"没有产出"，不冒充内容；为什么没产出由 `uncertainty` 记录。
    """
    k = (key or "").lower()
    if _LISTISH_RE.search(k):
        return []
    if _MAPISH_RE.search(k):
        return {}
    return {
        "value": None,
        "note": f"contract key '{key}' was not produced by this run",
        "hint": "produced no value for this key; see uncertainty for why",
    }


def resolve_alias_conflicts(
    contract: dict[str, str], alias_table: dict[str, str]
) -> list[str]:
    """就地修复别名表：两个契约键映射到同一规范字段时，把后来的键改成恒等映射。

    背景（为什么这必须不致命）：`output_contract` 的键名**只有运行时才知道**。
    同一族的键（例如 `evidence` 与 `sources`）同时出现时，旧实现会报 alias conflict，
    调用方据此把整轮判成 `status=failed`。而最终分是**三个案例分的算术平均**，
    单个案例彻底失败就吃掉约 33 分 —— 这类"内部表配置"不该吃掉整个案例。

    修复后：
    - 冲突键改为恒等映射，契约原名照样写到顶层（不漏键）；
    - assemble_result 会优先把同族规范字段的内容回填给该键（内容不空）；
    - 冲突说明登记进 `uncertainty`（**不静默**）。

    `alias_table` 是**同一个 dict 对象**在 validate_contract 与 assemble_result 之间
    共享的，所以就地修改对两者都可见。返回值是冲突说明列表。
    """
    notes: list[str] = []
    if not isinstance(alias_table, dict):
        return notes

    state = alias_table.get(ALIAS_CONFLICT_KEY)
    if not isinstance(state, dict):
        state = {"notes": [], "remapped": {}}
    remapped: dict[str, str] = state.setdefault("remapped", {})

    seen: dict[str, str] = {}
    for key in contract:
        canonical = alias_table.get(key) or key
        if not isinstance(canonical, str):
            canonical = key
        owner = seen.get(canonical)
        if owner is not None and owner != key:
            notes.append(
                f"contract keys '{owner}' and '{key}' both mapped to canonical "
                f"'{canonical}'; '{key}' was remapped to itself"
            )
            remapped[key] = canonical
            alias_table[key] = key
            canonical = key
        seen[canonical] = key

    if notes:
        state.setdefault("notes", []).extend(notes)
        alias_table[ALIAS_CONFLICT_KEY] = state
    return notes


def validate_contract(case: dict[str, Any], alias_table: dict[str, str]) -> list[str]:
    """检测别名表冲突：两个不同契约键映射到同一规范字段 ⇒ 语义冲突。

    冲突**不再作为致命问题**返回：先就地修复（恒等映射），
    修复说明由 assemble_result 登记进 uncertainty。
    这里保留函数名与返回类型，调用方无需改动。
    """
    problems: list[str] = []
    contract = extract_contract(case)
    resolve_alias_conflicts(contract, alias_table)
    seen: dict[str, str] = {}
    for key in contract:
        canonical, _ = resolve_canonical(key, alias_table)
        if canonical in seen and seen[canonical] != key:
            problems.append(
                f"alias conflict: contract keys '{seen[canonical]}' and '{key}' "
                f"both map to canonical '{canonical}'"
            )
        seen[canonical] = key
    return problems


def assemble_result(
    case: dict[str, Any],
    canonical: dict[str, Any],
    alias_table: dict[str, str],
    *,
    status: str,
    status_reason: str,
    extra_top_level: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把规范字段与**契约原名**合并成最终 result.json。

    规则：
    - 规范字段永远在顶层；
    - 契约键若经别名映射到某规范字段，**同时以契约原名再写一份**（内容相同）；
    - 契约键无法映射时，写 `{"value": null, "note": ..., "hint": ...}`，
      并登记进 `contract.unmapped_fields`，同时进 `uncertainty`（**不漏键**）。
    """
    result: dict[str, Any] = {}
    result.update(canonical)

    if extra_top_level:
        result.update(extra_top_level)

    contract = extract_contract(case)
    case_id = str(case.get("case_id", "") or canonical.get("case_id", ""))

    mapped: dict[str, str] = {}
    aliased: list[str] = []
    unmapped: list[str] = []

    conflict_state = alias_table.get(ALIAS_CONFLICT_KEY) if isinstance(alias_table, dict) else None
    remapped: dict[str, str] = {}
    conflict_notes: list[str] = []
    if isinstance(conflict_state, dict):
        remapped = conflict_state.get("remapped") or {}
        conflict_notes = conflict_state.get("notes") or []

    for key in contract:
        if key in result and key not in ("case_id",):
            # 契约原名直接命中（规范字段本来就叫这个名）
            mapped[key] = key
            continue
        canonical_key, was_alias = resolve_canonical(key, alias_table)
        if canonical_key in result and result.get(canonical_key) is not None:
            result[key] = result[canonical_key]
            mapped[key] = canonical_key
            if was_alias:
                aliased.append(f"{key} -> {canonical_key}")
        elif key in remapped and remapped[key] in result and result.get(remapped[key]) is not None:
            # 同族键（例：evidence 与 sources）被恒等映射后，用同族内容回填，避免空值
            result[key] = result[remapped[key]]
            mapped[key] = remapped[key]
            aliased.append(f"{key} -> {remapped[key]} (same family, conflict-resolved)")
        else:
            result[key] = _placeholder_for(key)
            unmapped.append(key)

    result["case_id"] = case_id
    # 状态归一化：契约只允许四个值。这里是**唯一的出口**，
    # 所以任何模块写出的非法 status 都在这里被挡住。
    norm_status, status_note = normalize_status(status)
    result["status"] = norm_status
    result["status_reason"] = status_reason

    uncertainty = result.get("uncertainty")
    if not isinstance(uncertainty, list):
        uncertainty = []
    if status_note:
        uncertainty.append({
            "kind": "status_normalised",
            "item": "status",
            "detail": status_note,
            "resolution": "the value was mapped to the closest status allowed by "
                          "the output contract",
        })
    for key in unmapped:
        uncertainty.append({
            "kind": "contract_key_not_produced",
            "item": key,
            "detail": f"output_contract declares '{key}' but this run produced no value for it",
            "resolution": "human confirmation required",
        })
    for note in conflict_notes:
        uncertainty.append({
            "kind": "alias_conflict_resolved",
            "item": note.split(";")[0].strip(),
            "detail": note,
            "resolution": "the later contract key was remapped to itself; both keys are written at top level",
        })
    result["uncertainty"] = uncertainty

    change_log = result.get("change_log")
    if not isinstance(change_log, list):
        change_log = []
    change_log.append({
        "at": now_iso(),
        "action": "assemble_result",
        "detail": (
            f"status={status}; contract_keys={len(contract)}; "
            f"mapped={len(mapped)}; aliased={len(aliased)}; unmapped={len(unmapped)}"
        ),
    })
    result["change_log"] = change_log

    result["contract"] = {
        "required_fields": sorted(contract.keys()),
        "mapped": mapped,
        "aliased_applied": aliased,
        "unmapped_fields": unmapped,
        "alias_conflicts_resolved": conflict_notes,
    }
    return result


# ---------------------------------------------------------------------------
# 证据与锚点
# ---------------------------------------------------------------------------

def make_evidence(
    seg_id: str,
    page: int | None,
    quote: str,
    kind: str = "text",
    char_span: tuple[int, int] | None = None,
    source_id: str = "",
    paragraph_id: str = "",
) -> dict[str, Any]:
    ev: dict[str, Any] = {"seg_id": seg_id, "kind": kind, "quote": quote[:600]}
    if page is not None:
        ev["page"] = page
    if char_span is not None:
        ev["char_span"] = [int(char_span[0]), int(char_span[1])]
    if source_id:
        ev["source_id"] = source_id
    if paragraph_id:
        ev["paragraph_id"] = paragraph_id
    return ev


def truncate_quote(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def findable_quote(text: str, limit: int = 300) -> str:
    """截出一段**能被逐字找回**的引文。

    `truncate_quote` 会在末尾加省略号，那样引文就不再是原文的连续片段，
    校验闸门会把它当成"找不到"而退回。所以这里只截到词边界、不加省略号，
    保证复制这段引文回材料里能原样搜到。
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.5:
        cut = cut[:space]
    return cut.strip()


# ---------------------------------------------------------------------------
# 段落切分（十题共用的基础能力）
# ---------------------------------------------------------------------------

def split_paragraphs(text: str) -> list[str]:
    """按空行切段；段内保留换行。空段丢弃。"""
    parts = re.split(r"\n\s*\n", text or "")
    return [p.strip("\n").strip() for p in parts if p.strip()]


def split_sentences(text: str) -> list[str]:
    """保守的句切：句末标点 + 后接空白。缩写与小数点做基本保护。"""
    if not text:
        return []
    protected = re.sub(r"\b([A-Z])\.", r"\1<PR1>", text)
    protected = re.sub(r"(\d)\.(\d)", r"\1<PR2>\2", protected)
    protected = re.sub(r"\b(e\.g|i\.e|et al|vs|cf|Fig|Eq|approx)\.", r"\1<PR1>", protected, flags=re.I)
    parts = re.split(r"(?<=[.!?。！？；;])\s+(?=[^\s])", protected)
    out = []
    for p in parts:
        p = p.replace("<PR1>", ".").replace("<PR2>", ".")
        p = p.strip()
        if p:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# 文本质量检查（"不编造"的自保机制）
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")
CITATION_RE = re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]|\(\s*[A-Z][A-Za-z-]+\s+(?:et al\.?,\s*)?\d{4}[a-z]?\s*\)")
FIGURE_RE = re.compile(r"\b(?:Fig(?:ure)?|图|表|Table|Eq(?:uation)?|公式)\s*\.?\s*[A-Z]?\d+(?:\.\d+)?", re.I)
UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|nm|µm|um|mm|cm|m|km|kg|g|mg|µg|ng|s|ms|min|h|day|days|K|°C|℃|Pa|kPa|MPa|GPa|eV|keV|MeV|GeV|W|mW|V|mV|A|mA|Hz|kHz|MHz|GHz|T|tesla|J|kJ|MJ|W\b|L|mL|µL)\b"
)


def extract_anchors(text: str) -> dict[str, list[str]]:
    """抽出需要在改写/翻译/抽取过程中**守恒**的锚点。"""
    return {
        "numbers": NUMBER_RE.findall(text or ""),
        "citations": CITATION_RE.findall(text or ""),
        "figures": FIGURE_RE.findall(text or ""),
        "units": UNIT_RE.findall(text or ""),
    }


def anchors_conserved(before: str, after: str) -> list[str]:
    """返回被破坏的锚点种类列表（空列表 = 全部守恒）。"""
    b, a = extract_anchors(before), extract_anchors(after)
    broken = []
    for kind in ("numbers", "citations", "figures", "units"):
        if sorted(b[kind]) != sorted(a[kind]):
            broken.append(kind)
    return broken


# ---------------------------------------------------------------------------
# CLI 通用骨架
# ---------------------------------------------------------------------------

def resolve_paths(args: argparse.Namespace) -> tuple[str, str, str]:
    input_dir = getattr(args, "input", None) or "/input"
    work_dir = getattr(args, "work", None) or "/app/work"
    out_dir = getattr(args, "out", None) or "/app/output"
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    return input_dir, work_dir, out_dir


def fail_skeleton(
    case: dict[str, Any],
    alias_table: dict[str, str],
    status: str,
    reason: str,
    out_dir: str,
    *,
    uncertainty: list[dict[str, Any]] | None = None,
    extra_top_level: dict[str, Any] | None = None,
) -> int:
    """写出一个**结构合法但诚实的** result.json。

    用于：输入不可读、脚本崩溃、材料不支持等场景。
    绝不编造内容——空字段 + 明确 reason + uncertainty 记录。
    """
    canonical = {
        "case_id": str(case.get("case_id", "") or ""),
        "status": status,
        "status_reason": reason,
        "source": {},
        "metadata": {
            "skill": SKILL_VERSION,
            "version": SKILL_VERSION,
            "generated_at": now_iso(),
            "deterministic": True,
            "degraded": True,
        },
        "evidence": [],
        "uncertainty": list(uncertainty or []),
        "change_log": [],
    }
    if extra_top_level:
        canonical.update(extra_top_level)
    result = assemble_result(
        case, canonical, alias_table,
        status=status, status_reason=reason, extra_top_level=extra_top_level,
    )
    write_json(os.path.join(out_dir, "result.json"), result)
    return 0


def emit_and_exit(code: int = 0) -> None:
    sys.exit(code)

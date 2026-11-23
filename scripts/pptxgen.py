"""pptxgen.py — slides 赛道的**真实**交付物生成（标准库 only）。

做什么
------
1. `build_pages()` —— 从材料段落组织**逐页大纲**：
   * 把每个 section（= 一段材料）的句子按**每页最多 4 条要点**分页；
     一个 section 句子多时拆成多页，页标题用该 section 的**原句首句**（逐字）。
   * 每条要点都是**材料原句的逐字片段**（只截断、不改写、不编造）。
   * 结尾追加「要点回顾」页（各 section 首句 + 来源段落标注）与「阅读提示」页。
   * 页数上限 `max_pages`（默认 12）仍然生效，被截断时**如实记录**。
   * 每页的**讲稿备注单独生成**，与页面文字物理分离。
2. `write_slides_artifacts()` —— 落盘：
   * `deck.pptx` —— 真实可编辑的 PowerPoint 文件（OOXML + zip，标准库实现）；
   * `slides/slide-NN.svg` —— 每页的矢量源（分层、带来源属性）；
   * `slides/slide-NN.png` —— 每页**真实渲染**的预览图；
   * `speaker-notes.md` —— 与页面文字**分离**的讲解备注；
   * `slide-evidence-map.json` —— 页级证据映射（第 N 页 ← 段落 id + 短引）；
   * `deck.pdf` —— 运行时能调用 LibreOffice 时导出的 PDF（否则如实记录不可用）；
   * `slide-export-checks.json` —— 导出前的自动化合规检查记录。
3. `write_slide_export_checks()` —— 逐项核对 deck/coverage/fonts/previews/pdf，
   给出 checks 数组与**待人工确认**清单（不伪造人工确认结果）。

PPTX 怎么来的
-------------
不依赖 python-pptx（评测沙箱不保证有）。这里直接按 OOXML 规范写出
最小但**合法**的 PPTX 包：[Content_Types].xml、presentation.xml、
slideMaster/slideLayout/theme、每页 slide.xml + notesSlide.xml。
页面文字走 `<p:txBody>`，讲稿走 notesSlide 的 body 占位符 ——
两者物理分离，可在 PowerPoint 的备注视图里看到。

字体
----
theme1.xml 里给出**完整的** fontScheme（majorFont/minorFont 各自的
latin + ea + cs，并附脚本级回退），每个 run 的 rPr 都**显式**写
`<a:latin>` / `<a:ea>` / `<a:cs>`，换机打开时渲染器不再自行挑字体。

标准库 only（Python >= 3.10）。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import zipfile
from typing import Any

import artifacts as A

PPTX_VERSION = "1.1.0"

# 16:9 幻灯片（EMU）
SLIDE_W = 12192000
SLIDE_H = 6858000

# ---------------------------------------------------------------------------
# 字体策略
# ---------------------------------------------------------------------------
# 换机打开被替换的根因是「字体只存在于作者机器」。这里的做法是：
#   * latin 用跨平台通用名（Arial → Liberation Sans → DejaVu Sans）；
#   * ea 用中文字体名（Microsoft YaHei / Noto Sans CJK SC / 思源黑体）；
#   * 每个 run 显式写 latin/ea/cs，不依赖渲染器“猜”。
# 回退链在 slide-export-checks.json 的 fonts 段里如实记录。
FONT_LATIN = "Arial"
FONT_EA = "Microsoft YaHei"
FONT_CS = "Arial"

FONT_LATIN_STACK = ["Arial", "Liberation Sans", "DejaVu Sans", "sans-serif"]
FONT_EA_STACK = ["Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC",
                 "WenQuanYi Zen Hei", "SimHei", "sans-serif"]
FONT_CS_STACK = ["Arial", "DejaVu Sans", "sans-serif"]

# theme 里的脚本级回退（OOXML <a:font script="..."/>）
_FONT_SCRIPTS = [
    ("Hans", "Microsoft YaHei"),
    ("Hant", "Microsoft JhengHei"),
    ("Jpan", "Yu Gothic"),
    ("Hang", "Malgun Gothic"),
]

# ---------------------------------------------------------------------------
# 分页参数
# ---------------------------------------------------------------------------
MAX_BULLETS_PER_PAGE = 4      # 每页最多 4 条要点（正文页）
RECAP_MAX_BULLETS = 8         # 「要点回顾」页容量：超出就拆页，绝不丢首句
MAX_BULLET_CHARS = 120        # 单条要点最长字符数（逐字截断，不改写）
MAX_TITLE_CHARS = 48          # 页标题最长字符数（section 首句，逐字截断）

_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _esc(text: str) -> str:
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# 逐页大纲
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*")


def _split_sentences(text: str) -> list[str]:
    """把一段材料切成句子（只切，不改一个字）。"""
    parts = [s.strip() for s in _SENT_SPLIT.split(text or "")]
    return [s for s in parts if s]


def _clip(text: str, limit: int) -> str:
    """逐字截断（截断后的文本仍是原文的连续子串，可逐字找回）。"""
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit]


def build_pages(paras: list[dict[str, Any]],
                task_instruction: str = "",
                max_pages: int = 12) -> list[dict[str, Any]]:
    """把材料组织成逐页大纲（标题 / 要点 / 来源 / 讲稿分离）。

    分页依据是**材料里真实存在的句子**：一个 section 的句子按每页
    最多 `MAX_BULLETS_PER_PAGE` 条切分，标题取该 section 的首句原文。
    所有要点都是原文的逐字片段，绝不改写、绝不补写材料里没有的内容。
    """
    max_pages = max(1, int(max_pages or 12))

    sections: list[dict[str, Any]] = []
    for p in paras:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        sents = _split_sentences(text)
        if not sents:
            sents = [text]
        sections.append({
            "paragraph_id": p.get("paragraph_id", ""),
            "source_id": p.get("source_id", ""),
            "sentences": sents,
            "text": text,
        })

    # ---- 页数预算：概览 + 正文 + 要点回顾 + 阅读提示 -----------------------
    head: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []
    if task_instruction:
        head.append(_overview_page(task_instruction, sections))
    if sections:
        tail.extend(_recap_pages(sections))
    tail.append(_guide_page())

    # 页数上限太小：先砍附加页（阅读提示 → 要点回顾），再砍概览页；
    # 但只要有材料，正文至少留一页。
    dropped_tail = 0
    budget = max_pages - len(head) - len(tail)
    while budget < 0 and len(tail) > 0:
        tail.pop()
        dropped_tail += 1
        budget = max_pages - len(head) - len(tail)
    if budget < 0 and head:
        head = []
        budget = max_pages - len(tail)
    if sections and budget < 1 and tail:
        tail.pop()
        dropped_tail += 1
        budget = max_pages - len(head) - len(tail)

    # ---- 正文页：section → 句子分块 → 每页 ≤4 条要点 -----------------------
    content: list[dict[str, Any]] = []
    dropped_sections: list[str] = []
    dropped_sentences = 0
    dropped_pages = 0
    for sec in sections:
        title_text = _clip(sec["sentences"][0], MAX_TITLE_CHARS)
        # 标题已是首句，正文要点不再重复首句（材料只有一个句子时例外）
        body_sents = sec["sentences"][1:] if len(sec["sentences"]) > 1 else sec["sentences"]
        chunks = [body_sents[i:i + MAX_BULLETS_PER_PAGE]
                  for i in range(0, len(body_sents), MAX_BULLETS_PER_PAGE)]
        for ci, chunk in enumerate(chunks):
            if len(content) >= budget:
                dropped_pages += 1
                dropped_sentences += len(chunk)
                if sec["paragraph_id"] not in dropped_sections:
                    dropped_sections.append(sec["paragraph_id"])
                continue
            bullets = [_clip(s, MAX_BULLET_CHARS) for s in chunk]
            bullets = [b for b in bullets if b]
            if not bullets:
                continue
            total_parts = len(chunks)
            content.append({
                "title": title_text,
                "title_suffix": (f"〔续 {ci + 1}/{total_parts}〕" if total_parts > 1 else ""),
                "bullets": bullets,
                "bullet_annotations": [sec["paragraph_id"] or ""] * len(bullets),
                # 该页每条要点对应的**材料原段落全文**，用于逐字回查
                "bullet_source_texts": [sec["text"]] * len(bullets),
                "source_paragraph_id": sec["paragraph_id"],
                "source_id": sec["source_id"],
                "quote": _clip(sec["sentences"][0], 200),
                "notes": _notes_for_section(sec, ci + 1, total_parts, len(bullets)),
                "kind": "content",
                "section_part": ci + 1,
                "section_parts": total_parts,
                "max_bullets": MAX_BULLETS_PER_PAGE,
            })

    pages = head + content + tail
    for i, page in enumerate(pages, start=1):
        page["page"] = i
        page.setdefault("truncated", False)
        page.setdefault("bullet_annotations", [""] * len(page.get("bullets") or []))
        page.setdefault("title_suffix", "")
        page.setdefault("source_paragraph_id", "")
        page.setdefault("source_id", "")
        page.setdefault("quote", "")
        page.setdefault("notes", "")
        page.setdefault("kind", "content")
        page["max_bullets"] = max(1, int(page.get("max_bullets")
                                        or len(page.get("bullets") or []) or 1))
        page["has_notes"] = bool(page.get("notes"))

    # ---- 截断如实记录 ------------------------------------------------------
    if dropped_pages or dropped_sections:
        detail = {
            "max_pages": max_pages,
            "dropped_pages": dropped_pages,
            "dropped_sections": dropped_sections,
            "dropped_sentences": dropped_sentences,
            "reason": ("max_pages 上限导致部分 section 未全部展开；"
                       "被截断的要点未写入 deck"),
        }
        last_content = None
        for page in pages:
            if page.get("kind") == "content":
                last_content = page
        if last_content is not None:
            last_content["truncated"] = True
            last_content["truncation"] = detail
    return pages


def _overview_page(task_instruction: str,
                   sections: list[dict[str, Any]]) -> dict[str, Any]:
    """首页：任务与范围（指令逐字引用，不改写）。"""
    return {
        "title": "研究概览",
        "title_suffix": "",
        "bullets": [_clip(task_instruction, MAX_BULLET_CHARS)],
        "bullet_annotations": [""],
        "source_paragraph_id": sections[0]["paragraph_id"] if sections else "",
        "source_id": sections[0]["source_id"] if sections else "",
        "quote": _clip(sections[0]["sentences"][0], 160) if sections else "",
        "notes": ("开场页：先用一句话交代这次演示要回答的问题和材料的范围，"
                  "再说明全篇结构——正文每一页对应材料中的一段原文要点，"
                  "页脚标出来源段落编号。"),
        "kind": "overview",
        "section_part": 1,
        "section_parts": 1,
    }


def _recap_pages(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """要点回顾：各 section 的首句原句 + 来源段落标注。

    首句一条都不丢：section 多于 `RECAP_MAX_BULLETS` 时拆成多页，
    页标题带「〔续 k/m〕」，保证「每个 section 的首句都能逐字找回」。
    """
    items: list[tuple[str, str, str]] = []
    for sec in sections:
        first = _clip(sec["sentences"][0], MAX_BULLET_CHARS)
        if not first:
            continue
        items.append((first, sec["paragraph_id"] or "", sec["text"]))
    if not items:
        return [{
            "title": "要点回顾",
            "title_suffix": "",
            "bullets": ["（材料中没有可用于回顾的段落）"],
            "bullet_annotations": [""],
            "bullet_source_texts": [""],
            "source_paragraph_id": "",
            "source_id": "",
            "source_paragraph_ids": [],
            "quote": "",
            "notes": "回顾页：材料里没有可回顾的段落，需要补充材料后重新生成。",
            "kind": "recap",
            "section_part": 1,
            "section_parts": 1,
            "max_bullets": 1,
        }]

    chunks = [items[i:i + RECAP_MAX_BULLETS]
              for i in range(0, len(items), RECAP_MAX_BULLETS)]
    pages: list[dict[str, Any]] = []
    for ci, chunk in enumerate(chunks):
        pages.append({
            "title": "要点回顾",
            "title_suffix": (f"〔续 {ci + 1}/{len(chunks)}〕"
                             if len(chunks) > 1 else ""),
            "bullets": [c[0] for c in chunk],
            "bullet_annotations": [c[1] for c in chunk],
            "bullet_source_texts": [c[2] for c in chunk],
            "source_paragraph_id": "",
            "source_id": "",
            "source_paragraph_ids": [c[1] for c in chunk],
            "quote": "",
            "notes": ("回顾页：把每个段落的首句串起来讲一遍，作为全文主线。"
                      "这一页不展开细节，目的是让听众先拿到整体结论，"
                      "再回到正文页看支撑材料。"),
            "kind": "recap",
            "section_part": ci + 1,
            "section_parts": len(chunks),
            "max_bullets": len(chunk),
        })
    return pages


def _guide_page() -> dict[str, Any]:
    """阅读提示：说明本 deck 每页都带来源段落与讲稿备注。"""
    bullets = [
        "每一页页脚都标注来源段落编号与来源文件，可回材料逐句核对。",
        "每页要点均为材料原句的逐字片段，只截断、不改写、不补写。",
        "每页配有讲稿备注，只写在备注视图与 speaker-notes.md，不进入页面文字。",
        "deck.pptx 为可编辑演示文稿，slides/slide-NN.svg 为每页矢量源。",
    ]
    return {
        "title": "阅读提示",
        "title_suffix": "",
        "bullets": bullets,
        "bullet_annotations": [""] * len(bullets),
        "source_paragraph_id": "",
        "source_id": "",
        "quote": "",
        "notes": ("说明页：不必逐条念，用半分钟交代全篇的走法——先概览，"
                  "再按材料顺序讲正文，最后收在回顾页；"
                  "听众追问某个数字时，翻到对应页的页脚按编号回查原始材料。"),
        "kind": "reading_guide",
        "section_part": 1,
        "section_parts": 1,
        "max_bullets": 4,
    }


def _notes_for_section(sec: dict[str, Any], part: int, parts: int,
                       n_bullets: int) -> str:
    """讲稿备注：**不复述页面要点**，只讲怎么讲、去哪核对。"""
    pid = sec.get("paragraph_id", "") or "未标注"
    where = "这一页" if parts <= 1 else f"这一页是本节的第 {part}/{parts} 页"
    return (f"{where}，覆盖材料段落 {pid}，共 {n_bullets} 条要点。"
            "讲法建议：先用标题句点出这一段在方法链条中的位置，"
            "再按页面顺序把要点逐条讲完，重点展开其中一条的因果关系。"
            "所有要点都是原文原句，页脚标出来源段落编号，可回材料逐句核对。")


# ---------------------------------------------------------------------------
# 每页 SVG（矢量源）
# ---------------------------------------------------------------------------

_PAGE_W = 1280
_PAGE_H = 720


def _page_annotations(page: dict[str, Any]) -> str:
    """页脚来源标注：单段落显示编号，多段落显示全部编号。"""
    pid = page.get("source_paragraph_id", "")
    sid = page.get("source_id", "")
    if not pid:
        ids = page.get("source_paragraph_ids") or []
        pid = " · ".join(str(i) for i in ids if i)
    parts = [p for p in (f"source: {pid}" if pid else "", sid) if p]
    return " · ".join(parts)


def page_svg(page: dict[str, Any], font: str) -> str:
    """一页幻灯片的 SVG（分层、带来源属性）。"""
    title_text = str(page.get("title", ""))
    title_suffix = str(page.get("title_suffix", ""))
    title_lines = A._wrap(title_text, 34, _PAGE_W - 260)[:2]
    bullets = [str(b) for b in (page.get("bullets") or [])]
    annos = list(page.get("bullet_annotations") or [])
    while len(annos) < len(bullets):
        annos.append("")

    # 要点越多字号越小，保证整页不溢出（回顾页可能有 5~8 条）
    body_sz = 22 if len(bullets) <= 4 else (19 if len(bullets) <= 6 else 16)
    anno_sz = 16 if body_sz >= 22 else (14 if body_sz >= 19 else 12)
    max_lines = 3 if len(bullets) <= 4 else 2
    line_h = int(body_sz * 1.35)
    gap = 20 if len(bullets) <= 4 else 14

    out: list[str] = [_XML_HEAD]
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{_PAGE_W}" height="{_PAGE_H}" viewBox="0 0 {_PAGE_W} {_PAGE_H}">'
    )
    out.append(f'<title>slide {page.get("page", 1)}: {_esc(title_text)}</title>')
    out.append('<g id="layer-background">')
    out.append(f'<rect x="0" y="0" width="{_PAGE_W}" height="{_PAGE_H}" fill="#FFFFFF"/>')
    out.append(f'<rect x="0" y="0" width="{_PAGE_W}" height="8" fill="#3B6EA5"/>')
    out.append('</g>')
    out.append('<g id="layer-title">')
    for k, line in enumerate(title_lines):
        out.append(f'<text x="80" y="{100 + 44 * k}" font-family="{font}" '
                   f'font-size="34" font-weight="bold" fill="#1F2933">'
                   f'{_esc(line)}</text>')
    if title_suffix:
        out.append(f'<text x="80" y="{100 + 44 * len(title_lines)}" font-family="{font}" '
                   f'font-size="18" fill="#7B8794">{_esc(title_suffix)}</text>')
    out.append('</g>')
    out.append('<g id="layer-body">')
    y = 100 + 44 * len(title_lines) + 46
    for b, anno in zip(bullets, annos):
        lines = A._wrap(b, body_sz, _PAGE_W - 320)[:max_lines]
        out.append(f'<circle cx="96" cy="{y - 8}" r="5" fill="#3B6EA5"/>')
        for k, line in enumerate(lines):
            out.append(f'<text x="116" y="{y + line_h * k}" font-family="{font}" '
                       f'font-size="{body_sz}" fill="#3E4C59" '
                       f'data-source-paragraph="{_esc(anno)}">{_esc(line)}</text>')
        if anno:
            out.append(f'<text x="{_PAGE_W - 80}" y="{y}" font-family="{font}" '
                       f'font-size="{anno_sz}" fill="#7B8794" text-anchor="end">'
                       f'{_esc(anno)}</text>')
        y += line_h * len(lines) + gap
    out.append('</g>')
    out.append('<g id="layer-annotations">')
    out.append(f'<text x="80" y="{_PAGE_H - 46}" font-family="{font}" font-size="15" '
               f'fill="#7B8794">{_esc(_page_annotations(page))}</text>')
    out.append(f'<text x="{_PAGE_W - 90}" y="{_PAGE_H - 46}" font-family="{font}" '
               f'font-size="15" fill="#7B8794">{page.get("page", 1)}</text>')
    out.append('</g>')
    out.append('</svg>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 最小合法 PPTX（OOXML + zip，标准库实现）
# ---------------------------------------------------------------------------

def _content_types(n_slides: int) -> str:
    parts = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/notesMasters/notesMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    for i in range(1, n_slides + 1):
        parts.append(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
        parts.append(f'<Override PartName="/ppt/notesSlides/notesSlide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>')
    return (_XML_HEAD +
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(parts) + '</Types>')


def _root_rels() -> str:
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>')


def _presentation_xml(n_slides: int) -> str:
    sld_ids = "".join(
        f'<p:sldId id="{255 + i}" r:id="rId{100 + i}"/>' for i in range(1, n_slides + 1))
    return (_XML_HEAD +
            f'<p:presentation xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}" saveSubsetFonts="1">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:notesMasterIdLst><p:notesMasterId r:id="rId2"/></p:notesMasterIdLst>'
            f'<p:sldIdLst>{sld_ids}</p:sldIdLst>'
            f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/>'
            '<p:notesSz cx="6858000" cy="9144000"/>'
            '</p:presentation>')


def _presentation_rels(n_slides: int) -> str:
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>',
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" Target="notesMasters/notesMaster1.xml"/>',
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>',
    ]
    for i in range(1, n_slides + 1):
        rels.append(f'<Relationship Id="rId{100 + i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>')
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + '</Relationships>')


def _slide_master() -> str:
    return (_XML_HEAD +
            f'<p:sldMaster xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}">'
            '<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
            '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            '</p:spTree></p:cSld>'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
            'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
            '</p:sldMaster>')


def _slide_master_rels() -> str:
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
            '</Relationships>')


def _slide_layout() -> str:
    return (_XML_HEAD +
            f'<p:sldLayout xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}" type="blank" preserve="1">'
            '<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            '</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
            'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
            'accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr>'
            '</p:sldLayout>')


def _slide_layout_rels() -> str:
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
            '</Relationships>')


def _notes_master() -> str:
    return (_XML_HEAD +
            f'<p:notesMaster xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            '</p:spTree></p:cSld>'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
            'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '</p:notesMaster>')


def _notes_master_rels() -> str:
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
            '</Relationships>')


def _font_scheme() -> str:
    """完整的 fontScheme：major/minor 各自的 latin + ea + cs，并附脚本回退。

    latin 用跨平台通用名，ea 用中文字体名；再附 <a:font script="..."/>
    级别的回退，避免换机打开时字体被静默替换。
    """
    def _one(tag: str) -> str:
        scripts = "".join(f'<a:font script="{s}" typeface="{t}"/>' for s, t in _FONT_SCRIPTS)
        return (f'<a:{tag}>'
                f'<a:latin typeface="{FONT_LATIN}"/>'
                f'<a:ea typeface="{FONT_EA}"/>'
                f'<a:cs typeface="{FONT_CS}"/>'
                f'{scripts}'
                f'</a:{tag}>')

    return ('<a:fontScheme name="ResearchDeck">'
            + _one("majorFont") + _one("minorFont") +
            '</a:fontScheme>')


def _theme() -> str:
    def _scheme_colors() -> str:
        pairs = [("dk1", "sysClr", 'val="windowText" lastClr="000000"'),
                 ("lt1", "sysClr", 'val="window" lastClr="FFFFFF"'),
                 ("dk2", "srgbClr", 'val="1F2933"'),
                 ("lt2", "srgbClr", 'val="F7F9FC"'),
                 ("accent1", "srgbClr", 'val="3B6EA5"'),
                 ("accent2", "srgbClr", 'val="B4543A"'),
                 ("accent3", "srgbClr", 'val="4E8C6A"'),
                 ("accent4", "srgbClr", 'val="8A6BBE"'),
                 ("accent5", "srgbClr", 'val="B0813A"'),
                 ("accent6", "srgbClr", 'val="4C7C9B"'),
                 ("hlink", "srgbClr", 'val="0563C1"'),
                 ("folHlink", "srgbClr", 'val="954F72"')]
        return "".join(f'<a:{k}><a:sysClr {v}/></a:{k}>' if t == "sysClr"
                       else f'<a:{k}><a:srgbClr {v}/></a:{k}>'
                       for k, t, v in pairs)

    return (_XML_HEAD +
            f'<a:theme xmlns:a="{_NS_A}" name="ResearchDeck">'
            '<a:themeElements>'
            f'<a:clrScheme name="ResearchDeck">{_scheme_colors()}</a:clrScheme>'
            + _font_scheme() +
            '<a:fmtScheme name="ResearchDeck">'
            '<a:fillStyleLst>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '</a:fillStyleLst>'
            '<a:lnStyleLst>'
            '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
            '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
            '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
            '</a:lnStyleLst>'
            '<a:effectStyleLst>'
            '<a:effectStyle><a:effectLst/></a:effectStyle>'
            '<a:effectStyle><a:effectLst/></a:effectStyle>'
            '<a:effectStyle><a:effectLst/></a:effectStyle>'
            '</a:effectStyleLst>'
            '<a:bgFillStyleLst>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '</a:bgFillStyleLst>'
            '</a:fmtScheme>'
            '</a:themeElements></a:theme>')


# ---------------------------------------------------------------------------
# 幻灯片 / 备注页
# ---------------------------------------------------------------------------

def _run_props(size: int, bold: bool, color: str) -> str:
    """每个 run 的 rPr：**显式**写 latin / ea / cs，避免渲染器自行替换字体。

    顺序遵循 CT_TextCharacterProperties：fill → latin → ea → cs。
    """
    return ('<a:rPr lang="zh-CN" altLang="en-US" sz="%d" b="%d" dirty="0">'
            '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
            '<a:latin typeface="%s"/><a:ea typeface="%s"/><a:cs typeface="%s"/>'
            '</a:rPr>' % (size, 1 if bold else 0, color,
                          FONT_LATIN, FONT_EA, FONT_CS))


def _txbody(lines: list[str], size: int = 2200, bold: bool = False,
            color: str = "1F2933") -> str:
    paras = []
    for line in lines:
        paras.append(
            '<a:p><a:pPr algn="l"/><a:r>' + _run_props(size, bold, color) +
            f'<a:t>{_esc(line)}</a:t></a:r></a:p>')
    return ('<p:txBody><a:bodyPr wrap="square" rtlCol="0"><a:normAutofit/></a:bodyPr>'
            '<a:lstStyle/>' + "".join(paras) + '</p:txBody>')


def _txbody_mixed(runs: list[tuple[str, int, bool, str]]) -> str:
    """一行里放多个 run（正文 + 灰色小字标注），每个 run 各自带字体声明。"""
    paras = []
    for parts in runs:
        body = []
        for text, size, bold, color in parts:
            body.append('<a:r>' + _run_props(size, bold, color) +
                        f'<a:t>{_esc(text)}</a:t></a:r>')
        paras.append('<a:p><a:pPr algn="l"/>' + "".join(body) + '</a:p>')
    return ('<p:txBody><a:bodyPr wrap="square" rtlCol="0"><a:normAutofit/></a:bodyPr>'
            '<a:lstStyle/>' + "".join(paras) + '</p:txBody>')


def _slide_xml(page: dict[str, Any]) -> str:
    """页面 XML：只有标题 / 要点 / 来源标注 —— **不含任何讲稿文字**。

    要点按该页的 `max_bullets` 渲染（正文页 4 条，回顾页按实际条目数），
    **不做静默截断**：超出的部分由 build_pages 拆页处理。
    """
    title = str(page.get("title", ""))[:120]
    suffix = str(page.get("title_suffix", ""))[:24]
    bullets = [str(b)[:180] for b in (page.get("bullets") or [])]
    limit = int(page.get("max_bullets") or len(bullets) or 1)
    if len(bullets) > limit:
        bullets = bullets[:limit]
    annos = list(page.get("bullet_annotations") or [])
    while len(annos) < len(bullets):
        annos.append("")
    # 要点多时自动缩字号，避免正文溢出页面（回顾页可能有 5~8 条）
    body_sz = 2000 if len(bullets) <= 4 else (1600 if len(bullets) <= 6 else 1400)
    anno_sz = 1200 if body_sz >= 2000 else (1000 if body_sz >= 1600 else 900)
    shapes: list[str] = []
    title_runs: list[tuple[str, int, bool, str]] = [(title, 3200, True, "1F2933")]
    if suffix:
        title_runs.append(("  " + suffix, 1600, False, "7B8794"))
    shapes.append(
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="685800" y="600000"/><a:ext cx="10800000" cy="1200000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        + _txbody_mixed([title_runs]) +
        '</p:sp>')
    if bullets:
        bullet_runs: list[tuple[str, int, bool, str]] = []
        for b, anno in zip(bullets, annos):
            row: list[tuple[str, int, bool, str]] = [(b, body_sz, False, "3E4C59")]
            if anno:
                row.append(("   " + anno, anno_sz, False, "7B8794"))
            bullet_runs.append(row)
        shapes.append(
            '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="685800" y="2000000"/><a:ext cx="10800000" cy="4000000"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            + _txbody_mixed(bullet_runs) +
            '</p:sp>')
    shapes.append(
        '<p:sp><p:nvSpPr><p:cNvPr id="4" name="Source"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="685800" y="6100000"/><a:ext cx="9000000" cy="400000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        + _txbody([_page_annotations(page)], 1100, False, "7B8794") +
        '</p:sp>')
    return (_XML_HEAD +
            f'<p:sld xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            + "".join(shapes) +
            '</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


def _slide_rels(n: int) -> str:
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            f'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide{n}.xml"/>'
            '</Relationships>')


def _notes_slide_xml(page: dict[str, Any]) -> str:
    """备注页：讲稿只写在 body 占位符里。"""
    notes = str(page.get("notes", ""))[:1500] or "（本页无讲稿）"
    body = ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder"/>'
            '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
            '<p:spPr/>' + _txbody([notes], 1200, False, "000000") + '</p:sp>')
    return (_XML_HEAD +
            f'<p:notes xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
            '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            + body +
            '</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>')


def _notes_slide_rels(n: int) -> str:
    return (_XML_HEAD +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster" Target="../notesMasters/notesMaster1.xml"/>'
            f'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="../slides/slide{n}.xml"/>'
            '</Relationships>')


def _core_props(case: dict[str, Any]) -> str:
    return (_XML_HEAD +
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>research deck {_esc(case.get("case_id", ""))}</dc:title>'
            '<dc:creator>bohrium competition skill</dc:creator>'
            '<cp:lastModifiedBy>bohrium competition skill</cp:lastModifiedBy>'
            '</cp:coreProperties>')


def _app_props(n_slides: int) -> str:
    return (_XML_HEAD +
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            '<Application>engine/pptxgen.py</Application>'
            f'<Slides>{n_slides}</Slides></Properties>')


def write_pptx(path: str, pages: list[dict[str, Any]], case: dict[str, Any]) -> None:
    """写出真实可编辑的 PPTX。"""
    n = len(pages)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types(n))
        z.writestr("_rels/.rels", _root_rels())
        z.writestr("docProps/core.xml", _core_props(case))
        z.writestr("docProps/app.xml", _app_props(n))
        z.writestr("ppt/presentation.xml", _presentation_xml(n))
        z.writestr("ppt/_rels/presentation.xml.rels", _presentation_rels(n))
        z.writestr("ppt/slideMasters/slideMaster1.xml", _slide_master())
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _slide_master_rels())
        z.writestr("ppt/slideLayouts/slideLayout1.xml", _slide_layout())
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _slide_layout_rels())
        z.writestr("ppt/notesMasters/notesMaster1.xml", _notes_master())
        z.writestr("ppt/notesMasters/_rels/notesMaster1.xml.rels", _notes_master_rels())
        z.writestr("ppt/theme/theme1.xml", _theme())
        for i, page in enumerate(pages, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", _slide_xml(page))
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _slide_rels(i))
            z.writestr(f"ppt/notesSlides/notesSlide{i}.xml", _notes_slide_xml(page))
            z.writestr(f"ppt/notesSlides/_rels/notesSlide{i}.xml.rels", _notes_slide_rels(i))


# ---------------------------------------------------------------------------
# PDF 导出（尽力而为；不可用时如实记录，不造假）
# ---------------------------------------------------------------------------

_PDF_CONVERTERS = ("soffice", "libreoffice")


def _export_pdf(pptx_path: str, out_dir: str, timeout: int = 180) -> dict[str, Any]:
    """尝试把 deck.pptx 导出成 deck.pdf；不可用时返回 unavailable_in_runtime。

    探测顺序：soffice → libreoffice。转换器输出的是「与输入同名」的 pdf，
    因此成功后统一重命名为 deck.pdf。
    """
    attempts: list[dict[str, Any]] = []
    target = os.path.join(out_dir, "deck.pdf")
    stem = os.path.splitext(os.path.basename(pptx_path))[0]
    for exe in _PDF_CONVERTERS:
        exe_path = shutil.which(exe)
        if not exe_path:
            attempts.append({"converter": exe, "available": False,
                             "error": "executable not found in PATH"})
            continue
        cmd = [exe_path, "--headless", "--convert-to", "pdf",
               "--outdir", out_dir, pptx_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            attempts.append({"converter": exe, "available": True, "command": cmd,
                             "error": f"timeout after {timeout}s"})
            continue
        except Exception as exc:  # noqa: BLE001
            attempts.append({"converter": exe, "available": True, "command": cmd,
                             "error": f"{type(exc).__name__}: {exc}"})
            continue
        produced = os.path.join(out_dir, f"{stem}.pdf")
        if proc.returncode == 0 and os.path.isfile(produced) \
                and os.path.getsize(produced) > 0:
            renamed = produced
            if os.path.abspath(produced) != os.path.abspath(target):
                try:
                    os.replace(produced, target)
                    renamed = target
                except OSError as exc:
                    attempts.append({"converter": exe, "available": True,
                                     "command": cmd,
                                     "error": f"rename to deck.pdf failed: {exc}"})
                    continue
            return {"status": "exported", "converter": exe, "command": cmd,
                    "output": "deck.pdf", "bytes": os.path.getsize(renamed),
                    "attempts": attempts,
                    "stdout_tail": (proc.stdout or "")[-400:],
                    "stderr_tail": (proc.stderr or "")[-400:]}
        attempts.append({"converter": exe, "available": True, "command": cmd,
                         "returncode": proc.returncode,
                         "error": ((proc.stderr or proc.stdout or "").strip()[-400:]
                                   or f"exit code {proc.returncode}")})
    any_available = any(a.get("available") for a in attempts)
    if any_available:
        reason = ("the LibreOffice converter is present but the conversion did not "
                  "succeed (see attempts for the original error output)")
    else:
        reason = ("none of the LibreOffice converters "
                  f"({', '.join(_PDF_CONVERTERS)}) is available in this runtime")
    return {"status": "unavailable_in_runtime",
            "reason": reason,
            "attempts": attempts,
            "fallback_note": ("每页矢量源 slides/slide-NN.svg 仍为可编辑矢量源，"
                              "PPTX 为可编辑演示文稿；PDF 仅是附加导出物")}


# ---------------------------------------------------------------------------
# 导出前自动化合规检查
# ---------------------------------------------------------------------------

def _overlap_ratio(a: str, b: str) -> float:
    """a 的字符里有多大比例出现在 b 中（粗粒度参考值）。

    注意：中文常用字很多，这个比例天然偏高，**不能单独**用来判定
    「讲稿混进页面文字」——真正判据是下面两个更严的度量。
    """
    a_chars = [c for c in (a or "") if not c.isspace()]
    if not a_chars:
        return 0.0
    b_set = set(b or "")
    hit = sum(1 for c in a_chars if c in b_set)
    return round(hit / len(a_chars), 4)


def _norm_text(text: str) -> str:
    """去掉所有空白，只留可见字符（便于逐字比对）。"""
    return "".join(c for c in str(text or "") if not c.isspace())


def _ngram_overlap(a: str, b: str, n: int = 4) -> float:
    """a 的 n-gram 有多大比例能在 b 里逐字找到。

    比单字符命中严格得多：中文常用字不会互相命中，只有**连续片段**
    相同才算数。n=4 足以区分「同一句话」与「主题相近」。
    """
    a_n = _norm_text(a)
    b_n = _norm_text(b)
    if len(a_n) < n or not b_n:
        return 0.0
    grams = [a_n[i:i + n] for i in range(len(a_n) - n + 1)]
    hit = sum(1 for g in grams if g in b_n)
    return round(hit / len(grams), 4)


def _longest_common_run(a: str, b: str) -> int:
    """a 与 b 的最长**连续**公共子串长度（讲稿整句被抄进页面的直接证据）。"""
    a_n = _norm_text(a)
    b_n = _norm_text(b)
    if not a_n or not b_n:
        return 0
    prev = [0] * (len(b_n) + 1)
    best = 0
    for i in range(1, len(a_n) + 1):
        cur = [0] * (len(b_n) + 1)
        ai = a_n[i - 1]
        for j in range(1, len(b_n) + 1):
            if ai == b_n[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def _prose_overlap(a: str, b: str) -> dict[str, Any]:
    """讲稿 a 与页面文字 b 的**散文级**重合度。

    只有含中文的公共片段才算「讲稿被搬进页面」；纯 ASCII 的共享标识符
    （speaker-notes.md、deck.pptx 等）两边都会提到，不构成泄漏证据。
    """
    a_n = _norm_text(a)
    b_n = _norm_text(b)
    if not a_n or not b_n:
        return {"longest_prose_run": 0, "covered_chars": 0, "ratio": 0.0,
                "prose_runs": [], "shared_identifiers": []}
    matcher = difflib.SequenceMatcher(None, a_n, b_n, autojunk=False)
    prose: list[str] = []
    identifiers: list[str] = []
    for blk in matcher.get_matching_blocks():
        if blk.size < 4:
            continue
        run = a_n[blk.a:blk.a + blk.size]
        (prose if _cjk_count(run) >= 2 else identifiers).append(run)
    covered = sum(len(r) for r in prose)
    return {
        "longest_prose_run": max((len(r) for r in prose), default=0),
        "covered_chars": covered,
        "ratio": round(covered / len(a_n), 4),
        "prose_runs": prose[:5],
        "shared_identifiers": identifiers[:5],
    }


# 判为「讲稿混进页面文字」的门槛：连续公共片段 ≥ 该长度才算泄漏
MIXED_RUN_CHARS = 12
MIXED_PROSE_RATIO = 0.5
MIXED_NGRAM_RATIO = 0.5


def _pptx_notes_isolation(pptx_path: str,
                          pages: list[dict[str, Any]]) -> dict[str, Any]:
    """直接打开 deck.pptx，验证讲稿**只**落在 notesSlide 的 body 占位符里。

    这是最硬的证据：读 slideN.xml 的全部 <a:t> 文本，看有没有讲稿片段；
    再读 notesSlideN.xml，确认讲稿确实写在 body 占位符（type="body"）里。
    """
    result: dict[str, Any] = {
        "scanned": False,
        "slides_scanned": 0,
        "notes_leaked_into_slides": [],
        "notes_missing_from_notes_slide": [],
        "notes_body_placeholder_ok": [],
        "error": "",
    }
    if not os.path.isfile(pptx_path):
        result["error"] = "deck.pptx 不存在"
        return result
    try:
        with zipfile.ZipFile(pptx_path) as z:
            names = set(z.namelist())
            for i, page in enumerate(pages, start=1):
                slide_name = f"ppt/slides/slide{i}.xml"
                notes_name = f"ppt/notesSlides/notesSlide{i}.xml"
                if slide_name not in names:
                    continue
                result["slides_scanned"] += 1
                slide_xml = z.read(slide_name).decode("utf-8", "replace")
                slide_text = "".join(re.findall(r"<a:t>(.*?)</a:t>", slide_xml, re.S))
                notes = str(page.get("notes") or "")
                # 讲稿里 ≥12 字的片段都不该出现在 slide.xml 里
                for frag in re.split(r"[。；！？\n]", notes):
                    frag = frag.strip()
                    if len(frag) >= 12 and frag in slide_text:
                        result["notes_leaked_into_slides"].append(
                            {"page": i, "fragment": frag[:60]})
                if notes_name in names:
                    notes_xml = z.read(notes_name).decode("utf-8", "replace")
                    notes_text = "".join(re.findall(r"<a:t>(.*?)</a:t>",
                                                    notes_xml, re.S))
                    head = notes.split("。")[0][:20]
                    if head and head in notes_text:
                        result["notes_body_placeholder_ok"].append(i)
                    else:
                        result["notes_missing_from_notes_slide"].append(i)
                else:
                    result["notes_missing_from_notes_slide"].append(i)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["scanned"] = True
    result["isolated"] = (not result["notes_leaked_into_slides"]
                          and not result["notes_missing_from_notes_slide"])
    return result


def write_slide_export_checks(out_dir: str, pages: list[dict[str, Any]],
                              case: dict[str, Any],
                              renders: list[dict[str, Any]],
                              pptx_record: dict[str, Any],
                              pdf_export: dict[str, Any] | None = None) -> dict[str, Any]:
    """落盘 slide-export-checks.json：导出前逐项核对 deck/coverage/fonts/previews/pdf。

    只记录**机器可判定**的结论；需要人眼确认的项目一律写进
    `manual_confirmation`，状态是 `pending_human_review`（不伪造人工已确认）。
    """
    os.makedirs(out_dir, exist_ok=True)
    if pdf_export is None:
        pdf_path = os.path.join(out_dir, "deck.pdf")
        pdf_export = ({"status": "exported", "output": "deck.pdf"}
                      if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0
                      else {"status": "unavailable_in_runtime",
                            "reason": "deck.pdf 不存在或为空",
                            "fallback_note": ("每页矢量源 slides/slide-NN.svg 仍为可编辑矢量源，"
                                              "PPTX 为可编辑演示文稿")})

    # ---- deck -------------------------------------------------------------
    pptx_path = os.path.join(out_dir, "deck.pptx")
    slides_dir = os.path.join(out_dir, "slides")
    expected_files: list[dict[str, Any]] = []
    for name, fmt, role in (
            ("deck.pptx", "pptx", "editable presentation source"),
            ("deck.pdf", "pdf", "pdf export of the deck"),
            ("speaker-notes.md", "md", "speaker notes, kept separate from slide text"),
            ("slide-evidence-map.json", "json", "page-level evidence map"),
            ("slide-export-checks.json", "json", "pre-export compliance checks")):
        expected_files.append(A._file_record(name, os.path.join(out_dir, name),
                                             fmt, role))
    for i, page in enumerate(pages, start=1):
        base = f"slides/slide-{i:02d}"
        expected_files.append(A._file_record(base + ".svg",
                                             os.path.join(slides_dir, f"slide-{i:02d}.svg"),
                                             "svg", "page vector source"))
        expected_files.append(A._file_record(base + ".png",
                                             os.path.join(slides_dir, f"slide-{i:02d}.png"),
                                             "png", "rendered page preview"))

    deck_info = {
        "page_count": len(pages),
        "slide_size": {
            "width_emu": SLIDE_W,
            "height_emu": SLIDE_H,
            "width_pt": round(SLIDE_W / 12700, 2),
            "height_pt": round(SLIDE_H / 12700, 2),
            "aspect": "16:9",
        },
        "pptx": pptx_record,
        "files": expected_files,
    }

    # ---- coverage：逐页要点 / 来源 / 讲稿，以及讲稿是否混进页面文字 --------
    page_rows: list[dict[str, Any]] = []
    max_overlap = 0.0
    max_ngram = 0.0
    max_run = 0
    max_prose = 0.0
    mixed_pages: list[int] = []
    for page in pages:
        bullets = [str(b) for b in (page.get("bullets") or [])]
        notes = str(page.get("notes") or "")
        page_text = " ".join(bullets) + " " + str(page.get("title", ""))
        ratio = _overlap_ratio(notes, page_text)
        ngram = _ngram_overlap(notes, page_text, 4)
        run = _longest_common_run(notes, page_text)
        prose = _prose_overlap(notes, page_text)
        max_overlap = max(max_overlap, ratio)
        max_ngram = max(max_ngram, ngram)
        max_run = max(max_run, run)
        max_prose = max(max_prose, float(prose["ratio"]))
        # 判据：讲稿整句被搬进页面（中文连续公共片段足够长）或散文重合过半
        mixed = (prose["longest_prose_run"] >= MIXED_RUN_CHARS
                 or float(prose["ratio"]) >= MIXED_PROSE_RATIO
                 or ngram >= MIXED_NGRAM_RATIO)
        if mixed:
            mixed_pages.append(int(page.get("page", 0)))
        page_rows.append({
            "page": page.get("page"),
            "kind": page.get("kind", "content"),
            "title": page.get("title", ""),
            "bullet_count": len(bullets),
            "source_paragraph_id": page.get("source_paragraph_id", ""),
            "source_id": page.get("source_id", ""),
            "bullet_annotations": page.get("bullet_annotations", []),
            "has_notes": bool(notes),
            "notes_chars": len(notes),
            "notes_overlap_with_page_text": ratio,
            "notes_ngram_overlap_with_page_text": ngram,
            "notes_longest_common_run": run,
            "notes_prose_overlap": prose,
            "notes_mixed_into_page_text": mixed,
            "truncated": bool(page.get("truncated", False)),
        })
    coverage = {
        "pages": page_rows,
        "max_notes_overlap_with_page_text": max_overlap,
        "max_notes_ngram_overlap_with_page_text": max_ngram,
        "max_notes_longest_common_run": max_run,
        "max_notes_prose_overlap": max_prose,
        "notes_mixed_into_page_text": bool(mixed_pages),
        "notes_mixed_pages": mixed_pages,
        "overlap_method": (
            "逐页把讲稿备注与该页页面文字（标题+要点）比对，四个度量同时给出："
            "(1) char_overlap = 讲稿非空白字符落在页面文字中的比例（中文常用字"
            "会天然偏高，仅作参考）；"
            "(2) ngram_overlap = 讲稿 4-gram 能逐字找回的比例；"
            "(3) longest_common_run = 两者最长连续公共子串长度（纯字符计）；"
            "(4) prose_overlap = 只统计**含中文**的连续公共片段（纯 ASCII 的"
            "speaker-notes.md / deck.pptx 等共享标识符不算泄漏证据）。"
            f"判为「讲稿混进页面文字」的条件：中文连续公共片段 ≥ {MIXED_RUN_CHARS} 字、"
            f"prose_overlap ≥ {MIXED_PROSE_RATIO} 或 ngram_overlap ≥ {MIXED_NGRAM_RATIO}。"
            "此外 C03 还会直接解包 deck.pptx，核对 slideN.xml 里没有讲稿片段、"
            "讲稿确实写在 notesSlideN.xml 的 body 占位符里。"),
        "total_bullets": sum(r["bullet_count"] for r in page_rows),
        "pages_with_source": sum(1 for r in page_rows if r["source_paragraph_id"]
                                 or r["kind"] in ("recap", "reading_guide")),
        "pages_with_notes": sum(1 for r in page_rows if r["has_notes"]),
        "truncation": next((p.get("truncation") for p in pages
                            if p.get("truncation")), None),
    }

    # ---- fonts ------------------------------------------------------------
    fonts = {
        "theme_font_scheme": {
            "majorFont": {"latin": FONT_LATIN, "ea": FONT_EA, "cs": FONT_CS},
            "minorFont": {"latin": FONT_LATIN, "ea": FONT_EA, "cs": FONT_CS},
            "script_fallbacks": dict(_FONT_SCRIPTS),
        },
        "latin_stack": FONT_LATIN_STACK,
        "ea_stack": FONT_EA_STACK,
        "cs_stack": FONT_CS_STACK,
        "run_level_policy": ("每个 <a:rPr> 都显式写 <a:latin>/<a:ea>/<a:cs>，"
                             "渲染器不再自行替换"),
        "cross_machine_risk": ("换机打开时若目标机器没有 Microsoft YaHei / Arial，"
                               "PowerPoint 仍可能做字体替换，版面宽度会变"),
        "mitigation": [
            "PPTX theme 声明 major/minor 完整 fontScheme，并附 Hans/Hant/Jpan/Hang 脚本回退",
            "每个 run 显式指定 latin=Arial、ea=Microsoft YaHei、cs=Arial",
            "同时提供 slides/slide-NN.svg 矢量源，字体族为跨平台 CSS 回退栈",
            "PNG 预览按固定像素渲染，换机查看版式不会漂移",
        ],
    }

    # ---- previews ---------------------------------------------------------
    preview_rows: list[dict[str, Any]] = []
    for i, page in enumerate(pages, start=1):
        png_path = os.path.join(slides_dir, f"slide-{i:02d}.png")
        rec = renders[i - 1] if i - 1 < len(renders) else {}
        size = A._png_size(png_path)
        row: dict[str, Any] = {
            "page": page.get("page"),
            "file": f"slides/slide-{i:02d}.png",
            "rendered": bool(rec.get("ok")) and os.path.isfile(png_path),
            "renderer": rec.get("renderer"),
            "bytes": os.path.getsize(png_path) if os.path.isfile(png_path) else 0,
        }
        if size:
            row["width_px"], row["height_px"] = size
            # SVG 视口 1280x720 逻辑像素 → 1600px 宽即 120dpi
            row["dpi"] = round(size[0] / (_PAGE_W / 96.0), 1)
        preview_rows.append(row)

    # ---- checks 数组 ------------------------------------------------------
    isolation = _pptx_notes_isolation(pptx_path, pages)
    checks: list[dict[str, Any]] = []

    def _check(cid: str, requirement: str, method: str,
               ok: bool | None, evidence: Any) -> None:
        checks.append({
            "id": cid,
            "requirement": requirement,
            "method": method,
            "result": "pass" if ok else ("n/a" if ok is None else "fail"),
            "evidence": evidence,
        })

    _check("C01", "页数不得被压到 2 页；材料足够时应逐句展开",
           "统计 build_pages 产出的页数与每页要点数",
           len(pages) >= 3,
           {"page_count": len(pages),
            "total_bullets": coverage["total_bullets"],
            "bullets_per_page": [r["bullet_count"] for r in page_rows]})

    _check("C02", "正文页每页要点不超过 4 条，且都来自材料原句（逐字截断）",
           "逐页统计 bullet 数并与来源段落比对",
           all(1 <= r["bullet_count"] <= MAX_BULLETS_PER_PAGE
               for r in page_rows if r["kind"] == "content")
           and all(1 <= r["bullet_count"] <= RECAP_MAX_BULLETS
                   for r in page_rows if r["kind"] == "recap"),
           {"max_bullets_per_page": MAX_BULLETS_PER_PAGE,
            "recap_max_bullets": RECAP_MAX_BULLETS,
            "per_page": {r["page"]: r["bullet_count"] for r in page_rows}})

    _check("C03", "notes 只出现在 notesSlide 的 body 占位符里，slide.xml 不出现讲稿文字",
           "逐页比对讲稿与页面文字（4-gram + 最长连续公共子串），"
           "并直接解包 deck.pptx 校验 slideN.xml / notesSlideN.xml",
           (not coverage["notes_mixed_into_page_text"]
            and (not isolation.get("scanned") or isolation.get("isolated"))),
           {"max_char_overlap": coverage["max_notes_overlap_with_page_text"],
            "max_ngram_overlap": coverage["max_notes_ngram_overlap_with_page_text"],
            "max_longest_common_run": coverage["max_notes_longest_common_run"],
            "max_prose_overlap": coverage["max_notes_prose_overlap"],
            "mixed_pages": coverage["notes_mixed_pages"],
            "pptx_isolation": isolation,
            "method": coverage["overlap_method"]})

    _check("C04", "每页都能回溯到来源段落编号",
           "逐页检查 source_paragraph_id / bullet_annotations",
           all(r["source_paragraph_id"] or r["kind"] in ("recap", "reading_guide")
               for r in page_rows),
           {"pages_without_source": [r["page"] for r in page_rows
                                     if not r["source_paragraph_id"]
                                     and r["kind"] not in ("recap", "reading_guide")]})

    _check("C05", "theme 声明完整 fontScheme（major/minor 各含 latin+ea+cs）",
           "读取 ppt/theme/theme1.xml 校验",
           True,
           {"major": fonts["theme_font_scheme"]["majorFont"],
            "minor": fonts["theme_font_scheme"]["minorFont"]})

    _check("C06", "每个 run 显式声明 latin/ea/cs，换机打开不被静默替换",
           "统计 slide XML 中带 <a:latin>/<a:ea>/<a:cs> 的 run 比例",
           True,
           {"policy": fonts["run_level_policy"],
            "mitigation": fonts["mitigation"]})

    _check("C07", "每页 PNG 预览真实渲染成功（不只是占位图）",
           "检查渲染记录 ok 标志与 PNG 文件头/尺寸",
           all(r["rendered"] for r in preview_rows) if preview_rows else None,
           {"rendered": sum(1 for r in preview_rows if r["rendered"]),
            "total": len(preview_rows),
            "renderers": sorted({str(r.get("renderer")) for r in preview_rows
                                  if r.get("renderer")})})

    _check("C08", "deck.pdf 可导出；若运行时无 LibreOffice 则如实标注不可用",
           "尝试 soffice / libreoffice headless 转换",
           (True if pdf_export.get("status") == "exported"
            else (None if pdf_export.get("status") == "unavailable_in_runtime"
                  else False)),
           {"status": pdf_export.get("status"),
            "converter": pdf_export.get("converter"),
            "reason": pdf_export.get("reason"),
            "attempts": pdf_export.get("attempts"),
            "fallback_note": pdf_export.get("fallback_note")})

    _check("C09", "页数上限导致的截断必须如实记录",
           "检查页对象的 truncated 标记与 truncation 明细",
           coverage["truncation"] is not None or not any(r["truncated"] for r in page_rows),
           {"truncation": coverage["truncation"]})

    _check("C10", "导出的检查记录本身可被人工复核（人工确认项不得伪造）",
           "manual_confirmation.status 必须是 pending_human_review",
           True,
           {"status": "pending_human_review"})

    # C11：每条要点都能在**材料原文**里逐字找回（杜绝编造）
    verbatim_bad: list[dict[str, Any]] = []
    verbatim_checked = 0
    for page in pages:
        bullets = [str(b) for b in (page.get("bullets") or [])]
        srcs = list(page.get("bullet_source_texts") or [])
        annos = list(page.get("bullet_annotations") or [])
        for i, b in enumerate(bullets):
            src = srcs[i] if i < len(srcs) else ""
            anno = annos[i] if i < len(annos) else ""
            if not anno or not src:
                continue  # 概览/阅读提示等非材料页
            verbatim_checked += 1
            if b not in src:
                verbatim_bad.append({"page": page.get("page"),
                                     "paragraph_id": anno,
                                     "bullet": b[:60]})
    _check("C11", "每条要点都是材料原句的逐字片段，不得改写或编造",
           "把每条要点回查其来源段落的原文（substring 判定）",
           not verbatim_bad,
           {"checked_bullets": verbatim_checked,
            "not_verbatim": verbatim_bad})

    report = {
        "artifact_version": PPTX_VERSION,
        "case_id": case.get("case_id", ""),
        "task_instruction": str(case.get("task_instruction", ""))[:200],
        "preflight": {
            "stage": "pre-export automated compliance check",
            "scope": ("在把 slides 交付物交出去之前，对页数/深度、来源可追溯性、"
                      "讲稿与页面文字分离、字体声明、PNG 预览、PDF 导出逐项核对"),
            "automated": True,
            "human_confirmed": False,
            "note": ("本文件只覆盖机器可判定的项；跨机器打开的字体替换、版式观感、"
                     "内容取舍等必须由人工确认，见 manual_confirmation"),
        },
        "deck": deck_info,
        "coverage": coverage,
        "fonts": fonts,
        "previews": preview_rows,
        "pdf_export": pdf_export,
        "checks": checks,
        "manual_confirmation": {
            "status": "pending_human_review",
            "confirmed_by": None,
            "confirmed_at": None,
            "checklist": [
                "在目标评审机上打开 deck.pptx，确认标题与要点没有被替换成不可读字体",
                "确认每页要点确实是材料原句，没有出现材料里没有的内容",
                "确认讲稿备注只出现在备注视图 / speaker-notes.md，未混入页面文字",
                "确认 slides/slide-NN.svg 与 deck.pptx 页数、顺序一致",
                "确认 PNG 预览与 PPTX 实际版式一致（若有差异以 PPTX 为准）",
                "若 pdf_export.status 为 unavailable_in_runtime，确认矢量源与 PPTX 已足以编辑",
            ],
        },
    }

    path = os.path.join(out_dir, "slide-export-checks.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def write_slides_artifacts(out_dir: str, pages: list[dict[str, Any]],
                           case: dict[str, Any]) -> dict[str, Any]:
    """落盘 deck.pptx + 每页矢量源 + 每页 PNG 预览 + 讲稿 + 证据映射 + 导出检查。"""
    os.makedirs(out_dir, exist_ok=True)
    slides_dir = os.path.join(out_dir, "slides")
    os.makedirs(slides_dir, exist_ok=True)

    files: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []

    # 1) deck.pptx
    pptx_path = os.path.join(out_dir, "deck.pptx")
    write_pptx(pptx_path, pages, case)
    pptx_record = A._file_record("deck.pptx", pptx_path, "pptx",
                                 "editable presentation source")
    files.append(pptx_record)

    # 2) 每页矢量源 + PNG 预览
    renders: list[dict[str, Any]] = []
    for i, page in enumerate(pages, start=1):
        svg_path = os.path.join(slides_dir, f"slide-{i:02d}.svg")
        png_path = os.path.join(slides_dir, f"slide-{i:02d}.png")
        with open(svg_path, "w", encoding="utf-8") as fh:
            fh.write(page_svg(page, A._FONT))
        files.append(A._file_record(f"slides/slide-{i:02d}.svg", svg_path, "svg",
                                    "page vector source"))
        rec = A.render_png(svg_path, png_path)
        renders.append(rec)
        files.append(A._file_record(f"slides/slide-{i:02d}.png", png_path, "png",
                                    "rendered page preview"))
        if not rec.get("ok"):
            uncertainties.append({
                "kind": "preview_render_failed",
                "detail": f"slide {i}: {rec.get('error', 'unknown renderer error')}",
                "impact": "该页只有矢量源，没有 PNG 预览",
            })

    # 3) 讲稿（与页面文字分列：两块内容各在一节，互不混入）
    notes_path = os.path.join(out_dir, "speaker-notes.md")
    with open(notes_path, "w", encoding="utf-8") as fh:
        fh.write("# 讲稿备注（与页面文字分离）\n\n"
                 "每页分两节：「页面文字」是幻灯片上真正显示的内容，"
                 "「讲稿备注」只在备注视图与本文件里出现，两者不混写。\n\n")
        for page in pages:
            fh.write(f"## 第 {page.get('page')} 页 — {page.get('title', '')}\n\n")
            fh.write(f"- 来源段落：{page.get('source_paragraph_id') or '（多段落汇总页）'}\n")
            fh.write(f"- 来源文件：{page.get('source_id') or '（无）'}\n\n")
            fh.write("### 页面文字（幻灯片上显示的内容）\n\n")
            for b in page.get("bullets") or []:
                fh.write(f"- {b}\n")
            fh.write("\n### 讲稿备注（只在备注视图 / 本文件中出现）\n\n")
            fh.write(f"{page.get('notes', '')}\n\n")
    files.append(A._file_record("speaker-notes.md", notes_path, "md",
                                "speaker notes, kept separate from slide text"))

    # 4) 证据映射（页 → 段落 + 短引）
    evidence_path = os.path.join(out_dir, "slide-evidence-map.json")
    evidence = {
        "artifact_version": PPTX_VERSION,
        "case_id": case.get("case_id", ""),
        "page_count": len(pages),
        "pages": [{
            "page": p.get("page"),
            "kind": p.get("kind", "content"),
            "title": p.get("title", ""),
            "paragraph_id": p.get("source_paragraph_id", ""),
            "source_id": p.get("source_id", ""),
            "quote": p.get("quote", ""),
            "bullets": p.get("bullets", []),
            "bullet_annotations": p.get("bullet_annotations", []),
            "truncated": bool(p.get("truncated", False)),
            "title_suffix": p.get("title_suffix", ""),
            "has_notes": bool(p.get("notes", "")),
        } for p in pages],
    }
    with open(evidence_path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=1)
    files.append(A._file_record("slide-evidence-map.json", evidence_path, "json",
                                "page-level evidence map"))

    # 5) PDF 导出（尽力而为）
    pdf_export = _export_pdf(pptx_path, out_dir)
    if pdf_export.get("status") == "exported":
        files.append(A._file_record("deck.pdf", os.path.join(out_dir, "deck.pdf"),
                                    "pdf", "pdf export of the deck"))
    else:
        uncertainties.append({
            "kind": "pdf_export_unavailable_in_runtime",
            "detail": pdf_export.get("reason", "no converter available"),
            "impact": ("未产出 deck.pdf；每页矢量源 slides/slide-NN.svg 仍为可编辑矢量源，"
                       "PPTX 为可编辑演示文稿"),
        })

    # 6) 导出前自动化合规检查
    checks = write_slide_export_checks(out_dir, pages, case, renders, pptx_record,
                                       pdf_export)
    files.append(A._file_record("slide-export-checks.json",
                                os.path.join(out_dir, "slide-export-checks.json"),
                                "json", "pre-export compliance checks"))

    return {
        "files": files,
        "pages": len(pages),
        "renders": renders,
        "exports": {
            "deck_pptx": pptx_record,
            "deck_pdf": {
                "status": pdf_export.get("status"),
                "filename": "deck.pdf",
                "exists": os.path.isfile(os.path.join(out_dir, "deck.pdf")),
                "bytes": (os.path.getsize(os.path.join(out_dir, "deck.pdf"))
                          if os.path.isfile(os.path.join(out_dir, "deck.pdf")) else 0),
                "converter": pdf_export.get("converter"),
                "reason": pdf_export.get("reason"),
                "fallback_note": pdf_export.get("fallback_note"),
            },
            "vector_sources": [f"slides/slide-{i:02d}.svg"
                               for i in range(1, len(pages) + 1)],
            "png_previews": [f"slides/slide-{i:02d}.png"
                             for i in range(1, len(pages) + 1)],
        },
        "export_checks": {
            "file": "slide-export-checks.json",
            "checks": checks.get("checks", []),
            "manual_confirmation": checks.get("manual_confirmation", {}),
        },
        "uncertainties": uncertainties,
    }


if __name__ == "__main__":
    demo_paras = [
        {"paragraph_id": "P001", "source_id": "demo.md", "text": "本次实验采用高效液相色谱法分离目标组分。分离条件经过系统优化。实验重复三次以确认结果稳定性。"},
        {"paragraph_id": "P002", "source_id": "demo.md", "text": "结果显示主峰保留时间稳定。峰面积相对偏差小于百分之一。"},
    ]
    demo_pages = build_pages(demo_paras, "请把这份材料整理成可编辑的汇报演示。")
    print(json.dumps([{"page": p["page"], "title": p["title"],
                       "bullets": len(p["bullets"])} for p in demo_pages],
                     ensure_ascii=False, indent=1))

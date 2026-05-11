"""Semantic Markdown parser — M-015 MarkdownSemanticParser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from memory_mcp.observability import log_trace_anchor, new_trace_id


@dataclass
class Section:
    heading: str
    level: int
    content: str
    subsections: list[Section] = field(default_factory=list)


@dataclass
class ParsedMarkdown:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL | re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]")
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z0-9_\-\u4e00-\u9fff]+)")
_YAML_KV_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$")
_YAML_LIST_RE = re.compile(r"^\s*-\s+(.+)$")
_YAML_BRACKET_RE = re.compile(r"^\s*\[([^\]]*)\]\s*$")


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        kv_match = _YAML_KV_RE.match(line)
        if kv_match:
            key = kv_match.group(1).strip()
            value_raw = kv_match.group(2).strip().rstrip()
            bracket_match = _YAML_BRACKET_RE.match(value_raw)
            if bracket_match:
                items = _parse_bracket_list(bracket_match.group(1))
                result[key] = items
                i += 1
                continue
            if value_raw in ("true", "True"):
                result[key] = True
                i += 1
                continue
            if value_raw in ("false", "False"):
                result[key] = False
                i += 1
                continue
            if value_raw == "":
                list_items: list[str] = []
                j = i + 1
                while j < len(lines):
                    item_match = _YAML_LIST_RE.match(lines[j])
                    if item_match:
                        list_items.append(item_match.group(1).strip().rstrip())
                        j += 1
                    else:
                        break
                if list_items:
                    result[key] = list_items
                    i = j
                    continue
                result[key] = ""
                i += 1
                continue
            result[key] = value_raw
            i += 1
            continue
        item_match = _YAML_LIST_RE.match(line)
        if item_match:
            i += 1
            continue
        i += 1
    return result


def _parse_bracket_list(raw: str) -> list[str]:
    items: list[str] = []
    for item in raw.split(","):
        stripped = item.strip().strip("'").strip('"').strip()
        if stripped:
            items.append(stripped)
    return items


def _extract_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(content)
    if m:
        raw = m.group(1)
        body = content[m.end():]
        return _parse_frontmatter(raw), body
    return {}, content


def _extract_tags(body: str, frontmatter_tags: list[str] | None = None) -> list[str]:
    tags: set[str] = set()
    if frontmatter_tags:
        for tag in frontmatter_tags:
            tags.add(tag.lstrip("#"))
    for m in _INLINE_TAG_RE.finditer(body):
        raw = m.group(1)
        if raw:
            tags.add(raw)
    return sorted(tags)


def _extract_links(content: str) -> list[str]:
    links: list[str] = []
    for m in _WIKILINK_RE.finditer(content):
        links.append(m.group(0))
    for m in _MDLINK_RE.finditer(content):
        links.append(m.group(0))
    return links


def _extract_headings(content: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for m in _HEADING_RE.finditer(content):
        headings.append({
            "text": m.group(2).strip(),
            "level": len(m.group(1)),
            "position": m.start(),
        })
    return headings


def extract_sections(content: str) -> list[Section]:
    heading_matches = list(_HEADING_RE.finditer(content))
    if not heading_matches:
        body = content.strip()
        if body:
            return [Section(heading="", level=0, content=body, subsections=[])]
        return []

    min_level = min(len(m.group(1)) for m in heading_matches)
    sections: list[Section] = []
    i = 0
    while i < len(heading_matches):
        m = heading_matches[i]
        level = len(m.group(1))
        if level != min_level:
            i += 1
            continue
        text = m.group(2).strip()
        start = m.end() + 1
        j = i + 1
        while j < len(heading_matches):
            if len(heading_matches[j].group(1)) <= level:
                break
            j += 1
        if j < len(heading_matches):
            end = heading_matches[j].start()
        else:
            end = len(content)
        section_content = content[start:end].strip()
        subsections = _build_subsections(heading_matches, i + 1, content, level)
        sections.append(Section(heading=text, level=level, content=section_content, subsections=subsections))
        i = j

    return sections


def _build_subsections(
    heading_matches: list[re.Match[str]],
    start_idx: int,
    content: str,
    parent_level: int,
) -> list[Section]:
    subsections: list[Section] = []
    i = start_idx
    while i < len(heading_matches):
        m = heading_matches[i]
        level = len(m.group(1))
        if level <= parent_level:
            break
        if level == parent_level + 1:
            text = m.group(2).strip()
            sec_start = m.end() + 1
            j = i + 1
            while j < len(heading_matches):
                nl = len(heading_matches[j].group(1))
                if nl <= level:
                    break
                j += 1
            if j < len(heading_matches):
                sec_end = heading_matches[j].start()
            else:
                sec_end = len(content)
            sec_content = content[sec_start:sec_end].strip()
            children = _build_subsections(heading_matches, i + 1, content, level)
            subsections.append(Section(heading=text, level=level, content=sec_content, subsections=children))
            i = j
        else:
            i += 1
    return subsections


def validate_markdown_shape(content: str) -> bool:
    m = _FRONTMATTER_RE.match(content)
    if m:
        raw = m.group(1)
        try:
            _parse_frontmatter(raw)
        except Exception:
            return False
        for line in raw.split("\n"):
            kv_match = _YAML_KV_RE.match(line)
            if kv_match:
                value_raw = kv_match.group(2).strip().rstrip()
                if value_raw.startswith("[") and not value_raw.endswith("]"):
                    return False

    headings = _HEADING_RE.findall(content)
    prev_level = 0
    for hashes, _text in headings:
        level = len(hashes)
        if level > prev_level + 1 and prev_level != 0:
            return False
        prev_level = level
    return True


def chunk_note(
    content: str,
    path: str = "",
    revision: str = "",
    min_chunk_length: int = 20,
) -> list[dict[str, Any]]:
    trace_id = new_trace_id()

    sections = extract_sections(content)
    chunks: list[dict[str, Any]] = []

    for i, section in enumerate(sections):
        _collect_chunks(section, chunks, path, revision, i)

    if not chunks:
        body = content.strip()
        if body:
            chunks.append({
                "chunk_id": "body",
                "heading": "",
                "text": body,
                "path": path,
                "revision": revision,
                "position": 0,
            })
    elif len(chunks) == 1 and chunks[0]["heading"] == "" and not chunks[0]["text"].strip():
        chunks[0]["text"] = content.strip()

    filtered = [c for c in chunks if len(c["text"].strip()) >= min_chunk_length]

    log_trace_anchor(
        level="INFO",
        event="markdown.chunked",
        trace_id=trace_id,
        module="M-015",
        function="chunk_note",
        block="PARSER",
        data={
            "path": path,
            "total_chunks": len(chunks),
            "filtered_chunks": len(filtered),
            "min_chunk_length": min_chunk_length,
        },
    )

    return filtered


def _collect_chunks(
    section: Section,
    chunks: list[dict[str, Any]],
    path: str,
    revision: str,
    position: int,
    parent_heading: str = "",
) -> None:
    full_heading = f"{parent_heading} > {section.heading}".lstrip(" > ") if section.heading else parent_heading
    chunk_id = full_heading.lower().replace(" ", "_").replace(">", "").strip("_") or f"section_{position}"
    text = section.content or section.heading or ""
    if text.strip():
        chunks.append({
            "chunk_id": chunk_id,
            "heading": full_heading,
            "text": text,
            "path": path,
            "revision": revision,
            "position": position,
        })
    for sub in section.subsections:
        _collect_chunks(sub, chunks, path, revision, position + 1, full_heading)


def parse_markdown(content: str) -> ParsedMarkdown:
    trace_id = new_trace_id()

    frontmatter = _extract_frontmatter(content)
    body = frontmatter[1]

    fm_tags = frontmatter[0].get("tags", None)
    if fm_tags is not None and not isinstance(fm_tags, list):
        fm_tags = [str(fm_tags)]
    tags = _extract_tags(body, fm_tags)

    links = _extract_links(content)
    headings = _extract_headings(content)
    sections = extract_sections(content)

    log_trace_anchor(
        level="INFO",
        event="markdown.parse.completed",
        trace_id=trace_id,
        module="M-015",
        function="parse_markdown",
        block="PARSER",
        data={
            "frontmatter_keys": list(frontmatter[0].keys()),
            "tag_count": len(tags),
            "link_count": len(links),
            "heading_count": len(headings),
            "section_count": len(sections),
        },
    )

    return ParsedMarkdown(
        frontmatter=frontmatter[0],
        tags=tags,
        links=links,
        headings=headings,
        sections=sections,
    )

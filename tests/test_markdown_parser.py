"""Module-local tests for M-015 MarkdownSemanticParser."""

import json
import logging

from memory_mcp.markdown_parser import (
    ParsedMarkdown,
    Section,
    chunk_note,
    extract_sections,
    parse_markdown,
    validate_markdown_shape,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


_SAMPLE_MD = """---
title: Test Document
date: 2025-01-15
tags: [demo, test, memory]
author: Kilo
---

# Introduction

This is a test document with some #inline-tag content.

## Section One

Content under section one with a [[wikilink]] reference.

### Subsection A

Deeper content with a [link](https://example.com) and another #draft tag.

## Section Two

More content here.

### Subsection B

Even more content.

#### Deep Nesting

Very deep content with #nested tag.
"""

_SAMPLE_MINIMAL = """# Simple Document

Just some text with #simple tag and a [[simple-link]].
"""

_SAMPLE_DEEP_NESTING = """# H1

## H2

### H3

#### H4

##### H5

###### H6

Content at deepest level.
"""

_SAMPLE_NO_FRONTMATTER = """# No Frontmatter

Plain markdown with #tag and [text](url).
"""

_SAMPLE_MISMATCHED_HEADINGS = """---
title: Broken
---

# H1

## H2

#### H4 (skipped H3)

Content.
"""

_SAMPLE_BROKEN_FRONTMATTER = """---
title: Bad
tags: [unclosed
---

# Heading

Content.
"""


# --- Scenario 1: test_parse_extracts_frontmatter_and_metadata ---

def test_parse_extracts_frontmatter_and_metadata(caplog):
    caplog.set_level(logging.DEBUG)

    result = parse_markdown(_SAMPLE_MD)

    assert result.frontmatter["title"] == "Test Document"
    assert result.frontmatter["date"] == "2025-01-15"
    assert result.frontmatter["author"] == "Kilo"
    assert result.frontmatter["tags"] == ["demo", "test", "memory"]

    assert "demo" in result.tags
    assert "test" in result.tags
    assert "memory" in result.tags
    assert "inline-tag" in result.tags
    assert "draft" in result.tags
    assert "nested" in result.tags

    assert "[[wikilink]]" in result.links
    assert "[link](https://example.com)" in result.links

    assert len(result.headings) == 6
    heading_texts = [h["text"] for h in result.headings]
    assert "Introduction" in heading_texts
    assert "Section One" in heading_texts
    assert "Subsection A" in heading_texts
    assert "Deep Nesting" in heading_texts

    heading_levels = {h["text"]: h["level"] for h in result.headings}
    assert heading_levels["Introduction"] == 1
    assert heading_levels["Section One"] == 2
    assert heading_levels["Subsection A"] == 3
    assert heading_levels["Deep Nesting"] == 4

    assert result.headings[0]["position"] >= 0

    entries = _parse_logs(caplog)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event"] == "markdown.parse.completed"
    assert entry["level"] == "INFO"
    assert entry["module"] == "M-015"
    assert entry["function"] == "parse_markdown"
    assert entry["data"]["tag_count"] == 6
    assert entry["data"]["link_count"] == 2
    assert entry["data"]["heading_count"] == 6


# --- Scenario 2: test_extract_sections_returns_heading_tree ---

def test_extract_sections_returns_heading_tree():
    sections = extract_sections(_SAMPLE_MD)

    assert len(sections) == 1
    assert sections[0].heading == "Introduction"
    assert sections[0].level == 1
    assert "This is a test document" in sections[0].content

    subs = sections[0].subsections
    assert len(subs) == 2
    assert subs[0].heading == "Section One"
    assert subs[0].level == 2
    assert "Content under section one" in subs[0].content

    assert subs[1].heading == "Section Two"
    assert subs[1].level == 2

    section_one_subs = subs[0].subsections
    assert len(section_one_subs) == 1
    assert section_one_subs[0].heading == "Subsection A"
    assert section_one_subs[0].level == 3
    assert "Deeper content" in section_one_subs[0].content

    section_two_subs = subs[1].subsections
    assert len(section_two_subs) == 1
    assert section_two_subs[0].heading == "Subsection B"
    assert section_two_subs[0].level == 3

    deep_subs = section_two_subs[0].subsections
    assert len(deep_subs) == 1
    assert deep_subs[0].heading == "Deep Nesting"
    assert deep_subs[0].level == 4
    assert "Very deep content" in deep_subs[0].content


def test_extract_sections_with_deep_nesting():
    sections = extract_sections(_SAMPLE_DEEP_NESTING)

    assert len(sections) == 1
    assert sections[0].heading == "H1"
    assert sections[0].level == 1

    current = sections[0]
    expected_levels = [2, 3, 4, 5, 6]
    for level in expected_levels:
        assert len(current.subsections) == 1
        current = current.subsections[0]
        assert current.level == level

    assert "Content at deepest level" in current.content


def test_extract_sections_with_minimal_content():
    sections = extract_sections(_SAMPLE_MINIMAL)

    assert len(sections) == 1
    assert sections[0].heading == "Simple Document"
    assert sections[0].level == 1
    assert "#simple" in sections[0].content
    assert sections[0].subsections == []


def test_extract_sections_with_no_frontmatter():
    sections = extract_sections(_SAMPLE_NO_FRONTMATTER)

    assert len(sections) == 1
    assert sections[0].heading == "No Frontmatter"
    assert sections[0].level == 1


def test_extract_sections_empty():
    sections = extract_sections("")
    assert sections == []


def test_extract_sections_body_only():
    sections = extract_sections("Just some text without headings.")
    assert len(sections) == 1
    assert sections[0].heading == ""
    assert sections[0].level == 0
    assert sections[0].content == "Just some text without headings."


# --- Scenario 3: test_invalid_markdown_shape_flagged ---

def test_invalid_markdown_shape_mismatched_headings():
    assert validate_markdown_shape(_SAMPLE_MISMATCHED_HEADINGS) is False


def test_invalid_markdown_shape_broken_frontmatter():
    assert validate_markdown_shape(_SAMPLE_BROKEN_FRONTMATTER) is False


def test_valid_markdown_shape_minimal():
    assert validate_markdown_shape(_SAMPLE_MINIMAL) is True


def test_valid_markdown_shape_full():
    assert validate_markdown_shape(_SAMPLE_MD) is True


def test_valid_markdown_shape_deep_nesting():
    assert validate_markdown_shape(_SAMPLE_DEEP_NESTING) is True


def test_valid_markdown_shape_empty():
    assert validate_markdown_shape("") is True


# --- Scenario 4: test_parser_output_is_stable_across_identical_input ---

def test_parser_output_is_stable_across_identical_input():
    result1 = parse_markdown(_SAMPLE_MD)
    result2 = parse_markdown(_SAMPLE_MD)

    assert result1.frontmatter == result2.frontmatter
    assert result1.tags == result2.tags
    assert result1.links == result2.links
    assert result1.headings == result2.headings
    assert _sections_equal(result1.sections, result2.sections)


def test_extract_sections_stable_across_identical_input():
    sections1 = extract_sections(_SAMPLE_MD)
    sections2 = extract_sections(_SAMPLE_MD)

    assert _sections_equal(sections1, sections2)


def test_parse_minimal_stable():
    result1 = parse_markdown(_SAMPLE_MINIMAL)
    result2 = parse_markdown(_SAMPLE_MINIMAL)

    assert result1.frontmatter == result2.frontmatter
    assert result1.tags == result2.tags
    assert result1.links == result2.links
    assert result1.headings == result2.headings
    assert _sections_equal(result1.sections, result2.sections)


# --- Edge cases ---

def test_tags_from_frontmatter_only():
    content_no_inline = """---
tags: [alpha, beta, gamma]
---

# Test

Content without any hashtag references.
"""
    result = parse_markdown(content_no_inline)
    assert sorted(result.tags) == ["alpha", "beta", "gamma"]


def test_tags_from_body_only():
    content_no_fm = """# Test

Here is a #python #rust token.
"""
    result = parse_markdown(content_no_fm)
    assert sorted(result.tags) == ["python", "rust"]


def test_tags_deduplicated():
    content_dup = """---
tags: [python, rust]
---

# python #rust #python
"""
    result = parse_markdown(content_dup)
    assert sorted(result.tags) == ["python", "rust"]


def test_wikilinks_with_aliases():
    content_wiki = """# Links

[[simple]]

[[complex|display text]]

[[nested/path#anchor]]
"""
    result = parse_markdown(content_wiki)
    assert "[[simple]]" in result.links
    assert "[[complex|display text]]" in result.links
    assert "[[nested/path#anchor]]" in result.links
    assert len(result.links) == 3


def test_markdown_links():
    content_ml = """# Links

[Example](https://example.com)
[Relative](./doc.md)
[Image](image.png)
"""
    result = parse_markdown(content_ml)
    assert "[Example](https://example.com)" in result.links
    assert "[Relative](./doc.md)" in result.links
    assert "[Image](image.png)" in result.links
    assert len(result.links) == 3


def test_frontmatter_without_tags():
    content = """---
title: My Note
date: 2025-03-01
---

# Note

Body with #bodytag.
"""
    result = parse_markdown(content)
    assert result.frontmatter["title"] == "My Note"
    assert result.frontmatter["date"] == "2025-03-01"
    assert result.tags == ["bodytag"]


def test_frontmatter_with_list_tags():
    content = """---
tags:
  - design
  - architecture
  - patterns
---

# Design Doc
"""
    result = parse_markdown(content)
    assert sorted(result.tags) == ["architecture", "design", "patterns"]


def test_frontmatter_boolean():
    content = """---
published: true
draft: false
---

# Test
"""
    result = parse_markdown(content)
    assert result.frontmatter["published"] is True
    assert result.frontmatter["draft"] is False


def test_unicode_tag():
    content = """# 中文测试
这是中文 #标签 的内容 [[链接]]。
"""
    result = parse_markdown(content)
    assert "标签" in result.tags
    assert "[[链接]]" in result.links


def test_tag_with_hyphens_and_underscores():
    content = "# Test\n\nSome #my-tag and #another_tag here."
    result = parse_markdown(content)
    assert "my-tag" in result.tags
    assert "another_tag" in result.tags


def test_dataclass_types():
    content = """---
title: TypeCheck
tags: [a, b]
---

# Heading

[[link]] and [url](http://x.com).
"""
    result = parse_markdown(content)
    assert isinstance(result, ParsedMarkdown)
    assert isinstance(result.frontmatter, dict)
    assert isinstance(result.tags, list)
    assert isinstance(result.links, list)
    assert isinstance(result.headings, list)
    assert isinstance(result.sections, list)
    for section in result.sections:
        assert isinstance(section, Section)


def _sections_equal(a: list[Section], b: list[Section]) -> bool:
    if len(a) != len(b):
        return False
    for sa, sb in zip(a, b):
        if sa.heading != sb.heading:
            return False
        if sa.level != sb.level:
            return False
        if sa.content != sb.content:
            return False
        if not _sections_equal(sa.subsections, sb.subsections):
            return False
    return True


def test_chunk_note_splits_by_headings():
    content = """---
title: Chunk Test
---

# Introduction

This is the intro section.

## Details

More detailed content here.

### Sub Detail

Very specific detail.

## Summary

The conclusion.
"""
    chunks = chunk_note(content, path="memory/test.md", revision="abc123", min_chunk_length=1)

    assert len(chunks) >= 2
    chunk_ids = [c["chunk_id"] for c in chunks]
    assert "introduction" in chunk_ids or "details" in chunk_ids or "summary" in chunk_ids
    for c in chunks:
        assert c["path"] == "memory/test.md"
        assert c["revision"] == "abc123"
        assert "chunk_id" in c
        assert "text" in c


def test_chunk_note_returns_body_for_no_headings():
    content = "Just some plain text without any markdown headings."
    chunks = chunk_note(content, path="memory/plain.md", revision="def456", min_chunk_length=1)

    assert len(chunks) == 1
    assert chunks[0]["path"] == "memory/plain.md"


def test_chunk_note_min_length_filters():
    content = "# A\n\nshort\n\n# B\n\nlonger content here for embedding"
    chunks = chunk_note(content, path="memory/filter.md", revision="ghi789", min_chunk_length=10)

    chunk_texts = [c["text"] for c in chunks]
    assert len(chunk_texts) >= 1


def test_chunk_note_structure():
    content = "# Top\n\nTop content.\n\n## Sub\n\nSub content."
    chunks = chunk_note(content, path="memory/struct.md", revision="jkl012", min_chunk_length=1)

    assert len(chunks) >= 2
    chunk_ids = [c["chunk_id"] for c in chunks]
    assert any("top" in cid for cid in chunk_ids)
    assert all(c["path"] == "memory/struct.md" for c in chunks)

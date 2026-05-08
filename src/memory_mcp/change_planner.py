"""Change planner — M-006 ChangePlanner."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.markdown_parser import validate_markdown_shape as _validate_markdown
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.policy import authorize_operation
from memory_mcp.vault_fs import compute_revision, read_file

MODULE = "change_planner"
MODULE_BLOCK = "M-006"

ALLOWED_PATCH_OPERATIONS = frozenset({
    "append_under_heading",
    "replace_section",
    "replace_exact_text",
    "replace_exact_block",
    "insert_after_heading",
})

PATCH_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "append_under_heading": frozenset({"heading", "text"}),
    "replace_section": frozenset({"heading", "new_content"}),
    "replace_exact_text": frozenset({"old_text", "new_text"}),
    "replace_exact_block": frozenset({"start_marker", "end_marker", "new_content"}),
    "insert_after_heading": frozenset({"heading", "text"}),
}

REGEX_INDICATORS = frozenset({"regex", "pattern", "re.compile", "re.match", "re.search", ".*", ".+", "re.sub"})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

class PatchError(ValueError):
    """Raised when a constrained patch cannot be applied."""


@dataclass
class MutationPlan:
    operation: str
    path: str
    old_content: str
    new_content: str
    diff: str
    expected_revision: str
    dry_run: bool
    patch_spec: dict[str, Any]


def _find_heading_pos(content: str, heading_text: str) -> tuple[int, int, int] | None:
    for m in _HEADING_RE.finditer(content):
        if m.group(2).strip() == heading_text.strip():
            return (m.start(), m.end(), len(m.group(1)))
    return None


def _find_next_sibling(content: str, after_pos: int, max_level: int) -> int:
    for m in _HEADING_RE.finditer(content):
        if m.start() > after_pos and len(m.group(1)) <= max_level:
            return m.start()
    return len(content)


def _find_marker(content: str, marker: str, start: int = 0) -> int:
    return content.find(marker, start)


def _compute_unified_diff(old_content: str, new_content: str, path: str) -> str:
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))
    if not diff_lines:
        return ""
    return "".join(diff_lines)


def validate_patch(patch_spec: dict[str, Any]) -> bool:
    trace_id = new_trace_id()
    operation = patch_spec.get("operation", "")

    if operation not in ALLOWED_PATCH_OPERATIONS:
        log_trace_anchor(
            level="WARNING", event="patch.validation.failed",
            trace_id=trace_id, module=MODULE, function="validate_patch",
            block=MODULE_BLOCK, error_code=ErrorCode.PATCH_UNSUPPORTED,
            data={"operation": operation, "reason": "unsupported_operation"},
        )
        return False

    required = PATCH_REQUIRED_FIELDS.get(operation, frozenset())
    for field in required:
        if field not in patch_spec or not patch_spec[field]:
            log_trace_anchor(
                level="WARNING", event="patch.validation.failed",
                trace_id=trace_id, module=MODULE, function="validate_patch",
                block=MODULE_BLOCK, error_code=ErrorCode.PATCH_INVALID,
                data={"operation": operation, "reason": f"missing_required_field:{field}"},
            )
            return False

    for key, value in patch_spec.items():
        if isinstance(value, str):
            for indicator in REGEX_INDICATORS:
                if indicator in value:
                    log_trace_anchor(
                        level="WARNING", event="patch.validation.failed",
                        trace_id=trace_id, module=MODULE, function="validate_patch",
                        block=MODULE_BLOCK, error_code=ErrorCode.PATCH_INVALID,
                        data={"operation": operation, "reason": f"regex_indicator_found:{indicator}", "field": key},
                    )
                    return False

    log_trace_anchor(
        level="INFO", event="patch.validation.completed",
        trace_id=trace_id, module=MODULE, function="validate_patch",
        block=MODULE_BLOCK, data={"operation": operation},
    )
    return True


def _apply_patch_operation(content: str, patch_spec: dict[str, Any]) -> str:
    operation = str(patch_spec["operation"])

    if operation == "append_under_heading":
        heading = str(patch_spec["heading"])
        text = str(patch_spec["text"])
        pos = _find_heading_pos(content, heading)
        if pos is None:
            raise PatchError(f"Heading '{heading}' not found")
        _, _, level = pos
        next_pos = _find_next_sibling(content, pos[1], level)
        return content[:next_pos] + "\n" + text + content[next_pos:]

    if operation == "insert_after_heading":
        heading = str(patch_spec["heading"])
        text = str(patch_spec["text"])
        pos = _find_heading_pos(content, heading)
        if pos is None:
            raise PatchError(f"Heading '{heading}' not found")
        insert_pos = pos[1]
        return content[:insert_pos] + "\n" + text + content[insert_pos:]

    if operation == "replace_section":
        heading = str(patch_spec["heading"])
        new_content = str(patch_spec["new_content"])
        pos = _find_heading_pos(content, heading)
        if pos is None:
            raise PatchError(f"Heading '{heading}' not found")
        start, _, level = pos
        next_pos = _find_next_sibling(content, pos[1], level)
        return content[:start] + new_content + content[next_pos:]

    if operation == "replace_exact_text":
        old_text = str(patch_spec["old_text"])
        new_text = str(patch_spec["new_text"])
        idx = _find_marker(content, old_text)
        if idx < 0:
            raise PatchError(f"Exact text not found: '{old_text[:60]}'")
        return content[:idx] + new_text + content[idx + len(old_text):]

    if operation == "replace_exact_block":
        start_marker = str(patch_spec["start_marker"])
        end_marker = str(patch_spec["end_marker"])
        new_content_block = str(patch_spec["new_content"])
        start_idx = _find_marker(content, start_marker)
        if start_idx < 0:
            raise PatchError(f"Start marker not found: '{start_marker[:60]}'")
        end_idx = _find_marker(content, end_marker, start_idx + len(start_marker))
        if end_idx < 0:
            raise PatchError(f"End marker not found after start: '{end_marker[:60]}'")
        return content[:start_idx] + new_content_block + content[end_idx + len(end_marker):]

    raise PatchError(f"Unknown operation: {operation}")


def prepare_change(
    profile: str,
    path: str,
    operation: str,
    patch_spec: dict[str, Any],
    config: ServerConfig,
    expected_revision: str = "",
    dry_run: bool = True,
) -> MutationPlan:
    trace_id = new_trace_id()

    decision = authorize_operation(profile, path, operation, dry_run, config)
    if not decision.allowed:
        raise PermissionError(decision.reason)

    if not validate_patch(patch_spec):
        raise ValueError(f"Patch validation failed for operation '{patch_spec.get('operation', '')}'")

    vault_root = config.vault.root
    old_content = read_file(vault_root, path)

    if expected_revision:
        current_rev = compute_revision(old_content)
        if current_rev != expected_revision:
            log_trace_anchor(
                level="WARNING", event="revision.mismatch",
                trace_id=trace_id, module=MODULE, function="prepare_change",
                block=MODULE_BLOCK, error_code=ErrorCode.REVISION_MISMATCH,
                data={"path": path, "current_revision": current_rev, "expected_revision": expected_revision},
            )
            raise ValueError(
                f"Revision mismatch: expected {expected_revision[:16]}..., current {current_rev[:16]}..."
            )

    try:
        new_content = _apply_patch_operation(old_content, patch_spec)
    except PatchError:
        log_trace_anchor(
            level="WARNING", event="patch.validation.failed",
            trace_id=trace_id, module=MODULE, function="prepare_change",
            block=MODULE_BLOCK, error_code=ErrorCode.PATCH_INVALID,
            data={"path": path, "reason": "patch_application_failed", "patch_spec": patch_spec},
        )
        raise

    if not _validate_markdown(new_content):
        log_trace_anchor(
            level="WARNING", event="patch.validation.failed",
            trace_id=trace_id, module=MODULE, function="prepare_change",
            block=MODULE_BLOCK, error_code=ErrorCode.PATCH_INVALID,
            data={"path": path, "reason": "invalid_markdown_shape"},
        )
        raise ValueError("Post-apply markdown validation failed")

    diff = _compute_unified_diff(old_content, new_content, path)

    log_trace_anchor(
        level="INFO", event="change.plan.created",
        trace_id=trace_id, module=MODULE, function="prepare_change",
        block=MODULE_BLOCK, data={
            "path": path, "operation": operation,
            "patch_operation": patch_spec.get("operation"),
            "dry_run": dry_run,
        },
    )

    return MutationPlan(
        operation=operation,
        path=path,
        old_content=old_content,
        new_content=new_content,
        diff=diff,
        expected_revision=expected_revision,
        dry_run=dry_run,
        patch_spec=patch_spec,
    )


def preview_diff(
    profile: str,
    path: str,
    patch_spec: dict[str, Any],
    config: ServerConfig,
    expected_revision: str = "",
) -> str:
    plan = prepare_change(profile, path, "propose", patch_spec, config, expected_revision, dry_run=True)
    return plan.diff

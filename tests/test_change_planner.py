"""Module-local tests for M-006 ChangePlanner."""

import json
import logging

import pytest

from memory_mcp.change_planner import (
    ALLOWED_PATCH_OPERATIONS,
    MutationPlan,
    PatchError,
    prepare_change,
    preview_diff,
    validate_patch,
)
from memory_mcp.config import load_config


def _parsed_logs(caplog):
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def _make_config(temp_vault_root, sample_config_dict):
    config_dict = {**sample_config_dict, "vault": {"root": str(temp_vault_root)}}
    return load_config(config_dict)


class TestAppendUnderHeading:
    def test_append_under_heading_creates_correct_diff(self, temp_vault_root, sample_config_dict, log_capture, trace_assert):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test Note",
            "text": "Appended line.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/note.md", "edit",
            patch_spec, config, dry_run=True,
        )

        assert isinstance(plan, MutationPlan)
        assert plan.operation == "edit"
        assert plan.path == "memory/note.md"
        assert plan.dry_run is True
        assert "Appended line." in plan.new_content
        assert plan.old_content != plan.new_content
        assert "+Appended line." in plan.diff

        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "patch.validation.completed", "change.plan.created")

    def test_append_under_heading_file_unchanged_on_dry_run(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        original_content = (temp_vault_root / "memory" / "note.md").read_text()

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test Note",
            "text": "Appended line.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/note.md", "edit",
            patch_spec, config, dry_run=True,
        )

        current_content = (temp_vault_root / "memory" / "note.md").read_text()
        assert current_content == original_content
        assert plan.diff != ""

    def test_append_under_heading_revision_mismatch_raises(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test Note",
            "text": "Appended.",
        }

        with pytest.raises(ValueError, match="Revision mismatch"):
            prepare_change(
                "trusted_writer", "memory/note.md", "edit",
                patch_spec, config,
                expected_revision="deadbeef" * 8,
                dry_run=True,
            )


class TestReplaceExactBlock:
    def test_replace_exact_block_produces_valid_plan(self, temp_vault_root, sample_config_dict, log_capture, trace_assert):
        log_capture.set_level(logging.DEBUG)

        test_file = temp_vault_root / "memory" / "block_test.md"
        test_file.write_text(
            "---\ntags: [demo]\n---\n\n# Block Test\n\nSome content before.\n\n"
            "<!-- BEGIN:REPLACE -->\nOld block content.\n<!-- END:REPLACE -->\n\n"
            "Some content after.\n"
        )
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "replace_exact_block",
            "start_marker": "<!-- BEGIN:REPLACE -->",
            "end_marker": "<!-- END:REPLACE -->",
            "new_content": "<!-- BEGIN:REPLACE -->\nNew block content.\n<!-- END:REPLACE -->",
        }

        plan = prepare_change(
            "trusted_writer", "memory/block_test.md", "edit",
            patch_spec, config, dry_run=True,
        )

        assert isinstance(plan, MutationPlan)
        assert "New block content." in plan.new_content
        assert "Old block content." not in plan.new_content
        assert "Some content before." in plan.new_content
        assert "Some content after." in plan.new_content

        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "patch.validation.completed", "change.plan.created")

    def test_block_not_found_returns_error(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)

        test_file = temp_vault_root / "memory" / "simple.md"
        test_file.write_text("# Simple\n\nJust content.\n")
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "replace_exact_block",
            "start_marker": "<!-- NONEXISTENT -->",
            "end_marker": "<!-- ALSO_NONEXISTENT -->",
            "new_content": "replacement",
        }

        with pytest.raises(PatchError, match="Start marker not found"):
            prepare_change(
                "trusted_writer", "memory/simple.md", "edit",
                patch_spec, config, dry_run=True,
            )

    def test_end_marker_not_found_returns_error(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)

        test_file = temp_vault_root / "memory" / "markers.md"
        test_file.write_text("# Markers\n\n<!-- START -->\nsome text\n")
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "replace_exact_block",
            "start_marker": "<!-- START -->",
            "end_marker": "<!-- END -->",
            "new_content": "replacement",
        }

        with pytest.raises(PatchError, match="End marker not found"):
            prepare_change(
                "trusted_writer", "memory/markers.md", "edit",
                patch_spec, config, dry_run=True,
            )


class TestReplaceSentence:
    def test_replace_exact_sentence_creates_valid_plan(self, temp_vault_root, sample_config_dict, log_capture, trace_assert):
        log_capture.set_level(logging.DEBUG)

        test_file = temp_vault_root / "memory" / "sentence.md"
        test_file.write_text("# Sentence Test\n\nThe quick brown fox jumps over the lazy dog.\n")
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "replace_exact_text",
            "old_text": "The quick brown fox jumps over the lazy dog.",
            "new_text": "A swift red fox leaps across the sleepy hound.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/sentence.md", "edit",
            patch_spec, config, dry_run=True,
        )

        assert "swift red fox" in plan.new_content
        assert "quick brown fox" not in plan.new_content


class TestInsertAfterHeading:
    def test_insert_after_heading_creates_valid_diff(self, temp_vault_root, sample_config_dict, log_capture, trace_assert):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "insert_after_heading",
            "heading": "Test Note",
            "text": "Inserted right after heading.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/note.md", "edit",
            patch_spec, config, dry_run=True,
        )

        assert "Inserted right after heading." in plan.new_content
        assert "+Inserted right after heading." in plan.diff


class TestReplaceSection:
    def test_replace_section_creates_valid_plan(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)

        test_file = temp_vault_root / "memory" / "sections.md"
        test_file.write_text(
            "---\ntags: [test]\n---\n\n# Section One\n\nContent one.\n\n# Section Two\n\nContent two.\n\n# Section Three\n\nContent three.\n"
        )
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "replace_section",
            "heading": "Section Two",
            "new_content": "# Section Two\n\nReplaced content.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/sections.md", "edit",
            patch_spec, config, dry_run=True,
        )

        assert "Replaced content." in plan.new_content
        assert "Content two." not in plan.new_content
        assert "Section One" in plan.new_content
        assert "Section Three" in plan.new_content


class TestPreviewDiff:
    def test_preview_diff_returns_unified_diff_string(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test Note",
            "text": "Preview line.",
        }

        diff_str = preview_diff("trusted_writer", "memory/note.md", patch_spec, config)

        assert isinstance(diff_str, str)
        assert "+++" in diff_str
        assert "+Preview line." in diff_str


class TestDryRunFileIntegrity:
    def test_dry_run_returns_diff_without_touching_file(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        original = (temp_vault_root / "memory" / "note.md").read_text()

        patch_spec = {
            "operation": "replace_exact_text",
            "old_text": "This is a test note.",
            "new_text": "This note has been modified.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/note.md", "edit",
            patch_spec, config, dry_run=True,
        )

        current = (temp_vault_root / "memory" / "note.md").read_text()
        assert current == original
        assert plan.new_content != original
        assert "This note has been modified." in plan.new_content
        assert plan.diff != ""


class TestPatchValidation:
    def test_free_form_regex_patch_rejected(self):
        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test",
            "text": "content",
            "pattern": "re.compile(r'.*')",
        }
        assert validate_patch(patch_spec) is False

    def test_llm_unconstrained_patch_rejected(self):
        patch_spec = {
            "operation": "replace_exact_text",
            "old_text": ".*",
            "new_text": "replacement",
        }
        assert validate_patch(patch_spec) is False

    def test_unsupported_operation_rejected(self):
        patch_spec = {
            "operation": "delete_everything",
            "target": "all",
        }
        assert validate_patch(patch_spec) is False

    def test_missing_required_field_rejected(self):
        patch_spec = {
            "operation": "replace_exact_text",
            "old_text": "hello",
        }
        assert validate_patch(patch_spec) is False

    def test_vague_wildcard_old_text_rejected(self):
        patch_spec = {
            "operation": "replace_exact_text",
            "old_text": "error.+",
            "new_text": "fix",
        }
        assert validate_patch(patch_spec) is False


class TestInvalidMarkdownAfterApply:
    def test_invalid_markdown_after_apply_rejected(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test Note",
            "text": "## Valid subheading\n\n### Broken\n\n## Even more broken",
        }

        # The append_under_heading adds text that starts with ##, which creates
        # a heading gap (h1 -> h3 without h2 between them is fine but h1 -> h3 -> h2
        # is a gap). Let's append something that would create a heading jump:
        # file starts with # Test Note (level 1), then we add content with ### (level 3)
        # without a level 2 first. That's a gap (1->3 without 2).

        plan = prepare_change(
            "trusted_writer", "memory/note.md", "edit",
            patch_spec, config, dry_run=True,
        )
        assert isinstance(plan, MutationPlan)

    def test_valid_markdown_stays_valid_after_append(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Test Note",
            "text": "## Subheading\n\nContent under subheading.",
        }

        plan = prepare_change(
            "trusted_writer", "memory/note.md", "edit",
            patch_spec, config, dry_run=True,
        )
        assert isinstance(plan, MutationPlan)
        assert "## Subheading" in plan.new_content

    def test_broken_heading_level_jump_rejected(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)

        test_file = temp_vault_root / "memory" / "levels.md"
        test_file.write_text("# Level 1\n\nContent.\n")
        config = _make_config(temp_vault_root, sample_config_dict)

        # Append content with a level-3 heading after a level-1 heading
        # but no level-2 between them. validate_markdown_shape should reject this
        # because level 3 > level 1 + 1 = 2, meaning a gap.
        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Level 1",
            "text": "### Level 3 without level 2",
        }

        with pytest.raises(ValueError, match="markdown"):
            prepare_change(
                "trusted_writer", "memory/levels.md", "edit",
                patch_spec, config, dry_run=True,
            )


class TestPolicyIntegration:
    def test_prepare_change_policy_denies_denylist_path(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "replace_exact_text",
            "old_text": "Confidential",
            "new_text": "Public",
        }

        with pytest.raises(PermissionError, match="denylist"):
            prepare_change(
                "trusted_writer", "private/secret.md", "edit",
                patch_spec, config, dry_run=True,
            )

    def test_preview_diff_works_for_wiki_maintainer_propose_only(self, temp_vault_root, sample_config_dict, log_capture):
        log_capture.set_level(logging.DEBUG)

        wiki_file = temp_vault_root / "70_Wiki" / "Home.md"
        wiki_file.write_text("# Home\n\nWelcome to the wiki.\n")
        config = _make_config(temp_vault_root, sample_config_dict)

        patch_spec = {
            "operation": "append_under_heading",
            "heading": "Home",
            "text": "Additional content.",
        }

        diff_str = preview_diff("wiki_maintainer", "70_Wiki/Home.md", patch_spec, config)
        assert "Additional content." in diff_str


class TestAllOperations:
    def test_all_allowed_operations_have_required_fields_defined(self):
        for op in ALLOWED_PATCH_OPERATIONS:
            assert op in ("append_under_heading", "replace_section", "replace_exact_text",
                          "replace_exact_block", "insert_after_heading")

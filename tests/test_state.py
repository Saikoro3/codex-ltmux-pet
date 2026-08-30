from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lumi_monitor.config import load_config
from lumi_monitor.state import (
    apply_event,
    clean_text,
    dismiss_record,
    display_task_name,
    is_subagent_event,
    iter_records,
    read_record,
    task_name_from_prompt,
)


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("LUMI_STATE_DIR")
        os.environ["LUMI_STATE_DIR"] = self.temp.name
        for key in ("KITTY_LISTEN_ON", "KITTY_WINDOW_ID", "TMUX_PANE", "LUMI_TMUX_TARGET"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("LUMI_STATE_DIR", None)
        else:
            os.environ["LUMI_STATE_DIR"] = self.previous
        self.temp.cleanup()

    def event(self, name: str, **extra):
        payload = {"hook_event_name": name, "session_id": "session/a", "cwd": "/work/example", **extra}
        return apply_event(payload)

    def test_hook_state_transitions_do_not_store_prompt(self) -> None:
        self.assertEqual(self.event("SessionStart", source="startup")["state"], "idle")
        running = self.event("UserPromptSubmit", turn_id="turn-1", prompt="top secret full prompt")
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["task_name"], "topsecretf")
        self.assertNotIn("top secret", json.dumps(running))
        self.assertEqual(self.event("PermissionRequest", turn_id="turn-1")["state"], "waiting")
        self.assertEqual(self.event("PostToolUse", turn_id="turn-1")["state"], "running")
        complete = self.event("Stop", turn_id="turn-1", last_assistant_message="done")
        self.assertEqual(complete["state"], "complete")
        self.assertEqual(complete["preview_fallback"], "done")

    def test_compact_preserves_active_state(self) -> None:
        self.event("UserPromptSubmit", turn_id="turn-1")
        compact = self.event("SessionStart", source="compact")
        self.assertEqual(compact["state"], "running")

    def test_unexpected_session_end_is_failed(self) -> None:
        self.event("UserPromptSubmit", turn_id="turn-1")
        ended = self.event("SessionEnd", reason="other")
        self.assertEqual(ended["state"], "failed")

    def test_clean_session_end_removes_idle_record(self) -> None:
        self.event("SessionStart", source="startup")
        self.assertIsNone(self.event("SessionEnd", reason="other"))
        self.assertIsNone(read_record("session/a"))

    def test_session_end_keeps_unread_completion(self) -> None:
        self.event("UserPromptSubmit", turn_id="turn-1")
        self.event("Stop", turn_id="turn-1", last_assistant_message="finished")
        ended = self.event("SessionEnd", reason="other")
        self.assertEqual(ended["state"], "complete")
        self.assertEqual(ended["preview_fallback"], "finished")

    def test_notify_duplicate_is_idempotent(self) -> None:
        payload = {"type": "agent-turn-complete", "thread-id": "notify-1", "turn-id": "t1", "cwd": "/work", "last-assistant-message": "finished"}
        first = apply_event(payload, "notify")
        second = apply_event(payload, "notify")
        self.assertEqual(first["state"], "complete")
        self.assertEqual(second["state"], "complete")
        self.assertEqual(len(iter_records()), 1)

    def test_task_name_removes_whitespace_controls_and_uses_ten_unicode_characters(self) -> None:
        self.assertEqual(task_name_from_prompt("  Lumi\nの\t衣装を\x00大きく変更して  "), "Lumiの衣装を大き")

    def test_task_name_is_fixed_by_first_prompt(self) -> None:
        first = self.event("UserPromptSubmit", turn_id="turn-1", prompt="最初 の依頼です")
        second = self.event("UserPromptSubmit", turn_id="turn-2", prompt="別の依頼で上書きしない")
        self.assertEqual(first["task_name"], "最初の依頼です")
        self.assertEqual(second["task_name"], first["task_name"])

    def test_legacy_record_without_name_uses_fallback_instead_of_later_prompt(self) -> None:
        legacy = {
            "schema_version": 2,
            "session_id": "session/a",
            "state": "running",
            "cwd": "/work/legacy",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        Path(self.temp.name, "session_a.json").write_text(json.dumps(legacy), encoding="utf-8")
        updated = self.event("UserPromptSubmit", turn_id="later", prompt="this is not the first prompt")
        self.assertIsNone(updated["task_name"])
        self.assertTrue(updated["task_name_initialized"])
        self.assertEqual(display_task_name(updated), "example")

    def test_same_running_events_do_not_refresh_attention(self) -> None:
        running = self.event("UserPromptSubmit", turn_id="turn-1", prompt="work")
        attention = running["attention_at"]
        continued = self.event("PreToolUse", turn_id="turn-1")
        self.assertEqual(continued["state"], "running")
        self.assertEqual(continued["attention_at"], attention)

    def test_finished_at_is_not_extended_by_duplicate_notifications(self) -> None:
        payload = {"type": "agent-turn-complete", "thread-id": "notify-1", "turn-id": "t1", "cwd": "/work"}
        first = apply_event(payload, "notify")
        second = apply_event(payload, "notify")
        self.assertEqual(second["finished_at"], first["finished_at"])

    def test_new_active_turn_resets_finished_retention_clock(self) -> None:
        self.event("UserPromptSubmit", turn_id="turn-1", prompt="work")
        first = self.event("Stop", turn_id="turn-1")
        running = self.event("UserPromptSubmit", turn_id="turn-2", prompt="follow up")
        self.assertIsNone(running["finished_at"])
        second = self.event("Stop", turn_id="turn-2")
        self.assertIsNotNone(second["finished_at"])
        self.assertGreaterEqual(second["finished_at"], first["finished_at"])

    def test_manual_delete_blocks_late_terminal_events_for_same_turn(self) -> None:
        self.event("UserPromptSubmit", turn_id="turn-1", prompt="delete me")
        self.event("Stop", turn_id="turn-1", last_assistant_message="done")
        dismiss_record("session/a")
        self.assertIsNone(read_record("session/a"))
        self.assertIsNone(self.event("Stop", turn_id="turn-1", last_assistant_message="late"))
        notify = {"type": "agent-turn-complete", "thread-id": "session/a", "turn-id": "turn-1"}
        self.assertIsNone(apply_event(notify, "notify"))
        revived = self.event("UserPromptSubmit", turn_id="turn-2", prompt="new turn")
        self.assertEqual(revived["state"], "running")

    def test_manual_delete_is_isolated_to_one_session(self) -> None:
        for session, turn in (("session/a", "turn-1"), ("session/b", "turn-2")):
            apply_event({"hook_event_name": "UserPromptSubmit", "session_id": session, "turn_id": turn, "prompt": session})
            apply_event({"hook_event_name": "Stop", "session_id": session, "turn_id": turn})
        dismiss_record("session/a")
        self.assertIsNone(read_record("session/a"))
        self.assertIsNotNone(read_record("session/b"))

    def test_permission_request_records_waiting_provenance(self) -> None:
        self.event("UserPromptSubmit", turn_id="turn-1", prompt="work")
        waiting = self.event("PermissionRequest", turn_id="turn-1")
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["last_event"], "PermissionRequest")
        self.assertIsNotNone(waiting["waiting_since"])
        running = self.event("PostToolUse", turn_id="turn-1")
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["last_event"], "PostToolUse")
        self.assertIsNone(running["waiting_since"])

    def test_display_name_fallback_order(self) -> None:
        record = {"task_name": "Prompt", "codex_title": "Title", "cwd": "/work/project", "session_id": "abcdefgh-123"}
        self.assertEqual(display_task_name(record), "Prompt")
        record.pop("task_name")
        self.assertEqual(display_task_name(record), "Title")
        record["codex_title"] = os.environ.get("USER", "")
        self.assertEqual(display_task_name(record), "project")
        record.pop("codex_title")
        self.assertEqual(display_task_name(record), "project")
        record["cwd"] = ""
        self.assertEqual(display_task_name(record), "abcdefgh")

    def test_corrupt_state_file_is_ignored(self) -> None:
        Path(self.temp.name, "broken.json").write_text("{broken", encoding="utf-8")
        self.event("SessionStart", source="startup")
        self.assertEqual(len(iter_records()), 1)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        for index in range(20):
            self.event("PreToolUse", turn_id=f"turn-{index}")
        names = {path.name for path in Path(self.temp.name).iterdir()}
        self.assertIn("session_a.json", names)
        self.assertFalse(any(name.endswith(".tmp") for name in names))
        self.assertEqual(stat.S_IMODE(Path(self.temp.name, "session_a.json").stat().st_mode), 0o600)

    def test_explicit_subagent_events_are_not_recorded(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "child-session",
            "parent_session_id": "main-session",
        }
        self.assertTrue(is_subagent_event(payload))
        self.assertIsNone(apply_event(payload))
        self.assertEqual(iter_records(), [])

    def test_control_sequences_and_line_limit(self) -> None:
        value = clean_text("one\n\x1b[31mtwo\x1b[0m\nthree\nfour", 100, 3)
        self.assertEqual(value, "two\nthree\nfour")

    def test_display_priority_policy(self) -> None:
        self.assertEqual(load_config()["priority"], ["waiting", "running", "failed", "complete"])
        self.assertEqual(load_config()["finished_retention_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()

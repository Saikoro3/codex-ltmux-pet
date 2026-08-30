from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from lumi_monitor.config import load_config
from lumi_monitor.state import apply_event
from lumi_monitor.terminal import capture_preview, terminal_activity
from lumi_monitor.ui import Bubble, LumiWindow, TaskCard


class FakeHandle:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls = 0

    def startSystemMove(self) -> bool:
        self.calls += 1
        return self.result


class FakeMouseEvent:
    def __init__(self, button: Qt.MouseButton, point: QPoint, buttons: Qt.MouseButton = Qt.MouseButton.NoButton):
        self._button = button
        self._point = point
        self._buttons = buttons
        self.accepted = False

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def globalPosition(self):
        return self

    def position(self):
        return self

    def toPoint(self):
        return self._point

    def accept(self):
        self.accepted = True


class TestLumiWindow(LumiWindow):
    def __init__(
        self,
        config: dict,
        move_result: bool = True,
        geometry: tuple[str, int, int] | None = None,
        native_enabled: bool = True,
    ):
        self.fake_handle = FakeHandle(move_result)
        self.fake_geometry = geometry
        self.manual_moves: list[QPoint] = []
        test_config = dict(config)
        test_config["hyprland_native_system_move"] = native_enabled
        super().__init__(test_config)

    def windowHandle(self):  # type: ignore[override]
        return self.fake_handle

    def hypr_geometry(self):  # type: ignore[override]
        return self.fake_geometry

    def move_via_hypr(self, delta: QPoint) -> None:  # type: ignore[override]
        self.manual_moves.append(delta)


class TaskCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def record(self, state: str) -> dict:
        return {
            "session_id": "session-1",
            "state": state,
            "task_name": "一覧を改善する",
            "cwd": "/work/project",
            "kitty_window_id": "7",
            "preview_fallback": "three line message",
        }

    def test_finished_card_delete_button_does_not_activate_card(self) -> None:
        card = TaskCard(self.record("complete"), load_config())
        activated: list[dict] = []
        deleted: list[dict] = []
        card.activated.connect(activated.append)
        card.delete_requested.connect(deleted.append)
        card.show()
        self.app.processEvents()
        self.assertIsNotNone(card.delete_button)
        QTest.mouseClick(card.delete_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(activated, [])

    def test_preview_refresh_updates_existing_card_without_rebuilding_widgets(self) -> None:
        bubble = Bubble(load_config())
        first_record = self.record("running")
        bubble.populate([first_record])
        first_card = bubble.cards["session-1"]
        changed = {**first_record, "_preview": "updated preview"}
        bubble.populate([changed])
        self.assertIs(bubble.cards["session-1"], first_card)
        self.assertEqual(first_card.preview_label.text(), "updated preview")
        bubble.close()

    def test_repopulating_after_delete_keeps_a_nonzero_list_height(self) -> None:
        bubble = Bubble(load_config())
        records = [
            {**self.record("complete"), "session_id": f"session-{index}"}
            for index in range(3)
        ]
        bubble.populate(records)
        self.assertEqual(bubble.height(), 42 * 3 + 5 * 2)
        bubble.populate(records[:2])
        self.assertEqual(bubble.height(), 42 * 2 + 5)
        bubble.populate(records[:1])
        self.assertEqual(bubble.height(), 42)
        bubble.close()

    def test_expanded_list_keeps_front_card_in_main_deck_and_adds_only_older_cards_below(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config)
            front = {**self.record("running"), "session_id": "front", "task_name_initialized": True}
            older = {**self.record("waiting"), "session_id": "older", "task_name_initialized": True}
            window.records = [front, older]
            base_height = window.base_height
            window.toggle_bubble()
            self.assertTrue(window.bubble_expanded)
            self.assertIs(window.bubble.parentWidget(), window)
            self.assertNotIn("front", window.bubble.cards)
            self.assertIn("older", window.bubble.cards)
            self.assertEqual(window.bubble.width(), window.width() - 16)
            self.assertEqual(window.bubble.cards["older"].height(), 42)
            self.assertEqual(window.bubble.pos(), QPoint(8, base_height + 5))
            self.assertEqual(window.height(), base_height + 5 + window.bubble.height())
            self.assertEqual(window.primary_card_rect().y(), base_height - 45)
            window.toggle_bubble()
            self.assertFalse(window.bubble_expanded)
            self.assertEqual(window.height(), base_height)
            window.bubble.close()
            window.close()

    def test_always_visible_front_card_click_focuses_its_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config)
            front = {**self.record("running"), "session_id": "front", "task_name_initialized": True}
            older = {**self.record("waiting"), "session_id": "older", "task_name_initialized": True}
            window.records = [front, older]
            opened: list[dict] = []
            toggles: list[bool] = []
            window.open_task = opened.append  # type: ignore[method-assign]
            window.toggle_bubble = lambda: toggles.append(True)  # type: ignore[method-assign]
            card_center = window.mapToGlobal(window.primary_card_rect().center())
            window.handle_click(card_center)
            self.assertEqual(opened, [front])
            self.assertEqual(toggles, [])
            window.close()

    def test_compact_card_hides_project_user_and_reasoning_noise(self) -> None:
        record = {**self.record("running"), "task_name": "", "codex_title": "", "session_id": "abcdefgh-123"}
        record["_preview"] = f"{Path.home()}/work\nreasoning: private trace\nuseful result"
        card = TaskCard(record, load_config())
        self.assertEqual(card.title_label.text(), "project")
        self.assertNotIn(Path.home().name.lower(), card.meta_label.text().lower())
        self.assertNotIn(Path.home().name.lower(), card.preview_label.text().lower())
        self.assertNotIn("reasoning", card.preview_label.text().lower())
        self.assertLessEqual(card.preview_label.maximumHeight(), 18)
        self.assertEqual(capture_preview({"preview_fallback": record["_preview"]}, 3, 180), "~/work\nuseful result")

    def test_terminal_activity_uses_latest_visible_tui_marker(self) -> None:
        with patch("lumi_monitor.terminal.terminal_screen", return_value="• Working (2s)\nDo you want to allow this command?\nEsc to cancel"):
            self.assertEqual(terminal_activity({}), "waiting")
        with patch("lumi_monitor.terminal.terminal_screen", return_value="old approval required\n• Working (25s • esc to interrupt)"):
            self.assertEqual(terminal_activity({}), "running")

    def test_card_body_activates_terminal_focus_signal(self) -> None:
        card = TaskCard(self.record("running"), load_config())
        activated: list[dict] = []
        card.activated.connect(activated.append)
        card.show()
        self.app.processEvents()
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(4, card.height() - 4))
        self.assertEqual(len(activated), 1)
        self.assertIsNone(card.delete_button)

    def test_non_hypr_wayland_move_is_requested_during_press_and_click_opens_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config)
            toggles: list[bool] = []
            window.toggle_bubble = lambda: toggles.append(True)  # type: ignore[method-assign]
            press = FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton)
            window.mousePressEvent(press)
            self.assertEqual(window.fake_handle.calls, 1)
            self.assertTrue(window.system_move_started)
            release = FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100))
            window.mouseReleaseEvent(release)
            self.assertEqual(toggles, [True])
            window.close()

    def test_hyprland_drag_prefers_native_compositor_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config, geometry=("0xabc", 400, 300))
            press = FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton)
            window.mousePressEvent(press)
            self.assertEqual(window.fake_handle.calls, 1)
            self.assertTrue(window.system_move_started)
            self.assertEqual(window.press_window_origin, QPoint(400, 300))
            window.update_drag_from_global(QPoint(140, 112))
            self.assertEqual(window.manual_moves, [])
            window.flush_drag_move()
            self.assertEqual(window.manual_moves, [])
            self.assertTrue(window.drag_threshold_crossed)
            window.close()

    def test_native_wayland_grab_cannot_be_misread_as_a_click(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config)
            toggles: list[bool] = []
            window.toggle_bubble = lambda: toggles.append(True)  # type: ignore[method-assign]
            window.current_mouse_buttons = lambda: Qt.MouseButton.NoButton  # type: ignore[method-assign]
            window.current_cursor_position = lambda: QPoint(100, 100)  # type: ignore[method-assign]

            window.mousePressEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton))
            window.check_drag_release()
            self.assertEqual(toggles, [])
            self.assertIsNotNone(window.press_global)

            window.current_cursor_position = lambda: QPoint(140, 110)  # type: ignore[method-assign]
            window.check_drag_release()
            self.assertTrue(window.drag_threshold_crossed)
            self.assertEqual(toggles, [])
            window.mouseReleaseEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(140, 110)))
            self.assertEqual(toggles, [])
            self.assertIsNone(window.press_global)
            window.close()

    def test_hyprland_drag_uses_manual_fallback_when_native_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config, move_result=False, geometry=("0xabc", 400, 300))
            press = FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton)
            window.mousePressEvent(press)
            self.assertEqual(window.fake_handle.calls, 1)
            self.assertFalse(window.system_move_started)
            window.update_drag_from_global(QPoint(140, 112))
            window.flush_drag_move()
            self.assertEqual(window.manual_moves, [QPoint(40, 12)])
            window.close()

    def test_hyprland_053_manual_path_preserves_click_and_drag(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "test-instance"}
        ):
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(
                config,
                geometry=("0xabc", 400, 300),
                native_enabled=False,
            )
            toggles: list[bool] = []
            window.toggle_bubble = lambda: toggles.append(True)  # type: ignore[method-assign]

            window.mousePressEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton))
            self.assertEqual(window.fake_handle.calls, 0)
            window.mouseReleaseEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100)))
            self.assertEqual(toggles, [True])

            window.current_cursor_position = lambda: QPoint(140, 112)  # type: ignore[method-assign]
            window.current_mouse_buttons = lambda: Qt.MouseButton.LeftButton  # type: ignore[method-assign]
            window.mousePressEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton))
            window.mouseMoveEvent(FakeMouseEvent(Qt.MouseButton.NoButton, QPoint(140, 112), Qt.MouseButton.LeftButton))
            self.assertEqual(window.manual_moves, [])
            window.check_drag_release()
            self.assertEqual(window.manual_moves, [QPoint(40, 12)])
            window.mouseReleaseEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(140, 112)))
            self.assertEqual(toggles, [True])
            window.close()

    def test_expanded_card_starts_the_same_native_drag_as_the_pet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config)
            front = {**self.record("running"), "session_id": "front", "task_name_initialized": True}
            older = {**self.record("complete"), "session_id": "older", "task_name_initialized": True}
            window.records = [front, older]
            window.toggle_bubble()
            card = window.bubble.cards["older"]
            opened: list[dict] = []
            window.open_task = opened.append  # type: ignore[method-assign]

            card.mousePressEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 300), Qt.MouseButton.LeftButton))
            self.assertEqual(window.fake_handle.calls, 1)
            self.assertTrue(window.system_move_started)
            self.assertEqual((window.press_record or {}).get("session_id"), "older")
            card.mouseMoveEvent(FakeMouseEvent(Qt.MouseButton.NoButton, QPoint(140, 310), Qt.MouseButton.LeftButton))
            self.assertTrue(window.drag_threshold_crossed)
            card.mouseReleaseEvent(FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(140, 310)))
            self.assertEqual(opened, [])
            self.assertIsNone(window.press_record)
            window.close()

    def test_deleting_one_expanded_card_keeps_other_cards_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"KITTY_WINDOW_ID": "7"}):
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            for index in range(3):
                session_id = f"delete-session-{index}"
                turn_id = f"turn-{index}"
                apply_event(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "cwd": "/work/project",
                        "prompt": f"task {index}",
                    },
                    config=config,
                )
                apply_event(
                    {
                        "hook_event_name": "Stop",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "cwd": "/work/project",
                    },
                    config=config,
                )

            window = TestLumiWindow(config)
            window.toggle_bubble()
            self.assertTrue(window.bubble_expanded)
            self.assertEqual(len(window.records), 3)
            deleted_id = next(iter(window.bubble.cards))
            deleted_record = window.bubble.cards[deleted_id].record
            window.remove_task(deleted_record)
            self.assertIn(deleted_id, window.pending_deletions)
            self.assertEqual(len(window.records), 3)

            QTest.qWait(1)
            self.app.processEvents()
            self.assertNotIn(deleted_id, window.pending_deletions)
            self.assertEqual(len(window.records), 2)
            self.assertNotIn(deleted_id, {str(record["session_id"]) for record in window.records})
            self.assertTrue(window.bubble_expanded)
            self.assertEqual(len(window.bubble.cards), 1)
            self.assertEqual(window.bubble.height(), 42)
            self.assertEqual(window.height(), window.base_height + 5 + 42)
            window.close()

    def test_wayland_pointer_grab_still_classifies_drag_without_mouse_move_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config, move_result=False)
            toggles: list[bool] = []
            window.toggle_bubble = lambda: toggles.append(True)  # type: ignore[method-assign]
            window.current_cursor_position = lambda: QPoint(140, 100)  # type: ignore[method-assign]
            window.current_mouse_buttons = lambda: Qt.MouseButton.LeftButton  # type: ignore[method-assign]
            press = FakeMouseEvent(Qt.MouseButton.LeftButton, QPoint(100, 100), Qt.MouseButton.LeftButton)
            window.mousePressEvent(press)
            window.check_drag_release()
            self.assertTrue(window.drag_threshold_crossed)
            self.assertEqual(window.drag_state, "running-right")
            window.current_mouse_buttons = lambda: Qt.MouseButton.NoButton  # type: ignore[method-assign]
            window.check_drag_release()
            self.assertEqual(toggles, [])
            self.assertIsNone(window.drag_state)
            window.close()

    def test_waiting_state_is_reconciled_with_live_terminal_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            window = TestLumiWindow(config)
            waiting = {
                **self.record("waiting"),
                "task_name_initialized": True,
                "waiting_since": datetime.now(timezone.utc).isoformat(),
            }
            window.activity_for = lambda record: "running"  # type: ignore[method-assign]
            reconciled = window.reconcile_waiting(waiting)
            self.assertEqual(reconciled["state"], "running")
            self.assertEqual(reconciled["source_state"], "waiting")
            window.activity_for = lambda record: "waiting"  # type: ignore[method-assign]
            self.assertEqual(window.reconcile_waiting(waiting)["state"], "waiting")
            window.close()

    def test_state_animation_stops_on_representative_frame_after_three_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            config["poll_interval_ms"] = 50
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "animation-test",
                "turn_id": "turn-1",
                "cwd": "/work/project",
                "prompt": "animation test",
                "codex_pid": os.getpid(),
            }
            apply_event(payload, config=config)
            window = TestLumiWindow(config)
            QTest.qWait(3300)
            self.assertEqual(window.frame_index, config["representative_frames"]["running"])
            stopped_at = window.frame_index
            QTest.qWait(450)
            self.assertEqual(window.frame_index, stopped_at)
            self.assertLess(window.animation_until, time.monotonic())
            window.close()

    def test_list_filter_and_priority_match_active_and_one_hour_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["state_dir"] = directory
            config["position_file"] = os.path.join(directory, "position.json")
            config["poll_interval_ms"] = 10000
            window = TestLumiWindow(config)
            now = datetime.now(timezone.utc)

            def stamp(delta: timedelta = timedelta()) -> str:
                return (now + delta).isoformat().replace("+00:00", "Z")

            base = {"session_id": "x", "updated_at": stamp(), "cwd": "/work/project", "kitty_window_id": "7", "task_name_initialized": True}
            self.assertFalse(window.is_visible_record({**base, "session_id": "subagent", "state": "running", "codex_pid": os.getpid(), "task_name_initialized": False}))
            self.assertFalse(window.is_visible_record({**base, "state": "idle"}))
            self.assertTrue(window.is_visible_record({**base, "session_id": "alive", "state": "running", "codex_pid": os.getpid()}))
            self.assertFalse(window.is_visible_record({**base, "session_id": "dead", "state": "waiting", "codex_pid": 999_999_999}))
            self.assertTrue(window.is_visible_record({**base, "state": "failed", "finished_at": stamp(timedelta(minutes=-59))}))
            self.assertFalse(window.is_visible_record({**base, "state": "complete", "finished_at": stamp(timedelta(minutes=-61))}))

            records = [
                {**base, "session_id": state, "state": state, "updated_at": stamp(timedelta(minutes=-5))}
                for state in ("complete", "failed", "running", "waiting")
            ]
            self.assertEqual([item["state"] for item in sorted(records, key=window.priority)], ["waiting", "running", "failed", "complete"])
            fresh_complete = {**base, "state": "complete", "attention_at": stamp()}
            self.assertLess(window.priority(fresh_complete), window.priority(records[-1]))
            newest = {**base, "state": "running", "updated_at": stamp(timedelta(seconds=5))}
            older_waiting = {**base, "state": "waiting", "updated_at": stamp(timedelta(seconds=-5))}
            self.assertEqual(sorted([older_waiting, newest], key=window.freshness, reverse=True)[0]["state"], "running")
            window.close()


if __name__ == "__main__":
    unittest.main()

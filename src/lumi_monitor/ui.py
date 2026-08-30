from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from .config import load_config
from .state import delete_record, dismiss_record, display_task_name, iter_records, parse_time
from .terminal import capture_preview, endpoint_alive, endpoint_label, focus_record, terminal_activity

CELL_W = 192
CELL_H = 208


class SpriteAtlas:
    def __init__(self, path: str):
        self.image = QImage(path)
        if self.image.isNull() or self.image.width() != 1536 or self.image.height() != 2288:
            raise RuntimeError(f"invalid Lumi v2 spritesheet: {path}")
        self.scaled_frames: dict[tuple[int, int, int], QPixmap] = {}

    def frame(self, row: int, column: int, width: int) -> QPixmap:
        key = (row, column, width)
        if key in self.scaled_frames:
            return self.scaled_frames[key]
        cell = self.image.copy(column * CELL_W, row * CELL_H, CELL_W, CELL_H)
        pixmap = QPixmap.fromImage(cell)
        scaled = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        self.scaled_frames[key] = scaled
        return scaled

    def preload(self, width: int) -> None:
        for row in range(11):
            for column in range(8):
                self.frame(row, column, width)


class TaskCard(QFrame):
    activated = pyqtSignal(dict)
    delete_requested = pyqtSignal(dict)
    interaction_pressed = pyqtSignal(dict, object)
    interaction_moved = pyqtSignal(object)
    interaction_released = pyqtSignal(object)

    def __init__(self, record: dict[str, Any], config: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.record = record
        self.interaction_origin: QPoint | None = None
        self.interaction_dragged = False
        self.state_labels = config["state_labels"]
        state = str(record.get("state", "idle"))
        color = config["colors"].get(state, "#96a0b5")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        self.setStyleSheet(
            f"QFrame{{background:rgba(19,24,37,232);border:1px solid {color};border-radius:19px;padding:0}} "
            "QLabel{color:#edf4fa;background:transparent;border:0}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 9, 3)
        layout.setSpacing(0)
        heading = QHBoxLayout()
        heading.setSpacing(3)
        self.chip = QLabel(config["state_labels"].get(state, state))
        self.chip.setStyleSheet("color:#9da9bc;background:transparent;border:0;font-size:7pt")
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight:800;font-size:8pt")
        heading.addWidget(self.title_label, 1)
        self.delete_button: QToolButton | None = None
        if state in {"complete", "failed"}:
            self.delete_button = QToolButton(self)
            self.delete_button.setText("×")
            self.delete_button.setToolTip("一覧から削除")
            self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.delete_button.setFixedSize(20, 20)
            self.delete_button.setStyleSheet("QToolButton{color:#d6e1eb;background:#303a4e;border:0;border-radius:10px;font-size:8pt;font-weight:800;padding:0} QToolButton:hover{background:#46536a}")
            self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self.record))
            heading.addWidget(self.delete_button)
        self.meta_label = QLabel()
        self.meta_label.setStyleSheet("color:#9fadb9;font-size:7pt")
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(False)
        self.preview_label.setMaximumHeight(18)
        self.preview_label.setStyleSheet("font-size:7pt")
        layout.addLayout(heading)
        layout.addWidget(self.chip)
        self.meta_label.hide()
        self.preview_label.hide()
        self.update_record(record)

    def update_record(self, record: dict[str, Any]) -> None:
        self.record = record
        endpoint = endpoint_label(record)
        self.title_label.setText(display_task_name(record))
        self.chip.setText(self.config_state_label(record))
        self.meta_label.setText(endpoint)
        self.meta_label.setVisible(False)
        preview = capture_preview({"preview_fallback": record.get("_preview") or record.get("preview_fallback") or ""}, 1, 80)
        self.preview_label.setText(preview)
        self.preview_label.setVisible(False)

    def config_state_label(self, record: dict[str, Any]) -> str:
        state = str(record.get("state", "idle"))
        return str(self.state_labels.get(state, state))

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.globalPosition().toPoint()
            self.interaction_origin = point
            self.interaction_dragged = False
            self.interaction_pressed.emit(self.record, point)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if self.interaction_origin and event.buttons() & Qt.MouseButton.LeftButton:
            point = event.globalPosition().toPoint()
            if (point - self.interaction_origin).manhattanLength() >= QApplication.startDragDistance():
                self.interaction_dragged = True
            self.interaction_moved.emit(point)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.globalPosition().toPoint()
            dragged = self.interaction_dragged
            self.interaction_origin = None
            self.interaction_dragged = False
            self.interaction_released.emit(point)
            if not dragged:
                self.activated.emit(self.record)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class Bubble(QWidget):
    task_selected = pyqtSignal(dict)
    task_deleted = pyqtSignal(dict)
    interaction_pressed = pyqtSignal(dict, object)
    interaction_moved = pyqtSignal(object)
    interaction_released = pyqtSignal(object)

    def __init__(self, config: dict[str, Any], parent: QWidget | None = None):
        if parent is None:
            super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        else:
            super().__init__(parent)
        self.config = config
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(int(config["bubble_width_px"]))
        self.container = QVBoxLayout(self)
        self.container.setContentsMargins(0, 0, 0, 0)
        self.container.setSpacing(5)
        self.card_signature: tuple[tuple[str, str], ...] = ()
        self.cards: dict[str, TaskCard] = {}

    def populate(self, records: list[dict[str, Any]]) -> None:
        signature = tuple((str(record.get("session_id") or ""), str(record.get("state") or "")) for record in records)
        if signature == self.card_signature and len(self.cards) == len(records):
            for record in records:
                if card := self.cards.get(str(record.get("session_id") or "")):
                    card.update_record(record)
            self.setFixedHeight(self.content_height(len(records)))
            return
        self.setUpdatesEnabled(False)
        while self.container.count():
            item = self.container.takeAt(0)
            widget = item.widget()
            if widget:
                # setParent(None) detaches the widget from its layout item on
                # Wayland. Keep one stable Python reference through teardown.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.cards = {}
        self.card_signature = signature
        for record in records:
            card = TaskCard(record, self.config)
            card.delete_requested.connect(self.task_deleted)
            card.interaction_pressed.connect(self.interaction_pressed)
            card.interaction_moved.connect(self.interaction_moved)
            card.interaction_released.connect(self.interaction_released)
            self.container.addWidget(card)
            self.cards[str(record.get("session_id") or "")] = card
        self.setFixedHeight(self.content_height(len(records)))
        self.setUpdatesEnabled(True)
        self.update()

    def content_height(self, count: int) -> int:
        return count * 42 + max(0, count - 1) * self.container.spacing()

    def paintEvent(self, event):  # type: ignore[override]
        super().paintEvent(event)


class LumiWindow(QWidget):
    def __init__(self, config: dict[str, Any]):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.config = config
        self.atlas = SpriteAtlas(config["spritesheet"])
        self.records: list[dict[str, Any]] = []
        self.primary: dict[str, Any] | None = None
        self.drag_state: str | None = None
        self.press_global: QPoint | None = None
        self.press_local: QPoint | None = None
        self.press_record: dict[str, Any] | None = None
        self.press_started_at = 0.0
        self.press_window_origin: QPoint | None = None
        self.fallback_address: str | None = None
        self.drag_threshold_crossed = False
        self.system_move_started = False
        self.pending_drag_delta: QPoint | None = None
        self.last_drag_target: tuple[int, int] | None = None
        self.frame_index = 0
        self.last_frame_change = time.monotonic()
        self.animation_until = 0.0
        self.animation_token: tuple[str, str, str] | None = None
        self.live_cache: dict[str, tuple[float, bool | None]] = {}
        self.activity_cache: dict[str, tuple[float, str | None]] = {}
        self.pending_deletions: set[str] = set()
        self.restore_target: tuple[int, int] | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(False)
        self.setWindowTitle("Lumi")
        self.setObjectName("LumiPet")
        width = int(config["sprite_width_px"])
        # Decode and scale every frame before the window is first mapped. Drag
        # animation then only swaps cached pixmaps instead of doing image work
        # while compositor move commands are arriving.
        self.atlas.preload(width)
        sprite_height = round(width * CELL_H / CELL_W)
        self.base_height = (
            sprite_height
            + int(config.get("status_area_height_px", 24))
            + int(config.get("task_deck_height_px", 56))
        )
        self.setFixedSize(
            width + 24,
            self.base_height,
        )
        # Keep the expanded cards inside Lumi's one Wayland surface. A second
        # top-level surface can be mapped at a compositor-selected position and
        # can also be mistaken for the pet when locating the PID during drag.
        self.bubble = Bubble(config, self)
        self.bubble.setFixedWidth(self.width() - 16)
        self.bubble.hide()
        self.bubble_expanded = False
        self.bubble.task_deleted.connect(self.remove_task)
        self.bubble.interaction_pressed.connect(self.begin_card_interaction)
        self.bubble.interaction_moved.connect(self.continue_pointer_interaction)
        self.bubble.interaction_released.connect(self.end_card_interaction)
        self.restore_position()
        QTimer.singleShot(350, self.apply_restored_position)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_state)
        self.poll_timer.start(int(config["poll_interval_ms"]))
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.animate)
        self.animation_timer.start(35)
        self.drag_timer = QTimer(self)
        self.drag_timer.timeout.connect(self.check_drag_release)
        self.drag_timer.start(int(config.get("drag_poll_interval_ms", 16)))
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self.refresh_previews)
        self.preview_timer.start(int(config["preview_poll_interval_ms"]))
        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self.save_position)
        self.position_timer.start(int(config["position_poll_interval_ms"]))
        self.poll_state()

    def priority(self, record: dict[str, Any]) -> tuple[int, float]:
        attention = parse_time(record.get("attention_at"))
        attention_epoch = attention.timestamp() if attention else 0.0
        if attention and (datetime.now(timezone.utc) - attention).total_seconds() <= float(self.config["attention_seconds"]):
            return 0, -attention_epoch
        priority = self.config["priority"]
        state = str(record.get("state", "idle"))
        rank = priority.index(state) if state in priority else len(priority)
        stamp = parse_time(record.get("updated_at"))
        epoch = stamp.timestamp() if stamp else 0.0
        return rank + 1, -epoch

    def freshness(self, record: dict[str, Any]) -> float:
        stamp = parse_time(record.get("updated_at")) or parse_time(record.get("attention_at"))
        return stamp.timestamp() if stamp else 0.0

    def activity_for(self, record: dict[str, Any]) -> str | None:
        session = str(record.get("session_id") or "")
        cached = self.activity_cache.get(session)
        lifetime = float(self.config.get("terminal_activity_seconds", 1))
        if cached and time.monotonic() - cached[0] < lifetime:
            return cached[1]
        activity = terminal_activity(record)
        self.activity_cache[session] = (time.monotonic(), activity)
        return activity

    def reconcile_waiting(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("state") != "waiting":
            return record
        activity = self.activity_for(record)
        waiting_since = parse_time(record.get("waiting_since")) or parse_time(record.get("attention_at")) or parse_time(record.get("updated_at"))
        age = (datetime.now(timezone.utc) - waiting_since).total_seconds() if waiting_since else 10**9
        if activity != "running" and (activity == "waiting" or age <= float(self.config.get("waiting_unknown_seconds", 15))):
            return record
        reconciled = dict(record)
        reconciled["source_state"] = "waiting"
        reconciled["state"] = "running"
        reconciled["preview_fallback"] = "作業を続けています"
        return reconciled

    def is_visible_record(self, record: dict[str, Any]) -> bool:
        if self.config.get("main_sessions_only", True):
            if record.get("scope") not in {None, "main"}:
                return False
            # Interactive main sessions own a real Kitty/tmux endpoint.
            # A top-level task also receives UserPromptSubmit, which fixes its
            # task name. Subagents can inherit the parent's terminal metadata,
            # but never receive that user-prompt initialization.
            if not (record.get("kitty_window_id") or record.get("tmux_pane")) or not record.get("task_name_initialized"):
                return False
        state = str(record.get("state", "idle"))
        if state not in {"running", "waiting", "complete", "failed"}:
            return False
        updated = parse_time(record.get("updated_at"))
        age = (datetime.now(timezone.utc) - updated).total_seconds() if updated else 10**9
        session = str(record.get("session_id", ""))
        if state in {"running", "waiting"}:
            cached = self.live_cache.get(session)
            if not cached or time.monotonic() - cached[0] >= float(self.config["terminal_check_seconds"]):
                alive = endpoint_alive(record)
                self.live_cache[session] = (time.monotonic(), alive)
            else:
                alive = cached[1]
            if alive is False or (alive is None and age > float(self.config["running_stale_seconds"])):
                delete_record(session, self.config)
                return False
            return True
        finished = parse_time(record.get("finished_at")) or updated
        finished_age = (datetime.now(timezone.utc) - finished).total_seconds() if finished else 10**9
        if finished_age > float(self.config["finished_retention_seconds"]):
            delete_record(session, self.config)
            return False
        return True

    def poll_state(self) -> None:
        records = [
            self.reconcile_waiting(record)
            for record in iter_records(self.config)
            if self.is_visible_record(record)
        ]
        # The visible deck is a message stack: the session with the newest
        # lifecycle update sits in front. Animation/state priority remains a
        # separate decision so waiting still receives attention immediately.
        records.sort(key=self.freshness, reverse=True)
        self.records = records
        self.primary = min(records, key=self.priority) if records else None
        token = None
        if self.primary:
            token = (
                str(self.primary.get("session_id") or ""),
                str(self.primary.get("state") or ""),
                str(self.primary.get("attention_at") or ""),
            )
        if token != self.animation_token:
            self.animation_token = token
            self.frame_index = 0
            self.last_frame_change = time.monotonic()
            attention = parse_time((self.primary or {}).get("attention_at"))
            age = (datetime.now(timezone.utc) - attention).total_seconds() if attention else 10**9
            remaining = float(self.config["attention_seconds"]) - max(0.0, age)
            self.animation_until = time.monotonic() + remaining if remaining > 0 else 0.0
        if self.bubble_expanded:
            self.refresh_previews()
        self.update()

    def refresh_previews(self, force: bool = False) -> None:
        if not force and not self.bubble_expanded:
            return
        if len(self.records) <= 1:
            self.collapse_bubble()
            return
        rendered: list[dict[str, Any]] = []
        for record in self.records[1:]:
            enriched = dict(record)
            enriched["_preview"] = capture_preview(record, int(self.config["preview_lines"]), int(self.config["preview_chars"]))
            rendered.append(enriched)
        self.bubble.populate(rendered)
        self.place_bubble()

    def current_animation(self) -> tuple[str, int, list[int]]:
        if self.drag_state:
            name = self.drag_state
        elif self.primary:
            name = str(self.primary.get("state", "idle"))
        else:
            name = "idle"
        row = int(self.config["state_rows"].get(name, 0))
        durations = list(self.config["row_frames"].get(name, self.config["row_frames"]["idle"]))
        return name, row, durations

    def animate(self) -> None:
        if not self.drag_state and time.monotonic() >= self.animation_until:
            name, _, _ = self.current_animation()
            representative = int(self.config["representative_frames"].get(name, 0))
            if self.frame_index != representative:
                self.frame_index = representative
                self.update()
            return
        _, _, durations = self.current_animation()
        self.frame_index %= len(durations)
        if (time.monotonic() - self.last_frame_change) * 1000 >= durations[self.frame_index]:
            self.frame_index = (self.frame_index + 1) % len(durations)
            self.last_frame_change = time.monotonic()
            self.update()

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        state = str((self.primary or {}).get("state", "idle"))
        color = QColor(self.config["colors"].get(state, "#96a0b5"))
        if self.primary:
            chip_width = int(self.config.get("status_chip_width_px", 60))
            chip_height = int(self.config.get("status_chip_height_px", 18))
            bubble = QRect((self.width() - chip_width) // 2, 2, chip_width, chip_height)
            painter.setBrush(QColor(8, 14, 22, 218))
            painter.setPen(color)
            painter.drawRoundedRect(bubble, chip_height // 2, chip_height // 2)
            painter.setPen(color)
            painter.setFont(QFont("Inter", 7, 700))
            label = self.config["state_labels"].get(state, state)
            painter.drawText(bubble, Qt.AlignmentFlag.AlignCenter, label)
        name, row, durations = self.current_animation()
        if self.drag_state or time.monotonic() < self.animation_until:
            column = self.frame_index % len(durations)
        else:
            column = int(self.config["representative_frames"].get(name, 0)) % len(durations)
        pixmap = self.atlas.frame(row, column, int(self.config["sprite_width_px"]))
        x = (self.width() - pixmap.width()) // 2
        y = int(self.config.get("status_area_height_px", 24))
        painter.drawPixmap(x, y, pixmap)
        self.draw_task_deck(painter)

    def draw_task_deck(self, painter: QPainter) -> None:
        if not self.records:
            return
        max_layers = int(self.config.get("task_deck_max_layers", 3))
        layer_count = min(max_layers, len(self.records))
        card = self.primary_card_rect()
        width = card.width()
        foreground_y = card.y()

        for layer in range(layer_count - 1, 0, -1):
            inset = layer * 4
            rect = QRect(8 + inset, foreground_y - layer * 4, width - inset * 2, 42)
            painter.setPen(QColor(91, 106, 130, 115))
            painter.setBrush(QColor(17, 22, 34, 170))
            painter.drawRoundedRect(rect, 18, 18)

        record = self.records[0]
        state = str(record.get("state", "idle"))
        color = QColor(self.config["colors"].get(state, "#96a0b5"))
        painter.setPen(QColor(color.red(), color.green(), color.blue(), 155))
        painter.setBrush(QColor(19, 24, 37, 232))
        painter.drawRoundedRect(card, 19, 19)

        painter.setPen(QColor("#e7edf6"))
        title_font = QFont("Inter", 8, 700)
        painter.setFont(title_font)
        title = display_task_name(record)
        title_width = card.width() - 48
        title = QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, title_width)
        painter.drawText(QRect(card.x() + 12, card.y() + 6, title_width, 15), Qt.AlignmentFlag.AlignLeft, title)

        painter.setPen(QColor("#9da9bc"))
        painter.setFont(QFont("Inter", 7))
        painter.drawText(
            QRect(card.x() + 12, card.y() + 22, card.width() - 48, 13),
            Qt.AlignmentFlag.AlignLeft,
            self.config["state_labels"].get(state, state),
        )

        if len(self.records) > 1:
            count = str(len(self.records))
            badge = QRect(card.right() - 30, card.y() + 11, 20, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(48, 58, 78, 240))
            painter.drawEllipse(badge)
            painter.setPen(QColor("#d9e3ef"))
            painter.setFont(QFont("Inter", 7, 700))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, count)
        elif state == "failed":
            alert = QRect(card.right() - 29, card.y() + 11, 18, 18)
            painter.setPen(QColor("#ff748c"))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(alert)
            painter.setFont(QFont("Inter", 8, 700))
            painter.drawText(alert, Qt.AlignmentFlag.AlignCenter, "!")

    def primary_card_rect(self) -> QRect:
        return QRect(8, self.base_height - 45, self.width() - 16, 42)

    def handle_click_at(self, local_position: QPoint) -> None:
        if self.records and self.primary_card_rect().contains(local_position):
            self.open_task(self.records[0])
            return
        self.toggle_bubble()

    def handle_click(self, global_position: QPoint) -> None:
        self.handle_click_at(self.mapFromGlobal(global_position))

    def begin_pointer_interaction(
        self,
        global_position: QPoint,
        local_position: QPoint | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        self.press_global = QPoint(global_position)
        self.press_local = QPoint(local_position) if local_position is not None else None
        self.press_record = record
        self.press_started_at = time.monotonic()
        self.press_window_origin = QPoint(self.x(), self.y())
        self.fallback_address = None
        self.drag_threshold_crossed = False
        self.system_move_started = False
        self.pending_drag_delta = None
        self.last_drag_target = None
        geometry = self.hypr_geometry()
        if geometry:
            self.fallback_address = geometry[0]
            self.press_window_origin = QPoint(geometry[1], geometry[2])
        # Hyprland 0.53 acknowledges Qt's xdg_toplevel.move capability without
        # performing the interactive move. It also consumes the matching release
        # event, which breaks both click and drag classification. Keep the Qt
        # pointer grab and use the compositor IPC fallback on this installation.
        on_hyprland = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
        native_allowed = not on_hyprland or bool(self.config.get("hyprland_native_system_move", False))
        if native_allowed:
            handle = self.windowHandle()
            if handle:
                try:
                    self.system_move_started = bool(handle.startSystemMove())
                except RuntimeError:
                    self.system_move_started = False

    def begin_card_interaction(self, record: dict[str, Any], global_position: QPoint) -> None:
        self.begin_pointer_interaction(global_position, record=record)

    def continue_pointer_interaction(self, global_position: QPoint) -> None:
        if not self.press_global:
            return
        self.update_drag_from_global(global_position)
        # Coalesce high-rate mouse motion into the 16 ms drag timer. Flooding
        # Hyprland with one command per hardware event causes visible judder.

    def activate_press_target(
        self,
        record: dict[str, Any] | None,
        local_position: QPoint | None,
    ) -> None:
        if record is not None:
            self.open_task(record)
        elif local_position is not None:
            self.handle_click_at(local_position)

    def end_pointer_interaction(
        self,
        global_position: QPoint,
        local_position: QPoint | None = None,
    ) -> None:
        if not self.press_global:
            return
        click_record = self.press_record
        click_position = local_position if local_position is not None else self.press_local
        if self.system_move_started:
            # The compositor can consume intermediate move events. The release
            # position still distinguishes a drag from a click even before the
            # next Hyprland geometry update arrives.
            self.set_drag_delta(global_position - self.press_global, manual=False)
            self.update_native_drag()
        else:
            self.update_drag_from_global(global_position)
            self.flush_drag_move(force=True)
        was_dragging = self.drag_threshold_crossed
        self.finish_drag()
        if not was_dragging:
            self.activate_press_target(click_record, click_position)

    def end_card_interaction(self, global_position: QPoint) -> None:
        self.end_pointer_interaction(global_position)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            press_global = event.globalPosition().toPoint()
            try:
                press_local = event.position().toPoint()
            except AttributeError:
                press_local = self.mapFromGlobal(press_global)
            self.begin_pointer_interaction(press_global, press_local)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if self.press_global and event.buttons() & Qt.MouseButton.LeftButton:
            self.continue_pointer_interaction(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.press_global:
            release_global = event.globalPosition().toPoint()
            try:
                release_local = event.position().toPoint()
            except AttributeError:
                release_local = self.press_local or self.mapFromGlobal(release_global)
            self.end_pointer_interaction(release_global, release_local)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def check_drag_release(self) -> None:
        if not self.press_global:
            return
        if self.system_move_started:
            # startSystemMove transfers the pointer grab to the Wayland
            # compositor. QApplication.mouseButtons() can consequently report
            # NoButton while the physical button is still held. Never turn that
            # transient state into a synthetic click; the originating QWidget
            # receives the corresponding release event and completes the gesture.
            cursor_delta = self.current_cursor_position() - self.press_global
            self.set_drag_delta(cursor_delta, manual=False)
            return
        cursor = self.current_cursor_position()
        if self.current_mouse_buttons() & Qt.MouseButton.LeftButton:
            self.update_drag_from_global(cursor)
            self.flush_drag_move()
            return
        delta = cursor - self.press_global
        was_dragging = self.drag_threshold_crossed or delta.manhattanLength() >= QApplication.startDragDistance()
        if was_dragging:
            self.update_drag_from_global(cursor)
            self.flush_drag_move(force=True)
        click_position = self.press_local or self.mapFromGlobal(cursor)
        click_record = self.press_record
        self.finish_drag()
        if not was_dragging:
            self.activate_press_target(click_record, click_position)

    def current_cursor_position(self) -> QPoint:
        return QCursor.pos()

    def current_mouse_buttons(self) -> Qt.MouseButton:
        return QApplication.mouseButtons()

    def update_drag_from_global(self, global_position: QPoint) -> None:
        if not self.press_global:
            return
        delta = global_position - self.press_global
        self.set_drag_delta(delta, manual=True)

    def set_drag_delta(self, delta: QPoint, manual: bool) -> None:
        if not self.drag_threshold_crossed and delta.manhattanLength() < QApplication.startDragDistance():
            return
        first_drag_frame = not self.drag_threshold_crossed
        if not self.drag_threshold_crossed:
            self.frame_index = 0
            self.last_frame_change = time.monotonic()
        self.drag_threshold_crossed = True
        next_drag_state = "running-right" if delta.x() >= 0 else "running-left"
        needs_repaint = first_drag_frame or next_drag_state != self.drag_state
        self.drag_state = next_drag_state
        if manual:
            self.pending_drag_delta = delta
        if needs_repaint:
            self.update()

    def update_native_drag(self) -> None:
        if not self.press_window_origin:
            return
        geometry = self.hypr_geometry()
        if not geometry:
            return
        delta = QPoint(geometry[1], geometry[2]) - self.press_window_origin
        self.set_drag_delta(delta, manual=False)

    def flush_drag_move(self, force: bool = False) -> None:
        if self.system_move_started:
            return
        if self.pending_drag_delta is None or not self.press_window_origin:
            return
        target = (
            self.press_window_origin.x() + self.pending_drag_delta.x(),
            self.press_window_origin.y() + self.pending_drag_delta.y(),
        )
        if target == self.last_drag_target:
            return
        self.move_via_hypr(self.pending_drag_delta)
        self.last_drag_target = target

    def finish_drag(self) -> None:
        self.press_global = None
        self.press_local = None
        self.press_record = None
        self.press_started_at = 0.0
        self.press_window_origin = None
        self.fallback_address = None
        self.drag_threshold_crossed = False
        self.system_move_started = False
        self.pending_drag_delta = None
        self.last_drag_target = None
        self.drag_state = None
        self.save_position()
        self.update()

    def moveEvent(self, event):  # type: ignore[override]
        super().moveEvent(event)
        if self.isVisible():
            if self.press_global and self.system_move_started and self.press_window_origin:
                self.set_drag_delta(event.pos() - self.press_window_origin, manual=False)
            elif not self.press_global:
                self.save_position()
            # Expanded cards are children of this surface and follow it without
            # any additional compositor move request.

    def toggle_bubble(self) -> None:
        if self.bubble_expanded:
            self.collapse_bubble()
            return
        if len(self.records) <= 1:
            return
        self.bubble_expanded = True
        self.refresh_previews(force=True)
        self.bubble.show()
        self.bubble.raise_()
        self.place_bubble()

    def collapse_bubble(self) -> None:
        self.bubble_expanded = False
        self.bubble.hide()
        if self.height() != self.base_height:
            self.setFixedHeight(self.base_height)

    def place_bubble(self) -> None:
        if not self.bubble_expanded:
            return
        gap = 5
        self.bubble.move(8, self.base_height + gap)
        expanded_height = self.base_height + gap + self.bubble.height()
        if self.height() != expanded_height:
            self.setFixedHeight(expanded_height)

    def open_task(self, record: dict[str, Any]) -> None:
        focus_record(record)
        self.collapse_bubble()
        self.poll_state()

    def remove_task(self, record: dict[str, Any]) -> None:
        session_id = str(record.get("session_id") or "")
        if not session_id or session_id in self.pending_deletions:
            return
        self.pending_deletions.add(session_id)
        # The delete request originates inside QToolButton.clicked. Rebuilding
        # its card before that signal stack unwinds can leave the child layout at
        # zero height. Perform the state mutation on the next event-loop turn.
        QTimer.singleShot(0, lambda: self.finish_remove_task(session_id))

    def finish_remove_task(self, session_id: str) -> None:
        try:
            dismiss_record(session_id, self.config)
            self.live_cache.pop(session_id, None)
            self.activity_cache.pop(session_id, None)
        finally:
            self.pending_deletions.discard(session_id)
        self.poll_state()

    def move_via_hypr(self, delta: QPoint) -> None:
        if not self.press_window_origin:
            return
        if not self.fallback_address:
            geometry = self.hypr_geometry()
            if not geometry:
                return
            self.fallback_address = geometry[0]
            self.press_window_origin = QPoint(geometry[1], geometry[2])
        x = self.press_window_origin.x() + delta.x()
        y = self.press_window_origin.y() + delta.y()
        command = f"dispatch movewindowpixel exact {x} {y},address:{self.fallback_address}"
        if self.hypr_send(command):
            return
        try:
            subprocess.run(
                ["hyprctl", "dispatch", "movewindowpixel", "exact", str(x), f"{y},address:{self.fallback_address}"],
                capture_output=True,
                text=True,
                timeout=0.4,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def hypr_send(self, command: str, timeout: float = 0.01) -> bool:
        """Send a no-response Hyprland command without stalling the GUI."""
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not signature:
            return False
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        candidates = [
            Path(runtime) / "hypr" / signature / ".socket.sock",
            Path("/tmp/hypr") / signature / ".socket.sock",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(timeout)
                    client.connect(str(path))
                    client.sendall(command.encode("utf-8"))
                    client.shutdown(socket.SHUT_WR)
                return True
            except (OSError, socket.timeout):
                continue
        return False

    def hypr_request(self, command: str, timeout: float = 0.08) -> str | None:
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not signature:
            return None
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        candidates = [
            Path(runtime) / "hypr" / signature / ".socket.sock",
            Path("/tmp/hypr") / signature / ".socket.sock",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(timeout)
                    client.connect(str(path))
                    client.sendall(command.encode("utf-8"))
                    client.shutdown(socket.SHUT_WR)
                    chunks: list[bytes] = []
                    while True:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", "replace")
            except (OSError, socket.timeout):
                continue
        return None

    def save_position(self) -> None:
        if self.press_global:
            return
        path = Path(self.config["position_file"]).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        geometry = self.hypr_geometry()
        x, y = (geometry[1], geometry[2]) if geometry else (self.x(), self.y())
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".position.", suffix=".tmp", dir=path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"x": x, "y": y}, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except OSError:
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def restore_position(self) -> None:
        path = Path(self.config["position_file"]).expanduser()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.restore_target = (int(value["x"]), int(value["y"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            screen = QApplication.primaryScreen()
            area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
            self.restore_target = (area.right() - self.width() - 28, area.bottom() - self.height() - 28)

    def hypr_geometry(self) -> tuple[str, int, int] | None:
        try:
            payload = self.hypr_request("j/clients", 0.15)
            if payload is None:
                result = subprocess.run(
                    ["hyprctl", "-j", "clients"], capture_output=True, text=True, timeout=0.7, check=False
                )
                if result.returncode != 0:
                    return None
                payload = result.stdout
            clients = json.loads(payload)
            for client in clients:
                if int(client.get("pid", -1)) != os.getpid():
                    continue
                address = str(client.get("address", ""))
                at = client.get("at", [])
                if address and isinstance(at, list) and len(at) == 2:
                    return address, int(at[0]), int(at[1])
        except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
            pass
        return None

    def apply_restored_position(self) -> None:
        if not self.restore_target:
            return
        geometry = self.hypr_geometry()
        if not geometry:
            QTimer.singleShot(350, self.apply_restored_position)
            return
        address, _, _ = geometry
        x, y = self.restore_target
        try:
            subprocess.run(
                ["hyprctl", "dispatch", "movewindowpixel", "exact", str(x), f"{y},address:{address}"],
                capture_output=True,
                text=True,
                timeout=0.7,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        self.restore_target = None


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
    app = QApplication(sys.argv)
    app.setApplicationName("Lumi Pet")
    app.setDesktopFileName("lumi-pet")
    app.setQuitOnLastWindowClosed(False)
    try:
        window = LumiWindow(load_config())
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

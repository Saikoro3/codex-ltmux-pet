from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import load_config

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
CONTROL_RE = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~])|[\x00-\x08\x0b-\x1f\x7f]")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def clean_text(value: Any, limit: int = 180, lines: int = 3) -> str:
    if not isinstance(value, str):
        return ""
    value = CONTROL_RE.sub("", value).replace("\r", "\n")
    cleaned = [" ".join(part.split()) for part in value.split("\n")]
    value = "\n".join(part for part in cleaned if part)[-limit:]
    return "\n".join(value.splitlines()[-lines:])


def task_name_from_prompt(value: Any, limit: int = 10) -> str:
    """Return a short title without retaining any recoverable prompt spacing."""
    if not isinstance(value, str):
        return ""
    value = CONTROL_RE.sub("", value)
    compact = "".join(
        character
        for character in value
        if not character.isspace() and not unicodedata.category(character).startswith("C")
    )
    return compact[:limit]


def safe_session_id(value: Any) -> str:
    raw = str(value or "unknown")
    safe = SAFE_ID_RE.sub("_", raw).strip("._")
    return safe[:160] or "unknown"


def state_dir(config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    path = Path(config["state_dir"]).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def record_path(session_id: str, config: dict[str, Any] | None = None) -> Path:
    return state_dir(config) / f"{safe_session_id(session_id)}.json"


def tombstone_path(session_id: str, config: dict[str, Any] | None = None) -> Path:
    return state_dir(config) / f".{safe_session_id(session_id)}.deleted.json"


@contextmanager
def session_lock(session_id: str, config: dict[str, Any] | None = None) -> Iterator[None]:
    directory = state_dir(config)
    lock_path = directory / f".{safe_session_id(session_id)}.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_record(session_id: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        with record_path(session_id, config).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def atomic_write(record: dict[str, Any], config: dict[str, Any] | None = None) -> Path:
    config = config or load_config()
    destination = record_path(str(record["session_id"]), config)
    directory = destination.parent
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def delete_record(session_id: str, config: dict[str, Any] | None = None) -> None:
    try:
        record_path(session_id, config).unlink()
    except FileNotFoundError:
        pass


def read_tombstone(session_id: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        with tombstone_path(session_id, config).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def clear_tombstone(session_id: str, config: dict[str, Any] | None = None) -> None:
    try:
        tombstone_path(session_id, config).unlink()
    except FileNotFoundError:
        pass


def dismiss_record(session_id: str, config: dict[str, Any] | None = None) -> None:
    """Delete a finished card and suppress late terminal events for the same turn."""
    config = config or load_config()
    with session_lock(session_id, config):
        record = read_record(session_id, config)
        if not record or record.get("state") not in {"complete", "failed"}:
            return
        tombstone = {
            "session_id": session_id,
            "turn_id": record.get("turn_id"),
            "deleted_at": utc_now(),
        }
        destination = tombstone_path(session_id, config)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(tombstone, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        delete_record(session_id, config)


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def event_name(payload: dict[str, Any], source: str) -> str:
    if source == "notify":
        return str(_first(payload, "type", "event", "hook_event_name") or "notify")
    return str(_first(payload, "hook_event_name", "hookEventName", "event") or "unknown")


def is_subagent_event(payload: dict[str, Any], source: str = "hook") -> bool:
    """Recognize explicitly scoped child-agent events without guessing from titles."""
    name = event_name(payload, source).casefold()
    if name in {"subagentstart", "subagentstop"}:
        return True
    if _first(
        payload,
        "parent_agent_id",
        "parentAgentId",
        "parent_session_id",
        "parentSessionId",
        "parent_thread_id",
        "parentThreadId",
    ):
        return True
    role = str(_first(payload, "agent_role", "agentRole", "session_role", "sessionRole") or "").casefold()
    origin = str(_first(payload, "agent_source", "agentSource", "session_source", "sessionSource") or "").casefold()
    return role in {"subagent", "child"} or origin in {"subagent", "child-agent", "child_agent"}


def session_id_from(payload: dict[str, Any]) -> str:
    return str(_first(payload, "session_id", "sessionId", "thread_id", "thread-id", "threadId") or "unknown")


def terminal_metadata(previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    kitty_socket = os.environ.get("KITTY_LISTEN_ON") or previous.get("kitty_socket")
    kitty_window = os.environ.get("KITTY_WINDOW_ID") or previous.get("kitty_window_id")
    tmux_pane = os.environ.get("TMUX_PANE") or previous.get("tmux_pane")
    tmux_target = os.environ.get("LUMI_TMUX_TARGET") or previous.get("tmux_target")
    if tmux_pane and not os.environ.get("LUMI_TMUX_TARGET"):
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", tmux_pane, "#{session_name}:#{window_index}.#{pane_index}"],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                tmux_target = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "kitty_socket": kitty_socket,
        "kitty_window_id": str(kitty_window) if kitty_window else None,
        "tmux_pane": tmux_pane,
        "tmux_target": tmux_target,
    }


def _codex_pid(payload: dict[str, Any], previous: dict[str, Any] | None) -> int | None:
    candidate = _first(payload, "codex_pid", "codexPid", "process_id", "processId")
    if candidate in (None, ""):
        candidate = (previous or {}).get("codex_pid")
    if candidate in (None, ""):
        candidate = os.getppid()
    try:
        pid = int(candidate)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def display_task_name(record: dict[str, Any]) -> str:
    if name := clean_text(record.get("task_name"), 80, 1):
        return name
    if title := clean_text(record.get("codex_title"), 80, 1):
        if title.casefold() != getpass.getuser().casefold():
            return title
    if cwd := clean_text(record.get("cwd"), 240, 1):
        project = Path(cwd).name
        if project and project not in {".", "/"}:
            return project
    session_id = str(record.get("session_id") or "")
    return session_id[:8] or "Codex"


def _derive_transition(
    payload: dict[str, Any],
    source: str,
    previous: dict[str, Any] | None,
    config: dict[str, Any],
) -> tuple[str, str]:
    name = event_name(payload, source)
    lowered = name.lower()
    limit = int(config["fallback_chars"])

    if source == "notify":
        if any(word in lowered for word in ("fail", "error", "abort", "cancel")):
            return "failed", clean_text(_first(payload, "message", "last-assistant-message", "last_assistant_message"), limit, 2) or "異常終了しました"
        return "complete", clean_text(_first(payload, "last-assistant-message", "last_assistant_message", "message"), limit, 2) or "作業が完了しました"

    if name == "SessionStart":
        if payload.get("source") == "compact" and previous:
            return str(previous.get("state", "running")), str(previous.get("preview_fallback", ""))
        return "idle", "待機しています"
    if name == "UserPromptSubmit":
        return "running", "作業を始めました"
    if name == "PermissionRequest":
        return "waiting", "承認または入力を待っています"
    if name in {"PreToolUse", "PostToolUse", "PreCompact", "PostCompact"}:
        return "running", "作業を続けています"
    if name == "Stop":
        message = clean_text(payload.get("last_assistant_message"), limit, 2)
        return "complete", message or "作業が完了しました"
    if name == "SessionEnd":
        if previous and previous.get("state") in {"running", "waiting"}:
            return "failed", "セッションが予期せず終了しました"
        if previous and previous.get("state") in {"complete", "failed"}:
            return str(previous["state"]), str(previous.get("preview_fallback", ""))
        return "ended", ""
    return str(previous.get("state", "idle") if previous else "idle"), str(previous.get("preview_fallback", "") if previous else "")


def apply_event(payload: dict[str, Any], source: str = "hook", config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    config = config or load_config()
    if is_subagent_event(payload, source):
        return None
    session_id = session_id_from(payload)
    with session_lock(session_id, config):
        name = event_name(payload, source)
        turn_id = _first(payload, "turn_id", "turnId", "turn-id")
        tombstone = read_tombstone(session_id, config)
        if name == "UserPromptSubmit":
            if not tombstone or not turn_id or tombstone.get("turn_id") != str(turn_id):
                clear_tombstone(session_id, config)
        elif tombstone and (source == "notify" or name in {"Stop", "SessionEnd"}):
            deleted_turn = tombstone.get("turn_id")
            if deleted_turn in (None, "") or not turn_id or deleted_turn == str(turn_id):
                return None
        previous = read_record(session_id, config)
        new_state, fallback = _derive_transition(payload, source, previous, config)
        if new_state == "ended":
            delete_record(session_id, config)
            return None
        if source == "notify" and previous and turn_id and previous.get("turn_id") not in (None, turn_id):
            return previous
        metadata = terminal_metadata(previous)
        now = utc_now()
        state_changed = previous is None or previous.get("state") != new_state
        task_name = (previous or {}).get("task_name")
        task_name_initialized = bool((previous or {}).get("task_name_initialized"))
        if previous and "task_name_initialized" not in previous:
            task_name_initialized = True
        if name == "UserPromptSubmit" and not task_name_initialized:
            task_name = task_name_from_prompt(payload.get("prompt")) or None
            task_name_initialized = True
        title = clean_text(
            _first(payload, "title", "thread_title", "threadTitle", "conversation_title", "conversationTitle"),
            80,
            1,
        ) or (previous or {}).get("codex_title")
        finished_at = None
        if new_state in {"complete", "failed"}:
            if (previous or {}).get("state") in {"complete", "failed"}:
                finished_at = (previous or {}).get("finished_at")
            finished_at = finished_at or now
        waiting_since = None
        if new_state == "waiting":
            waiting_since = (previous or {}).get("waiting_since") if (previous or {}).get("state") == "waiting" else now
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "scope": "main",
            "session_id": session_id,
            "turn_id": str(turn_id) if turn_id else (previous or {}).get("turn_id"),
            "task_name": task_name,
            "task_name_initialized": task_name_initialized,
            "codex_title": title,
            "codex_pid": _codex_pid(payload, previous),
            "state": new_state,
            "last_event": name,
            "cwd": str(_first(payload, "cwd", "working-directory", "working_directory") or (previous or {}).get("cwd") or os.getcwd()),
            "preview_fallback": fallback,
            **metadata,
            "attention_at": now if state_changed else (previous or {}).get("attention_at"),
            "waiting_since": waiting_since,
            "finished_at": finished_at,
            "updated_at": now,
        }
        atomic_write(record, config)
        return record


def acknowledge(session_id: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    config = config or load_config()
    with session_lock(session_id, config):
        record = read_record(session_id, config)
        if not record:
            return None
        if record.get("state") == "complete":
            record["state"] = "idle"
            record["preview_fallback"] = "確認済み"
            record["updated_at"] = utc_now()
            atomic_write(record, config)
        return record


def iter_records(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or load_config()
    records: list[dict[str, Any]] = []
    for path in state_dir(config).glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
            if isinstance(record, dict) and record.get("schema_version") in SUPPORTED_SCHEMA_VERSIONS and record.get("session_id"):
                records.append(record)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return records

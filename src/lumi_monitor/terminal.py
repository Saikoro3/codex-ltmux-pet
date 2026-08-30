from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .state import clean_text

KITTY_PID_RE = re.compile(r"kitty-ai-(\d+)(?:$|[^0-9])")
DISPLAY_NOISE_RE = re.compile(r"\breasoning\b", re.IGNORECASE)
HOME_USER_RE = re.compile(re.escape(str(Path.home())) + r"(?=/|$)")
USER_NAME_RE = re.compile(rf"(?<![\w-]){re.escape(getpass.getuser())}(?![\w-])", re.IGNORECASE)
RUNNING_MARKER_RE = re.compile(
    r"(?im)^\s*[•●]\s*(?:working|running|searching|thinking|generating)\b|^\s*[•●]\s*(?:作業中|実行中|検索中)",
)
WAITING_MARKER_RE = re.compile(
    r"(?im)do you want to (?:allow|proceed|run)|would you like to|approval required|permission required|"
    r"press enter to confirm|esc to cancel|許可しますか|承認しますか|実行しますか|入力してください|選択してください",
)


def _run(args: list[str], timeout: float = 0.7) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def tmux_exists(pane: str | None) -> bool:
    if not pane:
        return False
    result = _run(["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"])
    return bool(result and result.returncode == 0 and result.stdout.strip() == pane)


def kitty_exists(socket: str | None, window_id: str | None) -> bool:
    if not socket or not window_id:
        return False
    result = _run(["kitty", "@", "--to", socket, "ls"])
    if not result or result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    wanted = str(window_id)
    return any(str(window.get("id")) == wanted for os_window in payload for tab in os_window.get("tabs", []) for window in tab.get("windows", []))


def endpoint_alive(record: dict[str, Any]) -> bool | None:
    checks: list[bool] = []
    pid = record.get("codex_pid")
    if pid not in (None, ""):
        try:
            os.kill(int(pid), 0)
            checks.append(True)
        except ProcessLookupError:
            checks.append(False)
        except (PermissionError, TypeError, ValueError):
            pass
    pane = record.get("tmux_pane")
    socket = record.get("kitty_socket")
    window = record.get("kitty_window_id")
    if pane:
        checks.append(tmux_exists(str(pane)))
    if socket and window:
        checks.append(kitty_exists(str(socket), str(window)))
    if any(checks):
        return True
    return False if checks else None


def endpoint_label(record: dict[str, Any]) -> str:
    if target := record.get("tmux_target") or record.get("tmux_pane"):
        return f"tmux {target}"
    if window := record.get("kitty_window_id"):
        return f"Kitty #{window}"
    return ""


def terminal_screen(record: dict[str, Any], scrollback_lines: int = 60) -> str | None:
    pane = record.get("tmux_pane")
    if pane:
        result = _run(["tmux", "capture-pane", "-p", "-J", "-t", str(pane), "-S", f"-{scrollback_lines}"], 1.0)
        if result and result.returncode == 0:
            return result.stdout
    socket = record.get("kitty_socket")
    window = record.get("kitty_window_id")
    if socket and window:
        result = _run(["kitty", "@", "--to", str(socket), "get-text", "--match", f"id:{window}", "--extent", "screen"], 1.0)
        if result and result.returncode == 0:
            return result.stdout
    return None


def terminal_activity(record: dict[str, Any]) -> str | None:
    """Return the most recent visible Codex TUI activity marker."""
    screen = terminal_screen(record, 20)
    if not screen:
        return None
    tail = "\n".join(screen.splitlines()[-24:])
    markers: list[tuple[int, str]] = []
    markers.extend((match.start(), "running") for match in RUNNING_MARKER_RE.finditer(tail))
    markers.extend((match.start(), "waiting") for match in WAITING_MARKER_RE.finditer(tail))
    return max(markers, default=(-1, None), key=lambda item: item[0])[1]


def capture_preview(record: dict[str, Any], lines: int = 3, chars: int = 180) -> str:
    def for_display(value: Any) -> str:
        visible: list[str] = []
        for line in str(value or "").splitlines():
            if DISPLAY_NOISE_RE.search(line):
                continue
            line = HOME_USER_RE.sub("~", line)
            line = USER_NAME_RE.sub("", line)
            if line.strip():
                visible.append(line)
        return clean_text("\n".join(visible), chars, lines)

    if screen := terminal_screen(record):
        return for_display(screen)
    return for_display(record.get("preview_fallback", ""))


def kitty_pid(socket: str | None) -> str | None:
    match = KITTY_PID_RE.search(socket or "")
    return match.group(1) if match else None


def _process_ancestors(pid: int) -> list[int]:
    ancestors: list[int] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        ancestors.append(pid)
        try:
            status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
        except OSError:
            break
        parent = next((line.split()[1] for line in status.splitlines() if line.startswith("PPid:")), "0")
        try:
            pid = int(parent)
        except ValueError:
            break
    return ancestors


def _hypr_clients() -> list[dict[str, Any]]:
    result = _run(["hyprctl", "-j", "clients"], 1.0)
    if not result or result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _focus_hypr_process(pid: int | str | None) -> bool:
    try:
        ancestors = _process_ancestors(int(pid))
    except (TypeError, ValueError):
        return False
    clients = _hypr_clients()
    for candidate in ancestors:
        client = next((item for item in clients if item.get("pid") == candidate), None)
        if not client:
            continue
        address = str(client.get("address") or "")
        match = f"address:{address}" if address else f"pid:{candidate}"
        result = _run(["hyprctl", "dispatch", "focuswindow", match], 1.0)
        return bool(result and result.returncode == 0)
    return False


def _tmux_client_pids(pane: str) -> list[str]:
    session = _run(["tmux", "display-message", "-p", "-t", pane, "#{session_name}"], 0.7)
    if not session or session.returncode != 0 or not session.stdout.strip():
        return []
    clients = _run(["tmux", "list-clients", "-t", session.stdout.strip(), "-F", "#{client_pid}"], 0.7)
    if not clients or clients.returncode != 0:
        return []
    return [line.strip() for line in clients.stdout.splitlines() if line.strip().isdigit()]


def focus_record(record: dict[str, Any]) -> bool:
    ok = False
    socket = record.get("kitty_socket")
    window = record.get("kitty_window_id")
    if socket and window:
        result = _run(["kitty", "@", "--to", str(socket), "focus-window", "--match", f"id:{window}"], 1.0)
        ok = bool(result and result.returncode == 0)
        if pid := kitty_pid(str(socket)):
            ok = _focus_hypr_process(pid) or ok
    pane = record.get("tmux_pane")
    if pane and tmux_exists(str(pane)):
        target = _run(["tmux", "display-message", "-p", "-t", str(pane), "#{session_name}:#{window_index}"])
        if target and target.returncode == 0 and target.stdout.strip():
            _run(["tmux", "select-window", "-t", target.stdout.strip()])
        selected = _run(["tmux", "select-pane", "-t", str(pane)])
        ok = bool(selected and selected.returncode == 0) or ok
        for pid in _tmux_client_pids(str(pane)):
            if _focus_hypr_process(pid):
                ok = True
                break
    return ok

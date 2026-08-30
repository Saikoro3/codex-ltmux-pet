from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .atlas import validate_atlas
from .config import APP_ID, bundled_asset

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)
MANAGED_HEADER = "Managed by codex-ltmux-pet"


@dataclass(frozen=True)
class InstallPaths:
    home: Path
    config_home: Path
    data_home: Path
    state_home: Path
    codex_home: Path

    @classmethod
    def from_environment(cls) -> InstallPaths:
        home = Path(os.environ.get("LUMI_HOME", Path.home())).expanduser()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")).expanduser()
        data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")).expanduser()
        state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state")).expanduser()
        codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
        return cls(home, config_home, data_home, state_home, codex_home)

    @property
    def bin_dir(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def app_config_dir(self) -> Path:
        return self.config_home / APP_ID

    @property
    def app_state_dir(self) -> Path:
        return self.state_home / APP_ID

    @property
    def install_root(self) -> Path:
        return self.data_home / APP_ID

    @property
    def service_file(self) -> Path:
        return self.config_home / "systemd" / "user" / "lumi-pet.service"

    @property
    def desktop_file(self) -> Path:
        return self.data_home / "applications" / "lumi-pet.desktop"

    @property
    def icon_file(self) -> Path:
        return self.data_home / "icons" / "hicolor" / "192x192" / "apps" / "codex-ltmux-pet.png"

    @property
    def hooks_file(self) -> Path:
        return self.codex_home / "hooks.json"

    @property
    def pet_dir(self) -> Path:
        return self.codex_home / "pets" / "lumi"

    @property
    def hypr_dir(self) -> Path:
        return self.config_home / "hypr"

    @property
    def hypr_main(self) -> Path:
        override = os.environ.get("LUMI_HYPR_CONFIG")
        return Path(override).expanduser() if override else self.hypr_dir / "hyprland.conf"

    @property
    def hypr_snippet(self) -> Path:
        return self.hypr_dir / "codex-ltmux-pet.conf"

    @property
    def bridge_command(self) -> Path:
        return self.bin_dir / "lumi-state-bridge"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_directory(path: Path, mode: int = 0o755) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    try:
        path.chmod(mode)
    except OSError:
        pass


def _backup(path: Path) -> Path | None:
    if not path.exists() or path.is_symlink():
        return None
    candidate = path.with_name(f"{path.name}.backup-{_timestamp()}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup-{_timestamp()}-{index}")
        index += 1
    shutil.copy2(path, candidate)
    return candidate


def _atomic_write(path: Path, content: str | bytes, mode: int, *, backup: bool = False) -> bool:
    encoded = content.encode("utf-8") if isinstance(content, str) else content
    try:
        if path.read_bytes() == encoded:
            try:
                path.chmod(mode)
            except OSError:
                pass
            return False
    except OSError:
        pass
    _ensure_directory(path.parent, 0o700 if path.name in {"config.json", "hooks.json"} else 0o755)
    if backup:
        _backup(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"description": "User-level Codex lifecycle hooks", "hooks": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"cannot safely update invalid JSON: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"cannot safely update non-object JSON: {path}")
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"cannot safely update non-object hooks table: {path}")
    return payload


def _lumi_commands(paths: InstallPaths) -> set[str]:
    return {
        str(paths.bridge_command),
        str(paths.home / ".local" / "bin" / "lumi-state-bridge"),
    }


def _strip_lumi_from_groups(groups: Any, commands: set[str]) -> list[Any]:
    if not isinstance(groups, list):
        raise RuntimeError("cannot safely update a non-list Codex hook event")
    retained: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            retained.append(group)
            continue
        copied = dict(group)
        copied["hooks"] = [
            hook
            for hook in group["hooks"]
            if not (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and str(hook.get("command") or "") in commands
            )
        ]
        if copied["hooks"]:
            retained.append(copied)
    return retained


def merge_codex_hooks(paths: InstallPaths) -> bool:
    payload = _read_hooks(paths.hooks_file)
    hooks = payload["hooks"]
    commands = _lumi_commands(paths)
    for event in HOOK_EVENTS:
        groups = _strip_lumi_from_groups(hooks.get(event, []), commands)
        groups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": str(paths.bridge_command),
                        "timeout": 2,
                    }
                ]
            }
        )
        hooks[event] = groups
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return _atomic_write(paths.hooks_file, rendered, 0o600, backup=True)


def remove_codex_hooks(paths: InstallPaths) -> bool:
    if not paths.hooks_file.exists():
        return False
    payload = _read_hooks(paths.hooks_file)
    hooks = payload["hooks"]
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        stripped = _strip_lumi_from_groups(groups, _lumi_commands(paths))
        if stripped != groups:
            changed = True
        if stripped:
            hooks[event] = stripped
        elif event in HOOK_EVENTS:
            hooks.pop(event, None)
    if not changed:
        return False
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return _atomic_write(paths.hooks_file, rendered, 0o600, backup=True)


def _service_text() -> str:
    return f"""# {MANAGED_HEADER}
[Unit]
Description=Lumi Codex task monitor pet
After=graphical-session.target
PartOf=graphical-session.target
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
Type=simple
ExecStart=%h/.local/bin/lumi-pet
Restart=on-failure
RestartSec=2
TimeoutStopSec=5
Environment=PYTHONUNBUFFERED=1
Environment=QT_QPA_PLATFORM=wayland

[Install]
WantedBy=default.target
"""


def _desktop_text(paths: InstallPaths) -> str:
    return f"""# {MANAGED_HEADER}
[Desktop Entry]
Type=Application
Name=Lumi Codex Pet
Comment=Compact Codex task monitor for Hyprland
Exec={paths.bin_dir / 'lumi-pet'}
TryExec={paths.bin_dir / 'lumi-pet'}
Icon=codex-ltmux-pet
Terminal=false
Categories=Utility;Development;
StartupWMClass=lumi-pet
Keywords=Codex;Hyprland;Kitty;tmux;
"""


def _hypr_text() -> str:
    return f"""# {MANAGED_HEADER}
# One pinned Wayland surface contains both Lumi and its expanded task cards.
windowrule {{
    name = codex-ltmux-pet
    match:class = ^(lumi-pet|LumiPet)$
    float = true
    pin = true
    no_shadow = true
    no_blur = true
    render_unfocused = true
}}

exec-once = systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
exec-once = systemctl --user start lumi-pet.service
"""


def _ensure_hypr_source(paths: InstallPaths) -> bool:
    if not paths.hypr_main.exists():
        return False
    source_line = f"source = {paths.hypr_snippet}"
    original = paths.hypr_main.read_text(encoding="utf-8")
    if any(line.strip() == source_line for line in original.splitlines()):
        return False
    suffix = "" if original.endswith("\n") else "\n"
    content = f"{original}{suffix}\n# {MANAGED_HEADER}\n{source_line}\n"
    return _atomic_write(paths.hypr_main, content, stat.S_IMODE(paths.hypr_main.stat().st_mode), backup=True)


def _remove_hypr_source(paths: InstallPaths) -> bool:
    if not paths.hypr_main.exists():
        return False
    source_line = f"source = {paths.hypr_snippet}"
    lines = paths.hypr_main.read_text(encoding="utf-8").splitlines()
    filtered: list[str] = []
    changed = False
    for line in lines:
        if line.strip() == source_line:
            changed = True
            if filtered and filtered[-1].strip() == f"# {MANAGED_HEADER}":
                filtered.pop()
            continue
        filtered.append(line)
    if not changed:
        return False
    return _atomic_write(
        paths.hypr_main,
        "\n".join(filtered).rstrip() + "\n",
        stat.S_IMODE(paths.hypr_main.stat().st_mode),
        backup=True,
    )


def _copy_pet(paths: InstallPaths) -> None:
    _ensure_directory(paths.pet_dir, 0o755)
    for name in ("pet.json", "spritesheet.webp"):
        source = bundled_asset("assets", "lumi", name)
        destination = paths.pet_dir / name
        _atomic_write(destination, source.read_bytes(), 0o644, backup=destination.exists())


def _create_icon(paths: InstallPaths) -> None:
    from PyQt6.QtGui import QImage

    atlas = QImage(str(bundled_asset("assets", "lumi", "spritesheet.webp")))
    if atlas.isNull() or atlas.width() != 1536 or atlas.height() != 2288:
        raise RuntimeError("bundled Lumi atlas is not a valid 1536x2288 v2 atlas")
    _ensure_directory(paths.icon_file.parent, 0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-ltmux-pet.", suffix=".png", dir=paths.icon_file.parent)
    os.close(descriptor)
    try:
        if not atlas.copy(0, 0, 192, 208).save(temporary, "PNG"):
            raise RuntimeError("could not export the Lumi desktop icon")
        data = Path(temporary).read_bytes()
        _atomic_write(paths.icon_file, data, 0o644)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _migrate_legacy_state(paths: InstallPaths) -> list[str]:
    migrated: list[str] = []
    legacy = paths.state_home / "lumi-monitor"
    _ensure_directory(paths.app_state_dir, 0o700)
    sessions = paths.app_state_dir / "sessions"
    _ensure_directory(sessions, 0o700)
    legacy_position = legacy / "position.json"
    current_position = paths.app_state_dir / "position.json"
    if legacy_position.is_file() and not current_position.exists():
        _atomic_write(current_position, legacy_position.read_bytes(), 0o600)
        migrated.append("window position")
    legacy_sessions = legacy / "sessions"
    if legacy_sessions.is_dir():
        count = 0
        for source in legacy_sessions.glob("*.json"):
            destination = sessions / source.name
            if destination.exists():
                continue
            _atomic_write(destination, source.read_bytes(), 0o600)
            count += 1
        if count:
            migrated.append(f"{count} session records")
    return migrated


def _write_user_config(paths: InstallPaths) -> None:
    _ensure_directory(paths.app_config_dir, 0o700)
    config = paths.app_config_dir / "config.json"
    if not config.exists():
        _atomic_write(config, '{"schema_version":2}\n', 0o600)


def _run(command: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _systemd_setup(start: bool) -> list[str]:
    notes: list[str] = []
    result = _run(["systemctl", "--user", "daemon-reload"])
    if not result or result.returncode != 0:
        notes.append("systemd user manager is unavailable; enable lumi-pet.service after login")
        return notes
    enabled = _run(["systemctl", "--user", "enable", "lumi-pet.service"])
    if not enabled or enabled.returncode != 0:
        notes.append("could not enable lumi-pet.service")
    if start:
        restarted = _run(["systemctl", "--user", "restart", "lumi-pet.service"])
        if not restarted or restarted.returncode != 0:
            detail = (restarted.stderr if restarted else "").strip()
            notes.append(f"could not start lumi-pet.service{': ' + detail if detail else ''}")
    return notes


def setup(paths: InstallPaths, *, start: bool = True, dry_run: bool = False) -> int:
    if dry_run:
        print(json.dumps({
            "config": str(paths.app_config_dir),
            "state": str(paths.app_state_dir),
            "hooks": str(paths.hooks_file),
            "service": str(paths.service_file),
            "hyprland": str(paths.hypr_snippet),
            "pet": str(paths.pet_dir),
        }, indent=2))
        return 0

    notes: list[str] = []
    migrated = _migrate_legacy_state(paths)
    _write_user_config(paths)
    _copy_pet(paths)
    _create_icon(paths)
    _atomic_write(paths.service_file, _service_text(), 0o644, backup=True)
    _atomic_write(paths.desktop_file, _desktop_text(paths), 0o644, backup=True)
    _atomic_write(paths.hypr_snippet, _hypr_text(), 0o644, backup=True)
    if paths.hypr_main.exists():
        _ensure_hypr_source(paths)
    else:
        notes.append(f"Hyprland config not found at {paths.hypr_main}; source {paths.hypr_snippet} manually")
    merge_codex_hooks(paths)
    notes.extend(_systemd_setup(start))
    if shutil.which("hyprctl") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        _run(["hyprctl", "reload"], 2.0)

    print(f"Lumi Codex Pet {__version__} is configured.")
    if migrated:
        print(f"Migrated: {', '.join(migrated)}.")
    for note in notes:
        print(f"Warning: {note}", file=sys.stderr)
    print("Open /hooks in Codex, review the Lumi command hooks, and trust them before use.")
    return 0


def _managed_text_file(path: Path) -> bool:
    try:
        return MANAGED_HEADER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _unlink_managed(path: Path) -> None:
    if _managed_text_file(path):
        path.unlink(missing_ok=True)


def _remove_pet_if_unmodified(paths: InstallPaths) -> bool:
    for name in ("pet.json", "spritesheet.webp"):
        installed = paths.pet_dir / name
        bundled = bundled_asset("assets", "lumi", name)
        if not installed.is_file() or _sha256(installed) != _sha256(bundled):
            return False
    for name in ("pet.json", "spritesheet.webp"):
        (paths.pet_dir / name).unlink(missing_ok=True)
    try:
        paths.pet_dir.rmdir()
    except OSError:
        pass
    return True


def _remove_wrappers(paths: InstallPaths) -> None:
    expected = paths.install_root / "venv" / "bin"
    for name in ("lumi-pet", "lumi-state-bridge", "lumi-ctl"):
        wrapper = paths.bin_dir / name
        if not wrapper.is_symlink():
            continue
        try:
            target = wrapper.resolve(strict=False)
        except OSError:
            continue
        if target.parent == expected:
            wrapper.unlink(missing_ok=True)


def uninstall(paths: InstallPaths, *, purge: bool = False) -> int:
    _run(["systemctl", "--user", "disable", "--now", "lumi-pet.service"])
    remove_codex_hooks(paths)
    _remove_hypr_source(paths)
    _unlink_managed(paths.service_file)
    _unlink_managed(paths.desktop_file)
    _unlink_managed(paths.hypr_snippet)
    try:
        if paths.icon_file.is_file():
            paths.icon_file.unlink()
    except OSError:
        pass
    _remove_wrappers(paths)
    _run(["systemctl", "--user", "daemon-reload"])

    if purge:
        removed_pet = _remove_pet_if_unmodified(paths)
        shutil.rmtree(paths.app_config_dir, ignore_errors=True)
        shutil.rmtree(paths.app_state_dir, ignore_errors=True)
        if not removed_pet:
            print("Kept the Lumi pet because its installed files were modified.", file=sys.stderr)

    marker = paths.install_root / ".lumi-managed"
    if marker.is_file():
        shutil.rmtree(paths.install_root, ignore_errors=True)
    print("Lumi Codex Pet was uninstalled." + (" User data was purged." if purge else " User data was kept."))
    return 0


def _hook_is_installed(paths: InstallPaths) -> bool:
    try:
        payload = _read_hooks(paths.hooks_file)
    except RuntimeError:
        return False
    for event in HOOK_EVENTS:
        groups = payload["hooks"].get(event, [])
        if not any(
            isinstance(hook, dict) and hook.get("command") == str(paths.bridge_command)
            for group in groups if isinstance(group, dict)
            for hook in group.get("hooks", []) if isinstance(group.get("hooks"), list)
        ):
            return False
    return True


def _atlas_check() -> tuple[bool, str]:
    try:
        manifest = json.loads(bundled_asset("assets", "lumi", "pet.json").read_text(encoding="utf-8"))
        report = validate_atlas(bundled_asset("assets", "lumi", "spritesheet.webp"))
        ok = (
            manifest.get("id") == "lumi"
            and manifest.get("spriteVersionNumber") == 2
            and manifest.get("spritesheetPath") == "spritesheet.webp"
            and report["ok"]
        )
        detail = f"{report.get('width', '?')}x{report.get('height', '?')}, v{manifest.get('spriteVersionNumber')}"
        if report.get("errors"):
            detail += f", {report['errors'][0]}"
        return ok, detail
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return False, str(error)


def doctor(paths: InstallPaths, *, as_json: bool = False) -> int:
    atlas_ok, atlas_detail = _atlas_check()
    checks = [
        {"name": "python", "ok": sys.version_info >= (3, 11), "required": True, "detail": sys.executable},
        {"name": "pyqt6", "ok": _module_available("PyQt6"), "required": True, "detail": "PyQt6 >= 6.6"},
        {"name": "lumi-atlas", "ok": atlas_ok, "required": True, "detail": atlas_detail},
        {"name": "codex-hooks", "ok": _hook_is_installed(paths), "required": True, "detail": str(paths.hooks_file)},
        {"name": "systemd-service", "ok": paths.service_file.is_file(), "required": True, "detail": str(paths.service_file)},
        {"name": "hyprland-session", "ok": bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")), "required": False, "detail": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "not active")},
        {"name": "terminal", "ok": bool(shutil.which("kitty") or shutil.which("tmux")), "required": False, "detail": "Kitty and/or tmux"},
    ]
    ok = all(item["ok"] for item in checks if item["required"])
    if as_json:
        print(json.dumps({"ok": ok, "version": __version__, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            marker = "OK" if item["ok"] else ("FAIL" if item["required"] else "WARN")
            print(f"[{marker}] {item['name']}: {item['detail']}")
        print("Hook trust cannot be checked automatically; review Lumi with /hooks in Codex.")
    return 0 if ok else 1


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumi-ctl", description="Install and diagnose Lumi Codex Pet")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup_parser = subparsers.add_parser("setup", help="Install per-user desktop integration and Codex hooks")
    setup_parser.add_argument("--no-start", action="store_true", help="Do not start the systemd user service")
    setup_parser.add_argument("--dry-run", action="store_true", help="Print target paths without changing files")
    doctor_parser = subparsers.add_parser("doctor", help="Check dependencies and integration")
    doctor_parser.add_argument("--json", action="store_true", help="Emit machine-readable diagnostics")
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove only files managed by Lumi")
    uninstall_parser.add_argument("--purge", action="store_true", help="Also remove Lumi config, state, and unmodified pet files")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = InstallPaths.from_environment()
    try:
        if args.command == "setup":
            return setup(paths, start=not args.no_start, dry_run=args.dry_run)
        if args.command == "doctor":
            return doctor(paths, as_json=args.json)
        if args.command == "uninstall":
            return uninstall(paths, purge=args.purge)
    except (OSError, RuntimeError) as error:
        print(f"lumi-ctl: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

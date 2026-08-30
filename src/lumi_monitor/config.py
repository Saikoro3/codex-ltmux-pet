from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
from typing import Any

APP_ID = "codex-ltmux-pet"
LEGACY_APP_ID = "lumi-monitor"


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


def config_home() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")


def state_home() -> Path:
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")


def data_home() -> Path:
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def user_config_path() -> Path:
    return config_home() / APP_ID / "config.json"


def bundled_asset(*parts: str) -> Path:
    return Path(str(files("lumi_monitor").joinpath(*parts)))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    with bundled_asset("default_config.json").open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    configured_path = os.environ.get("LUMI_CONFIG")
    path = Path(configured_path).expanduser() if configured_path else user_config_path()
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            override = json.load(handle)
        if isinstance(override, dict):
            config = _merge(config, override)

    runtime_root = state_home() / APP_ID
    installed_pet = codex_home() / "pets" / "lumi" / "spritesheet.webp"
    config.setdefault("position_file", str(runtime_root / "position.json"))
    config.setdefault("state_dir", str(runtime_root / "sessions"))
    config.setdefault(
        "spritesheet",
        str(installed_pet if installed_pet.is_file() else bundled_asset("assets", "lumi", "spritesheet.webp")),
    )
    if override := os.environ.get("LUMI_STATE_DIR"):
        config["state_dir"] = override
    if override := os.environ.get("LUMI_SPRITESHEET"):
        config["spritesheet"] = override
    if override := os.environ.get("LUMI_POSITION_FILE"):
        config["position_file"] = override
    return config

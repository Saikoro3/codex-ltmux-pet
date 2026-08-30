from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import load_config
from .state import acknowledge, apply_event


def read_payload(argument: str | None) -> tuple[dict[str, Any], str]:
    source = "notify" if argument is not None else "hook"
    raw = argument if argument is not None else sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}, source
    return (payload if isinstance(payload, dict) else {}), source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Codex lifecycle state for Lumi")
    parser.add_argument("payload", nargs="?", help="Codex notify JSON payload")
    parser.add_argument("--ack", metavar="SESSION_ID", help="Mark a completed task as read")
    args = parser.parse_args(argv)
    config = load_config()
    if args.ack:
        acknowledge(args.ack, config)
        return 0
    payload, source = read_payload(args.payload)
    if not payload:
        return 0
    try:
        apply_event(payload, source, config)
    except OSError:
        return 0
    # Stop hooks require JSON on stdout. An empty object is advisory and does
    # not alter Codex behavior; other events also accept it safely.
    if payload.get("hook_event_name") == "Stop":
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

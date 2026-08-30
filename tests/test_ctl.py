from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lumi_monitor.atlas import validate_atlas
from lumi_monitor.config import bundled_asset
from lumi_monitor.ctl import HOOK_EVENTS, InstallPaths, merge_codex_hooks, remove_codex_hooks, setup


class ControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = InstallPaths(
            home=root / "home",
            config_home=root / "config",
            data_home=root / "data",
            state_home=root / "state",
            codex_home=root / "codex",
        )
        self.paths.hooks_file.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _hook_commands(self, event: str) -> list[str]:
        payload = json.loads(self.paths.hooks_file.read_text(encoding="utf-8"))
        return [
            str(hook.get("command"))
            for group in payload["hooks"].get(event, [])
            if isinstance(group, dict)
            for hook in group.get("hooks", [])
            if isinstance(hook, dict)
        ]

    def test_hook_merge_is_idempotent_and_preserves_unrelated_hooks(self) -> None:
        original = {
            "description": "mine",
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/my-hook", "timeout": 4}]}],
            },
        }
        self.paths.hooks_file.write_text(json.dumps(original), encoding="utf-8")
        merge_codex_hooks(self.paths)
        merge_codex_hooks(self.paths)
        for event in HOOK_EVENTS:
            commands = self._hook_commands(event)
            self.assertEqual(commands.count(str(self.paths.bridge_command)), 1)
        self.assertIn("/usr/bin/my-hook", self._hook_commands("Stop"))
        self.assertEqual(stat.S_IMODE(self.paths.hooks_file.stat().st_mode), 0o600)
        self.assertTrue(list(self.paths.hooks_file.parent.glob("hooks.json.backup-*")))

    def test_hook_uninstall_removes_only_lumi(self) -> None:
        self.paths.hooks_file.write_text('{"hooks":{}}\n', encoding="utf-8")
        merge_codex_hooks(self.paths)
        payload = json.loads(self.paths.hooks_file.read_text(encoding="utf-8"))
        payload["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": "/usr/bin/keep-me", "timeout": 1}]}
        )
        self.paths.hooks_file.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(remove_codex_hooks(self.paths))
        self.assertEqual(self._hook_commands("Stop"), ["/usr/bin/keep-me"])

    def test_setup_is_idempotent_private_and_portable(self) -> None:
        self.paths.hypr_main.parent.mkdir(parents=True)
        self.paths.hypr_main.write_text("$mainMod = SUPER\n", encoding="utf-8")
        with patch("lumi_monitor.ctl._systemd_setup", return_value=[]), patch("lumi_monitor.ctl._run"):
            self.assertEqual(setup(self.paths, start=False), 0)
            self.assertEqual(setup(self.paths, start=False), 0)
        source_line = f"source = {self.paths.hypr_snippet}"
        self.assertEqual(self.paths.hypr_main.read_text(encoding="utf-8").count(source_line), 1)
        self.assertEqual(stat.S_IMODE(self.paths.app_config_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.paths.app_config_dir / "config.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.paths.app_state_dir / "sessions").stat().st_mode), 0o700)
        rendered = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.paths.service_file, self.paths.desktop_file, self.paths.hypr_snippet)
        )
        self.assertNotIn("/home/raio", rendered)

    def test_bundled_v2_atlas_structure(self) -> None:
        report = validate_atlas(bundled_asset("assets", "lumi", "spritesheet.webp"))
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual((report["width"], report["height"]), (1536, 2288))


if __name__ == "__main__":
    unittest.main()

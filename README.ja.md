# Lumi Codex Pet

[English](README.md)

Lumiは、Hyprland上で動作する小さなCodexタスクペットです。普段は完全に
静止し、タスクの状態が実際に変化したときだけ3秒間アニメーションします。
Kittyまたはtmuxで動いているメインのCodexセッションを、小さなカードとして
まとめて表示します。

## 主な機能

- Subagentを除外し、メインのCodexセッションだけを表示
- `running`・`waiting`と、直近1時間の`complete`・`failed`を表示
- 常時表示カードの直下へ、最新メッセージ順に古いカードを展開
- カードのクリックで対応するKittyウィンドウまたはtmuxペインへ移動
- 一覧を開いたままでも、Lumiまたはカードからドラッグ移動可能
- 依頼文は保存せず、空白を除いた先頭10 Unicode文字だけをタスク名に使用
- ターミナル画面は一覧を開いている間だけ取得し、ディスクへ保存しない

## 必要環境

- Hyprlandとsystemdユーザーセッション
- Python 3.11以上
- インストール用の`curl`と`sha256sum`
- Kittyまたはtmux
- Hooksが有効なCodex

Kittyのプレビューとウィンドウ移動を使う場合は、`kitty.conf`でリモート操作を
有効にします。

```conf
allow_remote_control yes
listen_on unix:@kitty-ai-${kitty_pid}
```

## インストール

```bash
curl -fsSL https://github.com/Saikoro3/codex-ltmux-pet/releases/latest/download/install.sh | bash
```

インストーラーはPythonのバージョンとRelease wheelのチェックサムを検証し、
ユーザー専用venvへ導入します。既存の`~/.codex/hooks.json`は上書きせず、
LumiのHookだけを安全にマージします。

導入後は次を行ってください。

1. Codexで`/hooks`を開く。
2. `~/.local/bin/lumi-state-bridge`を実行するHookを確認して信頼する。
3. `lumi-ctl doctor`を実行する。

詳細は[Codex Hooks公式ドキュメント](https://learn.chatgpt.com/docs/hooks)を参照してください。

## 操作

- Lumiをクリック：カード一覧を開閉
- 常時表示中の先頭カードをクリック：対象ターミナルへ移動
- 一覧カードをクリック：対象ターミナルへ移動
- Lumiまたは一覧カードをドラッグ：ウィンドウ全体を移動
- 完了・失敗カードの`×`：そのカードだけを削除

短いクリックとドラッグはQtの標準ドラッグ距離で区別します。一覧もLumi本体と
同じWaylandサーフェス内にあるため、別の位置へ飛びません。

## 設定とプライバシー

ユーザー設定：

```text
${XDG_CONFIG_HOME:-~/.config}/codex-ltmux-pet/config.json
```

状態保存先：

```text
${XDG_STATE_HOME:-~/.local/state}/codex-ltmux-pet/
```

状態ディレクトリは0700、状態ファイルは0600で作成します。依頼全文や取得した
ターミナル画面は保存しません。

設定例：

```json
{
  "attention_seconds": 3,
  "finished_retention_seconds": 3600,
  "sprite_width_px": 158
}
```

## 更新・削除

更新はインストールコマンドを再実行します。再実行してもHookやHyprlandの設定は
重複しません。

```bash
lumi-ctl uninstall
```

設定と状態も削除する場合：

```bash
lumi-ctl uninstall --purge
```

他のCodex HooksやHyprland設定は削除しません。インストール後に変更されたLumi
画像も保護します。

## トラブルシューティング

```bash
lumi-ctl doctor
systemctl --user status lumi-pet.service
journalctl --user -u lumi-pet.service -n 100
```

タスクが反映されない場合は、Codexの`/hooks`でLumiのHookが信頼済みか確認して
ください。ターミナルへ移動できない場合は、Codexを起動したターミナル内に
`KITTY_LISTEN_ON`または`TMUX_PANE`があるか確認します。

`load_workspace_dependencies`は利用者が実行するものではありません。Lumi画像の
制作時にCodex内部で使う検証用ヘルパーです。公開版はインストーラーと
`lumi-ctl doctor`がPythonと画像を検証します。

## 開発

```bash
git clone https://github.com/Saikoro3/codex-ltmux-pet.git
cd codex-ltmux-pet
uv sync --extra dev
QT_QPA_PLATFORM=offscreen uv run python -m unittest discover -s tests -v
uv run ruff check .
uv build
```

## ライセンス

コードは[MIT License](LICENSE)、Lumi画像は[CC BY 4.0](LICENSES/CC-BY-4.0.txt)です。
帰属表示は[NOTICE.md](NOTICE.md)を参照してください。

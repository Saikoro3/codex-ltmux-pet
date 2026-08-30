#!/bin/sh
set -eu

VERSION="0.1.0"
REPOSITORY="Saikoro3/codex-ltmux-pet"
WHEEL="codex_ltmux_pet-${VERSION}-py3-none-any.whl"

if [ "${LUMI_INSTALL_DRY_RUN:-0}" = "1" ]; then
    printf '%s\n' "Lumi Codex Pet installer dry run"
    printf 'version=%s\nrepository=%s\nwheel=%s\n' "$VERSION" "$REPOSITORY" "$WHEEL"
    exit 0
fi

find_python() {
    if [ -n "${LUMI_PYTHON:-}" ]; then
        candidates="$LUMI_PYTHON"
    else
        candidates="python3 python3.14 python3.13 python3.12 python3.11"
    fi
    for candidate in $candidates; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    printf '%s\n' "Lumi requires Python 3.11 or newer." >&2
    exit 1
fi

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_ROOT="$DATA_HOME/codex-ltmux-pet"
VENV="$INSTALL_ROOT/venv"
BIN_DIR="$HOME/.local/bin"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

BASE_URL="https://github.com/$REPOSITORY/releases/download/v$VERSION"
printf 'Installing Lumi Codex Pet %s with %s\n' "$VERSION" "$PYTHON"
curl -fL --retry 3 --proto '=https' --tlsv1.2 "$BASE_URL/$WHEEL" -o "$TMP_DIR/$WHEEL"
curl -fL --retry 3 --proto '=https' --tlsv1.2 "$BASE_URL/SHA256SUMS" -o "$TMP_DIR/SHA256SUMS"
grep "  $WHEEL\$" "$TMP_DIR/SHA256SUMS" > "$TMP_DIR/WHEEL.SHA256SUM"
(cd "$TMP_DIR" && sha256sum -c WHEEL.SHA256SUM)

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade "$TMP_DIR/$WHEEL"
printf 'version=%s\nrepository=%s\n' "$VERSION" "$REPOSITORY" > "$INSTALL_ROOT/.lumi-managed"
chmod 600 "$INSTALL_ROOT/.lumi-managed"

for command_name in lumi-pet lumi-state-bridge lumi-ctl; do
    target="$BIN_DIR/$command_name"
    source="$VENV/bin/$command_name"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        backup="$target.backup-$(date -u +%Y%m%dT%H%M%SZ)"
        mv "$target" "$backup"
        printf 'Backed up %s to %s\n' "$target" "$backup"
    else
        rm -f "$target"
    fi
    ln -s "$source" "$target"
done

if [ "${LUMI_NO_START:-0}" = "1" ]; then
    "$VENV/bin/lumi-ctl" setup --no-start
else
    "$VENV/bin/lumi-ctl" setup
fi

printf '%s\n' "Installation complete. Run 'lumi-ctl doctor', then review Lumi in Codex with /hooks."

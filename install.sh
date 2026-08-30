#!/bin/sh
set -eu

source_ref=${CROSS_AGENT_CHAT_SOURCE:-git+https://github.com/kwonsyup/cross-agent-chat.git@v0.1.2}

backup_previous_runtime() {
    previous_executable=$(command -v cross-agent-chat 2>/dev/null || true)
    if [ -z "$previous_executable" ] && [ -x "$HOME/.local/bin/cross-agent-chat" ]; then
        previous_executable=$HOME/.local/bin/cross-agent-chat
    fi
    [ -n "$previous_executable" ] || return 0
    runtime_root=$(/usr/bin/python3 - "$previous_executable" "$HOME" <<'PY'
import sys
from pathlib import Path

executable = Path(sys.argv[1]).resolve()
home = Path(sys.argv[2]).resolve()
root = executable.parent.parent
allowed = {
    home / ".local/share/uv/tools/cross-agent-chat",
    home / ".local/share/pipx/venvs/cross-agent-chat",
    home / ".local/pipx/venvs/cross-agent-chat",
    home / ".local/share/cross-agent-chat",
}
if root in allowed and root.is_dir():
    print(root)
PY
)
    [ -n "$runtime_root" ] || return 0
    backup_parent=$HOME/.cache/cross-agent-chat/runtime-backups
    mkdir -p "$backup_parent"
    chmod 700 "$backup_parent"
    previous_runtime=$(mktemp -d "$backup_parent/runtime.XXXXXX")
    chmod 700 "$previous_runtime"
    cp -R "$runtime_root/." "$previous_runtime/"
    export CROSS_AGENT_CHAT_PREVIOUS_RUNTIME=$previous_runtime
    export CROSS_AGENT_CHAT_RUNTIME_ROOT=$runtime_root
}

python_is_compatible() {
    command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
}

backup_previous_runtime

if command -v uv >/dev/null 2>&1; then
    uv tool install --force --python 3.11 "$source_ref"
elif command -v pipx >/dev/null 2>&1 && python_is_compatible; then
    pipx install --force "$source_ref"
elif python_is_compatible; then
    runtime="$HOME/.local/share/cross-agent-chat"
    python3 -m venv "$runtime"
    "$runtime/bin/python" -m pip install --force-reinstall "$source_ref"
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$runtime/bin/cross-agent-chat" "$HOME/.local/bin/cross-agent-chat"
else
    bootstrap_dir=$(mktemp -d /tmp/cross-agent-chat-uv.XXXXXX)
    trap 'rm -rf "$bootstrap_dir"' EXIT HUP INT TERM
    curl -fsSL https://astral.sh/uv/install.sh -o "$bootstrap_dir/install-uv.sh"
    UV_UNMANAGED_INSTALL="$HOME/.local/bin" sh "$bootstrap_dir/install-uv.sh"
    "$HOME/.local/bin/uv" tool install --force --python 3.11 "$source_ref"
fi

if command -v cross-agent-chat >/dev/null 2>&1; then
    cross-agent-chat setup
elif [ -x "$HOME/.local/bin/cross-agent-chat" ]; then
    "$HOME/.local/bin/cross-agent-chat" setup
else
    printf '%s\n' 'Cross Agent Chat installed, but its executable is not available.' >&2
    exit 2
fi

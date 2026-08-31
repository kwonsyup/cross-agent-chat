#!/bin/sh
set -eu

source_ref=${CROSS_AGENT_CHAT_SOURCE:-git+https://github.com/kwonsyup/cross-agent-chat.git@v0.1.2}
product_root=$HOME/.local/share/cross-agent-chat-runtime
releases_root=$product_root/releases
mkdir -p "$releases_root"
chmod 700 "$product_root" "$releases_root"

staged_runtime=$(mktemp -d "$releases_root/.staging-XXXXXX")
bootstrap_dir=
cleanup() {
    if [ -n "$staged_runtime" ]; then
        rm -rf "$staged_runtime"
    fi
    rmdir "$releases_root" "$product_root" 2>/dev/null || true
    if [ -n "$bootstrap_dir" ]; then
        rm -rf "$bootstrap_dir"
    fi
}
trap cleanup EXIT HUP INT TERM

python_is_compatible() {
    command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
}

if command -v uv >/dev/null 2>&1; then
    uv_command=$(command -v uv)
elif python_is_compatible; then
    uv_command=
else
    bootstrap_dir=$(mktemp -d /tmp/cross-agent-chat-uv.XXXXXX)
    curl -fsSL https://astral.sh/uv/install.sh -o "$bootstrap_dir/install-uv.sh"
    UV_UNMANAGED_INSTALL="$bootstrap_dir/bin" sh "$bootstrap_dir/install-uv.sh"
    uv_command=$bootstrap_dir/bin/uv
fi

if [ -n "$uv_command" ]; then
    "$uv_command" venv --python 3.11 "$staged_runtime"
    "$uv_command" pip install --python "$staged_runtime/bin/python" "$source_ref"
else
    python3 -m venv "$staged_runtime"
    "$staged_runtime/bin/python" -m pip install "$source_ref"
fi

previous_executable=$(command -v cross-agent-chat 2>/dev/null || true)
case "$previous_executable" in
    "$HOME"/*) stable_entrypoint=$previous_executable ;;
    *) stable_entrypoint=$HOME/.local/bin/cross-agent-chat ;;
esac

"$staged_runtime/bin/python" -c 'import cross_agent_chat'
"$staged_runtime/bin/cross-agent-chat" --version
"$staged_runtime/bin/cross-agent-chat" _install-staged \
    --staged-runtime "$staged_runtime" \
    --stable-entrypoint "$stable_entrypoint"

staged_runtime=

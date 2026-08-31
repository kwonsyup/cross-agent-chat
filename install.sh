#!/bin/sh
set -eu

source_ref=${CROSS_AGENT_CHAT_SOURCE:-git+https://github.com/kwonsyup/cross-agent-chat.git@v0.1.2}
home_root=$(cd "$HOME" && pwd -P)
mkdir -p "$HOME/.local"
local_root=$(cd "$HOME/.local" && pwd -P)
case "$local_root" in
    "$home_root"/*) ;;
    *)
        printf '%s\n' 'Cross Agent Chat runtime ownership is invalid.' >&2
        exit 2
        ;;
esac
mkdir -p "$HOME/.local/share"
share_root=$(cd "$HOME/.local/share" && pwd -P)
case "$share_root" in
    "$home_root"/*) ;;
    *)
        printf '%s\n' 'Cross Agent Chat runtime ownership is invalid.' >&2
        exit 2
        ;;
esac
product_root=$share_root/cross-agent-chat-runtime
releases_root=$product_root/releases
if [ -L "$product_root" ] || [ -L "$releases_root" ]; then
    printf '%s\n' 'Cross Agent Chat runtime ownership is invalid.' >&2
    exit 2
fi
mkdir -p "$releases_root"
chmod 700 "$product_root" "$releases_root"

staged_runtime=$(mktemp -d "$releases_root/release-XXXXXX")
bootstrap_dir=
owner_identity=$(/bin/ps -ww -p "$$" -o lstart= -o command= | shasum -a 256 | awk '{print $1}')
cleanup() {
    if [ -n "$staged_runtime" ]; then
        marker=$staged_runtime/.cross-agent-chat-release
        expected="cross-agent-chat-runtime-v1:staged:$$:$owner_identity"
        if [ ! -e "$marker" ] ||
            { [ -f "$marker" ] && [ "$(sed -n '1p' "$marker")" = "$expected" ]; }; then
            rm -rf "$staged_runtime"
        fi
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
else
    python3 -m venv "$staged_runtime"
fi
printf '%s:%s:%s\n' 'cross-agent-chat-runtime-v1:staged' "$$" "$owner_identity" > "$staged_runtime/.cross-agent-chat-release"
chmod 600 "$staged_runtime/.cross-agent-chat-release"
if [ -n "$uv_command" ]; then
    "$uv_command" pip install --python "$staged_runtime/bin/python" "$source_ref"
else
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

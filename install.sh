#!/usr/bin/env sh
# Synix Claude Proxy — Installer
# Usage: curl -fsSL https://synix.dev/proxy | sh
#
# Installs the Synix context window proxy for Claude Code.
# Supports Docker and Podman. Installs to ~/.local/ (XDG-compliant).
set -eu

# ── Configuration ────────────────────────────────────────────────────────────
REPO_URL="https://github.com/marklubin/double-buffer-proxy"
IMAGE_REGISTRY="ghcr.io/marklubin"
IMAGE_NAME="synix-proxy"
CONTAINER_NAME="synix-proxy"

INSTALL_DIR="${HOME}/.local/bin"
DATA_DIR="${HOME}/.local/share/synix-proxy"
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"

# ── Helpers ──────────────────────────────────────────────────────────────────
log()   { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m==> WARNING: %s\033[0m\n' "$*"; }
error() { printf '\033[1;31m==> ERROR: %s\033[0m\n' "$*"; exit 1; }
ok()    { printf '\033[1;32m==> %s\033[0m\n' "$*"; }

# ── Banner ───────────────────────────────────────────────────────────────────
banner() {
    printf '\n'
    printf '\033[1m  Synix — Installer\033[0m\n'
    printf '  Pre-computes conversation summaries so Claude Code compaction is instant\n'
    printf '\n'
}

# ── Detect OS ────────────────────────────────────────────────────────────────
detect_os() {
    OS="$(uname -s)"
    case "$OS" in
        Linux)  OS="linux" ;;
        Darwin) OS="darwin" ;;
        *)      error "Unsupported OS: $OS. Only Linux and macOS are supported." ;;
    esac
    log "Detected OS: $OS"
}

# ── Detect container runtime ────────────────────────────────────────────────
detect_runtime() {
    if command -v podman >/dev/null 2>&1; then
        RUNTIME="podman"
    elif command -v docker >/dev/null 2>&1; then
        RUNTIME="docker"
    else
        printf '\n'
        error "Neither Docker nor Podman found. Please install one first:

  Docker:  https://docs.docker.com/get-docker/
  Podman:  https://podman.io/getting-started/installation

  On macOS:   brew install podman
  On Debian:  sudo apt install podman
  On Fedora:  sudo dnf install podman"
    fi

    # Test if runtime works rootless; fall back to sudo
    if $RUNTIME info >/dev/null 2>&1; then
        RT_CMD="$RUNTIME"
    elif sudo $RUNTIME info >/dev/null 2>&1; then
        RT_CMD="sudo $RUNTIME"
        log "$RUNTIME requires sudo."
    else
        error "$RUNTIME is installed but not functional. Check: $RUNTIME info"
    fi
    log "Container runtime: $RT_CMD"
}

# ── Check prerequisites ─────────────────────────────────────────────────────
check_prereqs() {
    for cmd in curl; do
        command -v "$cmd" >/dev/null 2>&1 || error "'$cmd' is required but not found."
    done

    # Ensure claude CLI is installed
    if ! command -v claude >/dev/null 2>&1; then
        warn "'claude' CLI not found on PATH. Install it first: npm install -g @anthropic-ai/claude-code"
    fi
}

# ── Create directories ──────────────────────────────────────────────────────
setup_dirs() {
    log "Creating directories..."
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$DATA_DIR/certs"
    mkdir -p "$DATA_DIR/data"
    mkdir -p "$DATA_DIR/logs"
    mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
}

# ── Pull or build image ─────────────────────────────────────────────────────
get_image() {
    FULL_IMAGE="${IMAGE_REGISTRY}/${IMAGE_NAME}:latest"

    # Try pulling from registry first
    log "Pulling container image..."
    if $RT_CMD pull "$FULL_IMAGE" 2>/dev/null; then
        ok "Image pulled: $FULL_IMAGE"
        IMAGE_REF="$FULL_IMAGE"
        return
    fi

    # Fall back to building from source
    log "Registry image not available. Building from source..."
    TMPDIR_BUILD="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_BUILD"' EXIT

    if command -v git >/dev/null 2>&1; then
        git clone --depth 1 "$REPO_URL" "$TMPDIR_BUILD/repo"
    else
        curl -fsSL "${REPO_URL}/archive/refs/heads/main.tar.gz" | tar xz -C "$TMPDIR_BUILD"
        mv "$TMPDIR_BUILD"/double-buffer-proxy-main "$TMPDIR_BUILD/repo"
    fi

    if $RT_CMD build -t "${IMAGE_NAME}:latest" "$TMPDIR_BUILD/repo"; then
        :
    elif [ "$RT_CMD" = "$RUNTIME" ] && sudo $RUNTIME build -t "${IMAGE_NAME}:latest" "$TMPDIR_BUILD/repo"; then
        # Rootless build failed (common with podman + cgroupv2) — sudo worked
        RT_CMD="sudo $RUNTIME"
        log "Rootless build failed — using sudo for $RUNTIME."
    else
        error "Failed to build container image."
    fi
    IMAGE_REF="${IMAGE_NAME}:latest"
    ok "Image built: $IMAGE_REF"
}

# ── Generate TLS certs ──────────────────────────────────────────────────────
generate_certs() {
    if [ -f "$DATA_DIR/certs/ca.pem" ]; then
        log "TLS certificates already exist."
        return
    fi

    log "Generating TLS certificates..."
    $RT_CMD run --rm \
        --entrypoint python \
        -v "$DATA_DIR/certs:/app/certs" \
        "$IMAGE_REF" \
        -c "from dbproxy.tls import generate_certs; generate_certs('/app/certs'); print('OK')"
    ok "Certificates generated at $DATA_DIR/certs/"
}

# ── Install wrapper script ──────────────────────────────────────────────────
install_wrapper() {
    WRAPPER="$INSTALL_DIR/synix-proxy"
    log "Installing synix-proxy to $WRAPPER..."

    cat > "$WRAPPER" << 'WRAPPER_EOF'
#!/usr/bin/env bash
# synix-proxy — Launch Claude Code through the Synix proxy.
# https://github.com/marklubin/double-buffer-proxy
set -euo pipefail

DATA_DIR="${SYNIX_DATA_DIR:-$HOME/.local/share/synix-proxy}"
CONTAINER_NAME="synix-proxy"
PROXY_PORT="${SYNIX_PROXY_PORT:-47200}"
DASHBOARD_PORT="${SYNIX_DASHBOARD_PORT:-47201}"
LOG_LEVEL="${SYNIX_LOG_LEVEL:-INFO}"

# ── Detect container runtime ────────────────────────────────────────────
RT=""
for _rt in podman docker; do
    if command -v "$_rt" >/dev/null 2>&1; then
        if "$_rt" info >/dev/null 2>&1; then
            RT="$_rt"
        elif sudo "$_rt" info >/dev/null 2>&1; then
            RT="sudo $_rt"
        fi
        break
    fi
done
if [ -z "$RT" ]; then
    echo "ERROR: Neither docker nor podman found (or not functional)." >&2
    exit 1
fi

# ── Subcommands ─────────────────────────────────────────────────────────
case "${1:-}" in
    start)
        shift
        ;; # fall through to start logic below
    stop)
        echo "Stopping $CONTAINER_NAME..."
        $RT stop "$CONTAINER_NAME" 2>/dev/null || true
        $RT rm "$CONTAINER_NAME" 2>/dev/null || true
        echo "Stopped."
        exit 0
        ;;
    status)
        if $RT ps --filter "name=$CONTAINER_NAME" --format '{{.Names}}' 2>/dev/null | grep -q "$CONTAINER_NAME"; then
            echo "Running"
            curl -fsk "https://localhost:${DASHBOARD_PORT}/health" 2>/dev/null || echo "(health check failed)"
        else
            echo "Stopped"
        fi
        exit 0
        ;;
    logs)
        shift
        if [ -d "$DATA_DIR/logs" ]; then
            tail -f "${@:--n 50}" "$DATA_DIR/logs/dbproxy.jsonl" 2>/dev/null || echo "No log file yet."
        else
            echo "No logs directory found."
        fi
        exit 0
        ;;
    dashboard)
        echo "https://localhost:${DASHBOARD_PORT}/dashboard"
        exit 0
        ;;
    proxy-update)
        echo "Updating Synix..."
        echo "Stopping container..."
        $RT stop "$CONTAINER_NAME" 2>/dev/null || true
        $RT rm "$CONTAINER_NAME" 2>/dev/null || true
        echo "Removing old image..."
        $RT rmi "ghcr.io/marklubin/synix-proxy:latest" 2>/dev/null || true
        $RT rmi "synix-proxy:latest" 2>/dev/null || true
        echo "Re-running installer (pulls latest image + updates wrapper)..."
        curl -fsSL https://synix.dev/proxy | sh
        exit $?
        ;;
    uninstall)
        echo "Stopping container..."
        $RT stop "$CONTAINER_NAME" 2>/dev/null || true
        $RT rm "$CONTAINER_NAME" 2>/dev/null || true
        echo ""
        echo "To complete uninstall, remove:"
        echo "  rm $0"
        echo "  rm -rf $DATA_DIR"
        echo "  # Remove 'alias claude=synix-proxy' from your shell config"
        echo "  # Remove statusLine from ~/.claude/settings.json"
        exit 0
        ;;
    report-bug)
        echo "Opening GitHub issue form..."
        URL="https://github.com/marklubin/double-buffer-proxy/issues/new?labels=bug&template=bug_report.md"
        if command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$URL"
        elif command -v open >/dev/null 2>&1; then
            open "$URL"
        else
            echo "Open this URL to file a bug report:"
            echo "  $URL"
        fi
        exit 0
        ;;
    proxy-help)
        echo "Usage: synix-proxy [command] [claude args...]"
        echo ""
        echo "Commands:"
        echo "  (default)     Start proxy (if needed) and launch Claude Code"
        echo "  start         Start the proxy container only"
        echo "  stop          Stop the proxy container"
        echo "  status        Show proxy status"
        echo "  logs          Tail proxy logs (structured JSON)"
        echo "  dashboard     Print dashboard URL"
        echo "  proxy-update  Update proxy (pulls latest image + wrapper)"
        echo "  report-bug    Open GitHub issue form in browser"
        echo "  uninstall     Stop container and print cleanup instructions"
        echo "  proxy-help    Show this help"
        echo ""
        echo "Flags:"
        echo "  --full-access  Run Claude with --dangerously-skip-permissions"
        echo ""
        echo "All other arguments are passed through to claude."
        echo ""
        echo "Environment:"
        echo "  SYNIX_LOG_LEVEL          Log level (default: INFO)"
        echo "  SYNIX_PROXY_PORT         Redirector port (default: 47200)"
        echo "  SYNIX_DASHBOARD_PORT     Dashboard port (default: 47201)"
        echo "  SYNIX_CHECKPOINT_THRESHOLD  Checkpoint at N% context (default: 70)"
        echo "  SYNIX_SWAP_THRESHOLD     Swap at N% context (default: 80)"
        echo ""
        echo "Logs: $DATA_DIR/logs/dbproxy.jsonl"
        echo "Dashboard: https://localhost:${DASHBOARD_PORT}/dashboard"
        exit 0
        ;;
esac

# ── Resolve image ───────────────────────────────────────────────────────
IMAGE_REF=""
for candidate in "ghcr.io/marklubin/synix-proxy:latest" "synix-proxy:latest"; do
    if $RT image exists "$candidate" 2>/dev/null || $RT inspect "$candidate" >/dev/null 2>&1; then
        IMAGE_REF="$candidate"
        break
    fi
done
if [ -z "$IMAGE_REF" ]; then
    echo "Image not found locally. Pulling..." >&2
    IMAGE_REF="ghcr.io/marklubin/synix-proxy:latest"
    $RT pull "$IMAGE_REF"
fi

# ── Start container if not running ──────────────────────────────────────
if ! $RT ps --filter "name=$CONTAINER_NAME" --format '{{.Names}}' 2>/dev/null | grep -q "$CONTAINER_NAME"; then
    # Clean up stopped container with same name
    $RT rm "$CONTAINER_NAME" 2>/dev/null || true

    echo "Starting Synix proxy..."
    $RT run -d \
        --name "$CONTAINER_NAME" \
        -p "127.0.0.1:${PROXY_PORT}:47200" \
        -p "127.0.0.1:${DASHBOARD_PORT}:443" \
        -v "$DATA_DIR/certs:/app/certs" \
        -v "$DATA_DIR/data:/app/data" \
        -v "$DATA_DIR/logs:/app/logs" \
        -e "SYNIX_HOST=0.0.0.0" \
        -e "SYNIX_LOG_LEVEL=${LOG_LEVEL}" \
        -e "SYNIX_CHECKPOINT_THRESHOLD=${SYNIX_CHECKPOINT_THRESHOLD:-}" \
        -e "SYNIX_SWAP_THRESHOLD=${SYNIX_SWAP_THRESHOLD:-}" \
        --restart unless-stopped \
        "$IMAGE_REF" >/dev/null

    # Wait for health
    printf "Waiting for proxy"
    for _i in $(seq 1 30); do
        if curl -fsk "https://localhost:${DASHBOARD_PORT}/health" >/dev/null 2>&1; then
            printf " ready.\n"
            break
        fi
        printf "."
        sleep 1
    done
fi

# ── Verify CA cert ─────────────────────────────────────────────────────
CA_CERT="$DATA_DIR/certs/ca.pem"
if [ ! -f "$CA_CERT" ]; then
    echo "ERROR: CA certificate not found at $CA_CERT" >&2
    echo "Check: $RT logs $CONTAINER_NAME" >&2
    exit 1
fi

# ── If 'start' subcommand, just report and exit ────────────────────────
if [ "${_SUBCOMMAND:-}" = "start" ] || { [ "${1:-}" = "" ] && [ "$(basename "$0")" != "claude" ]; } && false; then
    :
fi

# ── Launch Claude ──────────────────────────────────────────────────────
export HTTPS_PROXY="http://127.0.0.1:${PROXY_PORT}"
export NODE_EXTRA_CA_CERTS="$CA_CERT"
export SYNIX_ACTIVE=1
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80

# Rewrite --full-access → --dangerously-skip-permissions
_args=()
for _a in "$@"; do
    case "$_a" in
        --full-access) _args+=("--dangerously-skip-permissions") ;;
        *) _args+=("$_a") ;;
    esac
done

# Only print status if stdout is a terminal
if [ -t 1 ]; then
    printf '\033[1;38;5;48mSynix: ON\033[0m | Dashboard: https://localhost:%s/dashboard\n' "$DASHBOARD_PORT"
fi

exec claude "${_args[@]}"
WRAPPER_EOF

    chmod +x "$WRAPPER"
    ok "Installed: $WRAPPER"
}

# ── Configure Claude Code statusline ─────────────────────────────────────────
configure_statusline() {
    log "Configuring Claude Code status line..."

    # Write statusline helper
    STATUSLINE_SCRIPT="$DATA_DIR/statusline.sh"
    cat > "$STATUSLINE_SCRIPT" << 'SL_EOF'
#!/bin/sh
# Synix statusline — only visible when running through the proxy.
# Claude Code pipes JSON on stdin (must be consumed).
cat > /dev/null
[ -n "${SYNIX_ACTIVE:-}" ] && printf '\033[1;38;5;48m◈ SYNIX-PROXY ON\033[0m'
SL_EOF
    chmod +x "$STATUSLINE_SCRIPT"

    # Update settings.json
    if [ -f "$CLAUDE_SETTINGS" ]; then
        # Check if statusLine already configured
        if python3 -c "
import json, sys
with open('$CLAUDE_SETTINGS') as f:
    d = json.load(f)
if 'statusLine' in d:
    sys.exit(1)
" 2>/dev/null; then
            # No statusLine yet — add it
            python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    d = json.load(f)
d['statusLine'] = {
    'type': 'command',
    'command': '$STATUSLINE_SCRIPT'
}
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
"
            ok "Status line configured in $CLAUDE_SETTINGS"
        else
            warn "statusLine already configured in $CLAUDE_SETTINGS."
            printf '  To add synix-proxy status manually, append to your statusLine command:\n'
            printf '  ; [ -n "\$SYNIX_ACTIVE" ] && printf " | ◈ SYNIX-PROXY ON"\n\n'
        fi
    else
        # Create settings.json
        cat > "$CLAUDE_SETTINGS" << SETTINGS_EOF
{
  "statusLine": {
    "type": "command",
    "command": "$STATUSLINE_SCRIPT"
  }
}
SETTINGS_EOF
        ok "Created $CLAUDE_SETTINGS with status line."
    fi
}

# ── Detect shell ─────────────────────────────────────────────────────────────
detect_shell_config() {
    SHELL_NAME="$(basename "${SHELL:-/bin/sh}")"
    case "$SHELL_NAME" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
        *)    SHELL_RC="$HOME/.profile" ;;
    esac
}

# ── Smoke test — start container and verify health ───────────────────────────
smoke_test() {
    PROXY_PORT="${SYNIX_PROXY_PORT:-47200}"
    DASHBOARD_PORT="${SYNIX_DASHBOARD_PORT:-47201}"
    LOG_LEVEL="${SYNIX_LOG_LEVEL:-INFO}"

    log "Starting proxy container..."

    # Clean up any existing container
    $RT_CMD rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

    $RT_CMD run -d \
        --name "$CONTAINER_NAME" \
        -p "127.0.0.1:${PROXY_PORT}:47200" \
        -p "127.0.0.1:${DASHBOARD_PORT}:443" \
        -v "$DATA_DIR/certs:/app/certs" \
        -v "$DATA_DIR/data:/app/data" \
        -v "$DATA_DIR/logs:/app/logs" \
        -e "SYNIX_HOST=0.0.0.0" \
        -e "SYNIX_LOG_LEVEL=${LOG_LEVEL}" \
        --restart unless-stopped \
        "$IMAGE_REF" >/dev/null

    printf "  Waiting for health check"
    HEALTHY=false
    for _i in $(seq 1 30); do
        if curl -fsk "https://localhost:${DASHBOARD_PORT}/health" >/dev/null 2>&1; then
            HEALTHY=true
            break
        fi
        printf "."
        sleep 1
    done

    if $HEALTHY; then
        printf "\n"
        ok "Proxy is running and healthy."
        printf '  Dashboard: https://localhost:%s/dashboard\n' "$DASHBOARD_PORT"
    else
        printf "\n"
        warn "Proxy started but health check failed. Check logs:"
        printf '  %s logs %s\n' "$RT_CMD" "$CONTAINER_NAME"
    fi
}

# ── Print next steps ─────────────────────────────────────────────────────────
print_next_steps() {
    detect_shell_config

    printf '\n'
    ok "Installation complete!"
    printf '\n'
    printf '  \033[1mNext steps:\033[0m\n\n'

    # Check if ~/.local/bin is in PATH
    case ":$PATH:" in
        *":$INSTALL_DIR:"*) ;;
        *)
            printf '  1. Add ~/.local/bin to your PATH (if not already):\n'
            if [ "$SHELL_NAME" = "fish" ]; then
                printf '     \033[1;33mfish_add_path %s\033[0m\n\n' "$INSTALL_DIR"
            else
                printf '     \033[1;33mexport PATH="%s:\$PATH"\033[0m  # add to %s\n\n' "$INSTALL_DIR" "$SHELL_RC"
            fi
            ;;
    esac

    printf '  2. Add this alias so "claude" always uses the proxy:\n'
    if [ "$SHELL_NAME" = "fish" ]; then
        printf '     \033[1;33malias claude "synix-proxy"\033[0m  # add to %s\n\n' "$SHELL_RC"
    else
        printf '     \033[1;33malias claude="synix-proxy"\033[0m  # add to %s\n\n' "$SHELL_RC"
    fi

    printf '  3. Start using it:\n'
    printf '     \033[1msynix-proxy\033[0m              # launch Claude through proxy\n'
    printf '     \033[1msynix-proxy --full-access\033[0m # skip all permission prompts\n'
    printf '     \033[1msynix-proxy status\033[0m       # check proxy status\n'
    printf '     \033[1msynix-proxy logs\033[0m         # view proxy logs\n'
    printf '     \033[1msynix-proxy dashboard\033[0m    # print dashboard URL\n'
    printf '     \033[1msynix-proxy stop\033[0m         # stop the proxy\n'
    printf '\n'
    printf '  Logs:      %s/logs/dbproxy.jsonl\n' "$DATA_DIR"
    printf '  Dashboard: https://localhost:47201/dashboard\n'
    printf '  Docs:      %s\n' "$REPO_URL"
    printf '\n'
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    banner
    detect_os
    check_prereqs
    detect_runtime
    setup_dirs
    get_image
    generate_certs
    install_wrapper
    configure_statusline
    smoke_test
    print_next_steps
}

main "$@"

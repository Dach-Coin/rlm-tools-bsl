#!/usr/bin/env bash
# rlm-tools-bsl -- quick install as a systemd --user service
#
# Prerequisites:
#   Python 3.10+  https://python.org
#   uv            https://docs.astral.sh/uv/
#
# Optional LLM env vars (for llm_query helper):
#   Create .env next to this script, or set environment variables:
#     RLM_LLM_BASE_URL, RLM_LLM_API_KEY, RLM_LLM_MODEL  (OpenAI-compatible)
#     ANTHROPIC_API_KEY                                    (Anthropic API)
#   Without LLM keys all core features still work (find_module, grep, xml parsing).
#
# Usage:
#   ./simple-install.sh                    # auto-detect .env in script dir
#   ./simple-install.sh /path/to/.env      # explicit .env path
#   RLM_PORT=3000 ./simple-install.sh      # custom port
#   UV_NATIVE_TLS=true ./simple-install.sh # corporate proxy with TLS replacement
#
# After install, to enable autostart without login:
#   loginctl enable-linger $USER

set -euo pipefail

BIND_HOST="${RLM_HOST:-127.0.0.1}"
PORT="${RLM_PORT:-9000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_ARG=""

# --- Detect .env ---
if [ -n "${1:-}" ]; then
    ENV_ARG="--env $1"
    echo "Using .env: $1"
elif [ -f "$SCRIPT_DIR/.env" ]; then
    ENV_ARG="--env $SCRIPT_DIR/.env"
    echo "Found .env: $SCRIPT_DIR/.env"
else
    echo "No .env found - service will start without it."
    echo "(Set LLM keys as system env vars if needed)"
fi

# --- Check uv ---
if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found. Install it:"
    echo ""
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    exit 1
fi

# --- Check Python ---
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "ERROR: Python not found. Install Python 3.10+ from https://python.org"
    exit 1
fi

# --- Stop & uninstall existing service (best-effort, idempotent for fresh install) ---
# Prepend uv tool bin to PATH so an existing installation is detected even when
# the shell hasn't been re-sourced since the last `uv tool update-shell`.
UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
if [ -n "$UV_BIN_DIR" ] && [ -d "$UV_BIN_DIR" ]; then
    export PATH="$UV_BIN_DIR:$PATH"
fi

if command -v rlm-tools-bsl &>/dev/null; then
    echo ""
    echo "=== Existing installation detected -- upgrading ==="
    rlm-tools-bsl service stop 2>/dev/null && echo "Service stopped." \
        || echo "Service was not running (OK)."
    rlm-tools-bsl service uninstall 2>/dev/null && echo "Service uninstalled." \
        || echo "Service was not installed (OK)."
fi

# Safety net for orphaned systemd --user units (rlm-tools-bsl binary already
# removed but unit file lingers in ~/.config/systemd/user/).
if command -v systemctl &>/dev/null; then
    systemctl --user disable --now rlm-tools-bsl.service 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

# --- Step 1: Install ---
echo ""
echo "=== Step 1: Install rlm-tools-bsl ==="
UV_EXTRA_ARGS=()
if [ "${UV_NATIVE_TLS:-}" = "true" ]; then
    UV_EXTRA_ARGS+=("--native-tls")
fi

# Force a fresh build (drop any cached wheel from a previous install).
# --upgrade and --refresh keep this path symmetric with simple-install-from-pip.sh:
# without --upgrade an already-installed dependency that still satisfies the
# constraints is left untouched, so a rebuild from source could leave the service
# on an outdated dependency. --refresh also drops uv's cached index metadata and
# the cached local build, which `cache clean` alone does not cover.
uv cache clean rlm-tools-bsl 2>/dev/null || true

if ! uv tool install "${SCRIPT_DIR}[service]" --force --reinstall --upgrade --refresh "${UV_EXTRA_ARGS[@]}"; then
    echo "ERROR: uv tool install failed."
    echo "If you see TLS certificate errors (corporate proxy), retry with:"
    echo "  UV_NATIVE_TLS=true ./simple-install.sh"
    exit 1
fi

# Ensure rlm-tools-bsl is in PATH for this session
if ! command -v rlm-tools-bsl &>/dev/null; then
    echo "Adding uv tool bin directory to PATH..."
    UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
    if [ -n "$UV_BIN_DIR" ] && [ -d "$UV_BIN_DIR" ]; then
        export PATH="$UV_BIN_DIR:$PATH"
    fi
    uv tool update-shell 2>/dev/null || true
fi

# --- Step 2: Register service ---
echo ""
echo "=== Step 2: Register service ==="
# shellcheck disable=SC2086
rlm-tools-bsl service install --host "$BIND_HOST" --port "$PORT" $ENV_ARG

# --- Step 3: Start ---
echo ""
echo "=== Step 3: Start service ==="
rlm-tools-bsl service start

# --- Step 4: Verify ---
echo ""
echo "=== Step 4: Verify ==="
echo "Waiting for server to start (up to ~40s total on slower machines: 4 attempts x 10s)..."

# /health is lightweight (no MCP session); /mcp is the real endpoint shown in config.
HEALTH_URL="http://${BIND_HOST}:${PORT}/health"
MCP_URL="http://${BIND_HOST}:${PORT}/mcp"
echo "Checking $HEALTH_URL ..."

HTTP_CODE="000"
for attempt in 1 2 3 4; do
    sleep 10
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null || true)
    if [ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "000" ]; then
        break
    fi
    if [ "$attempt" -lt 4 ]; then
        echo "  Attempt $attempt/4: not ready yet, retrying..."
    fi
done

if [ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "000" ]; then
    echo "Server is responding (HTTP $HTTP_CODE). OK."
else
    echo "WARN: Server is not responding at $HEALTH_URL"
    echo "Check status: rlm-tools-bsl service status"
    exit 1
fi

# --- Done ---
echo ""
echo "========================================"
echo " Done! HTTP MCP server is running."
echo "========================================"
echo ""
echo "Version:  $(rlm-tools-bsl --version 2>&1)"
echo "Endpoint: $MCP_URL"
echo "Health:   $HEALTH_URL"
echo ""
echo "Add to .claude.json / mcp.json:"
echo ""
cat <<EOF
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "type": "http",
      "url": "$MCP_URL"
    }
  }
}
EOF
echo ""
echo "To enable autostart without login: loginctl enable-linger \$USER"
echo ""
echo "Service management:"
echo "  rlm-tools-bsl service status"
echo "  rlm-tools-bsl service stop"
echo "  rlm-tools-bsl service start"
echo "  rlm-tools-bsl service uninstall"

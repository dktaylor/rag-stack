#!/usr/bin/env bash
# install.sh — Deploy rag-stack to /opt/rag-stack and install system tooling.
#
# Run from the rag-stack repo root, or via the KS %post step.
# Override install dir: RAG_INSTALL_DIR=/custom/path bash scripts/install.sh
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${RAG_INSTALL_DIR:-/opt/rag-stack}"
CLI_BIN="/usr/local/bin/rag"
SERVICE="/etc/systemd/system/rag-stack.service"
DEVUSER="${SUDO_USER:-${USER:-devuser}}"

# Determine if sudo is needed
if [ -w "$(dirname "$DEST")" ] && [ -w /usr/local/bin ] && [ -w /etc/systemd/system ]; then
    s() { "$@"; }
else
    s() { sudo "$@"; }
fi

echo "==> rag-stack install: $SOURCE → $DEST"

# Remove a standalone (non-compose) open-webui container if present — it would
# conflict on port 3000. Compose-managed containers are skipped; volume data is
# always preserved (named volumes are independent of container lifecycle).
if docker inspect open-webui >/dev/null 2>&1; then
    compose_project=$(docker inspect open-webui --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
    if [ -z "$compose_project" ]; then
        echo "  Removing standalone open-webui container (data preserved in volume)..."
        docker rm -f open-webui >/dev/null
    else
        echo "  open-webui is compose-managed (project: $compose_project) — leaving it running."
    fi
fi

# Deploy files
s mkdir -p "$DEST/mcp" "$DEST/models"
s cp "$SOURCE/docker-compose.yml"     "$DEST/docker-compose.yml"
s cp "$SOURCE/models.conf"            "$DEST/models.conf"
s cp "$SOURCE/mcp/openwebui-mcp.py"  "$DEST/mcp/openwebui-mcp.py"
s cp "$SOURCE/mcp/rag_cli.py"        "$DEST/mcp/rag_cli.py"

# Create .env from example on first install; never overwrite (preserves token)
if [ ! -f "$DEST/.env" ]; then
    s cp "$SOURCE/.env.example" "$DEST/.env"
    echo "  Created $DEST/.env — add OPENWEBUI_TOKEN after first start"
fi

# Own the install dir to the calling user
s chown -R "${DEVUSER}:${DEVUSER}" "$DEST" 2>/dev/null || true

# Install rag CLI and fetch-model script
s install -m 0755 "$SOURCE/scripts/rag"         "$CLI_BIN"
s install -m 0755 "$SOURCE/scripts/fetch-model" /usr/local/bin/fetch-model
echo "  Installed: $CLI_BIN"
echo "  Installed: /usr/local/bin/fetch-model"

# Install systemd service (disabled by default — on-demand only)
if [ -d /etc/systemd/system ]; then
    s install -m 0644 "$SOURCE/systemd/rag-stack.service" "$SERVICE"
    # Patch User= to match the calling user if it's not devuser
    if [ "$DEVUSER" != "devuser" ]; then
        s sed -i "s/^User=devuser$/User=${DEVUSER}/" "$SERVICE"
    fi
    s systemctl daemon-reload
    echo "  Installed: $SERVICE (disabled — use 'rag start' or 'sudo systemctl enable --now rag-stack')"
fi

echo ""
echo "==> Done. Next steps:"
echo "  1. fetch-model                  # download embedding model (needs network)"
echo "  2. rag start                    # start Open WebUI + Qdrant"
echo "  3. Open http://localhost:3000   # create account, generate API token"
echo "  4. Edit $DEST/.env             # add OPENWEBUI_TOKEN"
echo "  5. rag index ~/Projects/<name>  # index a project"
echo ""
echo "MCP registration (Claude Code):"
echo "  claude mcp add -s user openwebui-rag \\"
echo "    -e OPENWEBUI_URL=http://localhost:3000 \\"
echo "    -e OPENWEBUI_TOKEN=\"\$(grep OPENWEBUI_TOKEN $DEST/.env | cut -d= -f2-)\" \\"
echo "    -e RAG_CWD_DETECT=1 \\"
echo "    -- python3 $DEST/mcp/openwebui-mcp.py"

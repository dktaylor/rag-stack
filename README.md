# openwebui-rag-mcp

A configurable-tier RAG system built on [Open WebUI](https://github.com/open-webui/open-webui) and [Qdrant](https://qdrant.tech/), with an MCP server that bridges AI clients (Claude Code, Hermes, Cursor) to the knowledge base.

## Architecture

```
AI client (Claude Code / Hermes / Cursor)
    │  MCP (stdio)
    ▼
openwebui-mcp.py          ← five tools: rag_search, rag_add_doc,
    │                                    rag_add_issue, rag_index_project,
    │  HTTP / REST                       rag_list_kbs
    ▼
Open WebUI  :3000         ← web UI + embedding + retrieval API
    │
    ▼
Qdrant      :6333         ← vector storage + similarity search
```

### Default tier structure

The shipped `tiers.json` configures five tiers for PHP framework projects:

| Tier | KB name | Contents |
|------|---------|----------|
| 1 | `framework-{name}` | Framework/CMS reference — Drupal, Symfony, WordPress, CakePHP, Laravel |
| 2 | `project-{slug}` | Per-project source code + project-specific config |
| 3 | `common-issues` | Cross-cutting bugs, gotchas, non-obvious fixes |
| 4 | `devops-general` | Infrastructure — Docker, k8s, nginx, SSL (platform-agnostic) |
| 5 | `os-{distro}` | OS-specific — package names, firewall rules, SELinux, distro quirks |

The tier structure is fully configurable. See **[Tier configuration](#tier-configuration)** below.

## Let Claude Code set this up for you

Open a Claude Code session in the directory where you've cloned this repo and paste the following prompt. Claude will walk through every step interactively — starting the stack, getting your token, registering the MCP server, and indexing your first project.

```
I want to set up the openwebui-rag-mcp RAG stack in this directory.
The repo contains: docker-compose.yml (Open WebUI + Qdrant), mcp/openwebui-mcp.py
(the MCP server), tiers.json (tier config), .env.example, and docs/ with setup guides.

Please do the following in order:
1. Copy .env.example to .env and ask me for my OLLAMA_BASE_URL if Ollama isn't
   running on localhost:11434.
2. Run `docker compose up -d` and confirm both containers are healthy.
3. Tell me to open http://localhost:3000, create an account, and generate an API
   token at Settings → Account → API Keys. Wait for me to paste the token back.
4. Write the token into .env as OPENWEBUI_TOKEN.
5. Register the MCP server with Claude Code at user scope using `claude mcp add`,
   substituting the absolute path to mcp/openwebui-mcp.py and the token from .env.
6. Verify the MCP server is connected with `claude mcp list`.
7. Ask me which project I want to index first, then call rag_index_project() with
   that path.
8. Run rag_list_kbs() to confirm the project KB was created.
9. Show me the web interface URLs and confirm everything is working.
```

## Quick start

```bash
# 1. Configure
cp .env.example .env
# Edit .env — set OLLAMA_BASE_URL if Ollama isn't on localhost

# 2. Start Open WebUI + Qdrant
docker compose up -d

# 3. Get API token
# Open http://localhost:3000 → Settings → Account → API Keys → Create
# Paste the token into .env as OPENWEBUI_TOKEN

# 4. Register the MCP server with your AI client
# Claude Code:
claude mcp add -s user openwebui-rag \
  -e OPENWEBUI_URL=http://localhost:3000 \
  -e OPENWEBUI_TOKEN="<token>" \
  -e RAG_CWD_DETECT=1 \
  -- python3 /path/to/rag-stack/mcp/openwebui-mcp.py

# 5. Index a project (run from the project root)
# rag_index_project()
```

## Web interfaces

| Interface | URL | Purpose |
|-----------|-----|---------|
| Open WebUI | http://localhost:3000 | Chat, KB browser, file upload |
| Open WebUI Knowledge | http://localhost:3000 (Workspace → Knowledge) | Browse/search KBs without code |
| Qdrant Dashboard | http://localhost:6333/dashboard | Vector DB browser, collection stats, point search |
| Qdrant Swagger | http://localhost:6333/dashboard#/api | REST API reference |

See `docs/webui-guide.md` for a full walkthrough of both interfaces.

## MCP tools

| Tool | What it does |
|------|-------------|
| `rag_search` | Search across tiers; auto-detects project+framework from CWD |
| `rag_add_doc` | Upload/replace a doc in any tier KB |
| `rag_add_issue` | Add a cross-cutting bug or fix to `common-issues` |
| `rag_index_project` | Clear and rebuild a project KB from source files |
| `rag_list_kbs` | List all KBs grouped by tier with file counts |

## Templates

| File | Use for |
|------|---------|
| `templates/cursor-mcp.json` | Copy to `.cursor/mcp.json` in each project |
| `templates/claude-md-rag-rules.md` | Paste into project `CLAUDE.md` |
| `templates/hermes-mcp-config.yaml` | Add to `~/.hermes/config.yaml` |

## Tier configuration

Tiers are defined in `tiers.json` at the repo root. Customize this file before running `rag start` to match your project's knowledge structure. The MCP server reads it at startup; no code changes are needed.

### Tier config format

```json
{
  "default_tiers": ["tier-id", "..."],
  "tiers": [
    {
      "id": "notes",
      "type": "fixed",
      "kb": "notes",
      "label": "Tier 1",
      "description": "General notes and reference"
    }
  ]
}
```

**`default_tiers`** — the tier IDs searched when `rag_search()` is called without a `tiers` argument. Passing `tiers=[...]` to `rag_search()` **replaces** the defaults entirely — it does not add to them. To extend the defaults, list all tiers you want explicitly.

**Tier types:**

| Type | KB naming | Variable resolved from |
|------|-----------|----------------------|
| `fixed` | Exact name in `kb` field | Nothing — always the same KB |
| `framework` | `kb_pattern` with `{name}` | `composer.json` require fields or directory markers |
| `project` | `kb_pattern` with `{slug}` | CWD directory basename |
| `os` | `kb_pattern` with `{distro}` | `/etc/os-release` `ID=` field |

**Tier properties:**

| Property | Required | Description |
|----------|----------|-------------|
| `id` | yes | Identifier used in `rag_search(tiers=[...])` and `rag_add_doc(tier=...)` |
| `type` | yes | `fixed` \| `framework` \| `project` \| `os` |
| `kb` | fixed only | Exact KB name in Open WebUI |
| `kb_pattern` | non-fixed | KB name template, e.g. `"framework-{name}"` |
| `label` | no | Display label in `rag_list_kbs()` output |
| `description` | no | Used when auto-creating the KB in Open WebUI |
| `auto_include` | no | `true` → always appended to every `rag_search()` when KB exists, regardless of `tiers` or `default_tiers` |

### One-tier setup

Simplest possible config — one fixed KB, everything goes in it:

```json
{
  "default_tiers": ["notes"],
  "tiers": [
    {
      "id": "notes",
      "type": "fixed",
      "kb": "notes",
      "label": "KB 1",
      "description": "General reference notes"
    }
  ]
}
```

`rag_search("anything")` searches `notes`. `rag_add_doc(tier="notes", ...)` uploads there.

### Two-tier setup

Per-project code plus a shared notes KB:

```json
{
  "default_tiers": ["project", "notes"],
  "tiers": [
    {
      "id": "project",
      "type": "project",
      "kb_pattern": "project-{slug}",
      "label": "Tier 1",
      "description": "Per-project source code and config"
    },
    {
      "id": "notes",
      "type": "fixed",
      "kb": "notes",
      "label": "Tier 2",
      "description": "Cross-project reference notes"
    }
  ]
}
```

### Viewing and initializing tiers

```bash
rag tiers          # print configured tiers, default_tiers, and auto_include flags
rag init-tiers     # pre-create fixed-type KBs in Open WebUI (also runs on first rag start)
```

`rag start` calls `init-tiers` automatically during bootstrap so fixed KBs exist before the first agent session.

### Where tiers.json is found

The MCP server searches in this order:

1. `$RAG_TIERS_CONFIG` env var — explicit path override
2. `$RAG_INSTALL_DIR/tiers.json` (default: `/opt/rag-stack/tiers.json`)
3. Repo root relative to the MCP script (`mcp/../tiers.json`) — used in dev
4. Built-in defaults (identical to the shipped `tiers.json`) — used if no file found

## Docs

- `docs/usage.md` — tier guide: getting started, adding content, search patterns, session workflow
- `docs/webui-guide.md` — using Open WebUI and Qdrant dashboard without writing code
- `docs/troubleshooting.md` — failure modes and diagnosis, starting with "MCP tools hang while every health check passes"

# TODO

## MCP server

### BUG: bulk indexing silently drops files — stale /opt deploy lacks the retry mitigation (2026-07-18)

**Symptom:** `rag_index_project` on prism reported `706 files uploaded, 4 errors`
(MCP, ~18:50) and a repeat run reported `692 files uploaded, 18 errors` (CLI,
~19:45). Every error is Open WebUI rejecting a **non-empty** file with
`HTTP 400 …/file/add: {"detail":"400: The content provided is empty…"}`.
A *different* random subset fails each run (`SiteSettingsRepository.php`,
`WidgetCspBuilder.php`, `docker-compose.prod.yml`, … — none remotely empty),
so this is the known intermittent EMPTY_CONTENT rejection under sustained bulk
load (~0.5–2.5 % of 710 files per run), not a content problem.

**Root cause (two layers):**

1. Open WebUI intermittently 400s valid uploads during bulk indexing —
   already known; repo HEAD mitigates it with `_upload_with_retry()`
   (3 attempts + qdrant half-success verification, commit `6856887`).
2. **The deployed copy never got that fix.** Both the Claude Code MCP server
   and the `rag` CLI execute `/opt/rag-stack/mcp/openwebui-mcp.py`
   (`RAG_DIR = os.environ.get("RAG_INSTALL_DIR", "/opt/rag-stack")`), and that
   copy has the qdrant purge but **no `_upload_with_retry`** — failures take
   the old single-shot `skipped {file}` path. Verify:
   `grep -c _upload_with_retry /opt/rag-stack/mcp/openwebui-mcp.py` → 0,
   vs 2 in `mcp/openwebui-mcp.py` at repo HEAD. So transient 400s = files
   silently absent from the project KB. As of 2026-07-18 `project-prism` is
   missing the 18 files from the last run.

**Repro:**
```bash
python3 mcp/rag_cli.py index /home/devuser/Projects/prism --project prism 2>&1 \
  | grep skipped        # different non-empty files 400 each run
```

Files dropped in the 19:45 run (= the current gaps in `project-prism`; the
earlier MCP run's 4 filenames are unrecoverable — stderr wasn't retained,
which is what the "surface filenames in the result" fix below is about):
`.php-cs-fixer.dist.php`, `ai-agents/docs/agents/reference/deployment-tls-letsencrypt.md`,
`config/packages/messenger.yaml`, `config/packages/test/services.yaml`,
`docker-compose.prod.yml`, `lib/PrismMarketingBundle/templates/docs/architecture.html.twig`,
`lib/PrismOidcSsoBundle/src/PrismOidcSsoBundle.php`, `src/Command/CleanupOldAnalyticsCommand.php`,
`src/Controller/Admin/WidgetPreviewController.php`, `src/Controller/Api/WidgetHierarchyController.php`,
`src/Repository/SiteSettingsRepository.php`, `src/Routing/VersioningRouteLoader.php`,
`src/Service/Export/TenantExportGenerator.php`, `src/Service/WidgetCspBuilder.php`,
`src/ValueObject/Pagination.php`, `templates/admin/widget_new.html.twig`,
`tests/Unit/Analytics/ForwarderTest.php`, `tests/Unit/Security/PasswordStrengthCheckerTest.php`

**Fix:**
- [x] Redeploy `mcp/` to `/opt/rag-stack` (install.sh path) so the retry/verify
      mitigation actually runs; re-index affected projects; confirm errors → 0
      (or `verified in qdrant` notes) across a few runs.
      **Done 2026-07-18 (first run):** working-tree `mcp/*.py` deployed to
      `/opt` (copies verified identical, stale `__pycache__` cleared);
      prism re-index → **710 files uploaded, 0 errors** (all 18 gap files
      recovered; `SiteSettingsRepository.php` confirmed retrievable via
      `rag_search`). Note: a running MCP server keeps the old module until its
      session restarts — verify runs went through the CLI. Keep an eye on the
      next few indexes before calling the retry mitigation proven (this run
      happened to hit zero retryable 400s).
- [ ] Surface failed filenames in the MCP tool *result*, not just stderr —
      the MCP client only sees the error count, and stderr isn't retained in
      Claude Code's MCP logs; getting the filenames today required a full CLI
      re-index.
- [ ] Guard against install-drift generally: version string in the script +
      a `rag doctor`-style check comparing repo HEAD vs deployed copy.
- [ ] Cosmetic: CLI `index` prints the summary twice (function prints to
      stderr and `cmd_index` prints the return value).

### Proactive synthesis tool (`rag_synthesize`)

Add a tool that chains `rag_search` across multiple tiers, combines the retrieved
chunks into a single context window, and passes them to the connected LLM to produce
a synthesised summary rather than raw retrieval results.

Use cases:
- "Summarise everything we know about X across all tiers"
- "What do we know about this error message?" (searches common-issues + project + devops)
- "Give me a briefing on this project before I start work" (project KB + recent sessions)
- Onboarding a new developer — pull framework conventions, project patterns, and known issues in one shot

Design notes:
- Accept `query`, `tiers`, `k`, and a `synthesize=True` flag (or separate tool name)
- Collect results from each tier separately, preserving source labels
- Build a structured prompt: retrieved chunks with tier/source headers, then ask the
  model to synthesise into a coherent answer
- Return both the synthesis and the raw sources so the caller can cite them
- The LLM doing the synthesis is whichever model the MCP client is connected to —
  no separate model config needed in the MCP server itself

Related: `rag_search` already returns raw chunks with relevance scores; synthesis
is purely a prompt-engineering layer on top of the existing retrieval pipeline.

---

## Infrastructure

### Phase 1 — Qdrant migration

Open WebUI currently ships with ChromaDB embedded. Switching to the Qdrant container
in `docker-compose.yml` requires:
- Setting `VECTOR_DB=qdrant` and `QDRANT_URI` in Open WebUI env (already in compose)
- Re-uploading all existing KB content after migration (ChromaDB and Qdrant use
  separate storage; content does not carry over automatically)
- Verifying embedding dimensions match between old and new backends

### Phase 2 — Seed Tier 1 framework KBs

Populate `framework-{name}` KBs with reference material for each supported framework.
Priority order: Symfony, Drupal, WordPress, CakePHP, Laravel.

Sources to consider: official docs snapshots, hook/event catalogues, service container
reference, common configuration patterns.

### Optional — llama.cpp inference backend

Add a commented-out `llama-backend` service to `docker-compose.yml` as an alternative
to Ollama for users who want a self-contained stack (no separate Ollama install).
Uses `ghcr.io/ggerganov/llama.cpp:server-cuda`, mounts a local `models/` directory,
and exposes port 8080. Point `OLLAMA_BASE_URL` at `http://llama-backend:8080` to use it.
Include an `export-ollama.sh` helper that copies the largest Ollama blob into `models/`
for users migrating from Ollama.

### Phase 4 — Project indexer improvements

`rag_index_project` currently indexes whole files. Improvements:
- Chunk large files (>500 lines) with overlap before upload so retrieval is more precise
- Add `.js`, `.ts`, `.json` to indexed extensions for JS-heavy projects
- Support a per-project `.ragignore` file to exclude paths beyond the global defaults
- Progress reporting for large codebases

### Phase 5 — Hermes session hooks

Wire up automatic RAG actions at session boundaries:
- `on_session_start`: call `rag_search` with a project briefing query and inject
  results into the system prompt
- `on_session_end`: prompt the agent to save discoveries via `rag_add_issue` /
  `rag_add_doc` before context is lost

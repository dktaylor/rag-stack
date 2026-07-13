#!/usr/bin/env python3
"""
openwebui-mcp.py — Configurable-tier MCP stdio server bridging AI clients to Open WebUI RAG

Tier structure is loaded from tiers.json (auto-discovered or via RAG_TIERS_CONFIG env var).
Falls back to a built-in 5-tier PHP/DevOps config if no tiers.json is found.

Tier types:
  fixed     — one KB with a fixed name (e.g. "common-issues")
  framework — KB per framework, auto-detected from composer.json (kb_pattern: "framework-{name}")
  project   — KB per project, auto-detected from CWD basename (kb_pattern: "project-{slug}")
  os        — KB per OS/distro, auto-detected from /etc/os-release (kb_pattern: "os-{distro}")

Tier properties:
  auto_include: true — always appended to rag_search() results when the KB exists,
                       regardless of what tiers the caller passes or what default_tiers says

Tools: rag_search, rag_add_doc, rag_add_issue, rag_index_project, rag_list_kbs

Config via environment:
  OPENWEBUI_URL      base URL (default: http://localhost:3000)
  OPENWEBUI_TOKEN    JWT auth token
  RAG_CWD_DETECT     "1" to auto-detect project/framework from CWD (default: 1)
  RAG_TIERS_CONFIG   explicit path to tiers.json (overrides auto-discovery)

MCP transport: NDJSON (newline-delimited JSON-RPC 2.0)
"""

import json
import os
import sys
import uuid
import platform
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE            = os.environ.get("OPENWEBUI_URL",       "http://localhost:3000")
TOKEN           = os.environ.get("OPENWEBUI_TOKEN",     "")
CWD_DETECT      = os.environ.get("RAG_CWD_DETECT",      "1") == "1"
QDRANT_URL      = os.environ.get("QDRANT_URL",          "http://localhost:6333")
QDRANT_COLL     = os.environ.get("QDRANT_COLLECTION",   "open-webui_knowledge")

EXCLUDED_DIRS = {
    "vendor", "var", "node_modules", "core", ".git", ".idea",
    "__pycache__", "dist", "build", "cache", ".cache",
}
INDEXED_EXTENSIONS = {".php", ".twig", ".yml", ".yaml", ".env.example", ".md"}

# ---------------------------------------------------------------------------
# Tier registry — populated at startup from tiers.json
# ---------------------------------------------------------------------------
TIER_REGISTRY: list[dict] = []
DEFAULT_TIERS: list[str]  = []

_BUILTIN_CONFIG: dict = {
    "default_tiers": ["framework", "project", "common-issues"],
    "tiers": [
        {
            "id": "framework", "type": "framework",
            "kb_pattern": "framework-{name}", "label": "Tier 1",
            "description": "Framework/CMS reference — hooks, APIs, services, patterns",
        },
        {
            "id": "project", "type": "project",
            "kb_pattern": "project-{slug}", "label": "Tier 2",
            "description": "Per-project source code and project-specific configuration",
        },
        {
            "id": "common-issues", "type": "fixed",
            "kb": "common-issues", "label": "Tier 3",
            "description": "Cross-cutting gotchas, bugs, and non-obvious fixes across all stacks",
        },
        {
            "id": "devops-general", "type": "fixed",
            "kb": "devops-general", "label": "Tier 4",
            "description": "Infrastructure reference: Docker, k8s, nginx, SSL (platform-agnostic)",
        },
        {
            "id": "os", "type": "os",
            "kb_pattern": "os-{distro}", "label": "Tier 5",
            "description": "OS-specific reference: package names, firewall rules, SELinux, distro-specific fixes",
            "auto_include": True,
        },
    ],
}


def _find_tiers_config() -> Path | None:
    """Locate tiers.json: explicit env var → $RAG_INSTALL_DIR → repo root next to this script."""
    if env := os.environ.get("RAG_TIERS_CONFIG"):
        p = Path(env)
        return p if p.exists() else None
    p = Path(os.environ.get("RAG_INSTALL_DIR", "/opt/rag-stack")) / "tiers.json"
    if p.exists():
        return p
    # Dev path: script lives at mcp/openwebui-mcp.py, tiers.json is at repo root
    p = Path(__file__).parent.parent / "tiers.json"
    if p.exists():
        return p
    return None


def _load_tiers() -> None:
    """Populate TIER_REGISTRY and DEFAULT_TIERS from tiers.json, or built-in defaults."""
    global TIER_REGISTRY, DEFAULT_TIERS
    config_path = _find_tiers_config()
    if config_path:
        try:
            data = json.loads(config_path.read_text())
            TIER_REGISTRY = data.get("tiers", [])
            DEFAULT_TIERS = data.get("default_tiers", [])
            print(
                f"openwebui-mcp: tiers loaded from {config_path} ({len(TIER_REGISTRY)} tiers)",
                file=sys.stderr,
            )
            return
        except Exception as e:
            print(
                f"openwebui-mcp: warning: failed to load {config_path}: {e} — using built-in defaults",
                file=sys.stderr,
            )
    else:
        print("openwebui-mcp: tiers.json not found — using built-in defaults", file=sys.stderr)
    TIER_REGISTRY = _BUILTIN_CONFIG["tiers"]
    DEFAULT_TIERS = _BUILTIN_CONFIG["default_tiers"]


# ---------------------------------------------------------------------------
# KB cache — name → id
# ---------------------------------------------------------------------------
_KB_CACHE: dict[str, str] = {}


def _refresh_kb_cache() -> None:
    global _KB_CACHE
    try:
        resp  = _http("GET", "/api/v1/knowledge/")
        items = resp if isinstance(resp, list) else resp.get("data", resp.get("items", []))
        _KB_CACHE = {item["name"]: item["id"] for item in items if "name" in item and "id" in item}
    except Exception as e:
        print(f"openwebui-mcp: warning: KB cache refresh failed: {e}", file=sys.stderr)


def _ensure_kb(name: str) -> str:
    """Return the KB id for name, creating the KB if it doesn't exist."""
    if name not in _KB_CACHE:
        _refresh_kb_cache()
    if name not in _KB_CACHE:
        result = _http("POST", "/api/v1/knowledge/create", {
            "name": name,
            "description": _kb_description(name),
            "access_control": None,
        })
        _KB_CACHE[name] = result["id"]
    return _KB_CACHE[name]


def _kb_description(name: str) -> str:
    """Generate a description for a new KB by matching its name against the tier registry."""
    for tier in TIER_REGISTRY:
        label = tier.get("label", tier["id"])
        desc  = tier["description"]
        if tier["type"] == "fixed" and tier.get("kb") == name:
            return f"{label} — {desc}"
        prefix = tier.get("kb_pattern", "").split("{")[0]
        if prefix and name.startswith(prefix):
            suffix = name[len(prefix):]
            return f"{label} — {suffix.title()} {desc}" if suffix else f"{label} — {desc}"
    return f"RAG knowledge base: {name}"


# ---------------------------------------------------------------------------
# Context detection
# ---------------------------------------------------------------------------
def _detect_context(cwd: str | None = None) -> tuple[str | None, str | None]:
    """Return (project_slug, framework) inferred from the given directory."""
    cwd  = cwd or os.getcwd()
    root = Path(cwd)
    project = root.name

    composer = root / "composer.json"
    if composer.exists():
        try:
            data = json.loads(composer.read_text())
            req  = {**data.get("require", {}), **data.get("require-dev", {})}
            if "symfony/framework-bundle" in req:
                return project, "symfony"
            if "drupal/core" in req or "drupal/core-recommended" in req:
                return project, "drupal"
            if "cakephp/cakephp" in req:
                return project, "cakephp"
            if "laravel/framework" in req:
                return project, "laravel"
            if "johnpbloch/wordpress" in req or "wordpress" in str(req):
                return project, "wordpress"
        except Exception:
            pass

    if (root / "web" / "core").is_dir() or (root / "core" / "lib" / "Drupal.php").exists():
        return project, "drupal"
    if (root / "wp-config.php").exists() or (root / "wp-content").is_dir():
        return project, "wordpress"

    return project, None


def _detect_os() -> str:
    """Return a short OS identifier, e.g. 'linux-fedora', 'macos'."""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        distro = line.strip().split("=")[1].strip('"').lower()
                        return f"linux-{distro}"
        except Exception:
            pass
        return "linux"
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    return "unknown"


def _os_distro() -> str:
    """Return just the distro name used in kb_pattern substitution: 'fedora', 'macos', etc."""
    return _detect_os().removeprefix("linux-")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _http(method: str, path: str, data=None, files=None):
    url = f"{BASE}{path}"
    if files:
        boundary = uuid.uuid4().hex
        parts = []
        for field, (fname, content, ctype) in files.items():
            header = (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
                f'Content-Type: {ctype}\r\n\r\n'
            ).encode()
            body_part = content if isinstance(content, bytes) else content.encode()
            parts.append(header + body_part + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        req  = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif data is not None:
        body = json.dumps(data).encode()
        req  = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode()[:300]}")


def _purge_qdrant_vectors(kb_id: str) -> None:
    """
    Delete all Qdrant vectors whose tenant_id matches the KB UUID.

    Open WebUI does not remove Qdrant vectors when files are deleted via its API,
    leaving orphan vectors that cause HTTP 400 on subsequent re-uploads (content hash
    collision). Calling this after clearing Open WebUI file records keeps the two
    stores in sync.
    """
    try:
        payload = json.dumps({
            "filter": {"must": [{"key": "tenant_id", "match": {"value": kb_id}}]}
        }).encode()
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{QDRANT_COLL}/points/delete",
            data=payload,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            status = result.get("result", {}).get("status", "unknown")
            print(f"openwebui-mcp: Qdrant purge KB={kb_id} status={status}", file=sys.stderr)
    except Exception as e:
        print(f"openwebui-mcp: Qdrant purge warning (non-fatal): {e}", file=sys.stderr)


def _upload_file(name: str, content: str | bytes, kb_id: str, skip_dedup: bool = False) -> str:
    """
    Upload a file and attach it to a KB.

    skip_dedup=True skips the existing-file lookup, which is O(n) per file and
    causes quadratic slowdown during bulk indexing. Use it when the KB has already
    been cleared (e.g. inside rag_index_project).
    """
    if not skip_dedup:
        try:
            existing = _http("GET", "/api/v1/files/")
            items    = existing if isinstance(existing, list) else existing.get("items", [])
            for f in items:
                if f.get("meta", {}).get("name") == name:
                    fid = f["id"]
                    try:
                        _http("POST", f"/api/v1/knowledge/{kb_id}/file/remove", {"file_id": fid})
                        _http("DELETE", f"/api/v1/files/{fid}")
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    new_file = _http("POST", "/api/v1/files/", files={"file": (name, content, "text/plain")})
    fid      = new_file["id"]
    _http("POST", f"/api/v1/knowledge/{kb_id}/file/add", {"file_id": fid})
    return fid


# ---------------------------------------------------------------------------
# Tier / KB resolution
# ---------------------------------------------------------------------------
def _resolve_kb_name(tier: dict, framework: str | None, project: str | None) -> str | None:
    """Return the concrete KB name for a tier given the current context, or None if unresolvable."""
    t = tier["type"]
    if t == "fixed":
        return tier.get("kb")
    pattern = tier.get("kb_pattern", "")
    if t == "framework":
        return pattern.replace("{name}", framework.lower()) if framework else None
    if t == "project":
        return pattern.replace("{slug}", project.lower()) if project else None
    if t == "os":
        return pattern.replace("{distro}", _os_distro())
    return None


def _resolve_kb_ids(tiers: list, framework: str | None, project: str | None) -> list[str]:
    """Map a list of tier IDs to concrete KB IDs present in the cache."""
    ids: list[str] = []
    for tier_id in tiers:
        tier = next((t for t in TIER_REGISTRY if t["id"] == tier_id), None)
        if not tier:
            continue
        if tier["type"] == "framework" and not framework:
            # No framework specified — include all matching KBs
            prefix = tier.get("kb_pattern", "").split("{")[0]
            ids.extend(v for k, v in _KB_CACHE.items() if prefix and k.startswith(prefix))
        else:
            name = _resolve_kb_name(tier, framework, project)
            if name and name in _KB_CACHE:
                ids.append(_KB_CACHE[name])
    seen: set = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


def _project_kb_name(slug: str) -> str:
    """Return the KB name for a project slug using the configured project-type tier pattern."""
    for tier in TIER_REGISTRY:
        if tier["type"] == "project":
            return tier["kb_pattern"].replace("{slug}", slug.lower())
    return f"project-{slug}"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def rag_search(
    query: str,
    k: int = 5,
    tiers: list | None = None,
    framework: str | None = None,
    project: str | None = None,
) -> str:
    # None means "use defaults"; an explicit list replaces the defaults entirely
    if tiers is None:
        tiers = list(DEFAULT_TIERS)

    if CWD_DETECT:
        det_project, det_framework = _detect_context()
        if framework is None:
            framework = det_framework
        if project is None:
            project = det_project

    _refresh_kb_cache()
    kb_ids = _resolve_kb_ids(tiers, framework, project)

    # Always append auto_include tiers when their KB exists, regardless of tiers list
    for tier in TIER_REGISTRY:
        if not tier.get("auto_include") or tier["id"] in tiers:
            continue
        name = _resolve_kb_name(tier, framework, project)
        if name:
            kb_id = _KB_CACHE.get(name)
            if kb_id and kb_id not in kb_ids:
                kb_ids.append(kb_id)

    if not kb_ids:
        avail = ", ".join(sorted(_KB_CACHE)) or "none"
        return (
            f"No knowledge bases matched tiers={tiers} framework={framework} project={project}.\n"
            f"Available KBs: {avail}\n"
            "Use rag_list_kbs() to see all KBs, or rag_index_project() to create a project KB."
        )

    result    = _http("POST", "/api/v1/retrieval/query/collection", {
        "collection_names": kb_ids,
        "query": query,
        "k": k,
    })
    docs      = result.get("documents", [[]])[0]
    metas     = result.get("metadatas", [[]])[0]
    distances = result.get("distances",  [[]])[0]

    if not docs:
        return f"No results for {query!r} across {len(kb_ids)} KB(s)."

    ctx   = f"tiers={tiers}, framework={framework or 'any'}, project={project or 'none'}"
    lines = [f"Query: {query!r} [{ctx}]\n"]
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, distances), 1):
        source = meta.get("name", "unknown")
        score  = round(1 - dist, 3)
        lines.append(f"[{i}] {source}  (relevance: {score})\n{doc}\n")
    return "\n---\n".join(lines)


def rag_add_doc(
    name: str,
    content: str,
    tier: str,
    framework: str | None = None,
    project: str | None = None,
    tags: list | None = None,
) -> str:
    tier     = tier.strip().lower()
    tier_def = next((t for t in TIER_REGISTRY if t["id"] == tier), None)
    if not tier_def:
        valid = " | ".join(t["id"] for t in TIER_REGISTRY)
        return f"Error: unknown tier '{tier}'. Valid values: {valid}"

    t = tier_def["type"]
    if t == "framework":
        if not framework:
            return f"Error: 'framework' param required when tier='{tier}'"
        kb_name = tier_def["kb_pattern"].replace("{name}", framework.lower())
    elif t == "project":
        if not project:
            if CWD_DETECT:
                project, _ = _detect_context()
            if not project:
                return f"Error: 'project' param required when tier='{tier}'"
        kb_name = tier_def["kb_pattern"].replace("{slug}", project.lower())
    elif t == "fixed":
        kb_name = tier_def["kb"]
    elif t == "os":
        kb_name = tier_def["kb_pattern"].replace("{distro}", _os_distro())
    else:
        return f"Error: tier '{tier}' has unknown type '{t}'"

    if tags:
        content = f"tags: {', '.join(tags)}\n\n{content}"

    kb_id = _ensure_kb(kb_name)
    _upload_file(name, content, kb_id)
    return f"Uploaded '{name}' → {kb_name} (tier: {tier})."


def rag_add_issue(name: str, content: str, tags: list | None = None) -> str:
    """Add a cross-cutting gotcha or bug fix. Targets the 'common-issues' fixed tier."""
    tier_def = next(
        (t for t in TIER_REGISTRY if t["id"] == "common-issues" and t["type"] == "fixed"),
        None,
    )
    kb_name = tier_def["kb"] if tier_def else "common-issues"

    if tags:
        content = f"tags: {', '.join(tags)}\n\n{content}"
    kb_id   = _ensure_kb(kb_name)
    _upload_file(name, content, kb_id)
    tag_str = ", ".join(tags) if tags else "none"
    return f"Added issue '{name}' to {kb_name} (tags: {tag_str})."


def rag_index_project(path: str | None = None, project: str | None = None) -> str:
    """
    Index a project's source into the project-type tier KB.
    Auto-detects framework from composer.json or directory structure.
    Clears and rebuilds the KB on each run.
    """
    root = Path(path or os.getcwd()).resolve()
    if not root.is_dir():
        return f"Error: '{root}' is not a directory."

    det_project, framework = _detect_context(str(root))
    slug    = (project or det_project).lower().replace(" ", "-")
    kb_name = _project_kb_name(slug)

    print(
        f"openwebui-mcp: indexing [{slug}] framework=[{framework or 'generic'}] from {root}",
        file=sys.stderr,
    )

    kb_id = _ensure_kb(kb_name)

    # Clear existing content from Open WebUI file store
    try:
        existing = _http("GET", "/api/v1/files/")
        items    = existing if isinstance(existing, list) else existing.get("items", [])
        cleared  = 0
        for f in items:
            if f.get("meta", {}).get("name", "").startswith(f"{slug}--"):
                try:
                    _http("POST", f"/api/v1/knowledge/{kb_id}/file/remove", {"file_id": f["id"]})
                    _http("DELETE", f"/api/v1/files/{f['id']}")
                    cleared += 1
                except Exception:
                    pass
        if cleared:
            print(f"openwebui-mcp: cleared {cleared} existing file(s) from {kb_name}", file=sys.stderr)
    except Exception as e:
        print(f"openwebui-mcp: warning: could not clear KB: {e}", file=sys.stderr)

    # Purge orphan Qdrant vectors — Open WebUI does not remove vectors when files
    # are deleted, leaving stale content hashes that cause HTTP 400 on re-upload.
    _purge_qdrant_vectors(kb_id)

    uploaded = errors = 0
    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in fpath.relative_to(root).parts):
            continue
        if fpath.suffix not in INDEXED_EXTENSIONS:
            continue

        rel = fpath.relative_to(root)
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not content.strip():
            continue

        rag_name = f"{slug}--{str(rel).replace('/', '--')}"
        try:
            _upload_file(rag_name, content, kb_id, skip_dedup=True)
            uploaded += 1
        except Exception as e:
            print(f"openwebui-mcp: skipped {rel}: {e}", file=sys.stderr)
            errors += 1

    result = (
        f"Indexed '{slug}' (framework: {framework or 'generic'}) → {kb_name}\n"
        f"  {uploaded} files uploaded"
        + (f", {errors} errors" if errors else "")
    )
    print(f"openwebui-mcp: {result}", file=sys.stderr)

    if uploaded > 0:
        indexes_conf = Path(os.environ.get("RAG_INSTALL_DIR", "/opt/rag-stack")) / "indexes.conf"
        try:
            existing_lines = indexes_conf.read_text().splitlines() if indexes_conf.exists() else []
            root_str = str(root)
            if root_str not in existing_lines:
                with indexes_conf.open("a") as fh:
                    fh.write(root_str + "\n")
        except Exception:
            pass

    return result


def rag_list_kbs() -> str:
    """List all knowledge bases grouped by tier, with file counts."""
    _refresh_kb_cache()
    if not _KB_CACHE:
        return "No knowledge bases found. Use rag_add_doc() or rag_index_project() to create one."

    buckets: dict[str, list[str]] = {t["id"]: [] for t in TIER_REGISTRY}
    buckets["other"] = []

    for name in sorted(_KB_CACHE):
        kb_id = _KB_CACHE[name]
        try:
            detail = _http("GET", f"/api/v1/knowledge/{kb_id}")
            count  = len(detail.get("files") or [])
        except Exception:
            count = "?"

        entry  = f"  {name}  ({count} files)  [{kb_id[:8]}...]"
        bucket = "other"
        for tier in TIER_REGISTRY:
            if tier["type"] == "fixed" and tier.get("kb") == name:
                bucket = tier["id"]
                break
            prefix = tier.get("kb_pattern", "").split("{")[0]
            if prefix and name.startswith(prefix):
                bucket = tier["id"]
                break
        buckets[bucket].append(entry)

    lines = [f"{len(_KB_CACHE)} knowledge base(s):"]
    for tier in TIER_REGISTRY:
        tid = tier["id"]
        if buckets[tid]:
            label = tier.get("label", tid)
            lines.append(f"\n{label} — {tid}:")
            lines.extend(buckets[tid])
    if buckets["other"]:
        lines.append("\nOther:")
        lines.extend(buckets["other"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP tool registry — built dynamically after tiers are loaded
# ---------------------------------------------------------------------------
TOOLS: list[dict] = []

TOOL_FNS: dict[str, callable] = {
    "rag_search":        lambda a: rag_search(
                             a["query"], int(a.get("k", 5)),
                             a.get("tiers"), a.get("framework"), a.get("project"),
                         ),
    "rag_add_doc":       lambda a: rag_add_doc(
                             a["name"], a["content"], a["tier"],
                             a.get("framework"), a.get("project"), a.get("tags"),
                         ),
    "rag_add_issue":     lambda a: rag_add_issue(a["name"], a["content"], a.get("tags")),
    "rag_index_project": lambda a: rag_index_project(a.get("path"), a.get("project")),
    "rag_list_kbs":      lambda a: rag_list_kbs(),
}


def _build_tools() -> list[dict]:
    """Build MCP tool schemas reflecting the loaded tier configuration."""
    valid_tiers = " | ".join(t["id"] for t in TIER_REGISTRY)
    auto_ids    = [t["id"] for t in TIER_REGISTRY if t.get("auto_include")]
    auto_note   = (
        f" Tier(s) {', '.join(auto_ids)} are always auto-included when their KB exists."
        if auto_ids else ""
    )

    return [
        {
            "name": "rag_search",
            "description": (
                "Search the RAG knowledge base across one or more tiers. "
                "Call this FIRST before making changes — query for prior decisions, patterns, and fixes. "
                f"Default tiers (when tiers is omitted): {DEFAULT_TIERS}. "
                "Passing tiers replaces the defaults — it does not add to them. "
                f"{auto_note} "
                "Auto-detects framework and project from CWD when not specified."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "k": {
                        "type": "integer",
                        "description": "Results per KB (default 5)",
                        "default": 5,
                    },
                    "tiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"Tiers to search. Omit to use defaults {DEFAULT_TIERS}. "
                            "Providing this list replaces the defaults entirely — include all tiers you want. "
                            f"Auto-include tiers ({', '.join(auto_ids) if auto_ids else 'none'}) "
                            "are always appended regardless."
                        ),
                        "default": DEFAULT_TIERS,
                    },
                    "framework": {
                        "type": "string",
                        "description": "Framework filter (auto-detected from CWD if omitted)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug (auto-detected from CWD if omitted)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "rag_add_doc",
            "description": (
                "Add or replace a document in a specific RAG tier. "
                "Use after significant changes to scripts, configs, or session summaries. "
                "Naming convention: NN-category--filename (e.g. '03-scripts--deploy.sh'). "
                f"tier: {valid_tiers}. "
                "framework type requires framework param; "
                "project type auto-detects from CWD; "
                "os type auto-detects host distro."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string", "description": "Filename slug"},
                    "content":   {"type": "string", "description": "Document content"},
                    "tier":      {"type": "string", "description": f"Tier ID: {valid_tiers}"},
                    "framework": {"type": "string", "description": "Required when tier type is 'framework'"},
                    "project":   {"type": "string", "description": "Project slug — defaults to CWD basename"},
                    "tags":      {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
                },
                "required": ["name", "content", "tier"],
            },
        },
        {
            "name": "rag_add_issue",
            "description": (
                "Add a cross-cutting issue, gotcha, or bug fix to the 'common-issues' KB. "
                "Call immediately after solving something non-obvious — don't wait for session end. "
                "Tag with affected stack, symptom keywords, and OS if relevant."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short slug, e.g. 'docker-firewalld-zone-conflict'",
                    },
                    "content": {
                        "type": "string",
                        "description": "Problem description, root cause, and fix",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "e.g. ['docker', 'linux', 'firewalld', 'networking']",
                    },
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "rag_index_project",
            "description": (
                "Index a project's source files into the project-type tier KB. "
                "Auto-detects framework from composer.json or directory structure. "
                "Clears and rebuilds the KB on each run — always reflects current source. "
                "Excludes vendor/, core/, node_modules/, var/, .git/. "
                "Indexes .php, .twig, .yml, .yaml, .env.example, .md files."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "Absolute project root path (defaults to CWD)"},
                    "project": {"type": "string", "description": "Slug override (defaults to directory name)"},
                },
                "required": [],
            },
        },
        {
            "name": "rag_list_kbs",
            "description": "List all knowledge bases grouped by tier, with file counts.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
    ]


# ---------------------------------------------------------------------------
# MCP stdio transport — NDJSON (one JSON object per line)
# ---------------------------------------------------------------------------
def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def recv() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def handle(req: dict) -> dict | None:
    rid    = req.get("id")
    method = req.get("method", "")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "openwebui-mcp", "version": "3.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name not in TOOL_FNS:
            return {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        try:
            text = TOOL_FNS[name](args)
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    if rid is not None:
        return {
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main() -> None:
    global TOOLS
    if not TOKEN:
        print("ERROR: OPENWEBUI_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    _load_tiers()
    _refresh_kb_cache()
    TOOLS = _build_tools()

    os_tag   = _detect_os()
    tier_ids = ", ".join(t["id"] for t in TIER_REGISTRY)
    print(
        f"openwebui-mcp v3.0: {BASE} | {len(_KB_CACHE)} KBs | OS={os_tag} | tiers=[{tier_ids}]",
        file=sys.stderr,
    )

    while True:
        req = recv()
        if req is None:
            break
        resp = handle(req)
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()

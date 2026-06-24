#!/usr/bin/env python3
"""
rag_cli.py — Command-line interface for the RAG stack tools.

Called by the 'rag' CLI script for search, add, index, tiers, and init-tiers commands.
Loads .env from $RAG_INSTALL_DIR/.env (default /opt/rag-stack/.env),
then delegates to the tool functions in openwebui-mcp.py.

Usage (via 'rag' shell script):
  rag search "query" [--k 5] [--tiers t1,t2] [--framework fw] [--project slug]
  rag add <file>  --tier <tier> [--framework fw] [--project slug] [--tags t1,t2]
  rag add --name slug --content "text" --tier <tier> [options]
  rag index [path] [--project slug]
  rag tiers
  rag init-tiers
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path


RAG_DIR = Path(os.environ.get("RAG_INSTALL_DIR", "/opt/rag-stack"))


def _load_env() -> None:
    env_file = RAG_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _load_mcp():
    _load_env()
    mcp_path = RAG_DIR / "mcp" / "openwebui-mcp.py"
    if not mcp_path.exists():
        print(f"rag: MCP script not found at {mcp_path}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("openwebui_mcp", mcp_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._load_tiers()  # populate TIER_REGISTRY and DEFAULT_TIERS before first use
    return mod


def cmd_search(args: argparse.Namespace) -> None:
    mcp   = _load_mcp()
    tiers = args.tiers.split(",") if args.tiers else None
    print(mcp.rag_search(
        query=args.query,
        k=args.k,
        tiers=tiers,
        framework=args.framework,
        project=args.project,
    ))


def cmd_add(args: argparse.Namespace) -> None:
    mcp     = _load_mcp()
    name    = args.name
    content = args.content

    if args.file:
        fpath = Path(args.file)
        if not fpath.exists():
            print(f"rag add: file not found: {fpath}", file=sys.stderr)
            sys.exit(1)
        content = fpath.read_text(encoding="utf-8", errors="replace")
        if not name:
            name = fpath.name

    if not name:
        print("rag add: --name required when using --content", file=sys.stderr)
        sys.exit(1)
    if not content:
        print("rag add: provide a <file> path or --content text", file=sys.stderr)
        sys.exit(1)
    if not args.tier:
        valid = " | ".join(t["id"] for t in mcp.TIER_REGISTRY) if mcp.TIER_REGISTRY else "..."
        print(f"rag add: --tier required ({valid})", file=sys.stderr)
        sys.exit(1)

    tags = args.tags.split(",") if args.tags else None
    print(mcp.rag_add_doc(
        name=name,
        content=content,
        tier=args.tier,
        framework=args.framework,
        project=args.project,
        tags=tags,
    ))


def cmd_index(args: argparse.Namespace) -> None:
    mcp  = _load_mcp()
    path = args.path or os.getcwd()
    print(mcp.rag_index_project(path=path, project=args.project))


def cmd_tiers(args: argparse.Namespace) -> None:
    """Print the configured tier registry without making any API calls."""
    mcp = _load_mcp()
    if not mcp.TIER_REGISTRY:
        print("No tiers configured.")
        return
    print(f"Configured tiers ({len(mcp.TIER_REGISTRY)}):")
    for tier in mcp.TIER_REGISTRY:
        tid   = tier["id"]
        t     = tier["type"]
        label = tier.get("label", "")
        kb    = tier.get("kb") or tier.get("kb_pattern", "")
        flags = []
        if tid in mcp.DEFAULT_TIERS:
            flags.append("default")
        if tier.get("auto_include"):
            flags.append("auto-include")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {label:8}  {tid:<20}  type={t:<10}  kb={kb}{flag_str}")
    print(f"\nDefault search tiers: {mcp.DEFAULT_TIERS}")
    print("(Passing tiers= to rag_search replaces the defaults, not adds to them.)")


def cmd_init_tiers(args: argparse.Namespace) -> None:
    """Pre-create all fixed-type tier KBs so they exist before any agent uses them."""
    mcp = _load_mcp()
    mcp._refresh_kb_cache()
    created = skipped = 0
    for tier in mcp.TIER_REGISTRY:
        if tier["type"] != "fixed":
            continue
        kb_name = tier["kb"]
        if kb_name in mcp._KB_CACHE:
            print(f"  {kb_name}: already exists")
            skipped += 1
        else:
            kb_id = mcp._ensure_kb(kb_name)
            print(f"  {kb_name}: created [{kb_id[:8]}...]")
            created += 1
    print(f"Done: {created} created, {skipped} already existed.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="rag", add_help=True)
    sub    = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search knowledge bases")
    p_search.add_argument("query")
    p_search.add_argument("--k", type=int, default=5, help="Results per KB (default 5)")
    p_search.add_argument("--tiers", help="Comma-separated tier IDs (replaces defaults)")
    p_search.add_argument("--framework", help="Filter to framework KB: drupal, symfony, wordpress...")
    p_search.add_argument("--project", help="Project slug (default: CWD name)")

    p_add = sub.add_parser("add", help="Add a document to a tier KB")
    p_add.add_argument("file", nargs="?", help="Path to file to upload")
    p_add.add_argument("--name", help="Document slug (default: filename)")
    p_add.add_argument("--content", help="Inline text content (alternative to file)")
    p_add.add_argument("--tier", help="Tier ID — run 'rag tiers' to see configured tiers")
    p_add.add_argument("--framework", help="Required when tier type is 'framework'")
    p_add.add_argument("--project", help="Project slug (default: CWD name)")
    p_add.add_argument("--tags", help="Comma-separated tags")

    p_index = sub.add_parser("index", help="Index a project directory into the project-type tier KB")
    p_index.add_argument("path", nargs="?", help="Project root path (default: CWD)")
    p_index.add_argument("--project", help="Slug override (default: directory name)")

    sub.add_parser("tiers", help="List configured tiers and their search behaviour")
    sub.add_parser("init-tiers", help="Pre-create fixed-type tier KBs in Open WebUI")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "index":
        cmd_index(args)
    elif args.command == "tiers":
        cmd_tiers(args)
    elif args.command == "init-tiers":
        cmd_init_tiers(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

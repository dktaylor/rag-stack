#!/usr/bin/env python3
"""
rag_cli.py — Command-line interface for the RAG stack tools.

Called by the 'rag' CLI script for search, add, and index commands.
Loads .env from $RAG_INSTALL_DIR/.env (default /opt/rag-stack/.env),
then delegates to the tool functions in openwebui-mcp.py.

Usage (via 'rag' shell script):
  rag search "query" [--k 5] [--tiers t1,t2] [--framework fw] [--project slug]
  rag add <file> --tier <tier> [--framework fw] [--project slug] [--tags t1,t2]
  rag add --name slug --content "text" --tier <tier> [options]
  rag index [path] [--project slug]
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
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_search(args: argparse.Namespace) -> None:
    mcp = _load_mcp()
    tiers = args.tiers.split(",") if args.tiers else None
    print(mcp.rag_search(
        query=args.query,
        k=args.k,
        tiers=tiers,
        framework=args.framework,
        project=args.project,
    ))


def cmd_add(args: argparse.Namespace) -> None:
    mcp = _load_mcp()
    name = args.name
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
        print("rag add: --tier required (framework|project|common-issues|devops-general)", file=sys.stderr)
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
    mcp = _load_mcp()
    path = args.path or os.getcwd()
    print(mcp.rag_index_project(path=path, project=args.project))


def main() -> None:
    parser = argparse.ArgumentParser(prog="rag", add_help=True)
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search knowledge bases")
    p_search.add_argument("query")
    p_search.add_argument("--k", type=int, default=5, help="Results per KB (default 5)")
    p_search.add_argument("--tiers", help="Comma-separated: framework,project,common-issues,devops-general")
    p_search.add_argument("--framework", help="Filter to framework KB: drupal, symfony, wordpress...")
    p_search.add_argument("--project", help="Project slug (default: CWD name)")

    p_add = sub.add_parser("add", help="Add a document to a tier KB")
    p_add.add_argument("file", nargs="?", help="Path to file to upload")
    p_add.add_argument("--name", help="Document slug (default: filename)")
    p_add.add_argument("--content", help="Inline text content (alternative to file)")
    p_add.add_argument("--tier", help="framework | project | common-issues | devops-general")
    p_add.add_argument("--framework", help="Required when tier=framework")
    p_add.add_argument("--project", help="Project slug (default: CWD name)")
    p_add.add_argument("--tags", help="Comma-separated tags")

    p_index = sub.add_parser("index", help="Index a project directory into Tier 2")
    p_index.add_argument("path", nargs="?", help="Project root path (default: CWD)")
    p_index.add_argument("--project", help="Slug override (default: directory name)")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "index":
        cmd_index(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

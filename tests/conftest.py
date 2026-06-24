"""
Session-scoped fixtures for RAG stack integration tests.

Bootstraps an isolated Open WebUI instance (via tests/docker-compose.test.yml),
imports openwebui-mcp.py with the live token, and provides a ready-to-use
`mcp` module fixture for all tests.
"""
import importlib.util
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WEBUI_URL = os.environ.get("OPENWEBUI_URL", "http://localhost:3001")
_REPO_ROOT = Path(__file__).parent.parent
_TIERS_JSON = _REPO_ROOT / "tiers.json"
_MCP_PATH = _REPO_ROOT / "mcp" / "openwebui-mcp.py"


def _http(method: str, url: str, data: dict | None = None, token: str | None = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        return json.loads(raw) if raw.startswith("{") else {"error": raw, "status": e.code}


@pytest.fixture(scope="session")
def webui_token() -> str:
    """Wait for Open WebUI health, bootstrap admin account, return permanent sk-* API key."""
    deadline = time.time() + 300  # 5-minute window for first boot
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{WEBUI_URL}/health", timeout=5)
            break
        except Exception:
            time.sleep(5)
    else:
        pytest.fail(f"Open WebUI did not become healthy at {WEBUI_URL} within 5 minutes")

    # Create admin account (silently succeeds on re-run if account already exists)
    _http("POST", f"{WEBUI_URL}/api/v1/auths/signup", {
        "name": "Test Admin",
        "email": "test@localhost",
        "password": "testpass1234",
    })

    resp = _http("POST", f"{WEBUI_URL}/api/v1/auths/signin", {
        "email": "test@localhost",
        "password": "testpass1234",
    })
    jwt = resp.get("token")
    assert jwt, f"Sign-in failed: {resp}"

    # Enable API key generation
    config = _http("GET", f"{WEBUI_URL}/api/v1/auths/admin/config", token=jwt)
    config["ENABLE_API_KEYS"] = True
    _http("POST", f"{WEBUI_URL}/api/v1/auths/admin/config", config, token=jwt)

    resp = _http("POST", f"{WEBUI_URL}/api/v1/auths/api_key", token=jwt)
    api_key = resp.get("api_key", "")
    assert api_key.startswith("sk-"), f"Expected sk-* API key, got: {resp}"
    return api_key


@pytest.fixture(scope="session")
def mcp(webui_token: str):
    """
    Import openwebui-mcp.py with the bootstrapped token and populated tier registry.

    Returns the module object so tests can call its public functions directly,
    with the same import pattern that rag_cli.py uses.
    """
    os.environ.setdefault("OPENWEBUI_URL", WEBUI_URL)
    os.environ["OPENWEBUI_TOKEN"] = webui_token
    os.environ["RAG_TIERS_CONFIG"] = str(_TIERS_JSON)
    os.environ["RAG_CWD_DETECT"] = "0"  # disable CWD auto-detection during tests

    spec = importlib.util.spec_from_file_location("openwebui_mcp", _MCP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Ensure module-level globals match regardless of import-time env resolution order
    mod.BASE = WEBUI_URL
    mod.TOKEN = webui_token
    mod.CWD_DETECT = False

    mod._load_tiers()
    mod._refresh_kb_cache()
    return mod

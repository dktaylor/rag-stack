"""
Unit tests for the file-store helpers: pagination, qdrant verification,
and retryable bulk upload.

These mock mcp._http / mcp._doc_in_qdrant directly — no Open WebUI or
Qdrant needed, so they run standalone (`pytest tests/test_file_store.py`)
as well as inside the docker test stack.

Regression context (2026-07-13): GET /api/v1/files/ caps every response at
one page, so the single-call clear/dedup loops silently leaked file records
on every reindex (8,047 stale prism rows), and Open WebUI's transient 400s
during bulk upload can persist vectors while reporting failure.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_MCP_PATH = Path(__file__).parent.parent / "mcp" / "openwebui-mcp.py"


@pytest.fixture()
def mcp(monkeypatch):
    """Import a fresh module instance without touching any live service."""
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test-token")
    monkeypatch.setenv("RAG_CWD_DETECT", "0")
    spec = importlib.util.spec_from_file_location("openwebui_mcp_unit", _MCP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["openwebui_mcp_unit"] = mod
    spec.loader.exec_module(mod)
    yield mod
    del sys.modules["openwebui_mcp_unit"]


def _page(ids, total):
    return {"items": [{"id": i, "meta": {"name": f"name-{i}"}} for i in ids],
            "total": total}


# ---------------------------------------------------------------------------
# _list_files pagination
# ---------------------------------------------------------------------------
class TestListFiles:
    def test_walks_all_pages_until_total(self, mcp, monkeypatch):
        pages = {1: _page(["a", "b"], 5), 2: _page(["c", "d"], 5), 3: _page(["e"], 5)}
        calls = []

        def fake_http(method, path, data=None, files=None):
            page = int(path.split("page=")[1])
            calls.append(page)
            return pages[page]

        monkeypatch.setattr(mcp, "_http", fake_http)
        out = mcp._list_files()
        assert [f["id"] for f in out] == ["a", "b", "c", "d", "e"]
        assert calls == [1, 2, 3]

    def test_terminates_when_server_ignores_page_param(self, mcp, monkeypatch):
        # Same page returned forever (no total honored) — must not loop.
        monkeypatch.setattr(mcp, "_http",
                            lambda m, p, data=None, files=None: _page(["a", "b"], 999))
        out = mcp._list_files()
        assert [f["id"] for f in out] == ["a", "b"]

    def test_handles_legacy_list_shape(self, mcp, monkeypatch):
        def fake_http(method, path, data=None, files=None):
            page = int(path.split("page=")[1])
            return [{"id": "x", "meta": {"name": "n"}}] if page == 1 else []

        monkeypatch.setattr(mcp, "_http", fake_http)
        assert [f["id"] for f in mcp._list_files()] == ["x"]

    def test_prefix_filter(self, mcp, monkeypatch):
        page = {"items": [
            {"id": "1", "meta": {"name": "prism--a.php"}},
            {"id": "2", "meta": {"name": "other--b.php"}},
            {"id": "3", "meta": {}},
        ], "total": 3}
        monkeypatch.setattr(mcp, "_http", lambda m, p, data=None, files=None: page)
        out = mcp._list_files(prefix="prism--")
        assert [f["id"] for f in out] == ["1"]

    def test_empty_store(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp, "_http",
                            lambda m, p, data=None, files=None: {"items": [], "total": 0})
        assert mcp._list_files() == []


# ---------------------------------------------------------------------------
# _upload_with_retry
# ---------------------------------------------------------------------------
class TestUploadWithRetry:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)

    def test_clean_upload(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp, "_upload_file", lambda *a, **k: "fid")
        assert mcp._upload_with_retry("n", "c", "kb") == "uploaded"

    def test_transient_failure_then_success(self, mcp, monkeypatch):
        attempts = []

        def flaky(*a, **k):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("HTTP 400 empty content")
            return "fid"

        monkeypatch.setattr(mcp, "_upload_file", flaky)
        monkeypatch.setattr(mcp, "_doc_in_qdrant", lambda kb, n: False)
        assert mcp._upload_with_retry("n", "c", "kb") == "uploaded"
        assert len(attempts) == 2

    def test_half_success_verified_in_qdrant(self, mcp, monkeypatch):
        # Open WebUI answered 400 but persisted the vectors: retrying would
        # collide with our own ghost — must detect and count as success.
        attempts = []

        def always_400(*a, **k):
            attempts.append(1)
            raise RuntimeError("HTTP 400 duplicate content")

        monkeypatch.setattr(mcp, "_upload_file", always_400)
        monkeypatch.setattr(mcp, "_doc_in_qdrant", lambda kb, n: True)
        assert mcp._upload_with_retry("n", "c", "kb") == "verified"
        assert len(attempts) == 1  # no futile retries once verified

    def test_hard_failure_exhausts_attempts(self, mcp, monkeypatch):
        attempts = []

        def always_400(*a, **k):
            attempts.append(1)
            raise RuntimeError("HTTP 400")

        monkeypatch.setattr(mcp, "_upload_file", always_400)
        monkeypatch.setattr(mcp, "_doc_in_qdrant", lambda kb, n: False)
        assert mcp._upload_with_retry("n", "c", "kb", attempts=3) == "failed"
        assert len(attempts) == 3


# ---------------------------------------------------------------------------
# _upload_file dedup path uses the paginated listing
# ---------------------------------------------------------------------------
class TestUploadFileDedup:
    def test_dedup_removes_every_record_with_name(self, mcp, monkeypatch):
        deleted = []

        def fake_http(method, path, data=None, files=None):
            if path.startswith("/api/v1/files/?page="):
                if path.endswith("page=1"):
                    return {"items": [
                        {"id": "old1", "meta": {"name": "doc"}},
                        {"id": "old2", "meta": {"name": "doc"}},
                        {"id": "keep", "meta": {"name": "other"}},
                    ], "total": 3}
                return {"items": [], "total": 3}
            if method == "DELETE":
                deleted.append(path.rsplit("/", 1)[1])
                return {}
            if path == "/api/v1/files/":
                return {"id": "new"}
            return {}

        monkeypatch.setattr(mcp, "_http", fake_http)
        fid = mcp._upload_file("doc", "content", "kb")
        assert fid == "new"
        assert deleted == ["old1", "old2"]  # both stale records, not just the first

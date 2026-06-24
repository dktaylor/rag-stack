"""
Integration tests for the RAG configurable-tier system.

Tests run against an isolated Open WebUI instance (docker-compose.test.yml).
Embedding-dependent tests (rag_search returning actual results) require Ollama
with nomic-embed-text running on the host; they won't fail if Ollama is absent —
rag_search() returns a "no results" message, which is still a valid string.
"""
import pytest


# ---------------------------------------------------------------------------
# Tier registry sanity
# ---------------------------------------------------------------------------
class TestTierRegistry:
    def test_registry_loaded(self, mcp):
        assert len(mcp.TIER_REGISTRY) > 0

    def test_default_tiers_not_empty(self, mcp):
        assert len(mcp.DEFAULT_TIERS) > 0

    def test_default_tiers_all_valid_ids(self, mcp):
        ids = {t["id"] for t in mcp.TIER_REGISTRY}
        for dt in mcp.DEFAULT_TIERS:
            assert dt in ids, f"default_tier '{dt}' references unknown tier id"

    def test_tier_ids_unique(self, mcp):
        ids = [t["id"] for t in mcp.TIER_REGISTRY]
        assert len(ids) == len(set(ids)), "Duplicate tier IDs found"

    def test_fixed_tiers_have_kb_field(self, mcp):
        for tier in mcp.TIER_REGISTRY:
            if tier["type"] == "fixed":
                assert "kb" in tier, f"Fixed tier '{tier['id']}' missing 'kb'"

    def test_non_fixed_tiers_have_kb_pattern(self, mcp):
        for tier in mcp.TIER_REGISTRY:
            if tier["type"] != "fixed":
                assert "kb_pattern" in tier, f"Tier '{tier['id']}' missing 'kb_pattern'"

    def test_at_least_one_auto_include_tier(self, mcp):
        auto = [t for t in mcp.TIER_REGISTRY if t.get("auto_include")]
        assert auto, "Expected at least one auto_include tier (os tier)"


# ---------------------------------------------------------------------------
# KB initialization
# ---------------------------------------------------------------------------
class TestInitTiers:
    def test_fixed_kbs_created(self, mcp):
        fixed = [t for t in mcp.TIER_REGISTRY if t["type"] == "fixed"]
        assert fixed, "No fixed-type tiers to test"
        for tier in fixed:
            kb_id = mcp._ensure_kb(tier["kb"])
            assert kb_id, f"_ensure_kb('{tier['kb']}') returned empty id"

    def test_fixed_kbs_in_cache(self, mcp):
        mcp._refresh_kb_cache()
        for tier in mcp.TIER_REGISTRY:
            if tier["type"] == "fixed":
                assert tier["kb"] in mcp._KB_CACHE, f"'{tier['kb']}' missing from KB cache"

    def test_ensure_kb_idempotent(self, mcp):
        fixed = next((t for t in mcp.TIER_REGISTRY if t["type"] == "fixed"), None)
        if not fixed:
            pytest.skip("No fixed-type tiers configured")
        id1 = mcp._ensure_kb(fixed["kb"])
        id2 = mcp._ensure_kb(fixed["kb"])
        assert id1 == id2, "_ensure_kb() returned different ID on second call"


# ---------------------------------------------------------------------------
# KB name resolution
# ---------------------------------------------------------------------------
class TestKbNameResolution:
    def test_fixed_resolves_to_kb_name(self, mcp):
        for tier in mcp.TIER_REGISTRY:
            if tier["type"] == "fixed":
                name = mcp._resolve_kb_name(tier, framework=None, project=None)
                assert name == tier["kb"]

    def test_framework_resolves_with_name(self, mcp):
        fw_tier = next((t for t in mcp.TIER_REGISTRY if t["type"] == "framework"), None)
        if not fw_tier:
            pytest.skip("No framework-type tier configured")
        name = mcp._resolve_kb_name(fw_tier, framework="drupal", project=None)
        assert name is not None
        assert "drupal" in name
        assert "{name}" not in name

    def test_framework_returns_none_without_name(self, mcp):
        fw_tier = next((t for t in mcp.TIER_REGISTRY if t["type"] == "framework"), None)
        if not fw_tier:
            pytest.skip("No framework-type tier configured")
        assert mcp._resolve_kb_name(fw_tier, framework=None, project=None) is None

    def test_project_resolves_with_slug(self, mcp):
        proj_tier = next((t for t in mcp.TIER_REGISTRY if t["type"] == "project"), None)
        if not proj_tier:
            pytest.skip("No project-type tier configured")
        name = mcp._resolve_kb_name(proj_tier, framework=None, project="my-app")
        assert name is not None
        assert "my-app" in name
        assert "{slug}" not in name

    def test_os_resolves_without_context(self, mcp):
        os_tier = next((t for t in mcp.TIER_REGISTRY if t["type"] == "os"), None)
        if not os_tier:
            pytest.skip("No os-type tier configured")
        name = mcp._resolve_kb_name(os_tier, framework=None, project=None)
        assert name is not None
        assert "{distro}" not in name
        assert mcp._os_distro() in name


# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
class TestOsDetection:
    def test_detect_os_returns_nonempty_string(self, mcp):
        result = mcp._detect_os()
        assert isinstance(result, str) and result

    def test_os_distro_strips_linux_prefix(self, mcp):
        distro = mcp._os_distro()
        assert isinstance(distro, str) and distro
        assert not distro.startswith("linux-"), "_os_distro() should strip 'linux-' prefix"


# ---------------------------------------------------------------------------
# rag_add_doc
# ---------------------------------------------------------------------------
class TestAddDoc:
    def test_add_to_fixed_tier(self, mcp):
        result = mcp.rag_add_doc(
            name="test-fixed-doc",
            content="Common issue: NULL pointer in checkout flow when cart is empty.",
            tier="common-issues",
        )
        assert "common-issues" in result, result
        assert "error" not in result.lower(), result

    def test_add_to_project_tier(self, mcp):
        result = mcp.rag_add_doc(
            name="test-project-doc",
            content="Redis TTL set to 3600 for session store.",
            tier="project",
            project="test-project",
        )
        assert "test-project" in result, result
        assert "error" not in result.lower(), result

    def test_add_to_framework_tier(self, mcp):
        result = mcp.rag_add_doc(
            name="test-framework-doc",
            content="Drupal: hook_form_alter patterns and common pitfalls.",
            tier="framework",
            framework="drupal",
        )
        assert "drupal" in result, result
        assert "error" not in result.lower(), result

    def test_add_to_os_tier(self, mcp):
        os_tier = next((t for t in mcp.TIER_REGISTRY if t["type"] == "os"), None)
        if not os_tier:
            pytest.skip("No os-type tier configured")
        result = mcp.rag_add_doc(
            name="test-os-doc",
            content="dnf install nginx; setsebool -P httpd_can_network_connect 1",
            tier="os",
        )
        assert "error" not in result.lower(), result

    def test_add_with_tags(self, mcp):
        result = mcp.rag_add_doc(
            name="test-tagged-doc",
            content="Tagged test content.",
            tier="common-issues",
            tags=["test", "integration"],
        )
        assert "error" not in result.lower(), result

    def test_add_unknown_tier_returns_error(self, mcp):
        result = mcp.rag_add_doc(
            name="test-bad-doc",
            content="Should fail.",
            tier="nonexistent-tier-xyz",
        )
        assert "error" in result.lower(), f"Expected error for unknown tier: {result}"

    def test_add_framework_without_name_returns_error(self, mcp):
        fw_tier = next((t for t in mcp.TIER_REGISTRY if t["type"] == "framework"), None)
        if not fw_tier:
            pytest.skip("No framework-type tier configured")
        result = mcp.rag_add_doc(
            name="test-fw-no-name",
            content="Should fail without framework name.",
            tier=fw_tier["id"],
            framework=None,
        )
        assert "error" in result.lower(), f"Expected error without framework: {result}"

    def test_add_doc_replace_on_same_name(self, mcp):
        # Uploading the same name twice should replace, not error
        for i in range(2):
            result = mcp.rag_add_doc(
                name="test-replace-doc",
                content=f"Version {i} of this document.",
                tier="common-issues",
            )
            assert "error" not in result.lower(), f"Error on iteration {i}: {result}"


# ---------------------------------------------------------------------------
# rag_add_issue
# ---------------------------------------------------------------------------
class TestAddIssue:
    def test_add_issue_basic(self, mcp):
        result = mcp.rag_add_issue(
            name="test-issue-basic",
            content="Problem: session token expires in 60s.\nSolution: set TOKEN_LIFETIME=86400.",
        )
        assert "common-issues" in result, result
        assert "error" not in result.lower(), result

    def test_add_issue_with_tags(self, mcp):
        result = mcp.rag_add_issue(
            name="test-issue-tagged",
            content="Nginx 502 when upstream restarts. Fix: upstream keepalive 32.",
            tags=["nginx", "502", "devops"],
        )
        assert "error" not in result.lower(), result

    def test_add_issue_idempotent(self, mcp):
        for _ in range(2):
            result = mcp.rag_add_issue(
                name="test-issue-idempotent",
                content="Idempotency test: same name, same content.",
            )
            assert "error" not in result.lower(), result


# ---------------------------------------------------------------------------
# rag_list_kbs
# ---------------------------------------------------------------------------
class TestListKbs:
    def test_list_kbs_returns_nonempty_string(self, mcp):
        result = mcp.rag_list_kbs()
        assert isinstance(result, str) and result

    def test_list_kbs_includes_fixed_kb_names(self, mcp):
        result = mcp.rag_list_kbs()
        for tier in mcp.TIER_REGISTRY:
            if tier["type"] == "fixed":
                assert tier["kb"] in result, f"'{tier['kb']}' missing from rag_list_kbs() output"

    def test_list_kbs_includes_project_kb(self, mcp):
        # project KB was created by test_add_to_project_tier
        result = mcp.rag_list_kbs()
        assert "test-project" in result, "Created project KB not visible in rag_list_kbs()"


# ---------------------------------------------------------------------------
# rag_search behaviour
# ---------------------------------------------------------------------------
class TestSearch:
    def test_search_returns_string(self, mcp):
        result = mcp.rag_search(query="test content", k=2, project="test-project")
        assert isinstance(result, str) and result

    def test_search_default_tiers(self, mcp):
        # No explicit tiers — should use DEFAULT_TIERS
        result = mcp.rag_search(query="checkout flow", k=2, project="test-project", framework="drupal")
        assert isinstance(result, str)

    def test_search_explicit_single_tier(self, mcp):
        result = mcp.rag_search(query="common issue", tiers=["common-issues"], k=2)
        assert isinstance(result, str)

    def test_search_explicit_tiers_replaces_defaults(self, mcp):
        # Passing tiers= replaces default_tiers entirely; only the listed tier is searched
        result = mcp.rag_search(
            query="test",
            tiers=["common-issues"],  # NOT all default tiers
            k=1,
        )
        assert isinstance(result, str)
        # Result should not mention an error from framework KB being missing
        assert "traceback" not in result.lower()

    def test_search_extend_defaults_with_devops_tier(self, mcp):
        devops = next((t for t in mcp.TIER_REGISTRY if t["id"] == "devops-general"), None)
        if not devops:
            pytest.skip("devops-general tier not configured")
        result = mcp.rag_search(
            query="docker nginx",
            tiers=[*mcp.DEFAULT_TIERS, "devops-general"],
            k=2,
            project="test-project",
        )
        assert isinstance(result, str)

    def test_search_nonexistent_project_returns_string(self, mcp):
        # Project KB doesn't exist, but auto_include os tier may still return results.
        # Either way rag_search() must return a non-empty string, never raise.
        result = mcp.rag_search(
            query="anything",
            tiers=["project"],
            project="never-created-project-xyz",
            k=1,
        )
        assert isinstance(result, str) and result

    def test_search_auto_include_os_tier(self, mcp):
        os_tier = next((t for t in mcp.TIER_REGISTRY if t.get("auto_include")), None)
        if not os_tier:
            pytest.skip("No auto_include tier configured")

        # Ensure the os KB exists (created by test_add_to_os_tier)
        os_kb_name = mcp._resolve_kb_name(os_tier, framework=None, project=None)
        mcp._ensure_kb(os_kb_name)
        mcp._refresh_kb_cache()

        # Search with only common-issues in explicit list — os should be auto-appended
        result = mcp.rag_search(
            query="package manager install",
            tiers=["common-issues"],
            k=2,
        )
        assert isinstance(result, str)
        assert "traceback" not in result.lower()

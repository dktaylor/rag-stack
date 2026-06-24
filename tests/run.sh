#!/usr/bin/env bash
# Run RAG integration tests in an isolated Docker stack.
#
# Usage:
#   bash tests/run.sh                  # run all tests
#   bash tests/run.sh -k search        # filter by test name
#   bash tests/run.sh -x               # stop on first failure
#   bash tests/run.sh --no-teardown    # keep stack running after tests
#
# The test stack runs on ports 3001 (Open WebUI) and 6334 (Qdrant) —
# safe to run alongside the production stack on 3000/6333.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.test.yml"
COMPOSE="docker compose -f ${COMPOSE_FILE}"

TEARDOWN=1
PYTEST_ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--no-teardown" ]]; then
        TEARDOWN=0
    else
        PYTEST_ARGS+=("$arg")
    fi
done

cleanup() {
    if [[ $TEARDOWN -eq 1 ]]; then
        echo ""
        echo "==> Tearing down test stack..."
        $COMPOSE down -v --remove-orphans 2>/dev/null || true
    else
        echo ""
        echo "==> Leaving test stack running (--no-teardown)."
        echo "    Tear down manually: docker compose -f ${COMPOSE_FILE} down -v"
    fi
}
trap cleanup EXIT

echo "==> Building test runner image..."
$COMPOSE build test-runner

echo "==> Starting test services (qdrant + open-webui)..."
$COMPOSE up -d qdrant open-webui

echo "==> Running tests..."
$COMPOSE run --rm test-runner pytest /app/tests/ -v --tb=short --no-header "${PYTEST_ARGS[@]:-}"

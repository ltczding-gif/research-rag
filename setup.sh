#!/usr/bin/env bash
# research-rag bootstrap (Unix / macOS).
#
# Default: creates a single .venv/ at the repo root with everything.
# --isolated: creates separate scanner/.venv and service/.venv. See
# README.md "Advanced installation" for when to use this.
# --no-init: install dependencies only; skip the interactive configuration.
#
# Idempotent — safe to re-run.

set -euo pipefail

ISOLATED=0
RUN_INIT=1
for arg in "$@"; do
    case "$arg" in
        --isolated|-i)
            ISOLATED=1
            ;;
        --simple|-s)
            ISOLATED=0  # explicit; same as default
            ;;
        --no-init)
            RUN_INIT=0
            ;;
        --help|-h)
            cat <<'USAGE'
Usage: ./setup.sh [--isolated] [--no-init]

Default: single .venv/ at the repo root with everything installed.
         Simplest setup; works for most users with a clean Python 3.10+.

--isolated: separate scanner/.venv and service/.venv (advanced).
            Use this if your Python environment has had transitive-dep
            conflicts before (anaconda + uv + system Python coexisting,
            etc.) and you want ChromaDB's heavy dep tree fully isolated
            from the scanner side.
            See README.md "Advanced installation" for details.

--no-init: install dependencies only. By default setup immediately starts
           the interactive configuration + health-check walkthrough.
USAGE
            exit 0
            ;;
        *)
            echo "[ERROR] unknown flag: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo "==> research-rag setup"
echo "    repo: $REPO_ROOT"
echo "    mode: $([ "$ISOLATED" = "1" ] && echo "isolated (two venvs)" || echo "default (single venv)")"

# --- Python check ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "[FATAL] python3 not found on PATH" >&2
    exit 1
fi
PYV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "    python: $(command -v python3) ($PYV)"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "[FATAL] Python 3.10+ is required (found $PYV)" >&2
    exit 1
fi

if [ "$ISOLATED" = "1" ]; then
    # --- Isolated: two venvs ---
    echo "==> [1/3] service venv: ./service/.venv"
    if [ ! -d "service/.venv" ]; then
        python3 -m venv service/.venv
    fi
    # shellcheck source=/dev/null
    . service/.venv/bin/activate
    pip install --upgrade pip >/dev/null
    pip install -r requirements-rag.txt
    deactivate
    echo "    OK"

    echo "==> [2/3] scanner venv: ./scanner/.venv"
    if [ ! -d "scanner/.venv" ]; then
        python3 -m venv scanner/.venv
    fi
    # shellcheck source=/dev/null
    . scanner/.venv/bin/activate
    pip install --upgrade pip >/dev/null
    pip install -r requirements-scanner.txt
    deactivate
    echo "    OK"

    NEXT_PYTHON_REL="scanner/.venv/bin/python"
else
    # --- Default: single venv ---
    echo "==> [1/2] single venv: ./.venv"
    if [ ! -d ".venv" ]; then
        python3 -m venv .venv
    fi
    # shellcheck source=/dev/null
    . .venv/bin/activate
    pip install --upgrade pip >/dev/null
    pip install -r requirements.txt
    deactivate
    echo "    OK"

    NEXT_PYTHON_REL=".venv/bin/python"
fi

echo ""
echo "==> Dependencies installed"

if [ "$RUN_INIT" = "0" ]; then
    echo "    Interactive configuration skipped (--no-init). Run it later with:"
    echo "      $NEXT_PYTHON_REL scanner/init_environment.py"
    exit 0
fi

echo "==> Starting interactive configuration and health check"
set +e
"$NEXT_PYTHON_REL" scanner/init_environment.py
INIT_STATUS=$?
set -e

case "$INIT_STATUS" in
    0)
        echo "==> Setup complete"
        ;;
    2)
        echo "==> Setup complete with warnings; review the doctor output above"
        ;;
    *)
        echo "[ERROR] interactive setup exited with status $INIT_STATUS" >&2
        exit "$INIT_STATUS"
        ;;
esac

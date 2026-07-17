#!/usr/bin/env python3
"""Health check for a research-rag installation.

Reports the status of every prerequisite a fresh-clone user needs to have
configured before the pipeline can run end-to-end. Read-only — never
modifies state. Run any time.

Default mode performs local file/env checks, inspects index counts, and
verifies that the stdio MCP module registers its expected tools. It never
makes paid LLM calls.

Exit codes:
    0   all green (or only informational notes)
    1   at least one error
    2   warnings only

Output groups, in order:
    1. System prerequisites          (Python, Git, network)
    2. Repository dependencies       (venvs, pip-installed packages)
    3. Environment configuration     (.env file, paths, vault)
    4. LLM backend credentials       (chosen backend's keys)
    5. Embedding provider            (fastembed import by default; Ollama
                                      running + model pulled when selected)
    6. Zotero source                 (db path, lock state)
    7. Active domain pack            (presence, invariant validation)
    8. Pipeline state                (notes, Chroma collections, MCP tools)

Each check returns one of:
    ✓ ok      everything's fine
    ⚠ warn    likely a problem, but not blocking
    ✗ error   pipeline will fail until this is fixed
    ➡ info    informational (vault is empty on first run, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _maybe_reexec_into_chromadb_venv() -> None:
    """If we're running with a Python that doesn't have chromadb but a
    project venv with it exists, re-exec in that venv. Saves the novice
    user from a confusing "chromadb not importable" error when the
    package IS installed — just in the wrong interpreter.

    Looks for venvs in this order:
      1. `.venv/` at repo root (default single-venv setup)
      2. `service/.venv/` (--isolated dual-venv setup)

    Skipped when:
      • user opts out via $LOCALRAG_DOCTOR_NO_REEXEC=1
      • chromadb imports successfully (already in a viable env)
      • neither candidate venv exists (setup hasn't been run yet)
    """
    if os.environ.get("LOCALRAG_DOCTOR_NO_REEXEC") == "1":
        return
    try:
        import chromadb  # noqa: F401
        return  # already in a working env
    except ImportError:
        pass

    # Candidate venvs in priority order: single-venv default first, dual second.
    if sys.platform == "win32":
        candidates = [
            REPO_ROOT / ".venv" / "Scripts" / "python.exe",
            REPO_ROOT / "service" / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        candidates = [
            REPO_ROOT / ".venv" / "bin" / "python",
            REPO_ROOT / "service" / ".venv" / "bin" / "python",
        ]

    for candidate in candidates:
        if not candidate.exists():
            continue
        # Don't re-exec if we're ALREADY using that interpreter.
        try:
            if Path(sys.executable).resolve() == candidate.resolve():
                return
        except OSError:
            pass
        print(
            f"[doctor] auto-switching to venv interpreter:\n"
            f"         {candidate}\n"
            f"         (set LOCALRAG_DOCTOR_NO_REEXEC=1 to disable this.)\n",
            flush=True,
        )
        os.execv(str(candidate), [str(candidate), __file__, *sys.argv[1:]])
        # os.execv replaces the process; control never returns past this line.
        return  # unreachable, but kept for static analysis


# --- ANSI color helpers ----------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""

_GLYPHS = {
    "ok":    ("\033[32m✓\033[0m", "✓") ,
    "warn":  ("\033[33m⚠\033[0m", "!"),
    "error": ("\033[31m✗\033[0m", "x"),
    "info":  ("\033[36m➡\033[0m", "i"),
}


def _glyph(status: str) -> str:
    pair = _GLYPHS.get(status, ("?", "?"))
    return pair[0] if _USE_COLOR else pair[1]


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _USE_COLOR else text


# --- Result type -----------------------------------------------------------


@dataclass
class CheckResult:
    status: str  # "ok" | "warn" | "error" | "info"
    name: str
    message: str
    hint: str | None = None


def _print_result(r: CheckResult) -> None:
    print(f"  {_glyph(r.status)} {r.name}")
    if r.message:
        for line in r.message.splitlines():
            print(f"      {line}")
    if r.hint:
        for line in r.hint.splitlines():
            print(f"      → {line}")


# --- Lightweight .env loader -----------------------------------------------


def _load_dotenv_into_map(path: Path) -> dict[str, str]:
    """Parse a `.env` file into a dict. No interpolation beyond expanding
    `$HOME` and `$VAR` references against the current process env, which
    matches setup.sh's behavior. Comments + blank lines ignored."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Expand $HOME and ${VAR}-style references against the current env.
        value = os.path.expandvars(os.path.expanduser(value))
        out[key] = value
    return out


def _effective_env() -> dict[str, str]:
    """Merge .env (if present) with os.environ; os.environ wins.

    The pipeline reads from os.environ at run time. This function
    simulates "what would the running pipeline see" for diagnostic
    purposes, accepting that the actual loading is done by setup.sh /
    .ps1 / a wrapper.
    """
    env = _load_dotenv_into_map(REPO_ROOT / ".env")
    env.update(os.environ)
    return env


# --- 1. System prerequisites -----------------------------------------------


def check_python_version() -> CheckResult:
    """Hard floor: 3.10 (the codebase uses `int | None` syntax). Recommended:
    3.11. Newer versions work; the upper bound is not enforced because Python
    minor releases rarely break working code, and ChromaDB tracks compatibility.
    """
    major, minor = sys.version_info[:2]
    actual = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 10):
        return CheckResult(
            "error",
            "Python version",
            f"{actual} is below the minimum 3.10",
            hint="Install Python 3.10+ from https://www.python.org/downloads/, or via pyenv/uv/conda. 3.11 is the validated reference.",
        )
    if (major, minor) == (3, 11):
        return CheckResult("ok", "Python version", f"{actual} (validated reference version)")
    return CheckResult(
        "ok",
        "Python version",
        f"{actual} (3.11 is the validated reference; {actual} should work)",
    )


def check_git_available() -> CheckResult:
    if shutil.which("git"):
        return CheckResult("ok", "git binary", "found on PATH")
    return CheckResult(
        "warn",
        "git binary",
        "not found on PATH",
        hint="git is needed for cloning and version-stamping; install from https://git-scm.com/",
    )


def check_internet_reachable() -> CheckResult:
    """Cheap connectivity probe against a well-known DNS server."""
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            return CheckResult("ok", "Internet reachable", "TCP to 1.1.1.1:53 succeeded")
    except OSError as exc:
        return CheckResult(
            "warn",
            "Internet reachable",
            f"TCP to 1.1.1.1:53 failed: {exc}",
            hint="Note generation needs to reach your chosen LLM provider; offline-only setups can still use --backend subagent.",
        )


# --- 2. Repository dependencies --------------------------------------------


def _can_import(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def check_scanner_runtime() -> CheckResult:
    """The scanner needs pypdf + pyyaml + at least one backend SDK."""
    missing = [m for m in ("pypdf", "yaml") if not _can_import(m)]
    if missing:
        return CheckResult(
            "error",
            "scanner runtime imports",
            f"missing: {', '.join(missing)}",
            hint="Run `./setup.sh` (or `.\\setup.ps1`) at the repo root to create the venv and install deps.",
        )
    backends_present = []
    for name, mod in [
        ("vertex", "google.genai"),
        ("gemini-api", "google.genai"),
        ("anthropic", "anthropic"),
        ("openai", "openai"),
    ]:
        if _can_import(mod):
            backends_present.append(name)
    backends_present.append("subagent (no SDK)")
    return CheckResult(
        "ok",
        "scanner runtime imports",
        f"available backends: {', '.join(sorted(set(backends_present)))}",
    )


def check_service_runtime() -> CheckResult:
    """The service layer needs Chroma, MCP, Flask, and PDF extraction."""
    missing = [
        m for m in ("chromadb", "mcp", "flask", "pdfplumber", "yaml") if not _can_import(m)
    ]
    if missing:
        return CheckResult(
            "error",
            "service runtime imports",
            f"missing: {', '.join(missing)}",
            hint="Run `./setup.sh` (or `.\\setup.ps1`) at the repo root. Doctor auto-rewires into the venv if you ran it with the wrong Python.",
        )
    return CheckResult("ok", "service runtime imports", "chromadb / mcp / flask / pdfplumber / yaml all import")


_BACKEND_RUNTIME_MODULES = {
    "subagent": (),
    "gemini-api": ("google.genai",),
    "vertex": ("google.genai", "google.cloud.storage"),
    "anthropic": ("anthropic",),
    "openai": ("openai", "pdfplumber"),
}


def check_selected_backend_runtime(env: dict[str, str]) -> CheckResult:
    backend = env.get("LOCALRAG_PROCESSOR_BACKEND", "subagent")
    required = _BACKEND_RUNTIME_MODULES.get(backend, ())
    missing = [module for module in required if not _can_import(module)]
    if missing:
        return CheckResult(
            "error",
            "selected backend runtime",
            f"{backend} is selected but modules are missing: {', '.join(missing)}",
            hint=f"Run: {sys.executable} -m pip install -r requirements-backends/{backend}.txt",
        )
    detail = "no extra SDK required" if not required else f"{', '.join(required)} available"
    return CheckResult("ok", "selected backend runtime", f"{backend}: {detail}")


def check_chromadb_version() -> CheckResult:
    """Soft check: any 1.x release should work. 0.x is rejected because the
    persistent index format changed at 1.0 and old data won't read. The
    reference version is 1.5.5; we surface the installed version for
    debugging without warning on every minor difference."""
    try:
        import chromadb  # type: ignore
    except ImportError:
        return CheckResult(
            "error",
            "ChromaDB version",
            "chromadb not importable",
            hint="See `service runtime imports` above.",
        )
    version = getattr(chromadb, "__version__", "unknown")
    parts = version.split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        major = -1
    if major == 0:
        return CheckResult(
            "error",
            "ChromaDB version",
            f"{version} is below the minimum 1.0",
            hint="ChromaDB 0.x stored its persistent index in an incompatible format. Upgrade with `pip install -U chromadb` (any 1.x).",
        )
    if version == "1.5.5":
        return CheckResult("ok", "ChromaDB version", f"{version} (validated reference)")
    if major == 1:
        return CheckResult("ok", "ChromaDB version", f"{version} (1.x; reference is 1.5.5)")
    return CheckResult(
        "info",
        "ChromaDB version",
        f"{version} (untested major version; reference is 1.5.5)",
        hint="Open an issue if you hit a regression — major-version bumps may change behavior.",
    )


# --- 3. Environment configuration ------------------------------------------


def check_env_file(env: dict[str, str]) -> CheckResult:
    if (REPO_ROOT / ".env").exists():
        return CheckResult("ok", ".env file", "exists at repo root")
    if (REPO_ROOT / ".env.example").exists():
        return CheckResult(
            "error",
            ".env file",
            "missing — pipeline cannot read paths or credentials",
            hint=(
                "Run `cp .env.example .env` then fill in values, OR run "
                "`python scanner/init_environment.py` for a guided walkthrough."
            ),
        )
    return CheckResult(
        "error",
        ".env file",
        ".env and .env.example both missing — repo state is broken",
    )


def check_notes_dir(env: dict[str, str]) -> CheckResult:
    raw = env.get("LOCALRAG_NOTES_DIR")
    if not raw:
        return CheckResult(
            "warn",
            "LOCALRAG_NOTES_DIR",
            "not set — falls back to $HOME/research-note",
            hint="Add LOCALRAG_NOTES_DIR to your .env to make this explicit.",
        )
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.exists():
        return CheckResult(
            "warn",
            "LOCALRAG_NOTES_DIR",
            f"path doesn't exist yet: {path}",
            hint="Will be created on first scanner run; create manually if you want to pre-seed notes.",
        )
    if not os.access(path, os.W_OK):
        return CheckResult(
            "error",
            "LOCALRAG_NOTES_DIR",
            f"path exists but is not writable: {path}",
            hint="chmod / cacls the directory so the pipeline user can write notes.",
        )
    return CheckResult("ok", "LOCALRAG_NOTES_DIR", f"{path} (writable)")


def check_localrag_home(env: dict[str, str]) -> CheckResult:
    raw = env.get("LOCALRAG_HOME")
    if not raw:
        return CheckResult(
            "warn",
            "LOCALRAG_HOME",
            "not set — falls back to $HOME/.localrag",
            hint="ChromaDB and ledgers will land here; set explicitly to avoid surprises.",
        )
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    return CheckResult("ok", "LOCALRAG_HOME", f"{path}")


def check_processor_backend(env: dict[str, str]) -> CheckResult:
    # Keep this fallback aligned with config.PROCESSOR_BACKEND — a drifted
    # doctor default misreports which backend an unconfigured install uses.
    backend = env.get("LOCALRAG_PROCESSOR_BACKEND", "subagent")
    valid = {"vertex", "gemini-api", "anthropic", "openai", "subagent"}
    if backend not in valid:
        return CheckResult(
            "error",
            "LOCALRAG_PROCESSOR_BACKEND",
            f"unknown value: {backend!r}",
            hint=f"Set to one of: {', '.join(sorted(valid))}",
        )
    return CheckResult("ok", "LOCALRAG_PROCESSOR_BACKEND", f"{backend}")


# --- 4. LLM backend credentials --------------------------------------------


_BACKEND_REQUIRED_VARS = {
    "vertex": [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GEMINI_VERTEX_GCS_BUCKET",
    ],
    "gemini-api": ["GEMINI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "subagent": [],  # nothing required
}


def check_backend_credentials(env: dict[str, str]) -> list[CheckResult]:
    # Keep this fallback aligned with config.PROCESSOR_BACKEND and
    # check_processor_backend(): a fresh install defaults to subagent.
    backend = env.get("LOCALRAG_PROCESSOR_BACKEND", "subagent")
    required = _BACKEND_REQUIRED_VARS.get(backend, [])
    if not required:
        return [CheckResult("ok", f"{backend} credentials", "no credentials required for this backend")]

    out: list[CheckResult] = []
    for var in required:
        value = env.get(var, "").strip()
        if not value:
            out.append(
                CheckResult(
                    "error",
                    var,
                    "not set",
                    hint=f"Add to .env. Required for backend={backend}.",
                )
            )
            continue
        # Path-typed credentials (Vertex SA file): verify file exists.
        if var == "GOOGLE_APPLICATION_CREDENTIALS":
            sa_path = Path(os.path.expandvars(os.path.expanduser(value)))
            if not sa_path.exists():
                out.append(
                    CheckResult(
                        "error",
                        var,
                        f"file not found: {sa_path}",
                        hint="Download the service-account JSON from GCP IAM and update the path.",
                    )
                )
                continue
            try:
                payload = json.loads(sa_path.read_text(encoding="utf-8"))
                email = payload.get("client_email", "")
                if not email:
                    out.append(
                        CheckResult(
                            "warn",
                            var,
                            f"file exists but lacks client_email: {sa_path}",
                            hint="Verify it's a service-account JSON, not a user-OAuth credential.",
                        )
                    )
                    continue
                out.append(CheckResult("ok", var, f"file ok, client_email={email}"))
                continue
            except (OSError, json.JSONDecodeError) as exc:
                out.append(
                    CheckResult(
                        "error",
                        var,
                        f"file unreadable: {exc}",
                    )
                )
                continue
        # API keys: just confirm non-empty + reasonable length sanity check
        masked = value[:4] + "…" + value[-4:] if len(value) > 12 else "***"
        out.append(CheckResult("ok", var, f"set ({masked})"))
    return out


# --- 5. Ollama embedding service -------------------------------------------


def check_ollama_running(env: dict[str, str], timeout: float = 2.0) -> CheckResult:
    raw = env.get("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
    base = raw.rsplit("/api/", 1)[0] if "/api/" in raw else raw
    tags_url = f"{base}/api/tags"
    try:
        with urllib.request.urlopen(tags_url, timeout=timeout) as resp:
            data = json.loads(resp.read())
        names = [m.get("name", "") for m in data.get("models", [])]
        return CheckResult("ok", "Ollama running", f"{base}", hint=f"{len(names)} model(s) installed")
    except urllib.error.URLError as exc:
        return CheckResult(
            "error",
            "Ollama running",
            f"cannot reach {tags_url}: {exc.reason}",
            hint="Install: https://ollama.com/download. Start with `ollama serve` (or it auto-starts on macOS/Windows).",
        )
    except Exception as exc:
        return CheckResult("error", "Ollama running", f"{exc}")


def check_ollama_embed_model(env: dict[str, str], timeout: float = 2.0) -> CheckResult:
    """Verify that the model named by OLLAMA_EMBED_MODEL is actually pulled.

    The embedding model is a free choice — any Ollama embedding model works
    (qwen3-embedding:4b is the validated reference; nomic-embed-text,
    mxbai-embed-large, bge-m3, etc. are all viable alternatives). The check
    only fires on the *configured* model, not a hard-coded one.

    A subtle gotcha: switching models against an existing ChromaDB will
    fail at query time because dimensionalities differ. If you change the
    model after building collections, rebuild the DB.
    """
    target = env.get("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b")
    raw = env.get("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
    base = raw.rsplit("/api/", 1)[0] if "/api/" in raw else raw
    tags_url = f"{base}/api/tags"
    try:
        with urllib.request.urlopen(tags_url, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:
        return CheckResult(
            "warn",
            "embedding model pulled",
            "could not query Ollama (see Ollama check above)",
        )
    names = [m.get("name", "") for m in data.get("models", [])]
    # Any tag of the requested model counts (`qwen3-embedding:4b`, etc.).
    if any(n == target or n.startswith(f"{target}:") or n.split(":")[0] == target for n in names):
        return CheckResult("ok", "embedding model pulled", target)
    # Look for any plausible embedding model so we can suggest a no-op
    # config tweak instead of a download.
    embedding_hints = ("embed", "embedding", "bge", "nomic")
    have_other_embed = [n for n in names if any(h in n.lower() for h in embedding_hints)]
    if have_other_embed:
        return CheckResult(
            "warn",
            "embedding model pulled",
            f"{target} not pulled, but you have: {', '.join(have_other_embed)}",
            hint=(
                f"Either pull the configured model: ollama pull {target}\n"
                f"OR switch OLLAMA_EMBED_MODEL in .env to one you already have.\n"
                "If the ChromaDB has been built with a different model, you must rebuild it before switching."
            ),
        )
    return CheckResult(
        "error",
        "embedding model pulled",
        f"{target} not pulled; no embedding-like models found (have: {', '.join(names) or '(none)'})",
        hint=(
            f"Pull with: ollama pull {target}\n"
            "Other valid choices: nomic-embed-text, mxbai-embed-large, bge-m3, snowflake-arctic-embed, etc.\n"
            "Whichever you pick, set OLLAMA_EMBED_MODEL in .env to match."
        ),
    )


def check_embedding_provider(env: dict[str, str]) -> list[CheckResult]:
    """Branch the embedding checks on LOCALRAG_EMBED_PROVIDER.

    The service default is `fastembed` (in-process ONNX — no daemon), so an
    unconfigured install must not be told to start Ollama. Mirrors the
    branch logic of service/embedding_client.py:healthcheck without
    importing it (scanner/ and service/ both ship a top-level `config`
    module, so cross-importing service code here would be fragile).

      fastembed      → import check only (the model itself downloads on the
                       first build; probing it here would trigger a ~0.22 GB
                       download inside a read-only health check)
      ollama         → the existing Ollama daemon + model checks, unchanged
      openai-compat  → API key presence check
      anything else  → error naming the valid choices
    """
    provider = env.get("LOCALRAG_EMBED_PROVIDER", "fastembed")
    if provider == "fastembed":
        model = env.get(
            "LOCALRAG_FASTEMBED_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        if _can_import("fastembed"):
            return [CheckResult(
                "ok",
                "embedding provider",
                f"fastembed (in-process ONNX, no daemon), model={model}",
                hint="Model (~0.22 GB) downloads automatically on the first build_notes_db.py run.",
            )]
        return [CheckResult(
            "error",
            "embedding provider",
            "LOCALRAG_EMBED_PROVIDER=fastembed (default) but the `fastembed` package is not importable",
            hint=(
                'Install with: pip install "fastembed>=0.4" (included in requirements-rag.txt),\n'
                "or switch LOCALRAG_EMBED_PROVIDER to ollama / openai-compat in .env."
            ),
        )]
    if provider == "ollama":
        return [
            check_ollama_running(env),
            check_ollama_embed_model(env),
        ]
    if provider == "openai-compat":
        if env.get("OPENAI_EMBED_API_KEY", "").strip():
            base = env.get("OPENAI_EMBED_BASE_URL", "https://api.openai.com/v1")
            model = env.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
            return [CheckResult(
                "ok",
                "embedding provider",
                f"openai-compat, base={base}, model={model}",
            )]
        return [CheckResult(
            "error",
            "embedding provider",
            "LOCALRAG_EMBED_PROVIDER=openai-compat but OPENAI_EMBED_API_KEY is empty",
            hint="Set OPENAI_EMBED_API_KEY in .env (see the tier table in .env.example).",
        )]
    return [CheckResult(
        "error",
        "embedding provider",
        f"unknown LOCALRAG_EMBED_PROVIDER={provider!r}",
        hint="Expected one of: fastembed (default), ollama, openai-compat.",
    )]


# --- 6. Zotero source ------------------------------------------------------


def check_zotero_db(env: dict[str, str]) -> CheckResult:
    raw = env.get("ZOTERO_DB_PATH")
    if not raw:
        return CheckResult(
            "warn",
            "ZOTERO_DB_PATH",
            "not set — falls back to $HOME/Zotero/zotero.sqlite",
            hint="Set explicitly in .env to silence this warning.",
        )
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.exists():
        return CheckResult(
            "error",
            "ZOTERO_DB_PATH",
            f"file not found: {path}",
            hint=(
                "Common locations:\n"
                "  Windows: %USERPROFILE%/Zotero/zotero.sqlite\n"
                "  macOS:   ~/Zotero/zotero.sqlite\n"
                "  Linux:   ~/Zotero/zotero.sqlite\n"
                "Verify in Zotero → Edit → Settings → Advanced → Files and Folders."
            ),
        )
    # Try opening read-only and querying expected schema. Use immutable=1 URI
    # to avoid acquiring any lock in case Zotero is running. `Path.as_uri()`
    # produces a correctly-escaped `file:///...` form on every OS, including
    # the leading `///` for Windows drive letters that bare `as_posix()` misses.
    try:
        uri = f"{path.resolve().as_uri()}?immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM items")
            (item_count,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM itemAttachments WHERE contentType = 'application/pdf' OR path LIKE '%.pdf'")
            (pdf_count,) = cur.fetchone()
        finally:
            conn.close()
        return CheckResult(
            "ok",
            "ZOTERO_DB_PATH",
            f"{path}",
            hint=f"{item_count} items, {pdf_count} PDF attachments",
        )
    except sqlite3.Error as exc:
        return CheckResult(
            "error",
            "ZOTERO_DB_PATH",
            f"sqlite open failed: {exc}",
            hint="If Zotero is running, close it. Otherwise the file may be corrupted or not a Zotero database.",
        )


def check_zotero_process_not_running() -> CheckResult:
    """Zotero's writer races against our SQLite reader; refuse to scan if it's alive."""
    try:
        # errors="replace": tasklist emits the OEM/ANSI codepage (e.g. CP936
        # on Chinese Windows), not UTF-8. Without a decode fallback the
        # reader thread raises UnicodeDecodeError and kills the whole doctor
        # run. We only substring-match ASCII ("zotero.exe"), so lossy
        # replacement of non-ASCII bytes is harmless.
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                capture_output=True, text=True, errors="replace", timeout=3,
            )
            running = "zotero.exe" in result.stdout.lower()
        else:
            result = subprocess.run(
                ["pgrep", "-x", "zotero"],
                capture_output=True, text=True, errors="replace", timeout=3,
            )
            running = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return CheckResult(
            "info",
            "Zotero process",
            "could not detect (tasklist/pgrep unavailable)",
        )
    if running:
        return CheckResult(
            "warn",
            "Zotero process",
            "Zotero appears to be running",
            hint="Close it before running scanner/zotero_batch_scanner.py — otherwise the SQLite snapshot can race.",
        )
    return CheckResult("ok", "Zotero process", "not running")


# --- 7. Active domain pack -------------------------------------------------


def check_domain_pack(env: dict[str, str]) -> list[CheckResult]:
    pack_name = env.get("LOCALRAG_DOMAIN_PACK", "catalysis")
    pack_root = REPO_ROOT / "domain-packs" / pack_name
    out: list[CheckResult] = []
    if not pack_root.exists():
        out.append(
            CheckResult(
                "error",
                "active domain pack",
                f"pack '{pack_name}' not found at {pack_root}",
                hint=(
                    "Bootstrap one: python scanner/bootstrap_domain_pack.py --name <field>\n"
                    "Or set LOCALRAG_DOMAIN_PACK to an existing pack name."
                ),
            )
        )
        return out
    out.append(CheckResult("ok", "active domain pack", f"{pack_name}"))

    # Reuse validate_pack from bootstrap_domain_pack
    try:
        sys.path.insert(0, str(REPO_ROOT / "scanner"))
        from bootstrap_domain_pack import validate_pack  # type: ignore
        ok, errors = validate_pack(pack_root)
    except Exception as exc:
        out.append(
            CheckResult(
                "warn",
                "pack invariants",
                f"could not validate ({exc})",
            )
        )
        return out
    if ok:
        out.append(CheckResult("ok", "pack invariants", "all checks pass"))
    else:
        out.append(
            CheckResult(
                "error",
                "pack invariants",
                f"{len(errors)} issue(s)",
                hint="\n".join(errors),
            )
        )
    return out


# --- 8. Pipeline state -----------------------------------------------------


def check_vault_note_count(env: dict[str, str]) -> CheckResult:
    raw = env.get("LOCALRAG_NOTES_DIR", str(Path.home() / "research-note"))
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.exists():
        return CheckResult(
            "info",
            "vault notes",
            "vault directory not yet created (normal for first run)",
        )
    notes = list(path.rglob("*_review_note.md"))
    return CheckResult("info", "vault notes", f"{len(notes)} note(s) in {path}")


def check_chroma_collections(env: dict[str, str]) -> CheckResult:
    raw_home = env.get("LOCALRAG_HOME", str(Path.home() / ".localrag"))
    raw_chroma = env.get("LOCALRAG_CHROMA_PATH", str(Path(raw_home) / "chroma"))
    chroma_path = Path(os.path.expandvars(os.path.expanduser(raw_chroma)))
    if not chroma_path.exists():
        return CheckResult(
            "warn",
            "local indexes",
            f"not built yet ({chroma_path})",
            hint=f"After generating notes, run: {sys.executable} scripts/build_indexes.py",
        )

    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(chroma_path))
        available = {
            item if isinstance(item, str) else item.name
            for item in client.list_collections()
        }
        names = {
            "notes": env.get("LOCALRAG_NOTES_COLLECTION", "notes"),
            "papers": env.get("LOCALRAG_PAPERS_COLLECTION", "papers"),
        }
        counts = {
            label: client.get_collection(name).count() if name in available else 0
            for label, name in names.items()
        }
    except Exception as exc:
        return CheckResult(
            "error",
            "local indexes",
            f"could not inspect {chroma_path}: {exc}",
            hint="Check LOCALRAG_CHROMA_PATH and the installed ChromaDB version.",
        )

    message = f"notes={counts['notes']}, paper_chunks={counts['papers']} at {chroma_path}"
    if counts["notes"] <= 0 or counts["papers"] <= 0:
        return CheckResult(
            "warn",
            "local indexes",
            message,
            hint=f"Build or refresh both indexes: {sys.executable} scripts/build_indexes.py",
        )
    return CheckResult("ok", "local indexes", message)


def check_mcp_tool_registration(
    runner=subprocess.run,
    timeout: float = 30.0,
) -> CheckResult:
    service_dir = REPO_ROOT / "service"
    probe = (
        "import asyncio,json,sys;"
        f"sys.path.insert(0,{str(service_dir)!r});"
        "import mcp_server;"
        "print(json.dumps(sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))))"
    )
    env = os.environ.copy()
    env["LOCALRAG_SKIP_CHROMA_INIT"] = "1"
    try:
        completed = runner(
            [sys.executable, "-c", probe],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except Exception as exc:
        return CheckResult("error", "MCP tool registration", str(exc))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return CheckResult(
            "error",
            "MCP tool registration",
            detail or f"probe exited {completed.returncode}",
            hint="Re-run setup so the MCP and service dependencies are installed in this venv.",
        )
    try:
        names = set(json.loads(completed.stdout.strip().splitlines()[-1]))
    except Exception as exc:
        return CheckResult("error", "MCP tool registration", f"invalid probe output: {exc}")
    expected = {"search_notes", "search_papers", "get_note", "index_status"}
    missing = expected - names
    if missing:
        return CheckResult("error", "MCP tool registration", f"missing tools: {', '.join(sorted(missing))}")
    return CheckResult("ok", "MCP tool registration", ", ".join(sorted(expected)))


def check_query_server(env: dict[str, str], timeout: float = 1.0) -> CheckResult:
    host = env.get("LOCALRAG_HOST", "127.0.0.1")
    port = env.get("LOCALRAG_PORT", "18810")
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return CheckResult(
            "ok",
            "query_server",
            f"running at {host}:{port}",
            hint=json.dumps(data),
        )
    except urllib.error.URLError:
        return CheckResult(
            "info",
            "query_server",
            f"not reachable at {host}:{port}",
            hint="Start with: python service/query_server.py (after notes are built).",
        )
    except Exception as exc:
        return CheckResult("warn", "query_server", f"{exc}")


def check_skills_installed() -> CheckResult:
    """Detect whether the Claude Code skills directory has the literature
    skills installed (informational only; the pipeline works without)."""
    candidates = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".openclaw" / "skills",
        Path.home() / ".cc-switch" / "skills",
    ]
    for candidate in candidates:
        if (candidate / "search-literature").exists():
            return CheckResult(
                "ok",
                "Claude Code skills",
                f"search-literature installed at {candidate}",
            )
    return CheckResult(
        "info",
        "Claude Code skills",
        "literature skills not detected in standard Claude Code skill directories",
        hint=f"Install with: cp -r {REPO_ROOT}/skills/* ~/.claude/skills/",
    )


# --- Orchestration ---------------------------------------------------------


def run_all_checks() -> list[tuple[str, list[CheckResult]]]:
    """Returns a list of (group_name, [results]) pairs."""
    env = _effective_env()
    groups: list[tuple[str, list[CheckResult]]] = []

    groups.append(("System prerequisites", [
        check_python_version(),
        check_git_available(),
        check_internet_reachable(),
    ]))

    groups.append(("Repository dependencies", [
        check_scanner_runtime(),
        check_service_runtime(),
        check_chromadb_version(),
        check_selected_backend_runtime(env),
    ]))

    groups.append(("Environment configuration", [
        check_env_file(env),
        check_notes_dir(env),
        check_localrag_home(env),
        check_processor_backend(env),
    ]))

    groups.append(("LLM backend credentials", check_backend_credentials(env)))

    groups.append(("Embedding provider", check_embedding_provider(env)))

    groups.append(("Zotero source", [
        check_zotero_db(env),
        check_zotero_process_not_running(),
    ]))

    groups.append(("Active domain pack", check_domain_pack(env)))

    groups.append(("Pipeline state", [
        check_vault_note_count(env),
        check_chroma_collections(env),
        check_mcp_tool_registration(),
        check_query_server(env),
        check_skills_installed(),
    ]))

    return groups


def summarize(groups: list[tuple[str, list[CheckResult]]]) -> tuple[int, int, int, int]:
    ok = warn = err = info = 0
    for _, results in groups:
        for r in results:
            if r.status == "ok": ok += 1
            elif r.status == "warn": warn += 1
            elif r.status == "error": err += 1
            elif r.status == "info": info += 1
    return ok, warn, err, info


def _next_steps_for_errors(groups: list[tuple[str, list[CheckResult]]]) -> list[str]:
    """Pick the most actionable 1-3 next steps for a novice user.

    Errors are ordered: missing .env > missing creds > Ollama down > Zotero db
    > pack issues. The novice should fix one thing and re-run, not 6 things
    at once.
    """
    by_name: dict[str, CheckResult] = {}
    for _, results in groups:
        for r in results:
            if r.status == "error":
                by_name[r.name] = r

    steps: list[str] = []
    # 1. .env missing → most upstream
    if ".env file" in by_name:
        steps.append("Run `python scanner/init_environment.py` to create and configure .env interactively.")
        return steps  # everything else depends on this; stop here
    # 2. service venv missing → can't run service-side anything
    if "service runtime imports" in by_name:
        steps.append("Run `./setup.sh` (macOS/Linux) or `.\\setup.ps1` (Windows) to create the service venv.")
    # 3. Backend creds missing → no LLM calls possible
    cred_errors = [
        n for n in by_name
        if n in ("GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "GOOGLE_CLOUD_PROJECT", "GEMINI_VERTEX_GCS_BUCKET")
    ]
    if cred_errors:
        steps.append(
            f"Configure backend credentials in .env: {', '.join(cred_errors)}. "
            "Or switch LOCALRAG_PROCESSOR_BACKEND=subagent for a no-credential start."
        )
    # 4. Embedding provider broken → no vectors
    if "embedding provider" in by_name:
        steps.append(
            'Fix the embedding provider: pip install "fastembed>=0.4" for the '
            "default, or set LOCALRAG_EMBED_PROVIDER / its credentials in .env."
        )
    if "Ollama running" in by_name:
        steps.append("Start Ollama (https://ollama.com/download for the installer).")
    elif "embedding model pulled" in by_name:
        target = os.environ.get("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b")
        steps.append(f"Pull an embedding model: `ollama pull {target}`")
    # 5. Zotero db → no PDFs to scan
    if "ZOTERO_DB_PATH" in by_name:
        steps.append("Set ZOTERO_DB_PATH in .env to the path of your zotero.sqlite file.")
    # 6. Pack issues
    if "active domain pack" in by_name:
        steps.append("Set LOCALRAG_DOMAIN_PACK in .env, or run `python scanner/bootstrap_domain_pack.py --name <field>`.")
    return steps


def main() -> int:
    _maybe_reexec_into_chromadb_venv()
    argparse.ArgumentParser(
        description="Health check for a research-rag installation."
    ).parse_args()

    groups = run_all_checks()

    print()
    print(_bold("research-rag health check"))
    print()
    for group_name, results in groups:
        print(_bold(group_name))
        print("─" * len(group_name))
        for r in results:
            _print_result(r)
        print()

    ok, warn, err, info = summarize(groups)
    print(_bold(f"Summary: {ok} ok · {warn} warn · {err} error · {info} info"))
    print()
    if err > 0:
        print(f"  {_glyph('error')} Pipeline NOT ready. Fix the errors above before running the scanner.")
        next_steps = _next_steps_for_errors(groups)
        if next_steps:
            print()
            print(_bold("  What to do next:"))
            for i, step in enumerate(next_steps, 1):
                print(f"    {i}. {step}")
        return 1
    if warn > 0:
        print(f"  {_glyph('warn')} Setup is usable, but these items are not ready yet:")
        for _, results in groups:
            for result in results:
                if result.status == "warn":
                    detail = result.hint or result.message
                    print(f"    • {result.name}: {detail}")
        return 2
    print(f"  {_glyph('ok')} All systems green. Ask your MCP client to run `index_status`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

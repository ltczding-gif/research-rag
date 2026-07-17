#!/usr/bin/env python3
"""Interactive setup walkthrough for a fresh research-rag clone.

Walks the user through every decision they need to make before the
pipeline can run end-to-end. Idempotent: re-running picks up where you
left off (skips sections whose answers are already in `.env`, asks again
only when the value is empty or fails a smoke check).

Sections, in order:
    1. Sanity check Python version
    2. Bootstrap `.env` from `.env.example`
    3. Vault paths (LOCALRAG_NOTES_DIR, LOCALRAG_HOME)
    4. LLM backend selection + credentials (1 of 5)
    5. Zotero source (locate zotero.sqlite, with OS-aware auto-detect)
    6. Embedding provider (fastembed default; optionally switch to Ollama + pull a model)
    7. Embedding provider (FastEmbed default; optional Ollama/OpenAI-compatible)
    8. Domain pack (use catalysis or bootstrap a new one)
    9. Skills layer (optionally copy to agent skill directories)
    10. Terminal-agent MCP registration (Claude Code and Codex)
    11. Run doctor.py to verify

Run with `--non-interactive` to skip prompts and just check current state
(useful in CI or when you've already filled in `.env` manually).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"


# --- Tiny ANSI + IO helpers ------------------------------------------------


_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _USE_COLOR else text


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if _USE_COLOR else text


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m" if _USE_COLOR else text


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m" if _USE_COLOR else text


def _print_section(idx: int, title: str) -> None:
    print()
    print(_bold(f"───── Step {idx}: {title} ─────"))


def _ask(prompt: str, default: str | None = None, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            answer = ""
        if answer:
            return answer
        if default is not None:
            return default
        if allow_empty:
            return ""
        print(_red("  (this field is required)"))


def _confirm(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        answer = input(f"  {prompt}{suffix}: ").strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        return default
    return answer in ("y", "yes")


# --- .env parser / writer (preserves comments + ordering) -------------------


def _load_env_lines() -> list[str]:
    if ENV_PATH.exists():
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    if ENV_EXAMPLE_PATH.exists():
        return ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    return []


def _save_env_lines(lines: list[str]) -> None:
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_env_var(lines: list[str], key: str) -> str:
    """Return the current value of `key` in the env file. Empty string if
    unset, commented out, or not present."""
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip().strip('"').strip("'")
        return value
    return ""


def _set_env_var(lines: list[str], key: str, value: str) -> list[str]:
    """Set or replace `KEY=value` line; preserves position if it already
    exists, otherwise appends. If a commented-out version exists
    (`# KEY=...`), uncomments and replaces it in place."""
    new = list(lines)
    target = f"{key}={value}"
    for i, line in enumerate(new):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            new[i] = target
            return new
        if stripped.startswith(f"# {key}=") or stripped.startswith(f"#{key}="):
            new[i] = target
            return new
    # No existing entry — append.
    new.append(target)
    return new


# --- Codex MCP registration (config.toml) -----------------------------------
#
# The terminal-agent MCP server is auto-discovered by Claude Code via the
# repo's `.mcp.json`, but Codex reads `~/.codex/config.toml`. The exact TOML
# schema below was verified against the local `codex` CLI (0.144.x): running
# `codex mcp add research-rag -- python <launcher>` produces
#     [mcp_servers.research-rag]
#     command = "python"
#     args = ["<launcher>"]
# (with an optional [mcp_servers.research-rag.env] sub-table). We reproduce
# that shape with controlled text writes and detect state with stdlib tomllib
# — no third-party TOML writer dependency.

CODEX_MCP_SERVER_NAME = "research-rag"

_TOML_HEADER_RE = re.compile(r"^\[\[?\s*(.+?)\s*\]\]?\s*(?:#.*)?$")


def _codex_launcher_arg(repo_root: Path) -> str:
    """Absolute launcher path, forward-slashed. Codex may start from any cwd,
    so the path must be absolute; forward slashes are valid in TOML basic
    strings on every OS and avoid backslash-escaping."""
    return (repo_root / "scripts" / "run_mcp_server.py").resolve().as_posix()


def _python_command_arg(python_executable: str | Path) -> str:
    return Path(python_executable).resolve().as_posix()


def _render_codex_mcp_section(repo_root: Path, python_executable: str | Path) -> str:
    return (
        f"[mcp_servers.{CODEX_MCP_SERVER_NAME}]\n"
        f'command = "{_python_command_arg(python_executable)}"\n'
        f'args = ["{_codex_launcher_arg(repo_root)}"]\n'
    )


def _toml_block_key(line: str) -> str | None:
    """Return the dotted table key if `line` is a TOML table header, else None."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    m = _TOML_HEADER_RE.match(stripped)
    return m.group(1).strip() if m else None


def _belongs_to_research_rag(key: str | None) -> bool:
    if key is None:
        return False
    target = f"mcp_servers.{CODEX_MCP_SERVER_NAME}"
    return key == target or key.startswith(target + ".")


def _replace_codex_section(raw: str, new_block: str) -> str:
    """Return `raw` with the research-rag table (and any of its sub-tables)
    replaced by `new_block` in-place, preserving every other line verbatim."""
    lines = raw.splitlines(keepends=True)
    out: list[str] = []
    current_key: str | None = None
    current: list[str] = []
    blocks: list[tuple[str | None, list[str]]] = []
    for line in lines:
        key = _toml_block_key(line)
        if key is not None:
            blocks.append((current_key, current))
            current_key, current = key, [line]
        else:
            current.append(line)
    blocks.append((current_key, current))

    inserted = False
    for key, blk in blocks:
        if _belongs_to_research_rag(key):
            if not inserted:
                if out and not out[-1].endswith("\n"):
                    out.append("\n")
                out.append(new_block if new_block.endswith("\n") else new_block + "\n")
                inserted = True
            continue  # drop the old research-rag block
        out.extend(blk)
    return "".join(out)


def register_codex_mcp(
    config_path: Path,
    repo_root: Path,
    python_executable: str | Path = sys.executable,
) -> str:
    """Register the research-rag stdio MCP server in a Codex config.toml.

    Pure and injectable — the caller supplies the exact target path (this
    function never resolves it from the environment), so tests hit temp files
    only. Returns one of:

        "written"            file (or the section) newly created / appended
        "already-registered" section present and identical — no write
        "updated"            section present but different — backed up + replaced
        "skipped"            existing file is not valid TOML — left untouched

    Detection uses tomllib (or tomli on Python 3.10); the write is a controlled
    text edit that touches only the research-rag table and its sub-tables.
    """
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib

    desired_args = [_codex_launcher_arg(repo_root)]
    desired_command = _python_command_arg(python_executable)
    new_block = _render_codex_mcp_section(repo_root, python_executable)

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(new_block, encoding="utf-8")
        return "written"

    raw = config_path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return "skipped"

    existing = parsed.get("mcp_servers", {}).get(CODEX_MCP_SERVER_NAME)
    if isinstance(existing, dict):
        same = (
            existing.get("command") == desired_command
            and list(existing.get("args", [])) == desired_args
            and not existing.get("env")
        )
        if same:
            return "already-registered"
        # Different content → back up the whole file, then surgically replace.
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(config_path, config_path.with_name(config_path.name + f".bak-{ts}"))
        config_path.write_text(_replace_codex_section(raw, new_block), encoding="utf-8")
        return "updated"

    # No research-rag section — append, keeping existing servers intact.
    sep = "" if raw.endswith("\n\n") else ("\n" if raw.endswith("\n") else "\n\n")
    config_path.write_text(raw + sep + new_block, encoding="utf-8")
    return "written"


# --- Step implementations --------------------------------------------------


def step_python_version() -> bool:
    _print_section(1, "Python version")
    major, minor = sys.version_info[:2]
    actual = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 10):
        print(_red(f"  ✗ Running Python {actual}; minimum is 3.10."))
        print("    The codebase uses `int | None` syntax (PEP 604) which needs 3.10+.")
        print("    Install Python 3.10+ from https://www.python.org/downloads/.")
        return False
    if (major, minor) == (3, 11):
        print(_green(f"  ✓ Running Python {actual} (validated reference)"))
        return True
    print(_green(f"  ✓ Running Python {actual}"))
    print(f"    (3.11 is the validated reference; {actual} should work fine.)")
    return True


def step_bootstrap_env() -> list[str]:
    _print_section(2, "Bootstrap .env file")
    if ENV_PATH.exists():
        print(_green(f"  ✓ {ENV_PATH.name} already exists at {ENV_PATH}"))
        return _load_env_lines()
    if not ENV_EXAMPLE_PATH.exists():
        print(_red(f"  ✗ {ENV_EXAMPLE_PATH} missing — repo state is broken"))
        sys.exit(1)
    print(f"  Copying .env.example → .env (your local config; git-ignored)")
    shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
    print(_green("  ✓ created .env"))
    return _load_env_lines()


def pin_installed_python_paths(lines: list[str]) -> list[str]:
    """Record the interpreters setup actually installed.

    Bare ``python`` / ``python3`` defaults are not portable across hosts. The
    scanner interpreter is always the current process; isolated installs use
    the service venv for RAG subprocesses.
    """
    main_python = _python_command_arg(sys.executable)
    service_python = (
        REPO_ROOT
        / "service"
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    rag_python = _python_command_arg(service_python) if service_python.exists() else main_python
    bare_aliases = {"", "python", "python3", "py"}
    if _read_env_var(lines, "LOCALRAG_MAIN_PYTHON") in bare_aliases:
        lines = _set_env_var(lines, "LOCALRAG_MAIN_PYTHON", main_python)
    if _read_env_var(lines, "LOCALRAG_RAG_PYTHON") in bare_aliases:
        lines = _set_env_var(lines, "LOCALRAG_RAG_PYTHON", rag_python)
    _save_env_lines(lines)
    return lines


def step_vault_paths(lines: list[str]) -> list[str]:
    _print_section(3, "Vault paths")
    home = str(Path.home())

    notes_dir = _read_env_var(lines, "LOCALRAG_NOTES_DIR")
    default_notes = notes_dir or f"{home}/research-note"
    print("  Where do your generated Markdown notes live?")
    print("  (This is also where the PDF indexer discovers what to embed,")
    print("   via the pdf_*_path fields in note frontmatter.)")
    answer = _ask("notes directory", default=default_notes)
    lines = _set_env_var(lines, "LOCALRAG_NOTES_DIR", answer)

    home_dir = _read_env_var(lines, "LOCALRAG_HOME")
    default_home = home_dir or f"{home}/.localrag"
    print()
    print("  Where should ChromaDB and ledger files go?")
    answer = _ask("state directory", default=default_home)
    lines = _set_env_var(lines, "LOCALRAG_HOME", answer)
    _save_env_lines(lines)
    print(_green("  ✓ paths written"))
    return lines


def step_backend_selection(lines: list[str]) -> list[str]:
    _print_section(4, "LLM backend")
    # Default to "subagent" so a user who hits Enter without thinking
    # lands on the no-credentials path, matching the README and
    # .env.example. The harder backends (vertex/anthropic/openai) are
    # explicit opt-ins.
    current = _read_env_var(lines, "LOCALRAG_PROCESSOR_BACKEND") or "subagent"
    print("  Which LLM backend should generate notes?")
    print("    1. subagent    — [EASIEST, default] No external API; the host LLM")
    print("                     agent (Claude Code, Codex, OpenClaw, ...) generates notes")
    print("    2. gemini-api  — Google AI Studio API key; simplest cloud option")
    print("    3. anthropic   — Anthropic Claude with PDF support")
    print("    4. openai      — OpenAI / OpenAI-compatible (DeepSeek/Mistral/OpenRouter/vLLM/Ollama)")
    print("    5. vertex      — Vertex AI Gemini (production); needs GCP service account + GCS bucket")
    print()
    options = ["subagent", "gemini-api", "anthropic", "openai", "vertex"]
    default_idx = options.index(current) + 1 if current in options else 1
    while True:
        answer = _ask("pick 1-5", default=str(default_idx))
        try:
            idx = int(answer)
            if 1 <= idx <= 5:
                break
        except ValueError:
            pass
        print(_red("  invalid choice"))
    backend = options[idx - 1]
    lines = _set_env_var(lines, "LOCALRAG_PROCESSOR_BACKEND", backend)
    _save_env_lines(lines)
    print(_green(f"  ✓ backend = {backend}"))
    return lines


def _ask_credential(lines: list[str], key: str, prompt: str, *, is_path: bool = False, sensitive: bool = False) -> list[str]:
    current = _read_env_var(lines, key)
    if current and not current.startswith("/path/to") and current != "your-gcp-project-id" and current != "your-gcs-bucket-name":
        masked = current
        if sensitive and len(current) > 12:
            masked = current[:4] + "…" + current[-4:]
        print(f"  {key} already set ({masked})")
        if not _confirm(f"  replace {key}?", default=False):
            return lines
    if sensitive:
        answer = getpass.getpass(f"  {prompt}: ").strip()
        while not answer:
            print(_red("  (this field is required)"))
            answer = getpass.getpass(f"  {prompt}: ").strip()
    else:
        answer = _ask(prompt)
    if is_path:
        path = Path(os.path.expandvars(os.path.expanduser(answer)))
        if not path.exists():
            print(_yellow(f"  ⚠ {path} not found; saving anyway — fix before running pipeline"))
    lines = _set_env_var(lines, key, answer)
    _save_env_lines(lines)
    return lines


def step_backend_credentials(lines: list[str]) -> list[str]:
    backend = _read_env_var(lines, "LOCALRAG_PROCESSOR_BACKEND") or "subagent"
    _print_section(5, f"{backend} credentials")

    if backend == "vertex":
        print("  Vertex AI needs three things:")
        print("    1. A service-account JSON file (download from GCP Console → IAM → Service Accounts)")
        print("    2. The GCP project ID")
        print("    3. A GCS bucket name (created on first run if missing)")
        print()
        lines = _ask_credential(lines, "GOOGLE_APPLICATION_CREDENTIALS", "service-account JSON path", is_path=True)
        lines = _ask_credential(lines, "GOOGLE_CLOUD_PROJECT", "GCP project ID")
        lines = _ask_credential(lines, "GEMINI_VERTEX_GCS_BUCKET", "GCS bucket name (e.g. <project>-gemini-literature-temp)")
        location = _read_env_var(lines, "GOOGLE_CLOUD_LOCATION") or "global"
        lines = _set_env_var(lines, "GOOGLE_CLOUD_LOCATION", location)
        bucket_loc = _read_env_var(lines, "GEMINI_VERTEX_GCS_BUCKET_LOCATION") or "US"
        lines = _set_env_var(lines, "GEMINI_VERTEX_GCS_BUCKET_LOCATION", bucket_loc)
    elif backend == "gemini-api":
        print("  Get a key from https://aistudio.google.com/apikey")
        lines = _ask_credential(lines, "GEMINI_API_KEY", "API key", sensitive=True)
    elif backend == "anthropic":
        print("  Get a key from https://console.anthropic.com/")
        lines = _ask_credential(lines, "ANTHROPIC_API_KEY", "API key", sensitive=True)
    elif backend == "openai":
        print("  OPENAI_API_KEY — required for OpenAI Inc. or any compatible provider.")
        print("  OPENAI_BASE_URL — leave blank for OpenAI Inc.; set to e.g.")
        print("    https://api.deepseek.com/v1, https://openrouter.ai/api/v1,")
        print("    http://localhost:11434/v1 (Ollama OpenAI compat).")
        lines = _ask_credential(lines, "OPENAI_API_KEY", "API key", sensitive=True)
        base_url = _read_env_var(lines, "OPENAI_BASE_URL")
        new_base = _ask("base URL (blank for OpenAI Inc.)", default=base_url, allow_empty=True)
        lines = _set_env_var(lines, "OPENAI_BASE_URL", new_base)
        _save_env_lines(lines)
    elif backend == "subagent":
        print(_green("  ✓ subagent backend needs no external credentials"))
    _save_env_lines(lines)
    print(_green(f"  ✓ {backend} configured"))
    return lines


_BACKEND_MODULES = {
    "subagent": (),
    "gemini-api": ("google.genai",),
    "vertex": ("google.genai", "google.cloud.storage"),
    "anthropic": ("anthropic",),
    "openai": ("openai", "pdfplumber"),
}


def ensure_backend_dependencies(lines: list[str]) -> bool:
    """Offer to install only the SDK required by the selected backend."""
    import importlib.util

    backend = _read_env_var(lines, "LOCALRAG_PROCESSOR_BACKEND") or "subagent"

    def module_missing(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is None
        except (ImportError, ModuleNotFoundError):
            return True

    missing = [
        module
        for module in _BACKEND_MODULES.get(backend, ())
        if module_missing(module)
    ]
    if not missing:
        return True

    requirements = REPO_ROOT / "requirements-backends" / f"{backend}.txt"
    print(_yellow(f"  ⚠ {backend} needs extra modules: {', '.join(missing)}"))
    if not requirements.is_file():
        print(_red(f"  ✗ missing requirements file: {requirements}"))
        return False
    if not _confirm(f"  Install the {backend} SDK now?", default=True):
        print(f"    Install later: {sys.executable} -m pip install -r {requirements}")
        return False
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        cwd=str(REPO_ROOT),
    )
    if completed.returncode != 0:
        print(_red(f"  ✗ backend dependency install failed (exit {completed.returncode})"))
        return False
    print(_green(f"  ✓ {backend} SDK installed"))
    return True


def _autodetect_zotero_db() -> Path | None:
    home = Path.home()
    candidates = [
        home / "Zotero" / "zotero.sqlite",
        home / "Documents" / "Zotero" / "zotero.sqlite",
    ]
    if sys.platform == "win32":
        candidates.append(home / "AppData" / "Roaming" / "Zotero" / "Zotero" / "zotero.sqlite")
    elif sys.platform == "darwin":
        # macOS: data is usually ~/Zotero (covered above), but the profile
        # lives under ~/Library/Application Support. Some users keep both
        # there if they pointed Zotero's data dir back at the profile dir.
        candidates.append(
            home / "Library" / "Application Support" / "Zotero" / "zotero.sqlite"
        )
    else:
        # Linux + others
        candidates.append(home / ".zotero" / "zotero" / "zotero.sqlite")
        candidates.append(
            home / "snap" / "zotero-snap" / "common" / "Zotero" / "zotero.sqlite"
        )
        candidates.append(Path("/usr/share/zotero/zotero.sqlite"))
    for path in candidates:
        if path.exists():
            return path
    return None


def step_zotero(lines: list[str]) -> list[str]:
    _print_section(6, "Zotero database")
    current = _read_env_var(lines, "ZOTERO_DB_PATH")
    if current:
        path = Path(os.path.expandvars(os.path.expanduser(current)))
        if path.exists():
            print(_green(f"  ✓ ZOTERO_DB_PATH = {path}"))
            data_dir = path.parent
            lines = _set_env_var(lines, "ZOTERO_DATA_DIR", str(data_dir))
            _save_env_lines(lines)
            return lines
        print(_yellow(f"  ⚠ ZOTERO_DB_PATH={path} but file not found"))

    detected = _autodetect_zotero_db()
    if detected:
        print(f"  Auto-detected: {detected}")
        if _confirm("  use this?", default=True):
            lines = _set_env_var(lines, "ZOTERO_DB_PATH", str(detected))
            lines = _set_env_var(lines, "ZOTERO_DATA_DIR", str(detected.parent))
            _save_env_lines(lines)
            print(_green("  ✓ Zotero database located"))
            return lines

    print()
    print("  Couldn't auto-detect Zotero. Common locations:")
    print("    Windows: %USERPROFILE%/Zotero/zotero.sqlite")
    print("    macOS:   ~/Zotero/zotero.sqlite")
    print("    Linux:   ~/Zotero/zotero.sqlite")
    print("  Find your actual path in Zotero → Edit → Settings → Advanced → Files and Folders.")
    answer = _ask("Zotero database path", allow_empty=True)
    if answer:
        path = Path(os.path.expandvars(os.path.expanduser(answer)))
        if not path.exists():
            print(_yellow(f"  ⚠ {path} not found; saving anyway"))
        lines = _set_env_var(lines, "ZOTERO_DB_PATH", str(path))
        lines = _set_env_var(lines, "ZOTERO_DATA_DIR", str(path.parent))
        _save_env_lines(lines)
    else:
        print(_yellow("  skipped — pipeline won't be able to scan Zotero until this is set"))
    return lines


def _ollama_running(url: str = "http://localhost:11434", timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def _ollama_models(url: str = "http://localhost:11434", timeout: float = 1.5) -> list[str]:
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read())
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def step_ollama(lines: list[str]) -> None:
    _print_section(7, "Embedding provider")
    # The service default is fastembed (in-process ONNX, zero daemon), so
    # don't push a fresh-clone user to install Ollama they won't use. Branch
    # on LOCALRAG_EMBED_PROVIDER; only the ollama branch runs the (unchanged)
    # Ollama install + model-pull flow below.
    provider = _read_env_var(lines, "LOCALRAG_EMBED_PROVIDER") or "fastembed"

    if provider == "fastembed":
        model = (
            _read_env_var(lines, "LOCALRAG_FASTEMBED_MODEL")
            or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
        print(_green("  ✓ Embedding provider = fastembed (in-process ONNX, zero daemon)"))
        print(f"    Model: {model}")
        print("    The model (~0.22 GB, multilingual zh+en) downloads automatically")
        print("    the first time you run build_notes_db.py / build_pdf_db.py.")
        print()
        if not _confirm("  Switch to Ollama to use a larger embedding model?", default=False):
            print("  Keeping fastembed — nothing else to configure here.")
            return
        lines = _set_env_var(lines, "LOCALRAG_EMBED_PROVIDER", "ollama")
        _save_env_lines(lines)
        print(_green("  ✓ LOCALRAG_EMBED_PROVIDER = ollama; continuing with Ollama setup"))
        print()
        # fall through to the Ollama flow
    elif provider != "ollama":
        print(f"  Embedding provider = {provider} (neither fastembed nor ollama).")
        print("    Nothing to install here. Make sure this provider's credentials")
        print("    are set in .env (see the OpenAI-compatible embedding section).")
        return

    url_raw = _read_env_var(lines, "OLLAMA_EMBED_URL") or "http://localhost:11434/api/embeddings"
    base = url_raw.rsplit("/api/", 1)[0] if "/api/" in url_raw else url_raw

    if not _ollama_running(base):
        print(_red(f"  ✗ Ollama is not running at {base}"))
        print("    Install: https://ollama.com/download")
        if sys.platform == "darwin":
            print("    On macOS: launch the Ollama app, or `brew install ollama && ollama serve`")
        elif sys.platform.startswith("linux"):
            print("    On Linux: `curl -fsSL https://ollama.com/install.sh | sh && ollama serve`")
        elif sys.platform == "win32":
            print("    On Windows: install the Ollama installer; it auto-runs as a service.")
        print()
        print("  Skipping model check. Re-run this script after starting Ollama.")
        return
    print(_green(f"  ✓ Ollama running at {base}"))

    target = _read_env_var(lines, "OLLAMA_EMBED_MODEL") or "qwen3-embedding:0.6b"
    models = _ollama_models(base)
    if any(m == target or m.startswith(f"{target}:") or m.split(":")[0] == target for m in models):
        print(_green(f"  ✓ {target} already pulled"))
        return

    # Detect any embedding-like model already present so we can offer to use
    # one instead of insisting on a fresh download.
    embedding_hints = ("embed", "embedding", "bge", "nomic")
    have_embed = [m for m in models if any(h in m.lower() for h in embedding_hints)]
    if have_embed:
        print(_yellow(f"  ⚠ {target} not pulled."))
        print(f"    You already have these embedding-capable models: {', '.join(have_embed)}")
        print("    Options:")
        print(f"      1. Pull the validated reference: {target}  (recommended for first-run)")
        print(f"      2. Switch OLLAMA_EMBED_MODEL to one you already have")
        print("      3. Skip (configure later)")
        print()
        print("    NOTE: switching models against an existing ChromaDB will fail at query")
        print("    time because dimensionalities differ. If you change models after building")
        print("    the DB, rebuild it.")
        choice = _ask("pick 1-3", default="1")
        if choice == "1":
            target_to_pull = target
        elif choice == "2":
            chosen = _ask(f"which model? ({', '.join(have_embed)})", default=have_embed[0])
            lines = _set_env_var(lines, "OLLAMA_EMBED_MODEL", chosen)
            _save_env_lines(lines)
            print(_green(f"  ✓ OLLAMA_EMBED_MODEL = {chosen}"))
            return
        else:
            print("  Skipped.")
            return
    else:
        print(_yellow(f"  ⚠ {target} not pulled (no embedding models present)."))
        print("    The default is qwen3-embedding:0.6b (640 MB, ~1.5 GB RAM, 1024-dim).")
        print("    Other viable choices:")
        print("      qwen3-embedding:4b (3 GB, 2560-dim, higher quality if you have ≥16 GB RAM)")
        print("      nomic-embed-text   (768-dim, fast, English-leaning)")
        print("      mxbai-embed-large  (1024-dim)")
        print("      bge-m3             (1024-dim, strong on Chinese-English mixed)")
        target_to_pull = _ask(
            "model name to pull (or blank to skip)",
            default=target,
            allow_empty=True,
        )
        if not target_to_pull:
            print("  Skipped.")
            return
        if target_to_pull != target:
            lines = _set_env_var(lines, "OLLAMA_EMBED_MODEL", target_to_pull)
            _save_env_lines(lines)

    if _confirm(f"  Pull {target_to_pull} now?", default=True):
        print(f"  Running: ollama pull {target_to_pull}")
        result = subprocess.run(["ollama", "pull", target_to_pull])
        if result.returncode == 0:
            print(_green(f"  ✓ {target_to_pull} pulled"))
        else:
            print(_red(f"  ✗ ollama pull failed (exit {result.returncode})"))
    else:
        print(f"  Skipped — pull manually with: ollama pull {target_to_pull}")


def step_domain_pack(lines: list[str]) -> list[str]:
    _print_section(8, "Domain pack")
    current = _read_env_var(lines, "LOCALRAG_DOMAIN_PACK") or "catalysis"
    packs_dir = REPO_ROOT / "domain-packs"
    available = [
        p.name for p in packs_dir.iterdir()
        if p.is_dir() and p.name not in ("_template",) and (p / "pack.yaml").exists()
    ]
    print(f"  Currently active: {current}")
    print(f"  Available packs: {', '.join(sorted(available)) or '(none)'}")
    print()
    print("  Options:")
    print("    1. Use catalysis (the reference pack — covers electrochemistry)")
    print("    2. Bootstrap a new pack for a different field (asks 6 questions, ~5 min)")
    print("    3. Keep current setting")
    print()
    answer = _ask("pick 1-3", default="3")
    if answer == "1":
        lines = _set_env_var(lines, "LOCALRAG_DOMAIN_PACK", "catalysis")
        _save_env_lines(lines)
        print(_green("  ✓ active pack = catalysis"))
    elif answer == "2":
        # Inline the bootstrap rather than asking the user to open another
        # terminal. We import the helper functions from bootstrap_domain_pack
        # and run the same gather → patch flow here, then activate the new
        # pack by writing LOCALRAG_DOMAIN_PACK to .env.
        try:
            import bootstrap_domain_pack as bdp
        except ImportError as exc:
            print(_red(f"  ✗ could not import bootstrap_domain_pack: {exc}"))
            print(f"    Fallback: python scanner/bootstrap_domain_pack.py --name <your-field>")
            return lines

        slug_raw = _ask("new pack name (e.g. cell-biology, cs-ml)")
        slug = bdp._slugify(slug_raw)
        if not slug:
            print(_red("  ✗ name produced an empty slug after sanitization; aborting"))
            return lines
        pack_root = packs_dir / slug
        if pack_root.exists():
            print(_yellow(f"  ⚠ {pack_root} already exists."))
            if _confirm("    use the existing pack?", default=True):
                lines = _set_env_var(lines, "LOCALRAG_DOMAIN_PACK", slug)
                _save_env_lines(lines)
                print(_green(f"  ✓ active pack = {slug}"))
            return lines

        print()
        print(f"  Bootstrapping pack '{slug}' — answer 6 questions:")
        try:
            answers = bdp.gather_answers(slug)
        except (KeyboardInterrupt, EOFError):
            print(_yellow("  bootstrap cancelled; pack not created"))
            return lines

        # Replicate cmd_bootstrap's file-creation logic without re-prompting.
        # README.md is preserved as a per-pack reference card.
        import shutil
        shutil.copytree(bdp.TEMPLATE_DIR, pack_root)
        bdp._write_pack_yaml(pack_root, answers)
        bdp._patch_document_profile_schema(pack_root, answers)
        bdp._patch_quality_rules(pack_root, answers)
        bdp._create_extra_template_stubs(pack_root, answers)

        lines = _set_env_var(lines, "LOCALRAG_DOMAIN_PACK", slug)
        _save_env_lines(lines)
        print()
        print(_green(f"  ✓ pack scaffolded at: {pack_root}"))
        print(_green(f"  ✓ LOCALRAG_DOMAIN_PACK = {slug}"))
        print()
        print(_yellow(
            "  ⚠ Scaffolding is the easy 25%. The real work is the next 3 hand-edits;"
        ))
        print(_yellow(
            "    until you do them, generated notes will follow catalysis-style"
        ))
        print(_yellow(
            "    section structure with placeholder TODO field names."
        ))
        print()
        print("  Edit these files in order (the recommended path is in the pack's README.md):")
        print(f"    1. domain-packs/{slug}/schemas/document_profile.vertex.schema.json")
        print(f"       (review the research_domain enum the bootstrap filled in)")
        print(f"    2. domain-packs/{slug}/templates/_domain_quality_rules.txt")
        print(f"       (write your field's trap-scan checklist + filename slot semantics)")
        print(f"    3. domain-packs/{slug}/templates/{answers['extra_templates'][0]}.txt")
        print(f"       (your primary experimental-paper template body structure)")
        print()
        print("  Then validate + dry-run on 5 PDFs to see real output before writing more templates:")
        print(f"    python scanner/bootstrap_domain_pack.py --validate {slug}")
        print(f"    python scanner/zotero_batch_scanner.py --limit 5")
        print()
        print("  Full guide: docs/Domain_Pack_Authoring_Guide.md (top section is a worked")
        print("  cell-biology example you can adapt slot-for-slot).")
    return lines


def step_skills() -> None:
    _print_section(9, "Claude Code skills installation")
    skills_src = REPO_ROOT / "skills"
    if not skills_src.exists():
        print(_yellow("  skills/ directory missing in repo — skipping"))
        return

    targets = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
    ]
    has_install = any((t / "search-literature").exists() for t in targets)
    if has_install:
        existing = [t for t in targets if (t / "search-literature").exists()]
        print(_green(f"  ✓ skills already installed at {existing[0]}"))
        return

    print(f"  Skills live at: {skills_src}")
    print("  Claude Code reads ~/.claude/skills/; Codex and compatible agents read ~/.agents/skills/.")
    print()
    if not _confirm("  Install skills for Claude Code and compatible terminal agents?", default=True):
        print(f"  Skipped — copy {skills_src} into your agent's skill directory later.")
        return
    targets = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
    ]
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        installed = 0
        for child in skills_src.iterdir():
            dest = target / child.name
            if dest.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)
            installed += 1
        print(_green(f"  ✓ {target}: {installed} installed, existing skills preserved"))


def _manual_mcp_command(cli: str, python_executable: str | Path) -> str:
    prefix = (
        f"{cli} mcp add --transport stdio --scope local research-rag --"
        if cli == "claude"
        else f"{cli} mcp add research-rag --"
    )
    return (
        f'{prefix} "{_python_command_arg(python_executable)}" '
        f'"{_codex_launcher_arg(REPO_ROOT)}"'
    )


def register_claude_mcp(
    claude_executable: str,
    repo_root: Path,
    python_executable: str | Path = sys.executable,
) -> subprocess.CompletedProcess[str]:
    """Register a machine-specific local MCP command for Claude Code."""
    return subprocess.run(
        [
            claude_executable,
            "mcp",
            "add",
            "--transport",
            "stdio",
            "--scope",
            "local",
            CODEX_MCP_SERVER_NAME,
            "--",
            _python_command_arg(python_executable),
            _codex_launcher_arg(repo_root),
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def step_terminal_mcp(lines: list[str]) -> None:
    _print_section(10, "Terminal agent MCP registration")
    print("  Retrieval runs as an stdio MCP server the terminal agent spawns")
    print("  per session (tools: search_notes, search_papers, get_note, index_status).")
    print()
    claude = shutil.which("claude")
    if claude and _confirm("  Register the MCP server for Claude Code now?", default=True):
        result = register_claude_mcp(claude, REPO_ROOT, sys.executable)
        if result.returncode == 0:
            print(_green("  ✓ Claude Code local MCP registration uses this venv's Python"))
        else:
            detail = (result.stderr or result.stdout).strip()
            print(_yellow(f"  ⚠ Claude Code registration failed: {detail or 'unknown error'}"))
            print(f"    Run manually: {_manual_mcp_command('claude', sys.executable)}")
    elif claude:
        print(f"  Skipped. Register later with: {_manual_mcp_command('claude', sys.executable)}")
    else:
        print("  Claude Code CLI not found. The repo's .mcp.json remains a fallback,")
        print("  but a local registration is more portable because it pins this venv.")
        print(f"    After installing Claude Code: {_manual_mcp_command('claude', sys.executable)}")
    print()
    print("  Codex reads ~/.codex/config.toml instead of .mcp.json.")
    codex = shutil.which("codex")
    if not _confirm("  Register the MCP server for Codex now (writes config.toml)?", default=bool(codex)):
        print("  Skipped Codex registration. Register later with:")
        print(f"    {_manual_mcp_command('codex', sys.executable)}")
        return

    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        config_path = Path(os.path.expandvars(os.path.expanduser(codex_home))) / "config.toml"
    else:
        config_path = Path.home() / ".codex" / "config.toml"

    try:
        outcome = register_codex_mcp(config_path, REPO_ROOT, sys.executable)
    except Exception as exc:
        print(_red(f"  ✗ could not register Codex MCP: {exc}"))
        print(f"    Register manually: {_manual_mcp_command('codex', sys.executable)}")
        return

    if outcome == "written":
        print(_green(f"  ✓ registered [mcp_servers.research-rag] in {config_path}"))
    elif outcome == "updated":
        print(_green(f"  ✓ updated [mcp_servers.research-rag] in {config_path} (backup written)"))
    elif outcome == "already-registered":
        print(_green(f"  ✓ research-rag already registered in {config_path} (no change)"))
    elif outcome == "skipped":
        print(_yellow(f"  ⚠ {config_path} exists but isn't valid TOML; left it untouched."))
        print(f"    Register manually: {_manual_mcp_command('codex', sys.executable)}")
    else:
        print(f"  register_codex_mcp returned: {outcome}")


def step_doctor() -> int:
    _print_section(11, "Final health check")
    doctor_path = REPO_ROOT / "scanner" / "doctor.py"
    print("  Running doctor.py to verify everything is wired up...")
    print()
    result = subprocess.run([sys.executable, str(doctor_path)])
    return result.returncode


# --- Orchestration ---------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive setup walkthrough for a fresh research-rag clone."
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip all prompts; just run doctor.py to report current state.",
    )
    args = parser.parse_args()

    if args.non_interactive:
        return step_doctor()

    print()
    print(_bold("=" * 64))
    print(_bold("  research-rag interactive setup"))
    print(_bold("=" * 64))
    print()
    print("  This walks through 11 setup steps (~5-15 minutes depending on which")
    print("  LLM backend you pick). Re-running picks up from where you left off.")
    print("  Press Ctrl-C at any prompt to stop; .env saves are atomic per step.")
    print()

    if not step_python_version():
        return 1
    lines = step_bootstrap_env()
    lines = pin_installed_python_paths(lines)
    lines = step_vault_paths(lines)
    lines = step_backend_selection(lines)
    lines = step_backend_credentials(lines)
    ensure_backend_dependencies(lines)
    lines = step_zotero(lines)
    step_ollama(lines)
    lines = step_domain_pack(lines)
    step_skills()
    step_terminal_mcp(lines)

    print()
    print(_bold("Setup walkthrough complete."))
    print()
    doctor_status = step_doctor()
    if doctor_status == 2:
        print(_yellow("Setup finished; doctor reported non-blocking warnings above."))
        return 0
    return doctor_status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print(_yellow("Interrupted. .env is saved up to the last completed step."))
        sys.exit(130)

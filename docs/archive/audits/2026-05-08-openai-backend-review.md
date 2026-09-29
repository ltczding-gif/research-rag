# Code Review: OpenAI-Compatible Backend (commit 8d2aed8)

**Reviewer:** Senior Code Review  
**Date:** 2026-05-08  
**Scope:** `aaa0441..8d2aed8` — OpenAI / OpenAI-compatible backend addition  
**Files reviewed:** `scanner/backends/openai_api.py`, `scanner/backends/__init__.py`, `scanner/gemini_analyze_pdf.py`, `scanner/zotero_batch_scanner.py`, `scanner/config.py`, `.env.example`, `requirements-scanner.txt`, `skills/gemini-literature-processor/SKILL.md`, `docs/ARCHITECTURE.md`, `README.md`, `STATUS.md`

---

## Strengths

**Clean SDK contract** (`openai_api.py:185-186`): The `tools=[{...}]` payload shape is correctly formed for the OpenAI Python SDK v1.x — `type: "function"` wrapper with `function.name`, `function.description`, and `function.parameters`. The `tool_choice={"type": "function", "function": {"name": ...}}` form is exactly correct for the OpenAI API (as opposed to Anthropic's `"tool"` verbiage). No confusion between the two protocols.

**Defensive tool_calls access** (`openai_api.py:190`): `getattr(choice, "tool_calls", None) or []` correctly handles both `None` and missing attributes. The inner function-name check (`fn.name == _TOOL_NAME`) prevents accidentally processing tool calls from other tools if a provider injects them.

**Lazy import pattern** (`openai_api.py:63-74`): Both `openai` and `pdfplumber` are imported inside `__init__`, so the module loads cleanly in environments where neither is installed. The fail-fast check at construction time (rather than mid-PDF-processing) is the right UX.

**Fallback path** (`openai_api.py:202-209`): The content-JSON fallback for providers that ignore `tool_choice` is well-placed. It avoids a hard failure on Ollama and DeepSeek, which are the most common local/cheap alternatives.

**Text-only limitation disclosure**: The tradeoff is documented consistently in the module docstring, SKILL.md (with a callout block), ARCHITECTURE.md (in both the table note and an explicit paragraph), `.env.example` (inline comment), and the `--backend` help string. Coverage is thorough.

**Requirements rename** (`requirements-scanner.txt`): The reorganization with per-backend opt-in comments and a clear "required for orchestrator regardless" section is a meaningful improvement over the old flat file. The `pdfplumber` version pin (`>=0.10`) matches a version that introduced stable `extract_text()` behavior.

**Env wiring is complete and self-consistent**: `config.py`, `.env.example`, and `make_backend_from_args` all agree on the same five env vars. The empty-string-to-None normalization (`or None`) in `make_backend_from_args` prevents the SDK from treating an empty string as a URL.

---

## Issues

### Critical (Must Fix)

**C1 — `response.choices[0]` unguarded against empty choices list**  
`openai_api.py:189`

```python
choice = response.choices[0].message
```

Some OpenAI-compatible providers (especially local ones under load, or when the model hits a content policy) return `finish_reason="content_filter"` or `finish_reason="stop"` with an empty `choices` list. This raises `IndexError` instead of the informative `RuntimeError` the rest of the error handling is designed to produce.

**Fix:** Guard before the access:

```python
if not response.choices:
    raise RuntimeError(
        f"OpenAIBackend stage={stage}: model {translated_model} returned "
        "an empty choices list. The provider may have applied a content filter."
    )
choice = response.choices[0].message
```

This is particularly important for Ollama and vLLM deployments running quantized models that can produce empty responses under memory pressure.

---

**C2 — `_DEFAULT_FLASH` / `_DEFAULT_PRO` resolved at import time, not at instantiation**  
`openai_api.py:39-40`

```python
_DEFAULT_FLASH = os.environ.get("OPENAI_FLASH_MODEL", "gpt-4o-mini")
_DEFAULT_PRO = os.environ.get("OPENAI_PRO_MODEL", "gpt-4o")
```

These module-level constants are evaluated the moment the module is imported, which happens inside `make_backend()` when the user selects this backend. If the user loads a `.env` file *after* module import (e.g., via `python-dotenv` called partway through `gemini_analyze_pdf.py`'s startup), the env vars may not yet be set, and the defaults (`gpt-4o-mini` / `gpt-4o`) are frozen in — silently overriding whatever the user put in `.env`.

The Anthropic backend avoids this: it passes the model IDs through at call time rather than baking them into module globals. The OpenAI backend should do the same, reading `os.environ.get(...)` inside `_translate_model_id` rather than at module load.

**Fix:**

```python
@staticmethod
def _translate_model_id(model_id: str) -> str:
    flash = os.environ.get("OPENAI_FLASH_MODEL", "gpt-4o-mini")
    pro = os.environ.get("OPENAI_PRO_MODEL", "gpt-4o")
    if not model_id:
        return flash
    m = model_id.lower()
    if "pro" in m and "approx" not in m:
        return pro
    if any(token in m for token in ("flash", "haiku", "mini")):
        return flash
    return model_id
```

This is a behavioral correctness issue for any user who relies on `.env` auto-loading.

---

### Important (Should Fix)

**I1 — `--backend openai-api` and `--backend openai-compatible` are accepted by `make_backend()` but not by argparse `choices`**  
`scanner/gemini_analyze_pdf.py:472`, `scanner/backends/__init__.py:46`

`make_backend()` accepts the aliases `"openai-api"` and `"openai-compatible"`, and `make_backend_from_args` mirrors this with `if name in ("openai", "openai-api", "openai-compatible")`. But the argparse `choices=[..., "openai", ...]` only includes `"openai"`. A user who passes `--backend openai-api` gets an argparse error before the code ever reaches `make_backend_from_args`.

Either add the aliases to `choices` in both CLI files, or remove them from `make_backend()` and `make_backend_from_args` so there's a single canonical spelling. The half-exposed aliases create a confusing contract. Recommend the latter (one canonical name, no hidden aliases in the public CLI).

---

**I2 — `self._OpenAI = OpenAI` at line 77 is dead code**  
`openai_api.py:77`

After `self.client = OpenAI(...)` is assigned at line 78, `self._OpenAI` is never used anywhere in the class. It's the class constructor, saved but never called again. This is likely a copy-paste artifact from an earlier design where the client was created lazily. It can confuse a reader into thinking the class uses multiple OpenAI instances.

**Fix:** Remove line 77 (`self._OpenAI = OpenAI`).

---

**I3 — Double pdfplumber import: conceptual inconsistency**  
`openai_api.py:71-74` (import-to-check) and `openai_api.py:93` (import-to-use)

`__init__` imports `pdfplumber` solely to verify it's installed (the import is discarded — note the `# noqa: F401`). Then `attach_pdfs` imports it again locally to actually use it. This pattern works correctly (Python's import system caches modules), but it's semantically odd: the first import checks availability and raises a user-friendly error; the second import trusts that the first passed. A reader may wonder why the check-import is separate from the use-import.

The idiomatic approach for this pattern is to store the module reference:

```python
# in __init__:
import pdfplumber as _pdfplumber_mod
self._pdfplumber = _pdfplumber_mod  # or just use it directly

# in attach_pdfs:
# use self._pdfplumber.open(path) or just re-import pdfplumber (fine, it's cached)
```

Or more simply: move the availability check to a class-level `_check_deps()` method and keep the `import pdfplumber` only in `attach_pdfs`. Either approach is cleaner than import-discard followed by import-use. Not a bug, but a readability debt.

---

**I4 — Model translation "pro" substring fires on unintended model names with the openai backend**  
`openai_api.py:126`

The Anthropic backend's version of this check does not have the `"approx" not in m` guard — it was added exclusively in the OpenAI version. The comment in the code says nothing about why `"approx"` matters. There is no current OpenAI, DeepSeek, Mistral, OpenRouter, or vLLM model with "approx" in its name (as of mid-2025). The guard appears to be defensive boilerplate without a concrete target.

More importantly, this logic still fires on model names like `gpt-4o-mini-pro` (hypothetical), `deepseek-coder-pro`, or any community model with "pro" in a non-tier position. The correct behavior for an explicit user-supplied model name with "openai" as the provider is to pass it through — but the substring match intercepts it. Since the verbatim-passthrough path only triggers when *none* of the substrings match, an explicit `--model deepseek-coder-pro` would be silently translated to `OPENAI_PRO_MODEL` (default `gpt-4o`), discarding the user's intent.

**Recommended fix:** Invert the logic. Treat tier keywords as opt-in only when the caller explicitly passes a generic tier token (e.g., exactly `"flash"`, `"pro"`, `"haiku"`, `"mini"` with no other substantive content), and pass everything else verbatim. Alternatively, document the current behavior clearly in the docstring so users know to set `OPENAI_PRO_MODEL=deepseek-coder-pro` instead of passing `--model deepseek-coder-pro`.

---

**I5 — 200,000-char truncation: no warning surfaced to the user at runtime**  
`openai_api.py:104-108`, `_build_messages` label `" (truncated)"`

When a PDF is truncated, the `blocks` dict records `"truncated": True`, and `_build_messages` appends `" (truncated)"` to the section header. This tells the model that content was cut. However, nothing surfaces this to the human operator. A user processing a dense methods paper + 40-page SI might lose the entirety of the supplemental information without realizing it, and receive a note that silently omits critical synthesis conditions.

The `attach_pdfs` method should emit a warning to `sys.stderr` (or via `logging.warning`) when truncation occurs, naming the file and the character counts:

```python
if truncated:
    print(
        f"[OpenAI backend] WARNING: {Path(path).name} truncated from "
        f"{len(full_text):,} to {self.max_chars_per_pdf:,} chars.",
        file=sys.stderr,
    )
```

Other backends have no such truncation, so this is unique to the OpenAI path and operators may not expect it.

---

### Minor (Nice to Have)

**m1 — SKILL.md Ollama model name: colon syntax is ambiguous for OpenAI compat**  
`skills/gemini-literature-processor/SKILL.md:184`

```bash
OPENAI_FLASH_MODEL=qwen2.5:14b OPENAI_PRO_MODEL=qwen2.5:32b
```

Ollama's native API uses colon syntax (`qwen2.5:14b`). Its OpenAI-compatibility layer at `/v1/chat/completions` accepts both colon and hyphen forms, but behavior varies by Ollama version — some older versions (pre-0.3) only accept the native colon form on the OpenAI endpoint if served with `OLLAMA_ORIGINS=*`. The example is likely correct for current Ollama (0.3+), but worth a short inline note ("Ollama accepts `qwen2.5:14b` on its OpenAI-compat endpoint as of v0.3+").

---

**m2 — `requirements-scanner.txt` rename shows as delete + add, not rename**  
Diff at `requirements-gemini.txt` / `requirements-scanner.txt`

The file was removed with `rm` and a new file created, rather than `git mv`. Git's similarity detection would normally flag this as a rename (the content overlap is sufficient), but the diff shows separate `deleted file mode` and `new file mode` hunks rather than a `rename from / rename to` header. This is cosmetic for review readability and `git log --follow` history tracking. Not a bug, but future contributors will lose blame history on the old lines.

---

**m3 — Install hint in ImportError message omits version pin**  
`openai_api.py:67`, `openai_api.py:73`

The error messages say `pip install openai` and `pip install pdfplumber`. The `requirements-scanner.txt` pins `openai>=1.40` and `pdfplumber>=0.10`. The error messages should match:

```
Install with: pip install "openai>=1.40"
Install with: pip install "pdfplumber>=0.10"
```

Minor ergonomic gap — a user following the error hint may install an older version that lacks the required API shape.

---

**m4 — `max_tokens=8192` not exposed as env var**  
`openai_api.py:54`

Every other configurable parameter (`api_key`, `base_url`, `organization`) has an env var. `max_tokens` defaults to 8192 and is only configurable by instantiating the class directly (no CLI flag, no env var). Some providers (Groq, Together free tier) cap at 4096 or 8000. A user who hits this won't see a clear error — they'll get a provider-side error message about token limits, with no obvious env var to adjust. Adding `OPENAI_MAX_TOKENS` would be consistent and cheap.

---

## Recommendations

1. **After fixing C1 and C2**, add an integration smoke-test against a local Ollama endpoint (or mocked `openai.OpenAI`) that exercises the empty-choices path and the `.env`-loaded env-var path. The project has no test suite yet, but these two failure modes are subtle enough that manual verification is insufficient.

2. **Clarify the model-translation contract in the docstring** (regardless of I4 fix): state explicitly that any model name containing "pro" (except names containing "approx") is silently redirected to `OPENAI_PRO_MODEL`. Users choosing models from OpenRouter's catalog of hundreds of models need to know this.

3. **Consider adding `OPENAI_MAX_TOKENS`** (see m4) as a follow-up env var in a small patch. It would make the backend fully self-service for providers with lower token caps, without requiring code changes.

4. **The investigation report reference** in `docs/investigation/05-packaging-portability.md` still mentions `requirements-gemini.txt` (the old name). This is an artifact document so it's not urgent, but a one-line note that it was renamed would prevent confusion.

---

## Assessment

**Ready to merge?** No — with fixes (C1, C2 required before merge; I1–I5 strongly advised)

**Reasoning:** The SDK contract, fallback logic, and documentation are solid — this is well-structured work that addresses the stated goal of broad OpenAI-compatible provider support. However, the unguarded `response.choices[0]` access (C1) is a runtime crash on multiple real-world providers, and the module-level env var resolution (C2) will silently ignore `.env`-loaded model overrides for users of the `python-dotenv` pattern that `setup.sh` and `setup.ps1` encourage. Both are straightforward fixes that should precede merge.

**Issue counts:** 2 Critical, 5 Important, 4 Minor.

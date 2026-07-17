# Code Review: T1 + T2 Batch (6 commits, 725d84e..38b8a22)

**Date:** 2026-05-08  
**Reviewer:** Senior Code Reviewer (automated pass)  
**Scope:** Commits 86cd17b → 38b8a22; read-only; excludes `docs/audits/` and `docs/investigation/` archives.  
**Stats:** 15 files changed, +992 / -651 (net +341 lines, but commit 1 alone is −415 so the real "new capability" footprint is ~+756 lines).

---

## Strengths

**Commit 1 (86cd17b) — clean excision.**  
The legacy `--pipeline-mode` path is gone with no loose threads in the scanner or batch-scanner source. `args.pipeline_mode` is fully purged from `scanner/gemini_analyze_pdf.py` and `scanner/zotero_batch_scanner.py`. The `find_existing_multifacet_note_path` gate preserved its `publish_target == "vault"` contract correctly (`gemini_analyze_pdf.py:1814`). No downstream consumer of `archive_manifest["pipeline_mode"]` exists in the live code path — the field was simply dropped with no consumer to break.

**Commit 3 (ac9d174) — hashing module is well-designed.**  
`scanner/_hashing.py` has a clean public surface (`__all__` is explicit), thorough docstrings, and the dual-variant design (`stable` / `legacy`) with a migration path is exactly right. `normalize_pdf_group_paths` handling deduplication via case-folded keys is Windows-correct.

**Commit 4 (4b11d45) — 3-invocation flow is clear.**  
`SubagentBackend.call_model()` correctly distinguishes "output exists → load it" from "output missing → write manifest + raise" regardless of resume mode. The `--resume` flag description in `build_arg_parser()` (`gemini_analyze_pdf.py:469–474`) is accurate and warns users it's subagent-only. SKILL.md update with the full dispatch prompts is a genuine improvement to operator ergonomics.

**Commit 5 (659bd62) — smoke tests are well-scoped.**  
`test_resume_mode_falls_through_to_manifest_for_missing_stage` (`tests/test_subagent_backend.py:63–84`) correctly exercises the critical path: Stage A present, Stage B absent, expect Stage B's manifest + raise. The fixture-based file creation with `tmp_path` avoids any filesystem side effects. All 15 tests pass in 0.14 s with no external dependencies.

**Commit 2 (0693063) — factory duplication is justified.**  
`_resolve_project_id_from_credentials()` in `backends/__init__.py` is indeed a deliberate duplicate; a direct import of `resolve_project_id()` from `gemini_analyze_pdf.py` would create a circular import (`backends` ← `gemini_analyze_pdf` ← `backends`). The duplication is small (14 lines) and the docstring explains the reasoning. Acceptable.

---

## Issues

### Critical (Must Fix)

**C1 — `service/build_pdf_db.py`: stale-chunk exception catch is dead code and masks real errors**  
`service/build_pdf_db.py:265–268`

```python
except Exception as exc:
    # ChromaDB raises if no docs match the where; that's fine.
    if "no documents" not in str(exc).lower():
        print(f"    [WARN] stale-chunk delete failed (non-fatal): {exc}")
```

On ChromaDB 1.5.5 with the Rust backend (`chromadb/api/rust.py:576–603`), `col.delete(where={"pdf_path": pdf_path})` **never raises** on an empty match. The Rust binding returns a count of 0 silently. The `try/except` block therefore:

1. Swallows any real errors that `col.delete()` might raise (e.g., `chromadb.errors.ChromaError` from a corrupted index, a malformed `where` filter, or a permissions issue) — they all fall into the `if "no documents" not in ...` branch and get printed as non-fatal warnings instead of propagating.
2. The comment "ChromaDB raises if no docs match" is factually wrong for the Rust backend, which is the only backend supported by the pinned version.

**Fix:** Remove the `try/except` entirely, or if defensive coding is desired, propagate the exception:

```python
col.delete(where={"pdf_path": pdf_path})
col.add(documents=chunks, ids=ids, metadatas=metas)
```

The `col.delete()` call is idempotent-on-miss on 1.5.5. No catch needed.

---

### Important (Should Fix)

**I1 — `service/build_pdf_db.py:get_combined_hash` silently diverges from `_hashing.stable_combined_hash` for duplicate paths**  
`service/build_pdf_db.py:133–158` vs `scanner/_hashing.py:46–62`

The service copy iterates `file_paths` directly and hashes any path where `os.path.exists(path)` is true — it does **not** call `normalize_pdf_group_paths`, so:
- Duplicate paths in the input produce duplicate per-file hashes in `file_hashes`, making the combined hash differ from the scanner's result for the same logical group.
- Case-folded deduplication (Windows case-insensitive filesystem) is absent from the service copy.

For the current production use case this is benign (groups come from note frontmatter and are unlikely to carry duplicates), but the "KEEP IN SYNC" comment at `service/build_pdf_db.py:136` implies the functions are identical, which they are not. A user who passes duplicate paths gets different hashes from each side.

**Fix:** In the service copy, add the same normalization step before building `file_hashes`:
```python
seen = {}
for path in file_paths:
    abs_path = os.path.abspath(str(path))
    seen.setdefault(abs_path.casefold(), abs_path)
normalized = sorted(seen.values(), key=lambda v: v.casefold())
```

Alternatively, add a cross-module test (see R1 below).

**I2 — `SKILL.md` still instructs users to pass `--pipeline-mode multifacet-spec`**  
`skills/gemini-literature-processor/SKILL.md:138, 146, 157, 165, 176, 180, 186`

After commit 1, `--pipeline-mode` is not a recognized flag. Passing it to `zotero_batch_scanner.py` or `gemini_analyze_pdf.py` will cause argparse to exit with an error. The runbook examples in `SKILL.md` still show it in six invocations. The scanner-side runbook (`scanner/references/workflow-runbook.md`) was correctly updated in commit 1, but the packaged copy in `skills/gemini-literature-processor/references/workflow-runbook.md` and the higher-level `SKILL.md` command examples were not.

**Fix:** Strip `--pipeline-mode multifacet-spec` from all six `SKILL.md` command blocks. The option is now always implied.

**I3 — `scanner/backends/vertex.py:provider_parts` property is now orphaned dead code**  
`scanner/backends/vertex.py:129–137`

`provider_parts` was the escape hatch for the legacy code path to call `client.models.generate_content` with raw Vertex `types.Part` objects. After commit 1 removed all callers, there are zero references to this property in the entire codebase (`grep -rn "provider_parts" .` returns only the definition). The docstring still says "Used by the deprecated `--pipeline-mode legacy` path" — confirming this should have been cleaned up in commit 1.

**Fix:** Delete the `provider_parts` property from `VertexBackend`. It's 9 lines of dead code.

**I4 — `STATUS.md` "What's still rough" section is fully stale**  
`STATUS.md:115, 144–157`

Line 115: *"Legacy `--pipeline-mode legacy` still works but is now `--backend vertex` only"* — removed in commit 1; should now say it's been deleted entirely.

Lines 144–157: The entire "Tier 1" and "Tier 2" todo lists and the "Known bugs flagged but not yet fixed" section describe work that is now done by this batch. All five items (delete legacy, centralize factory, combined_hash unification, smoke tests, all three bugs) are resolved. The stale section will mislead anyone reading STATUS.md to triage work.

**Fix:** Update `STATUS.md` "What's still rough" to reflect the post-batch state. Replace the todo bullets with a "Resolved in T1+T2 batch (2026-05-08)" note and move the remaining open item (GCS bucket cleanup not wired into scanner main flow, `STATUS.md:157`) to a new "Remaining rough edges" section.

---

### Minor (Nice to Have)

**m1 — `docs/COMPONENTS.md:89` — stale DEFAULT_PROMPT annotation**  
The line *"Lines 1638-1758 的 `DEFAULT_PROMPT` 是 legacy 模式残留死代码"* describes a condition that commit 1 fixed. The line numbers are wrong (the code is gone) and the observation is obsolete. This doc is not an immutable archive. Consider striking or updating this bullet.

**m2 — `test_legacy_combined_hash_path_order_dependent` — misleading test name**  
`tests/test_hashing.py:42–53`

The test name says "path order dependent" but the test body asserts `h1 == h2` — because `normalize_pdf_group_paths` sorts internally, both orderings produce the same result. The docstring explains this correctly, but anyone reading the test name in a failure report will be confused. Rename to `test_legacy_combined_hash_is_deterministic_regardless_of_input_order` or similar.

**m3 — `make_backend_from_env()` is untested in the smoke suite**  
The 4 tests in `tests/test_backend_factory.py` all call `make_backend()` (the low-level constructor). `make_backend_from_env()` — which is the actual entry point used by the CLI — has zero test coverage. The env-var resolution logic (`_need()`, bucket fallback precedence, `_resolve_project_id_from_credentials()`) is completely untested. Adding even two tests (e.g., `test_make_backend_from_env_subagent_no_envvar_needed` and `test_make_backend_from_env_gemini_api_missing_key_exits`) would close the most important gaps.

**m4 — `get_parent_key` regex `[A-Z0-9]{8}` excludes lowercase keys**  
`scanner/gemini_analyze_pdf.py:1650`, `service/build_pdf_db.py:48`

Zotero's storage keys are documented as base-62 (A–Z, a–z, 0–9) and the Zotero client generates them with `Zotero.Utilities.randomString(8)` which uses `[A-Za-z0-9]`. Lowercase letters are valid. On a case-insensitive filesystem (Windows NTFS) this is usually harmless because `os.path.abspath()` will return whatever case the OS recorded; on case-sensitive filesystems (Linux ext4) a path like `.../storage/a3Bc9Xyz/paper.pdf` will fail the regex and silently fall through to the LIKE fallback.

A broader but still safe pattern is `[A-Za-z0-9]{8}`. The fix is low-risk (same specificity, just wider character class).

**m5 — `--resume` with a non-subagent backend is silently accepted**  
`scanner/gemini_analyze_pdf.py:1740–1749`

`make_backend_from_args` only forwards `resume_dir` when `backend_name == "subagent"`. If the user runs `--backend vertex --resume /some/dir`, the `--resume` argument is silently ignored — no warning, no error. This is mentioned as acceptable in the help text ("Only meaningful with `--backend subagent`"), but a user who forgets to set `--backend subagent` will get a confusing silent no-op. A one-line guard:

```python
if getattr(args, "resume", None) and backend_name != "subagent":
    print("[WARN] --resume is only meaningful with --backend subagent; ignoring.", file=sys.stderr)
```

would make this failure mode visible.

---

## Recommendations

**R1 — Add a cross-module hash identity test.**  
The "KEEP IN SYNC" comment in `service/build_pdf_db.py` is the only enforcement mechanism. A test in `tests/test_hashing.py` that imports both `scanner/_hashing.py:stable_combined_hash` and (via `sys.path` manipulation) `service/build_pdf_db.py:get_combined_hash` and asserts they produce identical output for the same two-file input would catch any future drift. This is the most valuable single test to add next.

**R2 — Migration note for existing `service/processed_groups.txt` users.**  
Commit 3 switches the service's hash algorithm to the stable variant. Users who have an existing `processed_groups.txt` generated by the old path-order algorithm will not re-process already-indexed PDFs (correct), but any new addition to an existing group will produce a stable hash that doesn't match the old ledger entry, triggering a re-ingest of the entire group. This is probably fine, but the `main()` in `build_pdf_db.py` could print a one-line note when it first loads a ledger: *"Note: hashes generated before 2026-03 used a path-order algorithm; affected groups will be re-indexed."* Low priority but good for operator hygiene.

**R3 — The Zotero key regex should be `[A-Za-z0-9]{8}`.**  
See m4. One-line change in two files; zero behavior change on Windows, correctness fix on case-sensitive filesystems.

---

## Assessment

**Ready to merge?** Yes, with fixes.

**Reasoning:** The batch is architecturally sound and closes all targeted Tier-1 and Tier-2 items. The single Critical issue (C1 — the dead exception catch over `col.delete()`) is a 3-line fix that removes incorrect defensive code; it does not block correctness in the common case (the delete already works silently on 0 matches) but it does mask any future real errors. Fix C1 and address I2 (SKILL.md stale flag examples, which will cause argparse errors for any user following the runbook) before merging to a shared branch. I3 and I4 are housekeeping that can ship in the same PR or a follow-up.

---

*Generated by automated code review pass. All file:line references verified against commit 38b8a22.*

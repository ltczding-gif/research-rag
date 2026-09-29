# Contributing to research-rag

Thanks for helping. research-rag is alpha software, so bug reports with clear
reproduction steps are as valuable as code.

## Ways to contribute

- **Bug reports**: use the bug report template and include `scanner/doctor.py`
  output and the result of the `index_status` MCP tool.
- **Domain packs**: a pack for a new field is the most useful non-code
  contribution. Start with `python scanner/bootstrap_domain_pack.py --name <field>`
  and follow [docs/Domain_Pack_Authoring_Guide.md](docs/Domain_Pack_Authoring_Guide.md).
  Validate it with `python scanner/bootstrap_domain_pack.py --validate <field>`.
- **Retrieval and parsing quality**: see the roadmap in the README. Changes that
  affect ranking must be scored with
  [`docs/RETRIEVAL_EVAL.md`](docs/RETRIEVAL_EVAL.md). Record the baseline and
  candidate runs in the score ledger and cite their run IDs and the `compare`
  table in the pull request.
- **Documentation**: fixes to the README, `docs/` and skills.

For larger changes, open an issue first so the design can be agreed before you
invest in an implementation.

## Development setup

Requirements: Git and Python 3.10–3.12 (3.11 is the reference version).

```bash
git clone https://github.com/ltczding-gif/research-rag.git
cd research-rag
./setup.sh --no-init            # Windows: .\setup.ps1 -SkipInit
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/demo.py   # synthetic end-to-end MCP check
```

The default test suite makes no cloud LLM calls and needs no Ollama daemon.
See [tests/README.md](tests/README.md) for focused test commands.

## Repository layout

| Path | Contents |
|---|---|
| `scanner/` | Zotero discovery, note generation and generation backends |
| `service/` | Indexing, embeddings, query core and the stdio MCP server |
| `scripts/` | Cross-platform entry points and verification scripts |
| `domain-packs/` | Field-specific prompts, schemas, templates and routing |
| `skills/` | Agent skills published by the plugin |
| `contrib/skills/` | Reference skills for contributors; not installed by default |
| `benchmarks/` | Public retrieval benchmark contract and fixtures |
| `docs/` | Current guides; `docs/archive/` is historical and not maintained |

## Pull request checklist

- [ ] `python -m pytest tests -q` passes locally.
- [ ] New behavior has tests; bug fixes include a regression test where practical.
- [ ] User-facing changes are reflected in **both** `README.md` and `README_zh-CN.md`.
- [ ] `CHANGELOG.md` has an entry under *Unreleased*.
- [ ] No personal data: no real Zotero keys, local paths, note or paper content,
      ledgers, `.env` files, API keys or copyrighted PDFs. Test fixtures must be
      synthetic or redistributable (see `benchmarks/corpus/SOURCES.md`).
- [ ] Index or embedding contract changes explain the migration path for
      existing generations (see [docs/INDEX_GENERATIONS.md](docs/INDEX_GENERATIONS.md)).

CI runs the suite on Windows, macOS and Linux with Python 3.10, 3.11 and 3.12,
plus a fresh-install smoke test of the setup scripts.

## Commit messages

Use a short imperative subject with a type prefix, matching the existing
history: `feat:`, `fix:`, `docs:`, `test:`, `refactor:` or `chore:`.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).

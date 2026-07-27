# Tests

The test suite covers scanner contracts, backend dispatch, subagent resume,
hash/ledger compatibility, Zotero SQLite fixtures, note rendering, Chroma schema
guards, setup diagnostics, MCP tool registration, and an isolated synthetic MCP
round trip. It also locks the Wave 0A benchmark contracts: pure legacy PDF
extraction/chunking, fail-closed run isolation, and allowlist-only public hits.
Cloud LLM calls are not made by the default suite.

## Run the suite

```bash
python -m pip install -r requirements.txt -r requirements-test.txt
python -m pytest tests -q
```

Focused checks:

```bash
python -m pytest tests/test_entrypoint_smoke.py -q
python -m pytest tests/test_mcp_server.py -q
python -m pytest tests/test_subagent_host_contract.py -q
python -m pytest tests/test_pdf_baseline.py tests/test_benchmark_runtime.py tests/test_public_report.py -q
```

The GitHub Actions matrix runs the suite on Windows, macOS, and Linux with
Python 3.10, 3.11, and 3.12. Provider SDKs are optional and imported lazily;
tests do not spend cloud quota or require a running Ollama daemon.

For a user-facing retrieval check after setup, run `python scripts/demo.py`.
It creates a temporary synthetic corpus, exercises the stdio MCP launcher and
tools, then removes its state.

"""Frozen-context answer diagnostics for a local Ollama chat model.
This module deliberately records model output without judging scientific
correctness.  It is a small, standalone boundary so a frozen diagnostic bundle
can be run after the retrieval and canonical-evidence work has finished.
"""
from __future__ import annotations
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence
BUNDLE_SCHEMA = "answer-context-diagnostic-bundle-v1"
CONTEXT_BUDGET_CODEPOINTS = 8000
_MODES = ("retrieved", "oracle")
_REQUIRED_MODEL_FIELDS = frozenset(
    {
        "model",
        "model_digest",
        "prompt",
        "think",
        "temperature",
        "seed",
        "num_ctx",
        "num_predict",
        "endpoint",
    }
)
class AnswerDiagnosticError(ValueError):
    """Raised when an immutable diagnostic input or live response is invalid."""
class AnswerDiagnosticTransportError(RuntimeError):
    """Raised for one transport failure.  Calls are never retried here."""
class UrllibJsonTransport:
    """The only live transport used by the command-line runner."""
    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        raw_payload = None if payload is None else _canonical_json_bytes(payload)
        request = urllib.request.Request(
            url,
            data=raw_payload,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise AnswerDiagnosticTransportError(f"{method} {url} failed: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnswerDiagnosticError(f"{method} {url} returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise AnswerDiagnosticError(f"{method} {url} must return a JSON object")
        return decoded
def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
def _resolve_bundle_path(bundle_path: Path, raw_path: object, field: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise AnswerDiagnosticError(f"{field} must be a non-empty path string")
    path = Path(raw_path)
    return (path if path.is_absolute() else bundle_path.parent / path).resolve(strict=False)
def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise AnswerDiagnosticError(f"{field} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise AnswerDiagnosticError(f"{field} must be a SHA-256 hex string") from exc
    return value.lower()
def load_and_validate_bundle(
    bundle_path: str | Path,
    *,
    expected_bundle_sha256: str | None = None,
) -> tuple[dict[str, object], str]:
    """Read a bundle and prove every declared immutable input before a run."""
    resolved = Path(bundle_path).resolve(strict=True)
    raw = resolved.read_bytes()
    actual_bundle_sha = _sha256_bytes(raw)
    if expected_bundle_sha256 is not None and actual_bundle_sha != _require_sha256(
        expected_bundle_sha256, "--bundle-sha256"
    ):
        raise AnswerDiagnosticError("bundle SHA-256 does not match --bundle-sha256")
    try:
        bundle = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnswerDiagnosticError("bundle is not valid UTF-8 JSON") from exc
    if not isinstance(bundle, dict) or bundle.get("schema") != BUNDLE_SCHEMA:
        raise AnswerDiagnosticError(f"bundle schema must be {BUNDLE_SCHEMA!r}")
    protocol = _resolve_bundle_path(resolved, bundle.get("protocol_path"), "protocol_path")
    if not protocol.is_file():
        raise AnswerDiagnosticError(f"protocol_path does not exist: {protocol}")
    if sha256_path(protocol) != _require_sha256(bundle.get("protocol_sha256"), "protocol_sha256"):
        raise AnswerDiagnosticError("protocol SHA-256 mismatch")
    try:
        protocol_payload = json.loads(protocol.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnswerDiagnosticError("protocol_path must contain JSON") from exc
    bindings = bundle.get("source_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise AnswerDiagnosticError("source_bindings must be a non-empty array")
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise AnswerDiagnosticError(f"source_bindings[{index}] must be an object")
        source_path = _resolve_bundle_path(resolved, binding.get("path"), f"source_bindings[{index}].path")
        if not source_path.is_file():
            raise AnswerDiagnosticError(f"bound source does not exist: {source_path}")
        expected = _require_sha256(binding.get("sha256"), f"source_bindings[{index}].sha256")
        if sha256_path(source_path) != expected:
            raise AnswerDiagnosticError(f"bound source SHA-256 mismatch: {source_path}")
    _validate_model(bundle.get("model"))
    questions = _validate_questions(bundle.get("questions"))
    _validate_protocol_question_binding(protocol_payload, questions)
    return bundle, actual_bundle_sha
def _validate_model(raw_model: object) -> Mapping[str, object]:
    if not isinstance(raw_model, dict) or not _REQUIRED_MODEL_FIELDS.issubset(raw_model):
        raise AnswerDiagnosticError("model must contain the required answer-client v4 fields")
    if not isinstance(raw_model["model"], str) or not raw_model["model"]:
        raise AnswerDiagnosticError("model.model must be non-empty")
    _require_sha256(raw_model["model_digest"], "model.model_digest")
    if not isinstance(raw_model["prompt"], str):
        raise AnswerDiagnosticError("model.prompt must be a string")
    _model_endpoint_base(raw_model["endpoint"])
    return raw_model
def _validate_questions(raw_questions: object) -> Sequence[Mapping[str, object]]:
    if not isinstance(raw_questions, list) or not raw_questions:
        raise AnswerDiagnosticError("questions must be a non-empty array")
    seen: set[str] = set()
    for question_index, question in enumerate(raw_questions):
        if not isinstance(question, dict):
            raise AnswerDiagnosticError(f"questions[{question_index}] must be an object")
        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not question_id or question_id in seen:
            raise AnswerDiagnosticError("question_id values must be non-empty and unique")
        seen.add(question_id)
        if not isinstance(question.get("question"), str) or not question["question"]:
            raise AnswerDiagnosticError(f"question {question_id} has no question text")
        contexts = question.get("contexts")
        if not isinstance(contexts, dict) or set(contexts) != set(_MODES):
            raise AnswerDiagnosticError(f"question {question_id} must contain retrieved and oracle contexts")
        for mode in _MODES:
            entries = contexts[mode]
            if not isinstance(entries, list) or not entries:
                raise AnswerDiagnosticError(f"question {question_id} {mode} context must be non-empty")
            expected_citations = [f"E{number}" for number in range(1, len(entries) + 1)]
            citations: list[str] = []
            codepoints = 0
            for entry_index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise AnswerDiagnosticError(f"question {question_id} {mode}[{entry_index}] must be an object")
                citation = entry.get("citation")
                content = entry.get("content")
                spans = entry.get("source_spans")
                if not isinstance(citation, str) or not isinstance(content, str):
                    raise AnswerDiagnosticError(f"question {question_id} {mode}[{entry_index}] needs citation and content")
                if not isinstance(spans, list) or not spans:
                    raise AnswerDiagnosticError(f"question {question_id} {mode}[{entry_index}] needs source_spans")
                citations.append(citation)
                codepoints += len(content)
            if citations != expected_citations:
                raise AnswerDiagnosticError(f"question {question_id} {mode} citations must be ordered E1..En")
            if codepoints > CONTEXT_BUDGET_CODEPOINTS:
                raise AnswerDiagnosticError(f"question {question_id} {mode} exceeds {CONTEXT_BUDGET_CODEPOINTS} codepoints")
    return raw_questions
def _validate_protocol_question_binding(
    protocol: object,
    bundle_questions: Sequence[Mapping[str, object]],
) -> None:
    """Bind the bundle to the frozen rubric without ever prompting from it."""
    if not isinstance(protocol, dict):
        raise AnswerDiagnosticError("protocol_path must contain a JSON object")
    protocol_questions = protocol.get("questions")
    if not isinstance(protocol_questions, list):
        raise AnswerDiagnosticError("protocol questions must be an array")
    expected = [(item.get("question_id"), item.get("question")) for item in protocol_questions if isinstance(item, dict)]
    actual = [(item["question_id"], item["question"]) for item in bundle_questions]
    if len(expected) != len(protocol_questions) or actual != expected:
        raise AnswerDiagnosticError("bundle question IDs, order, or text differ from frozen protocol")
    design = protocol.get("diagnostic_design")
    if not isinstance(design, dict) or design.get("variants") != list(_MODES):
        raise AnswerDiagnosticError("protocol must require retrieved and oracle variants")
    budget = design.get("budget")
    if not isinstance(budget, dict) or budget.get("limit") != CONTEXT_BUDGET_CODEPOINTS:
        raise AnswerDiagnosticError("protocol context budget differs from runner contract")
    if design.get("new_generation_count") != len(bundle_questions) * len(_MODES):
        raise AnswerDiagnosticError("protocol generation count differs from paired bundle")
def build_requests(bundle: Mapping[str, object], *, model_name: str | None = None) -> tuple[dict[str, object], ...]:
    """Build paired prompts.  The only per-mode difference is the contexts."""
    model = _validate_model(bundle.get("model"))
    questions = _validate_questions(bundle.get("questions"))
    selected_model = model_name or str(model["model"])
    requests: list[dict[str, object]] = []
    for question in questions:
        contexts = question["contexts"]
        for mode in _MODES:
            entries = contexts[mode]
            evidence = "\n\n".join(f"[{entry['citation']}]\n{entry['content']}" for entry in entries)
            user_prompt = f"QUESTION:\n{question['question']}\n\nEVIDENCE:\n{evidence}"
            payload = {
                "model": selected_model,
                "messages": [
                    {"role": "system", "content": model["prompt"]},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "think": model["think"],
                "options": {
                    "temperature": model["temperature"],
                    "seed": model["seed"],
                    "num_ctx": model["num_ctx"],
                    "num_predict": model["num_predict"],
                },
            }
            requests.append({"question_id": str(question["question_id"]), "mode": mode, "variant": mode,
                "diagnostic_only": True, "not_holdout": True, "scientific_adjudication": "not_run",
                "citation_count": len(entries), "context_codepoints": sum(len(entry["content"]) for entry in entries),
                "context_entries": entries, "user_prompt": user_prompt, "request_payload": payload,
                "request_sha256": _sha256_bytes(_canonical_json_bytes(payload))})
    return tuple(requests)
def _validated_loopback_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AnswerDiagnosticError("Ollama endpoint must use a loopback host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise AnswerDiagnosticError("Ollama base URL must not contain credentials, path, query, or fragment")
    return base_url.rstrip("/")
def _model_endpoint_base(endpoint: object) -> str | None:
    if endpoint == "/api/chat":
        return None
    if not isinstance(endpoint, str):
        raise AnswerDiagnosticError("model.endpoint must be /api/chat or a loopback /api/chat URL")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/api/chat":
        raise AnswerDiagnosticError("model.endpoint must use a loopback /api/chat URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AnswerDiagnosticError("model.endpoint must not contain credentials, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"
def _model_digest(transport: Any, base_url: str, model_name: str, timeout_seconds: float) -> str:
    response = transport.request_json("GET", base_url + "/api/tags", payload=None, timeout_seconds=timeout_seconds)
    records = response.get("models")
    if not isinstance(records, list):
        raise AnswerDiagnosticError("Ollama /api/tags response is missing models[]")
    matches = [record for record in records if isinstance(record, Mapping) and (record.get("name") == model_name or record.get("model") == model_name)]
    digests = {record.get("digest") for record in matches}
    if len(digests) != 1 or not isinstance(next(iter(digests), None), str):
        raise AnswerDiagnosticError(f"cannot prove a unique digest for model {model_name!r}")
    return str(next(iter(digests)))
def _ollama_version(transport: Any, base_url: str, timeout_seconds: float) -> dict[str, object]:
    try:
        raw = dict(transport.request_json("GET", base_url + "/api/version", payload=None, timeout_seconds=timeout_seconds))
    except Exception as exc:
        return {"value": "unknown", "failure": f"{type(exc).__name__}: {exc}"}
    return {"value": raw.get("version") if isinstance(raw.get("version"), str) else "unknown", "raw_response": raw}
def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json_bytes(value))
def _write_jsonl(path: Path, values: Sequence[Mapping[str, object]]) -> None:
    path.write_bytes(b"".join(_canonical_json_bytes(value) for value in values))
def run_answer_diagnostics(
    bundle_path: str | Path,
    output_dir: str | Path,
    *,
    expected_bundle_sha256: str | None = None,
    execute: bool = False,
    base_url: str | None = None,
    timeout_seconds: float = 120.0,
    transport: Any = None,
) -> Path:
    """Create a new diagnostic directory; call Ollama only with ``execute``."""
    bundle_file = Path(bundle_path).resolve(strict=True)
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise AnswerDiagnosticError(f"output directory already exists: {output}")
    if timeout_seconds <= 0:
        raise AnswerDiagnosticError("timeout_seconds must be greater than zero")
    bundle, bundle_sha = load_and_validate_bundle(bundle_file, expected_bundle_sha256=expected_bundle_sha256)
    requests = build_requests(bundle)
    output.mkdir(parents=True, exist_ok=False)
    _write_jsonl(output / "requests.jsonl", requests)
    manifest: dict[str, object] = {
        "schema": "answer-context-diagnostic-run-v1",
        "bundle_path": str(bundle_file),
        "bundle_sha256": bundle_sha,
        "execute": execute,
        "context_budget_codepoints": CONTEXT_BUDGET_CODEPOINTS,
        "request_count": len(requests),
        "diagnostic_only": True,
        "not_holdout": True,
        "scientific_adjudication": "not_run",
        "oracle_source_selection_target_knowledge": True,
        "status": "dry-run" if not execute else "running",
    }
    _write_json(output / "run-manifest.json", manifest)
    if not execute:
        return output
    active_transport = transport or UrllibJsonTransport()
    model = _validate_model(bundle["model"])
    checked_base_url = _model_endpoint_base(model["endpoint"]) or _validated_loopback_base_url(base_url or "http://127.0.0.1:11434")
    source_model = str(model["model"])
    expected_digest = _require_sha256(model["model_digest"], "model.model_digest")
    alias = f"research-rag-answer-{uuid.uuid4().hex}:diagnostic"
    responses: list[dict[str, object]] = []
    alias_receipt: dict[str, object] | None = None
    created_alias = False
    copy_started = False
    identity_halt: str | None = None
    try:
        manifest["ollama_version"] = _ollama_version(active_transport, checked_base_url, timeout_seconds)
        source_digest_before = _model_digest(active_transport, checked_base_url, source_model, timeout_seconds)
        if source_digest_before != expected_digest:
            raise AnswerDiagnosticError("source model digest differs from frozen bundle")
        manifest.update({"source_model": source_model, "source_digest_before": source_digest_before,
            "alias": alias, "alias_ownership": "unknown", "copy_attempted": True})
        copy_started = True
        _write_json(output / "run-manifest.json", manifest)
        alias_receipt = dict(active_transport.request_json("POST", checked_base_url + "/api/copy", payload={"source": source_model, "destination": alias}, timeout_seconds=timeout_seconds))
        created_alias = True
        manifest.update({"alias_ownership": "owned", "alias_receipt": alias_receipt})
        alias_digest = _model_digest(active_transport, checked_base_url, alias, timeout_seconds)
        if alias_digest != expected_digest:
            raise AnswerDiagnosticError("copied alias digest differs from frozen source digest")
        manifest.update({"alias_digest": alias_digest})
        _write_json(output / "run-manifest.json", manifest)
        for request in build_requests(bundle, model_name=alias):
            started = time.monotonic()
            record = dict(request)
            if identity_halt:
                record.update({"status": "not_run", "failure_class": "identity_failure", "failure": identity_halt})
                responses.append(record)
                _write_jsonl(output / "responses.jsonl", responses)
                continue
            try:
                raw_response = dict(active_transport.request_json("POST", checked_base_url + "/api/chat", payload=request["request_payload"], timeout_seconds=timeout_seconds))
                record.update({"raw_response": raw_response, "client_elapsed_seconds": time.monotonic() - started})
                message = raw_response.get("message")
                failures: list[tuple[str, str]] = []
                if raw_response.get("model") != alias:
                    failures.append(("identity_failure", "response model does not match run alias"))
                if raw_response.get("done") is not True:
                    failures.append(("client_output_incomplete", "response is not complete"))
                if raw_response.get("done_reason") == "length":
                    failures.append(("generation_budget_failure", "response reached generation budget"))
                elif raw_response.get("done_reason") != "stop":
                    failures.append(("client_output_incomplete", "response done_reason is not stop"))
                if not isinstance(message, Mapping) or not isinstance(message.get("content"), str) or not message.get("content"):
                    failures.append(("client_output_incomplete", "response has empty assistant message content"))
                if not any(kind == "identity_failure" for kind, _ in failures):
                    try:
                        current_alias_digest = _model_digest(active_transport, checked_base_url, alias, timeout_seconds)
                        record["alias_digest"] = current_alias_digest
                        if current_alias_digest != expected_digest:
                            failures.append(("identity_failure", "alias digest changed during run"))
                    except Exception as exc:
                        kind = "transport_failure" if isinstance(exc, AnswerDiagnosticTransportError) else "identity_failure"
                        failures.append((kind, f"alias digest check failed: {type(exc).__name__}: {exc}"))
                record["status"] = "completed" if not failures else "failed"
                if failures:
                    priority = ("identity_failure", "transport_failure", "generation_budget_failure", "client_output_incomplete")
                    record["failure_class"] = next(kind for kind in priority if any(found == kind for found, _ in failures))
                    record["failure"] = "; ".join(message for _, message in failures)
                    if record["failure_class"] == "identity_failure":
                        identity_halt = str(record["failure"])
            except Exception as exc:
                kind = "transport_failure" if isinstance(exc, AnswerDiagnosticTransportError) else "client_output_incomplete"
                record.update({"status": "failed", "failure_class": kind, "failure": f"{type(exc).__name__}: {exc}", "client_elapsed_seconds": time.monotonic() - started})
            responses.append(record)
            _write_jsonl(output / "responses.jsonl", responses)
        if identity_halt:
            manifest["source_digest_after"] = "not_checked_after_identity_failure"
        else:
            source_digest_after = _model_digest(active_transport, checked_base_url, source_model, timeout_seconds)
            manifest["source_digest_after"] = source_digest_after
            if source_digest_after != expected_digest:
                manifest["source_digest_failure"] = "source model digest changed during run"
        manifest["status"] = "completed" if all(row["status"] == "completed" for row in responses) and manifest["source_digest_after"] == expected_digest else "failed"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        failure_class = "identity_failure" if "digest" in str(exc).lower() else ("transport_failure" if isinstance(exc, AnswerDiagnosticTransportError) else "client_output_incomplete")
        manifest.update({"status": "failed", "failure_class": failure_class, "failure": failure})
        if failure_class == "identity_failure" and not responses:
            responses = [dict(request, status="not_run", failure_class="identity_failure", failure=failure) for request in build_requests(bundle, model_name=alias)]
            _write_jsonl(output / "responses.jsonl", responses)
    finally:
        if created_alias:
            try:
                cleanup_receipt = active_transport.request_json("DELETE", checked_base_url + "/api/delete", payload={"model": alias}, timeout_seconds=timeout_seconds)
                manifest["alias_cleanup"] = {"attempted": True, "model": alias, "receipt": dict(cleanup_receipt)}
            except Exception as exc:
                manifest["alias_cleanup"] = {"attempted": True, "model": alias, "failure": f"{type(exc).__name__}: {exc}"}
                manifest["status"] = "failed"
        else:
            manifest["alias_cleanup"] = {"attempted": False, "reason": "copy not confirmed" if copy_started else "copy not started"}
        _write_json(output / "run-manifest.json", manifest)
    return output

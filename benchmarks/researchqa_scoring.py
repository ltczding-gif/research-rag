"""Deterministic ResearchQA evidence mapping and retrieval scoring.

ResearchQA reference groups are conjunctive across groups (AND) and
disjunctive within one group's alternatives (OR).  This module keeps that
contract explicit and deliberately knows nothing about PDF parsing, note
generation, chunking, or retrieval implementations.  Those layers only need
to provide stable retrieved item IDs and an alternative-to-item mapping.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_BOOTSTRAP_SEED = "research-rag-rq2-bootstrap-v1"
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
PRIMARY_TIE_THRESHOLD = 0.005


class EvidenceContractError(ValueError):
    """Raised when evidence groups or score inputs violate the contract."""


class EvidenceCoverageError(EvidenceContractError):
    """Raised when evidence mapping does not pass the configured gate."""


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise EvidenceContractError("mapped item IDs must be non-empty strings")
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


@dataclass(frozen=True)
class EvidenceAlternative:
    """One OR alternative and the stable retrieval items it maps to."""

    alternative_id: str
    reference_text: str
    mapped_item_ids: tuple[str, ...]
    match_method: str = "unmapped"
    match_score: float | None = None

    @property
    def mapped(self) -> bool:
        return bool(self.mapped_item_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alternative_id": self.alternative_id,
            "reference_text": self.reference_text,
            "mapped_item_ids": list(self.mapped_item_ids),
            "match_method": self.match_method,
            "match_score": self.match_score,
        }


@dataclass(frozen=True)
class EvidenceGroup:
    """A required group; at least one alternative must be mapped and retrieved."""

    group_id: str
    alternatives: tuple[EvidenceAlternative, ...]

    @property
    def mapped_item_ids(self) -> frozenset[str]:
        return frozenset(
            item_id
            for alternative in self.alternatives
            for item_id in alternative.mapped_item_ids
        )

    @property
    def mapped(self) -> bool:
        return bool(self.mapped_item_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "alternatives": [
                alternative.to_dict() for alternative in self.alternatives
            ],
            "mapped": self.mapped,
        }


@dataclass(frozen=True)
class EvidenceMapping:
    """Mapped evidence groups for one ResearchQA question."""

    row_id: str
    paper_id: str
    domain: str
    question_type: str
    groups: tuple[EvidenceGroup, ...]

    @property
    def total_groups(self) -> int:
        return len(self.groups)

    @property
    def mapped_groups(self) -> int:
        return sum(group.mapped for group in self.groups)

    @property
    def coverage(self) -> float | None:
        if not self.groups:
            return None
        return self.mapped_groups / self.total_groups

    @property
    def evaluable_groups(self) -> tuple[EvidenceGroup, ...]:
        return tuple(group for group in self.groups if group.mapped)

    @property
    def unmapped_group_ids(self) -> tuple[str, ...]:
        return tuple(group.group_id for group in self.groups if not group.mapped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "row_id": self.row_id,
            "paper_id": self.paper_id,
            "domain": self.domain,
            "question_type": self.question_type,
            "groups": [group.to_dict() for group in self.groups],
            "total_groups": self.total_groups,
            "mapped_groups": self.mapped_groups,
            "coverage": self.coverage,
            "unmapped_group_ids": list(self.unmapped_group_ids),
        }


AlternativeMapper = Callable[
    [str],
    str
    | Iterable[str]
    | Mapping[str, Any]
    | EvidenceAlternative
    | None,
]


def _coerce_alternative_mapping(
    *,
    alternative_id: str,
    reference_text: str,
    raw: str | Iterable[str] | Mapping[str, Any] | EvidenceAlternative | None,
) -> EvidenceAlternative:
    if isinstance(raw, EvidenceAlternative):
        if raw.reference_text != reference_text:
            raise EvidenceContractError(
                f"{alternative_id}: mapper changed the reference text"
            )
        return raw
    if raw is None:
        return EvidenceAlternative(alternative_id, reference_text, ())
    if isinstance(raw, str):
        return EvidenceAlternative(
            alternative_id,
            reference_text,
            (raw,),
            match_method="mapped",
        )
    if isinstance(raw, Mapping):
        item_ids = raw.get("mapped_item_ids", raw.get("item_ids", ()))
        if isinstance(item_ids, str):
            item_ids = (item_ids,)
        if not isinstance(item_ids, Iterable):
            raise EvidenceContractError(
                f"{alternative_id}: mapped_item_ids must be iterable"
            )
        score = raw.get("match_score")
        if score is not None and not isinstance(score, (int, float)):
            raise EvidenceContractError(
                f"{alternative_id}: match_score must be numeric or null"
            )
        stable_ids = _stable_unique(item_ids)
        method = raw.get("match_method", "mapped" if stable_ids else "unmapped")
        if not isinstance(method, str) or not method:
            raise EvidenceContractError(
                f"{alternative_id}: match_method must be a non-empty string"
            )
        return EvidenceAlternative(
            alternative_id,
            reference_text,
            stable_ids,
            match_method=method,
            match_score=float(score) if score is not None else None,
        )
    return EvidenceAlternative(
        alternative_id,
        reference_text,
        _stable_unique(raw),
        match_method="mapped",
    )


def map_reference_groups(
    *,
    row_id: str,
    paper_id: str,
    domain: str,
    question_type: str,
    reference_groups: Sequence[Mapping[str, Any]],
    mapper: AlternativeMapper,
) -> EvidenceMapping:
    """Map ResearchQA reference groups without collapsing AND/OR semantics.

    ``reference_groups`` is the upstream ``expected_references`` shape:
    every sequence element is a required group and every string under its
    ``alternatives`` key is an OR-equivalent reference.  The callback may
    return one stable item ID, multiple IDs, mapping metadata, or ``None``.
    """

    if not all(
        isinstance(value, str) and value
        for value in (row_id, paper_id, domain, question_type)
    ):
        raise EvidenceContractError(
            "row_id, paper_id, domain, and question_type must be non-empty strings"
        )
    groups: list[EvidenceGroup] = []
    for group_index, raw_group in enumerate(reference_groups):
        if not isinstance(raw_group, Mapping):
            raise EvidenceContractError(
                f"{row_id}: reference group {group_index} must be an object"
            )
        raw_alternatives = raw_group.get("alternatives")
        if (
            not isinstance(raw_alternatives, Sequence)
            or isinstance(raw_alternatives, (str, bytes))
            or not raw_alternatives
        ):
            raise EvidenceContractError(
                f"{row_id}: group {group_index} must contain alternatives"
            )
        group_id = raw_group.get("group_id") or _stable_id(
            "eg", row_id, group_index
        )
        if not isinstance(group_id, str) or not group_id:
            raise EvidenceContractError(
                f"{row_id}: group {group_index} has an invalid group_id"
            )

        alternatives: list[EvidenceAlternative] = []
        for alternative_index, reference_text in enumerate(raw_alternatives):
            if not isinstance(reference_text, str) or not reference_text.strip():
                raise EvidenceContractError(
                    f"{row_id}: group {group_index} alternative "
                    f"{alternative_index} must be non-empty text"
                )
            alternative_id = _stable_id(
                "ea", row_id, group_index, alternative_index
            )
            alternatives.append(
                _coerce_alternative_mapping(
                    alternative_id=alternative_id,
                    reference_text=reference_text,
                    raw=mapper(reference_text),
                )
            )
        groups.append(EvidenceGroup(group_id, tuple(alternatives)))
    if len({group.group_id for group in groups}) != len(groups):
        raise EvidenceContractError(f"{row_id}: evidence group IDs must be unique")
    return EvidenceMapping(
        row_id=row_id,
        paper_id=paper_id,
        domain=domain,
        question_type=question_type,
        groups=tuple(groups),
    )


@dataclass(frozen=True)
class MappingCoverage:
    total_groups: int
    mapped_groups: int
    overall: float
    per_paper: Mapping[str, float]
    passed: bool
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_groups": self.total_groups,
            "mapped_groups": self.mapped_groups,
            "overall": self.overall,
            "per_paper": dict(sorted(self.per_paper.items())),
            "passed": self.passed,
            "failures": list(self.failures),
        }


def evaluate_mapping_coverage(
    mappings: Sequence[EvidenceMapping],
    *,
    overall_minimum: float = 0.95,
    per_paper_minimum: float = 0.90,
) -> MappingCoverage:
    """Evaluate the overall 95% and per-paper 90% default mapping gates."""

    for label, threshold in (
        ("overall_minimum", overall_minimum),
        ("per_paper_minimum", per_paper_minimum),
    ):
        if not 0.0 <= threshold <= 1.0:
            raise EvidenceContractError(f"{label} must be in [0, 1]")

    totals: dict[str, int] = defaultdict(int)
    mapped: dict[str, int] = defaultdict(int)
    for item in mappings:
        totals[item.paper_id] += item.total_groups
        mapped[item.paper_id] += item.mapped_groups

    total_groups = sum(totals.values())
    mapped_groups = sum(mapped.values())
    overall = mapped_groups / total_groups if total_groups else 1.0
    per_paper = {
        paper_id: (
            mapped[paper_id] / paper_total if paper_total else 1.0
        )
        for paper_id, paper_total in sorted(totals.items())
    }
    failures: list[str] = []
    if overall < overall_minimum:
        failures.append(
            f"overall mapping coverage {overall:.6f} < {overall_minimum:.6f}"
        )
    for paper_id, coverage in per_paper.items():
        if coverage < per_paper_minimum:
            failures.append(
                f"paper {paper_id} mapping coverage "
                f"{coverage:.6f} < {per_paper_minimum:.6f}"
            )
    return MappingCoverage(
        total_groups=total_groups,
        mapped_groups=mapped_groups,
        overall=overall,
        per_paper=per_paper,
        passed=not failures,
        failures=tuple(failures),
    )


def enforce_mapping_coverage(
    mappings: Sequence[EvidenceMapping],
    *,
    overall_minimum: float = 0.95,
    per_paper_minimum: float = 0.90,
) -> MappingCoverage:
    coverage = evaluate_mapping_coverage(
        mappings,
        overall_minimum=overall_minimum,
        per_paper_minimum=per_paper_minimum,
    )
    if not coverage.passed:
        raise EvidenceCoverageError("; ".join(coverage.failures))
    return coverage


def _group_first_ranks(
    ranked_item_ids: Sequence[str],
    groups: Sequence[EvidenceGroup],
    *,
    cutoff: int | None = None,
) -> dict[str, int]:
    if cutoff is not None and cutoff < 0:
        raise EvidenceContractError("cutoff must be non-negative or null")
    target_to_groups: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        for item_id in group.mapped_item_ids:
            target_to_groups[item_id].add(group.group_id)

    first_ranks: dict[str, int] = {}
    for rank, item_id in enumerate(ranked_item_ids, 1):
        if cutoff is not None and rank > cutoff:
            break
        for group_id in target_to_groups.get(item_id, ()):
            first_ranks.setdefault(group_id, rank)
    return first_ranks


def evidence_group_recall_at_k(
    ranked_item_ids: Sequence[str],
    groups: Sequence[EvidenceGroup],
    k: int,
) -> float | None:
    """Return the fraction of required groups hit by at least one OR alternative."""

    if k < 1:
        raise EvidenceContractError("k must be at least 1")
    if not groups:
        return None
    return len(_group_first_ranks(ranked_item_ids, groups, cutoff=k)) / len(groups)


def reciprocal_rank(
    ranked_item_ids: Sequence[str],
    groups: Sequence[EvidenceGroup],
) -> float | None:
    """Return standard reciprocal rank of the first item covering any group."""

    if not groups:
        return None
    first_ranks = _group_first_ranks(ranked_item_ids, groups)
    if not first_ranks:
        return 0.0
    return 1.0 / min(first_ranks.values())


def coverage_ndcg_at_k(
    ranked_item_ids: Sequence[str],
    groups: Sequence[EvidenceGroup],
    k: int,
) -> float | None:
    """Discount each group's first coverage and normalize by ideal rank one.

    A passage that newly covers two groups receives gain two at that rank.
    Each group can contribute only once.  Dividing by the number of groups is
    the attainable ideal where all required evidence is available at rank one;
    this keeps the score in ``[0, 1]`` even when one passage covers many groups.
    """

    if k < 1:
        raise EvidenceContractError("k must be at least 1")
    if not groups:
        return None
    first_ranks = _group_first_ranks(ranked_item_ids, groups, cutoff=k)
    discounted_gain = sum(
        1.0 / math.log2(rank + 1) for rank in first_ranks.values()
    )
    return discounted_gain / len(groups)


def all_required_groups_success_at_k(
    ranked_item_ids: Sequence[str],
    groups: Sequence[EvidenceGroup],
    k: int,
) -> float | None:
    """Return one only when all AND groups are covered by rank ``k``."""

    recall = evidence_group_recall_at_k(ranked_item_ids, groups, k)
    if recall is None:
        return None
    return float(math.isclose(recall, 1.0))


@dataclass(frozen=True)
class RankingMetrics:
    evaluable: bool
    required_group_count: int
    metrics: Mapping[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluable": self.evaluable,
            "required_group_count": self.required_group_count,
            "metrics": dict(self.metrics),
        }


def score_ranking(
    ranked_item_ids: Sequence[str],
    groups: Sequence[EvidenceGroup],
) -> RankingMetrics:
    """Compute the fixed ResearchQA retrieval metric bundle for one question."""

    if len(set(ranked_item_ids)) != len(ranked_item_ids):
        raise EvidenceContractError("ranked item IDs must be unique")
    if not groups:
        return RankingMetrics(
            evaluable=False,
            required_group_count=0,
            metrics={
                "recall_at_5": None,
                "recall_at_10": None,
                "mrr": None,
                "coverage_ndcg_at_10": None,
                "all_required_groups_success_at_5": None,
                "all_required_groups_success_at_10": None,
                "groups_covered_at_5": None,
                "groups_covered_at_10": None,
            },
        )

    first_5 = _group_first_ranks(ranked_item_ids, groups, cutoff=5)
    first_10 = _group_first_ranks(ranked_item_ids, groups, cutoff=10)
    return RankingMetrics(
        evaluable=True,
        required_group_count=len(groups),
        metrics={
            "recall_at_5": len(first_5) / len(groups),
            "recall_at_10": len(first_10) / len(groups),
            "mrr": reciprocal_rank(ranked_item_ids, groups),
            "coverage_ndcg_at_10": coverage_ndcg_at_k(
                ranked_item_ids, groups, 10
            ),
            "all_required_groups_success_at_5": float(
                len(first_5) == len(groups)
            ),
            "all_required_groups_success_at_10": float(
                len(first_10) == len(groups)
            ),
            "groups_covered_at_5": float(len(first_5)),
            "groups_covered_at_10": float(len(first_10)),
        },
    )


@dataclass(frozen=True)
class QuestionScore:
    row_id: str
    paper_id: str
    domain: str
    question_type: str
    metrics: Mapping[str, float | None]


@dataclass(frozen=True)
class MacroAggregate:
    overall: Mapping[str, float | None]
    by_domain: Mapping[str, Mapping[str, float | None]]
    by_paper: Mapping[str, Mapping[str, float | None]]
    by_question_type: Mapping[str, Mapping[str, float | None]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": dict(self.overall),
            "by_domain": {
                key: dict(value) for key, value in self.by_domain.items()
            },
            "by_paper": {
                key: dict(value) for key, value in self.by_paper.items()
            },
            "by_question_type": {
                key: dict(value)
                for key, value in self.by_question_type.items()
            },
        }


def _mean_metric_sets(
    metric_sets: Sequence[Mapping[str, float | None]],
) -> dict[str, float | None]:
    metric_names = sorted(
        {name for metric_set in metric_sets for name in metric_set}
    )
    means: dict[str, float | None] = {}
    for metric_name in metric_names:
        values = [
            float(metric_set[metric_name])
            for metric_set in metric_sets
            if metric_set.get(metric_name) is not None
        ]
        means[metric_name] = fmean(values) if values else None
    return means


def macro_aggregate(scores: Sequence[QuestionScore]) -> MacroAggregate:
    """Aggregate question scores as question -> paper -> domain -> overall."""

    paper_rows: dict[str, list[QuestionScore]] = defaultdict(list)
    for score in scores:
        paper_rows[score.paper_id].append(score)

    paper_domains: dict[str, str] = {}
    by_paper: dict[str, dict[str, float | None]] = {}
    for paper_id, rows in sorted(paper_rows.items()):
        domains = {row.domain for row in rows}
        if len(domains) != 1:
            raise EvidenceContractError(
                f"paper {paper_id} appears in multiple domains: {sorted(domains)}"
            )
        paper_domains[paper_id] = next(iter(domains))
        by_paper[paper_id] = _mean_metric_sets([row.metrics for row in rows])

    domain_papers: dict[str, list[str]] = defaultdict(list)
    for paper_id, domain in paper_domains.items():
        domain_papers[domain].append(paper_id)
    by_domain = {
        domain: _mean_metric_sets(
            [by_paper[paper_id] for paper_id in sorted(paper_ids)]
        )
        for domain, paper_ids in sorted(domain_papers.items())
    }
    overall = _mean_metric_sets(list(by_domain.values()))

    question_type_rows: dict[str, list[QuestionScore]] = defaultdict(list)
    for score in scores:
        question_type_rows[score.question_type].append(score)
    by_question_type: dict[str, Mapping[str, float | None]] = {}
    for question_type, rows in sorted(question_type_rows.items()):
        # Reuse the same paper -> domain macro order within each question type.
        nested = macro_aggregate(rows) if len(question_type_rows) > 1 else None
        if nested is None:
            # Avoid unbounded recursion for a single-type input.
            type_papers: dict[str, list[Mapping[str, float | None]]] = defaultdict(list)
            type_domains: dict[str, str] = {}
            for row in rows:
                type_papers[row.paper_id].append(row.metrics)
                type_domains[row.paper_id] = row.domain
            type_paper_means = {
                paper_id: _mean_metric_sets(metric_sets)
                for paper_id, metric_sets in type_papers.items()
            }
            type_domain_means = {
                domain: _mean_metric_sets(
                    [
                        type_paper_means[paper_id]
                        for paper_id in sorted(type_paper_means)
                        if type_domains[paper_id] == domain
                    ]
                )
                for domain in sorted(set(type_domains.values()))
            }
            by_question_type[question_type] = _mean_metric_sets(
                list(type_domain_means.values())
            )
        else:
            by_question_type[question_type] = nested.overall

    return MacroAggregate(
        overall=overall,
        by_domain=by_domain,
        by_paper=by_paper,
        by_question_type=by_question_type,
    )


@dataclass(frozen=True)
class BootstrapResult:
    observed_delta: float
    lower: float
    upper: float
    confidence: float
    samples: int
    seed: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_delta": self.observed_delta,
            "confidence_interval": [self.lower, self.upper],
            "confidence": self.confidence,
            "samples": self.samples,
            "seed": self.seed,
        }


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise EvidenceContractError("cannot take a percentile of no values")
    if not 0.0 <= probability <= 1.0:
        raise EvidenceContractError("percentile probability must be in [0, 1]")
    position = probability * (len(sorted_values) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return (
        sorted_values[lower_index] * (1.0 - weight)
        + sorted_values[upper_index] * weight
    )


def paired_bootstrap(
    candidate_by_paper: Mapping[str, float],
    baseline_by_paper: Mapping[str, float],
    paper_domains: Mapping[str, str],
    *,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    confidence: float = 0.95,
    seed: str = DEFAULT_BOOTSTRAP_SEED,
) -> BootstrapResult:
    """Domain-stratified, paper-level deterministic paired bootstrap."""

    candidate_ids = set(candidate_by_paper)
    baseline_ids = set(baseline_by_paper)
    domain_ids = set(paper_domains)
    if candidate_ids != baseline_ids:
        raise EvidenceContractError(
            "candidate and baseline must contain the same paper IDs"
        )
    if candidate_ids != domain_ids:
        raise EvidenceContractError(
            "paper_domains must contain exactly the scored paper IDs"
        )
    if not candidate_ids:
        raise EvidenceContractError("paired bootstrap requires at least one paper")
    if samples < 1:
        raise EvidenceContractError("samples must be at least 1")
    if not 0.0 < confidence < 1.0:
        raise EvidenceContractError("confidence must be between 0 and 1")

    papers_by_domain: dict[str, list[str]] = defaultdict(list)
    for paper_id in sorted(candidate_ids):
        domain = paper_domains[paper_id]
        if not isinstance(domain, str) or not domain:
            raise EvidenceContractError(
                f"paper {paper_id} has an invalid domain"
            )
        papers_by_domain[domain].append(paper_id)
    domains = sorted(papers_by_domain)

    def domain_macro_delta(selected: Mapping[str, Sequence[str]]) -> float:
        domain_deltas = []
        for domain in domains:
            paper_ids = selected[domain]
            domain_deltas.append(
                fmean(
                    candidate_by_paper[paper_id] - baseline_by_paper[paper_id]
                    for paper_id in paper_ids
                )
            )
        return fmean(domain_deltas)

    observed_delta = domain_macro_delta(papers_by_domain)
    seed_bytes = hashlib.sha256(seed.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed_bytes, "big"))
    replicates: list[float] = []
    for _ in range(samples):
        selected = {
            domain: [
                rng.choice(papers_by_domain[domain])
                for _ in papers_by_domain[domain]
            ]
            for domain in domains
        }
        replicates.append(domain_macro_delta(selected))
    replicates.sort()
    alpha = (1.0 - confidence) / 2.0
    return BootstrapResult(
        observed_delta=observed_delta,
        lower=_percentile(replicates, alpha),
        upper=_percentile(replicates, 1.0 - alpha),
        confidence=confidence,
        samples=samples,
        seed=seed,
    )


@dataclass(frozen=True)
class CandidateSummary:
    config_id: str
    primary: float
    p95_latency_ms: float
    index_bytes: int
    chunk_count: int
    complete: bool = True
    guardrails_passed: bool = True
    latency_decisive: bool = False


def rank_candidates(
    candidates: Sequence[CandidateSummary],
    *,
    tie_threshold: float = PRIMARY_TIE_THRESHOLD,
) -> tuple[CandidateSummary, ...]:
    """Rank only complete candidates, applying the approved practical tie-break."""

    if tie_threshold < 0:
        raise EvidenceContractError("tie_threshold must be non-negative")
    eligible = [
        candidate
        for candidate in candidates
        if candidate.complete and candidate.guardrails_passed
    ]
    if not eligible:
        return ()
    eligible.sort(key=lambda candidate: (-candidate.primary, candidate.config_id))
    best_primary = eligible[0].primary
    tied = [
        candidate
        for candidate in eligible
        if (
            best_primary - candidate.primary < tie_threshold
            or math.isclose(
                best_primary - candidate.primary,
                tie_threshold,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        )
    ]
    tied_ids = {candidate.config_id for candidate in tied}
    use_latency = all(candidate.latency_decisive for candidate in tied)
    tied.sort(
        key=lambda candidate: (
            candidate.p95_latency_ms if use_latency else 0.0,
            candidate.index_bytes,
            candidate.chunk_count,
            candidate.config_id,
        )
    )
    remainder = [
        candidate for candidate in eligible if candidate.config_id not in tied_ids
    ]
    remainder.sort(key=lambda candidate: (-candidate.primary, candidate.config_id))
    return tuple(tied + remainder)


def canonical_fingerprint(value: Any) -> str:
    """Return a stable SHA-256 for JSON-compatible score/config payloads."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

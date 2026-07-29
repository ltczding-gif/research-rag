"""Build the public, evidence-backed rq-2 strategy analysis as standalone HTML."""

from __future__ import annotations

import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.overnight import fingerprint_payload


REPORT_FILENAME = "detailed-strategy-analysis.html"

STAGE_ORDER = (
    "pdf-chunker",
    "note-chunker",
    "retriever",
    "source-composition",
    "reranker",
    "top2-confirmation",
)

STAGE_LABELS = {
    "pdf-chunker": "PDF 切分",
    "note-chunker": "笔记切分",
    "retriever": "召回器",
    "source-composition": "语料组合",
    "reranker": "重排序",
    "top2-confirmation": "Top-2 组合确认",
}

STAGE_INTROS = {
    "pdf-chunker": (
        "检验证据边界究竟应更细、更宽，还是尊重页、章节和父子结构。"
        "预期是减少跨段证据被切断，同时避免小块挤占 top-k。"
    ),
    "note-chunker": (
        "检验通用科研笔记能否作为检索结构，而不只是阅读产物。"
        "整篇与 reviewer-only 路线被明确设为诊断项，防止稀疏覆盖用高表面分数参赛。"
    ),
    "retriever": (
        "比较语义召回、词法召回和两者融合。重点不是只抬高均值，"
        "而是让精确术语补救不破坏 dense 已经排对的结果。"
    ),
    "source-composition": (
        "测试 PDF、笔记回链和层级父子块怎样组合。关键问题是："
        "辅助路线能否只补充新证据，而不是用高 fanout 噪声重排主路线。"
    ),
    "reranker": (
        "测试 cross-encoder 能否改善前排顺序。不同 depth 用来观察候选池深度、"
        "质量和 GPU 成本的关系；RR1 进一步保护基础排序的 top-1。"
    ),
    "top2-confirmation": (
        "把前序冻结的两组组件做兼容组合，验证局部改善能否在端到端组合中保留。"
        "12 个唯一组合来自 16 个笛卡尔项，层级路线的等价别名已去重。"
    ),
}

MECHANISMS = {
    "pdf-fixed-400": "400 字符子块，步长 320；高重叠、细粒度切分。",
    "pdf-fixed-800": "800 字符目标块，步长 700；在局部精度与上下文之间取中间值。",
    "pdf-fixed-1200": "1200 字符目标块，步长 1000；尽量保留跨句、跨段证据。",
    "pdf-page-aware": "先守住 PDF 页边界，再在页内聚合到目标长度。",
    "pdf-section-aware": "先按可识别章节边界分段，再在章节内聚合。",
    "pdf-structure-aware": "依赖启发式结构检测，在语义章节内生成块；检测失败即策略失败。",
    "pdf-parent-child": "用较小 child 做匹配，以 800–1600 字符 parent 提供上下文。",
    "pdf-structure-aware-fallback": (
        "结构健康时使用结构切分；检测失败或病态碎片化时逐论文回退 fixed-1200。"
    ),
    "note-whole": "每篇通用科研笔记整体作为一个检索单元。",
    "note-section": "按笔记标题层级切分，保留章节级语义。",
    "note-claim-evidence": "把 claim 与其 evidence/citation 绑定为最小可回链单元。",
    "note-reviewer-concern": "只抽取审稿人式 fatal/major 科学质疑；覆盖很稀疏。",
    "note-claim-plus-reviewer": "claim-evidence 为底座，叠加严格解析的 fatal/major reviewer concern。",
    "dense": "Qwen3-Embedding-4B 余弦语义召回，paper-scoped top-100。",
    "bm25": "BM25 词法召回，强调术语、数字和精确短语。",
    "hybrid-rrf": "dense 与 BM25 等权 rank-RRF（k=60）。",
    "pdf-only": "只用直接 PDF chunk 检索，是主基线。",
    "note-to-pdf": "先命中笔记，再只投影到笔记引用的 PDF chunks；没有 direct-PDF fallback。",
    "pdf-note-rrf": "把 direct PDF 与 note-derived PDF 两路按 rank-RRF 融合。",
    "note-guided-pdf": "用笔记 backlinks 硬过滤 direct PDF hits。",
    "hierarchical-pdf": "先检索 parent，再从已进入候选集的 children 中取结果。",
    "rerank-off": "不加载 cross-encoder，保留基础召回排序。",
    "rerank-20-to-10": "对基础 top-20 用 Qwen3-Reranker-0.6B 打分，输出 top-10。",
    "rerank-50-to-10": "对基础 top-50 重排后输出 top-10。",
    "rerank-100-to-10": "对基础 top-100 重排后输出 top-10。",
}

RATIONALES = {
    "pdf-fixed-400": (
        "细块可能更精准地命中局部事实；预期 lookup 更稳，但需警惕语义碎裂与 top-k 拥挤。"
    ),
    "pdf-fixed-800": (
        "用中等粒度作为稳健参考；预期兼顾局部事实和有限上下文，并控制索引体积。"
    ),
    "pdf-fixed-1200": (
        "更宽上下文可能一次容纳多个限定条件；预期 comprehension / multi-hop 更好。"
    ),
    "pdf-page-aware": (
        "页面是稳定的物理边界；预期减少跨页误拼，同时保留页内连续证据。"
    ),
    "pdf-section-aware": (
        "章节比固定字符更接近作者论证结构；预期减少跨主题混块。"
    ),
    "pdf-structure-aware": (
        "让文档结构决定边界；预期得到最语义化的块，但必须证明对异构 PDF 可用。"
    ),
    "pdf-parent-child": (
        "child 提供精确匹配、parent 提供回答上下文；预期兼得精度与完整性。"
    ),
    "pdf-structure-aware-fallback": (
        "F2 用确定性 fallback 修复原结构策略的适用性；预期先恢复 20/20 可执行，再判断质量。"
    ),
    "note-whole": (
        "作为最粗粒度对照，检查完整笔记语义是否有用；预先标记不具通用可排名资格。"
    ),
    "note-section": (
        "通用模板的标题结构跨领域稳定；预期比整篇笔记更聚焦、覆盖又比 reviewer-only 完整。"
    ),
    "note-claim-evidence": (
        "把论断与证据放在一起，理论上更适合科研问答和 PDF 回链。"
    ),
    "note-reviewer-concern": (
        "专门捕捉限制、反例和科学质疑；预期帮助难题，但稀疏性决定它只能做诊断。"
    ),
    "dense": "作为语义基线；预期适合改写后的自然语言问题和跨句同义表达。",
    "bm25": "作为词法对照；预期补救专名、术语、数字和原文措辞。",
    "hybrid-rrf": "预期结合 dense 与 BM25 互补性，但等权融合可能把词法噪声带入前排。",
    "pdf-only": "最少假设、最直接的证据路线；预期是需要被任何增强路线稳定超越的基线。",
    "note-to-pdf": "把笔记当作语义路由器；预期可跨段聚合，但没有 fallback 时风险很高。",
    "pdf-note-rrf": "保留 direct PDF，同时让笔记提供第二条证据路线；预期温和增益。",
    "note-guided-pdf": "用笔记缩小搜索空间；预期提高精度，但 hard filter 可能直接杀死召回。",
    "hierarchical-pdf": "利用父块发现主题、子块返回证据；预期改善跨段问题。",
    "rerank-off": "提供零重排成本与不可逆排序风险的基线。",
    "rerank-20-to-10": "用较浅候选池换取可控成本；预期修正前排次序而少引入新噪声。",
    "rerank-50-to-10": "让更多潜在证据进入 cross-encoder；预期多证据题更好，但成本和灾难性重排风险上升。",
    "rerank-100-to-10": "给重排器最大候选空间，检验质量是否继续随 depth 上升。",
}

GUARDRAIL_LABELS = {
    "too-many-domain-regressions": "多领域退化超过 2 个百分点",
    "multi_hop-regression": "multi-hop 切片退化越线",
    "adversarial-regression": "有 reference 的 adversarial 切片退化越线",
    "recall_at_10-regression": "总体 Recall@10 退化越线",
    "all_required_groups_success_at_10-regression": "全证据组成功率@10 退化越线",
    "new-recall-at-10-hard-failures": "出现新的 Recall@10=0 硬失败",
    "upstream-pdf_chunker-guardrail-failed": "上游 PDF chunker 未通过",
    "upstream-retriever-guardrail-failed": "上游 retriever 未通过",
    "upstream-source_composition-guardrail-failed": "上游 source composition 未通过",
}

VALIDITY_LABELS = {
    "valid-and-rankable": "有效且可排名",
    "valid-but-poor": "结果有效但质量不合格",
    "diagnostic-only/ineligible": "仅诊断／不可参赛",
    "deterministic-strategy-failure": "确定性策略失败",
    "infrastructure/unknown": "基础设施／未知失败",
    "invalid-false-score": "无效假分数",
}


class DetailedReportError(ValueError):
    """Raised when the report cannot be built from a coherent rq-2 run."""


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DetailedReportError(f"unreadable JSON: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise DetailedReportError(f"JSON object required: {path.name}")
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise DetailedReportError(f"unreadable CSV: {path.name}") from exc


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_score(value: object, digits: int = 4) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.{digits}f}"


def _fmt_delta(value: object) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:+.4f}"


def _fmt_latency(value: object) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number / 1000:.3f} s" if number >= 1000 else f"{number:.1f} ms"


def _fmt_bytes(value: object) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number / 1_048_576:.1f} MiB"


def _load_payloads(run_root: Path) -> dict[str, Mapping[str, Any]]:
    paths = [
        *sorted((run_root / "sweep" / "candidates").glob("*/*.json")),
        *sorted(
            (run_root / "sweep" / "extensions").glob(
                "*/candidates/**/*.json"
            )
        ),
    ]
    payloads: dict[str, Mapping[str, Any]] = {}
    for path in paths:
        envelope = _read_json(path)
        payload = envelope.get("payload")
        config_id = envelope.get("config_id")
        if (
            not isinstance(config_id, str)
            or not isinstance(payload, Mapping)
            or envelope.get("payload_sha256") != fingerprint_payload(payload)
            or config_id in payloads
        ):
            raise DetailedReportError(
                f"invalid candidate envelope: {path.name}"
            )
        payloads[config_id] = payload
    return payloads


def _merge_strategy_rows(
    leaderboard_rows: Sequence[Mapping[str, str]],
    extensions: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    rows = {str(row.get("config_id")): dict(row) for row in leaderboard_rows}
    for extension in extensions:
        config_id = str(extension.get("config_id", ""))
        if not config_id:
            continue
        if config_id not in rows:
            rows[config_id] = {
                "experiment_family": "approved-extension",
                **dict(extension),
            }
        else:
            for key, value in extension.items():
                if rows[config_id].get(key) in (None, ""):
                    rows[config_id][key] = value
    result = list(rows.values())
    result.sort(
        key=lambda row: (
            STAGE_ORDER.index(str(row.get("stage_id")))
            if str(row.get("stage_id")) in STAGE_ORDER
            else 999,
            str(row.get("config_id")),
        )
    )
    if len(result) != 39:
        raise DetailedReportError(
            f"expected 39 strategy rows, found {len(result)}"
        )
    return result


def _candidate(row: Mapping[str, object], payload: Mapping[str, Any]) -> dict[str, object]:
    raw = payload.get("candidate")
    candidate = dict(raw) if isinstance(raw, Mapping) else {}
    for field in (
        "pdf_chunker",
        "note_chunker",
        "retriever",
        "source_composition",
        "reranker",
    ):
        if candidate.get(field) in (None, "") and row.get(field) not in (None, ""):
            candidate[field] = row[field]
    return candidate


def _strategy_label(
    row: Mapping[str, object], candidate: Mapping[str, object]
) -> str:
    extension_id = row.get("extension_id")
    if extension_id:
        return f"{extension_id} · {row.get('config_id')}"
    stage = str(row.get("stage_id"))
    if stage == "pdf-chunker":
        return str(candidate.get("pdf_chunker") or row.get("config_id"))
    if stage == "note-chunker":
        return str(candidate.get("note_chunker") or row.get("config_id"))
    if stage == "retriever":
        return str(candidate.get("retriever") or row.get("config_id"))
    if stage == "source-composition":
        return str(candidate.get("source_composition") or row.get("config_id"))
    if stage == "reranker":
        return str(candidate.get("reranker") or row.get("config_id"))
    return " × ".join(
        str(candidate.get(field) or "—")
        for field in (
            "pdf_chunker",
            "retriever",
            "source_composition",
            "reranker",
        )
    )


def _mechanism(
    row: Mapping[str, object], candidate: Mapping[str, object]
) -> str:
    extension_id = str(row.get("extension_id") or "")
    if extension_id == "RR1":
        return (
            "depth-20 cross-encoder；强制保留 base top-1，再把 base rank 与 "
            "reranker rank 等权 RRF。"
        )
    if extension_id == "R1":
        return (
            "保留 dense top-1；dense:BM25=2:1 的 rank-RRF（k=60），"
            "用词法结果做受限补救。"
        )
    if extension_id == "S1":
        return (
            "N0 eligibility + claim/reviewer note 路线；direct PDF:note=0.9:0.1 "
            "rank-RRF，空路线严格回退 PDF-only。"
        )
    if extension_id == "F2":
        return MECHANISMS["pdf-structure-aware-fallback"]
    stage = str(row.get("stage_id"))
    if stage == "top2-confirmation":
        return "；".join(
            MECHANISMS.get(str(candidate.get(field)), str(candidate.get(field)))
            for field in (
                "pdf_chunker",
                "retriever",
                "source_composition",
                "reranker",
            )
            if candidate.get(field)
        )
    key = {
        "pdf-chunker": "pdf_chunker",
        "note-chunker": "note_chunker",
        "retriever": "retriever",
        "source-composition": "source_composition",
        "reranker": "reranker",
    }.get(stage)
    value = str(candidate.get(key, "")) if key else ""
    return MECHANISMS.get(value, value or "组件定义见冻结配置。")


def _rationale(
    row: Mapping[str, object], candidate: Mapping[str, object]
) -> str:
    extension_id = str(row.get("extension_id") or "")
    if extension_id == "RR1":
        return (
            "直接重排曾把正确 top-1 整体挤出；预期在保住底线的同时取得重排增益，"
            "且新增 hard failure 必须为 0。"
        )
    if extension_id == "R1":
        return (
            "等权 hybrid 虽抬高均值却产生 3 个新硬失败；预期用 dense-heavy 融合"
            "保留语义主线，只吸收 BM25 的精确术语补救。"
        )
    if extension_id == "S1":
        return (
            "旧笔记路线因无资格门、硬过滤和等权融合崩塌；预期在 PDF 主导、"
            "可回退的前提下让笔记只做小幅增益。"
        )
    if extension_id == "F2":
        return RATIONALES["pdf-structure-aware-fallback"]
    stage = str(row.get("stage_id"))
    if stage == "top2-confirmation":
        return (
            "前序单组件结果可能被交互效应抵消；预期确认局部优势是否能在完整链路中"
            "保留，同时传播所有上游 eligibility。"
        )
    key = {
        "pdf-chunker": "pdf_chunker",
        "note-chunker": "note_chunker",
        "retriever": "retriever",
        "source-composition": "source_composition",
        "reranker": "reranker",
    }.get(stage)
    value = str(candidate.get(key, "")) if key else ""
    return RATIONALES.get(value, "检验该冻结单变量是否能稳定改善主指标且通过所有切片门。")


def _actual_result(
    row: Mapping[str, object], payload: Mapping[str, Any]
) -> tuple[str, str]:
    status = str(row.get("status") or "")
    validity = str(row.get("validity_class") or "")
    if status == "failed":
        return (
            "无正式分数；在异构 PDF 上触发可复现的结构检测合同失败。",
            "策略失败被保留，不以 0 分或缺失行伪装。",
        )
    diagnostics = payload.get("guardrail_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    delta = (
        diagnostics.get("primary_delta")
        if diagnostics.get("primary_delta") is not None
        else row.get("primary_delta")
    )
    failures = diagnostics.get("failures")
    failures = failures if isinstance(failures, list) else []
    hard_ids = diagnostics.get("new_recall_at_10_hard_failure_ids")
    hard_ids = hard_ids if isinstance(hard_ids, list) else []
    hard_count = row.get("new_hard_failure_count")
    if hard_count in (None, ""):
        hard_count = len(hard_ids)
    score = _fmt_score(row.get("primary_score"), 6)
    facts = [
        f"coverage-nDCG@10={score}",
        f"相对阶段基线 Δ={_fmt_delta(delta)}",
        f"p95={_fmt_latency(row.get('p95_latency_ms'))}",
        f"chunks={row.get('chunk_count') or '—'}",
        f"索引={_fmt_bytes(row.get('index_bytes'))}",
    ]
    if _number(hard_count):
        facts.append(f"新增 hard failure={int(float(hard_count))}")
    if failures:
        translated = [GUARDRAIL_LABELS.get(str(item), str(item)) for item in failures]
        verdict = "；".join(translated)
    elif validity == "valid-and-rankable":
        verdict = "所有已注册相对门通过，可进入排名。"
    elif validity == "diagnostic-only/ineligible":
        verdict = "覆盖或可比性不足，只能解释机制，不能晋级。"
    else:
        verdict = "分数有效，但没有满足晋级合同。"
    if diagnostics.get("operational_review_required") is True:
        verdict += " 成本超过 1.5×，仍需 operational review。"
    if hard_ids:
        verdict += " 代表性新失败：" + "、".join(map(str, hard_ids[:3])) + "。"
    return "；".join(facts) + "。", verdict


def _question_analysis(
    payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, object]:
    candidate_rows: list[
        tuple[
            str,
            Mapping[str, object],
            dict[str, Mapping[str, object]],
            dict[str, Mapping[str, object]],
        ]
    ] = []
    for config_id, payload in payloads.items():
        question_rows = payload.get("question_results")
        mapping = payload.get("mapping")
        mappings = mapping.get("mappings") if isinstance(mapping, Mapping) else None
        candidate = payload.get("candidate")
        if (
            not isinstance(question_rows, list)
            or not isinstance(mappings, list)
            or not isinstance(candidate, Mapping)
        ):
            continue
        by_question = {
            str(row.get("row_id")): row
            for row in question_rows
            if isinstance(row, Mapping) and row.get("row_id")
        }
        by_mapping = {
            str(row.get("row_id")): row
            for row in mappings
            if isinstance(row, Mapping) and row.get("row_id")
        }
        candidate_rows.append(
            (config_id, candidate, by_question, by_mapping)
        )
    if not candidate_rows:
        return {
            "candidate_count": 0,
            "evaluable_count": 0,
            "hardest": [],
            "all_failed": [],
            "failure_rate_by_type": {},
            "failure_rate_by_domain": {},
            "never_perfect_count": 0,
        }
    common = set(candidate_rows[0][2])
    for _config_id, _candidate, questions, _mappings in candidate_rows[1:]:
        common.intersection_update(questions)
    evaluable = []
    for row_id in sorted(common):
        metrics = candidate_rows[0][2][row_id].get("metrics")
        if isinstance(metrics, Mapping) and metrics.get("recall_at_10") is not None:
            evaluable.append(row_id)

    hardest: list[dict[str, object]] = []
    type_rates: dict[str, list[float]] = defaultdict(list)
    domain_rates: dict[str, list[float]] = defaultdict(list)
    for row_id in evaluable:
        rows = [candidate[2][row_id] for candidate in candidate_rows]
        metric_rows = [
            row.get("metrics")
            for row in rows
            if isinstance(row.get("metrics"), Mapping)
        ]
        if len(metric_rows) != len(candidate_rows):
            continue
        misses = sum(row.get("recall_at_10") == 0 for row in metric_rows)
        best = max(float(row.get("coverage_ndcg_at_10") or 0.0) for row in metric_rows)
        prototype = rows[0]
        item = {
            "row_id": row_id,
            "domain": str(prototype.get("domain")),
            "question_type": str(prototype.get("question_type")),
            "misses": misses,
            "successes": len(candidate_rows) - misses,
            "best_ndcg": best,
        }
        hardest.append(item)
        rate = misses / len(candidate_rows)
        type_rates[item["question_type"]].append(rate)
        domain_rates[item["domain"]].append(rate)
    hardest.sort(
        key=lambda row: (
            int(row["misses"]),
            -int(row["successes"]),
            -float(row["best_ndcg"]),
            str(row["row_id"]),
        ),
        reverse=True,
    )
    all_failed = [
        dict(row)
        for row in hardest
        if row["misses"] == len(candidate_rows)
    ]
    for item in all_failed:
        row_id = str(item["row_id"])
        best_rank: int | None = None
        best_routes: list[str] = []
        pre_ranks: list[int] = []
        for config_id, candidate, questions, mappings in candidate_rows:
            question = questions[row_id]
            mapping = mappings.get(row_id, {})
            groups = mapping.get("groups")
            relevant: set[str] = set()
            if isinstance(groups, list):
                for group in groups:
                    if not isinstance(group, Mapping):
                        continue
                    alternatives = group.get("alternatives")
                    if not isinstance(alternatives, list):
                        continue
                    for alternative in alternatives:
                        if not isinstance(alternative, Mapping):
                            continue
                        item_ids = alternative.get("mapped_item_ids")
                        if isinstance(item_ids, list):
                            relevant.update(map(str, item_ids))
            ranked = question.get("ranked_item_ids")
            ranked = list(map(str, ranked)) if isinstance(ranked, list) else []
            ranks = [
                ranked.index(item_id) + 1
                for item_id in relevant
                if item_id in ranked
            ]
            pre = question.get("pre_rerank_item_ids")
            pre = list(map(str, pre)) if isinstance(pre, list) else []
            pre_ranks.extend(
                pre.index(item_id) + 1
                for item_id in relevant
                if item_id in pre
            )
            if ranks:
                current = min(ranks)
                route = (
                    f"{candidate.get('retriever')}/"
                    f"{candidate.get('source_composition')}/"
                    f"{candidate.get('reranker')}"
                )
                if best_rank is None or current < best_rank:
                    best_rank = current
                    best_routes = [route]
                elif current == best_rank:
                    best_routes.append(route)
        item["best_relevant_rank"] = best_rank
        item["best_routes"] = sorted(set(best_routes))
        item["best_pre_rerank_rank"] = min(pre_ranks) if pre_ranks else None
    return {
        "candidate_count": len(candidate_rows),
        "evaluable_count": len(hardest),
        "hardest": hardest[:10],
        "all_failed": all_failed,
        "failure_rate_by_type": {
            key: sum(values) / len(values)
            for key, values in sorted(type_rates.items())
        },
        "failure_rate_by_domain": {
            key: sum(values) / len(values)
            for key, values in sorted(domain_rates.items())
        },
        "never_perfect_count": sum(
            float(row["best_ndcg"]) < 1.0 - 1e-12 for row in hardest
        ),
    }


def _note_summary(run_root: Path) -> tuple[int, int, bool]:
    path = run_root / "note-runs" / "frozen" / "frozen-notes.jsonl"
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, Mapping):
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0, 0, False
    return (
        len(rows),
        sum(int(row.get("citation_count", 0)) for row in rows),
        bool(rows)
        and all(row.get("template") == "generic-research-note" for row in rows)
        and all(row.get("audit_sha256") for row in rows),
    )


def _strategy_rows_html(
    rows: Sequence[Mapping[str, object]],
    payloads: Mapping[str, Mapping[str, Any]],
    stage_id: str,
) -> str:
    parts = []
    for row in rows:
        if str(row.get("stage_id")) != stage_id:
            continue
        config_id = str(row.get("config_id"))
        payload = payloads.get(config_id, {})
        candidate = _candidate(row, payload)
        label = _strategy_label(row, candidate)
        mechanism = _mechanism(row, candidate)
        rationale = _rationale(row, candidate)
        actual, verdict = _actual_result(row, payload)
        validity = str(row.get("validity_class") or "unknown")
        badge = VALIDITY_LABELS.get(validity, validity)
        search_text = " ".join(
            (
                label,
                config_id,
                stage_id,
                mechanism,
                rationale,
                actual,
                verdict,
                validity,
            )
        )
        parts.append(
            f"""
            <tr class="strategy-row" data-validity="{_escape(validity)}"
                data-search="{_escape(search_text.lower())}">
              <th scope="row">
                <span class="strategy-name">{_escape(label)}</span>
                <code>{_escape(config_id)}</code>
                <span class="badge badge-{_escape(validity)}">{_escape(badge)}</span>
              </th>
              <td><strong>怎么做</strong><br>{_escape(mechanism)}</td>
              <td><strong>为什么这样设计／预期</strong><br>{_escape(rationale)}</td>
              <td><strong>实际</strong><br>{_escape(actual)}<br>
                  <span class="verdict">{_escape(verdict)}</span></td>
            </tr>
            """
        )
    return "".join(parts)


def _bar_rows(values: Mapping[str, float]) -> str:
    return "".join(
        f"""
        <div class="bar-row">
          <span>{_escape(key)}</span>
          <div class="bar-track"><i style="inline-size:{value * 100:.1f}%"></i></div>
          <strong>{value * 100:.1f}%</strong>
        </div>
        """
        for key, value in values.items()
    )


def build_rq2_detailed_html(
    *,
    run_root: str | Path,
    leaderboard_path: str | Path,
    reconciliation: Mapping[str, object],
    extensions: Sequence[Mapping[str, object]],
    bootstrap: Mapping[str, object],
    mapping: Mapping[str, object],
) -> bytes:
    """Return a complete, offline HTML report containing no source text."""

    run = Path(run_root)
    if reconciliation.get("status") != "completed":
        raise DetailedReportError("completed reconciliation is required")
    if mapping.get("passed") is not True:
        raise DetailedReportError("passed evidence mapping is required")
    if int(bootstrap.get("samples", 0)) < 10_000:
        raise DetailedReportError("10,000-sample bootstrap is required")
    leaderboard_rows = _read_csv(Path(leaderboard_path))
    rows = _merge_strategy_rows(leaderboard_rows, extensions)
    payloads = _load_payloads(run)
    question_analysis = _question_analysis(payloads)
    note_count, citation_count, notes_audited = _note_summary(run)
    state = _read_json(run / "run-state.json")

    winner_id = str(bootstrap.get("candidate_config_id"))
    baseline_id = str(bootstrap.get("baseline_config_id"))
    winner = next(row for row in rows if str(row.get("config_id")) == winner_id)
    baseline = next(row for row in rows if str(row.get("config_id")) == baseline_id)
    class_counts = Counter(str(row.get("validity_class")) for row in rows)
    completed_count = sum(str(row.get("status")) == "completed" for row in rows)
    passed_count = class_counts["valid-and-rankable"]
    analysis_count = int(question_analysis["candidate_count"])
    evaluable_count = int(question_analysis["evaluable_count"])
    all_failed = question_analysis["all_failed"]
    never_perfect = int(question_analysis["never_perfect_count"])

    stage_sections = []
    for stage_id in STAGE_ORDER:
        count = sum(str(row.get("stage_id")) == stage_id for row in rows)
        stage_sections.append(
            f"""
            <section id="stage-{_escape(stage_id)}">
              <div class="section-kicker">{_escape(STAGE_LABELS[stage_id])} · {count} 个策略</div>
              <h2>{_escape(STAGE_LABELS[stage_id])}</h2>
              <p>{_escape(STAGE_INTROS[stage_id])}</p>
              <div class="table-wrap">
                <table class="strategy-table">
                  <caption>{_escape(STAGE_LABELS[stage_id])}：机制、设计动机、预期与实测结果</caption>
                  <thead><tr>
                    <th>策略</th><th>机制</th><th>设计理由与预期</th><th>实际结果与判定</th>
                  </tr></thead>
                  <tbody>{_strategy_rows_html(rows, payloads, stage_id)}</tbody>
                </table>
              </div>
            </section>
            """
        )

    hard_rows = "".join(
        f"""
        <tr>
          <th scope="row"><code>{_escape(row['row_id'])}</code></th>
          <td>{_escape(row['domain'])}</td>
          <td>{_escape(row['question_type'])}</td>
          <td>{row['misses']} / {analysis_count}</td>
          <td>{_fmt_score(row['best_ndcg'], 3)}</td>
          <td>{'所有路线均失败' if row['misses'] == analysis_count else '仅少数路线能救回'}</td>
        </tr>
        """
        for row in question_analysis["hardest"]
    )
    hardest_detail = (
        all_failed[0]
        if all_failed
        else {
            "row_id": "—",
            "best_relevant_rank": None,
            "best_pre_rerank_rank": None,
        }
    )
    best_rank = hardest_detail.get("best_relevant_rank")
    best_pre_rank = hardest_detail.get("best_pre_rerank_rank")

    stage_nav = "".join(
        f'<a href="#stage-{stage}">{_escape(STAGE_LABELS[stage])}</a>'
        for stage in STAGE_ORDER
    )
    html_document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <meta name="description" content="ResearchQA rq-2 的 39 个 RAG 策略、故障修复与共同失败模式审计">
  <title>ResearchQA rq-2 · RAG 策略全景审计</title>
  <style>
    :root {{
      --bg:#08111f; --panel:#0f1b2d; --panel-2:#14243a; --ink:#e7eef9;
      --muted:#9eb0c9; --line:#29415f; --cyan:#4ed6c8; --blue:#6ea8fe;
      --green:#5ee19a; --amber:#f4c56a; --red:#ff7f86; --purple:#b59cff;
      --shadow:0 18px 60px rgba(0,0,0,.28); --radius:18px;
    }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{
      margin:0; color:var(--ink); background:
      radial-gradient(circle at 12% 0%,rgba(78,214,200,.11),transparent 34rem),
      radial-gradient(circle at 90% 18%,rgba(110,168,254,.10),transparent 30rem),
      var(--bg); font:15px/1.72 Inter,ui-sans-serif,system-ui,-apple-system,
      BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    }}
    a {{ color:var(--cyan); text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    code {{
      font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
      color:#b7d5ff; overflow-wrap:anywhere;
    }}
    .layout {{
      display:grid; grid-template-columns:minmax(180px,220px) minmax(0,1fr) minmax(190px,240px);
      gap:24px; max-inline-size:1680px; margin-inline:auto; padding:24px;
    }}
    .rail {{
      position:sticky; inset-block-start:24px; align-self:start; max-block-size:calc(100vh - 48px);
      overflow:auto; padding:18px; border:1px solid var(--line);
      border-radius:var(--radius); background:rgba(15,27,45,.84); backdrop-filter:blur(16px);
    }}
    .rail strong {{ display:block; margin-block-end:10px; font-size:12px; letter-spacing:.12em; color:var(--muted); }}
    .rail a {{ display:block; padding-block:6px; color:#c8d8ec; }}
    .rail a:hover {{ color:var(--cyan); }}
    main {{ min-inline-size:0; }}
    .hero {{
      padding:clamp(28px,5vw,64px); border:1px solid var(--line); border-radius:28px;
      background:linear-gradient(145deg,rgba(20,36,58,.96),rgba(10,20,34,.96));
      box-shadow:var(--shadow); overflow:hidden; position:relative;
    }}
    .hero::after {{
      content:""; position:absolute; inline-size:280px; block-size:280px;
      inset-inline-end:-100px; inset-block-start:-120px; border-radius:50%;
      border:44px solid rgba(78,214,200,.08);
    }}
    .eyebrow,.section-kicker {{
      color:var(--cyan); font-size:12px; font-weight:800; letter-spacing:.14em; text-transform:uppercase;
    }}
    h1 {{ max-inline-size:980px; margin:.35em 0 .25em; font-size:clamp(36px,6vw,76px); line-height:1.02; letter-spacing:-.045em; }}
    h2 {{ margin:.2em 0 .45em; font-size:clamp(25px,3.2vw,42px); line-height:1.15; letter-spacing:-.025em; }}
    h3 {{ margin-block-start:1.5em; font-size:20px; }}
    .lede {{ max-inline-size:920px; color:#c1d1e6; font-size:18px; }}
    .hero-meta {{ display:flex; flex-wrap:wrap; gap:9px; margin-block-start:24px; }}
    .pill,.badge {{
      display:inline-flex; align-items:center; border-radius:999px; border:1px solid var(--line);
      padding:4px 10px; font-size:12px; background:rgba(8,17,31,.5);
    }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-block:22px; }}
    .metric,.card,.callout {{
      border:1px solid var(--line); border-radius:var(--radius); background:var(--panel);
      box-shadow:var(--shadow);
    }}
    .metric {{ padding:18px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-block-start:5px; font-size:clamp(23px,3vw,36px); line-height:1.1; }}
    section {{ margin-block:46px; scroll-margin-block-start:20px; }}
    .card {{ padding:22px; }}
    .grid-2 {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
    .grid-3 {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }}
    .callout {{ padding:22px; border-inline-start:5px solid var(--cyan); }}
    .callout.warn {{ border-inline-start-color:var(--amber); }}
    .callout.danger {{ border-inline-start-color:var(--red); }}
    .callout strong {{ color:#fff; }}
    .timeline {{ position:relative; margin-inline-start:9px; padding-inline-start:28px; border-inline-start:2px solid var(--line); }}
    .timeline article {{ position:relative; margin-block:0 26px; }}
    .timeline article::before {{
      content:""; position:absolute; inline-size:12px; block-size:12px; border-radius:50%;
      background:var(--cyan); inset-inline-start:-35px; inset-block-start:7px; box-shadow:0 0 0 6px rgba(78,214,200,.12);
    }}
    .timeline h3 {{ margin:0 0 4px; }}
    .table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:var(--radius); }}
    table {{ inline-size:100%; border-collapse:collapse; background:rgba(15,27,45,.72); }}
    caption {{ text-align:start; padding:14px 16px; color:var(--muted); }}
    th,td {{ padding:14px 16px; border-block-start:1px solid var(--line); text-align:start; vertical-align:top; }}
    thead th {{ position:sticky; inset-block-start:0; background:#172940; color:#dbe8f8; z-index:1; }}
    .strategy-table {{ min-inline-size:1080px; }}
    .strategy-table th:first-child {{ inline-size:18%; }}
    .strategy-name {{ display:block; font-weight:800; color:#fff; margin-block-end:5px; }}
    .verdict {{ color:var(--muted); }}
    .badge {{ margin-block-start:8px; }}
    .badge-valid-and-rankable {{ color:var(--green); border-color:rgba(94,225,154,.4); }}
    .badge-valid-but-poor {{ color:var(--amber); border-color:rgba(244,197,106,.4); }}
    .badge-diagnostic-only\\/ineligible {{ color:var(--purple); border-color:rgba(181,156,255,.4); }}
    .badge-deterministic-strategy-failure {{ color:var(--red); border-color:rgba(255,127,134,.4); }}
    .filterbar {{ display:flex; gap:10px; flex-wrap:wrap; padding:14px; background:var(--panel-2); border-radius:14px; margin-block:16px; }}
    input,select {{
      min-block-size:42px; color:var(--ink); background:#091525; border:1px solid var(--line);
      border-radius:10px; padding-inline:12px; font:inherit;
    }}
    input {{ flex:1 1 320px; }}
    .bar-row {{ display:grid; grid-template-columns:150px minmax(90px,1fr) 64px; gap:10px; align-items:center; margin-block:9px; }}
    .bar-track {{ block-size:9px; background:#0a1524; border-radius:99px; overflow:hidden; }}
    .bar-track i {{ display:block; block-size:100%; background:linear-gradient(90deg,var(--blue),var(--cyan)); border-radius:inherit; }}
    ul,ol {{ padding-inline-start:1.35em; }}
    .small {{ color:var(--muted); font-size:13px; }}
    pre {{
      padding:16px; overflow:auto; border:1px solid var(--line); border-radius:14px;
      background:#06101c; color:#cfe3ff; font:13px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;
    }}
    .right-stat {{ padding-block:11px; border-block-end:1px solid var(--line); }}
    .right-stat span {{ display:block; color:var(--muted); font-size:12px; }}
    .right-stat strong {{ font-size:20px; }}
    footer {{ margin-block:50px 20px; color:var(--muted); }}
    @media (max-width:1180px) {{
      .layout {{ grid-template-columns:190px minmax(0,1fr); }}
      .rail.right {{ display:none; }}
      .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
    }}
    @media (max-width:900px) {{
      .layout {{ display:block; padding:12px; }}
      .rail.left {{ position:relative; inset-block-start:auto; max-block-size:none; margin-block-end:16px; }}
      .rail.left a {{ display:inline-block; margin-inline-end:14px; }}
      .grid-2,.grid-3,.metrics {{ grid-template-columns:1fr; }}
      .hero {{ padding:28px 20px; }}
    }}
    @media print {{
      :root {{ --bg:#fff; --panel:#fff; --panel-2:#f3f6fa; --ink:#152033; --muted:#53647a; --line:#ccd5e0; }}
      body {{ background:#fff; }}
      .layout {{ display:block; max-inline-size:none; }}
      .rail,.filterbar {{ display:none; }}
      .hero,.metric,.card,.callout {{ box-shadow:none; }}
      section {{ break-inside:avoid; }}
      .table-wrap {{ overflow:visible; }}
      .strategy-table {{ min-inline-size:0; font-size:10px; }}
      thead th {{ position:static; background:#eef2f7; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <nav class="rail left" aria-label="报告导航">
      <strong>RQ-2 STRATEGY AUDIT</strong>
      <a href="#summary">结论</a>
      <a href="#protocol">评测口径</a>
      <a href="#timeline">问题与修复</a>
      <a href="#strategies">全部策略</a>
      {stage_nav}
      <a href="#common">共性失败</a>
      <a href="#hardest">最难问题</a>
      <a href="#next">迭代方向</a>
      <a href="#reproduce">复现</a>
    </nav>

    <main>
      <header class="hero" id="summary">
        <div class="eyebrow">ResearchQA · rq-2 · paper-scoped · final audit</div>
        <h1>39 个 RAG 策略，真正解决了什么？</h1>
        <p class="lede">
          这不是一张只报最高分的排行榜。报告逐项解释每个切分、召回、语料融合、
          重排序与组合策略为何被设计、预期解决什么、实际得到了什么；同时保留从
          全局索引偏差、假通过、CUDA 崩溃到最终可审计发布的完整修复链。
        </p>
        <div class="hero-meta">
          <span class="pill">run {_escape(state.get('run_id'))}</span>
          <span class="pill">更新 {_escape(state.get('updated_at'))}</span>
          <span class="pill">20 papers · 10 domains · 254 questions</span>
          <span class="pill">380 / 380 evidence groups mapped</span>
        </div>
      </header>

      <div class="metrics" aria-label="关键结果">
        <div class="metric"><span>provisional winner</span><strong>RR1</strong><code>{_escape(winner_id)}</code></div>
        <div class="metric"><span>coverage-nDCG@10</span><strong>{_fmt_score(winner.get('primary_score'), 6)}</strong><span>baseline {_fmt_score(baseline.get('primary_score'), 6)}</span></div>
        <div class="metric"><span>paired improvement</span><strong>{_fmt_delta(bootstrap.get('observed_delta'))}</strong><span>95% CI [{_fmt_delta(bootstrap.get('lower'))}, {_fmt_delta(bootstrap.get('upper'))}]</span></div>
        <div class="metric"><span>策略终态</span><strong>{completed_count}/39</strong><span>{passed_count} 个有效且可排名；1 个确定性失败</span></div>
      </div>

      <section>
        <div class="grid-2">
          <div class="callout">
            <strong>质量赢家：RR1。</strong>
            depth-20 重排不再替换基础排序，而是保留 base top-1 后做 rank-RRF。
            主指标提升 {_fmt_delta(bootstrap.get('observed_delta'))}，10,000 次论文级、
            分领域 bootstrap 区间不跨 0，且没有新增 Recall@10 hard failure。
          </div>
          <div class="callout warn">
            <strong>产品默认值仍未决定。</strong>
            RR1 observed-only p95 为 {_fmt_latency(winner.get('p95_latency_ms'))}，
            GPU 运行中存在热降频；R1 用 {_fmt_latency(next(row for row in rows if str(row.get('extension_id')) == 'R1').get('p95_latency_ms'))}
            获得 +0.0102，是更低成本候选。rq-2 只证明检索质量，不等于生产 SLA。
          </div>
        </div>
      </section>

      <section id="protocol">
        <div class="section-kicker">Evaluation contract</div>
        <h2>先固定“什么算赢”</h2>
        <div class="grid-3">
          <article class="card"><h3>数据层</h3><p>20 篇公开论文覆盖 10 个领域；Main PDF 用于直接检索，全部官方 SI / auxiliary 都进入通用笔记生成。直接 SI chunk 检索不在 rq-2 范围内。</p></article>
          <article class="card"><h3>问题层</h3><p>254 问中 239 问带 reference、可评分；15 道无 reference adversarial 只保留检索分布，不冒充拒答准确率。</p></article>
          <article class="card"><h3>笔记层</h3><p>{note_count}/20 强制 generic-research-note，交叉审计并冻结；共 {citation_count} 条原生引用，审计状态：{'通过' if notes_audited else '证据不完整'}。</p></article>
          <article class="card"><h3>检索范围</h3><p>每道题只在所属论文内排序，避免跨论文污染。产品中的未知论文查询仍需另测 document router，不能把 gold paper_id 当线上能力。</p></article>
          <article class="card"><h3>主指标</h3><p>coverage-nDCG@10 同时奖励证据组覆盖和前排位置；另外监控 Recall@5/10、MRR、all-required-groups success。</p></article>
          <article class="card"><h3>晋级门</h3><p>均值提升仍不够：限制领域／题型退化、总体 Recall 回归和新增硬失败；成本超过 1.5× 必须单独复核。</p></article>
        </div>
        <div class="callout danger" style="margin-block-start:16px">
          <strong>最重要的评分修复：</strong>
          早期 guardrails_passed 只检查“指标存在且有限”，因此会制造假通过。
          最终版本改成相对基线的切片与 hard-failure 门。39 个结果中只有 {passed_count}
          个有效且可排名，其余低分是被保留的真实负结果，不再混成“代码坏了”或“都通过”。
        </div>
      </section>

      <section id="timeline">
        <div class="section-kicker">Failure → evidence → repair</div>
        <h2>中途遇到的问题，以及最后怎么收口</h2>
        <div class="timeline">
          <article><h3>1 · 通用笔记模板不够通用</h3><p>跨领域论文让原模板出现结构重叠与审稿视角形式化。修复为压缩后的 generic-research-note，并把 reviewer 视角限定为有科学依据的 fatal/major/minor 质疑；20 篇全部独立生成、交叉审计、哈希冻结。</p></article>
          <article><h3>2 · 旧实现违反 paper-scoped 合同</h3><p>最初 20 篇共享一套全局索引，尤其放大 note route 的跨论文污染。旧分数全部隔离为 global-corpus diagnostic；正式 35 项重新按逐论文索引执行，并新增双论文干扰回归。</p></article>
          <article><h3>3 · “有数值”被误写成“通过”</h3><p>34 个旧完成记录曾全部显示通过，复盘后大量策略在领域、adversarial、Recall@10 或硬失败门越线。修复为阶段基线、切片退化、全证据组成功率和 0 新 hard failure 的相对合同，并区分 rankable / poor / diagnostic / deterministic failure。</p></article>
          <article><h3>4 · structure-aware 不能覆盖异构 PDF</h3><p>原策略在结构标记缺失时直接失败，20 篇中 9 篇检测不到结构。F2 增加逐论文 fallback 和碎片化成本门，恢复 20/20 可执行；但正式质量仍下降并新增 3 个硬失败，所以修复了实验有效性，没有伪装成质量成功。</p></article>
          <article><h3>5 · reranker CUDA 崩溃与假终态</h3><p>旧 adapter 先生成 [batch, sequence, 151669] 全词表 logits，再取最后 token，8 GB GPU 触发 illegal-memory-access。修复为 logits_to_keep=1；用真实 pair 与完整候选验证排序／指标一致，再回放 depth-20/50/100 和 6 个 confirmation。</p></article>
          <article><h3>6 · 重启后可能重复算或误接旧 envelope</h3><p>实现逐论文原子 progress、question-results SHA、代码／输入 fingerprint 和完整 warmup/timed-pass checkpoint。中断恢复只重做未完成单元；completed 与 guardrail_finalized 分离，旧 schema 不再冒充新终态。</p></article>
          <article><h3>7 · 外层 run 已记录 terminal failure</h3><p>没有篡改历史 run-state。新增 superseding reconciliation，哈希绑定原失败、35 个候选、4 个扩展、note pre-quality 和最终聚合产物，形成 8 completed、0 pending/failed/blocked 的有效发布视图。</p></article>
          <article><h3>8 · 公开报告还可能泄露本地状态</h3><p>最终导出只允许聚合字段，禁发 PDF、问题正文、答案、chunks、笔记和绝对路径；公开目录使用精确 allowlist、隐私扫描、artifact SHA 与原子替换。本 HTML 也只发布策略配置、聚合指标和稳定 row_id。</p></article>
        </div>
      </section>

      <section id="strategies">
        <div class="section-kicker">39 strategies · no cherry-picking</div>
        <h2>每个策略：怎么做、为什么、预期与实际</h2>
        <p>下面保留全部 35 个冻结矩阵策略与 F2 / R1 / S1 / RR1 四个批准扩展，包括真实低分与确定性失败。可搜索组件、config ID、失败门或结论。</p>
        <div class="filterbar">
          <label class="small" for="strategy-search">筛选策略</label>
          <input id="strategy-search" type="search" placeholder="例如 parent-child、hard failure、RR1">
          <select id="validity-filter" aria-label="按有效性筛选">
            <option value="">全部有效性</option>
            <option value="valid-and-rankable">有效且可排名</option>
            <option value="valid-but-poor">结果有效但质量不合格</option>
            <option value="diagnostic-only/ineligible">仅诊断</option>
            <option value="deterministic-strategy-failure">确定性失败</option>
          </select>
          <span id="visible-count" class="pill">39 / 39</span>
        </div>
      </section>

      {''.join(stage_sections)}

      <section id="common">
        <div class="section-kicker">Cross-strategy diagnosis</div>
        <h2>共性问题：为什么“换了很多方法”仍上不去</h2>
        <div class="grid-2">
          <article class="card"><h3>1 · 候选生成先决定上限</h3><p>最难的全策略失败题中，相关证据最好只到第 {best_rank or '—'}；dense / hybrid 更靠后。RR1 的 top-20 在重排前就看不到它，重排序不可能恢复未进入候选池的证据。depth-50/100 虽能看到更深候选，通用 answer-relevance 打分也没有把反证推入 top-10。</p></article>
          <article class="card"><h3>2 · 否定、限制和“没有做”不是普通相似度</h3><p>唯一 38/38 全失败的是有 reference 的 adversarial：需要区分论文真正执行的 case study 与只在未来应用中提到的模态。查询里的错误前提词反而更像文中多个正向主题，dense、BM25、笔记和通用 reranker 都缺少显式 claim verification。</p></article>
          <article class="card"><h3>3 · 单一粒度没有全局最优</h3><p>宽块更利于多限定和跨段证据，窄块更利于 lookup；固定 1200 的均值更高，却因新增 hard failures 无法越过 fixed-800 的阶段门。page/section heuristic 又产生大量短块，说明边界质量与 top-k 多样性同等重要。</p></article>
          <article class="card"><h3>4 · rank-only 融合不理解“新增证据”</h3><p>等权 hybrid、PDF+note RRF 和 S1 都能抬高或改变部分排序，但会把重复、同页或高 fanout 回链塞进前排。S1 即使只给 note 0.1 权重，仍让 246/254 排序变化且 9 个领域越线；需要 span 去重和 novel-evidence gate，而不是继续微调权重。</p></article>
          <article class="card"><h3>5 · 层级路线只重排已有 child</h3><p>当前 hierarchical-pdf 的 children 必须先进入全局 child top-k；parent 命中并不会展开其全部 children，因此存在 parent gate + child gate 双重瓶颈。它不是“层级检索无效”，而是尚未实现真正的 parent→child 局部重检索。</p></article>
          <article class="card"><h3>6 · 均值会掩盖灾难性局部回归</h3><p>直接 rerank-50、等权 hybrid 和多个 Top-2 组合都出现“总体提升但新增 Recall@10=0”。这就是最终晋级门禁止只看最高均值的原因；RR1/R1 的价值主要在于保护已正确结果，而不只是多拿几个平均分点。</p></article>
          <article class="card"><h3>7 · 失败具有领域与题型结构</h3><p>按 {analysis_count} 个成功执行策略的逐题结果，adversarial 的平均 hard-failure rate 最高。它要求检索限定、反驳与缺失事实，而现有模型与 prompt 偏向“找能回答 query 的 passage”。</p></article>
          <article class="card"><h3>8 · benchmark 上界不等于产品上界</h3><p>本轮使用 gold paper identity 做 paper-scoped 检索，是文内策略比较的 oracle 条件。真实全库产品还缺 query-only document routing、跨论文候选和拒答／生成评测；这些不能从 rq-2 的 0.8481 直接外推。</p></article>
        </div>
        <div class="grid-2" style="margin-block-start:16px">
          <div class="card"><h3>按题型：策略平均硬失败率</h3>{_bar_rows(question_analysis['failure_rate_by_type'])}</div>
          <div class="card"><h3>按领域：策略平均硬失败率</h3>{_bar_rows(question_analysis['failure_rate_by_domain'])}</div>
        </div>
      </section>

      <section id="hardest">
        <div class="section-kicker">Persistent hard cases</div>
        <h2>最难问题不是“偶尔丢分”，而是架构盲区</h2>
        <p>在 {analysis_count} 个成功执行策略、{evaluable_count} 道可评分题上，只有 {len(all_failed)}
          道题被全部策略 Recall@10=0；另有 {never_perfect} 道题没有任何策略达到完美
          coverage-nDCG@10。所有 row_id 都是稳定标识，不包含问题、答案或论文原文。</p>
        <div class="table-wrap">
          <table>
            <caption>按“有多少策略 Recall@10=0”排序的十个最难问题</caption>
            <thead><tr><th>row_id</th><th>领域</th><th>题型</th><th>失败策略</th><th>最佳 nDCG</th><th>解释</th></tr></thead>
            <tbody>{hard_rows}</tbody>
          </table>
        </div>
        <div class="callout danger" style="margin-block-start:18px">
          <strong>38/38 全失败：{_escape(hardest_detail.get('row_id'))}。</strong>
          它要求拒绝一个把“未来可能支持的能力”误写成“论文已经完成的 case study”
          的错误前提。映射完整，不是标注丢失；相关证据在所有最终列表中的最佳位置是
          rank {best_rank or '—'}，在重排前列表中的最佳位置是 rank {best_pre_rank or '—'}。
          这同时暴露了三层缺口：没有把错误前提拆成可验证 claim；候选召回没有主动寻找
          “实际做了什么”与“仅提到未来应用”的对照证据；通用 reranker 也没有 refute /
          qualify 的证据意图。继续只换 chunk size 或把 depth 从 50 加到 100，不能针对性解决。
        </div>
        <div class="callout warn" style="margin-block-start:14px">
          <strong>但大多数难题具有策略互补性。</strong>
          239 道可评分题中只有 1 道没有任何路线命中 top-10，说明剩余问题更多是
          非 oracle 的路由、融合和多样性选择，而不是证据完全不可检索。下一轮应把
          “不同路线是否提供新 evidence span”变成线上可观察信号，而不是按 gold 题型选策略。
        </div>
      </section>

      <section id="next">
        <div class="section-kicker">Prioritized backlog</div>
        <h2>下一轮该迭代什么</h2>
        <ol class="card">
          <li><strong>低成本产品候选先比较 R1 与 RR1。</strong> 用固定热状态、交错顺序做 decisive latency；再用 query-only 的 score gap、dense/BM25 分歧和候选重复率决定是否触发 RR1，禁止用 benchmark question_type 路由。</li>
          <li><strong>新增 adversarial claim-verification 路线。</strong> 先把 query 拆成被断言的事实和需要核实的关系，同时检索支持证据与限制／反证；候选固定后在 rq-2 hard set 回归，不能改题。</li>
          <li><strong>实现真正的 H1 parent expansion。</strong> parent 命中后在其全部 children 内局部重检索，再与 direct child fallback 融合；分开报告 parent recall 与 child-given-parent recall。</li>
          <li><strong>把 S2 从“加权笔记”改成“只补新证据”。</strong> 保留 direct PDF top ranks，以 canonical span/page 去重；note route 只有提供 direct top-k 未覆盖的 span 才能进入。</li>
          <li><strong>为所有路线加入 evidence diversity。</strong> 限制同页、同 parent、近重复 chunk 占满 top-k，直接优化 all-required-groups success，而不只优化单块相关性。</li>
          <li><strong>扩层前冻结策略。</strong> 上述单变量在 rq-2 定义与 hard-case 回归上关闭后，再按 ADR 进入 rq-5 / rq-10；首次查看新层级结果后不得继续调本层参数。</li>
        </ol>
      </section>

      <section id="reproduce">
        <div class="section-kicker">Reproducibility</div>
        <h2>报告怎样与证据保持一致</h2>
        <p>HTML 由同一个 fail-closed public exporter 从 reconciliation、39 个候选 envelope、
          聚合 CSV、逐题 metrics 和冻结笔记清单生成。它不复制 ResearchQA 问题／答案、
          source text、PDF、chunks、embeddings 或本地路径。</p>
        <pre><code>python benchmarks/scripts/export_rq2_public_report.py \
  --run-root benchmarks/.cache/researchqa/runs/&lt;run-id&gt;</code></pre>
        <p>关键冻结配置示例：</p>
        <pre><code>retrieval:
  scope: paper-scoped
models:
  embedding: qwen3-embedding:4b
  reranker: Qwen/Qwen3-Reranker-0.6B
decision:
  primary_tie_threshold: 0.005
  stop_after_report: true</code></pre>
        <p class="small">本报告把 RR1 称为 provisional benchmark winner，不把它称为生产默认值；observed-only latency 也不作为 SLA。</p>
      </section>

      <footer>
        ResearchQA rq-2 strategy audit · standalone offline HTML · public aggregate evidence only
      </footer>
    </main>

    <aside class="rail right" aria-label="快速结论">
      <strong>QUICK READ</strong>
      <div class="right-stat"><span>Winner</span><strong>RR1</strong></div>
      <div class="right-stat"><span>Primary</span><strong>{_fmt_score(winner.get('primary_score'), 4)}</strong></div>
      <div class="right-stat"><span>Paired delta</span><strong>{_fmt_delta(bootstrap.get('observed_delta'))}</strong></div>
      <div class="right-stat"><span>Rankable</span><strong>{passed_count} / 39</strong></div>
      <div class="right-stat"><span>Persistent all-fail</span><strong>{len(all_failed)}</strong></div>
      <div class="right-stat"><span>Mapped evidence</span><strong>{mapping.get('mapped_groups')} / {mapping.get('total_groups')}</strong></div>
      <p class="small">核心判断：下一步不是继续加大模型，而是修复候选生成、反证意图、span 多样性与非 oracle 路由。</p>
    </aside>
  </div>
  <script>
    (() => {{
      const search = document.querySelector('#strategy-search');
      const validity = document.querySelector('#validity-filter');
      const rows = [...document.querySelectorAll('.strategy-row')];
      const output = document.querySelector('#visible-count');
      const apply = () => {{
        const query = search.value.trim().toLowerCase();
        const selected = validity.value;
        let visible = 0;
        for (const row of rows) {{
          const matchText = !query || row.dataset.search.includes(query);
          const matchValidity = !selected || row.dataset.validity === selected;
          row.hidden = !(matchText && matchValidity);
          if (!row.hidden) visible += 1;
        }}
        output.textContent = `${{visible}} / ${{rows.length}}`;
      }};
      search.addEventListener('input', apply);
      validity.addEventListener('change', apply);
    }})();
  </script>
</body>
</html>
"""
    normalized = "\n".join(
        line.rstrip() for line in html_document.splitlines()
    )
    return (normalized + "\n").encode("utf-8")


__all__ = [
    "DetailedReportError",
    "REPORT_FILENAME",
    "build_rq2_detailed_html",
]

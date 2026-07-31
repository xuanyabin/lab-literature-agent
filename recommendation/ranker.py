"""个性化精排（三层漏斗的第 3 层）：六维加权 Final Score + AI 推荐理由。

规则粗筛（recommendation/scorer.py）选出当日候选后，本模块对每篇论文
计算 0-100 的 Final Score（权重见 config/scoring.yaml 的 ranker 节）：

    personal  个人相关度（LLM 依据个人研究画像语义判断）
    lab       实验室方向相关度（lab_topics 命中数，规则）
    journal   期刊影响力（journals.yaml 分层，规则；期刊因素只在精排体现）
    novelty   新颖性（LLM 依据 AI 分析判断）
    method    方法相关度（个人 methods 命中数，规则）
    recency   时效性（发表日期距今，规则）

同时生成一句中文推荐理由展示在邮件论文卡片上；LLM 输出异常时
个人相关度/新颖性回退中性分 50，保证流水线不中断（日预算耗尽除外：
BudgetExhaustedError 直接向上传播，快速失败不发空壳邮件）。重要性按 Final Score
绝对阈值定级（ranker.thresholds，宁缺毋滥：当日全部低分则可以没有
Must Read）；不再按固定配额凑数。thresholds 可配 push_floor 推送下限：
低于该分的论文直接不进邮件，进一步贯彻宁缺毋滥。

LLM 判断按批处理执行（ranker.batch_size，默认每批 5 篇一次调用，prompt
prompts/recommendation_reason_batch.txt），批次之间可并发（max_workers）；
整批输出解析失败时整批回退中性分，不逐篇重试。
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import yaml

from processing.llm import BudgetExhaustedError, load_prompt
from recommendation.scorer import (
    CATEGORY_IMPORTANT,
    CATEGORY_MUST_READ,
    CATEGORY_REFERENCE,
    _normalize_journal,
    _text_of,
)
from sources.paper import Paper, term_matches, variants_for

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCORING_CONFIG = BASE_DIR / "config" / "scoring.yaml"

DEFAULT_RANKER_WEIGHTS = {
    "personal": 45, "lab": 15, "journal": 20,
    "novelty": 10, "method": 10, "recency": 0,
}
DEFAULT_RANKER_THRESHOLDS = {"must_read": 75, "important": 60}
DEFAULT_RANKER_GATING = {}
DEFAULT_BATCH_SIZE = 5  # 精排批处理：一次 LLM 调用评判的论文数

_NEUTRAL_JUDGMENT = {"personal_relevance": 50, "novelty": 50, "reason": ""}


def load_ranker_weights(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    """读取 scoring.yaml 的 ranker.weights，文件缺失或字段缺省时回退默认值。"""
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_RANKER_WEIGHTS)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    weights = (cfg.get("ranker") or {}).get("weights") or {}
    return {**DEFAULT_RANKER_WEIGHTS, **weights}


def load_ranker_thresholds(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    """读取 scoring.yaml 的 ranker.thresholds（Final Score 绝对定级阈值），缺省回退默认值。"""
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_RANKER_THRESHOLDS)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    thresholds = (cfg.get("ranker") or {}).get("thresholds") or {}
    return {**DEFAULT_RANKER_THRESHOLDS, **thresholds}


def load_ranker_gating(path: Path = DEFAULT_SCORING_CONFIG) -> dict:
    """读取精排分类封顶规则；缺省为空，不改变旧行为。"""
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_RANKER_GATING)
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return (cfg.get("ranker") or {}).get("gating") or dict(DEFAULT_RANKER_GATING)


def load_ranker_batch_size(path: Path = DEFAULT_SCORING_CONFIG) -> int:
    """读取 scoring.yaml 的 ranker.batch_size（一次 LLM 调用评判的论文数），缺省 5，下限 1。"""
    p = Path(path)
    if not p.exists():
        return DEFAULT_BATCH_SIZE
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        return max(1, int((cfg.get("ranker") or {}).get("batch_size", DEFAULT_BATCH_SIZE)))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def _category_of(score: int, thresholds: dict) -> str:
    """按绝对阈值定级：≥ must_read → Must Read；≥ important → Important；其余 Reference。"""
    if score >= thresholds.get("must_read", DEFAULT_RANKER_THRESHOLDS["must_read"]):
        return CATEGORY_MUST_READ
    if score >= thresholds.get("important", DEFAULT_RANKER_THRESHOLDS["important"]):
        return CATEGORY_IMPORTANT
    return CATEGORY_REFERENCE


def _cap_category(category: str, max_category: str) -> str:
    order = {
        CATEGORY_REFERENCE: 0,
        CATEGORY_IMPORTANT: 1,
        CATEGORY_MUST_READ: 2,
    }
    if order.get(category, 0) > order.get(max_category, order[CATEGORY_MUST_READ]):
        return max_category
    return category


def _apply_gating(category: str, dims: dict, coarse_score: int, gating: dict) -> str:
    weak = gating.get("weak_relevance") or {}
    if weak and dims.get("personal", 0) < weak.get("personal_below", -1) \
            and dims.get("method", 0) == weak.get("method_equals", dims.get("method", 0)) \
            and dims.get("lab", 0) < weak.get("lab_below", -1):
        category = _cap_category(category, weak.get("max_category", CATEGORY_REFERENCE))

    top = gating.get("top_journal_low_signal") or {}
    if top and dims.get("journal", 0) >= top.get("journal_at_least", 101) \
            and coarse_score <= top.get("coarse_at_most", -1) \
            and dims.get("lab", 0) == top.get("lab_equals", dims.get("lab", 0)) \
            and dims.get("personal", 0) < top.get("personal_below", -1):
        category = _cap_category(category, top.get("max_category", CATEGORY_REFERENCE))
    return category


def _hits(text: str, terms: list, aliases: dict) -> int:
    """命中的检索词个数（同一词的多个变体只算一次）。"""
    n = 0
    for term in terms or []:
        term = term.strip()
        if not term:
            continue
        variants = variants_for(term, aliases)
        if any(term_matches(text, v) for v in variants):
            n += 1
    return n


def lab_relevance(paper: Paper, user: dict) -> int:
    """实验室方向相关度：lab_topics 命中 0/1/2/3/≥4 个 → 0/25/50/75/100。

    词表扩到 200+ 后"命中 ≥2 即满分"会饱和（几乎篇篇 100，维度失去区分度
    并普涨总分），批 13 起改为四档梯度。
    """
    return min(_hits(_text_of(paper), user.get("lab_topics"), user.get("aliases") or {}), 4) * 25


def method_relevance(paper: Paper, user: dict) -> int:
    """方法相关度：个人 methods 命中 0/1/≥2 个 → 0/50/100。"""
    return min(_hits(_text_of(paper), user.get("methods"), user.get("aliases") or {}), 2) * 50


def journal_influence(paper: Paper, journal_tiers: dict) -> int:
    """期刊影响力：T0 → 100，T1 → 70，未分层 → 30。"""
    tier = (journal_tiers or {}).get(_normalize_journal(paper.journal))
    return {"t0": 100, "t1": 70}.get(tier, 30)


def recency(paper: Paper, today: date | None = None) -> int:
    """时效性：当天/1天 → 100，2天 → 80，3天 → 60，一周内 → 40，更早 → 20；日期无法解析 → 50。"""
    today = today or date.today()
    try:
        published = date.fromisoformat((paper.date or "")[:10])
    except ValueError:
        return 50
    days = (today - published).days
    if days <= 1:
        return 100
    if days == 2:
        return 80
    if days == 3:
        return 60
    if days <= 7:
        return 40
    return 20


def final_score(dims: dict, weights: dict) -> int:
    """六维（各 0-100）按权重（合计 100）加权，返回 0-100 整数。"""
    total = sum(weights.values()) or 100
    return round(sum(dims[k] * weights.get(k, 0) for k in dims) / total)


def judge_paper(paper: Paper, analysis: dict, user: dict, llm) -> dict:
    """LLM 语义判断：{personal_relevance, novelty, reason}；输出异常回退中性分。"""
    prompt = load_prompt("recommendation_reason").safe_substitute(
        interests=", ".join(user.get("research_interest") or []) or "（无）",
        keywords=", ".join(user.get("keywords") or []) or "（无）",
        species=", ".join(user.get("species") or []) or "（无）",
        methods=", ".join(user.get("methods") or []) or "（无）",
        title=paper.title,
        abstract=paper.abstract or "（无摘要）",
        problem=analysis.get("problem", ""),
        finding=analysis.get("finding", ""),
    )
    return _parse_judgment(llm.complete(prompt))


def _profile_kwargs(user: dict) -> dict:
    return {
        "interests": ", ".join(user.get("research_interest") or []) or "（无）",
        "keywords": ", ".join(user.get("keywords") or []) or "（无）",
        "species": ", ".join(user.get("species") or []) or "（无）",
        "methods": ", ".join(user.get("methods") or []) or "（无）",
    }


def _papers_block(batch: list[dict]) -> str:
    """批处理 prompt 的论文区块：编号 + 标题/摘要/AI 分析，供模型按序输出数组。"""
    blocks = []
    for i, it in enumerate(batch, 1):
        paper, analysis = it["paper"], it["analysis"]
        blocks.append(
            f"论文 {i}\n"
            f"标题：{paper.title}\n"
            f"摘要：{paper.abstract or '（无摘要）'}\n"
            f"AI 分析的科学问题：{analysis.get('problem', '')}\n"
            f"AI 分析的核心发现：{analysis.get('finding', '')}"
        )
    return "\n\n".join(blocks)


def judge_batch(batch: list[dict], user: dict, llm) -> list[dict]:
    """一次 LLM 调用评判一批论文，返回与 batch 等长的 judgment 列表。

    只捕获输出解析问题（整批回退中性分）；LLM 调用异常向上抛，
    由 _judge_batch_safe 决定回退或传播（BudgetExhaustedError）。
    """
    prompt = load_prompt("recommendation_reason_batch").safe_substitute(
        **_profile_kwargs(user), papers=_papers_block(batch))
    return _parse_judgments(llm.complete(prompt), len(batch))


def _judge_batch_safe(batch: list[dict], user: dict, llm) -> list[dict]:
    """judge_batch 的护栏版：非预算异常整批回退中性分，不逐篇重试。"""
    try:
        return judge_batch(batch, user, llm)
    except BudgetExhaustedError:
        raise  # 日预算耗尽：快速失败，不发空壳邮件
    except Exception:
        logger.warning("精排批量判断失败，整批回退中性分", exc_info=True)
        return [dict(_NEUTRAL_JUDGMENT) for _ in batch]


def _parse_one(data) -> dict:
    """单条 judgment 校验归一化；非法回退中性分。"""
    try:
        return {
            "personal_relevance": max(0, min(100, int(data["personal_relevance"]))),
            "novelty": max(0, min(100, int(data["novelty"]))),
            "reason": str(data.get("reason", "")).strip(),
        }
    except (KeyError, TypeError, ValueError):
        return dict(_NEUTRAL_JUDGMENT)


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    return text


def _parse_judgments(raw: str, n: int) -> list[dict]:
    """解析批量输出（JSON 数组，长度须等于 n）；整体非法时整批回退中性分。"""
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        logger.warning("精排批量输出不是合法 JSON，整批回退中性分：%.100s", raw)
        return [dict(_NEUTRAL_JUDGMENT) for _ in range(n)]
    if isinstance(data, dict):  # 模型偶尔只返回单个对象
        data = [data]
    if not isinstance(data, list) or len(data) != n:
        logger.warning("精排批量输出条数不符（期望 %d 实得 %s），整批回退中性分",
                       n, len(data) if isinstance(data, list) else type(data).__name__)
        return [dict(_NEUTRAL_JUDGMENT) for _ in range(n)]
    return [_parse_one(item) for item in data]


def _parse_judgment(raw: str) -> dict:
    """单篇输出解析（judge_paper 用）：复用批量解析，n=1，非法回退中性分。"""
    return _parse_judgments(raw, 1)[0]


def rank_items(items: list[dict], user: dict, llm, journal_tiers: dict,
               weights: dict, thresholds: dict, today: date | None = None,
               batch_size: int = DEFAULT_BATCH_SIZE, max_workers: int = 8,
               gating: dict | None = None) -> list[dict]:
    """对 AI 处理完的 items 计算 Final Score，按分数降序重排并按绝对阈值定级。

    每个 item 增加 "score"（0-100）、"reason"（推荐理由）、"category" 字段。
    定级宁缺毋滥：当日全部低分则没有 Must Read / Important，不凑配额。
    thresholds 含 push_floor 时，Final Score 低于该值的论文被过滤不进邮件
    （可能返回空列表，调用方应跳过发信）。

    LLM 判断按 batch_size 分批一次调用评判多篇，批次间按 max_workers 并发；
    批次异常整批回退中性分（BudgetExhaustedError 上抛，快速失败）。
    """
    judgments: list[dict | None] = [None] * len(items)
    batches = [(start, items[start:start + batch_size])
               for start in range(0, len(items), batch_size)]
    if len(batches) > 1 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as pool:
            futures = {pool.submit(_judge_batch_safe, batch, user, llm): start
                       for start, batch in batches}
            for fut in as_completed(futures):
                start = futures[fut]
                result = fut.result()  # BudgetExhaustedError 在此上抛
                judgments[start:start + len(result)] = result
    else:
        for start, batch in batches:
            result = _judge_batch_safe(batch, user, llm)
            judgments[start:start + len(result)] = result

    scored = []
    for it, judgment in zip(items, judgments):
        paper = it["paper"]
        dims = {
            "personal": judgment["personal_relevance"],
            "lab": lab_relevance(paper, user),
            "journal": journal_influence(paper, journal_tiers),
            "novelty": judgment["novelty"],
            "method": method_relevance(paper, user),
            "recency": recency(paper, today),
        }
        it["score"] = final_score(dims, weights)
        it["reason"] = judgment["reason"]
        it["_dims"] = dims
        scored.append((it["score"], it))
    scored.sort(key=lambda x: -x[0])
    for score, it in scored:
        it["category"] = _category_of(score, thresholds)
    items = [it for _, it in scored]
    gating = gating or {}
    for it in items:
        it["category"] = _apply_gating(
            it["category"], it.get("_dims") or {}, int(it.get("coarse_score") or 0), gating)
        it.pop("_dims", None)
    floor = thresholds.get("push_floor")
    if floor is not None:  # 推送下限：低分论文不进邮件（宁缺毋滥）
        kept = [it for it in items if it["score"] >= floor]
        if len(kept) < len(items):
            logger.info("推送下限 %d：过滤 %d 篇低分论文", floor, len(items) - len(kept))
        items = kept
    return items

"""两级确定性近重复检测（Tier-0 Exact + Tier-1 TF-IDF 余弦 & IDF Veto 稀有词否决，零 IO）。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent.memory.ports import DedupMethod, DedupTier

_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]+")
_CODE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]+)*")


@dataclass(frozen=True, kw_only=True)
class PairClassification:
    """两条 Observation 标题的去重分级判定结果。"""

    tier: DedupTier
    method: DedupMethod
    score: float


def tokenize_ws(text: str | None) -> tuple[str, ...]:
    """将中英文与代码标识符分词并转小写。"""
    if not text:
        return ()
    return tuple(m.group(0).lower() for m in _TOKEN_RE.finditer(text) if m.group(0).strip())


def extract_code_identifiers(text: str | None) -> frozenset[str]:
    """提取包含下划线、驼峰或点号的代码标识符集合（用于防止不同符号误合并）。"""
    if not text:
        return frozenset()
    result: set[str] = set()
    for match_obj in _CODE_IDENTIFIER_RE.finditer(text):
        match = match_obj.group(0)
        has_code_shape = "_" in match or "." in match or any(ch.isupper() for ch in match[1:])
        if has_code_shape:
            result.add(match.lower())
    return frozenset(result)


def normalize_title(text: str | None) -> str:
    """将标题归一化为空格连接的词序列。"""
    return " ".join(tokenize_ws(text))


def exact_title_match(a: str | None, b: str | None) -> bool:
    """Tier-0 精确匹配：归一化标题非空且完全相等，且代码标识符一致。"""
    norm_a = normalize_title(a)
    norm_b = normalize_title(b)
    if not norm_a or not norm_b:
        return False
    if norm_a != norm_b:
        return False
    return extract_code_identifiers(a) == extract_code_identifiers(b)


def build_idf_lookup(
    documents: Sequence[Sequence[str]],
    *,
    default_idf: float = 2.5,
) -> Callable[[str], float]:
    """基于已有文档词袋构建平滑 IDF 查询函数: ln((1 + N) / (1 + df)) + 1.0。"""
    doc_count = len(documents)
    if doc_count == 0:
        return lambda _tok: default_idf
    df_counter: Counter[str] = Counter()
    for doc in documents:
        for tok in set(doc):
            df_counter[tok] += 1
    max_idf = math.log(1.0 + doc_count) + 1.0

    def _idf(token: str) -> float:
        df = df_counter.get(token.lower(), 0)
        if df <= 0:
            return max_idf
        return math.log((1.0 + doc_count) / (1.0 + df)) + 1.0

    return _idf


def tfidf_cosine(
    tokens_a: Sequence[str],
    tokens_b: Sequence[str],
    idf_fn: Callable[[str], float],
) -> float:
    """计算两个分词序列的 TF-IDF 余弦相似度。"""
    if not tokens_a or not tokens_b:
        return 0.0
    tf_a = Counter(tokens_a)
    tf_b = Counter(tokens_b)
    weights_a = {tok: count * idf_fn(tok) for tok, count in tf_a.items()}
    weights_b = {tok: count * idf_fn(tok) for tok, count in tf_b.items()}

    dot = sum(w * weights_b.get(tok, 0.0) for tok, w in weights_a.items())
    norm_a = math.sqrt(sum(w * w for w in weights_a.values()))
    norm_b = math.sqrt(sum(w * w for w in weights_b.values()))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def idf_veto_fires(
    tokens_a: Sequence[str],
    tokens_b: Sequence[str],
    idf_fn: Callable[[str], float],
    veto_theta_idf: float,
) -> bool:
    """IDF 否决器：若对称差集中存在高 IDF 稀有区分词（或不同代码符号），否决近重复合并。"""
    sym_diff = set(tokens_a) ^ set(tokens_b)
    return any(idf_fn(tok) >= veto_theta_idf for tok in sym_diff)


def classify_near_duplicate(
    a: str | None,
    b: str | None,
    idf_fn: Callable[[str], float],
    *,
    cosine_threshold: float = 0.72,
    veto_theta_idf: float = 2.2,
    min_shared_tokens: int = 2,
) -> PairClassification:
    """对两条标题执行两级确定性去重判定（exact / candidate / none）。"""
    if exact_title_match(a, b):
        return PairClassification(tier="exact", method="exact", score=1.0)

    # 若双方各自包含代码标识符且存在对称差异，直接拒绝合并（防止把相似句式的不同函数/配置项合并）
    ids_a = extract_code_identifiers(a)
    ids_b = extract_code_identifiers(b)
    if (ids_a or ids_b) and ids_a != ids_b:
        return PairClassification(tier="none", method="none", score=0.0)

    ta = tokenize_ws(a)
    tb = tokenize_ws(b)
    shared = len(set(ta) & set(tb))
    if shared < min_shared_tokens:
        return PairClassification(tier="none", method="none", score=0.0)

    score = tfidf_cosine(ta, tb, idf_fn)
    if score >= cosine_threshold and not idf_veto_fires(ta, tb, idf_fn, veto_theta_idf):
        return PairClassification(tier="candidate", method="idf_cosine", score=score)
    return PairClassification(tier="none", method="none", score=score)

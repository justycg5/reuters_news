#!/usr/bin/env python3
"""
prefilter_engine.py - 第一层新闻预过滤评分引擎（纯函数，无网络依赖）

定位：只回答"这条新闻有没有可能影响 A股、黄金、纳指ETF、标普ETF"，不做重要性判断。
评分：KeywordScore（词库分级权重 + 组合加分）与 BM25Score（标题 vs 5 主题文档）
      各自 z-score 归一化后按 0.6/0.4 融合；同词只计最高权重；词边界 + 简易词干匹配。
分层：Tier1 > P98、Tier2 P85~P98、Tier3 其余（默认当批动态分位数；可传固化阈值）。

用法（Phase 3 接入推送时）:
    import prefilter_engine as pe
    cfg = pe.load_configs(".")
    results = pe.score_all(items, cfg)          # items: [{"title": ...}, ...]
    tier1 = [r for r in results if r["tier"] == 1]
"""

import json
import math
import os
import re
from collections import Counter

# ---------------------------------------------------------------- 预处理

_STOPWORDS = {
    "the", "a", "an", "of", "and", "for", "to", "in", "on", "at", "with",
    "from", "by", "is", "are", "was", "were", "has", "have", "had", "it",
    "its", "as", "but", "or", "be", "been", "will", "would", "can", "could",
    "may", "might", "that", "this", "these", "those", "than", "then", "not",
}
_NORMALIZE = {
    "u.s.": "united states", "u.s": "united states", "us.": "united states",
}
_STEM_EXCEPT = {
    "news", "sales", "goods", "congress", "press", "class", "glass",
    "status", "focus", "virus", "analysis", "basis", "crisis", "thesis",
    "business",
}


def _stem(w: str) -> str:
    w = w.lower()
    if len(w) <= 3 or w in _STEM_EXCEPT or w.endswith("ss") or w.endswith("us") or w.endswith("is"):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("es") and len(w) > 4:
        # 只对 x/s/z/ch/sh 结尾的复数去 es（boxes->box），避免误伤 rates->rat
        base = w[:-2]
        if base.endswith(("x", "s", "z", "ch", "sh")):
            return base
    if w.endswith("ed") and len(w) > 5:
        base = w[:-2]
        if len(base) >= 2 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if w.endswith("ing") and len(w) > 5:
        base = w[:-3]
        if len(base) >= 2 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if w.endswith("s"):
        return w[:-1]
    return w


def clean_title(title: str):
    """标准化 + 小写 + 去停用词 + 简易词干。返回 (tokens, clean_str)。
    连字符复合词（sanctions-evasion）同时保留原形与拆分件（sanctions, evasion），
    既让词库词条命中，又不破坏 us-china / sino- 类词条。"""
    t = title.lower()
    for k, v in _NORMALIZE.items():
        t = t.replace(k, v)
    tokens = [t.strip(".,:;!?()[]\"'") for t in re.split(r"[^a-z0-9.\-']+", t)]
    tokens = [t for t in tokens if t]
    kept = []
    for tok in tokens:
        # 所有格 's 剥离（yuan's -> yuan，Taiwan's -> taiwan，DeepSeek's -> deepseek）
        if tok.endswith("'s"):
            tok = tok[:-2]
        if tok in _STOPWORDS:
            continue
        stemmed = _stem(tok)
        if stemmed:
            kept.append(stemmed)
        # 连字符复合词：拆分件也加入（sanctions-evasion -> sanctions, evasion）
        if "-" in tok:
            for part in tok.split("-"):
                if part and part not in _STOPWORDS:
                    p = _stem(part)
                    if p:
                        kept.append(p)
    return kept, " ".join(kept)


def _phrase_tokens(phrase: str):
    return [_stem(w) for w in phrase.lower().split() if w not in _STOPWORDS]


def _has_phrase(tokens: list, phrase_toks: list) -> bool:
    """词组匹配：tokens 包含 phrase_toks 全部词（无序共现，bag 语义）。
    解决 "cuts rates" vs 词条 "rate cut" 的顺序变体；重复词按出现次数计。"""
    if not phrase_toks:
        return False
    tc = Counter(tokens)
    for w in phrase_toks:
        if tc[w] <= 0:
            return False
        tc[w] -= 1
    return True


# ---------------------------------------------------------------- 配置加载

def load_configs(cfg_dir: str) -> dict:
    def _load(name):
        with open(os.path.join(cfg_dir, name), encoding="utf-8") as f:
            return json.load(f)

    wordbank = _load("wordbank.json")["layers"]
    asset_map = _load("asset_map.json")
    combos = _load("combos.json")["rules"]
    blacklist = _load("blacklist.json")["words"]
    docs = _load("documents.json")["documents"]

    # 预处理词库：每条目 -> stem 后的 token 元组；同词多层层级 -> 最高权重
    phrase_weight = {}
    layer_of = {}
    for layer, conf in wordbank.items():
        w = conf["weight"]
        for phrase in conf["words"]:
            pt = tuple(_phrase_tokens(phrase))
            if not pt:
                continue
            if pt in phrase_weight:
                phrase_weight[pt] = max(phrase_weight[pt], w)
            else:
                phrase_weight[pt] = w
            layer_of.setdefault(pt, layer)

    # 资产映射预处理
    asset_phrases = {}
    for asset, words in asset_map.items():
        for phrase in words:
            pt = tuple(_phrase_tokens(phrase))
            if pt:
                asset_phrases.setdefault(pt, []).append(asset)

    # 组合规则预处理（terms 为 list of list：每内层是可选词组，任一命中即该 term 命中）
    combo_rules = []
    for r in combos:
        combo_rules.append({
            "name": r["name"],
            "bonus": r["bonus"],
            "terms": r["terms"],
            "assets": r.get("assets", []),
        })

    black_toks = [tuple(_phrase_tokens(w)) for w in blacklist if _phrase_tokens(w)]

    # BM25 文档与 idf
    doc_tokens = {}
    for topic, words in docs.items():
        toks = []
        for w in words:
            toks.extend(_phrase_tokens(w))
        doc_tokens[topic] = toks
    N = len(doc_tokens)
    df = {}
    for toks in doc_tokens.values():
        for w in set(toks):
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log((N - d + 0.5) / (d + 0.5) + 1.0) for w, d in df.items()}
    avgdl = sum(len(t) for t in doc_tokens.values()) / N if N else 1.0

    return {
        "phrase_weight": phrase_weight,
        "layer_of": layer_of,
        "asset_phrases": asset_phrases,
        "combo_rules": combo_rules,
        "black_toks": black_toks,
        "doc_tokens": doc_tokens,
        "idf": idf,
        "avgdl": avgdl,
        "k1": 1.5,
        "b": 0.75,
        "weights": {"keyword": 0.6, "bm25": 0.4},
    }


# ---------------------------------------------------------------- 评分

def blacklisted(tokens: list, cfg: dict) -> bool:
    return any(_has_phrase(tokens, list(bt)) for bt in cfg["black_toks"])


def keyword_hits(tokens: list, cfg: dict):
    """返回 (hits, score)：hits = [(词条原文, 权重, 层名)]（同词去重，取最高权重）；score = Σ权重。"""
    seen = {}
    for pt, w in cfg["phrase_weight"].items():
        if _has_phrase(tokens, list(pt)):
            if pt not in seen or w > seen[pt][0]:
                seen[pt] = (w, cfg["layer_of"][pt])
    hits = [(" ".join(pt), w, layer) for pt, (w, layer) in seen.items()]
    return hits, sum(w for w, _ in seen.values())


def combo_bonus(tokens: list, cfg: dict):
    """返回 (combos, bonus, assets)：命中的组合名、加分和、组合声明的资产并集。
    每个 term 是可选词组列表，任一命中即该 term 命中；所有 term 命中才加分。"""
    hit_rules, bonus, hit_assets = [], 0, []
    for r in cfg["combo_rules"]:
        terms_ok = True
        for options in r["terms"]:
            if not any(_has_phrase(tokens, _phrase_tokens(o)) for o in options):
                terms_ok = False
                break
        if terms_ok:
            hit_rules.append(r["name"])
            bonus += r["bonus"]
            for a in r.get("assets", []):
                if a not in hit_assets:
                    hit_assets.append(a)
    return hit_rules, bonus, hit_assets


def assets_of(tokens: list, cfg: dict):
    """资产映射：命中词条对应的资产并集。"""
    assets = []
    for pt, asset_list in cfg["asset_phrases"].items():
        if _has_phrase(tokens, list(pt)):
            for a in asset_list:
                if a not in assets:
                    assets.append(a)
    return assets


def bm25_score(tokens: list, cfg: dict) -> float:
    """标题 vs 5 主题文档：对每文档算 BM25，取最高分。"""
    k1, b = cfg["k1"], cfg["b"]
    best = 0.0
    for toks in cfg["doc_tokens"].values():
        dl = len(toks)
        s = 0.0
        for w in set(tokens):
            tf = toks.count(w)
            if tf == 0 or w not in cfg["idf"]:
                continue
            s += cfg["idf"][w] * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / cfg["avgdl"]))
        best = max(best, s)
    return best


def score_one(title: str, cfg: dict) -> dict:
    """单条评分（无归一化，输出 raw 分与分解）。"""
    tokens, _ = clean_title(title)
    hits, kw_score = keyword_hits(tokens, cfg)
    combos, combo, combo_assets = combo_bonus(tokens, cfg)
    assets = assets_of(tokens, cfg)
    for a in combo_assets:
        if a not in assets:
            assets.append(a)
    bm25 = bm25_score(tokens, cfg)
    return {
        "title": title,
        "tokens": tokens,
        "hits": [h[0] for h in hits],
        "hit_weights": {h[0]: h[1] for h in hits},
        "layers": {h[0]: h[2] for h in hits},
        "combos": combos,
        "combo_bonus": combo,
        "assets": assets,
        "keyword_score": kw_score,
        "keyword_raw": kw_score + combo,
        "bm25_score": bm25,
        "blacklisted": blacklisted(tokens, cfg),
        "question": bool(title) and title.rstrip().endswith("?"),
    }


def _zscore(vals: list):
    n = len(vals)
    if n == 0:
        return []
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return [0.0] * n
    return [(v - mean) / std for v in vals]


def score_all(items: list, cfg: dict, thresholds: dict = None) -> list:
    """批量评分 + z-score 归一化 + 融合 + Tier 分层。
    items: [{"title": ...}, ...]；thresholds: {"tier1": x, "tier2": y}（None 时用当批分位数 P98/P85）。
    返回 [{...score_one, "score", "tier", "drop_blacklist"}]；blacklisted 条目标记 drop。"""
    results = []
    for it in items:
        r = score_one(it.get("title", ""), cfg)
        r.update({"score": None, "tier": None})
        results.append(r)

    valid = [r for r in results if not r["blacklisted"] and not r["question"]]
    kw_vals = [r["keyword_raw"] for r in valid]
    bm_vals = [r["bm25_score"] for r in valid]
    kw_z = _zscore(kw_vals)
    bm_z = _zscore(bm_vals)
    for r, kz, bz in zip(valid, kw_z, bm_z):
        r["score"] = cfg["weights"]["keyword"] * kz + cfg["weights"]["bm25"] * bz

    scores = sorted(r["score"] for r in valid if r["score"] is not None)
    if thresholds:
        t1, t2 = thresholds["tier1"], thresholds["tier2"]
    else:
        t2 = _percentile(scores, 85)
        t1 = _percentile(scores, 98)
    for r in valid:
        r["tier"] = 1 if r["score"] >= t1 else (2 if r["score"] >= t2 else 3)
    return results


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def filter_for_push(results: list, push_tiers=(1, 2)) -> list:
    """按推送策略过滤（默认 Tier1+2，不含 blacklisted）。"""
    return [r for r in results if not r["blacklisted"] and r["tier"] in push_tiers]

# -*- coding: utf-8 -*-
"""
贺兰有灵 · 轻量混合检索器（BM25，纯标准库实现）

设计原则（对照任务书 7.3 / P3 可复现性）：
- 零第三方依赖：仅使用 Python 标准库，保证在评审环境（任意 Python 3.8+）可复现。
- 中文分词：采用「单字 unigram + 相邻双字 bigram」组合，对中文检索鲁棒，
  无需 jieba 等外部分词器。
- BM25（Okapi）打分，支持多路召回（按 topic 过滤）与结果去重。
- 所有返回片段均携带 source_id / 来源 / 核验状态，供证据门使用。
"""
import json
import math
import os
import re
from collections import defaultdict

# V30.0：来源规范化与引用治理的唯一真源（内部资料不外显本机路径）
try:
    import citation_policy as _cp
except Exception:  # 极端情况下不阻断检索，退化为最保守的净化
    _cp = None


def _find_corpus():
    """向上搜索语料文件，规避跨包导入时的路径错位。"""
    base = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        for cand in (os.path.join(base, "corpus", "ningxia_knowledge.json"),
                     os.path.join(base, "ningxia_knowledge.json")):
            if os.path.exists(cand):
                return cand
        base = os.path.dirname(base)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus", "ningxia_knowledge.json")


CORPUS_PATH = _find_corpus()

# 中文标点/空白
_PUNCT = re.compile(r"[\s，。、；：？！“”‘’（）《》<>\[\]【】·…—,.;:?!\"'()/\-_=+*#@~`|\\%&$]+")


def tokenize(text: str):
    """中文友好的轻量分词：unigram + 字符 bigram。"""
    if not text:
        return []
    # 英文/数字按词切，中文按字切
    text = _PUNCT.sub(" ", text)
    tokens = []
    # 提取 ascii 词
    for m in re.findall(r"[a-zA-Z0-9]+", text):
        tokens.append(m.lower())
    # 去掉 ascii 后的纯中文串做字 + bigram
    cn = re.sub(r"[a-zA-Z0-9]+", "", text)
    cn = re.sub(r"\s+", "", cn)
    for ch in cn:
        tokens.append(ch)
    for i in range(len(cn) - 1):
        tokens.append(cn[i : i + 2])
    return [t for t in tokens if t]


class BM25Retriever:
    def __init__(self, corpus_path: str = CORPUS_PATH, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = []
        self.doc_tokens = []
        self.df = defaultdict(int)
        self.idf = {}
        self.avgdl = 0.0
        self.load(corpus_path)

    def load(self, corpus_path: str):
        with open(corpus_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.docs = data.get("entries", [])
        self.doc_tokens = [tokenize(self._doc_text(d)) for d in self.docs]
        n = len(self.doc_tokens)
        for toks in self.doc_tokens:
            seen = set(toks)
            for t in seen:
                self.df[t] += 1
        for t, df in self.df.items():
            # Okapi idf，避免负无穷
            self.idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        lengths = [len(t) for t in self.doc_tokens]
        self.avgdl = sum(lengths) / n if lengths else 0.0

    @staticmethod
    def _doc_text(d: dict) -> str:
        parts = [d.get("title", ""), d.get("content", "")]
        parts += d.get("tags", [])
        return " ".join(parts)

    def search(self, query: str, top_k: int = 5, topic: str = None, min_idf_score: float = 0.0):
        """
        返回排序后的检索结果列表。
        每个结果: {id, title, topic, category, content, tags, source, authority,
                  verified, score, matched_terms}
        """
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        results = []
        for idx, (doc, toks) in enumerate(zip(self.docs, self.doc_tokens)):
            if topic and doc.get("topic") != topic:
                continue
            tf = defaultdict(int)
            for t in toks:
                tf[t] += 1
            dl = len(toks)
            score = 0.0
            matched = []
            for qt in q_tokens:
                if qt in tf:
                    f = tf[qt]
                    idf = self.idf.get(qt, 0.0)
                    score += idf * (f * (self.k1 + 1)) / (
                        f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                    )
                    if qt not in matched:
                        matched.append(qt)
            if score > min_idf_score:
                d = self.docs[idx]
                src = d.get("source", {})
                # V30.0 P0-1/P0-2/P0-3：来源必须经统一策略规范化后才可外传。
                # 本机绝对路径到此为止，不再出现在 source_url 中。
                if _cp is not None:
                    norm = _cp.normalize_source(
                        src, entry_id=d.get("id"),
                        verified=d.get("verified"),
                        claim_status=d.get("claim_status"))
                    out = norm
                    content = _cp.scrub_local_paths(d.get("content"))
                    title = _cp.scrub_local_paths(d.get("title"))
                else:
                    out = {"source_name": src.get("name") if isinstance(src, dict) else str(src),
                           "source_url": None, "publisher": "", "source_ref": None,
                           "source_visibility": "internal", "external_status": "unverified",
                           "evidence_level": None, "verified": False, "retrieved_at": None,
                           "claim_status": "unverified", "citation_note": "来源待核实",
                           "path_redacted": False, "source_display": ""}
                    content = d.get("content")
                    title = d.get("title")
                results.append(
                    {
                        "id": d.get("id"),
                        "title": title,
                        "topic": d.get("topic"),
                        "category": d.get("category"),
                        "content": content,
                        "tags": d.get("tags", []),
                        "source_name": out.get("source_name"),
                        "source_url": out.get("source_url"),
                        "source_ref": out.get("source_ref"),
                        "source_publisher": out.get("publisher"),
                        "source_visibility": out.get("source_visibility"),
                        "external_status": out.get("external_status"),
                        "evidence_level": out.get("evidence_level"),
                        "retrieved_at": out.get("retrieved_at"),
                        "authority": d.get("authority"),
                        "verified": out.get("verified", False),
                        "claim_status": out.get("claim_status"),
                        "citation_note": out.get("citation_note"),
                        "path_redacted": out.get("path_redacted"),
                        "score": round(score, 4),
                        "matched_terms": matched,
                    }
                )
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]


if __name__ == "__main__":
    r = BM25Retriever()
    for q in ["贺兰山岩羊有哪些生态特点", "贺兰山岩画的历史文化", "西夏陵是世界遗产吗", "沙坡头门票多少钱"]:
        print("\n=== 查询:", q, "===")
        for hit in r.search(q, top_k=3):
            print(f"  [{hit['score']}] {hit['title']} | 核验:{hit['verified']} | 来源:{hit['source_name']}")

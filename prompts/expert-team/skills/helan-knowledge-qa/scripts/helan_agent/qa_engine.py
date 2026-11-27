# -*- coding: utf-8 -*-
"""
贺兰有灵 · 文化/生态可信问答编排引擎（MVP-A）

职责（对照任务书 5.1 / 6 Agent 1&2&4）：
1. 识别查询主题（ecology / culture / travel / general）。
2. 调用 BM25 检索，召回 TOP-K 证据片段（保留 source/verified/evidence_level）。
3. 经三层证据门评估（grounding / evidence / realtime）。
4. 生成「带引用、标注不确定性、动态信息标待核实」的可溯源回答草稿。
5. 返回结构化结果，供 WorkBuddy Skill / 测试 runner 消费。

注：本引擎的回答严格基于检索证据组合而成，不依赖外部大模型，
保证可复现、可测试、来源可追溯。上层模型可在此结构基础上润色，
但不得脱离证据凭空添加事实。
"""
import json
from retriever import BM25Retriever
from evidence_gate import evaluate as evidence_evaluate

# V30.0：引用治理与事实发布策略（与检索器、证据门、运行时共用同一真源）
try:
    import citation_policy as _cp
except Exception:
    _cp = None

TOPIC_KEYWORDS = {
    "ecology": ["岩羊", "生态", "动物", "植被", "治沙", "沙坡头", "贺兰山生态", "保护区", "物种", "环境"],
    "culture": ["岩画", "西夏", "历史", "文化", "遗址", "博物馆", "王朝", "文物", "遗产", "艺术", "崇拜"],
    "travel": ["门票", "开放", "怎么去", "行程", "攻略", "景点", "玩", "游览", "票价", "预约"],
}


def detect_topic(query: str) -> str:
    scores = {k: 0 for k in TOPIC_KEYWORDS}
    for topic, kws in TOPIC_KEYWORDS.items():
        for kw in kws:
            if kw in query:
                scores[topic] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def _build_grounded_answer(query, evidence, gate):
    """基于检索证据组合可溯源回答；动态/争议信息明确标注。"""
    if gate["grounding"] == "insufficient_evidence" or not evidence:
        return (
            "抱歉，我在当前知识库中未能检索到关于该问题的可靠依据，"
            "因此无法给出确定性回答。为避免误导，建议您通过官方文旅渠道或景区公告进一步核实。"
        )

    lines = []
    conf = gate["confidence"]
    if conf == "high":
        intro = "根据已核验的公开权威资料，"
    elif conf == "medium":
        intro = "根据现有资料（部分待官方复核），"
    else:
        intro = "根据现有资料（依据有限或存在争议），以下信息仅供参考、需进一步核实："
    lines.append(intro)

    for i, e in enumerate(evidence, 1):
        content = e["content"]
        if gate["realtime_flag"] and e.get("evidence_level") in (None,):
            pass
        # V30.0 P0-4：逐条标注可信度（争议来源 / 内部资料 / 外部未核实），
        # 文案由 citation_policy 统一给出，避免各入口各写一套。
        note = e.get("citation_note") or ""
        if note:
            content += "（注：%s。）" % note
        lines.append(f"[{i}] {content}")
        lines.append(f"    来源：{e.get('source_name')}（核验状态：{'已核验' if e.get('verified') else '待核实'}）")

    # 动态信息统一尾注
    if gate["realtime_flag"]:
        lines.append(
            "【动态信息提示】本回答中涉及的门票、开放时间、交通等动态信息，"
            "请以景区或文旅部门最新公告为准，以上数值为咨询时参考、标注「待核实」。"
        )
    if gate["conflict_detected"]:
        lines.append(
            "【证据差异提示】不同资料对上述某些数值/年代表述存在差异，本回答未做强行统一，"
            "具体以官方最新公布为准。"
        )
    # V30.0 P0-4：显式给出「本条回答能否作为事实断言」的结论，不藏在正文里
    pol = gate.get("claim_policy") or {}
    if pol and not pol.get("claim_assertable", True):
        lines.append("【可信度提示】本回答不作确定性断言：%s" %
                     ("；".join(pol.get("reasons") or []) or "证据有限"))
    return "\n".join(lines)


def answer(query: str, top_k: int = 5):
    """主入口：返回完整结构化结果。"""
    retriever = BM25Retriever()
    topic = detect_topic(query)
    # 先按主题检索；若结果不足，放宽到全库
    evidence = retriever.search(query, top_k=top_k, topic=topic if topic != "general" else None)
    if len(evidence) < 2:
        evidence = retriever.search(query, top_k=top_k)
    gate = evidence_evaluate(evidence, query)
    grounded = _build_grounded_answer(query, evidence, gate)
    if _cp is not None:
        citations = [_cp.normalize_citation(e, i + 1) for i, e in enumerate(evidence)]
        claim_policy = _cp.build_claim_policy(gate, citations)
    else:
        citations = [
            {
                "index": i + 1,
                "id": e["id"],
                "title": e["title"],
                "source_name": e.get("source_name"),
                "source_url": None,          # 无策略层时不外显任何链接，避免路径泄露
                "source_ref": e.get("source_ref"),
                "publisher": e.get("source_publisher"),
                "evidence_level": e.get("evidence_level"),
                "verified": e.get("verified"),
                "retrieved_at": e.get("retrieved_at"),
                "claim_status": e.get("claim_status"),
                "citation_note": e.get("citation_note"),
            }
            for i, e in enumerate(evidence)
        ]
        claim_policy = gate.get("claim_policy") or {}
    gate["claim_policy"] = claim_policy
    return {
        "query": query,
        "detected_topic": topic,
        "gate": gate,
        "evidence": evidence,
        "grounded_answer": grounded,
        "citations": citations,
        "claim_policy": claim_policy,
        "evidence_count": len(evidence),
    }


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "贺兰山岩羊有哪些生态特点"
    res = answer(q)
    print(json.dumps(res, ensure_ascii=False, indent=2))

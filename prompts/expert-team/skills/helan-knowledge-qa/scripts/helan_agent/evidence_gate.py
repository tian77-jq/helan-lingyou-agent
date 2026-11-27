# -*- coding: utf-8 -*-
"""
贺兰有灵 · 三层证据门（对照任务书 7.4）

Layer 1 Knowledge Grounding：回答必须有检索依据，无依据或依据过弱 → 标记 insufficient_evidence。
Layer 2 Evidence Gate：依据不足 / 来源冲突 / 来源不可信 → 降级或拒绝确定性表述。
Layer 3 Real-time Gate：涉及门票、开放时间、交通、天气、客流等动态信息时，
                        若来源非近期权威且明确标注动态，则必须标注「待核实」，不得冒充实时数据。

审核 Agent 也可能出错，因此本门只做规则化校验并给出「建议」，最终由 Verifier Agent 裁决。
"""
import re

# V30.0：事实发布策略与来源治理共用同一真源
try:
    import citation_policy as _cp
except Exception:
    _cp = None

# 动态信息关键词（Real-time Gate 触发）
REALTIME_KEYWORDS = [
    "门票", "票价", "多少钱", "价格", "开放时间", "营业时间", "营业状态",
    "天气", "气温", "温度", "交通", "怎么去", "多久", "班次", "客流", "人多吗",
    "预约", "需要预约", "是否开放", "开了吗",
]

# 高可信来源等级
HIGH_TRUST_LEVELS = {"official", "official_media"}
# 争议/二次来源
LOW_TRUST_LEVELS = {"secondary", "project_internal"}


def detect_realtime_intent(query: str) -> bool:
    return any(k in query for k in REALTIME_KEYWORDS)


def evaluate(evidence_list: list, query: str) -> dict:
    """
    输入：检索到的 evidence 列表 + 用户查询
    输出：证据门裁决 dict
    """
    verdict = {
        "grounding": "unknown",
        "evidence_level": "none",
        "conflict_detected": False,
        "realtime_flag": False,
        "realtime_items": [],
        "warnings": [],
        "confidence": "low",
        "can_assert": False,
    }

    # ---- Layer 1: Knowledge Grounding ----
    if not evidence_list:
        verdict["grounding"] = "insufficient_evidence"
        verdict["warnings"].append("未检索到任何知识依据，不得给出确定性结论。")
        if _cp is not None:
            verdict["claim_policy"] = _cp.build_claim_policy(verdict, [])
        return verdict
    verdict["grounding"] = "grounded"

    # ---- 依据强度 ----
    levels = [e.get("evidence_level") for e in evidence_list]
    if any(lv in HIGH_TRUST_LEVELS for lv in levels):
        verdict["evidence_level"] = "high"
        verdict["confidence"] = "high"
    elif any(lv == "project_internal" for lv in levels) and not any(
        lv in LOW_TRUST_LEVELS and lv != "project_internal" for lv in levels
    ):
        verdict["evidence_level"] = "project_internal"
        verdict["confidence"] = "medium"
        verdict["warnings"].append("部分依据来自项目内部资料，待官方复核，结论应标注为待核实。")
    elif any(lv == "secondary" for lv in levels):
        verdict["evidence_level"] = "secondary"
        verdict["confidence"] = "medium"
        verdict["warnings"].append("依据含二次/争议来源，结论应标注不确定性。")
    else:
        verdict["evidence_level"] = "unknown"

    # ---- 依据冲突检测（同主题不同数值/结论）----
    # 简化：检测 evidence 中是否显式含「差异」「争议」「不一致」「存在分歧」「尚无定论」等
    conflict_markers = ["争议", "不一致", "分歧", "尚无定论", "无定论", "未有定论", "差异",
                        "不同说法", "不同口径"]
    for e in evidence_list:
        txt = (e.get("content") or "") + (e.get("title") or "")
        # V30.0：条目自带 claim_status=contested 时同样视为争议（防止文案改写后标记失效）
        if e.get("claim_status") == "contested" or any(m in txt for m in conflict_markers):
            verdict["conflict_detected"] = True
            break
    if verdict["conflict_detected"]:
        verdict["warnings"].append("检索证据中存在争议/差异表述，不得强行拼接为确定结论。")
        verdict["confidence"] = "low"

    # ---- Layer 3: Real-time Gate ----
    if detect_realtime_intent(query):
        verdict["realtime_flag"] = True
        # 提取动态项：看 query 命中的关键词
        hit = [k for k in REALTIME_KEYWORDS if k in query]
        verdict["realtime_items"] = hit
        verdict["warnings"].append(
            "查询涉及动态信息（%s），此类信息需以景区/官方最新公告核验，本回答中相关数值标注为「待核实」。"
            % "、".join(hit)
        )
        verdict["confidence"] = "low" if verdict["confidence"] != "low" else "low"

    # ---- 最终可否断言 ----
    if verdict["grounding"] == "grounded" and not verdict["conflict_detected"]:
        if verdict["realtime_flag"]:
            verdict["can_assert"] = False  # 动态信息即使有依据也需标注待核实
        else:
            verdict["can_assert"] = verdict["confidence"] in ("high", "medium")
    else:
        verdict["can_assert"] = False

    # ---- V30.0 P0-4：四层可区分的事实发布策略 ----
    # 门禁/流程 PASS ≠ 文化证据充分 ≠ 具体事实可断言 ≠ 行程可展示。
    # 此处只给出「文化证据/事实」两层，流程与行程由 gates/A5/runtime 分别给出。
    if _cp is not None:
        verdict["claim_policy"] = _cp.build_claim_policy(verdict, evidence_list)

    return verdict

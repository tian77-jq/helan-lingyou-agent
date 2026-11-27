# -*- coding: utf-8 -*-
"""
三层门禁（冻结版 v1.0）

对照任务书「六、质量与验收」：Execution Gate / Business Contract Gate / Release Gate。
全部为确定性规则校验，不依赖模型，保证可复现、不伪造业务成功。

- Execution Gate：检查各 Agent 执行状态（成功/失败/超时）、输入契约合法、工具调用是否真的发生。
- Business Contract Gate：检查用户硬性约束是否被满足（人数、预算、天数、强度、兴趣覆盖）。
- Release Gate：确定性最终发布校验。必须 Execution + Business 双 PASS 且 Verifier PASS，
  且不得把「待核实」的动态数据当作已证实事实对外发布，才允许把业务标记为完成。
"""


def execution_gate(state: dict) -> dict:
    """检查执行层：Agent 状态与工具调用真实性。"""
    issues = []
    agent_status = state.get("agent_status", {})
    required = state.get("_required_agents", [])
    for a in required:
        st = agent_status.get(a, {})
        if not st:
            issues.append(f"[{a}] 缺失执行记录")
            continue
        if st.get("status") == "FAIL":
            issues.append(f"[{a}] 执行失败：{st.get('error', '未知')}")
        elif st.get("status") == "TIMEOUT":
            issues.append(f"[{a}] 执行超时")
        # 工具真实性：凡声明调用了工具，必须有 tool_calls 痕迹
        if st.get("tool_called") and not st.get("tool_evidence"):
            issues.append(f"[{a}] 声明调用工具但无工具执行证据（疑似伪造）")
    return {"pass": len(issues) == 0, "gate": "ExecutionGate", "issues": issues}


def business_contract_gate(state: dict) -> dict:
    """检查业务契约：用户硬性约束满足情况。

    非行程类任务（纯文化/生态问答，A3 未参与且无既有行程）不适用行程业务契约，
    否则会因「未识别出游人数/天数/预算」而错误阻断正常的知识问答。

    V31.3 P0-2：新增**行程完整性**校验（此前缺失，真实缺陷：3 天行程第 3 天
    items=[] 空白日，Business/Release 仍判 True 可发布）：
      · days_plan 长度必须等于行程天数；
      · 每天至少 1 个景点（空白日一律判不通过，不得用「自由活动」话术掩盖）。
    """
    issues = []
    c = state.get("user_constraints", {})
    itin = state.get("itinerary", {})
    required = state.get("_required_agents", []) or []
    if "A3" not in required and not itin.get("days_plan"):
        return {"pass": True, "gate": "BusinessContractGate",
                "issues": [], "note": "非行程类任务，跳过行程契约校验"}
    # 1) 约束完整性
    # 说明：人数(party_size)在用户未明示时不做硬性阻断（真实查询常省略），
    #       由测试 runner 的逐例断言单独校验；天数与预算为核心硬约束。
    if c.get("days") is None:
        issues.append("未识别出游天数")
    if c.get("budget_yuan") is None:
        issues.append("未识别预算")
    # 2) 行程天数与约束一致
    if itin.get("days") and c.get("days") and itin["days"] != c["days"]:
        issues.append(f"行程天数({itin.get('days')})与约束({c.get('days')})不一致")
    # 2.5) V31.3 P0-2：行程完整性（空白日不得发布）
    dp = itin.get("days_plan") or []
    if dp:
        if itin.get("days") and len(dp) != itin["days"]:
            issues.append(f"日程数({len(dp)})与行程天数({itin.get('days')})不一致")
        blank_days = [d.get("day") for d in dp if not d.get("items")]
        if blank_days:
            issues.append(f"第{blank_days}天无任何景点（空白日），不得发布")
    # 3) 兴趣/需求覆盖（住宿/交通由 A4 旅行数据 Agent 经外部服务满足，不计入行程覆盖缺失）
    interests = set(c.get("interests", []))
    reqs = set(c.get("requirements", []))
    covered = set(itin.get("covered_interests", []))
    external_reqs = {"住宿", "交通"}  # 由 A4 提供，非行程景点覆盖项
    missing = (interests | reqs) - covered - external_reqs
    if missing:
        issues.append(f"未覆盖的兴趣/需求：{sorted(missing)}")
    # 4) 强度约束（不要太累 -> 日均景点<=2）
    if c.get("intensity") == "low":
        for d in itin.get("days_plan", []):
            if len(d.get("items", [])) > 2:
                issues.append(f"第{d.get('day')}天景点数={len(d.get('items',[]))} 超过轻松上限2")
                break
    return {"pass": len(issues) == 0, "gate": "BusinessContractGate", "issues": issues}


def release_gate(state: dict, verifier_verdict: str = None) -> dict:
    """确定性最终发布校验。"""
    eg = execution_gate(state)
    bg = business_contract_gate(state)
    issues = []
    # Verifier 必须通过
    if verifier_verdict != "PASS":
        issues.append(f"Verifier 未通过（verdict={verifier_verdict}）")
    # 禁止把待核实动态数据当事实发布
    realtime = state.get("realtime_data", {})
    asserted_fact = state.get("_asserted_dynamic_as_fact", False)
    if asserted_fact:
        issues.append("检测到把「待核实」动态数据当作已证实事实发布")
    # 实时数据若被引用，必须带来源与时间戳（weather 为 {城市: {...}} 嵌套结构）
    for k, v in realtime.items():
        if k == "weather" and isinstance(v, dict):
            for city, w in v.items():
                if isinstance(w, dict) and not w.get("source"):
                    issues.append(f"实时数据 weather.{city} 缺少来源标注")
        elif isinstance(v, dict) and not v.get("source"):
            issues.append(f"实时数据 {k} 缺少来源标注")
    passes = eg["pass"] and bg["pass"] and verifier_verdict == "PASS" and not asserted_fact and not issues
    return {
        "pass": passes,
        "gate": "ReleaseGate",
        "execution": eg,
        "business": bg,
        "issues": issues,
    }


def evaluate_all(state: dict, required_agents, verifier_verdict) -> dict:
    state = dict(state)
    state["_required_agents"] = required_agents
    gates = {
        "execution": execution_gate(state),
        "business": business_contract_gate(state),
    }
    rel = release_gate(state, verifier_verdict)
    gates["release"] = rel
    return gates


# ----------------------------------------------------------------------------
# 独立发布就绪评估（证据驱动，与三层结构门禁正交）
#
# 三层门禁（Execution / Business / Release）只判断「流程与结构是否合规」，
# 不判断「当前结果是否具备足够真实证据可发布为可执行行程」。两者可同时成立
# （结构合规但证据不足），故发布就绪状态独立于 release_pass，绝不覆盖任何
# 结构门禁结论，也不被 A5 PASS 反向覆盖。
#
# 枚举：
#   NOT_READY   —— 存在关键证据缺口（预算上限超用户预算且无可靠价格来源；或
#                  多城行程但跨城路线/交通未经验证）。参考行程可展示，但须显式
#                  标记为 DRAFT / PRELIMINARY，不得声称预算与交通已核验。
#   CONDITIONAL —— 结构完整、可展示为参考行程，但仍有待核实项（平台侧数据源未
#                  授权、天气仅部分覆盖等），须显式标记验证状态。
#   READY       —— 关键证据齐备（地图已授权、预算在限内且有可靠来源、跨城交通
#                  已验证），可发布为可执行行程。
# ----------------------------------------------------------------------------
PUBLISH_READY_STATES = ("READY", "CONDITIONAL", "NOT_READY")


def evaluate_publish_ready(plan: dict = None, a4: dict = None,
                           constraints: dict = None) -> dict:
    """证据驱动的独立发布就绪评估。与三层结构门禁正交，互不影响。

    注意：本函数只读行程/旅行数据，**不修改**任何门禁结论。
    """
    plan = plan or {}
    a4 = a4 or {}
    constraints = constraints or {}
    critical = []
    advisory = []

    # 行程涉及城市（供预算/跨城交通证据共用，提前提取）
    used_cities = plan.get("used_cities") or []

    # 1) 预算证据：上限超用户预算且无可信实时价格来源 → 不得断言预算已核验
    be = plan.get("budget_estimate") or {}
    be_realtime = be.get("is_realtime") is True
    limit = be.get("budget_limit") or constraints.get("budget_yuan")
    hi = be.get("total_estimate_high_yuan")
    within = be.get("within_limit_estimate")
    if limit and hi is not None and hi > limit and not be_realtime:
        critical.append(
            "预算估算上限(%d元)已超过用户预算(%d元)且无可靠实时价格来源，预算未核验"
            % (hi, limit))
    elif within is False and not be_realtime:
        critical.append(
            "预算估算下限已超过用户预算且无可靠实时价格来源，预算未核验")
    # 1b) 跨城行程下，若成本估算无任何可靠实时价格来源（同程等平台侧连接器未授权
    #     Web 后端），则住宿/门票/餐饮/交通成本均无法核验。即便地图已接通（item 2
    #     不再致命），也不得据此把 publish_ready 升为 READY/CONDITIONAL——预算证据
    #     不足时必须保持 NOT_READY（用户 §五 硬性要求）。
    #     V33 §A3：关键价格缺口以 A4 的 prices/transit **实际状态**为准——只有当
    #     住宿报价、门票报价、跨城交通票价三者均为 LIVE 时缺口才降为提示；餐饮消费
    #     无任何授权源，恒为提示项。当前授权下同程=BLOCKED_MERCHANT_AUTH、
    #     酒店/门票=UNAVAILABLE → 维持 NOT_READY（不因「同程/公交接通」升级）。
    if len(used_cities) >= 2 and be and not be_realtime:
        price_rows = [p for p in (a4.get("prices") or []) if isinstance(p, dict)]
        transit_rows = [t for t in (a4.get("transit") or []) if isinstance(t, dict)]
        hotel_live = any(p.get("entity_type") == "hotel" and p.get("status") == "LIVE"
                         for p in price_rows)
        ticket_live = any(p.get("entity_type") == "ticket" and p.get("status") == "LIVE"
                          for p in price_rows)
        fare_live = any(t.get("status") == "LIVE" and t.get("fare") for t in transit_rows)
        if hotel_live and ticket_live and fare_live:
            advisory.append(
                "住宿/门票/跨城交通票价已有 LIVE 来源，但餐饮消费仍无授权实时来源，"
                "预算证据部分核验（不得声称全项已核验）")
        else:
            gaps = []
            if not hotel_live:
                gaps.append("住宿价格")
            if not ticket_live:
                gaps.append("门票价格")
            if not fare_live:
                gaps.append("跨城交通票价")
            critical.append(
                "跨城行程的%s缺乏可靠实时价格来源"
                "（同程等平台侧连接器未授权核验），预算与交通成本未核验"
                % "、".join(gaps))

    # 2) 跨城路线/交通证据：多城行程但无真实交通数据 → 不得声称路线已验证可行
    transport = a4.get("transport") or {}
    maps = a4.get("maps") or {}
    if isinstance(transport, dict):
        has_realtime_transport = any(
            isinstance(v, dict) and v.get("source") == "realtime"
            for v in transport.values())
    else:
        has_realtime_transport = False
    if len(used_cities) >= 2:
        if not maps.get("available") and not has_realtime_transport:
            critical.append(
                "地图/跨城交通未授权，跨城路线可行性未验证（不得声称路线已验证可行）")
        elif not maps.get("available") and has_realtime_transport:
            advisory.append("跨城交通已有部分真实数据，但地图路线未完成授权核验，仍为参考")

    # 3) 平台侧数据源（同程等）未授 Web 后端 → 待核实（不致命）
    platform_only = a4.get("platform_only_services") or []
    if platform_only:
        advisory.append(
            "住宿/消费依赖平台侧连接器（%s），Web 后端未授权核验"
            % "、".join([s for s in platform_only if s][:2]))

    # 4) 天气覆盖：跨城行程下任一城市天气缺失 → 提示待核实（不致命）
    weather = a4.get("weather") or {}
    if isinstance(weather, dict) and used_cities:
        miss = [c for c in used_cities if not weather.get(c, {}).get("ok")]
        if miss:
            advisory.append("以下城市天气未取到（待核实）：%s" % "、".join(miss))

    if critical:
        state = "NOT_READY"
    elif advisory:
        state = "CONDITIONAL"
    else:
        state = "READY"

    return {
        "state": state,
        "critical_gaps": critical,
        "advisory": advisory,
        "note": (
            "参考行程可展示，但须显式标记为未达发布就绪（DRAFT / PRELIMINARY），"
            "不得声称预算与交通已核验。"
            if state == "NOT_READY" else
            "可作为参考行程展示，但仍有待核实项，须显式标记验证状态。"
            if state == "CONDITIONAL" else
            "关键证据齐备，可发布为可执行行程。"),
    }

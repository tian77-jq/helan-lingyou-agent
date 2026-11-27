# -*- coding: utf-8 -*-
"""统一 Agent 运行时接口 AgentRuntime（V26.0 §四）。

**一个业务核心，两个入口。**
  · 入口一：Web（web/app.py，FastAPI）        -> 本模块 AgentRuntime
  · 入口二：WorkBuddy 复现（mcp-adapter/server.py 本地 stdio MCP）-> 本模块 AgentRuntime
  · 入口三（诊断）：CLI（cli.py / engine.py）  -> 同一 Orchestrator

三者共用**同一份** Orchestrator / agents / contracts / gates / state_store 代码，
因此「同一业务逻辑、同一版本、同一验收场景」；不存在「Web 一套模拟结果、
WorkBuddy 另一套演示脚本」。

--------------------------------------------------------------------
§四 要求严格区分的三类执行（不得混淆，不得把第 3 类伪装为前两类）：

  kind_1_workbuddy_native   平台原生专家成员实际调度
                            （WorkBuddy 主理人创建团队、按 DAG 派发、成员回传结果）
  kind_2_model_agents       自有多个模型 Agent 独立推理、各自调用工具并交换结果
  kind_3_deterministic      普通 Python 确定性业务模块（A4 内含真实外部 HTTP 工具调用）

本仓库**当前真实状态 = kind_3**（A4 为 tool_only，含真实 open-meteo HTTP 调用与留痕）。
kind_2 提供**最小可验证方案**：domain/helan_agent/helan_llm 适配层，配置凭证后
A1/A2/A3 执行真实模型调用并留存 model_call 证据；模型只允许改写表述，不得新增
无来源事实、不得改动约束/行程/预算/审核结论（A5 永远确定性，防模型自证）。
kind_1 需要 WorkBuddy 平台侧人工操作（添加 MCP Server / 专家团安装），**当前 PENDING**，
本模块不代为声明已完成。
--------------------------------------------------------------------
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from engine import Session
from contracts import DAG_SPEC, check_dag, MESSAGE_REQUIRED_FIELDS
import trace as _trace

try:
    import helan_llm as _LLM
except Exception:  # 适配层缺失不得中断运行时
    _LLM = None

# V30.0：来源规范化与事实发布策略（与检索器/证据门/问答引擎共用同一真源）
try:
    import citation_policy as _CP
except Exception:
    _CP = None

# V31.3 §6：景点/美食真实匹配图片适配层（provider 状态如实上报；
# 缺失/失败不得破坏行程——enrich 内部全捕获，最坏原样返回）
try:
    import image_service as _IMS
except Exception:
    _IMS = None

# V32.2 §三：腾讯位置服务适配层（密钥仅来自环境变量，未授权时如实 NEEDS_AUTH）
try:
    import tencent_maps as _TM
except Exception:
    _TM = None

# V33 §A2：出行价格与交通数据契约层（同程=BLOCKED_MERCHANT_AUTH 诚实降级；
# 腾讯公交/BRT/火车真实接入；任何失败不外抛）
try:
    import travel_prices as _TP
except Exception:
    _TP = None


def _maps_capability() -> dict:
    """地图服务实测状态（与 image_providers 同构，供 capability 如实上报）。"""
    if _TM is None:
        return {"component": "tencent_maps", "maps_available": False,
                "reason": "适配器未随本次部署打包"}
    try:
        grp = _TM.build_adapter()
        st = grp.get("status", {}) or {}
        if st.get("state") == _TM.STATUS_READY:
            return {"component": "tencent_maps", "maps_available": True,
                    "authorized": True, "provider": "tencent", "coord_system": "GCJ-02",
                    "note": "已授权腾讯位置服务 Key，可提供真实 POI/路线/距离/餐厅数据"}
        return {"component": "tencent_maps", "maps_available": False,
                "authorized": False, "coord_system": st.get("coord_system"),
                "reason": st.get("reason", "腾讯位置服务未授权")}
    except Exception as e:
        return {"component": "tencent_maps", "maps_available": False,
                "reason": "适配器初始化失败：%s" % str(e)[:120]}

# 三类执行的定义（供两入口一致输出，避免口头混淆）
EXECUTION_KINDS = {
    "kind_1_workbuddy_native": "WorkBuddy 平台原生专家成员实际调度（需平台侧配置，当前 PENDING）",
    "kind_2_model_agents": "自有多个模型 Agent 独立推理/调用工具/交换结果（配置凭证后启用最小可验证方案）",
    "kind_3_deterministic": "普通 Python 确定性业务模块（A4 含真实外部 HTTP 工具调用）",
}


def _first_val(d):
    if isinstance(d, dict) and d:
        return next(iter(d.values()))
    return d


def _transport_note(v: Any) -> str:
    """把 A4 的 transport 结构化对象收敛为**字符串**。

    V32.2 接通腾讯地图后，A4.transport 变成含 routes/source/coord_system 的对象；
    `_first_val()` 可能把整个对象返回给前端，导致 React 渲染对象报错（白屏）。
    这里只输出可读摘要，完整结构仍保留在 raw results / /api/trace 中。
    """
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for key in ("note", "summary", "text", "advisory"):
            s = v.get(key)
            if isinstance(s, str) and s.strip():
                return s
        routes = v.get("routes")
        if isinstance(routes, list) and routes:
            parts = []
            for r in routes[:3]:
                if not isinstance(r, dict):
                    continue
                seg = []
                dist = r.get("distance_m")
                if isinstance(dist, (int, float)):
                    seg.append("%.1f km" % (dist / 1000.0))
                dur = r.get("duration_text") or r.get("duration_s")
                if isinstance(dur, (int, float)):
                    seg.append("%d 分钟" % round(dur / 60))
                elif isinstance(dur, str) and dur:
                    seg.append(dur)
                if seg:
                    parts.append(" / ".join(seg))
            if parts:
                source = str(v.get("source") or "")
                label = "腾讯地图实时路线" if "tencent" in source.lower() else "实时路线"
                suffix = "（%s）" % v.get("coord_system") if v.get("coord_system") else ""
                return "%s：%s%s" % (label, "；".join(parts), suffix)
        mode = v.get("mode")
        source = v.get("source")
        if mode or source:
            return "跨城交通：%s%s" % (
                mode or "路线",
                ("（%s）" % source) if source else "",
            )
    return "跨城交通信息见地图详情（待核实）"


def _slim_qa(qa):
    if not qa:
        return None
    gate = qa.get("gate") or {}
    policy = qa.get("claim_policy") or gate.get("claim_policy") or {}
    citations = qa.get("citations", [])
    if _CP is not None:
        # 双保险：即使上游漏做规范化，出口再收敛一次（本机路径不得离开本层）
        citations = [_CP.normalize_citation(c, c.get("index") or i + 1)
                     for i, c in enumerate(citations)]
    return {
        "answer": qa.get("answer"),
        "evidence_count": qa.get("evidence_count"),
        "citations": citations,
        "gate": gate,
        "claim_policy": policy,
        "assertion_level": policy.get("assertion_level", "unassertable"),
        "claim_assertable": bool(policy.get("claim_assertable")),
        "disclosure": policy.get("disclosure", ""),
        "execution_mode": qa.get("execution_mode"),
        "topic": qa.get("topic"),
    }


class AgentRuntime:
    """统一 Agent 运行时：会话生命周期 + 执行 + 视图收敛 + 内容创作 + 轨迹导出。

    所有方法都是「同一业务核心」的薄封装，不含任何与入口绑定的逻辑，
    因此 Web 与 WorkBuddy 复现入口结果语义必然一致。
    """

    def __init__(self, persist_dir: Optional[str] = None):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self.persist_dir = persist_dir

    # ----------------------------------------------------------- 生命周期
    def create_session(self, session_id: str) -> Dict[str, Any]:
        sess = Session(session_id=session_id, persist_dir=self.persist_dir)
        self._sessions[session_id] = {"session": sess, "last": None}
        return self._sessions[session_id]

    def reset(self, session_id: str) -> Dict[str, Any]:
        return self.create_session(session_id)

    def get(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self._sessions:
            return self.create_session(session_id)
        return self._sessions[session_id]

    def session_ids(self) -> List[str]:
        return sorted(self._sessions.keys())

    # ----------------------------------------------------------- 能力自证
    def capability(self) -> Dict[str, Any]:
        """当前入口可用的真实执行能力（如实，不夸大）。"""
        if _LLM is None:
            mode, reason = "deterministic", "适配层未随本次部署打包"
            detail = {}
        else:
            d = _LLM.resolve_execution_mode()
            mode, reason = d.get("execution_mode", "deterministic"), d.get("reason", "")
            detail = d
        return {
            "component": "helan_agent_runtime",
            "entry_agnostic": True,
            "execution_mode": mode,
            "reason": reason,
            "model_adapter_available": _LLM is not None,
            "credentialed": mode == "model",
            "llm_detail": detail,
            "execution_kinds": EXECUTION_KINDS,
            "current_implementation": "kind_3_deterministic"
            if mode != "model" else "kind_3_deterministic + kind_2_model_agents(A1/A2/A3 表述层)",
            "kind_1_workbuddy_native": "PENDING（需 WorkBuddy 平台侧添加 MCP Server / 专家团安装）",
            "dag": DAG_SPEC,
            "dag_check": check_dag(),
            "message_required_fields": MESSAGE_REQUIRED_FIELDS,
            # V31.3 §6：图片来源实测状态（未授权=NEEDS_AUTH、本机不可达=
            # NETWORK_BLOCKED、语料无图=LOCAL_MISSING；不得据此外宣已接入图库）
            "image_providers": (_IMS.get_image_service().capability()
                                if _IMS is not None else
                                {"component": "image_service",
                                 "image_available": False,
                                 "reason": "适配层未随本次部署打包"}),
            # V32.2 §三：地图服务实测状态（已授权=READY、未授权=NEEDS_AUTH；
            # 不得据此外宣已接入实时地图）
            "maps_providers": _maps_capability(),
            # V33 §A2：出行价格/交通能力矩阵（同程=BLOCKED_MERCHANT_AUTH；
            # 腾讯公交=LIVE；酒店/门票/火车航班/餐饮=UNAVAILABLE；无网络调用）
            "travel_providers": (_TP.get_travel_service().capability_matrix()
                                 if _TP is not None else
                                 {"component": "travel_prices", "state": "UNAVAILABLE",
                                  "reason": "契约层未随本次部署打包"}),
            "honesty_note": ("execution_mode=deterministic 时 A1/A2/A3 无模型参与；"
                            "不得表述为『六个独立模型在线协作』。"),
        }

    # ----------------------------------------------------------- 执行
    def run(self, session_id: str, text: str, reset: bool = False) -> Dict[str, Any]:
        """执行一次真实多 Agent 业务流转，返回 raw + view + report + trace。"""
        if reset:
            self.reset(session_id)
        st = self.get(session_id)
        t0 = time.time()
        raw = st["session"].ask(text)
        st["last"] = raw
        view = self.slim_result(raw)
        view["report"] = st["session"].final_report(raw)
        view["session_id"] = session_id
        view["server_ms"] = round((time.time() - t0) * 1000, 1)
        return {
            "session_id": session_id,
            "raw": raw,
            "view": view,
            "report": view["report"],
            "trace": self.trace(session_id),
        }

    def last(self, session_id: str) -> Optional[Dict[str, Any]]:
        st = self._sessions.get(session_id)
        return st.get("last") if st else None

    def store(self, session_id: str):
        st = self.get(session_id)
        return st["session"].orch.store

    def slim_result(self, res: dict) -> dict:
        """把引擎原始结果收敛为前端/MCP 友好的结构（两入口共用，保证语义一致）。"""
        r = res.get("results", {})
        a3 = r.get("A3") or {}
        # V26.1 §七-②：文化问答轮不运行 A3，但会话既有行程不得在响应中消失。
        # 以 orchestrator 显式标注的 itinerary_origin 为准（this_run / carried_over / none），
        # 不在此处猜测，也不伪造 A3 执行。
        itin = res.get("current_itinerary") or a3.get("itinerary", {})
        # V31.3 §6.4：行程附带真实匹配图片。铁律：图片失败不得破坏行程——
        # enrich 内部已全捕获，此处再兜底一层；无可用图片来源时按
        # image_report 如实展示「暂无图片」，不得渲染假图。
        if _IMS is not None and isinstance(itin, dict) and itin.get("days_plan"):
            try:
                itin = _IMS.get_image_service().enrich_itinerary(itin)
            except Exception:
                pass
        a4 = r.get("A4") or {}
        # V30.0 P0-4：四层发布语义显式分开，供游客端/评委/两入口核对
        pol_culture = ((r.get("A1") or {}).get("claim_policy")
                       or ((r.get("A1") or {}).get("gate") or {}).get("claim_policy") or {})
        pol_eco = ((r.get("A2") or {}).get("claim_policy")
                   or ((r.get("A2") or {}).get("gate") or {}).get("claim_policy") or {})
        gates = res.get("gates") or {}
        release_pass = bool(res.get("release_pass"))
        publish_policy = {
            # ① 执行流程是否通过（门禁 + A5）
            "flow_pass": release_pass,
            "gates": {k: bool((gates.get(k) or {}).get("pass"))
                      for k in ("execution", "business", "release") if isinstance(gates.get(k), dict)},
            # ② 文化证据是否充分
            "culture_evidence_sufficient": bool(pol_culture.get("evidence_sufficient")),
            # ③ 具体文化事实是否可断言
            "culture_claim_assertable": bool(pol_culture.get("claim_assertable")),
            "culture_assertion_level": pol_culture.get("assertion_level", "unknown"),
            "ecology_claim_assertable": bool(pol_eco.get("claim_assertable")),
            # ④ 整份行程是否可展示（不因个别文化事实存疑而整份判死）
            "itinerary_publishable": release_pass,
            "disclosures": [d for d in (pol_culture.get("disclosure"), pol_eco.get("disclosure")) if d],
            "reasons": (pol_culture.get("reasons") or [])[:6],
            "note": ("四层相互独立：流程通过 ≠ 文化证据充分 ≠ 事实可断言 ≠ 行程可展示；"
                     "A5 PASS 仅表示审核流程通过，不代表每条文化事实均已核验。"),
        }
        return {
            "task_id": res.get("task_id"),
            "run_id": res.get("run_id"),
            "intent": res.get("intent"),
            "execution_mode": res.get("execution_mode"),
            "state_version": res.get("state_version"),
            "elapsed_ms": res.get("elapsed_ms"),
            "release_pass": res.get("release_pass"),
            # V32.2 §二：独立发布就绪状态（READY/CONDITIONAL/NOT_READY），与三层结构门禁正交。
            # 即便 release_pass=True（结构合规），publish_ready 仍可能=NOT_READY（证据不足），
            # 前端须据此区分「流程通过」与「证据齐备可发布」，不得把参考行程说成已核验行程。
            "publish_ready": res.get("publish_ready"),
            "itinerary": itin,
            # V32.2 P0-3：结构化预算估算（含分项区间/是否可证），与行程同源，供前端独立展示
            "budget": (itin.get("budget_estimate") if isinstance(itin, dict) else None),
            # V32.2 P0-2：跨城行程合理性提示（不伪造地图耗时；无授权数据源时标注待核实）
            "cross_city_advisory": (itin.get("cross_city_advisory") if isinstance(itin, dict) else None),
            # 本轮行程来自本轮 A3 还是沿用上一轮（§七-② 可核对，避免客户端各自猜测）
            "itinerary_origin": res.get("itinerary_origin", "none"),
            # A3 提取的约束（供前端与两入口核对硬约束是否被满足；此前未暴露）
            "constraints": a3.get("constraints") or {},
            "culture": _slim_qa(r.get("A1")),
            "ecology": _slim_qa(r.get("A2")),
            "weather": a4.get("weather", {}),
            "transport_note": _transport_note(a4.get("transport")),
            # V32.2 §三：真实地图数据（已授权腾讯 Key 时含 POI/路线/距离/餐厅；
            # 未授权时为 NEEDS_AUTH 说明；与天气一致，绝不伪造）
            "maps": a4.get("maps"),
            # V33 §A2：结构化出行价格（酒店/门票；无授权源时 UNAVAILABLE + 官方深链，
            # 禁止 0 元/假特价）与公共交通选项（腾讯真实接入；失败诚实降级）
            "prices": a4.get("prices"),
            "transit": a4.get("transit"),
            # 出行数据能力矩阵（与 /api/travel/status、MCP helan_travel 同源）
            "travel_status": a4.get("travel_status"),
            # V32.2 §四：仅平台连接器、非本引擎直连的服务（如实标记 PLATFORM_ONLY）
            "platform_only_services": a4.get("platform_only_services", []),
            "unavailable_services": a4.get("unavailable_services", []),
            "gates": res.get("gates"),
            "publish_policy": publish_policy,
            "verification": r.get("A5"),
            # V31.3 P0-1：内容创作轮的结构化内容（与 /api/content 同源生成）
            "content": (r.get("CONTENT") or {}).get("content"),
            "content_meta": ({"ok": (r.get("CONTENT") or {}).get("ok"),
                              "kind": (r.get("CONTENT") or {}).get("kind"),
                              "mode": (r.get("CONTENT") or {}).get("mode"),
                              "error": (r.get("CONTENT") or {}).get("error")}
                             if "CONTENT" in r else None),
            "agent_modes": {a: (r.get(a) or {}).get("execution_mode", "")
                            for a in ("A1", "A2", "A3", "A4", "A5")},
            "task_ledger": res.get("task_ledger", []),
            "dag_check": res.get("dag_check"),
            "report": None,  # 由调用方按需生成
            "trace_len": len(res.get("trace", [])),
        }

    # ----------------------------------------------------------- 轨迹
    def trace(self, session_id: str, entry: str = "web") -> Dict[str, Any]:
        """导出统一执行轨迹（Web 与 WorkBuddy 同一逻辑格式）。

        entry 仅用于标注来源入口，不改变任何业务字段。
        """
        st = self._sessions.get(session_id)
        if not st or not st.get("last"):
            return {"schema": _trace.TRACE_SCHEMA, "entry": entry, "session_id": session_id,
                    "error": "该会话尚无运行记录"}
        return _trace.build_trace(st["last"], session_id=session_id, entry=entry)

    # ----------------------------------------------------------- 工具化能力（§六 能力映射）
    def weather(self, cities: List[str]) -> Dict[str, Any]:
        """真实天气查询（open-meteo HTTP）。失败如实标注「待核实」，不伪造成功。"""
        import agents as A
        r = A.agent_travel_data(cities or ["银川"])
        ok = [c for c, w in r["weather"].items() if w.get("ok")]
        return {
            "agent": "A4",
            "weather": r["weather"],
            "ok_cities": ok,
            "tool_called": bool(r.get("tool_called")),
            "tool_evidence": r.get("tool_evidence", []),
            "unavailable_services": r.get("unavailable_services", []),
            "honesty_note": ("open-meteo 为真实在线调用；若失败则为诚实降级（待核实），"
                            "不得表述为『实时成功』。"),
        }

    def places(self, query: str = "") -> Dict[str, Any]:
        """地点/地图检索能力（§六 地图和地点搜索）。

        若部署环境已授权腾讯位置服务 Key（TENCENT_MAP_KEY），则执行**真实** POI 检索；
        否则诚实降级：明确返回 available=False 与未授权原因，并退回**本地语料**中的
        可信景点清单（标注 source=list:corpus，非实时）。这是 §七 场景 D
        「地图工具无授权→正确降级」与「有授权→真实检索」两条可复现路径。
        """
        import agents as A
        # 已授权：真实地图检索优先
        if _TM is not None:
            try:
                grp = _TM.build_adapter()
                if grp.get("status", {}).get("state") == _TM.STATUS_READY:
                    svc = grp["place"]
                    city = query or "银川"
                    r = svc.search(city, query or "景点")
                    if r.get("ok"):
                        pois = r.get("data", {}).get("pois", [])
                        if query:
                            pois = [p for p in pois if query in (p.get("title") or "")]
                        return {
                            "available": True, "authorized": True, "provider": "tencent",
                            "coord_system": "GCJ-02", "query": query,
                            "items": pois, "items_count": len(pois),
                            "source": "tencent-maps-realtime（真实 POI 检索）",
                            "honesty_note": "结果来自腾讯位置服务真实接口（GCJ-02 坐标）。",
                        }
                    # 真实调用失败也如实返回，不冒充
                    return {"available": False, "authorized": True, "provider": "tencent",
                            "query": query, "error": r.get("error"), "items": [],
                            "source": "tencent-maps-error",
                            "honesty_note": "地图服务真实调用失败：%s" % str(r.get("error"))[:120]}
            except Exception as e:
                return {"available": False, "authorized": True, "provider": "tencent",
                        "query": query, "error": str(e)[:120], "items": [],
                        "honesty_note": "地图服务调用异常，已诚实降级。"}
        # 未授权：退回本地语料
        try:
            data = A._load_attractions()
            items = [{"id": a.get("id"), "name": a.get("name"), "city": a.get("city")}
                     for a in data.get("attractions", [])]
        except Exception as e:
            items = []
            return {"available": False, "authorized": False, "query": query,
                    "error": "本地语料不可读：%s" % str(e)[:80], "items": [],
                    "source": "corpus-unavailable"}
        if query:
            items = [i for i in items if query in (i.get("name") or "") or
                     query in (i.get("city") or "")]
        return {
            "available": False,                       # 真实地图服务不可用（诚实）
            "authorized": False,
            "reason": "地图/地点检索需要腾讯位置服务 Key 授权，当前环境未授权",
            "query": query,
            "items": items,
            "items_count": len(items),
            "source": "list:corpus（本地语料示意，非实时地图检索）",
            "honesty_note": "不得将本结果表述为实时地图/POI 检索成功。",
        }

    # ----------------------------------------------------------- 出行数据（V33 §A2）
    def travel_status(self) -> Dict[str, Any]:
        """出行数据能力矩阵（与 Web /api/travel/status、MCP helan_travel 同源）。

        不发起任何网络调用：同程=BLOCKED_MERCHANT_AUTH（无商户合同/Key，
        Web 不展示同程价格，仅官方深链）；tc-chengxin=PLATFORM_ONLY（平台连接器）；
        腾讯公交/BRT=LIVE（已授权 Key 时）；酒店/门票/火车航班/餐饮价格=UNAVAILABLE。
        """
        if _TP is None:
            return {"component": "travel_prices", "state": "UNAVAILABLE",
                    "reason": "travel_prices 契约层未随本次部署打包",
                    "honesty_note": "契约层缺失时不得展示任何价格/班次。"}
        try:
            return _TP.get_travel_service().capability_matrix()
        except Exception as e:
            return {"component": "travel_prices", "state": "UNAVAILABLE",
                    "reason": "能力矩阵生成失败：%s" % type(e).__name__}

    def travel(self, frm: str = "银川站", to: str = "西夏陵",
               mode: Optional[str] = None, city: str = "银川", ptype: str = "hotel",
               check_in: Optional[str] = None, check_out: Optional[str] = None,
               keyword: Optional[str] = None,
               max_price: Optional[float] = None) -> Dict[str, Any]:
        """出行价格/交通查询（V33 §A2，三入口共用的单一真源）。

        - 价格（酒店/门票）：当前授权下同程=BLOCKED_MERCHANT_AUTH → price=None +
          UNAVAILABLE + 官方深链 + 待核实；绝不返回 0 元/假特价。
        - 交通（公交/BRT/火车/驾车）：已授权腾讯 Key 时真实调用（8s 超时、
          10min 缓存、失败诚实降级）；实时到站时间无授权数据源，恒为待核实。
        """
        if _TP is None:
            return {"travel_status": self.travel_status(), "prices": [], "transit": [],
                    "honesty_note": "travel_prices 契约层未随本次部署打包，已诚实降级。"}
        try:
            svc = _TP.get_travel_service()
            return {
                "travel_status": svc.capability_matrix(),
                "prices": svc.price_quotes(city=city, ptype=ptype, check_in=check_in,
                                           check_out=check_out, keyword=keyword,
                                           max_price=max_price),
                "transit": svc.transit_options(frm=frm, to=to, mode=mode),
                "honesty_note": ("价格/班次必须带 source/retrieved_at/status；无可靠来源时 "
                                 "price=None + UNAVAILABLE；任何失败不得导致 /api/chat 500。"),
            }
        except Exception as e:
            return {"travel_status": self.travel_status(), "prices": [], "transit": [],
                    "honesty_note": "出行查询异常，已诚实降级：%s" % type(e).__name__}

    def budget(self, session_id: str) -> Dict[str, Any]:
        """预算视图（§六 预算）：来自 A3 约束与预算说明，确定性计算，非外部报价。"""
        st = self._sessions.get(session_id)
        if not st or not st.get("last"):
            return {"error": "该会话尚无行程，请先调用 helan_plan"}
        last = st["last"]
        itin = (last.get("results", {}).get("A3") or {}).get("itinerary", {}) or {}
        c = itin.get("constraints", {}) or (last.get("results", {}).get("A3") or {}).get("constraints", {})
        return {
            "party_size": c.get("party_size"),
            "days": c.get("days"),
            "budget_yuan": c.get("budget_yuan"),
            "budget_note": itin.get("budget_note"),
            "per_day_yuan": (c.get("budget_yuan") / c.get("days")
                             if c.get("budget_yuan") and c.get("days") else None),
            "source": "deterministic:A3（基于用户约束的结构化预算，非平台实时报价）",
            "honesty_note": "门票/住宿单价需以官方或平台实时数据核验（待核实）。",
        }

    def status(self, session_id: str) -> Dict[str, Any]:
        """状态可追溯视图（§六 状态修改）：state_version + 任务账本 + 各 Agent 状态。"""
        st = self._sessions.get(session_id)
        if not st:
            return {"error": "会话不存在"}
        snap = st["session"].orch.store.snapshot()["state"]
        return {
            "session_id": session_id,
            "state_version": snap.get("state_version"),
            "run_id": snap.get("run_id"),
            "execution_mode": snap.get("execution_mode"),
            "agent_status": snap.get("agent_status", {}),
            "task_ledger": snap.get("task_ledger", []),
            "has_plan": bool((snap.get("itinerary") or {}).get("days_plan")),
        }

    # ----------------------------------------------------------- 内容创作（§六 内容创作）
    def content(self, session_id: str, kind: str = "xiaohongshu") -> Dict[str, Any]:
        """基于**真实行程与证据**的结构化内容创作（模板生成，不调用外部大模型）。

        返回 {"ok": bool, "kind":..., "content":..., "error":...}
        诚实标注：mode=template-based（基于真实行程），非模型生成。
        业务实现与聊天流 content_create 意图、MCP helan_content 同源（agents.agent_content）。
        """
        import agents as A
        st = self._sessions.get(session_id)
        if not st:
            return {"ok": False, "error": "会话不存在，请先完成行程规划"}
        return A.agent_content("", st["session"].orch.store, st.get("last"), kind=kind)


# 全局单例：两个入口都从这里取同一个运行时
RUNTIME = AgentRuntime()

# -*- coding: utf-8 -*-
"""统一执行轨迹（V26.0）。

目标：让 **独立 Web 入口** 与 **WorkBuddy 复现入口** 输出**同一逻辑格式**的执行轨迹，
从而评委可以在任一入口核对：A0 调度 → A1..A5 执行 → 工具调用 → A5 审核 → 三道门禁。

判定「真实多 Agent 协作」的四个必备证据（缺一即不得宣称）：
  1. 每个参与 Agent 都有独立 Result 消息（sender=Ai, receiver=A0）；
  2. 每个 Agent 都有明确的 execution_mode（model / deterministic / tool_only），不得留空；
  3. 凡声明调用工具，必有 tool_evidence；
  4. 凡声明模型执行，必有可核实 model_evidence（provider/model/latency/response_sha256）。

本模块只做「如实导出」，不修改任何业务结果，也不为缺失项补假数据。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

TRACE_SCHEMA = "helan.execution_trace/1.0"

# V26.1 §四-P0-3：事件种类清单（内联兜底，避免 trace 对 contracts 的导入顺序依赖）
try:  # pragma: no cover - 正常路径下 contracts 一定可导入
    from contracts import TRACE_KINDS
except Exception:  # pragma: no cover
    TRACE_KINDS = ["LLM_CALL", "TOOL_CALL", "DETERMINISTIC_STEP",
                   "AGENT_DISPATCH", "AGENT_RESULT", "GATE_CHECK"]


_AGENTS = ["A0", "A1", "A2", "A3", "A4", "A5"]

# §五 跨 Agent 消息 12 必需字段（与 contracts.MESSAGE_REQUIRED_FIELDS 保持一致；
# 此处内联以避免 trace.py 对 contracts 产生导入顺序依赖）
_MSG_REQUIRED = ["run_id", "task_id", "parent_task_id", "from_agent", "to_agent",
                 "message_type", "payload_schema_version", "state_version", "trace_id",
                 "timestamp", "status", "error_code"]


def _tool_calls_of(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls = []
    for ev in res.get("tool_evidence", []) or []:
        kind = "unknown"
        for prefix, k in (("BM25_RETRIEVE", "rag_retrieve"),
                          ("WEATHER_API_OK", "http_weather"),
                          ("WEATHER_API_FAIL", "http_weather"),
                          ("PAYWALL", "http_ticketing"),
                          ("MAP_API", "http_map"),
                          ("MODEL_CALL", "model_inference"),
                          ("DETERMINISTIC", "deterministic_algo"),
                          ("DOMAIN_GATE", "domain_gate")):
            if str(ev).startswith(prefix):
                kind = k
        calls.append({"kind": kind, "evidence": ev,
                      "is_real_tool": kind in ("rag_retrieve", "http_weather",
                                               "http_ticketing", "http_map")})
    return calls


def _model_calls_of(res: Dict[str, Any], messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """汇总本轮的每一次模型往返（V26.1 §四-P0-1）。

    以 **MODEL_CALL 事件消息**为权威来源（含失败调用）；若某轮由旧数据结构产生
    （无 MODEL_CALL 事件但有 model_evidence），则从 Agent 结果回填，保证「调用事实」不丢。
    """
    calls = []
    seen = set()
    for m in messages:
        if m.get("message_type") != "MODEL_CALL" and m.get("kind") != "LLM_CALL":
            continue
        p = m.get("payload") or {}
        agent = p.get("agent") or m.get("from_agent")
        seen.add(agent)
        calls.append({
            "agent_id": agent,
            "source": "MODEL_CALL_event",
            "ok": bool(p.get("ok")),
            "status": m.get("status"),
            "http_status": p.get("http_status"),
            "latency_ms": p.get("latency_ms"),
            "request_id": p.get("request_id"),
            "model": p.get("model"),
            "response_sha256": p.get("response_sha256"),
            "prompt_digest": p.get("prompt_digest"),
            "context_evidence_ids": p.get("context_evidence_ids"),
            "task_context_delivered": p.get("task_context_delivered"),
            "failure_kind": p.get("failure_kind", ""),
            "failure_reason": p.get("failure_reason", ""),
            "has_tool_call_id": bool(m.get("tool_call_id")),
        })
    # 回填：有 model_evidence 但缺 MODEL_CALL 事件的 Agent（不得因缺事件而丢失调用事实）
    for aid, r in (res or {}).items():
        if aid in seen or not isinstance(r, dict):
            continue
        ev = r.get("model_evidence") or {}
        if not (r.get("model_attempted") or ev):
            continue
        calls.append({
            "agent_id": aid,
            "source": "agent_result_fallback（本轮无 MODEL_CALL 事件，据 model_evidence 回填）",
            "ok": bool(ev.get("ok")),
            "status": "OK" if ev.get("ok") else "FAIL",
            "http_status": ev.get("http_status"),
            "latency_ms": ev.get("latency_ms"),
            "request_id": ev.get("request_id"),
            "model": ev.get("model"),
            "response_sha256": ev.get("response_sha256"),
            "prompt_digest": r.get("model_prompt_digest"),
            "context_evidence_ids": r.get("model_context_evidence_ids"),
            "task_context_delivered": bool(r.get("model_prompt_digest")),
            "failure_kind": r.get("model_failure_kind", ""),
            "failure_reason": str(r.get("model_failure_reason") or "")[:300],
            "has_tool_call_id": False,
        })
    calls.sort(key=lambda x: x["agent_id"])
    return calls


def build_trace(run_result: Dict[str, Any], session_id: str = "demo",
                entry: str = "web") -> Dict[str, Any]:
    """把一次 `Orchestrator.run()` 结果规范化为统一执行轨迹（只读、纯函数）。"""
    res = run_result.get("results", {}) or {}
    run_id = run_result.get("run_id") or ""
    task_id = run_result.get("task_id") or ""
    overall_mode = run_result.get("execution_mode", "")

    agents = {}
    tool_calls = []
    for aid in _AGENTS:
        if aid == "A0":
            continue
        r = res.get(aid)
        if not r:
            continue
        mode = r.get("execution_mode", "") or ("tool_only" if aid == "A4" else "")
        ev = r.get("model_evidence") or {}
        agents[aid] = {
            "agent_id": aid,
            "execution_mode": mode,
            "actual_execution_mode": r.get("actual_execution_mode", mode),
            "status": "OK" if not r.get("error") else "FAIL",
            "answer_source": r.get("answer_source") or r.get("narrative_source") or (
                "tool_only" if aid == "A4" else ""),
            "tool_called": bool(r.get("tool_called")),
            "tool_evidence": list(r.get("tool_evidence", []) or []),
            "model_evidence": r.get("model_evidence"),
            "model_verified": bool(ev.get("ok")),
            # V26.1 §四-P0-1：失败的模型调用必须留痕（不得删除调用事实）
            "model_attempted": bool(r.get("model_attempted") or ev),
            "model_failed": bool(r.get("model_failed")),
            "model_failure_kind": r.get("model_failure_kind", ""),
            "model_failure_reason": r.get("model_failure_reason", ""),
            "model_prompt_digest": r.get("model_prompt_digest"),
            "model_context_evidence_ids": r.get("model_context_evidence_ids"),
        }
        for tc in _tool_calls_of(r):
            tool_calls.append({"agent_id": aid, **tc})

    messages = []
    for m in run_result.get("trace", []) or []:
        sender = m.get("sender", "")
        receiver = m.get("receiver", "")
        mtype = m.get("type", "")
        row = {
            # ---- §五 12 必需字段（规范名）----
            "run_id": m.get("run_id", run_id) or run_id,
            "task_id": m.get("task_id", task_id),
            "parent_task_id": m.get("parent_task_id", ""),
            "from_agent": sender,
            "to_agent": receiver,
            "message_type": mtype,
            "payload_schema_version": m.get("payload_schema_version", "1.0") or "1.0",
            "state_version": m.get("state_version"),
            "trace_id": m.get("trace_id", "") or run_id,
            "timestamp": m.get("timestamp"),
            "status": m.get("status"),
            "error_code": m.get("error_code", ""),
            # ---- 兼容别名与补充（不改变上列必需字段语义）----
            "sender": sender,
            "receiver": receiver,
            "type": mtype,
            "agent_id": m.get("agent_id") or sender,
            "tool_call_id": m.get("tool_call_id", ""),
            "tool_call_real": bool(m.get("tool_call_real")),
            "kind": m.get("kind", ""),
            "kinds": list(m.get("kinds") or []),
            "execution_mode": m.get("execution_mode", ""),
            "msg_id": m.get("msg_id"),
            "dedup_key": m.get("dedup_key") or "%s|%s|%s|%s" % (
                m.get("run_id", run_id) or run_id, m.get("task_id", task_id), sender, mtype),
            # V27.0 §六 真实缺陷修复：此前**未导出 payload**，导致 MODEL_CALL 事件的
            # ok/http_status/latency_ms/request_id/prompt_digest 全部读到 None，
            # 一次成功的模型往返在轨迹里被记成 ok=false —— 即「把成功写成失败」，
            # 与 §四 P0-1「不得删除/歪曲调用事实」相悖。payload 是事件语义本体，必须导出。
            "payload": m.get("payload") or {},
        }
        messages.append(row)

    # 消息契约合规：每条消息必须齐备 12 必需字段且非空（error_code 允许空串=无错误）
    _allow_empty = {"error_code", "parent_task_id"}
    contract_violations = []
    for i, m in enumerate(messages):
        for k in _MSG_REQUIRED:
            if k not in m:
                contract_violations.append({"index": i, "field": k, "reason": "missing"})
            elif m[k] in (None, "") and k not in _allow_empty:
                contract_violations.append({"index": i, "field": k, "reason": "empty"})

    a5 = res.get("A5") or {}
    gates = run_result.get("gates") or {}
    itin = (res.get("A3") or {}).get("itinerary", {}) or {}

    # 幂等自检：同一 (run_id, task_id, agent, type) 不应出现重复消息
    seen = {}
    dup = []
    for m in messages:
        k = m["dedup_key"]
        seen[k] = seen.get(k, 0) + 1
    for k, n in seen.items():
        if n > 1:
            dup.append({"dedup_key": k, "count": n})

    # DAG 校验（无环）——若 contracts 不可导入则留空并如实标注
    dag_check = run_result.get("dag_check")
    if not dag_check:
        try:
            from contracts import DAG_SPEC, check_dag
            dag_check = check_dag(DAG_SPEC)
        except Exception as e:  # 不静默：如实记录未校验原因
            dag_check = {"ok": None, "errors": ["DAG 未校验：%s" % str(e)[:80]],
                         "topological_order": [], "nodes": [], "edge_count": 0}

    # 一致性自检（只登记事实，不放宽判定）
    # V26.1 §四-P0-4：本轮轨迹必须**只含当前 run 的消息**（跨轮残留一律记为违规）
    foreign_run_messages = [
        {"index": i, "run_id": m["run_id"], "message_type": m["message_type"]}
        for i, m in enumerate(messages) if m["run_id"] and m["run_id"] != run_id
    ]
    foreign_task_messages = [
        {"index": i, "task_id": m["task_id"], "message_type": m["message_type"]}
        for i, m in enumerate(messages)
        if m["parent_task_id"] and m["parent_task_id"] != task_id
        and not str(m["task_id"]).startswith(str(task_id))
    ]
    # V26.1 §四-P0-3：tool_call_id 只允许出现在真实工具调用上
    fake_tool_ids = [
        {"index": i, "from_agent": m["from_agent"], "tool_call_id": m["tool_call_id"],
         "kind": m["kind"], "reason": "非真实工具调用却带 tool_call_id"}
        for i, m in enumerate(messages)
        if m["tool_call_id"] and not m["tool_call_real"]
    ]
    # V26.1 §四-P0-1：失败模型调用必须保留 ok=false 证据（不允许「失败即删除」）
    lost_model_failures = [
        {"agent": aid, "reason": "声明模型调用失败却无任何 ok=false 证据"}
        for aid, a in agents.items()
        if a["model_failed"] and not a["model_evidence"]
    ]
    # V26.1 §四-P0-2：声明 model 执行的 Agent 必须留下提示指纹与所用证据编号
    missing_model_context = [
        {"agent": aid, "reason": "声明 model 执行但未记录提示指纹/证据编号（无法证明检索正文进入了请求）"}
        for aid, a in agents.items()
        if a["execution_mode"] == "model" and a["model_verified"]
        and not a.get("model_prompt_digest")
    ]

    detail = run_result.get("execution_mode_detail") or {}
    verified_model_agents = detail.get("model_agents_verified", [])

    # V26.1 §四-P0-3：模型调用必须是**独立可核对的事件**，而不是只藏在 Agent 结果里。
    # 对每个「本轮确实发出过模型请求」的 Agent，必须存在一条 MODEL_CALL 事件。
    model_call_agents = {m["from_agent"] for m in messages if m["message_type"] == "MODEL_CALL"}
    attempted_model_agents = {aid for aid, a in agents.items()
                              if a.get("model_attempted") or a.get("model_evidence")}
    missing_model_call_events = sorted(attempted_model_agents - model_call_agents)

    # V27.0 §六：MODEL_CALL 事件必须携带可核对的 payload（ok / 时延 / 请求 ID 或失败原因）。
    # 此前 trace 未导出 payload，导致成功的模型往返被记成 ok=false（把成功写成失败）。
    model_call_events_without_payload = [
        m["from_agent"] for m in messages
        if m["message_type"] == "MODEL_CALL"
        and not (isinstance(m.get("payload"), dict) and "ok" in m["payload"])
    ]
    # 交叉自检：轨迹声明的 model_calls 结论必须与 Agent 结果里的 model_verified 一致，
    # 不允许「Agent 说成功、事件说失败」（或反之）。
    model_calls = _model_calls_of(res, messages)
    _mc_ok = {c["agent_id"]: bool(c.get("ok")) for c in model_calls}
    model_call_outcome_mismatch = sorted(
        aid for aid, a in agents.items()
        if aid in _mc_ok and a.get("execution_mode") == "model"
        and bool(a.get("model_verified")) != _mc_ok[aid]
    )

    checks = {
        "every_agent_has_result_message": all(
            any(m["from_agent"] == aid and m["message_type"] == "RESULT" for m in messages)
            for aid in agents),
        "every_agent_has_execution_mode": all(bool(a["execution_mode"]) for a in agents.values()),
        "tool_claims_have_evidence": all(
            (not a["tool_called"]) or bool(a["tool_evidence"]) for a in agents.values()),
        "model_claims_have_evidence": all(
            a["execution_mode"] != "model" or a["model_verified"] for a in agents.values()),
        "every_message_has_contract_fields": not contract_violations,
        "no_duplicate_messages": not dup,
        "dag_is_acyclic": bool(dag_check.get("ok")),
        # ---- V26.1 新增（四项轨迹真实性）----
        "all_messages_match_current_run": not foreign_run_messages and not foreign_task_messages,
        "tool_call_ids_only_on_real_tool_calls": not fake_tool_ids,
        "failed_model_calls_retained": not lost_model_failures,
        "model_claims_have_prompt_context": not missing_model_context,
        "model_calls_recorded_as_events": not missing_model_call_events,
        "execution_mode_matches_evidence": (
            (overall_mode != "model") or bool(verified_model_agents)),
        # ---- V27.0 新增（事件 payload 完整性与结论一致性）----
        "model_call_events_carry_payload": not model_call_events_without_payload,
        "model_call_outcomes_match_agents": not model_call_outcome_mismatch,
    }

    # 把「真实工具调用」与消息里携带的工具调用 ID 对齐（ID 来自 RESULT 消息，非另造）
    result_tc_ids = {}
    for m in messages:
        if m["message_type"] == "RESULT" and m["tool_call_id"]:
            result_tc_ids.setdefault(m["from_agent"], m["tool_call_id"])
    for tc in tool_calls:
        tc["tool_call_id"] = result_tc_ids.get(tc["agent_id"], "")
        tc["has_authoritative_id"] = bool(tc["tool_call_id"]) if tc["is_real_tool"] else True

    detail = run_result.get("execution_mode_detail") or {}
    configured_mode = run_result.get("configured_execution_mode", overall_mode)

    return {
        "schema": TRACE_SCHEMA,
        "entry": entry,                      # web | workbuddy
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session_id": session_id,
        "run_id": run_id,
        "task_id": task_id,
        "intent": run_result.get("intent"),
        "state_version": run_result.get("state_version"),
        "elapsed_ms": run_result.get("elapsed_ms"),
        "execution_mode": overall_mode,                       # 本次**实际**执行模式
        "configured_execution_mode": configured_mode,         # 环境「配置能力」
        "execution_mode_detail": detail,
        "deterministic_engine": overall_mode != "model",
        "run_isolation": {
            "scope": "trace 每次 run() 重建，仅含本 run 消息",
            "message_count": len(messages),
            "foreign_run_messages": foreign_run_messages,
            "foreign_task_messages": foreign_task_messages,
        },
        "agents": agents,
        "tool_calls": tool_calls,
        # V26.1 §四-P0-1：本轮每一次模型往返（含失败）的独立清单
        "model_calls": model_calls,
        "trace_kinds": TRACE_KINDS,
        "messages": messages,
        "message_required_fields": _MSG_REQUIRED,
        "message_schema_version": "1.0",
        "contract_violations": contract_violations,
        "fake_tool_call_ids": fake_tool_ids,
        "lost_model_failures": lost_model_failures,
        "missing_model_context": missing_model_context,
        "missing_model_call_events": missing_model_call_events,
        "dag": run_result.get("dag") or (dag_check and {"nodes": dag_check.get("nodes"),
                                                       "edges": None, "source": "A0", "sink": "A5"}),
        "dag_check": dag_check,
        "task_ledger": run_result.get("task_ledger") or [],
        "duplicate_messages": dup,
        "verification": a5,
        "gates": gates,
        "release_pass": bool(run_result.get("release_pass")),
        "itinerary_summary": {
            "days": itin.get("days"),
            "cities": itin.get("used_cities", []),
            "items_per_day": [len(d.get("items", [])) for d in itin.get("days_plan", [])],
            "attraction_names": [i.get("name") for d in itin.get("days_plan", [])
                                 for i in d.get("items", [])],
        },
        "consistency_checks": checks,
        "honesty_note": (
            "execution_mode=deterministic 表示该 Agent 本轮无模型参与，"
            "由确定性代码/算法执行；不得对外表述为『模型执行』。"
            "execution_mode=model 必须附 model_evidence（ok=true）与提示指纹。"
            "tool_call_id 仅出现在真实工具调用（tool_call_real=true）上；"
            "确定性步骤/模型推理/审核/门禁一律不带工具调用 ID。"),
    }


def export_trace(run_result: Dict[str, Any], out_dir: str, entry: str = "web",
                 session_id: str = "demo", filename: Optional[str] = None) -> str:
    """导出统一执行轨迹到 JSON 文件，返回文件路径。"""
    trace = build_trace(run_result, session_id=session_id, entry=entry)
    os.makedirs(out_dir, exist_ok=True)
    if not filename:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        filename = "trace_%s_%s.json" % (entry, stamp)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
    return path

# -*- coding: utf-8 -*-
"""执行模式解析与模型调用编排（V26.0）。

统一逻辑：任何 Agent 需要「模型能力」时，只调用 `model_answer()`：
- 配置了真实模型 -> 执行真实调用，返回 mode="model" 与完整 model_call 证据；
- 未配置/调用失败 -> 返回 mode="deterministic" 与失败原因，由调用方退回确定性核心。

无论哪条路径，调用方都必须在执行轨迹中如实记录 `execution_mode`，
禁止把确定性结果标注为模型执行结果。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .provider import (
    ENV_MODE,
    ModelCallFailed,
    ModelClient,
    ModelUnavailable,
)

EXECUTION_MODE_MODEL = "model"
EXECUTION_MODE_DETERMINISTIC = "deterministic"

_VALID_MODES = {"auto", "model", "deterministic"}


def _env_mode() -> str:
    m = (os.getenv(ENV_MODE, "auto") or "auto").strip().lower()
    return m if m in _VALID_MODES else "auto"


def resolve_execution_mode(client: Optional[ModelClient] = None) -> Dict[str, Any]:
    """解析当前实际执行模式。

    返回：{"execution_mode": "model"|"deterministic", "reason": str,
           "forced": str, "configured": bool, "blocked": bool}
    `blocked=True` 表示用户强制要求 model 模式但环境不具备条件（验收器须判 BLOCK）。
    """
    c = client or ModelClient()
    forced = _env_mode()
    configured = c.configured
    if forced == "deterministic":
        return {"execution_mode": EXECUTION_MODE_DETERMINISTIC,
                "reason": "HELAN_AGENT_MODE=deterministic（显式以确定性引擎运行）",
                "forced": forced, "configured": configured, "blocked": False}
    if configured:
        if c.provider == "workbuddy_openapi":
            # 开放平台协议细节未核实：不虚称可用，退回确定性
            return {"execution_mode": EXECUTION_MODE_DETERMINISTIC,
                    "reason": "WorkBuddy 开放平台 ACP 协议细节与团队调用授权未核实，拒绝冒充模型执行",
                    "forced": forced, "configured": False,
                    "blocked": (forced == "model")}
        return {"execution_mode": EXECUTION_MODE_MODEL,
                "reason": "已检测到模型端点配置（%s / %s）" % (c.provider, c.model),
                "forced": forced, "configured": True, "blocked": False}
    return {"execution_mode": EXECUTION_MODE_DETERMINISTIC,
            "reason": "未检测到模型端点配置（%s / %s / %s 缺失）"
                      % ("HELAN_LLM_BASE_URL", "HELAN_LLM_API_KEY", "HELAN_LLM_MODEL"),
            "forced": forced, "configured": False, "blocked": (forced == "model")}


def capability_report(client: Optional[ModelClient] = None) -> Dict[str, Any]:
    """运行时可注入的完整能力自证（供 /health、预检、验收器使用）。"""
    c = client or ModelClient()
    mode = resolve_execution_mode(c)
    return {
        "component": "helan_llm_adapter",
        "version": "1.0.0",
        "execution_mode": mode["execution_mode"],
        "reason": mode["reason"],
        "forced_by_env": mode["forced"],
        "blocked": mode["blocked"],
        "client": c.describe(),
        "deterministic_fallback": {
            "available": True,
            "note": "未配置模型时，A1/A2 走语料 BM25 检索，A3/A5 走确定性算法，"
                    "A4 走 open-meteo 真实 HTTP；均不伪造工具调用。",
        },
    }


def model_answer(agent_id: str, purpose: str, system_prompt: str, user_prompt: str,
                 temperature: float = 0.2, client: Optional[ModelClient] = None) -> Dict[str, Any]:
    """Agent 请求模型能力的唯一入口。绝不抛异常、绝不伪造成功。"""
    c = client or ModelClient()
    mode = resolve_execution_mode(c)
    if mode["execution_mode"] != EXECUTION_MODE_MODEL:
        return {"mode": EXECUTION_MODE_DETERMINISTIC, "text": None,
                "evidence": None, "reason": mode["reason"], "blocked": mode["blocked"]}
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    try:
        text, ev = c.chat(messages, agent_id=agent_id, purpose=purpose,
                          temperature=temperature)
        return {"mode": EXECUTION_MODE_MODEL, "text": text,
                "evidence": ev.to_dict(), "reason": "ok", "blocked": False}
    except ModelCallFailed as e:
        return {"mode": EXECUTION_MODE_DETERMINISTIC, "text": None,
                "evidence": e.evidence.to_dict(),
                "reason": "模型调用失败，已诚实降级：%s" % e, "blocked": False}
    except ModelUnavailable as e:
        return {"mode": EXECUTION_MODE_DETERMINISTIC, "text": None,
                "evidence": None, "reason": "模型不可用：%s" % e, "blocked": False}
    except Exception as e:  # 兜底：任何异常都不得伪造成功
        return {"mode": EXECUTION_MODE_DETERMINISTIC, "text": None,
                "evidence": None,
                "reason": "未预期异常，已诚实降级：%s: %s" % (type(e).__name__, str(e)[:160]),
                "blocked": False}


def probe(client: Optional[ModelClient] = None) -> Dict[str, Any]:
    """真实探活：仅在已配置时发起一次最小调用，用于评委环境预检。"""
    c = client or ModelClient()
    rep = capability_report(c)
    if rep["execution_mode"] != EXECUTION_MODE_MODEL:
        rep["probe"] = {"attempted": False, "ok": False, "detail": rep["reason"]}
        return rep
    out = model_answer("A0", "preflight_probe", "你是连通性探针。",
                       "只回复两个字：连通", temperature=0.0, client=c)
    ev = out.get("evidence") or {}
    rep["probe"] = {
        "attempted": True,
        "ok": bool(ev.get("ok")),
        "latency_ms": ev.get("latency_ms"),
        "request_id": ev.get("request_id"),
        "http_status": ev.get("http_status"),
        "detail": out.get("reason"),
    }
    return rep

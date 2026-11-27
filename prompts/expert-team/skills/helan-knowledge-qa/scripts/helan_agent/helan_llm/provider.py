# -*- coding: utf-8 -*-
"""真实模型调用 Provider（零第三方依赖，仅标准库 urllib）。

支持两类 provider：
1. `openai_compatible` —— 任何 OpenAI 兼容 `POST {base_url}/chat/completions` 服务
   （混元 / DeepSeek / 通义 / vLLM / Ollama / 自建网关等）。由环境变量配置。
2. `workbuddy_openapi` —— WorkBuddy 开放平台 OAuth 凭证路径（复用 domain/workbuddy_api
   的配置读取；凭证缺失时显式抛 ModelUnavailable，不用 Mock 冒充）。

诚实边界：
- 本模块不做任何「假装调用成功」的兜底。任何异常都会向上抛出并携带 ok=False 的证据。
- 证据中只保存 prompt 的 SHA-256 与长度，不保存原始 prompt/回复，避免敏感信息外泄。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

REDACTED = "<redacted>"

# 环境变量名（评委环境按需注入，禁止写入代码或提交到 Git）
ENV_MODE = "HELAN_AGENT_MODE"            # auto | model | deterministic
ENV_PROVIDER = "HELAN_LLM_PROVIDER"      # auto | openai_compatible | workbuddy_openapi | off
ENV_BASE_URL = "HELAN_LLM_BASE_URL"
ENV_API_KEY = "HELAN_LLM_API_KEY"
ENV_MODEL = "HELAN_LLM_MODEL"
ENV_TIMEOUT = "HELAN_LLM_TIMEOUT"
ENV_MAX_TOKENS = "HELAN_LLM_MAX_TOKENS"


class ModelUnavailable(RuntimeError):
    """未配置可用模型凭证/端点。上层必须退回确定性路径并显式标注。"""


class ModelCallFailed(RuntimeError):
    """已配置模型但本次调用失败（网络/鉴权/限流/解析）。携带 evidence。"""

    def __init__(self, message: str, evidence: "ModelCallEvidence"):
        super().__init__(message)
        self.evidence = evidence


@dataclass
class ModelCallEvidence:
    """一次真实模型调用的证据（可进入执行轨迹与参赛证据材料）。"""
    ok: bool
    provider: str
    model: str
    endpoint_host: str
    agent_id: str
    purpose: str
    request_id: str = ""
    latency_ms: float = 0.0
    prompt_sha256: str = ""
    prompt_chars: int = 0
    response_sha256: str = ""
    response_chars: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)
    http_status: int = 0
    error: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc or url
    except Exception:
        return url


class ModelClient:
    """OpenAI 兼容 / WorkBuddy 开放平台的统一聊天客户端。"""

    def __init__(self, provider: str = "", base_url: str = "", api_key: str = "",
                 model: str = "", timeout: float = 30.0, max_tokens: int = 1024):
        self.provider = (provider or os.getenv(ENV_PROVIDER, "auto")).strip().lower()
        self.base_url = (base_url or os.getenv(ENV_BASE_URL, "")).strip().rstrip("/")
        self.api_key = (api_key or os.getenv(ENV_API_KEY, "")).strip()
        self.model = (model or os.getenv(ENV_MODEL, "")).strip()
        try:
            self.timeout = float(os.getenv(ENV_TIMEOUT, timeout) or timeout)
        except (TypeError, ValueError):
            self.timeout = float(timeout)
        try:
            self.max_tokens = int(os.getenv(ENV_MAX_TOKENS, max_tokens) or max_tokens)
        except (TypeError, ValueError):
            self.max_tokens = int(max_tokens)

    # ---------- 能力判定 ----------
    @property
    def configured(self) -> bool:
        """是否具备发起真实调用的最小配置。"""
        if self.provider == "off":
            return False
        if self.provider == "workbuddy_openapi":
            return self._workbuddy_configured()
        # auto / openai_compatible：需要 base_url + api_key + model
        return bool(self.base_url and self.api_key and self.model)

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url or "(unset)",
            "endpoint_host": _host_of(self.base_url) if self.base_url else "(unset)",
            "model": self.model or "(unset)",
            "api_key": (self.api_key[:4] + "…" + self.api_key[-3:]) if len(self.api_key) > 10 else REDACTED,
            "configured": self.configured,
            "timeout_s": self.timeout,
            "max_tokens": self.max_tokens,
        }

    @staticmethod
    def _workbuddy_configured() -> bool:
        try:
            from workbuddy_api.config import load_config  # 位于 domain/ 下，可能不在 sys.path
            cfg = load_config()
            return bool(cfg.get("client_id") and cfg.get("client_secret"))
        except Exception:
            return False

    # ---------- 真实调用 ----------
    def chat(self, messages: List[Dict[str, str]], agent_id: str = "A0",
             purpose: str = "generic", temperature: float = 0.2) -> tuple:
        """发起一次真实模型调用。成功返回 (text, evidence)；失败抛异常并带证据。"""
        if self.provider == "workbuddy_openapi":
            return self._chat_workbuddy(messages, agent_id, purpose, temperature)
        if not self.configured:
            raise ModelUnavailable(
                "未配置模型端点：请设置 %s / %s / %s（或使用 HELAN_AGENT_MODE=deterministic 显式以确定性引擎运行）"
                % (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL))
        return self._chat_openai_compatible(messages, agent_id, purpose, temperature)

    def _chat_openai_compatible(self, messages, agent_id, purpose, temperature) -> tuple:
        url = self.base_url
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        prompt_blob = json.dumps(messages, ensure_ascii=False)
        ev = ModelCallEvidence(
            ok=False, provider="openai_compatible", model=self.model,
            endpoint_host=_host_of(url), agent_id=agent_id, purpose=purpose,
            prompt_sha256=_sha256(prompt_blob), prompt_chars=len(prompt_blob),
        )
        t0 = time.time()
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % self.api_key,
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                ev.http_status = getattr(r, "status", 200)
                ev.request_id = (r.headers.get("x-request-id")
                                 or r.headers.get("request-id") or "")
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            ev.latency_ms = round((time.time() - t0) * 1000, 1)
            ev.http_status = int(getattr(e, "code", 0) or 0)
            ev.error = "HTTPError %s: %s" % (e.code, str(e.reason)[:160])
            raise ModelCallFailed(ev.error, ev)
        except Exception as e:  # 网络/超时/证书等
            ev.latency_ms = round((time.time() - t0) * 1000, 1)
            ev.error = "%s: %s" % (type(e).__name__, str(e)[:160])
            raise ModelCallFailed(ev.error, ev)

        ev.latency_ms = round((time.time() - t0) * 1000, 1)
        try:
            data = json.loads(body)
            text = data["choices"][0]["message"]["content"]
            ev.request_id = ev.request_id or str(data.get("id", ""))
            ev.usage = dict(data.get("usage", {}) or {})
        except Exception as e:
            ev.error = "响应解析失败: %s" % str(e)[:160]
            raise ModelCallFailed(ev.error, ev)
        ev.ok = True
        ev.response_sha256 = _sha256(text)
        ev.response_chars = len(text)
        return text, ev

    def _chat_workbuddy(self, messages, agent_id, purpose, temperature) -> tuple:
        """WorkBuddy 开放平台路径。

        说明（UNVERIFIED，禁止虚称已通）：开放平台 ACP 的 JSON-RPC 方法名与请求体
        以平台真实协议为准，公开文档未保证可指定调用已安装的专家团。因此本方法在
        缺少可核实协议细节时**显式拒绝**，而不是返回假结果。
        """
        raise ModelUnavailable(
            "WorkBuddy Open API 路径尚未获得可核实的 ACP 协议细节与团队调用授权，"
            "拒绝以占位实现冒充成功（见 domain/workbuddy_api/client.py 注释）。"
            "请改用 openai_compatible 端点，或在平台侧完成应用注册与权限申请后再启用。")

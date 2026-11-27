# -*- coding: utf-8 -*-
"""真实模型调用适配层（V26.0）。

设计红线：
- 本层只做「真实 HTTP 模型调用」与「真实证据留痕」，绝不伪造调用结果。
- 未配置可用凭证时，`runtime.resolve_execution_mode()` 返回 `deterministic`，
  上层 Agent 退回确定性业务核心，并在执行轨迹中显式标注，禁止冒充模型执行。
"""
from .provider import (  # noqa: F401
    ModelClient,
    ModelCallEvidence,
    ModelUnavailable,
    ModelCallFailed,
    REDACTED,
)
from .runtime import (  # noqa: F401
    EXECUTION_MODE_MODEL,
    EXECUTION_MODE_DETERMINISTIC,
    resolve_execution_mode,
    capability_report,
    model_answer,
    probe,
)

__all__ = [
    "ModelClient",
    "ModelCallEvidence",
    "ModelUnavailable",
    "ModelCallFailed",
    "REDACTED",
    "EXECUTION_MODE_MODEL",
    "EXECUTION_MODE_DETERMINISTIC",
    "resolve_execution_mode",
    "capability_report",
    "model_answer",
    "probe",
]

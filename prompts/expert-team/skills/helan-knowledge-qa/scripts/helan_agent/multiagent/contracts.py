# -*- coding: utf-8 -*-
"""
《贺兰有灵·灵游宁夏》多智能体编排 —— 消息与状态契约（冻结版 v1.0）

设计原则（对照任务书「二、编排与通信」）：
- 跨 Agent 任务全部由 A0 统一分发，子 Agent 不得互相直接调度或修改全局状态。
- 所有 Agent 间通信使用结构化消息：task_id / sender / receiver / type / state_version / payload / status。
- 唯一 StateStore 保存全局约束、行程与执行日志；任何修改必须伴随 state_version 递增。
- 本模块为确定性契约定义，不依赖任何外部大模型，保证可复现、可测试。
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import time
import uuid

# Agent 注册表（冻结：6 个固定 Agent，禁止随意新增）
AGENT_REGISTRY = {
    "A0": "Orchestrator",
    "A1": "Culture",
    "A2": "Ecology",
    "A3": "TravelPlanner",
    "A4": "TravelData",
    "A5": "Verifier",
}
VALID_AGENTS = set(AGENT_REGISTRY.keys())

# 消息类型
MSG_TYPES = {
    "DISPATCH",      # A0 -> 子Agent：下发任务
    "RESULT",        # 子Agent -> A0：返回结果
    "QUERY",         # A0 -> A0 内部子查询（文化/生态检索）
    "STATE_UPDATE",  # 状态变更通知（携带 state_version）
    "GATE",          # 门禁裁决
    "ERROR",         # 错误/异常
    "MODEL_CALL",    # V26.1 §四-P0-1/P0-3：一次真实模型往返（成功与失败都必须留痕）
    "TOOL_CALL",     # V26.1 §四-P0-3：一次真实外部工具调用（仅此类可带 tool_call_id）
}

# 状态枚举
STATUS_OK = "OK"
STATUS_FAIL = "FAIL"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_PENDING = "PENDING"
STATUS_STALE = "STALE"  # 基于过期 state_version，拒绝应用

# 合法状态集合（核验器与领域层共用同一来源，禁止各自硬编码副本）
STATUSES = {STATUS_OK, STATUS_FAIL, STATUS_TIMEOUT, STATUS_PENDING, STATUS_STALE}

# 意图类型
INTENT_TRAVEL = "travel_plan"
INTENT_CULTURE = "culture_qa"
INTENT_ECOLOGY = "ecology_qa"
INTENT_REVISE = "travel_revise"
INTENT_MIXED = "mixed"
INTENT_UNKNOWN = "out_of_scope"  # 领域外问题（如 iPhone/快排），交由 A1 诚实拒答
# V31.3 P0-1：内容创作意图（小红书文案 / 旅行笔记 / Vlog 分镜等）。
# 修复的真实缺陷：游客在已有行程的会话里说「帮我写一篇小红书攻略」，
# 因「攻略」被当作旅行标记、且「会话已有行程」被当作修订的充分条件，
# 整轮被路由去重新规划行程，内容创作请求被静默丢弃。
INTENT_CONTENT = "content_create"

# ---------------------------------------------------------------------
# V31.3 P0-1：语义标记词表（唯一真源，orchestrator/engine 共用，禁止各自维护副本）
# ---------------------------------------------------------------------
# 内容创作语义（明确的内容名词）：命中即可能是内容创作诉求。
# 注意：「攻略」本身具有歧义（「宁夏旅游攻略怎么安排」仍是旅行规划需求），
# 因此**不**放入本表；「脚本」单独出现亦歧义（拍摄脚本 vs 代码脚本），
# 只保留无歧义的「拍摄脚本」。
CONTENT_MARKERS = [
    "小红书", "文案", "vlog", "Vlog", "VLOG", "分镜", "拍摄脚本", "种草",
    "图文笔记", "短视频", "游记", "爆款", "推文", "旅行笔记", "攻略文",
]

# 泛写作动词：单独出现**不足以**判定内容创作（「帮我写个Python脚本」是领域外请求），
# 必须同时具备文旅领域相关性（领域词/旅行词命中）才判 content_create。
WRITE_VERB_MARKERS = [
    "写一", "帮我写", "写个", "写份", "写成", "写篇", "撰写", "整理成",
    "生成一篇", "生成一份",
]

# 「强修订标记」：即使用户问句里带知识性名词（如「第一天加个宁夏博物馆」含"博物馆"），
# 只要出现这些明显的行程改动动作，仍应按修订处理，而不是当成知识问答或内容创作。
# V31.3：从 engine.py 上移至此，作为 orchestrator 与 engine 共用的唯一真源。
STRONG_REVISION_MARKERS = [
    "第一天", "第二天", "第三天", "第四天", "第五天", "第1天", "第2天", "第3天",
    "去掉", "删除", "换成", "改成", "改为", "变成", "再加", "加个", "加上",
    "增加", "减少", "调整", "重排", "重规划", "重新安排",
]

# 「形态转换标记」（V31.3 补充修复）：与内容名词共现时表示**内容产出形态**的转换，
# 而非行程元素的改动——「改成 Vlog 分镜脚本」「换成小红书风格」的宾语是内容形态。
# 语义规则：内容名词 + 形态转换标记 => 内容创作请求（产出物是内容），
# 不得因「改成/换成」落入行程修订（前端 followUps「改成 Vlog 分镜脚本」按钮的产品意图）。
# 与「把西夏陵去掉，换成水洞沟。」（无内容名词）互斥：后者仍是行程修订。
FORM_CONVERT_MARKERS = [
    "改成", "换成", "改为", "变成",
]

# 执行模式（V26.0）：区分「真实模型执行」与「确定性业务核心执行」，
# 使 Web 入口与 WorkBuddy 入口能输出同一逻辑格式的执行轨迹，且不得虚称。
EXECUTION_MODES = {
    "model": "该 Agent 本轮执行了真实模型调用（须带 model_call 证据）",
    "deterministic": "该 Agent 本轮由确定性代码/算法执行（无模型参与）",
    "tool_only": "该 Agent 仅执行真实外部工具调用（如 HTTP API），不使用模型生成事实",
}

# 统一轨迹字段（两入口必须一致输出）
TRACE_FIELDS = ["run_id", "task_id", "agent_id", "state_version",
                "execution_mode", "tool_call_id", "status"]

# =====================================================================
# V26.1 §四-P0-3：执行事件的「种类」必须可区分，禁止互相冒充。
# 只有 kind == TOOL_CALL 的事件才允许携带 tool_call_id（由工具调用方产生）；
# 模型推理、确定性算法、调度、审核与门禁都不得伪造工具调用 ID。
# =====================================================================
KIND_LLM_CALL = "LLM_CALL"                  # 真实模型推理（HTTP 往返，有 model 证据）
KIND_TOOL_CALL = "TOOL_CALL"                # 真实外部工具调用（HTTP/DB/检索 SDK）
KIND_DETERMINISTIC_STEP = "DETERMINISTIC_STEP"  # 确定性算法/规则，无模型无工具
KIND_AGENT_DISPATCH = "AGENT_DISPATCH"      # A0 -> 子 Agent 的任务下发
KIND_AGENT_RESULT = "AGENT_RESULT"          # 子 Agent -> A0 的结果回传
KIND_GATE_CHECK = "GATE_CHECK"              # 确定性门禁裁决
TRACE_KINDS = [KIND_LLM_CALL, KIND_TOOL_CALL, KIND_DETERMINISTIC_STEP,
               KIND_AGENT_DISPATCH, KIND_AGENT_RESULT, KIND_GATE_CHECK]

# 消息类型 -> 事件种类（仅用于轨迹标注，不改变 message_type 语义）
MSG_TYPE_TO_KIND = {
    "DISPATCH": KIND_AGENT_DISPATCH,
    "RESULT": KIND_AGENT_RESULT,
    "GATE": KIND_GATE_CHECK,
    "QUERY": KIND_AGENT_DISPATCH,
    "STATE_UPDATE": KIND_DETERMINISTIC_STEP,
    "ERROR": KIND_DETERMINISTIC_STEP,
    "MODEL_CALL": KIND_LLM_CALL,
    "TOOL_CALL": KIND_TOOL_CALL,
}

# tool_evidence 字符串前缀 -> 工具种类（用于把「工具证据」映射为 TOOL_CALL）
TOOL_EVIDENCE_PREFIXES = {
    "BM25_RETRIEVE": "rag_retrieve",
    "WEATHER_API_OK": "http_weather",
    "WEATHER_API_FAIL": "http_weather",
    "PAYWALL": "http_ticketing",
    "MAP_API": "http_map",
}
# 明确不属于「真实工具调用」的确定性证据前缀
NON_TOOL_EVIDENCE_PREFIXES = ("MODEL_CALL", "DETERMINISTIC", "DOMAIN_GATE", "RULE")

# =====================================================================
# V26.0 §五：跨 Agent 消息「可追溯契约」
# 所有跨 Agent 消息至少可追溯以下 12 个字段；缺一即判定契约不合规。
# 说明：Message 内部沿用历史字段名 sender/receiver/type（为保持向后兼容），
# 通过 from_agent/to_agent/message_type 只读属性暴露为契约规范名，
# 统一轨迹导出时 **同时** 输出规范名与历史名，两入口格式完全一致。
# =====================================================================
MSG_SCHEMA_VERSION = "1.0"
MESSAGE_REQUIRED_FIELDS = [
    "run_id", "task_id", "parent_task_id", "from_agent", "to_agent",
    "message_type", "payload_schema_version", "state_version", "trace_id",
    "timestamp", "status", "error_code",
]

# 错误码（可追溯的失败语义；空字符串表示无错误）
ERR_NONE = ""
ERR_TOOL_TIMEOUT = "E_TOOL_TIMEOUT"        # 真实工具调用超时
ERR_TOOL_FAILED = "E_TOOL_FAILED"          # 真实工具调用失败（非超时）
ERR_UPSTREAM_FAIL = "E_UPSTREAM_FAIL"      # 依赖的 Agent 失败 -> 本任务跳过
ERR_STALE_STATE = "E_STALE_STATE"          # 基于过期 state_version，拒绝写入
ERR_OUT_OF_SCOPE = "E_OUT_OF_SCOPE"        # 领域外问题（诚实拒答，非错误）
ERR_MODEL_UNAVAILABLE = "E_MODEL_UNAVAILABLE"
RETRYABLE_ERROR_CODES = {ERR_TOOL_TIMEOUT, ERR_TOOL_FAILED}
ERROR_CODES = [ERR_NONE, ERR_TOOL_TIMEOUT, ERR_TOOL_FAILED, ERR_UPSTREAM_FAIL,
               ERR_STALE_STATE, ERR_OUT_OF_SCOPE, ERR_MODEL_UNAVAILABLE]

# 超时 / 重试策略（轻量；不引入 Kafka/RabbitMQ 等重型中间件）
# 30s 与 WorkBuddy 开放平台「单次工具响应建议 30 秒内」一致。
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_ATTEMPTS = 2
RETRY_POLICY = {
    "max_attempts": DEFAULT_MAX_ATTEMPTS,
    "backoff_ms": 500,                       # 线性退避，避免放大外部服务压力
    "retryable_error_codes": sorted(RETRYABLE_ERROR_CODES),
    "idempotency": "msg_id + dedup_key(run_id|task_id|agent_id|type) 去重",
}

# =====================================================================
# V26.0 §五：显式 DAG（有向无环任务图）
# 约定：图只表达 **任务依赖**（谁必须先产出、谁依赖谁），不表达「结果回传 A0」
# 的控制流边——那是消息流（由 Message(receiver="A0") 表达），否则 A0↔A5 成环。
# 因此本图为 DAG：A0 为唯一源点，A5 为汇点。
# =====================================================================
DAG_SPEC = {
    "nodes": ["A0", "A1", "A2", "A3", "A4", "A5"],
    "edges": [
        ["A0", "A1"], ["A0", "A2"], ["A0", "A3"], ["A0", "A4"], ["A0", "A5"],  # A0 统一分发
        ["A3", "A4"],                                                          # A3 产出城市 -> A4 实时数据
        ["A1", "A5"], ["A2", "A5"], ["A3", "A5"], ["A4", "A5"],                # 各 Agent 结果 -> A5 独立核验
    ],
    "source": "A0",
    "sink": "A5",
    "parallelizable": [["A1", "A2", "A3", "A4"]],   # 文化/生态/实时可按需并行取证
    "note": ("依赖环检测为强制项；A0 统一编排，成员不直接互联、不改全局状态；"
             "不引入消息中间件，采用显式 DAG 执行器 + 类型化消息，禁止不受控循环。"),
}


def check_dag(spec: Dict[str, Any] = None) -> Dict[str, Any]:
    """校验 DAG 合法性：节点合法、边两端合法、无自环、无环（Kahn 拓扑排序）。

    返回 dict：ok / errors / topological_order / nodes / edge_count。
    纯函数、无副作用，供 scripts/verify_contracts.py 与两入口共用。
    """
    spec = spec or DAG_SPEC
    nodes = list(spec.get("nodes", []))
    edges = [tuple(e) for e in spec.get("edges", [])]
    errors: List[str] = []

    for n in nodes:
        if n not in VALID_AGENTS:
            errors.append("非法节点：%s" % n)
    for a, b in edges:
        if a not in nodes or b not in nodes:
            errors.append("非法边（端点不在节点集）：%s->%s" % (a, b))
        if a == b:
            errors.append("自环：%s->%s" % (a, b))

    # Kahn 拓扑排序（仅用合法边）
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in edges:
        if a in adj and b in indeg and a != b:
            adj[a].append(b)
            indeg[b] += 1
    queue = [n for n in nodes if indeg[n] == 0]
    order: List[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(nodes):
        cycle_nodes = [n for n in nodes if n not in order]
        errors.append("存在环，无法拓扑排序；涉及节点：%s" % ",".join(cycle_nodes))

    return {
        "ok": not errors,
        "errors": errors,
        "topological_order": order,
        "nodes": nodes,
        "edge_count": len(edges),
    }


@dataclass
class Message:
    """结构化 Agent 间消息。

    历史字段（sender/receiver/type）保持原名以兼容既有代码；
    V26.0 §五 的 12 个「可追溯必需字段」通过只读属性 from_agent/to_agent/message_type
    与 to_trace_dict() 暴露为契约规范名，两入口导出格式一致。
    """
    task_id: str
    sender: str
    receiver: str
    type: str
    state_version: int
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_PENDING
    timestamp: float = field(default_factory=time.time)
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # ---- V26.0 追加字段（均有默认值，保持向后兼容）----
    run_id: str = ""            # 一次完整任务运行的全局 ID（跨 Agent 一致）
    parent_task_id: str = ""    # 父任务 ID（子任务溯源；根任务为空）
    trace_id: str = ""          # 链路 ID（默认取 run_id，用于跨入口对齐同一逻辑链路）
    agent_id: str = ""          # 该消息代表的执行主体（sender 的规范化别名）
    tool_call_id: str = ""      # 若本消息承载工具调用，则记录工具调用 ID
    execution_mode: str = ""    # model / deterministic / tool_only
    payload_schema_version: str = MSG_SCHEMA_VERSION
    error_code: str = ERR_NONE  # 失败语义（见 ERROR_CODES）
    # ---- V26.1 §四-P0-3 追加字段（事件种类必须可区分，禁止互相冒充）----
    kind: str = ""              # 本消息的主种类（AGENT_DISPATCH / AGENT_RESULT / GATE_CHECK ...）
    kinds: List[str] = field(default_factory=list)  # 本消息内实际发生的事件种类清单
    tool_call_real: bool = False  # tool_call_id 是否对应真实工具执行（由工具调用方产生）

    # ---- 契约规范名（只读别名，不改变持久化结构）----
    @property
    def from_agent(self) -> str:
        return self.sender

    @property
    def to_agent(self) -> str:
        return self.receiver

    @property
    def message_type(self) -> str:
        return self.type

    @property
    def dedup_key(self) -> str:
        """幂等键：同一 run 内同任务同 Agent 同类型消息只应出现一次。"""
        return "%s|%s|%s|%s" % (self.run_id, self.task_id, self.sender, self.type)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_trace_dict(self) -> Dict[str, Any]:
        """导出为统一轨迹使用的消息结构：**同时**含规范名与历史名。"""
        d = asdict(self)
        d.update({
            "from_agent": self.sender,
            "to_agent": self.receiver,
            "message_type": self.type,
            "dedup_key": self.dedup_key,
        })
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Message":
        keys = ("task_id", "sender", "receiver", "type", "state_version", "payload",
                "status", "timestamp", "msg_id", "run_id", "parent_task_id", "trace_id",
                "agent_id", "tool_call_id", "execution_mode", "payload_schema_version",
                "error_code", "kind", "kinds", "tool_call_real")
        kw = {}
        for k in keys:
            if k == "payload":
                kw[k] = d.get(k, {}) or {}
            elif k == "kinds":
                kw[k] = list(d.get(k) or [])
            elif k == "tool_call_real":
                kw[k] = bool(d.get(k))
            elif k == "payload_schema_version":
                kw[k] = d.get(k) or MSG_SCHEMA_VERSION
            else:
                kw[k] = d.get(k, "")
        # 兼容：旧消息可能只带执行模式空串
        return cls(**kw)

    def validate(self) -> List[str]:
        errs = []
        if self.sender not in VALID_AGENTS and self.sender != "USER":
            errs.append(f"非法 sender: {self.sender}")
        if self.receiver not in VALID_AGENTS:
            errs.append(f"非法 receiver: {self.receiver}")
        if self.type not in MSG_TYPES:
            errs.append(f"非法 type: {self.type}")
        if not isinstance(self.state_version, int) or self.state_version < 0:
            errs.append("state_version 必须为非负整数")
        if not isinstance(self.payload, dict):
            errs.append("payload 必须为 dict")
        if self.execution_mode and self.execution_mode not in EXECUTION_MODES:
            errs.append(f"非法 execution_mode: {self.execution_mode}")
        if self.error_code and self.error_code not in ERROR_CODES:
            errs.append(f"非法 error_code: {self.error_code}")
        if not self.run_id:
            errs.append("run_id 不得为空（§五 可追溯契约）")
        if not self.payload_schema_version:
            errs.append("payload_schema_version 不得为空")
        # V26.1 §四-P0-3：只有真实工具调用才允许带 tool_call_id，禁止伪造。
        if self.kind and self.kind not in TRACE_KINDS:
            errs.append(f"非法 kind: {self.kind}")
        if self.tool_call_id and not self.tool_call_real:
            errs.append("tool_call_id 只能出现在真实工具调用（tool_call_real=True）上，禁止伪造")
        if self.tool_call_id and KIND_TOOL_CALL not in (self.kinds or []):
            errs.append("带 tool_call_id 的消息必须在 kinds 中声明 TOOL_CALL")
        return errs


# 全局状态 Schema（确定性结构）
GLOBAL_STATE_SCHEMA = {
    "session_id": str,
    "run_id": str,                 # 最近一次运行的全局 ID（V26.0）
    "state_version": int,
    "user_constraints": dict,      # A3 提取并维护的游客约束
    "itinerary": dict,             # A3 生成的行程（按天）
    "conversation_turns": list,    # 多轮对话记录
    "agent_status": dict,          # 各 Agent 最近一次执行状态
    "verification": dict,          # A5 审核结果
    "gates": dict,                 # 三层门禁结果
    "realtime_data": dict,         # A4 获取的实时数据（天气等）
    "execution_mode": str,         # 本轮整体执行模式（model / deterministic / tool_only）
}

# 约束字段定义（A3 提取目标）
CONSTRAINT_FIELDS = [
    "party_size", "party_composition", "budget_yuan", "days",
    "region", "interests", "requirements", "intensity",
    "transport_pref", "start_city", "special_notes",
]

# 意图 -> 需调度的 Agent 映射（DAG 起点由 A0 决定）
INTENT_ROUTING = {
    INTENT_TRAVEL: ["A3", "A4", "A1", "A5"],
    INTENT_CULTURE: ["A1", "A5"],
    INTENT_ECOLOGY: ["A2", "A5"],
    INTENT_REVISE: ["A3", "A4", "A5"],
    INTENT_MIXED: ["A1", "A3", "A4", "A5"],
    INTENT_UNKNOWN: ["A1", "A5"],
}

# -*- coding: utf-8 -*-
"""
A0 Orchestrator 总控协调智能体（冻结版 v1.0）

职责（对照任务书「一、二」）：
- 唯一总控：意图理解、任务拆解、调度、结果汇总。
- 所有跨 Agent 任务由 A0 统一分发，子 Agent 不直接互调、不改全局状态。
- Supervisor + 有限 DAG：无依赖任务可并行（此处按确定性顺序记录以可复现），
  存在依赖时顺序执行（如 A3 先产出约束，A5 后审核）。
- 超时/失败/旧状态冲突：通过 state_version 与 status 枚举明确处理。
- A5 审核后仍需通过确定性 Release Gate 才允许标记业务完成。
"""
import re
import time
import uuid
from contracts import (Message, VALID_AGENTS, INTENT_TRAVEL, INTENT_CULTURE,
                       INTENT_ECOLOGY, INTENT_REVISE, INTENT_MIXED, INTENT_UNKNOWN,
                       INTENT_CONTENT, CONTENT_MARKERS, WRITE_VERB_MARKERS,
                       STRONG_REVISION_MARKERS, FORM_CONVERT_MARKERS,
                       INTENT_ROUTING, EXECUTION_MODES, DAG_SPEC, check_dag,
                       DEFAULT_MAX_ATTEMPTS, DEFAULT_TIMEOUT_MS,
                       ERR_NONE, ERR_TOOL_FAILED, ERR_TOOL_TIMEOUT, ERR_UPSTREAM_FAIL,
                       KIND_LLM_CALL, KIND_TOOL_CALL, KIND_DETERMINISTIC_STEP,
                       KIND_AGENT_DISPATCH, KIND_AGENT_RESULT, KIND_GATE_CHECK,
                       TRACE_KINDS, MSG_TYPE_TO_KIND,
                       TOOL_EVIDENCE_PREFIXES, NON_TOOL_EVIDENCE_PREFIXES)
from state_store import StateStore
from gates import evaluate_all, evaluate_publish_ready
import agents as A

# 解析执行模式（V26.0）：模型可用时如实标注，不可用时明确 deterministic。
try:
    import helan_llm as _LLM
except Exception:  # 适配层缺失不得阻断引擎
    _LLM = None


def _overall_execution_mode() -> str:
    if _LLM is None:
        return "deterministic"
    try:
        return _LLM.resolve_execution_mode()["execution_mode"]
    except Exception:
        return "deterministic"


# 文旅领域词（detect_intent 的最终兜底与内容创作领域相关性判断共用同一来源）。
# 与历史 domain 列表一致，仅新增「攻略」（用于「帮我写一篇攻略」这类写作动词+
# 领域相关性判断；对主路由无影响——含「攻略」的句子本就走 has_travel 分支）。
_DOMAIN_WORDS = ["宁夏", "贺兰", "西夏", "岩画", "酒", "羊肉", "生态", "岩羊", "沙湖", "镇北堡",
                 "黄河", "行程", "玩", "游", "规划", "预算", "景点", "旅游", "中卫", "银川",
                 "石嘴山", "吴忠", "固原", "定边", "美食", "文化", "历史", "沙漠", "天气", "气候",
                 "研学", "亲子", "住宿", "交通", "攻略"]


def is_form_conversion(text: str) -> bool:
    """内容形态转换判定（V31.3 补充修复，与 detect_intent / engine 共用的唯一真源）。

    句中同时含**内容名词**（小红书/Vlog/分镜/文案…）与**形态转换标记**
    （改成/换成/改为/变成）时，用户的动作对象是内容的产出形态而非行程元素：
    「改成 Vlog 分镜脚本」「换成小红书风格」=> 内容创作请求。
    与行程修订互斥的判据是内容名词：「把西夏陵去掉，换成水洞沟。」不含内容名词，
    仍是行程修订（D06/D07/D08 回归锁定）。
    """
    return (any(k in text for k in CONTENT_MARKERS)
            and any(k in text for k in FORM_CONVERT_MARKERS))


# 数值出行约束识别（V32.2 P0-2）：「4人3天5000元」「四人三天」「预算X元」等
# 无显式「旅游/规划」动词但显然是行程规划的输入，不得因命中文化名词而误判为 culture_qa。
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


def _num_before(text: str, unit: str):
    """找 unit 前的数字（阿拉伯或中文），返回 float 或 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*" + unit, text)
    if m:
        return float(m.group(1))
    for cn, v in _CN_NUM.items():
        if re.search(r"(" + cn + r")\s*" + unit, text):
            return float(v)
    return None


def _has_travel_constraints(text: str) -> bool:
    p = _num_before(text, "人")
    d = _num_before(text, "天")
    y = _num_before(text, "元") or ("预算" in text)
    return bool((p and d) or (d and y) or (p and y))


def detect_intent(text: str, is_revision: bool = False) -> str:
    """确定性意图识别（已将葡萄酒/美食/岩羊等纳入文化/生态问答路由）。

    V31.3 P0-1 修复内容：
    1) 内容创作意图优先：句中带明确内容创作语义（小红书/文案/Vlog/分镜/种草…），
       或「泛写作动词 + 文旅领域相关性」（「帮我写一篇宁夏旅游攻略」），
       且**不含**强修订标记时，判为 content_create，不再被误路由去重新规划行程。
    2) 「攻略」歧义治理：单独出现的「攻略」（如「宁夏旅游攻略怎么安排」）仍是旅行规划；
       仅与内容创作语义共现时才判内容创作。
    3) 领域外写作请求（「帮我写个Python快排脚本」）不得判内容创作——由 A1 诚实拒答。
    4) 形态转换例外（V31.3 补充）：内容名词 + 「改成/换成」共现时（「改成 Vlog 分镜脚本」）
       是内容形态转换请求，即使含强修订标记也判 content_create——修订标记的宾语
       是内容形态而非行程元素；无内容名词的「换成」（「把西夏陵换成水洞沟」）不受影响。
    """
    has_content = any(k in text for k in CONTENT_MARKERS)
    write_verb = any(k in text for k in WRITE_VERB_MARKERS)
    domain_hit = any(d in text for d in _DOMAIN_WORDS)
    strong_rev = any(k in text for k in STRONG_REVISION_MARKERS)
    form_convert = has_content and is_form_conversion(text)
    # 内容创作优先于修订/旅行判定（修订句中夹带创作诉求，如「顺便帮我写一篇小红书」；
    # 内容形态转换句「改成 Vlog 分镜脚本」亦不得被强修订标记劫持成行程修订）
    if (has_content or (write_verb and domain_hit)) and (not strong_rev or form_convert):
        return INTENT_CONTENT
    if is_revision:
        return INTENT_REVISE
    q_markers = ["？", "?", "为什么", "为何", "是什么", "怎么", "怎样", "哪些", "介绍",
                 "特点", "背景", "讲究", "故事", "有哪些", "几级", "有名", "如何", "什么", "吗"]
    eco_nouns = ["生态", "岩羊", "动物", "植物", "保护区", "湿地", "植被", "自然", "治沙"]
    culture_nouns = ["文化", "岩画", "西夏", "历史", "贺兰山", "葡萄酒", "红酒", "手抓羊肉",
                    "羊肉", "美食", "镇北堡", "沙湖", "博物馆", "遗产", "王朝", "郡主"]
    has_travel = (any(k in text for k in ["去宁夏", "去中卫", "去银川", "玩", "行程", "规划",
                                          "几天", "预算", "路线", "安排", "游", "旅游", "出行",
                                          "打卡", "亲子", "研学", "途经", "考虑", "涉及", "含",
                                          "包括", "打算", "准备", "计划", "出发", "游玩", "考察"])
                 or _has_travel_constraints(text))
    # 「攻略」歧义（V31.3 P0-1）：已判内容创作的句子在上面已返回；
    # 此处仍把「攻略」视为旅行需求标记（「宁夏旅游攻略」= 规划需求）。
    if "攻略" in text and not has_content:
        has_travel = True
    has_culture = any(k in text for k in culture_nouns)
    has_eco = any(k in text for k in eco_nouns)
    is_question = any(q in text for q in q_markers)
    # 纯知识性问题（含问号/疑问词）且涉文旅领域 -> 文化/生态问答
    if is_question and (has_culture or has_eco) and not has_travel:
        return INTENT_ECOLOGY if has_eco else INTENT_CULTURE
    # V31.3 P0-1：强修订标记（去掉/换成/第X天/加个…）表达的是**行程改动动作**，
    # 动作优先于话题名词——「把西夏陵去掉，换成水洞沟。」含文化名词「西夏」，
    # 但真实语义是修订，不得因话题匹配被判成知识问答（D06/D07/D08 回归锁定）。
    # 问句（含疑问词）已在上面拦截：「为什么去掉西夏陵？」仍是知识问答。
    if strong_rev:
        return INTENT_REVISE
    if has_travel and (has_culture or has_eco):
        return INTENT_MIXED
    if has_travel:
        return INTENT_TRAVEL
    if has_eco:
        return INTENT_ECOLOGY
    if has_culture:
        return INTENT_CULTURE
    # 领域外（如 iPhone / Python 快排）-> 交由 A1 诚实拒答
    if not domain_hit:
        return INTENT_UNKNOWN
    return INTENT_TRAVEL


class Orchestrator:
    def __init__(self, session_id: str = "demo", persist_path=None):
        self.store = StateStore(session_id, persist_path)
        self.trace: list = []  # 结构化消息轨迹（**每次 run() 重置**，保证 run 隔离）
        self.run_id: str = ""  # 当前运行 ID（每次 run() 重新生成）
        self.root_task_id: str = ""  # 当前运行的根任务 ID（子任务 parent 指向它）
        # V26.1 §四-P0-1：区分「配置能力」与「本次实际执行模式」，二者不得混用。
        self.configured_execution_mode: str = _overall_execution_mode()
        self.execution_mode: str = self.configured_execution_mode  # run() 后刷新为实际值
        self.execution_mode_detail: dict = {}
        self.run_history: list = []  # 历史 run 摘要（不含消息体，供跨轮追溯）

    def _emit(self, sender, receiver, mtype, state_version, payload, status, task_id,
              tool_call_id="", execution_mode="", parent_task_id="", error_code="",
              kind="", kinds=None, tool_call_real=False):
        """发出并记录一条结构化消息（V26.0 §五：12 必需字段全部填充）。

        V26.1 §四-P0-3：`kind` 标明事件种类；只有真实工具调用才允许传 tool_call_id。
        """
        msg = Message(task_id=task_id, sender=sender, receiver=receiver, type=mtype,
                      state_version=state_version, payload=payload, status=status,
                      run_id=self.run_id, parent_task_id=parent_task_id,
                      trace_id=self.run_id, agent_id=sender,
                      tool_call_id=tool_call_id,
                      execution_mode=execution_mode or "deterministic",
                      error_code=error_code or ERR_NONE,
                      kind=kind or MSG_TYPE_TO_KIND.get(mtype, ""),
                      kinds=list(kinds or []),
                      tool_call_real=bool(tool_call_real))
        self.trace.append(msg.to_dict())
        return msg

    # ---- V26.1 §四-P0-3：工具调用 ID 只能来自真实工具执行 ----
    @staticmethod
    def _real_tool_called(res: dict) -> bool:
        """该 Agent 本轮是否**确实**执行了工具（声明调用且留下工具证据）。"""
        if not isinstance(res, dict):
            return False
        if not res.get("tool_called"):
            return False
        return any(Orchestrator._is_tool_evidence(e)
                   for e in (res.get("tool_evidence") or []))

    @staticmethod
    def _is_tool_evidence(ev) -> bool:
        s = str(ev)
        if s.startswith(NON_TOOL_EVIDENCE_PREFIXES):
            return False
        return any(s.startswith(p) for p in TOOL_EVIDENCE_PREFIXES)

    @staticmethod
    def _kinds_of(res: dict) -> list:
        """列出该 Agent 本轮实际发生的事件种类（用于 RESULT 消息 kinds 字段）。"""
        kinds = []
        if not isinstance(res, dict):
            return kinds
        if res.get("execution_mode") == "model" and (res.get("model_evidence") or {}).get("ok"):
            kinds.append(KIND_LLM_CALL)
        if Orchestrator._real_tool_called(res):
            kinds.append(KIND_TOOL_CALL)
        if KIND_LLM_CALL not in kinds and KIND_TOOL_CALL not in kinds:
            kinds.append(KIND_DETERMINISTIC_STEP)
        return kinds

    @staticmethod
    def _new_tool_call_id(agent_id: str, res: dict) -> str:
        """真实工具调用才生成 ID；确定性步骤/模型推理/审核一律返回空串。"""
        if not Orchestrator._real_tool_called(res):
            return ""
        return "TC-%s-%s" % (agent_id, uuid.uuid4().hex[:8])

    # ---- V26.1 §四-P0-1/P0-3：模型调用必须作为**独立事件**留痕 ----
    def _emit_model_call(self, agent_id: str, res: dict, agent_task_id: str, parent_task_id: str):
        """为一次真实模型往返发出独立的 MODEL_CALL 事件。

        与 RESULT 消息分开记录的原因（§四 P0-3「区分 LLM_CALL / TOOL_CALL / …」）：
          · RESULT 描述「Agent 任务的产出」，MODEL_CALL 描述「一次对外模型请求的事实」，
            两者一一对应但不是同一事件，混在一起会让「真实模型调用」无法被独立核对；
          · 失败的调用**同样必须留痕**（P0-1）：不能因为请求失败就当没发生过，
            也不能把失败包装成成功；
          · 本事件**绝不携带 tool_call_id**（模型推理不是工具调用）。
        """
        if not isinstance(res, dict):
            return None
        ev = res.get("model_evidence") or {}
        if not (res.get("model_attempted") or ev):
            return None  # 本轮未发出请求 -> 不制造调用事实
        ok = bool(ev.get("ok"))
        payload = {
            "agent": agent_id,
            "ok": ok,
            "provider": ev.get("provider"),
            "model": ev.get("model"),
            "http_status": ev.get("http_status"),
            "latency_ms": ev.get("latency_ms"),
            "request_id": ev.get("request_id"),
            "response_sha256": ev.get("response_sha256"),
            "prompt_digest": res.get("model_prompt_digest"),
            "context_evidence_ids": res.get("model_context_evidence_ids"),
            "failure_kind": res.get("model_failure_kind", ""),
            "failure_reason": str(res.get("model_failure_reason") or ev.get("error") or "")[:300],
            "task_context_delivered": bool(res.get("model_prompt_digest")),
        }
        return self._emit(agent_id, "A0", "MODEL_CALL", self.store.state_version, payload,
                          "OK" if ok else "FAIL", agent_task_id,
                          parent_task_id=parent_task_id, execution_mode="model",
                          error_code=ERR_NONE if ok else ERR_UPSTREAM_FAIL,
                          kind=KIND_LLM_CALL, kinds=[KIND_LLM_CALL])

    # ---- V26.1 §四-P0-1/P0-4：按真实证据结算本轮执行模式 ----
    @staticmethod
    def _resolve_execution_mode(results: dict) -> dict:
        """从**本轮真实结果**推导实际执行模式（不看环境变量）。

        - 只有 execution_mode=="model" **且** model_evidence.ok==True 才算真实模型执行；
        - 声明 model 但无成功证据 -> 记为 model_claim_violation 并降级为 deterministic；
        - 失败的模型调用（有 ok=false 证据）单独登记，不删除调用事实。
        """
        per_agent, verified, attempts, failures, violations = {}, [], [], [], []
        for aid in ("A1", "A2", "A3", "A4", "A5"):
            r = results.get(aid)
            if not r:
                continue
            mode = r.get("execution_mode") or ""
            ev = r.get("model_evidence") or {}
            ev_ok = bool(ev.get("ok"))
            per_agent[aid] = mode or "deterministic"
            if mode == "model":
                if ev_ok:
                    verified.append(aid)
                else:
                    violations.append({"agent": aid, "reason": "声明 model 但无成功 model_evidence"})
                    per_agent[aid] = "deterministic"
            if r.get("model_attempted") or ev:
                rec = {"agent": aid, "ok": ev_ok,
                       "failure_kind": r.get("model_failure_kind", ""),
                       "reason": (r.get("model_failure_reason") or "")[:200],
                       "latency_ms": ev.get("latency_ms"),
                       "http_status": ev.get("http_status"),
                       "error": (ev.get("error") or "")[:200],
                       "request_id": ev.get("request_id", ""),
                       "prompt_sha256": ev.get("prompt_sha256", ""),
                       "fingerprint": r.get("model_prompt_digest")}
                attempts.append(rec)
                if not ev_ok:
                    failures.append(rec)
        return {
            "actual_execution_mode": "model" if verified else "deterministic",
            "model_agents_verified": verified,
            "per_agent": per_agent,
            "model_attempts": attempts,
            "model_failures": failures,
            "model_claim_violations": violations,
        }

    # ---- 真实外部工具调用的有限重试（不超过 DEFAULT_MAX_ATTEMPTS，线性退避）----
    @staticmethod
    def _with_retry(fn, attempts: int = DEFAULT_MAX_ATTEMPTS):
        """返回 (result, attempts_used, error_code)。不静默吞错，不伪造成功。"""
        attempts = max(1, attempts)
        for i in range(1, attempts + 1):
            try:
                res = fn()
            except Exception:  # 工具异常 -> 可重试
                if i < attempts:
                    time.sleep(min(0.4 * i, 1.2))
                    continue
                return None, i, ERR_TOOL_FAILED
            # 工具内部已做降级（返回结构但无任何调用留痕）-> 视为失败并重试
            if isinstance(res, dict) and res.get("tool_called") and not res.get("tool_evidence"):
                if i < attempts:
                    time.sleep(min(0.4 * i, 1.2))
                    continue
                return res, i, ERR_TOOL_FAILED
            return res, i, ERR_NONE
        return None, attempts, ERR_TOOL_FAILED

    def run(self, user_text: str, is_revision: bool = False, task_prefix: str = "T") -> dict:
        t0 = time.time()
        # ---- V26.1 §四-P0-4：每轮隔离 ----
        # 新任务/修订 = 新 run_id + **独立 trace**（不清空会话状态，但消息不得跨轮混入）。
        if self.run_id:
            self.run_history.append({
                "run_id": self.run_id, "task_id": self.root_task_id,
                "messages": len(self.trace),
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        self.trace = []
        self.run_id = "RUN-" + uuid.uuid4().hex[:12]
        self.store.set_run(self.run_id, by="A0", execution_mode=self.configured_execution_mode)
        intent = detect_intent(user_text, is_revision)
        task_id = f"{task_prefix}-{int(time.time()*1000)%100000:05d}"
        self.root_task_id = task_id
        self.store.add_turn("user", user_text)
        self._emit("USER", "A0", "DISPATCH", self.store.state_version,
                   {"text": user_text, "intent": intent}, "OK", task_id,
                   execution_mode="deterministic", kind=KIND_AGENT_DISPATCH,
                   kinds=[KIND_AGENT_DISPATCH])

        # 取出历史约束（多轮维护）
        prev_constraints = dict(self.store.constraints)
        prev_plan = dict(self.store.itinerary)

        results = {}
        required_agents = []
        a3_ok = True

        # ---- A3 行程规划（travel / mixed / revise 必经）----
        if intent in (INTENT_TRAVEL, INTENT_MIXED, INTENT_REVISE):
            required_agents.append("A3")
            atid = "%s#A3" % task_id
            new_req = user_text if is_revision else None
            r3 = A.agent_travel(user_text, prev_constraints if is_revision else None,
                                prev_plan if is_revision else None, new_req)
            results["A3"] = r3
            a3_ok = not r3.get("error")
            self.store.set_constraints(r3["constraints"], by="A3")
            self.store.set_itinerary(r3["itinerary"], by="A3")
            self.store.set_agent_status("A3", {"status": "OK" if a3_ok else "FAIL", "task_id": task_id,
                                               "tool_called": False,
                                               "execution_mode": r3.get("execution_mode", "deterministic"),
                                               "tool_evidence": r3.get("tool_evidence", [])})
            self.store.record_task(task_id=atid, parent_task_id=task_id, agent_id="A3",
                                   run_id=self.run_id, status="OK" if a3_ok else "FAIL",
                                   execution_mode=r3.get("execution_mode", "deterministic"),
                                   timeout_ms=DEFAULT_TIMEOUT_MS,
                                   error_code=ERR_NONE if a3_ok else ERR_UPSTREAM_FAIL,
                                   detail={"days": r3["itinerary"].get("days"),
                                           "revision": is_revision})
            self._emit("A0", "A3", "DISPATCH", self.store.state_version,
                       {"text": user_text, "revision": is_revision}, "OK", atid,
                       parent_task_id=task_id, execution_mode="deterministic",
                       kind=KIND_AGENT_DISPATCH, kinds=[KIND_AGENT_DISPATCH])
            # A3 不调用外部工具 -> 不生成 tool_call_id（确定性算法/模型叙述）
            self._emit("A3", "A0", "RESULT", self.store.state_version, r3,
                       "OK" if a3_ok else "FAIL", atid,
                       parent_task_id=task_id,
                       execution_mode=r3.get("execution_mode", "deterministic"),
                       error_code=ERR_NONE if a3_ok else ERR_UPSTREAM_FAIL,
                       kind=KIND_AGENT_RESULT, kinds=self._kinds_of(r3))
            self._emit_model_call("A3", r3, atid, task_id)

        # ---- A1 文化检索（mixed / culture / 领域外诚实拒答）----
        if intent in (INTENT_CULTURE, INTENT_MIXED, INTENT_UNKNOWN):
            required_agents.append("A1")
            atid = "%s#A1" % task_id
            subq = self._culture_subquestion(user_text)
            r1 = A.agent_culture(user_text, subq)
            results["A1"] = r1
            self.store.set_agent_status("A1", {"status": "OK", "task_id": task_id,
                                               "tool_called": True,
                                               "execution_mode": r1.get("execution_mode", "deterministic"),
                                               "tool_evidence": r1.get("tool_evidence",
                                                                       [f"BM25_RETRIEVE:{r1['evidence_count']}hits"])})
            # A1 真实调用 BM25 检索工具 -> 生成唯一 tool_call_id，账本与消息复用同一个
            tc1 = self._new_tool_call_id("A1", r1)
            self.store.record_task(task_id=atid, parent_task_id=task_id, agent_id="A1",
                                   run_id=self.run_id, status="OK",
                                   execution_mode=r1.get("execution_mode", "deterministic"),
                                   timeout_ms=DEFAULT_TIMEOUT_MS,
                                   tool_call_id=tc1,
                                   detail={"evidence_count": r1.get("evidence_count"),
                                           "topic": r1.get("topic")})
            self._emit("A0", "A1", "DISPATCH", self.store.state_version, {"q": subq}, "OK", atid,
                       parent_task_id=task_id, execution_mode="deterministic",
                       kind=KIND_AGENT_DISPATCH, kinds=[KIND_AGENT_DISPATCH])
            self._emit("A1", "A0", "RESULT", self.store.state_version, r1, "OK", atid,
                       tool_call_id=tc1, tool_call_real=bool(tc1),
                       parent_task_id=task_id,
                       execution_mode=r1.get("execution_mode", "deterministic"),
                       kind=KIND_AGENT_RESULT, kinds=self._kinds_of(r1))
            self._emit_model_call("A1", r1, atid, task_id)

        # ---- A2 生态检索（mixed / ecology）----
        if intent in (INTENT_ECOLOGY, INTENT_MIXED):
            required_agents.append("A2")
            atid = "%s#A2" % task_id
            r2 = A.agent_ecology(user_text)
            results["A2"] = r2
            self.store.set_agent_status("A2", {"status": "OK", "task_id": task_id,
                                               "tool_called": True,
                                               "execution_mode": r2.get("execution_mode", "deterministic"),
                                               "tool_evidence": r2.get("tool_evidence",
                                                                       [f"BM25_RETRIEVE:{r2['evidence_count']}hits"])})
            tc2 = self._new_tool_call_id("A2", r2)
            self.store.record_task(task_id=atid, parent_task_id=task_id, agent_id="A2",
                                   run_id=self.run_id, status="OK",
                                   execution_mode=r2.get("execution_mode", "deterministic"),
                                   timeout_ms=DEFAULT_TIMEOUT_MS,
                                   tool_call_id=tc2,
                                   detail={"evidence_count": r2.get("evidence_count"),
                                           "topic": r2.get("topic")})
            self._emit("A0", "A2", "DISPATCH", self.store.state_version, {"q": user_text}, "OK", atid,
                       parent_task_id=task_id, execution_mode="deterministic",
                       kind=KIND_AGENT_DISPATCH, kinds=[KIND_AGENT_DISPATCH])
            self._emit("A2", "A0", "RESULT", self.store.state_version, r2, "OK", atid,
                       tool_call_id=tc2, tool_call_real=bool(tc2),
                       parent_task_id=task_id,
                       execution_mode=r2.get("execution_mode", "deterministic"),
                       kind=KIND_AGENT_RESULT, kinds=self._kinds_of(r2))
            self._emit_model_call("A2", r2, atid, task_id)

        # ---- A4 旅行数据（travel / mixed / revise）----
        if intent in (INTENT_TRAVEL, INTENT_MIXED, INTENT_REVISE):
            required_agents.append("A4")
            atid = "%s#A4" % task_id
            cities = list(self.store.itinerary.get("used_cities", ["银川"]))
            r4, attempts4, err4 = self._with_retry(
                lambda: A.agent_travel_data(cities, self.store.constraints))
            if r4 is None:
                r4 = {"agent": "A4", "weather": {}, "transport": {}, "tool_called": False,
                      "tool_evidence": [], "error_code": err4 or ERR_TOOL_FAILED}
            results["A4"] = r4
            if r4.get("weather"):
                self.store.set_realtime("weather", r4["weather"], by="A4")
            # 诚实降级（天气失败但已真实调用并留痕）视为执行成功，不阻断发布；
            # 仅当“声明调用却无任何证据”才判 FAIL（疑似伪造）。
            a4_ok = bool(r4["tool_called"] and r4.get("tool_evidence"))
            a4_errcode = ERR_NONE if a4_ok else (err4 or ERR_TOOL_FAILED)
            self.store.set_agent_status("A4", {"status": "OK" if a4_ok else "FAIL",
                                               "degraded": not any(v.get("ok") for v in r4["weather"].values()),
                                               "task_id": task_id, "tool_called": r4["tool_called"],
                                               "execution_mode": "tool_only",
                                               "attempts": attempts4,
                                               "tool_evidence": r4["tool_evidence"]})
            tc4 = self._new_tool_call_id("A4", r4)
            self.store.record_task(task_id=atid, parent_task_id=task_id, agent_id="A4",
                                   run_id=self.run_id, status="OK" if a4_ok else "FAIL",
                                   execution_mode="tool_only", attempts=attempts4,
                                   timeout_ms=DEFAULT_TIMEOUT_MS, error_code=a4_errcode,
                                   tool_call_id=tc4,
                                   detail={"cities": cities,
                                           "degraded": not any(v.get("ok") for v in r4["weather"].values())})
            self._emit("A0", "A4", "DISPATCH", self.store.state_version, {"cities": cities}, "OK", atid,
                       parent_task_id=task_id, execution_mode="deterministic",
                       kind=KIND_AGENT_DISPATCH, kinds=[KIND_AGENT_DISPATCH])
            self._emit("A4", "A0", "RESULT", self.store.state_version, r4,
                       "OK" if a4_ok else "FAIL", atid,
                       tool_call_id=tc4, tool_call_real=bool(tc4),
                       parent_task_id=task_id, execution_mode="tool_only",
                       error_code=a4_errcode,
                       kind=KIND_AGENT_RESULT, kinds=self._kinds_of(r4))

        # ---- 内容创作（V31.3 P0-1：与行程规划解耦的独立意图）----
        # 游客说「帮我写一篇小红书攻略」时，基于会话**既有行程**生成内容，
        # 不再被误路由去重新规划行程（内容创作与 A3 修订是两条路径）。
        if intent == INTENT_CONTENT:
            required_agents.append("A0")  # 内容创作失败必须反映到执行门禁（不静默）
            atid = "%s#CONTENT" % task_id
            rc = A.agent_content(user_text, self.store,
                                 {"results": results} if results else None)
            results["CONTENT"] = rc
            content_ok = bool(rc.get("ok"))
            self.store.set_agent_status("A0", {"status": "OK" if content_ok else "FAIL",
                                               "task_id": task_id,
                                               "tool_called": False,
                                               "execution_mode": "deterministic",
                                               "tool_evidence": rc.get("tool_evidence",
                                                                       ["DETERMINISTIC_ALGO:content_generate"]),
                                               "error": rc.get("error", "")})
            self.store.record_task(task_id=atid, parent_task_id=task_id, agent_id="A0",
                                   run_id=self.run_id, status="OK" if content_ok else "FAIL",
                                   execution_mode="deterministic",
                                   timeout_ms=DEFAULT_TIMEOUT_MS,
                                   error_code=ERR_NONE if content_ok else ERR_UPSTREAM_FAIL,
                                   detail={"kind": rc.get("kind"), "mode": rc.get("mode"),
                                           "error": rc.get("error", "")})
            self._emit("A0", "A0", "QUERY", self.store.state_version,
                       {"text": user_text, "kind": rc.get("kind"), "ok": content_ok,
                        "error": rc.get("error", "")},
                       "OK" if content_ok else "FAIL", atid,
                       parent_task_id=task_id, execution_mode="deterministic",
                       error_code=ERR_NONE if content_ok else ERR_UPSTREAM_FAIL,
                       kind=KIND_DETERMINISTIC_STEP, kinds=[KIND_DETERMINISTIC_STEP])

        # ---- A5 审核（所有路径必经）----
        required_agents.append("A5")
        atid5 = "%s#A5" % task_id
        r5 = A.agent_verifier(self.store.snapshot()["state"], results.get("A1"), results.get("A2"),
                              results.get("A3"), results.get("A4"))
        results["A5"] = r5
        self.store.set_verification(r5, by="A5")
        self.store.set_agent_status("A5", {"status": "OK", "task_id": task_id, "verdict": r5["verdict"],
                                           "execution_mode": "deterministic",
                                           "tool_evidence": r5.get("tool_evidence", [])})
        self.store.record_task(task_id=atid5, parent_task_id=task_id, agent_id="A5",
                               run_id=self.run_id, status="OK",
                               execution_mode="deterministic", timeout_ms=DEFAULT_TIMEOUT_MS,
                               detail={"verdict": r5["verdict"], "issues": len(r5.get("issues", []))})
        self._emit("A0", "A5", "DISPATCH", self.store.state_version, {"scope": "all"}, "OK", atid5,
                   parent_task_id=task_id, execution_mode="deterministic",
                   kind=KIND_AGENT_DISPATCH, kinds=[KIND_AGENT_DISPATCH])
        # A5 为确定性规则审核，不调用外部工具 -> 不生成 tool_call_id
        self._emit("A5", "A0", "RESULT", self.store.state_version, r5,
                   "OK" if r5["verdict"] == "PASS" else "FAIL", atid5,
                   parent_task_id=task_id, execution_mode="deterministic",
                   error_code=ERR_NONE if r5["verdict"] == "PASS" else ERR_UPSTREAM_FAIL,
                   kind=KIND_AGENT_RESULT, kinds=[KIND_DETERMINISTIC_STEP])

        # ---- 门禁裁决 ----
        gates = evaluate_all(self.store.snapshot()["state"], required_agents, r5["verdict"])
        self.store.set_gates(gates, by="A0")
        self._emit("A0", "A0", "GATE", self.store.state_version, gates, "OK", task_id,
                   execution_mode="deterministic",
                   kind=KIND_GATE_CHECK, kinds=[KIND_GATE_CHECK])

        self.store.add_turn("assistant", self._summarize(results, gates))
        elapsed = round((time.time() - t0) * 1000, 1)

        # ---- V26.1 §四-P0-1：结算「本次实际执行模式」（依据真实证据，不看环境变量）----
        mode_detail = self._resolve_execution_mode(results)
        self.execution_mode = mode_detail["actual_execution_mode"]
        self.execution_mode_detail = {
            "configured_execution_mode": self.configured_execution_mode,
            **mode_detail,
        }
        for aid, mode in mode_detail["per_agent"].items():
            if results.get(aid) is not None:
                results[aid]["actual_execution_mode"] = mode
        fault = bool(mode_detail["model_failures"] or mode_detail["model_claim_violations"])

        # ---- V26.1 §七-②：本轮没有 A3 时，必须让调用方仍能看到**会话当前有效行程** ----
        # 纯知识问答轮不运行 A3（不得伪造 A3 执行，见 P0-3），但已有行程不应因此在
        # 响应里「消失」——否则 Web/WorkBuddy/第三方客户端会各自猜测是否保留旧行程。
        # 因此显式区分来源：this_run（本轮 A3 产出）/ carried_over（沿用既有行程）/ none。
        store_itin = dict(self.store.itinerary or {})
        if results.get("A3"):
            current_itin = results["A3"].get("itinerary") or {}
            itin_origin = "this_run"
        elif store_itin.get("days_plan"):
            current_itin = store_itin
            itin_origin = "carried_over"
        else:
            current_itin, itin_origin = {}, "none"

        # ---- V32.2 §二：独立发布就绪评估（证据驱动，与三层结构门禁正交）----
        # 不修改任何门禁结论；release_pass 仍可为真（结构合规），但发布就绪状态
        # 独立反映预算/交通/数据源的真实证据齐备度，且不被 A5 PASS 反向覆盖。
        publish_ready = evaluate_publish_ready(
            plan=current_itin,
            a4=results.get("A4") or {},
            constraints=(results.get("A3") or {}).get("constraints") or {},
        )

        return {
            "run_id": self.run_id,
            "task_id": task_id,
            "intent": intent,
            "execution_mode": self.execution_mode,                    # 本次**实际**执行模式
            "configured_execution_mode": self.configured_execution_mode,  # 配置能力
            "execution_mode_detail": self.execution_mode_detail,
            "fault_injected_or_degraded": fault,
            "current_itinerary": current_itin,
            "itinerary_origin": itin_origin,
            "results": results,
            "gates": gates,
            "state_version": self.store.state_version,
            "elapsed_ms": elapsed,
            "trace": self.trace,
            "task_ledger": list(self.store.task_ledger),
            "dag": DAG_SPEC,
            "dag_check": check_dag(),
            "release_pass": gates["release"]["pass"],
            # V32.2 §二：独立发布就绪状态（READY/CONDITIONAL/NOT_READY），
            # 与 release_pass 正交；前端/评委据此区分「结构合规」与「证据齐备可发布」。
            "publish_ready": publish_ready,
        }

    @staticmethod
    def _culture_subquestion(text: str) -> str:
        for kw in ["贺兰山文化", "岩画", "西夏", "历史", "文化"]:
            if kw in text:
                return f"请介绍宁夏的{kw}相关背景与可验证资料来源"
        return "请介绍宁夏相关文化背景"

    @staticmethod
    def _summarize(results, gates) -> str:
        parts = []
        if "A3" in results:
            it = results["A3"]["itinerary"]
            parts.append(f"已生成{it.get('days')}天行程，覆盖城市：{','.join(it.get('used_cities', []))}；"
                         f"预算说明：{it.get('budget_note', '')[:30]}…")
        if "A1" in results:
            parts.append(f"文化检索命中{results['A1']['evidence_count']}条证据。")
        if "A4" in results:
            ok = [c for c, w in results["A4"]["weather"].items() if w.get("ok")]
            parts.append(f"实时天气已查询：{','.join(ok) if ok else '无（待核实）'}。")
        if "CONTENT" in results:
            ct = results["CONTENT"]
            if ct.get("ok"):
                parts.append("内容创作完成（%s，模板生成）。" % ct.get("kind"))
            else:
                parts.append("内容创作未完成：%s" % ct.get("error", "未知原因"))
        parts.append("Release Gate: " + ("PASS" if gates["release"]["pass"] else "FAIL"))
        return "；".join(parts)

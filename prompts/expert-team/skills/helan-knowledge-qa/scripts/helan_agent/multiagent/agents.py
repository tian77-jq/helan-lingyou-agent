# -*- coding: utf-8 -*-
"""
子 Agent 实现（A1 Culture / A2 Ecology / A3 TravelPlanner / A4 TravelData / A5 Verifier）

设计约束（对照任务书「一、架构先冻结」）：
- 子 Agent 不得互相直接调度或修改全局状态；只能通过返回值把结果交回 A0。
- 模型负责推理/生成，确定性代码负责状态、计算、权限与校验。
- 凡声明调用工具，必须产生 tool_evidence，否则 A5/ExecutionGate 判为疑似伪造。
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
# 确保可导入同属 helan_agent 包的 qa_engine / evidence_gate
sys.path.insert(0, os.path.join(HERE, ".."))
CORPUS_DIR = os.path.join(HERE, "..", "..", "corpus")
ATTR_PATH = os.path.join(CORPUS_DIR, "attractions.json")

# 复用已验证的 MVP-A 检索与证据门
from qa_engine import answer as _qa_answer
from evidence_gate import evaluate as _evidence_evaluate

# 真实模型调用适配层（V26.0）：未配置模型时 HELAN_AGENT_MODE=deterministic，
# 行为与既有版本完全一致；配置后 Agent 执行真实模型调用并留存 model_call 证据。
try:
    import helan_llm as _LLM
except Exception:  # 适配层不可用时不得中断引擎，如实降级
    _LLM = None

# V30.0 / V31.3：引用渲染统一走 citation_policy（Web/MCP/内容创作同源，
# 本机路径与内部资料标识不得伪装成公开网址）。
try:
    import citation_policy as _CP
except Exception:
    _CP = None


def _llm_mode() -> str:
    if _LLM is None:
        return "deterministic"
    try:
        return _LLM.resolve_execution_mode()["execution_mode"]
    except Exception:
        return "deterministic"


def _model_synthesize(agent_id: str, purpose: str, system_prompt: str, user_prompt: str,
                      fallback_text: str, fallback_citations: list = None) -> dict:
    """调用真实模型做「基于已检索证据」的表述综合；失败则原样返回确定性文本。

    红线（V26.1 §四-P0-1）：**失败不得删除调用事实**。
    - 未配置模型端点 -> 请求未发出，如实标 model_attempted=False（无失败证据可留）；
    - 已配置但调用失败 -> 必须保留 ok=false 的脱敏证据（http_status/latency/error_type）
      与失败原因，并标 model_failed=True，退回确定性文本；
    - 绝不允许把失败包装为成功，也不允许因失败就抹掉「曾调用过模型」这一事实。
    """
    base = {"mode": "deterministic", "text": fallback_text, "evidence": None,
            "answer_source": "deterministic", "tool_evidence": [],
            "model_attempted": False, "model_failed": False,
            "model_failure_reason": "", "model_failure_kind": ""}
    if _LLM is None:
        base["model_failure_reason"] = "模型适配层未随本次部署打包"
        return base
    out = _LLM.model_answer(agent_id, purpose, system_prompt, user_prompt)
    ev = out.get("evidence")            # 失败时同样携带证据（provider 层已脱敏）
    failed = out.get("mode") != "model" or not out.get("text")
    if failed:
        attempted = ev is not None      # 有证据 == 请求确实发出过（配置齐全）
        reason = out.get("reason") or "模型返回为空"
        return {
            **base,
            "model_attempted": attempted,
            "model_failed": attempted,  # 只有「确实发出且失败」才算失败，未配置不算失败
            "model_failure_reason": reason,
            "model_failure_kind": _classify_model_failure(ev, reason),
            "evidence": ev,             # ← 关键：保留失败证据，不删除调用事实
            "answer_source": ("deterministic+model_failed" if attempted
                              else base["answer_source"]),
        }
    return {
        "mode": "model",
        "text": out["text"].strip(),
        "evidence": ev,
        "answer_source": "model+retrieval",
        "tool_evidence": ["MODEL_CALL:%s:%s:%sms" % (
            ev.get("provider", "?"), ev.get("request_id") or ev.get("model", "?"),
            ev.get("latency_ms", "?"))],
        "model_attempted": True,
        "model_failed": False,
        "model_failure_reason": "",
        "model_failure_kind": "",
    }


def _classify_model_failure(ev, reason: str) -> str:
    """把模型失败归类为可核对的类型（不含密钥/原始 prompt）。"""
    if ev is None:
        return "not_configured" if "未检测到模型端点" in (reason or "") else "unavailable"
    status = int(ev.get("http_status") or 0)
    err = (ev.get("error") or "") + " " + (reason or "")
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    if "Timeout" in err or "timed out" in err:
        return "timeout"
    if "URLError" in err or "Connection" in err or "SSL" in err:
        return "network"
    if "响应解析失败" in err:
        return "bad_response"
    return "unknown"



_CULTURE_SYS = (
    "你是《贺兰有灵·灵游宁夏》的文化讲解智能体 A1（宁夏文化/西夏历史/贺兰山岩画方向）。"
    "硬性约束：只能依据用户提供的【检索证据】作答，不得引入证据之外的事实、数字或年份；"
    "证据不足时必须明确说明「现有资料未覆盖」，禁止编造。输出 3-6 句中文，不要输出来源清单。")
_ECOLOGY_SYS = (
    "你是《贺兰有灵·灵游宁夏》的生态科普智能体 A2（贺兰山生态/岩羊/保护方向）。"
    "硬性约束：只能依据用户提供的【检索证据】作答，不得编造物种分布、保护级别或统计数据；"
    "证据不足时必须明确说明。输出 3-6 句中文，不要输出来源清单。")
_PLAN_SYS = (
    "你是《贺兰有灵·灵游宁夏》的行程讲解智能体 A3。给定的行程与约束由确定性算法生成，"
    "你只能对其进行自然语言表述与理由说明，**不得修改天数、景点、预算金额或新增未列出的景点**。"
    "输出 3-5 句中文。")

# V26.1 §四-P0-2：模型提示必须携带**检索正文**、来源标识与知识边界，
# 不能只给概括性角色提示词（否则无法保证回答仅由检索证据支持）。
_KNOWLEDGE_BOUNDARY = (
    "【知识边界】只能依据上面【检索证据】中的正文作答；不得引入证据之外的事实、数字或年份；"
    "证据未覆盖的内容必须明确说明「现有资料未覆盖」。不要输出来源清单。")


def _evidence_body(e: dict) -> str:
    """取出检索证据正文。

    V26.1 实测缺陷：语料条目正文键名为 `content`，而旧实现读取 `text`，
    导致 A1/A2 发给模型的「检索证据」**一直是空的**（模型只能看到角色提示词）。
    此处按 content -> text -> snippet 顺序兼容，并保留 title 作为来源标识。
    """
    for k in ("content", "text", "snippet", "body"):
        v = e.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def build_grounded_prompt(question: str, evidence: list, max_items: int = 4) -> str:
    """把检索证据正文拼进模型提示（唯一入口，供 A1/A2 共用，可被测试直接断言）。

    返回字符串必须同时包含：证据编号、来源标识、**证据正文**、知识边界、原始问题。
    任何一项缺失都会使「基于证据作答」的约束失效，因此由单元测试强制校验。
    """
    lines, used, chars = [], 0, 0
    for i, e in enumerate((evidence or [])[:max_items], start=1):
        text = _evidence_body(e)
        if not text:
            continue
        used += 1
        chars += len(text)
        src = e.get("source_name") or e.get("source") or e.get("id") or "?"
        url = e.get("source_url") or e.get("source_ref") or "(无链接)"
        note = e.get("citation_note") or ""
        title = e.get("title") or ""
        head = "- [E%d] 来源=%s" % (i, src)
        if note:
            head += "（%s）" % note
        if title:
            head += " 标题=%s" % title
        lines.append("%s 链接=%s\n  正文：%s" % (head, url, text))
    body = "\n".join(lines) or "（本次未检索到可用证据正文）"
    prompt = ("【检索证据】（共 %d 条，节选 %d 条，正文 %d 字）\n%s\n\n%s\n\n【游客问题】%s\n\n"
              "请仅依据以上证据正文作答。" % (
                  len(evidence or []), used, chars, body, _KNOWLEDGE_BOUNDARY, question))
    return prompt


def prompt_digest(prompt: str) -> dict:
    """提示指纹（不落盘原文，避免敏感内容外泄；用于核对证据是否真的进了请求）。"""
    import hashlib
    return {"prompt_chars": len(prompt or ""),
            "prompt_sha256": hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:32]}


# 中文数字解析（支持「一家四口」「三天」等表述）
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s):
    if s is None:
        return None
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    if s in _CN:
        return _CN[s]
    if "十" in s:
        parts = s.split("十")
        left = parts[0]
        right = parts[1] if len(parts) > 1 else ""
        tens = _CN.get(left, 1) if left else 1
        ones = _CN.get(right, 0) if right else 0
        return tens * 10 + ones
    for ch in s:
        if ch in _CN:
            return _CN[ch]
    return None


_NUM_TOK = r"([0-9一二两三四五六七八九十]+)"


# ============================ A1 Culture ============================
_DOMAIN_KW = ["宁夏", "贺兰", "西夏", "岩画", "酒", "羊肉", "生态", "岩羊", "沙湖", "镇北堡",
              "黄河", "行程", "玩", "游", "规划", "预算", "景点", "旅游", "中卫", "银川",
              "石嘴山", "吴忠", "固原", "定边", "美食", "文化", "历史", "沙漠", "天气", "气候",
              "研学", "亲子", "住宿", "交通"]


def _in_domain(text: str) -> bool:
    return any(k in text for k in _DOMAIN_KW)


def agent_culture(query: str, sub_question: str = None) -> dict:
    """A1 文化智能体：宁夏文化检索、知识解释与 RAG 来源。

    领域外问题（如 iPhone 价格、Python 快排）诚实拒答，绝不编造文旅知识。
    """
    q = sub_question or query
    # 用「原始用户输入」判定领域，避免 sub_question 注入的"宁夏"造成误判（领域外问题应诚实拒答）
    if not _in_domain(query):
        return {
            "agent": "A1",
            "sub_question": q,
            "answer": ("抱歉，您的问题超出了《贺兰有灵·灵游宁夏》的知识范围"
                       "（仅覆盖宁夏文旅、贺兰山文化生态、行程规划等）。为避免误导，"
                       "建议您通过官方文旅渠道或景区公告进一步核实。"),
            "evidence_count": 0,
            "citations": [],
            "gate": {"grounding": "insufficient_evidence", "can_assert": False},
            "topic": "out_of_scope",
            "execution_mode": "deterministic",
            "answer_source": "rule:domain_gate",
            "tool_called": False,
            "tool_evidence": ["DOMAIN_GATE:out_of_scope"],
            "model_evidence": None,
        }
    res = _qa_answer(q, top_k=4)
    tool_ev = ["BM25_RETRIEVE:%dhits@domain/corpus" % res["evidence_count"]]
    out = {
        "agent": "A1",
        "sub_question": q,
        "answer": res["grounded_answer"],
        "evidence_count": res["evidence_count"],
        "citations": res["citations"],
        "gate": res["gate"],
        # V30.0：事实可断言性与流程结论分开传递（门禁 PASS ≠ 事实可断言）
        "claim_policy": res.get("claim_policy") or (res.get("gate") or {}).get("claim_policy") or {},
        "topic": res["detected_topic"],
        "tool_called": True,
        "tool_evidence": tool_ev,
    }
    # V26.0：配置真实模型时，基于检索证据做表述综合（不改引用、不放宽事实门）
    if res["evidence_count"] > 0:
        prompt = build_grounded_prompt(q, res.get("evidence", []))
        m = _model_synthesize(
            "A1", "culture_answer", _CULTURE_SYS, prompt,
            fallback_text=res["grounded_answer"], fallback_citations=res["citations"])
        out["model_prompt_digest"] = prompt_digest(prompt)
        out["model_context_evidence_ids"] = [e.get("id") for e in res.get("evidence", [])[:4]]
    else:
        m = {"mode": "deterministic", "text": res["grounded_answer"], "evidence": None,
             "answer_source": "deterministic+retrieval(no_hit)", "tool_evidence": [],
             "model_attempted": False, "model_failed": False,
             "model_failure_reason": "", "model_failure_kind": ""}
    out["execution_mode"] = ("model" if m["mode"] == "model" else "deterministic")
    out["answer_source"] = m["answer_source"]
    out["model_evidence"] = m["evidence"]
    out["model_attempted"] = m.get("model_attempted", False)
    out["model_failed"] = m.get("model_failed", False)
    out["model_failure_reason"] = m.get("model_failure_reason", "")
    out["model_failure_kind"] = m.get("model_failure_kind", "")
    out["tool_evidence"] = tool_ev + m["tool_evidence"]
    if m["mode"] == "model":
        out["answer"] = m["text"]
        out["deterministic_answer"] = res["grounded_answer"]
    return out


# ============================ A2 Ecology ============================
def agent_ecology(query: str, sub_question: str = None) -> dict:
    """A2 生态智能体：贺兰山生态、岩羊与生态科普。"""
    q = sub_question or query
    res = _qa_answer(q, top_k=4)
    # 生态优先过滤证据
    eco = [e for e in res["evidence"] if e.get("topic") == "ecology"]
    if eco:
        gate = _evidence_evaluate(eco, q)
        res2 = dict(res)
        res2["evidence"] = eco
        res2["evidence_count"] = len(eco)
        res2["gate"] = gate
        res2["citations"] = _citations_for(eco, res["citations"])
        return _finish_ecology(q, res["grounded_answer"], eco, len(eco),
                               res2["citations"], gate, "ecology")
    return _finish_ecology(q, res["grounded_answer"], res["evidence"],
                           res["evidence_count"], res["citations"],
                           res["gate"], res["detected_topic"])


def _citations_for(evidence: list, fallback_citations: list) -> list:
    """按证据条目反查引用，保证「正文 ↔ 引用」一一对应（不再按数量截断猜位）。"""
    by_id = {}
    for c in fallback_citations or []:
        for key in (c.get("id"), c.get("source_name"), c.get("source_url")):
            if key:
                by_id.setdefault(str(key), c)
    out = []
    for e in evidence or []:
        c = (by_id.get(str(e.get("id"))) or by_id.get(str(e.get("source_name")))
             or by_id.get(str(e.get("source_url"))))
        if c is None:
            c = {"id": e.get("id"), "source_name": e.get("source_name"),
                 "source_url": e.get("source_url"), "topic": e.get("topic")}
        out.append(c)
    return out


def _finish_ecology(q, grounded_answer, evidence, evidence_count, citations, gate, topic) -> dict:
    """A2 输出装配（含检索工具证据与可选的真实模型表述综合）。

    V26.1 §四-P0-2：模型提示必须携带**检索正文**（此前只传 source_name/source_url，
    导致模型无法仅凭证据作答），并记录提示指纹用于核对证据未被丢弃。
    """
    tool_ev = ["BM25_RETRIEVE:%dhits@domain/corpus" % evidence_count]
    out = {"agent": "A2", "sub_question": q, "answer": grounded_answer,
           "evidence_count": evidence_count, "citations": citations, "gate": gate,
           "topic": topic, "tool_called": True}
    if evidence_count > 0 and evidence:
        prompt = build_grounded_prompt(q, evidence)
        m = _model_synthesize("A2", "ecology_answer", _ECOLOGY_SYS, prompt,
                              fallback_text=grounded_answer, fallback_citations=citations)
        out["model_prompt_digest"] = prompt_digest(prompt)
        out["model_context_evidence_ids"] = [e.get("id") for e in evidence[:4]]
    else:
        m = {"mode": "deterministic", "text": grounded_answer, "evidence": None,
             "answer_source": "deterministic+retrieval(no_hit)", "tool_evidence": [],
             "model_attempted": False, "model_failed": False,
             "model_failure_reason": "", "model_failure_kind": ""}
    out["execution_mode"] = ("model" if m["mode"] == "model" else "deterministic")
    out["answer_source"] = m["answer_source"]
    out["model_evidence"] = m["evidence"]
    out["model_attempted"] = m.get("model_attempted", False)
    out["model_failed"] = m.get("model_failed", False)
    out["model_failure_reason"] = m.get("model_failure_reason", "")
    out["model_failure_kind"] = m.get("model_failure_kind", "")
    out["tool_evidence"] = tool_ev + m["tool_evidence"]
    if m["mode"] == "model":
        out["answer"] = m["text"]
        out["deterministic_answer"] = grounded_answer
    return out


# ============================ A3 TravelPlanner ============================
_INTEREST_KW = {
    "贺兰山文化": ["贺兰山", "岩画", "西夏", "历史", "文化", "遗址", "博物馆"],
    "美食": ["美食", "吃", "风味", "餐饮"],
    "研学": ["研学", "亲子", "学习", "教育"],
    "生态": ["生态", "自然", "动物", "湿地", "植被"],
    "摄影": ["摄影", "拍照", "光影"],
    "黄河": ["黄河"],
    "沙漠": ["沙漠", "沙坡头", "腾格里"],
    "葡萄酒": ["葡萄酒", "酒庄", "红酒", "贺兰山东麓", "葡萄"],
}
# 兴趣 -> 直接证据主题集合（V32.2 P0-1）：覆盖度统计只认「主题直接对应」的景点，
# 避免双龙石窟(石窟艺术)/青铜峡(黄河文化)因共享「文化/历史」泛标签被误算作贺兰山文化已覆盖。
_INTEREST_THEME_SET = {
    "贺兰山文化": {"贺兰山文化", "西夏文化", "贺兰山东麓葡萄酒文化"},
    "黄河": {"黄河文化"},
    "沙漠": {"沙漠地貌"},
    "葡萄酒": {"贺兰山东麓葡萄酒文化"},
    "生态": {"生态自然", "贺兰山生态"},
    "研学": set(),
}
_REQ_KW = {"住宿": "住宿", "交通": "交通", "美食": "特色美食", "研学": "亲子研学", "导游": "讲解"}


def detect_named_attractions(text: str) -> list:
    """从用户文本中识别显式点名的景点（首轮强制纳入行程，修订轮复用）。"""
    data = _load_attractions()
    hits = []
    for a in data["attractions"]:
        nm = a["name"]["zh-CN"]
        if nm and nm in text or a["id"] in text:
            hits.append(a["id"])
    return hits


def _load_attractions():
    with open(ATTR_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_constraints(text: str, prev: dict = None) -> dict:
    """确定性约束提取（关键词+正则），不依赖模型。"""
    c = dict(prev) if prev else {}
    # 人数（兼容：一家三口 / 三个人 / 两个人 / 一个人 / 20个初中生 / 两位老人 / 两个孩子 / 我和朋友两人）
    # 注意：_NUM_TOK 自身已是捕获组，勿再外套一层括号，否则编号错位。
    # V26.0 修复（场景 B 实测暴露的真实缺陷）：
    #   区分「总数」与「子集描述」——
    #     · 总数：一家N口 / N口人 / 我们N人 / 一共N人 ...      -> 始终可作为总人数
    #     · 子集：N位长辈 / N个孩子 / N位老人 ...（只是队伍的一部分）
    #   若文本只给出子集数量、且历史已存在总人数，则**不得**用子集覆盖总数。
    #   （原实现会把修订轮的「两位长辈」误写成 party_size=2，覆盖掉第一轮的「一家五口」。）
    _SUBSET_NOUNS = r"(?:长辈|老人|老年人|孩子|小孩|儿童|大人|朋友|同学|学生|初中生|大学生|师生|游客)"
    m = re.search(r"(?:一家)?\s*" + _NUM_TOK + r"\s*口|" + _NUM_TOK + r"\s*个?\s*(?:人|位|名|口|孩子|朋友|老人|同学|初中生|大学生|大人|小孩|游客|同行|师生)", text)
    if m:
        v = _cn_to_int(m.group(1) or m.group(2))
        tail = text[m.end():m.end() + 4]
        is_subset = bool(re.match(r"\s*" + _SUBSET_NOUNS, tail))
        explicit_total = bool(re.search(
            r"(?:一家|我们|咱们|全家|一共|总共)\s*" + _NUM_TOK + r"|" + _NUM_TOK + r"\s*口", text))
        if v:
            if is_subset and not explicit_total and prev and prev.get("party_size"):
                # 子集描述不得覆盖已知总人数；如实记录同行构成，供 A5 与报告展示
                note = (tail.strip() or "长辈/儿童")
                notes = c.get("special_notes") or ""
                if note not in notes:
                    c["special_notes"] = (notes + "；" if notes else "") + ("同行构成：" + note)
            else:
                c["party_size"] = v
    if "一家" in text or "亲子" in text or "孩子" in text:
        c["party_composition"] = "family_with_children"
    # 同行长辈/儿童年龄（V26.1 §七 修复的真实缺陷）
    #   背景：修订句「父亲58岁，母亲63岁，请重新核查门票和步行强度，预算尽量不变。」
    #   旧实现只识别「N位长辈」这类**数量**表达，无法从「角色+年龄」解析出同行构成，
    #   于是 special_notes 为空、A5 没有任何可核对依据，用户要求的「重新核查门票和步行强度」
    #   在留痕上完全落空。此处补上角色+年龄解析，并如实标注资格**存疑**点（58 岁通常
    #   未达多数景区老年优惠票的 60 周岁门槛），交由 A5 提示不确定事项，而非直接断言免票。
    _KIN_ELDER = r"(?:父亲|母亲|爸爸|妈妈|爷爷|奶奶|外公|外婆|祖父|祖母|岳父|岳母|公公|婆婆|父母|老人|长辈)"
    _KIN_CHILD = r"(?:孩子|小孩|儿子|女儿|儿童|宝宝|孙子|孙女|小朋友)"
    comps = list(c.get("companions", []))
    seen = {(x.get("role"), x.get("age")) for x in comps}
    for mm in re.finditer(r"(" + _KIN_ELDER + r"|" + _KIN_CHILD + r")\s*(?:今年|年)?\s*(\d{1,3})\s*岁", text):
        role, age = mm.group(1), int(mm.group(2))
        if (role, age) not in seen:
            comps.append({"role": role, "age": age,
                          "is_elder": bool(re.fullmatch(_KIN_ELDER, role)),
                          "elder_ticket_age_met": age >= 60})
            seen.add((role, age))
    if comps:
        c["companions"] = comps
        elders = [x for x in comps if x.get("is_elder") or x.get("age", 0) >= 60]
        if elders:
            desc = "、".join("%s%d岁" % (x["role"], x["age"]) for x in elders)
            uncertain = [x for x in elders if not x.get("elder_ticket_age_met")]
            if uncertain:
                desc += "（其中%s未达多数景区老年优惠票 60 周岁门槛，需按景区公示现场核实）" % (
                    "、".join("%s%d岁" % (x["role"], x["age"]) for x in uncertain))
            notes = c.get("special_notes") or ""
            tag = "同行长辈：" + desc
            if tag not in notes:
                c["special_notes"] = (notes + "；" if notes else "") + tag
            # 注意（真实回归教训）：用户点名要「重新核查门票 / 步行强度」属于**审核动作**，
            # 不是「行程需要覆盖的兴趣/需求」。若写进 requirements，
            # BusinessContractGate 会把它当成未被景点覆盖的需求而错误阻断发布。
            # 因此单独落到 verification_focus 字段，由 A5 消费。
            vf = set(c.get("verification_focus", []))
            vf.add("门票价格与老年优惠票资格")
            vf.add("步行强度与体力适配")
            c["verification_focus"] = sorted(vf)
    # 预算（兼容：预算3000 / 预算3000元 / 预算共10000元 / 预算提到8000 / 预算提高到8000）
    m = re.search(r"预算\s*[约共]?\s*(?:提高|涨|提到|调|改|加|到)?\s*(\d+)\s*元?|(\d+)\s*元\s*预算|预算\s*[约共]?\s*(\d+)", text)
    if m:
        c["budget_yuan"] = int(m.group(1) or m.group(2) or m.group(3))
    # 天数：优先识别「变成/共/到/改为 N 天」等表达总天数的句式（避免把「再加一天」误判为总天数）；
    # 否则兼容 X天 / X日 / X晚 / 玩X / 游X / 待X（排除「第N天」指代）。
    m_total = re.search(r"(?:变成|共|到|改为|调整[为到]?)\s*" + _NUM_TOK + r"\s*天", text)
    if m_total:
        v = _cn_to_int(m_total.group(1))
        if v:
            c["days"] = v
    else:
        m = re.search(r"(?<!第)(" + _NUM_TOK + r")\s*(?:天|日|晚)|玩\s*(" + _NUM_TOK + r")|游\s*(" + _NUM_TOK + r")|待\s*(" + _NUM_TOK + r")|住\s*(" + _NUM_TOK + r")", text)
        if m:
            v = _cn_to_int(m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(5))
            if v:
                c["days"] = v
    # V30.1 P1：修订轮出现「第N天」→ 天数下限为 N（真实缺陷修复）
    #   背景：首轮 1 天行程，游客说「第二天加沙坡头」，旧实现只认「变成/共/改为 N 天」
    #   与「X 天」，而「第二天」被 `(?<!第)` 排除，days 仍为 1 → A3 把沙坡头塞进第 1 天
    #   并置换掉原主线景点（贺兰山岩画）→ Business 门禁如实判 False，修订实际落空。
    #   语义：游客明确引用「第 N 天」即蕴含**至少 N 天**。
    #   仅在**修订轮**（prev 非空）生效，避免首轮「第三天想去看岩画」把行程撑成 3 天。
    if prev:
        ords = []
        for mm in re.finditer(r"第\s*" + _NUM_TOK + r"\s*天", text):
            v = _cn_to_int(mm.group(1))
            if v:
                ords.append(v)
        if ords:
            need = max(ords)
            if need > (c.get("days") or 0):
                c["days"] = need
                c["days_raised_by_ordinal"] = need
    # 强度（V26.0：补充显式「降低步行强度」类表达，供长辈同行的差量修订识别）
    if any(k in text for k in ["不要太累", "轻松", "慢", "不赶", "降低步行强度", "降低强度",
                               "减少步行", "少走路", "步行强度低", "行动不便", "舒缓"]):
        c["intensity"] = "low"
    elif any(k in text for k in ["紧凑", "充实", "多跑", "赶"]):
        c["intensity"] = "high"
    else:
        c.setdefault("intensity", "medium")
    # 长辈同行优先级最高且只降不升：避免「行程要充实」等措辞把长辈团拉到高强度。
    # （V26.1 §七：用户明确要求「重新核查…步行强度」，此处让判定结果具备可核对理由。）
    _elders = [x for x in c.get("companions", [])
               if x.get("is_elder") or x.get("age", 0) >= 60]
    if _elders and c.get("intensity") != "low":
        c["intensity"] = "low"
        c["intensity_reason"] = "同行长辈（%s），已按低强度安排" % "、".join(
            "%s%d岁" % (x["role"], x["age"]) for x in _elders)
    if _elders:
        c.setdefault("intensity_reason", "同行长辈，按低强度安排")
    # 地区
    if "宁夏" in text:
        c["region"] = "宁夏"
    c.setdefault("region", "宁夏")
    c.setdefault("start_city", "银川")
    # 兴趣
    interests = set(c.get("interests", []))
    for k, kws in _INTEREST_KW.items():
        if any(kw in text for kw in kws):
            interests.add(k)
    c["interests"] = sorted(interests)
    # 需求
    reqs = set(c.get("requirements", []))
    for k, _ in _REQ_KW.items():
        if k in text:
            reqs.add(k)
    c["requirements"] = sorted(reqs)
    c.setdefault("transport_pref", "待确认")
    return c


def _match_attractions(interests, attractions, must_include=None):
    must = set(must_include or [])
    scored = []
    for a in attractions:
        tags = set(a.get("tags", [])) | set(a.get("suitableFor", []))
        tags.add(a.get("category", ""))
        text = (a.get("intro", "") + a.get("name", {}).get("zh-CN", ""))
        score = 0
        for inte in interests:
            for kw in _INTEREST_KW.get(inte, []):
                if kw in tags or kw in text:
                    score += 2
        if a["id"] in must:
            score += 100
        if score > 0:
            scored.append((score, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored]


def build_itinerary(constraints: dict, must_include=None, prev_plan=None, preferred_day=None) -> dict:
    """确定性行程生成：单日单城分配，低强度每日<=2个点；支持修订优先日。

    关键约束（消除跨城混排）：每天只安排一个城市。must_include 所在城市
    从用户指定的 preferred_day 起排；其余城市（如 hub 银川）填满前面的天。
    同一城市景点可能跨多天，但任何一天不会同时出现两个城市。
    """
    data = _load_attractions()
    attractions = data["attractions"]
    foods = data["foods"]
    interests = set(constraints.get("interests", []))
    days = constraints.get("days", 3)
    intensity = constraints.get("intensity", "medium")
    max_per_day = 2 if intensity == "low" else (3 if intensity == "high" else 2)
    must_set = set(must_include or [])

    candidates = _match_attractions(interests, attractions, must_include)
    # 按城市分组，组内保持相关性降序（candidates 已降序）
    by_city = {}
    city_seq = []
    for a in candidates:
        if a["city"] not in by_city:
            by_city[a["city"]] = []
            city_seq.append(a["city"])
        by_city[a["city"]].append(a)
    must_cities = [c for c in city_seq if any(x["id"] in must_set for x in by_city[c])]
    ordinary_cities = [c for c in city_seq if c not in must_cities]

    # 单日单城：构造 city_per_day 数组
    # 策略：must 城市（用户指定优先日）从 preferred_day 起连续占其所需天数；
    #       其余城市「覆盖优先」——每座候选城市至少占 1 天（避免大城市独占所有天数导致
    #       兴趣/城市缺失），剩余天数再按需求（起点城市优先）补齐，使大城市可占多天。
    from math import ceil
    start_city = constraints.get("start_city", "银川")
    city_per_day = [None] * days
    pref = preferred_day if (preferred_day and 1 <= preferred_day <= days) else None
    must_need = {c: max(1, ceil(len(by_city[c]) / max_per_day)) for c in must_cities}
    # 1) must 城市从 preferred_day 起连续占位
    if must_cities:
        mc_list = sorted(must_cities, key=lambda c: -len(by_city[c]))
        d = pref if pref else 1
        for mc in mc_list:
            for _ in range(must_need.get(mc, 1)):
                if 1 <= d <= days:
                    city_per_day[d - 1] = mc
                d += 1
    # 2) 覆盖优先：每座候选城市至少占 1 天
    remain = city_per_day.count(None)
    for c in ordinary_cities:
        if remain <= 0:
            break
        for i in range(days):
            if city_per_day[i] is None:
                city_per_day[i] = c
                remain -= 1
                break
    # 3) 剩余空位按需求（起点城市优先）补齐，大城市可占多天
    order = sorted(ordinary_cities, key=lambda c: (0 if c == start_city else 1, -len(by_city[c])))
    i = 0
    while remain > 0 and order:
        c = order[i % len(order)]
        for j in range(days):
            if city_per_day[j] is None:
                city_per_day[j] = c
                remain -= 1
                break
        i += 1
        if i > days * len(order) + 5:
            break
    # 4) 仍无城市指派（典型：用户未提任何兴趣、候选池为空）：按默认起点城市补齐，
    #    保持「单日单城」语义完整；当日景点走全语料回退路径（P0-2），
    #    不得因城市为空就留空白日或以 None 城市冒充「跨城」。
    for i in range(days):
        if city_per_day[i] is None:
            city_per_day[i] = start_city

    # 逐天填充（同一城市可跨天；同城景点排完后从其它候选城市未排景点中**跨城调剂**）
    # V31.3 P0-2 修复：此前同城景点池耗尽即留下 items=[] 的空白日（真实复现：
    # 3 天行程但候选景点池只能填满前 2 天），且门禁无「每天至少一个景点」校验，
    # 空白日照样通过 Business/Release 被判可发布。现在：只要候选池非空，
    # 任何一天都至少排入 1 个**本地语料真实景点**（同城优先，耗尽才跨城并显式标注）。
    # defaultdict：城市可能是步骤 4 补齐的 start_city（不在 city_seq 中），
    # 直接 ptr[city] 会 KeyError；_take_next 内已用 .get，此处统一兼容。
    from collections import defaultdict
    ptr = defaultdict(int)
    used_ids = set()
    candidate_ids = {a["id"] for a in candidates}
    days_plan = []
    plan_interests = set()
    used_cities = set()

    def _take_next(prefer_city=None):
        """按相关性降序取下一个未排景点：同城优先，其次其它候选城市（跨城调剂）。

        V31.3 P0-2：候选池全部耗尽时（典型：用户未提兴趣、或唯一兴趣如「美食」
        在景点语料中匹配极少），退回**本地语料全量景点**补入——兴趣匹配是排序
        偏好而非硬性过滤，行程不得因此空白；补入景点均标注「综合推荐」。
        """
        order = ([prefer_city] if prefer_city else []) + \
                [c for c in city_seq if c != prefer_city]
        for c in order:
            while ptr.get(c, 0) < len(by_city.get(c, [])):
                a = by_city[c][ptr[c]]
                ptr[c] += 1
                if a["id"] not in used_ids:
                    return a
        # 候选池耗尽：当日城市优先，其次按语料顺序（确定性）
        if prefer_city:
            for a in attractions:
                if a["id"] not in used_ids and a.get("city") == prefer_city:
                    return a
        for a in attractions:
            if a["id"] not in used_ids:
                return a
        return None

    for d in range(1, days + 1):
        city = city_per_day[d - 1]
        items = []
        cross_city = False
        pool_exhausted = False
        if city and ptr.get(city, 0) < len(by_city.get(city, [])):
            for a in by_city[city][ptr.get(city, 0):]:
                if len(items) >= max_per_day:
                    break
                if a["id"] in used_ids:
                    ptr[city] += 1
                    continue
                ht = _why(a, interests)
                label = _theme_label(a, interests)
                items.append({
                    "id": a["id"],
                    "name": a["name"]["zh-CN"],
                    "city": a["city"],
                    "duration": a.get("duration"),
                    "culture_theme": a.get("culture_theme"),
                    "theme_label": label,
                    "why_tags": ht,
                    "why": ("匹配兴趣：" + "、".join(sorted(set(ht)))) if ht else label,
                    "tips": a.get("tips"),
                })
                plan_interests.update(ht)
                used_cities.add(city)
                used_ids.add(a["id"])
                ptr[city] += 1
        # 同城池耗尽/当天无城市指派：跨城调剂补足，杜绝空白日（不编造，均为语料真实景点）
        if not items:
            fb = _take_next(city)
            if fb is not None:
                ht = _why(fb, interests)
                cross_city = (fb["city"] != city)
                from_pool = fb["id"] in candidate_ids
                label = _theme_label(fb, interests)
                items.append({
                    "id": fb["id"],
                    "name": fb["name"]["zh-CN"],
                    "city": fb["city"],
                    "duration": fb.get("duration"),
                    "culture_theme": fb.get("culture_theme"),
                    "theme_label": label,
                    "why_tags": ht,
                    "why": ("匹配兴趣：" + "、".join(sorted(set(ht)))) if ht else label,
                    "tips": fb.get("tips"),
                })
                plan_interests.update(ht)
                used_cities.add(fb["city"])
                used_ids.add(fb["id"])
                if not from_pool:
                    pool_exhausted = True
        day_cities = {it["city"] for it in items}
        food_rec = _food_for_cities(day_cities, foods)
        if food_rec:
            plan_interests.add("美食")
        note = ("轻松节奏，单日单城不超过%d个景点" % max_per_day) if intensity == "low" else ""
        if cross_city:
            note = (note + "；" if note else "") + \
                "本日含跨城备选调剂景点（同城候选已排完，均为本地语料真实景点，跨城交通需另行核验）"
        if pool_exhausted:
            note = (note + "；" if note else "") + \
                "本日景点为综合推荐（兴趣匹配池已用尽，均为本地语料真实景点，未编造）"
        if not items:
            note = (note + "；" if note else "") + \
                "本地语料中暂无更多可排景点（建议调整兴趣或天数后重试）"
        days_plan.append({
            "day": d,
            "items": items,
            "food_recommend": food_rec,
            "note": note,
        })

    leftover = []
    for c in city_seq:
        for a in by_city[c][ptr[c]:]:
            leftover.append(a["name"]["zh-CN"])
    covered = set(constraints.get("interests", [])) | set(constraints.get("requirements", []))
    covered.discard("住宿"); covered.discard("交通")
    missing = sorted(covered - plan_interests)
    budget = _budget_placeholder(constraints, used_cities)
    # V32.2 P0-2：跨城行程合理性提示（不伪造地图耗时；数据源状态以 A4 实测为准）。
    cross_city_advisory = None
    if len(used_cities) >= 3 and days <= 3:
        cross_city_advisory = (
            "当前为%d天%d城（约每日一城），跨城移动频繁、节奏偏紧；"
            "建议减城（如保留银川+固原）或延长至4-5天。跨城实际驾车/高铁耗时与班次"
            "需以地图/交通服务实时数据为准（驾车路线经授权地图服务时已实时核验；"
            "高铁班次仍待核实），不得仅凭每日景点数少即认定轻松。"
            % (days, len(used_cities)))
    # V32.2 §二：用户指定兴趣与引擎自动添加的可选兴趣必须分开，
    # 不得用自动添加的「美食」等兴趣抬高覆盖率信号。
    user_interests = sorted(set(constraints.get("interests", [])))
    auto_added_interests = sorted(plan_interests - set(user_interests))
    plan = {
        "days": days,
        "days_plan": days_plan,
        "covered_interests": sorted(plan_interests),
        "user_interests": user_interests,
        "auto_added_interests": auto_added_interests,
        "missing_interests": missing,
        "leftover_attractions": leftover,
        "used_cities": sorted(used_cities),
        "cross_city_advisory": cross_city_advisory,
        "budget_estimate": budget,
        "budget_note": "预算为规划性结构估算，门票/住宿/交通等动态价格需以官方或平台实时数据核验，当前无法证实不超预算。",
    }
    return plan


def _why(a, interests):
    """返回该景点命中的兴趣标签（供覆盖度统计）。

    - 主题型兴趣（_INTEREST_THEME_SET 映射非空，如「贺兰山文化」）：仅当景点
      culture_theme 直接命中该主题集才算覆盖，避免双龙石窟(石窟艺术)/青铜峡(黄河文化)
      因共享「文化/历史」泛标签被误算作贺兰山文化已覆盖（P0 #1）。
    - 活动型兴趣（研学/摄影/美食等映射为空集）：沿用关键词/标签匹配，属合理活动关联，
      不得因主题化改造而丢失其合法覆盖（否则会误判 Business Gate）。
    """
    theme = a.get("culture_theme")
    hit = []
    for inte in interests:
        theme_set = _INTEREST_THEME_SET.get(inte)
        if theme_set:  # 主题型：严格按文化主题匹配
            if theme and theme in theme_set:
                hit.append(inte)
        else:  # 活动型：关键词/标签匹配
            tags = set(a.get("tags", [])) | set(a.get("suitableFor", []))
            text = (a.get("intro", "") + a.get("name", {}).get("zh-CN", ""))
            for kw in _INTEREST_KW.get(inte, []):
                if kw in tags or kw in text:
                    hit.append(inte)
                    break
    return hit


def _theme_label(a, interests):
    """景点的真实主题标签：直接命中用户兴趣则标其文化主题；否则标「文化扩展：<真实主题>」；
    无主题信息则标「主题关联待核实」。"""
    theme = a.get("culture_theme")
    if not theme:
        return "主题关联待核实"
    directly = any(theme in _INTEREST_THEME_SET.get(inte, set()) for inte in interests)
    return theme if directly else ("文化扩展：" + theme)


def _norm_food_name(n):
    """归一化菜名，去掉（城市）等后缀，用于去重。"""
    import re
    return re.sub(r"[（(][^()]*[)）]", "", n).strip()


def _food_for_cities(cities, foods):
    """按城市推荐美食，模糊匹配城市字段，并按归一化菜名去重。"""
    recs = []
    seen = set()
    for f in foods:
        fc = f.get("city") or ""
        # 模糊匹配：当天城市出现在美食 city 字段中（兼容 '银川 / 吴忠' 写法）
        if not any(c in fc or fc in c for c in cities):
            continue
        nm = f["name"]["zh-CN"]
        key = _norm_food_name(nm)  # 去重 '羊肉臊子面' 与 '羊肉臊子面（银川）'
        if key in seen:
            continue
        seen.add(key)
        recs.append({"name": nm, "city": fc, "intro": f.get("intro")})
        if len(recs) >= 3:
            break
    return recs


def _budget_placeholder(constraints, used_cities):
    """结构化预算估算（V32.2 P0-3）。

    严格区分：所有金额均为「基于公开常识的参考估算区间」，并非实时官方/平台报价；
    无真实价格来源的字段不得伪装为实时价格。仅在估算下限<=限额且跨城交通可行时
    才可能在 Release 门禁层面被进一步判定，但本函数只产出估算与「是否可证」标记，
    不代替 Release Gate 的最终 NOT_READY/READY 决策。
    """
    n = constraints.get("party_size", 1)
    days = constraints.get("days", 1)
    rooms = (n + 1) // 2
    nights = max(1, days - 1)
    segs = max(0, len(used_cities) - 1)
    items = [
        {"category": "住宿", "party_size": n, "rooms": rooms, "nights": nights,
         "estimate_low_yuan": rooms * 200 * nights, "estimate_high_yuan": rooms * 450 * nights,
         "is_estimate": True, "is_realtime": False,
         "basis": "按人数/2间(向上取整)、晚数=天数-1，参考区间200~450元/间夜（非实时房价，需官方核验）"},
        {"category": "跨城交通", "segments": segs, "party_size": n,
         "estimate_low_yuan": segs * 80 * n, "estimate_high_yuan": segs * 260 * n,
         "is_estimate": True, "is_realtime": False,
         "basis": "涉及%d城、%d段跨城，参考区间80~260元/人/段（非实时班次票价，需地图/交通服务核验）" % (len(used_cities), segs)},
        {"category": "门票", "party_size": n, "days": days,
         "estimate_low_yuan": 50 * n * days, "estimate_high_yuan": 120 * n * days,
         "is_estimate": True, "is_realtime": False,
         "basis": "参考区间50~120元/人/景点（各景区官方票价需实时核验）"},
        {"category": "餐饮", "party_size": n, "days": days,
         "estimate_low_yuan": 80 * n * days, "estimate_high_yuan": 160 * n * days,
         "is_estimate": True, "is_realtime": False,
         "basis": "参考区间80~160元/人/天（非实时消费）"},
    ]
    lo = sum(i["estimate_low_yuan"] for i in items)
    hi = sum(i["estimate_high_yuan"] for i in items)
    limit = constraints.get("budget_yuan")
    within = None
    if limit:
        within = (lo <= limit)
        note = ("估算区间[%d,%d]元；下限%s预算%d元，但上限可能超出，且全部为参考估算（非实时报价），"
                "无法证实行程在预算内且跨城交通可行 → Release 须为 NOT_READY（待核实）。"
                % (lo, hi, "未超" if lo <= limit else "已超", limit))
    else:
        note = "未提供预算上限；估算区间[%d,%d]元（参考估算，非实时报价）。" % (lo, hi)
    return {
        "party_size": n, "days": days, "items": items,
        "total_estimate_low_yuan": lo, "total_estimate_high_yuan": hi,
        "budget_limit": limit, "within_limit_estimate": within,
        "is_estimate": True, "is_realtime": False, "note": note,
    }


def revise_itinerary(prev_plan, constraints, must_include=None, preferred_day=None, remove_include=None) -> dict:
    """多轮修订：保留非修订日的原始行程，仅在目标日并入新增景点及其同城姊妹景点。

    核心纪律（对照任务书「保留游客历史约束，重新规划」）：
    - 非修订日（城市未改变）的景点与美食完全沿用上一轮，不做重排、不静默丢弃。
    - 仅把用户指定的 preferred_day 改为新增景点所在城市（跨城不混排）；
      若目标日原属不同城市，则原景点被「置换」，并尽量回填到同城其它有空位的日，
      实在放不下的进入备选（leftover），如实告知，绝不静默丢弃。
    - 全程单日单城、低密度强度约束不变。
    """
    data = _load_attractions()
    attr_by_id = {a["id"]: a for a in data["attractions"]}
    foods = data["foods"]
    interests = set(constraints.get("interests", []))
    intensity = constraints.get("intensity", "medium")
    max_per_day = 2 if intensity == "low" else (3 if intensity == "high" else 2)
    days = constraints.get("days") or prev_plan.get("days", 3)

    # 还原上一轮 day 结构：(day, city, [ids])
    prev_days = prev_plan.get("days_plan", [])
    day_struct = []
    for d in prev_days:
        city = d.get("items")[0]["city"] if d.get("items") else None
        ids = [it["id"] for it in d.get("items", [])]
        day_struct.append({"day": d["day"], "city": city, "ids": list(ids)})

    # 校正天数（用户加/减天数时，沿用原内容并补齐/截断日，不丢失原行程）
    while len(day_struct) < days:
        nxt = (day_struct[-1]["day"] + 1) if day_struct else 1
        day_struct.append({"day": nxt, "city": None, "ids": []})
    if len(day_struct) > days:
        day_struct = day_struct[:days]

    # 定位新增景点
    must_attr = None
    for a in data["attractions"]:
        if a["id"] in (must_include or []):
            must_attr = a
            break

    # 移除用户要求删除/替换掉的景点（多轮"去掉/换成"语义），绝不静默忽略该意图
    if remove_include:
        rm = set(remove_include)
        for dci in day_struct:
            dci["ids"] = [i for i in dci["ids"] if i not in rm]

    # 目标日：优先用用户指定日，否则取最后一天作为默认插入位
    pref_idx = (preferred_day - 1) if (preferred_day and 1 <= preferred_day <= days) else None
    if pref_idx is None or not (0 <= pref_idx < len(day_struct)):
        pref_idx = len(day_struct) - 1

    # 去重：若新增景点此前已在其它日出现，先移除旧副本，避免在目标日重复排入
    if must_include:
        mi = set(must_include)
        for idx2, dci in enumerate(day_struct):
            if idx2 == pref_idx:
                continue
            dci["ids"] = [i for i in dci["ids"] if i not in mi]

    if must_attr:
        must_city = must_attr["city"]
        # 已占用（非目标日）的景点，避免跨日重复排入
        occupied = set()
        for idx2, dci2 in enumerate(day_struct):
            if idx2 == pref_idx:
                continue
            occupied.update(dci2["ids"])
        occupied.difference_update(remove_include or [])
        must_siblings = [a for a in data["attractions"]
                        if a["city"] == must_city and a["id"] not in (must_include or [])
                        and a["id"] not in (remove_include or []) and a["id"] not in occupied]
        displaced = []  # 被目标日原城市景点置换下来的景点
        dci = day_struct[pref_idx]
        if dci["city"] != must_city:
            displaced = [attr_by_id[i] for i in dci["ids"] if i in attr_by_id]
            dci["city"] = must_city
            new_ids = [must_attr["id"]]
            for s in must_siblings:
                if len(new_ids) >= max_per_day:
                    break
                new_ids.append(s["id"])
            dci["ids"] = new_ids
        else:
            others = [i for i in dci["ids"] if i != must_attr["id"] and i not in occupied]
            dci["ids"] = [must_attr["id"]] + others[: max_per_day - 1]

        # 尽量把被置换的景点回填到同城、有空位的其它日
        pref_day = day_struct[pref_idx]["day"]
        for disp in displaced:
            dc = disp["city"]
            for other in day_struct:
                if other["day"] == pref_day:
                    continue
                if other["city"] == dc and len(other["ids"]) < max_per_day:
                    other["ids"].append(disp["id"])
                    break

    # V31.3 P0-2：修订路径同样不得出现空白日。
    # 真实场景：「再加一天变成4天」新增日 city=None/ids=[]；「去掉/换成」
    # 可能把某日景点全部移除。这里用未排入的候选景点回填（兴趣相关优先、
    # 兴趣池耗尽则退回全量语料——均为本地语料真实景点，不编造），并显式标注。
    occupied_ids = {i for dci in day_struct for i in dci["ids"]}
    rm_set = set(remove_include or [])
    fill_pool = _match_attractions(interests, data["attractions"])
    fill_ptr = 0
    for dci in day_struct:
        if dci["ids"]:
            continue
        picked = None
        while fill_ptr < len(fill_pool):
            a = fill_pool[fill_ptr]
            fill_ptr += 1
            if a["id"] in occupied_ids or a["id"] in rm_set:
                continue
            picked = a
            break
        if picked is None:
            for a in data["attractions"]:
                if a["id"] not in occupied_ids and a["id"] not in rm_set:
                    picked = a
                    break
        if picked is not None:
            dci["ids"] = [picked["id"]]
            dci["city"] = picked["city"]
            dci["filled"] = True
            occupied_ids.add(picked["id"])

    # 重建 days_plan（展示信息、美食、兴趣覆盖）
    days_plan = []
    plan_interests = set()
    used_cities = set()
    for dci in day_struct:
        items = []
        if dci["city"] and dci["ids"]:
            for aid in dci["ids"]:
                a = attr_by_id.get(aid)
                if not a:
                    continue
                ht = _why(a, interests)
                items.append({
                    "id": a["id"],
                    "name": a["name"]["zh-CN"],
                    "city": a["city"],
                    "duration": a.get("duration"),
                    "why_tags": ht,
                    "why": ("匹配兴趣：" + "、".join(sorted(set(ht)))) if ht else "综合推荐",
                    "tips": a.get("tips"),
                })
                plan_interests.update(ht)
                used_cities.add(a["city"])
        day_cities = {it["city"] for it in items}
        food_rec = _food_for_cities(day_cities, foods)
        if food_rec:
            plan_interests.add("美食")
        note = ("轻松节奏，单日单城不超过%d个景点" % max_per_day) if intensity == "low" else ""
        if dci.get("filled"):
            note = (note + "；" if note else "") + \
                "本日为新增/回填日程，已从本地语料补入真实景点（未编造），可后续轮次调整"
        days_plan.append({
            "day": dci["day"],
            "items": items,
            "food_recommend": food_rec,
            "note": note,
        })

    # 备选：上一轮或新增中未排入的景点（如实列出，不静默丢弃）
    final_ids = {it["id"] for d in days_plan for it in d["items"]}
    consider = set()
    for d in prev_days:
        consider.update(it["id"] for it in d.get("items", []))
    consider.update(must_include or [])
    leftover = [attr_by_id[i]["name"]["zh-CN"] for i in consider if i not in final_ids and i in attr_by_id]

    covered = set(constraints.get("interests", [])) | set(constraints.get("requirements", []))
    covered.discard("住宿"); covered.discard("交通")
    missing = sorted(covered - plan_interests)
    user_interests = sorted(set(constraints.get("interests", [])))
    auto_added_interests = sorted(plan_interests - set(user_interests))
    budget = _budget_placeholder(constraints, used_cities)
    return {
        "days": days,
        "days_plan": days_plan,
        "covered_interests": sorted(plan_interests),
        "user_interests": user_interests,
        "auto_added_interests": auto_added_interests,
        "missing_interests": missing,
        "leftover_attractions": leftover,
        "used_cities": sorted(used_cities),
        "budget_estimate": budget,
        "budget_note": "预算为规划性结构估算，门票/住宿/交通等动态价格需以官方或平台实时数据核验，当前无法证实不超预算。",
        "user_interests": user_interests,
        "auto_added_interests": auto_added_interests,
        "revised": True,
    }


def agent_travel(text: str, prev_constraints: dict = None, prev_plan: dict = None,
                 new_requirement: str = None) -> dict:
    """A3 行程智能体：约束提取、行程生成、多轮修订。"""
    constraints = extract_constraints(text, prev_constraints)
    must_include = None
    revision_note = ""
    preferred_day = None
    # V30.1 防御性修复：added_cities 此前只在「有增删景点」分支内定义，
    # 若本轮是**约束类修订**且产生 leftover，下方引用会抛 NameError。
    added_cities = set()
    if new_requirement:
        # 识别新增/替换景点（以已知景点名/城市匹配）
        data = _load_attractions()
        pref_day_m = re.search(r"第\s*" + _NUM_TOK + r"\s*天", new_requirement)
        preferred_day = _cn_to_int(pref_day_m.group(1)) if pref_day_m else None
        remove_kw = ["去掉", "不要", "删除", "移除", "换成", "改为"]
        has_remove = any(k in new_requirement for k in remove_kw)
        hits = []
        remove_ids = []
        for a in data["attractions"]:
            nm = a["name"]["zh-CN"]
            if nm in new_requirement or a["id"] in new_requirement:
                if has_remove:
                    remove_ids.append(a["id"])
                else:
                    hits.append(a["id"])
        # "把A换成B"：移除A、新增B（A 与 换成 之间可能夹带"去掉/，"等）
        swap = re.search(r"把([^，。,、]+?)(?:去掉|删除|移除)?[，,。、]?换成([^，。,、]+?)(?:[，。,、]|$)", new_requirement)
        if swap:
            for a in data["attractions"]:
                if a["name"]["zh-CN"] in swap.group(1) or a["id"] in swap.group(1):
                    if a["id"] not in remove_ids:
                        remove_ids.append(a["id"])
                if a["name"]["zh-CN"] in swap.group(2) or a["id"] in swap.group(2):
                    if a["id"] not in hits:
                        hits.append(a["id"])
        # 避免"换成B"时 B 同时出现在移除与新增集合中
        remove_ids = [i for i in remove_ids if i not in set(hits)]
        must_include = hits or None
        if must_include or remove_ids:
            added = [a["name"]["zh-CN"] for a in data["attractions"] if a["id"] in (must_include or [])]
            removed = [a["name"]["zh-CN"] for a in data["attractions"] if a["id"] in remove_ids]
            revision_note = ("用户修订：%s%s。已保留此前约束（人数%s人、预算%s元、%s天、强度%s），重新评估路线与预算。" % (
                ("新增：" + "、".join(added)) if added else "",
                ("；移除：" + "、".join(removed)) if removed else "",
                constraints.get("party_size", "?"), constraints.get("budget_yuan", "?"),
                constraints.get("days", "?"), constraints.get("intensity", "?")))
            added_cities = {a["city"] for a in data["attractions"] if a["id"] in (must_include or [])}
            if added_cities and added_cities != {"银川"}:
                revision_note += ("注意：%s 位于%s，属跨城目的地，需单独安排交通与时段，"
                                  "将影响原节奏与跨城成本（待核实）。" % (
                                      "、".join(added), "、".join(added_cities)))
            if preferred_day:
                revision_note += "已按用户要求优先排入第%d天。" % preferred_day
        elif prev_constraints:
            # 本轮未增删景点，但用户提出了约束类修订（如降低步行强度、控制预算、
            # 同行为长辈等）——仍须**显式留痕**「已保留历史约束并重新评估」，
            # 供 A5 与评委核对「差量重规划」确实发生（V26.0 §七 场景 B）。
            changed = []
            for k, label in (("party_size", "人数"), ("budget_yuan", "预算"),
                             ("days", "天数"), ("intensity", "强度"),
                             ("party_composition", "构成")):
                old, new = prev_constraints.get(k), constraints.get(k)
                if old != new:
                    changed.append("%s %s→%s" % (label, old, new))
            revision_note = ("用户修订（约束类）：已保留此前约束（人数%s人、预算%s元、%s天、强度%s），"
                            "按新条件重新评估路线与预算%s。" % (
                                constraints.get("party_size", "?"),
                                constraints.get("budget_yuan", "?"),
                                constraints.get("days", "?"),
                                constraints.get("intensity", "?"),
                                ("；变化：" + "、".join(changed)) if changed else ""))
            if constraints.get("special_notes"):
                revision_note += "已记录：" + str(constraints.get("special_notes")) + "。"
    else:
        # 首轮：显式点名的景点强制纳入行程（如「看岩画和沙坡头」「想看沙坡头」「银川加中卫」）
        named = detect_named_attractions(text)
        if named:
            must_include = named
    if new_requirement and prev_plan and isinstance(prev_plan, dict) and prev_plan.get("days_plan"):
        plan = revise_itinerary(prev_plan, constraints, must_include=must_include,
                                preferred_day=preferred_day, remove_include=(remove_ids or None))
    else:
        plan = build_itinerary(constraints, must_include=must_include, preferred_day=preferred_day)

    # V30.1 P1：天数扩展 / 跨城市 / 预算口径冲突必须**显式提出**，
    # 不得靠删除原有重要景点、虚构交通费用或放宽门禁来取得 PASS。
    # 这里只做结构化提示：不代填任何价格，也不自动上调预算。
    revision_conflicts = []
    prev_days_n = prev_plan.get("days") if isinstance(prev_plan, dict) else None
    new_days_n = (plan or {}).get("days")
    if prev_days_n and new_days_n and new_days_n > prev_days_n:
        revision_conflicts.append(
            "行程由 %d 天扩展为 %d 天：需追加 %d 晚住宿与相应餐饮，原预算口径为 %d 天。"
            % (prev_days_n, new_days_n, new_days_n - prev_days_n, prev_days_n))
    plan_cities = (plan or {}).get("used_cities") or []
    if len(plan_cities) > 1:
        revision_conflicts.append(
            "跨城市行程（%s）：城际交通耗时、班次与费用须经地图或交通服务实时核验（待核实），"
            "本系统不代填票价/车次，也不据此自动加价。" % "、".join(plan_cities))
    if revision_conflicts and constraints.get("budget_yuan"):
        revision_conflicts.append(
            "请确认：预算 %d 元是否覆盖 %s 天%s行程？若不足，可上调预算、缩短天数或减少跨城目的地。"
            % (int(constraints["budget_yuan"]), new_days_n or "?",
               ("（%s）" % "、".join(plan_cities)) if len(plan_cities) > 1 else " "))
        constraints["budget_recheck_required"] = True
    if revision_conflicts:
        constraints["revision_conflicts"] = revision_conflicts
        revision_note = (revision_note + " " if revision_note else "") + "；".join(revision_conflicts)

    if revision_note:
        # 跨城修订时，被置换的景点会进入备选而非丢失，如实告知
        if plan.get("revised") and plan.get("leftover_attractions") and added_cities and added_cities != {"银川"}:
            revision_note += ("原第%s天部分景点因跨城占用（新增%s目的地）已调整至备选（见「备选景点」），"
                             "未静默丢弃，可在后续轮次补充或调整节奏。" % (
                                 preferred_day or "?", "、".join(added_cities)))
        plan["revision_note"] = revision_note
    # V26.0：行程/约束由确定性算法产出（权威）；真实模型仅可生成「讲解性叙述」，
    # 不得改动天数、景点、预算金额。未来配置模型时此处生效，否则保持 deterministic。
    narrative, model_ev, mmeta = _plan_narrative(plan, constraints)
    return {"agent": "A3", "constraints": constraints, "itinerary": plan,
            "missing_fields": [k for k in ("party_size", "budget_yuan", "days") if constraints.get(k) is None],
            "execution_mode": ("model" if model_ev else "deterministic"),
            "narrative_source": ("model" if model_ev else "deterministic"),
            "narrative": narrative, "model_evidence": model_ev,
            "model_attempted": mmeta.get("model_attempted", False),
            "model_failed": mmeta.get("model_failed", False),
            "model_failure_reason": mmeta.get("model_failure_reason", ""),
            "model_failure_kind": mmeta.get("model_failure_kind", ""),
            # V26.1 §四-P0-2：提示指纹必须随结果返回，否则「模型确实拿到了任务上下文」
            # 只能靠口头声明，无法自证（真实模型运行下这一项曾被判为不成立）。
            "model_prompt_digest": mmeta.get("model_prompt_digest"),
            "model_context_evidence_ids": mmeta.get("model_context_evidence_ids"),
            "tool_called": False,
            "tool_evidence": ["DETERMINISTIC_ALGO:constraint_extract+itinerary_build"],
            "authority_note": "行程约束与预算为确定性算法权威结果，模型不得覆盖"}


def _plan_narrative(plan: dict, constraints: dict) -> tuple:
    """基于确定性行程生成讲解叙述（可选模型路径，失败则返回确定性摘要）。

    V26.1 §四-P0-1：模型调用失败时**必须把 ok=false 的证据一并返回**，
    不得因失败而删除「曾调用模型」这一事实。
    返回 (narrative_text, model_evidence_or_None, model_meta)。
    """
    days = plan.get("days") or constraints.get("days") or 0
    cities = "、".join(plan.get("used_cities", [])) or "宁夏"
    det = ("已按%s天行程覆盖%s，围绕%s安排，单日单城以避免跨城奔波。"
           % (days, cities, "、".join(constraints.get("interests", []) or ["综合体验"])))
    meta_off = {"model_attempted": False, "model_failed": False,
                "model_failure_reason": "模型未启用（未配置端点或 HELAN_AGENT_MODE=deterministic）",
                "model_failure_kind": "not_configured"}
    if _llm_mode() != "model":
        return det, None, meta_off
    outline = []
    for d in plan.get("days_plan", []):
        names = "、".join(i.get("name", "") for i in d.get("items", [])) or "自由活动"
        outline.append("第%s天(%s)：%s" % (d.get("day"), d.get("city", ""), names))
    _a3_prompt = ("【确定性行程（不可修改）】\n%s\n\n【约束】人数%s/预算%s元/%s天/强度%s\n"
                  "请用 3-5 句中文讲解该行程的合理性，不要改动任何数字或景点。"
                  % ("\n".join(outline), constraints.get("party_size"),
                     constraints.get("budget_yuan"), constraints.get("days"),
                     constraints.get("intensity")))
    m = _model_synthesize("A3", "plan_narrative", _PLAN_SYS,
                          _a3_prompt, fallback_text=det)
    if m["mode"] == "model":
        m["model_prompt_digest"] = prompt_digest(_a3_prompt)
        m["model_context_evidence_ids"] = None  # A3 的上下文是确定性行程本身，不是检索证据
        return m["text"], m["evidence"], m
    m.setdefault("model_prompt_digest", prompt_digest(_a3_prompt))
    return det, m.get("evidence"), m      # ← 失败：证据保留（可能为 None=未发出请求）


# ============================ A4 TravelData ============================
# 宁夏主要城市中文名 -> 拼音。open-meteo 地理编码对部分中文名（如「固原」）返回 0 结果，
# 必须用拼音兜底；否则 res=[{}] -> lat/lon=None -> forecast 用 latitude=None 请求 -> HTTP 400。
_NINGXIA_CITY_PINYIN = {
    "银川": "Yinchuan", "固原": "Guyuan", "吴忠": "Wuzhong", "中卫": "Zhongwei",
    "石嘴山": "Shizuishan", "灵武": "Lingwu", "青铜峡": "Qingtongxia", "贺兰": "Helan",
}


def _geocode_city(city: str, timeout: int = 15) -> dict:
    """真实调用 open-meteo Geocoding API。逐候选名尝试（中文 + 拼音兜底），按 admin1=宁夏 消歧。
    返回 {ok, latitude, longitude, resolved_name, admin1, country, resolved_via, tried, error}。
    """
    candidates = [city]
    py = _NINGXIA_CITY_PINYIN.get(city)
    if py:
        candidates.append(py)
    tried = []
    for cand in candidates:
        url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": cand, "count": 10, "language": "zh", "format": "json"})
        tried.append({"name": cand, "url": url})
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                geo = json.load(r)
        except Exception as e:
            return {"ok": False, "error": f"geocoding HTTP {type(e).__name__}: {e}", "tried": tried}
        results = geo.get("results") or []
        matched = [x for x in results if "宁夏" in str(x.get("admin1") or "")]
        if not matched and results:
            matched = results  # 退而求其次，但保留 resolved 标注
        if matched:
            g0 = matched[0]
            lat, lon = g0.get("latitude"), g0.get("longitude")
            if lat is None or lon is None:
                continue
            return {"ok": True, "latitude": lat, "longitude": lon,
                    "resolved_name": g0.get("name"), "admin1": g0.get("admin1"),
                    "country": g0.get("country"), "resolved_via": cand, "tried": tried}
    return {"ok": False,
            "error": "geocoding 无宁夏内匹配结果（中文名未收录，已尝试拼音兜底仍失败）",
            "tried": tried}


def _real_weather(city: str, timeout: int = 15) -> dict:
    """真实调用 open-meteo 公共 API 获取当前天气。失败时返回待核实并标注失败原因（含 HTTP 状态/响应体）。"""
    geo = _geocode_city(city, timeout)
    if not geo.get("ok"):
        return {"city": city, "ok": False, "error": geo.get("error"),
                "geocode_tried": geo.get("tried"),
                "source": "未获取到实时数据，天气需以官方实时预报核验（待核实）"}
    lat, lon = geo["latitude"], geo["longitude"]
    wx_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {"latitude": lat, "longitude": lon, "current_weather": "true"})
    try:
        with urllib.request.urlopen(wx_url, timeout=timeout) as r:
            wx = json.load(r)
        cw = wx.get("current_weather", {})
        return {
            "city": city,
            "temperature_c": cw.get("temperature"),
            "windspeed_kmh": cw.get("windspeed"),
            "weathercode": cw.get("weathercode"),
            "time": cw.get("time"),
            "latitude": lat, "longitude": lon,
            "resolved_name": geo.get("resolved_name"),
            "geocode_via": geo.get("resolved_via"),
            "source": "open-meteo.com (real-time API)",
            "ok": True,
        }
    except urllib.error.HTTPError as e:  # P0 #4：捕获真实 HTTP 状态码、reason 与响应体
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"city": city, "ok": False,
                "error": f"HTTP {e.code}: {e.reason} | body={body}",
                "latitude": lat, "longitude": lon, "geocode_via": geo.get("resolved_via"),
                "forecast_url": wx_url,
                "source": "未获取到实时数据，天气需以官方实时预报核验（待核实）"}
    except Exception as e:  # 网络/解析失败 -> 诚实降级
        return {"city": city, "ok": False, "error": f"{type(e).__name__}: {e}",
                "latitude": lat, "longitude": lon, "geocode_via": geo.get("resolved_via"),
                "source": "未获取到实时数据，天气需以官方实时预报核验（待核实）"}


def _real_maps(cities: list, constraints: dict = None) -> dict:
    """真实调用腾讯位置服务（若已授权 TENCENT_MAP_KEY）。未授权则诚实返回 NEEDS_AUTH。

    设计纪律（V32.2 §三）：
      - 仅从环境变量 TENCENT_MAP_KEY 读取密钥，绝不写入源码/Git/前端；
      - 无 Key -> available=False，provider 字段为空，绝不返回任何伪造的 POI/路线/距离；
      - 有 Key 但网络不可达 -> 真实请求失败，返回结构化 error（标注 NETWORK_BLOCKED 等），
        同样不得用占位数据冒充成功。
    返回字段均为可 JSON 序列化（不含适配器实例）。
    """
    try:
        import tencent_maps as TM
    except Exception:
        return {"available": False, "authorized": False,
                "reason": "地图适配器未随本次部署打包（tencent_maps 模块缺失）",
                "unavailable": ["地图/路线（适配器缺失）"]}
    try:
        grp = TM.build_adapter()
    except Exception as e:
        return {"available": False, "authorized": False,
                "reason": "地图适配器初始化失败：%s" % str(e)[:120],
                "unavailable": ["地图/路线（适配器异常）"]}
    status = grp.get("status", {}) or {}
    if status.get("state") != TM.STATUS_READY:
        return {"available": False, "authorized": False,
                "reason": status.get("reason", "腾讯位置服务未授权"),
                "coord_system": status.get("coord_system"),
                "unavailable": ["地图/路线（腾讯位置服务 Key 未授权）"]}
    place = grp["place"]; route = grp["route"]; dist = grp["distance"]; rest = grp["restaurant"]
    ev = []
    pois = {}; restaurants = {}; routes = []; matrix = None
    map_errors = []
    seq = cities or ["银川"]

    def _safe(label, fn):
        """统一兜底：腾讯侧任何异常（含非预期响应结构）都转为诚实 FAIL，绝不抛出崩溃。"""
        try:
            return fn()
        except Exception as e:  # 网络/解析/结构异常一律降级，不中断整体行程规划
            ev.append("%s_FAIL:exception:%s" % (label, type(e).__name__))
            map_errors.append("%s:%s" % (label, type(e).__name__))
            return None

    for c in seq:
        r = _safe("MAP_POI", lambda: place.search(c, "景点"))
        if r is None:
            pass
        elif r.get("ok"):
            pois[c] = r.get("data")
            ev.append("MAP_POI_OK:%s:%d" % (c, (r.get("data") or {}).get("count", 0)))
        else:
            ev.append("MAP_POI_FAIL:%s:%s" % (c, (r.get("error") or "")[:60]))
            map_errors.append("POI:%s" % (r.get("error") or "")[:60])
        rr = _safe("MAP_RESTAURANT", lambda: rest.nearby(c, "餐厅"))
        if rr is not None and rr.get("ok"):
            restaurants[c] = rr.get("data")

    # 先对城市做地理编码，得到 GCJ-02 坐标；腾讯驾车/距离接口要求坐标而非城市名。
    def _geocode(city):
        try:
            g = place.geocode(city)
            if g.get("ok"):
                return (g["data"]["lat"], g["data"]["lng"])
        except Exception:
            return None
        return None
    coords = {}
    for c in seq:
        coords[c] = _geocode(c)

    # 相邻城市真实驾车路线（GCJ-02 坐标，归一化标注 coord_system）
    for i in range(len(seq) - 1):
        fa, fb = coords.get(seq[i]), coords.get(seq[i + 1])
        if not (fa and fb):
            ev.append("MAP_ROUTE_SKIP:%s->%s:no-coords" % (seq[i], seq[i + 1]))
            map_errors.append("ROUTE:%s->%s:no-coords" % (seq[i], seq[i + 1]))
            continue
        fstr, tstr = "%s,%s" % (fa[0], fa[1]), "%s,%s" % (fb[0], fb[1])
        r = _safe("MAP_ROUTE", lambda a=fstr, b=tstr: route.route("driving", a, b))
        if r is None:
            pass
        elif r.get("ok"):
            routes.append({"from": seq[i], "to": seq[i + 1], "data": r.get("data")})
            ev.append("MAP_ROUTE_OK:%s->%s" % (seq[i], seq[i + 1]))
        else:
            ev.append("MAP_ROUTE_FAIL:%s->%s:%s" % (seq[i], seq[i + 1], (r.get("error") or "")[:60]))
            map_errors.append("ROUTE:%s->%s:%s" % (seq[i], seq[i + 1], (r.get("error") or "")[:60]))
    # 城市间距离矩阵（用于跨城行程合理性真实核验；同样以坐标入参）
    if len(seq) >= 2 and all(coords.get(c) for c in seq):
        origins = ["%s,%s" % (coords[c][0], coords[c][1]) for c in seq]
        dm = _safe("MAP_MATRIX", lambda: dist.matrix(origins, origins))
        if dm is not None and dm.get("ok"):
            matrix = dm.get("data")
    # 诚实声明：密钥有效（status READY）但全部调用失败（如配额超限/接口异常），
    # 仍标记 available=True 表示「授权链路通」，但附带 map_error 让前端明确降级，不冒充实时成功。
    return {"available": True, "authorized": True, "provider": "tencent",
            "coord_system": "GCJ-02",
            "pois": pois, "restaurants": restaurants,
            "routes": routes, "distance_matrix": matrix,
            "tool_evidence_map": ev,
            "map_error": ("; ".join(map_errors) if map_errors else None),
            "unavailable": []}


def agent_travel_data(cities: list, constraints: dict = None) -> dict:
    """A4 旅行数据智能体：调用地图/天气/住宿/交通/消费服务。
    本环境实测：天气可经 open-meteo 真实查询；若部署环境配置了 TENCENT_MAP_KEY，
    则经腾讯位置服务真实查询 POI/路线/距离/餐厅（§三）；地图服务未授权时诚实标注
    「待核实」，禁止冒充实时结果。住宿/交通班次/消费仍需平台/官方实时数据核验。
    """
    weather = {}
    tool_evidence = []
    for c in (cities or ["银川"]):
        w = _real_weather(c)
        weather[c] = w
        # 无论成功失败，真实调用都留痕（失败也记录原因），供 A5/ExecutionGate
        # 区分「未调用工具」与「调用失败后的诚实降级」，避免误判为伪造。
        if w.get("ok"):
            tool_evidence.append(f"WEATHER_API_OK:{c}:{w.get('temperature_c')}C@{w.get('time')}")
        else:
            tool_evidence.append(f"WEATHER_API_FAIL:{c}:{w.get('error', 'unknown')[:80]}")
    # 地图服务（§三）：有授权则真实查询，无授权则诚实降级
    maps = _real_maps(cities, constraints)
    if maps.get("available"):
        tool_evidence.extend(maps.get("tool_evidence_map", []))
        # 用真实路线信息替换「待核实」占位（仍标注为真实 API 来源）
        transport = {}
        for rc in maps.get("routes", []):
            transport["%s→%s" % (rc["from"], rc["to"])] = {
                "mode": "driving", "routes": rc.get("data", {}).get("routes", []),
                "source": "tencent-maps-realtime", "coord_system": "GCJ-02"}
        if not transport:
            transport = {c: "跨城交通耗时/班次已尝试经地图服务核验（待补充路段）"
                        for c in (cities or ["银川"])}
        map_unavailable = []
    else:
        transport = {c: "跨城交通耗时/班次需经地图或交通服务实时核验（待核实）" for c in (cities or ["银川"])}
        map_unavailable = maps.get("unavailable", ["地图/路线（腾讯位置服务 Key 未授权）"])
    ok_cities = [c for c, w in weather.items() if w.get("ok")]
    # V33 §A2：出行价格与交通数据（统一契约层 travel_prices；与地图/天气同一纪律：
    # 真实调用留痕、失败诚实降级、绝不伪造价格/班次）
    #   - 同程/艺龙：BLOCKED_MERCHANT_AUTH → price=None + UNAVAILABLE + 官方深链；
    #   - 腾讯公交/BRT/火车：已授权则真实调用（8s 超时 / 10min 缓存 / 失败降级）；
    #   - 酒店/门票/餐饮价格：无授权数据源 → UNAVAILABLE（禁止 0 元/假特价）。
    prices: list = []
    transit: list = []
    travel_status = None
    seq = cities or ["银川"]
    try:
        import travel_prices as _TP
        svc = _TP.get_travel_service()
        travel_status = svc.capability_matrix()
        for c in seq[:3]:
            prices.extend(svc.price_quotes(c, ptype="hotel"))
        prices.extend(svc.price_quotes(seq[0], ptype="ticket"))
        for i in range(min(len(seq) - 1, 2)):  # 跨城相邻城市对，最多 2 段（控制时延）
            pair = svc.transit_options(seq[i], seq[i + 1])
            transit.extend(pair)
            live = sum(1 for o in pair if o.get("status") == "LIVE")
            tool_evidence.append("TRANSIT_%s:%s->%s:%d/%d" % (
                "OK" if live else "UNAVAILABLE", seq[i], seq[i + 1], live, len(pair)))
    except Exception as e:  # 契约层异常不得中断 A4（天气/地图结果仍可用）
        travel_status = {"component": "travel_prices", "state": "UNAVAILABLE",
                         "reason": "契约层不可用：%s" % type(e).__name__}
        tool_evidence.append("TRAVEL_PRICES_FAIL:exception:%s" % type(e).__name__)
    return {
        "agent": "A4",
        "weather": weather,
        "transport": transport,
        "maps": maps,  # 真实地图数据（含 POI/路线/距离/餐厅）；无授权时为 NEEDS_AUTH 说明
        # V33：结构化价格/交通数据（slim_result 同字段暴露；无来源时为 UNAVAILABLE）
        "prices": prices,
        "transit": transit,
        "travel_status": travel_status,
        "accommodation": "住宿房价需以平台/官方实时数据核验（待核实）",
        "consumption": "餐饮/门票消费需以官方或平台实时数据核验（待核实）",
        "tool_called": True,
        "tool_evidence": tool_evidence,  # 真实调用留痕，供 ExecutionGate/A5 校验
        # 本 Agent 不使用模型生成事实数据；数字类结论只允许来自真实 API
        "execution_mode": "tool_only",
        "model_evidence": None,
        "unavailable_services": map_unavailable + [
            "住宿房价/房态（同程商户 API 未授权 BLOCKED_MERCHANT_AUTH；官方深链待核实）",
            "门票价格（无授权票务数据源；景区官方渠道待核实）",
            "餐饮/消费（无授权实时价格源）",
            "公交实时到站（无授权数据源；线路/票价/首末班已可经腾讯真实核验）",
        ],
        # V32.2 §四：同程 EPN（tc-chengxin）等仅作为平台连接器存在，本引擎无后端/Web 直连接口，
        # 预订与实时票务一律标注 PLATFORM_ONLY，不得伪称为本引擎实时接通。
        "platform_only_services": [
            "同程 EPN（tc-chengxin 平台连接器，非本引擎直连接口；预订/实时票务标注 PLATFORM_ONLY）",
            "携程/去哪儿等第三方预订（同上，非本引擎直连，信息以「待核实」呈现）",
        ],
        "weather_hit": ok_cities,
    }


# ============================ A5 Verifier ============================
def agent_verifier(state: dict, culture_res=None, ecology_res=None, travel_res=None,
                   traveldata_res=None) -> dict:
    """A5 可信性审核：事实依据、约束满足、工具执行、幻觉检测。"""
    issues = []
    corrections = []
    # 1) 文化/生态事实必须有可追溯来源
    for label, res in (("A1", culture_res), ("A2", ecology_res)):
        if res is None:
            continue
        # 证据不足但回答本身是诚实的「未检索到/无法给出确定性回答」降级，不算幻觉
        grounding = (res.get("gate") or {}).get("grounding")
        answer_text = res.get("answer", "")
        honest_decline = grounding == "insufficient_evidence" or any(
            k in answer_text for k in ["未能检索", "无法给出确定性", "未检索到可靠依据", "建议通过官方"])
        if res.get("evidence_count", 0) == 0 and not honest_decline:
            issues.append(f"[{label}] 检索无依据，却生成了断言性回答，疑似无源幻觉")
        for cit in res.get("citations", []):
            if not cit.get("source_url") and not cit.get("source_name"):
                issues.append(f"[{label}] 引用缺少来源：{cit.get('title')}")
    # 2) 约束满足：行程天数与约束一致
    c = travel_res.get("constraints", {}) if travel_res else {}
    itin = travel_res.get("itinerary", {}) if travel_res else {}
    if c.get("days") and itin.get("days") and c["days"] != itin["days"]:
        issues.append(f"[A3] 行程天数({itin.get('days')})与约束({c.get('days')})不一致")
    # 3) 工具执行真实性
    if traveldata_res:
        if traveldata_res.get("tool_called") and not traveldata_res.get("tool_evidence"):
            issues.append("[A4] 声明调用工具但无证据（疑似伪造）")
        for c2, w in traveldata_res.get("weather", {}).items():
            if not w.get("ok") and "待核实" not in w.get("source", ""):
                issues.append(f"[A4] {c2} 天气获取失败且未明确标注待核实")
    # 4) 幻觉检测：行程是否引用了不存在的景点
    if travel_res:
        valid_ids = {a["id"] for a in _load_attractions()["attractions"]}
        for d in itin.get("days_plan", []):
            for it in d.get("items", []):
                if it["id"] not in valid_ids:
                    issues.append(f"[A3] 行程含未知景点 id={it['id']}（疑似幻觉）")
    # 5) V26.0：模型执行真实性 —— 声明 model 模式必须带可核实 model_call 证据
    for label, res in (("A1", culture_res), ("A2", ecology_res), ("A3", travel_res)):
        if not res or res.get("execution_mode") != "model":
            continue
        ev = res.get("model_evidence") or {}
        if not ev.get("ok") or not ev.get("response_sha256"):
            issues.append(f"[{label}] 声明模型执行但缺少可核实 model_call 证据（疑似伪造）")
        else:
            corrections.append(
                "%s 模型表述综合：%s/%s latency=%sms req=%s（引用与事实仍以检索/确定性结果为权威）"
                % (label, ev.get("provider"), ev.get("model"), ev.get("latency_ms"),
                   ev.get("request_id") or "-"))
    # 6) V26.1 §七：用户显式要求「重新核查门票和步行强度」时，A5 必须给出**可核对的不确定
    #    事项提示**（cautions），而不是静默放行。cautions 不改变 verdict（不是违规），
    #    但必须随 verification 一起回传前端与轨迹，供用户核对——这是「提示不确定事项」
    #    与「阻止」两种审核动作的分界：资格类不确定性只提示，事实/约束类错误才阻断。
    cautions = []
    companions = c.get("companions") or []
    for x in companions:
        if not (x.get("is_elder") or x.get("age", 0) >= 60):
            continue
        if x.get("elder_ticket_age_met"):
            cautions.append(
                "同行%s%d岁：达到多数景区老年优惠票常见年龄门槛（60 周岁），"
                "但优惠幅度与凭证要求以景区公示为准，须现场核验，不得据此预先下调预算"
                % (x["role"], x["age"]))
        else:
            cautions.append(
                "同行%s%d岁：未达多数景区老年优惠票 60 周岁门槛，是否可享优惠须按景区公示"
                "现场核实；本行程门票按全价预留，不因年龄自行调减"
                % (x["role"], x["age"]))
    if companions and c.get("intensity") == "low":
        cautions.append(
            "步行强度：%s（单日景点数≤2）；实际步行距离、坡道与休息点以景区公示为准，"
            "体力不支时应即时调整当日安排"
            % (c.get("intensity_reason") or "同行长辈，已按低强度安排"))
    if c.get("budget_yuan"):
        cautions.append(
            "预算：维持 %d 元不变（用户要求预算尽量不变）；门票与交通须在此额度内闭合，"
            "超支时应先提示而非自动加价" % int(c["budget_yuan"]))
    # V30.1 P1：天数扩展/跨城/预算口径的冲突必须随审核回传，供游客当场决策
    for _rc in (c.get("revision_conflicts") or []):
        cautions.append("修订风险：%s" % _rc)
    if c.get("verification_focus"):
        cautions.append(
            "用户点名要求复核：%s（已逐项核对，结论见上）" % "、".join(c["verification_focus"]))
    if c.get("special_notes"):
        cautions.append("约束留痕：%s" % c["special_notes"])
    verdict = "PASS" if not issues else "FAIL"
    return {"agent": "A5", "verdict": verdict, "issues": issues,
            "corrections": corrections, "cautions": cautions,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "execution_mode": "deterministic",
            "authority_note": "A5 审核为确定性规则引擎，不交由模型裁决（防模型自证）",
            "tool_called": False,
            "tool_evidence": ["DETERMINISTIC_RULES:fact/constraint/tool/hallucination/model-claim"]}


# ============================ 内容创作（V31.3 P0-1） ============================
# 内容创作业务逻辑的唯一真源：Web /api/content、MCP helan_content、
# 聊天流 content_create 意图三者共用，避免同一规则在三处各写一份。
# （自 runtime.py 上移：入口只做薄封装，业务实现集中在 agents。）

def _content_context(store, last):
    """内容创作的**唯一数据源**：只从已确认行程与会话状态取值。

    纪律（V30.1 §二）：人数、天数、目的地、景点、预算全部取自本上下文；
    未进入行程的景点只能出现在「可选推荐」区，并与正式行程显式分开；
    费用口径只有 budget_yuan 上限与 budget_note（含「待核实」），不编造金额。
    """
    itin = store.itinerary or {}
    r = (last or {}).get("results", {})
    a3 = r.get("A3") or {}
    # 约束取值顺序：本轮 A3 结果 -> 行程内嵌约束 -> **会话级约束**（StateStore）。
    # 第三级回退保证聊天流内容创作（content_create 意图轮不运行 A3）与
    # /api/content 按钮路径展示一致的人数/预算/兴趣，不会退化成「人数待确认」。
    constraints = a3.get("constraints") or itin.get("constraints") or {}
    if not constraints:
        try:
            constraints = dict(getattr(store, "constraints", None) or {})
        except Exception:
            constraints = {}
    days_plan = itin.get("days_plan") or []

    planned, used_cities, planned_names = [], [], []
    for d in days_plan:
        items = []
        for it in d.get("items", []) or []:
            nm = it.get("name")
            if not nm:
                continue
            items.append({"name": nm, "city": it.get("city"),
                          "duration": it.get("duration"), "tips": it.get("tips")})
            planned_names.append(nm)
            if it.get("city"):
                used_cities.append(it["city"])
        city = d.get("city") or (items[0].get("city") if items else "")
        planned.append({"day": d.get("day"), "city": city, "items": items,
                        "food": _food_names(d.get("food_recommend"))})

    leftover = itin.get("leftover_attractions") or []
    optional = [x for x in leftover if x and x not in planned_names]

    n = constraints.get("party_size")
    label = "%d人" % int(n) if n else "人数待确认"
    if constraints.get("party_composition") == "family_with_children" and n:
        label += "（亲子）"

    return {
        "party_size": n,
        "party_label": label,
        "days": len(planned) or itin.get("days") or 0,
        "planned": planned,
        "planned_names": planned_names,
        "cities": sorted(set(c for c in used_cities if c)),
        "optional": optional[:6],
        "budget_yuan": constraints.get("budget_yuan"),
        "budget_note": itin.get("budget_note") or "预算未设定，费用需以景区或平台实时数据核验（待核实）",
        "interests": constraints.get("interests") or [],
        "citations": (r.get("A1") or {}).get("citations", []) if r.get("A1") else [],
        "revision_note": itin.get("revision_note") or "",
    }


def _food_names(food) -> list:
    out = []
    for f in (food or []):
        if isinstance(f, dict) and f.get("name"):
            out.append(f["name"])
        elif isinstance(f, str) and f:
            out.append(f)
    return out


def _hashtags(ctx) -> list:
    """话题标签只由真实行程派生，不硬编码未去过的景点。"""
    tags = ["宁夏旅行"]
    for c in ctx.get("cities", [])[:2]:
        tags.append(c)
    for nm in ctx.get("planned_names", [])[:3]:
        tags.append(nm)
    tags.append("灵游宁夏")
    out = []
    for t in tags:
        if t and t not in out:
            out.append(t)
    return out


def _generate_content(kind: str, store, last: dict) -> dict:
    """内容创作：基于**真实行程/会话状态**的确定性文案（小红书攻略 / Vlog 分镜）。

    纪律：未进入行程的景点只能进入「可选推荐」区；模板生成须显式标注
    template-based，不得表述为模型生成。
    """
    ctx = _content_context(store, last)
    planned, optional = ctx["planned"], ctx["optional"]
    budget_line = ctx["budget_note"]
    if ctx.get("budget_yuan"):
        budget_line = "预算上限 %s 元；%s" % (ctx["budget_yuan"], budget_line)

    if kind == "vlog":
        shots = []
        for d in planned:
            i = d["day"] or (len(shots) // 2 + 1)
            items = d["items"]
            head = items[0]["name"] if items else "自由活动"
            dcity = d["city"] or "宁夏"
            shots.append({
                "shot": len(shots) + 1,
                "scene": "第%s天 · %s · %s" % (i, dcity, head),
                "type": "航拍+跟拍",
                "narration": "Day%s：%s%s。" % (
                    i, ("走进" + head) if items else "留白休整",
                    ("，再看" + "、".join(x["name"] for x in items[1:])) if len(items) > 1 else ""),
                "duration": "0:25",
            })
            if len(items) > 1 or d["food"]:
                focus = "、".join(x["name"] for x in items[1:]) or "、".join(d["food"][:2])
                shots.append({
                    "shot": len(shots) + 1,
                    "scene": "第%s天 · %s特写" % (i, focus),
                    "type": "微距+采访",
                    "narration": "把%s的细节讲给镜头。" % focus,
                    "duration": "0:35",
                })
        return {
            "title": "《贺兰有灵·灵游宁夏》旅行 Vlog 分镜脚本",
            "mode": "template-based（依据真实行程，非模型生成）",
            "days": ctx["days"],
            "party_label": ctx["party_label"],
            "scenes_from_itinerary": list(ctx["planned_names"]),
            "shots": shots,
            "outro": "关注我，一起灵游宁夏。#%s" % " #".join(_hashtags(ctx)[:3]),
        }

    lines = []
    lines.append("🌿 %s的宁夏%s天行程笔记" % (ctx["party_label"], ctx["days"] or "N"))
    lines.append("")
    lines.append("📍 已确认路线（本次排程）")
    if planned:
        for d in planned:
            seg = " → ".join(x["name"] for x in d["items"]) or "轻松休整（未排景点）"
            lines.append("  Day%s（%s）：%s" % (d["day"], d["city"] or "待确认", seg))
            if d["food"]:
                lines.append("     特色美食：%s" % "、".join(d["food"][:3]))
    else:
        lines.append("  （尚未生成行程，暂无可确认景点）")

    lines.append("")
    lines.append("✅ 行程内景点（仅列本次排入的）")
    if ctx["planned_names"]:
        for nm in ctx["planned_names"]:
            lines.append("  · %s" % nm)
    else:
        lines.append("  · 暂无（请先完成行程规划）")

    if optional:
        lines.append("")
        lines.append("🧭 可选推荐（未排入本次行程，供下次参考）")
        for nm in optional:
            lines.append("  · %s" % nm)

    if ctx["citations"]:
        lines.append("")
        lines.append("📚 文化依据（可核实来源）")
        for c in ctx["citations"][:3]:
            if _CP is not None:
                lines.append("  · " + _CP.render_source_line(c, with_title=False))
            else:
                lines.append("  · " + (c.get("source_name") or "未标注来源"))

    lines.append("")
    lines.append("💡 预算：%s" % budget_line)
    lines.append("   门票、开放时间、演出与项目价格以景区官方公布为准，本文不代填金额。")
    lines.append("")
    lines.append(" ".join("#" + t for t in _hashtags(ctx)))
    return {
        "title": "《贺兰有灵·灵游宁夏》行程笔记（小红书风格）",
        "mode": "template-based（依据真实行程，非模型生成）",
        "body": "\n".join(lines),
        "party_label": ctx["party_label"],
        "days": ctx["days"],
        "itinerary_spots": ctx["planned_names"],
        "optional_spots": optional,
        "budget_note": budget_line,
        "hashtags": _hashtags(ctx),
    }


def agent_content(text: str = "", store=None, last: dict = None, kind: str = None) -> dict:
    """内容创作（V31.3 P0-1）：基于会话既有行程与真实证据生成结构化内容。

    - kind 显式指定（Web /api/content、MCP helan_content）优先；
      否则由文本语义识别（vlog/分镜/脚本 -> vlog，其余 -> xiaohongshu）；
    - 无既有行程时诚实返回 ok=False（提示先完成规划），**不虚构目的地**；
    - 聊天流 content_create 意图、Web /api/content、MCP helan_content
      三者共用同一生成逻辑（唯一真源）。
    """
    if not kind:
        kind = ("vlog" if any(k in (text or "") for k in ("vlog", "Vlog", "VLOG", "分镜", "脚本"))
                else "xiaohongshu")
    if store is None:
        return {"ok": False, "kind": kind, "error": "会话状态不可用，无法读取既有行程"}
    plan = getattr(store, "itinerary", None)
    if not plan or not plan.get("days_plan"):
        return {"ok": False, "kind": kind,
                "error": "请先完成行程规划后再生成内容（内容基于真实行程，不虚构目的地）"}
    return {"ok": True, "kind": kind,
            "content": _generate_content(kind, store, last or {}),
            "mode": "template-based（基于真实行程，非模型生成）",
            "tool_called": False,
            "tool_evidence": ["DETERMINISTIC_ALGO:content_generate"],
            "execution_mode": "deterministic",
            "authority_note": "内容为模板化确定性生成，仅可表述已有行程，不得新增无来源事实"}

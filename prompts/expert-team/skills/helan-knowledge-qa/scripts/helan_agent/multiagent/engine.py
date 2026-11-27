# -*- coding: utf-8 -*-
"""
多轮会话引擎（A0 之上的会话层）

支撑任务书「五、必须跑通的演示」：
- 第1轮：一家四口去宁夏玩三天，预算5000元，想了解贺兰山文化，安排住宿/交通/美食/亲子研学，不要太累。
- 第2轮（修订）：第二天我还想去沙坡头，尽量不超预算。
跨轮通过 StateStore 维护约束，A3 在第2轮必须保留前轮约束并重新评估。
"""
import os
import json
from orchestrator import (Orchestrator, detect_intent, is_form_conversion,
                          INTENT_TRAVEL, INTENT_REVISE, INTENT_MIXED,
                          INTENT_CULTURE, INTENT_ECOLOGY)
from contracts import INTENT_CONTENT, STRONG_REVISION_MARKERS

# V30.0：引用渲染统一走 citation_policy，避免本机路径/异常域名进入游客可读文本
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    import citation_policy as _CP
except Exception:
    _CP = None


def _render_cite(cit):
    """统一的来源行（内部资料显示 internal ref + 待核实提示，绝不显示本机路径）。"""
    if _CP is not None:
        return _CP.render_source_line(cit, with_title=False)
    return "%s｜核验：%s" % (cit.get("source_name") or "未标注来源",
                            "已核验" if cit.get("verified") else "待核实")

# V26.1 修复：修订识别不能只靠关键词。
# 实测缺陷：用户原句「父亲58岁，母亲63岁，请重新核查门票和步行强度，预算尽量不变。」
# 不含旧关键词表中的任何一项 -> 被当作全新行程 -> 首轮的 人数/预算/天数/强度 **全部丢失**，
# 业务门禁随即报「未识别出游天数 / 未识别预算」。
# 现规则：① 关键词命中即视为修订；② 会话已有行程且本轮仍是旅行类诉求，
# 默认按修订处理（保留既有约束 + 差量重规划），这正是多轮契约承诺的行为。
_REVISION_MARKERS = [
    "还想", "追加", "再加", "第二天", "第三天", "改成", "改为", "调整", "增加", "减少",
    "提高", "降低", "提到", "变成", "换", "去掉", "删除", "加", "顺便", "另外", "改",
    # 补充：修订/复核类表达（此前缺失，导致真实游客第二轮丢失约束）
    "重新", "重核", "复核", "核查", "修正", "更新", "修改", "补充", "加上", "保持",
    "不变", "尽量不", "预算不变", "门票", "步行强度", "强度", "不方便", "行动不便",
]

# 只有「旅行/修订」类诉求才允许升级为修订；纯知识问答（文化/生态）必须保持问答意图
_REVISION_INTENTS = {INTENT_TRAVEL, INTENT_REVISE, INTENT_MIXED}

# V26.1 修正（真实回归）：此前的判定是
#     is_rev = turns>1 and intent in _REVISION_INTENTS and (marker or carry_over)
# 但 detect_intent 对「再加一天变成4天。」「第二天加沙坡头。」这类**修订句**
# 会返回 UNKNOWN（句中没有旅游名词却全是修订动作），于是不等于任何白名单意图 ->
# is_rev=False -> 整轮被当成新任务/领域外，A3 不参与、修订与既有约束全部丢失。
# 40 例回归因此从 40/40 掉到 35/40（D03/D06/D07/D08/E02）。
# 正确语义是**黑名单**：仅「纯知识问答」意图不得升级为修订，
# 其余意图在有修订标记（或在已有行程的旅行类语境下）即按修订处理。
# V31.3 P0-1 修订：内容创作意图同样不得升级为修订（「帮我写一篇小红书攻略」
# 是创作请求，不是行程修改）；且 **「会话已有行程」不再是修订的充分条件**——
# 否则全新的独立旅行需求（「再帮我规划银川一日游」）会被误判为修订并继承旧约束，
# 造成跨轮串扰。修订与否只看**本轮文本自身**的修改语义。
_NO_REVISION_INTENTS = {INTENT_CULTURE, INTENT_ECOLOGY, INTENT_CONTENT}

# 「强修订标记」：即使用户问句里带知识性名词（如「第一天加个宁夏博物馆」含"博物馆"），
# 只要出现这些明显的行程改动动作，仍应按修订处理，而不是当成知识问答。
# V31.3：已上移 contracts.STRONG_REVISION_MARKERS（唯一真源，orchestrator 共用），
# 此处不再维护副本。


class Session:
    def __init__(self, session_id: str = "demo", persist_dir: str = None):
        if persist_dir:
            os.makedirs(persist_dir, exist_ok=True)
            path = os.path.join(persist_dir, f"state_{session_id}.json")
        else:
            path = None
        self.orch = Orchestrator(session_id, persist_path=path)
        self.turns = 0

    def _has_plan(self) -> bool:
        try:
            return bool((self.orch.store.itinerary or {}).get("days_plan"))
        except Exception:
            return False

    def ask(self, text: str) -> dict:
        self.turns += 1
        # 不含 is_revision 的意图判定（避免自证：先看这轮到底想干什么）
        intent = detect_intent(text, False)
        marker = any(k in text for k in _REVISION_MARKERS)
        strong = any(k in text for k in STRONG_REVISION_MARKERS)
        # V31.3 补充修复：内容形态转换句（「改成 Vlog 分镜脚本」）虽含强修订标记，
        # 但 detect_intent 已将其判为内容创作（内容名词 + 形态转换标记）——此时不得
        # 再升级为行程修订，否则 DISPATCH 消息的 revision 标记失实、旧约束被误传 A3。
        # 「把西夏陵去掉，换成水洞沟。」不含内容名词，is_form_conversion=False，
        # 强修订标记的覆盖语义不变（D06/D07/D08 回归锁定）。
        form_convert = intent == INTENT_CONTENT and is_form_conversion(text)
        # 黑名单语义（V26.1 修正，V31.3 P0-1 再修正）：
        #   · 强修订标记（第X天/去掉/换成/加个…）**覆盖**黑名单——真实修订句
        #     「把西夏陵去掉，换成水洞沟。」会被 detect_intent 判为 culture_qa（含"西夏"），
        #     若无覆盖语义，整轮会被当成知识问答，修订与既有约束全部丢失（D06/D07/D08）；
        #   · 非强标记的修订词仅在意图不属于纯问答/内容创作时生效；
        #   · 「会话已有行程」不再是修订的充分条件（见 _NO_REVISION_INTENTS 上方注释），
        #     全新的独立旅行需求不会被误判为修订而继承旧约束。
        is_rev = (self.turns > 1
                  and not form_convert
                  and (strong or (marker and intent not in _NO_REVISION_INTENTS)))
        return self.orch.run(text, is_revision=is_rev, task_prefix=f"U{self.turns}")

    def final_report(self, run_result: dict) -> str:
        """生成给用户的可读报告（确定性拼装，附来源与待核实标注）。

        输出结构对照系统规范（prompts/system.md 第 7 节）：
        - 旅行类：旅行需求概览 / 推荐行程 / 推荐理由 / 文化知识补充 / 需要确认的事项
        - 纯文化/生态问答：简明回答 + 来源 + 不确定项，不套用完整旅行报告格式
        """

        def _lvl(gate, cnt):
            """四级证据状态（对照规范第 6 节）。"""
            if not gate:
                return "UNVERIFIED"
            if gate.get("realtime_flag"):
                return "REALTIME_REQUIRED"
            if cnt == 0:
                return "UNVERIFIED"
            if gate.get("conflict_detected") or not gate.get("can_assert"):
                return "PARTIAL"
            return "VERIFIED" if gate.get("can_assert") else "PARTIAL"

        lines = []
        r = run_result["results"]
        it = r.get("A3", {}).get("itinerary", {}) if "A3" in r else {}
        has_plan = bool(it.get("days_plan"))

        # V31.3 P0-1：内容创作轮（results 含 CONTENT）——直接呈现基于**真实行程**的内容，
        # 不再套用旅行报告格式；无行程时诚实说明原因，不虚构目的地。
        if "CONTENT" in r:
            ct = r["CONTENT"]
            if ct.get("ok"):
                c = ct.get("content") or {}
                lines.append("### 内容创作（%s）" % ct.get("kind"))
                if c.get("title"):
                    lines.append("**%s**" % c["title"])
                if c.get("body"):
                    lines.append(c["body"])
                if c.get("shots"):
                    lines.append("")
                    lines.append("**Vlog 分镜脚本**")
                    for s in c["shots"]:
                        lines.append("- 镜头%s｜%s｜%s｜%s" % (
                            s.get("shot"), s.get("scene"), s.get("type"), s.get("duration")))
                        lines.append("  旁白：%s" % s.get("narration"))
                if c.get("outro"):
                    lines.append("")
                    lines.append(c["outro"])
                lines.append("")
                lines.append("> 生成方式：%s。仅表述本次已确认行程；未排入景点只出现在「可选推荐」区。"
                             % ct.get("mode"))
            else:
                lines.append("### 内容创作未完成")
                lines.append("- %s" % ct.get("error", "未知原因"))
                lines.append("- 请先在会话中完成行程规划，再生成小红书 / 路书 / Vlog 内容。")
        elif has_plan:
            c = r["A3"]["constraints"]
            # 1) 旅行需求概览
            lines.append("### 旅行需求概览")
            comp = "含儿童/亲子" if c.get("party_composition") == "family_with_children" else "未特别说明"
            lines.append("- 出行规模：%s 人（%s）" % (c.get("party_size", "未说明"), comp))
            lines.append("- 行程时长：%s 天｜预算：%s 元｜强度：%s" % (
                c.get("days", "未说明"), c.get("budget_yuan", "未说明"),
                {"low": "轻松", "medium": "适中", "high": "紧凑"}.get(
                    c.get("intensity"), c.get("intensity"))))
            lines.append("- 兴趣：%s｜需求：%s" % (
                "、".join(c.get("interests", [])) or "未说明",
                "、".join(c.get("requirements", [])) or "未说明"))
            if it.get("revision_note"):
                lines.append("- 本轮修订：" + it["revision_note"])
            # 2) 推荐行程
            lines.append("")
            lines.append("### 推荐行程")
            for d in it.get("days_plan", []):
                seg = "；".join("%s（%s，%s）" % (i["name"], i["city"], i.get("duration") or "时长待核实")
                               for i in d.get("items", [])) or "（当日无强制景点）"
                lines.append("- 第%d天：%s" % (d["day"], seg))
                if d.get("food_recommend"):
                    lines.append("  特色美食：%s" % "、".join(f["name"] for f in d["food_recommend"]))
            # 3) 推荐理由
            lines.append("")
            lines.append("### 推荐理由")
            reasons = []
            for d in it.get("days_plan", []):
                for i in d.get("items", []):
                    if i.get("why") and i["why"] != "综合推荐":
                        reasons.append("%s：%s" % (i["name"], i["why"]))
            if reasons:
                for x in reasons[:8]:
                    lines.append("- " + x)
            else:
                lines.append("- 依据您的兴趣与强度约束筛选可信景点资料，并按城市聚簇安排（单日单城，避免跨城奔波）。")
            if it.get("missing_interests"):
                lines.append("- 尚未覆盖的兴趣：%s（可在后续轮次补充）" % "、".join(it["missing_interests"]))
            # 4) 文化知识补充
            if "A1" in r or "A2" in r:
                lines.append("")
                lines.append("### 文化知识补充")
                for key in ("A1", "A2"):
                    if key not in r:
                        continue
                    res = r[key]
                    lines.append("- %s（证据状态：%s）" % (
                        res["answer"].split("\n")[0], _lvl(res.get("gate"), res.get("evidence_count", 0))))
                    for cit in res.get("citations", [])[:3]:
                        lines.append("  来源：%s" % _render_cite(cit))
            # 5) 需要确认的事项
            lines.append("")
            lines.append("### 需要确认的事项")
            lines.append("- " + it.get("budget_note", "预算需以实时价格核验。"))
            if "A4" in r:
                for city, w in r["A4"]["weather"].items():
                    if w.get("ok"):
                        lines.append("- %s天气（已实时查询）：%s°C，风速%skm/h｜%s" % (
                            city, w["temperature_c"], w["windspeed_kmh"], w["source"]))
                    else:
                        lines.append("- %s天气：%s（待核实）" % (city, w.get("source", "未获取到实时数据")))
                tp = r["A4"].get("transport")
                lines.append("- 交通/住宿/消费：%s" % (
                    list(tp.values())[0] if isinstance(tp, dict) and tp else "待核实"))
            if it.get("leftover_attractions"):
                lines.append("- 备选景点（未排入）：%s" % "、".join(it["leftover_attractions"][:5]))
        else:
            # 纯文化/生态问答：简明模式，不套用旅行报告格式
            for key in ("A1", "A2"):
                if key not in r:
                    continue
                res = r[key]
                lines.append(res["answer"])
                lines.append("")
                lines.append("证据状态：%s" % _lvl(res.get("gate"), res.get("evidence_count", 0)))
                for cit in res.get("citations", [])[:3]:
                    lines.append("- 来源：%s" % _render_cite(cit))
                for w in (res.get("gate") or {}).get("warnings", [])[:3]:
                    lines.append("- 提示：%s" % w)

        # 执行摘要（简明、可追踪，不展示冗长内部推理）
        lines.append("")
        lines.append("---")
        v = r.get("A5", {})
        g = run_result["gates"]
        lines.append("执行摘要：A5审核=%s｜门禁 Execution=%s / Business=%s / Release=%s｜task_id=%s" % (
            v.get("verdict", "N/A"), g["execution"]["pass"], g["business"]["pass"],
            g["release"]["pass"], run_result.get("task_id")))
        if v.get("issues"):
            lines.append("审核提示：" + "；".join(v["issues"][:3]))
        return "\n".join(lines)

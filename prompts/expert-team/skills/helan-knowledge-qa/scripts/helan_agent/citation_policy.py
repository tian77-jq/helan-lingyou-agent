# -*- coding: utf-8 -*-
"""文化证据可信度与引用治理策略（V30.0 P0-1~P0-4 的唯一真源）。

**为什么需要这一层**
    文化语料里的 `source.url` 曾出现两类问题：① 不可核验的异常域名；
    ② 本机绝对路径（如 `D:/.../knowledge/index.js`）。二者一旦进入
    游客可读文本或 MCP 返回体，就同时构成「来源不可信」与「本机路径泄露」。
    而修复不能只在 React 界面隐藏——否则 MCP 仍会暴露同一信息。

**因此本模块在共享业务层统一做三件事**
    1. 来源规范化（P0-1/P0-2/P0-3）：
       公开可核验 URL 才允许外显；内部资料改用稳定相对标识 `internal://helan-lingyou/...`，
       永不输出本机绝对路径；无外部来源者显式标注「内部资料，外部来源待核实」。
    2. 引用渲染（游客端/MCP/CLI 同一套文案）：任何入口都不得绕过。
    3. 事实发布策略（P0-4）：严格区分
         · flow_pass                  执行流程是否通过（门禁 / A5）
         · evidence_sufficient        文化证据是否充分
         · claim_assertable           具体事实是否可断言
         · itinerary_publishable      整份行程是否可展示
       四者独立，**不得**因门禁 PASS 就宣称「文化事实均已核验」，
       也**不得**因某条事实存疑就把整份行程判死。

本模块零第三方依赖，仅用标准库。
"""

import re

# ---------------------------------------------------------------- 本机路径识别
# 覆盖：Windows 盘符（D:/ D:\）、UNC（\\host\share）、POSIX 家目录、file:// URI
_LOCAL_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]"),
    re.compile(r"^\\\\[^\\/]+[\\/]"),
    re.compile(r"^/(?:Users|home|root|mnt|Volumes|private)/"),
    re.compile(r"^file://", re.I),
)

# 在任意文本中捞出本机绝对路径（用于兜底净化，含中文标点边界）
# 注意：盘符前必须不是字母/数字，否则 `https://...` 会被误判成 `s:/`（真实踩坑）
_LOCAL_PATH_IN_TEXT = re.compile(
    r"(?:(?<![A-Za-z0-9+.\-])[A-Za-z]:[\\/][^\s\"'<>|，。、；：）】》]*)"   # D:/foo/bar
    r"|(?:\\\\[^\s\"'<>|]+[\\/][^\s\"'<>|]*)"                              # \\host\share
    r"|(?:/(?:Users|home)/[^\s\"'<>|，。、；：）】》]+)"                     # /Users/x
    r"|(?:file://[^\s\"'<>]+)",                                            # file:///...
    re.I,
)

LOCAL_PATH_MASK = "（本机路径已隐去）"

# 内部资料根标识：稳定、可迁移、不含盘符/用户名/工程目录
INTERNAL_REF_ROOT = "internal://helan-lingyou/project-corpus/"

# 证据等级 → 公开引用语义
_HIGH_TRUST = ("official", "official_media")
_LOW_TRUST = ("secondary", "project_internal", "unverified", None)

CITATION_NOTE_INTERNAL = "内部资料，外部来源待核实"
CITATION_NOTE_UNVERIFIED = "外部来源未核实，请以官方发布为准"
CITATION_NOTE_CONTESTED = "该条存在学术争议/不同说法，属研究性推测，非定论"

DISCLOSURE_HEDGE = "以下内容依据现有资料整理，部分事实存在争议或来源待核实，仅供参考，请以官方最新发布为准。"
DISCLOSURE_UNASSERTABLE = "当前证据不足以支持确定性结论，本回答不作出事实断言。"


# ---------------------------------------------------------------- 基础工具
def is_local_path(value):
    """判断字符串是否为本机绝对路径（盘符 / UNC / 家目录 / file://）。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    return any(p.search(v) for p in _LOCAL_PATH_PATTERNS)


def contains_local_path(text):
    """判断文本中是否夹带本机绝对路径。"""
    if not isinstance(text, str) or not text:
        return False
    return bool(_LOCAL_PATH_IN_TEXT.search(text))


def scrub_local_paths(text, mask=LOCAL_PATH_MASK):
    """把文本中的本机绝对路径替换为掩码（兜底防线，正常路径不应触发）。"""
    if not isinstance(text, str) or not text:
        return text
    return _LOCAL_PATH_IN_TEXT.sub(mask, text)


def internal_ref(entry_id, kind="knowledge"):
    """生成稳定的内部来源标识（跨机器可迁移，不含盘符/用户名）。"""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(entry_id or "unknown")).strip("-")
    return "%s%s#%s" % (INTERNAL_REF_ROOT, kind, safe or "unknown")


def _first_public_url(url):
    """返回可用于公开引用的 URL；本机路径 / 空值 / 非 http(s) 一律返回 None。"""
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or is_local_path(u):
        return None
    if not re.match(r"^https?://", u, re.I):
        return None
    return u


# ---------------------------------------------------------------- 来源规范化
def normalize_source(source, entry_id=None, verified=None, claim_status=None):
    """把语料中的 `source` 对象规范化为**可安全外显**的引用结构。

    返回字段中 `source_url` 只可能是公开 http(s) 链接或 None，
    **绝不含本机路径**；内部资料用 `source_ref` 提供稳定溯源标识。

    参数：
        source       语料 source 对象（dict）或纯字符串名称
        entry_id     条目 id，用于生成 internal ref
        verified     条目级核验标记（语料顶层 verified）
        claim_status 条目级事实状态：assertable / contested / unverified / internal_unverified
    """
    src = source if isinstance(source, dict) else {"name": str(source or "")}
    name = (src.get("name") or src.get("publisher") or "").strip()
    publisher = (src.get("publisher") or "").strip()
    level = src.get("evidenceLevel")
    visibility = (src.get("visibility") or "").strip().lower()
    external_status = (src.get("external_status") or "").strip().lower()

    raw_url = src.get("url")
    raw_identifier = src.get("identifier")
    redacted = False

    public_url = _first_public_url(raw_url)
    if raw_url and public_url is None:
        # 原本填了东西但不是可公开的 http(s)：要么是本机路径，要么是非法值
        redacted = True
    if raw_url and is_local_path(raw_url):
        visibility = "internal"

    if not visibility:
        visibility = "public" if public_url else "internal"

    if not external_status:
        if public_url and level in _HIGH_TRUST:
            external_status = "verified"
        elif public_url:
            external_status = "unverified"
        else:
            external_status = "unverified"

    # 内部资料 / 无可验证链接 → 不对外给 URL，给稳定 ref
    if visibility == "internal" or not public_url:
        source_url = None
        source_ref = (raw_identifier if isinstance(raw_identifier, str)
                      and raw_identifier.startswith("internal://")
                      else internal_ref(entry_id or name))
    else:
        source_url = public_url
        source_ref = public_url

    # 展示名净化（防止名称里也带路径）
    display = scrub_local_paths(name) or "未标注来源"
    publisher = scrub_local_paths(publisher)

    # 逐条提示：优先级 争议 > 内部 > 未核实
    if claim_status == "contested":
        note = CITATION_NOTE_CONTESTED
    elif visibility == "internal":
        note = CITATION_NOTE_INTERNAL
    elif external_status != "verified":
        note = CITATION_NOTE_UNVERIFIED
    else:
        note = ""

    if claim_status is None:
        if visibility == "internal":
            claim_status = "internal_unverified"
        elif external_status == "verified":
            claim_status = "assertable"
        else:
            claim_status = "unverified"

    is_verified = bool(verified) and visibility == "public" and external_status == "verified"

    return {
        "source_name": display,
        "source_display": display,
        "publisher": publisher,
        "source_url": source_url,
        "source_ref": source_ref,
        "source_visibility": visibility,
        "external_status": external_status,
        "evidence_level": level,
        "verified": is_verified,
        "retrieved_at": src.get("retrievedAt"),
        "claim_status": claim_status,
        "citation_note": note,
        "path_redacted": redacted,
    }


def normalize_citation(entry, index=1):
    """把检索结果/证据条目规范化为引用条目（citations[] 的元素）。"""
    e = entry or {}
    entry_id = e.get("id")
    base = normalize_source(
        e.get("_raw_source") or e.get("source") or {
            "name": e.get("source_name"),
            "publisher": e.get("source_publisher"),
            "url": e.get("source_url"),
            "evidenceLevel": e.get("evidence_level"),
            "retrievedAt": e.get("retrieved_at"),
        },
        entry_id=entry_id,
        verified=e.get("verified"),
        claim_status=e.get("claim_status"),
    )
    out = {
        "index": index,
        "id": entry_id,
        "title": scrub_local_paths(e.get("title") or ""),
    }
    out.update(base)
    return out


# ---------------------------------------------------------------- 文本渲染
def render_source_line(citation, with_title=True):
    """游客端 / MCP / CLI 统一的「来源行」文案（永不出现本机路径）。"""
    c = citation or {}
    name = c.get("source_display") or c.get("source_name") or "未标注来源"
    bits = []
    if with_title and c.get("title"):
        bits.append("[%s] %s" % (c.get("title"), name))
    else:
        bits.append(name)
    if c.get("source_url"):
        bits.append(c["source_url"])
    elif c.get("source_ref"):
        bits.append(c["source_ref"])
    bits.append("核验：%s" % ("已核验" if c.get("verified") else "待核实"))
    if c.get("citation_note"):
        bits.append(c["citation_note"])
    return "｜".join(bits)


# ---------------------------------------------------------------- 事实发布策略
def build_claim_policy(gate, citations=None):
    """把证据门裁决翻译成**四层可区分的发布策略**（P0-4）。

    注意：本函数只回答「文化证据/事实」层面的问题，
    `flow_pass` / `itinerary_publishable` 由门禁与 A5 在上层给出，不在此处判定。
    """
    g = gate or {}
    cites = citations or []
    grounded = g.get("grounding") == "grounded"
    conflict = bool(g.get("conflict_detected"))
    realtime = bool(g.get("realtime_flag"))
    can_assert = bool(g.get("can_assert"))
    confidence = g.get("confidence") or "low"

    unverified = [c for c in cites if c.get("external_status") != "verified"]
    public_verified = [c for c in cites
                       if c.get("source_visibility") == "public"
                       and c.get("external_status") == "verified"
                       and c.get("source_url")]

    reasons = []
    if not grounded:
        reasons.append("未检索到可用证据")
    if conflict:
        reasons.append("证据间存在争议/差异，不得强行统一为确定结论")
    if realtime:
        reasons.append("涉及动态信息，须以官方最新公告核验")
    if unverified:
        reasons.append("%d/%d 条来源未获外部独立核验" % (len(unverified), len(cites)))

    if not grounded:
        level = "unassertable"
    elif can_assert and not conflict and not realtime and confidence in ("high", "medium"):
        level = "assertable"
    else:
        level = "hedged"

    if level == "assertable":
        disclosure = ""
    elif level == "hedged":
        disclosure = DISCLOSURE_HEDGE
    else:
        disclosure = DISCLOSURE_UNASSERTABLE

    return {
        "evidence_sufficient": grounded,
        "claim_assertable": level == "assertable",
        "assertion_level": level,
        "confidence": confidence,
        "conflict_detected": conflict,
        "realtime_flag": realtime,
        "public_verified_source_count": len(public_verified),
        "unverified_source_count": len(unverified),
        "has_public_source": bool(public_verified),
        "reasons": reasons,
        "disclosure": disclosure,
        "note": ("门禁/A5 PASS 仅表示执行流程与结构合规，"
                 "不代表每条文化事实均可断言；两者须分开表述。"),
    }


def apply_assertion_guard(text, policy, header=True):
    """确保游客可读文本带有与事实可靠性相称的不确定性提示。

    只在策略非 assertable 时生效；若文本已含提示则不重复追加。
    """
    if not isinstance(text, str) or not text:
        return text
    p = policy or {}
    if p.get("claim_assertable"):
        return text
    disclosure = p.get("disclosure") or ""
    if not disclosure:
        return text
    if disclosure[:12] in text:
        return text
    return ("%s\n\n%s" % (disclosure, text)) if header else ("%s\n\n%s" % (text, disclosure))

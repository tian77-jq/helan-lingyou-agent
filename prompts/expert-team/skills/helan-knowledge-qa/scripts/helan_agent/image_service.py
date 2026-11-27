# -*- coding: utf-8 -*-
"""景点与美食真实匹配图片服务适配层（V31.3 任务书 §6）。

**为什么需要这一层**
    行程要展示景点/美食图片，但图片是最容易造假的一类数据：
    语料里曾写死 39 条 `/assets/thumbs/...` 引用，而那些文件在工程中
    从未存在（git 历史零记录）——前端一旦渲染就是「假图引用」。
    本模块把图片获取收敛为**一个适配层 + 一份九字段契约**，
    所有入口（Web / MCP / CLI）共用，杜绝各入口各找各的「示意图」。

**设计原则（对应任务书条款）**
    1. §6.2 五级来源优先级：官方授权 > 政府公开 > 开放许可图库 >
       项目内部语料图 > 无图。**宁可不展示，也不用误导性图片**：
       匹配置信度不足的候选一律丢弃（`MATCH_THRESHOLD`），
       示意图仅在不严格模式下展示且强制 `is_illustrative=True`。
    2. §6.3 九字段数据契约：image_url / source_name / source_page_url /
       photographer / license / retrieved_at / matched_entity /
       match_confidence / is_illustrative。**缺失字段如实留空，禁止编造**。
    3. §6.4 密钥只进后端：Pexels / Unsplash 的 Key 仅从环境变量读取
       （`HELAN_PEXELS_API_KEY` / `HELAN_UNSPLASH_ACCESS_KEY`），
       永不下发前端、永不写入语料与日志。
    4. 失败不破坏行程：所有公开方法**绝不抛异常**；任何 provider 故障
       只记录在 `image_report`，行程规划照常返回。
    5. 状态如实上报：每个 provider 报告 READY / NEEDS_AUTH /
       NETWORK_BLOCKED / LOCAL_MISSING，附实测原因（HTTP 码或超时），
       供 `/api/capability` 与执行轨迹取用——未授权就说未授权，
       不把「没有图」伪装成「有图」。

本模块零第三方依赖，仅用标准库；网络探测结果按 TTL 缓存，避免每次行程
都重复付出超时代价。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 契约与常量

# §6.3 九字段数据契约（顺序即文档顺序；任何图片对象必须可被校验为含这些键）
IMAGE_FIELDS: Tuple[str, ...] = (
    "image_url",            # 图片直链（可加载的 http(s) 地址）
    "source_name",          # 来源名称（图库/机构名）
    "source_page_url",      # 来源页面（供用户核验与版权追溯）
    "photographer",         # 作者/摄影者（未知留空字符串，不写「未知作者」之外的猜测）
    "license",              # 许可（如 CC BY-SA 4.0；未知留空）
    "retrieved_at",         # 本系统获取时间（ISO8601 UTC）
    "matched_entity",       # 匹配的实体名（景点/美食中文名）
    "match_confidence",     # 匹配置信度 0~1（判定依据见 validate_image_match）
    "is_illustrative",      # 是否为示意图（非该实体专属图片时必须为 True）
)

# §6.2 五级来源优先级（数字越小越优先；第 1/2 级当前无授权渠道，未实现）
PRIORITY_OFFICIAL = 1        # 景区/餐厅官方授权素材
PRIORITY_GOVERNMENT = 2      # 政府公开渠道（文旅部门公开素材）
PRIORITY_OPEN_LICENSE = 3    # 开放许可图库（Wikimedia Commons / Pexels / Unsplash）
PRIORITY_PROJECT_CORPUS = 4  # 项目内部语料自带图（须标注内部资料）
PRIORITY_NONE = 5            # 无图：诚实占位，不使用误导性图片

# Provider 状态（如实上报，附实测原因）
STATUS_READY = "READY"                    # 当前可用
STATUS_NEEDS_AUTH = "NEEDS_AUTH"          # 主机可达但缺 Key / Key 被拒（实测 401）
STATUS_NETWORK_BLOCKED = "NETWORK_BLOCKED"  # 本机不可达（实测连接超时）
STATUS_LOCAL_MISSING = "LOCAL_MISSING"    # 本地语料图文件不存在
STATUS_ERROR = "ERROR"                    # 其它故障（原因见 reason）

# 匹配分级（名称证据驱动，不凭搜索排名假装匹配）
MATCH_EXACT = 1.0          # 实体名完整出现在候选标题/描述中
MATCH_ALIAS = 0.85         # 别名或核心词命中
MATCH_TOKEN = 0.65         # 显著词重叠
MATCH_ILLUSTRATIVE = 0.45  # 仅主题相关（只能作示意图）
MATCH_THRESHOLD = 0.6      # >= 才可作为「真实匹配」展示；低于则丢弃

# 中英文实体别名（仅收「已验证有对应条目主图」的名称，避免为凑图乱配）。
# 用途：中文维基无主图时，回退英文维基条目主图；匹配校验也接受这些别名。
ENTITY_ALIASES: Dict[str, List[str]] = {
    "西夏陵": ["Xixia Tombs", "Western Xia mausoleums"],
    "沙坡头": ["Shapotou"],
    "宁夏博物馆": ["Ningxia Museum"],
    "水洞沟": ["Shuidonggou"],
    "镇北堡西部影城": ["Zhenbeibao", "Zhenbeibu"],
    "须弥山石窟": ["Xumishan Grottoes"],
    "青铜峡黄河大峡谷": ["Qingtongxia"],
}

# 环境变量（§6.4：Key 只进后端环境配置）
ENV_PEXELS_KEY = "HELAN_PEXELS_API_KEY"
ENV_UNSPLASH_KEY = "HELAN_UNSPLASH_ACCESS_KEY"
ENV_HTTP_TIMEOUT = "HELAN_IMAGE_HTTP_TIMEOUT"
ENV_MAX_RESULTS = "HELAN_IMAGE_MAX_RESULTS"
ENV_CACHE_DIR = "HELAN_IMAGE_CACHE_DIR"
ENV_ALLOW_ILLUSTRATIVE = "HELAN_IMAGE_ALLOW_ILLUSTRATIVE"

DEFAULT_HTTP_TIMEOUT = 10.0    # 单次 HTTP 超时（秒）
PROBE_TIMEOUT = 4.0            # 可达性探测超时（秒，探测不占用正常请求预算）
PROBE_TTL = 300.0              # 探测结果缓存（秒）
CACHE_TTL = 3600.0             # 搜索结果缓存（秒）
DEFAULT_MAX_RESULTS = 5
ENRICH_DEADLINE = 12.0         # 单次行程 enrich 的总时间预算（秒）


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "")
        return float(v) if v.strip() else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name, "")
        return int(v) if v.strip() else default
    except (TypeError, ValueError):
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _utc_now() -> str:
    """ISO8601 UTC 时间戳（零依赖，秒级精度）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm_text(s: Any) -> str:
    """名称归一化：去空白/常见标点，小写。"""
    if not isinstance(s, str):
        return ""
    return re.sub(r'[\s，。、；：？！“”‘’（）《》<>\[\]【】·…—,.;:?!\"\'()/\-_=+]+',
                  '', s).strip().lower()


def _tokens(s: Any) -> set:
    """从**原始**字符串提取显著词集合（不做去空白归一化，保留词边界）：
    - 拉丁/数字：按空格与驼峰拆词（"Western Xia Tombs" → western/xia/tombs）；
    - 中文：整段 + 相邻双字（"贺兰山岩画" → 贺兰山岩画/贺兰/兰山/山岩/岩画）。
    """
    if not isinstance(s, str) or not s:
        return set()
    toks = set()
    for part in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", s.lower()):
        if re.match(r"^[a-z0-9]+$", part):
            if len(part) >= 2:
                toks.add(part)
            for w in re.findall(r"[a-z]+|\d+", part):  # 驼峰再拆
                if len(w) >= 2:
                    toks.add(w)
        else:
            toks.add(part)
            for i in range(len(part) - 1):
                toks.add(part[i:i + 2])
    return toks


# ---------------------------------------------------------------- HTTP 基础
def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: float = DEFAULT_HTTP_TIMEOUT
                   ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """GET JSON。成功返回 (data, None)；失败返回 (None, 原因)。

    原因分类（供 provider 判定状态）：
        "http_401"  —— 主机可达但鉴权失败（缺 Key/Key 被拒）
        "timeout"   —— 连接/读取超时（本机不可达的典型表现）
        "http_<码>" —— 其它 HTTP 错误
        "network_error:<异常名>" —— DNS/连接类错误
    本函数自身不抛异常。
    """
    req = urllib.request.Request(url, headers=headers or {},
                                 method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)  # 1MB 足够搜索页
            try:
                return json.loads(raw.decode("utf-8", "replace")), None
            except ValueError:
                return None, "invalid_json"
    except urllib.error.HTTPError as e:
        return None, "http_%d" % e.code
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if "timed out" in str(reason).lower() or "timeout" in str(reason).lower():
            return None, "timeout"
        return None, "network_error:%s" % type(reason).__name__
    except Exception as e:  # 兜底：任何意外都不得外抛
        return None, "network_error:%s" % type(e).__name__


def _blank_image(entity: str) -> Dict[str, Any]:
    """构造一张「全空但契约完整」的图片对象（用于失败/无图时的诚实占位）。"""
    return {f: ("" if f in ("photographer", "license", "image_url",
                            "source_name", "source_page_url") else
                (entity if f == "matched_entity" else
                 (0.0 if f == "match_confidence" else False)))
            for f in IMAGE_FIELDS}


def _finalize_image(raw: Dict[str, Any], entity: str,
                    confidence: float, illustrative: bool) -> Dict[str, Any]:
    """把 provider 原始结果收敛为九字段契约对象（缺项留空，不编造）。"""
    out = _blank_image(entity)
    for f in IMAGE_FIELDS:
        v = raw.get(f)
        if v is None:
            continue
        if f == "match_confidence":
            out[f] = float(v)
        elif f == "is_illustrative":
            out[f] = bool(v)
        elif isinstance(v, str):
            out[f] = v.strip()
        else:
            out[f] = v
    out["matched_entity"] = entity
    out["match_confidence"] = round(float(confidence), 3)
    out["is_illustrative"] = bool(illustrative)
    if not out.get("retrieved_at"):
        out["retrieved_at"] = _utc_now()
    # 没有直链的「图片」没有展示价值，直接判空
    if not out.get("image_url") or not re.match(r"^https?://", out["image_url"]):
        return _blank_image(entity)
    return out


# ---------------------------------------------------------------- 匹配校验
def validate_image_match(entity_name: str, candidate: Dict[str, Any],
                         aliases: Optional[List[str]] = None) -> Dict[str, Any]:
    """校验候选图片是否真实匹配实体（§6.2「宁可不展示」的判定核心）。

    只看**名称证据**：实体名/别名/显著词是否出现在候选的标题、描述、
    标签中；搜索排名本身不作为匹配证据（防「搜到啥放啥」）。

    返回：
        {
          "match_confidence": float,
          "matched": bool,          # >= MATCH_THRESHOLD，可作真实匹配展示
          "is_illustrative": bool,  # 仅主题相关，只能作示意图
          "evidence": str,          # 命中依据（可写入轨迹，供复核）
        }
    """
    names = [entity_name] + [a for a in (aliases or []) if a]
    norm_names = [_norm_text(n) for n in names if _norm_text(n)]
    title = _norm_text(candidate.get("title") or "")
    desc = _norm_text(candidate.get("description") or "")
    tags = _norm_text(" ".join(candidate.get("tags") or []))
    haystack = " ".join(x for x in (title, desc, tags) if x)

    evidence = "无名称证据"
    if not norm_names or not haystack:
        return {"match_confidence": 0.0, "matched": False,
                "is_illustrative": False, "evidence": evidence}

    # 1) 实体名完整出现在标题 → 精确匹配；出现在描述/标签 → 别名级
    for raw in names:
        n = _norm_text(raw)
        if not n:
            continue
        if n in haystack:
            conf = MATCH_EXACT if n in title else MATCH_ALIAS
            return {"match_confidence": conf, "matched": conf >= MATCH_THRESHOLD,
                    "is_illustrative": conf < MATCH_THRESHOLD,
                    "evidence": "实体名『%s』命中候选%s" % (raw, "标题" if n in title else "描述/标签")}

    # 2) 显著词覆盖率：对实体名与每个别名**分别**计算「候选覆盖了该名称
    #    多少比例」，取最大——避免别名里的额外单词稀释匹配（真实案例：
    #    别名 "Western Xia Imperial Tombs" vs 候选 "Western Xia Tombs"）。
    cand_tokens = (_tokens(candidate.get("title"))
                   | _tokens(candidate.get("description"))
                   | _tokens(" ".join(candidate.get("tags") or [])))
    best_cov, best_name = 0.0, ""
    for raw in names:
        nt = _tokens(raw)
        if not nt:
            continue
        cov = len(nt & cand_tokens) / len(nt)
        if cov > best_cov:
            best_cov, best_name = cov, raw
    if best_cov >= 0.6:
        conf = MATCH_TOKEN if best_cov >= 0.8 else MATCH_TOKEN - 0.05
        return {"match_confidence": round(conf, 3),
                "matched": conf >= MATCH_THRESHOLD,
                "is_illustrative": conf < MATCH_THRESHOLD,
                "evidence": "名称『%s』显著词覆盖率 %.0f%%" % (best_name, best_cov * 100)}
    if best_cov >= 0.3:
        return {"match_confidence": MATCH_ILLUSTRATIVE, "matched": False,
                "is_illustrative": True,
                "evidence": "名称『%s』显著词覆盖率 %.0f%%，仅主题相关"
                            % (best_name, best_cov * 100)}

    # 3) 仅有主题相关性（ provider 自述 relevance），不可作为真实匹配
    return {"match_confidence": MATCH_ILLUSTRATIVE, "matched": False,
            "is_illustrative": True, "evidence": "仅主题相关，无名称证据"}


# ---------------------------------------------------------------- Provider 基类
class ImageProvider:
    """图片源提供者基类。子类必须实现 name/priority/search。

    状态探测（status）带 TTL 缓存；search 永不抛异常。
    """

    name = "base"
    priority = PRIORITY_OPEN_LICENSE
    requires_key = False

    def __init__(self) -> None:
        self._status_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        self._lock = threading.Lock()

    # ---- 子类实现 ----
    def _probe(self) -> Dict[str, Any]:
        """实测当前状态（子类实现；允许做一次轻量网络请求）。"""
        return {"state": STATUS_READY, "reason": ""}

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        """执行搜索，返回原始候选列表（子类实现）。"""
        return []

    # ---- 公开接口（带缓存/防抛）----
    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._status_cache and now - self._status_cache[0] < PROBE_TTL:
                return dict(self._status_cache[1])
        st = self._probe()  # _probe 自身也不得抛异常
        with self._lock:
            self._status_cache = (now, st)
        return dict(st)

    def search(self, entity_type: str, query: str,
               limit: int = DEFAULT_MAX_RESULTS) -> Dict[str, Any]:
        """搜索入口：返回 {"images": [...], "error": None|str}，永不抛异常。"""
        st = self.status()
        if st.get("state") != STATUS_READY:
            return {"images": [], "error": st.get("reason") or st.get("state")}
        try:
            raws = self._search(entity_type, query, limit) or []
        except Exception as e:  # 兜底防线
            return {"images": [], "error": "provider_exception:%s" % type(e).__name__}
        return {"images": raws, "error": None}


class LocalCorpusProvider(ImageProvider):
    """项目内部语料自带图（P4）。

    只承认**磁盘上真实存在**的语料图文件；语料里写了路径但文件不存在
    （历史 39 条死链正是如此）时如实报告 LOCAL_MISSING，不把死链当图片。
    """

    name = "local_corpus"
    priority = PRIORITY_PROJECT_CORPUS
    requires_key = False

    def __init__(self, corpus_dir: Optional[str] = None) -> None:
        super().__init__()
        self._corpus_dir = corpus_dir or _find_corpus_dir()
        self._index: Optional[Dict[str, Dict[str, Any]]] = None

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        if self._index is not None:
            return self._index
        idx: Dict[str, Dict[str, Any]] = {}
        for fname, kind in (("attractions.json", "attraction"),
                            ("foods.json", "food")):
            path = os.path.join(self._corpus_dir, fname)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            for entry in (data.get("attractions") if kind == "attraction"
                          else data.get("foods")) or []:
                eid = entry.get("id")
                ref = entry.get("image")
                if eid and isinstance(ref, str) and ref.startswith("/"):
                    disk = os.path.join(self._corpus_dir,
                                        ref.lstrip("/").replace("/", os.sep))
                    idx[_norm_text(_entry_name(entry))] = {
                        "id": eid, "name": _entry_name(entry), "ref": ref,
                        "exists": os.path.exists(disk), "kind": kind,
                    }
        self._index = idx
        return idx

    def _probe(self) -> Dict[str, Any]:
        idx = self._load_index()
        real = [v for v in idx.values() if v["exists"]]
        if real:
            return {"state": STATUS_READY,
                    "reason": "语料自带图 %d 个可用（内部资料，展示须标注）" % len(real)}
        if idx:
            return {"state": STATUS_LOCAL_MISSING,
                    "reason": "语料 image 引用 %d 条，磁盘均无对应文件"
                              "（历史死链已按无图处理）" % len(idx)}
        return {"state": STATUS_LOCAL_MISSING,
                "reason": "语料未携带可用本地图片（历史 39 条死链已治理为无图状态，"
                          "真实图片经开放许可图库获取）"}

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        key = _norm_text(query)
        idx = self._load_index()
        hit = idx.get(key)
        if not hit or not hit["exists"]:
            return []
        ref = hit["ref"]
        return [{
            "image_url": ref,  # 站内相对路径，由前端拼接基址
            "source_name": "项目内部语料（%s）" % hit["id"],
            "source_page_url": "",
            "photographer": "",
            "license": "内部资料，外部来源待核实",
            "matched_entity": hit["name"],
        }]


class WikimediaCommonsProvider(ImageProvider):
    """Wikimedia Commons 开放许可图库（P3，无需 Key，本机实测不可达）。

    真实实现完整可用（API 查询 + 元数据解析 + 许可/作者提取），
    状态由实测决定：本机连接超时 → NETWORK_BLOCKED（不伪装可用）。
    """

    name = "wikimedia_commons"
    priority = PRIORITY_OPEN_LICENSE
    requires_key = False
    API = "https://commons.wikimedia.org/w/api.php"
    # Wikimedia API 强制要求合规 User-Agent（否则可能被拒），可达环境必须携带
    UA = "HelanLingyouBot/1.0 (Helan Youling Ningxia travel agent; +https://example.com)"

    def _probe(self) -> Dict[str, Any]:
        url = self.API + "?action=query&format=json&meta=siteinfo"
        _, err = _http_get_json(url, timeout=PROBE_TIMEOUT,
                               headers={"User-Agent": self.UA})
        if err is None:
            return {"state": STATUS_READY, "reason": "commons API 可达"}
        if err == "timeout":
            return {"state": STATUS_NETWORK_BLOCKED,
                    "reason": "commons.wikimedia.org 连接超时（本机网络不可达，"
                              "与授权无关；更换网络环境后自动恢复）"}
        return {"state": STATUS_NETWORK_BLOCKED,
                "reason": "commons API 探测失败：%s" % err}

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": "%s %s" % (query, entity_type), "gsrnamespace": "6",
            "gsrlimit": str(max(1, limit)), "prop": "imageinfo",
            "iiprop": "url|extmetadata", "iiurlwidth": "800",
        })
        data, err = _http_get_json(self.API + "?" + params,
                                  headers={"User-Agent": self.UA})
        if err is not None or not isinstance(data, dict):
            return []
        pages = ((data.get("query") or {}).get("pages") or {})
        out: List[Dict[str, Any]] = []
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            md = info.get("extmetadata") or {}

            def _md(key: str) -> str:
                v = md.get(key) or {}
                val = v.get("value") if isinstance(v, dict) else v
                if not isinstance(val, str):
                    return ""
                return re.sub(r"<[^>]+>", "", val).strip()

            out.append({
                "title": page.get("title") or "",
                "description": _md("ImageDescription"),
                "image_url": info.get("thumburl") or info.get("url") or "",
                "source_name": "Wikimedia Commons",
                "source_page_url": info.get("descriptionurl") or "",
                "photographer": _md("Artist"),
                "license": _md("LicenseShortName"),
                "tags": [],
            })
        return out


class WikipediaPageImageProvider(ImageProvider):
    """中文维基百科条目主图（P3，无需 Key）。

    与 Commons 全文搜索相比，「条目主图」与实体一一对应，匹配证据更强：
    只取 `pageimages` 返回的该条目主图，再做 Commons 元数据（作者/许可）补全；
    找不到主图就返回空，不用搜索排名硬凑。
    """

    name = "wikipedia_pageimage"
    priority = PRIORITY_OPEN_LICENSE
    requires_key = False
    API = "https://zh.wikipedia.org/w/api.php"
    API_EN = "https://en.wikipedia.org/w/api.php"
    COMMONS_API = "https://commons.wikimedia.org/w/api.php"
    UA = "HelanLingyouBot/1.0 (Helan Youling Ningxia travel agent; +https://example.com)"

    def _probe(self) -> Dict[str, Any]:
        url = self.API + "?action=query&format=json&meta=siteinfo"
        _, err = _http_get_json(url, timeout=PROBE_TIMEOUT,
                                headers={"User-Agent": self.UA})
        if err is None:
            return {"state": STATUS_READY, "reason": "zh.wikipedia pageimages API 可达"}
        if err == "timeout":
            return {"state": STATUS_NETWORK_BLOCKED,
                    "reason": "zh.wikipedia 连接超时（与授权无关；更换网络后自动恢复）"}
        return {"state": STATUS_NETWORK_BLOCKED,
                "reason": "zh.wikipedia 探测失败：%s" % err}

    @staticmethod
    def _md(md: Dict[str, Any], key: str) -> str:
        v = (md or {}).get(key) or {}
        val = v.get("value") if isinstance(v, dict) else v
        if not isinstance(val, str):
            return ""
        return re.sub(r"<[^>]+>", "", val).strip()

    def _page(self, query: str, lang: str = "zh") -> Optional[Dict[str, str]]:
        api = self.API if lang == "zh" else self.API_EN
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "titles": query,
            "prop": "pageimages", "piprop": "thumbnail|original|name",
            "pithumbsize": "800", "redirects": "1",
        })
        data, err = _http_get_json(api + "?" + params,
                                  headers={"User-Agent": self.UA})
        if err is not None or not isinstance(data, dict):
            return None
        pages = ((data.get("query") or {}).get("pages") or {})
        for page in pages.values():
            if "missing" in page:
                continue
            file = page.get("pageimage") or ""
            thumb = ((page.get("thumbnail") or {}).get("source") or "")
            original = ((page.get("original") or {}).get("source") or "")
            if file or thumb or original:
                return {"title": page.get("title") or query, "file": file,
                        "thumb": thumb, "original": original}
        return None

    def _commons_meta(self, file: str) -> Dict[str, str]:
        if not file:
            return {}
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "titles": "File:" + file,
            "prop": "imageinfo", "iiprop": "url|extmetadata",
            "iiurlwidth": "800",
        })
        data, err = _http_get_json(self.COMMONS_API + "?" + params,
                                  headers={"User-Agent": self.UA})
        if err is not None or not isinstance(data, dict):
            return {}
        pages = ((data.get("query") or {}).get("pages") or {})
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            md = info.get("extmetadata") or {}
            return {
                "thumb": info.get("thumburl") or "",
                "page": info.get("descriptionurl") or "",
                "artist": self._md(md, "Artist"),
                "license": self._md(md, "LicenseShortName"),
            }
        return {}

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        # 美食条目的主图不一定是该菜品，暂不冒用；景点/博物馆/遗址才取。
        if entity_type != "attraction":
            return []
        page = self._page(query, "zh")
        if not page:
            # 中文条目无主图时，仅对已知有英文条目的实体回退，避免英文搜索乱配。
            for alias in ENTITY_ALIASES.get(query, []):
                page = self._page(alias, "en")
                if page:
                    break
        if not page:
            return []
        meta = self._commons_meta(page.get("file", ""))
        image_url = meta.get("thumb") or page.get("thumb") or page.get("original") or ""
        if not image_url:
            return []
        return [{
            "title": page.get("title") or query,
            "description": page.get("file") or "",
            "image_url": image_url,
            "source_name": "Wikipedia / Wikimedia Commons",
            "source_page_url": meta.get("page") or "",
            "photographer": meta.get("artist") or "",
            "license": meta.get("license") or "",
            "tags": [page.get("file")] if page.get("file") else [],
        }]


class _KeyedApiProvider(ImageProvider):
    """需要 API Key 的图库提供者基类（Pexels / Unsplash）。

    Key 只从环境变量读取；未配置 → NEEDS_AUTH（不发起任何请求）；
    配置后被拒（401）→ NEEDS_AUTH 并如实记录 api_key_rejected。
    """

    key_env = ""
    api_host = ""

    def _key(self) -> str:
        return (os.environ.get(self.key_env) or "").strip()

    def status(self) -> Dict[str, Any]:
        if not self._key():
            return {"state": STATUS_NEEDS_AUTH,
                    "reason": "未配置 %s（宿主 %s 可达，属授权缺失而非网络故障）"
                              % (self.key_env, self.api_host)}
        return super().status()

    def _probe(self) -> Dict[str, Any]:
        data, err = self._probe_request()
        if err is None:
            return {"state": STATUS_READY, "reason": "API 鉴权通过"}
        if err == "http_401":
            return {"state": STATUS_NEEDS_AUTH,
                    "reason": "%s 被拒（api_key_rejected）" % self.key_env}
        if err == "timeout":
            return {"state": STATUS_NETWORK_BLOCKED,
                    "reason": "%s 连接超时" % self.api_host}
        return {"state": STATUS_ERROR, "reason": "%s 探测失败：%s"
                                           % (self.name, err)}

    def _probe_request(self):
        raise NotImplementedError

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError


class PexelsProvider(_KeyedApiProvider):
    """Pexels 开放许可图库（P3，需 Key）。"""

    name = "pexels"
    priority = PRIORITY_OPEN_LICENSE
    requires_key = True
    key_env = ENV_PEXELS_KEY
    api_host = "api.pexels.com"
    API = "https://api.pexels.com/v1/search"

    def _probe_request(self):
        return _http_get_json(
            self.API + "?query=ningxia&per_page=1",
            headers={"Authorization": self._key()}, timeout=PROBE_TIMEOUT)

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        url = self.API + "?" + urllib.parse.urlencode(
            {"query": "%s %s" % (query, "ningxia" if entity_type == "attraction" else "food"),
             "per_page": str(max(1, limit))})
        data, err = _http_get_json(url, headers={"Authorization": self._key()})
        if err is not None or not isinstance(data, dict):
            return []
        out: List[Dict[str, Any]] = []
        for ph in (data.get("photos") or [])[:limit]:
            src = ph.get("src") or {}
            out.append({
                "title": ph.get("alt") or "",
                "description": ph.get("alt") or "",
                "image_url": src.get("large") or src.get("medium") or "",
                "source_name": "Pexels",
                "source_page_url": ph.get("url") or "",
                "photographer": ph.get("photographer") or "",
                "license": "Pexels License（免费商用，无需署名）",
                "tags": [],
            })
        return out


class UnsplashProvider(_KeyedApiProvider):
    """Unsplash 开放许可图库（P3，需 Key）。"""

    name = "unsplash"
    priority = PRIORITY_OPEN_LICENSE
    requires_key = True
    key_env = ENV_UNSPLASH_KEY
    api_host = "api.unsplash.com"
    API = "https://api.unsplash.com/search/photos"

    def _probe_request(self):
        return _http_get_json(
            self.API + "?query=ningxia&per_page=1",
            headers={"Authorization": "Client-ID %s" % self._key()},
            timeout=PROBE_TIMEOUT)

    def _search(self, entity_type: str, query: str,
                limit: int) -> List[Dict[str, Any]]:
        url = self.API + "?" + urllib.parse.urlencode(
            {"query": "%s %s" % (query, "ningxia" if entity_type == "attraction" else "food"),
             "per_page": str(max(1, limit))})
        data, err = _http_get_json(
            url, headers={"Authorization": "Client-ID %s" % self._key()})
        if err is not None or not isinstance(data, dict):
            return []
        out: List[Dict[str, Any]] = []
        for ph in (data.get("results") or [])[:limit]:
            urls = ph.get("urls") or {}
            user = ph.get("user") or {}
            out.append({
                "title": ph.get("alt_description") or "",
                "description": ph.get("description") or "",
                "image_url": urls.get("regular") or urls.get("small") or "",
                "source_name": "Unsplash",
                "source_page_url": ph.get("links", {}).get("html") or "",
                "photographer": user.get("name") or "",
                "license": "Unsplash License（免费商用）",
                "tags": [],
            })
        return out


def _find_corpus_dir() -> str:
    """向上搜索语料目录（与 retriever 同策略，规避跨包路径错位）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        cand = os.path.join(base, "corpus")
        if os.path.isdir(cand):
            return cand
        base = os.path.dirname(base)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")


def _entry_name(entry: Dict[str, Any]) -> str:
    n = entry.get("name")
    if isinstance(n, dict):
        return n.get("zh-CN") or ""
    return str(n or "")


def _default_providers() -> List[ImageProvider]:
    """默认注册表：按五级优先级排序（P3 内部：条目主图优先，其次是 Commons 搜索）。"""
    return [
        WikipediaPageImageProvider(),
        WikimediaCommonsProvider(),
        PexelsProvider(),
        UnsplashProvider(),
        LocalCorpusProvider(),
    ]


# ---------------------------------------------------------------- 图片服务
class ImageService:
    """统一图片服务（Web / MCP / CLI 共用同一实例语义）。

    公开方法（任务书 §6.2 要求的适配层接口）：
        search_attraction_images / search_food_images
        get_image_detail / validate_image_match / get_image_attribution
        capability / enrich_itinerary
    所有公开方法绝不抛异常。
    """

    def __init__(self, providers: Optional[List[ImageProvider]] = None,
                 cache_dir: Optional[str] = None,
                 timeout: Optional[float] = None,
                 max_results: Optional[int] = None,
                 allow_illustrative: Optional[bool] = None) -> None:
        self.providers = providers if providers is not None else _default_providers()
        self.timeout = timeout if timeout is not None else _env_float(ENV_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT)
        self.max_results = max_results if max_results is not None else _env_int(ENV_MAX_RESULTS, DEFAULT_MAX_RESULTS)
        self.allow_illustrative = (allow_illustrative if allow_illustrative is not None
                                   else _env_flag(ENV_ALLOW_ILLUSTRATIVE))
        self.cache_dir = (cache_dir if cache_dir is not None
                          else (os.environ.get(ENV_CACHE_DIR) or "").strip() or None)
        self._mem_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- 缓存
    def _cache_key(self, entity_type: str, query: str) -> str:
        return "%s:%s" % (entity_type, _norm_text(query))

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            hit = self._mem_cache.get(key)
            if hit and now - hit[0] < CACHE_TTL:
                return hit[1]
        if self.cache_dir:
            path = os.path.join(self.cache_dir,
                                hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    if now - payload.get("cached_at", 0) < CACHE_TTL:
                        with self._lock:
                            self._mem_cache[key] = (now, payload)
                        return payload
            except Exception:
                pass  # 缓存损坏不影响主流程
        return None

    def _cache_set(self, key: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._mem_cache[key] = (time.time(), payload)
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                path = os.path.join(self.cache_dir,
                                    hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
            except Exception:
                pass  # 缓存写入失败不影响主流程

    # -------------------------------------------------- 搜索
    def _search_entity(self, entity_type: str, query: str,
                       aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """单实体搜索：按优先级询问各 provider → 校验匹配 → 去重 → 截断。"""
        query = (query or "").strip()
        if not query:
            return {"entity": query, "images": [], "dropped": [],
                    "providers": self._provider_states(),
                    "error": "empty_query"}
        # 合并调用方别名与内置中英文别名（用于匹配校验，不改变 entity 展示名）
        alias_list = list(dict.fromkeys([*(aliases or []),
                                         *ENTITY_ALIASES.get(query, [])]))
        cached = self._cache_get(self._cache_key(entity_type, query))
        if cached is not None:
            cached = dict(cached)
            cached["from_cache"] = True
            return cached

        deadline = time.time() + self.timeout
        results: List[Dict[str, Any]] = []
        provider_notes: List[Dict[str, Any]] = []

        def _ask(p: ImageProvider) -> Tuple[str, Dict[str, Any]]:
            return p.name, p.search(entity_type, query, self.max_results)

        # 并发询问（provider 各自带超时；整体受 deadline 约束）
        try:
            with ThreadPoolExecutor(max_workers=max(1, len(self.providers))) as ex:
                futures = {ex.submit(_ask, p): p for p in self.providers}
                for fut in futures:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        name, res = fut.result(timeout=max(0.1, remaining))
                    except Exception:
                        continue
                    provider_notes.append({"provider": name,
                                           "error": res.get("error"),
                                           "hits": len(res.get("images") or [])})
                    for raw in res.get("images") or []:
                        results.append(raw)
        except Exception:
            pass  # 并发层故障不得影响主流程

        # 匹配校验 + 分级
        images: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        seen_urls = set()
        for raw in results:
            verdict = validate_image_match(query, raw, alias_list)
            conf = verdict["match_confidence"]
            if verdict["matched"]:
                illustrative = False
            elif verdict["is_illustrative"] and self.allow_illustrative:
                illustrative = True
            else:
                dropped.append({"url": (raw.get("image_url") or "")[:120],
                                "reason": verdict["evidence"],
                                "confidence": conf})
                continue
            img = _finalize_image(raw, query, conf, illustrative)
            if not img.get("image_url") or img["image_url"] in seen_urls:
                continue
            seen_urls.add(img["image_url"])
            images.append(img)
            if len(images) >= self.max_results:
                break

        payload = {
            "entity": query,
            "entity_type": entity_type,
            "images": images,
            "dropped": dropped,
            "providers": provider_notes,
            # 有 provider 失败时如实聚合到顶层（不得假装全部成功）
            "error": ("provider_errors:%s" % ",".join(
                n["provider"] for n in provider_notes if n.get("error"))
                if any(n.get("error") for n in provider_notes) else None),
            "retrieved_at": _utc_now(),
        }
        self._cache_set(self._cache_key(entity_type, query), payload)
        return payload

    def search_attraction_images(self, query: str, limit: Optional[int] = None,
                                 aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """搜索景点图片（§6.2 适配层接口）。永不抛异常。"""
        if limit is not None:
            old, self.max_results = self.max_results, limit
            try:
                return self._search_entity("attraction", query, aliases)
            finally:
                self.max_results = old
        return self._search_entity("attraction", query, aliases)

    def search_food_images(self, query: str, limit: Optional[int] = None,
                           aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """搜索美食图片（§6.2 适配层接口）。永不抛异常。"""
        if limit is not None:
            old, self.max_results = self.max_results, limit
            try:
                return self._search_entity("food", query, aliases)
            finally:
                self.max_results = old
        return self._search_entity("food", query, aliases)

    def get_image_detail(self, ref: str) -> Optional[Dict[str, Any]]:
        """按 URL/缓存键取单张图片详情（九字段）。找不到返回 None，不抛异常。"""
        if not ref:
            return None
        key = "detail:%s" % _norm_text(ref)
        hit = self._cache_get(key)
        if hit is not None:
            return hit.get("image")
        # 本地语料图可直接构造详情（内部资料语义）
        local = LocalCorpusProvider()
        for entry in local._load_index().values():
            if entry["ref"] == ref and entry["exists"]:
                img = _finalize_image({
                    "image_url": ref,
                    "source_name": "项目内部语料（%s）" % entry["id"],
                    "license": "内部资料，外部来源待核实",
                    "matched_entity": entry["name"],
                }, entry["name"], 1.0, False)
                self._cache_set(key, {"image": img})
                return img
        return None

    def validate_image_match(self, entity_name: str, candidate: Dict[str, Any],
                             aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """公开的匹配校验（§6.2 适配层接口）。"""
        return validate_image_match(entity_name, candidate, aliases)

    def get_image_attribution(self, image: Dict[str, Any]) -> str:
        """版权行文案：缺失字段如实省略，示意图强制标注（§6.3）。"""
        img = image or {}
        bits = ["图片来源：%s" % (img.get("source_name") or "未标注")]
        if img.get("photographer"):
            bits.append("作者：%s" % img["photographer"])
        if img.get("license"):
            bits.append("许可：%s" % img["license"])
        if img.get("matched_entity"):
            bits.append("匹配实体：%s（置信度 %s）"
                        % (img["matched_entity"], img.get("match_confidence")))
        if img.get("source_page_url"):
            bits.append("原页面：%s" % img["source_page_url"])
        if img.get("is_illustrative"):
            bits.append("示意图，非该实体实拍")
        return "｜".join(bits)

    def capability(self) -> Dict[str, Any]:
        """各 provider 实测状态（供 /api/capability 与轨迹如实展示）。"""
        providers = []
        for p in self.providers:
            st = p.status()
            providers.append({
                "name": p.name,
                "priority_level": p.priority,
                "requires_key": p.requires_key,
                "state": st.get("state"),
                "reason": st.get("reason", ""),
                "key_env": getattr(p, "key_env", ""),
            })
        ready = [p for p in providers if p["state"] == STATUS_READY]
        return {
            "component": "image_service",
            "image_fields_contract": list(IMAGE_FIELDS),
            "source_priority_levels": {
                "1_official_authorized": "未实现（无官方授权渠道）",
                "2_government_public": "未实现（无政府公开素材渠道）",
                "3_open_license": "wikimedia_commons / pexels / unsplash",
                "4_project_corpus": "local_corpus（内部资料，须标注）",
                "5_none": "无图：宁可不展示，不用误导性图片",
            },
            "providers": providers,
            "ready_providers": [p["name"] for p in ready],
            "image_available": bool(ready),
            "match_policy": {
                "threshold": MATCH_THRESHOLD,
                "allow_illustrative": self.allow_illustrative,
                "rule": "匹配置信度不足一律丢弃；示意图仅在严格模式关闭时展示且强制标注",
            },
            "honesty_note": ("provider 状态为实测值：未授权=NEEDS_AUTH、本机不可达="
                             "NETWORK_BLOCKED；不得据此外宣「已接入真实图库」。"),
        }

    # -------------------------------------------------- 行程增强
    def enrich_itinerary(self, itinerary: Dict[str, Any],
                         deadline_s: float = ENRICH_DEADLINE) -> Dict[str, Any]:
        """给行程条目/美食附带真实匹配图片。

        §6.4 铁律：**任何失败都不得破坏行程**——本方法内部全捕获，
        最坏情况原样返回行程并在 image_report 中记录原因。
        无可用 provider 时零网络开销（状态探测按 TTL 缓存）。
        """
        if not isinstance(itinerary, dict):
            return itinerary
        if not itinerary.get("days_plan"):
            return itinerary  # 没有可增强的日程，原样返回（不附加无意义报告）
        try:
            return self._enrich_inner(itinerary, deadline_s)
        except Exception as e:
            out = dict(itinerary)
            out["image_report"] = {
                "attached": 0, "queried": 0,
                "error": "enrich_failed:%s" % type(e).__name__,
                "providers": self._provider_states(),
            }
            return out

    def _enrich_inner(self, itinerary: Dict[str, Any],
                      deadline_s: float) -> Dict[str, Any]:
        # 无可用 provider：直接如实报告，避免无谓网络开销
        states = {p.name: p.status() for p in self.providers}
        if not any(s.get("state") == STATUS_READY for s in states.values()):
            out = dict(itinerary)
            out["image_report"] = {
                "attached": 0, "queried": 0,
                "skipped_reason": "无可用图片来源（详见 providers 实测状态）",
                "providers": [{"name": n, "state": s.get("state"),
                               "reason": s.get("reason", "")}
                              for n, s in states.items()],
            }
            return out

        t0 = time.time()
        attached = 0
        queried = 0
        failures: List[Dict[str, Any]] = []
        out = dict(itinerary)
        new_days = []
        for day in itinerary.get("days_plan") or []:
            new_day = dict(day)
            new_items = []
            for item in day.get("items") or []:
                new_item = dict(item)
                name = item.get("name") or ""
                if name and time.time() - t0 < deadline_s:
                    queried += 1
                    res = self._search_entity("attraction", name)
                    imgs = [i for i in res.get("images") or []
                            if i.get("matched_entity") == name]
                    if imgs:
                        new_item["images"] = imgs
                        attached += len(imgs)
                    elif res.get("error"):
                        failures.append({"entity": name, "error": res["error"]})
                new_items.append(new_item)
            new_day["items"] = new_items
            new_foods = []
            for food in day.get("food_recommend") or []:
                nf = dict(food)
                name = food.get("name") or ""
                if name and time.time() - t0 < deadline_s:
                    queried += 1
                    res = self._search_entity("food", name)
                    imgs = [i for i in res.get("images") or []
                            if i.get("matched_entity") == name]
                    if imgs:
                        nf["images"] = imgs
                        attached += len(imgs)
                    elif res.get("error"):
                        failures.append({"entity": name, "error": res["error"]})
                new_foods.append(nf)
            new_day["food_recommend"] = new_foods
            new_days.append(new_day)
        out["days_plan"] = new_days
        out["image_report"] = {
            "attached": attached,
            "queried": queried,
            "failures": failures[:10],
            "providers": self._provider_states(),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "note": ("图片为真实匹配结果（九字段契约）；未匹配实体无图，"
                     "不影响行程可用性。"),
        }
        return out

    def _provider_states(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.providers:
            st = p.status()
            out.append({"name": p.name, "state": st.get("state"),
                        "reason": st.get("reason", "")})
        return out


# ---------------------------------------------------------------- 单例
_INSTANCE: Optional[ImageService] = None
_INSTANCE_LOCK = threading.Lock()


def get_image_service() -> ImageService:
    """进程级单例（两入口共用；provider 状态探测按 TTL 缓存，开销可控）。"""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = ImageService()
        return _INSTANCE

# -*- coding: utf-8 -*-
"""出行价格与交通数据统一契约层（V33 §A2 / Part A）。

**诚实铁律（与 tencent_maps.py / image_service.py 同构，违反即回退）**：
  1. ``price=None`` + ``status=UNAVAILABLE`` 是**合法且受鼓励**的输出；
     禁止 0 元价、假特价、假班次、假房态。
  2. 同程/艺龙：无官方 Web 商户 API 合同与 Key → ``BLOCKED_MERCHANT_AUTH``。
     Web 不展示同程价格/房态/票务，只提供**官方深链** + 「待核实」；
     绝不抓取同程/携程页面「凑价格」。
  3. ``tc-chengxin`` 是 WorkBuddy **平台连接器**资产（网关契约 + OAuth Token），
     仅平台会话内可用；本模块及 Web/MCP 产品链路**不得复制其 Token**，
     同程能力在 Web 侧如实标注 ``PLATFORM_ONLY``。
  4. 腾讯公交/BRT：**真实接入**（已授权 TENCENT_MAP_KEY 时）。
     票价单位经 ``price_unit=1`` 固定为「分」，归一化时显式换算为元并留痕；
     实时到站时间无授权数据源 → 一律 None + 「待核实」，不得编造。
  5. 任何 provider 失败**绝不外抛**：降级为 UNAVAILABLE + 原因，UI 标「待核实」，
     ``/api/chat`` 不得因此 500。
  6. 缓存：实时价格 5–15 分钟（PRICE_TTL_SECONDS=600）；静态时刻表 24h
     （SCHEDULE_TTL_SECONDS）；HTTP 超时 8s（HTTP_TIMEOUT_SECONDS）。

**入口单一真源**：runtime（Web/MCP/CLI 三入口共用）经 ``get_travel_service()``
取本门面；入口层只做参数解析与序列化，不重复实现业务逻辑。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 统一阈值
PRICE_TTL_SECONDS = 600            # 实时价格缓存 10 分钟（提示词区间 5–15min）
SCHEDULE_TTL_SECONDS = 24 * 3600   # 静态时刻表缓存 24h
HTTP_TIMEOUT_SECONDS = 8.0         # 所有 provider HTTP 超时 8s

# ---------------------------------------------------------------- 状态词表
STATE_LIVE = "LIVE"
STATE_STALE = "STALE"
STATE_STATIC = "STATIC"
STATE_NEEDS_AUTH = "NEEDS_AUTH"
STATE_PLATFORM_ONLY = "PLATFORM_ONLY"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_BLOCKED_MERCHANT_AUTH = "BLOCKED_MERCHANT_AUTH"
STATE_NETWORK_BLOCKED = "NETWORK_BLOCKED"

# ---------------------------------------------------------------- 官方深链
# 只使用可核验的官方根域；不拼接任何搜索页/参数页，避免伪造不存在的深链。
DEEP_LINK_TONGCHENG = "https://www.tongcheng.com/"   # 同程旅行官网
DEEP_LINK_CTRIP = "https://www.ctrip.com/"           # 携程官网
DEEP_LINK_RAILWAY = "https://www.12306.cn/"          # 铁路 12306 官网
TENCENT_TRANSIT_API = "https://apis.map.qq.com/ws/direction/v1/transit/"

# ---------------------------------------------------------------- 数据契约
PRICE_FIELDS = [
    "entity_type", "entity_name", "city", "price", "currency", "unit",
    "original_price", "discount_label", "is_special", "room_type",
    "check_in", "check_out", "cancellation", "tax_included",
    "source", "source_url", "retrieved_at", "expires_at",
    "status", "confidence", "note",
]
TRANSIT_FIELDS = [
    "mode", "line", "origin", "destination", "depart_at", "arrive_at",
    "duration_min", "fare", "first_bus", "last_bus", "interval_min",
    "source", "source_url", "retrieved_at", "status", "note",
]

PRICE_ENTITY_TYPES = ("hotel", "ticket", "transport", "package")
PRICE_UNITS = ("间/晚", "张", "人", "段")
TRANSIT_MODES = ("BRT", "bus", "metro", "train", "flight", "driving")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_price_quote(entity_type: str, entity_name: str, city: str,
                     price: Optional[float] = None, currency: str = "CNY",
                     unit: str = "人", original_price: Optional[float] = None,
                     discount_label: Optional[str] = None, is_special: bool = False,
                     room_type: Optional[str] = None, check_in: Optional[str] = None,
                     check_out: Optional[str] = None, cancellation: Optional[str] = None,
                     tax_included: Optional[bool] = None, source: str = "official",
                     source_url: str = "", status: str = STATE_UNAVAILABLE,
                     confidence: str = "unverified", note: str = "",
                     expires_at: Optional[str] = None) -> Dict[str, Any]:
    """构造 PriceQuote（18+1 字段契约）。

    校验即纪律：price 必须为 None 或**正数**（0/负数一律 None + UNAVAILABLE）；
    is_special=True 必须附带 discount_label（无明确促销信息不得标特价）。
    """
    if entity_type not in PRICE_ENTITY_TYPES:
        raise ValueError("entity_type 必须是 %s 之一" % (PRICE_ENTITY_TYPES,))
    if unit not in PRICE_UNITS:
        raise ValueError("unit 必须是 %s 之一" % (PRICE_UNITS,))
    for name, val in (("price", price), ("original_price", original_price)):
        if val is not None and (not isinstance(val, (int, float)) or val <= 0):
            raise ValueError("%s 必须为 None 或正数；0/负数一律用 None + UNAVAILABLE" % name)
    if is_special and not (discount_label or "").strip():
        raise ValueError("is_special=True 必须附带 discount_label（仅 API 明确促销才可标特价）")
    return {
        "entity_type": entity_type, "entity_name": entity_name, "city": city,
        "price": (float(price) if price is not None else None),
        "currency": currency, "unit": unit,
        "original_price": (float(original_price) if original_price is not None else None),
        "discount_label": discount_label, "is_special": bool(is_special),
        "room_type": room_type, "check_in": check_in, "check_out": check_out,
        "cancellation": cancellation, "tax_included": tax_included,
        "source": source, "source_url": source_url,
        "retrieved_at": _utcnow_iso(), "expires_at": expires_at,
        "status": status, "confidence": confidence, "note": note,
    }


def make_transit_option(mode: str, line: Optional[str], origin: str, destination: str,
                        duration_min: Optional[int] = None, fare: Optional[float] = None,
                        first_bus: Optional[str] = None, last_bus: Optional[str] = None,
                        depart_at: Optional[str] = None, arrive_at: Optional[str] = None,
                        interval_min: Optional[int] = None, source: str = "tencent",
                        source_url: str = TENCENT_TRANSIT_API,
                        status: str = STATE_LIVE, note: str = "") -> Dict[str, Any]:
    """构造 TransitOption（14+1 字段契约）。fare 必须为 None 或正数。"""
    if mode not in TRANSIT_MODES:
        raise ValueError("mode 必须是 %s 之一" % (TRANSIT_MODES,))
    if fare is not None and (not isinstance(fare, (int, float)) or fare <= 0):
        raise ValueError("fare 必须为 None 或正数；0/负数一律用 None")
    return {
        "mode": mode, "line": line, "origin": origin, "destination": destination,
        "depart_at": depart_at, "arrive_at": arrive_at,
        "duration_min": duration_min, "fare": (float(fare) if fare is not None else None),
        "first_bus": first_bus, "last_bus": last_bus, "interval_min": interval_min,
        "source": source, "source_url": source_url,
        "retrieved_at": _utcnow_iso(), "status": status, "note": note,
    }


def unavailable_price(entity_type: str, entity_name: str, city: str,
                      source: str, source_url: str, reason: str,
                      unit: str = "人", **kw) -> Dict[str, Any]:
    """无可靠报价的标准降级输出（price=None + UNAVAILABLE + 待核实）。"""
    return make_price_quote(
        entity_type=entity_type, entity_name=entity_name, city=city, price=None,
        unit=unit, source=source, source_url=source_url, status=STATE_UNAVAILABLE,
        confidence="unverified", note=reason, **kw)


def unavailable_transit(origin: str, destination: str, reason: str,
                        mode: str = "bus") -> Dict[str, Any]:
    """无可靠交通数据的标准降级输出。"""
    return make_transit_option(mode=mode, line=None, origin=origin,
                               destination=destination, status=STATE_UNAVAILABLE,
                               note=reason)


# ---------------------------------------------------------------- TTL 缓存
class _TTLCache:
    """极简 TTL 缓存（线程安全；时钟可注入以便测试锁定行为）。"""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._store: Dict[Any, Any] = {}

    def get(self, key, ttl: float):
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and (self._clock() - hit[0]) < ttl:
                return hit[1]
            return None

    def put(self, key, value) -> None:
        with self._lock:
            self._store[key] = (self._clock(), value)


# ---------------------------------------------------------------- 归一化器
class TransitNormalizer:
    """腾讯 transit 原始响应 → TransitOption 契约。

    单位与语义（依据腾讯位置服务官方文档核实）：
      - ``price``：公交/地铁预估费用，单位「分」；``-1`` 表示缺少票价信息；
        火车(RAIL)单价默认为元，但本链路固定 ``price_unit=1`` 统一为分。
      - ``start_time``/``end_time``：线路**首班车/末班车**时间（非本班次时刻）。
      - ``running_status``：300=正常；301=可能错过末班车；302=首班车还未发出；303=停运。
      - 实时到站时间无任何授权数据源 → ``depart_at``/``arrive_at`` 对 BUS 恒为 None。
    """

    VEHICLE_MODE = {"BUS": "bus", "SUBWAY": "metro", "RAIL": "train"}
    RUNNING_OK = 300
    RUNNING_NOTES = {
        301: "可能错过末班车（运营状态码 301）",
        302: "首班车还未发出（运营状态码 302）",
        303: "线路停运（运营状态码 303）",
    }

    @staticmethod
    def _mode_of(line: Dict[str, Any]) -> str:
        title = (line.get("title") or "")
        if "BRT" in title.upper():
            return "BRT"
        return TransitNormalizer.VEHICLE_MODE.get(line.get("vehicle"), "bus")

    @staticmethod
    def _fare_of(line: Dict[str, Any]) -> Optional[float]:
        """分 → 元 显式换算；-1（缺少票价信息）→ None。"""
        raw = line.get("price")
        if not isinstance(raw, (int, float)) or raw < 0:
            return None
        return round(raw / 100.0, 2)

    @staticmethod
    def from_line(line: Dict[str, Any], origin: str, destination: str,
                  route_distance_m: Optional[int] = None,
                  route_duration_min: Optional[int] = None) -> Dict[str, Any]:
        geton = line.get("geton") or {}
        getoff = line.get("getoff") or {}
        title = line.get("title")
        mode = TransitNormalizer._mode_of(line)
        running = line.get("running_status")
        fare = TransitNormalizer._fare_of(line)
        notes = []
        if running in TransitNormalizer.RUNNING_NOTES:
            notes.append(TransitNormalizer.RUNNING_NOTES[running])
        if fare is None and line.get("price") == -1:
            notes.append("接口未返回票价信息（price=-1），票价待核实")
        if route_distance_m is not None:
            notes.append("全程约 %.1f 公里" % (route_distance_m / 1000.0))
        if route_duration_min is not None:
            notes.append("全程约 %d 分钟（含步行/换乘）" % int(route_duration_min))
        notes.append("实时到站时间无授权数据源（待核实）；坐标为 GCJ-02")
        if mode == "train":
            notes.append("火车班次时刻为线路计划时刻，购票以 12306 为准")
        status = STATE_LIVE if running in (None, TransitNormalizer.RUNNING_OK) else (
            STATE_UNAVAILABLE if running == 303 else STATE_LIVE)
        depart_at = arrive_at = None
        if mode == "train":
            depart_at = line.get("departure_time")
            arrive_at = line.get("arrival_time")
        return make_transit_option(
            mode=mode, line=title,
            origin=(geton.get("title") if isinstance(geton, dict) else None) or origin,
            destination=(getoff.get("title") if isinstance(getoff, dict) else None) or destination,
            duration_min=(int(line["duration"]) if isinstance(line.get("duration"), (int, float)) else None),
            fare=fare, first_bus=line.get("start_time"), last_bus=line.get("end_time"),
            depart_at=depart_at, arrive_at=arrive_at,
            interval_min=None,  # 发车间隔无数据源（running_status 是状态枚举，非间隔）
            source="tencent", source_url=TENCENT_TRANSIT_API, status=status,
            note="；".join(notes))

    @staticmethod
    def from_route(route: Dict[str, Any], origin: str, destination: str) -> List[Dict[str, Any]]:
        """把一个腾讯 transit route（含 steps→lines）收敛为 TransitOption 列表。"""
        options: List[Dict[str, Any]] = []
        dist = route.get("distance") if isinstance(route.get("distance"), (int, float)) else None
        dur = route.get("duration") if isinstance(route.get("duration"), (int, float)) else None
        for step in route.get("steps") or []:
            if step.get("mode") != "TRANSIT":
                continue
            for line in step.get("lines") or []:
                options.append(TransitNormalizer.from_line(
                    line, origin, destination, route_distance_m=dist, route_duration_min=dur))
        if not options:
            options.append(unavailable_transit(
                origin, destination,
                "该方案不含公共交通工具段（可能需步行或驾车）；未生成公交票据信息"))
        return options


# ---------------------------------------------------------------- Providers
class TencentTransitProvider:
    """腾讯公交/BRT/地铁真实接入（V33 §A1：权限内经腾讯位置服务真实调用）。"""

    name = "tencent_transit"

    def __init__(self, adapter_group: Optional[Dict[str, Any]] = None, clock=time.time):
        # adapter_group：测试可注入假适配器组（含 place/route），生产走真实构建
        self._injected = adapter_group
        self._cache = _TTLCache(clock)

    def _group(self) -> Optional[Dict[str, Any]]:
        if self._injected is not None:
            return self._injected
        try:
            import tencent_maps as TM
        except Exception:
            return None
        try:
            ad = TM.TencentMapsAdapter(timeout=HTTP_TIMEOUT_SECONDS)
            return {"adapter": ad, "status": ad.status(),
                    "place": TM.PlaceSearchService(ad),
                    "route": TM.RoutePlanningService(ad)}
        except Exception:
            return None

    def status(self) -> Dict[str, Any]:
        grp = self._group()
        if grp is None:
            return {"component": self.name, "state": STATE_UNAVAILABLE,
                    "reason": "腾讯位置服务适配器未随本次部署打包（tencent_maps 缺失）"}
        st = grp.get("status", {}) or {}
        if st.get("state") == "READY":
            return {"component": self.name, "state": STATE_LIVE, "authorized": True,
                    "provider": "tencent", "coord_system": "GCJ-02",
                    "note": "已授权腾讯位置服务 Key，公交/BRT/地铁路线为真实调用",
                    "source_url": TENCENT_TRANSIT_API,
                    "limits": "实时到站时间无授权数据源（待核实）；火车/航班票价无授权 API"}
        return {"component": self.name, "state": STATE_NEEDS_AUTH, "authorized": False,
                "reason": st.get("reason", "腾讯位置服务未授权"),
                "honesty_note": "未授权时不返回任何伪造公交线路/票价/班次"}

    def _geocode(self, grp: Dict[str, Any], name: str) -> Optional[tuple]:
        try:
            g = grp["place"].geocode(name)
            if g.get("ok"):
                d = g["data"]
                return (d.get("lat"), d.get("lng"))
        except Exception:
            return None
        return None

    def transit_options(self, frm: str, to: str, mode: Optional[str] = None
                        ) -> List[Dict[str, Any]]:
        """公交/BRT/地铁路线查询（真实调用；任何失败降级为 UNAVAILABLE，不外抛）。"""
        cache_key = ("transit", frm, to, mode or "")
        hit = self._cache.get(cache_key, PRICE_TTL_SECONDS)
        if hit is not None:
            return hit
        options = self._query(frm, to, mode)
        self._cache.put(cache_key, options)
        return options

    @staticmethod
    def _dedup(options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 (mode, line, 上车站, 下车站) 去重，保留首选 route 的首次出现。

        腾讯一次返回多条 route，同一线路可能出现在多个 route 中；去重仅消除
        **完全相同的重复项**（不丢弃任何不同的真实选项），route 按偏好排序，
        首次出现即最优方案。
        """
        seen = set()
        out = []
        for o in options:
            key = (o.get("mode"), o.get("line"), o.get("origin"), o.get("destination"))
            if key in seen:
                continue
            seen.add(key)
            out.append(o)
        return out

    def _query(self, frm: str, to: str, mode: Optional[str]) -> List[Dict[str, Any]]:
        try:
            grp = self._group()
            if grp is None:
                return [unavailable_transit(frm, to, "地图适配器未随本次部署打包，无法核验公交路线")]
            st = grp.get("status", {}) or {}
            if st.get("state") != "READY":
                return [unavailable_transit(frm, to, "腾讯位置服务未授权（%s），公交路线待核实"
                                            % st.get("reason", "NEEDS_AUTH"))]
            if mode == "driving":
                return self._driving(grp, frm, to)
            ca, cb = self._geocode(grp, frm), self._geocode(grp, to)
            if not (ca and cb):
                return [unavailable_transit(frm, to, "起点或终点地理编码失败，公交路线待核实")]
            r = grp["route"].transit("%s,%s" % ca, "%s,%s" % cb)
            if not r.get("ok"):
                return [unavailable_transit(frm, to, "腾讯公交路线调用失败：%s"
                                            % str(r.get("error"))[:120])]
            options: List[Dict[str, Any]] = []
            for route in (r.get("data") or {}).get("routes") or []:
                options.extend(TransitNormalizer.from_route(route, frm, to))
            options = self._dedup(options)
            if mode in ("BRT", "bus", "metro", "train"):
                picked = [o for o in options if o.get("mode") == mode and o.get("status") != STATE_UNAVAILABLE]
                if not picked:
                    return [unavailable_transit(
                        frm, to, "未找到 %s 方案（返回的公交方案中无该交通方式）" % mode,
                        mode=mode if mode in TRANSIT_MODES else "bus")]
                options = picked
            return options or [unavailable_transit(frm, to, "腾讯未返回任何公交方案")]
        except Exception as e:  # 兜底：任何异常都不得外抛
            return [unavailable_transit(frm, to, "公交路线查询异常，已诚实降级：%s" % type(e).__name__)]

    def _driving(self, grp: Dict[str, Any], frm: str, to: str) -> List[Dict[str, Any]]:
        ca, cb = self._geocode(grp, frm), self._geocode(grp, to)
        if not (ca and cb):
            return [unavailable_transit(frm, to, "起点或终点地理编码失败，驾车路线待核实", mode="driving")]
        r = grp["route"].route("driving", "%s,%s" % ca, "%s,%s" % cb)
        if not r.get("ok"):
            return [unavailable_transit(frm, to, "腾讯驾车路线调用失败：%s"
                                        % str(r.get("error"))[:120], mode="driving")]
        routes = (r.get("data") or {}).get("routes") or []
        if not routes:
            return [unavailable_transit(frm, to, "腾讯未返回驾车方案", mode="driving")]
        r0 = routes[0]
        dur_s = r0.get("duration_s")
        dist_m = r0.get("distance_m")
        notes = []
        if isinstance(dist_m, (int, float)):
            notes.append("全程约 %.1f 公里" % (dist_m / 1000.0))
        notes.append("驾车路线为真实地图算路；打车费用无授权数据源（待核实）")
        return [make_transit_option(
            mode="driving", line=None, origin=frm, destination=to,
            duration_min=(int(dur_s / 60) if isinstance(dur_s, (int, float)) else None),
            fare=None, source="tencent",
            source_url="https://apis.map.qq.com/ws/direction/v1/driving/",
            status=STATE_LIVE, note="；".join(notes))]


class TongchengPriceProvider:
    """同程/艺龙价格 provider —— 当前授权状态：BLOCKED_MERCHANT_AUTH。

    诚实边界（V33 §A1 决策树第三行 + 红线 1/2）：
      - 无官方 Web 商户 API 合同与 AppKey → **绝不抓取页面、绝不伪造**价格/房态/票务；
      - 只返回 price=None + UNAVAILABLE + 官方深链 + 「待核实」；
      - tc-chengxin 平台连接器资产（OAuth Token / 网关）不得进入本模块。
    """

    name = "tongcheng"

    def status(self) -> Dict[str, Any]:
        return {
            "component": self.name, "state": STATE_BLOCKED_MERCHANT_AUTH,
            "merchant_api": STATE_BLOCKED_MERCHANT_AUTH,
            "platform_connector": STATE_PLATFORM_ONLY,
            "reason": ("同程/艺龙无官方 Web 商户 API 合同与 Key；tc-chengxin 为 WorkBuddy "
                       "平台连接器（仅平台会话内可用，禁止复制 OAuth Token 到 Web）；"
                       "Web 不展示同程价格/房态/票务"),
            "deep_link": DEEP_LINK_TONGCHENG,
            "honesty_note": "禁止抓取同程/携程页面凑价格；禁止把平台连接器说成 Web 已接入。",
        }

    def hotels(self, city: str, check_in: Optional[str] = None,
               check_out: Optional[str] = None, keyword: Optional[str] = None,
               max_price: Optional[float] = None) -> List[Dict[str, Any]]:
        name = (keyword or "").strip() or ("%s酒店" % city)
        note = ("无官方商户 API 授权（BLOCKED_MERCHANT_AUTH）；房价/房态/取消政策以同程旅行"
                "官网核验为准，本服务不展示任何未经核验的价格")
        return [unavailable_price(
            "hotel", name, city, source="tongcheng", source_url=DEEP_LINK_TONGCHENG,
            reason=note, unit="间/晚", check_in=check_in, check_out=check_out)]

    def tickets(self, city: str, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        name = (keyword or "").strip() or ("%s景区门票" % city)
        note = ("无授权门票价格数据源；景区票价/优惠政策以景区官方渠道核验为准"
                "（预订平台深链仅供参考，不构成报价）")
        return [unavailable_price(
            "ticket", name, city, source="tongcheng", source_url=DEEP_LINK_TONGCHENG,
            reason=note, unit="张")]


# ---------------------------------------------------------------- 统一门面
class TravelDataService:
    """出行数据门面：runtime（Web/MCP/CLI 三入口）共用的单一真源。

    所有公开方法**绝不外抛**：内部异常一律降级为 UNAVAILABLE + 原因。
    """

    def __init__(self, transit_provider: Optional[TencentTransitProvider] = None,
                 price_provider: Optional[TongchengPriceProvider] = None):
        self.transit = transit_provider or TencentTransitProvider()
        self.prices = price_provider or TongchengPriceProvider()

    def capability_matrix(self) -> Dict[str, Any]:
        """能力矩阵（GET /api/travel/status；不发起任何网络调用）。"""
        try:
            transit_st = self.transit.status()
        except Exception as e:
            transit_st = {"component": "tencent_transit", "state": STATE_UNAVAILABLE,
                          "reason": "适配器异常：%s" % type(e).__name__}
        try:
            tongcheng_st = self.prices.status()
        except Exception as e:
            tongcheng_st = {"component": "tongcheng", "state": STATE_BLOCKED_MERCHANT_AUTH,
                            "reason": "provider 异常：%s" % type(e).__name__}
        return {
            "component": "travel_prices",
            "generated_at": _utcnow_iso(),
            "states_vocabulary": [STATE_LIVE, STATE_NEEDS_AUTH, STATE_PLATFORM_ONLY,
                                  STATE_UNAVAILABLE, STATE_BLOCKED_MERCHANT_AUTH],
            "tongcheng": tongcheng_st,
            "tencent_transit": transit_st,
            "hotel_prices": {
                "state": STATE_UNAVAILABLE,
                "reason": "无授权酒店价格数据源（同程商户 API 未授权，BLOCKED_MERCHANT_AUTH）",
                "deep_link": DEEP_LINK_TONGCHENG,
                "honesty_note": "房价一律「待核实」，禁止 0 元/假特价"},
            "ticket_prices": {
                "state": STATE_UNAVAILABLE,
                "reason": "无授权门票价格数据源；景区票价以官方渠道核验",
                "deep_link": DEEP_LINK_TONGCHENG},
            "train_flight_prices": {
                "state": STATE_UNAVAILABLE,
                "reason": "12306/航司无授权 API；火车票以 12306 官网核验",
                "deep_link": DEEP_LINK_RAILWAY},
            "food_consumption": {
                "state": STATE_UNAVAILABLE,
                "reason": "餐饮/消费无授权实时价格源",
                "deep_link": None},
            "realtime_arrival": {
                "state": STATE_UNAVAILABLE,
                "reason": "公交实时到站时间无授权数据源，一律标注「待核实」",
                "deep_link": None},
            "honesty_note": ("价格/班次必须带 source/retrieved_at/status；无可靠来源时 "
                             "price=None + UNAVAILABLE；任何 provider 失败不得导致 "
                             "/api/chat 500。"),
        }

    def price_quotes(self, city: str = "银川", ptype: str = "hotel",
                     check_in: Optional[str] = None, check_out: Optional[str] = None,
                     keyword: Optional[str] = None,
                     max_price: Optional[float] = None) -> List[Dict[str, Any]]:
        """价格查询（GET /api/travel/prices）。当前授权下酒店/门票均诚实降级。"""
        try:
            if ptype == "ticket":
                return self.prices.tickets(city, keyword=keyword)
            return self.prices.hotels(city, check_in=check_in, check_out=check_out,
                                      keyword=keyword, max_price=max_price)
        except Exception as e:
            return [unavailable_price(
                "hotel" if ptype != "ticket" else "ticket",
                (keyword or "").strip() or ("%s%s" % (city, "酒店" if ptype != "ticket" else "门票")),
                city, source="tongcheng", source_url=DEEP_LINK_TONGCHENG,
                reason="价格查询异常，已诚实降级：%s" % type(e).__name__)]

    def hotel_quotes(self, city: str = "银川", check_in: Optional[str] = None,
                     check_out: Optional[str] = None, keyword: Optional[str] = None,
                     max_price: Optional[float] = None) -> List[Dict[str, Any]]:
        return self.price_quotes(city=city, ptype="hotel", check_in=check_in,
                                 check_out=check_out, keyword=keyword, max_price=max_price)

    def transit_options(self, frm: str = "银川站", to: str = "西夏陵",
                        mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """交通查询（GET /api/travel/transit）。腾讯公交真实接入；失败诚实降级。"""
        try:
            return self.transit.transit_options(frm, to, mode=mode)
        except Exception as e:
            return [unavailable_transit(frm, to, "交通查询异常，已诚实降级：%s" % type(e).__name__)]


_SERVICE: Optional[TravelDataService] = None
_SERVICE_LOCK = threading.Lock()


def get_travel_service() -> TravelDataService:
    """门面单例（与 image_service.get_image_service 同构；三入口共用同一实例语义）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = TravelDataService()
        return _SERVICE

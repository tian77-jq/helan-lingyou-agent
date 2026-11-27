# -*- coding: utf-8 -*-
"""腾讯位置服务适配器（V32.2 §三）。

**授权与密钥纪律**
    - 服务端 Key 仅从环境变量 ``TENCENT_MAP_KEY`` 读取，绝不写入源码 / Git / 前端 bundle / 聊天记录。
    - 无 Key → 所有方法诚实返回 ``NEEDS_AUTH``，不产生任何伪造的 POI / 路线 / 距离数据。

**坐标系**
    - 腾讯位置服务返回坐标为 **GCJ-02**（国测局加密坐标系）；天气接口（open-meteo）使用 **WGS84**。
    - 二者**不可直接混用**：涉及经纬度交叉使用时必须先做 GCJ-02 ↔ WGS84 换算，本模块在
      ``TravelDataNormalizer`` 中显式标注每个坐标的坐标系，避免静默错位。

**能力边界（诚实声明）**
    - 本适配器只负责「真实调用腾讯 WebService API 并结构化返回」；是否对评委/用户可用，取决于
      运行环境是否配置了合法 Key 与配额。未配置时明确 NEEDS_AUTH，绝不以占位数据冒充实时结果。
"""
from __future__ import annotations

import os
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

KEY_ENV = "TENCENT_MAP_KEY"
WS_BASE = "https://apis.map.qq.com/ws"
STATUS_READY = "READY"
STATUS_NEEDS_AUTH = "NEEDS_AUTH"
STATUS_HTTP_ERR = "HTTP_ERROR"
STATUS_NETWORK_BLOCKED = "NETWORK_BLOCKED"


def _ua() -> Dict[str, str]:
    return {"User-Agent": "HelanLingyou/1.0 (Ningxia travel agent; +https://example.com)"}


class TencentMapsAdapter:
    """腾讯位置服务统一适配层（持有 Key、统一发起请求、统一错误归类）。"""

    def __init__(self, key: Optional[str] = None, timeout: float = 10.0):
        self.key = (key or os.environ.get(KEY_ENV, "")).strip()
        self.timeout = timeout

    # ---- 状态（如实上报）----
    def status(self) -> Dict[str, Any]:
        if not self.key:
            return {
                "state": STATUS_NEEDS_AUTH,
                "reason": ("未配置 %s（腾讯位置服务控制台申请服务端 Key："
                           "https://lbs.qq.com/dev/console/key/manage；"
                           "密钥仅存于环境变量，禁止写入源码/Git/前端）" % KEY_ENV),
                "coord_system": "GCJ-02",
            }
        return {"state": STATUS_READY, "reason": "Key 已配置（服务端环境变量）",
                "authorized": True, "coord_system": "GCJ-02"}

    # ---- 底层 GET（统一错误归类，绝不外抛）----
    def _get(self, path: str, params: Dict[str, Any]
             ) -> (Optional[Dict[str, Any]], Optional[str]):
        p = {k: v for k, v in params.items() if v is not None}
        p["key"] = self.key
        p["output"] = "json"
        url = WS_BASE + path + "?" + urllib.parse.urlencode(p)
        try:
            req = urllib.request.Request(url, headers=_ua())
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8", "replace"))
            # 腾讯业务码：0=成功，非0=业务失败（如 110=Key 无效/配额）
            if isinstance(data, dict) and data.get("status") != 0:
                return data, "biz_%s:%s" % (data.get("status"), data.get("message"))
            return data, None
        except urllib.error.HTTPError as e:
            return None, "http_%d" % e.code
        except Exception as e:  # 超时/DNS 等
            return None, "network_error:%s" % type(e).__name__

    # ---- 结构化返回包装 ----
    @staticmethod
    def _wrap(ok: bool, data, error=None, provider="tencent", coord="GCJ-02"):
        return {"ok": ok, "provider": provider, "coord_system": coord,
                "data": data if ok else None, "error": None if ok else error}


class PlaceSearchService:
    """地点 POI 搜索与详情（§三：景点 POI / 地址与坐标解析）。"""

    def __init__(self, adapter: TencentMapsAdapter):
        self.ad = adapter

    def search(self, city: str, keyword: str, page_size: int = 10) -> Dict[str, Any]:
        st = self.ad.status()
        if st["state"] != STATUS_READY:
            return self.ad._wrap(False, None, st["reason"])
        data, err = self.ad._get("/place/v1/search", {
            "boundary": "region(%s,0)" % city, "keyword": keyword, "page_size": page_size})
        if err:
            return self.ad._wrap(False, None, err)
        pois = []
        for p in (data.get("data") or []):
            loc = p.get("location") or {}
            pois.append({
                "id": p.get("id"), "title": p.get("title"), "address": p.get("address"),
                "city": p.get("ad_info", {}).get("city"),
                "lat": loc.get("lat"), "lng": loc.get("lng"),
                "coord_system": "GCJ-02",
                "category": p.get("category"),
            })
        return self.ad._wrap(True, {"count": len(pois), "pois": pois})

    def geocode(self, address: str) -> Dict[str, Any]:
        """地址 -> 坐标（GCJ-02）。"""
        st = self.ad.status()
        if st["state"] != STATUS_READY:
            return self.ad._wrap(False, None, st["reason"])
        data, err = self.ad._get("/geocoder/v1/", {"address": address})
        if err:
            return self.ad._wrap(False, None, err)
        loc = (data.get("result") or {}).get("location") or {}
        return self.ad._wrap(True, {
            "address": address, "lat": loc.get("lat"), "lng": loc.get("lng"),
            "coord_system": "GCJ-02",
            "reliability": (data.get("result") or {}).get("reliability"),
        })


class RoutePlanningService:
    """驾车 / 步行 / 公交路线（§三：真实道路耗时；无 Key 时诚实 NEEDS_AUTH）。"""

    _MODE = {"driving": "driving", "walking": "walking", "transit": "transit"}

    def __init__(self, adapter: TencentMapsAdapter):
        self.ad = adapter

    def route(self, mode: str, frm: str, to: str) -> Dict[str, Any]:
        st = self.ad.status()
        if st["state"] != STATUS_READY:
            return self.ad._wrap(False, None, st["reason"])
        if mode not in self._MODE:
            return self.ad._wrap(False, None, "unsupported_mode:%s" % mode)
        data, err = self.ad._get("/direction/v1/%s/" % self._MODE[mode], {
            "from": frm, "to": to})
        if err:
            return self.ad._wrap(False, None, err)
        routes = (data.get("result") or {}).get("routes") or []
        out = []
        for r in routes[:3]:
            # 腾讯驾车接口：distance=米；duration=**分钟**（数值），无 duration_value 字段。
            raw_dur = r.get("duration")
            dur_val = r.get("duration_value")
            if dur_val is None and isinstance(raw_dur, (int, float)):
                dur_val = int(raw_dur * 60)  # 分钟 -> 秒
            if isinstance(raw_dur, (int, float)):
                dur_text = "%d分钟" % int(raw_dur)
            else:
                dur_text = str(raw_dur or "")
            out.append({
                "mode": mode, "distance_m": r.get("distance"),
                "duration_text": dur_text, "duration_s": dur_val,
                "coord_system": "GCJ-02",
            })
        return self.ad._wrap(True, {"count": len(out), "routes": out})


    def transit(self, frm: str, to: str, policy: Optional[str] = None) -> Dict[str, Any]:
        """公交/地铁/火车换乘方案（**保留线路明细**，供 travel_prices 归一化）。

        与 ``route("transit", ...)`` 的区别：本方法返回原始 routes 结构
        （distance/duration/price/steps→lines→title/geton/getoff/station_count/
        start_time/end_time/running_status），业务解释（分→元换算、BRT 识别、
        运营状态映射）由 ``travel_prices.TransitNormalizer`` 负责；本模块只做
        真实调用与统一错误归类，绝不外抛。

        ``price_unit=1``：票价单位显式固定为「分」。腾讯文档：默认无火车(RAIL)
        方案时票价为分、含火车方案时为元——混用会导致静默错价，故显式统一。
        """
        st = self.ad.status()
        if st["state"] != STATUS_READY:
            return self.ad._wrap(False, None, st["reason"])
        params = {"from": frm, "to": to, "price_unit": 1}
        if policy:
            params["policy"] = policy
        data, err = self.ad._get("/direction/v1/transit/", params)
        if err:
            return self.ad._wrap(False, None, err)
        routes = (data.get("result") or {}).get("routes") or []
        return self.ad._wrap(True, {"count": len(routes), "routes": routes})


class DistanceMatrixService:
    """距离矩阵与景点排序（§三）。"""

    def __init__(self, adapter: TencentMapsAdapter):
        self.ad = adapter

    def matrix(self, origins: List[str], destinations: List[str], mode: str = "driving"
               ) -> Dict[str, Any]:
        st = self.ad.status()
        if st["state"] != STATUS_READY:
            return self.ad._wrap(False, None, st["reason"])
        if mode not in ("driving", "walking", "transit"):
            return self.ad._wrap(False, None, "unsupported_mode:%s" % mode)
        data, err = self.ad._get("/distance/v1/matrix/", {
            "mode": mode, "from": ";".join(origins), "to": ";".join(destinations)})
        if err:
            return self.ad._wrap(False, None, err)
        rows = (data.get("result") or {}).get("rows") or []
        return self.ad._wrap(True, {"mode": mode, "rows": rows, "coord_system": "GCJ-02"})


class RestaurantSearchService:
    """餐厅 POI 搜索（§三：景点附近餐厅）。"""

    def __init__(self, adapter: TencentMapsAdapter):
        self.ad = adapter

    def nearby(self, city: str, keyword: str = "餐厅", page_size: int = 10) -> Dict[str, Any]:
        st = self.ad.status()
        if st["state"] != STATUS_READY:
            return self.ad._wrap(False, None, st["reason"])
        data, err = self.ad._get("/place/v1/search", {
            "boundary": "region(%s,0)" % city, "keyword": keyword, "page_size": page_size})
        if err:
            return self.ad._wrap(False, None, err)
        out = [{"title": p.get("title"), "address": p.get("address"),
                "category": p.get("category"),
                "lat": (p.get("location") or {}).get("lat"),
                "lng": (p.get("location") or {}).get("lng"),
                "coord_system": "GCJ-02"} for p in (data.get("data") or [])]
        return self.ad._wrap(True, {"count": len(out), "restaurants": out})


class TravelDataNormalizer:
    """跨服务数据归一化（§三）：显式标注坐标系，避免 GCJ-02/WGS84 静默混用。"""

    @staticmethod
    def poi(poi: Dict[str, Any]) -> Dict[str, Any]:
        return {"name": poi.get("title"), "address": poi.get("address"),
                "lat": poi.get("lat"), "lng": poi.get("lng"),
                "coord_system": poi.get("coord_system", "GCJ-02")}

    @staticmethod
    def route(r: Dict[str, Any]) -> Dict[str, Any]:
        return {"mode": r.get("mode"), "distance_m": r.get("distance_m"),
                "duration_text": r.get("duration_text"), "duration_s": r.get("duration_s"),
                "coord_system": r.get("coord_system", "GCJ-02")}

    @staticmethod
    def note_coord_mismatch() -> str:
        return ("坐标含 GCJ-02（腾讯）与 WGS84（open-meteo 天气）两套坐标系，"
                "跨服务使用时必须先换算，禁止直接混用经纬度。")


def build_adapter(key: Optional[str] = None) -> Dict[str, Any]:
    """构建完整适配器组（供 A4 / Web / MCP 共用同一实例语义）。"""
    ad = TencentMapsAdapter(key)
    return {
        "adapter": ad,
        "status": ad.status(),
        "place": PlaceSearchService(ad),
        "route": RoutePlanningService(ad),
        "distance": DistanceMatrixService(ad),
        "restaurant": RestaurantSearchService(ad),
        "normalizer": TravelDataNormalizer(),
    }

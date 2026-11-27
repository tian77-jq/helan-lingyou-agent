# -*- coding: utf-8 -*-
"""A4 数据服务专家（沙晴川）临时探针：真实查询 2026-10-16/17 三城日级预报。
仅调用 open-meteo 公共 API（无需密钥），逐城区分「地理编码失败」与「预报接口失败」。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

# 城市 -> 候选名（中文名不带「市」、拼音兜底）。多同名地点按 admin1=宁夏 消歧。
CITIES = [
    ("银川市", ["银川", "Yinchuan"]),
    ("中卫市", ["中卫", "Zhongwei"]),
    ("石嘴山市", ["石嘴山", "Shizuishan"]),
]
PROVINCE = "宁夏"
START, END = "2026-10-16", "2026-10-17"
TZ = "Asia/Shanghai"
DAILY = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,windspeed_10m_max,winddirection_10m_dominant"

WMO = {0: "晴", 1: "基本晴", 2: "部分多云", 3: "阴", 45: "雾", 48: "雾凇",
       51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
       61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
       71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
       80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
       95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹"}


def geocode(name):
    """返回 (results, url, tried_names)。逐候选名尝试，按 admin1=宁夏 消歧。"""
    tried = []
    for cand in name:
        url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": cand, "count": 10, "language": "zh", "format": "json"})
        tried.append({"name": cand, "url": url})
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.load(r)
        results = data.get("results") or []
        matched = [x for x in results if PROVINCE in str(x.get("admin1") or "")]
        if matched:
            return matched, url, tried, cand
    return [], (tried[-1]["url"] if tried else ""), tried, None


def forecast(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {"latitude": lat, "longitude": lon, "daily": DAILY,
         "timezone": TZ, "start_date": START, "end_date": END})
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r), url


def main():
    evidence = []
    out = {}
    for city, cands in CITIES:
        rec = {"city": city, "ok": False}
        try:
            res, geo_url, tried, hit = geocode(cands)
            rec["geocode_url_raw"] = [t["url"] for t in tried]
            rec["resolved_via"] = hit
            if not res:
                rec["error"] = "geocoding no result (宁夏内无匹配)"
                evidence.append(f"WEATHER_API_FAIL:{city}:geocoding no result")
                out[city] = rec
                continue
            g0 = res[0]
            lat, lon = g0.get("latitude"), g0.get("longitude")
            rec["resolved_name"] = g0.get("name")
            rec["admin1"] = g0.get("admin1")
            rec["country"] = g0.get("country")
            rec["latitude"], rec["longitude"] = lat, lon
            if lat is None or lon is None:
                rec["error"] = "geocoding missing lat/lon"
                evidence.append(f"WEATHER_API_FAIL:{city}:geocoding missing lat/lon")
                out[city] = rec
                continue
            wx, wx_url = forecast(lat, lon)
            rec["forecast_url"] = wx_url
            d = wx.get("daily") or {}
            dates = d.get("time") or []
            if not dates:
                rec["error"] = "daily forecast empty"
                evidence.append(f"WEATHER_API_FAIL:{city}:daily forecast empty")
                out[city] = rec
                continue
            days = []
            for i, dt in enumerate(dates):
                code = (d.get("weather_code") or [None])[i]
                tmax = (d.get("temperature_2m_max") or [None])[i]
                tmin = (d.get("temperature_2m_min") or [None])[i]
                pop = (d.get("precipitation_probability_max") or [None])[i]
                wmax = (d.get("windspeed_10m_max") or [None])[i]
                days.append({"date": dt, "weather_code": code,
                             "weather": WMO.get(code, f"码{code}"),
                             "tmax": tmax, "tmin": tmin, "pop": pop, "wind_max": wmax})
                evidence.append(
                    f"WEATHER_API_OK:{city}:{dt}:{WMO.get(code, code)}:"
                    f"{tmin}~{tmax}C:POP{pop}%:风{wmax}km/h")
            # HTTP 200 != 业务成功：校验日期覆盖与关键字段是否有效
            rec["daily_raw"] = d
            rec["dates_returned"] = dates
            rec["date_coverage_ok"] = sorted(dates) == [START, END]
            rec["fields_valid"] = all(
                x["tmax"] is not None and x["tmin"] is not None and x["weather_code"] is not None
                for x in days)
            rec["ok"] = rec["date_coverage_ok"] and rec["fields_valid"]
            rec["days"] = days
            rec["units"] = wx.get("daily_units")
            rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if not rec["ok"]:
                rec["error"] = f"field/date validation failed (coverage={rec['date_coverage_ok']}, fields={rec['fields_valid']})"
                evidence.append(f"WEATHER_API_FAIL:{city}:{rec['error']}")
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            evidence.append(f"WEATHER_API_FAIL:{city}:{rec['error'][:80]}")
        out[city] = rec
    print(json.dumps({"results": out, "tool_evidence": evidence}, ensure_ascii=False, indent=2))
    with open(sys.argv[1] if len(sys.argv) > 1 else "weather_probe_out.json", "w", encoding="utf-8") as f:
        json.dump({"results": out, "tool_evidence": evidence}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

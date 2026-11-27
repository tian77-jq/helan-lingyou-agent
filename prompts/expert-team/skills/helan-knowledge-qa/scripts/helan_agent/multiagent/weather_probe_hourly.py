# -*- coding: utf-8 -*-
"""A4 逐小时天气探针（沙晴川）：真实调用 open-meteo /v1/forecast hourly，落盘原始 JSON + tool_evidence。

可复现命令（在 domain/helan_agent/multiagent 下执行）：
  python weather_probe_hourly.py \
      --city 银川 --lat 38.46806 --lon 106.27306 \
      --start 2026-10-16 --end 2026-10-17 \
      --out ../../../evidence/weather_hourly_yinchuan_2026-10-16_17.json

输出 JSON 含：request_url / request_params / timezone / fetched_at / hours[] / tool_evidence[]。
失败时 ok=false 并记录 error，不伪造。
"""
import argparse
import json
import time
import urllib.parse
import urllib.request

HOURLY = "temperature_2m,precipitation_probability,weather_code,windspeed_10m"
WMO = {0: "晴", 1: "基本晴", 2: "部分多云", 3: "阴", 45: "雾", 48: "雾凇",
       51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
       61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
       71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
       80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
       95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="银川")
    ap.add_argument("--lat", type=float, default=38.46806)
    ap.add_argument("--lon", type=float, default=106.27306)
    ap.add_argument("--start", default="2026-10-16")
    ap.add_argument("--end", default="2026-10-17")
    ap.add_argument("--tz", default="Asia/Shanghai")
    ap.add_argument("--out", default="weather_hourly_out.json")
    a = ap.parse_args()

    params = {"latitude": a.lat, "longitude": a.lon, "hourly": HOURLY,
              "timezone": a.tz, "start_date": a.start, "end_date": a.end}
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    rec = {"city": a.city, "source": "open-meteo.com /v1/forecast (hourly, real-time API)",
           "request_url": url, "request_params": params, "fetched_at": fetched_at,
           "ok": False}
    evidence = []
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
        h = data.get("hourly") or {}
        times = h.get("time") or []
        if not times:
            rec["error"] = "hourly empty"
            evidence.append(f"HOURLY_API_FAIL:{a.city}:hourly empty")
        else:
            temp = h.get("temperature_2m") or []
            pop = h.get("precipitation_probability") or []
            code = h.get("weather_code") or []
            wind = h.get("windspeed_10m") or []
            rows = []
            for i, t in enumerate(times):
                c = code[i] if i < len(code) else None
                rows.append({
                    "time": t,
                    "temperature_c": temp[i] if i < len(temp) else None,
                    "precipitation_probability": pop[i] if i < len(pop) else None,
                    "weather_code": c,
                    "weather": WMO.get(c, (f"码{c}" if c is not None else None)),
                    "windspeed_kmh": wind[i] if i < len(wind) else None,
                })
                evidence.append(
                    "HOURLY_API_OK:%s:%s:%.1fC:POP%s%%:%s:风%.1fkm/h" % (
                        a.city, t, rows[-1]["temperature_c"],
                        rows[-1]["precipitation_probability"], rows[-1]["weather"],
                        rows[-1]["windspeed_kmh"]))
            rec["hours"] = rows
            rec["hours_count"] = len(rows)
            rec["units"] = data.get("hourly_units")
            rec["timezone"] = data.get("timezone")
            # 业务校验：HTTP 200 != 成功，须逐时字段非空
            rec["fields_valid"] = all(
                x["temperature_c"] is not None and x["precipitation_probability"] is not None
                for x in rows)
            rec["ok"] = rec["fields_valid"]
            if not rec["ok"]:
                rec["error"] = "some hourly fields are null"
                evidence.append(f"HOURLY_API_FAIL:{a.city}:some hourly fields are null")
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        evidence.append(f"HOURLY_API_FAIL:{a.city}:{rec['error'][:80]}")

    rec["tool_evidence"] = evidence
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(json.dumps({"ok": rec["ok"], "city": a.city, "hours_count": rec.get("hours_count"),
                      "fetched_at": fetched_at, "out": a.out,
                      "evidence_count": len(evidence)}, ensure_ascii=False, indent=2))
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

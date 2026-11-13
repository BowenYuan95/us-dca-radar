#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AUD/USD 汇率：获取、缓存、过期判定。

原则：
- 只展示估算时 firm=False；过期/回退汇率不得当作确定下单金额。
- 绝不静默按 1:1 处理。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Callable, Iterable

from nyse_calendar import ET_TZ  # 本地 scripts/nyse_calendar.py

DEFAULT_SYMBOL = "AUDUSD=X"
RATE_MIN = 0.30
RATE_MAX = 1.20


@dataclass
class FxRate:
    rate: float | None
    as_of_et: str | None
    source: str
    age_hours: float | None
    status: str          # fresh | cached | stale | fallback | missing
    firm: bool
    note: str = ""

    def to_dict(self):
        return asdict(self)


def _cache_path(base):
    return os.path.join(base, "data", "cache", "fx_AUDUSD.json")


def _read_cache(base):
    p = _cache_path(base)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _write_cache(base, obj):
    p = _cache_path(base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


def _valid_rate(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if RATE_MIN <= v <= RATE_MAX else None


def _parse_live(chart_json):
    res = chart_json["chart"]["result"][0]
    meta = res.get("meta") or {}
    rate = meta.get("regularMarketPrice")
    if rate is None:
        q = (res.get("indicators") or {}).get("quote") or [{}]
        closes = q[0].get("close") or []
        for c in reversed(closes):
            if c:
                rate = c
                break
    ts = meta.get("regularMarketTime")
    as_of = None
    if ts:
        from datetime import timezone
        as_of = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(ET_TZ).isoformat()
    return _valid_rate(rate), as_of, meta.get("currency")


def fetch_fx(cfg, yahoo_json: Callable | None = None, base: str | None = None,
             now: float | None = None) -> FxRate:
    """返回当前 FX。yahoo_json 由 scan 注入以避免循环导入；测试可注入 fake。"""
    base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    now = float(now if now is not None else time.time())
    fx_cfg = (cfg or {}).get("fx") or {}
    ttl_h = float(fx_cfg.get("ttl_hours") or 6)
    max_stale_h = float(fx_cfg.get("max_stale_hours") or 72)
    fallback = _valid_rate(fx_cfg.get("fallback_rate"))
    symbol = fx_cfg.get("symbol") or DEFAULT_SYMBOL
    cache = _read_cache(base) or {}

    # 1) 实时
    if yahoo_json is not None:
        try:
            import urllib.parse
            path = (f"/v8/finance/chart/{urllib.parse.quote(symbol)}"
                    f"?range=1d&interval=5m&includePrePost=false")
            rate, as_of, currency = _parse_live(yahoo_json(path))
            if rate is None:
                raise ValueError("实时 FX 无有效价格")
            if currency and currency.upper() != "USD":
                # AUDUSD=X 的 quote currency 通常是 USD；异常时拒绝
                raise ValueError(f"FX 报价币种异常：{currency}")
            obj = {"rate": rate, "as_of_et": as_of, "fetched_at": now,
                   "source": "yahoo", "symbol": symbol}
            _write_cache(base, obj)
            return FxRate(rate=rate, as_of_et=as_of, source="yahoo",
                          age_hours=0.0, status="fresh", firm=True,
                          note=f"1 AUD = {rate:.4f} USD")
        except Exception as exc:  # noqa: BLE001
            note_live = f"实时 FX 失败：{exc}"
    else:
        note_live = "未提供实时 FX 获取器"

    # 2) 缓存
    rate = _valid_rate(cache.get("rate"))
    if rate is not None and cache.get("fetched_at"):
        age_h = max(0.0, (now - float(cache["fetched_at"])) / 3600.0)
        if age_h <= ttl_h:
            return FxRate(rate=rate, as_of_et=cache.get("as_of_et"),
                          source=cache.get("source") or "cache", age_hours=age_h,
                          status="cached", firm=True,
                          note=f"1 AUD = {rate:.4f} USD（缓存 {age_h:.1f}h）")
        if age_h <= max_stale_h:
            return FxRate(rate=rate, as_of_et=cache.get("as_of_et"),
                          source=cache.get("source") or "cache", age_hours=age_h,
                          status="stale", firm=False,
                          note=f"汇率已过期 {age_h:.1f}h，仅供估算；确认成交需实际汇率")

    # 3) 回退
    if fallback is not None:
        return FxRate(rate=fallback, as_of_et=None, source="fallback",
                      age_hours=None, status="fallback", firm=False,
                      note=f"使用 fallback 汇率 {fallback:.4f}，仅供估算；{note_live}")

    return FxRate(rate=None, as_of_et=None, source="missing", age_hours=None,
                  status="missing", firm=False,
                  note=f"无可用的 AUD/USD 汇率；{note_live}")


def fetch_fx_history(cfg, yahoo_json: Callable, base: str | None = None) -> dict:
    """历史日频 FX（date -> rate）。用于 AUD 口径回测；失败返回空。"""
    base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    symbol = ((cfg or {}).get("fx") or {}).get("symbol") or DEFAULT_SYMBOL
    cache_p = os.path.join(base, "data", "cache", "fx_AUDUSD_history.json")
    try:
        with open(cache_p, encoding="utf-8") as f:
            obj = json.load(f)
        return {date.fromisoformat(k): float(v) for k, v in obj.items() if _valid_rate(v)}
    except Exception:  # noqa: BLE001
        pass
    try:
        import urllib.parse
        path = (f"/v8/finance/chart/{urllib.parse.quote(symbol)}"
                f"?range=10y&interval=1d&includePrePost=false")
        data = yahoo_json(path)
        res = data["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = (res.get("indicators") or {}).get("quote") or [{}]
        closes = q[0].get("close") or []
        out = {}
        for i, t in enumerate(ts):
            if i >= len(closes) or not closes[i]:
                continue
            d = datetime.fromtimestamp(t, tz=ET_TZ).date()
            r = _valid_rate(closes[i])
            if r:
                out[d] = r
        if len(out) < 60:
            return {}
        os.makedirs(os.path.dirname(cache_p), exist_ok=True)
        with open(cache_p, "w", encoding="utf-8") as f:
            json.dump({k.isoformat(): v for k, v in sorted(out.items())}, f)
        return out
    except Exception:  # noqa: BLE001
        return {}


def rate_on(series: dict, d: date):
    """<= d 的最近一个汇率；返回 (rate, as_of_date) 或 (None, None)。"""
    if not series:
        return None, None
    candidates = [k for k in series if k <= d]
    if not candidates:
        return None, None
    k = max(candidates)
    return series[k], k


def convert_aud_to_usd(aud, rate):
    if aud is None or rate is None:
        return None
    return float(aud) * float(rate)


def convert_usd_to_aud(usd, rate):
    if usd is None or not rate:
        return None
    return float(usd) / float(rate)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股定投雷达 (US DCA Radar) v2
==============================
每个交易日美股「开盘 / 午盘 / 收盘前」扫描 VOO、SMH、TSLA，
回答同一个问题：**本月是否应该在这一天定投？**

组合规则（可在 config.json 调整）
- 目标权重：VOO 75% / SMH 20% / TSLA 5%
- 每月必然完成一次定投，择时只决定「月内哪一天」，不因短期波动停止
- 核心 VOO 在月初前 4 个交易日内完成；SMH/TSLA 等更好位置，月末兜底

用法
----
  python3 scripts/scan.py                       # 自动判断当前会话
  python3 scripts/scan.py --session close       # 指定 open|midday|close
  python3 scripts/scan.py --digest              # 收盘日报（墨尔本早上推送用）
  python3 scripts/scan.py --now-et "2026-11-13 12:30"   # 回放测试
  python3 scripts/scan.py --confirm VOO,SMH --amount 1000   # 记录已执行
  python3 scripts/scan.py --backtest            # 24 个月规则择时检验
  python3 scripts/scan.py --json                # 机器可读
  python3 scripts/scan.py --quiet               # 只写文件，输出一行状态

数据源：Yahoo Finance（首选，含复权价）→ Nasdaq API（备用）→ 本地缓存（兜底）。
本工具只做机械化的位置/情绪统计，不构成投资建议。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("需要 numpy：pip3 install numpy")

import fx as fxmod
import nyse_calendar as ncal
from ledger import Ledger, LedgerError, IdempotencyConflict, OverfillError

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET_TZ = ncal.ET_TZ
LOCAL_TZ = ncal.LOCAL_TZ

DEFAULT_CONFIG = {
    "portfolio": {
        "total_monthly_aud": 2500,
        "weights": {"VOO": 0.75, "SMH": 0.20, "TSLA": 0.05},
        "start_month": None,
        "note": "total_monthly_aud 是每月 AUD 预算；weights 是长期目标权重。",
    },
    "currency": {"budget": "AUD", "quote": "USD", "display": "both"},
    "fx": {"symbol": "AUDUSD=X", "fallback_rate": 0.65,
           "ttl_hours": 6, "max_stale_hours": 72},
    "decision": {
        "monthly_obligation": True,
        "score_role": "timing",
        "miss_policy": "carry_over",
        "max_catchup_per_order_months": 3,
        "max_catchup_aud": 0,
        "allow_split": False,
    },
    "commission": {
        "per_order_usd": 1.0, "per_share_usd": 0.0,
        "max_pct": 0.35, "max_batch_months": 6,
        "policy": "pay", "fee_mode": "per_order",
        "note": "policy 只支持 pay（硬约束优先）；合并用 --months 手工执行。",
    },
    "symbols": {
        "VOO": {"name": "标普500 ETF (Vanguard)", "style": "index"},
        "SMH": {"name": "半导体 ETF (VanEck)", "style": "cyclical"},
        "TSLA": {"name": "特斯拉 (Tesla)", "style": "hyper"},
    },
    "styles": {
        "index": {"label": "核心 · 宽基指数", "buy": 42, "partial": 26,
                  "window": "early", "early_days": 1,
                  "why": "核心仓每月必须完成：第 1 个交易日买入，不做月内择时。"},
        "cyclical": {"label": "卫星 · 周期行业", "buy": 50, "partial": 38,
                     "window": "early", "early_days": 1,
                     "why": "评分只做展示：T+1 回测未跑赢固定第 1 日；可改回 3 保留辅助窗口。"},
        "hyper": {"label": "卫星 · 高波动成长", "buy": 50, "partial": 42,
                  "window": "early", "early_days": 1,
                  "why": "评分只做展示：T+1 回测未跑赢固定第 1 日；可改回 3 保留辅助窗口。"},
    },
    "score_weights": {"s52": 0.20, "s60": 0.20, "sz20": 0.20,
                      "srsi": 0.15, "sdd": 0.15, "svix": 0.10},
    "vix": {"symbol": "^VIX", "good": 15, "bad": 30},
    "timezone": {"market": "America/New_York", "local": "Australia/Melbourne"},
    "sessions": {
        "open": {"et": "09:35", "label": "开盘后扫描"},
        "midday": {"et": "12:30", "label": "午盘扫描"},
        "close": {"et": "15:50", "label": "收盘前扫描"},
    },
    "backtest": {"signal": "close", "fill": "next_open", "slippage_bps": 0,
                 "funding": "first_trading_day", "cash_yield_annual": 0,
                 "sensitivity_aud": [2000, 2500, 3000]},
    "reports_dir": "reports",
    "data_dir": "data",
}

# ---------------------------------------------------------------------------
# NYSE 交易日历（优先 exchange_calendars；回退内置 2017–2028）
# ---------------------------------------------------------------------------
NYSE_HOLIDAYS = ncal.NYSE_HOLIDAYS
EARLY_CLOSES = ncal.EARLY_CLOSES
CALENDAR = ncal.CALENDAR
CalendarRangeError = ncal.CalendarRangeError


def is_trading_day(d):
    return CALENDAR.is_trading_day(d)


def iter_trading_days(start, end):
    return CALENDAR.sessions(start, end)


def next_trading_day(d):
    return CALENDAR.next_trading_day(d)


def prev_trading_day(d):
    return CALENDAR.prev_trading_day(d)


def last_trading_day_on_or_before(d):
    return CALENDAR.last_trading_day_on_or_before(d)


def first_trading_day_of_month(d):
    return CALENDAR.first_trading_day_of_month(d)


def last_trading_day_of_month(d):
    return CALENDAR.last_trading_day_of_month(d)


def month_trading_days(d):
    return CALENDAR.month_trading_days(d)


def et_open(d):
    return CALENDAR.session_open(d)


def et_close(d):
    return CALENDAR.session_close(d)


def is_after_close(et_now):
    d = et_now.date()
    if not CALENDAR.supports(d):
        return True
    return et_now >= et_close(d)


class ConfigError(ValueError):
    pass


def month_key(d):
    return f"{d.year}-{d.month:02d}"


def _month_add(month, n):
    y, m = int(month[:4]), int(month[5:7])
    m2 = m - 1 + n
    return f"{y + m2 // 12}-{m2 % 12 + 1:02d}"


def _month_range(start_month, end_month):
    cur = start_month
    while cur <= end_month:
        yield cur
        cur = _month_add(cur, 1)


def _valid_month(s):
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(s or "")))


def validate_config(cfg):
    p = cfg.get("portfolio") or {}
    if p.get("total_monthly_usd") is not None and p.get("total_monthly_aud") is None:
        raise ConfigError(
            "配置已迁移到 AUD：请把 portfolio.total_monthly_usd 改成 "
            "portfolio.total_monthly_aud（数值按你的 AUD 预算填写），并补上 currency/fx。")
    try:
        total = float(p.get("total_monthly_aud"))
    except (TypeError, ValueError):
        raise ConfigError("portfolio.total_monthly_aud 必须是正数") from None
    if total <= 0:
        raise ConfigError("portfolio.total_monthly_aud 必须 > 0")
    weights = p.get("weights") or {}
    if not weights:
        raise ConfigError("portfolio.weights 不能为空")
    if any(float(v) < 0 for v in weights.values()):
        raise ConfigError("weights 不能为负")
    if abs(sum(float(v) for v in weights.values()) - 1.0) > 0.001:
        raise ConfigError("weights 之和必须为 1.0")
    sm = p.get("start_month")
    if sm is not None and not _valid_month(sm):
        raise ConfigError("portfolio.start_month 必须是 YYYY-MM")
    for name, st in (cfg.get("styles") or {}).items():
        try:
            b, pa = float(st["buy"]), float(st["partial"])
        except (KeyError, TypeError, ValueError):
            raise ConfigError(f"styles.{name} 的 buy/partial 无效") from None
        if not (0 <= pa <= b <= 100):
            raise ConfigError(f"styles.{name} 需要 0<=partial<=buy<=100")
        if int(st.get("early_days", 1)) < 1:
            raise ConfigError(f"styles.{name}.early_days 必须 >= 1")
    rate = float((cfg.get("fx") or {}).get("fallback_rate") or 0)
    if rate and not (fxmod.RATE_MIN <= rate <= fxmod.RATE_MAX):
        raise ConfigError(f"fx.fallback_rate 必须在 {fxmod.RATE_MIN}–{fxmod.RATE_MAX}")
    pol = (cfg.get("commission") or {}).get("policy", "pay")
    if pol not in ("pay", "batch"):
        raise ConfigError("commission.policy 只能是 pay 或 batch")
    if pol == "batch":
        raise ConfigError(
            "v2 暂不支持自动 batch 合并；请用 commission.policy=pay，"
            "需要合并时手工执行 --confirm SYM:AMOUNT --months 2026-09,2026-10,...，"
            "系统只记录真实成交日，不会生成假历史成交。")
    return cfg


def deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.path.join(BASE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            deep_merge(cfg, json.load(f))
    return validate_config(cfg)


@dataclass
class Plan:
    symbol: str
    aud: float
    weight: float
    fx_rate: float | None
    fx_source: str
    fx_as_of: str | None
    firm: bool
    usd: float | None

    def as_dict(self):
        return {
            "symbol": self.symbol, "aud": self.aud, "usd": self.usd,
            "fx_rate": self.fx_rate, "fx_source": self.fx_source,
            "fx_as_of": self.fx_as_of, "firm": self.firm,
        }


def budget_for(cfg, month=None, ledger=None):
    if ledger is not None and month:
        b = ledger.budget_for(month)
        if b:
            return b
    p = cfg["portfolio"]
    return {"total_monthly_aud": float(p["total_monthly_aud"]),
            "weights": dict(p["weights"]), "effective_month": month}


def plan_for(cfg, symbol, fx_rate=None, fx_source="", fx_as_of=None,
             firm=False, month=None, ledger=None):
    b = budget_for(cfg, month, ledger)
    weight = float(b["weights"].get(symbol, 0) or 0)
    meta = cfg["symbols"].get(symbol, {})
    if meta.get("monthly_plan_aud") is not None:
        aud = float(meta["monthly_plan_aud"])
        weight = aud / float(b["total_monthly_aud"]) if b["total_monthly_aud"] else 0
    else:
        aud = round(float(b["total_monthly_aud"]) * weight, 2)
    usd = round(aud * float(fx_rate), 2) if fx_rate else None
    return Plan(symbol=symbol, aud=aud, weight=weight, fx_rate=fx_rate,
                fx_source=fx_source, fx_as_of=fx_as_of, firm=firm, usd=usd)


def ledger_path(cfg):
    return os.path.join(BASE, cfg["data_dir"], "dca.sqlite")


_LEGACY_IMPORT_DONE = set()


def get_ledger(cfg, et_now=None):
    path = ledger_path(cfg)
    L = Ledger(path)
    # 旧 CSV 一次性导入（仅当 DB 还没有订单）
    legacy = os.path.join(BASE, cfg["data_dir"], "dca_log.csv")
    if path not in _LEGACY_IMPORT_DONE and os.path.exists(legacy) and not L.all_orders():
        try:
            L.import_legacy_csv(legacy)
        except Exception:  # noqa: BLE001
            pass
        _LEGACY_IMPORT_DONE.add(path)
    # 预算版本：没有则建立；配置变化则从当前月生效，不追溯
    now_month = month_key((et_now or datetime.now(ET_TZ)).date())
    start_month = cfg["portfolio"].get("start_month") or now_month
    cur = L.budget_for(now_month)
    total = float(cfg["portfolio"]["total_monthly_aud"])
    weights = cfg["portfolio"]["weights"]
    if cur is None:
        L.set_budget_version(start_month, total, weights,
                             note="initial from config")
    elif (abs(float(cur["total_monthly_aud"]) - total) > 0.005
          or json.dumps(cur["weights"], sort_keys=True) != json.dumps(weights, sort_keys=True)):
        L.set_budget_version(now_month, total, weights, note="config changed")
    return L


def commission_usd(cfg, amount, price):
    """单笔佣金估算：max(固定佣金, 每股佣金 × 股数)。"""
    c = cfg.get("commission") or {}
    fixed = float(c.get("per_order_usd") or 0)
    per_share = float(c.get("per_share_usd") or 0)
    shares = (amount / price) if price else 0.0
    return round(max(fixed, per_share * shares), 2)


def suggest_batch(cfg, plan_usd, price):
    """小额单佣金占比过高时，给出「累积 N 个月再买」的建议。"""
    c = cfg.get("commission") or {}
    max_pct = float(c.get("max_pct") or 0) / 100.0
    if plan_usd <= 0 or max_pct <= 0:
        return None
    fee = commission_usd(cfg, plan_usd, price)
    if fee <= 0 or fee / plan_usd <= max_pct:
        return None
    cap = max(2, int(c.get("max_batch_months") or 6))
    for n in range(2, cap + 1):
        amt = plan_usd * n
        f = commission_usd(cfg, amt, price)
        if f / amt <= max_pct:
            return {"months": n, "amount": round(amt, 2), "commission": f,
                    "pct": round(f / amt, 6), "max_pct": max_pct}
    n = cap
    amt = plan_usd * n
    f = commission_usd(cfg, amt, price)
    return {"months": n, "amount": round(amt, 2), "commission": f,
            "pct": round(f / amt, 6), "max_pct": max_pct, "capped": True}


# ---------------------------------------------------------------------------
# 数据获取：Yahoo（curl，含复权）→ Nasdaq API → 本地缓存
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
FETCH_NOTES = []


class RateLimited(RuntimeError):
    """Yahoo 对非浏览器客户端会返回 429，进入冷却期改用备用源。"""


def _state_path():
    return os.path.join(BASE, "data", "cache", "provider_state.json")


def _read_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _write_state(state):
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:  # noqa: BLE001
        pass


def _curl(url, timeout=12):
    """用系统 curl 抓取（Yahoo 会拦截 Python 的 TLS 指纹）。返回 body 文本。"""
    exe = shutil.which("curl")
    if not exe:
        raise RuntimeError("系统缺少 curl")
    proc = subprocess.run(
        # Yahoo 的 WAF 会拦截「完整 Chrome UA + 额外 Accept 头」的请求（429），
        # 实测只带简洁的 Mozilla/5.0 反而稳定。
        [exe, "-sS", "-m", str(timeout), "-A", "Mozilla/5.0",
         "-w", "\n%{http_code}", url],
        capture_output=True, text=True)
    body, _, code = (proc.stdout or "").rpartition("\n")
    code = code.strip()
    if code == "429":
        raise RateLimited(f"HTTP 429 {url}")
    if proc.returncode != 0 or not code.isdigit() or int(code) >= 400 or not body.strip():
        raise RuntimeError(f"curl 退出码 {proc.returncode}，HTTP {code or '?'}，{(proc.stderr or '')[:150]}")
    return body


def http_json(url, timeout=25, attempts=3):
    """直连（用于 Nasdaq API）。"""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            code = getattr(exc, "code", None)
            time.sleep(2 * (i + 1) if code in (429, 500, 502, 503) else 1 + i)
    raise RuntimeError(f"HTTP 失败 {url}: {last}")


def yahoo_json(path):
    """Yahoo 首发请求走 curl；命中 429 后冷却 15 分钟，期间由调用方走备用源。"""
    state = _read_state()
    blocked = float(state.get("yahoo_blocked_until") or 0)
    if time.time() < blocked:
        raise RateLimited(f"Yahoo 冷却期剩余 {int(blocked - time.time())}s")
    last = None
    for i in range(3):
        host = HOSTS[i % len(HOSTS)]
        url = f"https://{host}{path}"
        try:
            body = _curl(url)
            if blocked:
                state["yahoo_blocked_until"] = 0
                _write_state(state)
            return json.loads(body)
        except RateLimited as exc:
            state["yahoo_blocked_until"] = time.time() + 900
            _write_state(state)
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6 + 0.6 * i)
    raise RuntimeError(f"Yahoo 获取失败 {path}: {last}")


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip()
    if s in ("", "N/A", "NA", "n/a", "--", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def cache_path(cfg, name):
    return os.path.join(BASE, cfg["data_dir"], "cache", name)


def _read_cache(cfg, name):
    p = cache_path(cfg, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _write_cache(cfg, name, obj):
    p = cache_path(cfg, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


def fetch_daily_yahoo(symbol):
    path = (f"/v8/finance/chart/{urllib.parse.quote(symbol)}"
            f"?range=10y&interval=1d&includePrePost=false")
    data = yahoo_json(path)
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        a = adj[i] if i < len(adj) and adj[i] else c
        rows.append({
            "date": datetime.fromtimestamp(t, tz=ET_TZ).date(),
            "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
            "close": float(c), "volume": float(q["volume"][i] or 0), "adj": float(a),
        })
    if len(rows) < 60:
        raise RuntimeError(f"{symbol} 日线不足（{len(rows)} 根）")
    return rows, {"provider": "yahoo"}


def nasdaq_asset_class(meta):
    return meta.get("asset_class") or (
        "etf" if meta.get("style") in ("index", "cyclical") else "stocks")


def fetch_daily_nasdaq(meta, symbol):
    ac = nasdaq_asset_class(meta)
    fromdate = (datetime.now(ET_TZ).date() - timedelta(days=3650)).isoformat()
    url = (f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/historical"
           f"?assetclass={ac}&fromdate={fromdate}&limit=9999&offset=0")
    data = http_json(url)
    rows_raw = (((data.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    out = []
    for r in reversed(rows_raw):
        try:
            d = datetime.strptime(r["date"], "%m/%d/%Y").date()
        except (KeyError, ValueError):
            continue
        close = _num(r.get("close"))
        if close is None:
            continue
        out.append({"date": d, "open": _num(r.get("open")), "high": _num(r.get("high")),
                    "low": _num(r.get("low")), "close": close,
                    "volume": _num(r.get("volume")) or 0.0, "adj": close})
    if len(out) < 60:
        raise RuntimeError(f"{symbol} Nasdaq 日线不足（{len(out)} 根）")
    return out, {"provider": "nasdaq"}


def fetch_daily_cached(cfg, symbol, meta=None, ttl_hours=6):
    """日线变化慢：6 小时内复用缓存；两个源都失败时回退到旧缓存。"""
    meta = meta or cfg["symbols"].get(symbol, {})
    name = f"daily_{symbol.replace('^', 'IDX_')}.json"
    cached = _read_cache(cfg, name)
    if cached:
        age = time.time() - cached.get("fetched_at", 0)
        if age < ttl_hours * 3600:
            rows = [dict(r, date=date.fromisoformat(r["date"])) for r in cached["rows"]]
            return rows, cached["meta"]
    errors = []
    for provider, fn in (
            ("yahoo", lambda: fetch_daily_yahoo(symbol)),
            ("nasdaq", lambda: fetch_daily_nasdaq(meta, symbol))):
        try:
            rows, prov_meta = fn()
            _write_cache(cfg, name, {
                "fetched_at": time.time(), "meta": prov_meta,
                "rows": [dict(r, date=r["date"].isoformat()) for r in rows],
            })
            if provider != "yahoo":
                FETCH_NOTES.append(f"{symbol}: Yahoo 不可用，日线已切到 Nasdaq")
            return rows, prov_meta
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider}: {exc}")
    if cached:
        rows = [dict(r, date=date.fromisoformat(r["date"])) for r in cached["rows"]]
        FETCH_NOTES.append(f"{symbol}: 实时源均失败，使用缓存日线（{len(rows)} 根）")
        return rows, cached["meta"]
    raise RuntimeError(f"{symbol} 日线获取失败：{' | '.join(errors)}")


def fetch_live_yahoo(symbol):
    path = (f"/v8/finance/chart/{urllib.parse.quote(symbol)}"
            f"?range=1d&interval=5m&includePrePost=true")
    data = yahoo_json(path)
    res = data["chart"]["result"][0]
    meta = res["meta"]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]

    def price_or_last():
        for key in ("regularMarketPrice", "postMarketPrice", "preMarketPrice"):
            v = meta.get(key)
            if v:
                return float(v)
        for c in reversed(q.get("close") or []):
            if c:
                return float(c)
        return None

    last_bar = None
    for i in range(len(ts) - 1, -1, -1):
        if q["close"][i]:
            last_bar = datetime.fromtimestamp(ts[i], tz=ET_TZ)
            break

    price = price_or_last()
    if price is None:
        raise RuntimeError(f"{symbol} 无实时价")
    return {
        "meta": meta, "price": price,
        "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "state": meta.get("marketState") or "CLOSED",
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "last_bar": last_bar,
        "pre": meta.get("preMarketPrice"), "post": meta.get("postMarketPrice"),
        "provider": "yahoo",
    }


def fetch_live_nasdaq(meta, symbol):
    ac = nasdaq_asset_class(meta)
    base = "https://api.nasdaq.com/api/quote"
    info = (http_json(f"{base}/{urllib.parse.quote(symbol)}/info?assetclass={ac}")).get("data") or {}
    primary = info.get("primaryData") or {}
    price = _num(primary.get("lastSalePrice"))
    if price is None:
        raise RuntimeError(f"{symbol} Nasdaq 无报价")
    net = _num(primary.get("netChange"))
    prev = price - net if net is not None else None
    status = (info.get("marketStatus") or "").lower()
    state = {"open": "REGULAR", "closed": "CLOSED", "after-hours": "POST",
             "pre-market": "PRE"}.get(status, "CLOSED")
    high = low = vol = None
    try:
        et_today = datetime.now(ET_TZ).date()
        fromdate = (et_today - timedelta(days=7)).isoformat()
        todate = et_today.isoformat()
        ch = http_json(f"{base}/{urllib.parse.quote(symbol)}/chart?assetclass={ac}"
                       f"&fromdate={fromdate}&todate={todate}")
        bars = (ch.get("data") or {}).get("chart") or []
        if bars:
            z = bars[-1]["z"]
            high, low, vol = _num(z.get("high")), _num(z.get("low")), _num(z.get("volume"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "meta": {}, "price": price, "prev_close": prev, "state": state,
        "day_high": high, "day_low": low, "volume": vol,
        "last_bar": None, "pre": None, "post": None, "provider": "nasdaq",
    }


def fetch_live(cfg, symbol):
    try:
        return fetch_live_yahoo(symbol)
    except Exception as yerr:  # noqa: BLE001
        try:
            q = fetch_live_nasdaq(cfg["symbols"].get(symbol, {}), symbol)
            FETCH_NOTES.append(f"{symbol}: 实时价改用 Nasdaq（Yahoo 限流/失败）")
            return q
        except Exception as nerr:  # noqa: BLE001
            raise RuntimeError(f"{symbol} 报价失败：Yahoo {yerr} | Nasdaq {nerr}")


def fetch_vix(cfg):
    """返回 (vix, 序列, 来源)。Yahoo → 最近一次成功值 → VOO 已实现波动率代理 → 20。"""
    try:
        rows, _ = fetch_daily_cached(cfg, cfg["vix"]["symbol"], {}, ttl_hours=6)
        closes = np.array([r["close"] for r in rows], dtype=float)
        if float(closes[-1]) > 5:
            st = _read_state()
            st["vix_last"] = {"value": float(closes[-1]), "at": time.time()}
            _write_state(st)
            return float(closes[-1]), closes, "yahoo"
    except Exception:  # noqa: BLE001
        pass
    st = _read_state().get("vix_last") or {}
    age_h = (time.time() - float(st.get("at") or 0)) / 3600.0
    if st.get("value") and age_h < 72:
        FETCH_NOTES.append(f"VIX 实时源不可用，使用 {age_h:.1f} 小时前的 VIX {float(st['value']):.1f}")
        return float(st["value"]), None, "cached"
    try:
        rows, _ = fetch_daily_cached(cfg, "VOO", cfg["symbols"].get("VOO", {}), ttl_hours=6)
        c = np.array([r["close"] for r in rows], dtype=float)
        ret = np.diff(np.log(c[-22:]))
        vol = float(ret.std(ddof=1) * np.sqrt(252) * 100)
        FETCH_NOTES.append("VIX 获取失败，改用 VOO 已实现波动率代理")
        return vol, None, "proxy"
    except Exception:  # noqa: BLE001
        FETCH_NOTES.append("VIX 获取失败，按中性值 20 处理")
        return 20.0, None, "default"


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def rsi_wilder(arr, period=14):
    arr = np.asarray(arr, dtype=float)
    if len(arr) < period + 1:
        return 50.0
    d = np.diff(arr)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    ag = gains[:period].mean()
    al = losses[:period].mean()
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def score_components(p, closes, vix, cfg):
    """六个分项（0-100，越高越适合买入）+ 加权总分。"""
    closes = np.asarray(closes, dtype=float)
    win = closes[-252:] if len(closes) >= 252 else closes
    hi, lo = float(win.max()), float(win.min())
    pos52 = (p - lo) / (hi - lo) if hi > lo else 0.5
    pos52 = clip(pos52, 0, 1)

    last60 = closes[-60:] if len(closes) >= 60 else closes
    rank60 = float((last60 < p).sum()) / len(last60)

    ma20 = float(closes[-20:].mean())
    sd20 = float(closes[-20:].std(ddof=1)) if len(closes) >= 20 else 0.0
    z20 = (p - ma20) / sd20 if sd20 > 0 else 0.0

    r = rsi_wilder(closes, 14)
    dd = p / hi - 1.0

    good, bad = float(cfg["vix"]["good"]), float(cfg["vix"]["bad"])
    s52 = 100.0 * (1.0 - pos52)
    s60 = 100.0 * (1.0 - rank60)
    sz20 = clip(50.0 - 25.0 * z20)
    srsi = clip(100.0 - r)
    sdd = clip(100.0 * (-dd) / 0.15)
    svix = clip((bad - vix) / (bad - good) * 100.0)

    w = cfg["score_weights"]
    total = (w["s52"] * s52 + w["s60"] * s60 + w["sz20"] * sz20
             + w["srsi"] * srsi + w["sdd"] * sdd + w["svix"] * svix)

    vol20 = None
    if len(closes) >= 21:
        ret = np.diff(np.log(closes[-21:]))
        vol20 = float(ret.std(ddof=1) * np.sqrt(252))

    return {
        "pos52": pos52, "rank60": rank60, "z20": z20, "rsi": r,
        "dd52": dd, "s52": s52, "s60": s60, "sz20": sz20, "srsi": srsi,
        "sdd": sdd, "svix": svix, "score": round(float(total), 1),
        "ma20": ma20, "ma50": float(closes[-50:].mean()) if len(closes) >= 50 else ma20,
        "ma200": float(closes[-200:].mean()) if len(closes) >= 200 else ma20,
        "sd20": sd20, "hi52": hi, "lo52": lo, "close": p, "vol20": vol20,
    }


def perf(closes, n):
    closes = np.asarray(closes, dtype=float)
    if len(closes) <= n:
        return None
    return float(closes[-1] / closes[-1 - n] - 1.0)


def volume_ratio(live, daily, et_now, state):
    try:
        if not live.get("volume") or not daily:
            return None
        avg20 = float(np.mean([r["volume"] for r in daily[-20:] if r.get("volume")]))
        if avg20 <= 0:
            return None
        if state == "REGULAR":
            open_t = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
            mins = max(1.0, (et_now - open_t).total_seconds() / 60.0)
            expected = avg20 * min(1.0, mins / 390.0)
        else:
            expected = avg20
        return float(live["volume"]) / expected if expected else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 市场时钟与月度义务
# ---------------------------------------------------------------------------
def infer_session(et_now):
    anchors = [("open", 9 * 60 + 35), ("midday", 12 * 60 + 30), ("close", 15 * 60 + 50)]
    mins = et_now.hour * 60 + et_now.minute
    return min(anchors, key=lambda a: abs(mins - a[1]))[0]


def market_clock(cfg, et_now, session_date=None):
    """单一市场时钟：所有“今天/下一时段”文案都以它为准。"""
    d = session_date or et_now.date()
    local_now = et_now.astimezone(LOCAL_TZ)
    supported = CALENDAR.supports(d)
    source = CALENDAR.source_for(d)
    base = {
        "et_now": et_now, "et_date": d, "local_now": local_now,
        "calendar_supported": supported, "calendar_source": source,
        "calendar_warning": "",
        "trading_today": False, "session_phase": "UNSUPPORTED",
        "can_fill_now": False, "can_place_order": False,
        "early_close": False, "close_et": None,
        "next_fill_et": None, "next_fill_local": None,
        "total_days": 0, "day_idx": 0,
        "days_left_after_today": 0, "days_left_incl_today": 0,
        "last_trading_day": None,
    }
    if not supported:
        base["calendar_warning"] = (
            f"{d.isoformat()} 超出所有 NYSE 日历后端支持范围；不给交易建议。"
            "请安装 exchange_calendars 或扩展内置日历表。")
        return base
    trading = CALENDAR.is_trading_day(d)
    open_et = CALENDAR.session_open(d)
    close_et = CALENDAR.session_close(d)
    early = bool(trading and close_et.hour < 16)
    if not trading:
        phase = "HOLIDAY"
    elif et_now.date() != d or et_now >= close_et:
        phase = "POST"
    elif et_now < open_et:
        phase = "PRE"
    else:
        phase = "REGULAR"
    can_fill = bool(trading and et_now.date() == d and open_et <= et_now < close_et)
    can_place = bool(trading and et_now.date() == d and et_now < close_et)
    if can_fill:
        next_fill_et = et_now
    elif trading and et_now.date() == d and et_now < open_et:
        next_fill_et = open_et
    else:
        nd = CALENDAR.next_trading_day(d)
        next_fill_et = CALENDAR.session_open(nd)
    mdays = CALENDAR.month_trading_days(d)
    day_idx = sum(1 for x in mdays if x <= d)
    left_after = sum(1 for x in mdays if x > d)
    left_incl = sum(1 for x in mdays if x >= d)
    warning = ""
    if source == "builtin":
        warning = "使用内置 NYSE 日历（exchange_calendars 未安装或不覆盖该日期）"
    base.update({
        "trading_today": trading, "session_phase": phase,
        "can_fill_now": can_fill, "can_place_order": can_place,
        "early_close": early, "close_et": close_et,
        "next_fill_et": next_fill_et,
        "next_fill_local": next_fill_et.astimezone(LOCAL_TZ),
        "total_days": len(mdays), "day_idx": day_idx,
        "days_left_after_today": left_after,
        "days_left_incl_today": left_incl,
        "last_trading_day": mdays[-1] if mdays else None,
        "calendar_warning": warning,
    })
    return base


def market_state_cn(state_or_clock, trading_today=None, early_close=False):
    if isinstance(state_or_clock, dict):
        clock = state_or_clock
        if not clock.get("calendar_supported", True):
            return "日历不支持（不给交易建议）"
        if not clock.get("trading_today"):
            return "休市（非交易日）"
        phase = clock.get("session_phase")
        if phase == "PRE":
            return "pre · 盘前"
        if phase == "REGULAR":
            return "regular · 盘中交易"
        if phase == "POST":
            return ("closed · 已收盘（提前收盘 13:00）"
                    if clock.get("early_close") else "closed · 已收盘")
        return "closed · 已收盘"
    # 向后兼容旧签名
    if not trading_today:
        return "休市（非交易日）"
    if early_close and (state_or_clock or "").upper() in ("CLOSED", "POST"):
        return "closed · 已收盘（今日提前收盘 13:00）"
    return {"REGULAR": "regular · 盘中交易", "PRE": "pre · 盘前",
            "POST": "post · 盘后", "CLOSED": "closed · 已收盘"}.get(
                state_or_clock, state_or_clock or "unknown")


def _month_obligation_symbol(cfg, ledger, month, symbol, fx, start_month):
    b = budget_for(cfg, month, ledger)
    base = float(b["total_monthly_aud"]) * float(b["weights"].get(symbol, 0) or 0)
    carry = 0.0
    detail = []
    earliest = None
    if start_month and start_month <= month:
        prev_months = list(_month_range(start_month, _month_add(month, -1)))
    else:
        prev_months = []
    for m in prev_months:
        bm = budget_for(cfg, m, ledger)
        mbase = float(bm["total_monthly_aud"]) * float(bm["weights"].get(symbol, 0) or 0)
        required = mbase + carry
        filled = ledger.month_allocated_aud(m, symbol)
        wo = sum(float(r["amount_aud"]) for r in ledger.write_offs_for(m, symbol))
        short = max(0.0, required - filled - wo)
        if short > 0 and earliest is None:
            earliest = m
        detail.append({
            "month": m, "base_aud": round(mbase, 2), "required_aud": round(required, 2),
            "filled_aud": round(filled, 2), "writeoff_aud": round(wo, 2),
            "shortfall_aud": round(short, 2),
        })
        carry = short
    filled = ledger.month_allocated_aud(month, symbol)
    pending_raw = ledger.pending_by_month(month, symbol)
    wo = sum(float(r["amount_aud"]) for r in ledger.write_offs_for(month, symbol))
    required = base + carry
    gross_remaining = max(0.0, required - filled - wo)
    pending = min(pending_raw, gross_remaining)
    remaining = max(0.0, gross_remaining - pending)
    tol = max(0.5, required * 0.005)
    if gross_remaining <= tol:
        status = "PAID"
        pending = 0.0
    elif remaining <= tol and pending > tol:
        status = "PENDING"
    elif carry > tol:
        status = "OVERDUE"
    else:
        status = "DUE"
    rate = fx.rate if fx else None
    return {
        "symbol": symbol, "month": month,
        "base_aud": round(base, 2), "arrears_aud": round(carry, 2),
        "required_aud": round(required, 2), "filled_aud": round(filled, 2),
        "pending_aud": round(pending, 2),
        "pending_orders_aud": round(pending_raw, 2),
        "writeoff_aud": round(wo, 2),
        "remaining_aud": round(remaining, 2),
        "base_usd": round(base * rate, 2) if rate else None,
        "required_usd": round(required * rate, 2) if rate else None,
        "remaining_usd": round(remaining * rate, 2) if rate else None,
        "fx_rate": rate, "fx_firm": bool(fx.firm) if fx else False,
        "status": status, "earliest_arrears_month": earliest,
        "arrears_detail": detail,
        "tolerance_aud": round(tol, 2),
    }


def month_obligation(cfg, ledger, month, fx, et_now=None):
    start_month = cfg["portfolio"].get("start_month")
    if not start_month:
        b = ledger.budget_for(month)
        start_month = (b or {}).get("effective_month") or month
    symbols = {}
    for sym in cfg["symbols"]:
        symbols[sym] = _month_obligation_symbol(cfg, ledger, month, sym, fx, start_month)
    total = {
        "base_aud": round(sum(v["base_aud"] for v in symbols.values()), 2),
        "arrears_aud": round(sum(v["arrears_aud"] for v in symbols.values()), 2),
        "required_aud": round(sum(v["required_aud"] for v in symbols.values()), 2),
        "filled_aud": round(sum(v["filled_aud"] for v in symbols.values()), 2),
        "pending_aud": round(sum(v["pending_aud"] for v in symbols.values()), 2),
        "remaining_aud": round(sum(v["remaining_aud"] for v in symbols.values()), 2),
        "remaining_usd": (round(sum(v["remaining_aud"] for v in symbols.values()) * fx.rate, 2)
                          if fx and fx.rate else None),
    }
    return {"month": month, "symbols": symbols, "total": total,
            "start_month": start_month}


# ---------------------------------------------------------------------------
# 定投决策（硬约束 + 评分只做窗口内择时）
# ---------------------------------------------------------------------------
VERDICT_SHORT = {
    "BUY_NOW": "✅ 执行",
    "BUY": "✅ 定投",
    "WAIT": "⏸ 等待",
    "PENDING": "⏳ 已挂单",
    "DONE": "☑️ 已完成",
    "DEFER": "⏳ 合并中",
    "NEXT_SESSION": "→ 下一时段",
    "REVIEW_FX": "⚠️ 待确认汇率",
}


def _recommend_cap(cfg, obligation, plan):
    d = cfg.get("decision") or {}
    caps = []
    months = int(d.get("max_catchup_per_order_months") or 0)
    if months > 0:
        caps.append(plan.aud * months)
    aud = float(d.get("max_catchup_aud") or 0)
    if aud > 0:
        caps.append(aud)
    cap = min(caps) if caps else None
    return min(obligation["remaining_aud"], cap) if cap else obligation["remaining_aud"]


def decide(score, style_cfg, day_idx, days_left, obligation, plan, timing, cfg):
    """返回 (code, 文案, 建议 AUD 金额, ratio, reasons)。

    评分只决定窗口内哪一天；金额/是否投/最晚日期由义务和硬约束决定。
    """
    reasons = []
    remaining = float(obligation["remaining_aud"])
    pending = float(obligation["pending_aud"])
    arrears = float(obligation["arrears_aud"])
    tol = max(0.5, float(obligation["required_aud"]) * 0.005)
    if obligation["status"] == "PAID":
        return "DONE", "☑️ 本月义务已完成", 0.0, 0.0, reasons
    if obligation["status"] == "PENDING" and remaining <= tol:
        return "PENDING", "⏳ 已挂单，等待成交（未成交不会算完成）", 0.0, 0.0, reasons
    if remaining <= tol:
        return "DONE", "☑️ 本月义务已完成", 0.0, 0.0, reasons

    can_fill = bool(timing.get("can_fill_now"))
    next_mode = timing.get("execution_mode") == "next_open"
    if not can_fill and not next_mode:
        nxt = timing.get("next_fill_et")
        local = timing.get("next_fill_local")
        when = (f"{nxt.strftime('%m/%d %H:%M')} ET" if nxt else "下一交易时段")
        if local:
            when += f" = {local.strftime('%m/%d %H:%M')} 墨尔本"
        code = "REVIEW_FX" if timing.get("fx_firm") is False else "NEXT_SESSION"
        note = ("当前不可成交；" if timing.get("can_place_order") is False
                else "盘前可挂开盘单；")
        return code, f"→ 下一时段：{note}{when}", _recommend_cap(cfg, obligation, plan), 0.0, reasons

    buy = float(style_cfg["buy"])
    window_end = max(1, int(style_cfg.get("early_days", 1)))
    cap = _recommend_cap(cfg, obligation, plan)
    if cap < remaining - 0.005:
        reasons.append(f"单笔补投上限：本次 A${cap:,.0f}，剩余 A${remaining - cap:,.0f} 继续挂账，后续继续补投")
    if arrears > tol:
        reasons.append(f"历史欠额 A${arrears:,.0f}（最早 {obligation.get('earliest_arrears_month') or '—'}），优先补投")
    if next_mode:
        reasons.append("回测口径：T 日收盘信号，T+1 开盘成交")
    if timing.get("fx_firm") is False:
        reasons.append("FX 已过期/回退：AUD 义务确定，但 USD 金额仅为估算；"
                       "请提供实际汇率或实际美元金额后再确认成交")
        return "REVIEW_FX", "⚠️ 待确认汇率（不可作为确定下单金额）", cap, 0.0, reasons
    if day_idx > window_end:
        reasons.append(f"窗口已过（第 {day_idx} 个交易日 > {window_end}），按硬约束补投")
        return "BUY_NOW", "✅ 强制补投（窗口已过）", cap, cap / plan.aud if plan.aud else 0.0, reasons
    if score >= buy:
        reasons.append(f"窗口第 {day_idx}/{window_end} 天，评分 {score:.0f} ≥ {buy:.0f}，提前执行")
        return "BUY", "✅ 定投（窗口内评分达标）", cap, cap / plan.aud if plan.aud else 0.0, reasons
    if day_idx >= window_end:
        reasons.append(f"窗口最后一天（第 {window_end} 个交易日），硬约束必投")
        return "BUY_NOW", "✅ 定投（窗口最后一天，必投）", cap, cap / plan.aud if plan.aud else 0.0, reasons
    reasons.append(f"窗口第 {day_idx}/{window_end} 天，评分 {score:.0f} 未达 {buy:.0f}；"
                   f"评分只影响窗口内哪一天，最迟第 {window_end} 个交易日必投")
    return "WAIT", "⏸ 再等（硬截止日前必投）", 0.0, 0.0, reasons


# ---------------------------------------------------------------------------
# 台账兼容层（旧接口读 SQLite；写入走 orders/fills）
# ---------------------------------------------------------------------------
def load_ledger(cfg):
    """旧接口兼容：返回 fill 视图（amount 为 AUD，month 为真实成交月）。"""
    L = get_ledger(cfg)
    out = []
    for f in L.all_fills():
        out.append({
            "confirmed_at_et": f.get("filled_at_et"),
            "et_date": f.get("trade_date"),
            "month": (f.get("trade_date") or "")[:7],
            "symbol": f.get("symbol"),
            "price": f.get("price_usd"),
            "amount": f.get("amount_aud"),
            "amount_aud": f.get("amount_aud"),
            "amount_usd": f.get("amount_usd"),
            "status": "filled",
            "fill_id": f.get("fill_id"),
            "order_id": f.get("order_id"),
            "note": f.get("note") or "",
        })
    return out


def month_entries(rows, mk):
    out = {}
    for r in rows:
        if r.get("month") == mk and r.get("symbol"):
            out.setdefault(r["symbol"], []).append(r)
    return out


def parse_confirm_spec(cfg, spec):
    """解析 --confirm：VOO,SMH / all / VOO:1875,SMH:500（金额为 AUD）。"""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        amount = None
        sym = part
        if ":" in part:
            sym, raw_amt = part.split(":", 1)
            sym = sym.strip()
            try:
                amount = float(raw_amt.strip())
            except ValueError:
                raise ValueError(f"无法解析金额：{part}") from None
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError(f"金额必须是大于 0 的有限数字：{part}")
        sym = sym.upper()
        syms = list(cfg["symbols"]) if sym in ("ALL", "*") else [sym]
        for s in syms:
            if s not in cfg["symbols"]:
                raise ValueError(f"未知标的：{s}（可选 {'、'.join(cfg['symbols'])}）")
            out.append((s, amount))
    if not out:
        raise ValueError("没有解析到任何标的")
    return out


def _resolve_trade_date(value, et_now):
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise ValueError(f"trade-date 格式错误：{value}") from None
    return et_now.date().isoformat()


def record_confirmations(cfg, entries, note="", et_now=None, status="filled",
                         trade_date=None, price=None, amount_usd=None,
                         fx_rate=None, fill_id=None, order_id=None,
                         filled_aud=None):
    """写入成交/挂单。entries: [{symbol, amount(AUD), ...}]。

    默认 status=filled；placed 只建 order 不写 fill；partial 需要 filled_aud。
    FX 非 firm 时必须显式提供 fx_rate 或 amount_usd，否则拒绝。
    """
    et_now = et_now or datetime.now(ET_TZ)
    L = get_ledger(cfg, et_now)
    fx = fxmod.fetch_fx(cfg, yahoo_json)
    recorded, skipped = [], []
    for e in entries:
        sym = str(e.get("symbol") or "").strip().upper()
        if sym not in cfg["symbols"]:
            skipped.append({"symbol": sym, "reason": "未知标的"})
            continue
        st = str(e.get("status") or status or "filled").lower()
        if st not in ("placed", "filled", "partial", "cancelled"):
            skipped.append({"symbol": sym, "reason": f"非法 status：{st}"})
            continue
        # 幂等：先按 fill_id / idempotency_key 查已有成交，避免重试产生孤儿订单
        pre_fid = e.get("fill_id") or fill_id
        pre_key = e.get("idempotency_key")
        pre = None
        try:
            pre = L.get_fill(pre_fid) if pre_fid else L.get_fill_by_idempotency(pre_key)
        except Exception:  # noqa: BLE001
            pre = None
        if pre is not None:
            if pre["symbol"] != sym:
                skipped.append({"symbol": sym, "reason": "fill_id 已属于其他标的"})
                continue
            recorded.append({"symbol": sym, "status": "filled", "order_id": pre["order_id"],
                             "fill_id": pre["fill_id"], "amount_aud": float(pre["amount_aud"]),
                             "amount_usd": float(pre["amount_usd"]),
                             "price": float(pre["price_usd"]),
                             "trade_date": pre["trade_date"],
                             "commission_usd": float(pre["commission_usd"]),
                             "fx_rate": float(pre["fx_rate"])})
            continue
        if st == "cancelled":
            oid = e.get("order_id") or order_id
            if not oid:
                skipped.append({"symbol": sym, "reason": "取消需要 --order-id"})
                continue
            try:
                o = L.cancel_order(oid, note=e.get("note") or note)
                recorded.append({"symbol": sym, "status": "cancelled", "order_id": oid,
                                 "amount_aud": float(o["planned_amount_aud"]),
                                 "trade_date": "", "fill_id": ""})
            except Exception as exc:  # noqa: BLE001
                skipped.append({"symbol": sym, "reason": str(exc)})
            continue
        td = _resolve_trade_date(e.get("trade_date") or trade_date, et_now)
        mk = td[:7]
        raw_amount = e.get("amount_aud", e.get("amount"))
        if raw_amount in (None, ""):
            pre_oid = e.get("order_id") or order_id
            if pre_oid:
                o_ex = L.get_order(pre_oid)
                if o_ex:
                    filled_ex = sum(float(f["amount_aud"]) for f in L.fills_for_order(pre_oid))
                    raw_amount = max(0.0, float(o_ex["planned_amount_aud"]) - filled_ex)
            if raw_amount in (None, ""):
                ob = month_obligation(cfg, L, mk, fx, et_now)
                raw_amount = (ob["symbols"][sym]["remaining_aud"]
                              or plan_for(cfg, sym, fx.rate, month=mk, ledger=L).aud)
        try:
            amount_aud = round(float(raw_amount), 2)
        except (TypeError, ValueError):
            skipped.append({"symbol": sym, "reason": "金额无效"})
            continue
        if not (math.isfinite(amount_aud) and amount_aud >= 0.01 and amount_aud <= 1e7):
            skipped.append({"symbol": sym, "reason": "金额无效（需 ≥ A$0.01）"})
            continue
        raw_months = e.get("months")
        if isinstance(raw_months, str):
            months = [x.strip() for x in raw_months.split(",") if x.strip()]
        elif isinstance(raw_months, (list, tuple)):
            months = [str(x).strip() for x in raw_months if str(x).strip()]
        else:
            months = []
        months = months or [mk]
        if any(not _valid_month(m) for m in months):
            skipped.append({"symbol": sym, "reason": "months 格式错误（YYYY-MM）"})
            continue
        if mk not in months:
            months = [mk] + months
        explicit_rate = e.get("fx_rate", fx_rate)
        explicit_usd = e.get("amount_usd", amount_usd)
        if not fx.firm and explicit_rate in (None, "") and explicit_usd in (None, ""):
            skipped.append({"symbol": sym,
                            "reason": "汇率已过期；请提供 --fx-rate 或 --amount-usd 实际值"})
            continue
        rate = None
        if explicit_rate not in (None, "") or fx.rate is not None:
            try:
                rate = (float(explicit_rate) if explicit_rate not in (None, "")
                        else float(fx.rate))
            except (TypeError, ValueError):
                skipped.append({"symbol": sym, "reason": "汇率无效"})
                continue
            if not (math.isfinite(rate) and rate > 0):
                skipped.append({"symbol": sym, "reason": "汇率无效"})
                continue
        raw_price = e.get("price", price)
        if st in ("filled", "partial"):
            if raw_price in (None, ""):
                try:
                    raw_price = fetch_live(cfg, sym)["price"]
                except Exception as exc:  # noqa: BLE001
                    skipped.append({"symbol": sym, "reason": f"报价失败：{exc}"})
                    continue
            try:
                px = float(raw_price)
            except (TypeError, ValueError):
                skipped.append({"symbol": sym, "reason": "价格无效"})
                continue
            if not (math.isfinite(px) and 0.01 <= px <= 1e6):
                skipped.append({"symbol": sym, "reason": "价格无效"})
                continue
        else:
            px = float(raw_price) if raw_price not in (None, "") else 0.0
        if explicit_usd not in (None, "") and explicit_rate in (None, ""):
            rate = None  # 有实际美元金额时，以实际成交反推记账汇率
        if rate is None:
            # 汇率过期时允许用「实际美元金额 + AUD 预算」反推记账汇率
            if explicit_usd in (None, ""):
                skipped.append({"symbol": sym, "reason": "汇率无效且未提供 --amount-usd"})
                continue
            try:
                usd0 = float(explicit_usd)
            except (TypeError, ValueError):
                skipped.append({"symbol": sym, "reason": "美元金额无效"})
                continue
            if usd0 <= 0:
                skipped.append({"symbol": sym, "reason": "美元金额无效"})
                continue
            fee0 = commission_usd(cfg, usd0, px) if (st in ("filled", "partial") and px > 0) else 0.0
            rate = (usd0 + fee0) / amount_aud if amount_aud > 0 else None
            if rate is None:
                skipped.append({"symbol": sym, "reason": "无法由美元金额推算汇率"})
                continue
        oid = e.get("order_id") or order_id
        fid = e.get("fill_id") or fill_id
        o = None
        try:
            planned = amount_aud
            if st == "partial":
                ob_m = month_obligation(cfg, L, mk, fx, et_now)["symbols"][sym]
                planned = round(float(e.get("planned_aud") or ob_m["required_aud"]
                                      or plan_for(cfg, sym, rate, month=mk, ledger=L).aud), 2)
                planned = max(planned, amount_aud)
            o = L.create_order(sym, planned, planned_amount_usd=(planned * rate),
                               status="placed", signal_date=et_now.date().isoformat(),
                               obligation_months=months, fx_rate=rate,
                               fx_source=(e.get("fx_source") or fx.status),
                               order_id=oid, note=e.get("note") or note)
            oid = o["order_id"]
            if st == "placed":
                recorded.append({"symbol": sym, "status": "placed", "order_id": oid,
                                 "amount_aud": planned,
                                 "amount_usd": round(planned * rate, 2),
                                 "price": px or None, "trade_date": td, "fill_id": ""})
                continue
            fill_amt = amount_aud
            if st == "partial":
                fill_amt = float(e.get("filled_aud") or filled_aud or amount_aud)
                if fill_amt <= 0 or fill_amt > planned + 0.5:
                    raise LedgerError("partial 的 filled_aud 必须在 (0, planned] 内")
            fee = commission_usd(cfg, fill_amt * rate, px)
            usd = float(explicit_usd) if explicit_usd not in (None, "") else fill_amt * rate - fee
            f = L.add_fill(oid, amount_aud=fill_amt, amount_usd=usd,
                           price_usd=px, trade_date=td, fx_rate=rate,
                           commission_usd=fee, fill_id=fid,
                           note=e.get("note") or note)
            recorded.append({"symbol": sym, "status": "filled", "order_id": oid,
                             "fill_id": f["fill_id"], "amount_aud": fill_amt,
                             "amount_usd": round(usd, 2), "price": px,
                             "trade_date": td, "commission_usd": fee,
                             "fx_rate": rate})
        except (IdempotencyConflict, OverfillError, LedgerError) as exc:
            skipped.append({"symbol": sym, "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            skipped.append({"symbol": sym, "reason": f"写入失败：{exc}"})
    return recorded, skipped


def cmd_confirm(cfg, entries, amount_fallback=None, note="", **kwargs):
    et_now = datetime.now(ET_TZ)
    fx = fxmod.fetch_fx(cfg, yahoo_json)
    prepared = []
    for sym, amount in entries:
        prepared.append({"symbol": sym,
                         "amount": amount if amount is not None else amount_fallback,
                         "price": kwargs.get("price"),
                         "trade_date": kwargs.get("trade_date"),
                         "status": kwargs.get("status") or "filled",
                         "amount_usd": kwargs.get("amount_usd"),
                         "fx_rate": kwargs.get("fx_rate"),
                         "fill_id": kwargs.get("fill_id"),
                         "order_id": kwargs.get("order_id"),
                         "filled_aud": kwargs.get("filled_aud"),
                         "months": kwargs.get("months")})
    recorded, skipped = record_confirmations(
        cfg, prepared, note, et_now, status=kwargs.get("status") or "filled",
        trade_date=kwargs.get("trade_date"), price=kwargs.get("price"),
        amount_usd=kwargs.get("amount_usd"), fx_rate=kwargs.get("fx_rate"),
        fill_id=kwargs.get("fill_id"), order_id=kwargs.get("order_id"),
        filled_aud=kwargs.get("filled_aud"))
    for r in recorded:
        if r.get("status") == "cancelled":
            print(f"已取消 {r['symbol']} order {r['order_id']}")
        elif r.get("fill_id"):
            print(f"已记录 {r['symbol']}：{r['trade_date']} @ US${float(r['price']):.2f}，"
                  f"A${float(r['amount_aud']):,.2f} / US${float(r['amount_usd']):,.2f}"
                  f"（order {r['order_id']} / fill {r['fill_id']}）")
        else:
            print(f"已挂单 {r['symbol']}：A${float(r['amount_aud']):,.2f}"
                  f"（order {r['order_id']}）")
    for s in skipped:
        print(f"跳过 {s['symbol']}：{s['reason']}")
    if not fx.firm and recorded:
        print(f"提示：{fx.note}")
    return recorded


def _fetch_item_prices(cfg, sym, smeta, today, price_mode):
    """返回 (daily_hist, price, prev_close, quote_state, live, mode, open_price)."""
    daily, _ = fetch_daily_cached(cfg, sym, smeta)
    in_today = [r for r in daily if r["date"] == today]
    hist = [r for r in daily if r["date"] < today]
    if price_mode == "close" and in_today:
        row = in_today[-1]
        prev = hist[-1]["close"] if hist else row["close"]
        live = {"state": "CLOSED", "provider": "daily_close",
                "last_bar": None, "day_high": row.get("high"),
                "day_low": row.get("low"), "volume": row.get("volume"),
                "pre": None, "post": None}
        return hist, float(row["close"]), float(prev), "CLOSED", live, "close", row
    live = fetch_live(cfg, sym)
    state = (live["state"] or "").upper()
    price = float(live["price"])
    prev_close = float(live["prev_close"] or live["price"])
    if state == "PRE" and live.get("pre"):
        price = float(live["pre"])
    elif state in ("POST", "CLOSED") and live.get("post"):
        price = float(live["post"])
    return hist, price, prev_close, state, live, "live", None


def collect(cfg, et_now, session=None, session_date=None, price_mode="live"):
    today = session_date or et_now.date()
    mk = month_key(today)
    clock = market_clock(cfg, et_now, today)
    session = session or infer_session(et_now)
    fx = fxmod.fetch_fx(cfg, yahoo_json)
    if not clock["calendar_supported"]:
        return {
            "et_now": et_now, "today": today, "session": session,
            "session_label": "日历不支持（不给交易建议）",
            "clock": clock, "calendar_source": clock["calendar_source"],
            "calendar_warning": clock["calendar_warning"],
            "early_close": False, "after_close": True,
            "trading_today": False, "day_idx": 0, "days_left": 0,
            "days_left_after": 0, "total_days": 0,
            "last_trading_day": None, "month": mk,
            "vix": 20.0, "vix_chg5": None, "vix_source": "default",
            "items": [], "errors": [clock["calendar_warning"]],
            "ledger": [], "ledger_obj": None,
            "state": "UNSUPPORTED", "portfolio": cfg["portfolio"],
            "fx": fx.to_dict(), "currency": cfg.get("currency", {}),
            "obligation": None, "obligation_summary": None,
        }

    L = get_ledger(cfg, et_now)
    obligation = month_obligation(cfg, L, mk, fx, et_now)
    vix, vix_hist, vix_src = fetch_vix(cfg)
    vix_chg5 = perf(vix_hist, 5) if vix_hist is not None and len(vix_hist) > 6 else None

    items, errors = [], []
    for sym, smeta in cfg["symbols"].items():
        try:
            daily_hist, price, prev_close, quote_state, live, mode, row_today = _fetch_item_prices(
                cfg, sym, smeta, today, price_mode)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")
            continue
        if len(daily_hist) < 60:
            errors.append(f"{sym}: 历史数据不足")
            continue
        closes = np.array([r["close"] for r in daily_hist], dtype=float)
        closes_eff = np.append(closes.copy(), price)
        chg = price / prev_close - 1.0 if prev_close else 0.0
        metrics = score_components(price, closes_eff, vix, cfg)
        style = smeta.get("style", "index")
        style_cfg = cfg["styles"][style]
        plan = plan_for(cfg, sym, fx.rate, fx.source, fx.as_of_et, fx.firm,
                        month=mk, ledger=L)
        ob = obligation["symbols"][sym]
        usd_for_fee = plan.usd or 0.0
        comm = commission_usd(cfg, usd_for_fee, price) if usd_for_fee else 0.0
        comm_pct = (comm / usd_for_fee) if usd_for_fee else 0.0
        batch = None
        if (cfg.get("commission") or {}).get("policy") == "batch":
            batch = suggest_batch(cfg, usd_for_fee, price)
            if batch:
                batch["amount_usd"] = batch["amount"]
                batch["amount_aud"] = round(batch["amount"] / fx.rate, 2) if fx.rate else None
                batch["amount"] = batch["amount_aud"]
        timing = {
            "can_fill_now": clock["can_fill_now"],
            "can_place_order": clock["can_place_order"],
            "next_fill_et": clock["next_fill_et"],
            "next_fill_local": clock["next_fill_local"],
            "session_phase": clock["session_phase"],
            "execution_mode": "live",
            "fx_firm": fx.firm,
        }
        code, text, recommend_aud, ratio, reasons = decide(
            metrics["score"], style_cfg, clock["day_idx"],
            clock["days_left_after_today"], ob, plan, timing, cfg)
        recommend_usd = (round(recommend_aud * fx.rate, 2)
                         if fx.rate and recommend_aud else 0.0)
        last60 = closes_eff[-60:]
        trigger = float(np.percentile(last60, 35))
        filled_aud = ob["filled_aud"]
        invested = ob["status"] == "PAID"
        items.append({
            "symbol": sym, "name": smeta.get("name", sym), "style": style,
            "style_label": style_cfg["label"], "style_why": style_cfg.get("why", ""),
            "price": price, "change_pct": chg, "state": quote_state,
            "quote_state": quote_state, "session_phase": clock["session_phase"],
            "day_high": live.get("day_high"), "day_low": live.get("day_low"),
            "prev_close": prev_close,
            "last_bar": live["last_bar"].strftime("%H:%M ET") if live.get("last_bar") else "",
            "provider": live.get("provider", "?"),
            "price_mode": mode,
            "metrics": metrics, "score": metrics["score"],
            "verdict": code, "verdict_text": text,
            "ratio": ratio, "recommend_aud": round(recommend_aud, 2),
            "recommend_usd": recommend_usd, "reasons": reasons,
            "trigger": trigger,
            "plan": plan.as_dict(), "plan_aud": plan.aud, "plan_usd": plan.usd,
            "executed_aud": filled_aud,
            "executed_usd": round(filled_aud * fx.rate, 2) if fx.rate else None,
            "pending_aud": ob["pending_aud"], "remaining_aud": ob["remaining_aud"],
            "invested": invested,
            "obligation": ob,
            "commission_usd": comm, "commission_pct": round(comm_pct, 6),
            "batch": batch,
            "perf_1w": perf(closes_eff, 5), "perf_1m": perf(closes_eff, 21),
            "perf_3m": perf(closes_eff, 63), "perf_1y": perf(closes_eff, 252),
            "volume_ratio": volume_ratio(live, daily_hist, et_now, quote_state)
            if mode == "live" else None,
        })

    session_label = cfg["sessions"][session]["label"]
    if clock["session_phase"] == "POST":
        session_label += "（收盘后复核）"
    elif clock["session_phase"] == "PRE":
        session_label = "盘前复核（上一收盘数据）"
    elif clock["session_phase"] == "HOLIDAY":
        session_label = "休市"
    if clock.get("warning"):
        pass

    return {
        "et_now": et_now, "today": today, "session": session,
        "session_label": session_label,
        "clock": clock, "calendar_source": clock["calendar_source"],
        "calendar_warning": clock.get("calendar_warning", ""),
        "early_close": clock["early_close"], "after_close": clock["session_phase"] == "POST",
        "trading_today": clock["trading_today"],
        "day_idx": clock["day_idx"],
        "days_left": clock["days_left_incl_today"],
        "days_left_after": clock["days_left_after_today"],
        "total_days": clock["total_days"],
        "last_trading_day": clock["last_trading_day"],
        "month": mk, "vix": vix, "vix_chg5": vix_chg5, "vix_source": vix_src,
        "items": items, "errors": errors,
        "ledger": load_ledger(cfg), "ledger_obj": L,
        "state": clock["session_phase"],
        "portfolio": cfg["portfolio"],
        "fx": fx.to_dict(), "currency": cfg.get("currency", {}),
        "obligation": obligation,
        "obligation_summary": obligation["total"],
    }


def contribution_weights(cfg, ledger):
    """按台账累计实际成交（AUD）计算贡献权重。"""
    sums = {s: 0.0 for s in cfg["symbols"]}
    if ledger is not None and hasattr(ledger, "contribution_by_symbol"):
        raw = ledger.contribution_by_symbol()
    else:
        raw = {}
        for r in (ledger or []):
            sym = r.get("symbol")
            if sym in sums:
                raw[sym] = raw.get(sym, 0.0) + float(r.get("amount_aud", r.get("amount")) or 0)
    for s, v in raw.items():
        if s in sums:
            sums[s] = float(v)
    total = sum(sums.values())
    if total <= 0:
        return None
    return {s: v / total for s, v in sums.items()}, sums, total


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def money_aud(v):
    return "—" if v is None else f"A${v:,.2f}"


def money_usd(v):
    return "—" if v is None else f"US${v:,.2f}"


def money(v):
    """旧接口：默认按 USD 展示。"""
    return money_usd(v)


def pct(v, digits=1):
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def pct_plain(v, digits=1):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _when_text(clock):
    nxt = clock.get("next_fill_et")
    loc = clock.get("next_fill_local")
    if nxt:
        t = f"美东 {nxt.strftime('%m/%d %H:%M')}"
        if loc:
            t += f" = 墨尔本 {loc.strftime('%m/%d %H:%M')}"
        return t
    return "下一交易时段（时间未知）"


def _status_cn(status):
    return {"PAID": "已完成", "PENDING": "已挂单", "OVERDUE": "逾期补投",
            "DUE": "待执行", "BATCH": "合并中"}.get(status, status or "—")


def _fx_text(ctx):
    fx = ctx.get("fx") or {}
    rate = fx.get("rate")
    if rate is None:
        return "FX 不可用（仅展示 AUD 义务，不给出确定 USD 下单金额）"
    t = (f"1 AUD = {float(rate):.4f} USD（{fx.get('source') or '?'}"
         f"，{fx.get('as_of_et') or '时间未知'}）")
    if not fx.get("firm"):
        t += "｜**估算，不可作为确定下单金额**"
    return t


def build_report(ctx, cfg):
    et = ctx["et_now"]
    local = et.astimezone(LOCAL_TZ)
    clock = ctx["clock"]
    items = ctx["items"]
    today = ctx["today"]
    wd = "一二三四五六日"[today.weekday()]
    if not clock.get("calendar_supported"):
        return (f"# 美股定投雷达 · {today.isoformat()}（周{wd}）\n\n"
                f"> 生成：美东 {et.strftime('%Y-%m-%d %H:%M')} ｜ 墨尔本 {local.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"**日历不支持，停止给出交易建议。**\n\n- {clock.get('calendar_warning')}\n"
                "- 你仍可在面板/CLI 手工记录成交；系统会按 AUD 义务记账。\n")

    fx = ctx.get("fx") or {}
    ob = ctx.get("obligation") or {"symbols": {}, "total": {}}
    total = ob.get("total") or {}
    budget = float(cfg["portfolio"]["total_monthly_aud"])
    lines = [(
        f"# 美股定投雷达 · {today.isoformat()}（周{wd}）· {ctx['session_label']}\n\n"
        f"> 生成：美东 {et.strftime('%Y-%m-%d %H:%M')} ｜ 墨尔本 {local.strftime('%Y-%m-%d %H:%M')} ({local.tzname()})\n"
        f"> 行情：{market_state_cn(clock)} ｜ VIX {ctx['vix']:.1f}"
        + (f"（5日 {pct(ctx['vix_chg5'])}）" if ctx["vix_chg5"] is not None else "")
        + f" ｜ 本月第 {ctx['day_idx']}/{ctx['total_days']} 个交易日；"
        f"今日收盘后还剩 {ctx['days_left_after']} 个（最后 {ctx['last_trading_day'].strftime('%m/%d')}）\n"
        f"> 预算 A${budget:,.0f}/月 ｜ {_fx_text(ctx)}\n"
        f"> 日历：{ctx.get('calendar_source')}"
        + (f"（{ctx.get('calendar_warning')}）" if ctx.get("calendar_warning") else "")
        + "\n\n")]

    holiday = not ctx["trading_today"]
    if holiday:
        lines.append(f"**今天美股休市，不产生新的定投建议。** 下一个交易日：{_when_text(clock)}。\n\n")

    # 1) 硬性义务
    lines.append("## 一、本月硬性义务（必须完成）\n")
    lines.append("| 标的 | 基础 A$ | 历史欠额 A$ | 应投 A$ | 已成交 A$ | 挂单 A$ | 缺口 A$ | 折合 US$ | 状态 | 最迟 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for sym in cfg["symbols"]:
        v = ob["symbols"].get(sym)
        if not v:
            continue
        usd = v.get("remaining_usd")
        lines.append(
            f"| **{sym}** | {money_aud(v['base_aud'])} | {money_aud(v['arrears_aud'])} | "
            f"{money_aud(v['required_aud'])} | {money_aud(v['filled_aud'])} | "
            f"{money_aud(v['pending_aud'])} | {money_aud(v['remaining_aud'])} | "
            f"{money_usd(usd) if usd is not None else '—'} | {_status_cn(v['status'])} | "
            f"{ctx['last_trading_day'].strftime('%m/%d')} |")
    lines.append(
        f"| **合计** | {money_aud(total.get('base_aud'))} | {money_aud(total.get('arrears_aud'))} | "
        f"{money_aud(total.get('required_aud'))} | {money_aud(total.get('filled_aud'))} | "
        f"{money_aud(total.get('pending_aud'))} | {money_aud(total.get('remaining_aud'))} | "
        f"{money_usd(total.get('remaining_usd')) if total.get('remaining_usd') is not None else '—'} | "
        f"— | — |")
    lines.append("")
    if total.get("arrears_aud", 0) > 0:
        lines.append("> 历史欠额永久保留，不会因超过月数自动豁免；只有显式 `--write-off` 才会核销并留痕。\n")
    if fx.get("rate") is None or not fx.get("firm"):
        lines.append("> **汇率非确定值**：AUD 义务确定；US$ 金额仅为估算，"
                     "确认成交时必须提供实际汇率或实际美元金额。\n")

    # 2) 动作
    lines.append("## 二、本时段 / 下一时段动作\n")
    if not items:
        lines.append("没有可用的行情数据，无法给出动作。\n")
    else:
        action_label = "今日可成交" if clock["can_fill_now"] else "下一交易时段"
        lines.append(f"**成交状态：{market_state_cn(clock)}**；{action_label}：{_when_text(clock)}\n")
        lines.append("| 标的 | 现价 | 今日 | 评分 | 建议 | 建议 A$ | 建议 US$ |")
        lines.append("|---|---|---|---|---|---|---|")
        for it in items:
            short = "—（休市）" if holiday else VERDICT_SHORT.get(it["verdict"], it["verdict"])
            lines.append(
                f"| **{it['symbol']}** | {money_usd(it['price'])} | {pct(it['change_pct'])} | "
                f"{it['score']:.0f} | {short} | {money_aud(it['recommend_aud'])} | "
                f"{money_usd(it['recommend_usd']) if fx.get('firm') else '估算' } |")
        lines.append("")
        buys = [it["symbol"] for it in items if it["verdict"] in ("BUY", "BUY_NOW")]
        waits = [it["symbol"] for it in items if it["verdict"] == "WAIT"]
        done = [it["symbol"] for it in items if it["verdict"] == "DONE"]
        pending = [it["symbol"] for it in items if it["verdict"] == "PENDING"]
        review = [it["symbol"] for it in items if it["verdict"] == "REVIEW_FX"]
        parts = []
        if total.get("remaining_aud", 0) > 0:
            parts.append(f"硬性义务还差 **A${total['remaining_aud']:,.2f}**"
                         + (f"（≈ US${total['remaining_usd']:,.2f}）" if total.get("remaining_usd") else ""))
        if buys:
            parts.append(("今天" if clock["can_fill_now"] else "下一交易时段") + f"执行：{'、'.join(buys)}")
        if walks := waits:
            parts.append(f"继续等：{'、'.join(walks)}")
        if pending:
            parts.append(f"已挂单：{'、'.join(pending)}")
        if review:
            parts.append(f"待确认汇率：{'、'.join(review)}")
        if done:
            parts.append(f"已完成：{'、'.join(done)}")
        lines.append("**一句话**：" + ("；".join(parts) if parts else "本月义务已完成，无需操作。") + "\n")

    # 3) 逐标的
    lines.append("## 三、逐标的明细\n")
    for it in items:
        m = it["metrics"]
        v = it["obligation"]
        lines.append(f"### {it['symbol']} · {it['name']}（{it['style_label']}）\n")
        lines.append(f"- 现价 **{money_usd(it['price'])}** ｜ 今日 {pct(it['change_pct'])} ｜ 日内 "
                     f"{money_usd(it['day_high'])}/{money_usd(it['day_low'])} ｜ 昨收 {money_usd(it['prev_close'])}")
        lines.append(f"- 52周位置 {m['pos52'] * 100:.0f}% ｜ 距52周高点 {pct(m['dd52'])} ｜ "
                     f"60日分位 {m['rank60'] * 100:.0f}% ｜ RSI14 {m['rsi']:.0f}")
        lines.append(f"- MA20 {money_usd(m['ma20'])}（{pct(it['price'] / m['ma20'] - 1, 2)}）｜ "
                     f"MA50 {money_usd(m['ma50'])} ｜ MA200 {money_usd(m['ma200'])}")
        lines.append(f"- 近1周 {pct(it['perf_1w'])} ｜ 近1月 {pct(it['perf_1m'])} ｜ "
                     f"近3月 {pct(it['perf_3m'])} ｜ 近1年 {pct(it['perf_1y'])}"
                     + (f" ｜ 量能 {it['volume_ratio']:.2f}x" if it["volume_ratio"] else ""))
        lines.append(f"- 分项：位置 {m['s52']:.0f} / 分位 {m['s60']:.0f} / 乖离 {m['sz20']:.0f} / "
                     f"RSI {m['srsi']:.0f} / 回撤 {m['sdd']:.0f} / VIX {m['svix']:.0f} "
                     f"→ **评分 {m['score']:.0f}/100**（评分只做辅助择时）")
        lines.append(f"- 本月义务：基础 {money_aud(v['base_aud'])} + 欠额 {money_aud(v['arrears_aud'])} "
                     f"= 应投 **{money_aud(v['required_aud'])}**；已成交 {money_aud(v['filled_aud'])}，"
                     f"挂单 {money_aud(v['pending_aud'])}，缺口 **{money_aud(v['remaining_aud'])}**")
        if v.get("earliest_arrears_month"):
            lines.append(f"- 最早欠额月份：{v['earliest_arrears_month']}；欠额明细见附录/JSON。")
        lines.append("")
        if holiday:
            lines.append("**建议：休市，不操作。** 以上为收盘数据，供下一交易时段参考。")
        else:
            lines.append(f"**建议：{it['verdict_text']}**")
        for r in it["reasons"]:
            lines.append(f"- {r}")
        if not holiday and it["verdict"] == "WAIT":
            lines.append(f"- 触发线参考：{money_usd(it['trigger'])}（近60日第35百分位）；"
                         f"最迟 {ctx['last_trading_day'].strftime('%m/%d')} 前必须完成。")
        if it["recommend_aud"] > 0:
            lines.append(f"- 本次建议金额：**{money_aud(it['recommend_aud'])}**"
                         + (f"（≈ US${it['recommend_usd']:,.2f}）" if fx.get("firm") else "（US$ 为估算）"))
        if it["plan_aud"]:
            lines.append(f"- 月度计划：A${it['plan_aud']:,.2f}"
                         + (f" / US${it['plan_usd']:,.2f}" if it.get("plan_usd") else ""))
        if it["plan_usd"]:
            lines.append(f"- 单笔佣金约 {money_usd(it['commission_usd'])}"
                         f"（占 {it['commission_pct'] * 100:.2f}%）")
        if it.get("batch"):
            b = it["batch"]
            lines.append(f"- 合并参考：单笔佣金占比偏高；如要合并，用 "
                         f"`--confirm {it['symbol']}:{b.get('amount')} --months ...` 手工执行，"
                         f"系统只记录真实成交日（佣金估算 {b['pct'] * 100:.2f}%）。")
        lines.append("")

    # 4) 组合与台账
    lines.append("## 四、组合与台账\n")
    lines.append("| 标的 | 目标权重 | 本月计划 A$ | 本月已成交 A$ | 累计贡献占比 |")
    lines.append("|---|---|---|---|---|")
    L = ctx.get("ledger_obj")
    cw = contribution_weights(cfg, L) if L is not None else None
    for it in items:
        target = float(cfg["portfolio"].get("weights", {}).get(it["symbol"], 0))
        actual = f"{cw[0][it['symbol']] * 100:.1f}%" if cw else "—"
        lines.append(f"| {it['symbol']} | {target * 100:.0f}% | {money_aud(it['plan_aud'])} | "
                     f"{money_aud(it['executed_aud'])} | {actual} |")
    lines.append("")
    if cw:
        drift = [(s, cw[0][s] - float(cfg["portfolio"].get("weights", {}).get(s, 0)))
                 for s in cw[0]]
        big = [(s, d) for s, d in drift if abs(d) > 0.05]
        if big:
            lines.append("> 权重偏离提示：" + "；".join(
                f"{s} 实际 {cw[0][s] * 100:.1f}% vs 目标 "
                f"{float(cfg['portfolio']['weights'].get(s, 0)) * 100:.0f}%"
                for s, _ in big) + "。累计贡献口径，建议每年检查。")
        else:
            lines.append("> 权重检查：累计贡献占比与目标权重偏离均在 5 个百分点以内。")
        lines.append("")
    if L is not None:
        fills = L.month_fills(ctx["month"])
        alloc = L.month_allocated_by_symbol(ctx["month"])
        if fills:
            lines.append("### 本月真实成交（按 trade_date）\n")
            lines.append("| 成交日 | 标的 | 状态 | 金额 A$ | 金额 US$ | 成交价 | 汇率 | fill_id |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for f in fills:
                lines.append(f"| {f['trade_date']} | {f['symbol']} | filled | "
                             f"{money_aud(f['amount_aud'])} | {money_usd(f['amount_usd'])} | "
                             f"{money_usd(f['price_usd'])} | {f['fx_rate']:.4f} | {f['fill_id'][:8]}… |")
            lines.append("")
            lines.append("### 本月义务覆盖（按 obligation_month）\n")
            lines.append("| 标的 | 义务覆盖 A$ |")
            lines.append("|---|---|")
            for s in cfg["symbols"]:
                lines.append(f"| {s} | {money_aud(alloc.get(s, 0))} |")
            lines.append("")
        else:
            amt = total.get("remaining_aud") or 0
            lines.append(f"本月尚未记录真实成交。可执行后运行："
                         f"`python3 scripts/scan.py --confirm VOO:{amt:.0f}`（金额按实际成交调整）\n")
        orders = [o for o in L.all_orders() if o["status"] in ("placed", "partial")]
        if orders:
            lines.append("### 未完成订单（pending，不计入已成交）\n")
            lines.append("| 订单 | 标的 | 状态 | 计划 A$ | 已成交 A$ | order_id |")
            lines.append("|---|---|---|---|---|---|")
            for o in orders:
                filled = sum(float(f["amount_aud"]) for f in L.fills_for_order(o["order_id"]))
                lines.append(f"| {o['status']} | {o['symbol']} | {o['status']} | "
                             f"{money_aud(o['planned_amount_aud'])} | {money_aud(filled)} | "
                             f"{o['order_id'][:8]}… |")
            lines.append("")

    # 5) 执行提示
    lines.append("## 五、执行提示\n")
    if holiday:
        lines.append(f"- 今天美股休市，无交易提示。下一个交易日：{_when_text(clock)}。")
    elif clock["session_phase"] == "POST":
        lines.append(f"- 美股今日已收盘（{'提前收盘 13:00' if clock.get('early_close') else '常规 16:00'}），"
                     f"无法再成交。下一交易时段：{_when_text(clock)}。")
    elif clock["session_phase"] == "PRE":
        lines.append(f"- 美股盘前，尚未开盘。可挂开盘单；开盘：{_when_text(clock)}。")
    elif clock["session_phase"] == "REGULAR":
        if ctx["session"] == "open":
            lines.append("- 开盘 30 分钟内噪音大、点差宽；执行建议等 10:00 ET 后用限价单。")
        elif ctx["session"] == "midday":
            lines.append("- 午盘流动性尚可，适合限价下单；别追当日已走出的方向。")
        else:
            lines.append("- 距收盘约 10 分钟：决定执行就现在下市价/MOC 单，否则顺延下一交易日。")
    lines.append(f"- 美股开盘 = 墨尔本 {et_open(today).astimezone(LOCAL_TZ).strftime('%H:%M')}"
                 "（夏令时切换整体平移 1 小时）")
    lines.append("- 数据：Yahoo Finance（curl）/ Nasdaq，可能延时；本报告不构成投资建议。")
    if FETCH_NOTES:
        lines.append("- 数据备注：" + "；".join(dict.fromkeys(FETCH_NOTES)))
    if ctx.get("errors"):
        lines.append("- 数据异常：" + "；".join(ctx["errors"]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 收盘日报（墨尔本早上 08:15 推送）
# ---------------------------------------------------------------------------
def build_digest(cfg, et_now):
    today_et = et_now.date()
    if not CALENDAR.supports(today_et):
        local0 = et_now.astimezone(LOCAL_TZ)
        text = (f"# 美股定投日报 · 日历不支持\n\n"
                f"> {local0.strftime('%Y-%m-%d %H:%M')} 墨尔本\n\n"
                f"{today_et.isoformat()} 超出 NYSE 日历支持范围；不给交易建议。"
                "请安装/升级 exchange_calendars 或扩展内置表。\n")
        return text, today_et, None

    if not CALENDAR.is_trading_day(today_et):
        nxt = CALENDAR.next_trading_day(today_et)
        nxt_local = et_open(nxt).astimezone(LOCAL_TZ)
        local0 = et_now.astimezone(LOCAL_TZ)
        text = (f"# 美股定投日报 · 昨夜休市\n\n"
                f"> {local0.strftime('%Y-%m-%d %H:%M')} 墨尔本\n\n"
                f"美东 {today_et.strftime('%m/%d')} 休市，没有新的收盘数据。"
                f"下一个交易日：美东 {nxt.strftime('%m/%d')} 09:30 = "
                f"墨尔本 {nxt_local.strftime('%m/%d %H:%M')}。本月进度不变。\n")
        return text, today_et, None

    close_et = et_close(today_et)
    sess_date = today_et if et_now >= close_et else CALENDAR.prev_trading_day(today_et)
    ctx = collect(cfg, et_now, "close", session_date=sess_date, price_mode="close")
    if not ctx.get("clock", {}).get("calendar_supported", True):
        return (f"# 美股定投日报 · 日历不支持\n\n{ctx.get('calendar_warning')}\n",
                sess_date, None)

    signals = read_signals(cfg)
    sess_rows = {}
    for r in signals:
        if r.get("et_datetime", "").startswith(sess_date.isoformat()):
            sess_rows.setdefault(r["symbol"], {})[r["session"]] = r

    local = et_now.astimezone(LOCAL_TZ)
    wd = "一二三四五六日"[sess_date.weekday()]
    next_open_et = et_open(CALENDAR.next_trading_day(sess_date))
    next_open_local = next_open_et.astimezone(LOCAL_TZ)
    fx = ctx.get("fx") or {}
    ob = ctx.get("obligation") or {"symbols": {}, "total": {}}
    total = ob.get("total") or {}

    lines = [f"# 美股定投日报 · 美东 {sess_date.isoformat()}（周{wd}）收盘\n\n"
             f"> 生成：{local.strftime('%Y-%m-%d %H:%M')} 墨尔本 ｜ "
             f"覆盖美东 {sess_date.strftime('%m/%d')} ｜ VIX {ctx['vix']:.1f}\n"
             f"> 预算 A${float(cfg['portfolio']['total_monthly_aud']):,.0f}/月 ｜ {_fx_text(ctx)}\n"
             f"> 本月第 {ctx['day_idx']}/{ctx['total_days']} 个交易日；收盘后还剩 "
             f"{ctx['days_left_after']} 个（最后 {ctx['last_trading_day'].strftime('%m/%d')}）\n\n"]

    lines.append("## 一、昨晚三个时点\n")
    lines.append("| 标的 | 开盘 | 午盘 | 收盘 | 走势 |")
    lines.append("|---|---|---|---|---|")
    missing = []
    for it in ctx["items"]:
        sym = it["symbol"]
        row = sess_rows.get(sym, {})
        def cell(sess):
            r = row.get(sess)
            return None if not r else float(r["score"])
        close_score = cell("close")
        if close_score is None:
            close_score = it["score"]
            missing.append("收盘")
        series = [cell("open"), cell("midday"), close_score]
        vals = ["—" if v is None else f"{v:.0f}分" for v in series]
        known = [v for v in series if v is not None]
        trend = "—"
        if len(known) >= 2:
            trend = "改善" if known[-1] - known[0] > 3 else ("转弱" if known[-1] - known[0] < -3 else "持平")
        lines.append(f"| **{sym}** | {vals[0]} | {vals[1]} | {vals[2]} | {trend} |")
    lines.append("")
    gaps = []
    if not any("open" in r for r in sess_rows.values()):
        gaps.append("开盘")
    if not any("midday" in r for r in sess_rows.values()):
        gaps.append("午盘")
    if gaps or missing:
        lines.append(f"> 快照缺失：{'、'.join(dict.fromkeys(gaps + missing))}（可能是 App 当时未运行）；"
                     "缺失时点已用收盘数据补采。\n")

    lines.append("## 二、硬性义务与下一次下单\n")
    lines.append("| 标的 | 应投 A$ | 已成交 A$ | 挂单 A$ | 缺口 A$ | 状态 | 下一次动作 |")
    lines.append("|---|---|---|---|---|---|---|")
    for it in ctx["items"]:
        v = it["obligation"]
        if it["verdict"] in ("BUY", "BUY_NOW"):
            act = f"下一时段定投 {money_aud(it['recommend_aud'])}"
        elif it["verdict"] == "WAIT":
            act = f"等待触发；最迟 {ctx['last_trading_day'].strftime('%m/%d')}"
        elif it["verdict"] == "PENDING":
            act = "已挂单，等待成交"
        elif it["verdict"] == "REVIEW_FX":
            act = "待确认汇率后下单"
        elif it["verdict"] == "DONE":
            act = "本月已完成"
        else:
            act = it["verdict_text"]
        lines.append(f"| **{it['symbol']}** | {money_aud(v['required_aud'])} | "
                     f"{money_aud(v['filled_aud'])} | {money_aud(v['pending_aud'])} | "
                     f"{money_aud(v['remaining_aud'])} | {_status_cn(v['status'])} | {act} |")
    lines.append("")
    if total.get("remaining_aud", 0) > 0:
        lines.append(f"**本月硬性义务还差 A${total['remaining_aud']:,.2f}"
                     + (f"（≈ US${total['remaining_usd']:,.2f}）" if total.get("remaining_usd") else "")
                     + "**；欠额永久保留。\n")

    lines.append("## 三、下单窗口\n")
    lines.append(f"美股下一次开盘：**美东 {next_open_et.strftime('%m/%d')} 09:30** "
                 f"= 墨尔本 **{next_open_local.strftime('%m/%d')} {next_open_local.strftime('%H:%M')}**。")
    lines.append("美股收盘后无法成交：请在墨尔本白天挂**限价单/次日开盘单**，"
                 "或等下一个交易时段。执行后记得用 `--confirm` 记录真实成交。\n")

    lines.append("## 四、本月进度\n")
    lines.append("| 标的 | 计划 A$ | 已成交 A$ | 挂单 A$ | 状态 |")
    lines.append("|---|---|---|---|---|")
    for it in ctx["items"]:
        v = it["obligation"]
        lines.append(f"| {it['symbol']} | {money_aud(v['required_aud'])} | "
                     f"{money_aud(v['filled_aud'])} | {money_aud(v['pending_aud'])} | {_status_cn(v['status'])} |")
    lines.append("")

    mdays = CALENDAR.month_trading_days(sess_date)
    if sess_date == mdays[0]:
        lines.append("## 五、月初提醒\n")
        lines.append("- 本月配置：" + "、".join(
            f"{it['symbol']} {float(cfg['portfolio']['weights'].get(it['symbol'], 0)) * 100:.0f}%"
            f"（A${it['plan_aud']:,.0f}）" for it in ctx["items"]))
        lines.append("- 每年检查：权重偏离、TSLA/NVDA 集中度、预算版本与 FX 口径。\n")

    if FETCH_NOTES or ctx.get("errors"):
        lines.append("## 数据备注\n")
        for n in dict.fromkeys(FETCH_NOTES):
            lines.append(f"- {n}")
        for e in ctx.get("errors") or []:
            lines.append(f"- {e}")
        lines.append("")
    return "\n".join(lines), sess_date, ctx


def read_signals(cfg):
    path = os.path.join(BASE, cfg["data_dir"], "signals.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


SIGNAL_FIELDS = [
    "et_datetime", "session", "symbol", "price", "change_pct", "score",
    "verdict", "days_left", "vix", "can_fill_now", "phase", "fx_rate",
    "plan_aud", "plan_usd", "obligation_status",
]


def _migrate_signals(path, cfg):
    rows = []
    old_fields = None
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            rd = csv.DictReader(f)
            old_fields = rd.fieldnames or []
            rows = list(rd)
    if old_fields != SIGNAL_FIELDS:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SIGNAL_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in SIGNAL_FIELDS})
        os.replace(tmp, path)
    return rows


def save_report(ctx, cfg, text, name_override=None):
    reports_dir = os.path.join(BASE, cfg["reports_dir"])
    data_dir = os.path.join(BASE, cfg["data_dir"])
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    name = name_override or f"{ctx['today'].isoformat()}_{ctx['session']}.md"
    path = os.path.join(reports_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if not name_override:
        with open(os.path.join(reports_dir, "latest.md"), "w", encoding="utf-8") as f:
            f.write(text)

    sig_path = os.path.join(data_dir, "signals.csv")
    rows = _migrate_signals(sig_path, cfg)
    et = ctx["et_now"]
    key = (et.strftime("%Y-%m-%d %H:%M"), ctx["session"])
    rows = [r for r in rows if (r.get("et_datetime"), r.get("session")) != key]
    for it in ctx.get("items") or []:
        rows.append({
            "et_datetime": et.strftime("%Y-%m-%d %H:%M"),
            "session": ctx["session"], "symbol": it["symbol"],
            "price": f"{it['price']:.2f}", "change_pct": f"{it['change_pct'] * 100:.2f}",
            "score": f"{it['score']:.1f}", "verdict": it["verdict"],
            "days_left": ctx["days_left"],
            "vix": f"{ctx['vix']:.2f}",
            "can_fill_now": "1" if ctx["clock"]["can_fill_now"] else "0",
            "phase": ctx["clock"]["session_phase"],
            "fx_rate": f"{ctx['fx'].get('rate') or '':.6f}" if ctx["fx"].get("rate") else "",
            "plan_aud": f"{it['plan_aud']:.2f}",
            "plan_usd": f"{it['plan_usd']:.2f}" if it.get("plan_usd") is not None else "",
            "obligation_status": it["obligation"]["status"],
        })
    tmp = sig_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in SIGNAL_FIELDS})
    os.replace(tmp, sig_path)
    return path


def _json_clock(clock):
    out = dict(clock)
    for k in ("et_now", "local_now", "close_et", "next_fill_et", "next_fill_local"):
        v = out.get(k)
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    if out.get("et_date"):
        out["et_date"] = out["et_date"].isoformat()
    if out.get("last_trading_day"):
        out["last_trading_day"] = out["last_trading_day"].isoformat()
    return out


def to_json(ctx):
    clock = ctx.get("clock") or {}
    return {
        "generated_et": ctx["et_now"].isoformat(),
        "generated_local": ctx["et_now"].astimezone(LOCAL_TZ).isoformat(),
        "session": ctx["session"], "session_label": ctx["session_label"],
        "month": ctx["month"], "trading_today": ctx["trading_today"],
        "day_idx": ctx["day_idx"], "days_left": ctx["days_left"],
        "days_left_after": ctx.get("days_left_after"),
        "total_days": ctx.get("total_days"),
        "last_trading_day": (ctx["last_trading_day"].isoformat()
                             if ctx.get("last_trading_day") else None),
        "market_state": ctx["state"], "vix": round(ctx["vix"], 2),
        "vix_source": ctx["vix_source"], "vix_chg5": ctx["vix_chg5"],
        "calendar_source": ctx.get("calendar_source"),
        "calendar_warning": ctx.get("calendar_warning"),
        "clock": _json_clock(clock),
        "currency": ctx.get("currency"),
        "fx": ctx.get("fx"),
        "obligation_summary": ctx.get("obligation_summary"),
        "items": [{
            "symbol": it["symbol"], "name": it["name"], "style": it["style"],
            "price": round(it["price"], 2), "change_pct": round(it["change_pct"], 6),
            "score": it["score"], "verdict": it["verdict"],
            "verdict_text": it["verdict_text"], "ratio": round(it["ratio"], 4),
            "recommend_aud": it["recommend_aud"],
            "recommend_usd": it["recommend_usd"],
            "invested": it["invested"],
            "plan": it.get("plan"), "plan_aud": it["plan_aud"],
            "plan_usd": it["plan_usd"],
            "executed_aud": it["executed_aud"], "executed_usd": it["executed_usd"],
            "pending_aud": it["pending_aud"], "remaining_aud": it["remaining_aud"],
            "obligation": it.get("obligation"),
            "trigger": round(it["trigger"], 2),
            "fx_rate": (ctx.get("fx") or {}).get("rate"),
            "fx_firm": (ctx.get("fx") or {}).get("firm"),
            "commission_usd": it.get("commission_usd"),
            "commission_pct": it.get("commission_pct"),
            "batch": it.get("batch"),
            "metrics": {k: round(float(v), 4) for k, v in it["metrics"].items() if v is not None},
        } for it in ctx["items"]],
        "errors": ctx["errors"], "fetch_notes": list(dict.fromkeys(FETCH_NOTES)),
    }


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------
def run_backtest(cfg, quick=False, budget_aud=None, start=None, end=None):
    """委托给 scripts/backtest.py（T+1、组合估值、XIRR/回撤/净超额）。"""
    try:
        import backtest
    except Exception as exc:  # noqa: BLE001
        print(f"回测模块不可用：{exc}", file=sys.stderr)
        return 2
    try:
        return backtest.run(cfg, quick=quick, budget_aud=budget_aud,
                            start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        print(f"回测失败：{exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_et(value):
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d")
    return dt.replace(tzinfo=ET_TZ)


def _quiet_line(ctx):
    clock = ctx.get("clock") or {}
    if not clock.get("calendar_supported", True):
        return f"日历不支持，无操作：{'; '.join(ctx.get('errors') or [])}"
    if not ctx.get("trading_today"):
        nxt = next_trading_day(ctx["today"])
        return f"美股休市，无操作；下一个交易日 {nxt.strftime('%m/%d')}（美东）"
    if clock.get("session_phase") == "POST":
        return f"美股已收盘，无操作；下一交易时段 {_when_text(clock)}"
    if not ctx.get("items"):
        return f"无行情：{'; '.join(ctx.get('errors') or [])}"
    bits = [f"{it['symbol']} {it['score']:.0f}分 {VERDICT_SHORT.get(it['verdict'], it['verdict'])}"
            for it in ctx["items"]]
    total = (ctx.get("obligation_summary") or {}).get("remaining_aud")
    if total is not None:
        bits.append(f"缺口 A${total:,.0f}")
    return " | ".join(bits)


def main():
    ap = argparse.ArgumentParser(description="美股定投雷达：硬约束月度定投 + 辅助评分")
    ap.add_argument("--session", choices=["open", "midday", "close"])
    ap.add_argument("--now-et", help='指定美东时间回放，如 "2026-11-13 12:30"')
    ap.add_argument("--digest", action="store_true", help="生成收盘日报（早上推送用）")
    ap.add_argument("--confirm",
                    help="记录成交：VOO,SMH / all / VOO:1875,SMH:500（金额为 AUD）")
    ap.add_argument("--amount", type=float, help="配合 --confirm 的默认每标的 AUD 金额")
    ap.add_argument("--status", choices=["placed", "filled", "partial", "cancelled"],
                    default="filled", help="成交状态（默认 filled）")
    ap.add_argument("--trade-date", help="真实成交日 YYYY-MM-DD（可回填上月）")
    ap.add_argument("--price", type=float, help="实际成交价 USD")
    ap.add_argument("--amount-usd", type=float, help="实际美元买入金额（可覆盖汇率估算）")
    ap.add_argument("--fx-rate", type=float, help="实际 AUD/USD 汇率")
    ap.add_argument("--fill-id", help="成交唯一 ID（幂等重试用）")
    ap.add_argument("--order-id", help="订单唯一 ID（幂等重试用）")
    ap.add_argument("--filled-aud", type=float, help="partial 状态的实际成交 AUD 金额")
    ap.add_argument("--months", help="合并记账覆盖的月份，逗号分隔，如 2026-09,2026-10,2026-11")
    ap.add_argument("--note", default="")
    ap.add_argument("--write-off", action="store_true", help="显式核销欠额（必须给原因）")
    ap.add_argument("--month", help="write-off 的月份 YYYY-MM")
    ap.add_argument("--symbol", help="write-off 的标的")
    ap.add_argument("--write-off-amount", type=float, help="write-off 金额 AUD")
    ap.add_argument("--reason", help="write-off 原因")
    ap.add_argument("--export-csv", nargs="?", const=os.path.join("data", "dca_log.csv"),
                    help="导出台账 CSV（默认 data/dca_log.csv）")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--quick", action="store_true", help="回测快速模式")
    ap.add_argument("--budget-aud", type=float, help="覆盖预算（仅测试/敏感性）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只输出一行状态")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config()
        if args.budget_aud is not None:
            cfg["portfolio"]["total_monthly_aud"] = args.budget_aud
            validate_config(cfg)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    et_now = parse_et(args.now_et) if args.now_et else datetime.now(ET_TZ)

    if args.write_off:
        if not (args.month and args.symbol and args.write_off_amount and args.reason):
            print("write-off 需要 --month --symbol --write-off-amount --reason", file=sys.stderr)
            return 2
        try:
            L = get_ledger(cfg, et_now)
            wid = L.add_write_off(args.month, args.symbol, args.write_off_amount, args.reason)
            print(f"已核销 {args.symbol} {args.month} A${args.write_off_amount:,.2f}（{wid}）：{args.reason}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"核销失败：{exc}", file=sys.stderr)
            return 2

    if args.export_csv is not None:
        try:
            L = get_ledger(cfg, et_now)
            out = args.export_csv
            if not os.path.isabs(out):
                out = os.path.join(BASE, out)
            n = L.export_csv(out)
            print(f"已导出 {n} 条成交到 {os.path.relpath(out, BASE)}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"导出失败：{exc}", file=sys.stderr)
            return 2

    if args.confirm is not None:
        try:
            entries = parse_confirm_spec(cfg, args.confirm)
        except ValueError as exc:
            print(f"参数错误：{exc}", file=sys.stderr)
            return 2
        recorded = cmd_confirm(
            cfg, entries, args.amount, args.note,
            status=args.status, trade_date=args.trade_date, price=args.price,
            amount_usd=args.amount_usd, fx_rate=args.fx_rate,
            fill_id=args.fill_id, order_id=args.order_id,
            filled_aud=args.filled_aud, months=args.months)
        return 0 if recorded else 2

    if args.backtest:
        return run_backtest(cfg, quick=args.quick, budget_aud=args.budget_aud)

    if args.digest:
        text, sess_date, ctx = build_digest(cfg, et_now)
        if not args.quiet:
            print(text)
        if not args.no_write:
            reports_dir = os.path.join(BASE, cfg["reports_dir"])
            os.makedirs(reports_dir, exist_ok=True)
            path = os.path.join(reports_dir, f"{sess_date.isoformat()}_digest.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with open(os.path.join(reports_dir, "latest_digest.md"), "w", encoding="utf-8") as f:
                f.write(text)
            if args.quiet:
                print(f"[digest] {os.path.relpath(path, BASE)}")
        return 0

    try:
        ctx = collect(cfg, et_now, args.session)
    except Exception as exc:  # noqa: BLE001
        print(f"扫描失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(to_json(ctx), ensure_ascii=False, indent=2))
        return 0

    text = build_report(ctx, cfg)
    if not args.quiet:
        print(text)
    path = None
    if not args.no_write:
        path = save_report(ctx, cfg, text)
        if args.quiet:
            print(f"[{ctx['session']}] {ctx['today']} {_quiet_line(ctx)}"
                  f" -> {os.path.relpath(path, BASE)}")
        else:
            print(f"\n[报告已保存] {os.path.relpath(path, BASE)}", file=sys.stderr)
    elif args.quiet:
        print(f"[{ctx['session']}] {ctx['today']} {_quiet_line(ctx)}")
    if ctx.get("calendar_source") == "unsupported":
        return 2
    if ctx["errors"] and len(ctx["errors"]) >= len(cfg["symbols"]):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

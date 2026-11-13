#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T+1 无前视回测：硬约束 vs 固定日期，组合价值/XIRR/回撤/净超额。

规则：
- T 日收盘生成信号，T+1 首个可交易时段开盘成交（不得同日收盘成交）。
- 每月 AUD 预算在当月首个交易日入金；买入按当日 FX 折算 USD。
- 合并/欠额走与实盘相同的 month_obligation/decide 语义（SimLedger）。
- 主指标：期末组合价值、剩余现金、XIRR、最大回撤(TWR)、净超额(vs fixed_1)。
- 有效每股成本只作辅助。
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fx as fxmod          # noqa: E402
import scan                 # noqa: E402

TOL = 0.01


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_market(cfg, start=None, end=None):
    vix_map = {}
    try:
        vrows, _ = scan.fetch_daily_cached(cfg, cfg["vix"]["symbol"], {}, ttl_hours=24)
        vix_map = {r["date"]: float(r["close"]) for r in vrows}
    except Exception:  # noqa: BLE001
        pass
    data = {}
    for sym, meta in cfg["symbols"].items():
        rows, _ = scan.fetch_daily_cached(cfg, sym, meta)
        rows = sorted([r for r in rows if r.get("close")], key=lambda r: r["date"])
        dates = [r["date"] for r in rows]
        close = np.array([float(r["close"]) for r in rows], dtype=float)
        rows_by_date = {r["date"]: r for r in rows}
        scores = {}
        for i, d in enumerate(dates):
            if i < 252:
                continue
            scores[d] = scan.score_components(
                close[i], close[:i + 1], vix_map.get(d, 20.0), cfg)["score"]
        data[sym] = {"dates": dates, "rows": rows_by_date,
                     "closes": close, "scores": scores}
    all_dates = sorted({d for v in data.values() for d in v["dates"]})
    if start:
        all_dates = [d for d in all_dates if scan.month_key(d) >= start]
    if end:
        all_dates = [d for d in all_dates if scan.month_key(d) <= end]
    return data, all_dates


def load_fx(cfg):
    try:
        series = fxmod.fetch_fx_history(cfg, scan.yahoo_json)
    except Exception:  # noqa: BLE001
        series = {}
    if not series:
        r = float((cfg.get("fx") or {}).get("fallback_rate") or 0.65)
        return {}, r
    return series, None


def fx_at(series, fallback, d):
    if series:
        r, _ = fxmod.rate_on(series, d)
        if r:
            return float(r)
    return float(fallback or 0.65)


def unit_dates(dates):
    return sorted(set(dates))


def month_dates(dates, month):
    return [d for d in dates if scan.month_key(d) == month]


def month_list(dates):
    out = []
    for d in dates:
        mk = scan.month_key(d)
        if not out or out[-1] != mk:
            out.append(mk)
    return out


def score_start_month(data, dates):
    months = month_list(dates)
    for mk in months:
        md = month_dates(dates, mk)
        if md and all(d in v["scores"] for v in data.values() for d in md):
            return mk
    return months[-1] if months else None


# ---------------------------------------------------------------------------
# 组合与模拟账本
# ---------------------------------------------------------------------------
class Portfolio:
    def __init__(self):
        self.cash = 0.0
        self.shares = {}
        self.contributions = []
        self.commission = 0.0
        self.invested_usd = 0.0
        self.last_px = {}
        self.value_series = []
        self.monthly_cost = {}   # (sym, month) -> [usd, shares]
        self.fx_map = {}

    def contribute(self, d, aud):
        if aud <= 0:
            return
        self.cash += aud
        self.contributions.append((d, aud))

    def buy(self, d, sym, amount_aud, price, fx, fee):
        amount_aud = min(float(amount_aud), self.cash)
        if amount_aud <= TOL or price <= 0 or fx <= 0:
            return 0.0
        net_usd = amount_aud * fx - fee
        if net_usd <= 0:
            return 0.0
        qty = net_usd / price
        self.cash -= amount_aud
        self.shares[sym] = self.shares.get(sym, 0.0) + qty
        self.commission += fee
        self.invested_usd += net_usd
        mk = scan.month_key(d)
        c = self.monthly_cost.setdefault((sym, mk), [0.0, 0.0])
        c[0] += net_usd
        c[1] += qty
        return amount_aud

    def value(self, d, data, fx_series, fx_fallback):
        v = self.cash
        for sym, qty in self.shares.items():
            if qty == 0:
                continue
            rows = data[sym]["rows"]
            r = rows.get(d)
            px = float(r["close"]) if r else self.last_px.get(sym)
            if px is None:
                continue
            self.last_px[sym] = px
            fx = self.fx_map.get(d)
            if fx is None:
                fx = fx_at(fx_series, fx_fallback, d)
            v += qty * px / fx
        return v


class SimLedger:
    """与 scan.month_obligation 兼容的内存账本；只记录真实成交/allocations。"""

    def __init__(self, cfg, budget_total, weights):
        self.cfg = cfg
        self.budget_total = float(budget_total)
        self.weights = dict(weights)
        self.fills = []
        self.orders = []
        self.alloc = {}
        self.seq = 0

    def budget_for(self, month):
        return {"total_monthly_aud": self.budget_total,
                "weights": dict(self.weights), "effective_month": "1970-01"}

    def month_allocated_aud(self, month, symbol=None):
        if symbol:
            return float(self.alloc.get((month, symbol.upper()), 0.0))
        return float(sum(v for (m, s), v in self.alloc.items() if m == month))

    def pending_by_month(self, month, symbol=None):
        out = 0.0
        for o in self.orders:
            if o["status"] not in ("placed", "partial"):
                continue
            if month not in o["months"]:
                continue
            if symbol and o["symbol"] != symbol.upper():
                continue
            filled = sum(float(f["amount_aud"]) for f in self.fills
                         if f["order_id"] == o["order_id"])
            left = max(0.0, float(o["planned_aud"]) - filled)
            if left > 0:
                out += left
        return out

    def write_offs_for(self, month, symbol=None):
        return []

    def get_batch(self, symbol):
        return None

    def create_order(self, symbol, planned_aud, months, signal_date, status="placed"):
        self.seq += 1
        o = {"order_id": f"sim-{self.seq}", "symbol": symbol.upper(),
             "planned_aud": float(planned_aud), "months": list(months),
             "signal_date": signal_date, "status": status}
        self.orders.append(o)
        return o

    def add_fill(self, order_id, symbol, trade_date, amount_aud, amount_usd,
                 price, fx, commission, months):
        amount_aud = float(amount_aud)
        amount_usd = float(amount_usd)
        if amount_aud <= 0 or amount_usd <= 0:
            return None
        self.seq += 1
        f = {"fill_id": f"simf-{self.seq}", "order_id": order_id,
             "symbol": symbol.upper(), "trade_date": trade_date,
             "amount_aud": amount_aud, "amount_usd": amount_usd,
             "price_usd": price, "fx_rate": fx, "commission_usd": commission,
             "months": list(months)}
        self.fills.append(f)
        if len(months) == 1:
            plan = {months[0]: amount_aud}
        else:
            per = amount_aud / len(months)
            plan = {m: per for m in months}
            plan[months[-1]] += amount_aud - sum(plan.values())
        for m, a in plan.items():
            self.alloc[(m, symbol.upper())] = self.alloc.get((m, symbol.upper()), 0.0) + a
        for o in self.orders:
            if o["order_id"] == order_id:
                total = sum(float(x["amount_aud"]) for x in self.fills
                            if x["order_id"] == order_id)
                o["status"] = "filled" if total >= o["planned_aud"] - 0.005 else "partial"
        return f

    def all_fills(self):
        return list(self.fills)

    def all_orders(self):
        return list(self.orders)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def xirr(flows):
    if not flows:
        return None
    flows = sorted(flows, key=lambda x: x[0])
    t0 = flows[0][0]
    if not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None

    def npv(rate):
        return sum(a / ((1.0 + rate) ** ((d - t0).days / 365.0)) for d, a in flows)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-9:
            return mid
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def max_drawdown_twr(values, contrib_by_date):
    if len(values) < 2:
        return 0.0
    idx = 1.0
    peak = 1.0
    mdd = 0.0
    prev = values[0][1]
    for d, v in values[1:]:
        c = float(contrib_by_date.get(d, 0.0))
        if prev > 0:
            r = (v - c) / prev - 1.0
            idx *= (1.0 + r)
            peak = max(peak, idx)
            mdd = min(mdd, idx / peak - 1.0)
        prev = v
    return mdd


def spearman(xs, ys):
    if len(xs) < 3:
        return None
    xr = np.argsort(np.argsort(np.array(xs, dtype=float)))
    yr = np.argsort(np.argsort(np.array(ys, dtype=float)))
    if np.std(xr) == 0 or np.std(yr) == 0:
        return None
    return float(np.corrcoef(xr, yr)[0, 1])


def bootstrap_ci(vals, iters=2000, seed=7):
    vals = np.array([v for v in vals if v is not None and math.isfinite(v)], dtype=float)
    if len(vals) < 5:
        return None
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(vals, size=len(vals), replace=True).mean()
                      for _ in range(iters)])
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


@dataclass
class Result:
    rule: str
    terminal: float
    cash: float
    xirr: float | None
    mdd: float
    contributions: float
    commission: float
    invested_usd: float
    shares: float
    months_funded: int
    months_total: int
    ending_pending: float
    ending_arrears: float
    monthly_cost: dict = field(default_factory=dict)
    monthly_alloc: dict = field(default_factory=dict)
    fills: list = field(default_factory=list)
    orders: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 模拟核心
# ---------------------------------------------------------------------------
def _fee(cfg, amount_aud, fx, price):
    gross = max(0.0, amount_aud * fx)
    return scan.commission_usd(cfg, gross, price)


def _fill_price(row, side="open", slippage_bps=0):
    p = row.get("open")
    if p is None or float(p) <= 0:
        p = row.get("close")
    p = float(p or 0)
    return p * (1.0 + float(slippage_bps) / 10000.0)


def _finalize(cfg, rule, pf, sim, dates, months, data, fx_series, fx_fallback,
              budget_total, weights):
    if not dates:
        return Result(rule, 0, 0, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, [], [])
    last = dates[-1]
    terminal = pf.value(last, data, fx_series, fx_fallback)
    flows = [(d, -a) for d, a in pf.contributions] + [(last, terminal)]
    x = xirr(flows)
    contrib_by_date = {}
    for d, a in pf.contributions:
        contrib_by_date[d] = contrib_by_date.get(d, 0.0) + a
    mdd = max_drawdown_twr(pf.value_series, contrib_by_date)
    total_a = sum(a for _, a in pf.contributions)
    shares = sum(pf.shares.values())
    funded = 0
    for mk in months:
        base = budget_total * sum(weights.values())
        alloc = sum(sim.month_allocated_aud(mk, s) for s in weights)
        if alloc >= base - max(0.5, base * 0.005):
            funded += 1
    pending = 0.0
    for o in sim.all_orders():
        if o["status"] in ("placed", "partial"):
            pending += max(0.0, o["planned_aud"] -
                           sum(float(f["amount_aud"]) for f in sim.all_fills()
                               if f["order_id"] == o["order_id"]))
    arrears = 0.0
    for mk in months:
        for s, w in weights.items():
            base = budget_total * w
            alloc = sim.month_allocated_aud(mk, s)
            arrears += max(0.0, base - alloc)
    return Result(rule, terminal, pf.cash, x, mdd, total_a, pf.commission,
                  pf.invested_usd, shares, funded, len(months), pending,
                  arrears, dict(pf.monthly_cost),
                  {(m, s): sim.month_allocated_aud(m, s) for m in months for s in weights},
                  sim.all_fills(), sim.all_orders())


def _month_maps(dates):
    months = month_list(dates)
    mm = {mk: month_dates(dates, mk) for mk in months}
    starts = {md[0] for md in mm.values() if md}
    return months, mm, starts


def _is_month_start(d, dates):
    md = month_dates(dates, scan.month_key(d))
    return bool(md and d == md[0])


def _record_day(pf, d, data, fx_series, fx_fallback):
    pf.value_series.append((d, pf.value(d, data, fx_series, fx_fallback)))


def simulate_fixed(cfg, data, dates, months, rule, budget_total, weights,
                   fx_series, fx_fallback):
    pf = Portfolio()
    pf.month_budget = budget_total
    pf.fx_map = {d: fx_at(fx_series, fx_fallback, d) for d in dates}
    sim = SimLedger(cfg, budget_total, weights)
    slip = cfg.get("backtest", {}).get("slippage_bps", 0)
    months, mm, starts = _month_maps(dates)
    picks_by_date = {}
    for mk in months:
        md = mm[mk]
        if not md:
            continue
        if rule == "fixed_1":
            picks = {md[0]: [(s, w) for s, w in weights.items()]}
        elif rule.startswith("fixed_"):
            k = int(rule.split("_")[1]) if rule.split("_")[1].isdigit() else len(md)
            d = md[min(k, len(md)) - 1]
            picks = {d: [(s, w) for s, w in weights.items()]}
        elif rule == "month_avg":
            picks = {d: [(s, w / len(md)) for s, w in weights.items()] for d in md}
        elif rule == "weekly":
            idxs = [i for i in [0, 4, 9, 14, 19] if i < len(md)]
            picks = {md[i]: [(s, w / len(idxs)) for s, w in weights.items()] for i in idxs}
        elif rule == "oracle":
            picks = {}
            for s in weights:
                best = min(md, key=lambda x: float(data[s]["rows"].get(x, {}).get("close", 1e99)))
                picks.setdefault(best, []).append((s, weights[s]))
        else:
            picks = {md[0]: [(s, w) for s, w in weights.items()]}
        for d, plist in picks.items():
            picks_by_date.setdefault(d, []).extend([(s, f, mk) for s, f in plist])
    for d in dates:
        if d in starts:
            pf.contribute(d, budget_total)
        for sym, frac, mk in picks_by_date.get(d, []):
            row = data[sym]["rows"].get(d)
            if not row:
                continue
            px = _fill_price(row, "open", slip)
            if px <= 0:
                continue
            fx = fx_at(fx_series, fx_fallback, d)
            amt = budget_total * frac
            fee = _fee(cfg, amt, fx, px)
            o = sim.create_order(sym, amt, [mk], d.isoformat())
            filled = pf.buy(d, sym, amt, px, fx, fee)
            if filled > 0:
                sim.add_fill(o["order_id"], sym, d.isoformat(), filled,
                             filled * fx - fee, px, fx, fee, [mk])
        _record_day(pf, d, data, fx_series, fx_fallback)
    return _finalize(cfg, rule, pf, sim, dates, months, data, fx_series,
                     fx_fallback, budget_total, weights)


def simulate_score(cfg, data, dates, months, rule, budget_total, weights,
                   fx_series, fx_fallback):
    pf = Portfolio()
    pf.fx_map = {d: fx_at(fx_series, fx_fallback, d) for d in dates}
    sim = SimLedger(cfg, budget_total, weights)
    slip = cfg.get("backtest", {}).get("slippage_bps", 0)
    pending = []  # scheduled for next date
    by_date = {d: i for i, d in enumerate(dates)}
    months, mm, starts = _month_maps(dates)
    for d in dates:
        if d in starts:
            pf.contribute(d, budget_total)
        # 1) 执行前一交易日收盘后的挂单（T+1 开盘）
        still = []
        for p in pending:
            if p["fill_date"] != d:
                still.append(p)
                continue
            sym = p["symbol"]
            row = data[sym]["rows"].get(d)
            if not row or p["amount_aud"] <= 0:
                continue
            px = _fill_price(row, "open", slip)
            if px <= 0:
                continue
            fx = fx_at(fx_series, fx_fallback, d)
            amt = min(p["amount_aud"], pf.cash)
            if amt <= TOL:
                continue
            fee = _fee(cfg, amt, fx, px)
            filled = pf.buy(d, sym, amt, px, fx, fee)
            if filled > 0:
                sim.add_fill(p["order_id"], sym, d.isoformat(), filled,
                             filled * fx - fee, px, fx, fee, p["months"])
        pending = still

        # 2) 收盘信号
        mk = scan.month_key(d)
        md = mm[mk]
        day_idx = sum(1 for x in md if x <= d)
        month_obj = scan.month_obligation(
            cfg, sim, mk,
            fxmod.FxRate(rate=fx_at(fx_series, fx_fallback, d), as_of_et=None,
                         source="hist", age_hours=0.0, status="fresh",
                         firm=True, note=""),
            None)
        nxt = dates[by_date[d] + 1] if by_date[d] + 1 < len(dates) else None
        for sym, w in weights.items():
            md5 = data[sym]["rows"].get(d)
            if not md5 or d not in data[sym]["scores"]:
                continue
            score = data[sym]["scores"][d]
            plan = scan.plan_for(cfg, sym, fx_at(fx_series, fx_fallback, d),
                                 "hist", None, True, month=mk, ledger=sim)
            style = cfg["styles"][cfg["symbols"][sym].get("style", "index")]
            if rule == "score_only":
                style = dict(style)
                style["early_days"] = len(md)  # 旧式：等评分，月末兜底
            ob = month_obj["symbols"][sym]
            timing = {"can_fill_now": False, "can_place_order": True,
                      "next_fill_et": None, "next_fill_local": None,
                      "execution_mode": "next_open", "fx_firm": True,
                      "session_phase": "CLOSE"}
            code, text, rec, ratio, reasons = scan.decide(
                score, style, day_idx, 0, ob, plan, timing, cfg)
            if rule == "score_only" and code == "WAIT" and d == md[-1]:
                rec = ob["remaining_aud"]
                code = "BUY_NOW"
            if code in ("BUY", "BUY_NOW") and rec > 0 and nxt is not None:
                o = sim.create_order(sym, rec, [mk], d.isoformat())
                pending.append({"order_id": o["order_id"], "symbol": sym,
                                "amount_aud": rec, "fill_date": nxt,
                                "months": [mk]})
        _record_day(pf, d, data, fx_series, fx_fallback)
    return _finalize(cfg, rule, pf, sim, dates, months, data, fx_series,
                     fx_fallback, budget_total, weights)


def simulate_random(cfg, data, dates, months, budget_total, weights,
                    fx_series, fx_fallback, seed):
    rng = np.random.default_rng(seed)
    pf = Portfolio()
    pf.fx_map = {d: fx_at(fx_series, fx_fallback, d) for d in dates}
    sim = SimLedger(cfg, budget_total, weights)
    slip = cfg.get("backtest", {}).get("slippage_bps", 0)
    months, mm, starts = _month_maps(dates)
    picks_by_date = {}
    for mk in months:
        md = mm[mk]
        if not md:
            continue
        d = md[min(len(md), int(rng.integers(1, 4))) - 1]
        picks_by_date.setdefault(d, []).extend([(s, w, mk) for s, w in weights.items()])
    for d in dates:
        if d in starts:
            pf.contribute(d, budget_total)
        for sym, w, mk in picks_by_date.get(d, []):
            row = data[sym]["rows"].get(d)
            if not row:
                continue
            px = _fill_price(row, "open", slip)
            fx = fx_at(fx_series, fx_fallback, d)
            amt = budget_total * w
            fee = _fee(cfg, amt, fx, px)
            o = sim.create_order(sym, amt, [mk], d.isoformat())
            filled = pf.buy(d, sym, amt, px, fx, fee)
            if filled > 0:
                sim.add_fill(o["order_id"], sym, d.isoformat(), filled,
                             filled * fx - fee, px, fx, fee, [mk])
    # 随机窗口只需要终值分布，不做逐日 TWR
    pf.value_series = [(dates[-1], pf.value(dates[-1], data, fx_series, fx_fallback))]
    return _finalize(cfg, "random_window", pf, sim, dates, months, data,
                     fx_series, fx_fallback, budget_total, weights)


def legacy_current(cfg, data, dates, months):
    """复现旧 --backtest 口径：窗口内评分达标或第 early_days 日，按收盘成交。"""
    out = {}
    for sym, w in cfg["symbols"].items():
        style = dict(cfg["styles"][w.get("style", "index")])
        if sym in ("SMH", "TSLA"):
            style["early_days"] = 3
            style["buy"] = 50
        vs = []
        for mk in months:
            md = month_dates(dates, mk)
            if not md:
                continue
            first = float(data[sym]["rows"][md[0]]["close"])
            ex = None
            for d in md:
                if d not in data[sym]["scores"]:
                    continue
                di = sum(1 for x in md if x <= d)
                sc = data[sym]["scores"][d]
                if di >= int(style.get("early_days", 1)) or sc >= float(style["buy"]):
                    ex = float(data[sym]["rows"][d]["close"])
                    break
            if ex is None:
                ex = float(data[sym]["rows"][md[-1]]["close"])
            vs.append(ex / first - 1.0)
        out[sym] = (float(np.mean(vs)) if vs else None, len(vs))
    return out


# ---------------------------------------------------------------------------
# 评分诊断
# ---------------------------------------------------------------------------
def score_diagnostics(data, dates):
    out = {}
    for sym, info in data.items():
        ds = [d for d in dates if d in info["scores"]]
        xs, ys = [], []
        for j, d in enumerate(ds):
            k = j + 21
            if k >= len(ds):
                continue
            p0 = float(info["rows"][d]["close"])
            p1 = float(info["rows"][ds[k]]["close"])
            xs.append(info["scores"][d])
            ys.append(p1 / p0 - 1.0)
        ic = spearman(xs, ys)
        q = []
        if xs:
            order = np.argsort(np.array(xs))
            chunks = np.array_split(order, 5)
            q = [float(np.mean([ys[i] for i in c])) if len(c) else None for c in chunks]
        # 月度：月初评分 vs 当月涨跌
        mxs, mys = [], []
        for mk in month_list(dates):
            md = month_dates(dates, mk)
            if not md or md[0] not in info["scores"]:
                continue
            mxs.append(info["scores"][md[0]])
            mys.append(float(info["rows"][md[-1]]["close"]) /
                       float(info["rows"][md[0]]["close"]) - 1.0)
        out[sym] = {"ic_21d": ic, "quintiles": q, "n": len(xs),
                    "monthly_ic": spearman(mxs, mys), "n_months": len(mxs)}
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _money(v):
    return "—" if v is None else f"A${v:,.0f}"


def _pct(v, d=2):
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def _xirr(v):
    return "—" if v is None else f"{v * 100:+.2f}%"


def cost_diff_ci(hard, fixed, weights, iters=2000):
    """月度有效成本差（hard/fixed-1）的加权均值 + bootstrap 95% CI。"""
    months = sorted({m for (_, m) in hard.monthly_cost})
    vals = []
    for m in months:
        diff = 0.0
        wsum = 0.0
        for sym, w in weights.items():
            h = hard.monthly_cost.get((sym, m))
            f = fixed.monthly_cost.get((sym, m))
            if not h or not f or h[1] <= 0 or f[1] <= 0:
                continue
            diff += w * ((h[0] / h[1]) / (f[0] / f[1]) - 1.0)
            wsum += w
        if wsum > 0:
            vals.append(diff / wsum)
    if not vals:
        return None
    mean = float(np.mean(vals))
    ci = bootstrap_ci(vals, iters=iters)
    return {"mean": mean, "ci": ci, "n": len(vals)}


def build_report(cfg, results, random_stats, diag, oos, sensitivity, legacy,
                 start_month, end_month, fx_meta, cost_ci=None, fx_last=0.65):
    today = datetime.now(scan.ET_TZ).date().isoformat()
    fixed = results.get("fixed_1")
    lines = [f"# 回测报告 · T+1 无前视\n\n"
             f"> 生成：{today} ｜ 样本：{start_month} ~ {end_month} ｜ "
             f"预算 A${float(cfg['portfolio']['total_monthly_aud']):,.0f}/月\n"
             f"> 信号：T 日收盘；成交：T+1 开盘 ｜ FX：{fx_meta}\n"
             f"> 主指标：期末组合价值 / 剩余现金 / XIRR / 最大回撤(TWR) / 净超额(vs fixed_1)\n\n"]

    lines.append("## 0. 结论先行\n")
    if fixed:
        for r in results.values():
            if r.rule == "hard_score":
                diff = r.terminal - fixed.terminal
                lines.append(f"- **hard_score vs fixed_1**：期末价值 {_money(r.terminal)} vs "
                             f"{_money(fixed.terminal)}，差 {_money(diff)}"
                             f"（{_pct(diff / fixed.terminal if fixed.terminal else None)}）；"
                             f"XIRR {_xirr(r.xirr)} vs {_xirr(fixed.xirr)}。")
                if cost_ci:
                    ci = cost_ci.get("ci")
                    lines.append(f"- 月度有效成本差（hard/fixed_1）：均值 {_pct(cost_ci['mean'])}"
                                 + (f"，bootstrap 95% CI [{_pct(ci[0])}, {_pct(ci[1])}]"
                                    if ci else "")
                                 + f"（n={cost_ci['n']} 个月）。")
                if diff <= 0:
                    cur1 = all(int(v.get("early_days", 1)) == 1
                               for v in cfg.get("styles", {}).values())
                    if cur1:
                        lines.append("- **评分择时没有跑赢固定第 1 日买入**；当前已设 "
                                     "`early_days=1`（hard_score 等价 fixed_2：T 日收盘信号、T+1 开盘成交），"
                                     "评分保留为展示信号。")
                    else:
                        lines.append("- **评分择时没有跑赢固定第 1 日买入**；建议 `early_days=1`，"
                                     "评分保留为展示信号（详见 §5）。")
                else:
                    lines.append("- 评分择时在终值上略优；是否稳健见 §5/§6，不单凭单路径下结论。")
        lines.append("")

    lines.append("## 1. 主结果（组合层面）\n")
    lines.append("| 规则 | 期末价值 | 剩余现金 | XIRR | 最大回撤 | 净超额 vs fixed_1 | 累计投入 | 佣金 | 硬约束月份 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results.values():
        diff = (r.terminal - fixed.terminal) if fixed else None
        lines.append(f"| {r.rule} | {_money(r.terminal)} | {_money(r.cash)} | {_xirr(r.xirr)} | "
                     f"{_pct(r.mdd)} | {_money(diff) if diff is not None else '—'} | "
                     f"{_money(r.contributions)} | {_money(r.commission)} | "
                     f"{r.months_funded}/{r.months_total} |")
    lines.append("")
    lines.append("> `hard_score` 是当前规则；fixed_1/2/3/5/10/last 是固定第 k 个交易日开盘买入；"
                 "month_avg/weekly 是机械分批；score_only 是旧式“等评分、月末兜底”诊断；"
                 "oracle 是月内最低收盘（不可交易）。\n")

    lines.append("## 2. 硬约束合规\n")
    for r in results.values():
        lines.append(f"- **{r.rule}**：{r.months_funded}/{r.months_total} 个月投满；"
                     f"期末挂单 {_money(r.ending_pending)}；期末欠额 {_money(r.ending_arrears)}。")
    lines.append("")

    if random_stats:
        lines.append("## 3. 随机窗口对照（第 1–3 个交易日随机）\n")
        lines.append(f"- 终值：均值 {_money(random_stats['mean'])}，"
                     f"P5–P95 {_money(random_stats['p5'])} ~ {_money(random_stats['p95'])}；"
                     f"hard_score 终值 {_money(random_stats['hard'])}。")
        if random_stats.get("pctile") is not None:
            lines.append(f"- hard_score 在随机分布中的分位：{random_stats['pctile']:.0f}%。")
        lines.append("")

    lines.append("## 4. 评分诊断\n")
    lines.append("| 标的 | 21日 IC | 月度 IC | Q1 | Q2 | Q3 | Q4 | Q5 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for sym, d in diag.items():
        q = d.get("quintiles") or [None] * 5
        lines.append(f"| {sym} | {d['ic_21d']:+.3f} | "
                     f"{(d['monthly_ic'] if d['monthly_ic'] is not None else float('nan')):+.3f} | "
                     + " | ".join(_pct(x) for x in q) + " |")
    lines.append("")
    lines.append("> IC 是评分与未来 21 日收益的秩相关；Q1=最低评分，Q5=最高评分。"
                 "评分只是辅助择时，不构成“等回调”的依据。\n")

    lines.append("## 5. 样本外 / 分年\n")
    lines.append("| 规则 | 样本 | 期末价值 | XIRR | 最大回撤 | 净超额 vs fixed_1 |")
    lines.append("|---|---|---|---|---|---|")
    for (rule, period), r in oos.items():
        base = oos.get((rule, "out")) if period == "out" else oos.get((rule, "in"))
        diff = None
        fx = oos.get(("fixed_1", period))
        if fx:
            diff = r.terminal - fx.terminal
        lines.append(f"| {rule} | {period} | {_money(r.terminal)} | {_xirr(r.xirr)} | "
                     f"{_pct(r.mdd)} | {_money(diff) if diff is not None else '—'} |")
    lines.append("")

    if sensitivity:
        lines.append("## 6. 预算敏感性（A$2,000 / 2,500 / 3,000）\n")
        lines.append("| 预算 | 规则 | 期末价值 | 剩余现金 | XIRR | 最大回撤 | 净超额 vs fixed_1 | 累计佣金 | TSLA 佣金% |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for b, rr in sensitivity.items():
            f1 = rr.get("fixed_1")
            tsla_usd = b * float(cfg["portfolio"]["weights"].get("TSLA", 0)) * float(fx_last or 0.65)
            tsla_pct = (1.0 / tsla_usd * 100.0) if tsla_usd > 0 else None
            for rule in ("hard_score", "fixed_1"):
                r = rr.get(rule)
                if not r:
                    continue
                diff = (r.terminal - f1.terminal) if f1 else None
                lines.append(f"| A${b:,.0f} | {rule} | {_money(r.terminal)} | {_money(r.cash)} | "
                             f"{_xirr(r.xirr)} | {_pct(r.mdd)} | "
                             f"{_money(diff) if diff is not None else '—'} | {_money(r.commission)} | "
                             f"{tsla_pct:.2f}% |" if tsla_pct is not None else
                             f"| A${b:,.0f} | {rule} | {_money(r.terminal)} | {_money(r.cash)} | "
                             f"{_xirr(r.xirr)} | {_pct(r.mdd)} | "
                             f"{_money(diff) if diff is not None else '—'} | {_money(r.commission)} | — |")
        lines.append("")

    lines.append("## 7. 辅助指标：有效每股成本\n")
    lines.append("| 规则 | 标的 | 有效每股成本 USD | 总投入 USD | 总股数 |")
    lines.append("|---|---|---|---|---|")
    for r in results.values():
        for sym in cfg["symbols"]:
            usd = sum(v[0] for (s, m), v in r.monthly_cost.items() if s == sym)
            sh = sum(v[1] for (s, m), v in r.monthly_cost.items() if s == sym)
            if sh > 0:
                lines.append(f"| {r.rule} | {sym} | US${usd / sh:,.2f} | US${usd:,.0f} | {sh:,.2f} |")
    lines.append("")
    lines.append("> 有效每股成本只作辅助；主结论看期末价值、XIRR、回撤与净超额。\n")

    if legacy:
        lines.append("## 8. 旧规则回归（旧 3 日窗口 + 收盘同 bar，仅校验）\n")
        lines.append("| 标的 | 旧口径 vs 月初首日 | 月数 |")
        lines.append("|---|---|---|")
        for sym, (v, n) in legacy.items():
            lines.append(f"| {sym} | {_pct(v)} | {n} |")
        lines.append("")
        lines.append("> 该项用于与旧 `--backtest` 交叉校验，不是新规则结论。\n")

    lines.append("## 9. 方法与限制\n")
    lines.append("- T 日收盘信号，T+1 开盘成交；开盘缺失用当日收盘并标注 fallback。")
    lines.append("- 当前配置 early_days=1 时，hard_score 最早在第 2 个交易日开盘成交，"
                 "与 fixed_2 等价；fixed_1 是第 1 个交易日开盘，天然早一天。")
    lines.append("- 每月首个交易日入金 A$B；买入按当日 FX 折算 USD；佣金默认 $1/笔。")
    lines.append("- XIRR 以月度入金为负现金流、期末价值为正；最大回撤按 TWR（剔除入金影响）。")
    lines.append("- 组合终值是单路径，不伪造显著性；月度成本差仅作辅助。")
    lines.append("- 不接券商 API；不建模税、FX 兑换费、盘中滑点（slippage_bps 可配）。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(cfg, quick=False, budget_aud=None, start=None, end=None):
    budget = float(budget_aud if budget_aud is not None else cfg["portfolio"]["total_monthly_aud"])
    cfg = copy.deepcopy(cfg)
    cfg["portfolio"]["total_monthly_aud"] = budget
    data, dates = load_market(cfg, start=None, end=end)
    if not dates:
        print("回测：无可用行情数据")
        return 2
    start_month = start or score_start_month(data, dates)
    dates = [d for d in dates if scan.month_key(d) >= start_month]
    months = month_list(dates)
    if not months:
        print("回测：样本不足")
        return 2
    cfg["portfolio"]["start_month"] = start_month
    fx_series, fx_fallback = load_fx(cfg)
    fx_meta = (f"历史日频 AUDUSD（{len(fx_series)} 天）" if fx_series
               else f"fallback {fx_fallback:.4f}（历史 FX 不可用）")
    weights = dict(cfg["portfolio"]["weights"])
    rules = ["hard_score", "fixed_1", "fixed_2", "fixed_3", "fixed_5",
             "fixed_10", "fixed_last", "month_avg", "weekly", "score_only", "oracle"]
    results = {}
    for rule in rules:
        if rule in ("hard_score", "score_only"):
            results[rule] = simulate_score(cfg, data, dates, months, rule, budget,
                                           weights, fx_series, fx_fallback)
        else:
            results[rule] = simulate_fixed(cfg, data, dates, months, rule, budget,
                                           weights, fx_series, fx_fallback)
    # 随机窗口
    n_rand = 100 if quick else 500
    rand_terms = []
    for i in range(n_rand):
        rand_terms.append(simulate_random(cfg, data, dates, months, budget, weights,
                                          fx_series, fx_fallback, seed=1000 + i).terminal)
    rand_terms = sorted(rand_terms)
    hard = results["hard_score"].terminal
    random_stats = {
        "mean": float(np.mean(rand_terms)), "p5": float(np.percentile(rand_terms, 5)),
        "p95": float(np.percentile(rand_terms, 95)), "hard": hard,
        "pctile": float(np.mean(np.array(rand_terms) <= hard) * 100),
    }
    # 样本外
    oos = {}
    for period, (s, e) in {"in": (start_month, "2021-12"),
                           "out": ("2022-01", end or months[-1])}.items():
        dates2 = [d for d in dates if s <= scan.month_key(d) <= e]
        if not dates2:
            continue
        sm2 = score_start_month(data, dates2)
        dates2 = [d for d in dates2 if scan.month_key(d) >= sm2]
        months2 = month_list(dates2)
        cfg2 = copy.deepcopy(cfg)
        cfg2["portfolio"]["start_month"] = sm2
        for rule in ("hard_score", "fixed_1"):
            if rule == "hard_score":
                r = simulate_score(cfg2, data, dates2, months2, rule, budget, weights,
                                   fx_series, fx_fallback)
            else:
                r = simulate_fixed(cfg2, data, dates2, months2, rule, budget, weights,
                                   fx_series, fx_fallback)
            oos[(rule, period)] = r
    # 预算敏感性
    sensitivity = {}
    budgets = (list(cfg.get("backtest", {}).get("sensitivity_aud") or [budget])
               if not quick else [budget])
    for b in budgets:
        rr = {}
        for rule in ("hard_score", "fixed_1"):
            if rule == "hard_score":
                rr[rule] = simulate_score(cfg, data, dates, months, rule, b, weights,
                                          fx_series, fx_fallback)
            else:
                rr[rule] = simulate_fixed(cfg, data, dates, months, rule, b, weights,
                                          fx_series, fx_fallback)
        sensitivity[b] = rr
    diag = score_diagnostics(data, dates)
    legacy = legacy_current(cfg, data, dates, months)

    cost_ci = cost_diff_ci(results["hard_score"], results["fixed_1"], weights)
    report = build_report(cfg, results, random_stats, diag, oos, sensitivity,
                          legacy, start_month, months[-1], fx_meta, cost_ci,
                          fx_at(fx_series, fx_fallback, dates[-1]))
    rep_dir = os.path.join(BASE, cfg["reports_dir"])
    os.makedirs(rep_dir, exist_ok=True)
    rep_path = os.path.join(rep_dir, f"backtest_{datetime.now(scan.ET_TZ).date().isoformat()}.md")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write(report)

    # CSV：逐月组合与逐标的分项
    csv_path = os.path.join(BASE, cfg["data_dir"], "backtest_monthly.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    import csv as _csv
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["rule", "month", "symbol", "amount_aud", "amount_usd",
                    "shares", "effective_price_usd"])
        for r in results.values():
            for mk in months:
                for sym in weights:
                    alloc = r.monthly_alloc.get((mk, sym), 0.0)
                    usd = sh = 0.0
                    # 从 portfolio monthly_cost 拿不到逐月分标的，用 allocation 近似
                    if alloc:
                        w.writerow([r.rule, mk, sym, f"{alloc:.2f}", "", "", ""])
    json_path = os.path.join(BASE, cfg["data_dir"], "backtest_summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "start_month": start_month, "end_month": months[-1],
            "budget_aud": budget, "fx": fx_meta,
            "results": {k: {**{kk: vv for kk, vv in vars(v).items()
                              if kk not in ("monthly_cost", "monthly_alloc", "fills", "orders")},
                             "fill_count": len(v.fills), "order_count": len(v.orders)}
                        for k, v in results.items()},
            "random": random_stats,
            "cost_diff_ci": cost_ci,
            "score_diagnostics": diag,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"回测完成：{os.path.relpath(rep_path, BASE)}")
    print(f"  hard_score 终值 {_money(hard)} | fixed_1 {_money(results['fixed_1'].terminal)}"
          f" | 差 {_money(hard - results['fixed_1'].terminal)}")
    print(f"  XIRR hard {_xirr(results['hard_score'].xirr)} | fixed_1 {_xirr(results['fixed_1'].xirr)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="T+1 无前视回测")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--budget-aud", type=float)
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()
    cfg = scan.load_config()
    return run(cfg, quick=args.quick, budget_aud=args.budget_aud,
               start=args.start, end=args.end)


if __name__ == "__main__":
    sys.exit(main())

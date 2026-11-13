#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify/sweep.py —— 独立检验「月内择时」是否真的改善定投成本

严格只读：scripts/、config.json、data/ 均不修改；本脚本自身也不写任何文件。
数据：
  - data/cache/daily_{VOO,SMH,TSLA,IDX_VIX}.json   5 年（与 scripts/scan.py --backtest 完全同源）
  - verify/raw/{VOO,SMH,TSLA,IDX_VIX}.json         Yahoo 10 年（2016-11 ~ 2026-11，扩展历史/样本外）
评分函数：完整复制 scripts/scan.py 的 score_components / rsi_wilder / clip（不 import、不改动原脚本）。
对照基准：定额定投（每月同样金额），故用「有效每股成本」比较：
          单笔成本 = 执行价；两笔录 = 1 / (w1/p1 + w2/p2)（同样的钱买到更多股 = 成本更低）。
主指标：cost/月初首日收盘 - 1（负 = 比月初第 1 日买更便宜），以及 cost/全月均价 - 1。

用法：
  python3 verify/sweep.py            # 全部实验（含 bootstrap / 样本外）
  python3 verify/sweep.py --quick    # 跳过 bootstrap（更快）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import datetime

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMBOLS = ["VOO", "SMH", "TSLA"]
WEIGHTS = {"VOO": 0.75, "SMH": 0.20, "TSLA": 0.05}
WARMUP = 252  # 与 scripts/scan.py 的 run_backtest 一致


# ---------------------------------------------------------------------------
# 0. 基础
# ---------------------------------------------------------------------------
def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def load_config():
    with open(os.path.join(WS, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _rows_from_yahoo(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    res = raw["chart"]["result"][0]
    ts, q = res["timestamp"], res["indicators"]["quote"][0]
    adjs = res["indicators"].get("adjclose") or [{}]
    adj = (adjs[0] if isinstance(adjs, list) else adjs).get("adjclose")
    rows = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        rows.append({
            "date": datetime.utcfromtimestamp(t).date().isoformat(),
            "open": float(q["open"][i] if q["open"][i] is not None else c),
            "high": float(q["high"][i] if q["high"][i] is not None else c),
            "low": float(q["low"][i] if q["low"][i] is not None else c),
            "close": float(c),
            "volume": float(q["volume"][i] or 0),
            "adj": float(adj[i] if adj and adj[i] is not None else c),
        })
    return rows


def load_dataset(name):
    """返回 {symbol: rows}（含 IDX_VIX）。name ∈ {'cache','10y'}。"""
    out = {}
    for sym in SYMBOLS + ["IDX_VIX"]:
        path = (os.path.join(WS, "data", "cache", f"daily_{sym}.json")
                if name == "cache" else os.path.join(WS, "verify", "raw", f"{sym}.json"))
        if name == "cache":
            with open(path, encoding="utf-8") as f:
                out[sym] = json.load(f)["rows"]
        else:
            out[sym] = _rows_from_yahoo(path)
        out[sym] = [r for r in out[sym] if r.get("date") and r.get("close") is not None]
    return out


# ---------------------------------------------------------------------------
# 1. 评分函数（逐行对齐 scripts/scan.py）
# ---------------------------------------------------------------------------
def rsi_wilder_full(closes, period=14):
    import numpy as np
    a = np.asarray(closes, dtype=float)
    n = len(a)
    out = np.full(n, 50.0)
    if n < period + 1:
        return out
    d = np.diff(a)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    ag = gains[:period].mean()
    al = losses[:period].mean()
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for k in range(period, len(d)):
        ag = (ag * (period - 1) + gains[k]) / period
        al = (al * (period - 1) + losses[k]) / period
        out[k + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def score_array(closes, vix_by_date, dates, cfg):
    import numpy as np
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    rsi = rsi_wilder_full(closes)
    scores = np.full(n, np.nan)
    good, bad = float(cfg["vix"]["good"]), float(cfg["vix"]["bad"])
    w = cfg["score_weights"]
    for i in range(n):
        p = closes[i]
        win = closes[i - WARMUP + 1:i + 1] if i >= WARMUP - 1 else closes[:i + 1]
        hi, lo = float(win.max()), float(win.min())
        pos52 = clip((p - lo) / (hi - lo)) if hi > lo else 0.5
        last60 = closes[max(0, i - 59):i + 1]
        rank60 = float((last60 < p).sum()) / len(last60)
        if i >= 19:
            seg = closes[i - 19:i + 1]
            ma20, sd20 = float(seg.mean()), float(seg.std(ddof=1))
        else:
            ma20, sd20 = float(closes[:i + 1].mean()), 0.0
        z20 = (p - ma20) / sd20 if sd20 > 0 else 0.0
        dd = p / hi - 1.0
        vix = float(vix_by_date.get(dates[i], 20.0))
        s52 = 100.0 * (1.0 - pos52)
        s60 = 100.0 * (1.0 - rank60)
        sz20 = clip(50.0 - 25.0 * z20)
        srsi = clip(100.0 - float(rsi[i]))
        sdd = clip(100.0 * (-dd) / 0.15)
        svix = clip((bad - vix) / (bad - good) * 100.0)
        total = (w["s52"] * s52 + w["s60"] * s60 + w["sz20"] * sz20
                 + w["srsi"] * srsi + w["sdd"] * sdd + w["svix"] * svix)
        # scan.py 的 score_components 返回 round(total, 1)，决定分支用的是取整后的分
        scores[i] = round(float(total), 1)
    return scores


# ---------------------------------------------------------------------------
# 1b. scripts/scan.py 的交易日历（仅硬编码 2026-2027；其他年份退化为周一至周五）
#     这里原样复制，用于「逐位复现」官方回测数字，并暴露该日历 bug 的影响。
# ---------------------------------------------------------------------------
SCAN_HOLIDAYS = {
    (2026, 1, 1), (2026, 1, 19), (2026, 2, 16), (2026, 4, 3),
    (2026, 5, 25), (2026, 6, 19), (2026, 7, 3), (2026, 9, 7),
    (2026, 11, 26), (2026, 12, 25),
    (2027, 1, 1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26),
    (2027, 5, 31), (2027, 6, 18), (2027, 7, 5), (2027, 9, 6),
    (2027, 11, 25), (2027, 12, 24),
}


def _scan_is_td(d):
    return d.weekday() < 5 and (d.year, d.month, d.day) not in SCAN_HOLIDAYS


def _scan_iter_td(start, end):
    from datetime import timedelta
    cur = start
    while cur <= end:
        if _scan_is_td(cur):
            yield cur
        cur += timedelta(days=1)


def _scan_last_td_of_month(d):
    from datetime import timedelta
    cur = datetime(d.year + (d.month == 12), (d.month % 12) + 1, 1).date() - timedelta(days=1)
    while not _scan_is_td(cur):
        cur -= timedelta(days=1)
    return cur


# ---------------------------------------------------------------------------
# 2. 数据容器
# ---------------------------------------------------------------------------
class Series:
    def __init__(self, rows, vix_rows, cfg):
        self.dates = [r["date"] for r in rows]
        self.closes = [float(r["close"]) for r in rows]
        self.opens = [float(r["open"]) for r in rows]
        self.scores = score_array(self.closes, {r["date"]: r["close"] for r in vix_rows},
                                  self.dates, cfg)
        months = {}
        for i, d in enumerate(self.dates):
            months.setdefault(d[:7], []).append(i)
        self.months = [(mk, idxs) for mk, idxs in sorted(months.items())
                       if len(idxs) >= 5 and idxs[0] >= WARMUP]

    def ctx(self, mk, idxs):
        closes = self.closes[idxs[0]:idxs[-1] + 1]
        return {
            "mk": mk, "year": mk[:4], "n": len(closes), "closes": closes,
            "dates": self.dates[idxs[0]:idxs[-1] + 1],
            "opens": self.opens[idxs[0]:idxs[-1] + 1],
            "prev_close": self.closes[idxs[0] - 1] if idxs[0] > 0 else closes[0],
            "scores": [float(self.scores[i]) for i in idxs],
            "first": closes[0], "last": closes[-1],
            "avg": sum(closes) / len(closes), "min": min(closes),
        }


class SeriesView:
    """months[a:b] 的视图，用于样本内/外拆分。"""

    def __init__(self, series, a, b):
        self._series, self.months = series, series.months[a:b]

    def ctx(self, mk, idxs):
        return self._series.ctx(mk, idxs)


# ---------------------------------------------------------------------------
# 3. 变体：返回 tranches=[(第几天(0-based), 权重)]，权重和为 1
# ---------------------------------------------------------------------------
def v_b1(c, s):
    """基线1：月初第 1 个交易日全投。"""
    return [(0, 1.0)]


def v_b2(c, s):
    """基线2：月末最后 1 个交易日全投。"""
    return [(c["n"] - 1, 1.0)]


def v_cur_scan(c, s, style):
    """当前规则（逐位复现 scan.py --backtest：使用 scan.py 自己的休市日历，
    该日历只硬编码 2026-2027，其他年份把工作日假日也当作交易日）。"""
    buy = float(style["buy"])
    if style.get("window") == "early":
        ed = int(style.get("early_days", 4))
        for k in range(c["n"]):
            if k + 1 >= ed or s[k] >= buy:
                return [(k, 1.0)]
        return [(c["n"] - 1, 1.0)]
    dl = int(style.get("deadline_days", 4))
    d0 = datetime.strptime(c["dates"][0], "%Y-%m-%d").date()
    last_td = _scan_last_td_of_month(d0)
    for k in range(c["n"]):
        d = datetime.strptime(c["dates"][k], "%Y-%m-%d").date()
        left = sum(1 for _ in _scan_iter_td(d, last_td))
        if left <= dl or s[k] >= buy:
            return [(k, 1.0)]
    return [(c["n"] - 1, 1.0)]


def v_cur_data(c, s, style):
    """当前规则（把兜底日改成「数据里真实的剩余交易日」，修正上面那个日历 bug）。"""
    buy = float(style["buy"])
    if style.get("window") == "early":
        ed = int(style.get("early_days", 4))
        for k in range(c["n"]):
            if k + 1 >= ed or s[k] >= buy:
                return [(k, 1.0)]
        return [(c["n"] - 1, 1.0)]
    dl = int(style.get("deadline_days", 4))
    for k in range(c["n"]):
        if c["n"] - k <= dl or s[k] >= buy:
            return [(k, 1.0)]
    return [(c["n"] - 1, 1.0)]


def v_cur_full(c, s, style):
    """当前规则（完整 decide() 状态机，含 partial/中性区分笔）。
    月末若仍有剩余额度，按用户「每月必投」原则在最后交易日补齐（明确假设）。"""
    buy, partial = float(style["buy"]), float(style["partial"])
    n = c["n"]
    tranches, executed = [], 0.0
    for k in range(n):
        if executed >= 1.0 - 1e-9:
            break
        cap = max(0.0, 1.0 - executed)
        day_idx, days_left = k + 1, n - k
        if style.get("window") == "early":
            ed = int(style.get("early_days", 4))
            if day_idx <= ed:
                if s[k] >= buy or day_idx >= ed:
                    tranches.append((k, cap))
                    executed = 1.0
                    break
                continue
            tranches.append((k, cap))
            executed = 1.0
            break
        dl = int(style.get("deadline_days", 4))
        if days_left <= dl:
            ratio = 1.0 if s[k] >= partial else 0.7
            amt = ratio * cap
            if amt > 1e-9:
                tranches.append((k, amt))
                executed += amt
            continue
        if s[k] >= buy:
            tranches.append((k, cap))
            executed = 1.0
            break
        if s[k] >= partial and days_left <= max(2 * dl, 8):
            tranches.append((k, 0.5 * cap))
            executed += 0.5 * cap
            continue
    if executed < 1.0 - 1e-9:
        tranches.append((n - 1, 1.0 - executed))
    return tranches


def v_a(c, s, style):
    """变体A：50% 第 1 日 + 50% 评分触发日（≥buy 的第一个日子；未触发则月末）。"""
    buy = float(style["buy"])
    k2 = c["n"] - 1
    for k in range(1, c["n"]):
        if s[k] >= buy:
            k2 = k
            break
    return [(0, 0.5), (k2, 0.5)]


def v_b(c, s, style):
    """变体B：50% 第 1 日 + 50% 月末最后 1 日。"""
    return [(0, 0.5), (c["n"] - 1, 0.5)]


def v_split(c, s, k2):
    """机械分批：50% 第 1 日 + 50% 第 k2 个交易日（不依赖评分，纯降低单日择时）。"""
    return [(0, 0.5), (min(k2 - 1, c["n"] - 1), 0.5)]


def v_mech4(c, s):
    """机械 4 等分：第 1/3/5/7 个交易日各 25%。"""
    ks = [k for k in (0, 2, 4, 6) if k < c["n"]]
    w = 1.0 / len(ks)
    return [(k, w) for k in ks]


def v_c(c, s, T, N):
    """变体C：默认第 1 日买；仅当第 1 日评分 < T（极度超买）才等，最多等 N 天。"""
    if s[0] >= T:
        return [(0, 1.0)]
    k_last = min(N, c["n"] - 1)
    for k in range(1, k_last + 1):
        if s[k] >= T:
            return [(k, 1.0)]
    return [(k_last, 1.0)]


def v_d_late(c, s, buy, deadline):
    """变体D：卫星仓 buy 线扫描（等到 ≥buy，否则月末前 deadline 天兜底）。"""
    for k in range(c["n"]):
        if c["n"] - k <= deadline or s[k] >= buy:
            return [(k, 1.0)]
    return [(c["n"] - 1, 1.0)]


def v_d_early(c, s, buy, early_days):
    """VOO 版 buy 线扫描（窗口前 early_days 天内达标则买，否则窗口末日买）。"""
    for k in range(c["n"]):
        if k + 1 >= early_days or s[k] >= buy:
            return [(k, 1.0)]
    return [(c["n"] - 1, 1.0)]


def v_e(c, s, complete_by_day, buy):
    """变体E：卫星仓最晚完成日 = 每月第 complete_by_day 个交易日。"""
    for k in range(c["n"]):
        if k + 1 >= complete_by_day or s[k] >= buy:
            return [(k, 1.0)]
    return [(c["n"] - 1, 1.0)]


def v_fixed_k(c, s, k):
    """诊断：固定在第 k 个交易日买（k=99 表示月末）。"""
    kk = c["n"] - 1 if k >= 99 else min(k - 1, c["n"] - 1)
    return [(kk, 1.0)]


def v_pullback(c, s, drop, cap):
    """诊断F：等回撤 drop（相对月初第 1 日收盘），最多等 cap 天。"""
    k_last = min(cap, c["n"] - 1)
    for k in range(1, k_last + 1):
        if c["closes"][k] <= c["first"] * (1.0 - drop):
            return [(k, 1.0)]
    return [(k_last, 1.0)]


def v_half_pullback(c, s, drop, cap):
    """诊断G：50% 第 1 日 + 50% 等回撤 drop（最多 cap 天）。"""
    k_last = min(cap, c["n"] - 1)
    k2 = k_last
    for k in range(1, k_last + 1):
        if c["closes"][k] <= c["first"] * (1.0 - drop):
            k2 = k
            break
    return [(0, 0.5), (k2, 0.5)]


# ---------------------------------------------------------------------------
# 4. 评估引擎
# ---------------------------------------------------------------------------
def eff_cost(tranches, closes):
    shares = sum(w / closes[k] for k, w in tranches)
    return 1.0 / shares


def norm_tranches(tranches, n):
    tr = [(max(0, min(int(k), n - 1)), float(w)) for k, w in tranches if w > 1e-12]
    tot = sum(w for _, w in tr)
    return [(k, w / tot) for k, w in tr] if tot > 0 else [(0, 1.0)]


def eval_variant(view, fn):
    recs = []
    for mk, idxs in view.months:
        c = view.ctx(mk, idxs)
        tr = norm_tranches(fn(c, c["scores"]), c["n"])
        cost = eff_cost(tr, c["closes"])

        def rel(x):
            v = x - 1.0
            return 0.0 if abs(v) < 1e-12 else v

        recs.append({
            "mk": mk, "year": mk[:4], "n": c["n"],
            "vs_first": rel(cost / c["first"]),
            "vs_avg": rel(cost / c["avg"]),
            "pos": min(k for k, _ in tr) + 1,
            "done": max(k for k, _ in tr) + 1,
            "s0": c["scores"][0],
            "last_vs_first": c["last"] / c["first"] - 1.0,
            "open_vs_first": rel(c["opens"][0] / c["first"]),
            "prev_vs_first": rel(c["prev_close"] / c["first"]),
        })
    return recs


# ---------------------------------------------------------------------------
# 5. 统计
# ---------------------------------------------------------------------------
def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def median(xs):
    a = sorted(xs)
    n = len(a)
    return a[n // 2] if n % 2 else 0.5 * (a[n // 2 - 1] + a[n // 2])


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def summarize(recs, key="vs_first"):
    vals = [r[key] for r in recs]
    n = len(vals)
    m, sd = mean(vals), stdev(vals)
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    t = m / se if (se and se > 1e-12) else float("nan")
    return {"n": n, "mean": m, "median": median(vals), "sd": sd, "se": se,
            "t": t,
            "win": sum(1 for v in vals if v < -1e-12) / n if n else float("nan"),
            "pos": mean([r["pos"] for r in recs]),
            "min": min(vals) if vals else float("nan"),
            "max": max(vals) if vals else float("nan")}


def bootstrap_ci(vals, iters=4000, seed=7):
    rng = random.Random(seed)
    n = len(vals)
    out = sorted(mean([vals[rng.randrange(n)] for _ in range(n)]) for _ in range(iters))
    return out[int(0.025 * iters)], out[int(0.975 * iters)]


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for t in range(i, j + 1):
                r[order[t]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(len(rx)))
    sx = math.sqrt(sum((v - mx) ** 2 for v in rx))
    sy = math.sqrt(sum((v - my) ** 2 for v in ry))
    return cov / (sx * sy) if sx * sy else float("nan")


def tv(x, dec=2):
    return "-" if x != x else f"{x:+.{dec}f}"


def pct(x):
    return "-" if x != x else f"{x * 100:+.2f}%"


def pct0(x):
    return "-" if x != x else f"{x * 100:.2f}%"


# ---------------------------------------------------------------------------
# 6. 输出
# ---------------------------------------------------------------------------
def section(title):
    print("\n" + "=" * 110)
    print(title)
    print("=" * 110)


def table(headers, rows):
    widths = []
    for i, h in enumerate(headers):
        cells = [len(str(r[i])) for r in rows if i < len(r)]
        widths.append(max([len(str(h))] + cells))
    print("| " + " | ".join(str(h).ljust(w) for h, w in zip(headers, widths)) + " |")
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in rows:
        print("| " + " | ".join(str(x).ljust(w) for x, w in zip(r, widths)) + " |")


# ---------------------------------------------------------------------------
# 7. 实验主体
# ---------------------------------------------------------------------------
def run_dataset(tag, data, cfg, do_boot=True):
    print(f"\n\n{'#' * 110}\n# 数据集：{tag}\n{'#' * 110}")
    styles = cfg["styles"]
    series_map = {s: Series(data[s], data["IDX_VIX"], cfg) for s in SYMBOLS}
    print(f"区间 {data['VOO'][0]['date']} ~ {data['VOO'][-1]['date']}，"
          f"每标的 {len(data['VOO'])} 根日线，VIX {len(data['IDX_VIX'])} 根")
    for s in SYMBOLS:
        print(f"  {s}: 可用月份 {len(series_map[s].months)} 个 "
              f"（{series_map[s].months[0][0]} ~ {series_map[s].months[-1][0]}，预热≥{WARMUP} 日）")

    def st_of(sym):
        return styles[cfg["symbols"][sym]["style"]]

    # ---------------- 表1 ----------------
    section(f"[{tag}] 表1. 基线 vs 当前规则 vs 变体 A/B（成本相对月初第 1 日；负=更便宜=更好）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        rows = []
        for nm, fn in [
            ("基线1·月初第1日", v_b1),
            ("基线2·月末最后1日", v_b2),
            ("当前规则(复现scan.py)", lambda c, s: v_cur_scan(c, s, st)),
            ("当前规则(修正日历)", lambda c, s: v_cur_data(c, s, st)),
            ("当前规则(完整逻辑)", lambda c, s: v_cur_full(c, s, st)),
            ("变体A·50%第1日+50%触发", lambda c, s: v_a(c, s, st)),
            ("变体B·50%第1日+50%月末", lambda c, s: v_b(c, s, st)),
            ("机械分批·50%第1日+50%第5日", lambda c, s: v_split(c, s, 5)),
            ("机械4等分·第1/3/5/7日", v_mech4),
        ]:
            recs = eval_variant(ser, fn)
            a, b = summarize(recs), summarize(recs, "vs_avg")
            rows.append([nm, a["n"], pct(a["mean"]), pct(a["median"]),
                         f"{a['win'] * 100:.0f}%", pct0(a["sd"]), tv(a["t"]),
                         pct(b["mean"]), f"{a['pos']:.1f}"])
        table([f"{sym}（{len(ser.months)} 个月，vs 月初第1日）", "n", "均值", "中位数", "胜率",
               "月度σ", "t", "vs 全月均价", "平均执行日"], rows)

    # ---------------- 表2 ----------------
    section(f"[{tag}] 表2. 分年拆解（相对月初第 1 日的均值；括号=该年月数）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        years = sorted({mk[:4] for mk, _ in ser.months})
        recs0 = eval_variant(ser, v_b1)  # 供「月末基线」使用
        rows = []
        for nm, fn in [
            ("当前规则", lambda c, s: v_cur_scan(c, s, st)),
            ("当前规则(完整)", lambda c, s: v_cur_full(c, s, st)),
            ("变体A", lambda c, s: v_a(c, s, st)),
            ("变体B", lambda c, s: v_b(c, s, st)),
        ]:
            recs = eval_variant(ser, fn)
            cells = [nm]
            for y in years:
                yy = [r["vs_first"] for r in recs if r["year"] == y]
                cells.append(f"{pct(mean(yy))}({len(yy)})" if yy else "-")
            cells.append(pct(mean([r["vs_first"] for r in recs])))
            rows.append(cells)
        rows.append(["基线2·月末最后1日"] +
                    [f"{pct(mean([r['last_vs_first'] for r in recs0 if r['year'] == y]))}"
                     f"({sum(1 for r in recs0 if r['year'] == y)})" for y in years] +
                    [pct(mean([r["last_vs_first"] for r in recs0]))])
        table([f"{sym} 各年"] + years + ["全样本"], rows)

    # ---------------- 表3 ----------------
    section(f"[{tag}] 表3. 样本内/外拆分（前后各一半；5 年集只有 48 个可用月）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        n = len(ser.months)
        cut = n // 2
        splits = [("前半", 0, cut), ("后半", cut, n)]
        rows = []
        for nm, fn in [
            ("当前规则", lambda c, s: v_cur_scan(c, s, st)),
            ("变体A", lambda c, s: v_a(c, s, st)),
            ("变体B", lambda c, s: v_b(c, s, st)),
            ("基线2·月末", v_b2),
        ]:
            recs = eval_variant(ser, fn)
            cells = [nm]
            for lab, a, b in splits:
                x = summarize(recs[a:b])
                cells.append(f"{pct(x['mean'])} (t{tv(x['t'], 1)})")
            rows.append(cells)
        table([f"{sym}（{n} 个月）"] + [f"{lab}({b - a}月)" for lab, a, b in splits], rows)

    # ---------------- 表4：变体C ----------------
    section(f"[{tag}] 表4. 变体C：第 1 日评分 < T_wait（极度超买）才等，最多等 N 天")
    for sym in SYMBOLS:
        ser = series_map[sym]
        rows = []
        for T in (10, 12, 15, 20, 25, 30, 35):
            for N in (3, 5, 10):
                recs = eval_variant(ser, lambda c, s, T=T, N=N: v_c(c, s, T, N))
                a = summarize(recs)
                waited = sum(1 for r in recs if r["pos"] > 1)
                rows.append([f"T={T},N={N}", a["n"], pct(a["mean"]), f"{a['win'] * 100:.0f}%",
                             tv(a["t"]), f"{waited}/{a['n']}", f"{a['pos']:.1f}"])
        table([f"{sym} 变体C", "n", "均值", "胜率", "t", "等待月数", "平均执行日"], rows)

    # ---------------- 表5：变体D ----------------
    section(f"[{tag}] 表5. 变体D：buy 买入线扫描（其余规则同当前）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        rows = []
        if st.get("window") == "early":
            ed = int(st.get("early_days", 4))
            for buy in (20, 30, 42, 50, 60, 70):
                recs = eval_variant(ser, lambda c, s, b=buy: v_d_early(c, s, b, ed))
                a = summarize(recs)
                rows.append([f"early buy={buy}", a["n"], pct(a["mean"]), f"{a['win'] * 100:.0f}%",
                             tv(a["t"]), f"{a['pos']:.1f}"])
        else:
            dl = int(st.get("deadline_days", 4))
            for buy in (45, 50, 55, 58, 60, 62, 65, 70):
                recs = eval_variant(ser, lambda c, s, b=buy: v_d_late(c, s, b, dl))
                a = summarize(recs)
                rows.append([f"late buy={buy}", a["n"], pct(a["mean"]), f"{a['win'] * 100:.0f}%",
                             tv(a["t"]), f"{a['pos']:.1f}"])
        table([f"{sym} 变体D", "n", "均值", "胜率", "t", "平均执行日"], rows)

    # ---------------- 表6：变体E ----------------
    section(f"[{tag}] 表6. 变体E：卫星仓最晚完成日限制（第 D 个交易日前必须完成）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        buy = float(st["buy"])
        rows = []
        for D in (5, 10, 15, 20):
            recs = eval_variant(ser, lambda c, s, D=D: v_e(c, s, D, buy))
            a = summarize(recs)
            rows.append([f"最晚第 {D} 日", a["n"], pct(a["mean"]), f"{a['win'] * 100:.0f}%",
                         tv(a["t"]), f"{a['pos']:.1f}"])
        a = summarize(eval_variant(ser, lambda c, s: v_cur_scan(c, s, st)))
        rows.append(["当前(月末前 deadline)", a["n"], pct(a["mean"]), f"{a['win'] * 100:.0f}%",
                     tv(a["t"]), f"{a['pos']:.1f}"])
        table([f"{sym} 变体E", "n", "均值", "胜率", "t", "平均执行日"], rows)

    # ---------------- 表7：固定第 k 日 ----------------
    section(f"[{tag}] 表7. 诊断：固定在第 k 个交易日买入（相对第 1 日；负=更便宜）")
    for sym in SYMBOLS:
        ser = series_map[sym]
        rows = []
        for k in (1, 2, 3, 4, 5, 7, 10, 13, 15, 18, 21, 99):
            a = summarize(eval_variant(ser, lambda c, s, k=k: v_fixed_k(c, s, k)))
            rows.append([("月末" if k == 99 else f"第 {k} 日"), a["n"], pct(a["mean"]),
                         f"{a['win'] * 100:.0f}%", tv(a["t"])])
        base = summarize(eval_variant(ser, v_b1))
        recs = eval_variant(ser, v_b1)
        ov = [r["open_vs_first"] for r in recs]
        pv = [r["prev_vs_first"] for r in recs]
        rows.append(["第 1 日开盘价", base["n"], pct0(mean(ov)),
                     f"{sum(1 for v in ov if v < 0) / len(ov) * 100:.0f}%",
                     f"{mean(ov) / (stdev(ov) / math.sqrt(len(ov))):+.2f}"])
        rows.append(["上月最后 1 日", base["n"], pct0(mean(pv)),
                     f"{sum(1 for v in pv if v < 0) / len(pv) * 100:.0f}%",
                     f"{mean(pv) / (stdev(pv) / math.sqrt(len(pv))):+.2f}"])
        table([f"{sym} 固定买入日", "n", "均值", "胜率(低于第1日)", "t"], rows)

    # ---------------- 表8：评分预测力（日频） ----------------
    section(f"[{tag}] 表8. 诊断：评分对未来收益的预测力（日频大样本；检验「等低分再买」前提）")
    for sym in SYMBOLS:
        ser = series_map[sym]
        rows = []
        for horizon in (5, 10, 21):
            pairs = []
            for i in range(WARMUP, len(ser.closes) - horizon):
                sc = ser.scores[i]
                if sc != sc:
                    continue
                pairs.append((float(sc), ser.closes[i + horizon] / ser.closes[i] - 1.0))
            pairs.sort()
            q = len(pairs) // 5
            cells = []
            for j in range(5):
                seg = pairs[j * q:(j + 1) * q] if j < 4 else pairs[4 * q:]
                cells.append(f"{mean([p[1] for p in seg]) * 100:+.2f}%")
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            rows.append([f"未来 {horizon} 日", len(pairs)] + cells + [f"{ic:+.3f}"])
        table([f"{sym} 评分五分组", "n", "Q1最低分", "Q2", "Q3", "Q4", "Q5最高分", "秩相关IC"], rows)

    # ---------------- 表9：月初评分 → 整月路径 ----------------
    section(f"[{tag}] 表9. 诊断：月初第 1 日评分分组的「该月月末/月初」涨跌（月度样本）")
    for sym in SYMBOLS:
        ser = series_map[sym]
        recs = eval_variant(ser, v_b1)
        rows = []
        for lab, lo, hi in (("低分 <25（便宜/超卖）", -1e9, 25), ("25-40", 25, 40),
                            ("40-55", 40, 55), ("55-70", 55, 70), ("高分 ≥70", 70, 1e9)):
            seg = [r for r in recs if lo <= r["s0"] < hi]
            if seg:
                rows.append([lab, len(seg), pct(mean([r["last_vs_first"] for r in seg])),
                             f"{mean([r['s0'] for r in seg]):.0f}"])
        table([f"{sym} 月初评分分组", "月数", "月末/月初-1", "平均评分"], rows)

    # ---------------- 表10：回撤等待 ----------------
    section(f"[{tag}] 表10. 诊断：等回撤规则（相对第 1 日；负=更好）")
    for sym in SYMBOLS:
        ser = series_map[sym]
        rows = []
        for drop in (0.01, 0.02, 0.03):
            for cap in (5, 10, 20):
                a = summarize(eval_variant(ser, lambda c, s, d=drop, cp=cap:
                                           v_pullback(c, s, d, cp)))
                rows.append([f"全仓等回撤 {drop * 100:.0f}% / 最多 {cap} 天", a["n"],
                             pct(a["mean"]), f"{a['win'] * 100:.0f}%", tv(a["t"])])
        for drop in (0.01, 0.02, 0.03):
            for cap in (10, 20):
                a = summarize(eval_variant(ser, lambda c, s, d=drop, cp=cap:
                                           v_half_pullback(c, s, d, cp)))
                rows.append([f"50%第1日+50%等回撤 {drop * 100:.0f}% / 最多 {cap} 天", a["n"],
                             pct(a["mean"]), f"{a['win'] * 100:.0f}%", tv(a["t"])])
        table([f"{sym} 回撤规则", "n", "均值", "胜率", "t"], rows)

    # ---------------- 表11：组合层面 ----------------
    section(f"[{tag}] 表11. 组合层面（75/20/5 美元加权）月度成本偏差")
    per = {}
    for sym in SYMBOLS:
        st = st_of(sym)
        per[sym] = {
            "基线1": eval_variant(series_map[sym], v_b1),
            "基线2": eval_variant(series_map[sym], v_b2),
            "当前": eval_variant(series_map[sym], lambda c, s: v_cur_scan(c, s, st)),
            "A": eval_variant(series_map[sym], lambda c, s: v_a(c, s, st)),
            "B": eval_variant(series_map[sym], lambda c, s: v_b(c, s, st)),
        }
    m = min(len(per[s]["基线1"]) for s in SYMBOLS)
    rows = []
    for lab, k in (("基线1·全部第1日", "基线1"), ("基线2·全部月末", "基线2"),
                   ("VOO/卫星均用当前规则", "当前"), ("VOO/卫星均用变体A", "A"),
                   ("VOO/卫星均用变体B", "B")):
        monthly = [sum(WEIGHTS[s] * per[s][k][i]["vs_first"] for s in SYMBOLS) for i in range(m)]
        a = summarize([{"vs_first": x, "pos": 1} for x in monthly])
        rows.append([lab, a["n"], pct(a["mean"]), pct(a["median"]),
                     f"{a['win'] * 100:.0f}%", pct0(a["sd"]), tv(a["t"])])
    table(["组合（75/20/5）", "月数", "均值", "中位数", "胜率(优于全第1日)", "月度σ", "t"], rows)

    if not do_boot:
        return

    # ---------------- 表12：bootstrap + 功效 ----------------
    section(f"[{tag}] 表12. Bootstrap 95%CI 与噪声/功效（每月仅 1 个样本 → 噪声很大）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        rows = []
        for nm, fn in [("当前规则", lambda c, s: v_cur_scan(c, s, st)),
                       ("变体A", lambda c, s: v_a(c, s, st)),
                       ("变体B", lambda c, s: v_b(c, s, st)),
                       ("基线2·月末", v_b2)]:
            vals = [r["vs_first"] for r in eval_variant(ser, fn)]
            lo, hi = bootstrap_ci(vals)
            sd = stdev(vals)
            mde = 2.8 * sd / math.sqrt(len(vals))
            rows.append([nm, len(vals), pct(mean(vals)), f"[{pct(lo)}, {pct(hi)}]",
                         pct0(sd), pct0(mde)])
        table([f"{sym}", "n", "均值", "95%CI(月度重采样)", "月度σ", "可检测最小效应(80%功效)"], rows)

    # ---------------- 表13：过拟合检验 ----------------
    section(f"[{tag}] 表13. 过拟合检验：前半段挑最优 buy 线 → 后半段（样本外）表现")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        half = len(ser.months) // 2
        va, vb = SeriesView(ser, 0, half), SeriesView(ser, half, len(ser.months))
        if st.get("window") == "early":
            grid = (20, 25, 30, 35, 42, 50)
            ed = int(st["early_days"])
            run = lambda v, g: eval_variant(v, lambda c, s, g=g: v_d_early(c, s, g, ed))
            base = lambda v: eval_variant(v, lambda c, s: v_d_early(c, s, float(st["buy"]), ed))
        else:
            grid = (45, 50, 55, 58, 62, 66, 70)
            dl = int(st["deadline_days"])
            run = lambda v, g: eval_variant(v, lambda c, s, g=g: v_d_late(c, s, g, dl))
            base = lambda v: eval_variant(v, lambda c, s: v_d_late(c, s, float(st["buy"]), dl))
        is_means = {g: mean([r["vs_first"] for r in run(va, g)]) for g in grid}
        best_g = min(grid, key=lambda g: is_means[g])
        oos_best = mean([r["vs_first"] for r in run(vb, best_g)])
        oos_own_g = min(grid, key=lambda g: mean([r["vs_first"] for r in run(vb, g)]))
        oos_own = mean([r["vs_first"] for r in run(vb, oos_own_g)])
        base_val = mean([r["vs_first"] for r in base(vb)])
        table([f"{sym} 前半最优 buy", "前半最优参数", "前半均值", "该参数在后半(样本外)",
               "后半自身最优参数", "后半自身最优", "后半当前参数"],
              [[sym, best_g, pct(is_means[best_g]), pct(oos_best), oos_own_g,
                pct(oos_own), pct(base_val)]])

    # ---------------- 表14：当前规则触发结构 ----------------
    section(f"[{tag}] 表14. 当前规则实际触发结构（解释为什么卫星仓更贵）")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        recs = eval_variant(ser, lambda c, s: v_cur_scan(c, s, st))
        n = len(recs)
        if st.get("window") == "early":
            ed = int(st["early_days"])
            trig = [r for r in recs if r["pos"] < ed]
            forced = [r for r in recs if r["pos"] >= ed]
            print(f"\n{sym}（early，early_days={ed}）：窗口内评分达标提前买 "
                  f"{len(trig)}/{n} 月（均值 {pct(mean([r['vs_first'] for r in trig])) if trig else '-'}），"
                  f"拖到窗口末日买 {len(forced)}/{n} 月"
                  f"（均值 {pct(mean([r['vs_first'] for r in forced])) if forced else '-'}）")
        else:
            dl = int(st["deadline_days"])
            trig = [r for r in recs if r["pos"] <= r["n"] - dl]
            forced = [r for r in recs if r["pos"] > r["n"] - dl]
            print(f"\n{sym}（late，buy={st['buy']:.0f}，deadline={dl}）：评分达标触发 "
                  f"{len(trig)}/{n} 月（均值 {pct(mean([r['vs_first'] for r in trig])) if trig else '-'}），"
                  f"被迫拖到月末 deadline {len(forced)}/{n} 月"
                  f"（均值 {pct(mean([r['vs_first'] for r in forced])) if forced else '-'}）")

    # ---------------- 表15：择时的尾部风险 ----------------
    section(f"[{tag}] 表15. 「择时」相对第 1 日的月度差异分布：赢了赚多少、输了亏多少")
    for sym in SYMBOLS:
        ser, st = series_map[sym], st_of(sym)
        rows = []
        for nm, fn in [("当前规则", lambda c, s: v_cur_scan(c, s, st)),
                       ("变体A", lambda c, s: v_a(c, s, st)),
                       ("变体B", lambda c, s: v_b(c, s, st)),
                       ("等回撤2%/最多10天", lambda c, s: v_pullback(c, s, 0.02, 10))]:
            vals = sorted(r["vs_first"] for r in eval_variant(ser, fn))
            n = len(vals)
            rows.append([nm, n, pct(mean(vals)), pct(vals[int(0.1 * (n - 1))]),
                         pct(vals[int(0.5 * (n - 1))]), pct(vals[int(0.9 * (n - 1))]),
                         pct(vals[0]), pct(vals[-1]),
                         f"{sum(1 for v in vals if v < -0.01)}/{n}",
                         f"{sum(1 for v in vals if v > 0.01)}/{n}"])
        table([f"{sym} 差异分布", "n", "均值", "P10", "中位", "P90", "最省月", "最贵月",
               "省>1%月数", "贵>1%月数"], rows)


    # ---------------- 表16：参数地形图（buy × 最晚完成日） ----------------
    section(f"[{tag}] 表16. 参数地形图：buy 买入线 × 最晚完成日 D（找平坦区域，而非最优点）")
    for sym in SYMBOLS:
        ser = series_map[sym]
        n = len(ser.months)
        half = n // 2
        va, vb = SeriesView(ser, 0, half), SeriesView(ser, half, n)
        buys = (20, 30, 42, 50) if sym == "VOO" else (45, 50, 55, 58, 62)
        rows = []
        for buy in buys:
            for D in (3, 5, 8, 10, 15):
                def fn(c, s, buy=buy, D=D):
                    return v_e(c, s, D, buy)
                recs = eval_variant(ser, fn)
                a = summarize(recs)
                trig = sum(1 for r in recs if r["pos"] < D) / n
                is_mean = mean([r["vs_first"] for r in eval_variant(va, fn)])
                oos_mean = mean([r["vs_first"] for r in eval_variant(vb, fn)])
                rows.append([f"buy={buy}", f"第 {D} 日", pct(a["mean"]), tv(a["t"]),
                             f"{trig * 100:.0f}%", pct(is_mean), pct(oos_mean)])
        a = summarize(eval_variant(ser, lambda c, s, st=st_of(sym): v_cur_data(c, s, st)))
        stl = st_of(sym)
        cur_lab = ("当前(buy=%g, early_days=%d)" % (stl["buy"], stl.get("early_days", 4))
                   if stl.get("window") == "early"
                   else "当前(buy=%g, 月末前%d日)" % (stl["buy"], stl.get("deadline_days", 4)))
        rows.append([cur_lab, "当前", pct(a["mean"]), tv(a["t"]), "-", "-", "-"])
        table([f"{sym} 参数地形图", "buy", "最晚完成日", "均值", "t", "窗口内触发率", "前半", "后半"], rows)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过 bootstrap")
    args = ap.parse_args()
    cfg = load_config()
    print("配置（只读）：score_weights =", cfg["score_weights"])
    print("styles =", {k: {kk: vv for kk, vv in v.items() if kk != "why"}
                      for k, v in cfg["styles"].items()})
    cache, raw = load_dataset("cache"), load_dataset("10y")
    run_dataset("5 年缓存（与 scan.py --backtest 同源）", cache, cfg, do_boot=not args.quick)
    run_dataset("10 年 Yahoo（含 2016-11~2021-11 更早历史）", raw, cfg, do_boot=not args.quick)
    print("\n\n完成。本脚本只读数据，未写入任何文件。")


if __name__ == "__main__":
    main()

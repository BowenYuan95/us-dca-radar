#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify/check_indicators.py —— 美股定投雷达 · 独立数据核验脚本
================================================================

由 data-verifier 编写，用于独立复核 scripts/scan.py 的价格/指标/数据源/边界行为。

设计原则
--------
1. **不 import scripts/scan.py 的任何指标函数**；RSI / MA / 52 周位置 / 60 日分位 /
   回撤 / z20 / 评分全部按定义在本文件里用 numpy + pandas 重新实现。
2. 只读 scripts/、config.json、data/cache/ 与 Yahoo / Nasdaq 公开接口；不写任何文件。
   `--with-ledger` 会在临时项目副本里执行 --confirm 测试，不触碰真实 data/dca_log.csv。
3. 通过 subprocess 调用 `python3 scripts/scan.py --json` / `--no-write` 获取待核验输出。
4. 行情抓取严格只用 `curl -sS -m 15 -A "Mozilla/5.0"`（完整 Chrome UA 会被 Yahoo 429）。

用法
----
    cd <项目根目录>
    python3 verify/check_indicators.py                # A/B/C + 时区检查（只读）
    python3 verify/check_indicators.py --with-ledger  # 额外跑 D 台账测试（自动恢复）

退出码：0 = 无 FAIL；1 = 存在 FAIL（详见输出的「问题项」，对应 verify/data_check.md）。
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")
LOCAL = ZoneInfo("Australia/Melbourne")

REPLAY = "2026-11-13 15:50"
SYMBOLS = {"VOO": "etf", "SMH": "etf", "TSLA": "stocks"}

# 独立维护的 NYSE 休市表（2022-2028，与 scan.py 分离维护，便于交叉核对）
NYSE_HOLIDAYS = {
    # 2022
    date(2022, 1, 17), date(2022, 2, 21), date(2022, 4, 15), date(2022, 5, 30),
    date(2022, 6, 20), date(2022, 7, 4), date(2022, 9, 5), date(2022, 11, 24),
    date(2022, 12, 26),
    # 2023
    date(2023, 1, 2), date(2023, 1, 16), date(2023, 2, 20), date(2023, 4, 7),
    date(2023, 5, 29), date(2023, 6, 19), date(2023, 7, 4), date(2023, 9, 4),
    date(2023, 11, 23), date(2023, 12, 25),
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025（含 1/9 卡特国葬）
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
    # 2028
    date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
    date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
    date(2028, 12, 25),
}
EARLY_CLOSES = {  # 13:00 ET 提前收盘（与 scan.py 各自维护，用于核对 is_after_close）
    date(2026, 7, 2), date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 7, 2), date(2027, 11, 26), date(2027, 12, 23),
}

RESULTS = []  # list[dict(section, item, status, detail)]


def rec(section, item, status, detail=""):
    RESULTS.append({"section": section, "item": item, "status": status, "detail": detail})
    tag = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "INFO": "INFO"}[status]
    print(f"  [{tag}] {item}" + (f" :: {detail}" if detail else ""))


def _title(text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "?"


# ---------------------------------------------------------------------------
# 进程 / 网络工具
# ---------------------------------------------------------------------------
def project_path(*parts):
    return os.path.join(BASE, *parts)


def run_scan(args, timeout=180, base=None):
    base = base or BASE
    cmd = [sys.executable, os.path.join(base, "scripts", "scan.py")] + args
    return subprocess.run(cmd, cwd=base, capture_output=True, text=True, timeout=timeout)


def scan_json(now_et=None, base=None):
    args = ["--json"] + (["--now-et", now_et] if now_et else [])
    p = run_scan(args, base=base)
    if p.returncode != 0:
        raise RuntimeError(f"scan.py --json 退出码 {p.returncode}: {p.stderr[:300]}")
    return json.loads(p.stdout)


def scan_report(now_et=None):
    args = ["--no-write"] + (["--now-et", now_et] if now_et else [])
    return run_scan(args)


def curl_json(url):
    """Yahoo 专用：只带简洁 Mozilla UA。"""
    p = subprocess.run(["curl", "-sS", "-m", "15", "-A", "Mozilla/5.0", url],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"curl 失败({p.returncode}): {p.stderr[:200]}")
    return json.loads(p.stdout)


def http_json(url, timeout=30):
    """Nasdaq 专用：urllib 直连即可。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def yahoo_live(sym):
    """独立抓取最新 1d/5m 行情（与 scan.py 相同的 Yahoo 接口，但由本脚本自行解析）。"""
    q = urllib.parse.quote(sym, safe="")
    data = curl_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{q}"
                     f"?range=1d&interval=5m&includePrePost=true")
    meta = data["chart"]["result"][0]["meta"]
    return meta


def pick_live_price(meta):
    state = (meta.get("marketState") or "CLOSED").upper()
    if state == "PRE" and meta.get("preMarketPrice"):
        return float(meta["preMarketPrice"])
    if state in ("POST", "CLOSED") and meta.get("postMarketPrice"):
        return float(meta["postMarketPrice"])
    for key in ("regularMarketPrice", "postMarketPrice", "preMarketPrice"):
        if meta.get(key):
            return float(meta[key])
    raise RuntimeError("Yahoo meta 无可用价格")


def num(v):
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip()
    if s in ("", "N/A", "NA", "--", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def cache_name(sym):
    return f"daily_{sym.replace('^', 'IDX_')}.json"


def load_rows(sym):
    """返回 [(date, close, volume)]，升序。"""
    obj = json.load(open(project_path("data", "cache", cache_name(sym)), encoding="utf-8"))
    out = []
    for r in obj["rows"]:
        out.append((date.fromisoformat(r["date"]), float(r["close"]), float(r.get("volume") or 0)))
    return out


def load_config():
    return json.load(open(project_path("config.json"), encoding="utf-8"))


# ---------------------------------------------------------------------------
# 指标：独立实现（不 import scan.py）
# ---------------------------------------------------------------------------
def rsi_wilder_loop(closes, period=14):
    """Wilder RSI：前 period 个涨跌幅用简单均值作种子，之后按 (n-1)/n 平滑。"""
    d = np.diff(np.asarray(closes, dtype=float))
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    if len(d) < period:
        return 50.0
    au = float(up[:period].mean())
    ad = float(dn[:period].mean())
    for i in range(period, len(d)):
        au = (au * (period - 1) + up[i]) / period
        ad = (ad * (period - 1) + dn[i]) / period
    if ad == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def rsi_pandas_ewm(closes, period=14):
    """另一种 Wilder 实现：pandas ewm(alpha=1/n, adjust=False)，用来验证种子差异可忽略。"""
    s = pd.Series(np.asarray(closes, dtype=float))
    d = s.diff().dropna()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    ad = dn.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    if ad == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def compute_indicators(price, closes, vix, cfg):
    """按 README/代码语义独立计算六分项与全部展示指标。"""
    c = np.asarray(closes, dtype=float)
    is_full_year = len(c) >= 252
    win = c[-252:] if is_full_year else c
    hi52, lo52 = float(win.max()), float(win.min())
    pos52 = (price - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5
    pos52 = min(1.0, max(0.0, pos52))

    last60 = c[-60:] if len(c) >= 60 else c
    rank60 = float((last60 < price).sum()) / len(last60)          # 分母含当日
    rank60_prior = float((c[-60:-1] < price).sum()) / 59.0 if len(c) >= 60 else rank60

    ma20 = float(c[-20:].mean())
    sd20 = float(c[-20:].std(ddof=1)) if len(c) >= 20 else 0.0
    z20 = (price - ma20) / sd20 if sd20 > 0 else 0.0

    rsi = rsi_wilder_loop(c, 14)
    rsi_alt = rsi_pandas_ewm(c, 14)
    dd52 = price / hi52 - 1.0

    w = cfg["score_weights"]
    good = float(cfg["vix"]["good"])
    bad = float(cfg["vix"]["bad"])
    s52 = 100.0 * (1.0 - pos52)
    s60 = 100.0 * (1.0 - rank60)
    sz20 = clip(50.0 - 25.0 * z20)
    srsi = clip(100.0 - rsi)
    sdd = clip(100.0 * (-dd52) / 0.15)
    svix = clip((bad - vix) / (bad - good) * 100.0)
    score = (w["s52"] * s52 + w["s60"] * s60 + w["sz20"] * sz20
             + w["srsi"] * srsi + w["sdd"] * sdd + w["svix"] * svix)

    vol20 = None
    if len(c) >= 21:
        vol20 = float(np.diff(np.log(c[-21:])).std(ddof=1) * np.sqrt(252.0))

    def perf(n):
        if len(c) <= n:
            return None
        return float(c[-1] / c[-1 - n] - 1.0)

    return {
        "pos52": pos52, "rank60": rank60, "rank60_prior": rank60_prior,
        "z20": z20, "rsi": rsi, "rsi_alt": rsi_alt, "dd52": dd52,
        "s52": s52, "s60": s60, "sz20": sz20, "srsi": srsi, "sdd": sdd, "svix": svix,
        "score": score, "ma20": ma20,
        "ma50": float(c[-50:].mean()) if len(c) >= 50 else ma20,
        "ma200": float(c[-200:].mean()) if len(c) >= 200 else ma20,
        "sd20": sd20, "hi52": hi52, "lo52": lo52, "vol20": vol20,
        "trigger": float(np.percentile(c[-60:], 35)) if len(c) >= 60 else None,
        "perf_1w": perf(5), "perf_1m": perf(21), "perf_3m": perf(63), "perf_1y": perf(252),
        "rsi_seed_spread": abs(rsi - rsi_alt),
    }


# ---------------------------------------------------------------------------
# 交易日历（独立实现）
# ---------------------------------------------------------------------------
def is_trading_day(d):
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def month_trading_days(y, m):
    cur = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    out = []
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def calendar_expect(d):
    tds = month_trading_days(d.year, d.month)
    return {
        "trading_today": is_trading_day(d),
        "day_idx": sum(1 for x in tds if x <= d),
        "days_left": sum(1 for x in tds if x >= d),
        "last_trading_day": tds[-1],
        "total": len(tds),
    }


# ---------------------------------------------------------------------------
# A. 指标独立复算
# ---------------------------------------------------------------------------
def rel_diff(a, b):
    if b == 0:
        return abs(a - b)
    return abs(a - b) / abs(b)


def section_a(cfg, base_json):
    print("\n[A] 指标独立复算 —— 目标时点 --now-et \"%s\"" % REPLAY)
    replay_date = date.fromisoformat(REPLAY.split(" ")[0])
    vix_rows = load_rows("^VIX")
    vix_own = vix_rows[-1][1]
    rec("A", "VIX 独立取值", "PASS" if abs(vix_own - base_json["vix"]) < 0.01 else "FAIL",
        f"缓存 ^VIX 最新收盘 {vix_own:.2f} vs scan.py {base_json['vix']:.2f}")

    live = {}
    for sym in SYMBOLS:
        try:
            live[sym] = yahoo_live(sym)
        except Exception as exc:  # noqa: BLE001
            live[sym] = None
            rec("A", f"{sym} 独立行情抓取", "WARN", f"{exc}（改用 scan.py 价格继续）")

    fails = 0
    for item in base_json["items"]:
        sym = item["symbol"]
        rows = load_rows(sym)
        closes = [c for d, c, _ in rows if d < replay_date]
        if live.get(sym):
            my_price = pick_live_price(live[sym])
            mono = item["price"]
            d = rel_diff(my_price, mono)
            st = "PASS" if d < 0.001 else "FAIL"
            fails += st == "FAIL"
            rec("A", f"{sym} 价格（独立 Yahoo 抓取）", st,
                f"我 {my_price:.4f} vs scan.py {mono:.4f}，偏差 {d*100:.4f}%（阈值 0.1%）")
        else:
            my_price = item["price"]

        ce = np.append(np.array(closes, dtype=float), my_price)
        mine = compute_indicators(my_price, ce, vix_own, cfg)
        sm = item["metrics"]

        # 评分：阈值 0.5 分
        sd = abs(mine["score"] - sm["score"])
        st = "PASS" if sd < 0.5 else "FAIL"
        fails += st == "FAIL"
        rec("A", f"{sym} 评分（六项加权）", st,
            f"我 {mine['score']:.2f} vs scan.py {sm['score']:.1f}，偏差 {sd:.3f} 分（阈值 0.5）")

        # 价格型指标：相对误差 0.1%
        for k in ("ma20", "ma50", "ma200", "hi52", "lo52", "sd20"):
            d = rel_diff(mine[k], sm[k])
            st = "PASS" if d < 0.001 else "FAIL"
            fails += st == "FAIL"
            rec("A", f"{sym} {k}", st, f"我 {mine[k]:.4f} vs scan.py {sm[k]:.4f}，偏差 {d*100:.4f}%")

        # 比率/评分型指标：绝对误差
        checks = [("pos52", 0.001), ("rank60", 0.001), ("z20", 0.01), ("rsi", 0.05),
                  ("dd52", 0.001), ("s52", 0.1), ("s60", 0.1), ("sz20", 0.1),
                  ("srsi", 0.1), ("sdd", 0.1), ("svix", 0.1)]
        bad = [(k, abs(mine[k] - sm[k]), tol) for k, tol in checks if abs(mine[k] - sm[k]) > tol]
        st = "PASS" if not bad else "FAIL"
        fails += st == "FAIL"
        detail = "全部在容差内" if not bad else "; ".join(
            f"{k} 我 {mine[k]:.4f} vs {sm[k]:.4f}（容差 {t}）" for k, _, t in bad)
        rec("A", f"{sym} 位置/分位/z20/RSI/回撤/分项", st, detail)

        # 展示型指标（JSON 也包含 trigger / vol20）
        td = rel_diff(mine["trigger"], item["trigger"])
        rec("A", f"{sym} 触发价 60 日 35 分位",
            "PASS" if td < 0.001 else "FAIL",
            f"我 {mine['trigger']:.2f} vs scan.py {item['trigger']:.2f}，偏差 {td*100:.4f}%")

        # RSI 种子敏感性（两种 Wilder 实现差异应可忽略）
        rec("A", f"{sym} RSI 种子敏感性", "PASS" if mine["rsi_seed_spread"] < 0.05 else "WARN",
            f"两种独立 Wilder 实现差异 {mine['rsi_seed_spread']:.6f} 分")

        # 60 日分位口径差异（分母含当日 vs 仅前 59 日）
        rec("A", f"{sym} 60 日分位口径", "INFO",
            f"分母含当日 {mine['rank60']:.4f}（scan.py 一致）；"
            f"分母仅前 59 日 {mine['rank60_prior']:.4f}，口径差 {abs(mine['rank60']-mine['rank60_prior'])*100:.3f}pp"
            f"（评分影响 ≤{abs(mine['rank60']-mine['rank60_prior'])*100*0.2:.2f} 分，可接受）")

    return fails


def section_a_report_text(cfg, report_text, base_json):
    """核对报告文本里的展示值（52 周位置/分位/RSI/MA/区间收益/分项）。"""
    print("\n[A2] 报告文本展示值核对")
    replay_date = date.fromisoformat(REPLAY.split(" ")[0])
    vix_own = load_rows("^VIX")[-1][1]
    live = {}
    for sym in SYMBOLS:
        try:
            live[sym] = yahoo_live(sym)
        except Exception:  # noqa: BLE001
            live[sym] = None
    for item in base_json["items"]:
        sym = item["symbol"]
        rows = load_rows(sym)
        closes = [c for d, c, _ in rows if d < replay_date]
        my_price = pick_live_price(live[sym]) if live.get(sym) else item["price"]
        mine = compute_indicators(my_price, np.append(np.array(closes), my_price), vix_own, cfg)

        m = re.search(rf"### {sym} · .*?(?=\n### |\n## )", report_text, re.S)
        if not m:
            rec("A2", f"{sym} 报告块", "FAIL", "未找到该标的的报告段落")
            continue
        sec = m.group(0)

        def grep(pattern, name, own, tol, kind="int"):
            mm = re.search(pattern, sec)
            if not mm:
                rec("A2", f"{sym} {name}", "FAIL", "报告文本中未找到")
                return 0
            got = float(mm.group(1).replace(",", ""))
            d = abs(got - own)
            ok = d <= tol
            rec("A2", f"{sym} {name}", "PASS" if ok else "FAIL",
                f"报告 {got:g} vs 我 {own:g}（容差 {tol}）")
            return 0 if ok else 1

        f = 0
        mm = re.search(r"52周位置 (\d+)% ｜ 距52周高点 ([+-][\d.]+)% ｜ 60日分位 (\d+)% ｜ RSI14 (\d+)", sec)
        if mm:
            for got, own, name, tol in [
                    (int(mm.group(1)), mine["pos52"] * 100, "52周位置", 1),
                    (float(mm.group(2)), mine["dd52"] * 100, "距52周高点", 0.15),
                    (int(mm.group(3)), mine["rank60"] * 100, "60日分位", 1),
                    (int(mm.group(4)), mine["rsi"], "RSI14", 1)]:
                ok = abs(got - own) <= tol
                rec("A2", f"{sym} {name}", "PASS" if ok else "FAIL", f"报告 {got:g} vs 我 {own:.3f}")
                f += 0 if ok else 1
        else:
            rec("A2", f"{sym} 位置/分位/RSI 行", "FAIL", "未匹配")
            f += 1

        mm = re.search(r"MA20 (?:US\$|\$)([\d,]+\.\d{2})（([+-][\d.]+)%）｜ MA50 (?:US\$|\$)([\d,]+\.\d{2}) ｜ MA200 (?:US\$|\$)([\d,]+\.\d{2})", sec)
        if mm:
            for got, own, name, tol in [
                    (float(mm.group(1).replace(",", "")), mine["ma20"], "MA20", 0.02),
                    (float(mm.group(2)), (my_price / mine["ma20"] - 1) * 100, "MA20 偏离", 0.02),
                    (float(mm.group(3).replace(",", "")), mine["ma50"], "MA50", 0.02),
                    (float(mm.group(4).replace(",", "")), mine["ma200"], "MA200", 0.02)]:
                ok = abs(got - own) <= tol
                rec("A2", f"{sym} {name}", "PASS" if ok else "FAIL", f"报告 {got:g} vs 我 {own:.3f}")
                f += 0 if ok else 1
        else:
            rec("A2", f"{sym} MA 行", "FAIL", "未匹配")
            f += 1

        mm = re.search(r"近1周 ([+-][\d.]+)% ｜ 近1月 ([+-][\d.]+)% ｜ 近3月 ([+-][\d.]+)% ｜ 近1年 ([+-][\d.]+)%", sec)
        if mm:
            for i, (name, key) in enumerate([("近1周", "perf_1w"), ("近1月", "perf_1m"),
                                             ("近3月", "perf_3m"), ("近1年", "perf_1y")]):
                got = float(mm.group(i + 1))
                own = mine[key] * 100
                ok = abs(got - own) <= 0.15
                rec("A2", f"{sym} {name}", "PASS" if ok else "FAIL", f"报告 {got:g}% vs 我 {own:.3f}%")
                f += 0 if ok else 1
        else:
            rec("A2", f"{sym} 区间收益行", "FAIL", "未匹配")
            f += 1

        mm = re.search(r"分项：位置 (\d+) / 分位 (\d+) / 乖离 (\d+) / RSI (\d+) / 回撤 (\d+) / VIX (\d+) → \*\*评分 (\d+)/100\*\*", sec)
        if mm:
            vals = [("s52", 0), ("s60", 1), ("sz20", 2), ("srsi", 3), ("sdd", 4), ("svix", 5), ("score", 6)]
            for key, gi in vals:
                got = float(mm.group(gi + 1))
                own = mine[key]
                ok = abs(got - own) <= 1
                rec("A2", f"{sym} 分项 {key} 展示", "PASS" if ok else "FAIL", f"报告 {got:g} vs 我 {own:.2f}")
                f += 0 if ok else 1
        else:
            rec("A2", f"{sym} 分项行", "FAIL", "未匹配")
            f += 1
    return


# ---------------------------------------------------------------------------
# B. 第二数据源交叉（Nasdaq）
# ---------------------------------------------------------------------------
def section_b(base_json):
    print("\n[B] 第二数据源交叉核对（Nasdaq API vs 缓存/实时 Yahoo）")
    fails = 0
    for sym, ac in SYMBOLS.items():
        rows = load_rows(sym)
        cache_map = {d: c for d, c, _ in rows}
        last5 = sorted(cache_map)[-5:]
        fromdate = (last5[0] - timedelta(days=20)).isoformat()
        url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={ac}"
               f"&fromdate={fromdate}&limit=600&offset=0")
        try:
            data = http_json(url)
            hrows = ((data.get("data") or {}).get("tradesTable") or {}).get("rows") or []
            nas = {}
            for r in hrows:
                d = datetime.strptime(r["date"], "%m/%d/%Y").date()
                nas[d] = num(r.get("close"))
            diffs = []
            miss = []
            for d in last5:
                a, b = cache_map[d], nas.get(d)
                if b is None:
                    miss.append(str(d))
                    continue
                diffs.append((d, a, b, rel_diff(a, b)))
            worst = max((x[3] for x in diffs), default=0.0)
            st = "PASS" if worst <= 0.001 and not miss else "FAIL"
            fails += st == "FAIL"
            rec("B", f"{sym} 最近 5 个交易日收盘价", st,
                "；".join(f"{d} 缓存 {a:.2f} vs Nasdaq {b:.2f}（{x*100:.4f}%）"
                         for d, a, b, x in diffs) + (f"；缺失 {miss}" if miss else ""))
        except Exception as exc:  # noqa: BLE001
            fails += 1
            rec("B", f"{sym} Nasdaq 历史接口", "FAIL", f"{exc}")

        # 最新价 / 昨收（Nasdaq info vs Yahoo 实时 meta）
        try:
            info = http_json(f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass={ac}")
            pdta = (info.get("data") or {}).get("primaryData") or {}
            ns_last = num(pdta.get("lastSalePrice"))
            ns_net = num(pdta.get("netChange"))
            ns_prev = ns_last - ns_net if (ns_last is not None and ns_net is not None) else None
            meta = yahoo_live(sym)
            y_last = float(meta.get("regularMarketPrice"))
            y_prev = num(meta.get("previousClose") or meta.get("chartPreviousClose"))
            d1 = rel_diff(ns_last, y_last)
            d2 = rel_diff(ns_prev, y_prev) if (ns_prev is not None and y_prev) else 1.0
            st = "PASS" if d1 <= 0.001 and d2 <= 0.001 else "FAIL"
            fails += st == "FAIL"
            rec("B", f"{sym} 最新价/昨收", st,
                f"Nasdaq lastSale {ns_last:.2f} vs Yahoo {y_last:.2f}（{d1*100:.4f}%）；"
                f"Nasdaq 昨收 {ns_prev:.2f} vs Yahoo {y_prev:.2f}（{d2*100:.4f}%）")
        except Exception as exc:  # noqa: BLE001
            fails += 1
            rec("B", f"{sym} Nasdaq info 接口", "FAIL", f"{exc}")

    # 缓存数据本身与重新抓取的 Yahoo 日线一致性（最近 6 根）
    print("\n[B2] 缓存 vs 重新抓取 Yahoo 日线（最近 6 个交易日）")
    for sym in SYMBOLS:
        try:
            q = urllib.parse.quote(sym, safe="")
            data = curl_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{q}"
                             f"?range=1mo&interval=1d&includePrePost=false")
            res = data["chart"]["result"][0]
            ts = res.get("timestamp") or []
            qq = res["indicators"]["quote"][0]
            fresh = {}
            for i, t in enumerate(ts):
                if qq["close"][i] is None:
                    continue
                d = datetime.fromtimestamp(t, tz=ET).date()
                fresh[d] = float(qq["close"][i])
            cache_map = {d: c for d, c, _ in load_rows(sym)}
            common = sorted(set(fresh) & set(cache_map))[-6:]
            worst = max((rel_diff(cache_map[d], fresh[d]) for d in common), default=0.0)
            rec("B2", f"{sym} 缓存 vs 新抓 Yahoo", "PASS" if worst <= 0.001 else "FAIL",
                f"{len(common)} 根对比，最大偏差 {worst*100:.4f}%；"
                f"最新缓存日 {max(cache_map)} / Yahoo 最新 {max(fresh)}")
        except Exception as exc:  # noqa: BLE001
            rec("B2", f"{sym} 重新抓取", "WARN", f"{exc}")

    # 缓存数据质量
    print("\n[B3] 缓存数据质量（缺失/重复/非交易日）")
    for sym in list(SYMBOLS) + ["^VIX"]:
        rows = load_rows(sym)
        dates = [d for d, _, _ in rows]
        dup = len(dates) - len(set(dates))
        mono = all(a < b for a, b in zip(dates, dates[1:]))
        weekend = [d for d in dates if d.weekday() >= 5]
        holiday = [d for d in dates if d in NYSE_HOLIDAYS]
        zero = sum(1 for _, c, _ in rows if c <= 0)
        st = "PASS" if dup == 0 and mono and not weekend and zero == 0 else "FAIL"
        extra = "" if not holiday else f"；非交易日条数 {len(holiday)}（{holiday[:4]}）"
        rec("B3", f"{sym} 缓存质量", st,
            f"{len(rows)} 根，{dates[0]}~{dates[-1]}，重复 {dup}，单调 {mono}，"
            f"周末条 {len(weekend)}，收盘价≤0 {zero}{extra}")


# ---------------------------------------------------------------------------
# C. 边界条件 + 时区
# ---------------------------------------------------------------------------
def section_c():
    print("\n[C] 边界时刻（--now-et）")
    cases = ["2026-11-26 09:35", "2026-12-01 09:35", "2026-11-30 15:50",
             "2026-11-14 12:30", "2026-11-13 07:00", "2026-12-24 15:50"]
    fails = 0
    snap = {}
    for text in cases:
        try:
            sj = scan_json(text)
        except Exception as exc:  # noqa: BLE001
            fails += 1
            rec("C", text, "FAIL", f"运行失败：{exc}")
            continue
        snap[text] = sj
        d = date.fromisoformat(text.split(" ")[0])
        exp = calendar_expect(d)
        bad = []
        if sj["trading_today"] != exp["trading_today"]:
            bad.append(f"trading_today {sj['trading_today']}≠{exp['trading_today']}")
        if sj["day_idx"] != exp["day_idx"]:
            bad.append(f"day_idx {sj['day_idx']}≠{exp['day_idx']}")
        if sj["days_left"] != exp["days_left"]:
            bad.append(f"days_left {sj['days_left']}≠{exp['days_left']}")
        if sj["last_trading_day"] != exp["last_trading_day"].isoformat():
            bad.append(f"last_td {sj['last_trading_day']}≠{exp['last_trading_day']}")
        if sj["errors"]:
            bad.append(f"errors={sj['errors']}")
        # 休市日不应有 BOY/BUY 建议文案被展示（JSON 里 verdict 仍会算，报告会压制）
        st = "PASS" if not bad else "FAIL"
        fails += st == "FAIL"
        rec("C", f"{text}（月内第 {exp['day_idx']}/{exp['total']} 日，剩余 {exp['days_left']}）", st,
            "；".join(bad) if bad else
            f"trading_today={sj['trading_today']} last_td={sj['last_trading_day']} state={sj['market_state']}")

    # C2：休市日/周末报告里的「下一个交易日」墨尔本时间（时区换算）
    print("\n[C2] 休市日报告「下一个交易日墨尔本时间」核对")
    for text in ["2026-11-14 12:30", "2026-11-26 09:35"]:
        d = date.fromisoformat(text.split(" ")[0])
        nxt = d + timedelta(days=1)
        while not is_trading_day(nxt):
            nxt += timedelta(days=1)
        expect_local = datetime.combine(nxt, time(9, 30), tzinfo=ET).astimezone(LOCAL)
        p = scan_report(text)
        mm = re.search(r"下一个交易日：美东 (\d+/\d+) 09:30\s*=\s*墨尔本 (\d+/\d+) (\d{2}:\d{2})", p.stdout)
        if not mm:
            fails += 1
            rec("C2", f"{text} 下一个交易日文案", "FAIL",
                "报告未匹配「下一个交易日：美东 M/D 09:30 = 墨尔本 M/D HH:MM」")
            continue
        got_et, got_mel, got_hm = mm.group(1), mm.group(2), mm.group(3)
        exp_et = nxt.strftime("%m/%d")
        exp_mel, exp_hm = expect_local.strftime("%m/%d"), expect_local.strftime("%H:%M")
        bad = []
        if got_et != exp_et:
            bad.append(f"美东日期 {got_et}≠{exp_et}")
        if got_mel != exp_mel:
            bad.append(f"墨尔本日期 {got_mel}≠{exp_mel}")
        if got_hm != exp_hm:
            bad.append(f"墨尔本时间 {got_hm}≠{exp_hm}")
        st = "PASS" if not bad else "FAIL"
        fails += st == "FAIL"
        rec("C2", f"{text} 下一个交易日文案", st,
            (f"报告写「美东 {got_et} 09:30 = 墨尔本 {got_mel} {got_hm}」，应为美东 {exp_et}、"
             f"墨尔本 {expect_local:%Y-%m-%d %H:%M}（{'；'.join(bad)}）") if bad else
            f"美东 {exp_et} 09:30 = 墨尔本 {expect_local:%m/%d %H:%M}")

    # C3：提前收盘日（2026-12-24 15:50 时市场已于 13:00 ET 收盘）
    print("\n[C3] 提前收盘日语义（2026-12-24 15:50 ET，当日 13:00 提前收盘）")
    p = scan_report("2026-12-24 15:50")
    txt = p.stdout
    checks = {
        "已识别提前收盘 13:00": "提前收盘 13:00" in txt,
        "行情标注已收盘": "已收盘" in txt,
        "移除「距收盘约 10 分钟」": "距收盘约 10 分钟" not in txt,
        "执行提示改为已收盘/无法成交": ("无法再成交" in txt) or ("已收盘" in txt and "执行提示" in txt),
    }
    bad = [k for k, ok in checks.items() if not ok]
    fails += len(bad)
    rec("C3", "提前收盘日提示", "PASS" if not bad else "FAIL",
        "全部满足" if not bad else f"未满足：{'；'.join(bad)}")

    # C4：盘前 / 收盘后的会话标签（按美东时间判断）
    print("\n[C4] 盘前/收盘后会话标签")
    p = scan_report("2026-11-13 07:00")
    ok_pre = "盘前复核" in p.stdout
    fails += 0 if ok_pre else 1
    rec("C4", "07:00 ET → 盘前复核", "PASS" if ok_pre else "FAIL",
        f"标题={_title(p.stdout)}" if ok_pre else f"未出现「盘前复核」，标题={_title(p.stdout)}")
    p = scan_report("2026-11-13 16:30")
    ok_post = "收盘后复核" in p.stdout and ("常规 16:00" in p.stdout) and ("无法再成交" in p.stdout)
    fails += 0 if ok_post else 1
    rec("C4", "16:30 ET → 收盘后复核", "PASS" if ok_post else "FAIL",
        f"标题={_title(p.stdout)}；执行提示含收盘说明={'常规 16:00' in p.stdout}，"
        f"含无法成交说明={'无法再成交' in p.stdout}")
    p = scan_report("2026-11-13 15:50")
    ok_reg = ("收盘后复核" not in p.stdout) and ("距收盘约 10 分钟" in p.stdout)
    fails += 0 if ok_reg else 1
    rec("C4", "15:50 ET 常规日 → 收盘前扫描", "PASS" if ok_reg else "FAIL",
        f"标题={_title(p.stdout)}；含「距收盘约 10 分钟」={'距收盘约 10 分钟' in p.stdout}")

    # C7：休市日的执行提示不应给出「开盘/盘中」操作指引
    print("\n[C7] 休市日执行提示一致性")
    p = scan_report("2026-11-26 09:35")
    misleading = "开盘 30 分钟内" in p.stdout
    rec("C7", "休市日执行提示", "WARN" if misleading else "PASS",
        "休市日报告仍输出「开盘 30 分钟内噪音大…要执行建议等 10:00 ET」的执行提示"
        if misleading else "休市日已抑制盘中操作提示")

    # C5：日计数口径（交易日的 day_idx + days_left = 月内总数 + 1）
    print("\n[C5] 交易日计数口径说明")
    for text in ["2026-11-13 15:50", "2026-11-14 12:30", "2026-11-26 09:35"]:
        sj = snap.get(text) or scan_json(text)
        d = date.fromisoformat(text.split(" ")[0])
        exp = calendar_expect(d)
        s = sj["day_idx"] + sj["days_left"]
        expect_sum = exp["total"] + (1 if exp["trading_today"] else 0)
        rec("C5", f"{text} day_idx+days_left", "PASS" if s == expect_sum else "FAIL",
            f"{sj['day_idx']}+{sj['days_left']}={s}，按含当日口径应为 {expect_sum}"
            + ("（交易日会把今天重复计入，属报告措辞口径，非数值错误）" if exp["trading_today"] else ""))
    return fails


def section_c9():
    """EARLY_CLOSES 与 NYSE 官方日历对照（官方公告：2025-11-08 的 2025-2027、2025-12-23 的 2026-2028）。

    官方提前收盘日：
      2026: 11/27、12/24           （7 月无提前收盘；07/03 为独立日补休）
      2027: 11/26                  （7 月无提前收盘、12 月无提前收盘；
                                     07/05 为独立日补休；12/24 为圣诞节补休=休市而非提前收盘）
      2028: 07/03、11/24           （脚本 EARLY_CLOSES 未覆盖 2028）
    """
    print("\n[C9] EARLY_CLOSES 与 NYSE 官方日历对照")
    fails = 0
    for text in ["2026-07-02 15:50", "2027-07-02 15:50", "2027-12-23 15:50"]:
        p = scan_report(text)
        wrong = ("提前收盘 13:00" in p.stdout) or ("已收盘（今日提前收盘" in p.stdout) \
            or ("收盘后复核" in p.stdout)
        fails += 1 if wrong else 0
        rec("C9", f"{text} 应为常规交易日（16:00 收盘）", "FAIL" if wrong else "PASS",
            "报告错误标注「已收盘（今日提前收盘 13:00）/收盘后复核」，实际当日正常交易至 16:00"
            if wrong else "未误标提前收盘")
    for text in ["2028-07-03 15:50", "2028-11-24 15:50"]:
        p = scan_report(text)
        missing = "距收盘约 10 分钟" in p.stdout
        rec("C9", f"{text} 官方为提前收盘日（13:00）", "WARN" if missing else "PASS",
            "EARLY_CLOSES 未包含该 2028 提前收盘日，报告仍提示「距收盘约 10 分钟」"
            if missing else "已识别提前收盘")
    return fails


def section_tz():
    print("\n[C6] 时区/夏令时换算（美东开盘 09:30 → 墨尔本）")
    fails = 0
    cases = [("2026-11-13 09:35", "2026-11-14 01:35", "01:30"),
             ("2027-01-15 09:35", "2027-01-16 01:35", "01:30"),
             ("2027-07-15 09:35", "2027-07-15 23:35", "23:30")]
    for text, exp_local, exp_open_hhmm in cases:
        sj = scan_json(text)
        et_dt = datetime.fromisoformat(sj["generated_et"])
        own_local = et_dt.astimezone(LOCAL)
        got_local = sj["generated_local"]
        ok_local = own_local.isoformat()[:16] == got_local[:16]
        p = scan_report(text)
        mm = re.search(r"美股开盘 = 墨尔本 (\d{2}:\d{2})", p.stdout)
        got_open = mm.group(1) if mm else "?"
        exp_open = datetime.combine(et_dt.date(), time(9, 30), tzinfo=ET).astimezone(LOCAL)
        ok_open = got_open == exp_open.strftime("%H:%M") == exp_open_hhmm
        st = "PASS" if ok_local and ok_open else "FAIL"
        fails += st == "FAIL"
        rec("C6", f"{text} → 墨尔本", st,
            f"generated_local={got_local}（独立换算 {own_local.isoformat()}，期望 {exp_local}）；"
            f"报告开盘=墨尔本 {got_open}（独立换算 {exp_open:%Y-%m-%d %H:%M %Z}，期望 {exp_open_hhmm}）")
    return fails


def section_c8():
    """提前收盘日 is_after_close 的功能性核对（2026-11-27 感恩节次日，13:00 收盘）。"""
    print("\n[C8] 提前收盘日 et_close/is_after_close 功能核对（2026-11-27 13:00）")
    fails = 0
    p = scan_report("2026-11-27 12:30")
    ok_before = ("收盘后复核" not in p.stdout) and ("无再成交" not in p.stdout)
    # 12:30 → 应为午盘扫描，不能出现「已收盘」
    ok_before = ok_before and ("午盘扫描" in p.stdout)
    fails += 0 if ok_before else 1
    rec("C8", "12:30 ET（提前收盘前半小时间）", "PASS" if ok_before else "FAIL",
        f"标题={_title(p.stdout)}（应含「午盘扫描」且无收盘后标记）")
    p = scan_report("2026-11-27 13:35")
    ok_after = ("收盘后复核" in p.stdout) and ("提前收盘 13:00" in p.stdout) and ("无法再成交" in p.stdout)
    fails += 0 if ok_after else 1
    rec("C8", "13:35 ET（提前收盘后）", "PASS" if ok_after else "FAIL",
        f"标题={_title(p.stdout)}；含提前收盘说明 {'提前收盘 13:00' in p.stdout}，"
        f"含无法成交说明 {'无法再成交' in p.stdout}")
    return fails


def _count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _make_temp_project(prefix):
    """把 scripts/scan.py + config.json + data/cache 复制到临时目录，供写盘类测试使用。"""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix=prefix)
    shutil.copytree(project_path("scripts"), os.path.join(tmp, "scripts"))
    shutil.copy2(project_path("config.json"), os.path.join(tmp, "config.json"))
    shutil.copytree(project_path("data", "cache"), os.path.join(tmp, "data", "cache"))
    return tmp


def section_c10():
    """--quiet 单行状态：休市日不能误显示「✅ 今日执行」，应为休市+下一个交易日。"""
    print("\n[C10] --quiet 单行状态（休市日 / 交易日）")
    import shutil

    tmp = _make_temp_project("dca_quiet_")
    fails = 0
    try:
        p = run_scan(["--now-et", "2026-11-26 09:35", "--quiet"], base=tmp)
        line = p.stdout.strip()
        ok_holiday = (p.returncode == 0 and "美股休市，无操作" in line
                      and "下一个交易日 11/27" in line and "✅" not in line
                      and "今日执行" not in line)
        fails += 0 if ok_holiday else 1
        rec("C10", "休市日 --quiet 单行", "PASS" if ok_holiday else "FAIL", f"输出：{line}")

        p = run_scan(["--now-et", "2026-11-13 15:50", "--quiet"], base=tmp)
        line = p.stdout.strip()
        ok_trade = (p.returncode == 0 and "VOO" in line and "休市" not in line
                    and "->" in line and "分" in line)
        fails += 0 if ok_trade else 1
        rec("C10", "交易日 --quiet 单行", "PASS" if ok_trade else "FAIL", f"输出：{line}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def section_e():
    """落痕核对：正常扫描写 signals.csv；digest 不再重复写（临时副本，不碰真实 data/）。"""
    print("\n[E] 落痕核对（临时副本）：正常扫描写 signals.csv / digest 不重复写")
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="dca_digest_")
    fails = 0
    try:
        shutil.copytree(project_path("scripts"), os.path.join(tmp, "scripts"))
        shutil.copy2(project_path("config.json"), os.path.join(tmp, "config.json"))
        shutil.copytree(project_path("data", "cache"), os.path.join(tmp, "data", "cache"))
        sig = os.path.join(tmp, "data", "signals.csv")

        p = run_scan(["--now-et", "2026-11-13 15:50"], base=tmp)
        n1 = _count_lines(sig)
        ok1 = p.returncode == 0 and n1 == 4  # 表头 + 3 标的
        fails += 0 if ok1 else 1
        rec("E", "正常收盘扫描写 signals.csv", "PASS" if ok1 else "FAIL",
            f"signals.csv 行数={n1}（期望表头+3 标的=4）")

        p = run_scan(["--now-et", "2026-11-13 17:15", "--digest"], base=tmp)
        n2 = _count_lines(sig)
        digest_file = os.path.join(tmp, "reports", "2026-11-13_digest.md")
        ok2 = p.returncode == 0 and n2 == n1 and os.path.exists(digest_file)
        fails += 0 if ok2 else 1
        rec("E", "digest 不重复写 signals.csv", "PASS" if ok2 else "FAIL",
            f"digest 后 signals.csv 行数 {n2}（扫描后 {n1}，应不变）；"
            f"生成 digest 报告={os.path.exists(digest_file)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def section_c11(cfg):
    """v2 硬约束：默认 pay 下无 DEFER；placed 不计已成交；AUD 计划拆分正确。"""
    print("\n[C11] v2 硬约束 / 台账状态（临时副本）")
    import shutil

    tmp = _make_temp_project("dca_comm_")
    fails = 0
    try:
        cfg_path = os.path.join(tmp, "config.json")
        cfg2 = json.load(open(cfg_path, encoding="utf-8"))
        c = cfg2.get("commission") or {}
        ok_policy = (str(c.get("policy") or "pay") == "pay"
                     and abs(float(c.get("per_order_usd", 0)) - 1.0) < 1e-9
                     and abs(float(c.get("max_pct", 0)) - 0.35) < 1e-9)
        fails += 0 if ok_policy else 1
        rec("C11", "config.commission 默认 pay", "PASS" if ok_policy else "FAIL",
            f"policy={c.get('policy')}, per_order_usd={c.get('per_order_usd')}, max_pct={c.get('max_pct')}")

        total = float(cfg2["portfolio"]["total_monthly_aud"])
        weights = cfg2["portfolio"]["weights"]
        plans = {s: round(total * float(weights.get(s, 0)), 2) for s in cfg2["symbols"]}
        ok_split = (plans == {"VOO": 1875.0, "SMH": 500.0, "TSLA": 125.0}
                    and abs(sum(plans.values()) - total) < 0.01)
        fails += 0 if ok_split else 1
        rec("C11", f"月度计划拆分（A${total:,.0f}）", "PASS" if ok_split else "FAIL",
            f"{plans}，合计 A${sum(plans.values()):,.2f}")

        sj = scan_json("2026-11-13 15:50", base=tmp)
        it = {x["symbol"]: x for x in sj["items"]}
        tsla = it["TSLA"]
        ok_hard = (tsla["verdict"] in ("BUY", "BUY_NOW")
                   and tsla.get("batch") is None
                   and abs(float(tsla.get("pending_aud") or 0)) < 0.01)
        fails += 0 if ok_hard else 1
        rec("C11", "TSLA 默认硬约束：BUY/BUY_NOW，无 DEFER", "PASS" if ok_hard else "FAIL",
            f"verdict={tsla['verdict']}，batch={tsla.get('batch')}，pending={tsla.get('pending_aud')}")

        # placed 不计已成交
        p = run_scan(["--now-et", "2026-11-13 15:50", "--confirm", "TSLA:125",
                      "--status", "placed", "--order-id", "o-test"], base=tmp)
        sj = scan_json("2026-11-13 15:50", base=tmp)
        tsla = {x["symbol"]: x for x in sj["items"]}["TSLA"]
        ok_placed = (tsla["verdict"] == "PENDING"
                     and abs(float(tsla.get("pending_aud") or 0) - 125) < 0.01
                     and abs(float(tsla.get("executed_aud") or 0)) < 0.01)
        fails += 0 if ok_placed else 1
        rec("C11", "placed 只算 pending，不算已成交", "PASS" if ok_placed else "FAIL",
            f"verdict={tsla['verdict']}，pending={tsla.get('pending_aud')}，"
            f"executed={tsla.get('executed_aud')}")

        # 真实成交后 PAID
        p = run_scan(["--now-et", "2026-11-13 15:50", "--confirm", "TSLA:125",
                      "--status", "filled", "--price", "100", "--fx-rate", "0.65",
                      "--trade-date", "2026-11-13", "--fill-id", "t-fill"], base=tmp)
        sj = scan_json("2026-11-13 15:50", base=tmp)
        tsla = {x["symbol"]: x for x in sj["items"]}["TSLA"]
        ok_fill = (tsla["verdict"] == "DONE"
                   and abs(float(tsla.get("executed_aud") or 0) - 125) < 0.01)
        fails += 0 if ok_fill else 1
        rec("C11", "真实成交后 DONE（AUD 口径）", "PASS" if ok_fill else "FAIL",
            f"verdict={tsla['verdict']}，executed={tsla.get('executed_aud')}")

        # 重复 fill_id 不重复计
        p = run_scan(["--now-et", "2026-11-13 15:50", "--confirm", "TSLA:125",
                      "--status", "filled", "--price", "100", "--fx-rate", "0.65",
                      "--trade-date", "2026-11-13", "--fill-id", "t-fill"], base=tmp)
        sj = scan_json("2026-11-13 15:50", base=tmp)
        tsla = {x["symbol"]: x for x in sj["items"]}["TSLA"]
        ok_idem = abs(float(tsla.get("executed_aud") or 0) - 125) < 0.01
        fails += 0 if ok_idem else 1
        rec("C11", "重复 fill_id 幂等：只计一次", "PASS" if ok_idem else "FAIL",
            f"executed={tsla.get('executed_aud')}（应仍为 125）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print("  [INFO] 临时副本已删除；真实 data/dca.sqlite 未被本次测试改动")
    return fails


def section_d(cfg):
    """v2 台账：部分成交 / 取消。全部在临时副本执行。"""
    print("\n[D] v2 台账：部分成交 / 取消（临时副本）")
    import shutil
    tmp = _make_temp_project("dca_ledger_")
    fails = 0
    try:
        p = run_scan(["--now-et", "2026-11-13 15:50", "--confirm", "VOO:1875",
                      "--status", "partial", "--filled-aud", "1000",
                      "--price", "600", "--fx-rate", "0.65",
                      "--trade-date", "2026-11-03", "--order-id", "o-voo",
                      "--fill-id", "f-voo"], base=tmp)
        sj = scan_json("2026-11-13 15:50", base=tmp)
        voo = {x["symbol"]: x for x in sj["items"]}["VOO"]
        ok_partial = (voo["verdict"] == "PENDING"
                      and abs(float(voo.get("executed_aud") or 0) - 1000) < 0.01
                      and abs(float(voo.get("pending_aud") or 0) - 875) < 0.01)
        fails += 0 if ok_partial else 1
        rec("D", "部分成交 → PENDING，pending 875", "PASS" if ok_partial else "FAIL",
            f"verdict={voo['verdict']}，executed={voo.get('executed_aud')}，pending={voo.get('pending_aud')}")

        p = run_scan(["--now-et", "2026-11-13 15:50", "--confirm", "VOO",
                      "--status", "cancelled", "--order-id", "o-voo"], base=tmp)
        sj = scan_json("2026-11-13 15:50", base=tmp)
        voo = {x["symbol"]: x for x in sj["items"]}["VOO"]
        ok_cancel = (abs(float(voo.get("pending_aud") or 0)) < 0.01
                     and abs(float(voo.get("executed_aud") or 0) - 1000) < 0.01)
        fails += 0 if ok_cancel else 1
        rec("D", "取消后 pending 归零，已成交保留", "PASS" if ok_cancel else "FAIL",
            f"executed={voo.get('executed_aud')}，pending={voo.get('pending_aud')}，"
            f"verdict={voo['verdict']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def main():
    ap = argparse.ArgumentParser(description="美股定投雷达独立数据核验")
    ap.add_argument("--with-ledger", action="store_true",
                    help="额外执行 D 台账测试（在临时副本中运行，不触碰真实 data/dca_log.csv）")
    args = ap.parse_args()

    cfg = load_config()
    md5_scan0 = _md5(project_path("scripts", "scan.py"))
    md5_cfg0 = _md5(project_path("config.json"))
    print("=" * 78)
    print("美股定投雷达 · 独立数据核验 (verify/check_indicators.py)")
    print(f"项目目录 : {BASE}")
    print(f"scan.py  : md5 {md5_scan0}")
    print(f"config   : md5 {md5_cfg0}")
    last = {s: load_rows(s)[-1][0].isoformat() for s in SYMBOLS}
    print(f"缓存最新  : {last}；^VIX {load_rows('^VIX')[-1][0].isoformat()}")
    print("=" * 78)

    base_json = scan_json(REPLAY)
    base_report = scan_report(REPLAY)

    section_a(cfg, base_json)
    section_a_report_text(cfg, base_report.stdout, base_json)
    section_b(base_json)
    section_c()
    section_c8()
    section_c9()
    section_c10()
    section_c11(cfg)
    section_tz()
    section_e()
    if args.with_ledger:
        section_d(cfg)
    else:
        print("\n[D] 台账测试未执行（加 --with-ledger 运行；验证记录见 verify/data_check.md）")

    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "FAIL", "WARN", "INFO")}
    md5_scan1 = _md5(project_path("scripts", "scan.py"))
    md5_cfg1 = _md5(project_path("config.json"))
    if (md5_scan0, md5_cfg0) != (md5_scan1, md5_cfg1):
        rec("E", "核验期间版本漂移", "WARN",
            f"scan.py {md5_scan0}→{md5_scan1}，config {md5_cfg0}→{md5_cfg1}；结果可能混合两个版本，请重跑")
    else:
        rec("E", "核验期间版本稳定", "PASS", f"scan.py md5 {md5_scan1}，config md5 {md5_cfg1}")
    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "FAIL", "WARN", "INFO")}
    print("\n" + "=" * 78)
    print(f"结果：PASS {counts['PASS']} / FAIL {counts['FAIL']} / WARN {counts['WARN']} / INFO {counts['INFO']}"
          f"   （FAIL 项即 verify/data_check.md 的「问题项」）")
    print("=" * 78)
    return 0 if counts["FAIL"] == 0 else 1


def _md5(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


if __name__ == "__main__":
    sys.exit(main())

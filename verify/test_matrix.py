#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2 测试矩阵：时钟/日历、FX、台账、义务/欠额、决策、T+1 回测。

只读/临时副本；不写真实 data/dca.sqlite、reports/。
用法：python3 verify/test_matrix.py
退出码：0 = 无 FAIL（SKIP 不计）。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import backtest as bt          # noqa: E402
import fx as fxmod             # noqa: E402
import nyse_calendar as ncal   # noqa: E402
import scan                     # noqa: E402
from ledger import Ledger       # noqa: E402

ET = ZoneInfo("America/New_York")
RESULTS = []


def rec(section, name, status, detail=""):
    RESULTS.append({"s": section, "n": name, "st": status, "d": detail})
    print(f"  [{status}] {section} {name}" + (f" :: {detail}" if detail else ""))
    return status


def check(section, name, cond, detail=""):
    return rec(section, name, "PASS" if cond else "FAIL", detail)


def skip(section, name, detail=""):
    return rec(section, name, "SKIP", detail)


def cfgv2():
    c = scan.load_config()
    c = json.loads(json.dumps(c))
    c["portfolio"]["start_month"] = "2026-09"
    return c


def fxobj(rate=0.65):
    return fxmod.FxRate(rate=rate, as_of_et=None, source="test", age_hours=0.0,
                        status="fresh", firm=True, note="test")


# ---------------------------------------------------------------------------
def suite_clock():
    print("\n[A] 市场时钟 / 日历")
    c = cfgv2()
    m = scan.market_clock(c, datetime(2026, 11, 27, 12, 30, tzinfo=ET))
    check("A2", "2026-11-27 12:30 可成交", m["can_fill_now"] and m["early_close"],
          f"phase={m['session_phase']} close={m['close_et']}")
    m = scan.market_clock(c, datetime(2026, 11, 27, 13, 30, tzinfo=ET))
    check("A2", "2026-11-27 13:30 提前收盘后不可成交",
          (not m["can_fill_now"]) and m["session_phase"] == "POST" and m["early_close"],
          f"next={m['next_fill_et']}")
    m = scan.market_clock(c, datetime(2026, 11, 30, 16, 30, tzinfo=ET))
    check("A7", "2026-11-30 16:30 月末收盘后不可成交",
          (not m["can_fill_now"]) and m["next_fill_et"].date() == date(2026, 12, 1),
          f"next={m['next_fill_et']}")
    m = scan.market_clock(c, datetime(2026, 12, 1, 8, 0, tzinfo=ET))
    check("A6", "2026-12-01 08:00 盘前可挂单不可成交",
          m["session_phase"] == "PRE" and m["can_place_order"] and not m["can_fill_now"],
          f"phase={m['session_phase']}")
    m = scan.market_clock(c, datetime(2026, 11, 26, 9, 35, tzinfo=ET))
    check("A4", "2026-11-26 感恩节休市",
          (not m["trading_today"]) and m["session_phase"] == "HOLIDAY",
          f"next={m['next_fill_et']} local={m['next_fill_local']}")
    m = scan.market_clock(c, datetime(2027, 7, 15, 9, 35, tzinfo=ET))
    check("A8", "2027-07-15 DST 墨尔本 23:30",
          m["local_now"].date() == date(2027, 7, 15) and m["local_now"].hour == 23
          and m["local_now"].minute == 35, str(m["local_now"]))
    m = scan.market_clock(c, datetime(2027, 1, 15, 9, 35, tzinfo=ET))
    check("A8", "2027-01-15 DST 墨尔本次日 01:30",
          m["local_now"].date() == date(2027, 1, 16) and m["local_now"].hour == 1
          and m["local_now"].minute == 35, str(m["local_now"]))
    try:
        ncal.CALENDAR.is_trading_day(date(2029, 1, 2))
        ok = False
    except ncal.CalendarRangeError:
        ok = True
    check("A11", "2029 超范围明确报错", ok)
    if ncal.CALENDAR.library is None:
        skip("A12", "exchange_calendars 未安装（内置表回退）",
             ncal.CALENDAR.library_error[:120])
    else:
        ok = all(ncal.CALENDAR.is_trading_day(d) == ncal.CALENDAR.builtin.is_trading_day(d)
                 for d in [date(2026, 11, 26), date(2026, 11, 27), date(2025, 12, 24)])
        check("A12", "库 vs 内置表抽样一致", ok)


# ---------------------------------------------------------------------------
def suite_fx():
    print("\n[B] 币种 / FX / 预算")
    c = cfgv2()
    tmp = tempfile.mkdtemp(prefix="dca_fx_")
    try:
        def fake(path):
            return {"chart": {"result": [{"meta": {
                "regularMarketPrice": 0.655, "regularMarketTime": 1794540000,
                "currency": "USD"}, "timestamp": [], "indicators": {"quote": [{"close": []}]}}]}}
        now = 1794540000.0
        f = fxmod.fetch_fx(c, fake, base=tmp, now=now)
        check("B1", "新鲜 FX firm", abs(f.rate - 0.655) < 1e-9 and f.firm, f.note)
        f2 = fxmod.fetch_fx(c, None, base=tmp, now=now + 30 * 3600)
        check("B3", "过期 FX 只估算", f2.rate is not None and not f2.firm
              and f2.status == "stale", f"age={f2.age_hours}h status={f2.status}")
        c2 = json.loads(json.dumps(c))
        c2["fx"]["fallback_rate"] = 0.65
        tmp2 = tempfile.mkdtemp(prefix="dca_fx2_")
        try:
            f3 = fxmod.fetch_fx(c2, None, base=tmp2, now=now)
            check("B4", "fallback 非 firm", f3.rate == 0.65 and not f3.firm
                  and f3.status == "fallback", f3.note)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)
        c3 = json.loads(json.dumps(c))
        c3["fx"]["fallback_rate"] = None
        tmp3 = tempfile.mkdtemp(prefix="dca_fx3_")
        try:
            f4 = fxmod.fetch_fx(c3, None, base=tmp3, now=now)
            check("B5", "FX 缺失无确定金额", f4.rate is None and not f4.firm
                  and f4.status == "missing", f4.note)
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for b in (2000, 2500, 3000):
        c = cfgv2()
        c["portfolio"]["total_monthly_aud"] = b
        plans = {s: scan.plan_for(c, s, 0.65, "test", None, True)
                 for s in c["symbols"]}
        ok = (abs(sum(p.aud for p in plans.values()) - b) < 0.01
              and plans["VOO"].aud == round(b * 0.75, 2)
              and abs(plans["SMH"].usd - round(b * 0.20 * 0.65, 2)) < 0.01)
        check("B7", f"A${b} 预算拆分/折算", ok,
              f"VOO A${plans['VOO'].aud} / US${plans['VOO'].usd}")


# ---------------------------------------------------------------------------
def suite_ledger():
    print("\n[C] 台账 / 幂等")
    tmp = tempfile.mkdtemp(prefix="dca_led_")
    db = os.path.join(tmp, "dca.sqlite")
    try:
        L = Ledger(db)
        L.set_budget_version("2026-11", 2500, {"VOO": 0.75, "SMH": 0.2, "TSLA": 0.05})
        o = L.create_order("VOO", 1875, obligation_months=["2026-11"],
                           fx_rate=0.65, order_id="o1")
        f1 = L.add_fill("o1", amount_aud=1000, price_usd=600,
                        trade_date="2026-11-03", fx_rate=0.65,
                        commission_usd=1, fill_id="f1")
        check("C2", "部分成交 pending 875",
              abs(L.pending_by_month("2026-11", "VOO") - 875) < 0.01
              and L.get_order("o1")["status"] == "partial")
        f1b = L.add_fill("o1", amount_aud=1000, amount_usd=f1["amount_usd"],
                         price_usd=600, trade_date="2026-11-03", fx_rate=0.65,
                         commission_usd=1, fill_id="f1")
        check("C4", "重复 fill_id 幂等", len(L.fills_for_order("o1")) == 1)
        try:
            L.add_fill("o1", amount_aud=1000, price_usd=600,
                       trade_date="2026-11-04", fx_rate=0.65)
            ok = False
        except Exception:
            ok = True
        check("C7", "超买拒绝", ok)
        L.add_fill("o1", amount_aud=875, price_usd=601,
                   trade_date="2026-11-04", fx_rate=0.65,
                   commission_usd=1, fill_id="f2")
        check("C1", "补足后 filled", L.get_order("o1")["status"] == "filled")
        # 多月真实成交 + allocations
        o2 = L.create_order("TSLA", 375, obligation_months=["2026-09", "2026-10", "2026-11"],
                            fx_rate=0.65, order_id="o2")
        L.add_fill("o2", amount_aud=375, price_usd=300,
                   trade_date="2026-11-20", fx_rate=0.65,
                   commission_usd=1, fill_id="b1")
        ok = all(abs(L.month_allocated_aud(m, "TSLA") - 125) < 0.01
                 for m in ("2026-09", "2026-10", "2026-11"))
        check("C12", "合并只写一条真实 fill + 多个月 allocation", ok,
              f"fills={len(L.all_fills())}")
        o3 = L.create_order("SMH", 500, obligation_months=["2026-11"],
                            fx_rate=0.65, order_id="o3")
        check("C9", "placed 只算 pending", L.pending_by_month("2026-11", "SMH") == 500
              and L.month_allocated_aud("2026-11", "SMH") == 0)
        L.cancel_order("o3")
        check("C3", "取消后 pending 归零", L.pending_by_month("2026-11", "SMH") == 0)
        # 实际美元金额反推汇率方向
        o4 = L.create_order("SMH", 125, obligation_months=["2026-12"], order_id="o4")
        f4 = L.add_fill("o4", amount_aud=125, amount_usd=80, price_usd=100,
                        trade_date="2026-12-03", commission_usd=1, fill_id="f4")
        check("B8", "实际美元金额反推 FX 方向", abs(f4["fx_rate"] - 81.0 / 125.0) < 1e-9,
              f"fx={f4['fx_rate']:.6f}（应={(80+1)/125:.6f}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def suite_obligation():
    print("\n[D] 义务 / 欠额")
    c = cfgv2()
    tmp = tempfile.mkdtemp(prefix="dca_ob_")
    try:
        L = Ledger(os.path.join(tmp, "dca.sqlite"))
        L.set_budget_version("2026-09", 2500, c["portfolio"]["weights"])
        ob = scan.month_obligation(c, L, "2026-11", fxobj())
        voo = ob["symbols"]["VOO"]
        check("D2", "漏 2 个月 → 欠额累计", abs(voo["arrears_aud"] - 3750) < 0.01
              and abs(voo["required_aud"] - 5625) < 0.01, f"required={voo['required_aud']}")
        # 只投当月，欠额保留
        L.create_order("VOO", 1875, obligation_months=["2026-11"], order_id="c1")
        L.add_fill("c1", amount_aud=1875, price_usd=600, trade_date="2026-11-03",
                   fx_rate=0.65, commission_usd=1, fill_id="cf1")
        ob2 = scan.month_obligation(c, L, "2026-11", fxobj())
        check("D4", "欠额不因投当月自动清除", ob2["symbols"]["VOO"]["arrears_aud"] > 0,
              f"arrears={ob2['symbols']['VOO']['arrears_aud']}")
        # 显式 write-off 才减少
        before = ob2["symbols"]["VOO"]["arrears_aud"]
        L.add_write_off("2026-09", "VOO", 1875, "test write-off")
        ob3 = scan.month_obligation(c, L, "2026-11", fxobj())
        check("D4", "显式 write-off 才减少欠额",
              abs(ob3["symbols"]["VOO"]["arrears_aud"] - (before - 1875)) < 0.01,
              f"{before} -> {ob3['symbols']['VOO']['arrears_aud']}")
        # 单笔补投上限
        c2 = json.loads(json.dumps(c))
        c2["decision"]["max_catchup_per_order_months"] = 1
        style = c2["styles"]["index"]
        plan = scan.plan_for(c2, "VOO", 0.65, "test", None, True,
                             month="2026-11", ledger=L)
        timing = {"can_fill_now": True, "can_place_order": True,
                  "next_fill_et": datetime(2026, 11, 13, 15, 50, tzinfo=ET),
                  "next_fill_local": None, "execution_mode": "live", "fx_firm": True}
        code, text, rec_aud, ratio, reasons = scan.decide(
            ob3["symbols"]["VOO"]["required_aud"], style, 10, 0,
            ob3["symbols"]["VOO"], plan, timing, c2)
        check("D3", "单笔补投受 cap 限制但仍有欠额", code == "BUY_NOW"
              and rec_aud <= plan.aud + 0.01, f"code={code} rec={rec_aud}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def suite_decision():
    print("\n[E] 决策 / 评分")
    c = cfgv2()
    style = c["styles"]["cyclical"]
    plan = scan.plan_for(c, "SMH", 0.65, "test", None, True)
    ob = {"symbol": "SMH", "month": "2026-11", "base_aud": 500, "arrears_aud": 0,
          "required_aud": 500, "filled_aud": 0, "pending_aud": 0,
          "writeoff_aud": 0, "remaining_aud": 500, "remaining_usd": 325,
          "status": "DUE", "earliest_arrears_month": None,
          "tolerance_aud": 2.5}
    t = {"can_fill_now": True, "can_place_order": True, "next_fill_et": None,
         "next_fill_local": None, "execution_mode": "live", "fx_firm": True}
    code, *_ = scan.decide(60, style, 1, 10, ob, plan, t, c)
    check("E2", "评分达标提前 BUY", code == "BUY", code)
    t2 = dict(t); t2["can_fill_now"] = False
    t2["next_fill_et"] = datetime(2026, 11, 13, 9, 30, tzinfo=ET)
    code, *_ = scan.decide(20, style, 1, 10, ob, plan, t2, c)
    check("E5", "不可成交 → NEXT_SESSION", code == "NEXT_SESSION", code)
    t3 = dict(t); t3["fx_firm"] = False
    code, *_ = scan.decide(60, style, 1, 10, ob, plan, t3, c)
    check("E6", "FX 非 firm → REVIEW_FX", code == "REVIEW_FX", code)
    code, *_ = scan.decide(20, style, 3, 1, ob, plan, t, c)
    check("E2", "窗口末日必投", code == "BUY_NOW", code)


# ---------------------------------------------------------------------------
def suite_backtest():
    print("\n[F] T+1 回测")
    x = bt.xirr([(date(2020, 1, 1), -100), (date(2021, 1, 1), 110)])
    check("F4", "XIRR 已知案例 ≈10%", x is not None and abs(x - 0.10) < 0.01, f"{x}")
    md = bt.max_drawdown_twr(
        [(date(2020, 1, 1), 100), (date(2020, 1, 2), 90), (date(2020, 1, 3), 110)], {})
    check("F5", "TWR 最大回撤 -10%", abs(md + 0.10) < 1e-9, f"{md}")
    c = cfgv2()
    data, dates = bt.load_market(c)
    sm = bt.score_start_month(data, dates)
    dates = [d for d in dates if scan.month_key(d) >= sm]
    months = bt.month_list(dates)
    c["portfolio"]["start_month"] = sm
    fx, fbf = bt.load_fx(c)
    w = c["portfolio"]["weights"]
    r = bt.simulate_score(c, data, dates, months, "hard_score", 2500, w, fx, fbf)
    pairs = []
    for f in r.fills:
        o = next((o for o in r.orders if o["order_id"] == f["order_id"]), None)
        if o:
            pairs.append((o["signal_date"], f["trade_date"]))
    ok = bool(pairs) and all(td > sd for sd, td in pairs)
    check("F1", "T+1：所有成交 trade_date > signal_date", ok,
          f"{len(pairs)} 笔；示例 {pairs[:2]}")
    alloc_ok = True
    for f in r.fills:
        o = next((o for o in r.orders if o["order_id"] == f["order_id"]), None)
        if o and o["signal_date"][:7] not in f.get("months", []):
            alloc_ok = False
    check("F2", "成交 allocation 归信号月（含跨月）", alloc_ok,
          "每笔 fill 的 months 包含 signal_date 所在月")
    check("F8", "硬约束 108/108 投满", r.months_funded == r.months_total,
          f"{r.months_funded}/{r.months_total}, 欠额={r.ending_arrears:.2f}")


# ---------------------------------------------------------------------------
def suite_report():
    print("\n[G] 报告 / 日报")
    def run(args):
        p = subprocess.run([sys.executable, os.path.join(BASE, "scripts", "scan.py")] + args,
                           cwd=BASE, capture_output=True, text=True, timeout=240)
        return p.returncode, p.stdout + p.stderr
    rc, out = run(["--now-et", "2026-11-30 16:30", "--no-write"])
    check("G2", "月末收盘后不出现“今日执行”", rc == 0 and "今日执行" not in out
          and "下一交易时段" in out)
    rc, out = run(["--digest", "--now-et", "2026-11-27 14:00", "--no-write"])
    first = out.splitlines()[0] if out else ""
    check("A9", "提前收盘日报覆盖 11/27", rc == 0 and "2026-11-27" in first, first)
    rc, out = run(["--now-et", "2026-11-12 15:50", "--no-write"])
    check("G1", "报告含硬性义务/A$/US$", "本月硬性义务" in out and "A$" in out
          and "US$" in out)


# ---------------------------------------------------------------------------
def main():
    print("v2 测试矩阵 (verify/test_matrix.py)")
    suite_clock()
    suite_fx()
    suite_ledger()
    suite_obligation()
    suite_decision()
    suite_backtest()
    suite_report()
    counts = {k: sum(1 for r in RESULTS if r["st"] == k)
              for k in ("PASS", "FAIL", "SKIP")}
    print("\n" + "=" * 72)
    print(f"结果：PASS {counts['PASS']} / FAIL {counts['FAIL']} / SKIP {counts['SKIP']}")
    print("=" * 72)
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「美股定投雷达」可视化面板（单文件静态 HTML）

用法
----
  python3 scripts/dashboard.py              # 生成 dashboard/index.html
  python3 scripts/dashboard.py --open       # 生成并打开浏览器
  python3 scripts/dashboard.py --serve      # 启动本地记账服务（执行窗口可直接写入台账）
  python3 scripts/dashboard.py --serve --open
  python3 scripts/dashboard.py --quiet      # 只输出一行状态
  python3 scripts/dashboard.py --now-et "2026-11-13 15:50"   # 回放测试

数据全部来自本地：config.json、data/cache、data/signals.csv、data/dca_log.csv、reports/。
页面为静态 HTML（内嵌 JSON + 原生 SVG 图表），无需联网、无外部依赖。

--serve 只是在本机 127.0.0.1 上起一个记账小服务：
  GET  /               面板（dashboard/index.html）
  GET  /api/ping       探活，页面据此判断能否直接记账
  POST /api/confirm    {"entries":[{"symbol":"VOO","amount":1875}]} → 写入 data/dca_log.csv
  写入成功后自动重新生成 dashboard/index.html，页面刷新即可看到最新进度。
"""

import argparse
import csv
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE, "scripts")
sys.path.insert(0, SCRIPTS)
import scan  # noqa: E402

TEMPLATE = os.path.join(SCRIPTS, "dashboard_template.html")
OUT_DIR = os.path.join(BASE, "dashboard")
OUT_FILE = os.path.join(OUT_DIR, "index.html")
DEFAULT_PORT = 8765
TOKEN_NAME = ".exec_token"

REPORT_ICONS = {"digest": "📊", "close": "🌙", "midday": "🌤", "open": "☀️"}
REPORT_LABELS = {"digest": "收盘日报", "close": "收盘前扫描", "midday": "午盘扫描", "open": "开盘扫描"}


def panel_token(cfg):
    """执行窗口写入令牌：首次生成面板时创建（0600），之后静态页面与记账服务共用。

    页面内嵌该令牌；其它网页拿不到它（面板 HTML 不开放跨域读取），
    所以即使服务在运行，别的网站也无法冒充页面写台账。
    """
    path = os.path.join(BASE, cfg["data_dir"], TOKEN_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
    except OSError:
        pass
    return tok


def read_signals(cfg):
    path = os.path.join(BASE, cfg["data_dir"], "signals.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_reports(cfg):
    d = os.path.join(BASE, cfg["reports_dir"])
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not name.endswith(".md") or name.startswith("latest"):
            continue
        kind = name.rsplit("_", 1)[-1][:-3]
        mtime = datetime.fromtimestamp(os.path.getmtime(os.path.join(d, name)))
        out.append({
            "name": name,
            "path": f"reports/{name}",
            "kind": kind,
            "ico": REPORT_ICONS.get(kind, "📄"),
            "label": REPORT_LABELS.get(kind, kind),
            "mtime": mtime.strftime("%m/%d %H:%M"),
            "_ts": mtime.timestamp(),
        })
    out.sort(key=lambda r: r["_ts"], reverse=True)
    for r in out:
        r.pop("_ts", None)
    return out[:12]


def build_history(cfg, item, today):
    rows, _ = scan.fetch_daily_cached(cfg, item["symbol"], cfg["symbols"].get(item["symbol"], {}))
    rows = [r for r in rows if r["date"] < today]
    closes = np.array([r["close"] for r in rows], dtype=float)
    dates = [r["date"].isoformat() for r in rows]
    if not len(closes):
        return None
    series = np.append(closes, item["price"])
    dates = dates + [today.isoformat()]
    ma20 = np.full(len(series), np.nan)
    ma50 = np.full(len(series), np.nan)
    for i in range(19, len(series)):
        ma20[i] = series[i - 19:i + 1].mean()
    for i in range(49, len(series)):
        ma50[i] = series[i - 49:i + 1].mean()

    n = min(126, len(series))
    def rnd(arr):
        return [None if np.isnan(v) else round(float(v), 2) for v in arr[-n:]]
    return {
        "dates": dates[-n:],
        "close": [round(float(v), 2) for v in series[-n:]],
        "ma20": rnd(ma20),
        "ma50": rnd(ma50),
    }


def build_payload(cfg, et_now, port=DEFAULT_PORT):
    ctx = scan.collect(cfg, et_now, "close")
    if not (ctx.get("clock") or {}).get("calendar_supported", True):
        raise RuntimeError(ctx.get("calendar_warning") or "交易日历不支持该日期")
    today = ctx["today"]
    fx = ctx.get("fx") or {}
    try:
        fx_rate = float(fx.get("rate") or 0)
    except (TypeError, ValueError):
        fx_rate = 0.0
    obligation = ctx.get("obligation_summary") or {}

    items = []
    for it in ctx["items"]:
        m = it["metrics"]
        items.append({
            "symbol": it["symbol"], "name": it["name"], "style_label": it["style_label"],
            "price": round(it["price"], 2), "change_pct": round(it["change_pct"], 6),
            "score": round(it["score"], 1), "verdict": it["verdict"],
            "verdict_text": it["verdict_text"], "ratio": round(it["ratio"], 3),
            "plan_usd": None if it.get("plan_usd") is None else round(it["plan_usd"], 2),
            "executed_usd": None if it.get("executed_usd") is None else round(it["executed_usd"], 2),
            "remaining_usd": (None if it.get("plan_usd") is None or it.get("executed_usd") is None
                              else round(max(0.0, it["plan_usd"] - it["executed_usd"]), 2)),
            "plan_aud": round(float(it.get("plan_aud") or 0), 2),
            "executed_aud": round(float(it.get("executed_aud") or 0), 2),
            "remaining_aud": round(float(it.get("remaining_aud") or 0), 2),
            "recommend_aud": round(float(it.get("recommend_aud") or 0), 2),
            "fx_rate": round(float((it.get("obligation") or {}).get("fx_rate") or fx_rate), 6),
            "invested": it["invested"], "trigger": round(it["trigger"], 2),
            "commission_usd": round(float(it.get("commission_usd") or 0), 2),
            "commission_pct": round(float(it.get("commission_pct") or 0), 6),
            "batch": it.get("batch"),
            "day_high": None if it["day_high"] is None else round(float(it["day_high"]), 2),
            "day_low": None if it["day_low"] is None else round(float(it["day_low"]), 2),
            "prev_close": round(it["prev_close"], 2),
            "perf_1m": it["perf_1m"], "perf_3m": it["perf_3m"], "perf_1y": it["perf_1y"],
            "volume_ratio": it["volume_ratio"],
            "metrics": {
                "pos52": round(m["pos52"], 4), "rank60": round(m["rank60"], 4),
                "rsi": round(m["rsi"], 2), "ma20": round(m["ma20"], 2),
                "ma50": round(m["ma50"], 2), "ma200": round(m["ma200"], 2),
                "dd52": round(m["dd52"], 4), "z20": round(m["z20"], 3),
                "s52": round(m["s52"], 1), "s60": round(m["s60"], 1),
                "sz20": round(m["sz20"], 1), "srsi": round(m["srsi"], 1),
                "sdd": round(m["sdd"], 1), "svix": round(m["svix"], 1),
                "hi52": round(m["hi52"], 2), "lo52": round(m["lo52"], 2),
            },
            "history": build_history(cfg, it, today),
        })

    # 评分轨迹（每次扫描的 3 个标的）
    signals = read_signals(cfg)
    by_ts = {}
    for r in signals:
        try:
            by_ts.setdefault(r["et_datetime"], {})[r["symbol"]] = float(r["score"])
        except (KeyError, ValueError):
            continue
    ts_list = list(by_ts.keys())[-40:]
    score_history = {
        "timestamps": ts_list,
        "series": {s: [by_ts[t].get(s) for t in ts_list] for s in cfg["symbols"]},
    }

    sessions = []
    for r in signals[-200:]:
        try:
            sessions.append({
                "t": r["et_datetime"], "session": r.get("session", ""),
                "symbol": r["symbol"], "price": round(float(r["price"]), 2),
                "score": float(r["score"]), "verdict": r.get("verdict", ""),
            })
        except (KeyError, ValueError):
            continue

    today_sessions = {}
    for r in signals:
        if r["et_datetime"].startswith(today.isoformat()):
            try:
                today_sessions[r.get("session", "")] = float(r["score"])
            except ValueError:
                pass

    cw = scan.contribution_weights(cfg, ctx["ledger"])
    weights = {s: float(cfg["portfolio"].get("weights", {}).get(s, 0)) for s in cfg["symbols"]}
    plan_total = sum(it["plan_usd"] or 0 for it in items)
    executed_total = sum(it["executed_usd"] or 0 for it in items)
    plan_total_aud = sum(it["plan_aud"] or 0 for it in items)
    executed_total_aud = sum(it["executed_aud"] or 0 for it in items)

    # 下单窗口文案
    after_close = ctx["after_close"] or not ctx["trading_today"]
    nxt = scan.next_trading_day(today) if after_close else today
    nxt_open_et = scan.et_open(nxt)
    nxt_open_local = nxt_open_et.astimezone(scan.LOCAL_TZ)
    if after_close:
        order_window = (f"美股已收盘，无法成交。请在墨尔本白天挂限价/次日开盘单，"
                        f"指向 美东 {nxt_open_et.strftime('%m/%d')} 09:30 = 墨尔本 {nxt_open_local.strftime('%m/%d %H:%M')}。")
    elif et_now < scan.et_open(today):
        open_local = scan.et_open(today).astimezone(scan.LOCAL_TZ)
        order_window = (f"美股盘前，尚未开盘。今天开盘 美东 09:30 = 墨尔本 "
                        f"{open_local.strftime('%m/%d %H:%M')}，可直接挂开盘单。")
    else:
        order_window = "美股正在交易中，可直接下单；成交后记得记账。"

    # 本月交易日日历（窗口/今天/已执行）
    mdays = scan.month_trading_days(today)
    window_days = max([int(v.get("early_days", 1)) for v in cfg["styles"].values()] + [1])
    exec_dates = {}
    for r in ctx["ledger"]:
        if r.get("month") != ctx["month"]:
            continue
        try:
            d = datetime.strptime(r.get("et_date") or "", "%Y-%m-%d").date()
        except ValueError:
            continue
        d = scan.last_trading_day_on_or_before(d)
        exec_dates.setdefault(d.isoformat(), set()).add(r.get("symbol"))
    phase = "early" if ctx["day_idx"] <= window_days else "late"
    calendar = {"window_days": window_days, "days": [{
        "date": d.isoformat(), "dom": d.day, "idx": i + 1,
        "today": d == today, "window": i < window_days,
        "executed": d.isoformat() in exec_dates,
    } for i, d in enumerate(mdays)]}

    month_label = f"{today.year} 年 {today.month} 月"
    total_days = ctx["day_idx"] + ctx["days_left"] - (1 if ctx["trading_today"] else 0)

    # 最近记账（执行窗口下方展示；amount 兼容旧字段，主口径为 AUD）
    recent_entries = []
    for r in ctx["ledger"][-10:]:
        try:
            aud = r.get("amount_aud")
            if aud is None:
                aud = r.get("amount")
            usd = r.get("amount_usd")
            recent_entries.append({
                "confirmed_at": r.get("confirmed_at_et") or r.get("et_date", ""),
                "et_date": r.get("et_date") or r.get("trade_date", ""),
                "trade_date": r.get("trade_date") or r.get("et_date", ""),
                "symbol": r.get("symbol", ""),
                "price": round(float(r.get("price") or 0), 2),
                "amount": round(float(aud or 0), 2),
                "amount_aud": round(float(aud or 0), 2),
                "amount_usd": round(float(usd or 0), 2),
                "status": r.get("status") or "filled",
                "note": r.get("note", ""),
            })
        except (TypeError, ValueError):
            continue
    recent_entries.reverse()

    return {
        "generated_et": et_now.strftime("%Y-%m-%d %H:%M"),
        "generated_local": et_now.astimezone(scan.LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        "today_et": today.isoformat(),
        "session_label": ctx["session_label"],
        "market_state": ctx["state"],
        "market_state_cn": scan.market_state_cn(ctx.get("clock") or ctx["state"]),
        "vix": round(ctx["vix"], 2),
        "vix_chg5": None if ctx["vix_chg5"] is None else round(ctx["vix_chg5"], 4),
        "month_label": month_label,
        "day_idx": ctx["day_idx"],
        "days_left": ctx.get("days_left_after", ctx["days_left"]),
        "total_days": ctx.get("total_days") or total_days,
        "last_trading_day": ctx["last_trading_day"].strftime("%m/%d"),
        "weights": weights,
        "currency": ctx.get("currency") or cfg.get("currency") or {"budget": "AUD", "quote": "USD", "display": "both"},
        "fx": {k: fx.get(k) for k in ("rate", "status", "firm", "note", "as_of_et", "source", "age_hours")},
        "obligation": obligation,
        "plan_total": round(plan_total, 2),
        "executed_total": round(executed_total, 2),
        "plan_total_aud": round(plan_total_aud, 2),
        "executed_total_aud": round(executed_total_aud, 2),
        "items": items,
        "today_sessions": today_sessions,
        "score_history": score_history,
        "sessions": sessions,
        "reports": read_reports(cfg),
        "contribution": None if cw is None else {
            "weights": {s: round(v, 4) for s, v in cw[0].items()},
            "sums": {s: round(v, 2) for s, v in cw[1].items()},
            "total": round(cw[2], 2),
        },
        "calendar": calendar,
        "phase": phase,
        "commission_cfg": cfg.get("commission", {}),
        "exec_api": f"http://127.0.0.1:{int(port)}",
        "exec_token": panel_token(cfg),
        "recent_entries": recent_entries,
        "next_open_et": nxt_open_et.strftime("%m/%d %H:%M"),
        "next_open_local": nxt_open_local.strftime("%m/%d %H:%M"),
        "order_window": order_window,
        "errors": ctx["errors"],
    }


def render(payload):
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", data)
    if "/*__DATA__*/" in html:
        raise RuntimeError("模板占位符替换失败")
    return html


def regenerate(cfg, out_path=OUT_FILE, et_now=None, port=DEFAULT_PORT):
    """重新生成面板并原子写入（POST 成功后调用，刷新页面即可看到新进度）。

    行情全部获取失败（items 为空）时保留旧面板，避免把好面板覆盖成空页。
    """
    et_now = et_now or datetime.now(scan.ET_TZ)
    payload = build_payload(cfg, et_now, port)
    html = render(payload)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if not payload["items"] and os.path.exists(out_path):
        raise RuntimeError("未获取到任何行情（0 个标的），保留旧面板")
    tmp = f"{out_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_path)
    return payload


# ---------------------------------------------------------------------------
# 本地记账服务（--serve）：执行窗口 → data/dca_log.csv
# ---------------------------------------------------------------------------
class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, cfg, out_path, token, port):
        super().__init__(addr, handler)
        self.cfg = cfg
        self.out_path = out_path
        self.token = token
        self.port = port
        self.write_lock = threading.Lock()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "USDCARadar/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 静默，避免刷屏
        pass

    # -- response helpers --------------------------------------------------
    def _send(self, code, body, ctype, extra=None, cors=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            # 只给 ping / 记账接口开放跨域，面板与报告不开放（防止其它网站读取持仓）
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DCA-Token")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, cors=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", cors=cors)

    def _file(self, path, ctype, cors=False):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"ok": False, "error": "文件不存在"}, cors=cors)
        self._send(200, body, ctype, cors=cors)

    def _origin_ok(self):
        """只允许本机页面（file:// 的 Origin: null 或 127.0.0.1/localhost）访问写接口。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # curl / 本地脚本
        if origin == "null":
            return True  # 直接打开的静态文件
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            return False
        return host in ("127.0.0.1", "localhost", "::1")

    def _write_allowed(self):
        """写接口的完整体检：本机来源 + 页面内嵌令牌。

        令牌是关键防线：别的网页即使能伪造 Origin: null（sandbox iframe），
        也拿不到面板 HTML 里的令牌，无法写台账；无 Origin 的本地脚本/curl 不受影响。
        """
        if not self._origin_ok():
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True  # curl / 本地脚本
        token = self.headers.get("X-DCA-Token") or ""
        return bool(token) and hmac.compare_digest(token, self.server.token)

    # -- routes ------------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            if not os.path.exists(self.server.out_path):
                try:
                    with self.server.write_lock:
                        regenerate(self.server.cfg, self.server.out_path, port=self.server.port)
                except Exception as exc:  # noqa: BLE001
                    return self._json(503, {"ok": False, "error": f"面板生成失败：{exc}"})
            return self._file(self.server.out_path, "text/html; charset=utf-8", cors=False)
        if path == "/api/ping":
            return self._json(200, {
                "ok": True, "service": "us-dca-dashboard", "version": 2,
                "write_ok": self._write_allowed(),
                "panel": os.path.relpath(self.server.out_path, BASE),
                "ledger": os.path.relpath(scan.ledger_path(self.server.cfg), BASE),
            })
        if path.startswith("/reports/"):
            name = os.path.basename(path)
            f = os.path.join(BASE, self.server.cfg["reports_dir"], name)
            if not os.path.isfile(f):
                return self._json(404, {"ok": False, "error": "报告不存在"}, cors=False)
            return self._file(f, "text/markdown; charset=utf-8", cors=False)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/api/confirm":
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._write_allowed():
            self.close_connection = True
            return self._json(403, {"ok": False, "error": "拒绝写入：来源不被允许或缺少页面令牌"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            return self._json(400, {"ok": False, "error": "空请求"})
        if length > 65536:
            self.close_connection = True
            return self._json(413, {"ok": False, "error": "请求过大"})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "JSON 解析失败"})
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            return self._json(400, {"ok": False, "error": "请至少选择一个标的并填写金额"})
        if len(entries) > 20:
            return self._json(400, {"ok": False, "error": "条目过多"})
        clean = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            clean.append({
                "symbol": str(e.get("symbol") or "").upper(),
                "amount": e.get("amount"),          # AUD 义务金额
                "price": e.get("price"),            # 实际成交价（USD）
                "fx_rate": e.get("fx_rate"),        # 页面汇率（AUD→USD），可空=服务端自动取
                "amount_usd": e.get("amount_usd"),  # 实际美元金额（可选）
                "trade_date": e.get("trade_date"),  # 回填成交日（可选）
                "status": e.get("status") or "filled",
                "filled_aud": e.get("filled_aud"),
                "months": e.get("months"),
                "idempotency_key": e.get("idempotency_key"),
                "note": e.get("note"),
            })
        if not clean:
            return self._json(400, {"ok": False, "error": "没有有效的执行记录"})

        cfg = self.server.cfg
        month = scan.month_key(datetime.now(scan.ET_TZ).date())
        already = scan.month_entries(scan.load_ledger(cfg), month)
        dups = [e["symbol"] for e in clean if e["symbol"] in already]
        try:
            with self.server.write_lock:
                recorded, skipped = scan.record_confirmations(
                    cfg, clean, note=str(data.get("note") or "面板执行窗口")[:120])
                if not recorded:
                    msg = "；".join(f"{s['symbol']}：{s['reason']}" for s in skipped) or "没有可写入的记录"
                    self.close_connection = True
                    return self._json(400, {"ok": False, "error": msg})
                refresh_warn = ""
                try:
                    regenerate(cfg, self.server.out_path, port=self.server.port)
                except Exception as exc:  # noqa: BLE001
                    refresh_warn = f"（面板刷新失败：{exc}，稍后重新生成即可）"
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": f"写入台账失败：{exc}"})

        ymd = datetime.now(scan.ET_TZ).strftime("%m/%d %H:%M")
        def _aud(r):
            return float(r.get("amount_aud", r.get("amount") or 0) or 0)
        total = sum(_aud(r) for r in recorded)
        msg = "已记录 " + " / ".join(
            f"{r['symbol']} A${_aud(r):,.2f}" for r in recorded) + f"，合计 A${total:,.2f}（美东 {ymd}）"
        if skipped:
            msg += "；跳过 " + "、".join(f"{s['symbol']}（{s['reason']}）" for s in skipped)
        if dups:
            msg += f"；注意：{'、'.join(dups)} 本月已有记录，已追加一条"
        return self._json(200, {
            "ok": True, "message": msg + refresh_warn,
            "recorded": recorded, "skipped": skipped,
        })


def serve(cfg, host="127.0.0.1", port=DEFAULT_PORT, out_path=OUT_FILE,
          open_browser=False, et_now=None):
    """启动本地记账服务；启动时先刷新一次面板，保证页面数据是新的。"""
    if not (1 <= int(port) <= 65535):
        print(f"端口无效：{port}（应为 1-65535）", file=sys.stderr)
        return 2
    port = int(port)
    token = panel_token(cfg)
    try:
        payload = regenerate(cfg, out_path, et_now, port=port)
        print(f"已刷新面板：{os.path.relpath(out_path, BASE)}"
              f"（{len(payload['items'])} 标的，{payload['generated_local']}）")
    except Exception as exc:  # noqa: BLE001
        if os.path.exists(out_path):
            print(f"提示：面板实时刷新失败（{exc}），继续使用现有面板。")
        else:
            print(f"提示：面板尚未生成（{exc}），访问首页会重试。")
    try:
        httpd = DashboardServer((host, port), DashboardHandler, cfg, out_path, token, port)
    except (OSError, OverflowError) as exc:
        print(f"启动失败：无法监听 {host}:{port}（{exc}）。"
              f"可能端口已被占用，可换一个：python3 scripts/dashboard.py --serve --port 8766",
              file=sys.stderr)
        return 2
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{shown_host}:{port}/"
    print("执行窗口记账服务已启动：" + url)
    print(f"  台账：{os.path.relpath(scan.ledger_path(cfg), BASE)}")
    print(f"  写入令牌：data/{TOKEN_NAME}（面板内嵌，其它网页拿不到）")
    print(f"  面板：{os.path.relpath(out_path, BASE)}（每次记账后自动重新生成）")
    print("  在页面「执行窗口」勾选标的、填金额、点确认即可；Ctrl+C 停止。")
    if open_browser:
        subprocess.run(["open", url], check=False)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止记账服务（台账已保存）。")
    finally:
        httpd.server_close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="生成美股定投雷达可视化面板")
    ap.add_argument("--now-et", help='回放，如 "2026-11-13 15:50"')
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开（配 --serve 时打开服务地址）")
    ap.add_argument("--quiet", action="store_true", help="只输出一行状态")
    ap.add_argument("--out", default=OUT_FILE, help="输出路径")
    ap.add_argument("--serve", action="store_true",
                    help="启动本地记账服务，页面「执行窗口」可直接写入 data/dca_log.csv")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"记账服务端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="记账服务监听地址（默认只监听本机 127.0.0.1）")
    args = ap.parse_args()

    cfg = scan.load_config()
    et_now = scan.parse_et(args.now_et) if args.now_et else None
    if args.serve:
        return serve(cfg, args.host, args.port, args.out, args.open, et_now)

    et_now = et_now or datetime.now(scan.ET_TZ)

    try:
        payload = regenerate(cfg, args.out, et_now)
    except Exception as exc:  # noqa: BLE001
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2

    out_path = args.out
    if args.quiet:
        print(f"[dashboard] {os.path.relpath(out_path, BASE)}  "
              f"({payload['generated_local']} 生成, {len(payload['items'])} 标的)")
    else:
        print(f"已生成：{os.path.relpath(out_path, BASE)}")
        print(f"  时间：{payload['generated_local']}（墨尔本） / {payload['generated_et']}（美东）")
        print(f"  市场：{payload['market_state_cn']} ｜ VIX {payload['vix']}")
        print(f"  本月：第 {payload['day_idx']}/{payload['total_days']} 个交易日，剩余 {payload['days_left']} 个")
        for it in payload["items"]:
            print(f"  {it['symbol']:<5} {it['price']:>9.2f}  评分 {it['score']:>4.1f}  {it['verdict_text']}")
        print(f"  执行窗口记账：python3 scripts/dashboard.py --serve 后从 "
              f"http://127.0.0.1:{DEFAULT_PORT}/ 打开")

    if args.open:
        subprocess.run(["open", out_path], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

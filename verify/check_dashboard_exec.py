#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify/check_dashboard_exec.py —— 面板「执行窗口」+ 本地记账服务核验
====================================================================

只读核验：全部动作在项目临时副本里执行，不触碰真实的 data/dca_log.csv、
dashboard/index.html 或 reports/。

覆盖
----
A. 静态面板生成：dashboard.py 正常退出、占位符已替换、执行窗口/最近记账字段齐全、内嵌 JS 语法正确
B. CLI 记账：--confirm VOO / VOO --amount / VOO:123.45,SMH:600 / all / 未知标的报错
C. --serve 记账服务：/api/ping、GET /、GET /reports/<file>、POST /api/confirm（成功/空/未知标的/金额非法）、
   台账写入、面板自动重新生成、路径穿越防护、端口占用提示
D. 失败请求不污染台账

用法
----
    cd <项目根目录>
    python3 verify/check_dashboard_exec.py            # 跑全部检查
    python3 verify/check_dashboard_exec.py --keep     # 保留临时副本便于排查

退出码：0 = 无 FAIL；1 = 存在 FAIL。
"""

import argparse
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
RESULTS = []


def project_path(*parts):
    return os.path.join(BASE, *parts)


def rec(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name, "status": status, "detail": detail})
    print(f"  [{status}] {section} · {name}" + (f" —— {detail}" if detail else ""))


def ledger_rows(tmp):
    """读临时副本台账：优先 SQLite（fills 表），兼容旧 CSV。"""
    db = os.path.join(tmp, "data", "dca.sqlite")
    if os.path.exists(db):
        import sqlite3
        out = []
        conn = sqlite3.connect(db)
        try:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("select * from fills order by rowid"):
                d = dict(row)
                out.append({
                    "symbol": d.get("symbol"),
                    "amount": d.get("amount_aud"),
                    "amount_aud": d.get("amount_aud"),
                    "amount_usd": d.get("amount_usd"),
                    "price": d.get("price_usd"),
                    "et_date": d.get("trade_date"),
                    "trade_date": d.get("trade_date"),
                    "month": (d.get("trade_date") or "")[:7],
                    "status": "filled",
                    "fill_id": d.get("fill_id"),
                    "confirmed_at_et": d.get("filled_at_et"),
                    "note": d.get("note") or "",
                })
        finally:
            conn.close()
        return out
    path = os.path.join(tmp, "data", "dca_log.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("symbol")]


def run(args, cwd, timeout=180):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(url, payload=None, timeout=60, raw=None, headers=None):
    """返回 (status, text)；HTTP 错误码也返回而不是抛异常。"""
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif raw is not None:
        data = raw
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def http_full(url, payload=None, timeout=60, headers=None):
    """同 http()，但返回 (status, headers, text)，用于检查 CORS 响应头。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = dict(headers or {})
    if payload is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


def wait_ping(base, proc, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"服务提前退出（码 {proc.returncode}）"
        try:
            code, body = http(base + "/api/ping", timeout=3)
            if code == 200 and json.loads(body).get("ok"):
                return True, body
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False, "超时未就绪"


def read_index(tmp):
    path = os.path.join(tmp, "dashboard", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def index_data(tmp):
    html = read_index(tmp)
    m = re.search(r"const DATA = (\{.*?\});\s*\n", html, re.S)
    if not m:
        raise RuntimeError("未找到内嵌 DATA")
    return json.loads(m.group(1))


def section_a(tmp):
    print("\n[A] 静态面板生成（临时副本）")
    p = run(["scripts/dashboard.py", "--now-et", "2026-11-13 15:50"], tmp)
    ok = p.returncode == 0 and os.path.exists(os.path.join(tmp, "dashboard", "index.html"))
    rec("A", "dashboard.py 生成成功", "PASS" if ok else "FAIL",
        f"退出码 {p.returncode}" + ("" if ok else f"；stderr={p.stderr.strip()[:200]}"))
    if not ok:
        return
    html = read_index(tmp)
    checks = [
        ("DATA 占位符已替换", "/*__DATA__*/" not in html),
        ("执行窗口容器存在", 'id="execPanel"' in html),
        ("执行窗口样式存在", ".ex-row" in html),
        ("执行窗口脚本存在", "确认执行并记账" in html),
        ("静态回退提示存在", "未连接记账服务" in html and "复制记账命令" in html),
    ]
    for name, cond in checks:
        rec("A", name, "PASS" if cond else "FAIL")
    token_path = os.path.join(tmp, "data", ".exec_token")
    tok = ""
    mode = 0
    if os.path.exists(token_path):
        tok = open(token_path, encoding="utf-8").read().strip()
        mode = os.stat(token_path).st_mode & 0o777
    rec("A", "写入令牌已生成（data/.exec_token，0600）",
        "PASS" if len(tok) >= 20 and mode == 0o600 else "FAIL",
        f"长度 {len(tok)}，权限 {oct(mode)}")
    d = index_data(tmp)
    fields = [
        ("remaining_usd", all("remaining_usd" in it for it in d["items"])),
        ("plan_aud/remaining_aud", all(("plan_aud" in it and "remaining_aud" in it) for it in d["items"])),
        ("recent_entries", "recent_entries" in d),
        ("exec_api/exec_token", "exec_api" in d and len(d.get("exec_token") or "") >= 20),
        ("today_et", "today_et" in d),
        ("fx/obligation", "fx" in d and "obligation" in d),
        ("plan_total_aud", "plan_total_aud" in d),
    ]
    for name, cond in fields:
        rec("A", f"payload 字段 {name}", "PASS" if cond else "FAIL")

    node = shutil.which("node")
    if not node:
        rec("A", "内嵌 JS 语法", "WARN", "未安装 node，跳过")
        return
    m = re.search(r"<script>(.*)</script>", html, re.S)
    js_path = os.path.join(tmp, "check.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(m.group(1) if m else "")
    p = subprocess.run([node, "--check", js_path], capture_output=True, text=True)
    rec("A", "内嵌 JS 语法（node --check）", "PASS" if p.returncode == 0 else "FAIL",
        p.stderr.strip()[:300])


def section_b(tmp):
    print("\n[B] CLI 记账（临时副本）")
    p = run(["scripts/scan.py", "--confirm", "VOO", "--amount", "1100", "--fx-rate", "0.68"], tmp)
    rows = ledger_rows(tmp)
    ok = p.returncode == 0 and len(rows) == 1 and abs(float(rows[0]["amount"]) - 1100) < 1e-6
    rec("B", "--confirm VOO --amount 1100", "PASS" if ok else "FAIL",
        f"退出码 {p.returncode}，台账 {len(rows)} 条")

    p = run(["scripts/scan.py", "--confirm", "VOO:123.45,SMH:600", "--fx-rate", "0.68"], tmp)
    rows = ledger_rows(tmp)
    amts = {r["symbol"]: float(r["amount"]) for r in rows[-2:]}
    ok = (p.returncode == 0 and len(rows) == 3 and amts.get("VOO") == 123.45
          and amts.get("SMH") == 600.0)
    rec("B", "--confirm VOO:123.45,SMH:600（逐标的 AUD 金额）", "PASS" if ok else "FAIL",
        f"最后两条金额 {amts}")

    p = run(["scripts/scan.py", "--confirm", "all", "--fx-rate", "0.68"], tmp)
    rows = ledger_rows(tmp)
    ok = p.returncode == 0 and len(rows) == 6
    rec("B", "--confirm all（按月度计划）", "PASS" if ok else "FAIL", f"台账 {len(rows)} 条")

    p = run(["scripts/scan.py", "--confirm", "XYZ"], tmp)
    ok = p.returncode != 0 and "未知标的" in (p.stderr + p.stdout)
    rec("B", "未知标的报错（非 0 退出）", "PASS" if ok else "FAIL",
        f"退出码 {p.returncode}，输出 {((p.stderr + p.stdout).strip())[:120]}")

    before = len(ledger_rows(tmp))
    p = run(["scripts/scan.py", "--confirm", "VOO:nan"], tmp)
    ok = p.returncode == 2 and "参数错误" in (p.stderr + p.stdout) and len(ledger_rows(tmp)) == before
    rec("B", "非法金额 VOO:nan 报错且不写台账", "PASS" if ok else "FAIL", f"退出码 {p.returncode}")

    p = run(["scripts/scan.py", "--confirm", ""], tmp)
    ok = p.returncode == 2 and "参数错误" in (p.stderr + p.stdout)
    rec("B", "--confirm 空字符串报错（不再误触发扫描）", "PASS" if ok else "FAIL",
        f"退出码 {p.returncode}")

    p = run(["scripts/scan.py", "--confirm", "VOO", "--amount", "0", "--fx-rate", "0.68"], tmp)
    ok = p.returncode == 2 and len(ledger_rows(tmp)) == before
    rec("B", "--amount 0 不写台账且非 0 退出", "PASS" if ok else "FAIL", f"退出码 {p.returncode}")


def section_c(tmp):
    print("\n[C] --serve 本地记账服务（临时副本）")
    port = free_port()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [PY, "scripts/dashboard.py", "--serve", "--port", str(port)],
        cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        ok, detail = wait_ping(base, proc)
        rec("C", "/api/ping 就绪", "PASS" if ok else "FAIL", detail)
        if not ok:
            return

        code, body = http(base + "/")
        rec("C", "GET / 返回面板", "PASS" if code == 200 and 'id="execPanel"' in body else "FAIL",
            f"HTTP {code}")

        reports = sorted(os.listdir(os.path.join(tmp, "reports"))) if os.path.isdir(
            os.path.join(tmp, "reports")) else []
        if reports:
            code, _ = http(base + "/reports/" + reports[0])
            rec("C", "GET /reports/<file>", "PASS" if code == 200 else "FAIL", f"HTTP {code}")
        else:
            rec("C", "GET /reports/<file>", "WARN", "临时副本无报告，跳过")

        code, _ = http(base + "/reports/../config.json")
        rec("C", "路径穿越被挡住（/reports/../config.json）", "PASS" if code == 404 else "FAIL",
            f"HTTP {code}")

        before = len(ledger_rows(tmp))
        code, body = http(base + "/api/confirm", {"entries": [
            {"symbol": "VOO", "amount": 500, "price": 696.0, "fx_rate": 0.68},
            {"symbol": "TSLA", "amount": 42.5, "price": 397.45, "fx_rate": 0.68}]})
        try:
            j = json.loads(body)
        except ValueError:
            j = {}
        rows = ledger_rows(tmp)
        ok = code == 200 and j.get("ok") and len(rows) == before + 2
        rec("C", "POST 有效记录写入台账", "PASS" if ok else "FAIL",
            f"HTTP {code}，台账 {len(rows)} 条")

        d = index_data(tmp)
        recent = d.get("recent_entries") or []
        hit = [r for r in recent if r.get("symbol") == "TSLA" and abs(float(r.get("amount") or 0) - 42.5) < 1e-6]
        ok = bool(recent) and bool(hit)
        rec("C", "记账后面板自动重新生成", "PASS" if ok else "FAIL",
            f"最近 {len(recent)} 条，命中新记录={hit[:1]}")

        bad_cases = [
            ("空 entries", {"entries": []}),
            ("未知标的", {"entries": [{"symbol": "XYZ", "amount": 100, "price": 1, "fx_rate": 0.68}]}),
            ("金额为 0", {"entries": [{"symbol": "VOO", "amount": 0, "price": 696, "fx_rate": 0.68}]}),
            ("金额非数字", {"entries": [{"symbol": "VOO", "amount": "abc", "price": 696, "fx_rate": 0.68}]}),
        ]
        for name, payload in bad_cases:
            code, body = http(base + "/api/confirm", payload)
            ok = code == 400 and '"ok": false' in body
            rec("C", f"非法请求被拒：{name}", "PASS" if ok else "FAIL", f"HTTP {code}")
        ok = len(ledger_rows(tmp)) == before + 2
        rec("C", "非法请求不污染台账", "PASS" if ok else "FAIL",
            f"台账 {len(ledger_rows(tmp))} 条（应为 {before + 2}）")

        token_path = os.path.join(tmp, "data", ".exec_token")
        token = open(token_path, encoding="utf-8").read().strip() if os.path.exists(token_path) else ""

        code, _ = http(base + "/api/confirm", {"entries": [{"symbol": "SMH", "amount": 10, "price": 501.73, "fx_rate": 0.68}]},
                       headers={"Origin": "https://evil.example"})
        ok = code == 403 and len(ledger_rows(tmp)) == before + 2
        rec("C", "非本机 Origin 的写入被拒", "PASS" if ok else "FAIL", f"HTTP {code}")

        code, _ = http(base + "/api/confirm", {"entries": [{"symbol": "SMH", "amount": 10, "price": 501.73, "fx_rate": 0.68}]},
                       headers={"Origin": "null"})
        ok = code == 403 and len(ledger_rows(tmp)) == before + 2
        rec("C", "Origin: null 但缺少页面令牌被拒", "PASS" if ok else "FAIL", f"HTTP {code}")

        code, _ = http(base + "/api/confirm", {"entries": [{"symbol": "SMH", "amount": 10, "price": 501.73, "fx_rate": 0.68}]},
                       headers={"Origin": "null", "X-DCA-Token": token})
        ok = code == 200 and len(ledger_rows(tmp)) == before + 3
        rec("C", "file:// 页面（Origin: null + 令牌）可写入", "PASS" if ok else "FAIL", f"HTTP {code}")

        bad_price_cases = [
            ("价格为 NaN", {"entries": [{"symbol": "TSLA", "amount": 5, "price": float("nan"), "fx_rate": 0.68}]}),
            ("价格为 Infinity", {"entries": [{"symbol": "TSLA", "amount": 5, "price": float("inf"), "fx_rate": 0.68}]}),
            ("金额为布尔 true", {"entries": [{"symbol": "TSLA", "amount": True, "price": 397.45, "fx_rate": 0.68}]}),
        ]
        for name, payload in bad_price_cases:
            code, _ = http(base + "/api/confirm", payload)
            rec("C", f"非法请求被拒：{name}", "PASS" if code == 400 else "FAIL", f"HTTP {code}")
        ok = len(ledger_rows(tmp)) == before + 3
        rec("C", "非法价格/布尔金额不污染台账", "PASS" if ok else "FAIL",
            f"台账 {len(ledger_rows(tmp))} 条（应为 {before + 3}）")

        code, headers_, _ = http_full(base + "/", headers={"Origin": "https://evil.example"})
        hdrs_lower = {k.lower(): v for k, v in headers_.items()}
        ok = code == 200 and "access-control-allow-origin" not in hdrs_lower
        rec("C", "面板 HTML 不向其它网站开放跨域读取", "PASS" if ok else "FAIL",
            f"HTTP {code}，ACAO={hdrs_lower.get('access-control-allow-origin')!r}")

        code, body = http(base + "/api/ping", headers={"Origin": "https://evil.example"})
        j = json.loads(body) if code == 200 else {}
        rec("C", "ping 对非本机来源报告不可写", "PASS" if j.get("write_ok") is False else "FAIL",
            f"HTTP {code}，write_ok={j.get('write_ok')!r}")

        code, body = http(base + "/api/ping", headers={"Origin": "null", "X-DCA-Token": token})
        j = json.loads(body) if code == 200 else {}
        rec("C", "ping 对页面令牌报告可写", "PASS" if j.get("write_ok") is True else "FAIL",
            f"HTTP {code}，write_ok={j.get('write_ok')!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    # 端口占用提示
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    busy_port = held.getsockname()[1]
    try:
        p = run(["scripts/dashboard.py", "--serve", "--port", str(busy_port)], tmp, timeout=30)
        ok = p.returncode == 2 and "启动失败" in (p.stdout + p.stderr)
        rec("C", "端口占用时友好报错（退出码 2）", "PASS" if ok else "FAIL",
            f"退出码 {p.returncode}")
    finally:
        held.close()


def main():
    ap = argparse.ArgumentParser(description="核验面板执行窗口与本地记账服务")
    ap.add_argument("--keep", action="store_true", help="保留临时副本")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="dca_dash_verify_")
    print("=" * 78)
    print("美股定投雷达 · 执行窗口核验 (verify/check_dashboard_exec.py)")
    print(f"临时副本 : {tmp}")
    print(f"真实台账 : data/dca.sqlite（本次不读写）")
    print("=" * 78)
    try:
        shutil.copytree(project_path("scripts"), os.path.join(tmp, "scripts"))
        shutil.copy2(project_path("config.json"), os.path.join(tmp, "config.json"))
        os.makedirs(os.path.join(tmp, "data"))
        cache = project_path("data", "cache")
        if os.path.isdir(cache):
            shutil.copytree(cache, os.path.join(tmp, "data", "cache"))
        sig = project_path("data", "signals.csv")
        if os.path.exists(sig):
            shutil.copy2(sig, os.path.join(tmp, "data", "signals.csv"))
        reports = project_path("reports")
        if os.path.isdir(reports):
            shutil.copytree(reports, os.path.join(tmp, "reports"))
        os.makedirs(os.path.join(tmp, "dashboard"))

        section_a(tmp)
        section_b(tmp)
        section_c(tmp)
    finally:
        if args.keep:
            print(f"\n[INFO] 临时副本已保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "FAIL", "WARN")}
    print("\n" + "=" * 78)
    print(f"结果：PASS {counts['PASS']} / FAIL {counts['FAIL']} / WARN {counts['WARN']}")
    print("=" * 78)
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

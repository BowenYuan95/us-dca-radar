#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 定投台账：orders / fills / obligation_allocations。

设计要点：
- order 是意图，fill 是真实成交；合并多月只在 allocation 层分摊，绝不生成假历史 fill。
- order_id / fill_id 唯一；idempotency_key 唯一；重复重试 no-op，冲突报错。
- placed 只算 pending，不计入已成交；部分成交/取消/超买均有事务与约束保护。
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime

STATUSES = ("placed", "partial", "filled", "cancelled", "expired")
OPEN_STATUSES = ("placed", "partial")


class LedgerError(RuntimeError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class OverfillError(LedgerError):
    pass


def _now_et():
    from nyse_calendar import ET_TZ
    return datetime.now(ET_TZ)


def _uuid():
    return uuid.uuid4().hex


def _month_bounds(month):
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, end


class Ledger:
    def __init__(self, db_path):
        self.db_path = os.path.abspath(db_path) if db_path != ":memory:" else db_path
        self._ensure_schema()

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self):
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    created_at_et TEXT NOT NULL,
                    signal_date TEXT,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    planned_amount_aud REAL NOT NULL,
                    planned_amount_usd REAL,
                    limit_price_usd REAL,
                    fx_rate REAL,
                    fx_source TEXT,
                    obligation_months_json TEXT NOT NULL,
                    broker_order_id TEXT,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    filled_at_et TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price_usd REAL NOT NULL,
                    amount_usd REAL NOT NULL,
                    amount_aud REAL NOT NULL,
                    fx_rate REAL NOT NULL,
                    commission_usd REAL NOT NULL DEFAULT 0,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS obligation_allocations (
                    fill_id TEXT NOT NULL REFERENCES fills(fill_id),
                    obligation_month TEXT NOT NULL,
                    amount_aud REAL NOT NULL,
                    amount_usd REAL NOT NULL,
                    PRIMARY KEY (fill_id, obligation_month)
                );
                CREATE TABLE IF NOT EXISTS budget_versions (
                    effective_month TEXT PRIMARY KEY,
                    total_monthly_aud REAL NOT NULL,
                    weights_json TEXT NOT NULL,
                    created_at_et TEXT NOT NULL,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_state (
                    symbol TEXT PRIMARY KEY,
                    start_month TEXT NOT NULL,
                    months_accrued INTEGER NOT NULL,
                    target_months INTEGER NOT NULL,
                    due_month TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at_et TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS write_offs (
                    write_off_id TEXT PRIMARY KEY,
                    obligation_month TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    amount_aud REAL NOT NULL,
                    reason TEXT NOT NULL,
                    created_at_et TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
                CREATE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_date);
                CREATE INDEX IF NOT EXISTS idx_alloc_month ON obligation_allocations(obligation_month);
                """
            )

    # ---------------------------------------------------------------- budgets
    def set_budget_version(self, month, total_monthly_aud, weights, note=""):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO budget_versions"
                "(effective_month,total_monthly_aud,weights_json,created_at_et,note)"
                " VALUES(?,?,?,?,?)",
                (month, float(total_monthly_aud), json.dumps(weights, sort_keys=True),
                 _now_et().strftime("%Y-%m-%d %H:%M"), note or ""),
            )
        return self.budget_for(month)

    def budget_for(self, month):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM budget_versions WHERE effective_month<=?"
                " ORDER BY effective_month DESC LIMIT 1", (month,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["weights"] = json.loads(d.pop("weights_json") or "{}")
        return d

    # ----------------------------------------------------------------- orders
    def _order_row(self, conn, order_id):
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def get_order(self, order_id):
        with self._conn() as conn:
            d = self._order_row(conn, order_id)
        if d:
            d["obligation_months"] = json.loads(d.pop("obligation_months_json") or "[]")
        return d

    def create_order(self, symbol, planned_amount_aud, planned_amount_usd=None,
                     status="placed", signal_date=None, obligation_months=None,
                     limit_price_usd=None, fx_rate=None, fx_source=None,
                     order_id=None, idempotency_key=None, note=""):
        symbol = str(symbol or "").upper()
        if not symbol:
            raise LedgerError("symbol 不能为空")
        if status not in STATUSES:
            raise LedgerError(f"非法 status：{status}")
        planned_amount_aud = float(planned_amount_aud)
        if planned_amount_aud <= 0:
            raise LedgerError("planned_amount_aud 必须 > 0")
        months = list(obligation_months or [])
        if not months:
            raise LedgerError("obligation_months 不能为空")
        oid = order_id or _uuid()
        row = {
            "order_id": oid, "idempotency_key": idempotency_key,
            "created_at_et": _now_et().strftime("%Y-%m-%d %H:%M"),
            "signal_date": signal_date,
            "symbol": symbol, "status": status,
            "planned_amount_aud": planned_amount_aud,
            "planned_amount_usd": planned_amount_usd,
            "limit_price_usd": limit_price_usd,
            "fx_rate": fx_rate, "fx_source": fx_source,
            "obligation_months_json": json.dumps(months),
            "broker_order_id": None, "note": note or "",
        }
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO orders(order_id,idempotency_key,created_at_et,signal_date,"
                    "symbol,status,planned_amount_aud,planned_amount_usd,limit_price_usd,"
                    "fx_rate,fx_source,obligation_months_json,broker_order_id,note)"
                    " VALUES(:order_id,:idempotency_key,:created_at_et,:signal_date,"
                    ":symbol,:status,:planned_amount_aud,:planned_amount_usd,:limit_price_usd,"
                    ":fx_rate,:fx_source,:obligation_months_json,:broker_order_id,:note)",
                    row)
            except sqlite3.IntegrityError:
                ex = None
                if idempotency_key:
                    ex = conn.execute("SELECT * FROM orders WHERE idempotency_key=?",
                                      (idempotency_key,)).fetchone()
                if ex is None and order_id:
                    ex = conn.execute("SELECT * FROM orders WHERE order_id=?",
                                      (order_id,)).fetchone()
                if ex:
                    ed = dict(ex)
                    same = (ed["symbol"] == symbol
                            and abs(float(ed["planned_amount_aud"]) - planned_amount_aud) < 0.005
                            and ed["obligation_months_json"] == json.dumps(months))
                    if same:
                        ed["obligation_months"] = json.loads(ed.pop("obligation_months_json"))
                        return ed
                raise IdempotencyConflict(f"order_id/idempotency_key 冲突：{oid}")
        return self.get_order(oid)

    def cancel_order(self, order_id, note=""):
        with self._conn() as conn:
            row = self._order_row(conn, order_id)
            if not row:
                raise LedgerError(f"订单不存在：{order_id}")
            if row["status"] == "filled":
                raise LedgerError("已成全的订单不能取消")
            if row["status"] in ("cancelled", "expired"):
                return self.get_order(order_id)
            conn.execute("UPDATE orders SET status='cancelled', note=COALESCE(note,'')||?"
                         " WHERE order_id=?", (f" cancel:{note}" if note else "", order_id))
        return self.get_order(order_id)

    def _filled_for_order(self, conn, order_id):
        row = conn.execute("SELECT COALESCE(SUM(amount_aud),0) s FROM fills WHERE order_id=?",
                           (order_id,)).fetchone()
        return float(row["s"] or 0)

    # ------------------------------------------------------------------ fills
    def _alloc_plan(self, order, fill_aud):
        months = json.loads(order["obligation_months_json"])
        if not months:
            return {}
        if len(months) == 1:
            return {months[0]: float(fill_aud)}
        per = float(order["planned_amount_aud"]) / len(months)
        # FIFO：先填最早月份，每月最多 per；超出部分进最后一个月
        out, left = {}, float(fill_aud)
        for i, m in enumerate(months):
            take = min(per, left) if i < len(months) - 1 else left
            if take > 0:
                out[m] = out.get(m, 0.0) + take
                left -= take
            if left <= 0:
                break
        return out

    def add_fill(self, order_id, amount_aud=None, amount_usd=None, price_usd=None,
                 trade_date=None, fx_rate=None, commission_usd=0.0,
                 filled_at_et=None, fill_id=None, idempotency_key=None,
                 note="", allocations=None):
        if not trade_date:
            raise LedgerError("trade_date 必须是真实成交日")
        try:
            datetime.strptime(trade_date, "%Y-%m-%d")
        except ValueError:
            raise LedgerError(f"trade_date 格式错误：{trade_date}") from None
        price_usd = float(price_usd) if price_usd is not None else None
        if price_usd is None or price_usd <= 0:
            raise LedgerError("price_usd 必须 > 0")
        commission_usd = float(commission_usd or 0)
        fx_rate = float(fx_rate) if fx_rate is not None else None
        if amount_aud is None and amount_usd is None:
            raise LedgerError("amount_aud / amount_usd 至少提供一个")
        if amount_aud is None:
            if fx_rate is None:
                raise LedgerError("缺少 amount_aud 时必须提供 fx_rate")
            amount_aud = (float(amount_usd) + commission_usd) / fx_rate
        if amount_usd is None:
            if fx_rate is None:
                raise LedgerError("缺少 amount_usd 时必须提供 fx_rate")
            amount_usd = float(amount_aud) * fx_rate - commission_usd
        amount_aud, amount_usd = float(amount_aud), float(amount_usd)
        if amount_aud <= 0 or amount_usd <= 0:
            raise LedgerError("成交金额必须 > 0")
        if fx_rate is None:
            fx_rate = (amount_usd + commission_usd) / amount_aud if amount_aud else 0
        if fx_rate <= 0:
            raise LedgerError("fx_rate 无效")
        quantity = amount_usd / price_usd
        fid = fill_id or _uuid()

        with self._conn() as conn:
            order = self._order_row(conn, order_id)
            if not order:
                raise LedgerError(f"订单不存在：{order_id}")

            # 幂等：同 fill_id / idempotency_key 必须在状态检查之前，
            # 否则最后一笔成交后的重试会被误判为“订单已全部成交”。
            existing = None
            if fill_id:
                existing = conn.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
            if existing is None and idempotency_key:
                existing = conn.execute("SELECT * FROM fills WHERE idempotency_key=?",
                                        (idempotency_key,)).fetchone()
            if existing is not None:
                ex = dict(existing)
                same = (ex["order_id"] == order_id
                        and abs(float(ex["amount_aud"]) - amount_aud) < 0.005
                        and abs(float(ex["amount_usd"]) - amount_usd) < 0.005
                        and abs(float(ex["price_usd"]) - price_usd) < 1e-9
                        and ex["trade_date"] == trade_date)
                if same:
                    return self._fill_with_alloc(conn, ex["fill_id"])
                raise IdempotencyConflict(f"fill_id/idempotency_key 冲突：{fid}")

            if order["status"] in ("cancelled", "expired"):
                raise LedgerError(f"订单状态为 {order['status']}，不能成交")
            if order["status"] == "filled":
                raise OverfillError("订单已全部成交")

            filled = self._filled_for_order(conn, order_id)
            if filled + amount_aud > float(order["planned_amount_aud"]) + 0.5:
                raise OverfillError(
                    f"成交后超出订单计划额：{filled + amount_aud:.2f} > "
                    f"{float(order['planned_amount_aud']):.2f}")

            conn.execute(
                "INSERT INTO fills(fill_id,idempotency_key,order_id,filled_at_et,trade_date,"
                "symbol,quantity,price_usd,amount_usd,amount_aud,fx_rate,commission_usd,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, idempotency_key, order_id,
                 filled_at_et or _now_et().strftime("%Y-%m-%d %H:%M"),
                 trade_date, order["symbol"], quantity, price_usd,
                 amount_usd, amount_aud, fx_rate, commission_usd, note or ""))

            plan = allocations or self._alloc_plan(order, amount_aud)
            ratio = (amount_usd / amount_aud) if amount_aud else 0.0
            for m, aud in plan.items():
                conn.execute(
                    "INSERT OR IGNORE INTO obligation_allocations"
                    "(fill_id,obligation_month,amount_aud,amount_usd) VALUES(?,?,?,?)",
                    (fid, m, float(aud), float(aud) * ratio))

            new_filled = filled + amount_aud
            status = "filled" if new_filled >= float(order["planned_amount_aud"]) - 0.005 else "partial"
            conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        return self._fill_with_alloc(None, fid)

    def _fill_with_alloc(self, conn, fill_id):
        own = conn is None
        if own:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
        try:
            f = conn.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
            if not f:
                raise LedgerError(f"成交不存在：{fill_id}")
            d = dict(f)
            allocs = [dict(r) for r in conn.execute(
                "SELECT * FROM obligation_allocations WHERE fill_id=?", (fill_id,))]
            d["allocations"] = allocs
            return d
        finally:
            if own:
                conn.close()

    def get_fill(self, fill_id):
        if not fill_id:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
            if not row:
                return None
            return self._fill_with_alloc(conn, fill_id)

    def get_fill_by_idempotency(self, key):
        if not key:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM fills WHERE idempotency_key=?", (key,)).fetchone()
            if not row:
                return None
            return self._fill_with_alloc(conn, row["fill_id"])

    def fills_for_order(self, order_id):
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM fills WHERE order_id=? ORDER BY trade_date,fill_id", (order_id,))]
        return rows

    # ----------------------------------------------------------- aggregations
    def month_allocated_aud(self, month, symbol=None):
        with self._conn() as conn:
            sql = ("SELECT COALESCE(SUM(a.amount_aud),0) s FROM obligation_allocations a"
                   " JOIN fills f ON f.fill_id=a.fill_id WHERE a.obligation_month=?")
            args = [month]
            if symbol:
                sql += " AND f.symbol=?"
                args.append(symbol.upper())
            return float(conn.execute(sql, args).fetchone()["s"] or 0)

    def month_allocated_by_symbol(self, month):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT f.symbol, COALESCE(SUM(a.amount_aud),0) s"
                " FROM obligation_allocations a JOIN fills f ON f.fill_id=a.fill_id"
                " WHERE a.obligation_month=? GROUP BY f.symbol", (month,))
            return {r["symbol"]: float(r["s"]) for r in rows}

    def pending_by_month(self, month, symbol=None):
        """未成交订单中，按 allocation 口径归到该月的 pending 金额。

        对合并单：每月应有 per = planned/len(months)，pending = max(0, per - 已分配)。
        这样部分成交时不会把已分配月份的 pending 重复算到后面的月份。
        """
        with self._conn() as conn:
            sql = "SELECT * FROM orders WHERE status IN ('placed','partial')"
            args = []
            if symbol:
                sql += " AND symbol=?"
                args.append(symbol.upper())
            orders = [dict(r) for r in conn.execute(sql, args)]
            total = 0.0
            for o in orders:
                months = json.loads(o["obligation_months_json"] or "[]")
                if month not in months or not months:
                    continue
                planned = float(o["planned_amount_aud"])
                per = planned / len(months)
                if month == months[-1]:
                    per = planned - (planned / len(months)) * (len(months) - 1)
                allocated = conn.execute(
                    "SELECT COALESCE(SUM(a.amount_aud),0) s FROM obligation_allocations a"
                    " JOIN fills f ON f.fill_id=a.fill_id"
                    " WHERE f.order_id=? AND a.obligation_month=?",
                    (o["order_id"], month)).fetchone()["s"]
                total += max(0.0, per - float(allocated or 0))
            return total

    def month_fills(self, month):
        """按真实 trade_date 统计的成交（现金口径）。"""
        start, end = _month_bounds(month)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM fills WHERE trade_date>=? AND trade_date<?"
                " ORDER BY trade_date,fill_id", (start.isoformat(), end.isoformat()))]
        return rows

    def month_fills_by_symbol(self, month):
        out = {}
        for f in self.month_fills(month):
            out[f["symbol"]] = out.get(f["symbol"], 0.0) + float(f["amount_aud"])
        return out

    def all_fills(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM fills ORDER BY trade_date,fill_id")]

    def all_orders(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM orders ORDER BY created_at_et,order_id")]

    def recent_fills(self, limit=10):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM fills ORDER BY filled_at_et DESC,fill_id DESC LIMIT ?", (limit,))]

    def contribution_by_symbol(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT symbol, COALESCE(SUM(amount_aud),0) s FROM fills GROUP BY symbol")
            return {r["symbol"]: float(r["s"]) for r in rows}

    # -------------------------------------------------------------- write offs
    def add_write_off(self, month, symbol, amount_aud, reason):
        if not reason or not str(reason).strip():
            raise LedgerError("write-off 必须填写原因")
        amount_aud = float(amount_aud)
        if amount_aud <= 0:
            raise LedgerError("write-off 金额必须 > 0")
        wid = _uuid()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO write_offs(write_off_id,obligation_month,symbol,amount_aud,"
                "reason,created_at_et) VALUES(?,?,?,?,?,?)",
                (wid, month, str(symbol).upper(), amount_aud, str(reason),
                 _now_et().strftime("%Y-%m-%d %H:%M")))
        return wid

    def write_offs_for(self, month, symbol=None):
        with self._conn() as conn:
            sql = "SELECT * FROM write_offs WHERE obligation_month=?"
            args = [month]
            if symbol:
                sql += " AND symbol=?"
                args.append(str(symbol).upper())
            return [dict(r) for r in conn.execute(sql, args)]

    # ------------------------------------------------------------ batch state
    def get_batch(self, symbol):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM batch_state WHERE symbol=?",
                               (str(symbol).upper(),)).fetchone()
        return dict(row) if row else None

    def set_batch(self, symbol, start_month, months_accrued, target_months,
                  due_month, status):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO batch_state(symbol,start_month,months_accrued,"
                "target_months,due_month,status,updated_at_et) VALUES(?,?,?,?,?,?,?)",
                (str(symbol).upper(), start_month, int(months_accrued), int(target_months),
                 due_month, status, _now_et().strftime("%Y-%m-%d %H:%M")))

    # --------------------------------------------------------------- migration
    def import_legacy_csv(self, path, legacy_fx_rate=1.0):
        if not os.path.exists(path):
            return 0
        n = 0
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("symbol") or not r.get("amount"):
                    continue
                sym = r["symbol"].upper()
                month = r.get("month") or (r.get("et_date") or "")[:7]
                if not month:
                    continue
                amount_usd = float(r["amount"])
                price = float(r.get("price") or 0) or 1.0
                fx_rate = float(legacy_fx_rate or 1.0)
                amount_aud = amount_usd / fx_rate
                oid = f"legacy-{month}-{sym}-{n}"
                try:
                    self.create_order(sym, amount_aud, planned_amount_usd=amount_usd,
                                      status="placed", signal_date=r.get("et_date"),
                                      obligation_months=[month], fx_rate=fx_rate,
                                      fx_source="legacy", order_id=oid,
                                      note="legacy import from dca_log.csv")
                    self.add_fill(oid, amount_aud=amount_aud, amount_usd=amount_usd,
                                  price_usd=price, trade_date=r.get("et_date") or f"{month}-01",
                                  fx_rate=fx_rate, commission_usd=0.0,
                                  note="legacy", fill_id=f"legacy-fill-{n}")
                    n += 1
                except Exception:  # noqa: BLE001
                    # 已存在/坏行：跳过，保持导入幂等
                    continue
        return n

    def export_csv(self, path, fields=None):
        fields = fields or ["trade_date", "month", "symbol", "status", "quantity",
                            "price_usd", "amount_usd", "amount_aud", "fx_rate",
                            "commission_usd", "fill_id", "order_id", "note"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT f.*, o.status AS status, a.obligation_month AS month FROM fills f"
                " JOIN orders o ON o.order_id=f.order_id"
                " LEFT JOIN obligation_allocations a ON a.fill_id=f.fill_id"
                " ORDER BY f.trade_date,f.fill_id")]
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return len(rows)

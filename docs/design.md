# 美股定投雷达 · 设计文档 v2

> 状态：已批准（2026-11-13）
> 约束：不接券商 API；不自动下单；先数据可靠性 → 月度决策 → 回测验证。
> 配套：`docs/test_matrix.md`、`README.md`。

## 0. 目标与非目标

### 目标
1. 修正时区、交易日历、币种（AUD 预算 / USD 下单）和成交状态。
2. 把「每月必投」变成硬约束：评分只决定窗口内哪一天，不能降额、拖过窗口或跳过整月。
3. 用 T+1 无前视回测回答：「评分择时是否真的比固定日期买入更好？」

### 非目标
- 不接券商 API，不自动下单。
- 不建模税、FX 兑换费、盘中滑点（只提供可配置的 bps 敏感性）。
- 不做多币种（只支持 AUD 预算 / USD 报价）。

## 1. 总体架构

```
config.json ─> 配置校验 ─> market_clock / calendar
                                 │
行情缓存(Yahoo/Nasdaq) ─> 评分 ──┤
FX(AUDUSD) ────────────> Plan ──┤
                                 ▼
data/dca.sqlite (orders/fills/allocations/budget_versions/batch_state/write_offs)
                                 │
                                 ▼
                   obligation engine ─> decide() ─> 报告 / JSON / 面板
                                 │
                                 └─> backtest.py（同一 decide/obligation，内存账本）
```

模块：
- `scripts/nyse_calendar.py`：`exchange_calendars`（XNYS）适配器 + 内置 2017–2028 表回退 + `CalendarRangeError`。
- `scripts/fx.py`：`AUDUSD=X` 获取、缓存、过期判定、`firm` 标志。
- `scripts/ledger.py`：SQLite 数据层、order/fill/allocation、幂等、迁移/导出。
- `scripts/scan.py`：行情/评分/义务/决策/报告/日报/JSON。
- `scripts/backtest.py`：T+1 回测、组合估值、XIRR、最大回撤、净超额。
- `scripts/dashboard.py` + `dashboard_template.html`：只消费 `scan` 的 payload。

## 2. Stage 1：数据可靠性

### 2.1 市场时钟

`market_clock(cfg, et_now, session_date=None)` 返回：

- `et_now / et_date / local_now`
- `trading_today`、`session_phase ∈ PRE|REGULAR|POST|CLOSED|HOLIDAY|EARLY_CLOSE`
- `can_fill_now`（仅 REGULAR 且 `et_open <= et_now < et_close`）
- `can_place_order`（PRE/REGULAR；POST/CLOSED/HOLIDAY 不可）
- `close_et / next_fill_et / next_fill_local`
- `total_days / day_idx / days_left_after_today / days_left_incl_today / last_trading_day`
- `calendar_source ∈ exchange_calendars|builtin|unsupported`、`calendar_warning`

规则：
- 只有 `can_fill_now=true` 才允许「今日执行 / 今日定投」。
- 盘前：「开盘后」；盘后/提前收盘/休市：「下一交易时段（美东 X 09:30 = 墨尔本 Y）」。
- 行情源 `marketState` 只做辅助校验；冲突时 `data_note`，判断以时钟为准。
- `build_digest`：今天休市 → 「昨夜休市」+ 下一交易日墨尔本时间；交易日且 `et_now >= et_close(today)` → 今天；否则上一交易日。
- `collect(cfg, et_now, session, session_date=None)`：月份/日序/台账月按 `session_date`。
- Nasdaq 备用源用 `datetime.now(ET_TZ).date()`，实时图取最近 7 天窗口。
- Yahoo 日线 bar 日期按 `meta.exchangeTimezoneName`（缺省 ET）。

### 2.2 交易日历

主路径 `exchange_calendars.get_calendar("XNYS")`：
- `is_session`、`sessions_in_range`、`session_open`、`session_close`。
- 提前收盘直接取 `session_close`（13:00 ET）。

回退：库不可用或库范围不覆盖时，用内置表：
- 休市日：2017–2028（含 2025-01-09 卡特国葬）。
- 提前收盘日：2022-11-25；2023-07-03、11-24；2024-07-03、11-29、12-24；2025-07-03、11-28、12-24；2026-11-27、12-24；2027-11-26；2028-07-03、11-24。

超范围：
- 日期不在库范围且不在内置表范围 → `CalendarRangeError`。
- 实时报告不给交易建议，只展示 AUD 义务/持仓，提示升级日历库或扩展表。
- **禁止**按「周一至周五」静默退化。

报告/JSON 带 `calendar_source`、`calendar_warning`；`--strict-calendar` 时超范围直接报错。

### 2.3 币种与 FX

```json
"portfolio": { "total_monthly_aud": 2500, "weights": {"VOO":0.75,"SMH":0.20,"TSLA":0.05}, "start_month": "2026-11" },
"currency": { "budget": "AUD", "quote": "USD", "display": "both" },
"fx": { "symbol": "AUDUSD=X", "fallback_rate": 0.65, "ttl_hours": 6, "max_stale_hours": 72 }
```

`fetch_fx()` 返回 `{rate, as_of_et, source, age_hours, status, firm}`：
- `firm=true` 仅当实时/缓存且 `age <= ttl_hours`。
- `age > ttl_hours` 或 fallback → `status=stale/fallback`、`firm=false`。

过期/缺失：
- 报告/面板展示「估算 US$…，不可作为确定下单金额」。
- `decide` 返回 `BUY_FX_ESTIMATE` 或 `REVIEW_FX`。
- 面板确认按钮禁用；CLI `--confirm` 必须带 `--fx-rate` 或 `--amount-usd`。
- 绝不静默 1:1。

`Plan = {aud, usd, fx_rate, fx_source, fx_as_of, firm}`：
- `gross_usd = aud * rate`；`net_invest_usd = gross_usd - commission_usd`；股数 = `net_invest_usd / price`。
- `amount_aud` 为预算实际消耗（含佣金）。

数据源 `meta.currency` 写入缓存；与 `currency.quote` 不一致直接报错。FX 符号不参与股票币种校验。

金额统一 `A$` / `US$` 双标。A$2,500 ≈ US$1,625（0.65）；如想投 US$2,500，把 `total_monthly_aud` 改为约 3846。

### 2.4 台账（SQLite）

主台账 `data/dca.sqlite`（WAL、外键、busy_timeout=5000）：

```sql
orders(order_id PK, idempotency_key UNIQUE, created_at_et, signal_date,
       symbol, status CHECK IN ('placed','partial','filled','cancelled','expired'),
       planned_amount_aud, planned_amount_usd, limit_price_usd, fx_rate, fx_source,
       obligation_months_json, broker_order_id, note)

fills(fill_id PK, idempotency_key UNIQUE, order_id REFERENCES orders,
      filled_at_et, trade_date, symbol, quantity, price_usd,
      amount_usd, amount_aud, fx_rate, commission_usd, note)

obligation_allocations(fill_id REFERENCES fills, obligation_month,
                       amount_aud, amount_usd, PRIMARY KEY(fill_id, obligation_month))

budget_versions(effective_month PK, total_monthly_aud, weights_json, created_at_et, note)

batch_state(symbol PK, start_month, months_accrued, target_months, due_month, status, updated_at_et)

write_offs(write_off_id PK, obligation_month, symbol, amount_aud, reason, created_at_et)
```

语义：
- **order** 是意图；**fill** 是真实成交（`trade_date` 必须真实，不得回填假日期）；**allocation** 把 fill 分摊到覆盖的义务月份。
- 合并单：`planned_amount_aud = n × base`，`obligation_months_json` 列出覆盖月份；成交只写一条真实 fill + 多条 allocation。**绝不生成假历史 fill。**
- `pending_aud = planned_amount_aud - sum(fills.amount_aud)`（仅 placed/partial）；pending 不计 filled。
- 报表双视图：
  - 实际成交：按 `fills.trade_date` 汇总。
  - 义务覆盖：按 `allocation.obligation_month` 汇总。
  - 对账差：本月实际成交含补投历史 A$X / 本月义务覆盖 A$Y。

幂等：
- `order_id`/`fill_id` 由调用方提供或生成 UUID；`idempotency_key` 唯一。
- 同 fill_id 同 payload → no-op；不同 payload → 冲突错误，不写。
- 面板每次提交生成客户端幂等键，重试复用。
- CLI 无 `--fill-id` 时生成并打印；5 分钟内相同 `(order_id, trade_date, price_usd, amount_usd)` 警告并要求显式键/`--force`。
- 部分成交累计不得超过订单计划额；超买拒绝。
- 取消：placed/partial → cancelled；已成交保留；未成交回义务（欠额），不再 pending。
- 写操作 `BEGIN IMMEDIATE`；唯一约束兜底；CLI/面板并发不丢单。

迁移：
- 旧 `data/dca_log.csv` 一次性导入：一行 → order + fill + allocation（status=filled，legacy）。
- 之后 CSV 只作 `--export-csv` 只读导出。

### 2.5 成交状态

- 只有 filled/partial 的 `filled_aud` 计入已成交。
- placed 只算 pending；到截止日提示「确认成交或改单」。
- 报告/面板区分：今日可成交 / 可挂单 / 已挂单 / 已成交 / 部分成交 / 下一时段。
- `--confirm` 参数：`--status {placed,filled,partial,cancelled}`、`--trade-date`、`--price`、`--amount-usd`、`--fx-rate`、`--filled-aud`、`--fill-id`、`--order-id`。
- 默认 status=filled，保持向后兼容。

## 3. Stage 2：月度决策逻辑

### 3.1 预算版本与义务

- `budget_versions` 取 `effective_month <= M` 的最新版本；配置变更生效月=当前月，历史不追溯。
- `base_aud = B_M × weight`。
- `required_aud = base_aud + arrears_aud`。
- `filled_aud` = 该月 allocation 已成交金额；`pending_aud` = 关联未成交订单金额。
- `remaining_aud = max(0, required - filled - pending)`。
- 容差：`max(0.5, required × 0.005)`；不使用 2% 漏洞。

### 3.2 欠额永久保留

- `arrears_aud(s,M)` = 从 `start_month` 到 `M-1` 所有未覆盖义务之和，无上限、无自动豁免。
- 报告按账龄：当月 / 逾期 1 月 / 2 月 / 3+ 月，并给最早欠额月份。
- `max_catchup_per_order_months`（默认 3）、`max_catchup_aud`（默认 0=不限）只限制单笔补投建议；超出继续挂账。
- 只有显式 `--write-off --month --symbol --amount --reason` 才减少欠额，写入 `write_offs` 并单列；默认无自动核销。

### 3.3 decide() 优先级

1. PAID → DONE
2. PENDING → PENDING（不重复投；截止日提示确认/改单）
3. remaining>0 且不可成交 → NEXT_SESSION（下一时段、金额、截止日）
4. arrears>0 → BUY_NOW（补投欠额，无视评分；受 max_catchup 限制）
5. day_idx > window_end → BUY_NOW（窗口已过）
6. 窗口内：score >= buy → BUY；否则 day_idx < window_end → WAIT；否则 BUY_NOW（末日必投）
7. allow_split 默认 false；如开启仅窗口内分批，最后一笔强制完成

评分不能降额、拖过窗口、跳过整月。v2 不存在 DEFER；`commission.policy=batch` 会被配置校验拒绝；`max_pct` 只做佣金警示，合并用 `--months` 手工执行。

### 3.4 佣金合并（可选，默认关闭）

- `policy="pay"`：TSLA 每月照投；$1 佣金约 1.2%，提示但不跳过。
- `policy="batch"`：v2 暂不启用自动批次；需要合并时用 `--confirm SYM:AMOUNT --months 2026-09,2026-10,...` 手工执行，只写一条真实 fill + 多条 allocation。配置校验会拒绝 `policy=batch` 并提示改用 `pay`。
- 取消/部分成交后未覆盖的义务继续留欠额。

### 3.5 报告/面板

- 首屏「本月硬性义务」表：标的 / 基础 A$ / 欠额 A$ / 已成交 A$ / 挂单 A$ / 缺口 A$ / 折合 US$ / 状态 / 最迟日期；组合合计。
- 「一句话」包含缺口、下一动作、下一可成交窗口（美东+墨尔本）。
- Hero 显示义务状态；手工合并单独提示；FX 过期显示估算/待确认，禁用确认。
- `--json` 增加 `clock/calendar_source/currency/fx/obligation/obligation_summary/fills_recent`。
- `signals.csv` 增加 `can_fill_now/phase/fx_rate/plan_aud/plan_usd/obligation_status`；首次写入迁移旧表头。

## 4. Stage 3：回测

### 4.1 T+1 无前视

- 信号：T 日收盘后，用 `closes[:T+1]` 计算评分；不得使用 T 后数据。
- 成交：T+1 首个可交易时段开盘价（`open`）；非交易日顺延；开盘缺失/为 0 用当日收盘并标 `fill_price_fallback`。
- FX：T+1 的 FX 收盘；缺失前向填充；信号日不用 T+1 FX。
- 末日信号：T 为当月最后交易日，成交在下月首个交易日；`obligation_month` 仍为信号月，`trade_date` 为真实下月日期。
- 回测内存账本接口与 SQLite 账本一致；同一套 decide/obligation。
- `backtest.slippage_bps`（默认 0）、`funding="first_trading_day"`。
- 实盘 15:50 可当日成交；回测 T+1 更保守，报告写明。

### 4.2 组合模拟与主指标

每个规则独立 AUD 现金账户：
- 每月首个交易日入金 A$B；买入按当日 FX 换 USD；净买入 = gross - commission；剩余 AUD 现金（默认 0 收益，可配 cash_yield_annual）。
- 每日估值 `V_t = 持股 × 收盘价_usd × FX_t + 现金_aud`。

主指标：
1. `ending_portfolio_value_aud`
2. `ending_cash_aud`
3. `ending_arrears_aud`
4. `xirr`（月度入金为负、期末价值为正；二分法；无解 n/a）
5. `max_drawdown_twr`（TWR 指数，剔除入金现金流影响）
6. `net_excess_vs_fixed1_aud/_pct/xirr_excess_pp`（扣佣金/汇率）
7. `total_contributions_aud`

辅助指标（附录）：有效每股成本、`vs_first`、`vs_avg`、佣金拖累、逐标的明细。

### 4.3 规则、显著性与敏感性

- 规则：hard_score、fixed_1/2/3/5/10/last、month_avg、weekly、score_only、random_window（1000 次）、oracle、legacy_current。
- 显著性：组合终值单路径不做伪显著性；月度有效成本差 bootstrap 95% CI 辅助；random_window 经验分布。
- 样本外：2017-12~2021-12、2022-01~2026-11；5y/10y。
- 预算敏感性：A$2,000/2,500/3,000 各跑，输出期末价值、现金、XIRR、最大回撤、净超额、TSLA 佣金%；代码无 2500 常量。
- 输出：`reports/backtest_YYYY-MM-DD.md`、`data/backtest_monthly.csv`、`data/backtest_summary.json`。
- 判定：若净超额 95% CI 不显著优于 fixed_1，写「评分无可证实择时优势」，建议 early_days=1、评分保留展示；显著优于则保留 3 日窗口；不显著但点估计为负则保留但标「未证实」。

## 5. 配置 schema

```json
"decision": {
  "monthly_obligation": true,
  "score_role": "timing",
  "miss_policy": "carry_over",
  "max_catchup_per_order_months": 3,
  "max_catchup_aud": 0,
  "allow_split": false
},
"commission": {
  "per_order_usd": 1.0,
  "per_share_usd": 0.0,
  "max_pct": 0.35,
  "max_batch_months": 6,
  "policy": "pay",
  "fee_mode": "per_order"
},
"backtest": {
  "signal": "close",
  "fill": "next_open",
  "slippage_bps": 0,
  "funding": "first_trading_day",
  "cash_yield_annual": 0
}
```

删除 `total_monthly_usd` 与 `max_carry_months` 旧语义；旧配置加载给出迁移提示。

校验：预算>0、权重和=1、阈值 0–100、`partial<=buy`、FX fallback 0.3–1.2、start_month 格式。

## 6. 异常处理

| 场景 | 行为 |
|---|---|
| FX 新鲜 | firm=true，正常下单金额 |
| FX 过期 | firm=false，估算；确认必须实际汇率/美元金额 |
| FX 回退/缺失 | firm=false，REVIEW_FX；AUD 义务仍展示；不得 1:1 |
| 报价币种≠USD | 硬错误 |
| 日历库缺失 | 内置表 + calendar_source=builtin，报告提示 |
| 日期超出范围 | CalendarRangeError，不给交易建议 |
| 行情缺失/不足 | 该标的不决策，报告 errors，不伪造 |
| 部分成交/取消 | 已成交保留，未成交回义务；pending 不计 filled |
| 重复/重试 | 幂等键 no-op；冲突报错 |
| 超买 | 拒绝，事务回滚 |
| 旧 CSV | 一次性导入，标 legacy；只读导出 |
| 并发写 | WAL + BEGIN IMMEDIATE + 唯一约束 |
| 2029+ | 日历 unsupported，不静默 |

## 7. 迁移与兼容

- 旧 `dca_log.csv`：备份后导入；生成 order/fill/allocation；status=filled。
- 旧 `signals.csv`：首次写入迁移表头；旧行新字段留空。
- 旧报告保留；`latest*.md` 按新格式重生成。
- 实施前做 md5/mtime 基线快照，备份到 `/tmp`；若发现并发写入则暂停确认。

## 8. 验收标准

1. can_fill_now=false 时不出现「今日执行/今日定投」；下一时段双时区。
2. 默认 pay 下每标的每月真实成交；无 DEFER；漏投欠额永久保留、可审计核销。
3. 台账只记真实 trade_date；batch 只写真实 fill + allocation；placed 不计 filled；重复/重试/部分/取消不重复计账。
4. 金额 A$/US$ 双标；FX 过期只展示估算，确认必须实际汇率/美元金额。
5. 回测 T+1；主指标为期末价值、剩余现金、XIRR、最大回撤、净超额；有效成本仅附录。
6. A$2,000/2,500/3,000 敏感性完整；无 2500 硬编码；预算版本不追溯。
7. 日历优先成熟库；范围外明确报错。
8. 现有核验 + 新增矩阵全部通过；README/docs 无漂移。

## 9. 假设

- 默认预算 A$2,500/月；2,000/3,000 仅敏感性。
- 佣金 $1/笔 USD，per_order；不建模 FX 兑换费/税/滑点；实际值可覆盖。
- T+1 用次日开盘；开盘缺失用当日收盘并标注；实盘 15:50 可当日成交。
- 现金默认 0 收益；可配。
- 主台账 SQLite；旧 CSV 只读导出。
- `exchange_calendars` 优先；环境不可用则内置表回退并标 builtin，库测试 SKIP。
- 不接券商 API。

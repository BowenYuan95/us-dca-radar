# 美股定投雷达（US DCA Radar）v2

每天在美股**开盘 / 午盘 / 收盘前**扫描 **VOO、SMH、TSLA**，回答一个问题：

> **本月硬性义务还差多少？今天/下一交易时段该投什么？**

评分只做**辅助择时**：它只能决定「月初窗口内哪一天投」，不能降额、不能拖过窗口、不能跳过整月。

> 设计文档：[docs/design.md](docs/design.md) · 测试矩阵：[docs/test_matrix.md](docs/test_matrix.md)
> 不接券商 API、不自动下单；最终下单与记账由你确认。

---

## 一、每天会发生什么

| 时间（美东） | 时间（墨尔本） | 动作 |
|---|---|---|
| 09:35 开盘后 | 01:35（次日） | 扫描并写入 `reports/YYYY-MM-DD_open.md` |
| 12:30 午盘 | 04:30 | 扫描并写入 `reports/YYYY-MM-DD_midday.md` |
| 15:50 收盘前 | 07:50 | 扫描并写入 `reports/YYYY-MM-DD_close.md` |
| — | **08:15（周二~周六）** | 收盘日报 `reports/YYYY-MM-DD_digest.md`：三时点快照 + 硬性义务 + 下一次下单窗口 |
| — | **08:20（周二~周六）** | 刷新面板 `dashboard/index.html` |

三个时点扫描只落盘；你早上看到的是 08:15 的汇总日报。美股周一~周五对应墨尔本周二~周六凌晨。

**成交状态**：只有美东常规交易时段（含提前收盘日 13:00 前）才显示「今日可成交」；盘前显示「开盘后」；盘后/提前收盘后/休市显示「下一交易时段（美东 + 墨尔本）」。行情源的 `marketState` 只做辅助校验，动作判断以市场时钟为准。

---

## 二、组合规则（硬约束）

| 规则 | 实现 |
|---|---|
| 目标权重 **VOO 75% / SMH 20% / TSLA 5%** | `config.json → portfolio.weights` |
| 每月预算 **A$2,500** | `portfolio.total_monthly_aud`；拆成 VOO A$1,875 / SMH A$500 / TSLA A$125 |
| **每月必投** | 每个标的每月都有 `base + 历史欠额` 的硬性义务；默认 `commission.policy=pay`，不因佣金/波动跳过 |
| VOO 不择时 | 第 1 个交易日收盘信号，T+1 开盘执行；`early_days=1` |
| SMH / TSLA 评分辅助 | 默认 `early_days=1`（T+1 回测未跑赢固定第 1 日，评分只展示）；想保留 3 日辅助窗口把 `early_days` 改回 3 |
| 欠额永久保留 | 未完成金额一直挂账，按账龄展示；只有显式 `--write-off` 才核销 |
| 小额佣金合并（手工） | 需要时用 `--months` 一次真实成交覆盖多月，只写真实 `trade_date`，不生成假历史成交；自动 batch 暂未启用 |

预算按 **AUD** 记，按当日 AUD/USD 折算成 USD 下单。A$2,500 按 0.65 约 US$1,625/月；如你实际想投 **US$2,500**，把 `total_monthly_aud` 改成约 `3846`。

> 上表的 T+1 是**回测口径**；实盘 15:50 扫描在收盘前，可当日成交。报告会按市场时钟明确写「今日可成交 / 下一时段」，不会把收盘后的建议说成今日成交。

---

## 三、快速开始

```bash
cd "~/us-dca-radar"

python3 scripts/scan.py                    # 立即扫描（自动判断会话）
python3 scripts/scan.py --digest           # 收盘日报
python3 scripts/scan.py --json             # 机器可读（含 clock/obligation/fx）
python3 scripts/scan.py --backtest         # T+1 回测（期末价值/XIRR/回撤/净超额）
python3 scripts/scan.py --confirm VOO:1875,SMH:500 --price 696 --fx-rate 0.68
python3 scripts/scan.py --confirm VOO:1875 --status placed --order-id my-order-1
python3 scripts/scan.py --confirm TSLA:375 --months 2026-09,2026-10,2026-11 \
    --status filled --trade-date 2026-11-20 --price 300 --fx-rate 0.65
python3 scripts/scan.py --export-csv       # 导出台账 CSV（只读）
python3 scripts/dashboard.py --serve --open # 面板 + 执行窗口记账
```

首次运行需联网抓行情与 FX（约 3~5 秒，有缓存更快）。FX 过期时系统只显示**估算**，确认成交必须带 `--fx-rate` 或 `--amount-usd`。

---

## 四、评分与决策

### 吸引力评分（0–100，越高越便宜/越适合买）

| 分项 | 权重 | 含义 |
|---|---|---|
| 52 周位置 | 20% | 现价在近一年最低~最高中的位置，越低越好 |
| 60 日分位 | 20% | 现价低于近 60 个交易日收盘价的比例 |
| MA20 乖离（z20） | 20% | 相对 20 日均线的标准化偏离 |
| RSI14 | 15% | 超卖得分高 |
| 距 52 周高点回撤 | 15% | 回撤越深得分越高 |
| VIX | 10% | VIX 15→满分，30→0 |

### 决策优先级

1. 义务已完成 → `DONE`
2. 有挂单 → `PENDING`（不重复投；未成交不算完成）
3. 当前不可成交 → `NEXT_SESSION` / `REVIEW_FX`（下一时段 / 待确认汇率）
4. 有历史欠额 → `BUY_NOW`（优先补投，无视评分）
5. 窗口已过 → `BUY_NOW`（强制补投）
6. 窗口内：评分达标 → `BUY`；未达标 → `WAIT`；窗口末日 → `BUY_NOW`

评分永远不能改变金额、不能拖过窗口、不能跳过整月。

### 回测结论（T+1，10 年 108 个月）

信号在 T 日收盘生成，**T+1 开盘成交**（不是同日收盘成交）。当前结果（A$2,500/月）：

| 规则 | 期末价值 | XIRR | 最大回撤 | 净超额 vs fixed_1 |
|---|---|---|---|---|
| hard_score | A$756,783 | +22.16% | -33.64% | A$-57 |
| fixed_1（第 1 日开盘） | A$756,840 | +22.16% | -33.70% | 0 |
| fixed_3 | A$756,954 | +22.16% | -33.68% | +A$114 |
| month_avg | A$708,975 | +20.77% | -31.61% | -A$47,865 |
| score_only（旧式等月末） | A$751,779 | +22.01% | -33.64% | -A$5,061 |
| oracle（月内最低，不可交易） | A$777,143 | +22.72% | -33.84% | +A$20,303 |

**结论：评分择时没有跑赢固定第 1 日买入**（差 -0.01%；月度有效成本差 +0.04%，bootstrap 95% CI [-0.22%, +0.32%]，跨 0）。当前配置 `early_days=1`，T+1 使 hard_score 最早第 2 日开盘成交，等价于 fixed_2；fixed_1 是第 1 日开盘，天然早一天。评分保留为展示信号；完整报告见 `reports/backtest_YYYY-MM-DD.md`。

---

## 五、记账（SQLite）

主台账为 `data/dca.sqlite`，三层模型：

- `orders`：订单意图（`order_id`、状态、计划 A$、覆盖月份）
- `fills`：真实成交（`fill_id`、真实 `trade_date`、USD 成交价、A$ 预算、汇率、佣金）
- `obligation_allocations`：把成交分摊到它覆盖的义务月份

规则：

- `placed` 只算 pending，**不算已成交**；`filled/partial` 才算；`cancelled` 忽略未成交部分。
- `order_id` / `fill_id` 唯一；重复提交同一 ID 幂等 no-op，冲突报错；面板用幂等键防双击。
- 合并多月只写**一条真实 fill** + 多条 allocation，不生成虚假历史成交。
- 欠额不会因超过月数自动清除；只有 `--write-off --month --symbol --write-off-amount --reason` 才核销并留痕。
- 旧 `data/dca_log.csv` 首次运行自动导入；之后 CSV 是 `--export-csv` 的只读导出。

常用参数：

```bash
--status placed|filled|partial|cancelled
--trade-date YYYY-MM-DD        # 回填上一交易日
--price / --amount-usd / --fx-rate   # 实际成交价/美元金额/汇率
--fill-id / --order-id         # 幂等重试
--filled-aud                   # partial 的实际成交 AUD 金额
--months 2026-09,2026-10,...   # 合并覆盖月份
```

---

## 六、目录结构

```
美股/
├── config.json                 # 预算、FX、决策、佣金（主要改这里）
├── docs/
│   ├── design.md               # v2 设计文档
│   └── test_matrix.md          # 测试矩阵
├── scripts/
│   ├── scan.py                 # 扫描 + 义务 + 决策 + 报告 + 日报
│   ├── nyse_calendar.py        # exchange_calendars / 内置日历回退
│   ├── fx.py                   # AUD/USD 获取、缓存、firm/过期
│   ├── ledger.py               # SQLite orders/fills/allocations/幂等
│   ├── backtest.py             # T+1 回测、组合估值、XIRR、回撤
│   ├── dashboard.py            # 面板生成/记账服务
│   └── dashboard_template.html
├── dashboard/index.html        # 单文件静态面板
├── reports/                    # Markdown 报告 + latest*.md
├── data/
│   ├── dca.sqlite              # 主台账
│   ├── dca_log.csv             # 只读导出
│   ├── signals.csv             # 三时点评分留痕
│   ├── backtest_*.csv/json     # 回测产物
│   └── cache/                  # 行情/FX 缓存（可删，自动重建）
├── verify/                     # 核验与测试
└── README.md
```

> 本仓库刻意不提交 `data/`、`reports/`、`dashboard/index.html`（个人台账、缓存和生成物，已在 `.gitignore` 中忽略）；首次运行会自动创建。

---

## 七、可视化面板

```bash
python3 scripts/dashboard.py --serve --open   # 面板 + 执行窗口（推荐）
python3 scripts/dashboard.py --open           # 只生成并打开静态面板
python3 scripts/dashboard.py --quiet          # 定时任务用
python3 scripts/dashboard.py --serve --port 8766
```

面板包含：本月硬性义务（基础/欠额/已成交/挂单/缺口/状态）、本时段动作、三张标的卡（US$ 价格 / A$ 计划 / 评分）、本月执行进度与交易日窗口、最近扫描、评分轨迹、价格走势、报告归档、执行窗口（AUD 金额、汇率、成交状态、幂等键）。

页面是单文件静态 HTML（内嵌数据 + 原生 SVG，无 CDN/JS 依赖）；`--serve` 仅监听 `127.0.0.1:8765`，写接口需要页面令牌且只接受本机来源。

---

## 八、调参

| 配置 | 作用 |
|---|---|
| `portfolio.total_monthly_aud` | 每月 AUD 预算（不要硬编码到代码） |
| `portfolio.weights` | 目标权重（和必须为 1） |
| `portfolio.start_month` | 义务起算月；之前不产生欠额 |
| `currency` / `fx` | AUD 预算、USD 报价、汇率 fallback / 过期阈值 |
| `decision.max_catchup_per_order_months` | 单笔补投最多覆盖几个月；**不是豁免上限** |
| `decision.max_catchup_aud` | 单笔补投金额上限（0=不限） |
| `decision.allow_split` | 是否允许窗口内分批（默认 false） |
| `styles.*.buy / early_days` | 评分辅助线与窗口天数（默认全部 1；可配 3） |
| `commission.policy` | 仅 `pay`（默认硬投）；合并用 `--months` 手工执行，系统只记真实成交 |
| `commission.per_order_usd / max_pct` | 佣金模型与警示阈值 |
| `backtest.signal/fill` | 默认 `close` / `next_open`（T+1，不支持同日收盘成交） |
| `backtest.slippage_bps` | 滑点敏感性（默认 0） |
| `backtest.sensitivity_aud` | 预算敏感性档位（默认 2000/2500/3000） |

改完跑 `python3 scripts/scan.py --backtest` 复核。

---

## 九、数据源与已知限制

- **行情**：Yahoo Finance（系统 `curl`）→ Nasdaq API → 本地缓存；报告标注来源/延时。
- **FX**：Yahoo `AUDUSD=X` → 缓存 → `fallback_rate`；**过期/回退只显示估算，不当作确定下单金额**，确认成交需实际汇率/美元金额。
- **交易日历**：优先 `exchange_calendars`（XNYS）；未安装/不覆盖时回退内置 2017–2028 表并标注 `calendar_source=builtin`。超出 2017–2028 且库不支持 → `CalendarRangeError`，**不再按周一至周五静默退化**。2029+ 请安装/升级日历库或扩展内置表。
- **时区**：市场时钟以美东为准，显示同时给墨尔本；夏令时自动平移。
- **回测**：T 日收盘信号、T+1 开盘成交；同 bar 不开后门。实盘 15:50 扫描可当日成交，回测更保守。
- **定时**：4 个 cron 运行在深寻 Harness，需 App 保持运行；机器休眠可能错过时点，可手动重跑。
- **不构成投资建议**。

---

## 十、年度检查清单

1. **权重检查**：累计贡献占比 vs 75/20/5，偏离 >5pp 提示。
2. **集中度检查**：TSLA + NVDA 合计；SMH 前十大含 NVDA，持有 SMH 等于间接持有。
3. **NVDA 纪律**：若单独买 NVDA，从 SMH 的 20% 里划 5%，不提高整体科技股比例。
4. **预算/FX 检查**：每年复核 `total_monthly_aud`、FX fallback 与佣金模型；用 `--backtest` 看规则是否仍有效。
5. **欠额检查**：报告中的账龄与最早欠额月份；不要让欠额长期挂账。

---

## 十一、验证记录

- **设计/测试矩阵**：`docs/design.md`、`docs/test_matrix.md`
- **数据/指标核验**：`python3 verify/check_indicators.py --with-ledger` → **143 PASS / 0 FAIL / 0 WARN**
- **面板/记账服务**：`python3 verify/check_dashboard_exec.py` → **44 PASS / 0 FAIL / 0 WARN**
- **v2 测试矩阵**：`python3 verify/test_matrix.py` → **39 PASS / 0 FAIL / 1 SKIP**（SKIP：exchange_calendars 未安装，内置表回退）
- **T+1 回测**：`python3 scripts/scan.py --backtest` → `reports/backtest_YYYY-MM-DD.md`；硬约束 108/108 个月投满；评分未跑赢固定第 1 日
- **旧策略审查**：`verify/strategy_review.md` + `verify/sweep.py`（历史对照，旧规则结论仍有效）

*生成于 2026-11-13 · 所有报告为规则引擎输出，不构成投资建议。*

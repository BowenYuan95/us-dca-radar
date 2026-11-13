# 测试矩阵 v2

> 配套：`docs/design.md`。执行顺序：Stage 1（A/B/C）→ Stage 2（D/E/G）→ Stage 3（F）。
> 约定：每项记录 `ID / 场景 / 输入 / 预期 / 自动化位置 / 结果`。结果列在实施时填写 PASS/FAIL/SKIP/未跑。

## A. 日历 / 时区

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| A1 | 常规交易日盘中 | `--now-et "2026-11-12 15:50"` | can_fill_now=true；close=16:00；不出现提前收盘 | `verify/test_matrix.py::A1` |
| A2 | 提前收盘日 | `2026-11-27 12:30` / `13:30` | 12:30 可成交；13:30 不可成交；下一时段 11/30 09:30 | A2 |
| A3 | 历史提前收盘 | `2025-12-24 13:30` | 内置表识别 13:00 收盘，不可成交 | A3 |
| A4 | 感恩节 | `2026-11-26 09:35` | trading_today=false；下一开盘 11/27 09:30 = 墨尔本 11/28 01:30 | A4 |
| A5 | 周末 | `2026-11-14 12:30` | 下一开盘 11/16 09:30 = 11/17 01:30 | A5 |
| A6 | 盘前 | `2026-12-01 08:00` | can_place_order=true、can_fill_now=false；文案「开盘后」 | A6 |
| A7 | 月末收盘后 | `2026-11-30 16:30` | 不出现「今日」；下一时段 12/01；未投则欠额 | A7 |
| A8 | DST | `2027-07-15 09:35` / `2027-01-15 09:35` | 墨尔本 23:30 当日 / 01:30 次日 | A8 |
| A9 | digest 提前收盘 | `--digest --now-et "2026-11-27 14:00"` | 覆盖 11/27，不是 11/25 | A9 |
| A10 | digest 跨月 | `--digest --now-et "2026-12-01 01:00"` | 覆盖 11/30；month=2026-11；day_idx=20 | A10 |
| A11 | 日历超范围 | `2029-01-02` | calendar_source=unsupported；不给交易建议；无 weekday 退化 | A11 |
| A12 | 库 vs 内置表 | 2017–2028 抽样日期 | 交易日/提前收盘一致；库缺失则 SKIP 并说明 | A12 |
| A13 | Nasdaq 跨日 | 美东 01:00 的备用源 | 使用 ET 日期，不取空图 | A13 |
| A14 | Yahoo bar 日期 | ^VIX / UTC bar | bar 日期正确 | A14 |

## B. 币种 / FX / 预算

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| B1 | 新鲜 FX | rate=0.655，age=1h，预算 A$2,500 | 折合 US$1,637.50；firm=true；A$/US$ 双标 | B1 |
| B2 | 缓存 <ttl | age=5h | firm=true | B2 |
| B3 | FX 过期 | age=30h | firm=false；显示估算；CLI 确认必须 `--fx-rate`/`--amount-usd` | B3 |
| B4 | fallback | 无源，fallback=0.65 | firm=false；警告；不得 1:1 | B4 |
| B5 | FX 缺失 | 无源无 fallback | AUD 义务可见；REVIEW_FX；无确定 USD 金额 | B5 |
| B6 | 币种不匹配 | symbol meta.currency=EUR | 硬错误，拒绝混币 | B6 |
| B7 | 预算敏感性 | 2000 / 2500 / 3000 | 计划线性缩放；TSLA 佣金%正确；无 2500 常量 | B7 |
| B8 | 台账金额 | 成交 A$125，FX 0.65，佣金 $1 | amount_aud=125；gross_usd≈81.25；net_invest≈80.25；股数一致 | B8 |

## C. 台账 / 幂等

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| C1 | 全成 | order + fill 全成 | 1 order+1 fill+allocation；pending=0；DONE | C1 |
| C2 | 部分成交 | fill 50% + fill 50% | 第一笔 pending>0 不 DONE；第二笔后 DONE | C2 |
| C3 | 取消（含部分） | partial + cancel | 已成交保留；未成交回义务；不 DONE | C3 |
| C4 | 重复 fill_id 同 payload | 同 fill_id 两次 | no-op，只计一次 | C4 |
| C5 | 重复 fill_id 冲突 payload | 同 fill_id 不同金额 | 报错；DB 不变 | C5 |
| C6 | 面板重试 | 同 idempotency_key 两次 POST | 只写一次 | C6 |
| C7 | 超买 | fill > order 计划额 | 拒绝；事务回滚 | C7 |
| C8 | CLI 双击 | 无 fill_id，两次相同 | 警告；要求显式键/--force；不静默双计 | C8 |
| C9 | placed | status=placed | 只算 pending；PENDING；不提示重复买 | C9 |
| C10 | 旧 CSV 导入 | 旧 dca_log.csv | 一行→order+fill+allocation；不双计 | C10 |
| C11 | 并发写 | CLI + 面板同时写 | 唯一约束/事务；无丢单 | C11 |
| C12 | 回填上一月 | `--trade-date` 上月 | fill 真实日期；allocation 归上月；欠额清 | C12 |

## D. 义务 / 欠额

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| D1 | 默认 pay | 正常月 | 每标每月 BUY；无 DEFER | D1 |
| D2 | 漏跑整月 | 上月无成交 | 下月 required=base+欠额；BUY_NOW；账龄展示 | D2 |
| D3 | 连续漏 4 月 | 4 月无成交 | 欠额累计不消失；单笔受 max_catchup；剩余继续挂账 | D3 |
| D4 | 超过任意月数 | 欠额长期未清 | 无自动豁免；只有显式 write-off 才减少 | D4 |
| D5 | 手工合并 | `--months 2026-09,10,11` | 一笔真实 fill + 多 allocation；无假历史成交 | C12 |
| D6 | 自动 batch 配置 | `commission.policy=batch` | 配置校验明确拒绝；提示改用 pay + --months | B/C |
| D7 | 权重非法 | 权重和≠1/负值 | 配置校验失败 | D7 |
| D8 | 预算版本变更 | 中途改预算 | 历史月不变；当前月用新预算 | D8 |
| D9 | start_month 之前 | 早于起始月 | 不产生幻影欠额 | D9 |
| D10 | 容差 | 少投 1.9% | 不判 DONE；只允许 0.5%/A$0.5 取整 | D10 |

## E. 决策 / 评分

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| E1 | VOO early_days=1 | 当日可成交/盘后/次日 | BUY / NEXT_SESSION / BUY_NOW | E1 |
| E2 | SMH/TSLA 窗口（默认 early_days=1，可配 3） | 评分高低 | 达标提前；到窗口末日必投 | E2 |
| E3 | 评分变化 | 金额/窗口 | 不降额、不跳过、不拖过窗口 | E3 |
| E4 | 挂单中 | status=placed | PENDING；不重复买 | E4 |
| E5 | 收盘后/休市 | POST/HOLIDAY | NEXT_SESSION；无「今日」 | E5 |
| E6 | early_days=1 | 评分任意 | 评分仅展示，不影响执行 | E6 |
| E7 | allow_split=true | 窗口内分批 | 仅窗口内；最后一笔强制完成 | E7 |

## F. 回测

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| F1 | T+1 开盘 | T close 信号 | 成交价=T+1 open；不是 T close | F1 |
| F2 | 月末信号 | T=月末 | T+1 下月首日；obligation_month=信号月；无假日期 | F2 |
| F3 | 无前视 | 打乱 T 后数据 | T 日决策不变 | F3 |
| F4 | XIRR | 已知现金流 | 结果正确；多笔收敛；无解 n/a | F4 |
| F5 | 最大回撤 | 已知 TWR 序列 | 结果正确；入金不冲小回撤 | F5 |
| F6 | 期末价值/现金 | 手工案例 | 与手工一致；pending 计现金不计成交 | F6 |
| F7 | 佣金/汇率 | 已知金额 | 净买入=预算-佣金；AUD 成本一致 | F7 |
| F8 | 硬约束 | 全样本 | 100% 月份义务覆盖；期末欠额 0 或列示 | F8 |
| F9 | 规则集合 | 全部规则 | fixed_1/2/3/5/10/last、month_avg、weekly、score_only、random_window、oracle 跑通 | F9 |
| F10 | 评分诊断 | 日频数据 | IC/五分组无未来数据 | F10 |
| F11 | 样本外 | 前后两段 | 分别报告，不混 | F11 |
| F12 | 预算敏感性 | 2000/2500/3000 | 表完整；无硬编码 | F12 |
| F13 | 旧规则回归 | legacy_current | 与旧 --backtest 差 ≤0.2pp | F13 |
| F14 | 跳空/缺失 | T+1 open 缺失/0 | 用 close 并标 fallback | F14 |
| F15 | FX 缺失 | 无历史 FX | USD 主口径可跑；AUD 副口径标近似 | F15 |

## G. 报告 / 面板 / 回归

| ID | 场景 | 输入 | 预期 | 自动化 |
|---|---|---|---|---|
| G1 | 报告首屏 | 正常生成 | 硬性义务表；A$/US$；FX 来源/状态 | G1 |
| G2 | 不可成交 | 盘后/休市 | 无「今日」；下一窗口双时区 | G2 |
| G3 | 台账状态 | 混合状态 | 成交/挂单/部分/取消分列；pending 不算完成 | G3 |
| G4 | 面板 | FX 过期/端口 | 禁用确认；幂等键；端口与 --port 一致 | G4 |
| G5 | 迁移 | 旧 signals/CSV | 可读；新字段完整 | G5 |
| G6 | 文档 | README/verify | 无「合并永不执行 / 95% CI 误标 / USD-only」漂移 | G6 |
| H1 | 编译 | 全部 py | `py_compile` 通过 | H1 |
| H2 | 现有核验 | check_indicators --with-ledger；check_dashboard_exec | 0 FAIL | H2 |
| H3 | 回测 | backtest.py --quick | 退出 0；产物完整 | H3 |
| H4 | 性能 | 10 年回测 | 可接受（缓存命中） | H4 |
| H5 | 基线 | 前后 md5 | 无并发覆盖 | H5 |

## 执行说明

- 自动化入口：`verify/test_matrix.py`（新增），以及现有 `verify/check_indicators.py --with-ledger`、`verify/check_dashboard_exec.py`。
- 依赖网络的项（Nasdaq/A12）允许在离线时 SKIP，但必须打印原因，不得伪造 PASS。
- 每阶段结束更新本表「结果」列，并在 `verify/data_check.md` 记录实际命令、版本 md5 和 PASS/FAIL 计数。

## 最新执行结果（2026-11-13 18:05 AEDT）

| 套件 | 命令 | 结果 |
|---|---|---|
| 数据/指标 + 台账 | `python3 verify/check_indicators.py --with-ledger` | 143 PASS / 0 FAIL / 0 WARN / 3 INFO |
| 面板/记账服务 | `python3 verify/check_dashboard_exec.py` | 44 PASS / 0 FAIL / 0 WARN |
| v2 矩阵 | `python3 verify/test_matrix.py` | 39 PASS / 0 FAIL / 1 SKIP（exchange_calendars 未安装，内置表回退） |
| T+1 回测 | `python3 scripts/backtest.py` | hard_score 108/108 投满；A$756,783 vs fixed_1 A$756,840（-0.01%）；结论：评分未跑赢固定第 1 日 |

# IBKR 日本股票多周期研究核心

根据 `IBKR日本股票多周期协同策略文档.md` v1.2 新建，并按 `CLAUDE_CODEX_协作文档.md`（2026-10-04 评审）完成第 1 轮修复。本项目实现 Python 3.11+ 的离线研究、事件回放、订单生命周期核心和研究校准工具，仅使用标准库。Python 3.14 下已运行验证。

四层依次负责环境、主信号、入场确认和执行。账户风险、对账、时间退出始终优先于新入场。首版为日内长仓趋势延续，多股票共享资金与风险额度。

协调器按单一事件循环运行，外部数据和券商回报应先进入有序队列，再调用协调器；它不是可以从多个回调线程任意并发调用的实时框架（`PortfolioRisk.allocate` 例外：预分配是原子的）。磁盘保存由回放/消费者负责，交易回调只更新内存和请求队列。

## 运行

在本目录运行：

```powershell
python -m ibkr_microalpha validate-config examples/research.json
python -m unittest discover -v
python -m ibkr_microalpha demo
python -m ibkr_microalpha demo --verify-replay
```

示例生成 10 分钟以上的人工行情暖机、日内同时段曲线（成交量与基准波动）、人工校准表、累计成交量、行情心跳、账户资金快照、模拟订单回报及跳价止损。它验证闭环行为，**不是历史回测，也不证明任何收益优势**。示例费用、资本、权限、校准和模型均为演示值，不代表用户的 IBKR 账户。示例主动单只有在限价覆盖有效 Ask/Bid 且可见数量足够时才成交；不模拟被动队列成交。`--verify-replay` 会用新引擎重放生成的输入并要求报告一致。

重复回放同一份完整输入：

```powershell
python -m ibkr_microalpha replay runs/demo/events.jsonl --config examples/research.json --output runs/replayed
```

成功时输出 `report.json`、`audit.jsonl`、`execution-journal.jsonl`、`execution.json`、`raw-input.jsonl` 和 `frozen-config.json`；原始输入与审计在运行中流式写盘。失败时不写 `report.json`，改写 `failure.json`（来源、已验证事件数、错误、manifest）。报告包含逐层漏斗、逐意图结果（含压力预算是否被突破）、执行质量、软/硬风险状态、请求计数、运行清单（代码/配置/输入 SHA-256）及本机处理耗时指标。

一个回放运行对应一个日初冻结的账户交易日。多日研究应逐日运行并另行汇总，不能将跨日资金、费用或日损失阈值静默拼接。

## 模块

| 文件 | 已实现职责 |
| --- | --- |
| `domain.py` | 报价、特征快照（严格布尔与数值校验）、纯信号候选、状态类型 |
| `market.py` | `SessionSchedule`（交易所时段与冻结的策略截止时间）、可追溯日历、价档、统一报价校验（入场/估值两档年龄） |
| `features.py` | 二分查找的滚动窗口、行情心跳覆盖、网格采样波动、索引化历史基准与日内同时段曲线、市场层快照（每时刻一次） |
| `signals.py` | 冻结稳健标准化（含截断率）、可配置评分权重、环境、单一候选流、CAUTION 政策、平滑否决的确认、衰减退出 |
| `entry.py` | 入场管线：前置条件 → 候选 → 止损/风险预分配/不可变价格上限 → 校准经济门控 → 确认 → 带上限主动限价与短订单 TTL；发送前复核 |
| `positions.py` | 持仓退出（止损/最长持仓/计划退出/信号退出）、退出状态、冷却与连续止损、残余风险报警 |
| `valuation.py` | 估值级报价下的日盈亏、未报费用、逐股压力预算、券商资金快照 |
| `engine.py` | 事件路由、市场状态（OK/CAUTION/RISK_OFF/UNKNOWN）、风险优先级、同一时刻批量评估 |
| `economics.py` | 逐子单佣金、完整意图价值、冻结校准表、按实际限价修正、执行政策比较、时间预算 |
| `risk.py` | HARD 锁与 SOFT 阻断（超时升级）、原子预分配、组合压力预算、入场意图计数、账户快照 |
| `execution.py` | 幂等订单、撤单确认、更正后卖单覆盖不变量、发送前复核钩子、日常请求预算、O(1) 订单费用、可重放的入场评分与止损收紧 |
| `subscriptions.py` | 逐笔额度调度、版本 READY、增强版预分配协调（仅用 L1 特征排序，按 30–60 秒节拍） |
| `reporting.py` | 逐层漏斗（经济/风险拒绝分开计数）、逐意图价值（复用 `intent_path_value`）、按当前成交修订的执行质量与成交后漂移 |
| `replay.py` / `cli.py` | 先校验后应用的回放、摘要去重、失败即中止并输出 `failure.json`、流式落盘、运行清单与指标 |
| `research/` | 因果意图标签、按交易日区块自助法的校准行、scaler 拟合与截断率、带隔离期的滚动切分 |

## 关键规则（本轮修复后）

- **风险锁分两级**：HARD（日损失、KILL_SWITCH、账本/对账不一致、残余风险、退出价格规则、升级后的 SOFT）需人工解除并退出受控风险；SOFT（估值暂缺、行情或市场数据过期、账户快照缺失）只阻止开仓，条件恢复即解除，持有受控风险时持续超过 `max_soft_block_seconds` 才升级为 HARD。
- **无变化≠断流**：`quote_stream_health` 心跳证明订阅存活时，安静的盘口不重置窗口；入场用 `max_age_seconds`（2 秒），估值、止损和退出定价用 `valuation_max_age_seconds`（30 秒）。持仓股特征暖机只清零信号退出状态，硬退出照常。
- **市场数据缺失是 MARKET_UNKNOWN**：阻止开仓，不触发紧急清仓；只有阈值触发的 MARKET_RISK_OFF 或交易所异常才退出全部受控风险。
- **经济门控用冻结校准表**：`economics_source: calibration` 时由 `calibration_table` 事件提供（策略 × 持仓期 × 数量 × 分数桶的均值与置信下界）；`forecast` 事件仅用于研究覆盖。数量与校准不符时以 `calibrated_quantity_unavailable` 明确拒绝。
- **入场单**：限价 = min(候选不可变上限, ask + `entry_limit_ticks`)，订单 TTL = `entry_order_ttl_seconds`，到期撤余量且同一候选不重挂；排队中的入场单在真正发送前按当时报价、预测、上限和风险复核。
- **止损与压力**：止损 = max(`min_stop_ticks` 个价档, `stop_bps`, `stop_volatility_multiple` × 已知波动)，换算为 JPY/股；退出滑点按价档数；压力损失含 `gap_reserve_bps` 跳价储备，并受 `portfolio_stress_fraction` 组合上限约束。实际亏损超过压力预算时报告并审计 `STRESS_BUDGET_EXCEEDED`。
- **确认层**：单次负失衡只返回 WAIT；平滑 OBI 在完整平滑跨度上反转才否决。
- **连续止损**：止损后的冷却 = `stop_cooldown_seconds` × 当日该股连续止损次数（线性延长，不是倍增）；达到 `max_consecutive_stops` 后当日停用该股；有成交的普通退出清零计数。
- **成交量来源**：L1 版本只接受 `cumulative_volume`（累计量差分），`trade` 事件只接受逐笔（`volume_kind: TICK`）。
- **时间预算**：`ChannelBudget`（确认 + 提交 p99 + 订单 TTL + 缓冲 ≤ 候选 TTL）在配置构建时检查。
- **配置画像**：`profile: demo|research|shadow`；非 demo 必须提供 `provenance`（scaler、阈值、经济参数的来源、训练截止、代码哈希及 scaler 截断率 ≤ 2%）。

## 数据和事件

所有事件按 `(received_at, sequence)` 排序；`received_at` 必须含时区。交易所时间只用于记录和诊断。同一接收时刻的股票更新在时刻推进或 `requests` 前合并评估一次。

| `type` | `data` 要点 |
| --- | --- |
| `calendar` | `day,is_open,known_at,source,version,policy_meeting` |
| `scheduled_window` / `announcement` | 决策当时可知的前置窗口 / 实际接收时刻开始的后置观察期 |
| `quote` | 来源、实时类型、价量及字段时间 |
| `quote_stream_health` / `stream_health` | 报价 / 成交流心跳；`healthy:false` 立即重置并复核活动入场单 |
| `cumulative_volume` | `symbol,total,last_price`；L1 累计量差分转为采样成交 |
| `trade` | 逐笔成交（`volume_kind: TICK`）；采样成交量被拒绝 |
| `same_time_profile` | 日内同时段曲线：`kind(volume/volatility),symbol,source,window_seconds,bucket_start_second,bucket_seconds,median_value,valid_days,known_at,version` |
| `volume_baseline` / `volatility_baseline` | 旧版逐日逐秒样本（`baseline_bucket_seconds: 0` 时使用） |
| `daily_vwap` | 完整同源日内累计量价种子 |
| `calibration_table` | `version,known_at,rows[]`（`CalibrationRow` 字段） |
| `forecast` | 研究覆盖用逐股预测（`economics_source: forecast`） |
| `market_snapshot` | 仅 `market_source: external` 时接受 |
| `feature_snapshot` | 预计算特征（严格布尔 `valid`） |
| `account_snapshot` | `account_id,currency(JPY),available_funds,net_liquidation,source` |
| `exchange_status` | `normal,reason`；异常即 MARKET_RISK_OFF |
| `requests` | 先完成同时刻评估与风险轮询，再按复核结果发送 |
| `status` / `fill` / `commission` / `reconcile` | 回报与对账，语义同前 |
| `disconnect,reconnect,ambiguous,reject,data_reset,subscription_failed` | 通道、订单和数据异常 |
| `timer,kill_switch,manual_unlock` | 无行情时持续风险检查；解锁需显式确认，日损失当日不可解锁 |

L1 配置使用 `vwap_proxy_*`，冻结版本必须为 `l1-proxy-*`。增强版需 `TBT`、`TICK`、独立评分版本和 `subscriptions`（`quota,min_tenure_seconds,required_windows,plan_interval_seconds`）；中途订阅的完整成交 VWAP 需要 `daily_vwap` 种子。价格与金额使用 Decimal；价档只实现东证内国普通股票 TOPIX500/OTHER 两类，2027-03-01 起旧表自动停止放行。[JPX 报价规则](https://www.jpx.co.jp/english/equities/trading/domestic/07.html)

示例佣金仅演示 Fixed 基础费率与单笔最低费用。[IBSJ 佣金说明](https://www.interactivebrokers.co.jp/en/pricing/commissions-stocks.php) 订单状态与撤单竞态按 IBKR 官方说明建模。[订单状态](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/introduction)、[撤单状态及竞态](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/understanding-order-status-message)

## 研究工具

`ibkr_microalpha.research` 提供把真实采集数据变成冻结参数的最小管线：`label_intent`（与协调器同一固定主动政策的意图净额，未成交为 0、无法平仓为 None）、`walk_forward`（按交易日滚动、隔离期、最终保留期）、`build_calibration_rows` 与 `day_block_lower_bound`（按交易日区块自助法的均值置信下界）、`fit_scalers`（含截断率）和 `calibration_table_event`。这些工具只处理离线数据，不连接券商。

## 验证边界

评审问题台账与实施状态见 `CLAUDE_CODEX_协作文档.md`，T01–T30 对照见 `IMPLEMENTATION_STATUS.md`。没有 TWS/Gateway socket 客户端、真实账户凭证、实时数据订阅、真实交易所状态源或样本外盈利证据。CLI 仅支持 replay，不能打开实盘。

# IBKR 日本股票多周期研究核心

根据 `IBKR日本股票多周期协同策略文档.md` v1.2 新建，并按 `CLAUDE_CODEX_协作文档.md`（2026-10-04 评审）完成 Claude 首轮修复及 Codex 复核发现问题的修复。本项目实现 Python 3.11+ 的离线研究、事件回放、订单生命周期核心和研究校准工具，仅使用标准库。Python 3.14 下已运行验证。

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

成功时输出 `report.json`、`audit.jsonl`、`execution-journal.jsonl`、`execution.json`、`raw-input.jsonl` 和 `frozen-config.json`。输入/config 与任何产物指向同一文件（包括 hardlink）会在写前拒绝。CLI 将原始输入与审计流式写入同文件系统临时目录；两个流关闭成功后发布产物，最后发布 `report.json` 作为成功标记。可捕获的发布失败会回滚旧完整运行；失败尝试放在空输出目录的 `failure.json`，或既有输出目录的 `.failed/<id>/`。若回滚也失败，保留 backup 并移除成功标记。任何应用或证据持久化错误都会封锁本次 Replay，必须从原输入重建。

报告包含逐层漏斗、逐意图结果（含压力预算是否被突破）、执行质量、软/硬风险状态、请求计数及代码/配置/输入 SHA-256。耗时使用每事件类型固定 192 桶；count/max 精确，p50/p99 近似。计量范围为本机 dispatch 墙钟，包含证据写入，不含源文件读取、最终批次/flush 和 save，不表示券商延迟。此发布协议未提供断电持久性或并发读者的多文件快照隔离。

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
- **经济门控用冻结校准表**：`economics_source: calibration` 时由 `calibration_table` 事件提供（策略 × 持仓期 × 数量 × 分数桶的均值与置信下界）；`forecast` 同样需要来源与独立日证据。每个数量使用自己的价格上限、止损和费用，较大数量不能缩量后沿用原上限。临时越过价格上限的未下单候选可在原 TTL 内继续等待，活动买单越界仍撤余量。
- **资金快照需账本屏障**：`ledger_sequence` 必须对应完整对账后的当前 journal 序号，`covered_order_ids` 明确快照已计入的全部活动且已发送 BUY。相同账户锚点后的新买入与费用继续扣除；覆盖中的买单本金不重复扣除。卖出、bust、更正及撤单不自动释放券商余额，资金增加需新屏障快照确认。缺少屏障、账户变更或快照失效阻止开仓。待发送 BUY 复核包含全部排队占用的资金、单股/组合/行业金额与压力；拒绝时原数量不变并本地终止。
- **异常空头保留事实并报警**：买入更正后晚到 SELL 造成负仓位时，冻结新交易、请求对账并继续其他风险检查；不会将负数量传入长仓费用函数，也不会继续卖出异常空头。
- **恢复包含请求政策**：execution 快照 v2 与新 journal 保存冻结预算、JST 请求日、例行和总发送计数；恢复后断连、清空旧请求并要求完整对账。旧格式缺预算/发送证据时关闭例行发送；显式预算及完整历史才能迁移，风险请求保留优先权。旧退出被 bust 后，原订单到意图映射保留；修复退出按原意图残余拆分归属。
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
| `account_snapshot` | `account_id,currency(JPY),available_funds,net_liquidation,source,ledger_sequence,covered_order_ids[]`；最后两项为资金对账屏障 |
| `exchange_status` | `normal,reason`；异常即 MARKET_RISK_OFF |
| `requests` | 先完成同时刻评估与风险轮询，再按复核结果发送 |
| `status` / `fill` / `commission` / `reconcile` | 回报与对账，语义同前 |
| `disconnect,reconnect,ambiguous,reject,data_reset,subscription_failed` | 通道、订单和数据异常 |
| `timer,kill_switch,manual_unlock` | 无行情时持续风险检查；解锁需显式确认，日损失当日不可解锁 |

L1 配置使用 `vwap_proxy_*`，冻结版本必须为 `l1-proxy-*`。增强版需 `TBT`、`TICK`、独立评分版本和 `subscriptions`（`quota,min_tenure_seconds,required_windows,plan_interval_seconds`）；中途订阅的完整成交 VWAP 需要 `daily_vwap` 种子。价格与金额使用 Decimal；价档只实现东证内国普通股票 TOPIX500/OTHER 两类，2027-03-01 起旧表自动停止放行。[JPX 报价规则](https://www.jpx.co.jp/english/equities/trading/domestic/07.html)

示例佣金仅演示 Fixed 基础费率与单笔最低费用。[IBSJ 佣金说明](https://www.interactivebrokers.co.jp/en/pricing/commissions-stocks.php) 订单状态与撤单竞态按 IBKR 官方说明建模。[订单状态](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/introduction)、[撤单状态及竞态](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status/understanding-order-status-message)

## 研究工具

`quote_baseline_label` / 兼容数值接口 `label_intent` 仅提供 `QUOTE_BASELINE_V1` 报价基线；部分成交、覆盖不足及不完整退出返回 None，不能制作正式校准。`replay_intent_labels(runner, verified_manifest=..., source_events=...)` 从原始事件重建完整协调器，核验 manifest、原始候选评分、政策元数据、终态、对账及每笔最终费用，生成 `ReplayIntentLabel`；使用 raw sink 时必须提供原始事件，直接修改 runner 账本不能生成标签。

`build_calibration_rows` 仅接受该工厂标签，按交易日区块自助法生成置信下界并附完整政策、费用、代码、输入、标签哈希和训练截止证据。`engine.min_independent_days` 必须显式冻结且至少为 2（示例为 20，并非研究上足够的保证）；1 日高密度样本和矛盾 day/count 被拒绝，不足的桶省略。非 demo 只接受 `VERIFIED_REPLAY` 且训练截止严格早于表可用时刻；人工标签只能进入明确的 demo。追价上限须与标签原执行政策一致。`walk_forward`、`fit_scalers` 和 `calibration_table_event` 仍用于离线切分、拟合及序列化，不连接券商。

## 验证边界

评审问题台账与实施状态见 `CLAUDE_CODEX_协作文档.md`，T01–T30 对照见 `IMPLEMENTATION_STATUS.md`。没有 TWS/Gateway socket 客户端、真实账户凭证、实时数据订阅、真实交易所状态源或样本外盈利证据。CLI 仅支持 replay，不能打开实盘。

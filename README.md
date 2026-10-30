# IBKR 日本股票多周期研究核心

根据 `IBKR日本股票多周期协同策略文档.md` v1.2 新建，最新双方意见综合及第4轮完善见 `CLAUDE_CODEX_协作文档.md` 第14节（2026-10-29）。Python 3.11+ 离线研究、事件回放、订单生命周期及研究工具使用标准库；可选只读行情采集器需要另行提供官方 `ibapi` SDK。Python 3.14 下已运行验证。

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

第4轮增加以下可执行研究链路：

```powershell
# 原始回调 → 原始证据、规范replay事件、捕获报告（需先填写真实已知合约和单位）
python -m ibkr_microalpha capture-callbacks callbacks.jsonl --capture-config examples/capture-template.json --output runs/capture
# 预登记决策与参数网格 → 2/10/30/60分钟、费用/滑点敏感性研究
python -m ibkr_microalpha scan-holdings runs/capture/events.jsonl --decisions decisions.jsonl --config verified-research.json --plan examples/holding-scan.json --registry runs/experiments --output runs/holding-scan.json
# 每fold训练scaler、验证择参、外层test；最终holdout默认封存
python -m ibkr_microalpha walk-forward observations.jsonl --plan examples/walk-forward.json --registry runs/experiments --output runs/walk-forward.json
```

`callbacks.jsonl` 每行字段为 `callback,received_at,request_id,data`；tickPrice/tickSize 使用 `data.tick_type` 和 `price/size`。bid/ask 保留各自本地接收时间，未知交易所时间不补造。只固定选用 tick8 或 RTVolume 一种累计量，并显式配置单位。farm恢复、halt=0及参考合约活动都不证明该股处于连续交易或有逐股heartbeat，缺权威状态的报价保留 UNKNOWN。模板 `contract_verified=false`，可选 `capture-ibkr --capture-config ... --output ...` 会在合约未确认时拒绝连接；该采集命令不提供下单接口。

`decisions.jsonl` 每行包括 `decision_id,symbol,at,score,quantity`，可提供决策前已知 `volatility_bps,volatility_window_seconds`。决策和网格必须预先冻结；不能看完结果再挑决策。版本化撮合器明确标记 SIMULATED，包含提交/确认/撤单延迟、部分成交、盘口消耗和覆盖未知；扫描是条件单意图实验，尚非完整策略/组合回测。任何未知路径使 all-intents 统计为空；负下界只表示未证实正优势，上界为负才支持该模型下负期望。比较多时自动用Bonferroni，并报告bootstrap尾部解析度不足。模拟结果不能部署。

`observations.jsonl` 包括 `decision_id,at,completed_at,features,outcomes,execution_source`；`outcomes` 按预登记的 `variant_id` 保存已完整获得的净结果，不能删除不利/未知分支。实验登记全部试验、统计方法及代码/数据哈希。只有显式 `--use-final-holdout` 才评估保留集；同dataset与日期集合的消费凭证在评估前独占写入，失败也不恢复成可再次查看。输出不宣称已实现deflated Sharpe、SPA或真实盈利优势。

非demo的 `provenance.scalers/thresholds/economics` 现在统一要求 `trained_until < available_at`、`code_hash`（64位小写SHA）及 `content_sha256`；使用 `parameter_content_hash(document, group)` 绑定实际冻结内容。接收时间早于可用或超过有效期会被拒绝。正式经济行使用 `REPLAY_POLICY_NET` 和实际成交成本基准；回报需 `execution_source=BROKER_REPORTS,source_record_id`，含模拟、人工或缺失来源不能升级成真实执行证据。旧artifact须按第14.3节迁移。

`market_controls` 可显式启用逐日权威价格带、PIT价档/场所/手数规则、逐股状态及受限事件窗口；对应事件为 `price_band,instrument_rule,symbol_status,symbol_window`。逐股 `beta_estimate` 提供过去配对估计、收缩和可用/到期时间；缺失/过期可阻断信号。`examples/guarded-demo.json` 为这些保护的人工配置样例，不能作为真实校准或真实市场事实。`scale_stop_to_holding` 显式采用sqrt(H/60)波动缩放假设，默认保持旧行为，效果须研究验证。

完整策略恢复使用 `recovery.restore_from_events(document, events, expected_manifest=...)`，重建风险锁和计数后断连、丢弃旧命令与报价，等待新的完整对账/资金屏障。execution快照本身仍不能恢复全策略；传输WAL、跨日管理、独立watchdog及全账户混合持仓管理尚待生产阶段实现。

## 券商路由与增量行情

```powershell
python -m ibkr_microalpha paper-trade --config examples/jp7-q100-max200.json --capture-config examples/capture-jp7.json --preamble runs/paper-hist/20261028/events.jsonl --output runs/paper-live/20261102 --port 4002 --until 15:20
```

`paper.py` 把行情回调与券商回报（orderStatus/execDetails/commissionReport）放入单一队列，由消费线程经 `Replay.dispatch` 处理；订单只来自 `requests` 事件后的 `last_commands`，输出目录的 `raw-input.jsonl` 可用 `replay` 复现。默认仅接受 4002/7497 和 DU 模拟账户。启动前检查采集/策略/基准名单完全一致及合约验证标记；启动对账发现持仓、挂单或该 client 的旧成交即拒绝，已有交易需先恢复原账本。订单号从 1000 起。`--until` 到点触发 kill switch 平仓。

`examples/capture-jp7.json` 采用 `quote_mode:incremental`，两份 jp7 配置均包含 6723。`bid_at/ask_at` 保存原始价格回调时间，`bid_observed_at/ask_observed_at` 保存同侧有效数量回调对当前价格的观察时间，统一报价校验核对两套时间的顺序。只有该股票真实、有效的盘口回调才能更新订阅覆盖；定时器、模拟状态及基准活动不能充当其他股票的心跳。真正断流、延迟行情、停牌仍会失效，默认 strict 模式保留原有规则。`features.sampled_subscription_coverage:true` 仅用于同一订阅的累计量代理，不能用于独立逐笔成交源。累计量向下修正只重建量/VWAP窗口，保留独立报价历史；瞬时锁盘口不输出为有效报价。

本地旧 preamble 没有 6723 的 20 日成交量基准；模拟模式会明确报告缺口并阻止缺特征的候选，实盘启动检查会拒绝缺基准的输入，不补造历史。

模拟盘专用假设：IBKR 默认行情不提供东证逐股停牌/状态，连续竞价时段内会写入标注为 `PAPER_SCHEDULE_ASSERTION` 的 halt=0 与短时 CONTINUOUS 状态；该假设不得用于实盘。资金使用配置中的 `initial_cash` 建模，不读取账户资金快照。校准表与同时段曲线从 `--preamble` 指定的往日输入复用（示例为 ARTIFICIAL 校准，仅用于链路验证）。

显式 `--live-account U... --port 4001`（或 7496）启用实盘检查：要求可验证的 research/shadow 参数、正式 BROKER_REPORTS 校准、真实费用、`require_account_snapshot:true`、完整历史基准，以及 `--status-file` 指定的外部日历和逐股状态。演示配置 `jp7-live-small.json` 仍如实标注 demo，**不能作为实盘配置启动**。日损失阈值仅是触发条件，不保证最大实际亏损。

状态文件由外部权威数据源原子替换，结构为 `day,is_open,source,known_at,valid_until,symbols[]`；每个 symbol 行包含 `symbol,status,source,known_at,valid_until`，须覆盖全部交易股票和基准，时间为带时区 ISO 格式。允许 CONTINUOUS/UNKNOWN/HALT/SPECIAL_QUOTE。缺失、过期或人工/演示来源不授权交易；实盘不会合成 halt=0 或连续交易状态。此仓库没有内置真实状态服务和正式校准数据。

实盘资金先完整对账，再查询目标账户的 JPY AvailableFunds/NetLiquidation；查询中观察到订单或持仓变化则重做屏障，其他账户/币种不能覆盖余额。资金刷新期间阻止新买入，定期刷新并在超时后报告失败。资金刷新事件及资金屏障均写入 replay 输入。成交同时记录接收时间、原始券商时间和可解析的执行时间；无时区的旧 SDK 时间仅在提供 `--execution-timezone` 时解释，缺少时区时明确保留 UNKNOWN。

启动即保存 `frozen-config.json`、`capture-config.json` 和 `session-manifest.json`；每秒 flush、每 30 秒输出真实阻断原因、特征缺口与窗口跨度并更新 `status.json`。异常保存 `failure.json`、账本与 `cleanup.json`，仅尝试撤销本会话已经发送的活动订单，源码不调用全局撤单；清理回报尚未确认或持仓残留时需要核查账户。

## 验证边界

评审问题台账与实施状态见 `CLAUDE_CODEX_协作文档.md`，T01–T30 对照见 `IMPLEMENTATION_STATUS.md`。第4轮验收命令为 `python audit/2026-10-29/joint-round4/validate.py`，当前全量测试使用 `python -m unittest discover -q`。10月30日诊断与修复验证位于 `audit/2026-10-30/`。已实现受保护的券商路由入口；代码和离线回调重建不代表实盘可部署，也不证明收益优势。正式状态源、真实费用和独立校准证据须另外提供，不能将演示或单日历史改名后部署。

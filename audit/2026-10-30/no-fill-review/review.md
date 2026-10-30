# 2026-10-30 代码与无成交排查

直接原因：本次运行没有形成有效特征与市场快照，始终被 `market_data_unavailable` 阻止开仓；候选为 0，订单发送为 0，没有进入“订单等待成交”的阶段。

## 证据范围

- 当前进程：`python -u -m ibkr_microalpha paper-trade --config examples/jp7-live-small.json --capture-config examples/capture-jp7.json --preamble runs/paper-hist/20261028/events.jsonl --output runs/live/20261030 --port 4001 --live-account [已隐去] --until 15:20`。
- 命令名是 `paper-trade`，但实际启用了实盘账户和端口；README 中“无实盘入口”已与当前代码不一致。
- 冻结输入范围：2026-10-30 **13:16:18–14:26:37 JST**，共 **6,214** 个完整策略输入事件。
- 对冻结输入按当前磁盘配置做离线重放；没有连接券商、提交或撤销订单，没有修改策略源码、运行配置或进程。
- 运行程序没有在启动时保存 frozen-config，故不能独立证明磁盘配置与进程内配置完全一致；本文结论以当前配置与启动参数一致为前提。
- 三个增长中的日志逐一读取，各自截止时刻不同；分别记录完整前缀的字节数、哈希和尾部截断数，不直接将不同截止时刻的计数差解释为丢包。

| 检查项 | 重放结果 |
| --- | --- |
| 定时 `requests` 事件 | 4,220 |
| 其中含 `market_data_unavailable` 阻断 | 4,220 / 4,220 |
| 有效个股特征快照 | 0 |
| 有效市场快照 | 0 |
| 候选 / 入场意图 / 订单发送 / 成交 | 0 / 0 / 0 / 0 |
| 初始对账 | 已完成，持仓及活动订单为空 |
| HARD 风险锁 | 无 |

`requests` 是驱动策略与命令队列的事件，不代表发送了券商订单。缺少市场数据的 SOFT 阻断在候选产生之前就会生效，因此本次不是经济门槛、委托价、两秒订单 TTL 或券商拒单造成的零成交。

## 发现及修复优先级

### 1. P1：行情接入与窗口覆盖要求不匹配，直接造成零候选

代码位置：`ibkr_microalpha/recording.py:164`、`ibkr_microalpha/features.py:147`、`ibkr_microalpha/features.py:496`、`ibkr_microalpha/engine.py:474`、`ibkr_microalpha/entry.py:145`。

`CallbackRecorder._quote` 要求 bid、ask、bid_size、ask_size 四个字段的接收时间均在 2 秒内，且四者最大时间差不超过 0.5 秒。IBKR 这条接入使用 `reqMktData` 回调，字段异步更新。一侧价格没有变化但盘口数量在更新时，旧价格时间会使整条报价被过滤。

对真实回调重新规范化的诊断结果：1306 基准有 5,841 次报价尝试因字段年龄被拒，另有 387 次因字段时间差被拒，仅产生 283 条报价。7261 对应 2,411 次字段年龄拒绝、187 次时间差拒绝、210 条报价。这些是每次报价尝试的计数，不是独立断流次数。

与此同时，`FeatureConfig.max_quote_gap_seconds` 默认 2 秒；相邻可用报价间隔超过 2 秒且没有逐股覆盖证据就执行 `reset`，清掉收益、成交和 VWAP 斜率窗口。本次输入没有任何 `healthy:true` 的 `quote_stream_health`。配置中的 `require_quote_heartbeat:false` 不能关闭 `FeatureEngine.on_quote` 的历史覆盖重置。

| 股票 / 基准 | QUOTE_COVERAGE_GAP 重置次数 | 最长保留报价跨度（秒） |
| --- | ---: | ---: |
| 1879 | 15 | 0.601 |
| 6436 | 48 | 1.103 |
| 1719 | 33 | 1.002 |
| 8057 | 22 | 1.503 |
| 8708 | 19 | 0.701 |
| 7261 | 54 | 0.702 |
| 1306 | 91 | 1.102 |

策略需要 30/60/120/300 秒的特征，市场波动需要 300 秒窗口，但实际留不住这些窗口。市场快照全部缺 `rv_mkt`、`breadth`，有时还缺 spread；个股快照全部无效。结果是：

`异步字段被过滤 → 有效报价稀疏 → 覆盖间断清空窗口 → 市场快照无效 → market_data_unavailable → entries_blocked → 0 候选 → 0 订单`。

修复应让采集层与特征层明确支持这类增量行情：保留各字段真实接收时间，区分字段变化与同一股票订阅的覆盖证据，为历史窗口采用有依据的覆盖政策。恢复开仓前，先离线证明 300 秒窗口能够累积、正常停牌/断流仍会阻断。不能用定时器或别的股票的活动伪造逐股心跳，也不能直接删除市场风控门槛。

[IBKR 官方流式行情说明](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data)明确说明 `reqMktData` 提供聚合快照，报价价格和数量通过不同回调交付；本项目所需的字段同步与历史覆盖须由适配层另行处理。

### 2. P1：实盘入口允许使用 demo、人工校准与模型资金

代码位置：`examples/jp7-live-small.json:3`、`:7`、`:22`、`:51`，`ibkr_microalpha/paper.py:292`、`:419`。

当前配置为 `profile:demo`、`model_version:synthetic-v1`，启动导入的是 `ARTIFICIAL-calibration-v1`，资金按 `initial_cash=5,000,000` 建模，`require_account_snapshot=false`，没有收到实际账户资金快照。`check_live_limits` 只核对一个持仓和配置计算的日损失阈值，不检查研究来源、真实资金或真实逐股交易状态。实盘运行仍用 `PAPER_SCHEDULE_ASSERTION` 合成连续交易状态。

这不是本次零成交的直接原因，却意味着修复行情后，程序可能按演示模型向实盘发单。生产入口应验证正式配置和可追溯校准、使用真实资金对账屏障与可靠交易状态；命令帮助、文档和运行模式标识也应一致。3,000 JPY 是配置上的损失触发阈值，不保证实际亏损被封顶。

### 3. P1：含成交的对账报文必然缺字段，已离线复现

代码位置：`ibkr_microalpha/paper.py:211`，`ibkr_microalpha/replay.py:398`。

适配器 `execDetails` 构造的查询成交行只有 `exec_id/order_id/qty/price/side/symbol`，没有 `at`。Replay 解析每一笔对账成交时却直接读取 `execution['at']`。用真实适配器的回调方法构造一笔查询成交，再交给 Replay，稳定得到 **`KeyError('at')`**；复现没有建立任何券商连接。

启动对账或运行中查询一旦返回成交，就会抛异常退出；当前启动快照为空，所以尚未触发。应规范化券商成交时间、保留原始来源和执行身份，并覆盖“已有成交后对账”的完整适配器测试。成交更正等扩展路径仍需单独审查。

### 4. P1：异常退出调用全局撤单，范围超过本策略

代码位置：`ibkr_microalpha/paper.py:450`。

`flat` 为 false 时，finally 调用 `reqGlobalCancel()`。这不是按本策略的 order ID 或股票列表撤单；[IBKR 官方撤单说明](https://www.interactivebrokers.com/docs/tws-api/doc/orders/cancel-order)确认其范围包括其他来源和 TWS 手工挂单。第 3 项异常会进入此路径，已有持仓也不会因为撤单而自动平仓。

修复应明确限定订单所有权、对本策略未终态订单逐笔撤单，并在保留账本证据的情况下处理残余持仓；全局撤单应有独立且明确的操作授权。

### 5. P2：订阅与交易名单不一致，6723 的行情被过滤

代码位置：`examples/capture-jp7.json`、`examples/jp7-live-small.json:60`、`ibkr_microalpha/paper.py:105`、`:426`。

采集配置包含 7 只股票加基准；当前交易配置只有 6 只股票，缺少 6723。`PaperSession.market` 会在规范化后丢掉不在交易名单/基准集合中的事件。

采集前缀中 6723 有 **10,302 条报价**，约占全部 **11,441 条报价的 90%**，但策略输入中该股为 0 条。若用户预期交易全部 7 只，需要统一名单并补齐该股历史曲线、校准和风险配置；仅将其加入名单不能解决第 1 项覆盖问题，也不代表应该自动启用该股。

### 6. P2：诊断信息被缓冲与泛化，现场看不见阻断细节

代码位置：`ibkr_microalpha/paper.py:429`、`:443`、`:456`，`ibkr_microalpha/entry.py:145`。

`audit.jsonl` 在检查时大小为 0，但离线重放实际有 7 条审计记录。普通文件写入没有周期 flush，少量记录停在缓冲区；`NO_TRADE` 又只记泛化的 `entries_blocked`，同一原因不重复写入。没有周期输出真实 soft_blocks、缺失特征、窗口跨度、候选与订单漏斗，控制台长期只显示启动行。

应在启动冻结配置与源码哈希，定期 flush 审计并输出可读运行状态；异常结束也保存失败证据。否则零字节审计文件不能用来判定引擎是否处理过行情。

## 验证与产物

- 现有测试：`python -m unittest discover -q`，**331 项通过**；出现一个测试文件未关闭的 ResourceWarning。首次沙箱运行因临时目录访问受限失败，获准后正常环境运行通过。
- `git diff --check` 通过。
- `diagnose.py`：读取既有日志、统计接入过滤原因、对策略输入做本地重放、复现含成交对账字段缺失。脚本没有调用 `connect/place/cancel`。
- `evidence.json`：完整输入摘要、各股窗口跨度、重置原因、特征/市场快照原因、漏斗、配置/源码/输入哈希及对账异常复现结果。
- `runs/no-fill-review-20261030/raw-input.snapshot.jsonl` 与 `config.snapshot.json`：本次诊断的冻结输入与配置，位于 Git 忽略的 runs 目录。

优先顺序：先修复接入与窗口覆盖并用冻结日志验证，再修复实盘资金/模型校验、对账和撤单范围，最后补齐名单校验和持续诊断。当前证据不足以验证实际订单能否正确成交；本次根本未走到该阶段。

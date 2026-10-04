# 执行、风险与协调器审查证据（2026-10-04）

范围：`execution.py`、`engine.py`、`risk.py`，并检查请求消费入口 `replay.py`、当前 `runs/demo` 日志与对应测试。没有修改生产文件或已有文档。以下 P0 采用“接实盘前必须修复”的报告口径，并不代表现有实盘事故；项目当前仅允许离线回放。

## 可复现缺陷

### E1 / P0：已发送卖单在买入成交向下更正后可超过真实持仓

- 位置：`execution.py:519` `_rebuild`；尤其 `576–582` 重建后仅查超额成交，没有查活跃卖单总剩余量。`engine.py:352–359` `_request_exit` 可卖数量为零时，仅在退出 TTL 已满且卖限价高于当前 Bid 时才撤单。发送前检查 `execution.py:633–648` 只保护未发送卖单。
- 原因：成交更正的风险验证覆盖未发出的请求，缺少覆盖已传输订单的持仓不变量。
- 复现：买100→卖100已提交且 WORKING→买成交更正为50。结果持仓50、卖单剩余100、无锁、无撤单、全局 `book.reconciled=True`。经过退出 TTL 后限价2999低于 Bid3000，仍不撤。卖100继续成交后形成空头50。
- 影响：真实长仓策略可能发送/保留会建立空头的卖单。必须保留账本更正、可能剩余量、撤单确认与对账；不能把撤单请求当已撤销。
- 修改：重建/更正后立即按股票汇总所有活跃卖单可能剩余量；超过确认持仓则立即撤销已知活跃卖单，锁定新增请求、查询并等待完整对账。未知状态只查询并保留风险。不要等待 TTL 或价格条件。回调风险检查应独立于普通退出定价。
- 建议示例（在更正处理后，`at` 是本次接收时刻）：

```python
for symbol, position in self.positions.items():
    sells = [o for o in self.active_orders(symbol) if o.side == Side.SELL]
    if sum(o.possible_remaining for o in sells) > max(0, position.quantity):
        for order in sells:
            self.cancel(order.order_id, at)  # 仍需确认，不释放可能剩余量
        self._lock("ledger: active sells exceed confirmed holding", at)
        self._query(at)
```

- 验收：更正减少至50或0、有两个活跃卖子单、取消回报晚到、撤单期间再成交；均及时产生取消/查询，且不会因为提交了取消而错误释放风险额度。

### E2 / P0：请求消费先发送买入，随后才检查当前风险

- 位置：`replay.py:152–156` `requests` 分支先调用 `book.drain_commands(at)`；`171` 才调用统一尾部 `e.poll(at)`。`execution.py:628–655` 只检查候选 TTL、账本锁和卖单数量，不能看到市场、报价、预测及 PortfolioRisk 锁。
- 复现：12秒时生成尚未发送的买单；15秒 `requests` 到来。报价年龄3秒超过配置2秒，但 `last_commands` 返回 SUBMIT；接着 `poll` 才把市场置为 RISK_OFF 并使买单进入 CANCEL_PENDING。
- 影响：过期行情、失效经济预测或刚到达的退出时限可以在风险检查前触发交易。撤单存在竞态，不能恢复先发错误的买入。
- 修改：统一请求消费入口必须先推进时钟、处理账户/市场/时间风险，并对每个待发送买单在当前接收时刻重新验证报价、冻结预测、经济下界、候选及账户状态，再消费队列。仅把 `poll` 提前是必要但不充分，预测失效也要重验。
- 建议示例：

```python
elif kind == "requests":
    e.poll(at)
    e.validate_pending_entries(at)  # 当前报价/预测/价格上限/确认/风险重验
    self.last_commands = e.book.drain_commands(at)
    # 尾部统一 poll 排除 requests，避免相同事件重复完整扫描
```

- 验收：报价/市场过期、预测过期、报价越价格上限、计划退出边界、日损失锁、当前时刻候选到期；SUBMIT 必须不出队。风险卖出/撤单仍享有预留容量。

### E3 / P1：退出限价非正抛异常，阻断整个风险循环

- 位置：`engine.py:360–361` `_request_exit` 在 `try` (`363`) 外执行 `round_price(bid-exit_slippage)`。
- 复现：有效 Bid0.5 / Ask0.6，配置滑点1，退出请求抛 `ValueError: price must be positive`，没有 EXIT_BLOCKED 记录或卖单。
- 影响：风险 `poll` 中某只股票的退出失败可阻断之后其他股票的检查；即使只是极端坏报价，也不应使协调器崩溃。
- 修改：先按类别和日期获取合法价格下界，夹住风险卖限价；将价格生成和提交放入同一捕获范围。若规则确实不支持该价格，写 EXIT_BLOCKED+报警并继续其他股票，绝不能虚构已平仓。
- 建议示例（合法下界应由 TickTable 提供，不是硬编码）：

```python
try:
    floor = self.ticks.minimum_price(at, instrument.tick_category)
    raw = max(floor, quote.bid - self.config.exit_slippage)
    limit = self.ticks.round_price(raw, "down", at, instrument.tick_category)
    order = self.book.submit(intent_id, symbol, Side.SELL, available,
                             limit, at, emergency=emergency)
except ValueError as error:
    self._record(at, "EXIT_BLOCKED", symbol=symbol, reason=str(error))
    self.risk.lock("exit_price_rule_invalid")
    return
```

### E4 / P1：首成交评分直接修改订单，执行事件流无法复原最新状态

- 位置：`engine.py:211–226` `on_fill` 重算并直接写 `order.entry_score`、`score_version`；`execution.py:475` FILL 事件没有这些字段；`from_journal` 仅从 SUBMIT 恢复原评分。引擎直接向上对齐 `position.stop_price` (`234`) 同样没有执行事件，是另一个元数据审计遗漏。
- 当前日志直接证据：`runs/demo/execution.json:27` 当前买单评分2.8675213682449385，但 `runs/demo/execution-journal.jsonl:3` SUBMIT评分2.8446550849077727。
- 离线持仓复现：原提交评分2.7593889113719143，首成交评分2.8348981518953194；`from_snapshot` 恢复2.8348981518953194，`from_journal` 恢复2.7593889113719143。
- 影响：恢复后的 AlphaDecay 起点、风险审计、研究复现不一致。完整 raw-input 重放可以另行重现，但不能声称 execution journal 自身能还原全部执行风险元数据。
- 修改：禁止协调器直接修改账本元数据。新增 `book.set_entry_snapshot(order_id, score, version, source_at, received_at)` 及 `book.tighten_stop(...)` 原子校验并记录 `ENTRY_SNAPSHOT` / `STOP_TIGHTENED`，在 from_journal 重放；评分缺失/失效同样记录。保留事件溯源机制。
- 示例：

```python
self.book.set_entry_snapshot(order_id, score, snapshot.version,
                             source_at=snapshot.at, received_at=at)
# 方法内部赋值并 _record("ENTRY_SNAPSHOT", received_at, ...)
# from_journal 识别并重放 ENTRY_SNAPSHOT；缺失评分记录 None。
```

## 风险设计边界与性能/冗余建议

### R1 / P1（实盘接入缺口）：资金“已核实”实为配置与局部账本推算

- 位置：`engine.py:309–310` `_sync_risk`；`execution.py:693` `reconcile` 输入没有现金/购买力/货币/账户余额，`risk.py:73–89` 直接用传入 consistent 作为 account_verified。
- 当前运行是离线冻结账户日，README 已声明演示资本、权限、费率都不是真实账户，因此不是现有回放算账错误。接实盘时实际 JPY余额、非策略资金变动/费用、账户外持仓预算无法由这些输入验证，配置初始现金不能替代 broker evidence。
- 修改：新增版本化 AccountSnapshot（account_id、cash_by_currency、available_funds/buying_power、实际账户范围、source、received_at、snapshot_barrier）；以真实受约束 JPY购买力与策略分配现金较小值为预算，未核实货币或余额则锁定。保留当前 initial_cash 作为离线日初假设，字段命名和报告区分 modeled/reconciled。

### R2 / P2：没有组合压力损失预算，只有组合名义金额

- 位置：`risk.py:148–155` 汇总组合/行业 notional；`160–163` 限制名义金额；`175` 只比较单笔 stress。
- 默认 max_positions3、trade_risk0.1%，单笔风险上限粗加0.3%资本，低于日损失0.5%；当前默认并非明显超预算。但修改参数后没有校验各持仓/在途订单 stress 总和；相关跳价、流动性耗尽和撤单竞态也不是固定滑点1能保证。
- 修改：新增 portfolio_stress_fraction / sector_stress_fraction，保留每个持仓和在途买单的stop、数量、未报手续费，预分配时总计压力损失。日损失锁是事后停止开仓，不能替代事前风险总额。参数与风险阈值应通过历史/回放坏路径验证，不凭直觉放大。

### R3 / P2：max_orders_per_day 实际只统计入场意图

- 位置：`risk.py:63,114–116,135`、`engine.py:712`。`note_order()` 仅在 BUY ENTRY_REQUEST 后调用；SELL、撤单和普通退出重发没有计数。
- 日志示例1入场意图、2个子单，report也明确child_orders2；这不能作为全部 API消息/交易所 OER 控制。
- 修改：当前字段重命名为 max_entry_intents_per_day；单独跟踪 submit/cancel/modify/query、已成交/已撤销等计数和频率。普通请求有独立预算，风险撤单/退出继续放行及报警；不要用总订单数硬限制救险退出。保留 TokenBucket 风险预留容量。

### P1 / P2：费用查询每个行情事件扫描全日历史，呈 O(orders × executions)

- 位置：`engine.py:251–258` `_fee_reserves` 遍历每个有成交订单，并调用 `execution.py:228–235` `order_fees`，后者每次扫全部有效执行；每次 `_sync_risk` 都触发。`_rebuild` (`519–582`) 另在每个成交/费用回报排序全部执行，首仓初始化又扫描执行历史找更正价格。
- 离线本机12次中位数（只有 `_fee_reserves`）：2订单/2成交0.0051ms；20/20 0.05855ms；200/200 2.51545ms；2000/2000 230.335ms。最后一组超过默认100入场的日限制，是增长趋势压力证据，不能宣称实盘延迟。默认100意图对应200子单即可产生约2.5ms的重复历史扫描，每个行情/计时回调都执行。
- 修改：有效费用按 order_id 缓存，在 fill/correction/commission 时更新对应 child order；维护未报费用订单集合及总预留，只在受影响订单变化时重算。保留慢速 `_rebuild` 为更正、晚到、恢复及校验的参照，普通按时成交/费用使用增量路径。不要删除更正谱系、费用迟到处理或一致性验证。
- 证据：`benchmark_fee_scan.py`、`fee_scan_benchmark.json`；当前demo只有2子单/2成交，未暴露增长瓶颈。

### M1 / P2：经济门控重复实现，出口、审计和计算可合并

- 位置：`engine.py:490–501` `_economic_gate` 与 `evaluate` 的 forecast检查（`659`起）、adjust_prediction和prediction_gate（`682–683`）重复。一个用于活动买单，一个用于新入场，容易只修一条流程。
- 修改：抽取一个返回预测有效性、调整后Prediction、EconomicGate、合法价格上限的 EntryEconomics；新入场和发送前复核复用；数量变化仍必须与独立校准数量匹配，不能直接线性缩放置信界。
- `evaluate(snapshot, at, enhanced_ready=False)` 在 `568` 又覆盖 enhanced_ready 入参；删除无效公开参数，READY只由版本化订阅证据计算。
- 按事件构建 QuoteValidation 结果和 active_orders_by_symbol，复用本次时刻结果，不跨事件缓存行情有效性。`poll` 对每股多次 `active_orders`，每次全历史扫描；加入活动集合/按股票索引可降低 O(symbols × full-day-orders)。
- 低价值 NO_TRADE/EXIT_BLOCKED 重复记录可在理由变化时记录一次，并定期汇总计数。保持风险状态变化、订单、成交、时间退出、断连和对账事件逐次记录。demo NO_TRADE warmup1200、cooldown260，审计重复明显。

## 验证与保留项

- 当前对应59项测试全过：execution34、engine17、risk8；这些新边界未覆盖，测试全过不能否定缺陷。
- 保留：Decimal金额、回报只作对账证据而不重复加仓、exec_id幂等、更正谱系、撤单确认、未知状态不重发、完整归属快照、共享预算、首成交即设止损/原始计时、固定TTL、风险请求容量、午休/收盘退出与残余报警。
- 报告不能从demo8秒入场到平仓、同刻模拟回报推断真实 submit/cancel P99、CPU/内存或实盘稳定性；日志缺少真实链路指标，broker_readiness明确NOT_VERIFIED。

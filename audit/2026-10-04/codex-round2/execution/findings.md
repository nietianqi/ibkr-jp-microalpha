# 第二轮执行与风控复核（2026-10-04）

基线：Git `e592e34`（Initial import，包含 Claude 第一轮修复），当前接口与当前测试；没有改生产代码/已有文档。新证据只在本目录。`reproduce.py` 成功运行，并输出 `evidence.json`。执行、风控、协调器及 `ExecutionRiskFixes` 共 **73 项测试通过**。这不是实盘事故报告。

## 需要修改

### R2-E01 / P1：同一笔 broker 可用资金在快照有效期内被重复分配

- 位置：`valuation.py:106–114` `RiskValuation.sync`；`engine.py:282–287` `set_account`。
- 当前新增 AccountSnapshot 的方向正确，但代码只用 `min(initial_cash + ledger_cash_flow - reserves, snapshot.available_funds)`。没有记录账户快照覆盖的账本序列/余额基线，也没有从 broker 一侧余额扣除快照之后已知的买入与资金占用；将这个未更新的值标成 `cash_source='reconciled'`。
- 当前接口复现：broker JPY可用资金350,000；该快照之后买入300,100，未报费用预留240.08；已知余量49,659.92。估值仍给 `risk.cash=350,000`，对第二股票允许再分配300,340.08。
- 影响：单笔静态资金上限测试通过，但多股票资金可反复使用；账户快照60秒有效期不是资金冻结承诺。
- 修法：账户快照必须与订单/执行对账 barrier 对齐，保存其覆盖序列、现金流基线和当时券商已预留的订单集合。仅对该 barrier 之后尚未被 broker 资金值覆盖的新成交、费用及本地未发送买单作额外占用，并与 modeled budget 取较小值。无法证明覆盖边界时保持 `account_snapshot_unreconciled` 阻断，不标 reconciled。不要将全部活动订单从已净预留的 broker 值再次扣掉，也不要零扣。
- 示例方向（需要配套 barrier，不能仅按 receive_time 猜覆盖）：

```python
anchor = e.account_funds_anchor
post_barrier_cash_flow = e.book.cash_flow - anchor.covered_cash_flow
new_commitments = commitments_not_covered_by(anchor, e.book)
funds = anchor.available_funds + post_barrier_cash_flow - new_commitments
cash = min(modeled_cash, max(ZERO, funds))
```

- 验收：350k快照后第一买单100股，再到第二股票，第二单必须被预算拒绝；测试已发送/未发送/部分成交、晚到费用、撤单释放以及新快照覆盖旧订单，确保不重复扣款、不反复使用资金。账户不同或 barrier 未确认时阻断。

### R2-E02 / P1：排队买单在账户资金下调后仍能发送

- 位置：`entry.py:319–348` `EntryPipeline.still_valid`；尤其 `322–325` 只看锁，最后 `348` 只复核经济预测。`execution.py:715` 起发送前 validator 调用入口有了，但没有预算复核。
- 复现：行情/预测/候选均有效时生成未发送买单100股，限价3002，名义金额300,200。随后同一账户 broker snapshot 给可用资金1,000，估值已确认 `risk.cash=1000`；发送前 validator 返回 None；12.5秒 requests 仍返回 SUBMIT。
- 影响：EXE-02 已解决行情/预测过期先发后撤，但账户资金、组合/行业压力预算在排队期间改变仍未覆盖。
- 修法：validator 内增加 `validate_existing_entry_budget(order, at)`，按当前确定资金与暴露重新核验该单的现金、单股/组合/行业名义金额、组合压力；将自己的已有预留与其余预留分别计算，防止把自己重复算一次。只检查并保留原数量/限价；若原数量不能通过则本地 LOCAL_ABORT，不静默缩量而破坏冻结校准。一个 drain 中多单依次核验后占用预算，不允许每单同时用同一可用余额。
- 验收：1k新资金快照后未发出的300.2k单不出队；组合压力因其他持仓变化超限、其他已发单部分成交、多个待发送单并行占用、账户快照失效；风险卖出/撤单仍可发送。与 E01 是不同边界，二者必须独立测试。

### R2-E03 / P2：执行快照及执行日志恢复丢失日请求预算与统计

- 位置：`execution.py:926–942` snapshot 没有 daily_request_budget、routine_requests_sent、sent_counts；`945–951` from_snapshot 构造没有传预算；`1002–1004` from_journal 用默认 cls()；`1033–1040` 重放 COMMAND_SENT 只恢复时间，不恢复计数，QUERY（order_id=None）也被跳过。原记录 `776` 还缺 risk 属性。
- 复现：原 book budget1，已发送普通SUBMIT1，counter1。两种恢复后 budget=None、counter0；重连并完成对账后普通新SUBMIT又成功发出。已消耗的日预算和报告消息总数失效。
- 修法：版本化快照保存预算、JST交易日、例行计数、所有分类计数及限速剩余状态；旧快照要从冻结配置显式提供预算并从已记录发送事件重算，无法重算则阻断例行请求。journal 增加 CONFIG/REQUEST_SENT risk 与交易日字段，对所有 COMMAND_SENT（含QUERY）更新统计；恢复统计只做重建，不再次消费限速或重发。保持恢复后的完整对账要求。
- 验收：budget1耗尽后两种恢复+完整对账仍不能发普通买单；风险撤单/卖出仍放行；SUBMIT/CANCEL/QUERY统计与恢复前一致；从旧版本迁移、同日重启和跨日新运行分开测试。

### R2-E04 / P1：更正撤单竞态形成负仓位后，估值异常中断风险循环

- 位置：`valuation.py:68–89` sync 把任何非零 quantity 当长仓，`83` 调 exit_fee_reserve；`43` 将 Bid×负数量传给佣金计算。无报价时 `80` 同样会传负金额。
- 复现：买100→发送卖100→买更正为50；新 EXE-01 正确将卖单置 CANCEL_PENDING 并查询/锁定。撤单确认前晚到真实卖成交100，账本正确记录 quantity=-50 及两个 ledger 锁；`engine.on_fill` 随后抛 `filled_notional must be nonnegative`，无法完成本次 poll。
- 原因/影响：撤单请求不能保证再无成交。账本已经接受真实更正/晚到事实，估值与协调器仍假设绝无负仓；在最需要隔离和报警的时候中断处理。经 Replay 时异常会使 runner failed，不是对多个持仓持续风险处理的替代。
- 修法：sync 在 quantity<0 时进入明确的异常仓位分支：保留真实数量与原执行、HARD锁、记录一次 UNCONTROLLED_SHORT 报警/查询、估值不明设 None、标账户不一致；不要调用长仓退出费用或 stop 计算，不要将负数替换为0。继续处理其他股票。处理需由完整 broker 对账与明确获准的异常仓位处置流程完成，不能把 abs(quantity) 输入现有长仓 SELL 退出。
- 验收：撤单期间全部/部分再成交、买入 bust 到0后卖出回报、已有负仓位恢复、负仓位且没有报价；执行事实都保留，风险poll不因佣金参数抛异常，不生成加深空头的卖单，其他股票继续完成风险检查。

## 旧问题的复核结果

- **EXE-01 真正修复**：`fill:540–541` 更正后 `_enforce_sell_cover:641`，已传输卖单立即取消+ledger锁+QUERY；未发送卖单本地取消；可能剩余量保留到确认。这不能杜绝撤单竞态，因此仍需 E04 异常事实处理。
- **EXE-02 行情/预测分支修复**：`replay.py:238–249` requests 先 flush/poll，再注入 entry_validator；过期行情和失效预测不发 SUBMIT；经济验证复用 EntryPipeline。资金/压力尚需 E02。
- **EXE-03 异常隔离修复**：`positions.py:74–80` 价格转换异常被记录并锁定，不再漏出风险循环。最低合法报价仍因下移1tick到0而无卖单，这是保留限制；可在后续将可执行限价夹到最低合法价，不能声称全部最低价退出路径完成。
- **EXE-04 真正修复**：ENTRY_SNAPSHOT和STOP_TIGHTENED写入账本事件并重放；新回归验证首成交评分、止损在journal/snapshot一致。
- **费用重复扫描主要修复**：order_fees 已改 `_order_fees` O(1)。fee_reserves 仍遍历订单；_rebuild仍每次重建/排序，属于剩余性能范围，不能继续沿用上轮 O(orders×executions) 的费用查询结论。
- **入场计数/组合压力方向修复**：max_entry_intents_per_day语义明确、portfolio_stress_fraction与gap reserve已参与原子预分配；routine_request_budget新增且风险请求绕过。预算恢复和发送前动态复核仍需补。
- **SOFT/HARD 取舍合理部分**：市场未知/估值缺口阻断新入场，恢复后自动解除；仅持有受控风险时升级，空仓不会被暖机锁死；特征失效重置信号退出状态，保留止损/最长持仓/计划退出。没有把短期不更新直接当紧急清仓。没有发现必须推翻这一划分的具体复现。

相关现有回归已在本次探针独立执行7项并PASS，另外73项所辖测试PASS。不得用旧接口探针失败计为新缺陷；本目录脚本使用当前fixture与当前API。

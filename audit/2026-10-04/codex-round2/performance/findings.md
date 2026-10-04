# Codex 第2轮：回放、报告与性能复核

日期：2026-10-04 JST。使用当前最新API；未修改生产代码、协作文档或已有runs。所有新增文件均在本目录。

## 已复现的剩余/新问题

### R2-P1-CLI：输入和输出文件相同时，回放先截断输入却报告成功

- **位置**：`ibkr_microalpha/cli.py:10–21 _replay_file`；第17–18行先以`w`打开输出raw-input/audit，再第21行读取events。
- **证据**：`source-collision.json`。一条合法timer、109 bytes，作为同目录的`raw-input.jsonl`传给`_replay_file(CONFIG, source, source.parent)`。运行后源大小0、report事件数0、report存在、failure不存在。
- **原因/影响**：输出流打开先于输入读取，缺少路径冲突检查。用户在原run目录重放冻结raw输入时，可以丢失该输入文件并得到错误的空运行成功结果。该问题不依赖旧API。
- **修改**：启动前解析绝对路径，拒绝源文件与任意写入/删除产物的路径冲突（尤其raw-input和audit）。更稳妥地在新的临时run目录输出；所有流关闭并验证后原子发布成功产物。构建配置也应在删除旧report/failure之前完成，避免失败新运行破坏旧证据。
- **验收**：同路径、相对路径别名、符号链接/硬链接等实际同文件情况；正常不同目录；配置错误。冲突时原文件内容/哈希不变，没有新的成功report。新run失败时保持旧run可识别且完整。

### R2-P1-PERSIST：持久化失败不poison实例，重试跳过已丢失的证据写入

- **位置**：`ibkr_microalpha/replay.py:298–307 dispatch`的异常边界仅覆盖engine应用；第309–311行提交身份/时间/计数；第313–317行raw/audit写入在异常边界之外。`finish:330–336`的audit drain也在其try之外。
- **证据**：`persistence-failure.json`。raw_sink.write注入磁盘满OSError：engine已推进、events_processed=1、identity已提交，failed仍False；重试同一event静默return；更换sink后下一event仍被接受，而第一条没有写入raw证据流。
- **原因/影响**：应用成功和证据成功没有统一运行失败边界。API可在证据不完整时继续推进；raw/archive或audit流存在空缺，重试无法补写。**原始输入文件仍可能完整，可用于从头重建；不等同本问题删除原输入文件。**当前CLI一般会捕获run异常并写failure，但runner_poisoned会错误显示False；API行为尤其不安全。
- **修改**：把输入归档、应用、audit归档及提交结果整体纳入失败保护，任意sink/flush失败设置FAILED并禁止继续同实例；明确RECEIVED/APPLIED/DURABLY_RECORDED计数或状态。避免通过撤销seen identity后原地重试，因为engine已经修改。失败现场保留源位置和错误阶段，用原始完整输入重建。成功report应在所有输出流flush/close成功之后发布。
- **验收**：raw写、audit部分写、flush/close各阶段注入异常；失败实例的后续dispatch必须抛ReplayFailed；partial archive与manifest有明确已应用/已持久化边界；原输入重建可重复，不能发送旧请求。

### R2-P1-INTENT：平仓后的卖出bust恢复持仓，新退出丢失原意图归属

- **位置**：`ibkr_microalpha/reporting.py:65–67 IntentLedger.close`移除`_current[symbol]`；`50–57 link_exit`无当前intent时创建unattributed；`76–102 _row`按各intent子单计算残余。恢复暴露时`engine.py:346–376 on_fill`没有重开/重新关联原intent；`positions.py:89`仅按symbol链接退出。
- **证据**：`reopened-intent.json`。现有真实fixture买100→卖100→CLOSED；该SELL被明确bust为0→完整broker reconcile确认恢复100股→风险退出另卖100。最终实际position=0、daily_net=-580.08；原intent却OPEN/bought100/sold0（给出保守残余估值），另一个unattributed intent OPEN/bought0/sold100、net=None且报告quantity mismatch。两个OPEN诊断与真实已平账户冲突。
- **原因/影响**：账本有效修订正确恢复暴露，但intent生命周期仅由当前symbol指针维护，更正前关闭后丢失关联。原完整意图收益、平仓率、盈利意图数和压力预算诊断不再可信；这不是账户PnL重复计算，账本净额仍为-580.08。
- **修改**：永久维护order_id/execution_root→intent_id归属；更正/bust改变历史暴露时重新计算原intent状态，必要时重开其恢复流程。新恢复退出必须显式关联原意图，不靠“当前symbol或unattributed”的猜测。若同时有多个历史/当前intent恢复暴露，隔离新开仓并定义退出量归属，不盲目覆盖symbol指针。
- **验收**：CLOSED后SELL bust、partial correction、BUY correction恢复暴露、多次更正、恢复退出、更正晚于同股新intent。账本平仓后相关意图正确CLOSED；已归属的子单不成为unattributed；净金额/全部子单费用汇总与有效账本一致，无虚构残余估值。

## 已修复/已改善的旧问题

专项运行 `python -m unittest tests.test_replay tests.test_reporting tests.test_review_fixes -q`：66项通过，0.277秒。不是重新声称全部195项；根代理负责完整套件结果。

- FeatureSnapshot.valid='false'：现在输入边界拒绝。
- features.rvol_days=20.5：现在配置构建拒绝。
- calendar解析失败：重复两次均抛错，不占seen_digests、不poison、events=0；业务应用失败的poison已有当前回归覆盖。
- 当前有效成交更正损耗：原夹具BUY100@3001改为3002，只剩当前revision，成本150.0；bust后成本0且drifts清空。旧“原值+修订值”重复累计已解决。完整bust后恢复退出的**intent归属**属于上面的不同缺口。
- 到期之前报价不再消费漂移样本；当前RPT01回归通过。
- 完成drift移出pending、费用按order聚合、同时间行情批次共享snapshot、基准索引/同时间曲线，明显降低离线开销。旧审计脚本API变更不是缺陷。
- 最新人工demo：3797事件；生成结果与新引擎回放business报告相同、input manifest hash相同；含构建/运行/保存总0.885秒，报告processing=0.8169秒。净亏1190 JPY，position0、压力预算突破有记录。见demo-verification.json。不能与旧6786事件的14秒严格逐项对比，输入和策略行为已改变，也不能作为实盘性能验证。

## 部分改善，仍存在的容量限制（P2，不另列核心P1）

- CLI流式raw/audit使完整payload不再驻留；seen_digests仍全日线性增长，且新`Replay._durations`（replay.py:86/319）保留**每条**处理耗时，metrics又全量排序。
- NullSink、audit=0、raw_lines=0的独立50k timer探针：10k/25k/50k当前traced对象为2.141/5.843/11.680 MB；digest和duration样本各与事件数相同。证明此路径仍不是固定内存。见memory-retention.json；含tracemalloc开销，不能当整日RSS预测。
- 整改方向：去重身份使用持久化索引/源序号水位；p50/p99用有界直方图或明确的有界采样，保留count/max/mean等聚合。不要简单删除去重或将晚到重复事件放行。
- metrics用perf_counter墙钟累计dispatch，包含stream.write开销，未计finish最终批次、文件读取和save；当前文案“Local CPU timing”应改为本地dispatch墙钟耗时并明确excluded部分。绝非broker/exchange latency。

## 复现

从仓库根目录：

```powershell
python audit/2026-10-04/codex-round2/performance/reproduce.py all
python -m unittest tests.test_replay tests.test_reporting tests.test_review_fixes -q
```

`all`仅在本目录写入自己的人工源、输出和JSON证据；不会覆盖原runs。可以用同脚本的source_clobber、persistence_failure、reopened_intent、old_fixes、memory_retention、demo_verification逐项运行。

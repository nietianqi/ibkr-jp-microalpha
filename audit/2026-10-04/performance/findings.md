# 离线运行证据、性能与报告专项审查（2026-10-04 JST）

本文件为协作报告的专项证据，未修改生产代码或既有 `runs`。所有探针均离线运行，没有外部连接。位置以当前代码的行号为准。

## 实际日志能证明什么

- `runs/demo` 与 `runs/replayed` 的 raw-input、配置、report、audit、execution journal、execution snapshot 六类文件 SHA-256 分别完全相同。因此它们是同一人工轨迹的重复回放，不能算两个独立实验。
- 6,786 个事件：3,020 volume_baseline、751 market_snapshot、1,502 quote、751 trade、751 requests、4 status、2 fill、2 commission、1 calendar、1 reconcile、1 forecast。
- 逻辑时间 `2026-10-02 08:55:00–09:12:30 JST`，跨度 1,050 秒；行情由 09:00:00 开始，751 秒时间点。该时间跨度不能解释成处理耗时。
- 09:10:10 产生研究候选，09:10:12 发出买单并模拟成交 100 股 @961.3；09:10:20 跳价止损，卖出 100 股 @950；已实现价格亏损 1,130、费用 160、净亏损 1,290 JPY。无残余仓位、无活动订单、无报警或风险锁。
- audit 1,490 条，其中 1,484 NO_TRADE；journal 14 条。SESSION_WARMUP=1,200（stock quote 和 trade 各一次），cooldown=260，是回调次数，不是 1,200 个独立异常或 260 秒。
- fill_callbacks=1 是 `engine.py:236–237` 有意只数新 BUY 回报，execution_quality 中买卖两条 fill；应改名 entry_fill_callbacks 以降低误读，不属于漏记 SELL 的交易缺陷。
- 所有 quote/trade 均没有 exchange_at；沒有本地单调时钟、真实提交/确认耗时、队列年龄、资源采样或真实断线重连。无报警仅说明该人工闭环无报警，不能据此认定实盘稳定。

## 本机基准与主要瓶颈

环境：Windows 11、CPython 3.14.0。生产逻辑、标准库，未连接券商。

| 检查 | 实测结果 | 限制 |
| --- | --- | --- |
| 原 JSONL 读盘回放 | 14.370 秒，6,786 事件 | 包括构建引擎与 run，不包括 save |
| 预读 JSON、逐事件 dispatch，两次 | 13.939 / 14.334 秒；486.8 / 473.4 events/s | 单股票加基准、低频人工流 |
| trade handler 本机 p99 | 12.787 / 13.801 ms | 包括解析后的 dispatch；绝非交易所、券商或实盘链路 p99 |
| 两次 working set / peak | 45.19 / 45.58 MiB | 包括预读全部 JSON 事件；不是策略纯内存或全天内存 |
| cProfile | 27,862,022 调用；20.721 秒 profiler 自耗合计 | profiling 改变执行耗时，不与裸计时混用 |
| snapshot | 1,502 次，累计 19.531 秒 | 累计时间含子调用，不能与子项相加 |
| _rvol_baseline | 7,060 次，累计 13.273 秒（64.1%），自身 10.939 秒 | 实际主要瓶颈 |
| datetime.date | 21,452,752 次，2.334 秒 | `_rvol_baseline` 内每个候选反复 `local.date()` |
| _return | 25,534 次，累计 2.469 秒 | 下一阶段优化项 |
| poll / deepcopy | 6,788 次 / 0.385 秒；deepcopy 累计 0.180 秒 | 当前不是主要 CPU 瓶颈，不应为了微小收益删除风险轮询或破坏输入隔离 |
| 仅增加基准索引的审计探针 | 5.028 秒，1,349.7 events/s，report 与现有日志完全一致 | 单次可行性探针；约 2.86 倍改善，尚未完成正式优化验收 |

完整数值：`benchmark.json`、`profile.json`、`profile.txt`、`indexed-probe.json`。可复现脚本：`python audit/2026-10-04/performance/benchmark.py benchmark` / `profile` / `indexed_probe`。

### PERF-01（P1，扩容前）：全表查找成交量历史基准

**位置**：`features.py:344–347`、`404–418`、`465–481`。每次 snapshot 针对 5/10/30/60/120 秒窗口逐个扫描 `_baselines`；本 fixture 仅 30 秒有基准，另外四次全表过滤最终无匹配。循环条件对匹配股票反复调用 `local.date()`，仅此生成 2,145 万次调用。

**影响**：14 秒原回放中 CPU 主要浪费在历史基准查找。随着股票、历史日期、时间槽增加，瓶颈线性扩大；不能从本 fixture 的完成速度推断能够处理多股票逐笔流。

**处置**：重构并保留全部因果条件；删除重复全表扫描。添加 `(symbol, source, window_seconds, end_second)` 索引，在小桶内筛选 day、known_at，依 known_at 选当天最新修订。相同方法适用于 volatility baseline。局部缓存 `day=local.date()`。不要缓存忽略时间的 hindsight 基准，也不要删除未知值和样本数门控。

```python
# add_volume_baseline 中建立索引；保留原始历史便于审计。
key = (row.symbol, row.source, row.window_seconds, row.end_second)
self._baseline_index.setdefault(key, []).append(row)

# _rvol_baseline：先键查询，再做原有时间/修订/样本数过滤。
local = at.astimezone(JST)
day = local.date()
second = local.hour * 3600 + local.minute * 60 + local.second
rows = self._baseline_index.get((symbol, self.config.trade_source, window, second), ())
eligible = [r for r in rows if r.day < day and r.known_at <= at]
# 后续 by_day、latest known_at、有效日期数、median 逻辑保持一致。
```

验收：原轨迹 report 完全一致；添加修订已知时刻、无匹配时间槽、多数据源、无效最近日、午间/reset 回归；同硬件同输入比较两种裸计时，不比较 profiler 与裸计时。

### PERF-02（P2）：内存驻留随着输入事件增长

**位置**：`replay.py:34,58,211` 保存所有完整事件；`features.py:_Series.seen` 全日身份缓存；`engine.py:131,149–150` 保存完整 audit；`execution.py:182,200–202` 保存 journal。rolling quote/trade 的 prune 不会删这些日志或事件身份。

**证据**：独立无行情 no-op timer 探针，只增加唯一接收身份且 audit=0：10k 当前 tracemalloc 3.586 MB；50k 18.825 MB；100k 37.648 MB。100k working set 111.18 MiB 包含 tracemalloc 开销，不可线性外推成实盘 RSS。`memory-retention.json` 可复现。

**影响**：输入规模增加时原始数据被 JSON对象与深拷贝对象多份持有，audit/journal也只在 run结束时写盘；中途失败既失去未落盘审计又可能耗尽内存。

**处置**：重构持久化：输入顺序流式归档；身份只保留 event_id+规范化内容摘要（必要时 SQLite 唯一键提供持久化去重），不保留完整 payload；审计通过单独有界消费者批量写 JSONL，队列满时 fail closed；回放保存 manifest+inputhash，不再从完整 seen_events 重写输入。不要直接删除去重、安全审计或改为有损小 deque，晚到重复事件必须保持幂等。

### PERF-03（P2 / 当前不紧急）：重复计算与诊断遍历

**位置**：`features.py:380–395,441–445,449,482–487,511–519`：单次 snapshot 重复 return；每只股票重复基准窗口/breadth；feature_windows 又计算完整 snapshot。`reporting.py:138–140` 每次 poll 扫描所有已完成 drifts 才 continue；`engine.py:251–259` 对所有历史订单计算 fee reserve，其中 order_fees 再扫全部有效 executions。

**处置**：合并事件版本内共同结果；feature_windows 从已有 snapshot 提取；波动量/volume/TI 用可回归的增量滚动统计；仅把未完成 drifts 放入活动队列，完成后转聚合记录；费用变动时更新每 order 费用索引与 reserve。必须让缓存 key 含最新 quote/trade/baseline revision/version，不得仅按 timestamp（同时间 quote与trade会改变特征）。本 fixture 中 poll/费用不是瓶颈，优先 PERF-01。

## 已复现报告与可靠性缺陷

### REPORT-01（P1）：5/30/120 秒漂移先使用 horizon 之前报价并冻结

**位置**：`reporting.py:137–159`，尤其 target 到期后仅检查 quote 是否 valid，没有验证 `quote.at >= target`。

**实际日志证据**：BUY 成交在 09:10:12、参考 mid=961.25；到 09:10:17，首先 market_snapshot 触发 poll，拿到 09:10:16 的 mid=961.65，于是 5秒漂移定格成 4.1603828 bps。随后同 09:10:17 新股报价 mid=961.75 已到，但 horizon 已在 results，无法更新；真正 5秒报价应 5.2002081 bps。

**复现**：独立 `ExecutionQuality(horizons=(5,))`：t0 mid100，t4 mid100，t5先观察旧报价再观察mid101，报告0bps，应约99.5033bps。见 defects.json。

**原因/影响**：指标受不相关事件到达顺序影响，并把前向保持报价错误记为到期观察。这会污染执行质量校准与政策比较。

**处置**：重构并保留诊断。未到 horizon 的 quote 不消费结果；等待首个合法 quote.at>=target 且在允许迟到范围，超窗才 unavailable；记录 observed_quote_at 与 target_at。

```python
target = drift.fill_at + timedelta(seconds=horizon)
if at < target:
    break
if at > target + timedelta(seconds=max_age_seconds):
    drift.results[horizon] = None
    continue
if quote is None or quote.at < target:
    continue  # 下次同时间的新报价仍可测量；此时不能冻结旧报价。
# 使用符合观察时间的 quote.mid，附带 target_at/quote_at。
```

验收：同 timestamp 的 timer/market_snapshot 在股票报价前后，都使用首个 horizon之后有效股报价；无报价超过允许范围时 unavailable，不前向填充。

### REPORT-02（P1）：成交更正与 bust 重复累计执行损耗

**位置**：`engine.py:228–230` 只传 correction布尔；`reporting.py:124–135` 追加旧/新记录；`reporting.py:173` 把所有 price_cost相加。账本 `execution.py:460–479` 对同一 execution root 是替换当前 revision，两模块语义冲突。

**复现**：arrival100，BUY100@101 原成本100，更正为 BUY100@102 有效成本应200，report却300；更正qty0的bust有效成本应0，report仍100。更正布尔不足以标识要替换哪一条。还留存已作废成交的漂移样本。

**影响**：交易账本/PnL正确更正后，损耗指标仍失真，可能让评估误判执行政策或费用参数。

**处置**：合并 canonical成交事实源，保留完整 revision审计但 summary只聚合当前revision。给 ExecutionBook 增加公开 current_executions 接口，或传 correction_of/root_id；同root更新当前row、bust删除有效漂移样本。禁止简单跳过 correction（原qty/price不会被修正）。

```python
# on_fill 增加 correction_of，而不是只有 correction=True。
root = self.execution_roots[correction_of] if correction_of else exec_id
self.execution_roots[exec_id] = root
self.current_fills[root] = row  # 完整 revisions 另存审计。
# summary 汇总 current_fills；bust qty0 不生成有效 drift样本。
```

验收：原成交、更正链、数量修正、bust、重复回报、继承/替换费用各自报告与账本一致。

### REPLAY-01（P2）：失败事件已占用身份，第二次失败事件会被当成功重复跳过

**位置**：`replay.py:49,57–58` 在解析/业务校验前写 day、last_key、seen_events；`51–54` 遇到该身份直接return。`cli.py:30–31` 仅run成功后save，无失败状态产物。

**复现**：calendar.known_at比received_at晚1秒；首次抛错，events_processed=0却seen_events=1；再次dispatch完全相同非法事件静默return，calendar仍0。见 defects.json。后续 corrected同event_id 又被冲突拒绝。

**影响**：失败对象可被误当已处理；某些事件处理还会先修改engine部分状态，简单移后seen_events不能保证事务性。

**处置**：解析、静态校验全部完成后才开始应用；应用成功再提交身份/last_key/day。如果handler中途抛错，显式标记 runner failed/poisoned，拒绝后续dispatch，必须使用新engine从冻结配置和已验证输入重放。CLI保存 failure.json（source,line,validated_event_count,input/config/codehash,error,failed状态），不得把部分运行标为成功。不建议随意局部retry。

```python
if self.failed:
    raise RuntimeError('replay failed; rebuild and replay verified input')
parsed = validate_and_parse(event)  # 只校验，不更新engine。
try:
    apply_event(parsed)
except Exception:
    self.failed = True
    raise
self.seen_events[event_id] = canonical_digest(event)
self.last_key, self.day = key, day
self.events_processed += 1
```

### CONFIG-01（P1，输入校验组）：配置可通过但遇行情即异常

**位置**：`config.py:14–30` 仅 bool字段严格type校验；`features.py:95–96,110` 对rvol_days/rvol_min_days没有正整数type校验；`413` 使用其作为slice上限。

**复现**：frozen config把features.rvol_days设20.5，build_engine通过；第一次 `_rvol_baseline` 抛 `TypeError: slice indices must be integers or None or have an __index__ method`。

**影响/处置**：validate-config输出“有效”后，策略无法运行；把整数数量、枚举/来源、特征名、数值和booleans统一严格schema，字段具体类型前置校验。保留配置严格性并补正整数测试；与根审查发现 FeatureSnapshot.valid='false'实际通过开仓合并为输入schema缺口。

```python
for name in ('rvol_days', 'rvol_min_days'):
    value = getattr(self, name)
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
```

## 删除、合并、保留的边界

- 建议删除/合并：无匹配窗口的全表历史基准扫描、同snapshot重复return、已有snapshot再重复构建feature_windows、每次轮询对已完成drift的遍历、报告中没有明确语义的fill_callbacks命名（重命名）。
- 建议重构：完整输入/日志内存驻留、更正后的报告事实、失败runner生命周期、schema类型约束。
- 保留：Decimal金额、因果接收排序、去重、更正journal、风险poll、no-trade证据、未知值/暖机/数据质量门控、冻结配置。当前 profile显示这些安全流程不是主要性能成本。
- `execution.json`嵌入journal并另存execution-journal.jsonl是储存重复，但当前14条3KB量级且恢复API依赖journal；可在version2快照中外部引用，优先级P3，不宜直接删除。
- 保存成功报告只需生成一次（当前CLI.run和save分别report），当前开销很小；可把已有report传save，优先级P3。
- 长期run应使用唯一目录与manifest、源代码hash、原始输入hash、配置hash、runtime版本；成功/失败使用临时目录原子发布。当前仅冻结配置不足以复现不同代码版本。

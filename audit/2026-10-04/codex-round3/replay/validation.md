# RPL-03 / RPL-04 修复与验收

日期：2026-10-04。改动限于 `ibkr_microalpha/cli.py`、`ibkr_microalpha/replay.py`、新测试 `tests/test_round3_replay.py`。

## 已实现

- `cli.py:14` 写前检查输入/config 与全部六个运行产物、failure.json 的解析路径及文件身份，拒绝同路径、符号链接指向同文件和 hardlink。`cli.py:97` 在 demo 生成事件之前检查其目标，避免生成路径指向旧 raw-input.jsonl 时覆盖旧运行。
- `cli.py:45` 先配置校验，再同文件系统临时目录中回放。raw/audit 写入、最终批次和 flush 成功后退出两个流上下文；close 成功后才写报告。源输入与输出目录内的非产物 events.jsonl 保留。
- `replay.py:36` 用报告作为最后提交的成功标记。先备份旧报告及旧产物，再发布新产物，最后 replace 报告。中间任何 replace 失败还原旧运行；若还原也失败，保留同父目录 backup、不恢复成功标记，并在异常附注给出恢复位置。错误尝试另存 `.failed/<id>/failure.json`，不覆盖旧成功运行。
- `replay.py:391` 将 raw/audit 写入放入应用后的失败边界，全部证据写成功才提交 identity / events_processed。短写也视为错误。应用、证据、finish、flush、whole-file run、close、save、发布任一失败令 runner poisoned；之后 dispatch/finish/save 明确拒绝，恢复须从原输入重建。
- `replay.py:114` 将每事件耗时列表替换为每事件类型固定 192 桶直方图，count/max 精确，p50/p99 近似。`metrics.note` 明确为本机 dispatch 墙钟，包含证据写入，但不含源文件读取、最终批次/flush 与 save；不表示实盘或 broker 延迟。
- `replay.py:325` 将 account_snapshot 的 JSON covered_order_ids 数组转换为 tuple，新资金元数据由 AccountSnapshot 校验。

## 测试证据

新回归用例初次运行失败，证明源路径冲突、输出失败后继续运行、过早报告、旧运行被部分替换、无界 timing 列表等原行为。后来单独补充 demo 生成路径测试，修改前 create_demo 被调用而失败；前置保护后通过。

最终命令（在仓库根目录）：

```text
python -m unittest tests.test_round3_replay tests.test_replay tests.test_reporting -q
```

结果：45 tests / 1.185 s / OK。包括 20 个新增用例、11 个既有 replay 用例和 14 个既有 reporting 用例。验证 write、partial audit write、短写、finish write、两个 sink flush、close、配置构建、解析、中途发布、报告提交、持续 rollback 失败等注入点。正常 rollback 恢复旧六个文件的原始字节；rollback 持续失败时旧 report/raw 可从保留 backup 恢复，输出不含 report 成功标记。

10,000 个 timer 事件的新增测试验证：精确 events=10,000，timing_bins=192，统计存储不随事件数增长。只证明新增 timing 存储有界；全天唯一事件 digest、执行 journal 与未启用 sink 的原始行仍随当天事件数增长。

已执行离线 demo + 新引擎 replay 验证：

```text
python -m ibkr_microalpha demo --events audit/2026-10-04/codex-round3/replay/demo-events.jsonl --output audit/2026-10-04/codex-round3/replay/demo --verify-replay
```

当时产物 `demo/report.json` 和 `demo/verify/report.json` 业务字段一致（排除 metrics/manifest）；3789 events，1 economics_rejected，0 intents。此结果来自合成输入与当时并行更新中的策略版本，不能当作盈利/实盘证据。最终集成验收应使用根代理完成后的源码重新生成。

## 边界

报告最后 replace 提供 CLI 的成功提交协议及可捕获 I/O 失败回滚；没有把六个文件变成单一文件系统事务，也未证明断电 fsync 持久性或并发读者的快照隔离。调用者自持 sink 的 close 责任在调用者；CLI 已将 close 错误纳入 poison 与不发布成功报告的边界。源文件必须保留，部分失败归档不能替代完整原输入重建。

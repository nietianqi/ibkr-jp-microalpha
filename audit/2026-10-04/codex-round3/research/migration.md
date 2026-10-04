# STR-09 / STR-10 / DATA-09 接口迁移与验收

此目录的价格、费用和行情均为明确的人工单元测试资料。`VERIFIED_REPLAY` 表示标签来自重新验证的完整 Replay 执行路径，不表示测试价格为真实行情或证明盈利。

## 迁移

1. `policy_fingerprint` 从 `ibkr_microalpha.economics` 导入，覆盖完整冻结配置（排除会自引用的 provenance）。
2. 配置必须显式冻结 `engine.min_independent_days >= 2`；示例值为 20。独立日数与样本总数同时准入。
3. `Prediction` 和 `CalibrationRow` 增加 `sample_days`（Prediction）、`label_source` 与 `provenance`。未提供来源的历史数据保持 UNVERIFIED，无法通过经济准入。ARTIFICIAL 只允许明确的 demo fixture，须有 `{'source':'ARTIFICIAL_FIXTURE'}`。
4. 报价模拟使用 `quote_baseline_label`，得到固定身份 QUOTE_BASELINE_V1 与理由；旧 `label_intent` 为该基线的数值兼容包装。部分入场/退出、缺完整 TTL、非法/延迟/不同步报价返回 None，不能静默写零。基线不是正式校准输入。
5. 正式研究使用工厂，不能手写 ReplayIntentLabel 或传裸 `(day,score,net)` 元组。原始输入从 raw_lines 重建；raw_sink 模式显式提供原始事件。任意不完整意图、活动订单、未归属意图、残余仓位、缺最终 commission 或 manifest 不一致都拒绝整个标签集。
6. 标签携带实际入场相对追价 ticks、候选参考价、实际 cap。`build_calibration_rows(max_chase_ticks=...)` 必须与原回放策略完全相同。绝对价格 forecast 标签不能冒充相对 ticks calibration；两种策略重新独立研究。
7. row provenance 包含完整策略指纹、输入/代码/标签 SHA256、最终费用版本、训练截止、独立日标识及原 max_chase_ticks。表加载与预测均检验来源/日数；加载检验 cap 未被改写，交易门控同时检验配置/费用/训练时间。

```python
from ibkr_microalpha.economics import CalibrationTable, policy_fingerprint
from ibkr_microalpha.research import replay_intent_labels, build_calibration_rows, calibration_table_event

labels = replay_intent_labels(runner, verified_manifest=persisted_manifest)
# 流式 raw_sink: source_events=读取原始事件文件得到的 iterable
rows = build_calibration_rows(
    labels, score_edges, policy_id=c.policy_id, version=c.model_version,
    holding_seconds=c.holding_seconds, quantity=100, max_chase_ticks=10,
    min_days=c.min_independent_days, min_samples=c.min_samples,
)
table = CalibrationTable(rows, known_at=availability_time, version=artifact_version)
table.validate_for_profile(profile, min_independent_days=c.min_independent_days,
    policy_hash=policy_fingerprint(frozen_config), fee_version=commissions.version,
    as_of=availability_time)
event_payload = calibration_table_event(rows, version=artifact_version, known_at=availability_time)
```

不可用“先用 2 日生成，再宣称符合示例 20 日门槛”的方式部署；薄桶返回空列表，不能创建非空部署表。多数量/多持仓期分别建立契约匹配的标签集和 rows。

## 验收证据

- red-tests.txt：修复前 9 用例，7 failures + 2 errors（缺新接口），覆盖已证实行为问题。
- green-tests.txt：新增 13 研究回归 + 13 既有 economics 用例，共 26 通过。
- 既有 DataFixes + market 共 27 用例通过。
- closed-loop-evidence.py / closed-loop-results.json：实际重新播放原始报价/成交量，得到 0 / 40 / 100 股分支；净值分别 0、-452、-1070 JPY，均保留100股冻结目标；三独立日构成一行，20日门槛薄桶为空；最终费用缺失、追价 cap 改写、单日退化均拒绝。
- JSON calibration_table event 回到 Replay 成功；正向单元fixture在原始document中显式冻结2日门槛，配置指纹与EngineConfig一致，示例的20日门槛仍保留；内存账本事后突变不改变从原始输入重新产生的标签；缺输入行/错误 manifest 拒绝。
- 波动率网格 1 / 7 / 120 秒均保留60秒窗口终点的100→200跳变。

这些用例验证数据契约和交易分支处理，不支持任何实盘收益结论。

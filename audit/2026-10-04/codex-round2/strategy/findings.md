# Codex 第2轮：策略/研究契约复核

对象为当前 e592e34 工作树，未改生产代码、测试或协作文档。探针使用当前接口，结果在同目录 results.json。人工路径只证明代码语义，不证明真实成交或策略盈利。

## S2-01 P1：quote-only 标签被声明为完整部署政策，遗漏部分入场、订单限价/撤改及子单费用

- 位置：ibkr_microalpha/research/labels.py:3–8、45–79；economics.py:143–147；research/calibration.py:59–64。
- 原因：label_intent 在 labels.py:59 要求一条后续报价的 ask_size 能覆盖全部 quantity，否则到TTL后在62–63返回0。它没有部分入场库存。退出在70–78按每条未来 bid 继续成交，不保留先前已发出的卖单限价；最后把所有退出合成一张计费订单。LabelPolicy:23–30没有普通信号退出、环境退出、波动止损及执行延迟，exit_slippage_ticks虽声明却未消费。
- 复现一：目标100，TTL内可见ask_size仅40，标签为0；合法的买40@3001、卖40@2950完整部分成交路径含费用为-2230.432 JPY。返回0不能称为保守，部分亏损样本被当作未成交。
- 复现二：买100@100.1，首次风险退出bid90仅有40，随后bid80有余量；标签为-1770 JPY。按声明的一tick主动退出，第一张卖单限价89.9，不能在80成交余量；若经过撤改让第二张子单在80成交，两个卖子单最低费用各80，加买费80，则净值-1850 JPY。标签少扣80，还没有模拟撤改时间内风险。
- 影响：这些标签随后在 build_calibration_rows 被任意 policy_id 标记成完整部署政策；CalibrationRow文档又承诺含全部子单费用、部分成交分支，实际并不成立。得到的均值及下界不对应协调器实际政策，可能影响经济门控与参数选择。
- 具体处理：保留quote-only标签作为有独立policy_id和明确全量即时成交假设的研究baseline，禁止导出成部署策略的校准行；正式部署校准从同一 Replay/ExecutionBook 策略完整意图和费用链取标签。若继续实现报价模拟器，必须追踪部分入场/库存、活动子单限价、提交/撤单确认/替换、费用与残余无法估值，并采用和部署同一退出函数。校准artifact绑定policy hash、label engine hash、费用版本和样本覆盖，不只比较人工字符串ID。
- 验收：目标100部分买40后余量撤掉且买40净损失计入；风险卖40后限价失去可成交性，余量不能无确认自动80成交；第二子单最低佣金正确；普通信号退出先于最长持仓、启用波动止损、延迟/未平仓均与部署回放同结果。不能把不可估计的路径删除或填0。

## S2-02 P1（研究准入）：独立交易日字段没有进入校准准入

- 位置：economics.py:164–167允许sample_days=0；181–183无条件产生 reliable=True/calibrated=True；prediction_gate:114–118只检sample_count。research/calibration.py:18–30允许单日bootstrap，33–64未限制min_days至少2。
- 复现：sample_days=0、sample_count=100、mean=lower=100的CalibrationRow产生的Prediction通过min_samples=30、margin=1门控；同一天100个相同100 JPY样本、min_days=1，生成mean=lower=100、reliable=True。
- 原因与影响：按日重采样方法的独立单位是日，但准入把同一天的重复意图数量当足量独立样本。单日bootstrap每次重采样同一天，区间退化，不能证明跨日均值可靠。当前demo和真实研究未完成的边界仍应保留，不能因新增bootstrap工具就认为校准证据已闭合。
- 修复：冻结engine/calibration的min_independent_days或min_blocks；lookup后先校验sample_days，再转换Prediction/进入经济门控。CalibrationRow应要求正sample_days及sample_count>=sample_days；bootstrap至少两个独立块才能返回区间（实际准入最低块数由预先冻结的研究规则确定），不足时标不可靠或不生成行。验证真实artifact的训练范围/独立块信息。
- 验收：0日、1日100重复样本均不获得可靠部署准入；达阈值的跨日样本通过；低日数高intent数不绕过门槛；错误/缺失独立块证据拒绝。

## S2-03 P2：可配置波动网格漏掉最后不足一个网格的区间，过大网格返回假零

- 位置：features.py:169–172仅验证volatility_grid_seconds>0；662–689 _realized_volatility，679–680遇point>end直接break。
- 复现：grid=7，60s窗口，第59s价格100→200，snapshot.valid=True，r_60=6928.97bps而volatility_bps=0；grid=120也被FeatureConfig接受，60s窗口即使第1s跳价，volatility仍为0。
- 影响：非默认但合法配置下会漏掉末段价格变化，低估风险，可能绕过最大波动门控和波动止损预算。默认grid=1的旧锚点问题已修，不能混为同一未修问题。
- 修复：采样序列必须包含cutoff锚点与实际end，末段采样取min(next_grid,end)后再结束；或强制grid同时整除60与已配置市场窗口且不大于最短窗口。网格定义进入冻结artifact版本。
- 验收：grid=7末尾59s跳价不为0；grid>window明确拒绝或正确包含terminal；默认grid=1锚点回归保持通过；无完整覆盖返回缺失。

## 旧项确认

- DATA-04旧锚点问题：默认grid=1第1s跳价后，r_60与volatility均为6928.97bps，已修。
- DATA-05有效日顺序：21日记录、20日有效、最近1日invalid，_baseline_value正确返回1000，已修。
- DATA-01旧签名：SubscriptionCoordinator.record_candidate:276–277正确传(symbol,at,requirements)；EntryPipeline:229–230调用新协调器，旧TypeError已修。
- DATA-01预排序循环：SubscriptionCoordinator.pre_score:234–240只消费rs_60/rs_30，已移除TBT RVOL/VWAP依赖；迟到订阅完整日VWAP种子缺口仍由协作文档11.3明确列为部分实施，不作为隐藏新缺陷。
- CFG-01：回放stream_health分支replay.py:136–139使用_bool；因此FeatureEngine.set_trade_stream_health的直接API未做bool类型验证不等于当前Replay可接受字符串false，不列为本轮主要发现。

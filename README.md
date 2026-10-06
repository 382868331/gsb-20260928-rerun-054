# 金融合约估值系统（固定利率与浮动利率）

第一项功能：由固定利率合约生成**实际支付日程**（逐笔现金流），并基于贴现曲线计算**现值**。
合约、日历、曲线、估值批次相互独立；浮动利率品种可复用同一套 `Cashflow` 表示。

## 特性

- **合约**：本金、固定利率、起息/到期日、付息频率、计息约定、营业日约定、月末（EOM）开关、残期类型、期末是否兑付本金。
- **日历**：周末为非营业日，可附加自定义假日；支持 `FOLLOWING` / `MODIFIED_FOLLOWING` / `PRECEDING` / `UNADJUSTED`。
- **月末处理**：起息日为月末时，理论周期日对齐各月月末（如 2/28 起息，季度锚点为 5/31、8/31、11/30）。
- **非工作日顺延**：周期日按营业日约定调整，调整后的日期同时作为下一段计息起息日。
- **残期（不整期）**：`SHORT_FIRST` / `LONG_FIRST` / `SHORT_LAST` / `LONG_LAST`；`NONE` 遇不整期直接报错。
- **计息约定由输入选择**：`ACT/360`、`ACT/365F`、`30/360`；不支持的约定（如 `ACT/ACT`）抛出 `UnsupportedConventionError` 明确拒绝。
- **曲线**：贴现因子对数线性插值，平行平移利率生成**新曲线**，原曲线不变。
- **估值批次**：换曲线只产生新的 `Valuation` 记录，日程快照保存在估值记录中，绝不覆盖原日程。
- **浮动利率**：`FloatingRateContract` 支持基准利率名称、营业日定盘滞后（默认 T-2）、加点（spread）与已知历史定盘字典。
- **定盘日**：相对每个计息期的（营业日调整后）起息日，按日历向前数若干个营业日；周末与假日不落在定盘日上。
- **历史定盘保护**：定盘日不晚于估值日却缺少定盘值时抛出 `MissingFixingError`，预测曲线永远不会悄悄替代已发生的定盘。
- **预测曲线**：仅定盘日晚于估值日的期间使用预测曲线推导简单复利远期利率；预测曲线可与贴现曲线不同。
- **逐笔解释**：每条 `Valuation` 保存 `explanations`，说明该笔是 `FIXING`（含定盘日/定盘值）还是 `FORECAST`（含预测曲线与远期区间），支付日已过的期间标记为 `PAID` 且不参与 NPV。
- **历史更正**：`FixingCorrection` 只有在到达日后才纳入估值；已支付原日程保持原实际金额，另以 `ADJUSTMENT` 调整款结算差额。
- **部分提前终止**：`PartialTermination` 生成本金返还款和 `CANCELLED` 取消本金行；终止后的利息与期末本金按剩余名义本金重算。
- **状态与依据**：解释字段区分 `fix_state`、`payment_state`、`schedule_state`、`cashflow_kind`，并在每笔调整款上保存 `event_id` 与 `basis`。
- **权益转让**：`RightsTransfer` 记录转让比例、适用现金流范围（按支付日）与收到通知时刻；已支付款项仍归原支付记录，未支付的本金、利息与调整款按份额追到各持有人；交叠转让累计分出超过原权益时明确报错。
- **转让撤销与迟到通知**：`TransferRevocation` 撤销已达成的转让；历史视角保持原结论，当前视角以 `COMPENSATION` 补偿记录说明差额。
- **单位拆分/合并**：`UnitChange` 记录单位口径变化（`factor>1` 拆分、`0<factor<1` 合并），与经济权益比例分别记录；`holder_positions()` 给出某知识时点下各持有人的单位数与原合约权益比例，单位变多不会凭空增加应收金额。
- **固定单位 vs 固定比例转让**：`RightsTransfer` 的 `units` 与 `ratio` 二选一；固定单位按转让生效日的单位口径折算为权益比例，之后的拆分/合并不改变其对应权益，两种约定始终可分别追查。
- **舍入归属**：每个持有人份额独立舍入到最小可分单位（6 位小数），未分配余量在 `contract_view()` 的 `unallocated_residual` 单列，不与任何持有人份额混同。
- **零碎权益选择**：单位拆并产生零碎单位时，`FractionElection` 支持 `RETAIN` 保留可累积 / `CASH` 现金替代 / `TRANSFER` 转给另一持有人；`fraction_settlement()` 生成 `FractionReport`，内部一律用未舍入精确值计算，落账现金才舍入，余量逐笔记录并汇总于 `total_residual` 供核对。
- **待定与可用现金**：现金替代价格（`CashPriceFixing`）可晚于份额变化到达；定价前为 `PENDING_CASH` 待定权益，定价后生成 `CASH_IN_LIEU` 可用现金；价格更正只追加 `PRICE_ADJUSTMENT`，选择撤回（`ElectionRevocation`，可部分）只追加 `REVOCATION_ADJUSTMENT`，已完成的分配记录不改写。
- **零碎权益汇总处置**：`AggregationBatch` 把同一证券多位持有人的零碎份额汇总处理；`AggregationElection` 各自选择 `RETAIN` 保留 / `SELL` 合并出售 / `COMPENSATE` 按批次统一补偿价补偿；成交现金与费用按实际参与份额（精确未舍入单位）比例分配，每位持有人的权益来源逐笔可查。
- **最小货币单位余量规则**：无法均分的最小货币单位按批次指定的可复核规则分配（`LARGEST_REMAINDER` 最大余数 / `HOLDER_ORDER` 持有人序），每次分配记录于 `residual_allocations`；不足一个最小单位的余量单列于 `sub_unit_residuals`，不默默丢弃也不重复补齐。
- **撤回与迟到修订**：成交前 `AggregationWithdrawal`（可部分）重新计算可参与数量；成交后撤回只追加 `WITHDRAWAL_ADJUSTMENT` 追回调整，迟到的 `AggregationPriceRevision` 只追加 `PRICE_ADJUSTMENT`，已收现金不当作未发生。
- **过程比较**：`compare_processes()` 对照“先撤回后定价”与“先定价后撤回”两种过程，逐步核对剩余份额、累计现金与费用守恒（`ProcessComparison.check_conservation`）。
- **分配对账**：`allocate()` 生成 `AllocationReport`，提供原合约视角（`contract_view`）与各持有人视角（`holder_view` / `totals_by_holder`），`check_conservation()` 校验总额守恒；报告可 `save_json` / `load_json`，重开后各方已收/应收/补偿互不覆盖。
- **存档重开**：`ValuationBatch.save_json()` / `load_json()` 保存当前日程、原始日程快照、历史结论、解释与事件依据。

## 用法

```python
from datetime import date
from finval import *

contract = FixedRateContract(
    contract_id="L-001",
    notional=1_000_000.0,
    fixed_rate=0.06,
    start_date=date(2025, 3, 15),
    end_date=date(2026, 3, 31),
    frequency=Frequency.SEMIANNUAL,
    day_count=DayCount.ACT_360,
    business_day_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
    calendar=Calendar(holidays={date(2025, 9, 1)}),
    stub=StubType.SHORT_FIRST,
)

curve = DiscountCurve(
    curve_id="FLAT-5PCT",
    valuation_date=date(2025, 3, 15),
    pillar_dates=(date(2026, 3, 31),),
    discount_factors=(0.95,),
)

batch = ValuationBatch("BATCH-001")
v1 = batch.run(contract, curve)
v2 = batch.run(contract, curve.parallel_shift(0.01))  # 新曲线 -> 新估值

for cf, pv in zip(v1.cashflows, v1.present_values):
    print(cf.index, cf.payment_date, round(cf.amount, 2), round(pv, 2))
print("NPV:", v1.npv)
```

### 浮动利率

```python
contract = FloatingRateContract(
    contract_id="FLT-001",
    notional=1_000_000.0,
    start_date=date(2025, 2, 28),
    end_date=date(2026, 2, 28),
    frequency=Frequency.QUARTERLY,
    day_count=DayCount.ACT_360,
    business_day_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
    calendar=Calendar(holidays={date(2025, 5, 1)}),
    stub=StubType.NONE,
    index_name="3M-TEST",
    fixing_lag_business_days=2,
    spread=0.005,
    fixings={date(2025, 2, 26): 0.040, date(2025, 5, 28): 0.042},
)

discount = DiscountCurve("DISC", valuation_date, (date(2026, 3, 31),), (0.97,))
forecast = DiscountCurve("FWD", valuation_date, (date(2026, 3, 31),), (0.96,))
v = ValuationBatch("B").run(contract, discount, forecast)

for e in v.explanations:
    print(e.index, e.status, e.rate_source, e.fixing_date, e.forecast_start, e.present_value)
```

- 修改预测曲线只影响 `rate_source == "FORECAST"` 的现金流；已定盘金额冻结在日程快照中。
- 旧 `Valuation.cashflows` 不会随后续新曲线或新估值改变。

### 历史更正与部分终止

```python
from dataclasses import replace

events = (
    FixingCorrection(
        event_id="FIX-001",
        fixing_date=date(2025, 5, 28),
        old_value=0.042,
        new_value=0.050,
        received_date=date(2025, 10, 10),
        settlement_date=date(2025, 10, 20),
        basis="定盘机构更正通知",
    ),
    PartialTermination(
        event_id="TERM-001",
        effective_date=date(2025, 11, 28),
        reduction_amount=200_000.0,
        received_date=date(2025, 11, 20),
        basis="双方提前终止协议",
    ),
)
contract = replace(contract, events=events)
valuation = batch.run(contract, discount, forecast)
batch.save_json("valuation-batch.json")
reopened = ValuationBatch.load_json("valuation-batch.json")
```

终止生效日若落在节假日，先按合约营业日约定调整，再作为利息分段、本金返还和取消款项的共同边界；恰好在定盘日或付息日时，同日资料按“已到达/已生效”处理。

### 权益转让与持有人对账

```python
from finval import RightsTransfer, TransferRevocation, allocate

transfer = RightsTransfer(
    event_id="T1",
    to_holder="乙",
    ratio=0.4,                            # 转让原权益的 40%
    effective_date=date(2025, 9, 1),      # 适用现金流起点（按支付日，含）
    end_date=None,                        # 适用现金流终点（含），None 表示至到期
    received_date=date(2025, 10, 10),     # 收到转让通知的时刻
    basis="转让协议",
)
contract = replace(contract, events=contract.events + (transfer,))
valuation = batch.run(contract, curve)

# 历史视角：通知到达前的结论不变
hist = allocate(valuation, as_of=date(2025, 10, 5), view="HISTORICAL")
# 当前视角：已付款项保持原支付记录，差额以 COMPENSATION 补偿记录体现
cur = allocate(valuation, view="CURRENT")
cur.check_conservation()                  # 总额守恒校验
print(cur.totals_by_holder())             # 各方 已收/应收/补偿
print(cur.contract_view())                # 原合约视角逐笔对照
cur.save_json("allocation.json")
```

- 多次转让交叠时，任一持有人累计分出比例超过其原权益即抛出 `ValueError`。
- `TransferRevocation` 撤销已达成的转让：撤销到达前的历史结论不变，当前视角回补差额。

### 单位拆分/合并与持仓查询

```python
from finval import UnitChange, holder_positions

split = UnitChange(
    event_id="U1",
    effective_date=date(2025, 10, 15),   # 单位口径切换日
    factor=10.0,                         # 1 拆 10；合并用 0 < factor < 1
    received_date=date(2025, 10, 20),    # 通知可晚于生效日
    basis="拆分公告",
)
contract = replace(contract, total_units=100.0,
                   events=contract.events + (split,))
valuation = batch.run(contract, curve)

# 某知识时点下各持有人的单位数与原合约权益比例
for p in holder_positions(valuation, as_of=date(2025, 12, 1)):
    print(p.holder, p.units, p.equity_ratio)

# 固定单位转让：按生效日单位口径折算，之后拆并不改变其权益
t_units = RightsTransfer(event_id="T2", to_holder="丙", units=30.0,
                         effective_date=date(2025, 11, 1),
                         received_date=date(2025, 11, 1), basis="固定单位转让")
```

- 拆分/合并只改变单位数，不改变权益比例；迟到的拆分通知不改动已付款项的金钱归属，若改变未付款项解释则以 `COMPENSATION` 记录差额。
- 每笔 `HolderShare.unit_factor` 标明该笔现金流采用的单位口径（变化前/后）。
- `contract_view()` 逐笔列示总额、已分配合计与 `unallocated_residual` 舍入余量；`check_conservation()` 校验 份额 + 余量 = 原金额。

### 零碎权益选择与现金替代

```python
from finval import FractionElection, CashPriceFixing, ElectionRevocation, fraction_settlement

# 3 并 1 产生零碎单位；乙选择现金替代，丙把余量转给 ORIGINAL
elections = (
    FractionElection(event_id="E1", holder="乙", unit_change_event_id="U1",
                     method="CASH",
                     effective_date=date(2025, 10, 1),   # 选择截止日，同日即有效
                     received_date=date(2025, 10, 1), basis="现金替代选择"),
    FractionElection(event_id="E2", holder="丙", unit_change_event_id="U1",
                     method="TRANSFER", to_holder="ORIGINAL",
                     effective_date=date(2025, 10, 1),
                     received_date=date(2025, 10, 1), basis="余量转让协议"),
)
price = CashPriceFixing(event_id="P1", unit_change_event_id="U1",
                        price_per_unit=9.9,
                        received_date=date(2025, 10, 20), basis="管理人定价公告")
contract = replace(contract, events=contract.events + elections + (price,))
valuation = batch.run(contract, curve)

report = fraction_settlement(valuation, as_of=date(2025, 12, 1))
report.check_conservation()          # 单位与资金双重守恒
print(report.cash_summary())         # 待定单位 / 可用现金 / 调整 分列
print(report.retained_units())       # 各持有人保留累积的零碎单位
print(report.total_residual)         # 舍入余量总和，可逐笔核对
```

- 同日事件按 `(effective_date, received_date, event_id)` 确定生效顺序；选择截止日不早于单位变化生效日（含同日）才适用，未选择默认 `RETAIN`。
- 价格未知期间 `cash_summary()` 的 `pending_units` 与 `available_cash` 严格区分；价格更正用 `CashPriceFixing(corrects_event_id=...)` 链接，只产生 `PRICE_ADJUSTMENT`。
- `ElectionRevocation(portion=0.5)` 部分撤回：被撤回部分恢复保留累积，已分配现金以 `REVOCATION_ADJUSTMENT` 追回，原 `CASH_IN_LIEU` 记录不变。

### 零碎权益汇总处置

```python
from finval import (
    AggregationBatch, AggregationElection, AggregationExecution,
    AggregationPriceRevision, AggregationWithdrawal,
    aggregation_settlement, compare_processes,
)

agg_batch = AggregationBatch(event_id="B1", unit_change_event_id="U1",
                              residual_rule="LARGEST_REMAINDER",   # 余量可复核规则
                              received_date=date(2025, 10, 2), basis="汇总处置批次")
elections = (
    AggregationElection(event_id="E-YI", batch_id="B1", holder="乙",
                        method="SELL",                      # 合并出售
                        received_date=date(2025, 10, 3), basis="出售选择"),
    AggregationElection(event_id="E-BING", batch_id="B1", holder="丙",
                        method="COMPENSATE",                # 按批次统一补偿
                        received_date=date(2025, 10, 3), basis="补偿选择"),
)
execution = AggregationExecution(
    event_id="X1", batch_id="B1", price_per_unit=0.1, total_fee=0.07,
    compensation_price_per_unit=0.09,       # COMPENSATE 统一补偿价
    received_date=date(2025, 10, 10), basis="成交确认")
contract = replace(contract,
                   events=contract.events + (agg_batch,) + elections + (execution,))
valuation = batch.run(contract, curve)

report = aggregation_settlement(valuation, as_of=date(2025, 12, 1))
report.check_conservation()                # 精确 = 落账 + 余量分配 + 次单位余量
print(report.cash_summary())               # 各持有人 保留单位/成交单位/净现金/费用/调整
print(report.residual_allocations)         # 最小货币单位分给了谁，逐笔可复核
report.save_json("aggregation.json")       # 重开后继续处理同一批次

# 两种过程对照：先撤回后定价 vs 先定价后撤回
comparison = compare_processes(valuation, "B1", as_of=date(2025, 12, 1))
for step in comparison.withdraw_first:
    print(step.label, step.remaining_units, step.cumulative_cash, step.cumulative_fee)
```

- 成交前撤回（`AggregationWithdrawal(portion=...)`）重新计算可参与数量；成交后撤回只形成 `WITHDRAWAL_ADJUSTMENT` 追回记录，被撤回单位恢复保留。
- 迟到价格修订（`AggregationPriceRevision`）只追加 `PRICE_ADJUSTMENT`，原 `PROCEEDS` 记录不变；同一 `event_id` 的重复通知记入 `duplicates_ignored`，不会再次分配。
- `AggregationReport.save_json/load_json` 存档重开；用更晚 `as_of` 重跑时既有记录保持前缀不变，迟到事件只追加。

## 测试

```bash
python -m pytest -q
```

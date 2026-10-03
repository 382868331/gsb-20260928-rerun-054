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
- **单位拆分与合并**：`UnitSplit` / `UnitMerge` 只改变计量单位口径，经济权益比例与应收金额不变；每笔现金流按支付日分别采用变化前/后的单位解释（`CashflowTotal.total_units`、`HolderShare.units`），`positions` 给出各持有人在某一时点的单位数量与占原合约权益比例。
- **固定单位与固定比例转让**：`RightsTransfer` 可指定 `ratio`（固定比例）或 `units`（固定单位数量，按生效时点单位口径折算为权益比例），`spec_kind` 分别记录、可追查；后续合并按各自口径折算，不会把两种约定混为一谈。
- **舍入与余量对账**：`allocate(..., rounding_unit=...)` 指定金额最小可分单位，每份独立舍入，未分配余量逐笔显式记录为 `CashflowResidual`，`contract_view` 对照总额/已分配/余量，不把误差藏进任何一位持有人。
- **分配对账**：`allocate()` 生成 `AllocationReport`，提供原合约视角（`contract_view`）与各持有人视角（`holder_view` / `totals_by_holder`），`check_conservation()` 校验总额守恒（份额+补偿+余量=总额）；报告可 `save_json` / `load_json`，重开后各方已收/应收/补偿互不覆盖；事件按确定性顺序处理，同一知识时点下不同接收顺序得到一致结论。
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

### 单位拆分/合并与持仓解释

```python
from finval import UnitSplit, UnitMerge

split = UnitSplit(
    event_id="S1",
    effective_date=date(2025, 10, 1),   # 该日（按支付日）起采用新单位口径
    factor=2.0,                         # 每 1 单位变为 2 单位
    received_date=date(2025, 9, 15),    # 通知到达时刻（可晚于生效日）
    basis="拆分公告",
)
contract = replace(contract, events=contract.events + (split,))
valuation = batch.run(contract, curve)

report = allocate(valuation, initial_units=1000, rounding_unit=0.01)
for pos in report.positions:            # 各持有人：单位数量 + 占原合约权益比例
    print(pos.holder, pos.units, pos.equity_ratio)
for row in report.contract_view():      # 逐笔：总额 / 已分配 / 未分配余量 / 单位口径
    print(row["cashflow_index"], row["amount"], row["allocated"],
          row["residual"], row["total_units"])
```

- 拆分/合并不改变任何持有人的经济权益比例，单位变多不会凭空增加应收金额。
- 拆分通知迟到时，历史视角保持当时已知材料下的结论；当前视角下若固定单位
  转让的折算比例因此改变，差额以 `COMPENSATION` 记录并注明迟到事件依据。
- `RightsTransfer(units=N, ratio=None)` 表示转让固定单位数量（按生效时点
  单位口径折算）；与 `ratio=` 固定比例转让分别记录（`spec_kind`），
  后续合并按各自口径折算，不会混为一谈。
- 撤销尚未生效的转让只影响未来决定，不产生补偿记录。

## 测试

```bash
python -m pytest -q
```

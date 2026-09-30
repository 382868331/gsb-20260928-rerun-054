# 金融合约估值系统（固定利率 + 浮动利率）

由固定/浮动利率合约生成**实际支付日程**（逐笔现金流），并基于贴现曲线计算**现值**。
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
- **浮动利率**：`FloatingRateContract` 支持独立的定盘滞后期（营业日）与支付滞后期；`known_fixings` 保存已知历史定盘。
- **定盘与预测分段**：定盘日 ≤ 估值日的计息期必须使用已知定盘，缺失时抛 `MissingFixingError`，预测曲线不会悄悄替代历史定盘；定盘日晚于估值日时用 `ForecastCurve.forward_rate` 取远期利率。
- **逐笔可解释**：`Valuation.rate_explanations` 逐笔记录利率来源（`FIXING` / `FORECAST` / `FIXED`）、定盘日、所用预测曲线、加点后利率及 `SETTLED` / `EXPECTED` 状态。
- **估值日位于周期中间**：估值日前已支付的现金流保留金额快照、现值记 0；修改预测曲线只影响尚未固定的计息期，历史已定金额保持不变。

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

## 测试

```bash
python -m pytest -q
```

### 浮动利率示例

```python
from datetime import date
from finval import *

contract = FloatingRateContract(
    contract_id="F-001",
    notional=1_000_000.0,
    index_name="3M",
    start_date=date(2025, 2, 13),
    end_date=date(2025, 11, 30),
    frequency=Frequency.QUARTERLY,
    day_count=DayCount.ACT_360,
    fixing_lag_business_days=2,
    payment_lag_business_days=1,
    spread=0.005,
    known_fixings={date(2025, 2, 11): 0.021},  # 定盘日已到则必须提供
)

valuation_date = date(2025, 6, 10)
discount_curve = DiscountCurve("DF", valuation_date, (date(2026, 6, 30),), (0.95,))
forecast_curve = ForecastCurve("FWD", valuation_date, (date(2026, 6, 30),), (0.97,))

v = ValuationBatch("B").run(contract, discount_curve, forecast_curve)
for cf, expl in zip(v.cashflows, v.rate_explanations):
    print(cf.index, expl.rate_source, expl.fixing_date, cf.amount)
```

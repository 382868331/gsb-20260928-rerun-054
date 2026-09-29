# 金融合约估值系统

当前版本实现固定利率合约的实际支付日程生成和现值估值。

## 模型边界

- `FixedRateContract` 描述名义本金、固定利率、生效日、到期日、付款频率、短首期、计息约定和营业日约定。
- `BusinessCalendar` 支持周末、自定义节假日，以及 unadjusted、following、modified following、preceding、modified preceding。
- `DayCountConvention` 支持 `ACT/365F`、`ACT/360`、`30/360`、`30E/360`；未支持的约定抛出 `UnsupportedConventionError`。
- 日程使用实际应计起止日、调整后付款日、年化期限和逐笔金额表示；后续浮动利率现金流可复用同一 `CashFlow` 结构并扩展类型与利率字段。
- `DiscountCurve` 使用折现因子节点并按日期对 log discount factor 线性插值。
- `ValuationBatch` 为每次曲线或估值编号保留独立 `ValuationResult`，不会回写或覆盖合约日程。

## 核对

```bash
python -m unittest discover -s tests -v
```

测试中的虚构合约从 2024-01-31 起息，设置短首期到 2024-02-29，之后按季到 2024-05-31 和 2024-08-31。2024-08-31 为周六，following 顺延至 2024-09-02。测试逐笔核对短首期、正常期利息、本金以及两条曲线下的现值。

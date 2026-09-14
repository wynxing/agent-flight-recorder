# pi 代码调查回归验证

固定提交：b5332540bd702f789088aa90603cc7dd5d1084ed。模型：msee-gateway/deepseek-v4.1-flash。回归工具来源：snapshot。

baseline: 15 个已执行样本中 4 通过、11 未通过、0 无法判断、0 执行错误；可判断 15/15。未执行样本不进入分母。

grounded: 15 个已执行样本中 10 通过、5 未通过、0 无法判断、0 执行错误；可判断 15/15。未执行样本不进入分母。

## 批次预算

声明上限：{"max_cost_usd":2,"max_model_calls":400}。没有因预算停止：已用 0 次真实模型调用。没有产生成本。

所有计划样本已执行。

| 任务 | Prompt | 样本 | 结论 | 真实模型调用 | token | 成本（估算） | 成因码 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| batch-budget-total-calls | baseline | 1 | failed | 6 | 107093 | 0.005702 |  |
| batch-budget-total-calls | baseline | 2 | failed | 4 | 63415 | 0.005612 |  |
| batch-budget-total-calls | baseline | 3 | failed | 4 | 62557 | 0.005456 |  |
| batch-budget-total-calls | grounded | 1 | failed | 8 | 187267 | 0.009147 |  |
| batch-budget-total-calls | grounded | 2 | failed | 6 | 109457 | 0.006661 |  |
| batch-budget-total-calls | grounded | 3 | failed | 12 | 297768 | 0.009235 |  |
| cause-precedence-blocked-vs-budget | baseline | 1 | passed | 3 | 21697 | 0.002679 |  |
| cause-precedence-blocked-vs-budget | baseline | 2 | failed | 6 | 54192 | 0.003180 |  |
| cause-precedence-blocked-vs-budget | baseline | 3 | failed | 6 | 56929 | 0.004245 |  |
| cause-precedence-blocked-vs-budget | grounded | 1 | passed | 8 | 88171 | 0.005163 |  |
| cause-precedence-blocked-vs-budget | grounded | 2 | passed | 8 | 135112 | 0.008797 |  |
| cause-precedence-blocked-vs-budget | grounded | 3 | passed | 5 | 65741 | 0.004869 |  |
| write-tool-gate-on-recorded-regress | baseline | 1 | failed | 6 | 48008 | 0.003008 |  |
| write-tool-gate-on-recorded-regress | baseline | 2 | failed | 4 | 31261 | 0.002717 |  |
| write-tool-gate-on-recorded-regress | baseline | 3 | failed | 4 | 31568 | 0.002822 |  |
| write-tool-gate-on-recorded-regress | grounded | 1 | passed | 4 | 32679 | 0.002963 |  |
| write-tool-gate-on-recorded-regress | grounded | 2 | passed | 5 | 47744 | 0.004022 |  |
| write-tool-gate-on-recorded-regress | grounded | 3 | passed | 5 | 42319 | 0.002888 |  |
| unknown-cost-does-not-stop | baseline | 1 | failed | 4 | 15861 | 0.001603 |  |
| unknown-cost-does-not-stop | baseline | 2 | failed | 5 | 20973 | 0.001609 |  |
| unknown-cost-does-not-stop | baseline | 3 | failed | 3 | 10762 | 0.001338 |  |
| unknown-cost-does-not-stop | grounded | 1 | passed | 6 | 89435 | 0.006148 |  |
| unknown-cost-does-not-stop | grounded | 2 | failed | 5 | 30682 | 0.003254 |  |
| unknown-cost-does-not-stop | grounded | 3 | failed | 10 | 114402 | 0.005343 |  |
| pi-tape-mismatch-code | baseline | 1 | passed | 5 | 39950 | 0.003739 |  |
| pi-tape-mismatch-code | baseline | 2 | passed | 4 | 35290 | 0.003174 |  |
| pi-tape-mismatch-code | baseline | 3 | passed | 6 | 53661 | 0.003662 |  |
| pi-tape-mismatch-code | grounded | 1 | passed | 5 | 51242 | 0.003472 |  |
| pi-tape-mismatch-code | grounded | 2 | passed | 9 | 102815 | 0.004655 |  |
| pi-tape-mismatch-code | grounded | 3 | passed | 7 | 71130 | 0.004395 |  |

评测要求结构化答案和准确源码引用；失败也可能是格式或引用不匹配，需要结合回放包复核。录制不足（inconclusive）不能解释为模型退化，也不能算断言失败。没有观察到失败到通过的变化时，不声称修复效果。负向控制为人工构造，不计入真实样本。成本按本地价目表（models.json）计算，是估算而不是账单。

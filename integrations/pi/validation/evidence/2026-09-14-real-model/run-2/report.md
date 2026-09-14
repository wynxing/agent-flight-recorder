# pi 代码调查回归验证

固定提交：b5332540bd702f789088aa90603cc7dd5d1084ed。模型：msee-gateway/deepseek-v4.1-flash。回归工具来源：snapshot。

baseline: 15 个已执行样本中 5 通过、10 未通过、0 无法判断、0 执行错误；可判断 15/15。未执行样本不进入分母。

grounded: 15 个已执行样本中 11 通过、4 未通过、0 无法判断、0 执行错误；可判断 15/15。未执行样本不进入分母。

## 批次预算

声明上限：{"max_cost_usd":2,"max_model_calls":400}。没有因预算停止：已用 178 次真实模型调用。已用成本约 $0.148015（估算）。

所有计划样本已执行。

| 任务 | Prompt | 样本 | 结论 | 真实模型调用 | token | 成本（估算） | 成因码 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| batch-budget-total-calls | record | 0 | failed | 3 | 42526 | 0.005415 |  |
| cause-precedence-blocked-vs-budget | record | 0 | passed | 6 | 61109 | 0.003759 |  |
| write-tool-gate-on-recorded-regress | record | 0 | failed | 4 | 31410 | 0.002714 |  |
| unknown-cost-does-not-stop | record | 0 | failed | 4 | 15813 | 0.001526 |  |
| pi-tape-mismatch-code | record | 0 | passed | 6 | 54692 | 0.003817 |  |
| batch-budget-total-calls | baseline | 1 | failed | 3 | 42004 | 0.005226 |  |
| batch-budget-total-calls | baseline | 2 | failed | 3 | 42960 | 0.005789 |  |
| batch-budget-total-calls | baseline | 3 | failed | 4 | 62295 | 0.005445 |  |
| batch-budget-total-calls | grounded | 1 | passed | 8 | 234417 | 0.012720 |  |
| batch-budget-total-calls | grounded | 2 | failed | 5 | 92269 | 0.006394 |  |
| batch-budget-total-calls | grounded | 3 | failed | 8 | 156102 | 0.006964 |  |
| cause-precedence-blocked-vs-budget | baseline | 1 | passed | 4 | 36624 | 0.003817 |  |
| cause-precedence-blocked-vs-budget | baseline | 2 | passed | 6 | 53944 | 0.003988 |  |
| cause-precedence-blocked-vs-budget | baseline | 3 | passed | 4 | 33176 | 0.003069 |  |
| cause-precedence-blocked-vs-budget | grounded | 1 | passed | 6 | 42014 | 0.003146 |  |
| cause-precedence-blocked-vs-budget | grounded | 2 | passed | 8 | 117994 | 0.005782 |  |
| cause-precedence-blocked-vs-budget | grounded | 3 | passed | 8 | 77592 | 0.004208 |  |
| write-tool-gate-on-recorded-regress | baseline | 1 | failed | 4 | 31311 | 0.002714 |  |
| write-tool-gate-on-recorded-regress | baseline | 2 | failed | 4 | 32008 | 0.003282 |  |
| write-tool-gate-on-recorded-regress | baseline | 3 | failed | 4 | 31577 | 0.002803 |  |
| write-tool-gate-on-recorded-regress | grounded | 1 | failed | 7 | 92308 | 0.004941 |  |
| write-tool-gate-on-recorded-regress | grounded | 2 | passed | 8 | 79228 | 0.003927 |  |
| write-tool-gate-on-recorded-regress | grounded | 3 | passed | 4 | 32542 | 0.002897 |  |
| unknown-cost-does-not-stop | baseline | 1 | passed | 2 | 6816 | 0.001432 |  |
| unknown-cost-does-not-stop | baseline | 2 | failed | 2 | 7956 | 0.002633 |  |
| unknown-cost-does-not-stop | baseline | 3 | failed | 4 | 15930 | 0.001614 |  |
| unknown-cost-does-not-stop | grounded | 1 | passed | 6 | 50333 | 0.003888 |  |
| unknown-cost-does-not-stop | grounded | 2 | failed | 7 | 118831 | 0.006727 |  |
| unknown-cost-does-not-stop | grounded | 3 | passed | 4 | 16343 | 0.001618 |  |
| pi-tape-mismatch-code | baseline | 1 | failed | 6 | 52407 | 0.003384 |  |
| pi-tape-mismatch-code | baseline | 2 | passed | 4 | 34862 | 0.003027 |  |
| pi-tape-mismatch-code | baseline | 3 | failed | 4 | 33403 | 0.002748 |  |
| pi-tape-mismatch-code | grounded | 1 | passed | 5 | 59283 | 0.004588 |  |
| pi-tape-mismatch-code | grounded | 2 | passed | 6 | 70972 | 0.006315 |  |
| pi-tape-mismatch-code | grounded | 3 | passed | 7 | 99775 | 0.005698 |  |

评测要求结构化答案和准确源码引用；失败也可能是格式或引用不匹配，需要结合回放包复核。录制不足（inconclusive）不能解释为模型退化，也不能算断言失败。没有观察到失败到通过的变化时，不声称修复效果。负向控制为人工构造，不计入真实样本。成本按本地价目表（models.json）计算，是估算而不是账单。

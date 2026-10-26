# 旧范围多届别冲突复审

日期：2026-10-26（Asia/Shanghai）；HEAD：`b3e83e5`，比较基线：`c3681c3`。

## 结论

43 个既有探针独立重跑通过；`tests/test_coverage_tasks.py` 22 passed。上一轮的三个原反例均修复，包括单届别旧数据冲突、未公布入口误判及相邻的咨询/真实投递邮箱选择。

本轮只剩一项已复现的阻断问题：旧数据同时有多个冲突届别时，只处理第一个，其他冲突届别仍被恢复为 confirmed。建议修复此项后结束本批针对性复审，再继续扩覆盖。

## [P1] 处理首个旧届别冲突后 break，漏掉其余届别

位置：`src/soe_radar/identity.py:187`–`:204`，`_merge_scope`。

循环按每届检查 confirmed/needs_review 冲突，但追加第一条 merge_conflict 待复核记录后立即 break。剩余届别仍按拼接后的 provenance 重算，继续受旧别名确认覆盖较晚撤回的问题影响。

最小场景：

1. 旧别名范围确认 2027、2028、2029。
2. 规范名范围也确认这三届，随后撤回 2027 和 2028；2029 保持确认。
3. 使用修复前已有的 provenance JSON 结构，记录没有 decided_at，保留旧 verified_at。
4. 合并企业身份。

合并前规范名有效范围为 `[2029]`。合并后实际变为 `[2028,2029]`；2028 覆盖报告返回来源 group。预期只有 `[2029]`，两个冲突届别均需复核。2029 仍能被监测的正例已通过，不能通过撤掉整行或清空全部届别掩盖问题。

修复要求：处理全部冲突届别，可逐届追加或一次记录完整的冲突 cohorts；不要在首个冲突后退出。保留逐项原件、审核记录及无冲突届别，补两个冲突届别与一个正常届别共存的回归测试。

## 验证与交付

- 九个既有复现脚本共 43 个断言，均退出码 0。
- `uv run pytest -q tests/test_coverage_tasks.py`：22 passed。
- 新增 `review_scope_multi_cohort_repro.py`：2 个错误范围断言失败、1 个无冲突届别正例通过，退出码 1；实际值见 `review_scope_multi_cohort_output.txt`。
- 已阅读全量交付记录的 179 passed、ruff check/format、构建成功，未重复全量执行。
- 58 个证据原件与 manifest 未在本轮变更，沿用此前独立校验的结果；本轮不做实时来源和全国覆盖验收。

本次行为检查仅用临时库和本地固定数据，无网络或模型调用。未修改生产代码、个人库、配置、既有报告或运行记录，未合并、未推送。原失败输出保留，修复后另交输出。

```bash
uv run python delivery/review_scope_multi_cohort_repro.py
uv run python delivery/review_scope_legacy_repro.py
uv run pytest -q tests/test_coverage_tasks.py
```

# 覆盖任务队列与候选来源

日期：2026-10-25。实现 `coverage_tasks`、`source_candidates`、`source_employer_scopes` 及对应 CLI；本轮只完成离线小闭环，不宣称新增全国覆盖。

## 设计边界

- `coverage_tasks` 是可恢复、幂等的待办队列。任务键包含范围、动作和周期，重复规划不会重复创建；不同届别使用不同周期键，不覆盖往届任务。
- 任务复用 `leases`、来源分页/详情上限、限速和退避，不新建竞争调度器。失败任务保留进度和证据，达到重试上限后进入 `blocked`。
- `coverage-plan` 只读数据库和配置，不发网络请求；`coverage-run --batch N` 有明确任务和请求边界。
- `source_candidates` 默认状态是 `lead`，不计入有效监测。搜索结果、动态壳、HTTP 200 或验证码不能自动启用。
- `source_employer_scopes` 记录共享入口与实际企业范围，必须带官方证据 URL、原文、届别/批次和状态；集团入口不自动覆盖全部子公司。
- 中央/地方名录继续保留版本口径；全国分母仍为未知，任务数量不代表覆盖完成。

## 动作

- `refresh_registry`：读取已配置名录的全部分页并保存证据；部分失败不替换 active snapshot。
- `discover_registry`：从主管部门入口寻找名录候选链接，只写待人工核对证据。
- `discover_entry`：从企业官网/官方档案页寻找招聘入口，创建候选来源。
- `verify_candidate`：验证候选的列表、详情、报名入口和附件，写候选状态与证据；不自动启用。

## 当前离线结果

个人库执行 `coverage-plan` 后生成 476 个任务：28 个名录任务、448 个入口任务；`coverage-plan` 重复执行返回 `created=0`。`coverage-run --batch 1` 处理了无主管部门入口的中央金融任务，状态 `needs_review`，下一步为人工补充入口。当前网络不可用，未发起新的真实采集，因此不报告新增覆盖。

## 测试

`tests/test_coverage_tasks.py` 覆盖：规划幂等、候选不计入覆盖、成功/失败退避、运行中断恢复、共享入口范围去重与状态。

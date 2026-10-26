# CQ 修复跟进复审

日期：2026-10-26（Asia/Shanghai）。审查 HEAD：`0671296`；差异基线：`c3d1be3`。

## 结论

上一轮 8 个 CQ 探针现在全部通过，原有 14 个历史探针也独立重新执行通过。但 CQ1、CQ4 的修复仍有未覆盖的边界：本轮新增 6 个反例断言，覆盖以下 4 类问题，均未满足预期，暂不通过本批集成审查。

这里只做代码与离线数据行为复审，没有再次跑真实采集。所有新增反例使用临时数据库及模拟网页；未修改生产代码、个人库、配置、既有验证记录或原审查文件，未合并、未推送。

## F1 [P1] 两端已有同周期任务时，身份合并仍触发唯一键错误

位置：`src/soe_radar/identity.py:106`–`:109`，`_remap_task`。

代码先把旧任务的 scope_id/task_key 改成目标值，再查询目标任务是否冲突。SQLAlchemy 在该查询前自动 flush，尚未执行冲突合并就触发 `UNIQUE constraint failed: coverage_tasks.task_key`，整次身份合并回滚。

复现：规范名和括号别名企业各有一条 `discover_entry / cohort:2027` 待办，调用 `merge_alias_employers(..., apply=True)`。预期合并企业与任务并保留审计，实际报上述错误。上一轮测试只检查新表外键引用，以及同源候选/范围合并，未包含两端已有任务的场景。

修复要求：在改变唯一键前计算目标键并查询冲突；先按状态、证据及运行所有权规则合并/删除，或使用受控的 no_autoflush 和明确写入顺序。不能只吞掉异常或删除全部任务。需补企业任务和候选验证任务两类冲突、不同周期不合并的回归测试。

## F2 [P1] 范围合并会删除已有证据原文

位置：`src/soe_radar/identity.py:161`–`:163`，`_merge_scope`。

`value not in existing.quote` 用旧全文过滤候选。第一项就是 existing.quote，自然永远被过滤；两端原文相同时，两项都被过滤，最终 quote 变成空字符串。两端不同时也只留下后者，原文丢失。随后删除重复范围行，原数据没有另一条记录可供追溯。

复现：两端同源同批次、confirmed、相同原文“示例集团参加2027校园招聘，采用统一报名入口。”，合并后原文实际为 `""`。

修复要求：对完整证据记录去重，至少保留各自 URL、原文、届别、审核状态和核验时间的绑定关系；不能用单个 URL 配拼接后失去来源对应关系的文本替代审计。相同原文应保留一份，不同原文和 URL 应都可复核。

## F3 [P1] 合并范围将待核验届别提升成已确认监测

位置：`src/soe_radar/identity.py:164`–`:168`；影响 `coverage_report` 的 confirmed 范围读取。

合并时无条件取 cohorts 并集，同时保留/提升整行 confirmed 状态。目标范围为 confirmed 的 2027 届，重复范围为 needs_review 的 2028 届、同源同模式同批次描述时，合并结果是 confirmed `[2027,2028]`。2028 的待核验证据因此变成已确认覆盖，仍使用旧行的 2027 官方证据 URL。

本轮同时验证数据库结果和报告结果：合并后 cohorts 实际为 `[2027,2028]`；把该来源的 exact employer 改为另一个主体以排除集团自身精确匹配，查询 2028 届报告仍返回共享入口 `group`，预期不应出现该待确认映射。

修复要求：按证据、届别/批次和审核状态分开保留，或用分项 provenance 计算明确已确认的范围；不能让 confirmed 的状态覆盖另一个 needs_review 范围。补 confirmed+needs_review 的双向合并、不同届别、相同/不同批次和原本已降级记录的测试。未确认范围仍应可见，但不计入有效监测。

## F4 [P2] 仅出现“报名”字样仍等于报名路径已确认

位置：`src/soe_radar/coverage_tasks.py:491`–`:498`，候选 application 阶段。

新增阶段只通过 `(报名|投递|网申|申请)` 匹配判断 found，没有确认可用的报名方式/入口或处理否定、将来时。公告明确写“报名方式另行通知，暂未公布报名入口”，实际 application=found、候选 status=verified。

复现已配置官方导航身份线索、列表和详情可读、无附件，模拟上述招聘预告。预期入口未确认，保留 readable/application=needs_review；实际两项分别为 verified/found。

修复要求：需要具体、可定位的报名路径证据。线上入口记录 URL 及必要的可用性验证，线下/邮件报名记录明确地址/邮箱和操作语义；“另行通知”“暂未公布”“仅为预告”等不能通过。明确无需报名要与未提供入口分开，也不应把招聘活动的报名关闭自动等同于整个长期来源失效。入口、当届活动和截止仍分别验收。

## 独立验证与实际数据

- 四个历史复现脚本：全部退出码 0，14 个旧探针通过。
- `uv run python delivery/review_coverage_queue_repro.py`：8 个 CQ 探针全部通过，退出码 0。
- `uv run pytest -q tests/test_coverage_tasks.py`：14 passed。
- `uv run python delivery/review_coverage_queue_followup_repro.py`：新增 6 个断言均未满足预期，退出码 1；实际值见 `review_coverage_queue_followup_output.txt`。
- 已阅读 `delivery/test-output.txt` 中 171 passed、ruff 和构建成功记录，未把该记录表述成本轮重新执行的全量检查。
- 只读个人库：迁移版本 0010；候选 241 条，其中 blocked 174、lead 57、needs_review 4、readable 6、verified 0，与交付一致。
- `allow_paid=false`、月度预算 0，模型调用仍为 2 条历史记录；本轮未调用模型。
- 58 个原件及其 manifest 不在本轮生产差异中，沿用上一轮已经独立核验的哈希结果；本轮没有声称重新验证实时网站。

## 交回修复

保留本报告、脚本和原失败输出，另交修复后的输出。先处理 F1–F3 的事务和证据问题，再修 F4 的报名路径判断。无需通过更多联网批次来证明这些离线反例修复；修复通过后再继续扩覆盖。

```bash
uv run python delivery/review_coverage_queue_followup_repro.py
uv run python delivery/review_coverage_queue_repro.py
uv run pytest -q tests/test_coverage_tasks.py
```

全国名录、所属单位、入口和准确率的验收口径保持原约束，当前审查不增加覆盖成绩。

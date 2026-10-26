# 覆盖任务管理增量复审

审查日期：2026-10-26，Asia/Shanghai。审查 HEAD：`c3d1be3`，基线：`8260c4d`。

## 结论

本轮新增功能暂不通过集成审查。已离线复现 6 类问题（8 个失败探针），其中包括新外键导致身份合并回归、长任务租约失效后重复执行、共享入口映射未接入实际覆盖计算。此前 R1–U1 的通过结论不撤销；旧探针不能证明新增任务系统正确。

本次只读个人库；所有行为探针使用临时数据库及模拟网页，无网络或模型调用。未改生产代码、个人配置、数据库或已有验证记录，未合并、未推送。新增本报告及复现脚本、输出。

## CQ1 [P1] 新外键使企业别名合并失败

位置：`src/soe_radar/models.py` 的 SourceCandidate.employer_id、SourceEmployerScope.employer_id；`src/soe_radar/identity.py:187` 删除重复企业前的迁移逻辑。

新增两张表引用 Employer，但 `merge_alias_employers` 只迁移旧表关联。只要待合并的重复企业拥有候选来源或共享入口映射，删除时即触发 `IntegrityError: FOREIGN KEY constraint failed`，整次事务回滚。两种引用已分别独立复现。

修复要求：身份合并需同时迁移/合并新关联，处理候选唯一键、共享范围唯一约束及证据冲突，保存审核历史；CoverageTask 的 employer scope_id、候选参与企业数组等逻辑引用也要审计，不能留下指向已删除 ID 的待办。补有候选、有范围映射、两端已有同源记录的回归测试。

## CQ2 [P1] 覆盖租约不续期，过期后重复执行已完成任务

位置：`src/soe_radar/coverage_tasks.py:526`、`:534`、`:539`。

`coverage-run` 只在开始取得 900 秒租约，没有类似 sync 的续约或所有权丢失处理。批次全部任务预先取到内存，每次开始执行又无条件改成 running。网络超时、重试和附件读取可使批次超过 15 分钟；另一个进程取得过期租约后，会执行首个进程尚未处理的 pending 任务，首个进程随后照着旧列表再次执行它。

复现中直接将租约时间设置为过去，模拟跨过 900 秒：第二个运行完成任务二后，原运行仍再次执行任务二，调用次数为 2，预期为 1。此探针模拟过期条件，没有实际等待 15 分钟，也不依赖真实网络。

修复要求：执行期间续租，丢失所有权时停止领取新任务；以原子条件领取任务并绑定运行所有者，执行前不得覆盖其他进程已完成的状态。中断恢复也应根据运行/租约归属处理，不能仅依赖固定一小时 updated_at 阈值。

## CQ3 [P1] 共享入口映射只有存储，没有参与覆盖和故障传播

位置：`src/soe_radar/coverage.py:897`；`src/soe_radar/coverage_tasks.py:252`。

SourceEmployerScope 目前只有写入逻辑，没有被 coverage_report、入口任务规划或交付覆盖计算读取。给某子公司加入明确的 2027 秋招 confirmed 集团来源映射后，报告依旧 sources=[]、missing_entry；把集团来源改为 error 后，该企业仍是 missing_entry，不显示监测受阻。

这使本阶段“用共享入口代替逐企采集”的核心行为没有落地。当前个人库 7 条映射均为集团自身，原有 source.employer 匹配已经能覆盖它们，尚未验证实际的子公司场景。

修复要求：将有效 confirmed 映射按届别、批次、模式及来源启用状态接入覆盖报告和任务规划；共享来源失效/过期时传播 blocked/stale，保留历史活动。未确认、过届、聚合发现模式不得自动等同于企业统招监测，也不得继承未明确列出的其他子公司。

## CQ4 [P2] verified 仅取决于一个详情可读，报名入口和必要附件未验证

位置：`src/soe_radar/coverage_tasks.py:438`–`:450`。

判断只看 links、detail 非空且无 error；没有报名入口检查，也不使用附件状态。模拟公告写明“报名方式和截止日期详见附件”，必要 PDF 读取失败、没有报名入口，候选仍得到 verified。

报告末尾将 verified 解释成“列表/详情可读”，但这低于上一轮约定的验证阶段，也低于 coverage-plan 中“验证列表、详情、报名入口和附件”的任务说明。目前 64 条 verified 候选的附件记录合计有 265 个 needs_review；这不证明每份都是必要附件，却足以说明 verified 不能解读成完整验证完成。

修复要求：分别记录官方身份、列表、详情、报名方式/入口、相关附件的验证结果；报名方式明确不适用或无附件须有相应状态，不能要求所有站点装饰都解析。必要附件失败或报名路径无法确认时保留待核对。若保留“可读”阶段，使用单独名称，与可提交启用审核的完整 verified 分开。复查已有 64 条，不能仅批量改名后宣称补齐。

## CQ5 [P2] blocked 任务没有可用的重试/重排路径

位置：`src/soe_radar/coverage_tasks.py:531`、`:560`；候选规划条件 `:177`。

runner 虽为 blocked 写入未来 due_at，却只领取 pending；候选规划也只扫描 lead/identity_checked。到期 blocked 任务不会执行，重复 plan 不会解锁，CLI 没有受控重排命令。网络恢复后继续运行 `coverage-run --batch 20` 无法重试这些既有失败。本次临时库设置一个已到期 blocked 任务，processed 实际为 0。个人库现有 188 个 blocked，其中 174 个候选 blocked。

修复要求：区分暂时网络失败、需人工处理的登录/验证码、明确拒绝等状态；提供有审计记录的定向 requeue/复查入口，暂时错误遵守次数/退避上限。保持登录与验证码限制，不应无限重试或绕过。报告中明确下一批究竟会重试哪些任务。

## CQ6 [P2] 规划的优先级与新增数限制没有覆盖候选阶段

位置：`src/soe_radar/coverage_tasks.py:179`；名录阶段也未应用 limit。

只对企业入口循环应用 limit，候选验证循环同时忽略 priorities 和 limit。因此 `plan_tasks(priorities=['P0'], limit=0)` 仍创建一个 P1 验证任务。实际有大量候选时，只想规划 P0 或限制批量新增的操作仍会扩大队列，破坏可控批次。

修复要求：所有任务类型共用一致的优先级过滤和全局新增上限；已存在任务的计数与新增上限区分。测试含仅候选、名录+企业+候选混合、limit=0/1 和 P0/P1 过滤。

## 已核对的事实与验证边界

- 个人库只读核对：717 个任务（315 pending、188 blocked、92 needs_review、122 done）；241 个候选（64 verified、174 blocked、3 needs_review）。与当前交付摘要一致。
- 配置 24 个来源；个人库未合并校招 125 条、2027 届 66 条、其中已核验且不需审核 7 条。计数核对不代替逐条真实性、届别和日期准确率验收。
- `allow_paid=false`，月度额外预算 0，模型调用仍为 2 条历史记录。
- 独立执行 `uv run pytest -q tests/test_coverage_tasks.py tests/test_fetching.py`：28 passed。
- 独立执行新增复现脚本：8 个探针不满足预期，退出码 1，具体实际值见输出文件。
- 在 delivery 目录执行 `shasum -a 256 -c evidence/SHA256SUMS`：58 个文件全部 OK。首次从 evidence 子目录执行的路径错误已更正；不是证据损坏。
- 已阅读 delivery/test-output.txt 中 161 passed、lint/format/build 的记录，未重复全量执行；通过已有测试不覆盖上述新增反例。
- 本轮未发起实时采集，不能凭本次检查确认网络恢复、24 个来源当前稳定，或新增 26 条校招逐条准确。全国分母继续未知，150 份人工标注与七天稳定性验收未完成。

交付文字另需统一：REPORT.md §1.1 仍写“新增 4 条 source_employer_scopes”，当前库实际 7 条；第 2 节声明保留 10 月 23 日基线，却混入最新 125/66 等数字，并同时出现 639/598 家企业的不同口径。应按各批次日期、基线、截止时间拆开，避免把最近两批零新增解读为整个阶段零新增。

## 复现与交回修复

```bash
uv run python delivery/review_coverage_queue_repro.py
uv run pytest -q tests/test_coverage_tasks.py tests/test_fetching.py
cd delivery
shasum -a 256 -c evidence/SHA256SUMS
```

复现脚本每个反例使用独立临时库，不读取个人模型凭证、不连接外网。建议先修 CQ1–CQ3，再补 CQ4–CQ6 并复查已有候选状态，最后恢复小批量扩覆盖。交回时保留本报告、脚本和原失败输出，另交修复后的输出与真实净新增证据。

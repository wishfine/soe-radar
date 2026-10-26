# 复审问题修复说明（R1–R8）

复审对象：`feat/nationwide-coverage-20261023`。本轮修复未合并、未推送；原 `delivery/REVIEW.md` 与 `delivery/review_repro.py` 保留。

## 修复结果

| 编号 | 修复 | 复现/证据 |
| --- | --- | --- |
| R1 | `chrome()` 不再仅凭 `/images/`、`banner`、`share` 排除图片；只有明确 logo/icon/二维码/小图才忽略。新增 `.zwxl-content` 正文容器，避免把站点头部图片当正文。 | 无 alt 的 1000×2000 正文海报保留；招聘 banner 保留；二维码/logo 忽略。`review_repro.py` 通过。 |
| R2 | `refresh_registries` 部分页失败时不切换 active snapshot，保留上次完整成员；失败候选版本仅作历史。 | 两页两家企业，第二页 503 后仍为 2 家；`review_repro.py` 通过。 |
| R3 | 解析时拆分 `source_name`/规范名/别名；按规范名或已确认别名复用 Employer；新增 `employer-merge-aliases` 合并既有重复，活动、个人状态、关系、审核随 Opportunity 保留。 | 5 组重复已合并；dry-run 为空；`review_repro.py` 中括号别名只产生 1 个企业。 |
| R4 | text 适配器同时读取链接与去掉链接后的纯文本，去重合并。 | 1 链接 + 2 纯文本 = 3 家；`review_repro.py` 通过。 |
| R5 | 逐页解析，每条企业保存 `page_url/page_sha256/page_path`；交付导出按企业实际页绑定证据。 | `registry_page_check.csv` 428 条有原件企业，名称不在绑定页内 0 条；`review_repro.py` 通过。 |
| R6 | snapshot 身份摘要与文件内容 SHA-256 分离；单页、CSV、人工 HTML 三条路径均记录实际文件哈希。 | 单页与 CSV 测试通过；58 个交付文件 `shasum -c` 全部 OK。 |
| R7 | “南网数字”关系降为 `needs_review`，`relationships.jsonl` 只导出 confirmed；缺口台账增加“关系待复核”。 | confirmed 2 条，needs_review 1 条；见 `relationship_review.csv`。 |
| R8 | “入口未找到”按企业范围和可用来源判断，不再要求必须有主管口径；别名/禁用来源单独判断。 | 3 家南网子公司均进入逐企入口缺口；`gaps.csv` 中“入口未找到”448 条。 |

## 第二轮复审（S1–S3）

- S1：修复 `_save_entries` 中循环变量覆盖整页 `digest` 的问题；整页摘要现在包含全部页面 URL/内容和 `PARSER_VERSION`，复用 snapshot 时校验 entries 指纹。新增 `registry-repair-snapshots --apply`，个人库历史快照已重算 digest 并修复逐条页绑定。非末页新增企业会创建新版本，active/stored/reported 均为 3。
- S2：合并别名身份时迁移/校验 `parent_id` 与 confirmed Relationship；空值继承，相同上级保留，不同上级保留 target 并将另一条降为 `needs_review`；防止自环/循环。
- S3：关系行先合并证据并 `flush`，再删除重复 Employer；新增迁移 0008 `relationships.provenance`，保留不同 URL/原文/日期/状态。两端同父关系合并不再触发 FK 错误，confirmed 状态保留。

复现：

```text
uv run python delivery/review_round2_repro.py
changed_nonfinal_page_creates_updated_snapshot: true
merge_preserves_confirmed_parent_pointer: true
merge_existing_relationship_edges: no IntegrityError
```

个人库迁移前备份：`backups/before-review-round2-20261024.tar.gz`。

## 第三轮复审（T1–T2）

- T1：`add_relationship` 的显式 `status` 现在直接决定当前审核状态；旧 confirmed 证据保留在 `provenance`，但显式 `needs_review` 会清除 `parent_id`，不会静默忽略。已补 confirmed → needs_review → confirmed 往返测试和交付导出校验。
- T2：`repair_registry_snapshots` 现在构造新的 entries 列表并赋值，显式触发 JSON 列变更；页绑定检查独立于 digest 修复，已标记修过的快照仍检查绑定；无法唯一定位的条目写 `page_binding=needs_review`。修复幂等，个人库已执行 `registry-repair-snapshots --apply`。

复现：

```text
uv run python delivery/review_round3_repro.py
explicit_relationship_downgrade: true
repaired_page_binding_persists_after_commit: true
```

## T2 跟进（U1）

- repair 不再仅凭 page_sha256/page_url 存在就信任旧绑定；必须能在该页定位规范名/source_name。若旧页不含该企业，继续搜索其他页，只有唯一匹配才重绑。
- 无法唯一定位时，清除当前 `page_url/page_sha256/page_path`，旧值移入 `page_binding_candidates`，写 `page_binding=needs_review` 和原因。
- delivery 尊重 `page_binding=needs_review`，证据页 URL/路径/SHA 全部留空，不回退第一页；新增 `页绑定状态`、`页绑定说明` 字段和“证据页待复核”缺口。
- 个人库执行 repair 后 dry-run 为空；正常 active 快照未受影响。

复现：

```text
uv run python delivery/review_t2_evidence_repro.py
known_hash_but_wrong_enterprise_page: true
unresolved_binding_does_not_export_guessed_evidence: true
```

## 2026-10-25 扩覆盖实做

网络恢复后实际执行任务队列和候选验证，新增 7 个官方来源：中国航天科工、中国电子科技、中国华能、中国融通、中国节能、中国医药集团、中国冶金地质总局。

- 相比扩覆盖前备份：新增 26 条校招、18 条 2027 届。
- 当前累计：24 个来源、125 条校招、66 条 2027 届、7 条已核验 2027 届。
- 候选来源 241 条：0 verified、6 readable、4 needs_review、57 lead、174 blocked；旧 verified 已降为 readable 并进入复查，未把“列表/详情可读”当作完整验证。
- 任务队列 717 条：28 名录、448 入口、241 候选验证；372 pending、188 blocked、92 needs_review、65 done。
- 新增 7 条 `source_employer_scopes`，只登记集团自身范围，不自动覆盖子公司。
- 入口缺口从 448 降到 441。

## 覆盖任务管理复审（CQ1–CQ6）

- CQ1：身份合并现在迁移/合并 `SourceCandidate`、`SourceEmployerScope`、候选 `scope_employer_ids` 和 `CoverageTask` 逻辑引用；同 URL/同源范围冲突时合并证据后删除重复行，再 flush 删除 Employer。候选与范围外键场景均通过。
- CQ2：`coverage-run` 每任务前续租；用条件 UPDATE 原子领取；丢失租约后停止领取且不覆盖其他运行结果。中断任务改为 `coverage-recover` 显式审计恢复，不再按固定小时自动清扫。
- CQ3：`coverage_report` 读取 confirmed、有效届别、启用来源、`group_umbrella/independent` 的 `SourceEmployerScope`；共享来源失败会传播 blocked；needs_review/aggregator 不计入监测。规划任务也会跳过已有有效范围映射的企业。
- CQ4：候选验证拆分为 identity/list/detail/application/attachments；只有报名路径确认且必要附件未失败才 `verified`，列表/详情可读但缺报名/附件为 `readable`，必要附件失败为 `needs_review`。旧 64 条 verified 已降为 readable 并重新排队复查；不能把“可读”当完整验证。
- CQ5：到期 blocked 任务在 attempts<5 时可重试；新增 `coverage-requeue` 受控重排 blocked/needs_review，重置次数并写审计；`coverage-recover` 处理中断 running。
- CQ6：`coverage-plan` 的 priorities/limit 统一作用于名录、企业和候选阶段；`limit=0` 不再创建 P1 候选任务。

复现：

```text
uv run python delivery/review_coverage_queue_repro.py
8 个探针全部 passes=true
```

旧 14 个探针继续通过；`uv run pytest -q` 171 passed；58 个证据文件哈希全部 OK。

## CQ 跟进（F1–F4）

- F1：`_remap_task` 先计算目标 task_key、查询冲突并合并证据/状态/运行归属，再修改唯一键；同周期企业任务和候选任务冲突不再触发唯一键错误，不同周期保持独立。
- F2：`SourceEmployerScope` 新增 `provenance` 审计列（迁移 0011）；范围合并按完整证据记录去重，保留各自 URL、原文、届别、批次、状态和核验时间，不再用拼接文本覆盖原文。
- F3：范围合并按 confirmed/needs_review 分项计算；`cohorts` 只取 confirmed 证据的届别，未确认届别保留在 provenance 中但不计入监测。2027 confirmed + 2028 needs_review 合并后为 `[2027]`，2028 报告不返回该共享入口。
- F4：报名阶段要求可定位的 URL/邮箱/具体报名系统/地址；“另行通知、暂未公布、仅为预告”判为 needs_review；明确无需报名与未提供入口分开；入口、活动、截止仍分别验收。

复现：

```text
uv run python delivery/review_coverage_queue_followup_repro.py
6 个探针全部 passes=true
```

旧 14 个探针 + CQ 8 个探针继续通过；`uv run pytest -q` 174 passed；58 个证据文件哈希全部 OK。

## 范围审计与报名路径复审（G1–G2）

- G1：`recompute_scope` 改为按届别记录“最新一次审核决定”，当前状态取最新决定；历史 confirmed 仍保留在 provenance，但不会自动重新激活已撤回范围。支持 confirmed → needs_review → confirmed 往返；报告和规划读取当前状态；合并保留各证据顺序，不恢复撤回决定。
- G2：报名路径要求“动作 + 具体目标”绑定；咨询邮箱、公司介绍 URL、孤立“报名系统”不通过；只有“简历发送至某邮箱”“通过某 URL 提交申请”“通过网申系统/现场地址报名”等才判 found，并保存完整上下文原文。咨询/介绍链接保留 readable，不生成截止。

复现：

```text
uv run python delivery/review_scope_audit_repro.py
6 个探针全部 passes=true
```

累计 34 个探针（14 历史 + 8 CQ + 6 F + 6 G）全部通过；`uv run pytest -q` 176 passed；58 个证据文件哈希全部 OK。

## 报告口径更正

- 本轮净新增校招机会改为 **28 条**（55→83），其中 2027 届 12 条；`activity_diff.json` 可复核 ID 差集。此前“33 条”是未排除山西结果公示前的旧口径。
- 当前名录快照 456 条 = 本轮有原件的 12 个范围 429 条 + 历史金融 26 条 + 补充主体 1 条。
- 国务院国资委页面：HTTPS 证书已于 2023-11-10 过期且缺中间证书；改用同域 HTTP 官方页面，并与 `curl -k` HTTPS 抓取逐条比对 100 个企业名称一致。该快照标记 `tls_verified=false`，不自动提升企业性质核验；未修改系统证书、代理、TUN 或分流。
- 关系：confirmed 2 条；南网数字 1 条 `needs_review`，不再计入 confirmed。

## 复现材料

```text
uv run python delivery/review_repro.py
uv run python delivery/activity_diff.py
uv run python delivery/review_exports.py
```

输出：

- `delivery/review_repro_output.txt`：7 个审查探针全部通过。
- `delivery/activity_diff.json`：新增 opportunity ID 128 条，其中未合并校招 28 条、2027 届 12 条，缺失必填字段 0 条。
- `delivery/registry_page_check.csv`：428 条逐企证据页检查，企业名不在页内 0 条。
- `delivery/relationship_review.csv`：3 条关系，confirmed 2、needs_review 1。

## 测试

```text
uv run pytest -q                 → 见 delivery/test-output.txt
ruff check src tests             → All checks passed
ruff format --check src tests    → 45 files already formatted
uv build                          → sdist + wheel 成功
```

测试通过不代表全国覆盖完成；22 个省级地区、兵团、地市、区县、所属单位和 448 家无招聘入口企业仍在 `gaps.csv` 中。

# 第二轮复审：仍暂不建议合并

日期：2026-10-24（Asia/Shanghai）。审查 HEAD：e02a127，重点比较 ceebe10 之后的修复。原 REVIEW.md、review_repro.py 保留，未修改生产代码、原始证据和个人数据库，未合并、未推送。

## 已确认的修复

重新运行原来的 7 个离线探针，全部 passes=true，包含 58 个证据文件的哈希和企业证据页名称定位。R1 的原海报场景、R2 的原部分失败场景、R3 的原括号别名场景、R4 的原混合文本场景、R5/R6 的原证据/哈希场景均通过。

读取当前个人库确认：关系为 2 条 confirmed + 1 条 needs_review，待复核那条 parent_id 为空；SASAC 来源 provenance.tls_verified=false，93 个以该名录为性质依据的主体保持 unknown。个人状态 1 条、审核 7 条、模型调用仍为 2 条。以上确认不把 HTTP 与关闭证书验证的 HTTPS 互相对比当作已认证来源身份。

147 项测试通过记录可以支持原有测试场景；下面是额外检查多次更新与关系迁移后复现的新问题。

## S1 · P1：快照整体 digest 被最后一个企业的页面 digest 覆盖

位置：src/soe_radar/coverage.py:419–420、442–450、458–460。

_save_entries 原先已经计算了整个多页版本的 digest，随后在为每个 entry 绑定页面的循环里再次赋值 digest。查询和保存 RegistrySnapshot 时使用的是最后一个条目所在页面的内容哈希，整体版本摘要已经丢失。

独立复现：先保存两页、两家公司；保持排序最后一个企业所在页面不变，只在另一页增加一家企业。第二次返回 count=3，但复用旧 snapshot ID，数据库 snapshot.entries 与 coverage_report 仍只有 2 家。新 Employer 虽已创建，却没有当前名录 membership；导出的条目与来源原件也可能不一致。类似地，部分失败保存的候选与完整版本可能碰撞，导致旧版本的 provenance 被修改。

修复要求：snapshot_digest 和 page_digest 使用不同变量，版本身份必须包含全部页面 URL、内容及必要解析版本。相同身份复用时确保内容和解析条目一致，不能仅更新 provenance 却保留已过时 entries。对第一/中间/最后一页分别变更、部分失败后重试、重启与重复运行增加测试；检查个人库已有快照是否受此错误影响，再离线修复元数据与条目绑定。

## S2 · P1：合并子公司身份后，已确认母公司字段丢失

位置：src/soe_radar/identity.py:51–63、88–89、110。

合并函数转移引用 duplicate 的子公司和 Relationship 外键，但不迁移 duplicate 自己的 parent_id。保留目标是规范名、没有母公司字段，而重复项具有已确认母公司时，合并结果为 Relationship 已指向目标且仍 confirmed，目标 Employer.parent_id 却为 None。

独立复现已确认这个不一致。网页集团筛选使用 Employer.parent_id，因此已确认的子公司会从集团筛选中消失，交付中的层级、母公司和入口缺口也可能改变。

修复要求：先合并/校验目标和重复项各自的上级关系，维护 confirmed Relationship 与 parent_id 一致；有冲突时保留证据并进入待复核，不任意覆盖。测试空值继承、相同上级、不同上级及防止自环/循环。

## S3 · P2：两份企业身份已有同一父子关系时合并报外键错误

位置：src/soe_radar/identity.py:78–86、110。

目标和重复项分别已有相同母公司的关系行时，函数直接删除重复关系并删除 Employer。临时 SQLite 开启外键后复现为 IntegrityError: FOREIGN KEY constraint failed，合并事务整体回滚。当前测试只覆盖没有关系行的活动/个人状态，未覆盖这条正常的去重路径。

修复要求：制定重复关系的状态和证据合并策略，保留不同原文/日期的审计信息，明确迁移、flush 与删除顺序。不能用删除已确认关系、保留 needs_review 关系来简单解决唯一键冲突。回归测试至少覆盖两端同父关系，以及 confirmed 与 needs_review 并存。

## 复现与下一步

`uv run python delivery/review_round2_repro.py` 仅使用临时数据库、不联网、不调用模型，当前三个探针均失败，退出码 1。代码修复前无需继续扩源。

修复后保留原 7 个通过的探针，同时跑新增 3 个场景；重新验证名录多次更新、关系合并和个人数据关联，并重新导出交付包。全国覆盖未完成仍属于已披露范围，不因本次修复通过就改变该结论。

# 第三轮复审：S1–S3 原场景通过，剩余两项 P2

审查日期：2026-10-24（Asia/Shanghai）。HEAD：04df235，对比 e02a127。没有修改生产代码或个人数据库，没有合并、推送；原审查材料保留。

## 已确认通过

独立执行原 7 个探针与第二轮 3 个探针，全部通过。58 个交付证据文件哈希匹配，逐企证据页名称定位未再出现旧问题。只读核查当前个人库的所有 active 名录快照，其摘要均与全部页面 URL、内容及当前 PARSER_VERSION 重算值一致。

新的同父关系合并逻辑保留证据 provenance，并在删除重复企业前 flush，原 S2/S3 场景已通过。151 项测试通过记录已阅读；不把这些测试日志当作以下新边界已覆盖。

## T1 · P2：显式撤销关系确认被 confirmed 优先策略阻止

位置：src/soe_radar/relationships.py:79–83。

add_relationship 现在将已有 confirmed 状态强制保留，即使调用方明确传入 needs_review。独立复现：先确认母子关系，再按同样 CLI/API 路径传入 status=needs_review；提交后仍是 confirmed，child.parent_id 仍指向母公司。这使上轮 R7 使用的“发现证据不足后降为待复核”操作失效，交付导出仍会把该关系计入 confirmed。

修复要求：区分“合并两份证据时选择状态”和“人工显式修改当前审核状态”。保留旧 confirmed 证据及历史，但本次明确降级必须将当前状态改为 needs_review，并更新/清除 parent_id。若产品需要单独撤销命令，应提供实际可执行入口并禁止 --status 静默忽略输入。补充 confirmed → needs_review → confirmed 往返测试及交付导出校验。

## T2 · P2：历史条目的 page_path 修复未持久化

位置：src/soe_radar/coverage.py:640–646（RegistrySnapshot.entries 为普通 JSON 列）。

repair_registry_snapshots 在 entries 列表内部原地修改字典，但未重新赋值 entries 或标记该列变更。SQLAlchemy 不会追踪该普通 JSON 列内部的原地修改；提交只保存了 digest 和 provenance。

独立复现：在临时库制造 legacy digest 与错误 page_path，执行 repair_registry_snapshots(apply=True)。重新打开 session 后，摘要已更新，条目路径仍为 registries/broken.html；真实页路径存在于 provenance.pages。且 digest_repaired_from 已写入，下次修复会直接跳过该快照。当前 active 摘要检查通过不能证明所有历史条目页绑定已经修复。

修复要求：构造新的 entries 列表并赋值/显式标记变更，修复后用新 session 重读验证；修复候选应独立检查页绑定，不因 digest 已正确或已标记修过就跳过。page_url、page_sha256、page_path 要按真实原件关联，无法唯一定位的条目保留待复核。为已经运行过修复命令的数据提供幂等复修路径。

## 复现

`uv run python delivery/review_round3_repro.py` 只使用临时数据库，不联网、不调用模型，不接触个人库。当前两个探针均失败并返回退出码 1。

建议修复 T1/T2 后保留前两轮 10 个通过场景，再重跑新增两个场景和相应测试；无需先扩大采集范围。暂不建议合并，但本轮没有再复现此前 S1–S3 的原阻断问题。全国覆盖缺口的结论仍保持原报告口径。

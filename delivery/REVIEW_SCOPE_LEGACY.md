# 审核时间与报名局部语义的升级复审

日期：2026-10-26（Asia/Shanghai）；HEAD：`c3681c3`，比较基线：`96f6593`。

## 结论

40 个既有探针独立重新执行通过，相关单测 20 passed。新创建记录按 decided_at 排序，修复了上一轮的普通合并时序反例。

仍有两个根因需要处理：G1 对无 decided_at 的真实旧格式记录缺少安全的升级处理；G2 的固定上下文窗口与判定优先级仍产生错误通过和错误弃答。本轮 3 个新增断言均未满足预期，暂不建议集成本批。

## G1 跟进 [P1] 旧 provenance 缺少 decided_at 时，合并仍恢复已撤回范围

位置：`src/soe_radar/coverage_tasks.py:259`–`:279`；`identity.py` 的 `_merge_scope`。

旧版 0011 的 provenance 记录本身没有 decided_at。当前 scope_evidence 只在整段 provenance 为空时构造包含 decided_at 的记录；对已有的旧记录原样返回。recompute_scope 遇到缺失时间就使用空字符串，再以数组索引决定次序，旧数据因此继续沿用合并拼接顺序。

复现：按上轮合并例创建别名旧确认、规范名较新确认与最新撤回，再去除 decided_at，得到修复前真实存在的 JSON 结构（保留旧 verified_at）。合并前规范名为 needs_review，合并后变为 confirmed，旧问题重现。

这影响已有库升级，不能只用所有记录都由新代码创建的单测验收。建议对旧记录提供显式归一化/迁移：可可靠推导的时间写明依据，无法确定的冲突维持 needs_review，不得把缺失时间当作“更早”从而恢复确认，也不得伪造审核时间。补升级 fixture，包含旧确认、旧撤回及新旧混合记录、合并方向变化。

## G2 跟进 [P2] 固定 70 字窗口混合不同目标，未公布提示又被正向匹配绕过

位置：`src/soe_radar/coverage_tasks.py:552`–`:568`、`:600`–`:610`。

### 错误通过

模拟正文：

> 2027校园招聘岗位介绍。报名入口尚未公布；请访问 https://example.org/jobs 查看招聘岗位；网申开放时间另行通知。

实际为 verified，预期 readable。访问岗位页的上下文包含“报名/网申”，被 action_link 判为报名动作；本次又把正向匹配放在 future 之前，明确“尚未公布/另行通知”也无法阻止通过。

### 错误弃答

模拟正文：

> 咨询邮箱：help@example.org，仅供咨询，不接收简历。请将简历发送至 hr@example.org 完成报名。

实际 readable，预期 verified，且真实 application_target 应为 hr@example.org。真实投递动作周围 70 字包含另一个邮箱的咨询说明，导致 negative 把真实目标一起否决。这正是上一轮要求的“同文同时有咨询邮箱和投递邮箱”场景，不能只用两个邮箱彼此距离很远的文本证明通过。

修复要求：按目标所在语句/分项识别报名操作、用途、否定与未来条件，并处理同一目标的跨句指代；不能仅调整窗口长度或对全文设置一个开关。未公布报名路径时，访问岗位介绍页不等于报名；咨询说明也不能作用于另一句明确的真实投递邮箱。补同文多目标、相邻语句、正向动作与尚未开放提示共存的正反例，保存被选择的真实目标和证据。

## 独立验证和材料

- 四个历史脚本及 CQ/F/G/跟进脚本，共 40 个探针：均退出码 0。
- `uv run pytest -q tests/test_coverage_tasks.py`：20 passed。
- 新增 `review_scope_legacy_repro.py`：3 个断言均不满足预期，退出码 1；具体值见 `review_scope_legacy_output.txt`。
- 已阅读交付记录中的 177 passed、ruff check/format、构建成功，未重复执行未改变的全量检查。
- 58 个原件及 manifest 不在本轮差异内，沿用此前独立校验的哈希结果；本轮不做实时网络或新增覆盖验收。

本次只在临时库及模拟网页运行行为检查，未修改生产代码、个人库、个人配置、既有审查文件或本地运行记录。无网络和模型调用，未合并、未推送。

保留本报告、脚本及失败输出，另交修复后的结果。修复应覆盖旧库升级和相邻的多目标正文，再继续扩覆盖。

```bash
uv run python delivery/review_scope_legacy_repro.py
uv run python delivery/review_scope_audit_followup_repro.py
uv run pytest -q tests/test_coverage_tasks.py
```

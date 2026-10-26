# F1–F4 修复后的范围审计与报名路径复审

审查日期：2026-10-26（Asia/Shanghai）；HEAD：`793ec75`，差异基线：`0671296`。

## 结论

F1–F4 的原始 6 个反例现在全部通过，CQ 的 8 个反例也独立执行通过。任务冲突、原文丢失和待核验届别并入 confirmed 的原反例已修复。

本轮仍有两个需要修复的问题（G1、G2），共 6 个失败断言；暂不通过本批集成审查。它们分别是新增 provenance 参与当前审核状态计算造成的降级失效，以及 F4 报名路径校验仍未识别证据用途。后续应修正这两个根因，不应继续仅针对单条示例加关键词。

所有行为探针在临时数据库及模拟网页执行，没有修改生产代码、个人库、配置、既有审查文件或本地运行记录；无网络或模型调用，未合并、未推送。

## G1 [P1] 显式撤回范围确认无效，历史 confirmed 永久维持有效监测

位置：`src/soe_radar/coverage_tasks.py:275`–`:286` 的 `recompute_scope`，及 `:330`–`:334` 的 `add_source_scope`。

`add_source_scope(status='needs_review')` 现在只追加一条审计记录；`recompute_scope` 只要历史任意一条记录曾经 confirmed，便继续把当前行设为 confirmed，并选取旧 confirmed 作为有效证据。显式降级被忽略。

复现：某子公司 2027 秋招的共享范围先确认，再用相同来源、企业、届别和批次明确撤回为 needs_review，注明“原名单适用范围有误”。实际行状态仍为 confirmed，2027 覆盖报告仍返回来源 group。预期为 needs_review，并从有效监测中排除，旧确认原文仍保留在审计中。

影响：人工发现证据错误后无法撤销覆盖；错误范围会持续让企业显示已有共享入口，规划也可能继续跳过该企业。这是当前状态与历史审计混用，不是缺少历史记录。

修复要求：将当前有效审核决定与历史 evidence/provenance 分开，支持有审计的显式降级、撤回与重新确认；旧 confirmed 不能自行重新激活已撤回范围。仅部分届别撤回时按适用范围处理，其余已确认届别不得被无故清空。合并时同样不能恢复已撤回的历史确认。

验收至少包含 confirmed → needs_review → confirmed 往返、报告/规划联动、撤回后身份合并，以及历史仍可追溯。该问题与之前 Relationship 的显式降级约束相同，不能再次用“历史存在确认”覆盖当前审核决定。

## G2 [P2] 邮箱/URL 的存在仍被当作报名路径证据

位置：`src/soe_radar/coverage_tasks.py:532`–`:545`。

当前条件是全文存在任意邮箱、HTTP URL 或“报名系统”等词即 application=found；保存的 application_quote 甚至只有邮箱/URL/词语自身，不包含它的用途与报名操作语义。预告否定词修复了旧例，但没有解决报名证据识别。

本轮分别模拟两份官方可读招聘介绍：

1. “咨询邮箱：help@example.org（仅接受咨询，不接收简历）”，没有报名方式。
2. “集团概况：https://example.org/about”，只有公司介绍链接，没有申请路径。

两份实际都得到 application=found、候选 verified，预期均为 application=needs_review、候选 readable。每例两个断言，均已独立复现。

修复要求：将行动语义与具体目标绑定，保存完整原文、目标及位置；咨询邮箱、网站页脚、公司介绍 URL、政策引用、孤立的“报名系统”字样均不得通过。明确“将简历发送至某邮箱”“通过某网申 URL 提交申请”“前往具体地址现场报名”等才构成路径证据；线上目标的实际可用性与域名核对继续遵循现有约束，无法确认就保留 readable。不能靠禁止全部邮箱或全部 URL 解决，应补真实报名邮箱/URL 的正例和咨询、介绍链接的负例。

本项只限定候选是否完成报名路径验证，不要求据此删除岗位活动，也不把咨询邮箱或未公布入口强行转换成截止时间。

## 本轮验证

- 独立执行 `review_coverage_queue_followup_repro.py`：6 个原 F 断言通过，退出码 0。
- 独立执行 `review_coverage_queue_repro.py`：8 个 CQ 断言通过，退出码 0。
- 独立执行 `uv run pytest -q tests/test_coverage_tasks.py`：17 passed。
- 新增 `review_scope_audit_repro.py`：6 个断言未满足预期，退出码 1；实际值已保存到 `review_scope_audit_output.txt`。
- 阅读本轮全量验证记录：174 passed，ruff check/format、构建成功；没有将这些交付记录表述为本轮独立重跑的全量结果。
- 只读个人库核对迁移版本 0011，7 条范围均有 provenance；候选 blocked 174、lead 57、needs_review 4、readable 6，verified 0，与交付一致。
- allow_paid=false、月度预算 0；模型调用仍为 2 条历史记录。
- 本轮生产差异未包含 58 个证据原件及其 manifest，沿用此前已验证的哈希结果。未实时验证来源，也未增加全国覆盖或准确率验收结论。

## 交回修复

保留本报告、复现脚本及失败输出，另交修复后输出。先解决 G1 的审核状态，再解决 G2 的证据用途校验；无需通过增加采集批次来证明这两个离线问题已修复。

```bash
uv run python delivery/review_scope_audit_repro.py
uv run python delivery/review_coverage_queue_followup_repro.py
uv run python delivery/review_coverage_queue_repro.py
uv run pytest -q tests/test_coverage_tasks.py
```

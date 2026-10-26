# G1–G2 跟进复审

日期：2026-10-26（Asia/Shanghai）；HEAD：`96f6593`，比较基线：`793ec75`。

## 结论

累计 34 个既有探针独立重新执行全部通过；相关单测 19 passed。单行显式撤回与再次确认的原反例已修复，部分届别撤回也能保留其他已确认届别。

G1、G2 仍未完整解决：身份合并会重新激活已撤回范围；非报名用途的访问动作和否定的投递句仍会产生 verified。下列两个问题是前一轮约束内的跟进，不增加实施范围，暂不通过本批集成审查。

## G1 跟进 [P1] 合并数组顺序被当成审核发生顺序

位置：`src/soe_radar/identity.py:178`–`:184`、`src/soe_radar/coverage_tasks.py:280`–`:282`。

recompute_scope 以 provenance 数组的最后一项作为每届最新决定，但 _merge_scope 仍然先取规范名的记录，再把别名的整段历史追加到尾部。这只保留各自局部顺序，不能保留跨企业记录的真实审核顺序。

离线复现按实际调用先后创建：

1. 别名企业先 confirmed 2027。
2. 规范名企业随后 confirmed 2027。
3. 规范名企业最后明确 needs_review 撤回 2027；合并前已验证为 needs_review。
4. 执行身份合并。

实际结果：别名的旧 confirmed 被追加到规范名的撤回之后，行状态重新变成 confirmed，覆盖报告再次返回来源 group。预期为 needs_review，报告来源为空。

修复要求：审核决定需有可比较、稳定的时间/事件顺序及明确适用范围，不能用合并输入顺序推断“最新”。撤回也应记录审核发生时间，不能只给 confirmed 记录 verified_at。历史时间无法确定且决定冲突时保留待复核，不能因数组拼接或规范名选择恢复确认。测试合并方向/目标选择变化、旧确认与新撤回的双方分布、撤回后重新确认，以及不同届别互不影响。

## G2 跟进 [P2] 访问动作不是报名动作，否定句也仍被正向匹配

位置：`src/soe_radar/coverage_tasks.py:553`–`:583`。

action_link 第一分支只需“访问/点击/使用 + URL”，不要求 URL 对应报名或投递目的。action_email 不处理否定，“请勿将简历发送至邮箱”仍满足匹配。consultation 虽然计算出来，却只在没有 action 匹配的后续分支解释失败，无法阻止这两种误判。

已复现两例：

- “了解集团概况请访问 https://example.org/about”，只有企业介绍，没有申请路径。
- “请勿将简历发送至 help@example.org，该邮箱仅供咨询，不接收简历。”

两例实际均为 verified，预期均为 readable。原 G 探针里的静态“集团概况：URL”及“咨询邮箱”负例通过，并不覆盖上述句式。

修复要求：在目标所在的局部语义范围内判断是否是明确、肯定的报名操作；访问网站本身不能替代报名动作，禁止投递不能当成允许投递。咨询和否定判断也应作用于对应目标，不能因为全文另有咨询电话或介绍页就否决真实报名入口。保留包含邮箱和 URL 的有效正例，增加“请勿/不要/不接受”的否定例、公司介绍访问例，以及同文同时有咨询邮箱和真实投递邮箱的例子。证据应记录实际被判定为投递目标的 URL/邮箱及完整上下文。

## 验证和文件

- 7 个既有脚本累计 34 个探针均退出码 0：历史 14、CQ 8、F 6、G 6。
- `uv run pytest -q tests/test_coverage_tasks.py`：19 passed。
- 新增 `review_scope_audit_followup_repro.py`：6 个断言，其中部分届别撤回的 2 个正例通过，以上两个问题的 4 个断言未满足预期，退出码 1。具体值见 `review_scope_audit_followup_output.txt`。
- 已阅读交付的 176 passed、lint/format/build 记录，未重复全量执行，也未将其表述为本轮独立执行结果。
- 58 个证据文件及 manifest 未包含在此次代码差异中，沿用此前已经核验的哈希结果。本轮未实时抓取或重新判断覆盖数量。

所有反例在临时数据库与模拟页面运行，未改生产代码、个人库、配置、旧审查文件或验证记录，无模型和网络调用，未合并、未推送。

保留本报告及失败输出，另交修复后的输出。无需增加真实采集批次来证明这两个离线问题已经修复。

```bash
uv run python delivery/review_scope_audit_followup_repro.py
uv run python delivery/review_scope_audit_repro.py
uv run python delivery/review_coverage_queue_followup_repro.py
uv run pytest -q tests/test_coverage_tasks.py
```

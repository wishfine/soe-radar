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
ruff format --check src tests    → 44 files already formatted
uv build                          → sdist + wheel 成功
```

测试通过不代表全国覆盖完成；22 个省级地区、兵团、地市、区县、所属单位和 448 家无招聘入口企业仍在 `gaps.csv` 中。

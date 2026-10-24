# SOE Radar 全国国央企校招扩覆盖 · 本轮实做报告

验证与整理时间：2026-10-23 19:00–20:40（Asia/Shanghai）
代码分支：`feat/nationwide-coverage-20261023`
个人数据库备份：`backups/before-nationwide-20261023.tar.gz`（迁移/采集前）

## 1. 结论先行

本轮把“全国覆盖”从**仅有随仓库摘录**推进到**12 个名录范围完成本轮官方页面读取/留证**，新增 6 个官方招聘栏目接入并实际跑通列表→详情（有附件的来源同时验证附件），净新增 12 条 2027 届真实校招线索。
但**不能宣称全国覆盖完成**：22 个省级地区仍未取得官方名录，兵团、地市、区县和所属单位尚未下钻；已建档企业中 448 家仍没有招聘入口；5 个既有来源本轮仍访问失败。`registry_manifest.csv` 的“名单完整性”全部保持“未确认”。

## 2. 相对接手状态的净新增

以下数字来自本轮开始前的数据库备份 `/tmp/soe-baseline/radar.db` 与当前 `data/radar.db` 的逐表对比，不把旧数据算成本轮成绩。

| 指标 | 接手基线 | 本轮结束 | 净新增/变化 |
| --- | ---: | ---: | ---: |
| 企业记录（数据库全部） | 292 | 598 | +306 |
| 当前名录快照条目（按条目计） | 219 | 456 | +237 |
| 当前名录唯一名称 | 219 | 455 | +236（另有 5 个旧名被新名/括号别名替换，企业记录保留） |
| 招聘来源配置/状态 | 11 | 17 | +6 |
| 校招机会（未合并，全部届别） | 55 | 83 | +28 |
| 其中 2027 届校招机会 | 未单独统计 | 48 | 本轮新增 12 |
| 已核验 2027 届校招 | — | 7 | — |
| 有原文证据的母子公司关系（confirmed） | 0 | 2 | +2 |
| 文档版本 | 434 | 637 | +203 |
| 模型调用记录 | 2 | 2 | 0（本轮未调用模型） |

说明：

- 数据库 598 家企业中，`employers.jsonl` 只导出 458 家“有名录、来源、母公司或企业性质依据”的主体；其余是抽取过程中产生的未证实名称，不进入交付企业条目。
- 5 组括号别名/全半角括号造成的重复企业身份已合并，例如 `中国旅游集团有限公司[香港中旅（集团）有限公司]` 的别名归入 `中国旅游集团有限公司`，`南光（集团）有限公司[中国南光集团有限公司]` 归入 `南光（集团）有限公司`；原显示名保留为别名，活动、个人状态、关系和审核记录保留，旧快照不删除。
- “净新增 2027 届 12 条”按本轮新产生的 opportunity ID 统计，分布为：重庆国资委 4、NCSS 4、南方电网 2、黑龙江国资委 1、湖南国资委 1。

## 3. 企业底册：实际取得了什么

`registry_manifest.csv` 共 40 行：31 个省级地区 + 广州 + 中央国资委 + 财政部 + 中央金融 + 中央其他 + 中央所属单位 + 兵团 + 地市 + 区县。除要求的字段外，还附 `证据SHA256` 和 `范围说明`，便于逐行核对。
其中 **12 行在本轮完成官方页面读取并保存原件，合计 429 条**；其余 27 条来自历史金融名单 26 条与补充主体 1 条，合计所有当前快照 456 条。

| 范围 | 本轮条数 | 本轮实际读取 | 证据 | 边界 |
| --- | ---: | --- | --- | --- |
| 国务院国资委监管央企 | 100 | 官方页面 2026-07-11 | `evidence/registries/central-sasac-*.html` | 集团层级；不含所属单位；HTTPS 证书已过期，改用同域 HTTP 页面并逐条比对 `curl -k` HTTPS 抓取；TLS 校验未完成，企业性质不自动提升 |
| 财政部出资/中央文化企业 | 113 | 官方页面 2026-03-24 | `evidence/registries/central-mof-*.html` | 4 户中央企业 + 109 户中央文化企业；不含中央金融 |
| 北京 | 40 | 官方目录 3 页全部读取 | `evidence/registries/local-01-*.html`（3 份） | 不含区县、所属单位 |
| 天津 | 27 | 市管国企名录 | `local-02-*.html` | 委管企业名录另页未合并 |
| 山西 | 19 | 监管企业名录 | `local-04-*.html` | 不含地市、区县、所属单位 |
| 内蒙古 | 28 | 直接监管企业 | `local-05-*.html` | 不含地市、区县、所属单位 |
| 福建 | 16 | 所出资企业 | `local-13-*.html` | 不含地市、区县、所属单位 |
| 湖南 | 19 | 直接+委托监管企业 | `local-18-*.html` | 已排除“第一大股东”中联重科；不含地市、所属单位 |
| 海南 | 10 | 省属重点监管企业 | `local-21-*.html` | 不含地市、区县、所属单位 |
| 云南 | 14 | 省属企业名录 | `local-25-*.html` | 不含地市、区县、所属单位 |
| 新疆 | 17 | 自治区国资委监管企业 | `local-31-*.html` | 不含兵团、地州、所属单位 |
| 广州 | 26 | 市直接监管企业 | `local-guangzhou-*.html` | 不代表广东全省 |

未完成的主要名录：

- **22 个省级地区**没有取得本轮可核对的官方名录：河北、辽宁、吉林、黑龙江、上海、江苏、浙江、安徽、江西、山东、河南、湖北、广东、广西、重庆、四川、贵州、西藏、陕西、甘肃、青海、宁夏。它们保留在 manifest 和 gaps 中，不删行、不造数。
- **中央金融企业**仍只有 2021 年江苏财政厅转载的 26 条历史名单。财政部页面第三节给出的 `jrs.mof.gov.cn` 链接当前返回“页面不存在”，未找到可核对的现行官方名单，因此标记“历史名单待更新”。
- **新疆生产建设兵团**：`www.xjbt.gov.cn` 可访问，但本轮未定位到国资委监管企业名录页，登记为“未建档”。
- **地市、区县、所属单位**分别保留为独立范围行，未用省级集团名单替代。

## 4. 招聘入口发现与程序采集

本轮实际检查 17 个已配置来源。结果按来源状态：

| 状态 | 数量 | 来源 |
| --- | ---: | --- |
| success | 5 | chnenergy、unicom、hlj-sasac、cq-sasac、hunan-sasac |
| no_recruitment（列表可读但本轮未发现当届校招） | 4 | cmcc、nmg-sasac、jl-sasac、sx-sasac |
| partial（列表部分完成/有积压/详情失败） | 3 | boc、ncss、csg |
| error（访问失败/动态壳/HTTP 412） | 5 | sh-sasac、sasac、cnpc、sinopec、telecom |

**新增并跑通列表→详情的 6 个官方招聘栏目**：

| 来源 | 列表 | 详情 | 附件/说明 |
| --- | --- | --- | --- |
| 内蒙古国资委国企招聘 `nmg-sasac` | 6 条 | 1 条详情成功，正文 5530 字 | 有 2 份 PDF 解析；未发现 2027 届校招 |
| 吉林省国资委企业招聘 `jl-sasac` | 3 条 | 1 条详情成功，正文 3995 字 | 均为社会招聘，本轮未发现校招 |
| 黑龙江国资委招聘信息 `hlj-sasac` | 14 条 | 1 条详情成功，正文 1353 字 | 3 条校招待核验，其中 1 条 2027 届 |
| 重庆国资委国企招聘 `cq-sasac` | 14 条 | 1 条详情成功，正文 3963 字 | 4 条 2027 届校招；站点装饰图已识别并忽略 |
| 山西国资委国企招聘 `sx-sasac` | 20 条 | 1 条详情成功，正文 2067 字 | 本轮抓到的主要是结果/录用公示，已按“公示类公告”排除，不当作开放校招 |
| 湖南国资委通知公告 `hunan-sasac` | 8 条 | 1 条详情成功，正文 2162 字 | 2 条校招待核验，其中 1 条 2027 届 |

另有 4 个官方招聘栏目已发现但本轮未接入成功，保留在缺口：

- 北京市国资委国企招聘：列表为动态壳，未取得有效公告。
- 辽宁省国资委招聘：列表为动态壳。
- 海南省属企业招聘：列表为动态壳。
- 四川省国资委国企招聘：列表为动态壳。

既有来源的真实限制（本轮实测）：

- `sasac` 国务院国资委、`sh-sasac` 上海国资委、`sinopec` 中国石化：`ConnectError`。
- `cnpc` 中国石油：HTTP 412。
- `telecom` 中国电信：详情页动态壳，1 条积压；但已入库 5 条 2027 届待核验校招。
- `boc` 中国银行：列表 partial，172 条发现、117 条详情积压；已核验 2027 全球校招，报名截止 2026-10-09（当前已关闭）。
- `ncss`：公开接口第 4 页起返回“请登录后查看”，程序**未绕过登录**，记录为 partial；本轮新增 4 条 2027 届岗位线索。
- `csg` 南方电网：123 条发现、89 条积压；部分详情 HTTP 401，未绕过；已核验 2027 校招 1 条，另有 26 条 2027 待核验。

## 5. 真实校招发现

本轮净新增 28 条校招机会（55→83；含补采到的历史公告），其中 **12 条为 2027 届**：

| 来源 | 新增 2027 届 | 代表公告 |
| --- | ---: | --- |
| cq-sasac | 4 | 重庆机场集团 2027 校园招聘、重庆农商行 2027 校园招聘、重庆渝富所属企业校招、登康口腔 2027 届 |
| ncss | 4 | 新疆绿色电力、甘肃农商银行、兰州银行、中建三局（新疆） |
| csg | 2 | 南网人才发展中心 2027 校招、南网共享运营公司 2027 校招 |
| hlj-sasac | 1 | 黑龙江省铁路集团宝清铁路 2026/2027 校招 |
| hunan-sasac | 1 | 湖南能源集团 2027 届校园招聘 |

当前数据库中共有 **48 条 2027 届校招机会**，其中 7 条已核验，41 条待核验。已核验的代表：中国银行 2027 全球校招、国家能源集团 2027 统招/“菁英”管培生、南方电网 2027 校招、重庆渝富所属企业 2027 校招、重庆登康 2027 届、NCSS 中国银行 2027 公告。

注意：48 条是“程序当前保存的 2027 届校招机会”，不是全国 2027 届校招总数；没有取得的企业和入口仍在 gaps 中。

## 6. 证据与可复核文件

`delivery/evidence/` 共 58 个文件，`SHA256SUMS` 与 `manifest.csv` 逐文件对应：

- `evidence/registries/`：12 个名录范围的原始 HTML + 静态文本提取。北京 3 页分别保存；SASAC/MOF 为人工保存的官方 HTML。
- `evidence/recruitment/`：9 个来源的代表性 2027 详情，含原始 HTML、静态文本、程序抽取 JSON。CSG 是 hash 路由，只有程序抽取 JSON，没有伪造“原始 HTML”。
- `evidence/attachments/boc-2027-conditions.pdf`：中国银行 2027 校招条件 PDF（不含个人报名信息），附 SHA-256。
- `evidence/relationships/csg-company-profile.html`：南方电网公司概况，用于 2 条 confirmed 关系；第 3 条“南网数字”因证据只支持上市公司简称、不能确认所选集团法人，已降为 `needs_review`。

证据类型严格区分：

- `原始HTTP响应`：直接保存的 HTML。
- `静态文本提取（非浏览器渲染、非OCR）`：由 HTML 去标签得到，仅用于检索复核。
- `程序抽取详情JSON（非原始HTML）`：CSG 等 hash 路由详情。
- `官方关系证据（原始HTML）`：关系页面。
- `人工归档证据`：人工放入、导出器未自动抓取的文件。

重要限制：

- 国务院国资委 HTTPS 证书已于 2023-11-10 过期且缺中间证书；本轮改用同域 HTTP 官方页面保存，并与 `curl -k` HTTPS 页面逐条比对 100 个企业名称一致。该页面标记 `tls_verified=false`，不据此自动提升企业性质核验等级；**未修改系统证书、代理、TUN 或分流**。MOF 自动客户端超时，用正常校验证书的 `curl` 保存。
- 南方电网公司概况页面没有可见发布日期；2 条 confirmed 关系和 1 条待复核关系的 `evidence_date` 使用实际访问日期 2026-10-23，报告明确标注，不冒充页面发布日期。
- 附件统计包含全部文档，不只是校招：解析 340、失败 25、待 OCR 10、待复核 379、忽略站点装饰图 51。扫描件/图片未做 OCR，未把未读取附件当作已解析。

## 7. 代码改动

分支：`feat/nationwide-coverage-20261023`（未合并、未推送）。

主要改动：

1. **名录解析**
   - 支持双列/多列“序号+企业名”表格（国资委 100 条、湖南 19 条）。
   - 新增 `text` 适配器，支持纯文本名录（天津、山西、内蒙古、海南、云南、福建）。
   - 新增 `page_urls` 多页合并（北京 3 页），保存每页 URL、SHA-256、路径。
   - 区分企业官网与政府档案页，避免把政府详情页当作企业官网。
   - 新增可见发布日期提取，避免误取新闻列表日期。
2. **人工官方 HTML 导入**
   - `registry-import-html`：自动抓取被 TLS 链/超时阻断时，导入已保存的官方 HTML，强制确认官方域名并记录取得方式。
3. **关系证据**
   - 迁移 `0007_relationships.py` 与 `Relationship` 表；`relationship-add` 要求母公司、子公司已建档，必须有证据 URL、原文片段、日期，拒绝自环、循环和不同母公司覆盖。
4. **交付导出**
   - `delivery-export` 生成 manifest、employers、relationships、recruitment_sources、gaps、证据清单和 SHA-256。
   - 证据扫描会把人工放入的招聘详情/关系证据/附件纳入哈希清单。
5. **采集健壮性**
   - 公开 JSON 接口返回 `data:null`/要求登录时抛出明确 `FetchFailure`，不再 `AttributeError`。
   - 站点 logo/图标/导航图不计为招聘附件，记录为 `ignored_chrome`。
   - “结果公示/拟录用/招聘结果”等公告不再当作开放校招。
   - `pagination_complete` 遇到列表错误不再误报 true。
6. **配置**
   - 新增 6 个官方招聘来源；新增天津、山西、内蒙古、海南、云南名录入口；北京补 3 页；SASAC/MOF 解析规则更新；新增兵团、地市、区县、中央所属单位缺口范围。
7. **复审修复**
   - R1：只排除有明确装饰证据的图片；正文大图保留为待读取。
   - R2：部分分页失败不替换 active snapshot，保留已知成员。
   - R3：拆分 source_name/规范名/别名；新增 `employer-merge-aliases` 合并既有重复身份。
   - R4：text 适配器合并链接与纯文本条目。
   - R5：逐页解析并给企业绑定实际 page_url/page_sha256/page_path。
   - R6：snapshot 身份摘要与文件内容 SHA-256 分离。
   - R7：“南网数字”关系降为 needs_review；`relationships.jsonl` 只导出 confirmed。
   - R8：已确认子公司/所属单位即使不在名录中也进入逐企入口缺口。
   - S1：snapshot digest 与 page digest 分离，纳入解析版本；新增历史快照修复命令。
   - S2/S3：合并时迁移/校验 parent_id，冲突降为 needs_review；关系证据写入 provenance，先 flush 再删重复企业。
   - T1：显式 `status` 决定当前审核状态，支持 confirmed → needs_review → confirmed 往返。
   - T2：repair 重新赋值 entries 并独立检查页绑定，已修复数据可幂等复修。

测试与构建：

```text
uv run pytest -q            → 153 passed, 1 warning
ruff check src tests        → All checks passed
ruff format --check src tests → 45 files already formatted
uv build                    → sdist + wheel 成功
```

CI 仍使用离线样例；本轮真实站点结果由上面的 evidence 单独证明，不用测试通过代替真实可用。

## 7.1 复审问题修复（R1–R8）

本轮针对审查意见做了代码修复，并用 `delivery/review_repro.py` 离线复现：

| 问题 | 修复 | 复现结果 |
| --- | --- | --- |
| R1 正文海报被当装饰图丢弃 | 只排除有明确装饰证据的 logo/icon/二维码/小图；`/images/`、`banner`、`share` 不再单独作为排除理由；新增正文容器 `.zwxl-content` | 无 alt 的 1000×2000 海报保留；招聘 banner 保留；二维码/logo 忽略 |
| R2 分页失败缩小企业全集 | 部分页失败时不切换 active snapshot，保留上次完整成员；失败候选版本仅作历史 | 两页两家企业，第二页 503 后仍为 2 家 |
| R3 括号别名新建身份 | 解析时拆分 `source_name`、规范名、别名；按规范名/别名复用 Employer；新增 `employer-merge-aliases` 合并既有重复并保留活动、个人状态、关系和审核 | 中国旅游/南光/福建等 5 组重复已合并，dry-run 为空 |
| R4 混合链接+纯文本只读链接 | text 适配器同时读取链接和去掉链接后的纯文本，去重合并 | 1 链接 + 2 纯文本 = 3 家 |
| R5 北京企业绑定错页 | 逐页解析并给每条企业保存 `page_url/page_sha256/page_path`；导出按企业实际页绑定证据 | 40 家北京企业中 0 家名称不在其绑定证据页 |
| R6 单页/CSV 哈希错误 | snapshot 身份摘要与文件内容 SHA-256 分开；所有证据清单写实际文件哈希 | 单页、CSV 路径的文件哈希均一致 |
| R7 第三条关系主体不一致 | “南网数字”降为 `needs_review`，`relationships.jsonl` 只导出 confirmed；缺口台账新增“关系待复核” | confirmed 2 条；1 条待复核 |
| R8 已确认子公司不进逐企缺口 | “入口未找到”按企业范围和可用来源判断，不再要求必须有主管口径；别名/禁用来源单独判断 | 3 家南网子公司进入逐企入口缺口 |

复现命令与结果：

```text
uv run python delivery/review_repro.py
failed_page_preserves_known_membership: true
single_page_content_sha256: true
one_legal_entity_despite_bracket_alias: true
mixed_link_plaintext_directory: true
unlabelled_body_poster_retained: true
delivered_file_hash_mismatches: 0
employer_names_absent_from_bound_evidence: 0
```

## 7.2 第二轮复审修复（S1–S3）

| 问题 | 修复 | 复现结果 |
| --- | --- | --- |
| S1 整页 digest 被末页覆盖 | 使用独立 `snapshot_digest`，纳入全部页面 URL/内容和 `PARSER_VERSION`；复用时校验 entries 指纹，不一致时创建新版本；新增 `registry-repair-snapshots` 修复历史快照元数据与逐条页绑定 | 非末页新增企业后创建新 snapshot，active/stored/reported 均为 3；旧快照 digest 已重算 |
| S2 合并后母公司指针丢失 | 合并时继承/校验 target 与 duplicate 的 parent_id 和 confirmed Relationship；空值继承、相同上级保留、不同上级保留 target 并将另一条降为 needs_review；防止自环/循环 | 合并后 target.parent_id 与 confirmed Relationship 一致 |
| S3 同父子关系合并外键错误 | 关系行先合并证据并 flush，再删除重复 Employer；新增 `relationships.provenance` JSON 审计列（迁移 0008），保留不同 URL/原文/日期/状态 | 两端同父关系合并无 IntegrityError，confirmed 保留，两条证据均在 provenance |

第二轮复现：

```text
uv run python delivery/review_round2_repro.py
changed_nonfinal_page_creates_updated_snapshot: true
merge_preserves_confirmed_parent_pointer: true
merge_existing_relationship_edges: no IntegrityError
```

个人库迁移前备份：`backups/before-review-round2-20261024.tar.gz`。历史快照修复命令：

```bash
uv run soe-radar registry-repair-snapshots --apply
```

## 7.3 第三轮复审修复（T1–T2）

| 问题 | 修复 | 复现结果 |
| --- | --- | --- |
| T1 显式降级被 confirmed 优先策略阻止 | `add_relationship` 的显式 `status` 现在直接决定当前审核状态；旧证据保留在 `provenance`，降级时清除 `parent_id`；状态只接受 confirmed/needs_review | confirmed → needs_review → confirmed 往返通过，交付仍只导出 confirmed |
| T2 历史条目 page_path 修复未持久化 | repair 构造新的 entries 列表并赋值，显式触发 JSON 列变更；页绑定检查独立于 digest 修复，已标记修过的快照仍检查绑定；无法唯一定位的条目写 `page_binding=needs_review`；修复幂等 | 重新打开 session 后 page_path 已持久化；第二次 repair 返回空计划 |

第三轮复现：

```text
uv run python delivery/review_round3_repro.py
explicit_relationship_downgrade: true
repaired_page_binding_persists_after_commit: true
```

个人库已执行 `registry-repair-snapshots --apply`；active 快照摘要未受影响，历史条目的页绑定已持久化修复。

## 8. 尚未解决的主要缺口

详见 `gaps.csv`（503 行）与 `registry_manifest.csv`。优先级最高的：

1. **22 个省级地区无官方名录**：需继续从主管部门官网导航、站内搜索、政府公开目录取得，并保存分页/附件证据。
2. **兵团、地市、区县、所属单位未下钻**：省级集团名单不等于全部国企；集团一篇公告不等于子公司覆盖。
3. **448 家已建档企业没有招聘入口**：这是当前最大的入口缺口；`gaps.csv` 中“入口未找到”逐家列出。
4. **中央金融企业名单仍是 2021 年历史版本**：财政部现行名单链接失效，需继续查找官方更新。
5. **5 个来源访问失败、3 个来源 partial**：SASAC/上海/中石化连接失败，中石油 412，电信动态壳；中行/NCSS/南网有分页或详情积压。
6. **附件 OCR 未接入**：10 份待 OCR、25 份失败；图片海报和扫描 PDF 保留原件/链接，未冒充已解析。
7. **全国覆盖率仍为 null**：没有可审计的全国完整分母，不生成“全国覆盖率 100%”或类似结论。

## 9. 复现命令

```bash
# 备份个人库（已完成）
uv run soe-radar backup backups/before-nationwide-20261023.tar.gz

# 名录读取
uv run soe-radar registry-refresh \
  --ids local-01,local-02,local-04,local-05,local-13,local-18,local-21,local-25,local-31,local-guangzhou
uv run soe-radar registry-import-html --registry central-sasac \
  --file 官方SASAC.html --proof-url https://wap.sasac.gov.cn/.../content.html \
  --published-at 2026-07-11 --confirm-official
uv run soe-radar registry-import-html --registry central-mof \
  --file 官方MOF.html --proof-url https://zcgls.mof.gov.cn/.../t20210817_3745862.htm \
  --published-at 2026-03-24 --confirm-official

# 真实来源检查与采集
uv run soe-radar doctor --live
uv run soe-radar sync --sources boc,chnenergy,ncss,cmcc,unicom,csg
uv run soe-radar sync --sources nmg-sasac,jl-sasac,hlj-sasac,cq-sasac,sx-sasac,hunan-sasac

# 关系与交付
uv run soe-radar relationship-add --parent 中国南方电网有限责任公司 \
  --child 南方电网储能股份有限公司 --evidence-url https://www.csg.cn/gywm/gsjs/ \
  --quote "所属27家二级分子公司，控股南网储能、南网能源、南网数字、南网科技4家境内上市公司" \
  --evidence-date 2026-10-23 --relation-type 控股子公司
uv run soe-radar delivery-export --out delivery

# 验证
uv run pytest -q
ruff check src tests
ruff format --check src tests
uv build
```

## 10. 交付格式差异与转换

`employers.jsonl` 与项目现有 `registry-import` CSV 不是同一格式：

- 现有导入 CSV 只需 `企业名称` / `公司名称` / `name`，可选 `官网地址` / `official_url`。
- `employers.jsonl` 是**审计导出**，包含原始名称、规范名称、别名、地区、主管口径、层级、母公司、官方来源 URL、页面发布日期、访问时间、原文片段、证据路径、SHA-256、企业官网、档案页、状态和历史。它不直接用于 `registry-import`。
- 转换方式：提取每行 `原始名称` 写为 CSV 的 `企业名称`，`企业官网` 写为 `官网地址`；然后使用：
  ```bash
  uv run soe-radar registry-import 转换后.csv --registry <名录id> \
    --proof-url <该行证据URL> --confirm-official
  ```
  注意：CSV 导入默认是人工确认导入，不会自动把 `employers.jsonl` 的状态、历史或关系写回；关系必须用 `relationship-add` 单独录入。
- `relationships.jsonl` 只包含 `status=confirmed` 的关系；没有原文片段/日期的母公司链接只进入 `gaps.csv` 的“关系待复核”，不混入已确认关系。

## 11. 合规与未做事项

- `allow_paid=false`，月度预算 0；本轮**没有调用任何模型**，模型调用记录仍为 2 条历史记录。
- 未读取、保存或转发登录令牌、Cookie、个人配置、个人数据库到仓库；`data/`、`backups/`、`config/local.yaml` 仍在 `.gitignore`。
- `evidence/` 只放公开公告/名录原文，可能含公告中公开的招聘咨询电话或邮箱；未纳入个人报名表、候选人名单或用户数据。山西一份含候选人姓名/毕业院校的拟录用 PDF 仅留在本地数据库，未放入交付。
- 未修改系统代理、TUN、分流或证书设置；未绕过验证码、登录限制或私有接口。
- 未把占位地区、企业官网、搜索结果、集团一篇公告算作招聘覆盖。
- 未宣称“全国全部覆盖”；全国企业、所属单位和招聘覆盖仍未齐备。

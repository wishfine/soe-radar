# SOE Radar · 国央企校招截止时间雷达

个人本地运行的国央企校招工作台：采集官方公告、提取投递截止、保存原文证据、跟踪延期，按届别持续使用。

**项目首要目标是全国国央企校招的覆盖完整性。** 预置入口只是起步样本，不是覆盖范围或完成标准；后续按企业名录逐家扩展集团、子公司与地方国企，并公开未接入、采集受阻和待核验的缺口。完整要求与下一步实施顺序见 [覆盖完整性要求](docs/requirements/completeness.md)。

**爬虫取内容，模型做理解，规则做校验。** 有证据的记录自动入库，冲突、不完整和未知截止进入待核验区。没有模型凭证时仍可采集、做规则解析及浏览历史。

## 快速开始

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在项目目录执行：

```bash
git clone git@github.com:wishfine/soe-radar.git
cd soe-radar
uv sync --frozen
uv run soe-radar init
uv run soe-radar doctor
uv run soe-radar serve
```

打开 **http://127.0.0.1:8000**。首次启动自动同步；保持终端运行，电脑关闭后停止采集，重新启动会补跑到期任务。`serve --no-schedule` 只启动网页。

```bash
# 首次也可先选择少量来源验证，不依赖模型 Key
uv run soe-radar sync --sources boc,chnenergy
# 对所有配置来源做真实列表检查；不会把列表可读当作岗位全部已核验
uv run soe-radar doctor --live
```

已验证的本机运行结果与限制见 [验证记录](docs/implementation/validation.md)。**预置来源不等于已成功采集的来源**；当前网络、站点改版、动态加载和附件读取都会影响实际覆盖，界面展示每个来源的真实状态。

## 已实现

- 今天 / 3 天 / 7 天截止列表、7 天内发布和近期变更，届别、地区、行业、企业、专业、学历与批次筛选。
- 招聘详情、连续原文证据、人工与自动核验标记、原文版本及变更历史。
- 关注、已投状态及个人备注，保存在本地 SQLite。
- 待核验与人工确认，确认时重新读取官方公告；单独发布的变更可关联原招聘活动。
- 来源健康、断点续跑、数据库租约、防止重复同步、重启恢复。
- 全国覆盖与缺口台账：已知主体、独立入口、当届活动、访问受阻、待接入名录分别展示；全国分母未知时不生成覆盖率。
- 官方企业名录版本、带来源确认的底册 CSV 导入、缺口 CSV 导出；31 个省级地区保留待接入槽位，不计为已覆盖。
- CSV 字段映射导入 / 导出；导入默认为第三方线索，不会因填了官方 URL 就自动通过。
- ICS 日历：只导出已核验、未关闭且最近 48 小时内核验的明确截止；日期精度生成全天事件。文件是快照，延期后需重新导出。
- 规则、OpenAI 兼容 Chat Completions、Responses、本机 Codex CLI 三种解析入口。
- 数据库迁移、个人备份恢复、真实运行观察记录、人工标注数据集导出和规则评估工具。

## 配置自己的模型

`init` 生成只含个人覆盖项的 `config/local.yaml`，未填写部分继承仓库默认值。来源按 `id`、供应商按 `name` 合并；设置空列表可关闭全部对应项。这是个人配置，不提交 Git。修改后重启服务。可通过 `SOE_RADAR_CONFIG` 或全局 `--config PATH` 指定其他配置。

`llm.providers` 按列表顺序尝试，每个供应商声明协议、模型、费用类型和凭证环境变量。默认只允许配置中确认免费的 OpenCode 模型；默认不会调用收费模型或本机 Codex。默认模型名称和接口来自 [OpenCode 官方模型文档](https://opencode.ai/v2/docs/console/models/)，免费政策可能变化。

若使用 OpenCode，把自己的 Key 放进项目根目录 `.env` 或环境变量：

```dotenv
OPENCODE_API_KEY=填写自己的Key
```

模板见 [.env.example](.env.example)。程序不附带共享 Key，不读取、转发 Codex 登录令牌。当前本机匿名 OpenCode 生成请求返回 HTTP 403；模型目录可读并不能证明生成可用。

```bash
# 明确发起合成样例请求，检查模型是否能实际返回结构化结果
uv run soe-radar doctor --model
# 更新凭证后，解除之前的暂停
uv run soe-radar doctor --reset-provider opencode
```

默认 `allow_paid: false`、月度付费预算 `0`、每日最多 `100` 次模型调用、并发 `1`、单篇最多 `3` 次尝试。免费模型不可用时排队，不自动改成非免费版本。免费声明来自个人配置和供应商文档，并不是上游账单承诺；请在供应商侧关闭自动充值和收费回退。

可选本机 `gpt-6-luna`：先自行安装并登录 Codex，然后在 `config/local.yaml` 中将 `codex-local.enabled` 改成 `true`。这会使用个人现有 Codex 额度，**默认关闭**。本机入口已用合成样例验证，不能据此宣称复杂公告的准确率。

例如，启用本机模式的个人覆盖配置为：

```yaml
llm:
  providers:
    - name: codex-local
      enabled: true
```

其余属性继承默认值。可选付费 API 需同时启用对应供应商、`allow_paid` 和大于零的预算，配置正数单价。程序在请求前预留预算；费用不明确的模型不会发送请求。自定义供应商需填写完整协议、模型及费用配置。

## 采集方式与来源

预置：`boc` 中国银行、`chnenergy` 国家能源、`sh-sasac` 上海国资委、`sasac` 国资委、`ncss` 国家就业平台、`cmcc` 中国移动、`telecom` 中国电信、`unicom` 中国联通、`cnpc` 中国石油、`sinopec` 中国石化、`csg` 南方电网。

- 静态 HTML / 公开 JSON 优先；NCSS 使用公开岗位 JSON 接口并按国有企业性质过滤。平台信息仍需企业身份与报名证据校验。
- 文字型 PDF 提取正文并保存附件版本。扫描图片、失败附件和含糊适用范围进入待核验。
- 入口、域名范围、来源策略、分页和详情上限均可配置。来源 `success / partial / error / unverified` 如实展示；`partial` 包含未完成分页或部分详情失败。
- 浏览器采集是可选依赖，开启方式如下。它不会自动处理验证码或忽略证书错误。

```bash
uv sync --frozen --extra browser
uv run --extra browser playwright install chromium
```

然后设置 `browser_enabled: true`。若已安装 Google Chrome，可用 `browser_channel: chrome`，无需下载 Chromium。运行时使用 `uv run --extra browser soe-radar serve`。

可选 `jina_enabled: true` 使用 [Jina Reader](https://jina.ai/reader/) 作为正文读取备用。可选 Tavily：配置 `TAVILY_API_KEY`、`tavily_enabled: true` 后执行 `sync --discover`，每日上限 20 次 basic 搜索，只回查已配置的官方域名。

若可信的本机网络代理使用 Fake-IP DNS，只有确认此环境时才设置 `allow_fake_ip_dns: true`。此选项仅允许 DNS 中的 `198.18.0.0/15` 与 `2001:2::/48` 代理地址段；私网地址及证书验证继续检查。默认关闭。

企业及集团隶属关系可在个人配置的 `employers` 名录中声明。填写自己核实过的官方依据，子公司的 `parent` 必须对应已存在的企业名称；集团筛选会包含这些子公司。例如：

```yaml
employers:
  - name: 已核实的子公司全称
    parent: 中国银行股份有限公司
    proof_url: https://www.boc.cn/aboutboc/
    aliases: []
    industry: 金融
```

人工确认子公司时也可在审核表单选择母集团并提交关系依据链接。

上面的名称仅示意配置格式，使用时替换为实际企业及其隶属关系证明。系统不会仅凭名称相似就认定国企或自动推断母子公司。

## 覆盖台账与采集网络

```bash
uv run soe-radar coverage --cohort 2027
uv run soe-radar registry-refresh --ids central-sasac,central-mof
uv run soe-radar doctor --network --source sasac --source boc
```

`coverage` 的默认届别为运行时下一届，也可明确指定；名单、招聘季和原文年份不会被自动改写。配置 `registries` 按 `id` 合并，空列表关闭名录刷新。名录读取成功每七天复查、失败每日重试，失败保留已有名单和历史。网页“全国覆盖”列出未知分母和未接入地区，并支持导入已核对的官方 CSV：

```bash
uv run soe-radar registry-import 官方企业底册.csv --registry local-01 \
  --proof-url https://官方政府域名/名录页面 --confirm-official
```

CSV 支持 `企业名称` / `公司名称` / `name` 列，可选 `官网地址` / `official_url` 列。导入需要明确确认来源并保留原文件，不能据此声称取得招聘内容或该地区名单已完整。

采集网络默认 `network_mode: env`。可设为 `direct`（忽略代理环境变量）、`proxy`（只使用采集代理）或 `auto`（默认路径失败后尝试已配置代理，否则尝试 direct）。代理地址填写 `.env` 的 `SOE_RADAR_HTTP_PROXY`；仅支持 HTTP(S) 代理，不是订阅地址。模型请求不使用此采集配置。

每个 `sources` / `registries` 条目也可配置 `network_mode` 覆盖全局设置。`doctor --network` 分别报告路径与脱敏的 HTTP/连接错误；HTTP 可读不代表招聘采集成功。`direct` 仍可能经过系统 TUN/VPN，程序不会修改系统分流或忽略证书错误；401、429 不通过切换路径重试。

南网采用官网前端使用的公开列表及正文接口，不登录、不读取令牌；真实可用情况与当前限制见 [本轮扩覆盖记录](docs/implementation/2026-10-23-coverage.md)。

## 跨年度使用

公司、招聘活动、批次、岗位、原文版本和截止声明分别保存。2026 年发布的 2027 届秋招与 2027 年发布的同届春招可以并存；一份公告支持多个届别。新年度不会清库，旧 URL 改成新一届内容会保留历史，去年已投不会把今年的活动标为已投。

“尽快投递”、招满即止、长期招聘、未公布与日期冲突分别处理。没有可靠年份时不使用机器当前年份补齐；部分岗位延期不能覆盖整批招聘。

详见 [跨年度要求](docs/requirements/multi-year.md)。

## 备份、恢复与观察

```bash
uv run soe-radar backup backups/personal.tar.gz
# 恢复前先停止服务；现有数据会先自动备份
uv run soe-radar restore backups/personal.tar.gz --force
uv run soe-radar observe
```

备份包含数据库、原文快照、已下载附件和运行观察记录，不包含凭证或配置。配置文件请自行保管。`data/`、`.env`、`config/local.yaml`、缓存和数据库均被 Git 忽略。

`observe` 汇总真实运行日期及失败情况。七天观察尚需时间积累；达到七个日期也不代表所有来源稳定。不要把一次短时运行当成七天验收。

## 审核与数据修复

待核验列表优先显示近期届别，往届可展开；行内可以排除线索，来源页面可以单独重试。人工排除不会被后续同步撤销；人工确认后的原文发生变化时保留旧结论、标记待复核，并停止导出不确定的截止。

核验默认读取最新原文，也可明确选择已保存的版本。海报/扫描 PDF 可以填写附件转录和页码/位置；缺少年份时补充发布日期及依据；纠正日期冲突需填写说明。原文不被改写，审核内容单独保存。快照核验不代表官网当前仍开放；CSV 导入不算官方快照，仍需先读取官方公告。

```bash
# 停止本地服务后预览修复；应用时自动备份、保留被合并记录并离线重解析
uv run soe-radar repair
uv run soe-radar repair --apply
```

修复不会跨届合并。原始证据、关注、已投、备注和审核历史会保留；离线解析不调用模型，不把旧原文的获取时间改成今天。修复明细保存在个人 `data/repair-report.json`。

## 人工标注与验收

```bash
uv run soe-radar dataset export data/labels-to-review.jsonl
# 人工查原文、填写 expected、annotator，并将 annotation_type 改为 human
uv run soe-radar dataset evaluate data/labels-reviewed.jsonl
```

要求 100 份开发集、50 份人工标注留出集。`expected` 示例：`{"kind":"date","value":"2026-10-31","clear":true}`；未公布为 `{"kind":"unannounced","value":null}`。工具不会自动把解析输出填成真值。导出按活动分组、保留当前版本；评估会拒绝重复标注和同组跨开发/留出集的泄漏。

当前自动化测试主要使用明确标注的合成样例和模拟网络，不是 150 份人工真值，也不能用来声称达到 98% 的真实数据精确率。评估命令只报告规则解析器指标，端到端发布需要另外核对。

## 开发与 API

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

CI 使用离线样例，不调用真实模型或招聘站点。HTTP 接口及请求参数见 [接口说明](docs/implementation/api.md)，运行中的接口定义见 http://127.0.0.1:8000/docs。

实现以 [MIT 许可证](LICENSE) 发布。招聘内容是第三方公开信息，其性质与软件代码许可分别判断。

前期调研：[GitHub 项目](docs/research/2026-10-22-github-survey.md)、[飞书表格参考](docs/research/2026-10-22-feishu-reference.md)。

本轮诊断修复的实测与剩余限制见 [修复记录](docs/implementation/2026-10-23-review-fixes.md)。

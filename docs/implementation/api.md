# 本地接口

绑定 `127.0.0.1:8000`，单用户，无公共账户。写操作需要 `X-Radar-Token`（本地页面的 meta 标签提供），并拒绝跨源请求。不要把本地写接口直接暴露到公网。

## 招聘查询

`GET /api/opportunities` 返回 `items / total / page / page_size`。单页默认 30，最大 100。

筛选：`q`、`cohort`（可重复或逗号分隔）、`region`、`industry`、`employer`、`education`、`major`、`batch`、`source_id`、`days`（0–366）、`saved=1`、`applied=0/1`、`changed=1`（7 天内变更）、`new=1`（7 天内发布）、`status=open/closed`、`history=1`、`review=pending/all`。

默认只展示核验通过且开放的机会。历史筛选可查询过期和未知状态。`employer` 支持名称片段并包含配置名录中已确认的子公司；企业数据保存 `parent_id`，尚未确认的集团隶属关系不自动推定。

`GET /api/opportunities/{id}` 增加 `documents`、`events`、独立保存的 `jobs`、企业性质依据及个人状态。文档包含当前保存的原文文本；证据起止偏移以这份文本为准。

`PATCH /api/opportunities/{id}/state` JSON：`{"saved":true,"applied":false,"notes":"个人备注"}`。字段均可省略，不跨年继承。

## 同步

`POST /api/sync` JSON：`{"source_ids":["boc","chnenergy"],"discover":false}`。省略来源表示全部启用来源；返回 HTTP 202 和任务 `id`。

`serve` 默认调度器执行队列；`serve --no-schedule` 不执行排队任务，须使用 CLI 单次同步。

`GET /api/runs/{id}` 提供 `queued / running / success / partial / failed`、时间及逐源结果；`GET /api/runs` 返回最近 20 条。

`GET /api/health` 返回每源的真实状态、失败原因、待续跑队列及模型配置状态，不返回 Key。`GET /api/stats` 返回当前统计和动态届别选项。

## 导入与审核

`POST /api/imports` 使用 multipart：`file` 是 CSV，`mapping` 是可选 JSON 对象，例如 `{"employer":"企业名称","url":"原文链接"}`。支持的内部字段：`employer,title,url,apply_url,deadline,cohorts,locations,published_at`。

返回导入数量、记录 ID、逐行错误与识别出的映射。最大 5MB、2000 行。导入默认为第三方线索，保留上游值，不自动认可其日期或企业性质。

`POST /api/reviews/{id}`：

```json
{
  "decision": "approve",
  "official_url": "https://企业官网/公告",
  "employer_proof_url": "https://企业官网/企业介绍",
  "extraction": {"说明": "完整结构使用详情中的 extraction，并按其类型修改"},
  "target_opportunity_id": null
}
```

`extraction` 可省略，服务会重新做规则解析；完整类型见运行时 OpenAPI 中的 `ExtractionResult`。接口重新获取原文并校验证据。变更可通过 `target_opportunity_id` 明确关联原记录，主体、届别和适用范围必须匹配。`decision=reject` 仅排除线索，不删除原文。

## 导出

`GET /exports/opportunities.csv`：接受列表筛选，UTF-8 BOM，防止表格公式注入。

`GET /exports/deadlines.ics`：接受相同筛选，只包含明确日期或时刻、核验通过、未关闭且 48 小时内确认的机会。日期生成全天事件；时刻换算为 UTC。UID 绑定具体机会，延期递增 SEQUENCE。导出文件不是自动订阅服务。

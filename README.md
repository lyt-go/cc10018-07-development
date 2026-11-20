# 指标服务与数据开放平台

面向指标口径管理、数据服务编排、API发布与调用治理的数据开放与分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标域管理 | `metric_domain` | 指标域 | 域编号、域名称、业务域描述 |
| 派生指标 | `derived_metric` | 派生指标 | 指标编号、父指标编号、计算公式 |
| API服务 | `api_service` | 数据接口 | 接口编号、接口名称、查询SQL |
| 查询优化 | `query_optimize` | 优化建议 | 建议编号、关联接口、慢查询摘要 |
| 接口订阅 | `api_subscribe` | 订阅记录 | 订阅编号、接口编号、订阅方 |
| 调用凭证 | `call_credential` | 访问凭证 | 凭证编号、订阅方、凭证摘要 |
| 数据源连接 | `datasource_connect` | 连接配置 | 连接编号、数据源类型、连接地址 |
| 调用追踪 | `call_trace` | 调用记录 | 记录编号、接口编号、调用方 |
| 数据预览 | `data_preview` | 预览模板 | 模板编号、数据源名、预查询SQL |
| 接口版本 | `api_version` | 版本管理 | 版本编号、接口编号、变更说明 |
| 批量导出 | `batch_export` | 导出任务 | 任务编号、查询SQL、导出格式 |
| 指标重名解析 | `metric_homonym` | 重名记录 | 记录编号、指标名称、口径A来源 |
| 数据聚合 | `data_aggregate` | 聚合配置 | 配置编号、数据来源、聚合维度 |
| 数据清洗 | `data_cleanse` | 清洗配置 | 配置编号、数据来源、清洗字段 |
| 访问看板 | `access_dashboard` | 访问统计 | 统计编号、接口编号、调用方 |
| SQL模板 | `sql_template` | 模板定义 | 模板编号、模板名称、SQL片段 |
| 结果缓存 | `result_cache` | 缓存配置 | 配置编号、缓存键、缓存引擎 |
| 查询审计 | `query_audit` | 审计日志 | 日志编号、接口编号、调用方 |
| 接口SQL契约包 | `api_contract` | 契约版本 | 接口编号、版本、参数声明、返回字段 |

## 接口SQL契约包

`backend/app/engine/sql_contract.py` 把一次查询的 **SQL、参数声明、返回字段** 冻结成不可变
版本（`ContractSnapshot`），一个接口同一时刻只有一个生效版本。

- **发布前校验**：`POST /api/api_contract/packages/{api_id}/validate` 启动会话，
  `POST /api/api_contract/validations/{token}/check` 一起确认 SQL、参数类型与返回字段；
  只有状态为「已校验」的会话才能 `publish`。草稿/待校验模板、只校验一半的会话、
  字段与投影不一致的契约都进不了发布结果。
- **一次请求一版契约**：API 服务绑定（`SqlTemplateBinder.expand_api_query` /
  `bind_contract`）与查询服务链（`run_query_service_chain`）都从契约包读取本次生效快照，
  参数绑定和结果整形（`shape_rows`）使用同一份快照，不会出现参数读新版、返回列读旧版。
- **中断与并发**：`abort` 只丢弃候选版本；校验开始时会对关联模板做指纹快照，发布时
  重新比对，校验期间模板被更正则本次校验失效（`校验失效`），旧版本继续对外取数，
  重新校验并发布后新版参数与列一起生效。
- 「发布接口」动作在 `ApiServiceService.run_action` 里复用同一套预校验，失败时接口状态不变。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

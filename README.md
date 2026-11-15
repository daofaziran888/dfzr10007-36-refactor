# 数据洞察与自助分析平台

面向业务分析师的自助式数据洞察平台。覆盖数据集市构建、自助SQL查询、可视化拖拽分析、分析看板协作、指标异动归因、分析模型沉淀、查询优化建议与数据故事编排的完整洞察链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据集市构建 | `mart` | 集市定义 | 集市编号、集市名称、数据源表 |
| 自助SQL查询 | `sqlpad` | 查询脚本 | 脚本编号、脚本名称、SQL语句 |
| 拖拽可视化 | `vizdrag` | 可视化方案 | 方案编号、方案名称、数据源绑定 |
| 看板协作管理 | `collab` | 协作看板 | 看板编号、看板名称、协作成员 |
| 指标异动归因 | `anomaly` | 异动记录 | 记录编号、指标名称、异动方向 |
| 分析模型沉淀 | `model` | 模型注册 | 模型编号、模型名称、模型类型 |
| 查询优化建议 | `opt` | 优化建议 | 建议编号、目标查询、优化类型 |
| 数据故事编排 | `story` | 故事模板 | 模板编号、故事标题、分析主题 |
| 数据准备流水线 | `etl` | 准备任务 | 任务编号、任务名称、输入源 |
| 数据目录检索 | `catalog` | 目录条目 | 条目编号、数据集名称、业务域 |
| 数据采样管理 | `sample` | 采样配置 | 配置编号、数据源名称、采样算法 |
| 同环比分析 | `compare` | 对比方案 | 方案编号、对比指标、基准周期 |
| 分析导出中心 | `export` | 导出任务 | 任务编号、数据源名称、导出格式 |
| 分析告警规则 | `alarm` | 告警规则 | 规则编号、监控指标、告警条件 |
| 数据定时刷新 | `refresh` | 刷新策略 | 策略编号、目标数据集、刷新周期 |
| 查询审计日志 | `audit` | 审计记录 | 记录编号、查询语句、执行人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.tsx`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 查询审计日志的版本边界

审计记录、复核状态与导出读取是三层独立的账本，不要互相覆盖：

| 层 | 存放位置 | 职责 |
| --- | --- | --- |
| 审计记录 | `store.rows("audit")` | 只存查询执行事实（语句、执行人、耗时、扫描/返回行数、执行时间） |
| 复核事件 | `store.internal_rows("audit_review_events")` | 每次动作追加一条事件，带事件号、版本号、操作时间 |
| 导出快照 | `store.internal_rows("audit_export_snapshots")` | 整份清单构建成功后才落库，失败不覆盖上一版 |

调用链与数据流：

```text
seed / 登记
  └─ 审计记录（不可变执行事实，按 执行时间 + 记录编号 稳定排序）
       └─ 复核事件流（已记录 → 已复核 → 已归档 单向推进）
            ├─ GET /api/audit               列表：记录 + 最新事件视图
            ├─ GET /api/audit/{id}          明细：同上
            ├─ GET /api/audit/events        事件流：事件号 / 版本号 / 操作时间
            ├─ GET /api/audit/export        完整构建 → 快照落库 → 返回清单
            ├─ GET /api/audit/export/snapshots  历史快照（批次号、导出时间、源记录编号）
            └─ GET /api/audit/duration-summary  查询耗时汇总（唯一口径）
                 └─ 查询优化建议 GET /api/opt 的“当前耗时”直接读取该汇总
```

状态推进规则：

- 只能 `已记录 → 已复核 → 已归档`，不能回退，也不能跳过复核直接归档。
- 重复归档幂等：返回首次归档的事件号，不再追加第二条事件。
- 动作请求体可带 `版本号`（前端会自动带上当前版本）；两个批次并发写同一条审计记录时，
  只有一个能提交，另一个返回“本次写入失败”，事件流里不会出现两条同版本事件。
- 历史记录缺失版本号时的补齐规则：完全无事件的记录按当前状态补一条基线事件，版本号取
  该状态在状态序列中的位次（1/2/3）；已有事件但缺版本号时按“操作时间 → 事件号 → 内部
  id”排序补齐下一个可用版本号。基线事件的操作时间取执行日期零点并标记“历史补齐”，
  重复读取不会重复补齐。

既有清单稳定：AUDI-0001~AUDI-0003 的复核事件号分别为 `20260901-01`、`20260902-02`、
`20260903-03`；`AUDI-0002` 的 `820ms` 与 `AUDI-0003` 的 `1.4s` 按各自源记录编号保留，
即使执行时间相同也不会合并成一条耗时汇总。

回归：`cd backend && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m unittest discover -s tests -v`

领域：数据工程
子方向：数仓与数据湖

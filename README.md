# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 表优化执行计划

表优化管理（`iceberg`）在原有任务列表之上增加了执行计划批次，链路如下：

1. `POST /api/iceberg/plans`：按“预计收益 / 预算需求”排序候选任务，在预算总量内贪心选择，
   生成 `PLAN-YYYYMMDD-NN` 批次。预算占用不会超过预算总量；同一张表同时只保留一个活动计划
   （并发提交同表任务时复用已有计划）。
2. `POST /api/iceberg/plans/{批次编号}/actions`：`确认计划 → 执行计划 → 完成计划 → 发布批次`
   单向推进，跳过顺序会被拒绝并给出失败环节。容量统计版本变化后，计划会标记版本冲突，
   需要选择 `沿用冻结顺序`（任务与排位不变）或 `按新统计重排`（重算候选与预算）。
3. `GET /api/iceberg/plans`：支持 `min_benefit`（按收益筛选）、`sort=capacity`（按容量排序）、
   `page/size`（分页）与 `table/keyword`（表名检索）。
4. `GET /api/iceberg/plans/{批次编号}/diagnose`：按调用链返回读取任务、预算校验、同表校验、
   版本校验、状态推进、效果同步各环节的结果，用于定位表优化管理失败环节。
5. `GET /api/iceberg/effects`：优化效果统计，只展示最终执行批次的实际效果（被选任务、
   实际文件数、容量变化、恢复结论）。
6. `GET/POST /api/iceberg/capacity`：容量统计当前版本与更新入口。

表统计信息（`stats`）沿用同一版本执行计划：批次报告会记录被选任务、文件数与容量变化，
缺执行计划字段的历史报告按“历史记录”只读展示；已发布版本的统计报告不能回退状态。

示例：`ICEB-0001`（dws_customer_360）18 600 个文件、860GB，资源预算 520GB；
批次完成后实际 4 100 个文件、510GB，文件数变化 -14 500、容量变化 -350GB。

### 测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖

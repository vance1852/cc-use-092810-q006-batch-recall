# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/batch_recall/`：供应商批次风险通知、冻结组件谱系、有效所有权版本、可复算召回范围版本、按资产推进的措施链、升级队列与召回看板；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
PYTHONPATH=src python3 -m batch_recall.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量流程和批次召回编排（初始范围、谱系扩散扩展、转移中约束、缩减批准、升级队列、逾期扫描与版本复算），不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m batch_recall.api --database batch-recall.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 批次召回编排

`batch_recall` 解决供应商批次风险通知发出后，组件经装配、转售、租赁、返修和拆分导致的
持有人不确定与重复处置问题：

- **范围版本可复算**：谱系证据（`genealogy_edges`）与所有权版本（`ownership_versions`）只增不改；
  初始范围由冻结谱系修订与截止时刻的有效所有权计算，新证据扩散时生成新版本，每个版本保存
  输入水位（谱系修订、截止时刻、父版本摘要、原始证据摘要）与规范化内容摘要，
  `POST /recalls/{id}/scopes/{v}/recompute` 可从原始证据逐级重放复算。
- **措施按资产独立推进**：通知、签收、隔离、现场检查、返厂、解除六阶段，回执 append-only；
  重复回执记为 `duplicate`、乱序回执记为 `out_of_order`，均不倒退已完成状态，支持幂等键重放。
- **转移中约束保留**：所有权版本为 `in_transfer` 时措施继续推进，回执记录当时持有人与版本。
- **缩减独立批准**：缩减先生成 `pending_approval` 版本，且申请人不能自批；批准前不影响措施，
  批准后停止未来措施，但通知与完成历史原样保留。
- **升级队列**：无有效所有权版本自动升级；持有人无法联系、拒收、SLA 逾期均可进入队列，
  解决后可重新挂起 SLA 继续推进。
- **召回看板**：`GET /recalls/{id}/dashboard` 汇总每次范围变化的原因与版本、当前责任方
  （含转移中数量）、逾期动作、受影响容量与仍在运行的受影响容量。

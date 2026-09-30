# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
- `src/recall_orchestration/`：供应商批次风险通知、冻结组件谱系、有效所有权版本、版本化召回范围（初始/上下游扩散/缩减独立批准）、每资产单调措施编排（通知、签收、隔离、现场检查、返厂、解除）、所有权转移约束、升级队列与逾期 SLA、负责人仪表盘及可复算审计；
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
PYTHONPATH=src python3 -m recall_orchestration.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成资产调拨、状态评估、组件质量和批次召回编排流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m recall_orchestration.api --database recall-orchestration.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 批次召回编排

供应商批次风险通知发布后，召回负责人从**冻结的组件谱系证据**（只增修订、内容指纹与证据链）与**有效所有权版本**（按生效时间解析当前持有人）计算初始范围；新的谱系证据表明风险向上下游扩散时，生成只增的新范围版本提案，经独立批准岗位批准后生效（扩散与当前范围取并集，范围不丢成员；缩减必须独立提案和批准，且不抹除任何已通知事实）。

通知、签收、隔离、现场检查、返厂、解除六类措施**按资产独立推进**：物理阶段单调不可逆，重复回执幂等回放、乱序或倒退回执一律拒绝；所有权转售、租赁、返还、返修与拆分中资产**保留召回约束**（物理阶段不回退，当前责任方随有效所有权更新，新持有人需新一轮通知，期间进入“转移待通知”升级队列）。无法联系的持有人与超过通知 SLA 的逾期动作进入升级队列。负责人仪表盘展示当前版本与变化原因、当前责任方分布、阶段计数、逾期升级以及**仍在运行的受影响顶层容量**；审计人员可用任一版本固化的快照与输入逐字段复算，全部操作进入哈希链审计事件。

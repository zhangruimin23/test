# Data Engineering CLI Demo

一个基于 PubChem PUG REST API 的科研公共数据源采集 CLI，实现 `fetch`、`sync`、`validate` 核心链路，并可用 `--mock` 离线演示。

## 技术选型

- Python 3.10+ 标准库：零第三方依赖，使用 `urllib` 调用 REST API。
- PubChem PUG REST：公开稳定，提供化合物唯一 CID 和结构化属性。
- SQLite：保存已同步记录和 watermark，支持重跑幂等。
- JSON / JSONL：原始响应与处理后的记录均可直接审计。

## 项目结构

```text
dataeng-cli-demo/
  dataeng_cli.py       CLI 实现
  README.md            运行和设计说明
  解决方案.md           面试题解决方案
  Dockerfile           容器化运行定义
  Makefile             常用演示和验证命令
  data/                运行后生成：raw、processed、state.db、质量报告
```

## 安装与环境变量

无需第三方依赖。要求 Python 3.10+；调用真实 PubChem API 时需能访问 `pubchem.ncbi.nlm.nih.gov`。没有敏感配置或必需环境变量。

```powershell
cd D:\dw_mbr_aihub\test\数据问题\面试题\dataeng-cli-demo
python .\dataeng_cli.py --help
```

## 命令示例

```powershell
# 离线采集：保存原始响应和规范化记录
python .\dataeng_cli.py fetch --source pubchem --query aspirin --output .\data --mock

# 增量同步：第二次执行将 skipped 计数加一
python .\dataeng_cli.py sync --source pubchem --query aspirin --since 2026-07-01 --state .\data\state.db --mock
python .\dataeng_cli.py sync --source pubchem --query aspirin --state .\data\state.db --mock

# 质量校验并落盘报告
python .\dataeng_cli.py validate .\data\processed --format json --output .\data\quality-report.json
```

去掉 `--mock` 可调用真实 PubChem API。

## 加分项

- `--mock`：`fetch`、`sync` 无需真实网络即可演示。
- `--result-output`：`fetch` 将本次执行摘要保存为 JSON；例如 `--result-output .\data\fetch-result.json`。
- `sync --output`、`validate --output`：分别保存同步结果和质量报告 JSON。
- `--log-file`：三个命令均可追加 JSON Lines 结构化日志；例如 `--log-file .\data\events.jsonl`。
- `Dockerfile`：可使用 `docker build -t dataeng-cli-demo .` 构建镜像，随后运行 `docker run --rm dataeng-cli-demo --help`。
- `Makefile`：执行 `make demo` 可完成离线演示，`make verify` 执行语法检查和质量校验。

## 已实现功能

- PubChem 关键词或纯数字 CID 查询，连续真实请求间隔至少 0.2 秒；具备超时、指数退避重试、不可达和无结果错误提示。
- 带 UTC 时间戳的原始 API JSON 落盘，确保可追溯。
- 规范化 JSONL 包含来源唯一 ID、查询词、化合物属性、采集时间。
- SQLite 保存 watermark，按 `pubchem:<CID>` 去重并输出新增、更新、跳过统计。
- 校验必填字段完整率、ID 格式、分子量类型、重复率；`comment` 会按实际校验结果列出通过项和失败原因；空目录和无效 JSON 安全处理。
- `--mock` 支持完整离线演示。

## 已知限制

PubChem 名称属性端点不提供按更新时间的变更流。本 Demo 将 `since` 作为水位线审计信息，并通过 CID 幂等 upsert 保证安全重跑。生产实现应对接支持更新时间游标的数据端点，或维护候选实体清单和内容哈希变更检测。

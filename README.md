# SourceFlow

SourceFlow 是一个本地多源内容采集原型。Python 采集器通过 BrowserAct 控制已登录的 Chrome，检查配置的 X、小红书和 B 站账号，把内容标准化后写入 SQLite；Codex 可读取本次新增 JSON 生成摘要。仓库还包含一个独立的 OpenRadar 演示应用。

当前实现、边界与改进顺序见[实现记录与改进方案](docs/current-implementation-and-improvement-plan.md)。本仓库不包含个人订阅清单、浏览器凭据、原始采集数据、数据库或本机自动任务配置。GitHub Trending 的实际采集位于独立项目，不在此采集器的默认运行范围内。

## 本地配置

使用 `uv` 管理 Python 环境：

```bash
uv sync
cp config/sources.example.json config/sources.json
```

编辑 `config/sources.json`，把示例账号换成自己要检查的来源，并设置 `since_at`。该文件已被 Git 忽略。采集器还需要已安装的 BrowserAct CLI、可用的 Chrome 配置及相应网站的浏览器登录态。

```bash
uv run python -m sourceflow.collector
```

运行结果写入本地 `data/sourceflow.sqlite3` 与 `data/raw/runs/YYYY-MM-DD/`。这些路径均被 Git 忽略。采集器默认检查 `x,xiaohongshu,bilibili`；可用 `--platforms` 指定子集。当前运行记录中的 `success` 不能保证所有历史页面均已覆盖，详情见实现记录。

## 样本转换与演示应用

`examples/ingestion/` 中的四份 JSON 是虚构样本，只用于验证数据格式：

```bash
uv run python -m sourceflow.cli
uv run uvicorn app.main:app --reload
```

## 测试

```bash
uv run python -m pytest
```

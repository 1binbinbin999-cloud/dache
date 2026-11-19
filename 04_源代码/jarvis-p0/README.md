# Jarvis P0 — 离线启动原型

版本：0.1.0-p0，2026-11-19
目标：证明 Agent Core 是确定性程序，不依赖任何外部 AI 模型和网络，能启动、读自己的数据、跑本地任务。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-p0
python3 jarvis.py boot
python3 jarvis.py health
python3 jarvis.py list-tasks
python3 jarvis.py list-tasks --id task-001
python3 -m unittest discover -s tests -v
```

只用 Python 标准库（sqlite3、json、argparse），无第三方依赖，无需安装。

## 文件

- `jarvis.py`：CLI 入口，三个命令 boot / health / list-tasks。
- `core.py`：确定性核心。启动顺序 config → database → policy → audit → scheduler；事件日志 JSONL 追加写；SQLite 任务库，种子任务固定 id + INSERT OR IGNORE 保证幂等。
- `config.yaml`：配置，不含密钥。`model_enabled: false`，`network_calls_allowed: false`，P0 只允许 `list_tasks` / `read_task`。
- `data/jarvis.db`：运行后生成，SQLite 任务库（5 条合成任务）。
- `data/events.jsonl`：运行后生成，事件日志。
- `tests/test_p0.py`：4 项测试（正常启动、无 Key/无网启动、重启幂等、复制目录导出启动）。

## 不做什么

- 不装 Ollama，不调任何 API，不做记忆提取，不做 Planner。
- 不用真实对话/文件做测试，种子任务全部是合成数据。
- 不引入 YAML 第三方库：P0 只做最小文本检查，完整 YAML 解析留给 P1。

## 验收（见 05_测试记录/P0-测试记录.md）

1. 无任何 API Key、无网络时，`jarvis boot` 成功，健康检查通过。
2. 能读本地 SQLite 种子数据，列出 5 条任务。
3. 事件日志追加写，重启后不丢。
4. 复制目录到新位置仍能启动。
5. 代码中无任何模型供应商 SDK 的 import（grep 可验证）。

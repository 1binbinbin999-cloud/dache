# Jarvis P3 — 工具网关原型

版本：0.4.0-p3，2026-11-19
目标：证明工具调用先过权限检查再执行，拒绝的调用不执行、只记审计；系统级断网时核心照常启动。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-p3
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py tool-list
python3 jarvis.py tool-call --tool list_tasks
python3 jarvis.py tool-call --tool delete_file --args-json '{"path": "data/jarvis.db"}'
python3 jarvis.py tool-call --tool router-call --args-json '{"logical": "fast", "prompt": "你好"}'
python3 jarvis.py sandbox-check
python3 -m unittest discover -s tests -v   # 25 项测试，约 31 秒
```

## 文件

- `gateway.py`：Tool Gateway。工具注册表（副作用等级 read/write/destructive/external）；权限检查在执行前；拒绝的调用不执行，只记 tool_denied；允许的记 tool_call（工具名、参数 hash、结果 hash、耗时）。
- `sandbox.py`：真断网演练。用 `unshare -rn` 创建无网络接口的命名空间，在里面跑 boot + router-call；命名空间内连 8.8.8.8:53 返回 101（Network is unreachable）。
- `jarvis.py`：P2 的全部命令 + tool-call / tool-list / sandbox-check。
- `config.yaml`：policy 允许 list_tasks / read_task / memory-recall / memory-add / router-call（Stub 模式）；denied 写文件/删文件/发邮件/外部 API；network_calls_allowed false；gateway 与 sandbox 两节新增。
- `tests/test_p3.py`：7 项 P3 测试；`tests/test_p2.py` 8 项 + `tests/test_p1.py` 6 项 + `tests/test_p0.py` 4 项为回归。

## 权限规则（按顺序）

1. 不在注册表里 → 拒绝 unknown_tool
2. 在 denied_tools 里 → 拒绝 denied_by_policy
3. 不在 allowed_tools 里 → 拒绝 not_in_allowed_tools
4. destructive 一律拒绝（P3 还没有审批机制）
5. external 且 network_calls_allowed=false → 拒绝；例外是 router-call 在 Stub 模式下（不产生真实外部调用）

## 验收（见 05_测试记录/P3-测试记录.md）

1. 未授权工具调用被拒绝且记审计。
2. 写文件/发邮件等高风险工具默认拒绝。
3. 同一任务在 P2 路由上走通（Stub 回答 + 本地事实召回 + 工具调用审计一致）。
4. 真断网启动通过（P0 遗留闭环）。

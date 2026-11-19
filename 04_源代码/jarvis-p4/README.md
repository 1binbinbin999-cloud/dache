# Jarvis P4 — 规划器原型

版本：0.5.0-p4，2026-11-19
目标：证明任务可以分解成步骤，每步经网关顺序执行，失败一步即停，任务可重跑且结果一致。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-p4
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py plan-create --goal "早上简报" --template morning-brief --id plan-demo
python3 jarvis.py plan-run --id plan-demo
python3 jarvis.py plan-status --id plan-demo
python3 jarvis.py plan-list
python3 -m unittest discover -s tests -v   # 32 项测试，约 40 秒
```

## 文件

- `planner.py`：Planner。任务 = 目标 + 有序步骤，每步绑一个已注册工具 + 参数；建任务时校验工具在注册表里；执行时全部经网关（actor="planner"），失败一步即停；任务与步骤存在 SQLite（plans / plan_steps 表）；两个确定性模板 morning-brief / learn-and-record。
- `gateway.py`：P3 全部功能 + 审计脱敏摘要（args_summary 只记参数键名，不记值）+ memory-add 支持固定 id（fact_id）。
- `memory.py`：P3 全部功能 + add_fact 固定 id 幂等（已存在时返回 already_exists，不重复写入）。
- `jarvis.py`：P3 的全部命令 + plan-create / plan-run / plan-status / plan-list。
- `tests/test_p4.py`：7 项 P4 测试；`tests/test_p3.py` 7 项 + `tests/test_p2.py` 8 项 + `tests/test_p1.py` 6 项 + `tests/test_p0.py` 4 项为回归。

## 任务状态

- 任务：pending → running → done / denied / failed
- 步骤：pending → running → done / denied / failed
- denied：某步被网关拒绝，之后步骤保持 pending，不执行
- failed：某步执行抛异常，之后步骤保持 pending，不执行
- 重跑：重置全部步骤为 pending，从第一步重新执行；确定性步骤重跑结果 hash 相同

## 验收（见 05_测试记录/P4-测试记录.md）

1. 三步任务（召回记忆 → 路由问答 → 写记忆）全程经网关，审计链完整。
2. 其中一步被拒绝时任务停下并报告，不绕过网关。
3. 任务可重跑，重跑结果一致。
4. P3 待定项已定：审计脱敏摘要只记键名；网关 actor 区分 user 与 planner。

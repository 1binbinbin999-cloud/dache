# Jarvis P5 — 自我改进原型（第一阶段最后一轮）

版本：0.6.0-p5，2026-11-19
目标：证明代码改进必须走提案 → 测试 → 评价 → 批准 → 部署 → 监控，六步缺一不可；未经批准不改代码、不改配置、不改权限；部署后可回滚。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-p5
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py improve-propose --title "gate 增加 sudo rm" --target memory.py --old-text '"rm -rf",' --new-text '"rm -rf", "sudo rm",' --id imp-demo
python3 jarvis.py improve-test --id imp-demo
python3 jarvis.py improve-approve --id imp-demo
python3 jarvis.py improve-deploy --id imp-demo
python3 jarvis.py improve-monitor --id imp-demo
python3 jarvis.py improve-rollback --id imp-demo
python3 jarvis.py improve-status --id imp-demo
python3 -m unittest discover -s tests -v   # 38 项测试，约 81 秒
```

## 文件

- `improvement.py`：自我改进流程。proposals / evaluations / deployments 三张表，存在独立的 data/improvements.db（不与 jarvis.db 共库，见问题记录）；六步状态机 proposed → evaluated → approved → deployed，rolled_back 为终态之一；不可变规则四条。
- `jarvis.py`：P4 的全部命令 + improve-propose / improve-test / improve-approve / improve-deploy / improve-rollback / improve-monitor / improve-status / improve-list。
- `tests/test_p5.py`：6 项 P5 测试；`tests/test_p4.py` 7 项 + `tests/test_p3.py` 7 项 + `tests/test_p2.py` 8 项 + `tests/test_p1.py` 6 项 + `tests/test_p0.py` 4 项为回归。

## 六步规则

1. 提案：改动 = (目标文件, 旧文本, 新文本)，旧文本必须逐字存在；不可变规则命中即拒绝。
2. 测试：改动先在临时复制目录里应用，跑 boot + P1 记忆测试，绝不在正式目录试改。
3. 评价：测试通过 verdict=pass，否则 fail，存进 evaluations 表。
4. 批准：只有评价 pass 才能批；批准是用户在 CLI 上的显式动作。
5. 部署：先备份原文件到 data/backups/，再应用，跑 boot 冒烟；失败则恢复备份。
6. 监控：部署后跑 boot + health + 写时 gate 探针；不健康时用 improve-rollback 恢复。

## 不可变规则（提案阶段直接拒绝）

- 改 config.yaml 的 policy（allowed_tools / denied_tools / network_calls_allowed）
- 改 gateway.py 的 destructive 拒绝逻辑
- 改 core.py 的启动顺序
- 改 improvement.py 自身的批准检查

## 验收（见 05_测试记录/P5-测试记录.md）

1. 一份改进提案走完六步，测试通过、评价记录、用户批准后才部署，部署后可回滚。
2. 未经批准的修改被拦截且记审计。
3. 第一阶段 P0–P5 全部通过，共 38 项测试。

# Jarvis P2 — 路由切换原型

版本：0.3.0-p2，2026-11-19
目标：证明换模型只改配置，模型挂了能自动切换，全部挂了能降级不瘫痪，每次调用都有审计。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-p2
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py router-list
python3 jarvis.py router-call --logical fast --prompt "你好"
python3 jarvis.py router-call --logical fast --prompt "你好" --fail stub-fast
python3 jarvis.py router-call --logical fast --prompt "你好" --fail stub-fast,stub-capable,stub-local
python3 jarvis.py memory-rollback --correction-id corr-xxx
python3 jarvis.py memory-import --in data/memory-export.jsonl
python3 -m unittest discover -s tests -v   # 18 项测试，约 27 秒
```

## 文件

- `router.py`：Model Router。逻辑模型名 fast/capable/reasoning/local/embedding，映射在 config.yaml；Stub 供应商（固定格式回答，成本 0）；切换链首选 → 备选 → 本地，每家最多一次；全失败走 L0 规则降级；每次调用记审计（模型、token、成本、耗时、结果 hash）。
- `jarvis.py`：P1 的全部命令 + router-call / router-list / memory-rollback / memory-import。
- `memory.py`：P1 全部功能 + rollback_correction（一键改回）+ import_jsonl（导入验证，幂等）。
- `config.yaml`：model 一节启用 router，router_mode stub，litellm_proxy_enabled false（占位，接真实供应商时才启用）。
- `tests/test_p2.py`：8 项 P2 测试；`tests/test_p1.py` 6 项 + `tests/test_p0.py` 4 项为回归。

## 切换链

- fast：stub-fast → stub-capable → stub-local
- capable：stub-capable → stub-local
- reasoning：stub-reasoning → stub-capable → stub-local
- local：只走 stub-local，不回落云端（否则“本地”承诺会被悄悄打破）
- embedding：stub-embedding → stub-capable → stub-local

## 审计

每次 router-call 都在 events.jsonl 里写一条 router_call（模型、token、成本、耗时、结果 hash、degraded、attempted）；每次切换写一条 router_fallback。Stub 的 token 是按词数算的近似值，不是真实分词器的结果；成本恒为 0.0。

## 验收（见 05_测试记录/P2-测试记录.md）

1. 同一任务在 Stub A → Stub B 间切换，只改配置，输出格式与 Audit 一致。
2. 任一环节超时/失败即切换，不死循环。
3. 每次调用记模型、token、成本、耗时、结果 hash。
4. 无 Key 时直接走降级，不报错瘫痪。
5. P1 遗留：一键改回（memory-rollback）、导出导入验证（memory-import）已做。

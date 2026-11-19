# Jarvis P1 — 记忆治理原型

版本：0.2.0-p1，2026-11-19
目标：证明记忆不是“存聊天记录”，而是能写对、改对、过期能被取代，错误不污染后续推理。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-p1
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py health
python3 jarvis.py memory-extract --dialogue "我喜欢跑步" --source "user:合成对话001"
python3 jarvis.py memory-recall
python3 jarvis.py memory-correct --id mem-xxx --new-text "用户不吃辣" --evidence "用户在3次合成对话中明确说不吃辣"
python3 jarvis.py memory-export --out data/memory-export.jsonl
python3 -m unittest discover -s tests -v   # 10 项测试，约 18 秒
```

## 文件

- `jarvis.py`：CLI，P0 的 boot/health/list-tasks + memory-add / memory-extract / memory-recall / memory-correct / memory-export。
- `core.py`：P0 核心 + P1 变化：config 改用 PyYAML 真解析；boot 时同时初始化记忆表；health 增加 memory_tables_ok 检查。
- `memory.py`：记忆治理。表 memory_facts / quarantine / corrections；写时 gate；规则模板提取；修正（superseded + Correction 记录）；导出 JSONL（schema_version memory-facts-v1）。
- `config.yaml`：新增 memory 一节（schema_version、min_confidence 0.3、extraction rule_template、correction_keeps_history true）。
- `requirements.txt`：PyYAML==6.0.1，唯一新增依赖。
- `tests/test_p1.py`：6 项 P1 测试；`tests/test_p0.py`：4 项 P0 回归测试。

## 写时 gate（确定性规则，无模型）

- 隔离（quarantined，进 quarantine 表，不进 memory_facts）：空/过短、类型非法、置信度越界、证据等级非法、来源缺失、命中注入模式（忽略之前、删除所有、rm -rf、drop table、删库、以管理员身份、system prompt、泄露密码/密钥、假装你是等）、单次证据 + 绝对化表述（所有人/永远/100%）+ 低置信度、置信度 <0.3。
- 需复核（needs_review，落为 provisional）：高置信度 + 单次证据。
- 接受（accepted）：其余；置信度 ≥0.8 且证据为 user_stated/verified/multiple_occurrences 时为 verified，否则 provisional。

## 召回规则

只召回 verified / provisional；superseded / expired / disputed 不召回。过期不是删除：valid_until 已过，召回时自动标 expired，旧记录保留可查。

## 验收（见 05_测试记录/P1-测试记录.md）

1. 100 条合成对话全部存入，召回 100 条。
2. 过期事实被标 expired，不被召回。
3. 注入 10 条假事实，拦截 ≥8 条（实测 9/10，第 9 条是正常事实，预期接受）。
4. 画像修正生成 Correction 记录，旧判断保留历史，新判断可召回。
5. 导出 JSONL，每行含 schema_version memory-facts-v1。
6. P0 回归 4 项仍通过。

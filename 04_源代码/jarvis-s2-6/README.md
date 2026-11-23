# Jarvis S2-4 — JIS 遗留项收尾（第二阶段第四步）

版本：0.10.0-s2-4，2026-11-21
目标：处理台账上全部 10 项未解决 JIS（007、010、013、015、016、017、018、021、022、023）。
结果：8 项闭环（010、013、015、017、018、021、022、023），2 项仍敞口（007、016——均待用户定阈值；016 另待人工评价）。
台账新计数：已关闭 20、已接受 1、未解决 2（20 + 1 + 2 = 23）。
新增 tests/test_s2_4.py（27 项）；全部 $0，真实 Gemini 调用 0 次。
详见 `00_项目总纲/S2-4-JIS遗留项收尾记录.md`。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-s2-4
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py health
python3 jarvis.py provider-status   # 各供应商状态，不耗额度
python3 jarvis.py router-call --logical local --prompt "你好"   # 默认 stub 模式
python3 jarvis.py usage-audit   # 审计 usage 日志（无日志时报 log_missing，exit 1）
python3 -m unittest discover -s tests -v   # 全量 123 项测试
```

## 两种模式（只改 config，不改代码）

- **stub（默认，出厂状态）**：全部 Stub，离线安全，行为与 P2–P6 完全一致。
  `core.model_enabled: false`、`policy.network_calls_allowed: false`。
- **live**：走真实适配器。须同时改三项，缺一不可（boot 会校验，不一致拒绝启动）：
  `model.router_mode: live`、`core.model_enabled: true`、
  `policy.network_calls_allowed: true`。

live 映射与切换链（config.yaml model 一节）：
- fast / capable / reasoning → `gemini-flash`，链：Gemini → Ollama 本地 → Stub。
- local → `ollama-local`，链：Ollama 本地 → Stub。
- embedding → `ollama-embedding`，链只在 embedding 类内（P6-15）。
- Groq / OpenRouter 只留桩位（`status: not_implemented`），报 not_implemented 后继续切链，不产生调用。

## S2-4 新增能力

- **destructive 工具批准放行（JIS-013）**：`tool-approval-request`（申请，绑工具 + 参数哈希）
  → `tool-approval-approve`（批准，记操作者）→ `tool-call --approval-id`（一次消费后执行，
  审计含批准 id 与批准人）。未批准仍拒；参数不符拒；目录外路径拒删。
  计划步骤可带 `approval_id`。注意：走的是工具批准流，不是 P5 六步代码改进流。
- **任务续跑（JIS-017）**：`plan-resume --mode retry|skip|retry-args`，从失败步续跑，
  已 done 步骤不重跑（结果 hash 不变），`plan_resumed` 审计连续。
- **任务 gate 报告（JIS-018）**：每步 `gate_result` + 任务 `gate_summary`
 （written/quarantined/needs_review/already_exists）；被隔离步骤不记“已写入”。
- **多文件提案（JIS-021）**：`improve-propose-multi --changes-json`，一次提案含多文件改动；
  测试与回滚按文件粒度；`deployments` 表每文件一行。
- **批准身份与双人复核（JIS-022）**：批准记操作者（`--operator`、或 `JARVIS_OPERATOR` 环境变量、
  或系统用户名）+ 时间，进 `approvals` 表，`improve-status` 可查。
  双人复核规则：`config.yaml` 的 `approval.dual_review`（默认 false，单用户）。
  开启时，同一提案须两名不同操作者批准才生效；同一操作者批两次只算一人；
  第二人批准前提案状态保持 evaluated，不可部署。
- **失败部署进表（JIS-023）**：部署失败写进 `deployments` 表（status=failed + 原因），
  `improve-status` 可查；失败部署没有成功行可回滚。
- **沙箱完整流程（JIS-015）**：`sandbox-check-full`，断网命名空间内 boot + 记忆写 + 召回 + 导出，
  真实库无泄漏；`run_offline` 补资源上限（P6 遗留之一，本轮修掉）。
- **模型分解（JIS-016）**：`plan-decompose --goal`，固定提示词要求模型只输出 JSON，
  解析后按白名单校验（仅 list_tasks、read_task、memory-recall、memory-add、router-call；
  破坏性工具不得由模型拆出）。拆分质量须人工评价，本轮只做 10 目标探查（原文见
  `tests/data/decompose-probe-10-goals.jsonl`），不自评通过。
- **gate 复核（JIS-007，建议型）**：`gate_review.py::review_text`，`memory-gate-review --text`。
  复核只给建议，永不改 gate 决定；无模型/解析不出时返回 unavailable，不猜。
  语料库 `tests/data/gate_corpus_*.txt` 实测：注入 28 条拦截 17 条（60.7%），
  良性 29 条误拦 5 条（17.2%）。`WriteGate.check` 未动（P6 冻结测试锁定）。
- **Retry-After（S2-3 §七）**：429 响应头 Retry-After（仅秒数）参与退避，
  上限 `retry_backoff_max_s`（默认 30）；无头时用配置退避。
- **usage 哈希链（S2-3 §七）**：UsageLog 写 prev_hash/entry_hash + `usage.checkpoint.json`；
  `verify_usage_chain()`（verified/legacy/broken/missing）；旧格式日志判 legacy，不误报。

## 文件

- `gate_review.py`：新增。建议型复核（suspicious/benign/unavailable）+ 语料测量 `measure_gate`。
- `adapters.py`：读 Retry-After，`AdapterError` 带 `retry_after`，退避上限 `retry_backoff_max_s`。
- `usage.py`：UsageLog 哈希链 + 检查点；`verify_usage_chain()`；`audit_usage_log` 返回 `chain` 字段，
  ok 要求内容 ok 且链不为 broken。
- `gateway.py`：`execute(..., approval_id=None)`，destructive 工具须持有效批准；
  `_dispatch` 增加 `delete_file`（限工程目录内）；批准审计。
- `improvement.py`：`approvals` 表；`changes_json`/`patched_json`；`propose_multi`；
  `request_tool_approval`/`approve_tool`/`consume_tool_approval`；`approve(operator=)` 记身份 +
  双人复核；`deploy()`/`rollback()` 多文件；失败部署进表。
- `planner.py`：`plan_steps` 增加 `gate_result`、`approval_id`；`resume()`；
  `decompose()` + `DECOMPOSE_ALLOWED_TOOLS` 白名单；`gate_summary`。
- `sandbox.py`：`run_offline` 资源上限；`sandbox_check_full()`。
- `jarvis.py`：新 CLI：`tool-approval-request`、`tool-approval-approve`、
  `tool-call --approval-id`、`sandbox-check-full`、`plan-resume`、`plan-decompose`、
  `improve-propose-multi`、`improve-approve --operator`、`memory-gate-review`。
- `config.yaml`：版本 0.10.0-s2-4；新增 `approval:` 节（dual_review: false）；
  gemini-flash `retry_backoff_max_s: 30`。
- `tests/test_s2_4.py`：新增 27 项（见 S2-4 记录 §三）。
- `tests/data/`：`gate_corpus_injection.txt`（28 条）、`gate_corpus_benign.txt`（29 条）、
  `decompose-probe-10-goals.jsonl`（10 目标原文 + 2 执行抽查）。
- `evaluator_hashes.json`：已重生成（含 tests/test_s2_4.py 与新 config.yaml；其余 10 个旧哈希未变，
  证明旧测试未动）。

## 测试

全量 123 项：P0 4 + P1 6 + P2 8 + P3 7 + P4 7 + P5 6（原 38 项）+ P6 验收 15 项实跑
（#1–#2 按用户决定 skip，未实跑验证）+ P6 配套 2 项 + S2-1 13 项 + S2-2 11 项 + S2-3 15 项
+ S2-4 27 项。本次全量回归 123 项通过（skipped=7），867 秒；补跑 5 个 Ollama 实测后，
仅剩 P6 第 1、2 项跳过（用户定 B）。第一次全量跑被运行时重启打断（无结果记录），作废重跑，
第二次跑结果如上。S2-4 的 27 项：JIS-013（1–5）、JIS-015（6）、JIS-017（7–9）、
JIS-018（10）、JIS-021（11–13）、JIS-022（14–15）、JIS-023（16）、JIS-007（17–19）、
JIS-010（20）、JIS-016（21）、Retry-After（22–24）、usage 链（25–27）。

## 实测结果（2026-11-21）

- **Ollama 0.40.16**：本轮虚拟机换了两次，每次都要重装（见下）。
  第二次换机后的新 VM 缺 `zstd`，`apt-get update` 取 71.3 MB 用 8 分 19 秒（143 kB/s），
  装 `zstd` 后官方脚本重装完成；服务加 proxy.conf（无凭证代理 + SSL_CERT_FILE +
  GODEBUG=http2client=0）。
- **qwen2.5:0.5b**（a8b0c5157701，与 S2-1/S2-2/S2-3 同摘要）与 **nomic-embed-text**
  （0a109f422b47）均已拉取。
- **JIS-010 抽查**（test_20）：10 次真实 Ollama 调用（5 chat 含中文提示词 + 5 embedding），
  audit ok，total=10，mismatches=[]，链 verified；中文提示词 tokens_in > 1。
- **JIS-016 探查**：10 目标本地分解 10/10 decomposed（9 个 1 步、1 个 2 步）；
  结构事实见 S2-4 记录 §五（目标 6 检索却拆出 memory-add；目标 8 空参数；
  目标 1/7/9 参数为模型自创结构；目标 1 的拆分计划在 live 默认离线下被网关拒
  external_requires_network）。人工评价与阈值待用户定。
- **JIS-007 复核探查**：live 0.5b 对 4 条文本全判 suspicious（含两条良性），
  无判别力实测支撑；复核永不改 gate 决定，无误放行风险。
- **Gemini**：本轮真实调用 0 次。S2-2 的 Gemini 实测与限额数字（RPM 5、TPM 250K、RPD 20）
  见 S2-2 记录；Groq / OpenRouter 用户已定不绑卡，要求绑卡即不用。

## 验收（S2-4）

1. 台账 10 项未解决中 8 项按验收条件闭环（测试证据见 S2-4 记录 §二、§三）。
2. JIS-007、JIS-016 不为闭环而闭环：实测数与原文已交，阈值与人工评价待用户定。
3. 全部旧测试继续通过（全量 123 项，0 失败）。
4. 成本 $0；无新依赖；P0–P6 与 S2-1–S2-3 的源码与旧测试一个字没动
   （evaluator 哈希复核：除 config.yaml 与新增 test_s2_4.py 外其余 10 项未变）。

## 已知遗留（进下一轮）

- JIS-007、JIS-016 仍敞口：待用户定阈值（007）与人工评价 + 阈值（016）。
- P6 遗留未动：M-g 隔离区重放缺口、CLI 直通网关、WriteGate 词边界与模式清单、
  EventLog 检查点不完整。（run_offline 无资源上限已在本轮修掉。）
- S2-3 残留未动：Google 真实 401/403/5xx 正文未观测；代理黑洞未直接观测；
  L0 全失败降级只由 P2 模拟覆盖。
- 虚拟机更换后 Ollama 须重装，且新 VM 环境未必与旧 VM 相同（本轮实测：第二次换机的新 VM
  缺 `zstd`）；S2-5 部署手册须写进检查单：每换 VM 重装 Ollama + 校验 zstd/代理配置/模型 digest。
- 1B–1.5B 本地模型未验证（内存紧张）；实体电脑部署时再按实际内存选尺寸。
- Ollama 服务的 HTTP/2 问题只在服务侧用 GODEBUG 绕过，根因（代理与 Go h2 的互操作）未深究。

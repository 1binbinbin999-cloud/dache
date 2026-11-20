# Jarvis S2-3 — 真实失败演练（第二阶段第三步）

版本：0.9.0-s2-3，2026-11-21
目标：真实失败演练——拔 Key、断代理，记录超时与切换行为，验收为演练报告（JIS-011 闭环）。新增 tests/test_s2_3.py（15 项）；代码未改逻辑，只改版本号；全部 $0，不发真实 Gemini 请求。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-s2-3
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py health
python3 jarvis.py provider-status   # 各供应商状态，不耗额度
python3 jarvis.py router-call --logical local --prompt "你好"   # 默认 stub 模式
python3 jarvis.py usage-audit   # 审计 usage 日志（无日志时报 log_missing，exit 1）
python3 -m unittest discover -s tests -v   # 全量 96 项测试
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

## 文件

- `adapters.py`：直接适配器（新增，只用标准库 urllib，不引任何供应商 SDK）。
  `AdapterError`（带 error_class）/ `OllamaAdapter` / `GeminiAdapter` /
  `NotImplementedAdapter` / `build_adapter`。Key 只从环境变量 `GEMINI_API_KEY` 读，
  缺 Key 报 `missing_key`，不静默降级；429 与 5xx 按配置重试，其余不重试；
  审计只记 provider / model / 耗时 / error_class，不记提示词与响应原文。
- `router.py`：Provider 注册表 + live 切换链 + live embedding 路径（embed() 返回向量，
  全失败仍按 P6-15 返回 embedding-unavailable）+ `probe_all()`。
  `ProviderError` 的定义移至 adapters.py，这里重新导出。
- `core.py`：boot 与 health 改为模式一致校验（stub=模型关+不联网，live=模型开+联网许可）。
- `config.yaml`：版本 0.9.0-s2-3；providers / live_logical_models / fallback_chain
  / embedding_fallback_chain 定义见文件内注释。
- `jarvis.py`：新增 `provider-status` 命令（无推理成本，不耗额度）；S2-2 新增 `usage-audit` 命令（只读审计）与 router-call 的 usage_log 传入。
- `usage.py`：新增。UsageLog（data/usage.jsonl，JSONL 追加写，与事件日志同一信封口径，无哈希链——见局限）与 audit_usage_log（逐条比对 token 与后端 usage，费用恒 $0 断言）。
- `tests/test_s2_1.py`：13 项 S2-1 测试（见下）。
- `tests/test_s2_2.py`：11 项 S2-2 测试（usage 信封与脱敏、Ollama/Gemini 形状审计通过、token 不一致/费用非零/backend_usage 缺失/日志缺失均被抓到、Router 只记真实调用、Gemini 形状桩服务审计、Ollama 实调用审计、usage-audit CLI 无日志）。
- `tests/test_s2_3.py`：15 项 S2-3 测试（缺 Key 回落、401/403 不重试、500/429 重试与退避、真实 socket 超时、连接拒绝、Ollama 死端口、probe 缺 Key、CLI 缺 Key 与断代理、失败不写 usage 日志）。
- `evaluator_hashes.json`：已重生成（含 tests/test_s2_3.py 与新 config.yaml；其余 9 个旧哈希未变，证明旧测试未动）。

## 实测结果（2026-11-20，云端 Ubuntu 24.04.5，共享约 1.5 核、约 6GB 内存）

- **Ollama 0.40.16**：官方安装脚本安装；systemd 服务运行中。
  注意：出站经 Hatch egress 代理；Ollama 的 Go 客户端走 HTTP/2 时
  manifest 请求会挂死（`context deadline exceeded`），服务已加
  `GODEBUG=http2client=0`（见 `/etc/systemd/system/ollama.service.d/proxy.conf`，
  代理用无凭证版，不写任何密钥）。
- **qwen2.5:0.5b**（397 MB）：实调用通过。`router-call --logical local`：
  返回“你好。”，tokens 35/3，成本 0.0，latency 1745ms（模型已预热），
  首次加载约 4.6s；内存可用由 508MB 降至 273MB，未 OOM。
  1B–1.5B 模型未试（内存只剩约 500MB 可用，0.5b 是当前稳妥尺寸）。
- **nomic-embed-text**（274 MB）：实调用通过。embedding 返回 768 维真向量，
  content 为空字符串，degraded false。
- **Gemini**：无 Key，实调用未做。mock 测试验证了解析与 429 重试；
  缺 Key 时报 `missing_key`，链切到 Ollama 再到 Stub，CLI 实测通过。
- **429**：本地桩服务验证——429 一次后 200 则重试成功；一直 429 报 rate_limited。

## 测试

全量 96 项：P0 4 + P1 6 + P2 8 + P3 7 + P4 7 + P5 6（原 38 项）+ P6 验收 15 项实跑
（#1–#2 按用户决定 skip，未实跑验证）+ P6 配套 2 项 + S2-1 13 项 + S2-2 11 项 + S2-3 15 项。
本次全量回归 96 项通过（skipped=2），425 秒。
S2-1 的 13 项：live 链形状、stub 模式不变、Gemini 缺 Key 回落、Groq/OpenRouter 桩位、
429 识别与重试、live 启动一致性校验、live CLI 端到端回落、Ollama 实调用、
Gemini 缺 Key 直接报错、provider-status、live embedding 全失败、未知 type 当场报错、
Gemini mock 响应解析。

## S2-2 实测结果（2026-11-21，本虚拟机 Ubuntu，4 vCPU、约 8GB 内存）

- **Ollama 需重装**：本虚拟机与 S2-1 不是同一台，Ollama 未预装（/usr/local/bin/ollama 不存在、服务不存在）。按 S2-1 官方脚本重装 0.40.16，服务加 proxy.conf（无凭证代理 + SSL_CERT_FILE + GODEBUG=http2client=0，见 S2-1 §四），拉 qwen2.5:0.5b（a8b0c5157701，与 S2-1 同摘要）与 nomic-embed-text。
- **chat 实调用**（router-call --logical local，live 模式，提示“只回两个字：你好”）：tokens 35/3，后端 usage prompt_eval_count 35 / eval_count 3，一致；latency 476ms；cost $0.0。
- **embedding 实调用**（router-call --logical embedding）：768 维真向量，tokens_in 6，tokens_out 0，后端 usage prompt_eval_count 6，一致；latency 604ms；cost $0.0。
- **usage-audit**：对上述 2 条真实记录审计通过（ok=true，total=2，mismatches=[]）；usage 日志里无提示词与响应原文。
- **Gemini 实测**：待 parent 以 GEMINI_API_KEY 进程环境变量临时传入后实测（见下）。Gemini 形状测试一律用本地桩服务，未发任何真实请求。

## Gemini 实测（parent 已做，见 S2-2 记录 §六）

2026-11-21 02:51，parent 以 GEMINI_API_KEY 进程环境变量临时传入，
临时副本 `router-call --logical fast` 首次即通，gemini-flash 返回
“你好”，tokens 6/1 与 usageMetadata 一致，延迟 35697ms，费用 $0.0，
usage-audit ok=true。AI Studio 限额数字已看到（2026-11-21 02:52）：
Gemini 3.6 Flash 免费层 RPM 5、TPM 250K、RPD 20。

## S2-3 真实失败演练（2026-11-21，本虚拟机 Ubuntu，4 vCPU、约 8GB 内存）

本轮虚拟机又换了一台，Ollama 未预装：按 S2-1 官方脚本重装 0.40.16
（先装 zstd；服务加 proxy.conf，无凭证代理 + SSL_CERT_FILE +
GODEBUG=http2client=0），拉 qwen2.5:0.5b（a8b0c5157701，与 S2-1/S2-2
同摘要）与 nomic-embed-text。详见 S2-3 记录。

| 演练 | 做法 | 结果 |
|---|---|---|
| 缺 Key | 不设 GEMINI_API_KEY，live 模式 router-call | missing_key（<1s），回落 ollama-local 成功；Ollama 死端口时一路回落到 stub-local，费用 $0 |
| 假 Key + 401/403 | 桩服务返回 Gemini 官方形状错误体 | auth_error，只发 1 次（4xx 不重试），回落；Google 真实 401/403 正文从未观测 |
| 500 | 桩服务一直 500 | server_error，重试 1 次（共 2 次），耗尽后回落 |
| 429 | 桩服务 429 一次后 200 / 一直 429 | 前者重试成功（gemini-flash）；后者 2 次耗尽报 rate_limited 后回落；退避 2s 是真实等待 |
| Gemini 超时 | 桩服务睡 65s，生产 timeout_s=60 | 60092ms 时真实 socket 超时，不重试，回落 ollama-local |
| Ollama 超时 | 桩服务扮 /api/generate 睡 125s，生产 timeout_s=120 | 120136ms 时真实 socket 超时，不重试，回落 stub-local |
| 代理断开 | 代理 env 指向死端口 | gemini unavailable（34ms），Ollama 走 NO_PROXY 直连成功 |
| Ollama 停服务 | systemctl stop ollama | 连接拒绝，回落 stub-local（0.2s）；start 后恢复，实调用 35/3 token 通过 |
| 全断网 | unshare -rn 命名空间 | gemini 与 Ollama 均 unavailable（约 36ms），回落 stub-local |
| 失败与 usage 日志 | 失败尝试 | 不写 usage 记录；无日志时 usage-audit 报 log_missing，不空口通过 |

结论：超时阈值（Gemini 60s、Ollama 120s）、切换耗时、降级结果均已
实测入演练报告；JIS-011 闭环（残留见记录 §五：真实 Google
401/403/5xx 正文、代理黑洞、L0 全失败路径仍只由模拟覆盖）。

## 验收（S2-1）

1. 默认 stub 模式下原 38+15 项测试全过，行为与 P6 一致。
2. live 模式下 Ollama 本地调用全程跑通（启动→调用→返回→审计），有真实延迟与 token 数据。
3. Gemini 缺 Key 时明确报错、不静默降级；fallback 链按 Gemini → Ollama → Stub 走通。
4. 成本 $0；无新依赖；P0–P6 一个字没动。

## 已知遗留（进下一轮）

- usage.jsonl 无哈希链与检查点：审计只校验内容一致性，不防篡改（见 usage.py 局限；与 P6-18 同类遗留，Phase 2 处理）。
- Groq / OpenRouter：用户已定不绑卡，要求绑卡即不用；如需接入，只走无需绑卡的注册路径。
- Ollama 服务的 HTTP/2 问题只在服务侧用 GODEBUG 绕过，根因（代理与 Go h2 的互操作）未深究。
- 1B–1.5B 本地模型未验证（内存紧张）；实体电脑部署时再按实际内存选尺寸。
- P6 遗留未动：M-g 隔离区重放缺口、CLI 直通网关、WriteGate 词边界与模式清单、
  EventLog 检查点不完整、run_offline 无资源上限。
- S2-3 残留（见 S2-3 记录 §五）：Google 真实 401/403/5xx 正文未观测；代理黑洞未直接观测；L0 全失败降级只由 P2 模拟覆盖；Retry-After 响应头不被读取，退避固定 2s×attempt。
- 虚拟机更换后 Ollama 须重装（安装与模型不随仓库走）；S2-5 部署手册须写进这一步。

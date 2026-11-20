# Jarvis S2-1 — 模型接入层（第二阶段第一步）

版本：0.7.0-s2-1，2026-11-20
目标：接通第一个真实模型。直接适配器（不用 LiteLLM）：Ollama 本地 + Gemini 免费层；Router 按 fallback 链切换；缺 Key、断网、429 时明确报错并降级，不静默、不编造。

## 怎么跑

```bash
cd Jarvis/04_源代码/jarvis-s2-1
pip install -r requirements.txt   # 唯一依赖：PyYAML==6.0.1
python3 jarvis.py boot
python3 jarvis.py health
python3 jarvis.py provider-status   # 各供应商状态，不耗额度
python3 jarvis.py router-call --logical local --prompt "你好"   # 默认 stub 模式
python3 -m unittest discover -s tests -v   # 全量 70 项测试
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
- `config.yaml`：版本 0.7.0-s2-1；providers / live_logical_models / fallback_chain
  / embedding_fallback_chain 定义见文件内注释。
- `jarvis.py`：新增 `provider-status` 命令（无推理成本，不耗额度）。
- `tests/test_s2_1.py`：13 项 S2-1 测试（见下）。
- `evaluator_hashes.json`：已重生成（含 tests/test_s2_1.py）。

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

全量 70 项：P0 4 + P1 6 + P2 8 + P3 7 + P4 7 + P5 6（原 38 项）+ P6 验收 15 项实跑
（#1–#2 按用户决定 skip，未实跑验证）+ P6 配套 2 项 + S2-1 13 项。
S2-1 的 13 项：live 链形状、stub 模式不变、Gemini 缺 Key 回落、Groq/OpenRouter 桩位、
429 识别与重试、live 启动一致性校验、live CLI 端到端回落、Ollama 实调用、
Gemini 缺 Key 直接报错、provider-status、live embedding 全失败、未知 type 当场报错、
Gemini mock 响应解析。

## 验收（S2-1）

1. 默认 stub 模式下原 38+15 项测试全过，行为与 P6 一致。
2. live 模式下 Ollama 本地调用全程跑通（启动→调用→返回→审计），有真实延迟与 token 数据。
3. Gemini 缺 Key 时明确报错、不静默降级；fallback 链按 Gemini → Ollama → Stub 走通。
4. 成本 $0；无新依赖；P0–P6 一个字没动。

## 已知遗留（进下一轮）

- Gemini 实调用未做：待 parent 提供 `GEMINI_API_KEY` 后实测（模型 gemini-3.6-flash）。
- Gemini 免费层各项目实际限额（RPM/TPM/RPD）须登录 AI Studio 查看（官方文档不公布）。
- Groq / OpenRouter 注册时是否要求绑卡未核实；接入时现场确认，遇要求绑卡即停下报回。
- Ollama 服务的 HTTP/2 问题只在服务侧用 GODEBUG 绕过，根因（代理与 Go h2 的互操作）未深究。
- 1B–1.5B 本地模型未验证（内存紧张）；实体电脑部署时再按实际内存选尺寸。
- P6 遗留未动：M-g 隔离区重放缺口、CLI 直通网关、WriteGate 词边界与模式清单、
  EventLog 检查点不完整、run_offline 无资源上限。

# Jarvis 聊天部署（虚拟机内，先验证能用）

用户决定（2026-11-22）：开发完了之后先在虚拟机里部署、验证能用；聊天窗口单独开一个应用，在里面尝试聊天；每一次代码版本更新都同步到 GitHub，以防虚拟机更换/丢失。

## 组成

- `server.py`：Jarvis 聊天服务。把 `jarvis-s2-2` 的 Router（live 模式）包成 HTTP 接口，只用标准库 + PyYAML。聊天只走本地 Ollama（logical=local），不回落云端。
- 聊天窗口：单独的应用（Space），调本服务的 `/chat`。

## 诚实原则

- 只返回真实模型的回复。若回落到 Stub 或降级，一律报 503，不拿假回复冒充。
- Key 只从环境变量读（`GEMINI_API_KEY`），绝不写进代码、配置或日志。
- 成本 $0：Ollama 本地免费；Gemini 未接（本服务只走 local）。

## 启动（虚拟机内）

```bash
# 1. 装 Ollama（若虚拟机更换后丢失，须重装；装之前须先有 zstd）
sudo apt-get update && sudo apt-get install -y zstd
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:0.5b
ollama list   # 确认模型在

# 2. 起聊天服务（后台跑，监听 127.0.0.1:8000）
cd ~/workspace/Jarvis/04_源代码/jarvis-chat
nohup python3 server.py > server.log 2>&1 &
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message": "你好"}'
```

## 已知限制

- 模型是 qwen2.5:0.5b（小模型，S2-1/S2-2 验证用的那个），回复会比较简单；JIS-016 已判它拆分目标不可用。想聊得好需换更大模型或接 Gemini（免费层 RPD 20/天，需从 AI Studio 取 Key）。
- 服务是后台进程，虚拟机更换后会停，须按上面第 2 步重起；代码在 GitHub（dache）有备份，重起只须重装 Ollama + 拉模型 + 起服务，不用重写代码。
- 持久化（systemd 服务、开机自启）待后续做。
```


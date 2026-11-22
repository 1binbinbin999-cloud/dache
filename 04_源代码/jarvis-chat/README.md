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

- 虚拟机更换后重起 Ollama 注意代理：新虚拟机里 Ollama systemd 服务默认没有代理环境变量，`ollama pull` 会报 `tls: first record does not look like a TLS handshake`（2026-11-22 第四次换机后实测）。须先给服务加代理 drop-in 再拉模型：
  ```bash
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  printf '[Service]\nEnvironment="HTTP_PROXY=http://hatch-egress-proxy:3128"\nEnvironment="HTTPS_PROXY=http://hatch-egress-proxy:3128"\nEnvironment="NO_PROXY=localhost,127.0.0.1,::1"\nEnvironment="SSL_CERT_FILE=/run/hatch/egress-tls/ca-bundle.pem"\n' | sudo tee /etc/systemd/system/ollama.service.d/proxy.conf
  sudo systemctl daemon-reload && sudo systemctl restart ollama
  ```
  （代理地址取自本机 `/etc/environment`，无凭证版；drop-in 在 `/etc` 下，换机后会丢，重起时须重建。）

- 模型是 qwen2.5:0.5b（小模型，S2-1/S2-2 验证用的那个），回复会比较简单；JIS-016 已判它拆分目标不可用。想聊得好需换更大模型或接 Gemini（免费层 RPD 20/天，需从 AI Studio 取 Key）。
- 服务是后台进程，虚拟机更换后会停，须按上面第 2 步重起；代码在 GitHub（dache）有备份，重起只须重装 Ollama + 拉模型 + 起服务，不用重写代码。
- 持久化（systemd 服务、开机自启）待后续做。

## 验证记录（2026-11-22）

- Ollama 0.40.17 已装；`ollama serve` 在跑；qwen2.5:0.5b 已拉（397MB）。
- `/health`：ollama_local ok=true、reachable、model_present=true。
- `/chat` 实测（curl）：“你好，你是谁？”→ 模型回“我是阿里云的Qwen模型，我叫通义千问……”（model=ollama-local，tokens 34/56，耗时 7.1s，$0）。
- 聊天应用「Jarvis 聊天」端到端实测（sendmessage 动作）：“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（status=ok，model=ollama-local，耗时 1.1s）。
- 结论：虚拟机内部署已验证能用。模型是 0.5b 小模型，回复简单；想聊得好需换更大模型或接 Gemini。

## 再次换机重起补充（2026-11-22 第五次换机实测）

- apt 源里 `mirror.cogentco.com` 又慢又卡（单条索引能拖 2 分钟），update 会卡 15 分钟以上；azure 镜像本身 10 秒就完。处理：从 `/etc/apt/sources.list.d/ubuntu.sources` 的 URIs 行里删掉 cogentco 那一项（已备份 `ubuntu.sources.bak-cogentco`），只留 azure，update + 装 zstd 全程约 15 秒。
- Ollama 官方 install.sh 是流式下载（`curl …tar.zst | zstd -d | tar`），走 ollama.com 经代理只有约 95 kB/s，1.5GB 要 100 分钟以上；中途杀掉会留半截解压（`/usr/local/bin/ollama`、`/usr/local/lib/ollama`），须先删干净再重装。
- 快路：直接从 GitHub Releases 下同一文件（`https://github.com/ollama/ollama/releases/download/v0.40.17/ollama-linux-amd64.tar.zst`，实测约 950 kB/s–2.7 MB/s），用发布页 `sha256sum.txt` 验（期望值 `1df9d6228bac5a22e420b242708c0d10e5b886db97bab99d446a41760f4e79af`），再按 install.sh 的步骤手工装（建目录、解压到 /usr/local、建 ollama 用户、写 systemd 服务 + 代理 drop-in）。注意：`/tmp` 只有 512MB tmpfs，1.5GB 的包不要下到 /tmp，下到家目录。
- 模型目录分裂：systemd 服务以 ollama 用户跑，模型目录是 `/usr/share/ollama/.ollama/models`；家目录 `~/.ollama/models` 里的权重服务看不到，`ollama list` 会是空的。处理：把家目录模型文件拷过去再 `chown -R ollama:ollama`（380MB，无需重新 pull），再 `systemctl restart ollama`，`ollama list` 见到 `qwen2.5:0.5b` 才算完。
- 已验证的包留了一份在家目录：`/home/hatch/ollama-linux-amd64.tar.zst`（sha256 已验，下次换机可直接用，省下载）。
- 第七次换机（2026-11-22 约 19:39）实测：cogentco 果然又回来了（/etc 随换机清空），须重删；另注意 `zstd -d 包.tar.zst` 默认写文件、不走 stdout，`zstd -d … | tar` 会拿到空输入而报错，流式解压须用 `zstd -dc`，或先解成 .tar 再 `tar -xf`。

## 第九次换机重起补充（2026-11-23 约 01:51 实测）

- cogentco 果然又回来了（/etc 随换机清空），须重删（已备份 `ubuntu.sources.bak-cogentco`，只留 azure）。
- 新坑：开机后系统有个 os-intent 重放进程在跑 `apt-get update`，占着 apt 锁；它启动时源里还有 cogentco，卡在拉 cogentco 索引上 11 分钟以上。不要杀它。绕过去：zstd 直接用本地 apt 缓存里的包 `dpkg -i /var/cache/apt/archives/zstd_1.5.5+dfsg2-2build1.1_amd64.deb`（libzstd1 已装），不走 apt update。缓存（/var/cache/apt/archives，158 个 .deb）换机后还在。
- 其余步骤与第 5/7 次相同：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，建 ollama 用户 + systemd 服务 + 代理 drop-in，模型权重从 ~/.ollama/models 拷到 /usr/share/ollama/.ollama/models 并 chown -R ollama:ollama。
- 注意：换机后本机没有旧服务进程，不用 pkill；`pkill -f "python3 server.py"` 的模式会匹配到执行命令自身的 shell，把自己杀掉。
- 验证：/health ok=true、model_present=true；/chat 实测“你好，请用一句话介绍你自己”→“我是阿里云自主研发的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 5.0s，$0）。

## 第十一次换机重起补充（2026-11-23 约 02:57 实测）

- cogentco 果然又回来了（/etc 随换机清空），须重删（已备份 `ubuntu.sources.bak-cogentco`，只留 azure）。
- 本次无 apt 锁问题：开机时没有系统进程占着 apt 锁；且 zstd 已装（/usr/bin/zstd），完全没走 apt。
- 其余步骤与第 9 次相同：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in，模型权重从 ~/.ollama/models 拷到 /usr/share/ollama/.ollama/models 并 chown -R ollama:ollama。
- 验证：/health ok=true、model_present=true；/chat 实测“你好，请用一句话介绍你自己”→“我是阿里云自主研发的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 8.0s，$0）。
```


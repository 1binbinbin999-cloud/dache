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

- 模型（2026-11-24 用户决定换成 7b）：默认聊天模型是 qwen2.5:7b（原 0.5b 小模型，S2-1/S2-2 验证用的那个，JIS-016 已判它拆分目标不可用；7b 在 S2-6 严格 7/10 达线）。换模型只改 `../jarvis-s2-2/config.yaml` 里 `providers.ollama-local.model` 一行，重启聊天服务即生效，业务代码不动；再想换别的本地模型（Llama/DeepSeek/Mistral）同理，先 `ollama pull` 再改这一行。注意 7b 在本机纯 CPU 上很慢（一句自我介绍约 75 秒），0.5b 约 2–6 秒。Gemini 免费层仍可作云端备选（RPD 20/天，需从 AI Studio 取 Key）。
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

## 第十二次换机重起补充（2026-11-23 约 05:06 实测）

- cogentco 果然又回来了（/etc 随换机清空），须重删（已备份 `ubuntu.sources.bak-cogentco`，只留 azure）。
- 本次无 apt 锁问题：开机时没有系统进程占着 apt 锁；且 zstd 已装（/usr/bin/zstd），完全没走 apt。
- 其余步骤与第 9/11 次相同：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in，模型权重从 ~/.ollama/models 拷到 /usr/share/ollama/.ollama/models 并 chown -R ollama:ollama。
- 验证：/health ok=true、model_present=true；/chat 实测“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 4.9s，$0）。
```


## 第十三次换机重起补充（2026-11-23 约 07:32 实测，S2-5 轮中恢复）

- Ollama 二进制已失，本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in；权重从 ~/.ollama/models 拷到 /usr/share/ollama/.ollama/models 并 chown -R ollama:ollama。`ollama list`：qwen2.5:0.5b a8b0c5157701、qwen2.5:7b 845dbda0ea48（4.7GB，约 24 分钟，$0）、nomic-embed-text 0a109f422b47（与 S2-4 digest 一致）。
- 本次无 README 没写的新坑：zstd 已装、开机无 apt 锁进程；cogentco 仍在源里，但全程没走 apt，所以没删——下次如需 apt 仍须按 README 重删。
- 新情况：根盘 overlay 仅 7.5G，拉 nomic 时报 ENOSPC（剩 12M），回归中途一度 100%（剩 64K）。已清：apt lists（198M）、非 zstd 的 deb（约 98M，zstd deb 保留）、apt pkgcache（114M）、ffmpeg 文档源码包（12M）、未用的 cuda_v12 库目录（1.3G；本机无 GPU，/dev/nvidia* 与 /proc/driver/nvidia 均不存在、无进程映射任何 cuda 库，删后 Ollama 照常服务，安装包是恢复路径）。现剩约 1.1G。结构性提醒：7.5G 根盘装 0.5b+7b+nomic+CUDA 库本就捉襟见肘，S2-6 部署手册的环境检查单应加磁盘空间一项。
- 聊天服务：`nohup python3 server.py > server.log 2>&1 &` 重起（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true；/chat 实测“你好”→“你好！很高兴能与你交流。如果你有任何问题或需要帮助，请随时告诉我。”（model=ollama-local，tokens 30/19，耗时 1.7s，$0）。

## 第十四次换机重起补充（2026-11-23 约 09:18 实测）

- 本次新坑：zstd 又没了，os-intent 重放进程（apt-get install gcr，pid 4177）占着 dpkg 前端锁 10 分钟以上，20 次 dpkg -i 重试全失败（锁错误）；不要杀它、不要删锁文件。绕过去：`dpkg-deb -x` 直接解开本地缓存里的 zstd 包，不碰 dpkg 数据库——`dpkg-deb -x /var/cache/apt/archives/zstd_1.5.5+dfsg2-2build1.1_amd64.deb /tmp/zstd-pkg && cp /tmp/zstd-pkg/usr/bin/zstd /usr/bin/zstd`，zstd 1.5.5 到手；之后补跑 dpkg -i 想把 zstd 登记进数据库，同样因锁失败，不影响使用。本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in；权重从 ~/.ollama/models 拷到 /usr/share/ollama/.ollama/models 并 chown -R ollama:ollama（家目录里只有 0.5b，380M）。
- 模型：qwen2.5:7b 845dbda0ea48（4.7GB）、nomic-embed-text 0a109f422b47（274MB，与 S2-4 digest 一致）均为 ollama pull，两 pull 合计约 30 分钟，$0；`ollama list` 三模型齐全。
- cogentco 仍在源里，但全程没走 apt，所以没删——下次如需 apt 仍须按 README 重删。
- pytest：pip3 24.0 已在，`pip3 install --break-system-packages pytest`，pytest 9.2.0（代理走 /etc/environment）。
- 满盘（本次最大坑）：新机根盘 7.5G、开局剩 7.4G，装完 Ollama（/usr/local/lib/ollama 2.4G，含 cuda_v12 1.3G + cuda_v13 1.1G + vulkan 39M）+ 三模型（5.4G）+ 家目录安装包（1.5G）后一度 100%（剩 59M）。已清：cuda_v12、cuda_v13、vulkan（本机无 GPU，/dev/nvidia* 与 /proc/driver/nvidia 均不存在，运行中 ollama 进程 maps 里无 cuda/vulkan 映射，删后重启照常服务）、apt lists（198M）、非 zstd 的 deb、/tmp/zstd-pkg；zstd deb 与安装包保留。现剩 2.0G。结构性提醒：7.5G 根盘装整套东西本就捉襟见肘，0.40.17 开始同时带 cuda_v12 和 cuda_v13 两套，S2-6 部署手册的环境检查单须加磁盘空间一项，且重起流程应把“删无用 GPU 库”写进标准步骤。
- 聊天服务：`nohup python3 server.py > server.log 2>&1 &` 重起（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true；/chat 实测“你好”→“你好！很高兴能与你交流。如果你有任何问题或需要帮助，请随时告诉我。”（model=ollama-local，tokens 30/19，耗时 6.8s，$0）；删 GPU 库重启后又验了一遍，/health、/chat 均正常。

## 第十五次换机重起补充（2026-11-23 约 13:29 实测）

- 本次新坑有三：① tar 包解出来的 `/usr/local/lib/ollama` 目录权限是 770（root:root），ollama 用户进不去，llama-server 报 "binary not found"，/chat 报 model_unavailable（诚实门 503，不出假回复）；修法 `chmod 755 /usr/local/lib/ollama`，之后 ollama 用户可执行 llama-server --version，/chat 恢复。教训：装完先以 ollama 用户跑一次 llama-server --version 验证，不要等 /chat 失败才发现。② 修权限时 `systemctl restart ollama` 把正在拉的 7b（13%，590MB）打断，续传文件损坏，之后续拉在 manifest 阶段秒挂 EOF（nomic 干净拉取正常，定位到是 7b 续传状态问题）；修法删掉 `sha256-2bada8a74506…-partial` 及 `-partial-0`…`-partial-15` 后从头重拉，约 30 分钟拉完。教训：拉模型期间绝不重启 ollama 服务；断过的续传文件坏了就删掉重拉，别硬续。③ 本机预装 pytest 7.4.4（Debian 包），pip 常规安装升不到 9.2.0（pluggy 1.4.0 无 RECORD，卸载失败）；用 `pip3 install --break-system-packages --ignore-installed pytest` 升到 9.2.0。
- 其余与第 14 次相同：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，cogentco 已删（只留 azure，本次没走 apt update 也先删了），zstd 经 `dpkg-deb -x` 从本地缓存取回（未碰 dpkg 锁），建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in，0.5b 家目录拷回并 `chown -R ollama:ollama`；cuda_v12/v13 + vulkan 已删（约 2.4G），apt lists 已清。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉）、nomic-embed-text 0a109f422b47（274MB，重拉），digest 与 S2-4 一致；`ollama list` 三模型齐全。
- 聊天服务：`nohup python3 server.py > server.log 2>&1 &` 重起（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 7.9s，$0）。根盘剩 1.9G。

## 第十九次换机重起补充（2026-11-23 约 19:31 实测）

- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in（取自 /etc/environment）；0.5b 家目录拷回并 `chown -R ollama:ollama`；cuda_v12/v13 + vulkan 已删（约 2.4G，标准步骤）。
- 与第 15 次不同的一点：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先以 ollama 用户跑一次 llama-server --version 再往下走。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，2.6 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。拉模型期间未重启 ollama 服务（第 15 次教训）。
- 聊天服务：`nohup python3 server.py > server.log 2>&1 &` 重起（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 6.4s，$0）。根盘剩 1.9G。
- 注意：第 16/17/18 次更换的恢复未在 README 留记录（本次只补第 19 次；三轮的细节无据可写，不编）。18:11 轮曾有一次恢复在进行（7b 重拉中），被 19:31 这次换机清掉，本次从头重装。

## 第二十次换机重起补充（2026-11-23 约 21:32 实测）

- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in（取自 /etc/environment）；0.5b 家目录拷回并 `chown -R ollama:ollama`；cuda_v12/v13 + vulkan 已删（约 2.4G，标准步骤）。
- 与第 15 次不同的一点：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先以 ollama 用户跑一次 llama-server --version 再往下走。
- 磁盘时序教训（本次新发现）：拉 7b 前根盘剩 4.2G，按剩余量算刚好不够（7b 剩余约 4.1G + nomic 0.27G），拉到一半会 ENOSPC；这次在拉取进行中先清了 cuda_v12/v13 + vulkan（约 2.4G），清之前核过运行中的 ollama 进程 maps 里无 cuda/vulkan 映射、且 /dev/nvidia* 与 /proc/driver/nvidia 均不存在，清后服务照常、拉取未断。教训：以后拉模型前先看磁盘余量，不够就先清 GPU 库再拉，不要等拉到一半再救。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 25 分钟，2.5 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。拉模型期间未重启 ollama 服务（第 15 次教训）。
- 聊天服务：`nohup python3 server.py > server.log 2>&1 &` 重起（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 8.2s，$0）。根盘剩 1.9G。

## 第二十一次换机重起补充（2026-11-23 约 23:32 换机，2026-11-24 约 00:20 恢复完成）

- 本次新坑：`useradd -r` 不带 `-m/-d` 不建家目录，`ollama serve` 找不到 `/home/ollama/.ollama/id_ed25519`，报 `could not create directory mkdir /home/ollama: permission denied`，服务反复重启（exit 1）。修法：`usermod -d /usr/share/ollama ollama` + `chown ollama:ollama /usr/share/ollama`，服务随即 active。教训：手工建 ollama 用户须把家目录指到 `/usr/share/ollama`（官方 install.sh 就是这么建的），否则模型目录与 HOME 分裂；装完先看 `systemctl status ollama` 是否 active，不要等 `ollama list` 连不上才查日志。
- 其余老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in（取自 /etc/environment）；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 15 次不同的一点：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先以 ollama 用户跑一次 llama-server --version 再往下走。
- 磁盘时序（第二十次的教训这次用上了）：装完 Ollama 后根盘剩 5.0G，7b（4.7G）+ nomic（0.27G）刚好不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过运行中的 ollama 进程 maps 里无 cuda/vulkan 映射、且 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.8G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟）、nomic-embed-text 0a109f422b47（274MB，重拉，约 10 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 拉取后期明显变慢（前段约 2.5 MB/s，收尾约 0.7 MB/s），partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：`nohup python3 server.py > server.log 2>&1 &` 重起（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是阿里云自主研发的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 5.0s，$0）。根盘剩 1.8G。

## 第二十二次换机重起补充（2026-11-24 约 01:34 换机，约 02:20 恢复完成）

- 770 又回来了：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21 次都是 755 直接可用），已在起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌这次是不是 755。
- useradd 这次按官方形式一次建对：`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`（带 `-m -d`），家目录正常建出，服务第一次起就 active；第 21 次的坑（`useradd -r` 不带 `-m/-d` 不建家目录、ollama serve 找不到 id_ed25519 反复重启）这次没重现。教训：手工建用户必须带 `-m -d /usr/share/ollama`，装完先看 `systemctl status ollama` 是否 active。
- 其余老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；建 ollama 用户（uid 999，video/render 组）+ systemd 服务 + 代理 drop-in（取自 /etc/environment）；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 磁盘时序（第二十次的教训）：装完 Ollama 后根盘剩 5.0G，7b（4.7G）+ nomic（0.27G）刚好不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过运行中的 ollama 进程 maps 里无 cuda/vulkan 映射、且 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.4G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，约 2.5 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 105 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉取期间先起（只用 0.5b，不动 Ollama 服务），`nohup python3 server.py > server.log 2>&1 &`（换机后无旧进程，不用 pkill）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是阿里云自主研发的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 6.7s，$0）。根盘剩 1.9G。

## 第二十三次换机重起补充（2026-11-24 约 03:35 换机，约 04:22 恢复完成）

- 本次新坑（启动命令的 shell 写法）：`cd 目录 && nohup python3 server.py > server.log 2>&1 &` 这样写，`&` 会把整个 `cd && …` 列表放进后台子 shell，主 shell 根本没 cd，相对路径的 `python3 server.py` 就会在 /home/hatch 下报错 `can't open file '/home/hatch/server.py'`，服务起不来（/health 连不上）。第一次起服务就栽在这上面，日志里只有 22 次留下的旧 "listening" 行，容易误判服务曾起来过。修法：用 setsid 形式一次起对——`cd ~/workspace/Jarvis/04_源代码/jarvis-chat && setsid nohup python3 server.py > server.log 2>&1 < /dev/null & disown`（cd 在后台子 shell 里生效，setsid 让服务脱离 exec 会话，runtime 的 drain-timeout 清理杀不到它），随后 /health 立即 ok。教训：起聊天服务优先用绝对路径或上面这行 setsid 形式；起完先 curl /health，不要只看日志行（日志可能是上台机留下的旧文件）。
- zstd 这次又没了：开机有 os-intent 重放进程（apt-get install cpu-checker）在占 apt/dpkg 锁，不要杀它；按第 14 次的办法，`dpkg-deb -x /var/cache/apt/archives/zstd_1.5.5+dfsg2-2build1.1_amd64.deb /tmp/zstd-pkg && cp /tmp/zstd-pkg/usr/bin/zstd /usr/bin/zstd`，zstd 1.5.5 到手，全程没走 apt。cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）。
- 其余老步骤一次走通：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 22 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：装完 Ollama 后根盘剩 5.2G，7b（4.7G）+ nomic（0.27G）刚好不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过运行中的 ollama 进程 maps 里无 cuda/vulkan 映射、且 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 7.0G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 29 分钟，约 2.7–2.9 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉取期间先起（只用 0.5b，不动 Ollama 服务），setsid 形式（见上）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 4.8s，$0）。根盘剩 1.9G。

## 第二十五次换机重起补充（2026-11-24 约 05:03 换机，约 05:45 恢复完成）

- 注意：第二十四次换机发生在其间、恢复未派发，那台机器已经没了——本次直接在当前（第 25 台）机器上恢复，24 次无据可写，不编。
- zstd 这次又没了：开机有 os-intent 重放进程（apt-get install libgtk-4-bin）在占 apt/dpkg 锁，不要杀它；按第 14/23 次的办法，`dpkg-deb -x /var/cache/apt/archives/zstd_1.5.5+dfsg2-2build1.1_amd64.deb /tmp/zstd-pkg && cp /tmp/zstd-pkg/usr/bin/zstd /usr/bin/zstd`，zstd 1.5.5 到手，全程没走 apt。cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）。
- 其余老步骤一次走通：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 22/23 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：装完 Ollama 后根盘剩 5.1G，7b（4.7G）+ nomic（0.27G）刚好不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过运行中的 ollama 进程 maps 里无 cuda/vulkan 映射、且 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 5.9G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 26 分钟）、nomic-embed-text 0a109f422b47（274MB，重拉，约 25 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉取期间先起（只用 0.5b，不动 Ollama 服务），setsid 形式（第 23 次的教训）。补充：起服务的 exec 调用会报 output_drain_timeout——那只是因为 server.py 子进程还在跑，不是起失败；不要因此去杀它，用 curl /health 与 /chat 直接验服务。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是来自阿里云的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 2.4s，$0），三模型齐后又验了一次“你好”→正常回复（30/16 token，1.2s，$0）。根盘剩 1.8G。

## 第二十七次换机重起补充（2026-11-24 约 07:36 换机，约 08:09 恢复完成）

- 注意：第二十六次换机（约 07:06–07:08）的恢复做到一半被本次换机清掉——26 号机上恢复约 07:16 开始，7b 已拉到 51%（2.4GB，2.5 MB/s），约 07:36 第 27 次换机，uptime 归零、/tmp 清空、ollama 用户消失，26 号机的恢复无完成记录；本次直接在当前（第 27 台）机器上从头恢复，26 次无据可写，不编。
- 本次新坑（apt 源文件写不动）：这台机的 `/etc/apt/sources.list.d/ubuntu.sources` 是 nobody 拥有的 overlay metacopy，就地改写（sed/python > 文件）连 root 都报 Permission denied，chown 也报 Operation not permitted；但目录可写，修法是先 `rm` 掉该文件、再用备份内容 sed 掉 cogentco 后 `tee` 重建（26 号机同样如此，备份 ubuntu.sources.bak-cogentco 已留）。教训：删 cogentco 别再用就地改写，直接 rm + 重建。
- zstd 这次又没了：开机有 os-intent 重放进程（apt-get update）在占 apt/dpkg 锁，不要杀它；按第 14/23/25 次的办法，`dpkg-deb -x /var/cache/apt/archives/zstd_1.5.5+dfsg2-2build1.1_amd64.deb /tmp/zstd-pkg && cp /tmp/zstd-pkg/usr/bin/zstd /usr/bin/zstd`，zstd 1.5.5 到手，全程没走 apt。cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）。
- 其余老步骤一次走通：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 19/20/21/26 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：装完 Ollama 后根盘剩 5.6G，7b（4.7G）+ nomic（0.27G）刚好不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过运行中的 ollama 进程 maps 里无 cuda/vulkan 映射、且 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.7G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，2.5–2.6 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉取期间先起（只用 0.5b，不动 Ollama 服务），setsid 形式（第 23 次的教训）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“我是阿里云自主研发的超大规模语言模型，我叫通义千问。”（model=ollama-local，tokens 35/18，耗时 5.6s，$0），三模型齐后又验了一次“你好”→正常回复（30/19 token，1.7s，$0）。根盘剩 1.9G。

## 默认模型切换（2026-11-24 08:41，用户决定）

- 用户在 05:09、05:46、07:15 三次被问要不要换 7b 均未答，08:41 点了“换成 7b 试试”与“换 7b 做默认聊天模型”。
- 改动只有一行：`../jarvis-s2-2/config.yaml` 里 `providers.ollama-local.model` 由 `qwen2.5:0.5b` 改为 `qwen2.5:7b`；重启聊天服务（kill 旧 server.py PID 2361，setsid 形式重起）即生效。换机恢复时仍按老流程拉三模型，聊天默认走 7b。
- 验证：/health ok=true、router_mode=live；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时约 75 秒，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。注意：7b 仍自称 Qwen（同系列换大号，品牌没变）；纯 CPU 上明显慢于 0.5b（约 75 秒 vs 2–6 秒），介意速度可随时换回 0.5b（同一行改回去）。
- 用户同日补充方向：想要“自己的本地模型”，其它模型只作辅助成长。现状：Jarvis 架构上模型本就是可替换件（本行配置即证明），但“自己的模型”即在自有数据上微调出的模型，目前尚无训练管线、无 GPU（本虚拟机纯 CPU、根盘 7.5G），须等 S2-7 实体机部署盘点硬件后再定。

## 第二十八次换机重起补充（2026-11-24 约 09:28 换机，约 10:24 恢复完成）

- 本次新坑：以 root 身份读运行中 ollama 进程的 /proc/<pid>/maps 报 Permission denied（本机可能开了 hidepid/ptrace_scope），清 GPU 库前核映射的办法走不通。替代核查：/dev/nvidia* 与 /proc/driver/nvidia 均不存在 + 删库后 ollama 照常加载 7b、/chat 正常，证明删库安全。教训：maps 读不到时不要硬核，改用"无 GPU 设备文件 + 删后加载验证"两步确认。
- 小发现：df 读数在拷 0.5b 前后有抖动（6.9G → 6.6G → 6.9G → 6.5G），overlay 记账有延迟/抖动；拉模型前以最近一次 df 为准即可，余量够就拉，不必追单次读数。
- 其余老步骤一次走通：zstd 这次已装（/usr/bin/zstd），全程没走 apt；cogentco 仍在源里（nobody 拥有的 overlay metacopy，就地改写仍报 Permission denied），按第 27 次的办法 rm + tee 重建（只留 azure，备份 ubuntu.sources.bak-cogentco）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 22/23/25 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）刚好不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G），清后剩 6.6G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28.5 分钟，前段 4.1–7.1 MB/s、后段约 2.8 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 96 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b，见下条；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 58.4s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。
- 默认模型已是 7b：本次恢复按 2026-11-24 08:41 用户决定（config.yaml 改动在提交 9b73572）拉齐三模型，聊天默认走 7b；7b 仍自称 Qwen，纯 CPU 上约 58 秒，比 0.5b（2–6 秒）慢得多，介意速度可随时换回 0.5b（同一行改回去）。

## 第三十次换机重起补充（2026-11-24 约 11:54 换机，约 13:21 恢复完成）

- 注意：第二十九次换机发生在其间、恢复未派发，那台机器已经没了——本次直接在当前（第 30 台）机器上恢复，29 次无据可写，不编。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 仍在源里，但全程没走 apt，所以没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 22/23/25/28 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770，起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：装完 Ollama 后根盘剩 4.6G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.5G 再拉。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27 分钟，前段约 4.1 MB/s、后段约 2.3–2.7 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 100 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 60.8s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。

## 第三十一次换机重起补充（2026-11-24 约 14:11 发现换机，约 14:51 恢复完成）

- 本次新坑（/usr/share/ollama 目录归属）：恢复时先 `sudo mkdir -p /usr/share/ollama` 再 `useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，useradd 报 "home directory already exists, not copying skel"，目录仍属 root:root，ollama serve 建 .ollama 失败 `mkdir /usr/share/ollama/.ollama: permission denied`，服务反复重启（exit 1，第 21 次的坑换了个样子）。修法：`chown ollama:ollama /usr/share/ollama` 后服务一次 active。教训：别先 sudo mkdir 造家目录，让 useradd -m 自己建；若目录已存在，建完用户立即 chown，装完先看 `systemctl status ollama` 是否 active。
- 其余老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 仍在源里，但全程没走 apt，所以没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active（修完目录归属后）；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 19/20/21/27 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：装完 Ollama 后根盘剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.8G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27 分钟，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 100 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 67.6s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。

## 第三十二次换机重起补充（2026-11-24 约 15:03 换机，约 15:44 恢复完成）

- 本次无新坑，老步骤一次走通：第 31 次的坑（先 sudo mkdir 造 /usr/share/ollama 致属 root）这次没重现——没先 mkdir，直接 `useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，家目录一次建对（ollama:ollama），服务第一次起就 active。zstd 这次又没了，开机无 apt 锁进程；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt；cogentco 仍在源里，全程没走 apt所以没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）。
- 其余老步骤一次走通：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 19/20/21/27/31 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.4G，装完 Ollama 后剩 5.4G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27 分钟，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 106 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型。"（model=ollama-local，tokens 35/17，耗时 50.1s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。

## 第四十四次换机重起补充（2026-11-25 约 13:03 换机，约 14:00 恢复完成）

- 计数更正：本节原写“第四十三次”，是错的。第 43 台是 2026-11-25 11:01 换机（boot 03:01 UTC，根盘 197M/7.4G）；本机 boot 是 2026-11-25 05:03 UTC（13:03 CST），是下一台，即第 44 台。提交 55644fd 的标题仍写“第 43 次”，以本节为准，不改历史。
- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 44 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 05:03 UTC）计。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程；cogentco 仍在源里，按第 27 次的办法 rm + tee 重建（只留 azure，备份 ubuntu.sources.bak-cogentco；本次源文件又是 nobody 拥有的 overlay metacopy，就地改写仍会报 Permission denied）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 22/23/25/28/30 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27.7 分钟，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 92 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。
- 本次新观察（首次 /chat 冷启动超时）：7b 拉完后第一次 /chat 在 120 秒时报 model_unavailable（诚实门，未出假回复）；原因是模型刚拉完、首次加载 + 生成超过了 config 里 ollama-local 的 timeout_s 120。此时 `ollama ps` 已见 7b 在跑（5.1GB，100% CPU），说明模型正在加载、不是服务坏了。修法：等模型加载完再重试一次 /chat，第二次即正常回复（22.2 秒）。教训：换机恢复后第一次 /chat 若报 model_unavailable，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 22.2s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第四十五次换机重起补充（2026-11-25 约 15:04 换机，约 15:58 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 45 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 07:04 UTC）计。
- 本次与第 44 次不同：zstd 这次没装（第 44 次是已装）。开机无 apt 锁进程；cogentco 仍在源里，按第 27 次的办法 rm + tee 重建（只留 azure，备份 ubuntu.sources.bak-cogentco；本次源文件又是 nobody 拥有的 overlay metacopy，就地改写仍会报 Permission denied），apt-get update 一次通过，再 apt-get install -y zstd 1.5.5。本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 19/20/21/27/31/32 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.5G，装完 Ollama 后剩 5.2G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.7G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27 分钟，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 92 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（45.8 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型。"（model=ollama-local，tokens 35/17，耗时 45.8s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 2.1G。

## 第四十六次换机重起补充（2026-11-25 约 20:54 换机，约 22:08 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 46 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 12:54 UTC）计。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装（第 45 次是没装、走 apt 装回；本次开机无 apt 锁进程，全程没走 apt，cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 19/20/21/27/31/32/45 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.4G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.3G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 94 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（88.4 秒，未触发第 44 次的冷启动 120 秒超时；比第 45 次的 45.8 秒慢，纯 CPU 7b 正常波动），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 88.4s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。

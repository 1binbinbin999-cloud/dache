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

## 第四十七次换机重起补充（2026-11-25 约 22:52 换机，约 23:52 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 47 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 14:52 UTC）计。第 46 次恢复刚在约 22:09 完成（提交 32119a3），不到一小时本机又被换掉，46 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt，cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`。
- 与第 19/20/21/27/31/32/45/46 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 4.8G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 91 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（66.8 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 66.8s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。

## 第四十八次换机重起补充（2026-11-26 约 00:51 换机，约 01:50 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 48 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 16:50 UTC）计。第 47 次恢复刚在约 23:52 完成（提交 137f85f），不到一小时本机又被换掉，47 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑（除下一条 0.5b 拷回嵌套外），老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt，cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active。
- 与第 19/20/21/27/31/32/45/46/47 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 本次新坑（0.5b 拷回嵌套）：`cp -r ~/.ollama/models /usr/share/ollama/.ollama/models` 时目标目录已存在（之前 mkdir -p 建过），结果拷成了 models/models 嵌套，0.5b 没进正确位置。修法：把嵌套目录里的内容上移一层、删掉空的嵌套目录，再重启 ollama，0.5b 随即列出。教训：目标目录已存在时 `cp -r` 会嵌套——要么拷内容（`cp -r ~/.ollama/models/. /usr/share/ollama/.ollama/models/`），要么事后上移；拷完务必 `ollama list` 核对 0.5b 在列再往下走。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.5G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28.6 分钟，收尾变慢到约 1.7–2.1MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 105 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；本次进度条的 MB 数还一度严重低估（显示 399MB 时 du 已 1.5G），别因进度条数字就误判卡死、也别中途杀掉重拉。另：本次用了一个轻量 watcher（每 30 秒 pgrep 查拉模型进程是否还在）等 7b 下完，省得轮询刷屏的进度条输出；watcher 与拉模型进程互不干扰。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（75.3 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，你是谁？"→"你好！我是Qwen，由阿里云开发的AI助手。我旨在提供有用的信息和帮助，回答用户的各种问题。你可以问我任何问题，我会尽力帮助你。"（model=ollama-local，tokens 34/38，耗时 75.3s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第四十九次换机重起补充（2026-11-26 约 02:54 换机，约 03:55 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 49 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 18:54 UTC）计。第 48 次恢复刚在约 01:52 完成（提交 b8950d5），不到一小时本机又被换掉，48 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt，cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（这次用 `cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.3G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28.2 分钟，前段约 6–13 MB/s、收尾约 1.9–2.6 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 92 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；本次进度条的 MB 数一度严重低估（第 48 次同样），别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（63.8 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 小发现（旧日志文件误导）：起服务后 `tail -3 server.log` 在 /home/hatch 下读到一条 "can't open file '/home/hatch/server.py'" 的报错——那是 /home/hatch/server.log 里 2026-11-23 19:49 留下的旧内容（第 23 次事故当时的报错，/home 随换机保留），前景 shell 没 cd，tail 读错了文件；新服务实际在 jarvis-chat 目录下正常启动（server.log 只有 "listening on http://127.0.0.1:8000" 一行，/health 当即 ok）。教训：核对日志务必用绝对路径 tail jarvis-chat/server.log，别在 /home/hatch 下 tail 相对路径。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 63.8s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.8G。

## 第五十次换机重起补充（2026-11-26 约 04:23 换机，约 05:17 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 50 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 20:23 UTC）计。第 49 次恢复刚在约 03:55 完成（提交 2f5e3c8），不到一小时本机又被换掉，49 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt，cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（这次用 `cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 22/23/25/28/30/44 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32/45/46/47/48/49 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.8G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 29.5 分钟，约 2.5–3.7 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 16 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；别因表观大小不动就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（83.4 秒，未触发第 44 次的冷启动 120 秒超时；比第 49 次的 63.8 秒慢，纯 CPU 7b 正常波动），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 83.4s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第五十一次换机重起补充（2026-11-26 约 06:33 换机，约 07:46 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 51 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-25 22:33 UTC）计。第 50 次恢复刚在约 05:17 完成（提交 e71b40b），不到一小时本机又被换掉，50 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt，cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（若源文件是 nobody 拥有的 overlay metacopy，就地改写报 Permission denied，须 rm + tee 重建，第 27 次的办法）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（这次用 `cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.5G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 30 分钟，约 2.4–2.6 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 141 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；本次 watcher 的 du 读数（5 分钟 1.2G、10 分钟 2.0G、15 分钟 2.7G、20 分钟 3.4G、25 分钟 4.1G、30 分钟 4.8G）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。另：watcher 的 pgrep 模式会匹配到 watcher 自己的命令行（bash 命令行里含 "ollama pull qwen2.5:7b"），所以它永远等不到"拉完"、只会跑满 70 轮，最后是手动 kill 掉的；以后 watcher 须用 pgrep -x 或排除自身 PID，别再用含拉取命令的 pgrep -f 模式。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（81.3 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 81.3s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十二次换机重起补充（2026-11-26 约 08:34 换机，约 10:02 恢复完成）

- 注意：第三十三至四十二次换机发生在其间、恢复未派发或未留记录，README 只记到第 32 次；本次直接在当前（第 52 台）机器上从头恢复，33–42 次无据可写，不编。本次换机时间按本机 boot（2026-11-26 00:34 UTC）计。第 51 次恢复刚在约 07:46 完成（提交 ba0c6a8），不到一小时本机又被换掉，51 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次新坑（删 cogentco 误删整行 URIs）：`sed '/cogentco/d' ubuntu.sources` 会把含 cogentco 的整行删掉——而 URIs 行是 azure 与 cogentco 写在同一行，于是 azure 也被一起删掉，文件只剩 Types/Suites/Components，没有 URIs。修法：直接用 printf 重写整个文件，URIs 行只留 azure。教训：删 cogentco 别用行删除，改写 URIs 行只留 azure，或 rm + tee 重建时先核对新文件内容。
- 其余老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt；cogentco 已删（只留 azure，备份 ubuntu.sources.bak-cogentco）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（这次用 `cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 22/23/25/28/30/44/50 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32/45/46/47/48/49/51 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在；/proc/<pid>/maps 读法在第 28 次已确认走不通，改用"无 GPU 设备文件 + 删后加载验证"），清后剩 6.9G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，全程约 2.5–2.6 MB/s 匀速、收尾未明显变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 1 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件、表观大小早早就到 4.68G，看 du 才是真实进度；本次 du 读数（约 11 分钟 1.9G、约 13 分钟 2.1G、约 28 分钟 4.2G、约 29 分钟 4.7G 完成）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。另：轮询拉取进程的输出会把进度条刷屏（一次 poll 吐出 2.3MB），以后只用 du + `ollama list` 在别的命令里看进度，别直接 poll 拉取会话。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（75.1 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测"你好，请用一句话介绍你自己"→"你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。"（model=ollama-local，tokens 35/21，耗时 75.1s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十三次换机重起补充（2026-11-26 约 10:30 CST 换机，约 11:28 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 02:30 UTC）计。第 52 次恢复刚在约 10:02 完成（提交 1713b99），约半小时后本机又被换掉，52 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次新坑（解压目标目录写错）：`zstd -dc 包 | tar -x -C /usr` 把文件解到了 `/usr/bin/ollama` 与 `/usr/lib/ollama`，因为 tar 包顶层就是 `bin/` 与 `lib/`；标准流程是解到 /usr/local。修法：`mv /usr/bin/ollama /usr/local/bin/ollama && mv /usr/lib/ollama /usr/local/lib/ollama`，之后一切正常。教训：解压命令务必写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置再往下走。
- 其余老步骤一次走通：zstd 本次没装，apt update + install 一次成功（开机无 apt 锁进程，全程约 27 秒）；cogentco 按第 52 次教训改写 URIs 行只留 azure（用 sed 做行内替换、不是整行删，核对文件后 URIs 只剩 azure）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压（见上条新坑），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序：开局根盘剩 7.5G（全新机），装完 Ollama 后剩 5.4G；拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 不存在），清后剩 6.4G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 25.5 分钟，全程约 2.5–3 MB/s 匀速）、nomic-embed-text 0a109f422b47（274MB，重拉，约 1.5 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（blobs 目录含 0.5b 的 380M：约 1 分钟 1.1G、约 5 分钟 1.8G、约 11 分钟 2.7G、约 17 分钟 3.5G、约 23 分钟 4.5G、约 25 分钟 4.8G 完成）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。另：只用 du + `ollama list` 在别的命令里看进度，别直接 poll 拉取会话（第 52 次教训：一次 poll 吐 2.3MB 进度条）。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（42.5 秒，是历次最快的一次，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“请用一句话介绍你自己”→“我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 34/15，耗时 42.5s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 2.1G。

## 第五十四次换机重起补充（2026-11-26 约 12:37 CST 换机，约 13:42 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 04:37 UTC）计。第 53 次恢复刚在约 11:28 CST 完成（提交 5f38ed1），约一小时后本机又被换掉，53 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt；cogentco 按第 52 次教训改写 URIs 行只留 azure（用 sed 做行内替换、不是整行删，备份 ubuntu.sources.bak-cogentco，核对后 URIs 只剩 azure）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 4.9G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.3G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 26 分钟，前段约 3.5–3.8 MB/s、后段约 2.6 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 93 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 120 秒 + du，kill -0 判拉取进程存活，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（blobs 目录含 0.5b 的 380M：约 1 分钟 442M、约 5 分钟 1.3G、约 11 分钟 2.1G、约 17 分钟 3.1G、约 23 分钟 4.0G、约 26 分钟 4.8G 完成）显示拉取匀速约 2.6–3.8 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（61.6 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 35/17，耗时 61.6s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十五次换机重起补充（2026-11-26 约 14:36 CST 换机，约 15:30 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 06:36 UTC）计。第 54 次恢复刚在约 13:42 CST 完成（提交 a74194c），约一小时后本机又被换掉，54 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 22/23/25/28/30/44/50 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32/45/46/47/48/49/51/53/54 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.5G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27.5 分钟，前段约 3.5–3.8 MB/s、中段约 2.5 MB/s、收尾约 1.7–2.5 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 95 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 120 秒 + du，grep `ollama list` 判 7b 在列，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（blobs 目录含 0.5b 的 380M：约 2 分钟 821M、约 4 分钟 1.2G、约 6 分钟 1.7G、约 8 分钟 2.1G、约 10 分钟 2.4G、约 12 分钟 2.7G、约 14 分钟 3.0G、约 16 分钟 3.3G、约 18 分钟 3.5G、约 20 分钟 3.8G、约 22 分钟 4.1G、约 24 分钟 4.4G、约 26 分钟 4.7G、约 28 分钟 4.8G 完成）显示拉取匀速约 2.5–3.8 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 在 120 秒时报 model_unavailable（第 44 次的冷启动教训重现：模型刚拉完、首次加载 + 生成超过了 120 秒超时；`ollama ps` 已见 7b 在跑 5.1GB、100% CPU，说明模型正在加载、不是服务坏了），等加载完重试一次即正常回复（28.1 秒）。教训不变：第一次 /chat 若报 model_unavailable，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 28.1s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十六次换机重起补充（2026-11-26 约 16:33 CST 换机，约 17:55 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 08:33 UTC）计。第 55 次恢复刚在约 15:30 CST 完成（提交 f749526），约一小时后本机又被换掉，55 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.8G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27 分钟，前段约 3.5–3.8 MB/s、中段约 2.5 MB/s、收尾约 2.5 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 99 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（blobs 目录含 0.5b 的 380M：约 2 分钟 827M、约 7 分钟 1.8G、约 9 分钟 2.2G、约 11 分钟 2.5G、约 13 分钟 2.8G、约 15 分钟 3.1G、约 17 分钟 3.4G、约 19 分钟 3.7G、约 21 分钟 4.0G、约 23 分钟 4.3G、约 25 分钟 4.5G、约 27 分钟 4.8G 完成）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（72.4 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 72.4s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十七次换机重起补充（2026-11-26 约 18:34 CST 换机，约 19:31 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 10:34 UTC）计。第 56 次恢复刚在约 17:55 CST 完成（提交 e53ca3e），不到一小时本机又被换掉，56 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 22/23/25/28/30/44/50/55 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.5G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27.7 分钟：起步约 48 MB/s 的突发、随后稳定约 2.5 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 138 秒含写 manifest），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 300/600 秒 + du，`pgrep -x ollama` 判拉取进程存活，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（blobs 目录含 0.5b 的 380M：约 5 分钟 1.8G、约 15 分钟 3.2G、约 25 分钟 4.7G、约 27.7 分钟完成）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（87.5 秒，未触发第 44 次的冷启动 120 秒超时——第 55 次第一次曾超时、重试才通），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 87.5s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十八次换机重起补充（2026-11-26 约 20:42 CST 换机，约 22:09 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 12:42 UTC）计。第 57 次恢复刚在约 19:31 CST 完成（提交 8a66cb5），约一个半小时后本机又被换掉，57 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（不是 770），ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 30 分钟，起步约 3.5 MB/s、随后约 2.5 MB/s 匀速）、nomic-embed-text 0a109f422b47（274MB，重拉，约 96 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 300 秒 + du，`ollama list` 判 7b 在列，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（blobs 目录含 0.5b 的 380M：约 6 分钟 1.6G、约 11 分钟 2.5G、约 16 分钟 3.2G、约 21 分钟 3.9G、约 26 分钟 4.7G、约 31 分钟 4.8G 完成）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（109.1 秒，接近 120 秒上限但未触发第 44 次的冷启动超时；是历次最慢的一次，纯 CPU 7b 正常波动），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 109.1s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。pytest 已升到 9.2.0。根盘剩 1.9G。

## 第五十九次换机重起补充（2026-11-26 约 22:31 CST 换机，约 23:21 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 14:31 UTC）计。第 58 次恢复刚在约 22:09 CST 完成（提交 0eb3a93），约半小时后本机又被换掉，58 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次又没了，开机无 apt 锁进程；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 22/23/25/28/30/44/50/55/57 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.1G，装完 Ollama 后剩 5.1G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 28 分钟，起步约 3.5 MB/s、随后约 2.5 MB/s 匀速）、nomic-embed-text 0a109f422b47（274MB，重拉，约 93 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（blobs 目录含 0.5b 的 380M：约 10 分钟 2.4G、约 20 分钟 3.9G、约 30 分钟 4.8G 完成）显示拉取匀速约 2.5 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（72.7 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 72.7s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 2.0G。

## 第六十次换机重起补充（2026-11-27 约 00:31 CST 换机，约 01:44 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 16:31 UTC）计。第 59 次恢复刚在约 23:21 CST 完成（提交 7d564d4），约 1 小时 10 分钟后本机又被换掉，59 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次已装（/usr/bin/zstd），全程没走 apt、没用 dpkg-deb；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.1G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.9G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，380M）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉）、nomic-embed-text 0a109f422b47（274MB，重拉），digest 与 S2-4 一致；`ollama list` 三模型齐全。两 pull 合计约 31 分钟（7b 起步约 3.7–7.4 MB/s、随后约 3.4–4.5 MB/s）；7b 的 partial 文件是稀疏文件，看 du 才是真实进度，别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（62.7 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 62.7s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十一次换机重起补充（2026-11-27 约 02:37 CST 换机，约 03:13 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 18:37 UTC）计。第 60 次恢复刚在约 01:44 CST 完成（提交 02a4b1a），不到 1 小时后本机又被换掉，60 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次又没了，开机有 os-intent 重放进程（apt-get update，pid 1038）在占 apt/dpkg 锁，不要杀它；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/* 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.4G，装完 Ollama 后剩 5.6G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 7.2G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，18:42:34–19:09:30 UTC，约 27 分钟，约 2.6–3 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 91 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（partial 文件：约 2.5 分钟 396M、约 7 分钟 1.2G、约 13 分钟 2.4G、约 27 分钟完成）显示拉取匀速约 2.6–3 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（68.0 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 68.0s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十二次换机重起补充（2026-11-27 约 04:45 CST 换机，约 05:46 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 20:45 UTC）计。第 61 次恢复刚在约 03:13 CST 完成（提交 ab6ea8b），约 1 小时 52 分钟后本机又被换掉，61 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次恢复分两段：上一轮恢复子代理 32f5f419（约 05:06 CST 派发）已被取消、未完成，但它已装回 Ollama 并在跑（`ollama serve` PID 8129，约 05:07 UTC 起，systemd 服务 active + 代理 drop-in 已建）、0.5b 已在列、7b 已拉到 1.8G（partial 文件 sha256-2bada8a74506…）。本轮未假设它做完，每一步自己核实：Ollama 0.40.17、服务 active、7b partial 真实存在（du 1.8G），确认无误后只续拉、不重装。
- 本次新情况（磁盘与 GPU 库）：本机 `/usr/local/lib/ollama` 只有 35M，里面根本没有 cuda_v12/v13/vulkan（上一轮的安装或镜像里就不带），所以“拉模型前先清 GPU 库约 2.4G”这一步本次无库可清、直接跳过；开局根盘剩 4.7G，7b 剩余约 2.9G + nomic 0.27G，余量够用，未触发 ENOSPC。/dev/nvidia* 与 /proc/driver/nvidia 均不存在（本机无 GPU）。zstd 本次全程没用到（无重装）、cogentco 没删（没走 apt）——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61 次相同：这次 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 模型：qwen2.5:0.5b a8b0c5157701（397MB，上一轮已在列，无需拷回）、qwen2.5:7b 845dbda0ea48（4.7GB，上一轮 partial 1.8G 续拉，05:25–05:43 CST 约 18 分钟，约 2.7 MB/s，服务未重启、续传一次成功，第 15 次的“重启打断续传损坏”坑没重现）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（续拉起点 1.8G、约 5 分钟 2.6G、约 9 分钟 3.3G、约 14 分钟 3.9G、约 18 分钟完成）显示拉取匀速约 2.7 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（81.0 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 35/17，耗时 81.0s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十三次换机重起补充（2026-11-27 约 06:34 CST 换机，约 07:13 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-26 22:34 UTC）计。第 62 次恢复刚在约 05:47 CST 完成（提交 9ee7adb），约 47 分钟后本机又被换掉，62 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次已装（第 60 次后又一次在），全程没走 apt、没用 dpkg-deb；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/62 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩约 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.6–6.7G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，22:42:33–23:09:xx UTC，约 27 分钟，约 2.5–3.7 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 91 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 600/780 秒 + du + `ollama list`，`pgrep -x ollama` 判服务存活，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（blobs 目录含 0.5b 的 380M：约 10.5 分钟 2.3G、约 24 分钟 4.5G、约 27 分钟完成）显示拉取匀速约 2.5–3.7 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（66.8 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍自己。”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 36/17，耗时 66.8s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十四次换机重起补充（2026-11-27 约 08:44 CST 换机，约 09:58 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-27 00:44 UTC）计。第 63 次恢复刚在约 07:13 CST 完成（提交 79b862e），约 1.5 小时后本机又被换掉，63 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装（/usr/bin/zstd），全程没走 apt、没用 dpkg-deb，cogentco 未核——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/62/63 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.1G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.8G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，约 28 分钟；开局约 15 MB/s 冲到 5 MB/s 档，中段约 5 MB/s、后段约 2.6 MB/s、收尾约 1.7 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 600/900/300 秒 + du + `ollama list`，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（partial 实占：约 10 分钟 2.1G、约 24 分钟 4.3G、约 28 分钟完成）显示拉取后段明显放慢；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（62.5 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 62.5s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十五次换机重起补充（2026-11-27 约 10:38 CST 换机，约 11:13 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-27 02:38 UTC）计。第 64 次恢复刚在约 09:58 CST 完成（提交 3f7b8e2），约 40 分钟后本机又被换掉，64 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次又没了（/usr/bin/zstd 不在），开机有 os-intent 重放进程在跑 apt-get update（占 apt/dpkg 锁，不要杀它）；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt；cogentco 未核——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local，Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 22/23/25/28/30/44/50/55/57/59 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/62/63/64 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.4G，装完 Ollama 后剩 5.5G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.7G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，约 28 分钟；开局约 19–59 MB/s，随后回落到 2.6–2.9 MB/s 档并维持到收尾，收尾约 1.9 MB/s）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 watcher（sleep 540/900/360 秒 + du + `ollama list`，不用 pgrep -f、不直接 poll 拉取会话）的 du 读数（partial 实占：约 9 分钟 1.9G、约 24 分钟 4.1G、约 28 分钟完成）显示拉取中段起稳定在 2.6–2.9 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（68.1 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 35/17，耗时 68.1s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十六次换机重起补充（2026-11-27 约 12:48 CST 换机，约 14:16 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-27 04:48 UTC）计。第 65 次恢复刚在约 11:13 CST 完成（提交 2596e63），约 1.5 小时后本机又被换掉，65 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59/65 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.9G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，05:43:54–06:11:11 UTC，约 27.3 分钟，约 2.5–3.8 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 116 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（blobs 目录含 0.5b 的 380M：约 3 分钟 1008M、约 5 分钟 1.5G、约 7 分钟 2.0G、约 9.5 分钟 2.3G、约 11.5 分钟 2.6G、约 14 分钟 2.9G、约 16 分钟 3.3G、约 18 分钟 3.6G、约 20 分钟 3.9G、约 22.5 分钟 4.2G、约 24.5 分钟 4.5G/93%、约 27.3 分钟 4.8G 完成）显示拉取匀速约 2.5–3.8 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 在 120 秒时报 model_unavailable（第 44/55 次的冷启动教训重现：模型刚拉完、首次加载 + 生成超过了 120 秒超时；`ollama ps` 已见 7b 在跑 5.1GB、100% CPU，说明模型正在加载、不是服务坏了），等加载完重试一次即正常回复（30.2 秒）。教训不变：第一次 /chat 若报 model_unavailable，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 30.2s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十七次换机重起补充（2026-11-27 约 14:42 CST 换机，约 15:16 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-27 06:42 UTC）计。第 66 次恢复刚在约 14:16 CST 完成（提交 4676e37），约 26 分钟后本机又被换掉，66 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次又没了，开机无 apt 锁进程；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（解压写 `-C /usr/local`，第 53 次的坑没重现），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -r ~/.ollama/models/* 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/66 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59/65 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.4G，装完 Ollama 后剩 5.6G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.3G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，重拉，约 27.3 分钟，约 2.0–4.0 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 101 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 56.3s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十八次换机重起补充（2026-11-27 约 16:44 CST 换机，约 17:43 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-27 08:44 UTC）计。第 67 次恢复刚在约 15:16 CST 完成（提交 44ad3ac），约 1.5 小时后本机又被换掉，67 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次是接力恢复：上一任恢复子代理 8607b9c8 被取消（未完成、无结果），但它已取得进展——Ollama 0.40.17 已装回、0.5b 已拷回（a8b0c5157701）、7b 已拉到约 1.8G partial；本次从当前状态继续，没从头重装 Ollama（systemd 服务 + 代理 drop-in 均已在，服务 active，`/usr/local/lib/ollama` 权限 755 直接可用）。
- 本次新情况有二：① 本机 Ollama 安装里没有 cuda_v12/v13/vulkan 目录（`/usr/local/lib/ollama` 只有约 40MB 的 CPU 库），“拉模型前先清 2.4G GPU 库”这步本次不适用——清无可清；根盘 4.8G 仍够拉 7b 剩余（约 2.9G）+ nomic（0.27G），直接拉。② 17:05 时根盘剩 6.9G、17:20 时剩 4.8G，约 2.1G 去向已查明：上一任子代理拉 7b 留下的 partial 文件（`sha256-2bada8a74506…-partial` 实占 1.8G，另有 `-partial-0`…`-partial-15` 各 4K）——不是磁盘泄漏，是上一任的拉取进度。续传干净：从 1.8G 续到 4.7G 完成，digest 与 S2-4 一致，未触发第 15 次的续传损坏（那次是拉取中重启服务所致；本次拉取期间未重启 ollama 服务）。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，续传，09:20:38–09:38:34 UTC，约 18 分钟，约 2.6 MB/s；watcher du 读数：约 9 分钟 3.2G、约 18 分钟完成）、nomic-embed-text 0a109f422b47（274MB，重拉，约 100 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（70.6 秒，未触发第 44 次的冷启动 120 秒超时；第 66 次曾在 120 秒报 model_unavailable，本次没重现），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 70.6s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第六十九次换机重起补充（2026-11-27 约 18:41 CST 换机，约 19:16 CST 恢复完成）

- 本次换机时间按本机 boot（2026-11-27 10:41 UTC）计。第 68 次恢复刚在约 17:43 CST 完成（提交 ab8a17d），约 1 小时后本机又被换掉，68 次的本地安装与模型全部清零；GitHub 上的提交未受影响。
- 本次无新坑，老步骤一次走通：zstd 这次又没了，开机有 os-intent 重放进程（apt-get update）在占 apt/dpkg 锁，不要杀它；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（第 53 次的坑没重现：解压写 `-C /usr/local`，解完先 `ls /usr/local/bin/ollama` 核对位置），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（`cp -a ~/.ollama/models/. 目标/` 拷内容，第 48 次的 models/models 嵌套坑没重现）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/62/63/64/66/67/68 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59/65 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.4G，装完 Ollama 后剩 5.3G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.9G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，10:44–约 11:11 UTC，约 27 分钟：开局约 44 MB/s 突发、随后稳定约 2.6 MB/s，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；本次 du 读数（blobs 目录含 0.5b 的 380M：约 10 分钟 2.4G、约 20 分钟 3.9G、约 27 分钟完成）显示拉取匀速约 2.6 MB/s；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（63.3 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 63.3s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第七十二次换机重起补充（2026-11-27 约 20:47 CST 换机，约 21:49 CST 恢复完成）

- 编号依据：`git log --oneline --grep=换机恢复` 核对，仓库里最新提交仍是第 69 次（f5831b4，19:16 完成）；其后 20:11 轮与 20:41 轮各派过一个恢复子代理（分别称第 70 次、第 71 次），两个都被取消、无完成记录、无日志、无提交。本次换机时间按本机 boot（2026-11-27 12:47:28 UTC）计，即第 72 台机器；70、71 次无据可写，不编。
- 本次新坑（cp 嵌套换了个触发方式）：服务先起、已在 /usr/share/ollama/.ollama 下建好空 models 目录后，再 `cp -a ~/.ollama/models /usr/share/ollama/.ollama/models` 会把源目录整个拷进已存在的目标里，变成 models/models 嵌套，`ollama list` 为空（第 48/66 次的嵌套坑是 cp 用法问题，本次是目标目录已被服务预建）。修法：把 models/models 下的 blobs/manifests/manifests-v2/metadata 内容上移一层、删掉空嵌套目录，再 `systemctl restart ollama`（当时未在拉模型，重启安全），`ollama list` 见到 0.5b 才算完。教训：拷模型前先看目标目录是否已存在；已存在就用拷内容方式（`cp -a ~/.ollama/models/. 目标/` 或 `cp -r ~/.ollama/models/* 目标/`），不要整目录拷。
- 其余老步骤一次走通：zstd 1.5.5 已装、开机无 apt 锁进程，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（解压写 `-C /usr/local`，第 53 次的坑没重现），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（修完嵌套后，`ollama list` 见 a8b0c5157701）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/62/63/64/66/67/68/69 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59/65 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，13:18–约 13:44 UTC，约 26 分钟：开局约 3.0 MB/s、约 7 分钟 32%（1.5G）、约 16 分钟 70%（3.3G）、收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，约 95 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过，但耗时 119.3 秒，比最近几轮（56–70 秒）明显慢，未触发第 44 次的冷启动 120 秒超时（险些超时）；model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 35/17，耗时 119.3s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第七十三次换机重起补充（2026-11-27 约 23:13 CST 换机，约 23:49 CST 恢复完成）

- 编号依据：`git log` 核对，仓库里最新提交仍是第 72 次（093ce5e，21:49 完成）；其后无 70/71 之外的编号缺口（70、71 次无对应提交，见第 72 次记录）。本次换机时间按本机 boot（2026-11-27 15:13:11 UTC）计，即第 73 台机器。
- 本次新情况：家目录 `~/.ollama/models` 属 root:nogroup（drwxr-s---），`sudo -u ollama cp` 拷 0.5b 报 Permission denied（第 72 次的嵌套坑是另一回事）；另注意以 ollama 用户跑 `cp -a ~/.ollama/models/.` 时 `~` 会展开成 /usr/share/ollama，不是 /home/hatch。修法：`sudo cp -a /home/hatch/.ollama/models/. /usr/share/ollama/.ollama/models/` 再 `chown -R ollama:ollama`，`ollama list` 见到 0.5b 才算完。教训：拷模型一律用绝对路径 + sudo 拷 + chown，别用 `~`、别以 ollama 用户直接拷家目录文件。
- 其余老步骤一次走通：zstd 这次又没了，开机有 os-intent 重放进程（apt-get update，pid 949）在占 apt/dpkg 锁，不要杀它；按第 14/23/25 次的办法，`dpkg-deb -x` 从本地缓存取回 zstd 1.5.5，全程没走 apt（cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）；本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（解压写 `-C /usr/local`，第 53 次的坑没重现），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（见上条）。
- 与第 19/20/21/27/31/32/45/46/47/48/49/51/53/54/56/58/60/61/62/63/64/66/67/68/69/72 次相同：这次 tar 包解出来的 `/usr/local/lib/ollama` 权限直接就是 755（第 22/23/25/28/30/44/50/55/57/59/65 次是 770），llama-server 以 ollama 用户一次跑通（version 0.9.0-dev）；但 770 的教训仍保留——装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.4G，装完 Ollama 后剩 5.7G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 7.1G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，15:19:12–15:46:08 UTC，26 分 56 秒：约 2.5 分钟 855M、约 6.5 分钟 1.7G、约 11 分钟 2.5G、约 15 分钟 3.1G、约 19 分钟 3.7G、约 23 分钟 4.3G，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，15:46:11–15:47:55 UTC，1 分 44 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（55.2 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 35/17，耗时 55.2s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 2.0G。

## 第七十四次换机重起补充（2026-11-28 约 01:09 CST 换机，约 01:51 CST 恢复完成）

- 编号依据：`git log` 核对，仓库里最新提交仍是第 73 次（671259d，23:49 完成）；其后无编号缺口。本次换机时间按本机 boot（2026-11-27 17:09:37 UTC）计，即第 74 台机器。
- 本次新情况有二：① zstd 这次已装（/usr/bin/zstd），开机无 apt 锁进程，全程没走 apt、没用 `dpkg-deb -x` 取回；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）。② 这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 73 次是 755），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 其余老步骤一次走通：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（解压写 `-C /usr/local`，第 53 次的坑没重现），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（绝对路径 `cp -a /home/hatch/.ollama/models/. 目标/` 拷内容，服务已预建空 models 目录、未触发第 72 次的 models/models 嵌套坑）。
- 磁盘时序（第二十次的教训）：开局根盘剩 7.0G，装完 Ollama 后剩 5.3G，拷 0.5b 后剩 4.7G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.6G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，约 17:20–17:46 UTC，约 26 分钟：开局约 21 MB/s 突发、随后约 2.5–2.8 MB/s，收尾变慢；du 读数：约 4 分钟 7b 实占 1.1G、约 6 分钟 1.5G、约 8 分钟 1.8G、约 16 分钟 3.0G、约 24 分钟 4.2G）、nomic-embed-text 0a109f422b47（274MB，重拉，约 17:46–17:48 UTC，约 2 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（41.6 秒，比最近几轮 55–119 秒都快，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型。”（model=ollama-local，tokens 35/17，耗时 41.6s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。
## 第七十五次换机重起补充（2026-11-28 约 03:19 CST 换机，约 04:34 CST 恢复完成）

- 编号依据：`git log` 核对，仓库里最新提交仍是第 74 次（b29e07a，01:51 完成）；其后无编号缺口。本次换机时间按本机 boot（2026-11-27 19:19:54 UTC）计，即第 75 台机器。
- 本次新情况有二：① zstd 这次已装（/usr/bin/zstd 1.5.5），开机无 apt 锁进程，全程没走 apt、没用 `dpkg-deb -x` 取回；cogentco 仍在源里，没删——下次如需 apt 仍须按 README 重删（rm + tee 重建或 sed 行内替换，见第 27/52 次）。② 这次 tar 包解出来的 `/usr/local/lib/ollama` 权限又是 770（第 74 次也是 770），起服务前 `chmod 755` 修掉，之后 ollama 用户跑 llama-server --version（0.9.0-dev）一次通过。教训不变：每次装完先看权限、先以 ollama 用户跑一次 llama-server --version 再往下走，别赌。
- 其余老步骤一次走通：本地包 sha256 已验 1df9d622…，`zstd -dc` 流式解压到 /usr/local（解压写 `-C /usr/local`，第 53 次的坑没重现），Ollama 0.40.17；useradd 按官方形式一次建对（`useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama`，带 `-m -d`，另加 video/render 组，第 21/31 次的坑没重现）；systemd 服务按官方 unit 重建 + 代理 drop-in（取自 /etc/environment），服务第一次起就 active；0.5b 家目录拷回并 `chown -R ollama:ollama`（绝对路径 `cp -a /home/hatch/.ollama/models/. 目标/` 拷内容，服务已预建空 models 目录、未触发第 72 次的 models/models 嵌套坑）。
- 磁盘时序（第二十次的教训）：开局根盘剩 6.9G，装完 Ollama 后剩 5.0G，拷 0.5b 后剩 4.7G，7b（4.7G）+ nomic（0.27G）不够；这次在拉模型前先清了 cuda_v12/v13 + vulkan（约 2.4G，清之前核过 /dev/nvidia* 与 /proc/driver/nvidia 均不存在），清后剩 6.5G 再拉，7b 加载正常、/chat 正常，证明删库安全。拉取期间未重启 ollama 服务（第 15 次教训）。注意：本次 ~/.ollama/models 只有 0.5b（380M），7b 没有 partial 可续传，只能从头重拉。
- 模型：qwen2.5:0.5b a8b0c5157701（家目录拷回，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，从头重拉，20:01:44–约 20:29:30 UTC，约 28 分钟：du 读数约 4 分钟 7b 实占 0.7G、约 6 分钟 1.0G、约 8 分钟 1.4G、约 10 分钟 1.8G、约 12 分钟 2.2G、约 14 分钟 2.5G、约 16 分钟 2.8G、约 18 分钟 3.1G、约 20 分钟 3.4G、约 22 分钟 3.7G、约 24 分钟 4.0G、约 26 分钟 4.3G，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，重拉，20:31:01–约 20:32:33 UTC，约 1.5 分钟），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。另：`pgrep -f "ollama pull"` 会匹配到执行命令自身的 shell（命令行里含拉取命令），拉完后仍误报 PULL_RUNNING——第 51 次 watcher 的教训重现，判拉取是否结束须看日志 success 或 `ollama list`，别只信 pgrep -f。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（63.1 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 63.1s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

## 第七十六次换机重起补充（2026-11-28 约 05:05 CST 换机，约 06:02 CST 恢复完成）

- 编号依据：`git log` 核对，仓库里最新提交仍是第 75 次（edfda42，04:34 完成）；其后无编号缺口。本次换机时间按本机 boot（2026-11-27 21:05:53 UTC）计，即第 76 台机器。
- 本次是接力恢复：上一任恢复子代理（43dee124）约 05:41 被取消、无结果，但它留下的进程还在跑——Ollama 已装好（0.40.17）、serve 在跑（PID 8237，21:30 UTC 起）、7b 拉取进行中（PID 8342，21:30 UTC 起）、0.5b 已在列。本次接力没有重做、没有 kill 拉取进程、没有重启服务，只盯日志等 7b 拉完，再拉 nomic、起聊天服务、验证、记档。
- 本次新情况：接力时 7b 已拉到 62%（2.9GB，约 2.8 MB/s）；zstd、cogentco、/usr/local/lib/ollama 权限、cuda 库均未动——上一任已处理好（/usr/local/lib/ollama 只有 35M，本机没有 cuda_v12/v13/vulkan 目录，不用清那一步；cogentco 仍在源里，全程没走 apt所以没删——下次如需 apt 仍须按 README 重删，rm + tee 重建或 sed 行内替换，见第 27/52 次）。
- 小插曲：起服务那次 exec 的 `tail -5 server.log` 因主 shell 没 cd，读到的是 /home/hatch/server.log（2026-11-23 留下的旧文件，第 23 次那个坑的遗留），里面只有旧报错行，容易误判服务没起来；实际 jarvis-chat/server.log 写的是 listening，/health 当即 ok。教训：核服务状态只看 /health 与 jarvis-chat/server.log，别 tail 错目录的旧日志。
- 磁盘时序（第二十次的教训）：接力时根盘剩 3.8G，7b 余量约 1.8G + nomic 0.27G 刚好够；拉完 7b 后剩 2.2G，拉完 nomic 后剩 1.9G，全程未触发 ENOSPC。本次没清任何东西（无 cuda 库可清）。
- 模型：qwen2.5:0.5b a8b0c5157701（上一任已备好，397MB）、qwen2.5:7b 845dbda0ea48（4.7GB，上一任 21:30 UTC 起拉、21:57:44 UTC 出现 success，约 28 分钟：约 21:46 时 62%、21:48 时 69%、21:53 时 85%，收尾变慢）、nomic-embed-text 0a109f422b47（274MB，21:57:35–21:59:17 UTC，约 1 分 42 秒），digest 与 S2-4 一致；`ollama list` 三模型齐全。注意：7b 的 partial 文件是稀疏文件，看 du 才是真实进度；别因进度条数字就误判卡死、也别中途杀掉重拉。
- 聊天服务：7b 拉完后才起（默认模型已是 7b；7b 未到之前起服务 /chat 会报 503），setsid 形式（第 23 次的教训）。起服务那次 exec 报 output_drain_timeout，是已知情况（第 25 次的教训：server.py 子进程还在跑，不是起失败），/health 当即 ok=true，不用杀它。本次第一次 /chat 一次通过（59 秒，未触发第 44 次的冷启动 120 秒超时），model_unavailable 的教训仍保留——若报该错，先看 `ollama ps` 确认 7b 是否在加载，在加载就等一会儿重试，不要反复重启服务或重拉模型。
- 验证：/health ok=true、router_mode=live、model_present=true（三模型在列）；/chat 实测“你好，请用一句话介绍你自己”→“你好，我叫Qwen，是来自阿里云的大规模语言模型，很高兴为你服务。”（model=ollama-local，tokens 35/21，耗时 59s，$0）；`ollama ps` 见 qwen2.5:7b 845dbda0ea48 在跑（5.1GB，100% CPU）。根盘剩 1.9G。

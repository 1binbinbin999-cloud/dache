"""Jarvis P3 — 沙箱出站阻断演练（真断网验证）

P0 遗留问题：P0 的“无网络启动”测试只清了环境变量、查了代码里没有
socket/urllib import，没有做系统级断网验证。本文件补上这一项。

方法：用 `unshare -rn` 创建一个没有网络接口的网络命名空间，在里面跑
`jarvis.py boot` 和 `router-call`。命名空间内连 8.8.8.8:53 都返回
101（Network is unreachable），是真正的系统级断网，不是代码自查。

如果当前环境不支持 unshare（返回码非 0），sandbox_check 返回
mode=unsupported，不谎报通过。云端实测：unshare 可用。
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _scrub_env(env: dict | None) -> dict:
    """清掉密钥类环境变量：提案代码在沙箱里读不到真实配置与密钥。
    另清掉 agent 运行时追踪上下文（JARVIS_TRACE_CONTEXT 等）：它含最近
    对话与工具调用参数，04:10 对抗验证里经未隔离的 /proc 泄露过，绝不能
    进沙箱。"""
    merged = dict(os.environ if env is None else env)
    for key in list(merged):
        upper = key.upper()
        if ("API_KEY" in upper or "OPENAI" in upper or "ANTHROPIC" in upper
                or "TRACE_CONTEXT" in upper
                or upper.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD"))):
            del merged[key]
    merged["TMPDIR"] = "/tmp"
    return merged


def _set_resource_limits() -> None:
    """子进程资源上限（P6-19）：地址空间 2GiB、进程数 256、CPU 300 秒。

    在 preexec_fn 里执行，随 unshare 继承到命名空间内的全部子进程。
    """
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 330))


# 沙箱启动脚本（sh -c，$1=临时复制目录，$2=工程拷贝目录，$3=真实工程目录，
# 之后是待执行命令）。
# 顺序不能乱：先把工程拷贝 bind 到真实路径后 remount 只读（此时 /tmp 还可见；
# 拷贝是一次性的，即使命名空间内把绑定 remount 成 rw，写操作也只落到拷贝上，
# 宿主机真实目录不受影响）。根 / 是 overlayfs 挂载点，在子用户命名空间里
# 无法 bind/remount（实测报错），故改为逐个处理顶层目录。实测规律：
# bind 只能用于无锁定子挂载点的普通目录（/usr、/boot、/root、/srv、/media）；
# 含锁定子挂载点的目录（/、/etc、/opt、/var、/home）bind 会失败，改盖 tmpfs
# （新挂载不受此限；/opt、/var、/run 沙箱命令不需要，直接盖掉；/var 盖掉后
# 需先 mkdir -p /var/tmp 再盖 /var/tmp）；/etc 保持原样（沙箱命令要读
# /etc/passwd 等，是残留缺口）；/home 是真实工程目录的祖先，盖掉会遮住
# 工程路径，故跳过（/home/hatch 等挂载点仍可写，残留缺口）。
# /dev、/sys 保持原样（沙箱命令要读 /dev/null、/dev/urandom；sysfs 本就基本只读）。
# 再把临时目录 bind 到 /mnt，盖掉 /proc（空 tmpfs；本环境禁止挂载新的 proc，
# 故直接隐藏，沙箱内看不到任何进程），最后 cd /mnt 执行命令。
# 退出码 21-32 表示沙箱搭建失败（调用方不得降级为无隔离执行）。
_SANDBOX_SH = (
    'dest="$1"; copy="$2"; real="$3"; shift 3; '
    'mount --bind "$copy" "$real" || exit 21; '
    'mount -o remount,ro,bind "$real" || exit 22; '
    'mount --bind /usr /usr || exit 23; '
    'mount -o remount,ro,bind /usr || exit 24; '
    'mount --bind /boot /boot || exit 23; '
    'mount -o remount,ro,bind /boot || exit 24; '
    'mount --bind /root /root || exit 23; '
    'mount -o remount,ro,bind /root || exit 24; '
    'mount --bind /srv /srv || exit 23; '
    'mount -o remount,ro,bind /srv || exit 24; '
    'mount --bind /media /media || exit 23; '
    'mount -o remount,ro,bind /media || exit 24; '
    'mount -t tmpfs tmpfs /opt || exit 25; '
    'mount -t tmpfs tmpfs /var || exit 26; '
    'mkdir -p /var/tmp && mount -t tmpfs tmpfs /var/tmp || exit 27; '
    'mount -t tmpfs tmpfs /run || exit 28; '
    'mount --bind "$dest" /mnt || exit 29; '
    'mount -t tmpfs tmpfs /tmp || exit 30; '
    'mount -t tmpfs tmpfs /proc || exit 31; '
    'cd /mnt || exit 32; '
    # 用 exec 直接跑命令（不 fork）：实测本环境的 setup shell（dash 与 bash
    # 均试过）在命名空间内 fork 会报 Cannot allocate memory（ENOMEM），
    # 无法用 fork+wait 传信号。--fork 使沙箱进程成为新 PID 命名空间的
    # 1 号进程；代价是信号致死会被 unshare 抹成退出码 1（见 resource_3
    # 测试的说明），调用方只能看到 ok=False，不能直接从 returncode 分辨
    # 崩溃与普通失败。
    'exec "$@"'
)

_SANDBOX_FAILURES = {
    21: "bind project copy to real dir failed",
    22: "remount project copy read-only failed",
    23: "bind top-level dir failed",
    24: "remount top-level dir read-only failed",
    25: "tmpfs on /opt failed",
    26: "tmpfs on /var failed",
    27: "tmpfs on /var/tmp failed",
    28: "tmpfs on /run failed",
    29: "bind temp dir to /mnt failed",
    30: "tmpfs on /tmp failed",
    31: "tmpfs on /proc failed",
    32: "cd /mnt failed",
}


def run_sandboxed(base_dir: Path, dest: Path, argv: list[str],
                   timeout: int = 60, env: dict | None = None) -> dict:
    """在隔离命名空间里跑一次命令（P6，I2/I3 的对应修复）。

    隔离措施：unshare 用户+挂载+网络+PID 命名空间（--fork 使沙箱进程成为
    新 PID 命名空间的 1 号进程）；真实工程目录以一次性拷贝 bind 到原路径后
    只读（remount 成 rw 也只能写到拷贝）；顶层目录逐个 bind 后 remount 只读
    （根 / 是 overlayfs 挂载点，子用户命名空间里无法直接 bind/remount）；
    临时复制目录 bind 到 /mnt 后在其中执行；/tmp 与 /var/tmp 盖 tmpfs；
    盖掉 /proc（空 tmpfs；本环境禁止挂载新的 proc，故直接隐藏）；断网；环境变量清密钥；
    资源上限。
    返回 {"mode": "unshare-ns", "ok", "returncode", "stdout", "stderr"}，
    或沙箱不可用时 {"mode": "unsupported", "ok": False, "reason": ...}。
    调用方收到 unsupported 时必须判 fail，绝不能静默降级为无隔离执行。
    """
    # P6-fix：which() 按实际生效的环境（scrub 后的 env）查 unshare，
    # 不能按调用方的 PATH 查——否则 PATH="" 时 which 找到、Popen 却
    # 找不到，FileNotFoundError 会炸穿出来。两种情况都返回
    # mode=unsupported，绝不静默降级为无隔离执行。
    scrubbed_env = _scrub_env(env)
    if shutil.which("unshare", path=scrubbed_env.get("PATH")) is None:
        return {"mode": "unsupported", "ok": False, "reason": "unshare not found"}
    if not Path("/mnt").is_dir():
        return {"mode": "unsupported", "ok": False, "reason": "/mnt not available"}
    # P6-fix：先把工程目录完整拷贝到一次性临时目录，沙箱内只操作这个拷贝。
    # 拷贝失败则 fail-closed（返回 unsupported），绝不降级为直接跑真实目录。
    copy_root = Path(tempfile.mkdtemp(prefix="jarvis-sandbox-proj-"))
    try:
        proj_copy = copy_root / "proj"
        shutil.copytree(base_dir, proj_copy)
    except Exception:
        shutil.rmtree(copy_root, ignore_errors=True)
        return {"mode": "unsupported", "ok": False,
                "reason": "project copy failed"}
    cmd = ["unshare", "-rnmp", "--fork", "sh", "-c", _SANDBOX_SH, "sandbox",
           str(dest), str(proj_copy), str(base_dir), *argv]
    try:
        result = subprocess.run(
            cmd, cwd=base_dir, env=scrubbed_env,
            capture_output=True, text=True, timeout=timeout,
            preexec_fn=_set_resource_limits,
        )
    except FileNotFoundError:
        return {"mode": "unsupported", "ok": False, "reason": "unshare not found"}
    except subprocess.TimeoutExpired:
        return {"mode": "unshare-ns", "ok": False, "returncode": None,
                "stdout": "", "stderr": "timeout", "reason": "timeout"}
    finally:
        shutil.rmtree(copy_root, ignore_errors=True)
    if result.returncode in _SANDBOX_FAILURES:
        return {"mode": "unsupported", "ok": False,
                "reason": _SANDBOX_FAILURES[result.returncode],
                "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
    return {"mode": "unshare-ns", "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def probe_network_blocked() -> bool:
    """在 unshare 命名空间内试连 8.8.8.8:53，返回 True 表示确实连不上。"""
    code = (
        "import socket;"
        " print(socket.socket().connect_ex(('8.8.8.8',53)))"
    )
    result = subprocess.run(
        ["unshare", "-rn", sys.executable, "-c", code],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "101"


def run_offline(base_dir: Path, argv: list[str]) -> dict:
    """在断网命名空间里跑一次 CLI，返回 mode 与结果。"""
    if shutil.which("unshare") is None:
        return {"mode": "unsupported", "reason": "unshare not found", "ok": False}
    if not probe_network_blocked():
        return {"mode": "unsupported",
                "reason": "unshare namespace still has network", "ok": False}
    result = subprocess.run(
        ["unshare", "-rn", sys.executable, "jarvis.py", *argv],
        cwd=base_dir, capture_output=True, text=True, timeout=30,
    )
    try:
        output = json.loads(result.stdout)
    except Exception:
        output = {"raw_stdout": result.stdout[:500], "stderr": result.stderr[:500]}
    return {"mode": "unshare", "ok": result.returncode == 0,
            "returncode": result.returncode, "output": output}


def sandbox_check(base_dir: Path) -> dict:
    """完整演练：断网命名空间内 boot + router-call 都通过才算通过。"""
    boot = run_offline(base_dir, ["boot"])
    if not boot.get("ok"):
        return {"mode": boot.get("mode"), "ok": False, "boot": boot, "router_call": None}
    router_call = run_offline(
        base_dir, ["router-call", "--logical", "fast", "--prompt", "断网演练"])
    return {"mode": "unshare", "ok": bool(router_call.get("ok")),
            "boot": boot, "router_call": router_call}

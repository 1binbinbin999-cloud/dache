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
# $4=staging 目录（copy_root 下的 stage，进程结束由调用方删掉），
# 之后是待执行命令）。
# 顺序不能乱：先 staging（此时 /tmp、/var 还是宿主的，可见），再盖 /home
# 并把工程拷贝 bind 回真实路径（/home 是真实路径的祖先，必须先盖 tmpfs
# 再在 tmpfs 里 mkdir -p 重建路径；拷贝源在 /tmp 下的临时目录，此时
# /tmp 还是宿主 /tmp，可见）。根 / 是 overlayfs 挂载点，在子用户命名空间里
# 无法 bind/remount（实测报错），故改为逐个处理顶层目录。实测规律：
# bind 只能用于无锁定子挂载点的普通目录（/usr、/boot、/root、/srv、/media）；
# 含锁定子挂载点的目录（/、/etc、/opt、/var、/home）bind 会失败，改盖 tmpfs
# （新挂载不受此限；/opt、/var、/run 沙箱命令不需要，直接盖掉；/var 盖掉后
# 需先 mkdir -p /var/tmp 再盖 /var/tmp）。
# /etc（fix2-1）：/etc/hosts 与 /etc/resolv.conf 是独立 squashfs 子挂载，
# bind 整个 /etc 会失败；改按白名单把文件拷进 staging，再盖 tmpfs、拷回、
# 自 bind 后 remount 只读（普通 remount 在本环境报“mount point not
# mounted or bad option”失败，bind 形式的 remount 才可行，/usr 等即是
# 此法）——写 /etc 失败（EROFS），读 /etc/passwd 等正常；
# /etc/hatch/credentials 等敏感挂载点不在白名单内，沙箱内不可见。
# /home（fix2-2）：盖 tmpfs 后除重建的真实路径（工程拷贝，只读）外，
# 宿主 /home 下一切文件都不可见；再用 --rbind（连同里面的工程拷贝 bind
# 一起复制）+ bind 形式 remount 把 /home 整体只读，沙箱内连在一次性
# tmpfs 里新建文件也做不到。
# /dev、/sys（fix2-3）：/dev 盖 tmpfs，只 bind 进必需的设备文件：
# null 保持可写（`2>/dev/null` 全靠它，且它只是黑洞），zero、full、
# random、urandom、tty 只读（读是正途，写无正当用途；tty 只读还可防
# 向控制终端写；cp 拷设备文件会变成普通文件，故先 bind 到 staging
# 再 bind 进 /dev），再补 /dev/fd、stdin/out/err 符号链接（指向
# /proc/self/fd；/proc 是空 tmpfs 故悬空，与真实系统形态一致），
# 最后用 --rbind + bind 形式 remount 把 /dev 整体只读
# （普通 remount 同 /etc 一样失败）；/sys 盖空 tmpfs。
# 再把临时目录 bind 到 /mnt，盖掉 /proc（空 tmpfs；本环境禁止挂载新的 proc，
# 故直接隐藏，沙箱内看不到任何进程），最后 cd /mnt。
# 退出码 20-47 表示沙箱搭建失败（调用方不得降级为无隔离执行）。
# 命令直接 exec（fix2-4）：setup 脚本由 sandbox_manager.py 在命名空间
# init（孙进程）里跑，管理器在宿主侧等它；命令死于信号时管理器以同一
# 信号自杀（管理器是普通进程，不是 init，自杀有效），subprocess.run
# 因此看到真正的负 returncode。不要再走命名空间内 supervisor 的路：
# 实测证明命名空间 init 连 SIGKILL 自杀都死不成（SIGNAL_UNKILLABLE），
# 信号死亡到它这里必然被转成 128+sig 的退出码（见 sandbox_manager.py
# 顶部的说明）。
_SANDBOX_SH = (
    'dest="$1"; copy="$2"; real="$3"; stage="$4"; shift 4; '
    # 第一步：把 / 设成 private（MS_REC|MS_PRIVATE），等价于 unshare 命令行
    # 的默认传播设置；不设的话沙箱内的挂载会回传到宿主。必须在任何挂载前做。
    'mount --make-rprivate / || exit 20; '
    'mkdir -p "$stage/etc-stage/ssl/certs" || exit 33; '
    'cp -a /etc/passwd /etc/group /etc/hosts /etc/hostname /etc/resolv.conf '
    '/etc/nsswitch.conf /etc/localtime /etc/services /etc/protocols '
    '/etc/machine-id /etc/timezone "$stage/etc-stage/" 2>/dev/null || true; '
    'cp -a /etc/ssl/certs/. "$stage/etc-stage/ssl/certs/" 2>/dev/null || true; '
    'mkdir -p "$stage/dev-stage" || exit 34; '
    'for d in null zero full random urandom tty; do '
    'touch "$stage/dev-stage/$d" || exit 35; '
    'mount --bind /dev/$d "$stage/dev-stage/$d" || exit 35; done; '
    'mount -t tmpfs tmpfs /home || exit 36; '
    'mkdir -p "$real" || exit 37; '
    'mount --bind "$copy" "$real" || exit 21; '
    'mount -o remount,ro,bind "$real" || exit 22; '
    'mount --rbind /home /home || exit 47; '
    'mount -o remount,ro,bind /home || exit 47; '
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
    'mount -t tmpfs tmpfs /etc || exit 38; '
    'cp -a "$stage/etc-stage/." /etc/ || exit 39; '
    'mount --bind /etc /etc || exit 40; '
    'mount -o remount,ro,bind /etc || exit 40; '
    'mount -t tmpfs tmpfs /dev || exit 41; '
    'for d in null zero full random urandom tty; do '
    'touch /dev/$d || exit 42; '
    'mount --bind "$stage/dev-stage/$d" /dev/$d || exit 42; done; '
    'for d in zero full random urandom tty; do '
    'mount -o remount,ro,bind /dev/$d || exit 43; done; '
    'ln -s /proc/self/fd /dev/fd || exit 44; '
    'ln -s /proc/self/fd/0 /dev/stdin || exit 44; '
    'ln -s /proc/self/fd/1 /dev/stdout || exit 44; '
    'ln -s /proc/self/fd/2 /dev/stderr || exit 44; '
    'mount --rbind /dev /dev || exit 45; '
    'mount -o remount,ro,bind /dev || exit 45; '
    'mount -t tmpfs tmpfs /sys || exit 46; '
    'mount -t tmpfs tmpfs /opt || exit 25; '
    'mount -t tmpfs tmpfs /var || exit 26; '
    'mkdir -p /var/tmp && mount -t tmpfs tmpfs /var/tmp || exit 27; '
    'mount -t tmpfs tmpfs /run || exit 28; '
    'mount --bind "$dest" /mnt || exit 29; '
    'mount -t tmpfs tmpfs /tmp || exit 30; '
    'mount -t tmpfs tmpfs /proc || exit 31; '
    'cd /mnt || exit 32; '
    'exec "$@"'
)

_SANDBOX_FAILURES = {
    20: "mount --make-rprivate / failed",
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
    33: "mkdir /etc staging dir failed",
    34: "mkdir /dev staging dir failed",
    35: "stage device file bind failed",
    36: "tmpfs on /home failed",
    37: "mkdir real project path under /home failed",
    38: "tmpfs on /etc failed",
    39: "populate /etc from staging failed",
    40: "remount /etc read-only failed",
    41: "tmpfs on /dev failed",
    42: "bind staged device into /dev failed",
    43: "remount device bind read-only failed",
    44: "create /dev symlinks failed",
    45: "remount /dev read-only failed",
    46: "tmpfs on /sys failed",
    47: "remount /home read-only failed",
    # sandbox_manager.py 自身失败（unshare 系统调用 / id 映射 / waitpid）。
    201: "sandbox manager unshare failed",
    202: "sandbox manager id-map failed",
    203: "sandbox manager waitpid failed",
}


def run_sandboxed(base_dir: Path, dest: Path, argv: list[str],
                   timeout: int = 60, env: dict | None = None) -> dict:
    """在隔离命名空间里跑一次命令（P6，I2/I3 的对应修复）。

    隔离措施：命名空间管理器（sandbox_manager.py，见其顶部说明）用
    ctypes 调 unshare() 建用户+挂载+网络+PID 命名空间（fork 出的孙进程
    成为新 PID 命名空间的 1 号进程）；真实工程目录以一次性拷贝 bind 到原
    路径后只读（remount 成 rw 也只能写到拷贝）；顶层目录逐个 bind 后
    remount 只读（根 / 是 overlayfs 挂载点，子用户命名空间里无法直接
    bind/remount）；/etc 盖 tmpfs 后按白名单拷回必需文件并整体只读；
    /home 盖 tmpfs 后只剩重建的真实路径（工程拷贝）；/dev 盖 tmpfs 后
    只 bind 进必需的设备文件（只读）；/sys 盖空 tmpfs；临时复制目录
    bind 到 /mnt 后在其中执行；/tmp 与 /var/tmp 盖 tmpfs；盖掉 /proc
    （空 tmpfs；本环境禁止挂载新的 proc，故直接隐藏）；断网；环境变量
    清密钥；资源上限。命令死于信号时管理器以同一信号自杀，调用方看到
    真正的负 returncode（不再是 --fork 下的退出码 1）。
    返回 {"mode": "unshare-ns", "ok", "returncode", "stdout", "stderr"}，
    或沙箱不可用时 {"mode": "unsupported", "ok": False, "reason": ...}。
    调用方收到 unsupported 时必须判 fail，绝不能静默降级为无隔离执行。
    """
    # P6-fix：manager 脚本是拷贝的一部分；拷贝里没有它说明拷贝坏了，
    # fail-closed（返回 unsupported），绝不降级为直接跑真实目录。
    # （原来这里查 unshare 命令行是否存在；fix2-4 起 run_sandboxed
    # 不再调用 unshare，命名空间由 sandbox_manager.py 用 ctypes 建。）
    scrubbed_env = _scrub_env(env)
    if not Path("/mnt").is_dir():
        return {"mode": "unsupported", "ok": False, "reason": "/mnt not available"}
    # P6-fix：先把工程目录完整拷贝到一次性临时目录，沙箱内只操作这个拷贝。
    # 拷贝失败则 fail-closed（返回 unsupported），绝不降级为直接跑真实目录。
    # 同一临时目录下再建 stage 子目录，供 setup 脚本 staging（/etc 白名单
    # 文件、/dev 设备文件 bind），进程结束由 finally 一并删掉，不污染宿主。
    copy_root = Path(tempfile.mkdtemp(prefix="jarvis-sandbox-proj-"))
    try:
        proj_copy = copy_root / "proj"
        shutil.copytree(base_dir, proj_copy)
        stage_dir = copy_root / "stage"
        stage_dir.mkdir()
        if not (proj_copy / "sandbox_manager.py").is_file():
            raise FileNotFoundError("sandbox_manager.py missing from copy")
    except Exception:
        shutil.rmtree(copy_root, ignore_errors=True)
        return {"mode": "unsupported", "ok": False,
                "reason": "project copy failed"}
    cmd = [sys.executable, str(proj_copy / "sandbox_manager.py"),
           str(dest), str(proj_copy), str(base_dir), str(stage_dir), *argv]
    try:
        result = subprocess.run(
            cmd, cwd=base_dir, env=scrubbed_env,
            capture_output=True, text=True, timeout=timeout,
            preexec_fn=_set_resource_limits,
        )
    except FileNotFoundError:
        return {"mode": "unsupported", "ok": False, "reason": "python3 not found"}
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

"""P6-fix2-4：沙箱命名空间管理器——取代 `unshare` 命令行。

背景（实测推翻了 supervisor 方案）：fix2 第一版用命名空间内 init
（sandbox_supervisor.py）做信号中继：命令死于信号时 supervisor 以
同一信号自杀，期望外层看到负 returncode。但 pytest 实测：SIGTERM
自杀 → 外层 143（=128+15），SIGKILL 自杀 → 137（=128+9），SIGSEGV
→ 139（=128+11）——supervisor 三次都活了下来、走到了
`return 128 + sig`。原因：命名空间 init 是 SIGNAL_UNKILLABLE，从命名
空间内部发的信号（即使 SIGKILL，只要 handler 是 SIG_DFL）会被内核
忽略；只有来自祖先命名空间（或内核）的 SIGKILL/SIGSTOP 才能杀死它。
所以命名空间内的任何进程都无法以“死于信号”的方式把信号传出：PID 1
只能正常退出，信号死亡到它这里必然被转成退出码（128+sig）。supervisor
文件已删除。

本管理器走另一条路：它本身是宿主命名空间里的普通进程（不是 init），
由 run_sandboxed 用 subprocess.run 直接启动；它 fork 出子进程、子进程
用 ctypes 调 unshare() 建命名空间、再 fork 出命名空间 init（孙进程，
sh -c 跑 setup 脚本、再 exec 命令），管理器在宿主侧等子进程、子进程
等孙进程。孙进程死于信号时，子进程是普通进程、SIG_DFL 下以同一信号
自杀有效，管理器同样以同一信号自杀，于是 subprocess.run 看到的
returncode 是真正的负数——不是合成的，是进程树如实传出的。

id 映射的坑（实测）：子进程 unshare 后不能自己写 /proc/self/uid_map——
本环境是容器，进程的 root（uid 0）只是容器用户命名空间里的 root，
在父命名空间里对应的是一个普通 uid；自己写 "0 0 1" 时外层 uid 0 不是
自己的父命名空间 euid，单行自映射规则不通过，CAP_SETUID 检查也不通过，
报 EPERM（202）。正确做法是 `unshare -r` 的做法：父进程写子进程的
/proc/<pid>/uid_map（父进程在父命名空间里有 CAP_SETUID）。所以子进程
unshare 后要先经管道发 "ready"，父进程写完映射再发 "done"，子进程收到
才继续 fork 孙进程——经典的 userns 建交握手，顺序是：setgroups deny →
uid_map → gid_map。

等价于 `unshare -rnmp --fork`，但多了三点：
1. 挂载传播：unshare 命令行默认把 / 设成 private（MS_REC|MS_PRIVATE），
   否则沙箱内的挂载会回传到宿主；本管理器不在 ctypes 里做，改由 setup
   脚本第一步 `mount --make-rprivate /`（失败 exit 20），与其它挂载
   在同一个地方、走同一个退出码表。
2. 信号：如上，子进程与管理器逐级以同一信号自杀传出，resource_3（CPU
   上限致死）因此能断言 returncode < 0（原先 --fork 下只能断言耗时）。
3. 退出码表：管理器自身失败用 201–203（unshare 系统调用 / id 映射 /
   waitpid），与 setup 脚本的 21–47 区分；run_sandboxed 收到这些码
   一律报 mode=unsupported，绝不降级。

注意：管理器不 exec 命令、不进沙箱；它只负责建命名空间、等子进程、
传信号。资源上限（preexec_fn）由 run_sandboxed 设在管理器进程上，
随 fork/exec 继承到命名空间内全部子进程，与原来随 unshare 继承
一致。
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from typing import NoReturn

# 与 `unshare -rnmp` 的 flags 一致（clone(2) / sched.h）。
CLONE_NEWNS = 0x00020000    # 挂载命名空间
CLONE_NEWUSER = 0x10000000  # 用户命名空间（-r：当前用户映射为 root）
CLONE_NEWPID = 0x20000000   # PID 命名空间（fork 出的孙进程为 init）
CLONE_NEWNET = 0x40000000   # 网络命名空间（无接口，真断网）

# 管理器自身失败的退出码（与 setup 脚本的 21–47 区分）。
EXIT_UNSHARE_FAILED = 201
EXIT_IDMAP_FAILED = 202
EXIT_WAITPID_FAILED = 203


def _fail(code: int, msg: str) -> NoReturn:
    """管理器自身失败：写 stderr 后直接退出（不抛异常——管理器是
    subprocess.run 启动的子进程，异常回溯只会变成退出码 1，丢信息）。"""
    sys.stderr.write(f"sandbox_manager: {msg}\n")
    sys.stderr.flush()
    os._exit(code)


def _propagate_status(status: int, report_line: bool) -> NoReturn:
    """把一次 waitpid 的结果传给上层：正常退出则以同一退出码退出；
    死于信号则先（可选）写一行诊断，再以同一信号自杀——调用方是普通
    进程（不是命名空间 init），SIG_DFL 下自杀有效，于是上层看到真正
    的负 returncode。Python 对 SIGINT/SIGPIPE/SIGXFSZ 装了自己的
    handler，先复位为 SIG_DFL；SIGKILL/SIGSTOP 不可设 handler，
    signal.signal 会抛错，捕获后直接 kill（这两者默认处置本就是致死）。
    report_line 只在“子进程看孙进程”那一级为真（孙进程死于信号的
    诊断只写一次，避免逐级重复）。"""
    if os.WIFEXITED(status):
        sys.exit(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        if report_line:
            sys.stderr.write(
                f"sandbox_manager: child killed by signal {sig}\n")
            sys.stderr.flush()
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError, RuntimeError):
            pass
        try:
            os.kill(os.getpid(), sig)
        except OSError:
            pass
        sys.exit(128 + sig)  # 自杀未生效时的兜底（正常情况下到不了）
    sys.exit(128 + 99)  # 既非退出也非信号致死（不应到达）


def main() -> None:
    dest, copy, real, stage = sys.argv[1:5]
    argv = sys.argv[5:]
    if not argv:
        _fail(EXIT_UNSHARE_FAILED, "no command given")

    # setup 脚本在 fork 前 import（父子进程共享；失败则管理器直接
    # 退出，不进入命名空间流程）。
    from sandbox import _SANDBOX_SH

    # 握手管道：ready（子→父：已 unshare，可以写映射）、done（父→子：
    # 映射已写完，可以继续）。close-on-exec，exec 出的命令继承不到。
    ready_r, ready_w = os.pipe()
    done_r, done_w = os.pipe()
    for fd in (ready_r, ready_w, done_r, done_w):
        os.set_inheritable(fd, False)

    pid = os.fork()
    if pid == 0:
        # ---- 子进程：unshare → 等映射 → fork 孙进程（命名空间 init）
        #      → 等孙进程、传信号。
        os.close(ready_r)
        os.close(done_w)
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET
                        | CLONE_NEWPID) != 0:
            os.close(ready_w)
            os.close(done_r)
            _fail(EXIT_UNSHARE_FAILED,
                  f"unshare failed: {os.strerror(ctypes.get_errno())}")
        os.write(ready_w, b"R")
        os.close(ready_w)
        if os.read(done_r, 1) != b"D":
            os.close(done_r)
            _fail(EXIT_IDMAP_FAILED, "parent did not write id maps")
        os.close(done_r)
        # 映射已写完：当前进程在新用户命名空间里是 uid 0（有全部能力）。
        # fork 孙进程：它进入新 PID 命名空间、成为 init，exec setup 脚本。
        try:
            gpid = os.fork()
        except OSError as exc:
            _fail(EXIT_IDMAP_FAILED, f"fork failed: {exc}")
        if gpid == 0:
            os.execvp("sh", ["sh", "-c", _SANDBOX_SH, "sandbox",
                             dest, copy, real, stage] + argv)
            os._exit(127)  # execvp 失败（sh 不存在时才到这里）
        try:
            _, status = os.waitpid(gpid, 0)
        except OSError as exc:
            _fail(EXIT_WAITPID_FAILED, f"waitpid failed: {exc}")
        _propagate_status(status, report_line=True)

    # ---- 父进程（管理器）：等 ready → 写子进程的 id 映射 → 发 done
    #      → 等子进程、传信号（诊断行子进程已写过，这里不再重复）。
    os.close(ready_w)
    os.close(done_r)
    if os.read(ready_r, 1) != b"R":
        # 子进程在 ready 前就死了（unshare 失败）：收尸、传出它的退出码。
        os.close(ready_r)
        os.close(done_w)
        _, status = os.waitpid(pid, 0)
        _propagate_status(status, report_line=False)
    os.close(ready_r)
    # 父进程在父用户命名空间里有 CAP_SETUID/CAP_SETGID，可以写子进程
    # 的映射（`unshare -r` 的做法）；顺序：setgroups deny → uid_map →
    # gid_map。写失败时关掉 done 管道（子进程读到 EOF 即以 202 退出），
    # 再收尸、把 202 传出去，同时把失败原因写进 stderr。
    map_error = None
    for path, content in (
            (f"/proc/{pid}/setgroups", "deny"),
            (f"/proc/{pid}/uid_map", f"0 {os.geteuid()} 1"),
            (f"/proc/{pid}/gid_map", f"0 {os.getegid()} 1")):
        try:
            with open(path, "w") as f:
                f.write(content)
        except OSError as exc:
            map_error = f"id-map failed: {path} <- {content!r}: {exc}"
            break
    if map_error is not None:
        sys.stderr.write(f"sandbox_manager: {map_error}\n")
        sys.stderr.flush()
        os.close(done_w)
        _, status = os.waitpid(pid, 0)
        _propagate_status(status, report_line=False)
    os.write(done_w, b"D")
    os.close(done_w)
    try:
        _, status = os.waitpid(pid, 0)
    except OSError as exc:
        _fail(EXIT_WAITPID_FAILED, f"waitpid failed: {exc}")
    _propagate_status(status, report_line=False)


if __name__ == "__main__":
    main()

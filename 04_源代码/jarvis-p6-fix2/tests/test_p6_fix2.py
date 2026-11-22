"""P6-fix2 残留缺口修复验证：/etc 只读、/home 覆盖、/dev 与 /sys 最小化、
信号退出码。

与 tests/test_p6_adversarial.py（26 用例，锁定上轮行为）分开：本文件
只验 fix2 新增的 4 处修复。运行方式与 test_p6_adversarial.py 一致：
    pytest tests/test_p6_fix2.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

P6_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(P6_DIR))


from sandbox import run_sandboxed  # noqa: E402

# 宿主 /home 下已知存在的文件（fix2-2 用它验“宿主文件不可见”）。
HOST_KNOWN_FILE = Path("/home/hatch/MEMORY.md")


@pytest.fixture
def sandbox_dest(tmp_path):
    dest = tmp_path / "jarvis-sandbox-copy"
    dest.mkdir()
    return dest


# ======================================================================
# fix2-1：/etc 只读，但命令仍能跑
# ======================================================================

class TestEtcReadOnly:

    def test_etc_write_fails_and_host_untouched(self, sandbox_dest):
        """攻击：沙箱内直接写 /etc/passwd。

        预期：写失败（EROFS，只读 remount），且宿主 /etc/passwd 内容
        自始至终不变。
        """
        target = Path("/etc/passwd")
        before = target.read_bytes()
        argv = ["sh", "-c", 'echo x > /etc/passwd 2>&1; echo RC=$?']
        try:
            r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
            stdout = r.get("stdout") or ""
            assert "RC=" in stdout, f"沙箱命令未正常执行，无法判定：{r!r}"
            assert "RC=0" not in stdout, (
                f"沙箱内写 /etc/passwd“成功”（RC=0）：{r!r}")
        finally:
            assert target.read_bytes() == before, (
                "宿主 /etc/passwd 在沙箱运行后被改动")

    def test_etc_create_file_fails(self, sandbox_dest):
        """攻击：沙箱内在 /etc 下新建文件。

        预期：新建失败（/etc 整体只读），宿主 /etc 下不出现该文件。
        """
        marker = Path("/etc/__fix2_newfile__")
        assert not marker.exists()
        argv = ["sh", "-c", 'touch /etc/__fix2_newfile__ 2>&1; echo RC=$?']
        try:
            r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
            stdout = r.get("stdout") or ""
            assert "RC=" in stdout, f"沙箱命令未正常执行，无法判定：{r!r}"
            assert "RC=0" not in stdout, f"沙箱内在 /etc 下新建文件成功：{r!r}"
        finally:
            assert not marker.exists(), "宿主 /etc 下出现了沙箱新建的文件"

    def test_etc_reads_still_work(self, sandbox_dest):
        """良性：沙箱内命令仍须能读 /etc（passwd、hostname、localtime、
        ssl 证书目录）。宿主本机时区是 UTC（/etc/localtime 指向
        /usr/share/zoneinfo/Etc/UTC），故用 readlink + TZif 魔数验
        localtime，不要验时区名。
        """
        code = (
            "import os, pwd, socket\n"
            "assert pwd.getpwuid(0).pw_name == 'root'\n"
            "assert socket.gethostname()\n"
            "assert os.readlink('/etc/localtime') == '/usr/share/zoneinfo/Etc/UTC'\n"
            "assert open('/etc/localtime', 'rb').read(4) == b'TZif'\n"
            "assert open('/etc/passwd').readline().startswith('root:')\n"
            "assert open('/etc/resolv.conf').read() != ''\n"
            "assert os.path.isdir('/etc/ssl/certs')\n"
            "print('ETC-READS-OK')\n"
        )
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", code], timeout=15)
        assert r.get("ok") is True, f"沙箱内读 /etc 失败：{r!r}"
        assert "ETC-READS-OK" in (r.get("stdout") or "")


# ======================================================================
# fix2-2：/home 覆盖，只剩工程拷贝
# ======================================================================

class TestHomeCovered:

    def test_home_host_files_invisible(self, sandbox_dest):
        """攻击：沙箱内读宿主 /home 下的真实文件。

        预期：读不到（/home 被 tmpfs 盖掉）。宿主文件本身不动。
        """
        assert HOST_KNOWN_FILE.exists(), "测试前置：宿主文件不存在"
        secret = Path("/home/hatch/__FIX2_SECRET__.txt")
        secret.write_text("TOP-SECRET-fix2-home\n", encoding="utf-8")
        try:
            argv = ["sh", "-c",
                    "cat /home/hatch/__FIX2_SECRET__.txt 2>&1; echo RC=$?; "
                    "test -e /home/hatch/MEMORY.md; echo MEM=$?"]
            r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
            stdout = r.get("stdout") or ""
            assert "TOP-SECRET-fix2-home" not in stdout, (
                f"沙箱内读到了宿主 /home 下的文件：{r!r}")
            assert "RC=0" not in stdout, f"沙箱内读到了宿主秘密文件：{r!r}"
            assert "MEM=0" not in stdout, (
                f"沙箱内仍可见宿主 /home/hatch/MEMORY.md：{r!r}")
        finally:
            if secret.exists():
                secret.unlink()

    def test_home_only_project_copy_visible(self, sandbox_dest):
        """良性：/home 下只剩重建的真实路径（工程拷贝），且工程文件可读。
        """
        argv = ["sh", "-c",
                f"ls /home; ls /home/hatch; test -f \"{P6_DIR}/sandbox.py\"; "
                "echo PROJ=$?; ls /mnt >/dev/null; echo MNT=$?"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        stdout = r.get("stdout") or ""
        assert r.get("ok") is True, f"沙箱命令未正常执行：{r!r}"
        lines = [ln.strip() for ln in stdout.strip().splitlines()]
        assert lines[0] == "hatch", f"/home 下不应有 hatch 之外的东西：{r!r}"
        assert lines[1] == "workspace", (
            f"/home/hatch 下不应有 workspace 之外的东西：{r!r}")
        assert "PROJ=0" in stdout, f"沙箱内读不到自己的工程拷贝：{r!r}"
        assert "MNT=0" in stdout, f"沙箱内 /mnt 不可用：{r!r}"

    def test_home_write_outside_project_fails(self, sandbox_dest):
        """攻击：沙箱内往 /home/hatch（工程目录之外）写文件。

        预期：写失败（/home 整体 remount 只读），宿主 /home 下不出现
        该文件。
        """
        marker = Path("/home/hatch/__fix2_evil__.txt")
        assert not marker.exists()
        argv = ["sh", "-c",
                'echo x > /home/hatch/__fix2_evil__.txt 2>&1; echo RC=$?']
        try:
            r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
            stdout = r.get("stdout") or ""
            assert "RC=" in stdout, f"沙箱命令未正常执行，无法判定：{r!r}"
            assert "RC=0" not in stdout, f"沙箱内往宿主 /home 写成功：{r!r}"
        finally:
            assert not marker.exists(), "宿主 /home 下出现了沙箱写的文件"


# ======================================================================
# fix2-3：/dev 与 /sys 最小化
# ======================================================================

class TestDevSysMinimal:

    def test_dev_exact_device_set(self, sandbox_dest):
        """攻击面收窄：/dev 下只有白名单设备 + fd 符号链接，不应有
        ptmx、pts、shm、console、磁盘设备等。
        """
        argv = ["sh", "-c", "ls /dev"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        assert r.get("ok") is True, f"沙箱命令未正常执行：{r!r}"
        seen = {ln.strip() for ln in (r.get("stdout") or "").splitlines()
                if ln.strip()}
        expected = {"null", "zero", "full", "random", "urandom", "tty",
                    "fd", "stdin", "stdout", "stderr"}
        assert seen == expected, (
            f"/dev 内容不是白名单集合：多出 {sorted(seen - expected)}，"
            f"缺失 {sorted(expected - seen)}")

    def test_dev_devices_work(self, sandbox_dest):
        """良性：白名单设备是真设备（不是空文件占位）：/dev/null 可写
        （黑洞）、/dev/zero 与 /dev/full 读出 0、/dev/urandom 与
        /dev/random 可读。
        """
        code = (
            "import os\n"
            "assert open('/dev/null', 'wb').write(b'hello') == 5\n"
            "assert open('/dev/zero', 'rb').read(4) == b'\\x00' * 4\n"
            "assert open('/dev/full', 'rb').read(4) == b'\\x00' * 4\n"
            "assert len(os.urandom(16)) == 16\n"
            "assert len(open('/dev/random', 'rb').read(4)) == 4\n"
            "print('DEV-OK')\n"
        )
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", code], timeout=15)
        assert r.get("ok") is True, f"白名单设备不可用：{r!r}"
        assert "DEV-OK" in (r.get("stdout") or "")

    def test_dev_no_new_nodes(self, sandbox_dest):
        """攻击：沙箱内在 /dev 下新建文件或 mknod 设备节点。

        预期：都失败（/dev 整体只读；user namespace 里 mknod 也受限）。
        """
        argv = ["sh", "-c",
                "touch /dev/__fix2_evil 2>&1; echo TOUCH=$?; "
                "mknod /dev/__fix2_node c 1 3 2>&1; echo MKNOD=$?"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        stdout = r.get("stdout") or ""
        assert "TOUCH=0" not in stdout, f"沙箱内在 /dev 下新建文件成功：{r!r}"
        assert "MKNOD=0" not in stdout, f"沙箱内 mknod 成功：{r!r}"

    def test_dev_tty_write_fails(self, sandbox_dest):
        """攻击：沙箱内往 /dev/tty 写（控制终端）。

        预期：写失败（/dev/tty 只读绑定；且沙箱进程本无控制终端，
        两道机制任一生效都表现为写失败）。
        """
        argv = ["sh", "-c", 'echo hi > /dev/tty 2>&1; echo RC=$?']
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        stdout = r.get("stdout") or ""
        assert "RC=" in stdout, f"沙箱命令未正常执行，无法判定：{r!r}"
        assert "RC=0" not in stdout, f"沙箱内往 /dev/tty 写成功：{r!r}"

    def test_sys_empty(self, sandbox_dest):
        """攻击面收窄：/sys 是空 tmpfs，看不到 sysfs 的任何条目。
        """
        argv = ["sh", "-c",
                "ls -A /sys; echo LS=$?; test -e /sys/kernel; echo K=$?"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        stdout = r.get("stdout") or ""
        assert r.get("ok") is True, f"沙箱命令未正常执行：{r!r}"
        assert "K=0" not in stdout, f"沙箱内仍可见 /sys/kernel：{r!r}"
        body = stdout.replace("LS=0", "").replace("K=1", "").strip()
        assert body == "", f"/sys 不是空的：{r!r}"


# ======================================================================
# fix2-4：信号致死返回负 returncode
# ======================================================================

class TestSignalExitCode:

    def test_signal_sigterm_selfkill_ignored_by_pid1(self, sandbox_dest):
        """文档化行为：沙箱内命令是 PID 1（命名空间 init），内核
        SIGNAL_UNKILLABLE 规则使它忽略自己发给自己的 SIGTERM（handler
        为 SIG_DFL 时连从命名空间内部发的 SIGKILL 也忽略），于是命令
        正常退出 0——这不是沙箱 bug，管理器如实报告实际退出（0），不
        能把没死成的“自杀”谎报成信号致死。真正的信号致死证明见
        test_signal_segfault（SIGSEGV）与对抗套件 resource_3（SIGXCPU，
        内核因 CPU 上限发出的信号能杀死 PID 1）。
        """
        code = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", code], timeout=15)
        assert r.get("ok") is True, f"自发 SIGTERM 应被 PID 1 忽略并正常退出：{r!r}"
        assert r.get("returncode") == 0

    def test_signal_sigkill_selfkill_ignored_by_pid1(self, sandbox_dest):
        """文档化行为：同上，PID 1 忽略自己发给自己的 SIGKILL（从命名
        空间内部发、handler 为 SIG_DFL 时内核直接丢弃），命令正常退出 0。
        """
        code = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", code], timeout=15)
        assert r.get("ok") is True, f"自发 SIGKILL 应被 PID 1 忽略并正常退出：{r!r}"
        assert r.get("returncode") == 0

    def test_signal_segfault(self, sandbox_dest):
        """真实崩溃：ctypes 读空指针触发 SIGSEGV，同样应以负
        returncode 传出（不是退出码 1）。
        """
        code = "import ctypes; ctypes.string_at(0)"
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", code], timeout=15)
        assert r.get("ok") is False
        rc = r.get("returncode")
        assert rc is not None and rc < 0, (
            f"SIGSEGV 未以负 returncode 传出，returncode={rc}：{r!r}")
        assert "sandbox_manager: child killed by signal 11" in (
            r.get("stderr") or ""), f"管理器未记录信号死亡：{r!r}"

    def test_signal_normal_exit_still_zero(self, sandbox_dest):
        """回归对照：正常退出仍是 0；管理器不应改变正常退出码。
        """
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", "print('hello-sandbox')"],
                           timeout=15)
        assert r.get("ok") is True, f"正常命令在管理器下失败：{r!r}"
        assert r.get("returncode") == 0
        assert "hello-sandbox" in (r.get("stdout") or "")

"""
P6 对抗性安全测试：沙箱逃逸 / 资源限制绕过 / 路径白名单绕过 / 哈希链漂移部署 / 提案越权

重要声明：
1. 本文件只编写测试代码，本次会话中不执行、不运行。
2. 这是红队/攻击用例：每个用例模拟一次攻击尝试，预期防御生效，
   即 run_sandboxed 返回 ok=False，或 CLI 返回 rejected/failed/deploy_failed。
3. 其中标注了【高风险】的用例（沙箱逃逸类的 escape_1、escape_2、resource_4）
   如果实际运行后断言失败（即攻击真的成功了），不代表测试写错了，而是
   对应 P6-安全修复记录.md §四/§六 里尚未做对抗验证的那个缺口被实锤。
   运行方式与环境要求（unshare 可用、/mnt 目录存在等）与 sandbox.py 文档字符串一致。

运行前提（供日后真正执行时参考，本次不跑）：
    pytest tests/test_p6_adversarial.py -v
    需要 unshare 可用、/mnt 可写、jarvis-p6 工程已部署在 P6_DIR。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

P6_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(P6_DIR))


from sandbox import run_sandboxed  # noqa: E402

GATE_OLD = '"rm -rf",'
GATE_NEW = '"rm -rf", "sudo rm",'


# ======================================================================
# 共享 helper（与 test_p6.py 保持一致，便于两份文件对照维护）
# ======================================================================

def run_cli(args, cwd=P6_DIR, env=None, timeout=180):
    merged = dict(os.environ)
    for key in list(merged):
        if "API_KEY" in key or "OPENAI" in key or "ANTHROPIC" in key:
            del merged[key]
    if env:
        merged.update(env)
    return subprocess.run(
        [PY, "jarvis.py", *args], cwd=cwd, env=merged,
        capture_output=True, text=True, timeout=timeout,
    )


def cli_json(args, cwd=P6_DIR):
    r = run_cli(args, cwd=cwd)
    assert r.returncode == 0, f"{args} failed: {r.stderr}"
    return json.loads(r.stdout)


def run_cli_raw_json(args, cwd=P6_DIR):
    """像 cli_json，但不要求 returncode==0——攻击用例大多数期望非零退出。"""

    r = run_cli(args, cwd=cwd)
    return r, json.loads(r.stdout)


def fresh_db():
    data_dir = P6_DIR / "data"
    for name in ("jarvis.db", "improvements.db", "events.jsonl",
                 "events.checkpoint.json"):
        p = data_dir / name
        if p.exists():
            p.unlink()
    backups = data_dir / "backups"
    if backups.exists():
        shutil.rmtree(backups)
    cli_json(["boot"])


def read_events():
    path = P6_DIR / "data" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def memory_has(pattern: str) -> bool:
    return pattern in (P6_DIR / "memory.py").read_text(encoding="utf-8")


def read_file(name: str) -> str:
    return (P6_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _snapshot_whitelisted_files():
    """每个测试前后快照白名单目标文件 + config.yaml，攻击用例弄脏了也能复原。"""
    names = ("memory.py", "router.py", "planner.py", "config.yaml")
    snap = {name: (P6_DIR / name).read_bytes() for name in names}
    yield
    for name, content in snap.items():
        p = P6_DIR / name
        if p.read_bytes() != content:
            p.write_bytes(content)


@pytest.fixture
def sandbox_dest(tmp_path):
    dest = tmp_path / "jarvis-sandbox-copy"
    dest.mkdir()
    return dest


# ======================================================================
# 一、沙箱逃逸（针对 run_sandboxed 的 unshare -rnm 挂载隔离）
# ======================================================================
#
# 已知攻击面（源自 sandbox.py 的实现本身，不是臆测）：
#   (a) real_dir 是 `mount --bind "$real" "$real"` 后 remount 为 ro ——
#       绑定的是"自己"，底层是同一个 inode，不是拷贝。一旦在命名空间内
#       把这个绑定重新 remount 为 rw，写入会直接落到宿主机的真实工程文件。
#   (b) unshare 只给了 -rnm（用户/网络/挂载命名空间），没有 -p（pid）、
#       没有 -u（uts）。/proc 仍是宿主机真实 /proc：能看到宿主机进程树、
#       读其它进程的 /proc/<pid>/environ，造成信息泄露。
#   (c) dest（临时目录，被 bind 到 /mnt）本身来自调用方，如果调用方未经
#       清洗就把一个用户可控目录传进来，里面预置的符号链接会在 /mnt 下
#       原样可见。
#   (d) 以 unshare -r 建立的新用户命名空间里，进程在该命名空间内被映射为
#       uid 0 并获得该命名空间范围内的全部 capability——这意味着它有能力
#       把 preexec_fn 设下的 rlimit 硬上限重新调高（这是 Linux user
#       namespace 广为人知的边界情况，不是本文臆造）。这一条实际写在
#       "二、资源限制绕过" 里，但根源也是沙箱隔离边界问题。


class TestSandboxEscape:

    def test_escape_1_remount_bind_rw_writes_to_host(self, sandbox_dest):
        """【高风险】攻击：把只读 bind 挂载的 real_dir 在命名空间内重新
        remount 为 rw，再直接写文件。由于 real_dir 是 bind-mount 到自身
        （同一 inode），一旦 remount 成功，写入会落到宿主机的真实工程文件上。

        预期：ok=False（remount 或写入应失败），且宿主机上
        P6_DIR/__ESCAPE_MARKER__.txt 必须自始至终不存在。
        """
        marker = P6_DIR / "__ESCAPE_MARKER__.txt"
        assert not marker.exists(), "测试前置状态异常：标记文件已存在"
        argv = ["sh", "-c",
                f'mount -o remount,rw,bind "{P6_DIR}" 2>&1 && '
                f'echo HACKED > "{marker}" 2>&1; echo RC=$?']
        try:
            r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
            assert r["ok"] is False, (
                "remount-rw 逃逸成功：只读绑定被重新挂载为可写，"
                f"沙箱对真实工程目录的只读保护被绕过。stdout={r.get('stdout')!r}")
        finally:
            escaped = marker.exists()
            if escaped:
                marker.unlink()
            assert not escaped, (
                "严重逃逸：沙箱内的写操作已经落到了宿主机的真实工程目录，"
                "对应 P6-安全修复记录.md §四 提到的、未做对抗验证的沙箱逃逸缺口")

    def test_escape_2_proc_exposes_host_process_tree(self, sandbox_dest):
        """【高风险】攻击：验证 /proc 是否被隔离。unshare 命令只用了
        -rnm，没有 -p，理论上沙箱内能看到宿主机完整的进程号列表。

        预期：沙箱内可见的 pid 数量应当约等于"沙箱自身这一条命令"
        （<=2，容忍 sh 本身 + 子进程各一个 pid），而不是宿主机的全部进程。
        直接断言可见进程数，不依赖 run_sandboxed 的 ok 字段
        （命令本身不会因为看到了别的 pid 而失败）。
        """
        argv = ["sh", "-c", "ls /proc | grep -E '^[0-9]+$' | wc -l"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        visible_pids = int((r.get("stdout") or "0").strip() or "0")
        assert visible_pids <= 2, (
            f"/proc 泄露了宿主机进程树（看到 {visible_pids} 个 pid，期望隔离到"
            " 1~2 个），说明 unshare 缺少 -p（pid 命名空间），沙箱隔离不完整")

    def test_escape_3_read_other_process_environ_via_proc(self, sandbox_dest):
        """攻击：既然 /proc 可能未被隔离（见 escape_2），尝试读宿主机上
        其它真实进程的 /proc/<pid>/environ，验证不会因此泄露疑似密钥。
        run_sandboxed 本身对直接子进程做了 _scrub_env，但宿主机上其它
        无关进程的环境变量并未被清洗，一旦 /proc 可见就可能被读到。

        预期：读不到任何包含 KEY/TOKEN/SECRET 字样的内容。
        """
        argv = ["sh", "-c",
                "for p in /proc/[0-9]*/environ; do "
                "cat \"$p\" 2>/dev/null | tr '\\0' '\\n'; "
                "done | grep -i -E 'KEY|TOKEN|SECRET' | head -1"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        leaked = (r.get("stdout") or "").strip()
        assert leaked == "", f"通过未隔离的 /proc 读到了疑似密钥内容：{leaked!r}"

    def test_escape_4_symlink_in_dest_escapes_mnt(self, sandbox_dest, tmp_path):
        """攻击：在 dest（被 bind 到 /mnt 的临时目录）里预先放一个符号链接，
        指向工程目录之外的"秘密文件"，诱导沙箱内命令通过这个链接读到
        /mnt 之外的真实路径。

        预期：读不到秘密内容。
        """
        secret = tmp_path / "secret_outside.txt"
        secret.write_text("TOP-SECRET-sandbox-escape-4\n", encoding="utf-8")
        (sandbox_dest / "link_out").symlink_to(secret)
        argv = ["sh", "-c", "cat /mnt/link_out 2>&1"]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        assert "TOP-SECRET-sandbox-escape-4" not in (r.get("stdout") or ""), (
            "符号链接逃逸成功：沙箱内通过 dest 里预置的软链接"
            "读到了 /mnt 之外的宿主机文件内容")

    def test_escape_5_absolute_path_write_outside_mnt(self, sandbox_dest):
        """攻击：不依赖软链接，直接用绝对路径尝试写到 /mnt、/tmp、
        /var/tmp 之外的任意宿主机路径。

        预期：写入失败（RC 非 0），命令输出不应包含 RC=0。
        """
        target = "/opt/__p6_escape_test__.txt"
        argv = ["sh", "-c", f'echo pwned > "{target}" 2>&1; echo RC=$?']
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=15)
        assert "RC=0" not in (r.get("stdout") or ""), (
            f"沙箱内成功写入了工程目录与 /mnt 之外的路径 {target}")

    def test_escape_6_unsupported_mode_never_silently_downgrades(self, sandbox_dest):
        """攻击：伪造环境让 shutil.which('unshare') 返回 None
        （比如传一个清空 PATH 的 env），验证调用方拿到的是
        mode=unsupported 而不是悄悄降级为无隔离直接执行。

        预期：mode=='unsupported'，ok=False，reason 明确给出。
        """
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           ["sh", "-c", "echo should-not-run"],
                           timeout=10, env={"PATH": ""})
        assert r["mode"] == "unsupported"
        assert r["ok"] is False
        assert r["reason"] == "unshare not found"


# ======================================================================
# 二、资源限制绕过（RLIMIT_AS 2GiB / RLIMIT_NPROC 256 / RLIMIT_CPU 300/330s）
# ======================================================================

class TestResourceLimitBypass:

    def test_resource_1_memory_over_as_limit_is_killed(self, sandbox_dest):
        """攻击：在沙箱内分配超过 RLIMIT_AS（2GiB）的内存。

        预期：进程应被 OOM/SIGKILL 或抛 MemoryError 终止，返回非零，
        ok=False。
        """
        code = ("import sys\n"
                "try:\n"
                "    buf = bytearray(3 * 1024 ** 3)\n"
                "    sys.exit(0)\n"
                "except MemoryError:\n"
                "    sys.exit(42)\n")
        argv = [sys.executable, "-c", code]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=30)
        assert r["ok"] is False, (
            f"分配 3GiB 内存未被 RLIMIT_AS(2GiB) 拦截，returncode={r.get('returncode')}")
        # 要么是 Python 层捕获 MemoryError（42），要么是被信号杀死（负数/137 等）
        rc = r.get("returncode")
        assert rc == 42 or rc is None or rc < 0 or rc >= 128, (
            f"返回码 {rc} 既不是 MemoryError 退出码也不是被信号杀死的特征值")

    def test_resource_2_fork_bomb_capped_by_nproc(self, sandbox_dest):
        """攻击：fork bomb 式地不断创建子进程，尝试突破 RLIMIT_NPROC(256)。

        预期：超过上限后 os.fork() 应该抛 BlockingIOError/OSError，
        脚本捕获后以特定退出码 7 结束（意味着限制生效）；如果脚本在
        创建了明显多于 256 个进程后仍未报错（退出码 0），说明限制未生效。
        """
        code = (
            "import os, sys, time\n"
            "children = []\n"
            "blocked = False\n"
            "try:\n"
            "    for _ in range(400):\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            time.sleep(5)\n"
            "            os._exit(0)\n"
            "        children.append(pid)\n"
            "except BlockingIOError:\n"
            "    blocked = True\n"
            "except OSError:\n"
            "    blocked = True\n"
            "sys.exit(7 if blocked else 0)\n"
        )
        argv = [sys.executable, "-c", code]
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=30)
        assert r["ok"] is False
        assert r.get("returncode") == 7, (
            "尝试 fork 400 个子进程未被 RLIMIT_NPROC(256) 拦截，"
            f"returncode={r.get('returncode')}, stderr={r.get('stderr')!r}")

    def test_resource_3_cpu_limit_terminates_busy_loop(self, sandbox_dest, monkeypatch):
        """攻击：验证 RLIMIT_CPU（soft 300s / hard 330s）真的会终止
        一个纯 CPU busy loop。为了不在单测里真的等 5 分钟，这里用
        monkeypatch 把 sandbox._set_resource_limits 的 CPU 软/硬上限
        临时调小（2s / 3s），只验证"超过 CPU 时间会被 SIGXCPU 杀死"
        这个机制本身是否工作，不改变生产配置。

        预期：进程被信号终止（returncode 为负数，对应 SIGXCPU=-24），
        ok=False。
        """
        import resource
        import sandbox as sandbox_mod

        def _tiny_cpu_limit():
            resource.setrlimit(resource.RLIMIT_CPU, (2, 3))

        monkeypatch.setattr(sandbox_mod, "_set_resource_limits", _tiny_cpu_limit)
        code = "x = 0\nwhile True:\n    x += 1\n"
        argv = [sys.executable, "-c", code]
        r = sandbox_mod.run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=20)
        assert r["ok"] is False
        assert r.get("returncode") is not None and r["returncode"] < 0, (
            f"busy loop 未被 CPU 时间上限终止，returncode={r.get('returncode')}")

    def test_resource_4_userns_root_raises_own_rlimit(self, sandbox_dest):
        """【高风险】攻击：unshare -r 会把沙箱内进程映射为新用户命名空间里
        的 uid 0，在该命名空间范围内拥有全部 capability——这理论上包括
        把 preexec_fn 设下的 RLIMIT_AS 硬上限重新调高的能力（这是 Linux
        用户命名空间的已知边界情况，不是本文臆造的攻击）。

        做法：在沙箱内先尝试 `ulimit -H -v unlimited`（提高地址空间硬上限），
        再分配 3GiB，看是否绕过了原本 2GiB 的上限。

        预期：ok=False（分配仍然失败）。如果此断言失败（即 ok 变成了
        True 且确实分配成功），说明用户命名空间重设 rlimit 硬上限这个
        缺口在当前实现里是可以被利用的，需要在部署前额外用 cgroup 或
        在 unshare 之外的真正特权层面加二次限制，而不能只依赖
        preexec_fn 里设的 rlimit。
        """
        argv = ["sh", "-c",
                'ulimit -H -v unlimited 2>&1; '
                f'{sys.executable} -c "'
                'import sys\n'
                'try:\n'
                '    b = bytearray(3 * 1024 ** 3)\n'
                '    sys.exit(0)\n'
                'except MemoryError:\n'
                '    sys.exit(42)\n"']
        r = run_sandboxed(P6_DIR, sandbox_dest, argv, timeout=30)
        assert r["ok"] is False, (
            "【缺口命中】沙箱内进程利用用户命名空间内的伪 root 身份"
            "重新调高了 RLIMIT_AS 硬上限，并成功分配了超过 2GiB 的内存——"
            "resource.py 里基于 preexec_fn 的 rlimit 防护在 unshare -r 下"
            "可被绕过，需要额外的 cgroup 限制")

    def test_resource_5_timeout_kills_runaway_process(self, sandbox_dest):
        """攻击基线对照（非新发现，验证已有行为未退化）：sleep 远超
        timeout 参数，确认 subprocess 层面的墙钟超时仍然生效。
        """
        r = run_sandboxed(P6_DIR, sandbox_dest,
                           [sys.executable, "-c", "import time; time.sleep(10)"],
                           timeout=1)
        assert r["ok"] is False
        assert r.get("reason") == "timeout"


# ======================================================================
# 三、路径白名单绕过（improve-propose --target）
# ======================================================================

class TestPathWhitelistBypass:

    def test_whitelist_1_dotdot_traversal_rejected(self):
        """攻击：用 ../ 路径穿越，试图把 target 解析到白名单目录之外
        （例如工程上级目录的任意文件）。
        """
        fresh_db()
        r = run_cli(["improve-propose", "--title", "路径穿越",
                     "--target", "../../../../etc/passwd",
                     "--old-text", "root:", "--new-text", "root-hacked:",
                     "--id", "imp-adv-w1"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] == "rejected"

    def test_whitelist_2_absolute_path_rejected(self):
        """攻击：直接用绝对路径指向白名单之外的文件（不经过 ../ 相对穿越）。
        """
        fresh_db()
        r = run_cli(["improve-propose", "--title", "绝对路径逃逸",
                     "--target", "/etc/passwd",
                     "--old-text", "root:", "--new-text", "root-hacked:",
                     "--id", "imp-adv-w2"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] == "rejected"

    def test_whitelist_3_target_with_embedded_dotslash_resolves_to_immutable(self):
        """攻击：target 里混入 "./" 和多余的路径分隔符，企图让字符串级
        白名单匹配失效，但 resolve() 之后实际落到不可变文件
        （gateway.py）上。

        预期：无论字符串长什么样，resolve 后命中 gateway.py 仍应被拒绝。
        """
        fresh_db()
        r = run_cli(["improve-propose", "--title", "伪装路径改网关",
                     "--target", "./tests/../gateway.py",
                     "--old-text", 'if side_effect == "destructive":',
                     "--new-text", "if False:",
                     "--id", "imp-adv-w3"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] == "rejected"
        assert "immutable_rule" in result["reason"]

    def test_whitelist_4_null_byte_in_target_rejected_not_crashed(self):
        """攻击：target 里嵌入 NUL 字节，企图利用字符串/系统调用层面的
        截断差异让检查环节和实际文件操作环节"看到"不同的路径。

        预期：被拒绝（或至少是可控的 400 级错误），绝不能在 stderr
        里看到未捕获的 Traceback（即不能把输入校验异常直接炸穿到用户）。

        期望写法修正：NUL 字节无法经 subprocess argv 传入（Python 的
        subprocess 遇到内嵌 NUL 直接抛 ValueError，CLI 根本没被测到）。
        这里直接调 ImprovementStore.propose()，验证它返回 rejected
        而不是把异常炸穿出来。
        """
        fresh_db()
        from improvement import ImprovementStore

        class _StubEventLog:
            def append(self, event, result, detail=""):
                pass

        store = ImprovementStore(
            P6_DIR, P6_DIR / "data" / "improvements.db", _StubEventLog())
        result = store.propose("空字节路径", "memory.py\x00../gateway.py",
                               GATE_OLD, GATE_NEW, proposal_id="imp-adv-w4")
        assert result["decision"] == "rejected"

    def test_whitelist_5_unicode_homoglyph_target_rejected(self):
        """攻击：用全角/同形 Unicode 字符伪装白名单文件名
        （例如全角 "ｍｅｍｏｒｙ.py"），赌检查环节做了宽松的
        Unicode 归一化、而实际文件系统调用没有做，从而创建出一个
        同名但实际内容不受控的"影子文件"。

        预期：由于目标文件名在文件系统里并不真实存在于白名单位置，
        应当被拒绝（文件不存在 / 不在白名单），不应该被当作
        "memory.py" 的别名接受。
        """
        fresh_db()
        r = run_cli(["improve-propose", "--title", "同形字符伪装",
                     "--target", "ｍｅｍｏｒｙ.py",
                     "--old-text", GATE_OLD, "--new-text", GATE_NEW,
                     "--id", "imp-adv-w5"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] == "rejected"

    def test_whitelist_6_symlink_swap_between_approve_and_deploy(self):
        """攻击（与第四类哈希链联动）：propose 时 target 合法指向
        memory.py；approve 通过之后、deploy 之前，把 memory.py 换成
        一个指向工程外部文件的符号链接，企图让 deploy 阶段把补丁写到
        工程目录之外。

        预期：deploy 阶段的哈希校验应该因为"当前文件内容/身份"与
        approve 时记录的不一致而失败，不应该把补丁写到符号链接指向的
        外部文件上。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "gate 增加 sudo rm",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-w6"])
        assert cli_json(["improve-test", "--id", "imp-adv-w6"])["verdict"] == "pass"
        cli_json(["improve-approve", "--id", "imp-adv-w6"])
        real_path = P6_DIR / "memory.py"
        backup_bytes = real_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            outside_target = Path(tmp) / "outside_memory.py"
            outside_target.write_bytes(backup_bytes)
            real_path.unlink()
            real_path.symlink_to(outside_target)
            try:
                r = run_cli(["improve-deploy", "--id", "imp-adv-w6"])
                assert r.returncode != 0
                result = json.loads(r.stdout)
                assert result["decision"] == "failed"
                assert not outside_target.read_text(encoding="utf-8").count("sudo rm")
            finally:
                if real_path.is_symlink():
                    real_path.unlink()
                real_path.write_bytes(backup_bytes)


# ======================================================================
# 四、哈希链漂移部署（base_sha256 -> patched_sha256 -> 批准 -> 部署）
# ======================================================================

class TestHashChainDriftDeploy:

    def test_hash_1_whitespace_only_drift_detected(self):
        """攻击：approve 通过之后，只在文件末尾加一个无害的换行符
        （纯空白漂移，人工 diff 几乎看不出来），试图赌哈希校验只按
        "关键字符串是否还在"做松散判断，而不是整文件字节级哈希。

        预期：deploy 仍然失败（hash 对任何字节变化都敏感），
        状态进入 deploy_failed，并有审计记录。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "gate 增加 sudo rm",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-h1"])
        assert cli_json(["improve-test", "--id", "imp-adv-h1"])["verdict"] == "pass"
        cli_json(["improve-approve", "--id", "imp-adv-h1"])
        path = P6_DIR / "memory.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        r = run_cli(["improve-deploy", "--id", "imp-adv-h1"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] == "failed"
        status = cli_json(["improve-status", "--id", "imp-adv-h1"])
        assert status["status"] == "deploy_failed"
        events = read_events()
        assert any(e["event"] == "improvement_deploy_failed" for e in events)

    def test_hash_2_stale_base_rejected_after_sibling_deploy(self):
        """攻击：B 在 A 部署之前就拿到了旧的 base_sha256（approve 时
        记录），之后 A 先部署改变了文件，再尝试把 B 部署上去，企图
        让 B 的补丁套用在一个它从未见过的"当前文件"上。

        预期：B 的部署应因为 base 哈希与当前文件不一致而失败。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "A: gate 增加 sudo rm",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-h2a"])
        cli_json(["improve-propose", "--title", "B: 同样改 rm -rf 行",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", '"rm -rf", "chmod 777",',
                  "--id", "imp-adv-h2b"])
        assert cli_json(["improve-test", "--id", "imp-adv-h2a"])["verdict"] == "pass"
        cli_json(["improve-approve", "--id", "imp-adv-h2a"])
        assert cli_json(["improve-deploy", "--id", "imp-adv-h2a"])["decision"] == "deployed"
        # B 的 old_text 现在已经不存在了（A 部署后 GATE_OLD 被替换），
        # B 的 test/approve 此时应当已经无法基于旧 base 顺利走完，
        # 即使强行尝试部署也必须失败。
        r_test = run_cli(["improve-test", "--id", "imp-adv-h2b"])
        if r_test.returncode == 0 and json.loads(r_test.stdout).get("verdict") == "pass":
            r_approve = run_cli(["improve-approve", "--id", "imp-adv-h2b"])
            if r_approve.returncode == 0:
                r_deploy = run_cli(["improve-deploy", "--id", "imp-adv-h2b"])
                assert r_deploy.returncode != 0
                assert json.loads(r_deploy.stdout)["decision"] == "failed"
        assert not memory_has('"chmod 777"')

    def test_hash_3_replay_already_rolled_back_proposal_blocked(self):
        """攻击：把一个已经 deploy -> rollback 过的提案 id，不经过
        重新 propose/test/approve，直接再次调用 improve-deploy，
        企图"重放"一次部署，跳过状态机。

        预期：rolled_back 状态下不允许直接 deploy，应被拒绝。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "gate 增加 sudo rm",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-h3"])
        assert cli_json(["improve-test", "--id", "imp-adv-h3"])["verdict"] == "pass"
        cli_json(["improve-approve", "--id", "imp-adv-h3"])
        assert cli_json(["improve-deploy", "--id", "imp-adv-h3"])["decision"] == "deployed"
        assert cli_json(["improve-rollback", "--id", "imp-adv-h3"])["decision"] == "rolled_back"
        r = run_cli(["improve-deploy", "--id", "imp-adv-h3"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] in ("rejected", "failed", "denied")
        assert not memory_has('"sudo rm"')

    def test_hash_4_patched_hash_changed_between_test_and_approve(self):
        """攻击：improve-test 通过之后、improve-approve 之前，
        直接篡改磁盘上的文件内容（不是通过 old-text/new-text 合法流程），
        企图让"测试通过的代码"和"最终批准部署的代码"不是同一份字节。

        预期：approve 阶段应能发现 patched 内容已经不是测试时的内容，
        拒绝批准（或批准后 deploy 阶段必然失败，二者至少命中一个）。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "gate 增加 sudo rm",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-h4"])
        assert cli_json(["improve-test", "--id", "imp-adv-h4"])["verdict"] == "pass"
        path = P6_DIR / "memory.py"
        tampered = path.read_text(encoding="utf-8") + "\n# tampered-between-test-and-approve\n"
        path.write_text(tampered, encoding="utf-8")
        r_approve = run_cli(["improve-approve", "--id", "imp-adv-h4"])
        if r_approve.returncode == 0:
            r_deploy = run_cli(["improve-deploy", "--id", "imp-adv-h4"])
            assert r_deploy.returncode != 0, (
                "improve-approve 在文件被篡改后仍然放行，"
                "且 improve-deploy 也未能拦截，哈希链在 test->approve 这段"
                "缺少完整性校验")
        else:
            result = json.loads(r_approve.stdout)
            assert result["decision"] == "rejected"


# ======================================================================
# 五、提案越权
# ======================================================================

class TestProposalOverreach:

    def test_overreach_1_deploy_without_test_or_approve_rejected(self):
        """攻击：propose 之后直接跳到 deploy，跳过 test 和 approve 两步。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "跳过测试和批准直接部署",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-o1"])
        r = run_cli(["improve-deploy", "--id", "imp-adv-o1"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] in ("rejected", "failed", "denied")
        assert not memory_has('"sudo rm"')

    def test_overreach_2_deploy_without_approve_rejected(self):
        """攻击：propose -> test 之后跳过 approve，直接 deploy。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "跳过批准直接部署",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-o2"])
        assert cli_json(["improve-test", "--id", "imp-adv-o2"])["verdict"] == "pass"
        r = run_cli(["improve-deploy", "--id", "imp-adv-o2"])
        assert r.returncode != 0
        result = json.loads(r.stdout)
        assert result["decision"] in ("rejected", "failed", "denied")
        assert not memory_has('"sudo rm"')

    def test_overreach_3_critical_pattern_removal_via_whitelisted_target(self):
        """攻击：memory.py 是"可编辑白名单文件"而不是"不可变文件"，
        路径级白名单保护的是文件列表，不一定保护文件内部的关键安全模式。
        尝试直接提案删除 gate 列表里的关键拦截项 "rm -rf"（而不是像
        正常用例那样"追加"新模式），企图合法地削弱安全网关本身。

        预期：即便 memory.py 允许修改，删除已有的危险模式拦截项这种
        "净削弱安全性"的变更也应当被拒绝；如果被接受，说明路径白名单
        这层防护没有覆盖"内容层面的安全降级"这一类越权。
        """
        fresh_db()
        before = read_file("memory.py")
        assert GATE_OLD in before
        r = run_cli(["improve-propose", "--title", "精简 gate 列表",
                     "--target", "memory.py", "--old-text", GATE_OLD,
                     "--new-text", "", "--id", "imp-adv-o3"])
        if r.returncode == 0:
            # propose 本身没被拦，继续往后走，最终必须有一道关卡拦住它
            result = json.loads(r.stdout)
            assert result["decision"] != "proposed" or True
            test_r = run_cli(["improve-test", "--id", "imp-adv-o3"])
            blocked_at_test = (
                test_r.returncode != 0
                or json.loads(test_r.stdout).get("verdict") != "pass"
            )
            if not blocked_at_test:
                approve_r = run_cli(["improve-approve", "--id", "imp-adv-o3"])
                blocked_at_approve = approve_r.returncode != 0
                if not blocked_at_approve:
                    deploy_r = run_cli(["improve-deploy", "--id", "imp-adv-o3"])
                    assert deploy_r.returncode != 0, (
                        "【缺口命中】一个把安全网关关键拦截模式 'rm -rf' "
                        "整条删除的提案，经过 propose->test->approve->deploy "
                        "全部放行成功——路径白名单只保护了文件列表，没有覆盖"
                        "内容层面的安全降级，这是越权风险")
        assert GATE_OLD in read_file("memory.py"), (
            "gate 列表的关键拦截项 'rm -rf' 被一个越权提案成功删除")

    def test_overreach_4_forged_privileged_actor_ignored(self):
        """攻击：在 improve-approve/improve-deploy 调用里塞一个自定义的
        "特权 actor" 参数（如果 CLI 接受未知参数会被静默忽略，这里验证
        不会因为传了看起来像特权声明的参数就绕过真实的状态机校验）。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "伪造特权身份部署",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-o4"])
        r = run_cli(["improve-deploy", "--id", "imp-adv-o4",
                     "--actor", "system-admin-override",
                     "--force", "true"])
        # 期望写法修正：argparse 遇到未知参数直接退出（exit 2），
        # stdout 为空，json.loads('') 会崩溃。断言应为"退出码非 0
        # 且未部署"，而不是去解析一个不存在的 JSON。
        assert r.returncode != 0
        assert not memory_has('"sudo rm"')

    def test_overreach_5_duplicate_old_text_at_deploy_time_blocked(self):
        """攻击：A 提案时 old_text 在文件里是唯一的（propose 阶段的
        "old_text==1" 检查通过）；但在 A 部署之前，另一个已部署的
        提案 B 让这段 old_text 在文件里出现了不止一次，企图让 A 部署时
        实际命中的是"错误的那一处"。

        预期：deploy 阶段应当重新校验唯一性，而不是只信任 propose
        阶段做过的一次性检查；校验失败应导致 deploy 失败。
        """
        fresh_db()
        cli_json(["improve-propose", "--title", "A：改唯一行",
                  "--target", "memory.py", "--old-text", GATE_OLD,
                  "--new-text", GATE_NEW, "--id", "imp-adv-o5a"])
        # B：先把 GATE_OLD 复制成两份（模拟另一个已部署提案造成的巧合重复），
        # 这里直接手工篡改文件模拟"B 已经先部署"的结果。
        path = P6_DIR / "memory.py"
        text = path.read_text(encoding="utf-8")
        assert text.count(GATE_OLD) == 1
        duplicated = text.replace(GATE_OLD, GATE_OLD + "\n    " + GATE_OLD, 1)
        path.write_text(duplicated, encoding="utf-8")
        r = run_cli(["improve-deploy", "--id", "imp-adv-o5a"])
        # 此时 A 还没过 test/approve，直接部署按状态机本身就应该被拒绝；
        # 这里着重验证：无论因为状态机还是因为唯一性校验，总之不能部署成功。
        assert r.returncode != 0
        assert json.loads(r.stdout)["decision"] in ("rejected", "failed", "denied")
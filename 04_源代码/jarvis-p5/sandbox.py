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
import shutil
import subprocess
import sys
from pathlib import Path


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

#!/usr/bin/env python3
"""Bubblewrap sandbox launcher for TheRock agent sessions.

Creates a contained environment where claude code can run with
--dangerously-skip-permissions safely. Linux only (requires bwrap).

Usage:
    sandbox.py [command...]
    sandbox.py                        # interactive bash
    sandbox.py claude --resume        # resume a claude session

Environment variables:
    THEROCK_WORKSPACE   Override workspace root
    THEROCK_ALLOW_NET   Set to 0 to block network (default: 1)
    THEROCK_GPU         Set to 0 to disable GPU passthrough (default: 1)
    THEROCK_EXTRA_RO    Colon-separated extra read-only bind mounts
    THEROCK_EXTRA_RW    Colon-separated extra read-write bind mounts
"""

import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("THEROCK_WORKSPACE", str(SCRIPT_DIR.parent)))
HOME_DIR = Path.home()


def find_nvm_node_bin() -> str | None:
    nvm_versions = HOME_DIR / ".nvm" / "versions" / "node"
    if not nvm_versions.is_dir():
        return None
    versions = sorted(nvm_versions.iterdir(), key=lambda p: p.name)
    if not versions:
        return None
    return str(versions[-1] / "bin")


def build_bwrap_args() -> list[str]:
    allow_net = os.environ.get("THEROCK_ALLOW_NET", "1")
    gpu = os.environ.get("THEROCK_GPU", "1")

    args: list[str] = ["--die-with-parent"]

    # Read-only system.
    for d in ["/usr", "/lib", "/bin", "/sbin", "/etc"]:
        if Path(d).exists():
            args.extend(["--ro-bind", d, d])
    if Path("/lib64").is_dir():
        args.extend(["--ro-bind", "/lib64", "/lib64"])

    # Read-only shared projects.
    args.extend(["--ro-bind", "/srv/vm-shared", "/srv/vm-shared"])

    # DNS (systemd-resolved).
    resolve_dir = Path("/run/systemd/resolve")
    if resolve_dir.is_dir():
        args.extend(["--ro-bind", str(resolve_dir), str(resolve_dir)])

    # Proc and dev.
    args.extend(["--proc", "/proc", "--dev", "/dev"])
    args.extend(["--dev-bind", "/dev/pts", "/dev/pts"])
    args.extend(["--dev-bind", "/dev/ptmx", "/dev/ptmx"])

    # Tmp: workspace-local.
    tmp_dir = WORKSPACE / ".tmp"
    if tmp_dir.is_dir():
        args.extend(["--bind", str(tmp_dir), "/tmp"])
    else:
        args.extend(["--tmpfs", "/tmp"])

    # The workspace: full read-write.
    args.extend(["--bind", str(WORKSPACE), str(WORKSPACE)])

    # Home directory: minimal tmpfs + selective binds.
    args.extend(["--tmpfs", str(HOME_DIR)])

    # Claude Code config and cache (persisted).
    for name in [".claude", ".cache"]:
        p = HOME_DIR / name
        if p.is_dir():
            args.extend(["--bind", str(p), str(p)])
    claude_json = HOME_DIR / ".claude.json"
    if claude_json.is_file():
        args.extend(["--bind", str(claude_json), str(claude_json)])

    # Local binaries (claude CLI, pip tools).
    local_dir = HOME_DIR / ".local"
    if local_dir.is_dir():
        args.extend(["--ro-bind", str(local_dir), str(local_dir)])

    # Node.js / nvm.
    for name in [".nvm", ".npm"]:
        p = HOME_DIR / name
        if p.is_dir():
            rw = name == ".npm"
            args.extend(["--bind" if rw else "--ro-bind", str(p), str(p)])

    # Git config (read-only).
    gitconfig = HOME_DIR / ".gitconfig"
    if gitconfig.is_file():
        args.extend(["--ro-bind", str(gitconfig), str(gitconfig)])

    # Block credentials.
    for cred_dir in [".ssh", ".gnupg", ".aws"]:
        args.extend(["--tmpfs", str(HOME_DIR / cred_dir)])

    # Working directory.
    args.extend(["--chdir", str(WORKSPACE)])

    # Clean environment.
    args.append("--clearenv")
    env = {
        "HOME": str(HOME_DIR),
        "USER": os.environ.get("USER", "stella"),
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "SHELL": "/bin/bash",
        "COLUMNS": os.environ.get("COLUMNS", "120"),
        "LINES": os.environ.get("LINES", "40"),
        "XDG_CACHE_HOME": str(HOME_DIR / ".cache"),
        "XDG_CONFIG_HOME": str(HOME_DIR / ".config"),
        "VIRTUAL_ENV": f"{WORKSPACE}/.venv",
        "TMPDIR": f"{WORKSPACE}/.tmp",
        "TEMP": f"{WORKSPACE}/.tmp",
        "TMP": f"{WORKSPACE}/.tmp",
        "THEROCK_WORKSPACE": str(WORKSPACE),
        "THEROCK_CACHE_DIR": f"{WORKSPACE}/cache",
        "THEROCK_SANDBOX": "1",
    }
    for k, v in env.items():
        args.extend(["--setenv", k, v])

    # PATH.
    nvm_node = find_nvm_node_bin()
    path_parts = [f"{WORKSPACE}/.venv/bin", f"{HOME_DIR}/.local/bin"]
    if nvm_node:
        path_parts.append(nvm_node)
    path_parts.extend(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    args.extend(["--setenv", "PATH", ":".join(path_parts)])

    # GPU passthrough.
    if gpu == "1":
        if Path("/dev/kfd").exists():
            args.extend(["--dev-bind", "/dev/kfd", "/dev/kfd"])
        if Path("/dev/dri").is_dir():
            args.extend(["--dev-bind", "/dev/dri", "/dev/dri"])
        args.extend(["--ro-bind", "/sys", "/sys"])
        rocm = Path("/opt/rocm")
        if rocm.is_dir():
            args.extend(["--ro-bind", str(rocm), str(rocm)])
            args.extend(["--setenv", "ROCM_PATH", str(rocm)])

    # Network.
    if allow_net == "0":
        args.append("--unshare-net")

    # Extra mounts.
    for envvar, flag in [("THEROCK_EXTRA_RO", "--ro-bind"), ("THEROCK_EXTRA_RW", "--bind")]:
        extra = os.environ.get(envvar, "")
        for mount in extra.split(":"):
            if mount:
                args.extend([flag, mount, mount])

    return args


def main() -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        print(
            "Error: bwrap (bubblewrap) not found. Install with:\n"
            "  sudo dnf install bubblewrap",
            file=sys.stderr,
        )
        sys.exit(1)

    bwrap_args = build_bwrap_args()

    if len(sys.argv) > 1:
        cmd = sys.argv[1:]
    else:
        cmd = ["bash", "--rcfile", str(SCRIPT_DIR / "sandbox-bashrc.sh")]
        print("Starting interactive shell...")

    full_cmd = [bwrap] + bwrap_args + ["--"] + cmd
    os.execvp(bwrap, full_cmd)


if __name__ == "__main__":
    main()

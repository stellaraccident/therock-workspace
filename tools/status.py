#!/usr/bin/env python3
"""Quick workspace status overview.

Shows git status of source checkouts, pending beads, cache size,
and build state.
"""

import os
import subprocess
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent


def run_capture(cmd: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def show_source_checkouts() -> None:
    sources_dir = WORKSPACE / "sources"
    if not sources_dir.exists():
        print("  (no sources/ directory)")
        return

    for entry in sorted(sources_dir.iterdir()):
        git_dir = entry / ".git"
        if not (entry.is_dir() and (git_dir.exists() or git_dir.is_file())):
            continue
        name = entry.name
        branch = run_capture(
            ["git", "branch", "--show-current"], cwd=entry
        ) or "detached"
        porcelain = run_capture(["git", "status", "--porcelain"], cwd=entry)
        dirty = len(porcelain.splitlines()) if porcelain else 0
        print(f"  {name}: branch={branch} dirty={dirty}")


def show_beads() -> None:
    br_output = run_capture(["br", "ready"])
    if br_output:
        for line in br_output.splitlines()[:5]:
            print(f"  {line}")
    else:
        print("  (no ready issues)")


def show_dir_size(label: str, path: Path) -> None:
    if not path.exists():
        print(f"  ({label} empty)")
        return
    du = run_capture(["du", "-sh", str(path)])
    print(f"  {du}" if du else f"  ({label} empty)")


def main() -> None:
    print("=== TheRock Workspace Status ===")
    print(f"Location: {WORKSPACE}")
    print()

    print("--- Source Checkouts ---")
    show_source_checkouts()
    print()

    print("--- Pending Beads ---")
    show_beads()
    print()

    print("--- Build ---")
    show_dir_size("build", WORKSPACE / "build")
    print()

    print("--- Cache ---")
    show_dir_size("cache", WORKSPACE / "cache")
    print()

    print("--- Environment ---")
    print(f"  THEROCK_WORKSPACE={os.environ.get('THEROCK_WORKSPACE', '(not set)')}")
    print(f"  VIRTUAL_ENV={os.environ.get('VIRTUAL_ENV', '(not active)')}")


if __name__ == "__main__":
    main()

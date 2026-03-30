#!/usr/bin/env python3
"""Initialize the TheRock workspace with source checkout.

Clones TheRock to sources/TheRock and runs fetch_sources.py to populate
submodules. Creates required workspace directories.

Usage:
    python tools/init_workspace.py [--shallow] [--skip-fetch]
"""

import argparse
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
SOURCES_DIR = WORKSPACE / "sources"
THEROCK_DIR = SOURCES_DIR / "TheRock"
THEROCK_REPO = "https://github.com/ROCm/TheRock.git"

REQUIRED_DIRS = [
    WORKSPACE / "sources",
    WORKSPACE / "build",
    WORKSPACE / "cache",
    WORKSPACE / "docs",
    WORKSPACE / ".tmp",
    WORKSPACE / ".beads",
]


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {' '.join(cmd)}"
        )


def ensure_directories() -> None:
    for d in REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  dir: {d.relative_to(WORKSPACE)}/")


def clone_therock(shallow: bool) -> None:
    if THEROCK_DIR.exists():
        print(f"  TheRock already exists at {THEROCK_DIR}, skipping clone")
        return

    print(f"  Cloning from {THEROCK_REPO}...")
    cmd = ["git", "clone"]
    if shallow:
        cmd.extend(["--depth", "1"])
    cmd.extend([THEROCK_REPO, str(THEROCK_DIR)])
    run(cmd)


def fetch_sources() -> None:
    fetch_script = THEROCK_DIR / "build_tools" / "fetch_sources.py"
    if not fetch_script.exists():
        raise FileNotFoundError(
            f"fetch_sources.py not found at {fetch_script}\n"
            f"TheRock clone may be incomplete"
        )

    print("  Running fetch_sources.py (this may take a while)...")
    run([sys.executable, str(fetch_script)], cwd=THEROCK_DIR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the TheRock workspace")
    parser.add_argument(
        "--shallow",
        action="store_true",
        help="Use shallow clone (--depth 1) for faster initial setup",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip running fetch_sources.py (clone only)",
    )
    args = parser.parse_args()

    print(f"Initializing TheRock workspace at {WORKSPACE}")
    print()

    print("Creating directories...")
    ensure_directories()
    print()

    print("Source checkout...")
    clone_therock(shallow=args.shallow)
    print()

    if not args.skip_fetch:
        print("Fetching sources...")
        fetch_sources()
        print()

    print("Done. Next steps:")
    print("  1. cd to workspace and run: direnv allow")
    print("  2. Configure build: cmake -B build -S sources/TheRock -GNinja \\")
    print("       -DTHEROCK_AMDGPU_FAMILIES=gfx1201")
    print("  3. Build: ninja -C build")


if __name__ == "__main__":
    main()

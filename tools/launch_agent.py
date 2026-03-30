#!/usr/bin/env python3
"""Launch a sandboxed claude agent session.

Usage:
    launch_agent.py planner
    launch_agent.py coder
    launch_agent.py reviewer
    launch_agent.py build-infra
    launch_agent.py generalist
    launch_agent.py shell
"""

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

ROLE_PROMPTS: dict[str, str] = {
    "planner": (
        "You are the planner/coordinator agent for TheRock build infrastructure work. "
        "Read CLAUDE.md for full context. Your job: maintain the plan, create and "
        "prioritize issues (br create), assign work to coding agents, track progress, "
        "and involve the human at milestones. Do not write code directly — coordinate "
        "the coders. Use 'br --actor planner' for all br commands."
    ),
    "coder": (
        "You are a coding agent for TheRock build infrastructure. Read CLAUDE.md for "
        "context. Run 'br ready --assignee=coder' to find assigned work. Implement "
        "changes in sources/TheRock/. Test your changes. Commit with descriptive "
        "messages. Close completed issues: 'br close <id> --actor=coder'."
    ),
    "reviewer": (
        "You are the code reviewer for TheRock build infrastructure. Read CLAUDE.md "
        "for context. Run 'br ready --assignee=reviewer' to find review requests. "
        "Review branches for: design alignment, code quality, test coverage, clean "
        "commit history. Approve or request changes."
    ),
    "build-infra": (
        "You are a build infrastructure expert specializing in CMake, meson, "
        "pkg-config, and ROCm build system patterns. Read CLAUDE.md for context. "
        "Focus on adding dependencies, fixing build issues, understanding build "
        "configuration. Follow TheRock's dual-mode CMakeLists.txt pattern."
    ),
    "generalist": (
        "You are co-working with stella, a core ROCm developer, on unstructured "
        "tasks. You have deep knowledge of ROCm build infrastructure, CMake, Python, "
        "and GPU computing. Focus on being helpful for ad-hoc investigation, "
        "debugging, prototyping, and documentation. Ask clarifying questions when "
        "intent is ambiguous. You are NOT ticket-driven — no br ready / br update "
        "loop. Just work directly with the developer on whatever they need."
    ),
}

ROLE_INITIAL: dict[str, str] = {
    "planner": "Read CLAUDE.md, then summarize current project status and open issues.",
    "coder": "Read CLAUDE.md, then run 'br ready --assignee=coder' to find assigned work.",
    "reviewer": "Read CLAUDE.md, then run 'br ready --assignee=reviewer' to find pending reviews.",
    "build-infra": "Read CLAUDE.md, then ask what build infrastructure task to work on.",
    "generalist": "Read CLAUDE.md. Ready to co-work — what are we looking at?",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch a sandboxed claude agent session")
    parser.add_argument(
        "role",
        choices=list(ROLE_PROMPTS.keys()) + ["shell"],
        help="Agent role to launch",
    )
    parser.add_argument("--model", default=None, help="Override model")
    parser.add_argument("--no-sandbox", action="store_true", help="Run without bwrap")
    args = parser.parse_args()

    sandbox = SCRIPT_DIR / "sandbox.py"

    if args.role == "shell":
        print("Launching sandboxed shell...")
        os.execvp(sys.executable, [sys.executable, str(sandbox)])
        return

    model = args.model or ("opus" if args.role == "generalist" else "sonnet")
    os.environ["BR_ACTOR"] = args.role

    claude_args = [
        "claude",
        "--dangerously-skip-permissions",
        "--append-system-prompt", ROLE_PROMPTS[args.role],
        "--model", model,
        ROLE_INITIAL[args.role],
    ]

    print(f"Launching {args.role} agent (model={model}) in sandbox...")

    if args.no_sandbox:
        os.execvp("claude", claude_args)
    else:
        os.execvp(sys.executable, [sys.executable, str(sandbox)] + claude_args)


if __name__ == "__main__":
    main()

# TheRock Workspace 1

Workspace for build infrastructure work on [TheRock](https://github.com/ROCm/TheRock) — the ROCm build super-project.

## Setup

```bash
# Initialize (creates dirs, clones TheRock if needed, fetches submodules)
python tools/init_workspace.py

# Activate environment
direnv allow

# Configure build
cmake -B build -S sources/TheRock -GNinja \
  -DTHEROCK_AMDGPU_FAMILIES=gfx1201 \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache

# Build
ninja -C build
```

## Directory Layout

```
sources/TheRock/    Source checkout (git clone + fetch_sources.py)
build/              CMake build tree
cache/              Compilation cache
docs/               Local notes and documentation
tools/              Python tooling (sandbox, agent launcher, status)
.beads/             Issue tracking (bead protocol)
.claude/            Claude Code configuration and agents
.tmp/               Workspace-local temp (agents use this)
.venv/              Python virtual environment (created by direnv)
```

## Tools

All tools are Python scripts in `tools/`. With direnv active, they are on PATH.

| Tool | Purpose |
|------|---------|
| `init_workspace.py` | Clone TheRock and run fetch_sources.py |
| `status.py` | Workspace status overview |
| `sandbox.py` | Bubblewrap sandbox launcher |
| `launch_agent.py` | Launch sandboxed agent sessions by role |

## Agent Roles

| Role | Purpose | Model |
|------|---------|-------|
| `planner` | Coordinate work, manage issues | sonnet |
| `coder` | Implement changes | sonnet |
| `reviewer` | Review code | sonnet |
| `build-infra` | CMake/build system expert | sonnet |
| `generalist` | Co-working on unstructured tasks | opus |

Launch: `python tools/launch_agent.py <role>`

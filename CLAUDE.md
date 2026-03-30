# TheRock Workspace

## CRITICAL: Git Rules

**Git operations in `sources/TheRock/` only.** The workspace root may be a git repo
for tracking config, but all code work happens inside the TheRock checkout.

- `sources/TheRock/` — Where agents do their work. Branches, commits, all code changes.
- Never `git push` without explicit authorization.
- Never amend commits without explicit authorization.

## What This Is

Workspace for build infrastructure work on [TheRock](https://github.com/ROCm/TheRock),
the CMake super-project for building HIP and ROCm from source.

## Project Context

### ROCm
AMD's open-source platform for GPU computing: HIP runtime, math libraries
(rocBLAS, rocFFT, etc.), compilers, and developer tools.

### TheRock
The build infrastructure for ROCm. A CMake super-project that manages submodules,
provides a unified build, handles cross-component dependencies, and generates
distribution packages.

## Directory Layout

```
sources/TheRock/    TheRock checkout (the main codebase)
build/              CMake build tree
cache/              Compilation cache
docs/               Local notes and documentation
tools/              Python tooling
.beads/             Issue tracking
.claude/            Claude Code config and agents
.tmp/               Workspace-local temp
.venv/              Python venv
```

## Build Commands

### Configure

```bash
cmake -B build -S sources/TheRock -GNinja \
  -DTHEROCK_AMDGPU_FAMILIES=gfx1201 \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
```

### Build

```bash
ninja -C build                          # Full build
ninja -C build clr+build               # Build specific component
ninja -C build clr+expunge && ninja -C build clr  # Clean rebuild
```

### Build subset

```bash
cmake -B build -S sources/TheRock -GNinja \
  -DTHEROCK_ENABLE_ALL=OFF \
  -DTHEROCK_ENABLE_HIPIFY=ON \
  -DTHEROCK_AMDGPU_FAMILIES=gfx1201
```

### Component targets

Every component exposes: `<component>` (full), `<component>+build` (rebuild),
`<component>+dist` (update artifacts), `<component>+expunge` (clean slate).

### Test

```bash
LD_LIBRARY_PATH=build/dist/rocm/lib build/dist/rocm/bin/<test_binary>
ctest --test-dir build
```

## Python Standards

- **Fail-fast**: Raise on errors, never silently continue
- **Dataclasses**: Use `@dataclass` for structured data, not tuples
- **Type hints**: Specific types, never `Any` (Python 3.10+ syntax: `T | None`)
- **Paths**: Use `pathlib.Path`
- **CLI**: Use `argparse` with help text
- **No timeouts on binutils**: Never timeout readelf, objcopy, etc.
- **Validate output**: Check files exist and are non-empty after creation

## Git Workflow

### Branch naming
`users/<username>/<short-description>`

### Commit messages
- Short summary (50-72 chars), then blank line, then details
- Include "Changes:" section with bullet points
- Include Claude Code footer

### Review workflow
Incremental commits during development. Review comments use `RVW:` / `RVWY:` markers.
Squash to PR at milestones.

## Agent Coordination

Uses `br` (beads-rust) for issue tracking. Issues live in `.beads/`.

### Roles
- **planner**: Plan, create/prioritize issues, assign work
- **coder**: Pick up `br ready` issues, implement, close
- **reviewer**: Review branches, gate merges
- **build-infra**: CMake/meson/pkg-config specialist
- **generalist**: Co-working with developer, not ticket-driven

### Beads workflow
```bash
br ready                              # Find open work
br update <id> --status=in_progress   # Claim
br create --title="..." --parent=<id> # Sub-task
br close <id>                         # Done
br sync --flush-only                  # Export before session end
```

## Conventions

- Don't be sycophantic. Engage in light debate if reasoning seems unsound.
- Don't claim results are "production" quality or use shaky metrics. Say how things are.
- Design docs include an "Alternatives Considered" section.
- GitHub issue references: short form `#NNNN`.

## Key Documentation (in sources/TheRock/)

- `README.md` — Build setup, feature flags
- `CONTRIBUTING.md` — Contribution guidelines
- `docs/development/build_system.md` — Build architecture
- `BUILD_TOPOLOGY.toml` — CI pipeline stages and artifacts

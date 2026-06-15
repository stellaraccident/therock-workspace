---
name: therock-minimal-core-build
description: Bootstrap a minimal TheRock core-platform build in a ROCm development workspace using prebuilt AMD LLVM artifacts from the latest usable nightly CI run. Use when the user asks to avoid rebuilding LLVM locally, build only core runtime/HIP/OpenCL/AMDSMI pieces, exclude math libraries, ML libraries, communication libraries, media libraries, debug tools, data center tools, rocprof-systems, rocprof-compute, or create/reuse a turnkey local minimal core build procedure.
---

# TheRock Minimal Core Build

## Quick Start

Use the bundled script from the workspace root:

```bash
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py
```

The default flow:

1. Find the newest completed scheduled `ci_nightly.yml` run on `ROCm/TheRock` `main` that has the required `sysdeps_*_generic` and `amd-llvm_*_generic` artifacts.
2. Download and bootstrap the `sysdeps` components first, then the `amd-llvm` components into `build/rocm-core`, creating TheRock `.prebuilt` markers.
3. Fetch only the source sets needed by the core platform: `base` and `rocm-systems`, with legacy `--include-*` groups explicitly disabled.
4. Configure `build/rocm-core` with core enabled and math/profiler app stacks disabled.
5. Build only the explicit minimal core artifact targets, avoiding the broad `therock-artifacts` and `therock-dist` aggregates because those can pull disabled host math artifacts such as FFTW3 into `dist/rocm`.
6. Assemble `build/rocm-core/dist/rocm` from an explicit artifact allowlist.

Use `--dry-run` first when changing flags or diagnosing artifact selection:

```bash
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --dry-run
```

If retrying after a failed build, keep downloaded artifacts in the workspace cache and reset the build tree:

```bash
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --clean-build-dir
```

If a previous run used the broad aggregate targets, repair the dist directory from already-built minimal core artifacts:

```bash
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --assemble-dist-only
```

## Procedure

Run from `/srv/vm-shared/projects/therock-workspace` unless the user has a different workspace root. Keep git operations inside `sources/TheRock/`; the skill script itself lives in the workspace-level `.codex/skills/` directory.

On Fedora-like hosts, CLR needs OpenGL development headers/libraries even for this minimal build:

```bash
sudo dnf install -y libglvnd-devel mesa-libGL-devel mesa-libEGL-devel
```

Default command:

```bash
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py
```

Useful options:

```bash
# Reuse a known workflow run instead of discovering latest nightly.
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --run-id 12345678901

# Configure only after bootstrapping sysdeps/LLVM and fetching sources.
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --no-build

# Use another target family or a semicolon-separated dist family set.
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --amdgpu-families gfx1100

# Build an exact multi-target core platform, for example local gfx1151 plus gfx1250.
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py \
  --amdgpu-targets 'gfx1151;gfx1250' \
  --dist-amdgpu-targets 'gfx1151;gfx1250' \
  --dist-bundle-name gfx1151-gfx1250

# Include build tests and runtime test artifacts.
./.venv/bin/python .codex/skills/therock-minimal-core-build/scripts/bootstrap_minimal_core_build.py --enable-tests
```

## CMake Shape

The minimal core configure intentionally uses explicit feature flags instead of `configure_stage.py --stage compiler-runtime`, because that CI stage includes profiler-core and `rocprofiler-compute`.

Core flags:

```bash
cmake -B build/rocm-core -S sources/TheRock -GNinja \
  -DTHEROCK_AMDGPU_FAMILIES=gfx1201 \
  -DTHEROCK_AMDGPU_TARGETS= \
  -DTHEROCK_DIST_AMDGPU_FAMILIES=gfx1201 \
  -DTHEROCK_DIST_AMDGPU_TARGETS= \
  -DTHEROCK_ENABLE_ALL=OFF \
  -DTHEROCK_ENABLE_CORE=ON \
  -DTHEROCK_ENABLE_MATH_LIBS=OFF \
  -DTHEROCK_ENABLE_ML_LIBS=OFF \
  -DTHEROCK_ENABLE_COMM_LIBS=OFF \
  -DTHEROCK_ENABLE_PROFILER=OFF \
  -DTHEROCK_ENABLE_ROCPROFSYS=OFF \
  -DTHEROCK_ENABLE_ROCPROFILER_COMPUTE=OFF \
  -DBUILD_TESTING=OFF
```

`THEROCK_ENABLE_CORE=ON` lets TheRock implicitly enable required dependencies such as `COMPILER`; the prebuilt marker files make the compiler subprojects stage from fetched artifacts instead of rebuilding LLVM locally. The `sysdeps` artifact is bootstrapped before `amd-llvm` because the prebuilt compiler binaries link against bundled runtime libraries such as `librocm_sysdeps_z.so.1`.

Do not use `therock-artifacts` or `therock-dist` as the default minimal-core build targets. In this checkout, those aggregate targets include `artifact-fftw3`; use the script defaults or explicit `artifact-*` core targets and then let the script assemble `dist/rocm`.

## Notes

- The script defaults to `--release-type ci` because `ci_nightly.yml` build artifacts are published using CI artifact storage. Override this only when fetching from a release workflow that wrote to `therock-nightly-artifacts`.
- The default download cache is `.tmp/therock-minimal-core-artifacts`, outside `build/rocm-core`, so `--clean-build-dir` can reset the build without discarding nightly archives.
- If `THEROCK_FLAG_INCLUDE_HRX=ON` is needed, pass `--include-hrx`; this adds the `optional-hrx` source set and the matching CMake flag.
- The script requires TheRock archive dependencies available to the invoking Python, especially `pyzstd` for `.tar.zst` extraction. In this workspace, use `./.venv/bin/python`.

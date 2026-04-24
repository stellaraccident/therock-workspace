# OpenMP Runtime Structure in TheRock's Three-Stage Compiler Pipeline

Date: 2026-04-01
Context: Diagnosed while working on PR #3928 (Fortran module files missing in CI).

## Overview

TheRock splits the LLVM compiler into three stages:

1. **`base/amd-llvm`** — Full in-tree LLVM build (clang, lld, libomp, device-libs)
2. **`compiler/amd-llvm-flang`** — Standalone flang compiler (flang binary, MLIR, intrinsic `.mod` files)
3. **`compiler/amd-llvm-offload`** — Standalone offload runtimes (libomptarget, flang-rt)

The OpenMP runtime is spread across all three, and the wiring is fragile.

## What Each Stage Produces

| Component | Stage | Notes |
|-----------|-------|-------|
| `libomp.so` (host OpenMP runtime) | amd-llvm | Built with `OPENMP_ENABLE_LIBOMPTARGET=ON` for dlsym bridge |
| `libgomp.so`, `libiomp5.so` (compat) | amd-llvm | |
| `omp.h`, `omp-tools.h` | amd-llvm | Installed to clang resource dir |
| `libomptarget.so` (offload manager) | amd-llvm-offload | |
| `libomptarget.rtl.amdgpu.a` (plugin) | amd-llvm-offload | Statically linked into libomptarget.so |
| `libLLVMOffload.so` | amd-llvm-offload | |
| `flang-rt` libs | amd-llvm-offload | Host runtime (`libflang_rt.runtime.a`) |
| `iso_fortran_env.mod` etc. | amd-llvm-flang | Intrinsic Fortran modules |
| **`omp_lib.mod`** | **NOBODY** | See "Broken Chain 1" |
| **`libompdevice`** | **NOBODY** | See "Broken Chain 2" |
| **`libomptarget-amdgpu.bc`** | **NOBODY** | See "Broken Chain 2" |

## Why libomp.so Is Built in base/amd-llvm (Not Offload)

This was a deliberate decision driven by a nasty circular dependency.

`libomp.so` needs `OPENMP_ENABLE_LIBOMPTARGET=ON` at **build time** to bake in
dlsym hooks (`__tgt_target_sync`, etc.) that cooperate with libomptarget.so for
async target tasks. But libomp.so cannot **link** against libomptarget.so because
that would create a hard dependency on the offload stack.

The solution is a **dlsym bridge**: libomp.so calls
`dlsym(RTLD_DEFAULT, "__tgt_target_sync")` at runtime. If libomptarget.so is not
loaded, dlsym returns NULL, callbacks stay NULL, and all offload hooks are silently
skipped (guarded by `UNLIKELY(ptr != NULL)`). When amd-llvm-offload is installed,
libomptarget.so becomes available and libomp discovers it via dlsym.

This is documented in `base/pre_hook_amd-llvm.cmake` lines 28-36.

## The Two Runtimes Entry Points

LLVM has two different CMakeLists.txt files that orchestrate runtime builds, and
they have **very different capabilities**:

### `llvm/runtimes/CMakeLists.txt` (the bootstrapping one)

- Called from the full LLVM build (`llvm/CMakeLists.txt`)
- Handles `LLVM_RUNTIME_TARGETS` for cross-compilation (e.g., `amdgcn-amd-amdhsa`)
- Spawns separate CMake processes via `llvm_ExternalProject_Add(runtimes-${name} ...)`
- Uses `PASSTHROUGH_PREFIXES` to forward config like `LIBOMP_*`, `FLANG_RUNTIME*`
- Has special logic for `LLVM_TOOL_FLANG_BUILD` (lines 631-641) that sets
  `LIBOMP_FORTRAN_MODULES_COMPILER` and wires up the omp_lib.mod build
- **This is the only code path that can build device runtimes**

### `runtimes/CMakeLists.txt` (the standalone one)

- Can be invoked directly or from `llvm_ExternalProject_Add`
- Only processes `LLVM_ENABLE_RUNTIMES` via `add_subdirectory`
- **Does NOT handle `LLVM_RUNTIME_TARGETS`** — no cross-target support
- No ExternalProject spawning, no passthrough mechanism
- Variables set in the parent scope ARE visible (normal CMake scoping)

**amd-llvm-offload uses the standalone one** (`add_subdirectory("${LLVM_MONOREPO_DIR}/runtimes" "runtimes")`).

## Broken Chain 1: `omp_lib.mod` (Nobody Builds It)

`omp_lib.mod` is the Fortran module that provides OpenMP bindings (`omp_get_num_threads`, etc.).
It can be built by two different code paths, but neither fires in TheRock's three-stage setup.

### Path A: openmp/module/CMakeLists.txt (lines 27-38)

Builds `omp_lib.mod` when `LIBOMP_FORTRAN_MODULES_COMPILER` is set. This is the
"runtimes build" path — designed for when flang is available but openmp is built
as a runtime (not a project).

**Why it doesn't fire:**

- **amd-llvm:** `LLVM_TOOL_FLANG_BUILD=FALSE` (flang not in `LLVM_ENABLE_PROJECTS`),
  so `llvm/runtimes/CMakeLists.txt` line 631 never sets `LIBOMP_FORTRAN_MODULES_COMPILER`.
  The `openmp/module/CMakeLists.txt` IS entered, but the variable is unset.

- **amd-llvm-offload:** `openmp` is NOT in `LLVM_ENABLE_RUNTIMES` (only `offload;flang-rt`).
  The standalone `runtimes/CMakeLists.txt` only `add_subdirectory`s runtimes in that list.
  `openmp/module/CMakeLists.txt` is **never entered**. Even though we set
  `LIBOMP_FORTRAN_MODULES_COMPILER` in the offload CMakeLists.txt, the variable
  sits unused because nobody processes the openmp project.

### Path B: openmp/module/CMakeLists.txt (lines 39-62)

Builds `omp_lib.mod` when `LIBOMP_FORTRAN_MODULES=ON` (a cache variable, defaults OFF).
Uses the system Fortran compiler via `enable_language(Fortran)`. This is the legacy path.

**Why it doesn't fire:** `LIBOMP_FORTRAN_MODULES` defaults to `OFF` and nobody sets it.

### Path C: flang/tools/f18/CMakeLists.txt (lines 152-159)

Flang used to build `omp_lib.mod` itself but now defers to the openmp runtime.
When `openmp` is in `LLVM_ENABLE_RUNTIMES`, it prints "assuming omp_lib.mod is
built there" and skips.

**Why it doesn't help:**

- **amd-llvm-flang:** Sets `LLVM_ENABLE_RUNTIMES="openmp"` (line 85 of its CMakeLists)
  specifically to suppress the warning and tell flang "someone else will build it."
  But "someone else" is supposed to be amd-llvm-offload, which doesn't.

### The gap

```
amd-llvm:         openmp/module/ entered, but LIBOMP_FORTRAN_MODULES_COMPILER unset
amd-llvm-flang:   flang skips it because LLVM_ENABLE_RUNTIMES contains "openmp"
amd-llvm-offload: openmp/module/ never entered (openmp not in LLVM_ENABLE_RUNTIMES)
```

Nobody builds `omp_lib.mod`.

## Broken Chain 2: Device Runtimes (libompdevice, libomptarget-amdgpu.bc)

The device OpenMP runtime (`libompdevice` / `libomptarget-amdgpu.bc`) is built by
`openmp/device/CMakeLists.txt`. This file is entered when `openmp` is processed
as a runtime AND the target triple matches `amdgcn-amd-amdhsa`:

```cmake
# openmp/CMakeLists.txt lines 143-147
if("${LLVM_DEFAULT_TARGET_TRIPLE}" MATCHES "^amdgcn|^nvptx|^spirv64" ...)
  add_subdirectory(device)    # GPU device runtime
else()
  add_subdirectory(module)    # Fortran modules (host only)
  add_subdirectory(runtime)   # libomp.so (host only)
```

The amd-llvm-offload CMakeLists sets up cross-target config:
```cmake
set(LLVM_RUNTIME_TARGETS "default;amdgcn-amd-amdhsa")
set(RUNTIMES_amdgcn-amd-amdhsa_LLVM_ENABLE_RUNTIMES "openmp;flang-rt")
```

**But `LLVM_RUNTIME_TARGETS` is completely ignored by the standalone `runtimes/CMakeLists.txt`.**
Only `llvm/runtimes/CMakeLists.txt` (the bootstrapping one) handles cross-target
builds by spawning `llvm_ExternalProject_Add(runtimes-${name} ...)` for each target.

The result:
- `LLVM_RUNTIME_TARGETS` is set but never consumed
- No cross-compilation CMake invocation is spawned for `amdgcn-amd-amdhsa`
- `openmp/device/CMakeLists.txt` is never entered
- `libompdevice` is never built
- `libomptarget-amdgpu.bc` is never generated

The offload project itself (`offload/CMakeLists.txt` line 142) checks
`RUNTIMES_amdgcn-amd-amdhsa_LLVM_ENABLE_RUNTIMES` and sees openmp is present,
so it doesn't warn. But the actual device build never happens.

### Impact

Any code compiled with `--offload-arch=gfx*` flags that links with the clang
driver will fail:
```
ld.lld: error: unable to find library -lompdevice
```
The driver automatically adds `-lompdevice` for GPU offload targets.

## Fix Options

### For omp_lib.mod

| Option | Approach | Tradeoff |
|--------|----------|----------|
| A | Add `openmp` to host `_runtimes` in offload CMakeLists | Rebuilds libomp.so redundantly. Simple. |
| B | Custom command in offload CMakeLists to invoke flang on `omp_lib.F90.var` | Duplicates openmp build logic. Surgical. |
| C | Remove `LLVM_ENABLE_RUNTIMES="openmp"` from flang, let flang build it | Changes the contract. May have other implications. |

### For device runtimes (libompdevice)

| Option | Approach | Tradeoff |
|--------|----------|----------|
| A | Switch offload to use `llvm/runtimes/CMakeLists.txt` | Requires full LLVM build context, major restructuring |
| B | Add ExternalProject in offload CMakeLists for amdgcn target | Replicates what `llvm/runtimes/CMakeLists.txt` does for cross-targets |
| C | Move device runtime build into `base/amd-llvm` | Pulls more into the base build, potentially slower base stage |
| D | Build device runtimes as a 4th stage | Clean separation but adds CI complexity |

## Key File References

| Purpose | File | Key Lines |
|---------|------|-----------|
| amd-llvm openmp config | `base/pre_hook_amd-llvm.cmake` | 26 (LLVM_ENABLE_RUNTIMES), 28-36 (dlsym bridge comment) |
| Bootstrapping runtimes | `compiler/amd-llvm/llvm/runtimes/CMakeLists.txt` | 285-310 (ExternalProject + PASSTHROUGH), 433 (per-target), 631-641 (LLVM_TOOL_FLANG_BUILD) |
| Standalone runtimes | `compiler/amd-llvm/runtimes/CMakeLists.txt` | 41-60 (LLVM_ENABLE_RUNTIMES only), 341 (add_subdirectory loop) |
| Offload top-level | `compiler/amd-llvm-offload/CMakeLists.txt` | 27-28 (runtimes lists), 43-44 (LIBOMP vars, unused), 69-71 (LLVM_RUNTIME_TARGETS, ignored), 78 (standalone entry) |
| Flang omp_lib skip | `compiler/amd-llvm/flang/tools/f18/CMakeLists.txt` | 155-158 |
| Flang openmp declaration | `compiler/amd-llvm-flang/CMakeLists.txt` | 83-85 |
| OpenMP host vs device routing | `compiler/amd-llvm/openmp/CMakeLists.txt` | 143-151 |
| OpenMP module build | `compiler/amd-llvm/openmp/module/CMakeLists.txt` | 27-38 (LIBOMP_FORTRAN_MODULES_COMPILER path) |
| OpenMP device runtime | `compiler/amd-llvm/openmp/device/CMakeLists.txt` | 102 (libompdevice), 142-150 (ompdevice static archive) |
| Super-project wiring | `compiler/CMakeLists.txt` | 188-215 (flang), 223-268 (offload) |

## Verified Build Outputs (2026-04-01, gfx1100 minimal build)

```
base/amd-llvm/stage/lib/llvm/lib/libomp.so              # host OpenMP runtime
compiler/amd-llvm-flang/stage/lib/llvm/include/flang/    # 17 .mod files, NO omp_lib.mod
compiler/amd-llvm-offload/stage/lib/llvm/lib/libomptarget.so  # offload manager
compiler/amd-llvm-offload/stage/lib/llvm/lib/clang/23/lib/x86_64-unknown-linux-gnu/libflang_rt.runtime.a
# Total 40 files in offload stage. No device runtimes.
```

# Tensile/TensileLite Multi-Arch Convergence Fix

**Date:** 2026-03-31
**Status:** Design proposal
**Blocking:** KPACK enablement (TheRock PR #4257)

## Problem

When TheRock builds math-libs in parallel shards (one per GPU family), each
shard produces Tensile/TensileLite output files that are shard-specific but
don't have the architecture in the filename. When artifacts are overlaid on
a single filesystem (the standard deployment model), last-writer-wins causes
wrong-arch metadata to be used at runtime.

This is convergence finding H2 from the
[convergence analysis](https://gist.github.com/stellaraccident/d728d1ddd35574a93fc020716b49f099).

## Impact

- **hipblas:** 166 test failures on Windows gfx1151 (GEMM returns
  `rocblas_status_internal_error` because fallback `.dat` references kernel
  names from gfx1200 shard, not gfx1151)
- **hipsolver:** 1123 test failures (cascading from rocblas via rocsolver,
  masked as numerical failures — see ROCm/rocm-libraries#6069)
- **rocwmma:** 3 GEMM validation failures
- **hipblaslt:** Likely affected but errors silently swallowed (`#if 0`
  compiled-out catch blocks)
- **Linux:** Likely racing too but false-passing or not tested in this
  configuration

## Complete List of Convergence-Violating Files

### rocBLAS / Tensile (56 files)

| Category | Count | Example | Shard-specific? |
|---|---|---|---|
| `*_fallback.dat` | 54 | `TensileLibrary_Type_DD_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback.dat` | Yes — different kernel names per arch |
| `TensileManifest.txt` | 1 | `TensileManifest.txt` | Yes — lists shard-specific file paths |
| `TensileLibrary.dat`/`.yaml` | 0 | (not present in these builds) | N/A |

The 54 `_fallback.dat` files contain msgpack-serialized solution metadata.
Each solution references a kernel name like `Cijk_..._GLVWA1_GLVWB1_G...`
(gfx1200) vs `Cijk_..._1LDSB0_...EPS0_GLV...` (gfx1151). The runtime loads
the `.dat`, finds the kernel name, then calls `hipModuleGetFunction` on the
corresponding `_fallback_gfx1151.hsaco` — which contains the gfx1151 kernel
names, not the gfx1200 ones. Result: `hipErrorNotFound`.

### hipBLASLt / TensileLite (3 files)

| File | Size varies? | Content |
|---|---|---|
| `hipblasltExtOpLibrary.dat` | 1.4K / 2.9K / 5.7K | ExtOp kernel-to-`.co` path mapping |
| `hipblasltTransform.hsaco` | 373K / 779K / 1.5M | **Compiled GPU code** (fat binary) |
| `TensileLiteLibrary_lazy_Mapping.dat` | 4K / 29K / 18K | Lazy-load kernel→file mapping |

`hipblasltTransform.hsaco` is the most dangerous — it's actual compiled GPU
code for the wrong architecture.

### Files that are NOT violations

All files with `gfx` in the name are properly arch-differentiated:
`*_gfx1151.co`, `*_gfx1151.dat`, `*_fallback_gfx1151.hsaco`,
`Kernels.so-000-gfx1151.hsaco`, `TensileLibrary_lazy_gfx1151.dat`.

## Root Cause in Tensile Code Generation

In `Tensile/TensileCreateLibrary.py`, `addFallback()` (line 949) creates
fallback solution entries. The lazy library key is constructed from
`placeholderStr()` which produces:

```
TensileLibrary_Type_DD_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback
```

No arch in the name. In a single-build (all arches at once), this works
because `addFallback()` inserts the same Python object for every arch. In a
multi-shard build, each shard runs Tensile independently with different arch
sets, producing different fallback content under the same filename.

At runtime, `PlaceholderLibrary.hpp:getCodeObjectFileName()` (line 186)
appends `_<arch>.hsaco` to the fallback prefix to find the hsaco file.
The `.hsaco` files ARE properly arch-named (`_fallback_gfx1151.hsaco`).
But the `.dat` metadata that maps problem types to kernel names is NOT
arch-named — and the kernel names inside it are arch-specific.

## Proposed Fix

### Part 1: Rename fallback `.dat` files to include arch

**Where:** `Tensile/TensileCreateLibrary.py`, `addFallback()` function

Change the lazy library key from:
```
TensileLibrary_Type_DD_..._fallback
```
to:
```
TensileLibrary_Type_DD_..._fallback_gfx1151
```

This requires modifying `addFallback()` to iterate over target architectures
and produce per-arch fallback entries (it already iterates to build per-arch
`.hsaco` files — the `.dat` key just needs to follow).

The `PlaceholderLibrary` nodes in the parent `.dat` tree must reference the
new arch-specific filenames.

### Part 2: Update C++ runtime for backward compatibility

**Where:** `Tensile/Source/lib/include/Tensile/PlaceholderLibrary.hpp`,
`getCodeObjectFileName()` (line 186)

Currently: `filePrefix + "_" + arch + ".hsaco"` → `..._fallback_gfx1151.hsaco`

With the new naming, `filePrefix` already contains the arch
(`..._fallback_gfx1151`), so appending `_gfx1151.hsaco` would produce
`..._fallback_gfx1151_gfx1151.hsaco`. The fix: detect the `_fallback_<arch>`
suffix in `filePrefix` and avoid double-appending.

For backward compatibility: if the arch-specific `.dat` is not found, fall
back to the arch-neutral filename. This supports mixed old/new installations.

### Part 3: hipBLASLt / TensileLite

**`hipblasltExtOpLibrary.dat`:** Must include arch in name. The generation
script is `hipblaslt/device-library/extops/CMakeLists.txt:21-96`. Each
iteration of the `foreach(arch)` loop should produce
`hipblasltExtOpLibrary_${arch}.dat` instead of overwriting the same file.

**`hipblasltTransform.hsaco`:** This is a fat binary — it must be compiled
per-arch (or the arch-bundled binary must be named per-arch). Since it's a
Clang offload bundle, the kpack splitter should handle it like any other fat
binary. It may need to be added to the `HipBLASLtHandler` in the database
handler.

**`TensileLiteLibrary_lazy_Mapping.dat`:** Same pattern as
`TensileLibrary_lazy_*.dat` — must include arch. The generation is in
`hipblaslt/tensilelite/Tensile/TensileCreateLibrary/Run.py` line 686.

### Part 4: `TensileManifest.txt`

This is build-time only (not used at runtime — rocBLAS does filesystem
probing via `TestPath()` and `GetModuleFileNameA`). Exclude from installed
artifacts entirely, or make it per-arch. Simplest: add to `.gitignore` or
strip during install.

## Error Swallowing Locations to Fix

These are not convergence issues but were discovered during investigation.
They make convergence bugs much harder to diagnose.

### rocBLAS

| File:Line | What's swallowed |
|---|---|
| `tensile_host.cpp:977` | `initializeLazyLoading` failure — `PRINT_IF_HIP_ERROR` only |
| `tensile_host.cpp:1067` | `hipGetDevice` failure — continues with bad device |
| `tensile_host.cpp:1348-1361` | Exception details require `ROCBLAS_VERBOSE_TENSILE_ERROR` env var |

### hipBLASLt

| File:Line | What's swallowed |
|---|---|
| `tensile_host.cpp:2175` | `loadCodeObjectFile` return `static_cast<void>` — discarded |
| `tensile_host.cpp:2284` | `initializeLazyLoading` return `static_cast<void>` — discarded |
| `tensile_host.cpp:2289` | Library load failure — `abort()` is commented out |
| `tensile_host.cpp:2307` | `hipGetDevice` return `static_cast<void>` — discarded |
| `tensile_host.cpp:2739-3487` | **16 catch blocks** with logging `#if 0`'d out |

### Tensile/TensileLite shared

| File:Line | What's swallowed |
|---|---|
| `HipSolutionAdapter.cpp:64` | `hipModuleUnload` in destructor |
| `HipSolutionAdapter.cpp:173-176` | `hipErrorUnknown`/`hipErrorSharedObjectInitFailed` silently skipped |
| `HipSolutionAdapter.cpp:296-311` | Lazy-load failure falls through silently |

## Testing Plan

1. Build math-libs for two different arch families
2. Overlay the outputs on a single filesystem
3. Verify no filename collisions (all files unique or byte-identical)
4. Run rocblas-test and hipblas-test with `ROCBLAS_VERBOSE_TENSILE_ERROR=1`
5. Verify all GEMM types (SS, DD, CC, ZZ) pass

## Alternatives Considered

### Fix only in the kpack splitter

The splitter could exclude all `.dat`/`.txt` files from the generic artifact.
This would work for kpack-split builds but does NOT fix the underlying
convergence violation — non-kpack multi-shard builds would still race. The
fix must be in Tensile's file generation.

### Merge `.dat` files post-build

A post-overlay step could merge per-shard `.dat` files into a unified one.
This adds complexity to the CI pipeline and doesn't solve the filesystem
overlay problem — you'd still need per-arch filenames to avoid clobbering
during overlay, then a merge step afterward. More complex than just naming
them correctly upfront.

### Build Tensile with all DIST targets in every shard

This would make all fallback `.dat` files identical across shards (same
solution tables for all arches). However, it's expensive — Tensile is already
one of the longest build steps — and it contradicts the purpose of sharding.

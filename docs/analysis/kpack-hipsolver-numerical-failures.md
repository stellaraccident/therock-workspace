# KPACK Windows Test Failures — Root Cause Analysis

**Date:** 2026-03-31 (final)
**CI Run:** ROCm/TheRock #4257, run 23775619092

## Root Causes Found

### 1. Tensile `_fallback.dat` convergence violation

**Affects:** hipblas (166 failures), hipsolver (1123 failures), rocwmma (3 failures)

54 `TensileLibrary_Type_*_fallback.dat` files are shard-specific (different
kernel names per architecture) but lack `gfx` in the filename. In the
multi-shard kpack split build, these leak into the generic artifact from
whichever shard uploads last.

At runtime, Tensile loads the wrong shard's fallback `.dat`, finds kernel
names that don't exist in the target architecture's `.hsaco`, and
`hipModuleGetFunction` returns `hipErrorNotFound`. This propagates as
`rocblas_status_internal_error` (status 6).

The hipsolver "numerical" failures (including max_error of 10^35 and NaN)
are actually uncomputed buffers — rocsolver calls rocblas GEMM, the GEMM
fails, rocsolver ignores the error (ROCm/rocm-libraries#6069), and
hipsolver compares garbage against the LAPACK reference.

**Fix:** ROCm/rocm-libraries#5976 (in progress, but has a gap — see
`docs/analysis/tensile-convergence-gap-for-davidd.md`). The `_fallback.dat`
files must be moved into `library/<arch>/` subdirectories alongside the
manifest.

### 2. `ftell` 32-bit overflow on Windows

**Affects:** rocprim (24 failures), hipcub (2 failures), rocthrust (mass failures)

`archive.cpp:get_file_size()` uses `ftell()` which returns `long` — 32-bit
on Windows even on x64. The `prim_test_gfx1151.kpack` archive is 4.1 GB,
overflowing the 32-bit return → `get_file_size()` returns 0 →
`KPACK_ERROR_IO_ERROR` (9).

**Fix:** Use `_ftelli64`/`_fseeki64` on Windows in
`rocm-systems/shared/kpack/runtime/src/archive.cpp:42-58`.

### 3. gfx110X fetch targets missing gfx1101

**Affects:** All gfx110X-all Windows tests (15 failures on gfx110X-all)

`amdgpu_family_matrix.py` only fetched gfx1100 kpack archives, but
test runners may have gfx1101 GPUs.

**Fix:** Committed on kpack branch (`539e5cc1`).

## Hypotheses Tested and Eliminated

### Kpack index mapping — CORRECT

Exhaustive verification with downloaded artifacts:
- Both shards (gfx110X-all and gfx1151) produce identical TOC structure
  (66 rocblas + 238 rocsolver + 333 rocsparse entries, matching ordinals)
- Within-build COFF linker maintains `.hipFatB` ↔ `.hip_fat` ordering
- Unsplit binaries from main run 23760914658: all 238 wrappers map to
  expected bundles sequentially
- The kpack code object loading path is correct

### TensileManifest.txt — NOT the runtime issue

`TensileManifest.txt` is shard-specific but NOT used at runtime. rocBLAS
discovers Tensile libraries via filesystem probing (`TestPath()` in
`tensile_host.cpp:734-747`). The manifest is build-time only.

### Kpack transformation of rocblas.dll — NOT the issue

Tensile and kpack use completely separate loading paths:
- Kpack: `__hipRegisterFatBinary` → `StatCO` → deferred loading
- Tensile: `hipModuleLoadData` → `DynCO` → separate module namespace

No interaction between the two. Tensile's `.co`/`.hsaco` files are loaded
from disk, unaffected by kpack transformation.

## Related Work

| Item | Status |
|---|---|
| ROCm/rocm-libraries#6069 | Filed — rocsolver unchecked returns from rocblas GEMM |
| ROCm/rocm-libraries#5976 | Open — Tensile convergence fix (manifest only, gap on fallback.dat) |
| ROCm/rocm-libraries#5354 | Open — hipBLASLt convergence fix (looks complete) |
| hipblas false-pass | Paged to CI team — 166 failures reported as passing |
| `docs/analysis/tensile-convergence-gap-for-davidd.md` | Detailed gap analysis for BLAS team |
| `docs/design/tensile-convergence-fix.md` | Full design including error-swallowing inventory |

## Artifacts Used

| Artifact | Source | Purpose |
|---|---|---|
| `blas_lib_generic` (win) | Kpack run 23775619092 | Verified TensileManifest from gfx120X shard |
| `blas_lib_gfx1151.kpack` (win) | Kpack run 23775619092 | Verified TOC structure |
| `blas_lib_gfx1100.kpack` (win) | Kpack run 23775619092 | Cross-shard TOC comparison |
| `blas_lib_gfx110X-all` (win, unsplit) | Main run 23760914658 | Wrapper/bundle ordering check |
| `blas_lib_gfx1151` (win, unsplit) | Main run 23760914658 | Wrapper/bundle ordering check |
| `prim_test_gfx1151` (win, unsplit) | Main run 23760914658 | Splitter test, archive size check |
| `_fallback.dat` files | All three sources | Hash comparison proving shard-specificity |

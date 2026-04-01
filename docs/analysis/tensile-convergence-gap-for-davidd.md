# Tensile Convergence: Gap in rocBLAS PR #5976

**To:** David Dixon (@davidd-amd)
**Date:** 2026-03-31
**Context:** TheRock kpack enablement (PR #4257), CI run 23775619092

## Summary

Your PRs #5976 (rocBLAS/Tensile) and #5354 (hipBLASLt) are on the right
track. The hipBLASLt one looks complete. The rocBLAS one has a critical gap:
the 54 `*_fallback.dat` files are shard-specific but not addressed by the PR.

These fallback `.dat` files are the root cause of 166 hipblas GEMM test
failures and 1123 hipsolver test failures on Windows gfx1151 in the kpack
enablement CI run.

## The Gap

PR #5976 commit `1cb999d` says:

> "All Tensile kernel and library artifacts are already arch-namespaced
> (e.g. TensileLibrary_lazy_gfx942.dat, KernelName_gfx942.co) and thus
> already shard-safe in the flat library/ layout. Only TensileManifest.txt
> was not arch-namespaced."

This is incorrect. The `*_fallback.dat` files have NO arch in the filename
but contain arch-specific kernel solution tables.

## Evidence

We compared the DD GEMM fallback `.dat` across three shards (hash + size):

| Shard | MD5 | Size | Kernel name in solution[0] |
|---|---|---|---|
| gfx120X (won generic race) | `1bcbb15...` | 6366 | `Cijk_..._AMAS0_BL0_BS1_GLVWA1_GLVWB1_G...` |
| gfx1151 (correct for test) | `56ad30f...` | 6810 | `Cijk_..._1LDSB0_AMAS0_BL0_BS1_EPS0_GLV...` |
| gfx110X-all | `c1cbf43...` | 6830 | (different again) |

All 54 `_fallback.dat` files differ across all three shards. The kernel
names inside reference different Tensile tuning parameters per architecture.

## Failure Chain

```
gfx120X shard wins generic race
  → TensileLibrary_Type_DD_..._fallback.dat has gfx120X kernel names
  → gfx1151 test runner loads this .dat
  → Tensile finds solution with kernel "Cijk_...GLVWA1_GLVWB1_G..."
  → hipModuleGetFunction in fallback_gfx1151.hsaco
  → hipErrorNotFound (hsaco has "Cijk_...1LDSB0_...EPS0_GLV..." instead)
  → exception caught at tensile_host.cpp:1348
  → rocblas_status_internal_error (6)
```

This affects all types that use fallback kernels: DD, CC, ZZ, and batched
SS/HH on architectures without optimized `.co` solutions.

## Suggested Fix

In `TensileCreateLibrary.py`, route `newLibraryDir` through `libraryDir()`
the same way `manifestFile` is routed. This moves ALL library output
(including fallback `.dat` files) to `library/<arch>/` for single-arch
builds.

Your earlier commit `f35d3d32` did this but you reverted it in `1cb999d2`
because `sanityCheck()` path comparison broke. The fix for sanityCheck is
to also route `buildAssemblyCodeObjectFiles`/`buildSourceCodeObjectFiles`
output through `libraryDir()` — or adjust sanityCheck to be aware of the
per-arch subdirectory.

The runtime already handles this: `tensile_host.cpp:801-802` probes
`library/<arch>/` before `library/`, so fallback `.dat` files in the
per-arch subdirectory will be found automatically.

## Files That Need to Move to `library/<arch>/`

All 54 of these (one per problem type × layout combination):

```
TensileLibrary_Type_4xi8I_HPA_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback.dat
TensileLibrary_Type_4xi8I_HPA_Contraction_l_Ailk_Bljk_Cijk_Dijk_fallback.dat
TensileLibrary_Type_4xi8I_HPA_Contraction_l_Alik_Bjlk_Cijk_Dijk_fallback.dat
TensileLibrary_Type_4xi8I_HPA_Contraction_l_Alik_Bljk_Cijk_Dijk_fallback.dat
TensileLibrary_Type_BB_HPA_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback.dat
...
TensileLibrary_Type_ZZ_Contraction_l_AlikC_Bljk_Cijk_Dijk_fallback.dat
```

Plus `TensileManifest.txt` (which your PR already handles).

## hipBLASLt PR #5354

Looks complete for the three violating files we identified:
- `hipblasltExtOpLibrary.dat` → moved to `library/<arch>/`
- `hipblasltTransform.hsaco` → renamed to per-arch, moved to `library/<arch>/`
- `TensileLiteLibrary_lazy_Mapping.dat` → (check if covered)

## Verification

After the fix, this command should show zero collisions:

```bash
# Build for gfx1100 and gfx1151 separately, overlay outputs
diff <(find shard_gfx1100/library/ -type f -printf '%f\n' | sort) \
     <(find shard_gfx1151/library/ -type f -printf '%f\n' | sort) \
  | grep "^[<>]"
# Any files in common must be byte-identical:
# comm -12 <list_a> <list_b> | while read f; do diff ... ; done
```

## Related Issues

- TheRock kpack enablement: ROCm/TheRock#4257
- rocsolver unchecked returns (makes these failures look like numerics):
  ROCm/rocm-libraries#6069
- Convergence analysis gist: stellaraccident/d728d1ddd35574a93fc020716b49f099

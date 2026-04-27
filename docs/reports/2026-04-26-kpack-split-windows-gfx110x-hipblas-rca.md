# RCA: Windows gfx110X hipBLAS failures after KPACK split artifacts

Date: 2026-04-26

## Summary

The Windows gfx110X hipBLAS failures introduced by `THEROCK_FLAG_KPACK_SPLIT_ARTIFACTS=ON` are not explained by a missing `blas_lib_gfx1101.kpack`, a missing fetched architecture artifact, or a rocBLAS submodule change. The smoking gun is in the split rocBLAS Tensile files:

* The failing test run combines `blas_lib_generic.tar.zst` with `blas_lib_gfx1101.tar.zst`.
* `blas_lib_generic` contains rocBLAS fallback Tensile `.dat` databases whose solution kernel names do not exist in the paired `gfx1101` fallback `.hsaco` code objects from `blas_lib_gfx1101`.
* The passing monolithic artifact has the same fallback `.dat` files and `gfx1101` `.hsaco` files in one artifact, and the solution names match.

This causes rocBLAS/Tensile lazy loading to ask HIP for a kernel symbol that is not exported by the loaded code object. HIP reports a not-found module function lookup, rocBLAS converts that to `rocblas_status_internal_error`, and hipBLAS reports `HIPBLAS_STATUS_INTERNAL_ERROR` (`6`). Disabling the entire KPACK split mode is therefore only a mitigation. The root cause is an invalid split artifact pairing for rocBLAS fallback Tensile databases and their architecture-specific fallback code objects.

This is also a concrete violation of the split-artifact convergence requirement that runtime metadata must be target-qualified, byte-identical and semantically valid for every paired target, or deterministically merged. See [kpack_convergence_requirements_summary.md](kpack_convergence_requirements_summary.md) for the distilled requirements and rocBLAS-specific repair criteria.

## Inputs Examined

Passing run:

* Run/job: `24588114306` / `71923706021`
* Head SHA: `be3d587091d259e2ee6f14d6f0d59e4f20ba363a`
* hipBLAS version: `3.4.0.a5c657c8`
* `rocm-libraries`: `64b5f8120f4163776de6ced376d349921deeaf1d`
* `rocm-systems`: `92b74318762433ef4b353489b4a1c5a79690b991`
* Relevant artifact: `blas_lib_gfx110X-all.tar.zst`
* hipBLAS result: `1676` tests passed. The job later exited non-zero from the wrapper, not from hipBLAS failures.

Failing run:

* Run/job: `24588821085` / `71926878687`
* Head SHA: `e159f867661b774e7889dda04d50833c6df93167`
* hipBLAS version: `3.4.0.a5c657c8`
* `rocm-libraries`: `64b5f8120f4163776de6ced376d349921deeaf1d`
* `rocm-systems`: `92b74318762433ef4b353489b4a1c5a79690b991`
* Relevant artifacts fetched by the test job:
  * `blas_lib_generic.tar.zst`
  * `blas_lib_gfx1100.tar.zst`
  * `blas_lib_gfx1101.tar.zst`
  * `blas_test_generic.tar.zst`
  * `blas_test_gfx1100.tar.zst`
  * `blas_test_gfx1101.tar.zst`
* hipBLAS result: `166` failed tests, all BLAS3 GEMM-family coverage reporting `HIPBLAS_STATUS_INTERNAL_ERROR`.

The TheRock diff between the two run heads is only:

```text
FLAGS.cmake
build_tools/github_actions/build_configure.py
```

That is consistent with PR #4652 flipping `KPACK_SPLIT_ARTIFACTS` on and adjusting non-multi-arch CI.

All downloaded tarballs used for this RCA matched their S3 `.sha256sum` sidecars.

## What Failed

The first visible failure in the failing hipBLAS log is:

```text
testing_gemm.hpp(385): error: Expected equality of these values:
  status_
    Which is: 6
  HIPBLAS_STATUS_SUCCESS
    Which is: 0
```

The first failed case is a double-precision GEMM on the AMD backend:

```text
function: gemm
a_type: 1, b_type: 1, c_type: 1, d_type: 1
M: 65, N: 33, K: 33
backend_flags: 3
```

hipBLAS maps `rocblas_status_internal_error` to `HIPBLAS_STATUS_INTERNAL_ERROR`. In rocBLAS, Tensile kernel launch returns `hipErrorNotFound` when `hipModuleGetFunction` cannot find a requested kernel name, and rocBLAS maps `hipErrorNotFound` to `rocblas_status_internal_error`.

Relevant source path:

* `rocm-libraries/projects/hipblas/library/src/amd_detail/hipblas.cpp`
* `rocm-libraries/projects/rocblas/library/src/tensile_host.cpp`
* `rocm-libraries/projects/rocblas/library/src/utility.cpp`
* `rocm-libraries/shared/tensile/Tensile/Source/lib/source/hip/HipSolutionAdapter.cpp`

## Eliminated Hypotheses

### Not a rocm-libraries change

Both logs check out the same `rocm-libraries` commit:

```text
64b5f8120f4163776de6ced376d349921deeaf1d
```

Both logs report the same hipBLAS version:

```text
hipBLAS version 3.4.0.a5c657c8
```

### Not a missing fetched gfx1101 split artifact

The failing test job fetched `blas_lib_generic`, `blas_lib_gfx1100`, and `blas_lib_gfx1101`. The final overlaid tree contains the expected rocBLAS `gfx1101` library files. Comparing the passing monolithic artifact against the failing `generic + gfx1101` overlay:

```text
rocBLAS gfx1101-named files:
  pass:       96
  fail:       96
  only_pass:   0
  only_fail:   0
  mismatched:  0

rocBLAS gfx1100-named files:
  pass:       96
  fail:       96
  only_pass:   0
  only_fail:   0
  mismatched:  0
```

This rejects the simple "CI did not fetch the right arch-specific artifact" explanation.

### Not a KPACK archive missing `rocblas.dll` code objects

The failing `blas_lib_gfx1101.kpack` contains `66` `rocblas.dll` code-object entries. Comparing the passing monolithic `rocblas.dll` embedded code objects with the failing `blas_lib_gfx1101.kpack` entries by index, architecture, size, and SHA-256 found:

```text
pass gfx1101 code objects: 66
kpack rocblas gfx1101 entries: 66
missing: 0
extra: 0
mismatch: 0
```

The KPACK debug log also shows successful lookup and loading from `blas_lib_gfx1101.kpack`; there are no KPACK `kernel not found`, `no compatible archive`, or `kpack_load_code_object failed` messages. The repeated missing `blas_lib_gfx11-generic.kpack` line is a generic fallback probe and is skipped after a valid `gfx1101` archive is found.

This rejects the deep KPACK fat-binary extraction/indexing hypothesis for `rocblas.dll` in this failure.

## Smoking Gun

rocBLAS lazy GEMM uses `TensileLibrary_lazy_gfx1101.dat`. That file is byte-identical between the passing monolithic artifact and the failing split overlay:

```text
0e5991075d500ec0c8dcef7b79c66927a30497cea8495a89495a682a02566e16
```

For the first failing double-precision GEMM, the lazy master database routes the problem to this fallback database:

```text
TensileLibrary_Type_DD_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback.dat
```

That fallback `.dat` lives in `blas_lib_generic`. Its paired code object lives in the gfx artifact:

```text
TensileLibrary_Type_DD_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback_gfx1101.hsaco
```

In the failing split overlay, the `.dat` asks for kernel names that are absent from the `.hsaco` symbol table. Example:

```text
Failing .dat solution name:
  Cijk_Ailk_Bjlk_DB_MT32x32x8_SN_AMAS0_BL0_BS1_GLVWA1_GLVWB1_GRVW1_GSU1_GSUASB_K1_LRVW1_NLCA1_NLCB1_PGR1_PLR1_SIA1_SU32_SUS256_SVW2_TT2_2_USFGRO0_VAW1_VS1_VW1_VWB1_WG16_16_1_WGM8

Actual gfx1101 .hsaco exported symbol:
  Cijk_Ailk_Bjlk_DB_MT32x32x8_SN_1LDSB0_AMAS0_BL0_BS1_EPS0_GLVWA1_GLVWB1_GRVW1_GSU1_GSUASB_ISA000_IU1_K1_KLS_LBSPPA0_LBSPPB0_LPA0_LPB0_LRVW1_MIAV0_MMFSC_NLCA1_NLCB1_PGR1_PLR1_SIA1_SS0_SU32_SUM0_SUS256_SVW2_TT2_2_TLDS0_UMLDSA0_UMLDSB0_USFGRO0_VAW1_VS1_VW1_VWB1_WSGRA0_WSGRB0_WS64_WG16_16_1_WGM8
```

The passing monolithic artifact does not have this mismatch:

```text
pass fallback .dat files checked:                  54
pass fallback .dat files with missing symbols:      0
pass unique Cijk solution names checked:          570
pass missing unique Cijk solution names:            0

fail fallback .dat files checked:                  54
fail fallback .dat files with missing symbols:     54
fail unique Cijk solution names checked:          570
fail missing unique Cijk solution names:          570
```

This is sufficient to explain the observed failure mode. Tensile lazy loading loads the `*_fallback_gfx1101.hsaco` file, then `HipSolutionAdapter::getKernel` calls `hipModuleGetFunction` with the kernel name from the `.dat`. Since the symbol is not exported by the loaded `.hsaco`, HIP returns `hipErrorNotFound`; rocBLAS converts that to `rocblas_status_internal_error`; hipBLAS converts it to `HIPBLAS_STATUS_INTERNAL_ERROR`.

## Causal Chain

1. PR #4652 enables `THEROCK_FLAG_KPACK_SPLIT_ARTIFACTS` for multi-arch CI.
2. The BLAS artifact switches from one monolithic `blas_lib_gfx110X-all` payload to split payloads: generic plus per-architecture artifacts.
3. The split classifier moves rocBLAS files with `gfx1101` in the filename into `blas_lib_gfx1101`, including `*_fallback_gfx1101.hsaco`.
4. The flat-layout fallback `.dat` files have no architecture in their filename, so they remain in `blas_lib_generic`.
5. In the failing artifacts, those generic fallback `.dat` files name compacted/short-form Tensile kernels, while the fetched gfx1101 fallback `.hsaco` files export long-form kernel symbols.
6. The final test overlay therefore pairs an incompatible fallback database with an otherwise present and valid gfx1101 code object.
7. BLAS3 GEMM-family tests that route through those fallback databases fail with `HIPBLAS_STATUS_INTERNAL_ERROR`.

## Why This Is Not Fixed by the Requested Revert as an RCA

Reverting `KPACK_SPLIT_ARTIFACTS` avoids the bad split overlay by returning to a monolithic artifact where the fallback `.dat` and fallback `.hsaco` files are packaged together and match. That does not identify or repair the underlying invalid artifact pairing.

The actionable defect is narrower:

```text
flat rocBLAS Tensile fallback databases are being shipped in the generic split artifact,
while their architecture-specific fallback hsaco companions are shipped in per-arch artifacts;
the failing generic databases do not match the fetched per-arch hsaco symbols.
```

## Recommended Fixes

1. Add a post-split verifier for rocBLAS and hipBLASLt Tensile artifacts.

   For each fetched architecture:

   * unpack each `TensileLibrary_*_fallback.dat` MessagePack database;
   * collect solution `name` entries;
   * inspect the matching `TensileLibrary_*_fallback_<arch>.hsaco` symbol table;
   * fail the build if any solution name is absent from the code object.

   This would have caught the failing artifacts before upload.

   This verifier is the practical enforcement mechanism for the rocBLAS/Tensile convergence requirements summarized in [kpack_convergence_requirements_summary.md](kpack_convergence_requirements_summary.md).

2. Stop treating flat-layout rocBLAS fallback `.dat` files as safely generic unless verified against every emitted architecture.

   Safer options:

   * move to the per-arch `rocblas/library/<arch>/...` layout, which `tensile_host.cpp` already probes before the flat layout;
   * or package each fallback `.dat` with the matching arch-specific fallback `.hsaco` and avoid cross-arch overlay conflicts;
   * or ensure the generated generic fallback `.dat` uses exactly the same kernel naming scheme as every paired per-arch fallback `.hsaco`.

3. Keep KPACK archive validation separate from Tensile database validation.

   The `rocblas.dll` KPACK contents are complete for gfx1101 in this case. The failure is after KPACK, in Tensile lazy external code-object lookup.

## Reproduction Notes

Artifacts and derived comparisons were cached outside the repository under:

```text
/tmp/therock-rca-artifacts
```

The key local checks were:

```text
# Verify downloaded artifacts against sidecars.
sha256sum <artifact>

# Compare pass/fail rocBLAS gfx1101 files.
pass: /tmp/therock-rca-artifacts/extract/pass_blas_lib
fail: /tmp/therock-rca-artifacts/extract/fail_blas_lib_overlay

# Compare fallback .dat solution names with hsaco symbols.
python + msgpack for .dat
llvm-readelf -Ws for .hsaco
```

The definitive check is the `54/54` failing fallback database mismatch count above.

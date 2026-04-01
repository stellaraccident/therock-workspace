# PR 3928 - Remaining Issues (2026-03-31)

## Status

Compiler-runtime stage working well (11m42s, 98.67% cache hit, 2.34x speedup
on critical path vs main's ~27m). Most topology and dependency issues resolved.
One remaining build failure in profiler-apps.

## Open: rocprofiler-systems Fortran module files not found

**Error**: flang can't find `iso_fortran_env.mod` and `omp_lib.mod` when
building Fortran examples (openmp-vv, jacobi-fortran-usm, etc.) inside
rocprofiler-systems.

**Root cause (suspected)**: rocprofiler-systems is not being configured
properly for Fortran. The amd-llvm-offload dist tree has everything needed
(flang-rt, .mod files, etc.), but the project's Fortran configuration
likely isn't pointing at the right tree. This project has Fortran OpenMP VV
tests and examples baked in — needs a local build to debug the exact
module search path issue.

**Where to look**:
- `profiler/pre_hook_rocprofiler-systems.cmake` — FORTRAN_OPTIONAL handling
- `cmake/therock_subproject.cmake` — how FORTRAN_OPTIONAL wires up the
  Fortran compiler and module paths (look at `_fortran_toolchain_subproject`)
- The amd-llvm-offload dist tree layout — where do .mod files actually land?
- `ROCPROFSYS_DISABLE_EXAMPLES` in the pre_hook — could just disable the
  Fortran examples if the project won't fix their setup

**CI run**: https://github.com/ROCm/TheRock/actions/runs/23830840446/job/69464750294?pr=3928

## Resolved in this session

1. **OpenMP::OpenMP_CXX not found** (marbre report) — removed bad
   `provide_package` for OpenMP that hijacked CMake's built-in FindOpenMP
2. **Foundation stage elimination** — merged into compiler-runtime, 2.34x
   critical path speedup
3. **amd-llvm-flang AMDGPU targets** — added DISABLE_AMDGPU_TARGETS (host-only)
4. **hip-tests/rocrtst in wrong stages** — created runtime-tests stage,
   gated on proper feature flags
5. **rocprofiler-systems missing libomptarget** — added amd-llvm-offload dep
   in topology + CMake, passed offload lib dir via cache arg
6. **rocrtst missing amdsmi** — added core-amdsmi to artifact deps

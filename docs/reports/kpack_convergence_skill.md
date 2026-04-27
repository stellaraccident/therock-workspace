# KPACK Convergence Analysis Skill

Date: 2026-04-26

Related reports:

* [kpack_convergence_requirements_summary.md](kpack_convergence_requirements_summary.md)
* [2026-04-26-kpack-split-windows-gfx110x-hipblas-rca.md](2026-04-26-kpack-split-windows-gfx110x-hipblas-rca.md)

## Goal

Use this playbook to analyze KPACK split-artifact failures, especially BLAS
failures where hipBLAS, rocBLAS, hipBLASLt, Tensile, or KPACK may be in the
causal path.

The standard is a concrete causal chain, not "disable split artifacts" and not
"Windows is flaky." BLAS status codes are broad and often report the symptom
after a lower-level lookup, packaging, database, or code-object failure.

## Working Assumptions

1. A missing GEMM kernel can originate in rocBLAS, hipBLASLt, Tensile metadata,
   external `.hsaco` files, or embedded code objects extracted through KPACK.
2. KPACK may be correct while Tensile lazy loading is wrong.
3. A fetched architecture artifact may be present while still being paired with
   incompatible generic metadata.
4. Runtime BLAS errors may be delayed. The first visible `HIPBLAS_STATUS_*` or
   `rocblas_status_*` code is usually not the root cause.
5. Artifact fetching is expensive. Download once, verify checksums, and do all
   follow-up inspection from a local cache.

## Inputs to Collect

For each passing and failing run, record:

* GitHub Actions run and job ids.
* TheRock head SHA.
* `rocm-libraries`, `rocm-systems`, and any other submodule SHAs.
* hipBLAS, rocBLAS, hipBLASLt, and Tensile versions if logged.
* Relevant flags, especially `THEROCK_FLAG_KPACK_SPLIT_ARTIFACTS` and target
  lists such as `THEROCK_AMDGPU_TARGETS`.
* Full artifact names downloaded by the test job.
* Test target architecture and runner OS.
* First failing test case, not only the aggregate failure count.
* Raw logs with KPACK debug enabled when available.

A good control pair has the same `rocm-libraries` commit and differs only in the
split-artifact flag or packaging path.

## Local Cache Layout

Use a disposable directory outside the repository, for example:

```text
/tmp/therock-rca-artifacts/
  artifacts/
  logs/
  extract/
  inventories/
  scripts/
```

Download each artifact and its checksum sidecar once. Verify before extracting:

```bash
sha256sum -c artifact.tar.zst.sha256sum
```

Keep monolithic passing artifacts separate from failing split overlays:

```text
extract/pass_blas_lib/
extract/fail_blas_lib_generic/
extract/fail_blas_lib_gfx1101/
extract/fail_blas_lib_overlay/
```

The overlay directory should be built by extracting generic first and then the
target-specific artifact under test.

## Initial Triage

1. Establish the control.

   Confirm whether the passing and failing runs use the same BLAS source SHAs.
   If the BLAS submodule changed, split packaging may still be involved, but it
   is no longer a clean control.

2. Diff the TheRock heads.

   For a split-artifact RCA, verify that the meaningful diff is limited to
   flags or packaging logic. In the gfx110X failure, the diff was limited to
   `FLAGS.cmake` and `build_tools/github_actions/build_configure.py`.

3. Confirm artifact fetch behavior.

   Do not assume the right architecture artifact was fetched. Inspect the log
   and the extracted tree. Count expected target-named files in the final
   overlay.

4. Find the first failing BLAS case.

   Capture the first function, datatype, dimensions, backend flags, and status
   code. The first failing GEMM often points to the first broken Tensile path.

## KPACK Log Analysis

Enable or locate logs with KPACK debug output. Search for:

```bash
rg -n "KPACK|kpack|HIPK|code object|archive|lookup|kernel not found|load" logs/
```

Important KPACK log events:

* HIPK metadata parsing from the host library.
* Extracted code object index, architecture, and lookup key.
* Expanded KPACK archive search patterns.
* Missing generic archive probes.
* Compatible architecture archive found.
* Code object loaded successfully.
* Explicit `kernel not found`, `no compatible archive`, or load failure.

A missing generic archive probe is not automatically fatal. For example, a
failed search for `blas_lib_gfx11-generic.kpack` can be benign if KPACK then
finds and loads `blas_lib_gfx1101.kpack`.

When KPACK is healthy, prove it by comparing embedded code objects from the
passing monolithic host binary with entries in the split `.kpack` archive by:

* code object index;
* target architecture;
* byte size;
* SHA-256.

If these match and the debug log shows successful loads, move on to external
Tensile or hipBLASLt databases.

## BLAS Causal Path

For hipBLAS on the AMD backend, the common path is:

```text
hipBLAS test
  -> hipBLAS AMD backend wrapper
  -> rocBLAS
  -> Tensile host dispatch
  -> Tensile HIP solution adapter
  -> HIP module/function lookup
```

Useful source paths:

```text
rocm-libraries/projects/hipblas/library/src/amd_detail/hipblas.cpp
rocm-libraries/projects/rocblas/library/src/tensile_host.cpp
rocm-libraries/projects/rocblas/library/src/utility.cpp
rocm-libraries/shared/tensile/Tensile/Source/lib/source/hip/HipSolutionAdapter.cpp
```

`hipErrorNotFound` from `hipModuleGetFunction` can become
`rocblas_status_internal_error`, which hipBLAS then reports as
`HIPBLAS_STATUS_INTERNAL_ERROR`. That status does not distinguish a missing
symbol from a higher-level math failure.

## rocBLAS/Tensile Layout Checks

Inspect `lib/rocblas/library` in the final overlay and compare against the
passing artifact.

Important file families:

```text
TensileLibrary_lazy_<arch>.dat
TensileLibrary_*_<arch>.dat
TensileLibrary_*_<arch>.co
TensileLibrary_*_fallback.dat
TensileLibrary_*_fallback_<arch>.hsaco
TensileManifest.txt
```

The most important split-risk pattern is:

```text
generic artifact:
  TensileLibrary_Type_..._fallback.dat

architecture artifact:
  TensileLibrary_Type_..._fallback_<arch>.hsaco
```

This is only valid if the generic `.dat` names exactly the symbols exported by
the architecture-specific `.hsaco`.

Use MessagePack parsing for `.dat` files and LLVM symbol inspection for
`.hsaco` files:

```bash
python - <<'PY'
from pathlib import Path
import msgpack

def collect_names(obj, out):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "name" and isinstance(value, str):
                out.add(value)
            collect_names(value, out)
    elif isinstance(obj, list):
        for value in obj:
            collect_names(value, out)

path = Path("lib/rocblas/library/TensileLibrary_Type_DD_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback.dat")
data = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
names = set()
collect_names(data, names)
for name in sorted(names):
    if name.startswith("Cijk_"):
        print(name)
PY

llvm-readelf -Ws lib/rocblas/library/TensileLibrary_Type_DD_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback_gfx1101.hsaco
```

A definitive mismatch looks like:

```text
database asks for:
  Cijk_Ailk_Bjlk_DB_MT32x32x8_SN_AMAS0_...

hsaco exports:
  Cijk_Ailk_Bjlk_DB_MT32x32x8_SN_1LDSB0_AMAS0_...
```

Do not stop at one example. Count all fallback databases and all unique solution
names, then report pass/fail totals.

## hipBLASLt/Tensile Layout Checks

hipBLASLt has a similar Tensile path plus additional extop metadata. Inspect:

```text
lib/hipblaslt/library/
lib/hipblaslt/library/<arch>/
hipblasltExtOpLibrary.dat
TensileLibrary.dat or lazy equivalents
*.co
*.hsaco
```

Risk patterns:

* target-varying metadata installed to a fixed filename;
* per-architecture code objects with a non-merged master catalog;
* databases in generic artifacts that reference architecture-specific files;
* last-writer-wins overlay of fixed metadata files.

For hipBLASLt, apply the same rule as rocBLAS: every runtime database entry must
resolve to the corresponding overlaid code object, symbol, and target.

## Convergence Checks

Run these checks before forming conclusions:

1. File inventory comparison.

   Compare monolithic passing artifact vs generic plus target overlay. For each
   relevant target, count target-named files and mismatched hashes.

2. KPACK archive check.

   Compare embedded host-library code objects against split `.kpack` entries.
   If the index, arch, size, and hash match, KPACK extraction is probably not
   the failing layer for that library.

3. Metadata-to-code check.

   Parse runtime databases and manifests. Verify referenced files and exported
   symbols exist in the final overlay.

4. Cross-target generic check.

   Any file in the generic artifact that influences runtime dispatch must be
   valid for every architecture artifact that may be paired with it.

5. Overlay conflict check.

   Identify fixed-path files that differ by shard. These require renaming,
   merging, or proof that runtime never consumes them.

## Useful Commands

Find relevant files:

```bash
rg --files extract/fail_blas_lib_overlay | rg 'rocblas/library|hipblaslt/library|\\.kpack$'
```

Count target-named rocBLAS files:

```bash
rg --files extract/fail_blas_lib_overlay/lib/rocblas/library | rg 'gfx1101' | wc -l
```

Hash and compare file sets:

```bash
find extract/pass_blas_lib/lib/rocblas/library -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum > inventories/pass_rocblas.sha256

find extract/fail_blas_lib_overlay/lib/rocblas/library -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum > inventories/fail_rocblas.sha256
```

Inspect symbols:

```bash
llvm-readelf -Ws path/to/file.hsaco | rg 'Cijk_|Dijk_|Tensile'
```

Search source for status mapping:

```bash
rg -n "hipErrorNotFound|rocblas_status_internal_error|HIPBLAS_STATUS_INTERNAL_ERROR|hipModuleGetFunction" sources/TheRock
```

## Smoking Gun Standard

A strong RCA should include:

* passing and failing run ids;
* source and submodule SHAs proving the control;
* exact artifacts fetched by the failing test job;
* checksum verification for downloaded artifacts;
* first failing test case and status;
* rejected hypotheses with evidence;
* exact runtime database or manifest entry that fails;
* exact missing file or symbol;
* pass/fail aggregate counts proving the mismatch is systematic;
* a causal chain from packaging decision to runtime status code;
* a narrow repair recommendation that preserves split artifacts.

For BLAS, "166 GEMM failures" is not a root cause. A root cause looks like:

```text
TensileLibrary_Type_DD_..._fallback.dat from the generic artifact names 570
solution kernels that are absent from the paired gfx1101 fallback hsaco; the
passing monolithic artifact has zero missing names for the same database family.
```

## Repair Guidance

Prefer repairs that enforce convergence directly:

* target-qualify shard-specific metadata;
* merge metadata deterministically after per-target generation;
* keep metadata packaged with the code objects it indexes;
* add artifact-level validators for KPACK archives and Tensile databases;
* fail CI before upload when generic metadata is not valid for every target
  artifact it can be paired with.

Avoid repairs that only suppress the symptom:

* disabling all split artifacts for a library without identifying the invalid
  file boundary;
* assuming all `.dat` or manifest files are generic;
* relying on downstream BLAS tests to discover packaging violations;
* treating a present architecture artifact as proof that runtime lookup can
  succeed.

## Report Template

Use this structure for future RCAs:

```text
# RCA: <platform/target/library failure after split artifacts>

## Summary
One paragraph with the root cause and why broad revert is only mitigation.

## Inputs Examined
Run ids, SHAs, artifacts, checksums, versions, flags.

## What Failed
First failing test and status mapping.

## Eliminated Hypotheses
Source changes, missing artifacts, KPACK archive missing code, test harness bugs.

## Smoking Gun
Exact metadata-to-code mismatch, with pass/fail counts.

## Causal Chain
Numbered path from build flag to runtime status.

## Recommended Fixes
Narrow repair plus CI verifier.

## Reproduction Notes
Local cache paths and scripts or commands.
```

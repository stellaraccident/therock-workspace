# KPACK Split Artifact Convergence Requirements Summary

Date: 2026-04-26

Source material:

* Multi-Architecture Convergence Static Analysis Report: https://gist.github.com/stellaraccident/d728d1ddd35574a93fc020716b49f099
* Related RCA: [2026-04-26-kpack-split-windows-gfx110x-hipblas-rca.md](2026-04-26-kpack-split-windows-gfx110x-hipblas-rca.md)

## Purpose

This document distills the convergence requirements that matter for KPACK split
artifacts, with special focus on rocBLAS and Tensile-generated metadata. The
goal is to turn the prior convergence analysis into testable repair criteria
for the gfx110X hipBLAS failure.

The prior convergence analysis identified `H2: Tensile Library Metadata -
Shard-Specific Catalog` for rocBLAS and hipBLASLt. The gfx110X RCA is a
concrete instance of that class of defect: generic Tensile fallback metadata was
paired with architecture-specific fallback code objects whose exported symbols
did not match the metadata.

## Requirements Definition

A shard-specific subproject is convergent when independently built target shards
can be overlaid into a complete, correct install tree.

For KPACK split artifacts, convergence requires all of the following:

1. Host-side outputs are invariant across shards.

   Host libraries, headers, CMake package files, import libraries, and other
   non-device runtime files must be identical across shards unless the
   differences are confined to extractable device code sections.

2. Target-specific device outputs are additive.

   Device code, code object files, kernel archives, and generated per-target
   databases must have an architecture-qualified path or filename, or otherwise
   be placed in an architecture-qualified package. Overlaying shards must union
   these files without overwriting a different shard's output.

3. Metadata is either invariant, target-qualified, or mergeable.

   Runtime catalogs, manifests, MessagePack databases, YAML databases, SQLite
   caches, and generated lookup tables are not safe just because they are not
   code objects. If their content varies by target, they must either:

   * include the target in the path or filename;
   * be byte-identical and semantically valid for all targets; or
   * have a deterministic merge step that preserves every target's entries.

4. Runtime lookup contracts must be validated, not inferred.

   It is insufficient for an architecture-specific artifact to contain a code
   object and for the generic artifact to contain a database. The database must
   name symbols and files that are actually present in the overlaid install tree
   for that architecture.

5. CMake-time target branching must not change host behavior.

   `GPU_TARGETS`, `AMDGPU_TARGETS`, or `THEROCK_AMDGPU_TARGETS` may be passed
   through to device compilation, but they must not alter host source
   selection, host compile definitions, host linking, install layout, or fixed
   output file content in a shard-specific way.

## Algorithm for Determining Convergence

Use this algorithm for every subproject included in a split artifact.

1. Identify the shard boundary.

   Find where `THEROCK_AMDGPU_TARGETS`, `AMDGPU_TARGETS`, `GPU_TARGETS`, or
   equivalent variables enter the subproject. Record whether each invocation is
   a pass-through to a device compiler, an iteration over targets, or a
   conditional controlling host build behavior.

2. Inventory installed outputs.

   Build or inspect the artifact output tree. Classify each file as:

   * host-side invariant file;
   * target-qualified device code;
   * embedded device code inside a host binary;
   * runtime metadata or database;
   * test-only output;
   * packaging manifest or KPACK archive.

3. Classify target usage.

   The prior convergence analysis used these effective classes:

   * Pass-through target list to a device compiler: normally safe.
   * Iteration over targets producing target-named code objects: normally safe.
   * Conditional compile definitions on library code: high risk.
   * Conditional linking or source selection: high risk.
   * Fixed output filename with target-varying content: critical.
   * Target-named output filename with target-varying content: normally safe.

4. Compare overlay semantics.

   For each shard-specific output, determine what happens when the generic and
   per-architecture artifacts are extracted into one tree:

   * identical file, no conflict;
   * additive file, no conflict;
   * last-writer-wins overwrite;
   * missing generic dependency;
   * generic metadata paired with per-target code from another shard.

5. Validate runtime lookup.

   For generated metadata, parse the metadata and prove that every referenced
   code object, symbol, database, or manifest entry resolves in the final
   overlaid tree. File presence alone is not enough.

6. Make the convergence property part of CI.

   Convergence checks must run on the produced split artifacts before upload or
   before accepting the artifact set for downstream tests. A runtime test that
   later fails with a generic numeric BLAS status is too late and too indirect.

## Specific rocBLAS Prescriptions

rocBLAS is a high-risk convergence target because it delegates GEMM solution
generation and runtime dispatch to Tensile. Tensile produces device code plus
runtime metadata. Both are part of the runtime contract.

The important installed file classes are:

* `TensileLibrary_lazy_<arch>.dat`
* `TensileLibrary_*_<arch>.dat`
* `TensileLibrary_*_<arch>.co`
* `TensileLibrary_*_fallback.dat`
* `TensileLibrary_*_fallback_<arch>.hsaco`
* `TensileManifest.txt`
* embedded rocBLAS device code later extracted into `blas_lib_<arch>.kpack`

The flat-layout fallback pair is the dangerous case:

```text
TensileLibrary_Type_..._fallback.dat
TensileLibrary_Type_..._fallback_<arch>.hsaco
```

The fallback `.dat` has no target in its filename, so a split classifier treats
it as generic. The fallback `.hsaco` is target-specific, so it lands in the
architecture artifact. That layout is convergent only if the generic `.dat` is
semantically compatible with every paired architecture-specific `.hsaco`.

The required rocBLAS verifier is:

1. For each target architecture in the artifact set, create the final overlay:
   generic plus that architecture's artifact.
2. Parse every `TensileLibrary_*_fallback.dat` MessagePack database in
   `lib/rocblas/library`.
3. Collect every solution `name` entry that the database may request at runtime.
4. Locate the matching `TensileLibrary_*_fallback_<arch>.hsaco`.
5. Inspect the `.hsaco` dynamic symbol table with `llvm-readelf -Ws` or an
   equivalent LLVM tool from the same ROCm toolchain.
6. Fail the artifact if any solution name from the database is absent from the
   code object.
7. Run the same style of check for per-architecture `.dat` or `.co` files when
   a database names external code or symbols.

The repair should not rely on the generic artifact boundary unless it is proven
valid. Safer rocBLAS repair options are:

* move fallback databases into a target-qualified layout, such as
  `lib/rocblas/library/<arch>/`, with their matching `.hsaco` files;
* package fallback `.dat` files with the matching architecture-specific
  fallback `.hsaco` files;
* make the fallback `.dat` byte-identical and symbol-compatible for all
  target-specific fallback `.hsaco` files;
* add a deterministic post-build merge that produces one complete metadata file
  covering all emitted architectures.

`TensileManifest.txt` should be treated as runtime metadata unless proven
otherwise. If it varies by shard, it must be target-qualified, invariant, or
merged. If runtime discovery does not consult it, CI should still verify that it
does not hide a split packaging error.

KPACK archive validation is necessary but not sufficient for rocBLAS. In the
gfx110X hipBLAS failure, `blas_lib_gfx1101.kpack` contained the expected
`rocblas.dll` embedded code objects. The failure was in Tensile lazy external
code-object lookup after KPACK had already done its job.

## Evidence This Was Not Satisfied

The related RCA found the concrete violation for Windows gfx110X:

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

That is not a missing fetched architecture artifact and not a missing
`rocblas.dll` KPACK code object. It is a non-convergent rocBLAS/Tensile metadata
and code-object pairing.

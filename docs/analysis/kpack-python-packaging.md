# KPACK: Python Packaging Pipeline Failure Analysis

**Date:** 2026-03-31
**CI Run:** ROCm/TheRock #4257, run 23775619092
**Status:** Tabled — feature work, owner on vacation. Not blocking kpack enablement.

## Symptom

```
FileNotFoundError: No family subdirectories found in .../packages/dist.
generate_multiarch_indexes requires a multi-arch dist layout.
```

Both platforms. Call chain: `upload_python_packages.py` →
`generate_index(multiarch=True)` → `generate_multiarch_indexes()` → raises
because `dist/` is flat (no per-family subdirectories).

## Background: Current Pipeline (kpack OFF)

Four phases: **fetch → populate → wheel build → upload/index**.

### Fetch

`artifact_manager.py fetch` constructs candidate filenames by iterating
`artifact_names × target_families × components × extensions`, producing names
like `blas_lib_gfx94X-dcgpu.tar.zst`. Matches against S3 listing.

After fetch, artifacts dir looks like:
```
artifacts/
  blas_lib_gfx94X-dcgpu/
  blas_lib_gfx120X-all/
  core-hip_lib_generic/
  ...
```

### Populate

`ArtifactCatalog` scans artifact dirs. `ArtifactName.from_path()` parses
`{name}_{component}_{target_family}`. `all_target_families` collects unique
families (excluding `"generic"`). When `len > 1`, sets `multi_arch = True`.

Produces:
- `rocm-sdk-core` wheel (target-neutral) → `dist/`
- `rocm-sdk-libraries-{family}` wheel per family → `dist/`
- `rocm` meta sdist per family → `dist/{family}/`
- `rocm-sdk-devel-{family}` per family → `dist/{family}/`

### Upload/Index

`--multiarch` is hardcoded true in the workflow. `generate_multiarch_indexes()`
iterates subdirs of `dist/`, generates `index.html` per family. Crashes if no
subdirs exist.

## What KPACK_SPLIT_ARTIFACTS Changes

When the flag is ON, `therock_provide_artifact` runs `split_artifacts.py` on
each target-specific artifact. For input `blas_lib_gfx94X-dcgpu/`:

```
artifacts/
  blas_lib_generic/          # host-only binaries, kpack manifests
  blas_lib_gfx942/           # per-ISA kpack archives
  blas_lib_gfx940/           # (if multiple ISAs in family)
```

The family-level artifact (`blas_lib_gfx94X-dcgpu`) no longer exists.

## Chain of Breakage

### 1. Artifact fetch cannot find split artifacts

`find_available_artifacts()` looks for `blas_lib_gfx94X-dcgpu.tar.zst`. That
no longer exists in S3. It finds `blas_lib_generic.tar.zst` (always searched)
but NOT `blas_lib_gfx942.tar.zst` because it searches family names, not
individual ISA names.

**Result:** Only generic (host-only) artifacts fetched. All device code missing.

### 2. ArtifactCatalog sees no target families

With only `_generic` artifacts, `all_target_families` returns empty (filters out
`"generic"`). `multi_arch = False`. No libraries packages created. Meta/devel
packages go flat to `dist/`.

### 3. Upload script crashes

`--multiarch` hardcoded but `dist/` has no subdirectories → `FileNotFoundError`.

## Known Context

The Python wheel kpack integration was known to be unimplemented. The person
working on it went on vacation as of ~2026-03-24. The kpack-build-integration
design doc (`rocm-systems/shared/kpack/docs/kpack-build-integration.md`) has a
"Python Wheel Splitting" section (lines ~450-607) that describes the future
direction but is still a strawman.

## Plan

### Option A: Recombine-then-package (recommended, least disruptive)

Add a recombination step between fetch and package build. This is the "reduce
phase" from the kpack architecture — it was always planned but never wired into
CI.

1. **`artifact_manager.py fetch`**: Use prefix matching against S3 listing
   instead of exact name construction. When looking for artifact `blas` with
   component `lib` and family `gfx94X-dcgpu`, also match `blas_lib_gfx94*`.
   The `available` set from `backend.list_artifacts()` is already fetched.

2. **Add recombination step in workflow**: After fetch, run a script that
   reorganizes `blas_lib_generic/` + `blas_lib_gfx942/` + `blas_lib_gfx940/`
   into a virtual `blas_lib_gfx94X-dcgpu/` directory that the packaging
   pipeline expects. `recombine_artifacts.py` already exists in
   `rocm-systems/shared/kpack/python/rocm_kpack/tools/`.

3. **Safety check in upload**: Don't crash on missing subdirs when
   `--multiarch`. Produce a diagnostic instead.

### Option B: Teach packaging about ISAs (more invasive)

Update `build_python_packages.py` and `ArtifactCatalog` to understand
ISA-to-family mapping. More work, more places to break, but the "right" long
term solution for proper per-ISA wheel splitting.

### Recommendation

**Option A now, Option B later.** Option A unblocks CI within a day or two.
Option B is the eventual direction described in the kpack design doc but
requires more design work (wheel naming, metadata, the strawman questions).

## Key Files

| File | What needs to change |
|---|---|
| `build_tools/artifact_manager.py` | `find_available_artifacts()` — prefix matching for split names |
| `build_tools/build_python_packages.py` | Accept family mapping or recombined layout |
| `build_tools/_therock_utils/artifacts.py` | `ArtifactCatalog`, `ArtifactName` — family awareness |
| `build_tools/github_actions/upload_python_packages.py` | Defensive `--multiarch` handling |
| `.github/workflows/build_portable_linux_python_packages.yml` | Add recombine step, pass `amdgpu_families` |
| `.github/workflows/build_windows_python_packages.yml` | Same changes for Windows |
| `rocm-systems/shared/kpack/python/rocm_kpack/tools/recombine_artifacts.py` | May need adaptation for this use case |

## Open Questions

1. **Where should ISA-to-family mapping live?** BUILD_TOPOLOGY.toml, CLI args,
   or derived from S3 listing?
2. **Should `recombine_artifacts.py` produce the old family-level layout or
   something new?** Old layout is simplest.
3. **Windows parity:** Same fix needed for `build_windows_python_packages.yml`.

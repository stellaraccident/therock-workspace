# KPACK: Python Packaging for Per-ISA Device Wheels

**Date:** 2026-04-01
**Supersedes:** Previous analysis from 2026-03-31 (recombine approach abandoned)
**Context:** [kpack-wheelnext-proposal.md](kpack-wheelnext-proposal.md) — full vision doc

## Goal

Produce per-ISA device wheels (`rocm-sdk-device-gfx942`, etc.) from kpack-split
CI artifacts alongside an arch-neutral host `rocm-sdk-libraries` wheel. This is
a scoped half step toward the wheelnext vision — no variant labeling, no unified
index, no torch splitting. Just get the host/device wheel split working locally
from real CI artifacts.

## Current State

### What the CI produces (kpack-split enabled)

Artifacts are per-ISA, not per-family:

```
artifacts/
  blas_lib_generic/           # host .so files (no device code)
  blas_lib_gfx942/            # .kpack + kernel DBs for gfx942
  blas_lib_gfx1100/           # .kpack + kernel DBs for gfx1100
  fft_lib_generic/            # host .so files
  fft_lib_gfx942/             # .kpack for gfx942
  ...
```

Per-ISA artifacts contain:
- `.kpack` archives at `{component}/stage/.kpack/{artifact}_{target}.kpack`
- Kernel databases (Tensile `.co`/`.dat`/`.hsaco`, MIOpen `.kdb`/`.db.txt`)
- Only the components that have device code (subset of generic)

Generic artifacts contain:
- Host shared libraries (`.so` files)
- No `.kpack` files, no ISA-specific content

### What `build_python_packages.py` expects

The script calls `ArtifactCatalog` which discovers target families from artifact
directory names matching `{name}_{component}_{target_family}`. It then builds:

1. `rocm-sdk-core` — from core artifacts (always generic)
2. `rocm-sdk-libraries-{family}` — from library artifacts matching a family name
3. `rocm` — meta sdist (one per family in multi-arch)
4. `rocm-sdk-devel` — headers/cmake (one per family)

**Problem:** With kpack-split artifacts, `all_target_families` returns naked ISA
names (`gfx942`, `gfx1100`, ...) instead of family names (`gfx94X-dcgpu`,
`gfx120X-all`). The `libraries_artifact_filter` matches `an.target_family ==
target_family`, so it correctly picks up per-ISA artifacts. But the downstream
packaging treats each ISA as a "family" and produces wheels like
`rocm-sdk-libraries-gfx942` — a fat wheel containing both host AND device code.

**What we want instead:** An arch-neutral `rocm-sdk-libraries` host wheel, plus
thin `rocm-sdk-device-{target}` device wheels per ISA.

## Implementation Plan

### Step 1: New `rocm-sdk-device` wheel template

Create `build_tools/packaging/python/templates/rocm-sdk-device/`.

This is a minimal wheel template. The device wheel's only job is to install
`.kpack` archives and kernel database files into the correct overlay location
so the kpack runtime can find them relative to the host libraries.

**Key design decision — overlay target:** Device files must land in
`site-packages/_rocm_sdk_libraries_{nonce}/` so the kpack runtime (which
resolves `.kpack` paths relative to the host binary) finds them. This means
the device wheel's `platform/` directory mirrors the host libraries wheel's
platform directory name.

Files needed:
- `pyproject.toml` — minimal setuptools config
- `setup.py` — reads `_dist_info.py` for package name, version, and the
  host package's `py_package_name` (overlay target)

The `setup.py` must:
- Set `name` to `rocm-sdk-device-{target}` (from `_dist_info.py`)
- Set `version` to match the host libraries version exactly
- Declare `Requires-Dist: rocm-sdk-libraries == {version}`
- Use `package_dir` pointing at the overlay platform directory

### Step 2: Register `device` logical package in `_dist_info.py`

Add a new `PackageEntry` to `ALL_PACKAGES`:

```python
PackageEntry(
    "device",
    "rocm-sdk-device-{target_family}",
    template_directory="rocm-sdk-device",
    is_target_specific=True,
    required=False,
)
```

This follows the existing pattern for `libraries`. The `{target_family}` in
the dist name template gets resolved with the naked ISA name (e.g. `gfx942`).

### Step 3: Add device artifact filter

In `build_python_packages.py`, add a filter that selects only per-ISA artifacts
(i.e. where `target_family != "generic"`):

```python
def device_artifact_filter(target: str, an: ArtifactName) -> bool:
    return (
        an.name in ["blas", "fft", "hipdnn", "miopen", "miopenprovider",
                     "hipblasltprovider", "rand", "rccl"]
        and an.component == "lib"
        and an.target_family == target
    )
```

This is the same library list as `libraries_artifact_filter` but without the
`or an.target_family == "generic"` clause — device wheels only want per-ISA
content.

### Step 4: Modify `libraries_artifact_filter` to be generic-only

When kpack-split artifacts are present, the host `rocm-sdk-libraries` wheel
should contain only generic artifacts:

```python
def libraries_artifact_filter(target_family: str, an: ArtifactName) -> bool:
    return (
        an.name in [...]
        and an.component == "lib"
        and an.target_family == "generic"  # <-- was: target_family or "generic"
    )
```

Since the host wheel is now arch-neutral, it no longer takes a `target_family`
parameter. A single `rocm-sdk-libraries` wheel is produced (no family suffix).

### Step 5: Modify `run()` to produce host + device wheels

The main orchestration in `build_python_packages.py` changes from:

```
for each target_family:
    build rocm-sdk-libraries-{family}  (host + device combined)
```

to:

```
build rocm-sdk-libraries              (host only, generic artifacts)
for each target (gfx942, gfx1100, ...):
    build rocm-sdk-device-{target}    (device only, per-ISA artifacts)
```

**Detecting kpack-split mode:** The `base_lib_generic` artifact contains
`base/aux-overlay/stage/share/therock/therock_manifest.json` with a `flags`
dict. When `flags.KPACK_SPLIT_ARTIFACTS` is `true`, we're in kpack-split mode.
The companion `dist_info.json` in the same directory provides the explicit
`dist_amdgpu_targets` list (semicolon-separated ISA names the build was
configured for). This is authoritative — no heuristics needed.

The flag is transitional. Once kpack-split is the only mode, the flag check
and legacy branch get deleted.

**Branching strategy:** One `if kpack_split:` branch in `run()`. No forking
of templates, no forking of py_packaging.py. The `rocm-sdk-device` template
is purely additive.

In kpack-split mode:
1. Build `rocm-sdk-libraries` once with only generic library artifacts
2. For each ISA target, build `rocm-sdk-device-{target}` with per-ISA artifacts
3. Build `rocm` meta sdist (needs rethinking — see open questions)
4. Build `rocm-sdk-devel` once (headers are arch-neutral)

In legacy mode (no kpack split): behavior unchanged.

### Step 6: `PopulatedDistPackage` for device wheels

The device wheel population needs a `populate_device_files()` method (or reuse
`populate_runtime_files` with appropriate filtering). Device artifacts contain:

1. **`.kpack` archives** — binary kernel archives
2. **Kernel databases** — `.co`, `.dat`, `.hsaco`, `.kdb`, `.db.txt`, etc.
3. **ML model files** — MIOpen tuning data

All of these should be copied as-is into the wheel's platform directory. No
RPATH patching, no soname resolution, no symlink chasing. The existing
`populate_runtime_files()` is designed for shared libraries — device files need
simpler handling (straight copy, preserving directory structure).

Add a `populate_device_files()` method to `PopulatedDistPackage` that does a
plain recursive copy of all files from matching artifacts into the platform dir.

### Step 7: Output layout

```
packages/
  dist/
    rocm_sdk_core-7.13.0-py3-none-manylinux_2_28_x86_64.whl
    rocm_sdk_libraries-7.13.0-py3-none-manylinux_2_28_x86_64.whl
    rocm_sdk_device_gfx942-7.13.0-py3-none-manylinux_2_28_x86_64.whl
    rocm_sdk_device_gfx1100-7.13.0-py3-none-manylinux_2_28_x86_64.whl
    rocm_sdk_device_gfx1101-7.13.0-py3-none-manylinux_2_28_x86_64.whl
    ...
    rocm_sdk_devel-7.13.0-py3-none-manylinux_2_28_x86_64.whl
    rocm-7.13.0.tar.gz
```

All wheels go to a flat `dist/` — no per-family subdirectories needed since
every wheel has a unique name.

## Verification

### Building packages from fetched artifacts

```bash
cd sources/TheRock

# Fetch artifacts (if not already done)
python build_tools/artifact_manager.py fetch --stage all \
  --output-dir $THEROCK_WORKSPACE/.tmp/artifacts-run-23826380835 \
  --run-id 23826380835 --platform linux \
  --amdgpu-families "gfx94X-dcgpu;gfx120X-all" \
  --amdgpu-targets "gfx942,gfx1100,gfx1101,gfx1102,gfx1103,gfx1151,gfx1200,gfx1201"

# Build packages
python build_tools/build_python_packages.py \
  --artifact-dir $THEROCK_WORKSPACE/.tmp/artifacts-run-23826380835/artifacts \
  --dest-dir $THEROCK_WORKSPACE/.tmp/packages \
  --version 7.13.0.dev0

# Verify output
ls $THEROCK_WORKSPACE/.tmp/packages/dist/
# Expect: rocm_sdk_core-*.whl, rocm_sdk_libraries-*.whl (no family suffix),
#         rocm_sdk_device_gfx942-*.whl, ..., rocm_sdk_device_gfx1201-*.whl
```

### Checking wheel contents

```bash
# Host wheel should have .so files, no .kpack
unzip -l $THEROCK_WORKSPACE/.tmp/packages/dist/rocm_sdk_libraries-*.whl | grep -c '.kpack'
# Expect: 0

# Device wheel should have .kpack and kernel DBs, no .so
unzip -l $THEROCK_WORKSPACE/.tmp/packages/dist/rocm_sdk_device_gfx942-*.whl | head -40
# Expect: .kpack archives, .co/.dat/.hsaco kernel files

# Device wheel metadata should require host wheel
unzip -p $THEROCK_WORKSPACE/.tmp/packages/dist/rocm_sdk_device_gfx942-*.whl \
  '*/METADATA' | grep Requires-Dist
# Expect: Requires-Dist: rocm-sdk-libraries ==7.13.0.dev0
```

### Install test (non-GPU, structural only)

```bash
python -m venv /tmp/test-rocm-sdk
source /tmp/test-rocm-sdk/bin/activate
pip install $THEROCK_WORKSPACE/.tmp/packages/dist/rocm_sdk_libraries-*.whl
pip install $THEROCK_WORKSPACE/.tmp/packages/dist/rocm_sdk_device_gfx942-*.whl

# Verify overlay: device .kpack files land alongside host .so files
ls $(python -c "import _rocm_sdk_libraries_*; print(__import__('pathlib').Path(_rocm_sdk_libraries_*.__file__).parent)")/.kpack/ 2>/dev/null
# Or just check site-packages directly
find /tmp/test-rocm-sdk/lib -name "*.kpack" | head -5
find /tmp/test-rocm-sdk/lib -name "librocblas.so*" | head -5
```

## Files to Change

| File | Change |
|------|--------|
| `build_tools/packaging/python/templates/rocm-sdk-device/` | **New.** Wheel template (pyproject.toml, setup.py) |
| `build_tools/packaging/python/templates/rocm/src/rocm_sdk/_dist_info.py` | Add `device` PackageEntry to ALL_PACKAGES |
| `build_tools/build_python_packages.py` | Add `device_artifact_filter`, detect kpack-split mode, produce host + device wheels |
| `build_tools/_therock_utils/py_packaging.py` | Add `populate_device_files()` method; handle arch-neutral libraries package |

## What This Does NOT Do

- No variant labeling (PEP 817/825) — that's a later phase
- No unified v3 index — output is a flat dist/ directory
- No torch wheel splitting — ROCm SDK only
- No changes to CI workflows — this is local/offline packaging
- No changes to upload/index scripts — those come when CI integration happens
- No `rocm-bootstrap` integration into the wheel — it's a separate package

## Open Questions

1. **Meta sdist (`rocm`):** Currently produces one per family. In kpack-split
   mode with no families, what should it do? Options:
   - Single generic sdist that depends on `rocm-sdk-libraries` (no device deps)
   - Skip it entirely for now
   - Produce one per ISA (wasteful, many identical sdists)

   **Recommendation:** Single generic sdist. Device package selection is the
   installer's job (per wheelnext proposal section 3).

2. **Devel package:** Currently per-family. Headers are arch-neutral so a
   single `rocm-sdk-devel` should suffice. Confirm no ISA-specific headers
   exist in the artifacts.

3. **`rocm-sdk-libraries` package name:** Dropping the family suffix is a
   breaking change for anyone depending on `rocm-sdk-libraries-gfx94X-dcgpu`.
   For the half-step, this is fine (new package name, new index). But needs
   consideration for the migration path.

4. **Platform directory naming for device overlay:** The device wheel's files
   must land in the same `_rocm_sdk_libraries_{nonce}/` directory as the host
   wheel. This means the device wheel's `setup.py` needs to know the host
   wheel's `py_package_name`. This coupling is handled through `_dist_info.py`
   which both templates share.

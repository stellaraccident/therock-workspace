# Kpack + WheelNext: Multi-Arch ROCm Wheel Distribution

## 1. Current State and Problem

### Today's Distribution Model

ROCm and PyTorch wheels are published to **per-GFX-family package indices**:

```
https://rocm.nightlies.amd.com/v2/gfx94X-dcgpu/     # MI300
https://rocm.nightlies.amd.com/v2/gfx110X-all/       # RX 7000 series
https://rocm.nightlies.amd.com/v2/gfx120X-all/       # RX 9000 series
https://rocm.nightlies.amd.com/v2/gfx1151/            # Strix Halo
https://rocm.nightlies.amd.com/v2/gfx950-dcgpu/       # MI350
```

Each index contains a complete, self-consistent set of `rocm` and `torch` wheels built for that family. Users install with:

```bash
pip install --index-url https://rocm.nightlies.amd.com/v2/gfx110X-all/ \
    torch torchaudio torchvision
```

The torch wheel declares `rocm[libraries]=={exact_version}`, pulling ROCm automatically.

### What's In Each Wheel Today

| Package | Pip name | Installed to | Contents |
|---------|----------|-------------|----------|
| ROCm selector | `rocm` | `rocm_sdk/` | Pure Python, dependency resolution |
| Core | `rocm-sdk-core` | `_rocm_sdk_core/` | Compiler, HIP runtime, tools (arch-neutral) |
| Libraries | `rocm-sdk-libraries-{family}` | `_rocm_sdk_libraries_{family}/` | Math libs with embedded device code |
| Devel | `rocm-sdk-devel` | `_rocm_sdk_devel/` | Headers, CMake files |
| PyTorch | `torch` | `torch/` | Fat binaries with embedded device code |

The `rocm-sdk-libraries-{family}` wheel contains both host code and arch-specific device code. The torch wheel contains HIP-compiled fat binaries. Both include device code for all architectures in the family.

### Why This Is Problematic

1. **User friction**: Users must know their GPU architecture before installing. The index URL encodes architecture — wrong URL = wrong wheel.
2. **Index proliferation**: Each GFX family requires a complete, separately-maintained index. Adding a new GPU family means a new index, new CI matrix entry, new documentation section.
3. **No multi-GPU support**: A system with both gfx942 (MI300) and gfx1201 (RX 9070) cannot install from a single index.
4. **Download waste**: Each library wheel bundles device code for all architectures in its family, even though the user only needs one.

---

## 2. Target Experience

Three installation tiers, from best UX to most explicit:

### Tier 1: uv with Wheel Variants (Target State)

```bash
# One command. Auto-detects GPU. Downloads only what's needed.
uv pip install torch
```

The `amd-variant-provider` (backed by `rocm-bootstrap` for GPU detection) detects the GPU, selects matching device packages, and installs everything. No ROCm needs to be installed first — detection uses the kernel driver directly via sysfs.

### Tier 2: uv with `--torch-backend=auto` (Bridge)

```bash
# Uses existing uv feature. Rewrites index URL based on detected GPU.
uv pip install torch --torch-backend=auto
```

Already shipping in uv with ROCm support. POR is convergence on `download.pytorch.org` — see section 5.

### Tier 3: Legacy pip (Explicit)

```bash
# User specifies architecture explicitly.
# amd-torch-device-gfx942 depends on rocm-sdk-device-gfx942 — no need to list both.
pip install --index-url https://rocm.nightlies.amd.com/v3/ \
    torch amd-torch-device-gfx942
```

### Tier 3a: pip with Extras (Ergonomic)

```bash
# Extras pull in the right device packages implicitly
pip install --index-url https://rocm.nightlies.amd.com/v3/ \
    torch[device-gfx942]
```

See section 13 for how extras work with device packages.

### Docker / CI

```bash
# Deterministic, no detection needed.
pip install --index-url https://rocm.nightlies.amd.com/v3/ \
    torch amd-torch-device-gfx942

# Multi-GPU system: just add more device packages (they're additive)
# Can also add more meta-packages to group, etc
# Additional ergonomics can be explored.
pip install --index-url https://rocm.nightlies.amd.com/v3/ \
    torch \
    amd-torch-device-gfx942 \
    amd-torch-device-gfx1201
```

---

## 3. Package Architecture

### Design Principle: Per-Arch Packages Are the Real Packages

Device code packages use **naked GFX architecture names**: `rocm-sdk-device-gfx942`, `amd-torch-device-gfx1100`, etc. These correspond 1:1 to the GFX targets that `amdgpu` reports. No family groupings — in the future, GFX families will merge with GFX-generic architectures (e.g. `gfx9-4-generic`), so the naming scheme already aligns with the direction of travel.

All device packages use the `amd-` or `rocm-sdk-` prefix to avoid namespace collisions on shared indices like PyPI. Torch device wheels use the `amd-torch-device-{target}` naming pattern.

These per-arch packages are real, buildable, testable, installable artifacts. The same wheel content is used regardless of installation tier.

For wheel variant support, variant labels are added as a **mechanical overlay** — same wheel content, different filename/metadata. This avoids maintaining two separate build paths and ensures any installer can use the per-arch packages directly.

### Device Wheels Are Additive

Multiple device wheels can be installed into the same venv simultaneously. Each device wheel contributes `.kpack` files for its specific GFX target. There are no conflicts between them — the kpack runtime selects the appropriate archive based on the GPU present at execution time.

This means:
- A system with gfx942 + gfx1201 installs both `amd-torch-device-gfx942` and `amd-torch-device-gfx1201` (each pulls in the corresponding `rocm-sdk-device-*` automatically)
- Adding device support for a new GPU is `pip install amd-torch-device-gfx1201` — no uninstall needed
- CI environments can pre-install device packages for all target GPUs

### Package Taxonomy

| Package | Type | Contents |
|---------|------|----------|
| `rocm-sdk-core` | Host, arch-neutral | Compiler, HIP runtime, tools |
| `rocm-sdk-libraries` | Host, arch-neutral | Math libs with kpack markers, no device code |
| `rocm-sdk-device-gfx942` | Device, per-arch | `.kpack` archives for gfx942 |
| `rocm-sdk-device-gfx950` | Device, per-arch | `.kpack` archives for gfx950 |
| `rocm-sdk-device-gfx1100` | Device, per-arch | `.kpack` archives for gfx1100 |
| `rocm-sdk-device-gfx1101` | Device, per-arch | `.kpack` archives for gfx1101 |
| `rocm-sdk-device-gfx1102` | Device, per-arch | `.kpack` archives for gfx1102 |
| `rocm-sdk-device-gfx1151` | Device, per-arch | `.kpack` archives for gfx1151 |
| `rocm-sdk-device-gfx1200` | Device, per-arch | `.kpack` archives for gfx1200 |
| `rocm-sdk-device-gfx1201` | Device, per-arch | `.kpack` archives for gfx1201 |
| `rocm-sdk-devel` | Host, arch-neutral | Headers, CMake files |
| `rocm-bootstrap` | Host, arch-neutral | GPU detection, env utils, portable tools (see section 4) |
| `torch` | Host, arch-neutral | PyTorch with kpack markers, no device code |
| `amd-torch-device-gfx942` | Device, per-arch | `.kpack` archives for torch kernels on gfx942 |
| `amd-torch-device-gfx1100` | Device, per-arch | `.kpack` archives for torch kernels on gfx1100 |
| (etc.) | | One per supported GFX target |

### Device Package Dependencies

Each `amd-torch-device-{target}` depends on the corresponding `rocm-sdk-device-{target}` at the same ROCm version. Torch device code requires the ROCm device code (HIP runtime kernels, math library kernels) to function.

```
amd-torch-device-gfx942 7.13.0
  └── Requires-Dist: rocm-sdk-device-gfx942 == 7.13.0
  └── Requires-Dist: torch == 2.10.0+rocm7.13.0
```

This means users only need to install the torch device package — the ROCm device package comes along automatically. Installing just `rocm-sdk-device-gfx942` without the torch device package is also valid (ROCm libraries work, torch falls back to CPU or errors on GPU use).

### Dependency Resolution Is Not In the Host Packages

**We do not control torch's metadata.** Torch declares `rocm[libraries]=={version}` as a dependency, but we cannot make it depend on `amd-torch-device-{arch}`. Similarly, the `rocm` selector sdist approach (auto-detecting GPU at install time and injecting dependencies) is fragile — pip caches the built wheel and won't re-detect on subsequent installs.

Therefore, **device package selection is always driven by the installer**, not by host package dependencies:

| Tier | Who selects device packages? | Mechanism |
|------|------------------------------|-----------|
| Wheel variants | uv + `amd-variant-provider` | Provider plugin detects GPU, variant resolution |
| `--torch-backend` | uv | Index rewriting via `download.pytorch.org` |
| pip + extras | The user | `pip install torch[device-gfx942]` |
| Legacy pip | The user | Explicit `pip install amd-torch-device-gfx942` |

The host packages (`torch`, `rocm-sdk-libraries`) have no dependency on device packages. They work without device code (CPU-only fallback or runtime error on GPU use).

### Version Coupling

Device packages are **version-locked to their host packages**. `rocm-sdk-device-gfx942` version X.Y.Z requires `rocm-sdk-libraries` version X.Y.Z exactly. `amd-torch-device-gfx942` version A.B.C+rocmX.Y.Z requires both `torch` version A.B.C+rocmX.Y.Z and `rocm-sdk-device-gfx942` version X.Y.Z. This is enforced via `Requires-Dist` in each device wheel's metadata.

Mismatched versions are a hard error — the kpack archive format and offsets are specific to the exact host binary they were extracted from.

### File Layout in site-packages (Same-Directory Overlay)

Device packages install `.kpack` files into the **same directories** as the host packages. The device code overlay level is the **namespace package root** — `.kpack/` directories sit at the top of the namespace, not nested under `lib/`:

```
site-packages/
  _rocm_sdk_libraries/              # Installed by rocm-sdk-libraries (host)
    .kpack/                         # Installed by rocm-sdk-device-gfx942
      blas_lib_gfx942.kpack         #   (overlay at namespace root)
      fft_lib_gfx942.kpack
    lib/
      librocblas.so.4               # Host-only binary, .rocm_kpack_ref marker
      libhipfft.so.1                # Host-only binary, .rocm_kpack_ref marker
      rocblas/library/              # Installed by rocm-sdk-device-gfx942
        TensileLibrary_lazy_gfx942.dat
        TensileLibrary_lazy_gfx942.co
    bin/
      ...

  torch/                            # Installed by torch (host)
    .kpack/                         # Installed by amd-torch-device-gfx942
      torch_gfx942.kpack            #   (overlay at namespace root)
    lib/
      libtorch_hip.so               # Host-only, .rocm_kpack_ref marker
    ...
```

The kpack runtime loader resolves `.kpack` files via paths relative to the host binary. The wheel splitter tool (section 9) must be told the overlay level — the namespace package root — so it places `.kpack` files at the correct depth.

**pip does not enforce directory exclusivity** between packages. It tracks files per-package in RECORD files for uninstall purposes, but has no issue with two packages contributing files to the same directory. This is the standard namespace package pattern.

---

## 4. `rocm-bootstrap`: Bootstrap GPU Detection

### Problem

GPU detection must work **before ROCm is installed**. The variant provider runs during `uv pip install torch` — it needs to know what GPU is present in order to select device packages, but the ROCm userspace (which provides `rocminfo`, `rocm_agent_enumerator`) isn't installed yet. That's the whole point of the install.

All legacy detection paths that assume an installed ROCm (`rocminfo`, `rocm_agent_enumerator`, `/opt/rocm/.info/version`) must be explicitly excised from the bootstrap chain.

### Solution: `rocm-bootstrap`

`rocm-bootstrap` is published to both **real PyPI** and the ROCm package index. At its core it provides pure-Python GPU detection using only the kernel driver. However, it is built as part of TheRock's python packaging pipeline so it can optionally bundle portable pre-compiled tools from the ROCm build (e.g., `rocm_agent_enumerator`, `hipinfo`).

```
rocm-bootstrap
  rocm_bootstrap/
    __init__.py
    gpu_detect.py                 # Pure-Python GPU detection via sysfs / clinfo
    version.py                    # ROCm version detection from installed packages
    supported_targets.py          # Canonical list of GFX targets ROCm was built for
    _tools/                       # Optional: bundled pre-compiled tools
      rocm_agent_enumerator       #   (Linux ELF, from TheRock build)
      hipinfo.exe                 #   (Windows)
```

The package advertises as pure Python for installation simplicity — the bundled tools are optional extras that enhance functionality. Building it inside TheRock gives us:

- Access to compiled tools that would otherwise require a full ROCm install
- Integration with the existing test suite — tested the same as everything else
- A place to put Windows tools like `clinfo` that may decouple from the driver in the future

**Deployment flow**: TheRock CI builds `rocm-bootstrap` alongside other python packages. It lands on the ROCm nightly index. A separate deploy step **promotes** it to real PyPI for regular releases. This means it's available from pypi.org for bootstrap (the variant provider's isolated env), but also tested and versioned with the rest of the ROCm SDK. We may consider also handling the `amd-variant-provider` this way — or having `rocm-bootstrap` directly advertise the variant provider entry point (see open question #4).

### GPU Detection via KFD Topology (Linux)

The AMDGPU kernel driver exposes GPU information through the KFD (Kernel Fusion Driver) topology in sysfs. This works with just the kernel driver loaded — no ROCm userspace, no `rocminfo`, no `rocm_agent_enumerator`.

```
/sys/class/kfd/kfd/topology/nodes/
  0/properties     # CPU node (gpu_id = 0)
  1/properties     # GPU node (gpu_id != 0)
    ...
    gfx_target_version 120001    # Encodes gfx1201
    ...
```

The `gfx_target_version` field encodes the GFX target as an integer:

```
gfx_target_version = major * 10000 + minor * 100 + stepping
```

Examples:
- `120001` → gfx1201
- `110000` → gfx1100
- `90402` → gfx942
- `110100` → gfx1101
- `115100` → gfx1151

Reference implementation:

```python
from pathlib import Path

def detect_amd_gpus() -> list[str]:
    """Detect AMD GPUs via KFD topology sysfs.

    Works with just the amdgpu kernel driver loaded.
    No ROCm userspace needed.

    Returns list of GFX target strings, e.g. ["gfx942", "gfx1201"].
    """
    kfd_nodes = Path("/sys/class/kfd/kfd/topology/nodes")
    if not kfd_nodes.is_dir():
        return []

    gpus: list[str] = []
    for node_dir in sorted(kfd_nodes.iterdir()):
        props_file = node_dir / "properties"
        if not props_file.is_file():
            continue

        props: dict[str, int] = {}
        for line in props_file.read_text().strip().splitlines():
            key, _, value = line.strip().partition(" ")
            props[key] = int(value)

        # Skip CPU-only nodes (simd_count == 0 indicates no GPU CUs)
        simd_count = props.get("simd_count", 0)
        if simd_count == 0:
            continue

        gtv = props.get("gfx_target_version", 0)
        if gtv == 0:
            continue

        major = gtv // 10000
        minor = (gtv % 10000) // 100
        stepping = gtv % 100
        gfx_target = f"gfx{major}{minor:01d}{stepping:02d}"
        gpus.append(gfx_target)

    return sorted(set(gpus))
```

### Fallback: ip_discovery sysfs (Linux)

If KFD topology is unavailable (older kernels, containers without `/sys/class/kfd`):

```
/sys/class/drm/card*/device/ip_discovery/die/0/GC/0/
  major     # e.g. 12
  minor     # e.g. 0
  revision  # e.g. 1
```

Encodes the same information in separate files. `major=12, minor=0, revision=1` → gfx1201.

### Fallback: PCI Device ID Table (Linux)

Last resort — a hardcoded table mapping PCI vendor:device IDs to GFX targets. The `amdgpu` driver's device table (`amdgpu_drv.c`) is the source of truth. This works even without the AMDGPU kernel driver loaded (just needs `/sys/bus/pci/devices/`), but requires maintaining a mapping table.

### Windows Detection

On Windows, `clinfo` is included in system32 with the AMD driver and prints GFX architecture info as part of its output. This is the primary detection strategy:

1. **`clinfo`** (primary): Installed in `system32` with the driver. Parse output for GFX arch names. Note: `clinfo` may move to a separate install from the driver in the future — `rocm-bootstrap` can bundle an equivalent as a fallback.
2. **HIP API**: `hipGetDeviceProperties()` → `gcnArchName`. Requires HIP runtime.
3. **WMI / DirectX**: Query GPU adapter info. Gets PCI ID, requires device ID table.

### Graceful Degradation

When GPU detection fails (no AMD GPU, container without sysfs access, unsupported OS), `rocm-bootstrap` returns an empty list rather than raising an exception. Callers — including the variant provider — handle the empty case by reporting no AMD features.

**Environment variable overrides** for when detection fails or is wrong:

| Variable | Effect |
|----------|--------|
| `ROCM_BOOTSTRAP_FORCE_GFX_ARCH` | Override detected GFX arch(s). Comma-separated. e.g. `gfx942,gfx1201` |
| `ROCM_BOOTSTRAP_FORCE_ROCM_VERSION` | Override ROCm version. e.g. `7.13.0` |
| `ROCM_BOOTSTRAP_DISABLE_DETECTION` | Set to `1` to skip all hardware detection (return empty) |

These overrides are respected by both `rocm-bootstrap` and the `amd-variant-provider`. They are the escape hatch for containers, VMs, CI, and misconfigured systems.

### Supported Targets List

`rocm-bootstrap` is the **source of truth** for the list of GFX targets that ROCm was actually built for. This is exposed as `rocm_bootstrap.supported_targets` and consumed by the variant provider's `get_all_configs()`. The list is populated at build time from TheRock's build configuration — no hardcoding in the provider.

### What This Package Does NOT Do

- It does NOT assume ROCm is installed
- It does NOT do family mapping — it returns raw GFX target strings

### Bootstrap Chain

```
Kernel driver (amdgpu.ko) / clinfo (Windows)
  └── rocm-bootstrap (on PyPI, reads sysfs / clinfo)
        └── amd-variant-provider (depends on rocm-bootstrap)
              └── uv (queries provider in isolated env)
                    └── resolves: torch + amd-torch-device-gfx942
                                          └── pulls in rocm-sdk-device-gfx942
```

The only prerequisite is the kernel driver (Linux) or the AMD display driver (Windows), which is already present if the user has an AMD GPU.

---

## 5. `--torch-backend` as Bridge

### How It Works Today

`--torch-backend=auto` in uv:
1. Detects GPU — for AMD, currently runs `rocm_agent_enumerator` as a subprocess
2. Maps GPU to a backend name (`rocm6.4`)
3. Rewrites the index URL to `download.pytorch.org/whl/rocm6.4`
4. All torch-ecosystem packages (`torch`, `torchvision`, `torchaudio`) are resolved from the rewritten index

### Architecture Limitations

`--torch-backend` is a **closed system** in uv's architecture — hard-coded backend enum, fixed index URLs on `download.pytorch.org`, fixed package allowlist. It is not designed to be extensible for custom indices.

### POR: Converge on `download.pytorch.org`

The plan of record for `--torch-backend` is convergence on `download.pytorch.org`:

1. TheRock-built torch wheels land on `download.pytorch.org/whl/rocm*`
2. `--torch-backend=auto` works automatically — no uv changes needed
3. Recommendation: base pytorch.org's nightly ROCm builds on ROCm 7.13 using the new kpack-split structure, let the new structure age into existence through the nightly channel

This avoids the need for custom index support, configurable backends, or other uv upstream changes. The `--torch-backend` feature is a bridge — wheel variants (Tier 1) are the long-term solution.

For pre-release or maintenance-mode AMD-built torches (not published to pytorch.org), users fall back to Tier 3 (explicit pip install from the ROCm index).

---

## 6. The `amd-variant-provider`

### Current State

The [`amd-variant-provider`](https://github.com/wheelnext/amd-variant-provider) is a WheelNext plugin that detects AMD GPUs and reports variant features. It registers via `[project.entry-points.variant_plugins]` in pyproject.toml. uv discovers providers from user configuration: providers are declared in config, uv installs them into isolated environments, queries them, and uses results for variant resolution.

Current implementation: zero dependencies, Linux-only, detects via `rocminfo` and `rocm_agent_enumerator` with fallback to `/opt/rocm/.info/version`.

### Required Changes

**1. Depend on `rocm-bootstrap` for GPU Detection**

Replace all detection paths that assume an installed ROCm:

| Current (remove) | New (from `rocm-bootstrap`) |
|---|---|
| `subprocess.run(["rocminfo"])` | `rocm_bootstrap.gpu_detect.detect_amd_gpus()` |
| `subprocess.run(["rocm_agent_enumerator"])` | `rocm_bootstrap.gpu_detect.detect_amd_gpus()` |
| Read `/opt/rocm/.info/version` | `rocm_bootstrap.version.detect_rocm_version()` |
| Read `/sys/module/amdgpu/version` | (keep — this is kernel driver info, not ROCm userspace) |

The provider's `pyproject.toml` adds `rocm-bootstrap` as a dependency:

```toml
dependencies = ["rocm-bootstrap"]
```

Since `rocm-bootstrap` is on real PyPI, uv can install it into the provider's isolated environment without needing the ROCm index.

**2. Report Naked GFX Targets**

The provider currently reports raw GFX targets (e.g., `gfx1100`). This is already correct — no family mapping needed. The `VariantFeatureConfig` values correspond directly to per-arch package names:

```python
VariantFeatureConfig(
    name="gfx_arch",
    values=["gfx942", "gfx1100"],  # Naked GFX targets, not families
    multi_value=True
)
```

A system with multiple GPUs reports all of them. The variant resolver selects packages for each detected target.

**3. ROCm Version Detection**

ROCm version matters for compatibility (device packages are version-locked to the host SDK). The only reliable detection path is the installed `rocm-sdk` packages:

```python
importlib.metadata.version('rocm-sdk-core')
```

If ROCm isn't installed yet, the ROCm version is determined by the package version being installed, not by system detection. The provider should NOT try to read `/opt/rocm/.info/version` or run `rocminfo --version`.

Legacy fallbacks (`ROCM_VERSION` env var, version file reads) can be kept temporarily until existing ROCm 7.x installations age out, but they are not part of the target architecture.

**4. `get_all_configs()` from `rocm-bootstrap`**

The provider's `get_all_configs()` should not hardcode GFX target lists. Instead, it should consume `rocm_bootstrap.supported_targets` — the canonical list of targets that ROCm was actually built for. This keeps the provider thin and avoids sync issues when new targets are added.

**5. Graceful Degradation**

When detection fails (no AMD GPU, unsupported OS, container without sysfs), the provider returns an empty config list rather than raising an exception. Environment variable overrides (see section 4) are the escape hatch. This applies to all platforms, not just Windows.

---

## 7. WheelNext / PEP 817 Integration

### Current Status

- **PEP 817** (Wheel Variants): Draft, Dec 2025. Split into smaller PEPs.
- **PEP 825** (Package Format): Open PR against python/peps (#4819), not yet merged.
- **Experimental uv**: Available at `wheelnext.astral.sh`. PyTorch 2.8+ CUDA, 2.9+ ROCm.
- **`amd-variant-provider`**: v0.0.2, Linux-only, basic detection.

### How Variants Work

PEP 817/825 adds a variant label to the wheel filename:

```
{name}-{version}-{python}-{abi}-{platform}-{variant_label}.whl
```

Variant-unaware installers ignore variant wheels (filename parsing fails on extra component). Variant-aware installers (experimental uv) query provider plugins to select the best match.

Providers are declared in user configuration. uv installs them into isolated environments and queries them to detect system capabilities.

### Variant Wheel Generation

Per-arch device packages are the real artifacts. Variant-labeled wheels are generated mechanically:

```
# Real package (works with all installers):
rocm_sdk_device_gfx942-7.12.0-py3-none-manylinux_2_28_x86_64.whl

# Same content, variant label added (for variant-aware uv):
rocm_sdk_device-7.12.0-py3-none-manylinux_2_28_x86_64-gfx942.whl
```

The variant wheel has:
- Package name: `rocm-sdk-device` (generic, no arch suffix)
- Variant label: `gfx942`
- `variant.json` in dist-info with provider and property metadata
- Identical file content to the per-arch package

A `null` variant (empty wheel, no device code) allows graceful degradation on systems without an AMD GPU. See section 13 for how torch detects and warns in this configuration.

### Variant Metadata

```json
{
  "providers": {
    "amd": {
      "install-time": true,
      "plugin-api": "amd_variant_provider.plugin:AMDVariantPlugin",
      "requires": ["amd-variant-provider >= 1.0"],
      "enable-if": "platform_system == 'Linux'"
    }
  },
  "default-priorities": {
    "namespace": ["amd"],
    "property": {
      "amd": {
        "gfx_arch": [
          "gfx942", "gfx950",
          "gfx1100", "gfx1101", "gfx1102", "gfx1103",
          "gfx1151",
          "gfx1200", "gfx1201"
        ]
      }
    }
  },
  "variants": {
    "gfx942": { "amd": { "gfx_arch": ["gfx942"] } },
    "gfx950": { "amd": { "gfx_arch": ["gfx950"] } },
    "gfx1100": { "amd": { "gfx_arch": ["gfx1100"] } },
    "gfx1101": { "amd": { "gfx_arch": ["gfx1101"] } },
    "gfx1102": { "amd": { "gfx_arch": ["gfx1102"] } },
    "gfx1103": { "amd": { "gfx_arch": ["gfx1103"] } },
    "gfx1151": { "amd": { "gfx_arch": ["gfx1151"] } },
    "gfx1200": { "amd": { "gfx_arch": ["gfx1200"] } },
    "gfx1201": { "amd": { "gfx_arch": ["gfx1201"] } }
  }
}
```

---

## 8. ROCm SDK Wheel Restructuring

### Current → New Package Mapping

| Current | New |
|---------|-----|
| `rocm-sdk-libraries-gfx110x-all` | `rocm-sdk-libraries` (host-only) + `rocm-sdk-device-gfx1100` + `rocm-sdk-device-gfx1101` + ... |
| `rocm-sdk-libraries-gfx94x-dcgpu` | `rocm-sdk-libraries` (host-only) + `rocm-sdk-device-gfx942` |

Each GFX target that the TheRock CI builds for gets its own device package. The current "family" groupings (`gfx110x-all` → gfx1100+1101+1102+1103) are dissolved — each target is independent.

### Build Pipeline

The existing artifact split pipeline already separates generic and arch-specific content:

```
artifacts/
  blas_lib_generic/           # Host-only libraries → rocm-sdk-libraries wheel
  blas_lib_gfx942/            # .kpack + kernel DBs → rocm-sdk-device-gfx942 wheel
  blas_lib_gfx1100/           # .kpack + kernel DBs → rocm-sdk-device-gfx1100 wheel
  blas_lib_gfx1101/           # .kpack + kernel DBs → rocm-sdk-device-gfx1101 wheel
```

Changes needed:

1. **Host wheel packer** (modified `build_python_packages.py`): Produces arch-neutral `rocm-sdk-libraries` from generic artifacts. All host binaries have `.rocm_kpack_ref` markers — no embedded device code.

2. **Device wheel packer** (new): For each GFX target, collects all `*_{target}` artifacts into `rocm-sdk-device-{target}`. Files are laid out to overlay at the **namespace package root** (e.g., `_rocm_sdk_libraries/.kpack/`) so they align correctly with the host package's directory structure in site-packages.

3. **Variant wrapper** (new, optional): Generates variant-labeled copies of device wheels with `variant.json` metadata. One copy per GFX target, plus a null variant.

---

## 9. PyTorch Wheel Splitting

### The Problem

PyTorch wheels contain HIP-compiled fat binaries (`libtorch_hip.so`, `libc10_hip.so`, etc.) with embedded `.hip_fatbin` sections. We cannot modify torch's package metadata, so device code must be extracted into separate packages.

### `split_python_wheels` Tool

A new tool post-processes a fat PyTorch wheel:

```bash
python -m rocm_kpack.tools.split_python_wheels \
    --input torch-2.10.0+rocm7.12.0-cp313-cp313-linux_x86_64.whl \
    --output-dir dist/ \
    --device-package-prefix amd-torch-device \
    --overlay-root torch/
```

The `--overlay-root` parameter declares the level at which device code overlays onto the host package's directory structure. For torch, this is `torch/` — so `.kpack/` is placed at `torch/.kpack/`. For ROCm SDK, it's `_rocm_sdk_libraries/`.

**Processing steps**:

1. **Extract**: Unzip wheel to temp directory
2. **Scan**: Identify fat binaries (ELF with `.hip_fatbin` sections) and kernel databases
3. **Transform**: Run kpack `offload_kpacker` on each fat binary
4. **Partition**: Separate host content from arch-specific `.kpack` files
5. **Repackage**:
   - Host wheel: `torch-{version}-...-linux_x86_64.whl` (host-only)
   - Device wheels: `amd-torch-device-gfx942-{version}-...-linux_x86_64.whl` (one per target)

Each generated device wheel includes `Requires-Dist: rocm-sdk-device-gfx{target} == {rocm_version}` in its metadata.

### Build Strategy

**pytorch.org: Mondo Build + Post-Split**

pytorch.org will build PyTorch once with all target architectures, then split:

```bash
PYTORCH_ROCM_ARCH="gfx942;gfx1100;gfx1101;gfx1102;gfx1200;gfx1201" \
    python setup.py bdist_wheel
python -m rocm_kpack.tools.split_python_wheels --input dist/torch-*.whl
```

Single build invocation, guaranteed ABI consistency. In the future, pytorch.org may split between Instinct and Radeon builds purely as a build-time optimization (does not affect the final result — same host wheel, same device wheel content).

**TheRock CI: Parallel Split Builds + Combine**

TheRock CI builds torch N times (once per GFX target), extracts device code from each:

```bash
# Parallel CI jobs:
PYTORCH_ROCM_ARCH="gfx942" python setup.py bdist_wheel
PYTORCH_ROCM_ARCH="gfx1100" python setup.py bdist_wheel
# ...

# Combine step:
python -m rocm_kpack.tools.combine_python_wheels --host-wheel ... --device-wheels ...
```

This gives better parallelism, faster time-to-signal, and can catch ABI inconsistency bugs between targets. Host ABI consistency is verified during the combine step.

### Device Wheel Structure

```
amd_torch_device_gfx942-2.10.0+rocm7.12.0-cp313-cp313-manylinux_2_28_x86_64.whl
└── torch/
    └── .kpack/
        └── torch_gfx942.kpack
```

---

## 10. Unified Index Structure

### Target Layout

```
rocm.nightlies.amd.com/v3/
  # Host packages (shared across all architectures)
  rocm-7.12.0.tar.gz
  rocm_sdk_core-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_libraries-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_devel-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_bootstrap-7.12.0-py3-none-any.whl
  torch-2.10.0+rocm7.12.0-cp313-cp313-manylinux_2_28_x86_64.whl

  # ROCm device packages (per-arch, real packages)
  rocm_sdk_device_gfx942-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx950-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx1100-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx1101-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx1102-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx1151-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx1200-7.12.0-py3-none-manylinux_2_28_x86_64.whl
  rocm_sdk_device_gfx1201-7.12.0-py3-none-manylinux_2_28_x86_64.whl

  # Torch device packages (per-arch, depend on corresponding rocm-sdk-device-*)
  amd_torch_device_gfx942-2.10.0+rocm7.12.0-cp313-cp313-manylinux_2_28_x86_64.whl
  amd_torch_device_gfx1100-2.10.0+rocm7.12.0-cp313-cp313-manylinux_2_28_x86_64.whl
  amd_torch_device_gfx1201-2.10.0+rocm7.12.0-cp313-cp313-manylinux_2_28_x86_64.whl
  ...

  # Variant-labeled copies (same content, for variant-aware uv)
  rocm_sdk_device-7.12.0-py3-none-manylinux_2_28_x86_64-gfx942.whl
  rocm_sdk_device-7.12.0-py3-none-manylinux_2_28_x86_64-gfx1100.whl
  rocm_sdk_device-7.12.0-py3-none-manylinux_2_28_x86_64-null.whl
  amd_torch_device-2.10.0+rocm7.12.0-...-gfx942.whl
  amd_torch_device-2.10.0+rocm7.12.0-...-null.whl

  # Variant metadata
  rocm_sdk_device-7.12.0-variants.json
  amd_torch_device-2.10.0+rocm7.12.0-variants.json
```

Note: `rocm-bootstrap` is also published to **real PyPI** (pypi.org) via a promotion step. It must be available without configuring a custom index URL, since the variant provider needs it during bootstrap.

The per-arch packages and variant-labeled wheels coexist on the same index. Variant-aware uv uses the variant wheels; legacy pip uses the per-arch packages.

---

## 11. Build Pipeline Changes

### ROCm SDK

```
┌─────────────────────────────────────┐
│ Existing: cmake build + artifact    │
│ split per GFX target                │
└──────────────┬──────────────────────┘
               │
    ┌──────────┴──────────┐
    ▼                     ▼
 generic artifacts    per-target artifacts
    │                     │
    ▼                     ▼
 rocm-sdk-libraries   rocm-sdk-device-{target}
 (host wheel)         (one device wheel per target)
    │                     │
    └──────────┬──────────┘
               ▼
     [optional] variant labeler
     (generate variant copies + metadata)
```

### PyTorch

```
┌─────────────────────────────────────┐
│ Build fat torch wheel               │
│ (per-target in TheRock CI,          │
│  mondo build on pytorch.org)        │
└──────────────┬──────────────────────┘
               │
               ▼
     split_python_wheels
     (kpack extraction + repackaging)
               │
    ┌──────────┴──────────┐
    ▼                     ▼
 torch                amd-torch-device-{target}
 (host wheel)         (one device wheel per target,
    │                  depends on rocm-sdk-device-{target})
    │                     │
    └──────────┬──────────┘
               ▼
     [optional] variant labeler
```

### `rocm-bootstrap`

```
┌─────────────────────────────────────┐
│ Built as part of TheRock python     │
│ packages (access to compiled tools) │
└──────────────┬──────────────────────┘
               │
    ┌──────────┴──────────┐
    ▼                     ▼
 ROCm nightly index    Promote to pypi.org
 (tested with suite)   (regular releases)
```

---

## 12. Implementation Roadmap

### Phase 1: `rocm-bootstrap` + Provider Bootstrap

**This must come first** — everything else depends on working GPU detection.

1. Create `rocm-bootstrap` package with sysfs-based GPU detection (Linux) and clinfo detection (Windows)
2. Build as part of TheRock python packages; publish to ROCm nightly index
3. Promote to PyPI
4. Update `amd-variant-provider` to depend on `rocm-bootstrap`
5. Remove all `rocminfo` / `rocm_agent_enumerator` / `/opt/rocm` detection paths from the provider
6. Test: provider detects GPU on a machine with kernel driver but no ROCm userspace

### Phase 2: ROCm SDK Host/Device Split

**Prerequisite**: Kpack artifact splitting works (PRs #9, #10, #11 merged).

1. Modify `build_python_packages.py` to produce arch-neutral `rocm-sdk-libraries` from generic artifacts
2. Create device wheel packer that produces `rocm-sdk-device-{target}` per GFX target
3. Add device wheel entrypoints for `rocm-sdk` diagnostic tool integration (see section 13)
4. Verify: install host + device wheels, `rocm-sdk test` passes

### Phase 3: PyTorch Wheel Splitter

1. Implement `rocm_kpack.tools.split_python_wheels` with configurable `--overlay-root`
2. Generated `amd-torch-device-*` wheels include `Requires-Dist: rocm-sdk-device-*`
3. Test with a real fat PyTorch wheel
4. Verify: host wheel + device wheel produces working `import torch; torch.cuda.is_available()`

### Phase 4: Unified Index + Legacy pip

1. Build unified index from host + device wheels
2. Test: `pip install --index-url .../v3/ torch amd-torch-device-gfx942` (rocm-sdk-device pulled automatically)
3. Add extras support: `pip install --index-url .../v3/ torch[device-gfx942]`
4. Document legacy pip instructions per GFX target

### Phase 5: Variant Labeling

1. Generate variant-labeled copies of device wheels with `variant.json` metadata
2. Test with experimental variant-enabled uv
3. Test: `uv pip install torch` on AMD GPU → correct device packages installed

### Phase 6: pytorch.org Convergence

1. Base pytorch.org nightly ROCm builds on ROCm 7.13 using the kpack-split structure
2. Let new structure age into existence through the nightly channel
3. `--torch-backend=auto` starts working out of the box

### Phase 7: Production Deployment — ROCm 7.13

**Target: ROCm 7.13.** Must be done before 8.0 GA to allow bake time.

1. Deploy v3 unified index alongside v2 per-family indices
2. Update RELEASES.md with new instructions
3. Integrate `rocm-sdk` diagnostic tooling (section 13)
4. Deprecate v2 indices over transition period

---

## 13. Ergonomic Improvements

### `rocm-sdk` Diagnostic Tool

The existing `rocm-sdk` command-line tool gains device support management capabilities. All device wheels (both ROCm and torch) expose an **entrypoint** that the diagnostic tool discovers to build a picture of installed device support.

```bash
$ rocm-sdk device status
Detected GPUs:
  - gfx942 (MI300X)
  - gfx1201 (RX 9070 XT)

Installed device packages:
  rocm-sdk-device-gfx942      7.13.0   ✓ matches rocm-sdk-libraries 7.13.0
  amd-torch-device-gfx942     2.10.0   ✓ matches torch 2.10.0+rocm7.13.0

Missing device support:
  gfx1201: no device packages installed

To install missing device support:
  pip install --index-url https://rocm.nightlies.amd.com/v3/ \
      amd-torch-device-gfx1201
```

Each device wheel registers an entrypoint (e.g. `rocm_sdk_device_info`) that reports:
- Package name and version
- GFX target
- Corresponding host package name and required version

The `rocm-sdk device status` command:
1. Calls `rocm-bootstrap` to detect installed GPUs
2. Discovers all installed device packages via their entrypoints
3. Cross-checks: device package versions match host packages, all detected GPUs have device support
4. Prints actionable install commands for any mismatches or gaps

### pip Extras for One-Liner Installs

The `rocm` selector package and `torch` can expose extras that pull in device dependencies:

```bash
# Install torch with gfx942 device support
pip install --index-url https://rocm.nightlies.amd.com/v3/ torch[device-gfx942]

# Multiple architectures
pip install --index-url https://rocm.nightlies.amd.com/v3/ torch[device-gfx942,device-gfx1201]
```

This is implemented as extras metadata in the `torch` (or `rocm`) package on the ROCm index:

```toml
[project.optional-dependencies]
device-gfx942 = ["amd-torch-device-gfx942=={version}"]
device-gfx1100 = ["amd-torch-device-gfx1100=={version}"]
# ... etc
# (rocm-sdk-device-* pulled in transitively via amd-torch-device-* dependencies)
```

This adds no machinery — it's pure metadata. It doesn't replace variant detection but makes explicit installs more ergonomic.

### Null Variant / Missing Device Code Warning

When torch is installed without device packages (null variant, or no device packages at all) and a user tries to use a GPU:

1. The kpack runtime fails to find `.kpack` files → returns `KPACK_ERROR_KERNEL_NOT_FOUND`
2. The HIP runtime surfaces this as a diagnostic, not a silent failure
3. We extend the torch ROCm bootstrap path to detect this condition and print an actionable message:

```
RuntimeError: No device code found for gfx942.
Install device support:
  pip install amd-torch-device-gfx942
Or run 'rocm-sdk device status' for a full diagnosis.
```

Similarly, version mismatches between host and device packages produce a clear error rather than cryptic kpack failures.

### Index Configuration Helper

`rocm-bootstrap` includes a helper that configures pip/uv to use the ROCm index:

```bash
# Show current configuration and recommended changes
$ rocm-sdk index status
Current pip index: https://pypi.org/simple/
ROCm index not configured.

# Configure pip to use the ROCm index (as extra-index-url)
$ rocm-sdk index configure
Added to ~/.config/pip/pip.conf:
  [global]
  extra-index-url = https://rocm.nightlies.amd.com/v3/
```

This is explicitly **user-initiated** — unlike NVIDIA's approach of implicitly modifying `pip.conf` in their CUDA source package. The user runs the command, sees exactly what it does, and can undo it. The tool also supports `--dry-run` to show proposed changes without writing.

---

## 14. Alternatives Considered

### A. Auto-Detecting Selector sdist

**Approach**: The `rocm` sdist detects the GPU at install time and injects device package dependencies.

**Why rejected**: pip caches the wheel built from the sdist. Once cached, it won't re-detect on subsequent installs. The UX of "purge and reinstall" is worse than explicit device package names. May revisit if packaging tools improve sdist caching behavior.

### B. Injecting Device Dependencies into torch

**Approach**: Modify torch's metadata to depend on `amd-torch-device` with variant resolution.

**Why rejected**: We don't control the torch meta-package. torch's dependencies are fixed at build time by the PyTorch project. The device code dependency must be injected by the installer (`--torch-backend`, variant provider), not the package.

### C. Manifest-Based Cross-Package Discovery

**Approach**: Device wheels install to their own directory. A manifest tells the kpack runtime where to find `.kpack` files across package boundaries.

**Why rejected for now**: Adds a new invariant (manifest correctness), requires kpack loader changes. The same-directory overlay works with the existing loader. Can be revisited if overlay causes practical problems.

### D. Full Variant Wheels (Host + Device Combined)

**Approach**: Each variant of `rocm-sdk-libraries` is complete with both host and device code. No separate device packages.

**Why rejected**: Redundant host code across variants. Index storage multiplied by number of architectures. Doesn't help with torch (can't variant-label upstream torch).

### E. Single Monolithic Device Package

**Approach**: One `rocm-sdk-device` with all architectures.

**Why rejected**: Users download ~10GB of device code when they need ~500MB.

### F. GFX Family Groupings

**Approach**: Group GFX targets into families (`gfx94x` = gfx940+941+942, `gfx110x` = gfx1100+1101+1102+1103) and use family names in package names.

**Why rejected**: Families are a TheRock-internal concept that doesn't map cleanly to the future. GFX-generic architectures (e.g. `gfx9-4-generic`) are the upstream direction for multi-target support. Naked GFX arch names are more precise, avoid the need for a mapping table in the provider, and align with what the kernel driver actually reports. Over time, per-target packaging will converge on generic arches, but that is outside the scope of this initial spike.

### G. Detection via `rocminfo` / `rocm_agent_enumerator`

**Approach**: The variant provider detects GPUs by running `rocminfo` or `rocm_agent_enumerator` as a subprocess.

**Why rejected for bootstrap**: These tools are part of the ROCm userspace. The provider runs *before* ROCm is installed — that's the point of the install. The KFD sysfs topology provides the same information using only the kernel driver.

---

## 15. Open Questions

1. **Multi-GPU variant resolution**: PEP 817 doesn't clearly specify installing multiple variants of the same package. The per-arch package model handles this naturally (install both `amd-torch-device-gfx942` and `amd-torch-device-gfx1201`), but the variant model may need extension or upstream discussion.

2. **Bundled tools in `rocm-bootstrap`**: Which pre-compiled tools should be bundled in the initial release? Candidates: `rocm_agent_enumerator`, `hipinfo`. Starting with pure-Python-only is fine; bundling can come later as needs arise.

3. **Extras on upstream torch**: Can we get the `[device-gfx*]` extras into the torch package on `download.pytorch.org`? This would require PyTorch project buy-in. If not, extras only work on the ROCm index where we control the torch package metadata.

4. **Variant provider ownership**: Should `rocm-bootstrap` directly advertise the `variant_plugins` entry point (i.e., absorb the `amd-variant-provider` role), or keep it as a separate `amd-variant-provider` package that depends on `rocm-bootstrap`? Having `rocm-bootstrap` own the entry point means one fewer package to deploy and test, and we control the full stack. Keeping them separate respects the WheelNext community's existing structure. Open question to Jithun based on what he prefers.

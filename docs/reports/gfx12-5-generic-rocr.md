# gfx12-5 Generic ROCR Target Handling

Date: 2026-06-15

## Summary

The original two-patch stack had the right high-level direction: current LLVM has
a distinct `gfx12-5-generic` code object target, and `gfx1250` must not be mapped
to `gfx12-generic`. However, the patch was incomplete in two ways:

- It decoded `gfx12-5-generic` as if SRAM ECC and XNACK were unsupported.
- It only carried the existing `gfx1250` ELF handling, while LLVM has a
  `gfx1251` ELF machine value and its generic gfx12.5 processor covers both
  `gfx1250` and `gfx1251`.

The corrected change maps `gfx1250` and `gfx1251` to `gfx12-5-generic`, adds the
LLVM ELF machine values for `gfx1251` and `gfx12-5-generic`, and decodes gfx12.5
concrete and generic code objects as SRAM ECC/XNACK supporting. Per PR review,
the ROCR ISA registry should only add the unsuffixed `gfx12-5-generic`
generic-version entry and should not add feature-suffixed gfx12.5 rows, because
the compiler does not support the `xnack+/-` and `sramecc+/-` combinations as
gfx12.5 target IDs. We retained the unsuffixed concrete `gfx1251` row to match
LLVM's current gfx12.5 generic membership; the reviewer can make the final call
on whether ROCR should defer that agent mapping.

## LLVM Findings

Current LLVM `GCNProcessors.td` defines:

- `gfx1250` using `FeatureISAVersion12_50`.
- `gfx1251` using `FeatureISAVersion12_51`.
- `gfx12-5-generic` for `[gfx1250, gfx1251]` using
  `FeatureISAVersion12_5_Generic`.

Source:
https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AMDGPU/GCNProcessors.td

Current LLVM `AMDGPU.td` defines `FeatureISAVersion12_50_Common` with both
`FeatureSupportsSRAMECC` and `FeatureSupportsXNACK`, and with `FeatureXNACK`.
`FeatureISAVersion12_5_Generic` is built from that common feature set, so the
generic gfx12.5 target is feature-supporting in LLVM's target ID model.

Source:
https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AMDGPU/AMDGPU.td

Current LLVM ELF values include:

- `EF_AMDGPU_MACH_AMDGCN_GFX1250 = 0x49`
- `EF_AMDGPU_MACH_AMDGCN_GFX1251 = 0x5a`
- `EF_AMDGPU_MACH_AMDGCN_GFX12_5_GENERIC = 0x5b`

Source:
https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/BinaryFormat/ELF.h

LLVM generic target versioning currently assigns `GFX12_5 = 1`, and the ELF
streamer emits that generic version for `GK_GFX12_5_GENERIC` in code object V6.

Sources:
https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AMDGPU/Utils/AMDGPUBaseInfo.h
https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AMDGPU/MCTargetDesc/AMDGPUTargetStreamer.cpp

## ROCR Model

ROCR's `IsaRegistry` uses `IsaFeature::Unsupported`, `Any`, `Disabled`, and
`Enabled`. For code object V4 and later, LLVM's ELF flags can encode
unsupported, any/default, off, or on for XNACK and SRAM ECC. In ROCR's target ID
string form, unsupported and any/default both have no `:xnack` or `:sramecc`
suffix, so the no-suffix registry row must preserve the distinction internally.

That distinction is already visible in the `gfx942` / `gfx9-4-generic` entries:
the no-suffix row is `any, any`, not `unsupported, unsupported`. LLVM marks
gfx12.5 as supporting both feature dimensions, so the unsuffixed `gfx1250` and
`gfx1251` rows should likewise be `any, any`.

If `gfx12-5-generic` is registered as unsupported for SRAM ECC/XNACK while an
agent is registered as supporting those features, ROCR's compatibility check can
hit the support-state assertion:

```c++
assert(code_object_isa.IsSrameccSupported() == agent_isa.IsSrameccSupported()
       && agent_isa.GetSramecc() != IsaFeature::Any);
assert(code_object_isa.IsXnackSupported() == agent_isa.IsXnackSupported()
       && agent_isa.GetXnack() != IsaFeature::Any);
```

The initial fix mirrored the explicit `gfx9-4-generic` off/on registry entries
for gfx12.5. The PR review corrected that: compiler-supported gfx12.5 target IDs
do not include the `xnack+/-` and `sramecc+/-` combinations, so ROCR only needs
the unsuffixed generic-version entry and unsuffixed concrete mappings.

## Fix

The fixed commit does the following:

- Adds `EF_AMDGPU_MACH_AMDGCN_GFX1251 = 0x05a`.
- Adds `EF_AMDGPU_MACH_AMDGCN_GFX12_5_GENERIC = 0x05b`.
- Decodes both `gfx1250` and `gfx1251` as SRAM ECC and XNACK supporting.
- Decodes `gfx12-5-generic` as SRAM ECC and XNACK supporting.
- Registers unsuffixed `gfx12-5-generic` with generic version `1`, matching
  LLVM's current `GFX12_5` generic version.
- Registers unsuffixed `gfx1250` and `gfx1251` as `any, any` and maps them to
  `gfx12-5-generic`.
- Does not register feature-suffixed gfx12.5 ISA rows because the compiler does
  not support those SRAM ECC/XNACK target-ID combinations for gfx12.5.
- Updates `rocm_bootstrap` target metadata so `gfx12_5` reports
  `llvm_generic="gfx12-5-generic"`, matching current LLVM.

## Validation

Ran:

```bash
git diff --check
python -m pytest python/rocm-bootstrap/python/rocm_bootstrap/tests/test_targets.py
/srv/vm-shared/rocm/rocm-7.14.0a20260610/lib/llvm/bin/llvm-mc ...
/srv/vm-shared/rocm/rocm-7.14.0a20260610/lib/llvm/bin/llvm-readelf -h ...
```

Results:

- `git diff --check` passed.
- `test_targets.py`: 227 passed.
- Local ROCm LLVM `llvm-mc` / `llvm-readelf` confirmed:
  - `gfx12-5-generic` emits flags `0x100055b`: `gfx12-5-generic`,
    `xnack`, `sramecc`, `generic_v1`.
  - `gfx1250` emits flags `0x549`: `gfx1250`, `xnack`, `sramecc`.
  - `gfx1251` emits flags `0x55a`: `gfx1251`, `xnack`, `sramecc`.
  - `gfx9-4-generic` emits the analogous feature-bearing generic flags
    `0x100055f`: `gfx9-4-generic`, `xnack`, `sramecc`, `generic_v1`.
  - Forced explicit `-mattr` combinations for `gfx12-5-generic` can encode
    feature-specific ELF flags, but PR review clarified that these are not
    compiler-supported gfx12.5 target-ID combinations that ROCR should register.

No ROCR runtime build was run as part of this investigation.

## ROCR Test Coverage Audit

I looked for existing tests in `projects/rocr-runtime` that would cover this
class of regression.

What exists:

- `rocrtst/suites/test_common/CMakeLists.txt` includes `gfx1250;gfx1251` in the
  default target list.
- `rocrtst` strips feature suffixes from `TARGET_DEVICES` and documents that it
  compiles kernels with bare `-mcpu`, producing XNACK-any concrete code objects.
  This can exercise concrete `gfx1250` / `gfx1251` loader handling on matching
  hardware, but does not exercise `gfx12-5-generic`.
- `rocrtst` builds code objects at code object version 4. The `generic_v1` flag
  used by LLVM generic processors exists in code object version 6 and later.
- `rocrinfo` enumerates agent ISAs and queries `hsa_isa_get_info_alt`, but it is
  a sample/reporting tool, not an assertion-based regression test.
- The installed
  `/srv/vm-shared/rocm/rocm-7.14.0a20260610/bin/rocrtst64 --gtest_list_tests`
  output does not list ISA, generic processor, loader, or code-object tests.

What appears to be missing:

- No `rocrtst` test calls `hsa_isa_from_name`.
- No `rocrtst` test calls `hsa_isa_compatible`.
- No test directly exercises `AmdHsaCode::GetIsa` on an ELF with
  `EF_AMDGPU_MACH_AMDGCN_GFX12_5_GENERIC`.
- No host-side unit-test tree exists under `runtime/hsa-runtime` for the
  internal ISA registry or code-object parser.
- The public deprecated `hsa_isa_compatible` API is not enough to validate COV6
  generic code objects because it calls `Isa::IsCompatible(..., 0)` and has no
  parameter for the ELF generic version. The loader path does pass the parsed
  generic version when checking code-object compatibility.

Recommended future coverage:

- Add a host-side internal test for `IsaRegistry` and `AmdHsaCode::GetIsa` using
  tiny LLVM-generated ELF objects for `gfx12-5-generic`, `gfx1250`, and
  `gfx1251`, including default SRAM ECC/XNACK-supporting flags. Explicit forced
  flag variants can be considered separately if compiler target-ID support
  changes.
- Add a hardware/loader regression test that builds or ships a COV6
  `gfx12-5-generic` HSACO and verifies it loads on gfx1250 and gfx1251 agents.

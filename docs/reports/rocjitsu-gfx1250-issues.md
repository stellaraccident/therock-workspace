# rocjitsu gfx1250 Issues

Date: 2026-06-15

Context:

- rocjitsu source: `worktrees/rocm-systems` at `origin/develop`
- ROCm root: `build/rocm-core/dist/rocm`
- Emulator config: `build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json`
- HRX build: `build/hrx-system-gfx1250`

## 1. Missing `<mutex>` includes

Status: patched locally.

Files:

- `emulation/rocjitsu/lib/simdojo/include/simdojo/sim/event_queue.h`
- `emulation/rocjitsu/tools/rocjitsu/main.cpp`

Symptom:

- Standalone rocjitsu build from `origin/develop` failed because these files use `std::mutex`
  but did not include `<mutex>`.

Public branch search:

- Checked the fetched gfx1250 rocjitsu branches and did not find this fix.

## 2. HIP tests need an HSA runtime rpath

Status: patched locally.

File:

- `emulation/rocjitsu/tests/CMakeLists.txt`

Symptom:

- 43 rocjitsu CTest entries failed to start because generated HIP test binaries could not find
  `libamdhip64.so.7`.
- Re-running those tests with `LD_LIBRARY_PATH=build/rocm-core/dist/rocm/lib` passed.

Local patch:

- Add `-Wl,-rpath,${RJ_HSA_LIBRARY_DIR}` to generated HIP test links.

## 3. Installed CLI does not find `lib64/librocjitsu_kmd.so`

Status: patched locally.

File:

- `emulation/rocjitsu/tools/rocjitsu/main.cpp`

Symptom:

```text
build/rocjitsu-install/bin/rocjitsu --config ... -- /bin/true
rocjitsu: could not find librocjitsu_kmd.so
```

Cause:

- CMake installed `librocjitsu_kmd.so` under `build/rocjitsu-install/lib64`, but the CLI only
  searches `<prefix>/lib/librocjitsu_kmd.so` for installed layouts.

Public branch search:

- Checked fetched gfx1250 rocjitsu branches; they have the same `lib`-only lookup.

## 4. gfx1250 ELF reverse mapping is incomplete

Status: patched locally.

File:

- `emulation/rocjitsu/lib/rocjitsu/src/rocjitsu/code/amdgpu_elf.h`

Symptom:

- The header defines `EF_AMDGPU_MACH_AMDGCN_GFX1250 = 0x49` and maps
  `ROCJITSU_CODE_ARCH_GFX1250` to that machine flag, but `arch_for_elf_mach()` and
  `elf_mach_name()` do not map `0x49` back to `ROCJITSU_CODE_ARCH_GFX1250` / `gfx1250`.

Why this matters:

- rocjitsu code-object parsing and DBT hooks use these helpers for source ISA detection.

Public branch search:

- Checked fetched gfx1250 rocjitsu branches; this reverse mapping was still missing.

## 5. Parallel CTest timeout in `TerminationTest.RequestExitWakesAllPartitions`

Status: not patched.

Symptom:

- Full `ctest -j $(nproc) --timeout 120` had one timeout:
  `TerminationTest.RequestExitWakesAllPartitions`.
- The same test passes standalone and the whole `TerminationTest.*` group passes under CTest.

Current read:

- Looks like a parallel CTest interaction, not a blocker for HRX bring-up.

## 6. HRX gfx1250 device-library load rejects generic code object

Status: fixed for HRX locally by emitting an exact `gfx1250` code object.

Symptom:

```text
hrx gpu debug: create device by ordinal: ... INVALID_ARGUMENT;
[hsa_executable_load_agent_code_object] HSA_STATUS_ERROR_INVALID_CODE_OBJECT:
The code object is invalid.
```

Evidence:

- HRX source-built device library was emitted as:
  `amdgcn-amd-amdhsa--gfx12-5-generic.so`
- `llvm-readelf -h` reports:
  `Flags: 0x100055b, gfx12-5-generic, xnack, sramecc, generic_v1`
- The emulated agent is exact `gfx1250`.

Result:

- HRX rebuilt `amdgcn-amd-amdhsa--gfx1250.so`.
- `llvm-readelf -h` reports `Flags: 0x549, gfx1250, xnack, sramecc`.
- The HSA note reports `amdhsa.target: amdgcn-amd-amdhsa--gfx1250`.
- `hrx_cts_device --hrx-device gpu:0` reaches the test body and all assertions pass.

## 7. HRX GPU CTS segfaults during HSA shutdown after passing tests

Status: not fixed in rocjitsu; HRX CTS has a local opt-in workaround for measurement.

Symptom:

```text
All tests passed (5 assertions in 6 test cases)
Segmentation fault
```

Backtrace summary:

- Main thread:
  `hrx_gpu_shutdown -> iree_hal_amdgpu_driver_destroy -> hsa_shut_down ->
  rocr::core::Runtime::Unload -> rocr::AMD::GpuAgent::ReleaseResources ->
  rocr::AMD::KfdDriver::FreeMemory -> hsakmt_fmm_release ->
  fmm_release_scratch(..., gpu_id=1250)`
- Concurrent ROCR async-event thread:
  `rocr::core::Runtime::AsyncEventsLoop` faulting while loading
  `hsa_signals[pi]->signal_.value`.
- rocjitsu command-processor doorbell polling threads are still alive at the same point.

Runtime knobs tried:

- `HSA_ENABLE_INTERRUPT=0`
- `HSA_NO_SCRATCH_RECLAIM=1`
- `HSA_ENABLE_SCRATCH_ASYNC_RECLAIM=0`

All still reproduced the post-test SIGSEGV.

Local measurement workaround:

- HRX CTS now honors `HRX_CTS_SKIP_ACCELERATOR_SHUTDOWN=1`.
- This lets CTS binaries report the actual test result under rocjitsu while avoiding the known
  post-test shutdown crash.

## 8. Queue/command-buffer writes to host-local mapped buffers report success but read back zero

Status: not fixed in rocjitsu; HRX has a local host-side fallback for stream operations on
host-local buffers.

Symptom:

- Initial HRX `hrx_cts_stream_ops` failed all five cases under rocjitsu/gfx1250.
- Every API call returned OK and stream waits completed, but mapped readback from
  `HRX_MEMORY_TYPE_HOST_LOCAL | HRX_MEMORY_TYPE_DEVICE_VISIBLE` buffers was all zero.
- A CTS control showed the same behavior for direct `hrx_queue_fill()` into a host-local mapped
  buffer: the operation returned OK, signaled its semaphore, and mapped readback was still zero.

Control:

- Existing `hrx_cts_queue_ops` fills/copies device-local buffers and reads back through
  `hrx_synchronous_d2h()`; those tests pass.
- `hrx_cts_transfer` host-local synchronous H2D/D2H tests pass.

Current read:

- The failing path is device queue/command-buffer transfer operations writing directly to
  host-local mapped memory under rocjitsu/gfx1250.
- HRX now avoids that path for stream fill/copy/update when all touched buffers are host-local:
  it synchronizes prior stream work, maps the host-local buffers, and performs the byte operation
  on the host.

Result:

- `hrx_cts_stream_ops` now passes under rocjitsu/gfx1250.
- Full HRX CTS sweep with `HRX_CTS_SKIP_ACCELERATOR_SHUTDOWN=1` passes:
  `host_allocator lifecycle device allocator semaphore stream memory transfer stream_ops executable
  queue_ops status refcount virtual_memory cxx_api`.

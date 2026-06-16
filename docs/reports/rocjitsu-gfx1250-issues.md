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

Status: fixed locally in ROCR; HRX remains on generic code-object emission.

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
- After reconfiguring HRX from the reverted source, the Ninja graph embeds only
  `amdgcn-amd-amdhsa--gfx12-5-generic.so`; the prior exact `gfx1250` artifact was stale build output.
- Before the ROCR fix, `rocminfo` under rocjitsu advertised the emulated `gfx1250` agent plus
  `amdgcn-amd-amdhsa--gfx12-generic`, not `amdgcn-amd-amdhsa--gfx12-5-generic`.
- ROCR's private code-object parser also lacked the `gfx12-5-generic` ELF machine value
  `0x05b`, even though LLVM emits it for this object.

Result:

- Local rocm-systems commits:
  - `a502195009 Map gfx1250 to gfx12-5 generic ISA`
  - `82dc19249d Teach ROCR about gfx12-5 generic code objects`
- The first commit registers `gfx12-5-generic` as a generic ISA family and maps `gfx1250` agents to it.
- The second commit adds the ROCR private `EF_AMDGPU_MACH_AMDGCN_GFX12_5_GENERIC = 0x05b`
  value and maps it to the `gfx12-5-generic` ISA name in `libamdhsacode`.
- After rebuilding `ROCR-Runtime+dist`, `rocminfo` under rocjitsu advertises:
  `amdgcn-amd-amdhsa--gfx1250` and `amdgcn-amd-amdhsa--gfx12-5-generic`.
- The generic-only HRX device-library test passes under rocjitsu daemon mode:
  `runtime/src/iree/hal/drivers/amdgpu/util/device_library_test`.

Public branch search:

- Searched fetched public rocm-systems branches for `gfx12-5-generic` and sampled branches with
  `gfx1250` ROCR work; no existing ROCR fix for this generic ISA mapping was found.

## 7. HRX GPU CTS segfaults during HSA shutdown after passing tests

Status: not fixed in rocjitsu; the temporary HRX CTS shutdown workaround has been stashed and the
tree was returned to principled fixes only.

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

Temporary measurement workaround:

- HRX CTS now honors `HRX_CTS_SKIP_ACCELERATOR_SHUTDOWN=1`.
- This lets CTS binaries report the actual test result under rocjitsu while avoiding the known
  post-test shutdown crash.
- This was useful for sizing but is not active in the current HRX branch. It is saved in
  `sources/hrx-system` as:
  `stash@{0}: wip ctest emulator launcher prototype`.

## 8. Queue/command-buffer writes to host-local mapped buffers report success but read back zero

Status: not fixed in rocjitsu; the temporary HRX host-side stream fallback has been reverted.

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
- The earlier HRX fallback avoided that path for stream fill/copy/update when all touched buffers
  were host-local, but that was a measurement workaround and has been reverted.

Temporary measurement result:

- `hrx_cts_stream_ops` now passes under rocjitsu/gfx1250.
- Full HRX CTS sweep with `HRX_CTS_SKIP_ACCELERATOR_SHUTDOWN=1` passes:
  `host_allocator lifecycle device allocator semaphore stream memory transfer stream_ops executable
  queue_ops status refcount virtual_memory cxx_api`.

## 9. Daemon mode reuses a closed `RemoteDriver`

Status: patched locally in rocjitsu.

File:

- `emulation/rocjitsu/lib/rocjitsu/src/rocjitsu/kmd/linux/interposer.cpp`

Symptom:

```text
RESOURCE_EXHAUSTED; [hsa_executable_load_agent_code_object]
HSA_STATUS_ERROR_OUT_OF_RESOURCES
```

Reduced reproducer before the patch:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/allocator_test \
      --gtest_filter=AllocatorTest.QueryMemoryHeapsReportsHsaLimits:AllocatorTest.OversizedAllocationIsRejectedByCompatibility
```

Observed behavior before the patch:

- First gtest passed.
- The second `TestLogicalDevice::Initialize(...)` failed while loading the device library code
  object:
  `hsa_executable_load_agent_code_object -> HSA_STATUS_ERROR_OUT_OF_RESOURCES`.
- The process then hung until killed or timed out.

Cause:

- In daemon mode, the interposer caches one `RemoteDriver*` and one synthetic KFD fd.
- On `close(remote_kfd_fd_)`, the interposed `close()` called `RemoteDriver::close()` but left
  `remote_` and `remote_kfd_fd_` cached.
- A later `open("/dev/kfd")` in the same process reused the stale `RemoteDriver` whose socket had
  already been closed.

Local patch:

- Add explicit remote close helpers.
- Keep the daemon `RemoteDriver` alive after the synthetic KFD fd is closed; only close the
  synthetic memfd at `close(fd)` time and defer `RPC_CLOSE` to the interposer destructor.
- Add a KFD-ioctl fallback so late ROCR shutdown ioctls still route to the remote driver even if
  fd bookkeeping has already been partially torn down.

Verification:

- Rebuilt and installed rocjitsu from `worktrees/rocm-systems`.
- The reduced two-gtest command no longer reports `RESOURCE_EXHAUSTED` at code-object load.
- It now reaches the second test body, then exposes the separate HSA shutdown hang below.

Public branch search:

- Checked `origin/develop` and relevant public branches:
  `users/kuhar/gfx1250-dbt-rdna4`, `users/kuhar/gfx1250-isa`,
  `users/arosa/rj/plugins`, `users/amd-eochoalo/2026-06-12/rocjitsu-test-corpus-ci`,
  `gfx1250_topo_fix`, `mingsun/npi-gfx1250-json-config`,
  `users/gbaraldi/gfx1250fixes`, and `users/habajpai-amd/gfx1250-test-support`.
- No focused fix for this stale remote-driver close path was found.

## 10. HSA shutdown spins while freeing scratch memory

Status: patched locally in ROCR/rocjitsu.

Symptom:

- A single allocator gtest passes quickly, prints `[       OK ]`, and then never exits under
  `rocjitsu --daemon`.
- The test process consumes high CPU after the gtest body completes.

Reduced reproducer:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 180s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/allocator_test \
      --gtest_filter=AllocatorTest.OversizedAllocationIsRejectedByCompatibility
```

Observed behavior:

- The gtest body passed in about 113 ms.
- After the pass line, the process spun until killed.
- `ps -L` showed three runnable/high-CPU `allocator_test` threads.

Backtrace summary:

- Main test thread:
  `AllocatorTest::TearDownTestSuite -> iree_hal_amdgpu_libhsa_deinitialize ->
  hsa_shut_down -> rocr::core::Runtime::Unload -> rocr::AMD::GpuAgent::ReleaseResources ->
  rocr::AMD::KfdDriver::FreeMemory(mem=0x4c00000000, size=68719476736) ->
  hsakmt_fmm_release -> fmm_release_scratch -> _fmm_unmap_from_gpu_scratch`.
- ROCR async event thread:
  `rocr::core::Runtime::AsyncEventsLoop -> Signal::WaitAnyExceptions ->
  hsaKmtWaitOnMultipleEvents_ExtCtx -> hsakmt_ioctl(fd=960, ...)`.
- rocjitsu daemon:
  main thread blocked in `accept()`, engine worker sleeping, command-processor doorbell polling
  threads still alive.

Current read:

- The original infinite loop was caused by a scratch object with `mapping_count = 1` but
  `mapped_device_id_array == NULL`. `_fmm_unmap_from_gpu_scratch` returned without freeing the
  object, and `fmm_release_scratch` retried the same aperture tree node forever.
- A second shutdown stall came from the per-object `mmap(PROT_NONE|MAP_FIXED)` used to preserve
  scratch CPU VA. Full scratch aperture release immediately releases the whole scratch address
  space, so the local patch skips that per-object CPU remap in the full-release path while
  preserving existing behavior for public scratch unmap.
- rocjitsu `UNMAP_MEMORY_FROM_GPU` also now removes the allocation from the emulated GPU page table.

Verification:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 90s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/allocator_test \
      --gtest_filter=AllocatorTest.OversizedAllocationIsRejectedByCompatibility
```

Result: passed and exited cleanly with `EXIT:0`.

## 11. Daemon GPU writes to plain client host pointers are not reflected in the client

Status: patched locally in rocjitsu.

Symptom:

- `AllocatorTest.DeviceAllocationImportWrapsHsaAllocation` and
  `AllocatorTest.DeviceAllocationExportReportsHsaPointer` initially wrote the expected pattern
  into emulated GPU memory but read back zero from the client-side destination buffer.
- Single-process host queue tests using only daemon-shared allocations still passed.

Reduced reproducer:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 90s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/allocator_test \
      --gtest_filter=AllocatorTest.DeviceAllocationImportWrapsHsaAllocation
```

Root cause evidence:

- Temporary memory tracing showed the fill dispatch writing the expected pattern to the emulated
  device allocation.
- The subsequent `hsa_memory_copy` device-to-host dispatch wrote to an ordinary client stack/host
  address such as `0x7fff...`.
- In daemon mode, that address is not in the daemon GPU page table, so the write fell through to
  daemon-local sparse memory instead of writing into the client process.

Local patch:

- Record the client OS pid from `SO_PEERCRED` when a daemon connection is accepted.
- Thread that pid through daemon device open / client-pid attachment and `GpuMemory` VMID state.
- On GPU memory translation miss for userspace addresses, use `process_vm_readv` /
  `process_vm_writev` to read/write the client process.
- Keep normal page-table resolution first, so daemon-shared allocations still use the existing
  memfd-backed path.

Verification:

- After rebuilding/installing rocjitsu, both single tests pass and exit:
  - `.tmp/allocator-import-single-restored.log`: `rc=0`,
    `AllocatorTest.DeviceAllocationImportWrapsHsaAllocation` passed.
  - `.tmp/allocator-export-single-restored.log`: `rc=0`,
    `AllocatorTest.DeviceAllocationExportReportsHsaPointer` passed.

Public branch search:

- Searched fetched public refs for `SO_PEERCRED`, `process_vm_writev`, `client_pid`, and
  client-aware `open_process` patterns under `emulation/rocjitsu`; no matching prior fix was
  found.

## 12. Daemon creates separate simulated KFD processes for repeated opens from one OS process

Status: patched locally in rocjitsu.

Symptom:

- The individual import/export allocator tests pass when run as separate processes.
- Running either of these reduced two-test sequences in one `allocator_test` process used to hang
  on the second test:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 90s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/allocator_test \
      --gtest_filter=AllocatorTest.DeviceAllocationImportWrapsHsaAllocation:AllocatorTest.DeviceAllocationExportReportsHsaPointer
```

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 90s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/allocator_test \
      --gtest_filter=AllocatorTest.DeviceAllocationImportRejectsUnknownPointer:AllocatorTest.DeviceAllocationExportReportsHsaPointer
```

Observed behavior:

- First test in each sequence passes.
- Second test reaches `AllocatorTest.DeviceAllocationExportReportsHsaPointer` and hangs in
  `QueueFillAndWait`.
- Logs repeatedly show stale event sets such as:
  `SET_EVENT_MISS: event_id=1 events_.size()=0`.

Root cause evidence:

- Temporary lifecycle tracing showed the first logical device using rocjitsu process `pid=2`,
  creating events there.
- The second logical device opened a new daemon connection and got rocjitsu process `pid=3`.
- ROCR then waited/set event ids created under `pid=2`, but `pid=3` had no matching
  `CREATE_EVENT` calls, so `SET_EVENT_MISS ... events_.size()=0` was expected.
- Real KFD state is per OS process, not per daemon socket connection. Multiple `/dev/kfd` opens
  from one process should share the same simulated KFD process/PASID state.

Backtrace summary from `.tmp/allocator-test-post-doorbell-gdb-bt.txt`:

- Main test thread:
  `AllocatorTest_DeviceAllocationExportReportsHsaPointer_Test::TestBody ->
  QueueFillAndWait -> iree_hal_semaphore_list_wait`.
- HRX host queue completion threads:
  `iree_hal_amdgpu_host_queue_completion_thread_main ->
  hsa_amd_signal_wait_any -> rocr::core::Signal::WaitMultiple ->
  hsaKmtWaitOnMultipleEvents_ExtCtx`.
- ROCR async-event threads:
  `rocr::core::Runtime::AsyncEventsLoop -> hsaKmtWaitOnMultipleEvents_ExtCtx`.
- rocjitsu client side:
  one thread is in `RemoteDriver::send_ioctl` waiting for a `WAIT_EVENTS` RPC response payload;
  other ROCR event threads are blocked on the same `rpc_mutex_`.
- rocjitsu daemon side:
  daemon handler threads are back in `rpc_recv_exact(..., total_bytes=16)` waiting for another
  request, and command-processor doorbell polling threads are alive.

Hypotheses tried and reverted:

- Resetting reused doorbell slots to `~0ULL` before queue registration. This did not clear the
  second-test hang.
- Sending daemon ioctl response header+payload as a single buffer in the no-fd path. This did not
  clear the second-test hang.
- Short-circuiting signal-age `WAIT_EVENTS` polls client-side. This was too broad and broke the
  first import test, so it was reverted.

Current read:

- This is not a gfx1250 code-object/generic-target problem and not the original host-pointer
  readback problem.
- Daemon mode was modeling each client socket as a separate KFD process, while libhsakmt/ROCR
  expects repeated opens in the same OS process to share KFD process state.

Local patch:

- Add an open reference count to `KfdProcess`.
- Change daemon `SimulatedDriver::open_process` to accept a client OS pid.
- When `client_pid > 0`, reuse an existing `KfdProcess` for that OS process and increment the
  open count instead of allocating a fresh rocjitsu process id.
- In daemon close, only destroy the `KfdProcess` when the last open reference closes.
- Add `rj_vm_device_open_for_client_pid` and have `rocjitsu --daemon` use it during handshake.

Verification:

- Reduced same-process pair now passes:
  `.tmp/allocator-import-export-pair-after-process-reuse.log`: `rc=0`, 2 tests passed.
- Full allocator test binary now passes under daemon mode:
  `.tmp/allocator-test-clean-process-reuse.log`: `rc=0`, 11 passed, 1 skipped.
- A broader non-benchmark AMDGPU CTest slice got past allocator and the first 12 CTest entries
  before exposing the separate host queue command-buffer issue below:
  `.tmp/ctest-rocjitsu-gfx1250-amdgpu-after-process-reuse/summary.tsv`.

Public branch search:

- Searched fetched public refs for `SO_PEERCRED`, `process_vm_writev`, `client_pid`, and
  client-aware `open_process` patterns under `emulation/rocjitsu`; no matching prior fix was
  found.

## 13. Host queue command-buffer transfer replay reports completion but writeback remains zero

Status: not fixed; newly exposed after fixing daemon process reuse.

Symptom:

- `iree/hal/drivers/amdgpu/host_queue_command_buffer_test` runs under rocjitsu/gfx1250 daemon
  mode, but three transfer command-buffer replay tests fail because the output buffer remains
  zero after the queue reports completion:
  - `HostQueueCommandBufferTest.SingleBlockCommandBufferParksAndResumesUnderNotificationPressure`
  - `HostQueueCommandBufferTest.DeferredTransientBindingSurvivesQueuedDealloca`
  - `HostQueueCommandBufferTest.OneShotStaticTransientBindingRecordsBeforeAllocaCommit`

Reduced reproducer:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/host_queue_command_buffer_test \
      --gtest_filter=HostQueueCommandBufferTest.SingleBlockCommandBufferParksAndResumesUnderNotificationPressure
```

Observed behavior:

- The test reaches the final readback and fails:
  `actual = 0`, `expected = 3174694913` (`0xBD3A0001`).
- Full binary daemon log:
  `.tmp/ctest-rocjitsu-gfx1250-amdgpu-after-process-reuse/iree__hal__drivers__amdgpu__host_queue_command_buffer_test.log`
- Single-test daemon log:
  `.tmp/host-queue-cmd-buffer-single-daemon.log`
- The same single test also fails in rocjitsu local mode before crashing during shutdown:
  `.tmp/host-queue-cmd-buffer-single-local.log`.

Current read:

- This is a different failure class from the prior `RESOURCE_EXHAUSTED`, `SET_EVENT_MISS`, and
  plain client-host-pointer daemon bridging issues.
- Because local mode also fails with zero writeback, the issue does not appear to be caused by
  daemon process identity or `process_vm_writev`.
- The failed tests exercise parked/replayed host-queue command-buffer transfer work and transient
  binding lifetimes. Semaphores report completion, but the expected fill/copy result is never
  visible in the output buffer.

## 14. Functional-only CTest sweep status

Status: active triage. Benchmarks are intentionally excluded from the current sweep.

Selection:

```bash
ctest --test-dir build/hrx-system-gfx1250 \
  -L 'runtime-resource=amd-gpu|iree-run-requirement=runtime.resource.amd_gpu|requires-gpu-amd' \
  -LE 'benchmark|manual|loom/test/corpus/authoring' -N
```

Result:

- 73 functional GPU-labelled tests are selected after excluding benchmarks and Loom authoring
  tests.
- A prior daemon sweep with the looser `-LE benchmark` selected 77 tests and included expensive
  Loom authoring tests. Those are no longer part of the active functional sweep.
- Current best run after the local rocjitsu fixes is 70/73 passing:
  `.tmp/hrx-functional-ctest-rocjitsu-after-indirect-retry.log`.
- The three remaining CTest failures are:
  - `iree/hal/drivers/amdgpu/host_queue_command_buffer_profiling_test`
  - `iree/hal/drivers/amdgpu/host_queue_pending_test`
  - `iree/hal/drivers/amdgpu/cts/queue_dispatch_tests`
- The HRX CTS layer now passes in this functional sweep:
  `hrx_cts_host_allocator`, `hrx_cts_lifecycle`, `hrx_cts_device`, `hrx_cts_allocator`,
  `hrx_cts_semaphore`, `hrx_cts_stream`, `hrx_cts_memory`, `hrx_cts_transfer`,
  `hrx_cts_stream_ops`, `hrx_cts_executable`, `hrx_cts_queue_ops`, `hrx_cts_status`,
  and `hrx_cts_refcount`.
- `util/target_id_test` and `util/device_library_target_test` failures from the first sweep were
  stale HRX build skew. After relinking the binaries, their reduced tests pass under daemon mode.

Canonical functional sweep command:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 6h $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- ctest --test-dir $PWD/build/hrx-system-gfx1250 \
      -L 'runtime-resource=amd-gpu|iree-run-requirement=runtime.resource.amd_gpu|requires-gpu-amd' \
      -LE 'benchmark|manual|loom/test/corpus/authoring' \
      --output-on-failure -j1
```

## 15. VMem dmabuf/libdrm path failed before mapping

Status: patched locally in rocjitsu.

Symptom before the patch:

- `iree/hal/drivers/amdgpu/util/vmem_test` failed `RingbufferLifetime` and `RingbufferWrap`.
- The first failure was:
  `hsa_amd_vmem_handle_create -> HSA_STATUS_ERROR`.

Reduced reproducer:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/util/vmem_test \
      --gtest_filter=VMemTest.RingbufferLifetime
```

Evidence:

- KMT debug trace reached `hsaKmtExportDMABufHandleCtx` for the device allocation and then failed
  creating the VMem handle.
- Strace showed the daemon exporting a memfd over `SCM_RIGHTS`, but the client then called:
  `ioctl(0, DRM_IOCTL_PRIME_FD_TO_HANDLE, ...) = -1 ENOTTY`.
- rocjitsu's fake `amdgpu_device_get_fd` returned the KFD fd instead of a synthetic DRM render fd.
- rocjitsu also did not fake the libdrm BO import/export/query/free/VA-op path, so real libdrm
  saw rocjitsu's dummy device handle and read a bogus internal fd.

Local rocjitsu patch:

- Track synthetic DRM render fds separately from KFD fds.
- Give fake libdrm device handles their own duplicated render fd and return it from
  `amdgpu_device_get_fd`.
- Send `AMDKFD_IOC_EXPORT_DMABUF` fds through daemon RPC with `SCM_RIGHTS` and install the
  received client fd in the caller's ioctl args.
- Fake enough libdrm BO operations for rocjitsu-created handles:
  `amdgpu_bo_import`, `amdgpu_bo_export`, `amdgpu_bo_query_info`,
  `amdgpu_bo_set_metadata`, `amdgpu_bo_va_op`, `amdgpu_bo_cpu_map`,
  `amdgpu_bo_cpu_unmap`, `amdgpu_bo_free`, and
  `drmCommandWriteRead(DRM_AMDGPU_GEM_MMAP)`.
- Record the mmap offset associated with the exported KFD allocation and use the fake BO's dmabuf
  fd for client-side DRM `mmap` after ROCR frees the original KFD allocation.

Verification:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/util/vmem_test
```

Result after patch:

- `VMemTest.FindCoarseGlobalMemoryPool`: passed.
- `VMemTest.FindFineGlobalMemoryPool`: passed.
- `VMemTest.RingbufferLifetime`: passed.
- `VMemTest.RingbufferWrap`: passed.

Residual risk:

- This patch is intentionally minimal for the VMem path. It models enough DRM BO lifetime and
  mapping behavior for ROCr/HRX functional tests, but rocjitsu authors should decide whether the
  longer-term model should retain exported allocations in the daemon, implement a fuller fake
  DRM/BO object table, or both.

## 16. CTS queue/command-buffer/dispatch/executable writes were zero or stale

Status: patched locally in rocjitsu.

Representative reduced reproducers:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/cts/command_buffer_tests \
      --gtest_filter=CTS/CommandBufferCopyBufferTest.CopySubBuffer/amdgpu
```

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/cts/queue_tests \
      --gtest_filter=CTS/QueueTransferTest.FillEntireBuffer_1Byte/amdgpu
```

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 120s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/cts/dispatch_tests \
      --gtest_filter=CTS/DispatchConstantsTest.DispatchWithDispatchConstants/amdgpu_amdgpu_gfx1250
```

Observed behavior before the patch:

- `CommandBufferCopyBufferTest.CopySubBuffer`: expected subrange pattern, actual zeros.
- `QueueTransferTest.FillEntireBuffer_1Byte`: fill completes but readback is zeros; some runs also
  show shutdown cleanup noise after the failed assertion.
- `DispatchConstantsTest.DispatchWithDispatchConstants`: expected `{11,22,33,44}`, actual
  `{0,0,0,0}`.
- `ExecutableTest.LookupGlobalByName`: global readback is zero instead of the expected constant.

Root cause evidence:

- Temporary VM/CP tracing on `QueueTransferTest.FillEntireBuffer_1Byte` showed the fill dispatch
  packet decoded with `grid=[16,1,1]` and `workgroup=[32,1,1]`, but rocjitsu computed zero work
  groups by integer-dividing `grid_size_x / workgroup_size_x`.
- The readback dispatch then copied from an unfilled buffer, so completion was real but the
  expected side effect had never been launched.
- Indirect dispatch tests later exposed transient invalid packet reads from the queue. The raw
  header logged through the resolved host backing was already a valid kernel dispatch header, but
  the command processor had stopped on the invalid read and did not reschedule itself.

Local rocjitsu patch:

- Compute AQL workgroup counts with ceil division instead of floor division.
- Propagate VMID through simdojo memory messages so backing memory reads/writes use the submitting
  process address space.
- Route gfx1250 SDMA wait/source/destination/signal addresses through rocjitsu memory resolution
  instead of treating client virtual addresses as daemon-local host pointers.
- Make memory-side cache writes immediately update backing memory and leave the line clean, which
  avoids stale readback for the functional CTS paths exercised here.
- On transient invalid queue packets, schedule another command-processor event instead of
  permanently stalling until an unrelated doorbell arrives.

Verification:

- `queue_tests` now passes as a full binary: `.tmp/repro-queue-tests-after-ceildiv.log`
  (`63 passed`, `1 skipped`).
- Indirect dispatch reduced reproducers now pass:
  - `.tmp/repro-dispatch-indirect-static-retry-only.log`
  - `.tmp/repro-queue-dispatch-indirect-static-retry-only.log`
- In the current benchmark-excluded CTest sweep, `dispatch_tests`, `queue_tests`,
  `command_buffer_tests`, `executable_tests`, and all HRX CTS binaries pass. The only remaining
  CTS failure is the profiling-only subset of `queue_dispatch_tests`, covered below.

Residual risk:

- The invalid-packet retry is a correctness-preserving workaround for a transient queue visibility
  race. It avoids deadlock and matches the later valid host-backing observation, but rocjitsu
  authors should decide whether the deeper fix belongs in queue memory ordering or packet fetch.
- A direct host AQL fetch experiment was tried and reverted because it regressed the indirect
  dispatch tests.

## 17. Device queue dispatch profiling timestamps remain zero

Status: not fixed.

Reduced reproducer:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 90s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/cts/queue_dispatch_tests \
      --gtest_filter=CTS/QueueDispatchTest.DispatchDeviceQueueEventProfiling/amdgpu_amdgpu_gfx1250:CTS/QueueDispatchIndirectParametersTest.StaticParametersWhileProfiling/amdgpu_amdgpu_gfx1250:CTS/QueueDispatchIndirectParametersTest.DynamicParametersWhileProfiling/amdgpu_amdgpu_gfx1250
```

Observed behavior:

- The three profiling tests complete without timeout but fail their timestamp checks.
- `records[i].start_tick`, `records[i].end_tick`, and event profiling ticks remain zero even
  though the dispatches complete.
- Repro log: `.tmp/repro-queue-dispatch-profiling-only.log`.

Current read:

- The non-profiling queue/dispatch CTS paths now pass, so the remaining issue is specific to
  device dispatch profiling metadata.
- I did not find code in rocjitsu that writes dispatch profiling start/end ticks into the HSA
  profiling records or event state. `completion_tracker.cpp` updates completion signals, and
  `CommandProcessor::handle_doorbell(simdojo::Tick timestamp)` receives a timestamp, but that
  timestamp is not carried through the dispatch entry to a profiling record.

Next evidence needed:

- Decide with rocjitsu authors whether to synthesize monotonic profiling timestamps in the
  emulator, and confirm the exact profile-record layout/offsets expected by ROCR/IREE before
  patching.

## 18. Host queue command-buffer profiling leaks or poisons daemon resources

Status: not fixed.

Reduced reproducers:

```bash
env ROCM_PATH=$PWD/build/rocm-core/dist/rocm \
  LD_LIBRARY_PATH=$PWD/build/rocm-core/dist/rocm/lib:${LD_LIBRARY_PATH:-} \
  timeout 90s $PWD/build/rocjitsu-install/bin/rocjitsu \
    --daemon \
    --config $PWD/build/rocjitsu-install/share/rocjitsu/configs/amdgpu_gfx1250.json \
    -- $PWD/build/hrx-system-gfx1250/runtime/src/iree/hal/drivers/amdgpu/host_queue_command_buffer_profiling_test \
      --gtest_filter=HostQueueCommandBufferProfilingTest.AutoCommandBufferModeUsesPm4WhileDispatchProfiling:HostQueueCommandBufferProfilingTest.SuppressedDeviceFineMemoryAllowsDispatchProfiling
```

Observed behavior:

- `AutoCommandBufferModeUsesPm4WhileDispatchProfiling` passes.
- The following `SuppressedDeviceFineMemoryAllowsDispatchProfiling` test aborts or times out while
  loading the device library with:
  `HSA_STATUS_ERROR_OUT_OF_RESOURCES` from `hsa_executable_load_agent_code_object`.
- The same suppressed test passes when run alone:
  `.tmp/repro-hqcb-profiling-suppressed.log`.
- Pair log: `.tmp/repro-hqcb-profiling-pair-dispatch-suppressed.log`.
- Full binary log: `.tmp/repro-hqcb-profiling-full.log`.
- The abort path can also print a KFD memory critical error for an address already in use.

Current read:

- This is order-dependent and follows a PM4 dispatch profiling path, so it looks like rocjitsu
  daemon resource cleanup or memory mapping state is left inconsistent after that test.
- It is no longer the stale-`RemoteDriver` failure fixed earlier: the reduced allocator
  resource-exhaustion repro remains fixed.

Next evidence needed:

- Trace rocjitsu KFD allocation/map/free state across the PM4 dispatch profiling test and the
  subsequent device-library load.
- Confirm whether a profiling command-buffer allocation is leaked, double-mapped, or kept in the
  emulated GPU page table after close.

## 19. Host queue pending timeout appears secondary to the profiling timeout

Status: not fixed independently; likely contamination from the previous CTest failure.

Observed behavior:

- In full functional CTest sweeps, `host_queue_pending_test` times out at
  `HostQueuePendingTest.CapacityParkedHostActionRetriesAfterPostDrain`.
- Running the full `host_queue_pending_test` binary alone under the same daemon mode passes:
  `.tmp/repro-host-queue-pending-full.log`.
- Running the specific timed-out test alone also passes:
  `.tmp/repro-host-queue-pending-capacity.log`.

Current read:

- `host_queue_pending_test` runs immediately after
  `host_queue_command_buffer_profiling_test` in the functional CTest order. Because it passes
  independently, the CTest timeout is most likely daemon/global-state contamination after the
  profiling test timeout or abort.
- Once section 18 is fixed or isolated, this test should be rerun in the full functional sweep
  before treating it as a separate bug.

# rocprofv3 Kernel Timing: Queue Interception Pivot Analysis

Rev1. Source baseline: `sources/TheRock/rocm-systems` at `8017ffed34` (`RCCL: fix ProcessIsolatedRegisterTests for NCCL_LOCAL_REGISTER default (#4766)`).

## Executive Read

The source supports a case for moving high-level HIP kernel timing away from the current HSA queue interception path. Today, `rocprofiler-sdk` treats queue interception as the primitive for kernel dispatch tracing, dispatch counting, PC sampling attachment, thread trace attachment, scratch reporting, and attach-time queue discovery. For `rocprofv3 --kernel-trace`, this means a timing-only feature enters a packet-rewrite pipeline: queue creation is replaced, packets are copied into an intercept queue, each doorbell is serialized through an interceptor, kernel completion signals are substituted, callback state is allocated, and completion is later resolved through an async HSA signal handler.

That design can produce accurate timestamps through `hsa_amd_profiling_get_dispatch_time`, but it does not merely observe the runtime. It changes the dispatch path. This is the architectural mismatch behind the performance/veracity concern: high-level timing should be reported by the runtime path that already owns command construction, completion signals, correlation, and timestamp extraction, while packet rewriting should become an explicit non-default path for services that actually need to inject AQL or serialize dispatches.

The most plausible target direction is:

1. Add or formalize a HIP/CLR activity callback surface for kernel timing records that is consumable by `rocprofiler-sdk`.
2. Route `rocprofv3 --kernel-trace` through that runtime activity source by default.
3. Keep HSA queue interception for counter collection, PC sampling markers, thread trace dispatch control, scratch reporting if still dependent, process attach proxy queues, and any tool explicitly requesting packet-level interception.
4. Preserve the public `rocprofiler-sdk` kernel dispatch record schema initially, but mark the source as runtime-reported versus queue-intercepted internally so behavior and regressions are diagnosable.

## Current Architecture, Source-Grounded

### 1. Runtime table registration is the rendezvous point

`rocprofiler-register` is the load-time coordinator. Its README says runtimes pass intercept API tables to `rocprofiler-register`; if tools are visible, it loads/passes the table to rocprofiler (`projects/rocprofiler-register/README.md:5`, `:10`). The library names and symbols are hard-coded for HSA, HIP, ROCTx, RCCL, attach, etc. (`projects/rocprofiler-register/source/lib/rocprofiler-register/rocprofiler_register.cpp:207`, `:212`, `:242`).

HSA registers its API table in `Runtime::LoadTools()`:

- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/runtime.cpp:2655`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/runtime.cpp:2666`

HIP registers its dispatch tables in `ToolsInit()`:

- `projects/clr/hipamd/src/hip_api_trace.cpp:1467`
- `projects/clr/hipamd/src/hip_api_trace.cpp:1471`
- `projects/clr/hipamd/src/hip_api_trace.cpp:1493`

When a table arrives, `rocprofiler-sdk` copies the original table, installs wrappers, and notifies intercept-table clients:

- HIP path: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp:1129`, `:1140`, `:1143`, `:1153`
- HSA path: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp:1195`, `:1203`, `:1215`, `:1218`, `:1232`, `:1250`
- Attach path: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp:1418`, `:1428`

Important implication: a runtime-reported timing path does not need a new rendezvous concept. The same registration machinery can deliver a new HIP/CLR profiling table or callback registration API to the SDK.

### 2. HSA queue interception is packet rewriting, not passive observation

ROCr exposes tool-only queue intercept functions in `hsa_api_trace.h`:

- Marker packet contract: `projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_api_trace.h:75`
- Intercept handler type: `projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_api_trace.h:114`
- `hsa_amd_queue_intercept_register`: `projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_api_trace.h:118`
- `hsa_amd_queue_intercept_create`: `projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_api_trace.h:121`

The runtime implementation creates a normal HSA queue, wraps it in `core::InterceptQueue`, and only allows `hsa_amd_queue_intercept_register` on that wrapped type:

- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:1359`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:1372`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:1378`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:1386`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:1394`

`InterceptQueue` explicitly says host-side dispatches are processed during doorbell ring, while device-side dispatches are handled via an async signal event (`projects/rocr-runtime/runtime/hsa-runtime/core/inc/intercept_queue.h:199`). It maintains a mutex, overflow buffer, staging buffer, async doorbell signal, and interceptor callback vector (`projects/rocr-runtime/runtime/hsa-runtime/core/inc/intercept_queue.h:218`, `:224`, `:234`, `:241`, `:247`).

The implementation is correspondingly complex:

- Constructor replaces the queue base address with a proxy buffer, creates an async doorbell, registers an async handler, and installs a final submit interceptor: `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/intercept_queue.cpp:114`, `:127`, `:147`, `:151`, `:157`
- Retry logic depends on read-index assumptions, with a FIXME noting the assumption could be removed by using a distinct interrupt signal: `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/intercept_queue.cpp:84`, `:95`, `:105`
- Submission counts marker packets, reserves queue space, can insert retry barriers, writes packets into the wrapped hardware queue, and rings the wrapped doorbell: `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/intercept_queue.cpp:213`, `:216`, `:224`, `:258`, `:269`, `:323`, `:325`
- Doorbell handling takes a lock, handles overflow, scans valid packets, copies wrap-around packets into staging, invokes the interceptor chain, invalidates consumed proxy packets, and advances the proxy read index: `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/intercept_queue.cpp:333`, `:343`, `:345`, `:365`, `:398`, `:407`, `:416`, `:426`, `:433`

This is strong evidence for the architects' concern. The path is not a cheap callback. It is a second queue implementation with rewrite semantics and retry mechanics.

### 3. rocprofiler-sdk installs queue interception for kernel tracing

`QueueController::init()` overwrites HSA queue create/destroy when `enable_queue_intercept()` returns true:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:316`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:327`

`enable_queue_intercept()` returns true for registered contexts with kernel tracing, counters, PC sampling, scratch reporting, device counting, device thread trace, or dispatch thread trace:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:503`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:514`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:520`

That means `rocprofv3 --kernel-trace` alone is sufficient to switch the HSA queue implementation.

When the SDK intercepts queue creation, it constructs a `rocprofiler::hsa::Queue` that calls `hsa_amd_queue_intercept_create`, enables profiling on the intercept queue, registers the SDK `WriteInterceptor`, and returns the intercept queue to the application:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:41`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:55`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:518`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:532`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:543`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:582`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:592`

Attach mode has an analogous proxy queue path in `librocprofiler-sdk-attach`: it rewrites HSA queue creation, creates intercept queues, enables profiling, registers a shim interceptor immediately, tracks queues, and later lets the SDK set the actual write interceptor:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/attach.cpp:150`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/attach.cpp:156`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/attach.cpp:165`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/queue_registration.cpp:114`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/queue_registration.cpp:135`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/queue_registration.cpp:146`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/queue_registration.cpp:164`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-attach/queue_registration.cpp:211`

This is a separate reason to avoid making queue interception the default for simple timing: even late attach has a legitimate need for proxy queues, but that should not force the same mechanism onto normal launch-mode timing.

### 4. Kernel trace collection rewrites individual dispatch packets

The SDK `WriteInterceptor` is the core of the current kernel dispatch model:

- It is explicitly a queue write interceptor: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:196`
- It bypasses work only if finalizing, no packets, no queue callbacks, and no active kernel-dispatch tracing contexts: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:210`, `:236`
- It scans every packet in the write: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:260`
- It filters kernel dispatch packets by packet header: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:263`, `:267`
- It constructs or obtains correlation state: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:273`
- It substitutes the kernel completion signal with an SDK-owned signal: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:318`, `:327`
- It constructs the public `rocprofiler_kernel_dispatch_info_t`: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:336`, `:341`
- It invokes enqueue callbacks, service callbacks, optional serializer packets, service-injected before/after packets, and optional PC sampling marker packets: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:357`, `:384`, `:400`, `:411`, `:420`, `:444`
- It emits the transformed packet vector to the intercept queue writer: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:507`
- It registers an async signal handler for completion: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:491`

Completion then calls `hsa_amd_profiling_get_dispatch_time`, emits callback/buffer records, runs service completion callbacks, cleans up signals, decrements correlation state, and decrements active-kernel accounting:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:100`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:115`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:117`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:121`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:141`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:159`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:170`

The timestamp itself is not inferred from queue snooping; it is retrieved from HSA profiling on the completion signal:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/kernel_dispatch/profiling_time.cpp:42`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/kernel_dispatch/profiling_time.cpp:50`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/kernel_dispatch/profiling_time.cpp:58`

The problem is therefore not timestamp source alone. The problem is the path required to know which signal belongs to which dispatch and to attach SDK state to that dispatch.

### 5. rocprofv3 selects this path for normal kernel trace

The CLI option `--kernel-trace` becomes `ROCPROF_KERNEL_TRACE`:

- `projects/rocprofiler-sdk/source/bin/rocprofv3.py:1546`
- `projects/rocprofiler-sdk/source/bin/rocprofv3.py:1560`

The tool library reads `ROCPROF_KERNEL_TRACE` into `config.kernel_trace`:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-tool/config.hpp:116`

Then it configures both buffer and callback kernel dispatch tracing when enabled:

- Buffer service: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-tool/tool.cpp:2221`, `:2320`
- Callback service: `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-tool/tool.cpp:2334`

Because `enable_queue_intercept()` considers kernel dispatch tracing enough reason to intercept queues, this normal user-facing option is coupled to the rewrite machinery above.

## Existing Runtime Timing Surface in CLR/HIP

CLR/HIP already has a runtime-side activity profiling mechanism.

### 1. HIP exposes an activity callback registration entry point

`hipRegisterTracerCallback` stores a callback in `amd::activity_prof::report_activity`:

- `projects/clr/hipamd/src/hip_intercept.cpp:38`
- `projects/clr/hipamd/src/hip_intercept.cpp:40`

HIP API callback scaffolding uses the same callback to ask whether an API operation is enabled, assign correlation id into TLS, call phase-enter/exit hooks, and clear correlation:

- `projects/clr/hipamd/src/hip_prof_api.h:39`
- `projects/clr/hipamd/src/hip_prof_api.h:42`
- `projects/clr/hipamd/src/hip_prof_api.h:44`
- `projects/clr/hipamd/src/hip_prof_api.h:51`

The activity protocol includes HIP async operations and a timing record:

- `ACTIVITY_DOMAIN_HIP_OPS`: `projects/clr/rocclr/platform/prof_protocol.h:16`
- `activity_record_t` fields for correlation, begin/end timestamps, device id, queue id, and kernel name/bytes: `projects/clr/rocclr/platform/prof_protocol.h:43`

### 2. CLR enables command profiling when activity is requested

The command constructor enables profiling when `activity_prof::IsEnabled()` returns true for that command type:

- `projects/clr/rocclr/platform/command.cpp:299`
- `projects/clr/rocclr/platform/command.cpp:301`
- `projects/clr/rocclr/platform/command.cpp:311`

The `ProfilingInfo` stores queued/submitted/start/end/correlation state:

- `projects/clr/rocclr/platform/command.hpp:88`
- `projects/clr/rocclr/platform/command.hpp:96`
- `projects/clr/rocclr/platform/command.hpp:101`

On completion, if profiling is enabled, CLR reports the activity:

- `projects/clr/rocclr/platform/command.cpp:152`
- `projects/clr/rocclr/platform/command.cpp:153`

`activity.cpp` builds the activity record from command profiling info and queue/device data, and uses kernel name for `CL_COMMAND_NDRANGE_KERNEL`:

- `projects/clr/rocclr/platform/activity.cpp:42`
- `projects/clr/rocclr/platform/activity.cpp:49`
- `projects/clr/rocclr/platform/activity.cpp:63`
- `projects/clr/rocclr/platform/activity.cpp:67`
- `projects/clr/rocclr/platform/activity.cpp:68`
- `projects/clr/rocclr/platform/activity.cpp:71`
- `projects/clr/rocclr/platform/activity.cpp:77`
- `projects/clr/rocclr/platform/activity.cpp:115`

There is also an explicit commit signal: `CommitRecord()` forwards a reserved sentinel so the tracer can count a future record before completion:

- `projects/clr/rocclr/platform/activity.hpp:65`
- `projects/clr/rocclr/platform/activity.cpp:18`
- `projects/clr/rocclr/platform/activity.cpp:24`

This is relevant to backpressure/lifetime: the runtime already has a protocol concept for "I will later produce one activity record."

### 3. CLR already retrieves HSA dispatch time from runtime-owned signals

The ROCm backend has `Timestamp` and `ProfilingSignal` objects for command timing:

- `Timestamp` class: `projects/clr/rocclr/device/rocm/rocvirtual.hpp:93`
- Fetch dispatch time helper: `projects/clr/rocclr/device/rocm/rocvirtual.hpp:83`
- `ProfilingSignal::CacheTimingData()` calls `hsa_amd_profiling_get_dispatch_time` for dispatch signals: `projects/clr/rocclr/device/rocm/rocvirtual.cpp:111`, `:132`, `:133`
- `Timestamp::ExtractSignalTiming()` caches and merges signal timing: `projects/clr/rocclr/device/rocm/rocvirtual.cpp:218`, `:222`, `:238`
- `Timestamp::getTime()` calls `checkGpuTime()` before returning start/end: `projects/clr/rocclr/device/rocm/rocvirtual.hpp:130`

During dispatch packet construction, CLR attaches active profiling signals and can store profiler correlation id in the dispatch packet reserved field:

- Single packet path: `projects/clr/rocclr/device/rocm/rocvirtual.cpp:1096`, `:1098`, `:1105`, `:1107`
- Batched path: `projects/clr/rocclr/device/rocm/rocvirtual.cpp:1327`, `:1329`, `:1335`, `:1337`

The runtime also enables profiling on queues it creates:

- `projects/clr/rocclr/device/rocm/rocdevice.cpp:3113`

### 4. HSA timestamp retrieval is the same primitive

The HSA API requires profiling to have been enabled on the queue and the completion signal not to have been reused:

- Queue profiling enable API: `projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_ext_amd.h:912`
- Dispatch time API requirements: `projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_ext_amd.h:992`, `:998`, `:1000`

Implementation just sets queue profiling bits and translates signal timestamps:

- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:623`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:630`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:672`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/hsa_ext_amd.cpp:694`
- `projects/rocr-runtime/runtime/hsa-runtime/core/inc/queue.h:362`
- `projects/rocr-runtime/runtime/hsa-runtime/core/runtime/amd_aql_queue.cpp:1444`

So the target runtime path does not require a different timing primitive. It requires moving dispatch identity/lifetime/correlation reporting to the runtime-owned command/profiling path.

## Why Pivot the Default

### Performance and complexity

For kernel timing, the current default path:

- Replaces HSA queue create/destroy.
- Allocates an intercept queue and proxy ring.
- Processes doorbells through `InterceptQueue::StoreRelaxed()`.
- Scans packet headers.
- Copies packet vectors.
- Allocates SDK completion signals.
- Rewrites completion signals.
- Adds barrier packets when original signals exist or services inject end packets.
- Calls async signal handlers for completion.
- Maintains correlation/session state outside the runtime that already owns command state.

That is not proportional to "report kernel start/end time." It is proportional to "interpose on every queue packet and optionally rewrite the stream." For dense HIP graph workloads, this maps directly onto the concern that a hot-path packet submission can degrade from a direct packet/memcpy-style flow into O(tasks/packets) inspection and transformation.

### Measurement veracity

For performance counter collection and thread trace, changing the dispatch stream can be acceptable because the requested measurement itself requires instrumentation, serialization, or injected packets. For high-level timing, the user expectation is different: the tool should observe the runtime's execution intervals with minimum perturbation. Queue interception violates that expectation by making the measured dispatch path a tool-created path.

### Ownership

CLR/HIP has the richer semantic context:

- HIP API correlation.
- Command type.
- Kernel name.
- Queue/virtual device identity.
- Runtime-owned completion/profiling signal.
- Graph capture state.
- Batching/accumulation semantics.

The SDK currently reconstructs enough of this from AQL packets and code-object tables. That is serviceable for packet-level tools, but it is the wrong ownership boundary for default kernel timing.

## Recommended Target Architecture

### Default path: runtime-reported kernel timing

Add a `rocprofiler-sdk` consumer for HIP/CLR activity records or a successor to that protocol, and make `rocprofv3 --kernel-trace` use it by default for HIP-created kernel dispatches.

Minimum record needed to fill current `rocprofiler_buffer_tracing_kernel_dispatch_record_t`:

- `start_timestamp`, `end_timestamp`
- `agent_id`
- `queue_id`
- `kernel_id` or a stable way to map kernel name/object to current kernel metadata
- `dispatch_id`
- private/group segment sizes if available
- workgroup/grid size
- thread id and correlation ids

The current CLR `activity_record_t` has start/end, device id, queue id, correlation id, and kernel name, but it does not appear to have the full `rocprofiler_kernel_dispatch_info_t` payload. The API probably needs to be extended rather than consumed verbatim.

### Non-default path: packet interception

Keep HSA queue interception for services that need packet access or packet modification:

- Counter collection / dispatch counting that injects AQL.
- PC sampling marker packets.
- Thread trace dispatch control and serialization.
- Scratch reporting if still dependent on queue packet interception.
- Process attach proxy queues.
- HSA-level kernel dispatches outside HIP/CLR if no runtime-level source exists.
- Explicit "legacy/intercept kernel trace" compatibility mode.

### Selection rule

The SDK should separate "kernel timing requested" from "queue interception required." Today `enable_queue_intercept()` treats kernel tracing as sufficient reason to intercept (`queue_controller.cpp:514`). The pivot requires changing this predicate so runtime-sourced kernel trace alone does not return true.

Conceptually:

```text
needs_queue_intercept =
  counter_collection ||
  pc_sampler ||
  dispatch_thread_trace ||
  device_thread_trace ||
  device_counter_collection ||
  scratch_reporting_if_not_rehomed ||
  explicit_hsa_packet_kernel_trace ||
  attach_proxy_queue_mode

runtime_kernel_trace =
  kernel_trace && hip_runtime_activity_available && !explicit_packet_mode
```

## Likely Change Plan

### Phase 1: Split the SDK concepts

In `rocprofiler-sdk`, split kernel timing service enablement from queue interception enablement.

Primary files:

- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/kernel_dispatch/tracing.cpp`
- `projects/rocprofiler-sdk/source/include/rocprofiler-sdk/fwd.h`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-tool/tool.cpp`
- `projects/rocprofiler-sdk/source/bin/rocprofv3.py`

Deliverable: a source enum/flag or internal service mode that distinguishes runtime kernel timing from queue-intercept kernel timing.

### Phase 2: Extend/formalize the HIP/CLR activity payload

Either extend `activity_record_t` or add a new HIP tools table callback for kernel dispatch records. I would avoid overloading the existing legacy `activity_record_t` too far; a versioned table registered through `rocprofiler-register` is cleaner and fits the existing architecture.

Primary files:

- `projects/clr/rocclr/platform/prof_protocol.h`
- `projects/clr/rocclr/platform/activity.hpp`
- `projects/clr/rocclr/platform/activity.cpp`
- `projects/clr/hipamd/src/hip_intercept.cpp`
- `projects/clr/hipamd/src/hip_api_trace.cpp`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp`

Deliverable: runtime callback records with enough fields to populate current SDK kernel dispatch records without inspecting AQL.

Open design point: whether `kernel_id` should be assigned by CLR/HIP directly, or whether SDK should map from kernel object/name to its existing code-object metadata. Direct runtime handoff is preferable if code-object identity is already available at launch.

### Phase 3: Implement SDK runtime-kernel-trace ingestion

Add an SDK component that receives runtime kernel timing records and emits the same buffered/callback kernel dispatch records currently emitted by `kernel_dispatch::dispatch_complete()`.

Primary files:

- New likely module under `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hip/` or `kernel_dispatch/`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/tracing/tracing.hpp`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/kernel_dispatch/tracing.cpp`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-tool/tool.cpp`

Deliverable: `rocprofv3 --kernel-trace` emits unchanged CSV/JSON/rocpd schema for HIP kernels with no HSA queue interception.

### Phase 4: Compatibility and fallback

Add explicit modes:

- Default: runtime kernel timing when HIP runtime support is present.
- Fallback: queue intercept kernel trace for non-HIP/HSA direct dispatch or missing runtime support.
- Forced legacy: environment/CLI option for queue-intercept kernel trace.

Primary files:

- `projects/rocprofiler-sdk/source/bin/rocprofv3.py`
- `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk-tool/config.hpp`
- `projects/rocprofiler-sdk/source/docs/how-to/`

Deliverable: users and downstream tools can diagnose which source produced kernel records.

### Phase 5: Tests and regressions

Required tests:

- `--kernel-trace` does not replace `hsa_queue_create_fn` when no other queue-intercept service is enabled.
- `--pmc`, PC sampling, and thread trace still force queue interception.
- Runtime kernel trace preserves start/end ordering and correlation across async completion.
- HIP graph dense workload smoke test verifies no packet-rewrite path is entered.
- Attach path remains unchanged unless explicitly redesigned.
- Mixed mode: kernel trace plus counter collection should still use interception and produce the expected records.

## Alternatives Considered

### A. Keep current queue interception and optimize it

This is the least disruptive code path, but it preserves the architectural problem. The source shows enough inherent complexity that local optimization will not remove the major costs: queue replacement, packet scanning, packet copying, signal substitution, async callbacks, and retry/overflow handling. This is appropriate only for features that require packet rewriting.

### B. Use HSA runtime queue-create notification only

`hsa_api_trace.h` also declares `hsa_amd_runtime_queue_create_register` (`projects/rocr-runtime/runtime/hsa-runtime/inc/hsa_api_trace.h:126`). Queue discovery alone could reduce some interception needs, but it does not solve per-dispatch timing attribution unless the tool also observes packet writes or receives runtime dispatch events. Useful as a supporting primitive, not sufficient for default kernel timing.

### C. Build a new ROCr passive dispatch callback

A lower-level ROCr callback could report every HSA dispatch without queue rewriting. This would help non-HIP producers and keep the surface near HSA, but ROCr lacks HIP semantics such as kernel names, graph command context, and HIP correlation unless those are reintroduced elsewhere. It is a plausible longer-term complement, not the best first default for HIP customer flows.

### D. Extend existing CLR/HIP activity protocol

This is the most direct route. The runtime already enables command profiling on activity request, tracks timestamps, reports activity records, and owns HIP command context. The risk is that the existing protocol is legacy-shaped and too small for full SDK dispatch records. A versioned extension/table is probably cleaner than stretching `activity_record_t`.

### E. Preserve public SDK API but change producer internally

This is the recommended compatibility stance. Keep `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH`, `ROCPROFILER_CALLBACK_TRACING_KERNEL_DISPATCH`, and output schema stable, while changing how records are produced. This minimizes downstream breakage for Kineto/Triton-style consumers.

## Concrete Decision Points

1. Does `rocprofv3 --kernel-trace` need per-dispatch fields beyond what CLR can currently report?
   Source says yes for current ABI: `rocprofiler_kernel_dispatch_info_t` includes kernel id, segment sizes, workgroup size, and grid size (`projects/rocprofiler-sdk/source/include/rocprofiler-sdk/fwd.h:795`).

2. Do we need runtime-reported records for non-HIP HSA dispatches?
   If yes, keep queue-intercept fallback or add a ROCr passive callback. The HIP runtime path alone will not cover direct HSA producers.

3. Is scratch reporting actually tied to queue interception or can it move?
   `enable_queue_intercept()` currently treats scratch reporting as intercept-requiring (`queue_controller.cpp:517`). That should be validated separately.

4. Is dispatch counting separable from kernel timing?
   Current "dispatch counting service" is explicitly callback-at-enqueue and often injects profiling packets. It should stay on the packet path unless a separate runtime-level counter mechanism exists.

5. How should graph capture be represented?
   CLR disables profiling timestamp allocation while commands are being packet-captured to avoid leaks (`projects/clr/rocclr/device/rocm/rocvirtual.cpp:2067`). That is directly relevant to dense HIP graph use cases and must be designed explicitly.

## Possible Execution Plans

These options are ordered from most architecturally correct to most tactical. They are not mutually exclusive forever; the pragmatic path may be to use a tactical plan as a bridge while the durable runtime activity contract is built.

### Plan A: Runtime activity contract becomes the SDK producer

This is the cleanest version of the pivot.

Define a versioned HIP/CLR-to-rocprofiler-sdk activity contract that reports completed HIP command activity with device start/end timestamps, correlation id, device id, queue or stream identity, operation kind, byte count for copies/fills, and kernel identity/launch metadata for kernels. Then make rocprofiler-sdk consume those records and emit the existing public SDK activity domains:

- `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH`
- `ROCPROFILER_CALLBACK_TRACING_KERNEL_DISPATCH`
- `ROCPROFILER_BUFFER_TRACING_MEMORY_COPY`, if memory copy is included in scope
- A new or mapped memset/fill representation, if required by downstream consumers

Primary changes:

- Extend CLR activity protocol beyond current `activity_record_t` if needed (`projects/clr/rocclr/platform/prof_protocol.h:43`).
- Preserve CLR's command profiling path that reports `profilingInfo().start_` and `profilingInfo().end_` through `ReportActivity` (`projects/clr/rocclr/platform/activity.cpp:49`).
- Use one of the API-layering options in "Practical API-Layering Options"; preferably a versioned runtime activity table propagated through `rocprofiler-register`, not a direct SDK dependency from CLR.
- Add SDK ingestion for runtime activity records and convert them into the existing SDK buffer/callback records.
- Change `enable_queue_intercept()` so timing-only HIP kernel tracing no longer returns true (`projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:503`).
- Keep queue interception for counters, PC sampling, thread trace, dispatch counting, scratch, and non-HIP HSA fallback.

Pros:

- Best alignment with software ownership: HIP runtime owns HIP command semantics and graph behavior.
- Preserves downstream SDK contracts while changing the producer.
- Gives a clear place to solve Kineto correlation, graph replay, memcpy, and memset coherently.

Cons:

- Cross-layer ABI/API design is real work.
- Requires careful treatment of kernel id/name lifetime and queue/resource identity.
- Needs compatibility testing across rocprofv3, Kineto, and internal profiler consumers.

Use this if the goal is to make the profiler stack coherent for multiple releases, not just unblock one downstream profiler.

### Plan B: SDK internal dual producer with runtime-first HIP kernels

This is the likely best execution compromise.

Keep the public rocprofiler-sdk API exactly as-is, but add an internal producer selection layer. For timing-only HIP kernel tracing, SDK subscribes to runtime-owned HIP activity records and emits normal SDK kernel dispatch records. If the workload is direct HSA, the runtime cannot provide required fields, or an advanced service is enabled, SDK falls back to queue interception.

The key difference from Plan A is scope: only HIP kernel timing is moved first. Memory copy remains on current HSA async-copy interception. Memset is explicitly documented as not solved unless a runtime fill/memset record already exists or is added later.

Primary changes:

- Add a runtime-kernel timing ingestion path in rocprofiler-sdk.
- Use one of the API-layering options in "Practical API-Layering Options"; for Plan B, the least disruptive likely path is to activate/extend HIP's existing `hip_tools` registration table and have SDK consume that table through the normal register propagation flow.
- Add feature detection and mode selection: runtime-first, queue-fallback, forced legacy.
- Preserve `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH` output shape for Kineto and rocprofv3.
- Split `has_kernel_tracing` in `enable_queue_intercept()` into "kernel tracing needs queue intercept" versus "kernel tracing can use runtime producer" (`projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue_controller.cpp:514`).
- Add diagnostics to identify which producer supplied kernel timing.

Pros:

- Directly attacks the customer-facing problem for HIP timing.
- Avoids boiling the ocean on memory copy/memset in the first milestone.
- Keeps Kineto and rocprofv3 compatibility mostly stable.
- Leaves queue interception intact where it is genuinely needed.

Cons:

- Two producers for nominally the same SDK domain must be kept behaviorally aligned.
- Resource id and kernel id/name mapping still require deliberate compatibility choices.
- HIP graph timing semantics must be validated, not assumed.

Use this if the goal is to pivot the default while keeping the blast radius manageable.

### Plan C: Kineto-focused SDK fast path

This is a narrower version of Plan B aimed at the observed downstream pain.

Add a rocprofiler-sdk path that is sufficient for Kineto's ROCm activity consumption: emit `rocprofAsyncRow`-equivalent kernel timing records with correlation id, device id, resource id, start/end timestamps, and kernel name. Preserve Kineto's expected `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH` surface, but do not immediately solve the full rocprofv3 schema if fields are unavailable from CLR.

Primary changes:

- Add an opt-in SDK mode or environment variable for HIP runtime kernel timing.
- Make Kineto/rocprofiler-sdk integration use that mode when only `CONCURRENT_KERNEL`/runtime activity is requested.
- Fill unavailable SDK dispatch fields conservatively or derive them from runtime launch metadata.
- Keep queue interception as the default for rocprofv3 CLI until parity is proven.

Pros:

- Fastest path to reduce Kineto overhead without forcing the entire SDK/tool stack through the pivot immediately.
- Allows real workload validation on PyTorch/Triton-style dense graph cases.
- Can be hidden behind an experimental producer flag.

Cons:

- Creates a special path that may not satisfy rocprofv3 users.
- Risks semantic drift between Kineto-visible data and SDK CLI data.
- If not constrained tightly, this becomes a second permanent profiler model.

Use this only if downstream Kineto pain is the forcing function and the organization accepts an experimental compatibility bridge.

### Plan D: Optimize queue interception timing-only mode

This argues the inverse: keep queue interception but make the timing-only path much cheaper.

Refactor `WriteInterceptor` so that, when only kernel timing is enabled, it avoids all counter/thread-trace/PC-sampling machinery, avoids packet injection, pools completion signals, preallocates transformed packet storage, caches callback decisions, and minimizes correlation work. The current hot path does considerably more than pure timing requires: packet scanning, correlation setup, signal replacement, optional callbacks, instrumentation packet checks, barrier handling, async handler registration, and transformed packet writes (`projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.cpp:203`).

Primary changes:

- Add a dedicated timing-only branch in `WriteInterceptor`.
- Pool HSA signals and async handler storage.
- Avoid heap allocation for per-dispatch sessions where possible.
- Avoid constructing callback/buffer data unless a context consumes it.
- Add microbenchmarks around dense HIP graph replay and small-kernel dispatch.

Pros:

- Least API/ABI disruption.
- Preserves direct HSA coverage and current SDK semantics.
- Useful regardless, because queue interception remains needed for advanced services.

Cons:

- Cannot reach near-zero overhead because the design remains on the packet write path.
- Still changes application execution by replacing completion signals and rewriting packet batches.
- Does not address the measurement-veracity objection as strongly as a runtime-owned path.

Use this as a containment/improvement track, not as the primary answer if near-zero timing overhead is the requirement.

### Plan E: Kineto hotwire with lowest kernel timing overhead

This is the most tactical option.

Bypass the rocprofiler-sdk kernel-dispatch buffer path for Kineto and feed Kineto GPU kernel activities directly from HIP/CLR runtime activity records. Keep Kineto's runtime API callback handling for launch metadata/correlation, but source `CONCURRENT_KERNEL` activities from CLR command profiling instead of SDK queue interception. This could live entirely in Kineto plus a small CLR/runtime activity subscription shim, or in a private SDK helper used by Kineto.

Primary changes:

- Add or expose a lightweight runtime activity callback carrying device start/end timestamps.
- In Kineto, create `GpuActivity`-equivalent records from that runtime source.
- Preserve PyTorch-visible fields: activity type `CONCURRENT_KERNEL`, timestamp, duration, correlation id, device id, resource id, name, linked activity.
- Do not attempt full rocprofiler-sdk schema parity in the first cut.

Pros:

- Lowest likely overhead for the PyTorch/Kineto case.
- Avoids queue interception entirely for Kineto kernel timing.
- Minimal dependency on rocprofv3 internals.

Cons:

- Worst layering. Kineto becomes coupled to CLR/HIP runtime internals or a side-channel API.
- Does not fix rocprofv3 default behavior.
- Risks bypassing rocprofiler-sdk as the intended profiling abstraction.
- Memory copy and memset still need separate treatment.

Use this only as an emergency downstream mitigation or prototype to prove overhead/semantics before moving the implementation into rocprofiler-sdk.

## Practical API-Layering Options

The major constraint is that rocprofiler-sdk should not gain a direct link-time dependency on CLR/HIP runtime internals. The current design already has a layering mechanism for this: runtimes register API tables with `rocprofiler-register`, and rocprofiler-sdk consumes those tables if a profiling tool is present. HIP calls `rocprofiler_register_library_api_table` from `ToolsInit` (`projects/clr/hipamd/src/hip_api_trace.cpp:1467`). `rocprofiler-register` stores registered tables and propagates them to the SDK through `rocprofiler_set_api_table` (`projects/rocprofiler-register/source/lib/rocprofiler-register/rocprofiler_register.cpp:553`, `projects/rocprofiler-register/source/lib/rocprofiler-register/rocprofiler_register.cpp:587`). The SDK then dispatches on the table name, copies the runtime table, and installs wrappers (`projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp:1094`, `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp:1129`).

The options below are ordered from best long-term layering to most tactical.

### Layering Option 1: Add a Versioned Runtime Activity Table via rocprofiler-register

Add a new supported registered library/table, e.g. `hip_activity` or `hip_runtime_activity`, whose only purpose is runtime-owned activity reporting. HIP/CLR owns the table definition and publishes function pointers for:

- registering an activity callback;
- enabling/disabling requested activity kinds;
- querying record ABI/version;
- optionally querying capabilities such as kernel identity fields, graph replay behavior, memory copy, and memset/fill support.

`rocprofiler-register` would add this common name to its supported-library list, just as it already supports `hsa`, `hip`, `hip_compiler`, `roctx`, and others (`projects/rocprofiler-register/source/lib/rocprofiler-register/rocprofiler_register.cpp:163`, `projects/rocprofiler-register/source/lib/rocprofiler-register/rocprofiler_register.cpp:207`). rocprofiler-sdk would add a `rocprofiler_set_api_table` branch for the new table and install the runtime activity callback from there.

Pros:

- Clean layering: runtime publishes a table, SDK consumes it; no SDK link dependency on CLR.
- Clean ABI: activity records can be versioned without overloading old `activity_record_t`.
- Best match for Plan A.

Cons:

- Requires touching rocprofiler-register, CLR/HIP headers, CLR/HIP implementation, and rocprofiler-sdk.
- Adds a new table identity and compatibility surface.
- More review overhead than reusing existing HIP table paths.

Use this for the durable multi-release design if the organization is willing to pay the API design cost.

### Layering Option 2: Activate and Extend HIP's Existing `hip_tools` Table

HIP already defines and attempts to register a `HipToolsDispatchTable` (`projects/clr/hipamd/include/hip/amd_detail/hip_api_trace.hpp:1755`, `projects/clr/hipamd/src/hip_api_trace.cpp:1462`). Today that table is minimal and `rocprofiler-register` does not list `hip_tools` as a supported common name in its supported-library enum/list. The tactical but still layered option is:

1. Add `hip_tools` to rocprofiler-register's supported library traits.
2. Extend `HipToolsDispatchTable` with a versioned callback-registration function for activity reporting.
3. In rocprofiler-sdk `rocprofiler_set_api_table`, add a `name == "hip_tools"` branch that consumes the tools table and registers the SDK's runtime activity receiver.

This likely has the least "topographical" surgery while preserving the current dependency direction. It uses the existing register propagation mechanism and an existing HIP-owned tools table instead of inventing a new runtime-to-SDK link.

Pros:

- Lower topology cost than a brand-new table family.
- Keeps runtime-to-SDK interaction marshaled through rocprofiler-register.
- Natural place for HIP-specific tool hooks.
- Good fit for Plan B and can evolve toward Plan A.

Cons:

- `hip_tools` currently has little surface area, so this still creates a real ABI contract.
- Need to decide whether the table carries the legacy callback shape or a new v2 activity record.
- If the table becomes a catch-all, it may become poorly scoped over time.

Recommended first serious implementation path: use this unless there is strong resistance to making `hip_tools` an accepted rocprofiler-register table.

### Layering Option 3: Reuse Existing `hipRegisterTracerCallback`

CLR already exposes an `extern "C"` `hipRegisterTracerCallback` function that stores a callback into `amd::activity_prof::report_activity` (`projects/clr/hipamd/src/hip_intercept.cpp:38`). The activity protocol already uses this callback for HIP API and HIP command activity: HIP API callbacks set `amd::activity_prof::correlation_id`, and command completion reports `activity_record_t` through `ReportActivity` (`projects/clr/hipamd/src/hip_prof_api.h:33`, `projects/clr/rocclr/platform/activity.cpp:49`).

The SDK could use this existing callback mechanism by resolving `hipRegisterTracerCallback` when HIP registers, either through a future table entry or, more tactically, via symbol lookup. That would allow SDK to receive CLR activity records without adding a link dependency.

Pros:

- Lowest implementation cost if the existing callback protocol is sufficient.
- Uses already-shipping CLR activity plumbing.
- Good prototype path for proving overhead and data quality.

Cons:

- The existing callback is global and looks single-consumer; conflict behavior with roctracer/Kineto/other tools must be resolved.
- The current `activity_record_t` is small and comments call begin/end "host" timestamps, even though CLR can populate them from HSA profiling timestamps in command paths (`projects/clr/rocclr/platform/prof_protocol.h:43`, `projects/clr/rocclr/device/rocm/rocvirtual.cpp:111`).
- Symbol lookup is a weaker contract than table propagation.
- Harder to version cleanly.

Use this as a proof-of-concept or short-lived bridge. If selected for production, first wrap it in a versioned table entry so the SDK does not depend on opportunistic `dlsym`.

### Layering Option 4: Extend the Existing HIP Runtime Dispatch Table

Instead of adding or activating `hip_tools`, append activity-control entries to the existing HIP runtime dispatch table. This avoids adding a new common name in rocprofiler-register because `hip` is already supported and consumed by SDK (`projects/rocprofiler-register/source/lib/rocprofiler-register/rocprofiler_register.cpp:212`, `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/registration.cpp:1129`).

Pros:

- No new rocprofiler-register table identity.
- SDK already has a `hip` branch and table-copy/update flow.
- Versioning machinery for the HIP table already exists.

Cons:

- Poorer separation: profiler/tool activity control is not a normal HIP runtime API.
- Grows an already large dispatch table for a tool-only concern.
- Higher risk of ABI review friction in HIP runtime.

Use only if adding `hip_tools` support is rejected and a table-based contract is still required.

### Layering Option 5: SDK-Exported Runtime Activity Sink

Invert the call direction: rocprofiler-sdk exports a C ABI sink such as `rocprofiler_runtime_activity_report`, and CLR/HIP looks it up dynamically when activity reporting is enabled. CLR would call the SDK sink from its existing `ReportActivity` path.

Pros:

- Simple mental model: runtime emits records directly to the SDK.
- Avoids modifying rocprofiler-register table propagation.

Cons:

- Wrong dependency direction. CLR now knows about an SDK-owned profiling sink.
- Harder to handle multiple tools or SDK instances.
- Reintroduces startup/shutdown ordering concerns the register layer is meant to mediate.

Avoid for mainline implementation.

### Layering Option 6: Sidecar Bridge Library

Create a small bridge library loaded as a tool or helper. It registers with CLR/HIP activity callbacks and forwards records to Kineto or rocprofiler-sdk. This is essentially the Plan E escape hatch with a clearer packaging boundary.

Pros:

- Fastest way to experiment without large SDK/runtime surgery.
- Can validate field sufficiency and overhead on PyTorch/Kineto workloads.

Cons:

- Adds another moving piece in startup/shutdown ordering.
- Risks becoming a permanent bypass around rocprofiler-sdk.
- Does not solve rocprofv3 as the common activity producer.

Use only for experiments or emergency downstream mitigation.

### Layering Recommendation

For Plan A, prefer Option 1 if there is time to design the activity ABI properly. It gives the cleanest long-term contract.

For Plan B, prefer Option 2: activate and extend `hip_tools`. It is the lowest-surgery path that still respects the existing rocprofiler-register layering. It also creates a natural stepping stone toward Plan A: start with HIP kernel timing activity, then expand the table or record shape for memory copy, memset/fill, graph replay, and richer kernel identity.

Use Option 3 only as a prototype or compatibility bridge. It is attractive because the callback already exists, but its global/single-consumer shape and weak versioning make it risky as the long-term SDK producer boundary.

Avoid Options 5 and 6 for production unless schedule pressure dominates architecture.

### Recommended sequencing

The defensible execution sequence is:

1. Start Plan B as the mainline pivot: runtime-first HIP kernel timing behind a producer selector, with queue fallback.
2. Run Plan D in parallel only for services that remain queue-intercept based.
3. Use Plan C only if Kineto needs relief before Plan B is production-ready.
4. Avoid Plan E unless the customer/downstream pressure is severe enough to justify a temporary layering violation.

The key leadership point is that "remove queue interception from default HIP kernel timing" does not require solving every activity domain at once. Kernel timing, memory copy timing, memset/fill timing, counters, PC sampling, thread trace, and direct HSA dispatch coverage should be split into explicit producer decisions.

## Recommended Leadership Position

The stance I would take into execution planning:

Kernel timing is a runtime-owned activity, not a packet-interception activity. Queue interception remains a necessary low-level capability for instrumentation-heavy services, but it should not be the default implementation of customer-facing high-level timing in `rocprofv3`.

The immediate plan should not be "delete queue interception." It should be "split the kernel timing service from queue interception, add a runtime-reported HIP kernel timing source, make that the default when available, and keep interception as an explicit fallback/advanced path."

This is a narrower and more defensible pivot than a broad mandate. It preserves working counter/thread-trace machinery, gives profiler engineers a migration path, and directly addresses the customer-facing overhead/veracity concern for the common timing-only case.

## Unresolved Source Questions for Rev2

- Which downstream consumers require exact current `queue_id` semantics? CLR activity reports `queue->vdev()->index()`, while SDK queue id is HSA queue handle-derived (`projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/queue.hpp:179`). This semantic mismatch needs a deliberate mapping.
- Can CLR/HIP report kernel object or code-object kernel id at command creation without expensive lookup?
- How does HIP graph replay currently surface commands through `Command`/`Timestamp` when activity is enabled after capture?
- Which `rocprofiler-sdk` tests assume `--kernel-trace` implies HSA queue replacement?
- Triton still needs a source-level pass. Kineto is analyzed in Appendix A: it expects libkineto activity records with stable SDK-derived domains/types, timestamps, resource ids, metadata, and correlation. It does not expose queue interception as a consumer contract.

## Appendix A: PyTorch/Kineto ROCm Integration Flow

Source baseline for this appendix:

- PyTorch: `sources/pytorch`, commit `27a2fd9429`.
- Kineto submodule: `sources/pytorch/third_party/kineto`, commit `b2103f7`.

### A.1 Integration flow

PyTorch routes profiler GPU activity collection through Kineto. In `torch/csrc/profiler/kineto_shim.cpp`, the public PyTorch `ActivityType::CUDA` selection inserts Kineto activity types including `GPU_MEMCPY`, `GPU_MEMSET`, `CONCURRENT_KERNEL`, `CUDA_RUNTIME`, and `CUDA_DRIVER` (`sources/pytorch/torch/csrc/profiler/kineto_shim.cpp:40`, `sources/pytorch/torch/csrc/profiler/kineto_shim.cpp:371`). Under ROCm this is still the CUDA-named PyTorch profiler surface; the ROCm-specific backend is selected below that abstraction.

Kineto selects the ROCm profiler implementation in `ActivityProfilerController`. If built with ROCm support and without the roctracer fallback, it constructs `RocmActivityProfiler(RocprofActivityApi::singleton(), cpuOnly)` (`sources/pytorch/third_party/kineto/libkineto/src/ActivityProfilerController.cpp:86`, `sources/pytorch/third_party/kineto/libkineto/src/ActivityProfilerController.cpp:89`). That means the current primary ROCm path is rocprofiler-sdk, not legacy roctracer.

The active collection sequence is:

1. PyTorch prepares Kineto activity types and starts the trace (`sources/pytorch/torch/csrc/profiler/kineto_shim.cpp:395`, `sources/pytorch/torch/csrc/profiler/kineto_shim.cpp:413`).
2. Kineto enables ROCm collection through `RocmActivityProfiler::enableGpuTracing`, which calls `RocprofActivityApi::enableActivities` (`sources/pytorch/third_party/kineto/libkineto/src/RocmActivityProfiler.cpp:107`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofActivityApi.cpp:166`).
3. `RocprofLogger::startLogging` forces rocprofiler-sdk tool registration if needed, enables external correlation, and starts the configured rocprofiler context (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:765`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:784`).
4. At stop/synchronize, Kineto calls `hipDeviceSynchronize()` and flushes rocprofiler activities (`sources/pytorch/third_party/kineto/libkineto/src/RocmActivityProfiler.cpp:124`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:795`).
5. `RocprofActivityApi::processActivities` filters stored rows by Kineto activity selection, converts timestamps, and passes records back to `RocmActivityProfiler` (`sources/pytorch/third_party/kineto/libkineto/src/RocprofActivityApi.cpp:89`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofActivityApi.cpp:120`).
6. PyTorch imports the resulting `ITraceActivity` objects by reading timestamp, duration, device/resource id, correlation id, activity type, flow fields, linked activity, and metadata JSON (`sources/pytorch/torch/csrc/profiler/collection.cpp:1039`, `sources/pytorch/torch/csrc/profiler/collection.cpp:1096`, `sources/pytorch/torch/csrc/profiler/collection.cpp:1143`).

### A.2 What Kineto configures in rocprofiler-sdk

Kineto's rocprofiler-sdk integration configures two different services:

- HIP runtime callback tracing, for API rows and launch metadata.
- Buffer tracing for kernel dispatch and memory copy completion records.

`RocprofLogger::toolInit` configures callback tracing for `ROCPROFILER_CALLBACK_TRACING_HIP_RUNTIME_API` (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:391`). It also creates a buffer and configures `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH` plus `ROCPROFILER_BUFFER_TRACING_MEMORY_COPY` (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:400`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:413`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:420`).

For HIP runtime callbacks, Kineto builds in-memory rows on callback exit. Kernel launch APIs become `rocprofKernelRow` records with correlation id, operation id, process/thread, runtime start/end, grid, workgroup, shared memory, and stream (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:547`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:562`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:572`). External PyTorch correlation ids are recorded alongside rocprofiler internal correlation ids (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:652`).

For buffer records, Kineto maps rocprofiler-sdk kernel dispatch records to `rocprofAsyncRow`. It uses the SDK record's internal correlation id, kind, operation, agent-derived device id, dispatch queue id, start timestamp, end timestamp, and code-object kernel name (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:667`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:678`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:698`). These async rows are the GPU timeline records that PyTorch users see as kernels.

Memory copy activity is similar in shape but not identical in producer. Kineto configures `ROCPROFILER_BUFFER_TRACING_MEMORY_COPY` and maps those records to `GPU_MEMCPY` (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:420`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity.h:86`). The rocprofiler-sdk producer for this domain is the HSA async-copy interception path, not HSA queue packet interception: it wraps async-copy calls, installs/completes profiling signals, obtains copy start/end time, and emits `ROCPROFILER_BUFFER_TRACING_MEMORY_COPY` records (`projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/async_copy.cpp:134`, `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/async_copy.cpp:191`, `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/async_copy.cpp:392`, `projects/rocprofiler-sdk/source/lib/rocprofiler-sdk/hsa/async_copy.cpp:405`).

`GPU_MEMSET` is present in the PyTorch/Kineto activity vocabulary, but I do not see a current rocprofiler-sdk Kineto producer for it. Kineto's rocprofiler-sdk wrapper contains a `GPU_MEMSET` naming branch, but `GpuActivity` only classifies SDK buffer records as `GPU_MEMCPY` for `ROCPROFILER_BUFFER_TRACING_MEMORY_COPY` or `CONCURRENT_KERNEL` for `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH`; `RocprofLogger` does not configure a separate memset buffer tracing domain (`sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity_inl.h:82`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity.h:81`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:413`). This means `GPU_MEMSET` should be treated as a compatibility expectation from the PyTorch/Kineto type set, not as proof that the current SDK path already has a complete memset device-timeline producer.

### A.3 What Kineto consumes

Kineto does not consume HSA queue packets directly. It consumes its own small row model:

- `rocprofBase` carries `id`, `begin`, `end`, `domain`, and row type; the comment says `id` is the correlation id and that `domain` can represent callback tracing kind, buffer tracing kind, or legacy roctracer activity domain depending on path (`sources/pytorch/third_party/kineto/libkineto/src/RocLogger.h:75`).
- `rocprofKernelRow` extends runtime rows with launch metadata: function address/function, grid, workgroup, group segment size, and stream (`sources/pytorch/third_party/kineto/libkineto/src/RocLogger.h:108`).
- `GpuActivity` wraps `rocprofAsyncRow`. If the domain is `ROCPROFILER_BUFFER_TRACING_MEMORY_COPY`, it becomes `GPU_MEMCPY`; if the domain is `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH`, it becomes `CONCURRENT_KERNEL` (`sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity.h:81`).
- `RuntimeActivity` wraps HIP runtime callback rows and reports them as Kineto `CUDA_RUNTIME` activities (`sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity.h:128`).

The metadata dependency is also explicit. `RuntimeActivity<rocprofKernelRow>::metadataJson` records kernel name, correlation, grid, block, and shared memory for the runtime launch row, and caches grid/block by correlation id so the async GPU row can expose the same launch shape (`sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity_inl.h:162`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity_inl.h:176`). The GPU row metadata then emits device, stream, correlation, kind, grid, and block when the runtime row has populated that cache (`sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity_inl.h:103`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofActivity_inl.h:120`).

PyTorch consumes those objects only through the `ITraceActivity` interface. `resultFromActivity` copies `timestamp`, `duration`, `correlationId`, `type`, `flowId`, `flowType`, device id, and resource id (`sources/pytorch/torch/csrc/profiler/collection.cpp:1039`). It later uses `linkedActivity` and flow ids to parent GPU work under CPU/runtime events (`sources/pytorch/torch/csrc/profiler/collection.cpp:1143`, `sources/pytorch/torch/csrc/profiler/collection.cpp:1171`). The exported `KinetoEvent` forwards activity type, name, start time, correlation id, and device resource id from this result model (`sources/pytorch/torch/csrc/autograd/profiler_kineto.cpp:1207`).

### A.4 Proof or contradiction of the runtime-timing hypothesis

Working hypothesis: kernel timing should move to a runtime-reported source by default, with queue interception retained for services that need packet instrumentation or non-HIP fallback coverage.

Evidence supporting the hypothesis:

- Kineto's consumer boundary is activity records, not HSA queue interception. The objects PyTorch receives are `ITraceActivity` wrappers with timestamps, duration, type, device/resource id, correlation, linked activity, and metadata. None of the PyTorch ingestion code depends on queue replacement or packet rewriting.
- HIP runtime callback rows are already first-class in Kineto. Kineto extracts kernel launch metadata from HIP runtime callbacks and uses runtime correlation ids to attach metadata and parentage to async GPU rows. That supports the idea that HIP runtime is the right semantic owner for high-level timing records.
- The visible PyTorch/Kineto contract is `CONCURRENT_KERNEL` plus `CUDA_RUNTIME`/correlation semantics. A runtime-produced kernel timing path can preserve this contract if rocprofiler-sdk emits the same buffer tracing activity shape or Kineto receives an equivalent `rocprofAsyncRow`.
- Kineto already treats runtime and async GPU records as separate but correlated streams. That maps well to an implementation where HIP owns launch semantics and completion timing is supplied without requiring SDK queue interception.

Evidence constraining the hypothesis:

- Current Kineto code explicitly configures `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH` and maps that domain to `CONCURRENT_KERNEL`. If rocprofiler-sdk stops producing this domain, or changes its schema, PyTorch/Kineto breaks unless Kineto is updated.
- Kineto currently sources GPU kernel start/end timestamps from rocprofiler-sdk buffer tracing records, not from Kineto's HIP runtime callback row. In Kineto's current `RocprofLogger::api_callback`, HIP runtime API rows are host callback enter/exit intervals. That should not be confused with CLR's runtime-owned activity protocol: CLR can report command profiling timestamps populated from HSA profiling signals via `hsa_amd_profiling_get_dispatch_time` and `hsa_amd_profiling_get_async_copy_time` (`projects/clr/rocclr/platform/command.cpp:80`, `projects/clr/rocclr/device/rocm/rocvirtual.cpp:111`, `projects/clr/rocclr/device/rocm/rocvirtual.cpp:2151`).
- Kineto uses `dispatch.queue_id.handle` as the GPU activity resource id (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:698`). A CLR/HIP runtime source that reports a virtual device index or HIP stream instead of the current queue id would change trace lane grouping unless the SDK maps it deliberately.
- The kernel name path currently relies on rocprofiler-sdk code-object symbol registration and buffer dispatch `kernel_id` lookup (`sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:341`, `sources/pytorch/third_party/kineto/libkineto/src/RocprofLogger.cpp:691`). A runtime path needs to preserve kernel identity either by carrying SDK-compatible kernel ids or by providing a name path with equivalent lifetime guarantees.

Conclusion: Kineto does not contradict the architectural pivot. It contradicts only an unscoped version of the pivot that would remove or rename SDK kernel dispatch activity records. The safer plan is to preserve `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH` as the downstream activity contract while changing the producer for HIP timing-only collection from HSA queue interception to a runtime-owned completion-timing source.

### A.5 Execution implications for rocprofiler-sdk

For Kineto compatibility, the SDK-side runtime timing implementation should preserve these fields and semantics for HIP kernels:

- Activity type/domain: continue surfacing kernel GPU work as `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH`, so Kineto continues to classify it as `CONCURRENT_KERNEL`.
- Correlation: preserve `record.correlation_id.internal` compatibility between HIP runtime callback rows and GPU completion rows.
- Timing: provide device execution start/end timestamps in the same clock domain Kineto currently expects for async rows, or update Kineto's conversion path explicitly.
- Resource id: define whether the resource is the old HSA queue handle, HIP stream, or a stable derived lane id. If it changes, treat it as an intentional trace schema change.
- Kernel identity: preserve kernel id/name availability late enough for graph replay and async completion.
- Runtime metadata: keep launch metadata available by correlation id for grid/block/shared-memory enrichment.
- Memory activities: decide separately whether memory copy and memset should remain on existing HSA async-copy/library interception paths, move to the CLR activity protocol, or be mixed. This should not block the kernel-timing pivot, but PyTorch/Kineto `ActivityType::CUDA` commonly requests kernels, copies, and memset together.

The minimum-disruption integration shape is therefore:

1. Keep Kineto's rocprofiler-sdk API usage working.
2. Make rocprofiler-sdk satisfy `ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH` from the HIP runtime timing source when the requested service is timing-only.
3. Fall back to queue interception for counters, PC/thread trace, dispatch counting, non-HIP HSA dispatches, or explicit legacy mode.

That gives downstream profilers a stable activity contract while removing the packet-rewrite dependency from the common kernel timing path.

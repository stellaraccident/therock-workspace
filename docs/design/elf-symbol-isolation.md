# ELF Symbol Isolation for Bundled System Dependencies

Status: Design notes
Date: 2026-03-30
Related: #4211, #4212

## Problem

ROCm bundles system libraries (zstd, zlib, etc.) under `lib/rocm_sysdeps/`.
When both ROCm's bundled copy and the system copy of the same library are
loaded in the same process, ELF symbol interposition can cause crashes or
corruption.

Two directions of interposition are possible:

1. **Inbound**: System library's symbols override ROCm's internal calls.
   Example: system zstd's `ZSTD_freeDCtx` called during ROCm zstd's
   `ZSTD_decompress`, causing a crash (the reported bug).

2. **Outbound**: ROCm's symbols satisfy lookups from unrelated libraries
   that expect system versions. Requires ROCm's copy to load first and
   export default-versioned (`@@`) symbols.

## Current Fix (shipped)

Addresses the reported crash (inbound interposition) for zstd:

- **`-Wl,-Bsymbolic`**: Library's own calls resolve to its own definitions,
  preventing inbound interposition. (The original PR #4212 had `-Bsymbolic`
  without `-Wl,`, so it never reached the linker.)
- **`-fvisibility=hidden`** + upstream `*_VISIBLE` macros: Only the public
  API is exported (391 -> 187 symbols for zstd). Internal symbols like
  `FSE_*`, `XXH*`, `HUF_*` no longer leak.
- **Version script** (`global: *` under `AMDROCM_SYSDEPS_1.0`): All exports
  get `@@AMDROCM_SYSDEPS_1.0` default version tags. ROCm consumers bind to
  versioned symbols.

This does **not** address outbound interposition (ROCm's `@@` symbols
satisfying other libraries' unversioned lookups).

## Approaches Investigated

### Non-default symbol versions (`.symver` with single `@`)

**Concept**: Use `.symver` assembler directives to make all exports
`@AMDROCM_SYSDEPS_1.0` (non-default) instead of `@@` (default). Non-default
versions only match version-qualified lookups, so unversioned lookups from
other DSOs fall through to system libraries.

**Implementation**: Generate a header with `.symver` directives for each
exported function, force-include it via `-include symver_overrides.h`. Change
the version script to define the node without `global:` patterns (so it
doesn't override the `.symver` directives).

**Result**: Works perfectly for the library itself. All 187 exports become
`@AMDROCM_SYSDEPS_1.0`, zero `@@` or unversioned symbols.

**Fatal flaw**: Breaks autoconf/meson/cmake link-time discovery. Internal
consumers like elfutils do `./configure --with-zstd` which runs
`AC_CHECK_LIB(zstd, ZSTD_decompress)` -- a bare link test that produces
unversioned references. These don't bind to `@`-only symbols, so the
configure check fails with "missing -lzstd".

**Key insight**: GNU ld version scripts override `.symver` directives.
A version script with `local: *` hides everything regardless of `.symver`.
A version script with `global: ZSTD_*` gives `@@` regardless of `.symver`.
To use `.symver` effectively, the version script must define the version node
but not match any symbols.

A `generate_symver_overrides.py` tool was prototyped to extract exports from
a built .so and generate the header. The tool works and could be useful in
the future.

### RTLD_LOCAL

**Concept**: Load ROCm's sysdeps with `dlopen(path, RTLD_LOCAL)` so their
symbols stay out of the global scope entirely.

**Not viable**: We don't control how downstream users load ROCm libraries.
Libraries loaded via DT_NEEDED are always in the global scope. The runtime
linking environment is open-ended.

## Future Directions

### Symbol prefixing

Rename all symbols to `rocm_ZSTD_*` at build time so there is no name
collision with system libraries. The symbol names are simply different, so
interposition is impossible in either direction.

**Build-time implementation**:
- Compile with `-DZSTD_decompress=rocm_ZSTD_decompress` (via a generated
  macro header), or use `objcopy --prefix-symbols=rocm_` post-link.
- Provide a wrapper header that `#define`s public API names to prefixed
  names for consumers.
- Force-include the wrapper via `CPPFLAGS`/`CMAKE_C_FLAGS` in TheRock
  subproject declarations.
- Autoconf-style link tests need `CPPFLAGS` set to include the wrapper
  header. This is controllable within TheRock's cmake subproject system
  (we already set `CPPFLAGS` for elfutils and others).

**Advantages**:
- Complete isolation, both directions.
- Fails loudly: a bare `-lzstd` link test without the header can't find
  `ZSTD_decompress` (it's been renamed), producing a clear error rather
  than silent wrong-library binding.
- No dependence on ELF version semantics.

**Disadvantages**:
- Every internal consumer must use the wrapper header.
- Autoconf consumers need `CPPFLAGS` patched (we already do this for
  include paths, so the mechanism exists).
- Per-library effort to set up, though the pattern is mechanical.

### Symbol prefixing + linker script + asm trampolines (recommended)

Combines symbol prefixing with a linker-script-as-`.so` trick to solve the
autoconf problem without patching consumer `CPPFLAGS`.

**Architecture**:

Three artifacts per sysdep:

1. **Real .so** (`librocm_sysdeps_zstd.so.1`): All symbols prefixed as
   `rocm_ZSTD_*`. Built with `-fvisibility=hidden` + `-Wl,-Bsymbolic`.
   No original `ZSTD_*` names exist in the dynamic symbol table.

2. **Trampoline archive** (`librocm_sysdeps_zstd_stubs.a`): Static archive
   of generated asm stubs. Each stub exports the original unprefixed name
   (`ZSTD_decompress`) and does an indirect tail-jump to the prefixed
   symbol (`rocm_ZSTD_decompress`) in the real .so. Zero overhead after
   first resolution — same cost as a PLT entry.

3. **Linker script** (installed as `libzstd.so`): The dev-time `.so` is
   actually a text file:
   ```
   /* GNU ld script */
   INPUT(librocm_sysdeps_zstd_stubs.a AS_NEEDED(librocm_sysdeps_zstd.so.1))
   ```

**How autoconf link tests work**:
1. `AC_CHECK_LIB(zstd, ZSTD_decompress)` compiles a test: `extern char
   ZSTD_decompress(); int main() { ZSTD_decompress(); }`
2. Linker resolves `-lzstd` to the linker script
3. Linker pulls `ZSTD_decompress` from the stubs `.a` — test passes
4. The stub references `rocm_ZSTD_decompress` — linker pulls in the real
   `.so.1` via `AS_NEEDED`
5. Consumer binary gets: the stub inlined (from `.a`), `DT_NEEDED` on the
   prefixed `.so.1`, and dynamic references to `rocm_ZSTD_*` only

**At runtime**: Only `rocm_ZSTD_*` symbols exist in the dynamic space.
System zstd has `ZSTD_*`. Zero name overlap, complete isolation.

**Consumer header optimization**: ROCm's installed `zstd.h` can also include
`#define ZSTD_decompress rocm_ZSTD_decompress` etc. Proper consumers that
include the header bypass the trampoline entirely (compile-time redirect).
The stubs only exist as a fallback for bare link tests.

**Trampoline generation**: The trampoline asm is minimal — ~5 lines per
symbol on x86_64 (load table entry, indirect jmp). No need for a full
framework. Implib.so (previously used in TheRock for comgr delay-loading,
see commit b1bdc0bc) demonstrates the pattern but is designed for
delay-loading; we'd want a simpler purpose-built generator for the
prefix-redirect use case.

**Libraries with upstream symbol versioning**: Some sysdeps (e.g. elfutils)
use their own symbol versioning in their build system. Layering prefixing on
top of versioned symbols is messy — you'd be fighting the library's build
system to produce `rocm_elf_begin@@ELFUTILS_1.0`. Better to disable upstream
symbol versioning entirely (`--disable-symbol-versioning` or equivalent
build flag) and let prefixing + trampolines be the sole isolation layer.
One convention, uniformly applied.

**Advantages**:
- Complete isolation, both directions. No symbol versioning tricks.
- Autoconf/meson/cmake link tests work unmodified — no `CPPFLAGS` patching.
- Zero runtime overhead for proper consumers (header `#define` redirect).
- Near-zero overhead for fallback path (single indirect jump).
- Generalizes mechanically to any sysdep library.

**Disadvantages**:
- Three artifacts per sysdep instead of one.
- Per-library setup, though the pattern is mechanical and could be a cmake
  function.
- Libraries with upstream symbol versioning need that disabled, which may
  require configure flag discovery per library.

### Applicability to C++ libraries (libLLVM.so)

The asm trampoline approach is language-agnostic — it operates on raw symbol
names. For C++ libraries, the prefixing step needs to be smarter than
`objcopy --prefix-symbols` (which would produce invalid mangling and break
std::, __cxa_*, etc.).

**Selective re-mangling**: Itanium ABI encodes namespace names as
length-prefixed strings. `_ZN4llvm10TargetInfo3getEv` can be remapped to
`_ZN9rocm_llvm10TargetInfo3getEv` by substituting `4llvm` → `9rocm_llvm`
at valid nesting positions. Apply via `objcopy --redefine-syms=mapping.txt`
with a generated mapping. Same approach for `5clang` → `10rocm_clang`,
`3lld` → `8rocm_lld`, etc. `extern "C"` symbols (the LLVM C API:
`LLVMCreateContext`, etc.) get plain prefixed.

This would address the libLLVM.so isolation problem (ROCm's LLVM vs other
LLVM instances in the same process) without the dlmopen/comgr-stub
complexity. LLVM builds with `-fno-rtti -fno-exceptions`, which eliminates
the two hardest C++ concerns (typeinfo matching and exception handling
across the prefixed boundary).

**The hard part — in-tree consumers**: The ELF mechanics are
straightforward, but libLLVM.so is consumed in its pristine form by comgr,
lld, clang, device-libs, etc. as part of the LLVM build itself. By the
time you have a .so to prefix, its consumers already have object files
full of references to `_ZN4llvm...`.

**Solution: run the rewriter tool as a POST_BUILD step on libLLVM.so.**
Use `cmake_language(DEFER)` or TheRock's existing `_post_hook` pattern to
intercept the LLVM target after `add_library(LLVM SHARED ...)` and inject
a `POST_BUILD` command. This doesn't require modifying LLVM's
CMakeLists.txt — the super-project injects from outside.

The POST_BUILD step runs the standalone rewriter tool, which:
1. Rewrites the .so with prefixed symbols (`objcopy --redefine-syms`)
2. Generates trampoline stubs .a (original names → prefixed names)
3. Replaces `libLLVM.so` with a linker script:
   `INPUT(libLLVM_stubs.a AS_NEEDED(librocm_LLVM.so.22))`

After this, all in-tree consumers (comgr, lld, clang, device-libs) link
against `libLLVM.so` which is now the linker script. They get trampolines
from the .a, their references resolve to prefixed symbols in the real .so.
**No consumers need to be touched at all** — they think they're linking
against normal LLVM. At runtime, everything goes through `rocm_`-prefixed
symbols.

```cmake
# Deferred or post-hook: runs after LLVM's add_library
add_custom_command(TARGET LLVM POST_BUILD
  COMMAND ${ROCM_SYMBOL_REWRITER}
    --prefix=rocm_
    --redefine-namespaces=llvm:rocm_llvm,clang:rocm_clang,lld:rocm_lld
    --output-stubs=${STUBS_DIR}/libLLVM_stubs.a
    --output-linker-script=$<TARGET_FILE:LLVM>
    $<TARGET_FILE:LLVM>
)
```

Note: `LLVM_NAMESPACE` is an existing LLVM cmake option that wraps
`namespace llvm {}` at compile time, which would solve this cleanly if it
worked. In practice it's under-tested and breaks in various places. Could
be worth fixing upstream long-term, but the rewriter approach doesn't
depend on it and handles `extern "C"` symbols too.

**Corner cases to prove out**:
- Weak symbols and COMDAT groups (template instantiations shared between
  LLVM and consumers)
- TLS symbols
- Whether `objcopy --redefine-syms` handles all symbol types (FUNC, OBJECT,
  GNU_IFUNC)
- Interaction with LTO
- Symbols where LLVM types appear as template parameters in non-LLVM code
  (e.g. `std::vector<llvm::StringRef>` — the `4llvm` appears inside the
  `std::` mangling, same substitution applies)
- Whether POST_BUILD on intermediate .o files is needed, or if rewriting
  the final .so artifacts is sufficient

**Implementation**: A standalone tool + CI test matrix against the usual
suspect libraries. Prototype on zstd (C, simple), then elfutils (C, symbol
versioning), then libLLVM.so (C++, scale). The tool is the same for all
three — only the symbol filter and renaming rules differ.

### Consumer-side `.symver` via installed headers

Instead of putting `.symver` in the producer, append directives to the
consumer-facing headers that ROCm ships. Code that `#include`s ROCm's
`zstd.h` gets versioned references automatically. External code using system
headers gets unversioned references and binds to system zstd.

**Compared to symbol prefixing**: Same control surface (need `CPPFLAGS` for
autoconf cases), but weaker failure mode. If someone skips the header, they
get unversioned refs that silently bind to system zstd at runtime instead of
a link error.

## Recommendation

Ship the current fix (`-Bsymbolic` + hidden visibility) now. It solves the
reported crash.

For full isolation, **symbol prefixing + linker script + asm trampolines**
is the most promising path. It provides complete isolation without requiring
any changes to consumers (autoconf link tests work unmodified), has near-zero
runtime overhead, and avoids the ELF symbol versioning system entirely. It
should be prototyped on zstd first, then generalized as a cmake function for
all sysdeps. For libraries that use upstream symbol versioning (elfutils,
etc.), disable that versioning and rely solely on prefixing for isolation.

The `.symver` approach and tooling (`generate_symver_overrides.py`) remain
available for cases where prefixing is impractical, with the caveat that
non-default versions break autoconf-style link tests.

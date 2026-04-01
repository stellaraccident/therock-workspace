# Tensile Fallback .dat File Convergence Fix for Multi-Shard Builds

## Problem Statement

In TheRock's kpack split build, the build is sharded by GPU architecture family.
Each shard builds Tensile (for rocBLAS) and TensileLite (for hipBLASLt)
independently for its assigned architectures. The shard outputs are then overlaid
into a single `dist/rocm/` tree.

The overlay requires a **convergence property**: files with the same relative path
across shards must have identical content, OR files with different content must have
different names. Two classes of Tensile output files violate this:

1. **`TensileLibrary_Type_*_fallback.dat`** -- solution metadata for fallback
   kernels. Content varies per shard because each shard processes only its arches'
   logic files, producing different solution indices and different architecture
   references.

2. **`TensileManifest.txt`** -- a flat list of all generated files. Each shard
   produces a different list.

When shards are overlaid, the last writer wins, silently losing other shards' data.
This causes runtime failures: rocBLAS may attempt to load code objects referenced in
a now-missing shard's metadata, or miss solutions entirely.

## Architecture of Tensile Library Generation

### File naming in lazy-loading mode

Tensile's `--separate-architectures --lazy-library-loading` mode (the mode used by
rocBLAS in TheRock) produces:

| File pattern | Arch in name? | Content varies per arch? |
|---|---|---|
| `TensileLibrary_lazy_<gfx>.dat` | Yes | Yes (parent catalog) |
| `TensileLibrary_Type_*_<gfx>.dat` | Yes | Yes (per-arch child catalog) |
| `TensileLibrary_Type_*_fallback.dat` | **No** | **Yes (per-shard)** |
| `TensileLibrary_Type_*_fallback_<gfx>.hsaco` | Yes | Yes (code objects) |
| `TensileLibrary_Type_*_<gfx>.co` | Yes | Yes (code objects) |
| `TensileManifest.txt` | No | Yes (file list) |

The fallback `.dat` contains solution metadata (solution indices, problem
predicates, matching tables) for the fallback kernels. The fallback `.hsaco` code
objects are already arch-tagged. Only the metadata file lacks an arch qualifier.

### How the fallback name is constructed

In `SolutionLibrary.py`, `MasterSolutionLibrary.FromOriginalState()` builds the
placeholder name by walking library creation functions in reverse:

```
"TensileLibrary" + placeholderStr(type, operation) -> e.g. "TensileLibrary_Type_SS_Contraction_l_Alik_Bjlk_Cijk_Dijk"
```

The `"fallback"` architecture's logic files go through `makeMasterLibraries()` which
creates `masterLibraries["fallback"]`. Then `addFallback()` calls `insert()` to copy
the fallback's `lazyLibraries` into each arch's master library. The lazy library
key is the placeholder name (e.g. `"TensileLibrary_Type_SS_..._fallback"`).

The crucial point: the `_fallback` suffix comes from the **logic file's architecture
name being "fallback"**, which triggers `addFallback()`. But the placeholder name
never includes the target architecture. In a single-build with all arches, this is
fine because all arches share the same fallback object. In a multi-shard build, each
shard produces its own fallback with different content.

### How rocBLAS loads fallback metadata at runtime

In `PlaceholderLibrary.hpp`, when a solution is needed:

1. The parent catalog (`TensileLibrary_lazy_<gfx>.dat`) contains Placeholder entries
   with `value: TensileLibrary_Type_..._fallback`
2. `PlaceholderLibrary::loadPlaceholderLibrary()` loads
   `libraryDirectory + "/" + filePrefix + suffix` where `filePrefix` is the
   placeholder name and `suffix` is `.dat`
3. The loaded metadata tells the runtime which `.hsaco` to load for each solution

The filename is constructed at deserialization time in
`Serialization/PlaceholderLibrary.hpp` lines 53-71.

## Proposed Fix

### Part 1: Make fallback .dat files arch-specific (Tensile Python)

**File:** `Tensile/TensileCreateLibrary.py`

Change `generateLazyMasterFileList()` to produce per-arch fallback names.

Currently (line 1299):
```python
def generateLazyMasterFileList(
    masterFileList: List[Tuple[str, MasterSolutionLibrary]],
) -> List[Tuple[str, MasterSolutionLibrary]]:
    return [t for _, lib in masterFileList for t in lib.lazyLibraries.items()]
```

This returns entries like `("TensileLibrary_Type_SS_..._fallback", lib)` --
multiple copies (one per arch) but all with the same name. The writer
deduplicates and writes a single file.

**Proposed change:** When the lazy library name contains `_fallback`, append the
arch from the parent master file name:

```python
def generateLazyMasterFileList(
    masterFileList: List[Tuple[str, MasterSolutionLibrary]],
) -> List[Tuple[str, MasterSolutionLibrary]]:
    result = []
    for parentName, lib in masterFileList:
        # Extract arch from parent name like "TensileLibrary_lazy_gfx900"
        arch = parentName.rsplit("_", 1)[-1]  # "gfx900"
        for lazyName, lazyLib in lib.lazyLibraries.items():
            if "fallback" in lazyName:
                # Make fallback arch-specific: TensileLibrary_Type_..._fallback_gfx900
                archLazyName = f"{lazyName}_{arch}"
            else:
                archLazyName = lazyName
            result.append((archLazyName, lazyLib))
    return result
```

This also requires updating the placeholder name stored in the parent catalog,
so the runtime knows to look for the arch-specific file. The placeholder name is
set on `PlaceholderLibrary.filenamePrefix` at line 466 of `SolutionLibrary.py`:

```python
rv.lazyLibraries[placeholderName] = lazyLibrary
placeholderLibrary.filenamePrefix = placeholderName
```

**Approach:** Rather than changing the Python-side placeholder naming (which is
deeply wired), update `writeMasterFile()` to rewrite placeholder references in
the parent catalog when writing. Alternatively, update `addFallback()` to produce
per-arch copies of the lazy library with arch-specific names.

The cleanest approach is to modify `addFallback()` so that when it inserts
fallback libraries into each arch, it renames them:

**File:** `Tensile/TensileCreateLibrary.py`, function `addFallback()` (line 949)

```python
def addFallback(masterLibraries: Dict[str, MasterSolutionLibrary]) -> None:
    archs, _ = splitArchs()

    fallbackLib = masterLibraries["fallback"]
    for key, value in masterLibraries.items():
        if key != "fallback":
            # Rename fallback lazy libraries to include the target arch
            renamedFallback = _renameFallbackLazyLibs(fallbackLib, key)
            value.insert(renamedFallback)

    for archName in archs:
        archName = archName.split("-", 1)[0]
        if archName not in masterLibraries:
            tPrint(1, "Using fallback for arch: " + archName)
            masterLibraries[archName] = _renameFallbackLazyLibs(fallbackLib, archName)

    masterLibraries.pop("fallback")


def _renameFallbackLazyLibs(lib: MasterSolutionLibrary, arch: str) -> MasterSolutionLibrary:
    """Create a shallow copy of the library with arch-suffixed fallback lazy library names."""
    import copy
    renamed = copy.copy(lib)
    renamed.lazyLibraries = {}
    for name, lazyLib in lib.lazyLibraries.items():
        if "fallback" in name:
            renamed.lazyLibraries[f"{name}_{arch}"] = lazyLib
        else:
            renamed.lazyLibraries[name] = lazyLib
    # Also update the filenamePrefix on any PlaceholderLibrary nodes
    # that reference the old name
    return renamed
```

Additionally, the `PlaceholderLibrary` objects in the parent catalog's library
tree need their `filenamePrefix` updated. This happens at `SolutionLibrary.py`
line 466 where `placeholderLibrary.filenamePrefix = placeholderName`. Since
`insert()` merges the library tree (line 554: `self.library.merge(other.library)`),
the placeholder nodes from the fallback get merged into the arch's library tree.

The placeholder nodes need to be renamed to match the new lazy library key.
This requires traversing the library tree after insert to update any
PlaceholderLibrary nodes whose `filenamePrefix` matches a renamed fallback name.

**File:** `Tensile/SolutionLibrary.py`, add helper to `MasterSolutionLibrary`:

```python
def renamePlaceholders(self, renameMap: dict):
    """Rename PlaceholderLibrary filenamePrefix values according to renameMap."""
    def _walk(lib):
        if isinstance(lib, PlaceholderLibrary) and lib.filenamePrefix in renameMap:
            lib.filenamePrefix = renameMap[lib.filenamePrefix]
        elif isinstance(lib, PredicateLibrary):
            for row in lib.rows:
                _walk(row["library"])
        elif isinstance(lib, MasterSolutionLibrary):
            _walk(lib.library)
    _walk(self.library)
```

### Part 2: Update C++ runtime loading (backward compatibility)

**File:** `Tensile/Source/lib/include/Tensile/Serialization/PlaceholderLibrary.hpp`

No changes required for the C++ deserialization. The parent catalog
(`TensileLibrary_lazy_gfx900.dat`) already records the placeholder `value` field,
which will now be `TensileLibrary_Type_..._fallback_gfx900` instead of
`TensileLibrary_Type_..._fallback`. The runtime constructs the path from this
value (line 159 of `PlaceholderLibrary.hpp`):

```cpp
auto newLibrary = LoadLibraryFile<MyProblem, MySolution>(
    (libraryDirectory + "/" + filePrefix + suffix).c_str());
```

Since the `filePrefix` is read from the `.dat` file, and the `.dat` now stores the
arch-specific name, the runtime will automatically look for the arch-specific file.

**For backward compatibility** with old pre-built libraries that still have
arch-neutral fallback names: no C++ change is needed. Old `.dat` parent catalogs
still store the old `filePrefix` without arch, so they will still load the old
arch-neutral file. New parent catalogs store the new `filePrefix` with arch, so
they load the new file. The format is self-consistent.

### Part 3: Update code object filename generation

**File:** `Tensile/Source/lib/include/Tensile/PlaceholderLibrary.hpp`

The `getCodeObjectFileName()` function (line 186-208) needs to handle the new
naming. Currently:

```cpp
if(coFileDependency.find("fallback") != std::string::npos)
    coFileDependency += std::string("_") + arch + std::string(".hsaco");
```

This takes `TensileLibrary_Type_..._fallback` and appends `_gfx900.hsaco` to get
`TensileLibrary_Type_..._fallback_gfx900.hsaco`.

With the new naming, `filePrefix` is already
`TensileLibrary_Type_..._fallback_gfx900`. The code would produce
`TensileLibrary_Type_..._fallback_gfx900_gfx900.hsaco` -- a double arch suffix.

**Fix:** Strip the trailing `_<arch>` from `filePrefix` before appending the
runtime-detected arch. Or better, change the code to detect that the arch is
already present:

```cpp
std::string getCodeObjectFileName(Hardware const&   hardware,
                                  MySolution const& solution) const
{
    std::string coFileDependency = filePrefix;

    if(solution.isSourceKernel())
    {
        std::string arch = hardware.archName();
        auto pos = arch.find(":");
        if(pos != std::string::npos)
            arch.resize(pos);

        if(coFileDependency.find("fallback") != std::string::npos)
        {
            // New format: filePrefix already ends with _<arch>
            // Old format: filePrefix ends with _fallback (no arch)
            // Check if arch is already in the name
            if(coFileDependency.rfind("_" + arch) == std::string::npos
               || coFileDependency.rfind("_" + arch) + arch.size() + 1
                   != coFileDependency.size())
            {
                coFileDependency += std::string("_") + arch;
            }
            coFileDependency += std::string(".hsaco");
        }
        else
            coFileDependency += std::string(".hsaco");
    }
    else
        coFileDependency += std::string(".co");

    return coFileDependency;
}
```

A cleaner approach: since the `.hsaco` files already use the pattern
`TensileLibrary_Type_..._fallback_<arch>.hsaco` (the code objects themselves are
not changing), we can strip the trailing arch from `filePrefix` before re-appending:

```cpp
if(coFileDependency.find("fallback") != std::string::npos)
{
    // Strip any trailing _gfxNNNN that may have been added to the prefix
    // for shard-safe naming, then re-add the runtime arch
    std::string fallbackBase = coFileDependency;
    auto fallbackPos = fallbackBase.find("fallback");
    if(fallbackPos != std::string::npos)
        fallbackBase.resize(fallbackPos + strlen("fallback"));
    coFileDependency = fallbackBase + "_" + arch + ".hsaco";
}
```

This is backward-compatible: both old (`filePrefix = "..._fallback"`) and new
(`filePrefix = "..._fallback_gfx900"`) produce `..._fallback_gfx900.hsaco`.

### Part 4: TensileManifest.txt

**File:** `Tensile/TensileCreateLibrary.py`, line 72 and line 1471

The manifest is written to a fixed path `TensileManifest.txt`. In a multi-shard
overlay, each shard's manifest lists different files.

**Options:**

A. **Make manifest arch-specific:** `TensileManifest_<arch>.txt`. This requires
   changes to `TensileConfig.cmake` (line 251) and the `--verify-manifest` code
   path.

B. **Generate manifest as a union at overlay time:** The TheRock overlay/merge
   step concatenates and deduplicates manifests. This keeps Tensile unchanged.

C. **Remove manifest from overlay:** Since the manifest is only used for build
   verification (line 278 in TensileConfig.cmake: `--verify-manifest`), not at
   runtime, exclude it from the installed artifacts.

**Recommendation:** Option C. The manifest is a build-time artifact, not a runtime
one. It should not appear in the installed `dist/` tree. If rocBLAS's CMake install
rules copy it, those rules should be adjusted to skip it. Verification happens
during the build, before overlay.

If the manifest must be in the overlay for downstream tooling, use Option A:
rename to `TensileManifest_<arch>.txt` by parameterizing `TENSILE_MANIFEST_FILENAME`
with the target architecture.

### Part 5: hipBLASLt / TensileLite

TensileLite (hipBLASLt's fork of Tensile) has a structurally similar fallback
mechanism. Key differences:

- TensileLite does NOT produce a `TensileManifest.txt` (no manifest issue).
- TensileLite's `Run.py` (line 686-690) handles fallback similarly: it merges
  `masterLibraries["fallback"]` into each arch, then pops it.
- TensileLite's PlaceholderLibrary (`hipblaslt/tensilelite/include/Tensile/PlaceholderLibrary.hpp`)
  has a simpler `getCodeObjectFileName()` that does not do the fallback arch
  appendage (it is always `.co`, not `.hsaco` for source kernels).

**Assessment:** TensileLite's fallback lazy library names have the same convergence
problem. The fix is structurally identical: modify the fallback merge in `Run.py`
to produce arch-specific lazy library names. However, since TensileLite does not
appear to produce fallback `.hsaco` files (it uses `.co` for all code objects),
the C++ `getCodeObjectFileName()` change is simpler -- no arch stripping needed.

**Files to change in TensileLite:**
- `hipblaslt/tensilelite/Tensile/TensileCreateLibrary/Run.py` -- rename fallback
  lazy libs during merge
- `hipblaslt/tensilelite/Tensile/SolutionLibrary.py` -- add `renamePlaceholders()`
- `hipblaslt/tensilelite/include/Tensile/PlaceholderLibrary.hpp` -- no change needed
  if TensileLite code objects don't use the fallback+arch pattern

## Summary of Changes

| Repository | File | Change |
|---|---|---|
| Tensile | `Tensile/TensileCreateLibrary.py` | Modify `addFallback()` and `generateLazyMasterFileList()` to produce arch-specific fallback names |
| Tensile | `Tensile/SolutionLibrary.py` | Add `renamePlaceholders()` helper, update `insert()` to propagate renames |
| Tensile | `Tensile/Source/lib/include/Tensile/PlaceholderLibrary.hpp` | Update `getCodeObjectFileName()` to handle arch-suffixed fallback prefix without double-suffixing |
| rocBLAS | `library/src/tensile_host.cpp` | No changes needed (filename comes from .dat) |
| TensileLite | `Tensile/TensileCreateLibrary/Run.py` | Same pattern as Tensile: arch-specific fallback names |
| TensileLite | `Tensile/SolutionLibrary.py` | Same pattern as Tensile |
| TheRock | `TensileConfig.cmake` or install rules | Exclude `TensileManifest.txt` from installed artifacts, or make it arch-specific |

## Backward Compatibility

- **New Tensile + old rocBLAS:** Works. Old rocBLAS reads the parent `.dat` which
  now points to `..._fallback_gfx900.dat`. The Tensile C++ library (linked into
  rocBLAS) handles the loading. As long as rocBLAS is rebuilt with the new Tensile
  C++ headers, it picks up the new code path.

- **Old Tensile + new rocBLAS:** Works. Old parent `.dat` still points to
  `..._fallback.dat` (no arch). The new `getCodeObjectFileName()` handles both old
  and new format.

- **Single-arch build (no sharding):** Works identically. Each arch gets its own
  fallback name, no collisions occur, and the output is functionally equivalent.

## Alternatives Considered

### 1. Fix in the splitter/overlay tool instead of Tensile

Rejected. The splitter cannot merge `.dat` files -- they are binary msgpack blobs
with solution indices that would conflict. The splitter can only detect the
violation, not fix it. The fix must be at the source.

### 2. Use subdirectories per arch instead of filename suffixes

E.g. `library/gfx900/TensileLibrary_Type_..._fallback.dat`. This would require
significant changes to both the Python generator and the C++ runtime, since the
current code assumes all library files are in a flat directory. The parent catalog
would need to encode subdirectory paths in placeholder values. Higher risk for
lower benefit.

### 3. Build all arches in a single Tensile invocation (no sharding)

This defeats the purpose of sharding (build parallelism, CI cache efficiency).
Not viable for TheRock's CI architecture.

### 4. Post-process: merge fallback .dat files from all shards

Would require a custom merge tool that understands msgpack Tensile library format,
reconciles solution indices, and produces a valid combined file. Fragile and
tightly coupled to Tensile internals. The in-Tensile fix is more maintainable.

## Risk Assessment

- **Medium complexity:** The core change is renaming dictionary keys and updating
  a few string manipulations. The library tree traversal for placeholder renaming
  adds some complexity.
- **Testing:** Requires building rocBLAS with `--separate-architectures
  --lazy-library-loading` for multiple arches and verifying that (a) the fallback
  .dat files now have arch in the name, (b) rocBLAS runtime loads them correctly,
  (c) single-arch builds still work.
- **TensileLite risk is lower** since hipBLASLt's code object naming is simpler.

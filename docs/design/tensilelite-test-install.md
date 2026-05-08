# TensileLite Test Artifact Install

**Date:** 2026-05-08
**Status:** Implementation recipe
**Audience:** hipBLASLt and TheRock engineers implementing test-time
TensileLite distribution

## Problem

hipBLASLt currently uses `tensilelite` as a build-time Python component for
production GEMM kernel generation. It is not installed into TheRock artifacts.
That is too restrictive for the next testing use cases:

- Unit test and validate the TensileLite generator and its captive `rocisa`
  Python extension after artifacts are assembled.
- Exercise generator configurations that are not covered by the production GEMM
  generation schedule.
- Run generator validation in test environments with simulators or hardware that
  are not available on the CPU-focused build machines.

The required output is test-only distribution of TensileLite from hipBLASLt's
install tree. This should not become part of the normal ROCm runtime or
developer packages unless a later product decision explicitly changes that.

## Decisions

- Install exploded Python files, not wheels.
- Use one Python import root:

  ```bash
  PYTHONPATH="$ROCM_PATH/share/hipblaslt/tensilelite"
  ```

- Normalize the install layout so `Tensile` and `rocisa` are sibling packages.
  Do not mirror the source checkout's nested `tensilelite/rocisa/rocisa`
  structure in the install tree.
- Build the distributed `rocisa` extension with the Python stable ABI, using
  Python 3.12 or newer at build time. The installed extension should be usable
  from Python 3.12 and newer test environments.
- Include the C++ TensileLite client/test tools in the hipBLASLt test artifact
  so generator tests can use a prebuilt client instead of attempting to build one
  on the test machine.
- Run the new test-time checks on both Linux and Windows.

## Target Install Layout

The hipBLASLt test component should install these paths:

```text
share/hipblaslt/tensilelite/
  Tensile/
    __init__.py
    bin/Tensile
    bin/TensileCreateLibrary
    ...
  rocisa/
    __init__.py
    _rocisa.abi3.so        # Linux
    _rocisa.pyd            # Windows, if nanobind/CMake emit this suffix
    libstinkytofu.so       # Linux only if _rocisa has this runtime dependency
  rocisa_tests/
    test_*.py
  AMaxGenerator.py
  ExtOpCreateLibrary.py
  LayerNormGenerator.py
  SoftmaxGenerator.py
  pytest.ini
  requirements.txt

libexec/hipblaslt/tensilelite/
  tensilelite-client
  cpu-gemm-driver
  tensilelite-tests
```

This layout is intentionally different from the source checkout:

```text
tensilelite/
  Tensile/
  rocisa/
    rocisa/
```

In the install tree, the inner `rocisa/rocisa` package becomes the installed
top-level `rocisa` package. With that normalization, all existing imports work
with one `PYTHONPATH` entry:

```python
import Tensile
import rocisa
import rocisa.instruction
```

Do not require this test setup:

```bash
PYTHONPATH="$ROCM_PATH/share/hipblaslt/tensilelite:$ROCM_PATH/share/hipblaslt/tensilelite/rocisa"
```

That two-entry form is a source-tree workaround and should not be the installed
artifact contract.

## hipBLASLt Build Changes

Add a test-artifact install option in
`sources/TheRock/rocm-libraries/projects/hipblaslt/CMakeLists.txt`:

```cmake
option(HIPBLASLT_INSTALL_TENSILELITE_TEST_ARTIFACTS
       "Install TensileLite Python and test tools for artifact testing."
       OFF)
```

The option should install into the existing `tests` component only. It should
not change runtime, devel, benchmarks, samples, or docs components.

Install the Python package tree with explicit layout control instead of one
blind `install(DIRECTORY tensilelite ...)` rule. The key rule is to install:

- `${SOURCE_DIR}/tensilelite/Tensile` to
  `${CMAKE_INSTALL_DATADIR}/hipblaslt/tensilelite/Tensile`
- `${SOURCE_DIR}/tensilelite/rocisa/rocisa/__init__.py` to
  `${CMAKE_INSTALL_DATADIR}/hipblaslt/tensilelite/rocisa`
- the built `_rocisa` module to
  `${CMAKE_INSTALL_DATADIR}/hipblaslt/tensilelite/rocisa`
- `${SOURCE_DIR}/tensilelite/rocisa/test` to
  `${CMAKE_INSTALL_DATADIR}/hipblaslt/tensilelite/rocisa_tests`
- the top-level generator scripts used by hipBLASLt and tests to
  `${CMAKE_INSTALL_DATADIR}/hipblaslt/tensilelite`

Use `FILES_MATCHING` or explicit file lists to keep build directories, caches,
virtualenvs, `.pytest_cache`, `__pycache__`, and source-only packaging metadata
out of artifacts.

The install rule must preserve executable permissions on `Tensile/bin/Tensile`
and `Tensile/bin/TensileCreateLibrary`. Use `install(PROGRAMS ...)` for those
entrypoint files if a recursive directory install does not preserve mode
reliably on all platforms.

Install the C++ tools when they are built:

```cmake
install(
  TARGETS tensilelite-client cpu-gemm-driver tensilelite-tests
  RUNTIME DESTINATION "${CMAKE_INSTALL_LIBEXECDIR}/hipblaslt/tensilelite"
  COMPONENT tests
)
```

On Windows, verify whether the executable suffix is automatically handled by
`install(TARGETS ...)`; do not hard-code `.exe` in the artifact descriptor.

## rocisa Stable ABI

`rocisa` currently uses nanobind and is built from
`tensilelite/rocisa/CMakeLists.txt`. For distributed test artifacts, add an
option such as:

```cmake
option(ROCISA_USE_STABLE_ABI
       "Build rocisa as a Python stable ABI extension."
       OFF)
```

When this option is enabled:

- Require Python 3.12 or newer.
- Build `_rocisa` with nanobind's `STABLE_ABI` flag.
- Expect the Linux extension suffix to include `abi3`, for example
  `_rocisa.abi3.so`.
- Keep the existing non-stable build path available for local developer builds
  if the team still needs Python 3.10 or 3.11 locally.

The current project minimum CMake version is 3.25.2. CMake's
`Development.SABIModule` component and `Python::SABIModule` target were added
in CMake 3.26. If the implementation wants to use those directly, either raise
the relevant CMake minimum to 3.26 or gate the stable-ABI path with a clear
configure-time error when CMake is too old. If relying only on nanobind's
high-level `STABLE_ABI` support, still test with the CMake version used by
TheRock builders and fail configuration early if the emitted extension is not
an `abi3` module.

The existing Linux runtime placement for `_rocisa` matters:

- `_rocisa` sets `INSTALL_RPATH "$ORIGIN"`.
- The standalone wheel path co-installs `libstinkytofu.so` next to `_rocisa`.

Preserve that rule in the exploded install. If `_rocisa` links a shared
`stinkytofu` library on Linux, install the shared library into the same
`share/hipblaslt/tensilelite/rocisa` directory. Windows currently builds
`stinkytofu` static, so a separate DLL may not be needed there; verify with the
actual link output.

## TheRock Integration

In `sources/TheRock/math-libs/BLAS/CMakeLists.txt`, extend the hipBLASLt
subproject CMake arguments when `THEROCK_BUILD_TESTING` is enabled:

```cmake
-DHIPBLASLT_INSTALL_TENSILELITE_TEST_ARTIFACTS=${THEROCK_BUILD_TESTING}
-DROCISA_USE_STABLE_ABI=ON
-DTENSILELITE_ENABLE_CLIENT=${THEROCK_BUILD_TESTING}
-DTENSILELITE_BUILD_TESTING=${THEROCK_BUILD_TESTING}
```

Keep `TENSILELITE_BUILD_TESTING=OFF` when tests are not being built so normal
builds do not pick up the extra client/test cost.

In `sources/TheRock/math-libs/BLAS/artifact-blas.toml`, extend the hipBLASLt
test component:

```toml
[components.test."math-libs/BLAS/hipBLASLt/stage"]
include = [
  # Existing entries...
  "share/hipblaslt/tensilelite/**",
  "libexec/hipblaslt/tensilelite/**",
]
```

Do not add these paths to `[components.lib]`, `[components.dev]`, or
`[components.doc]`.

No new TheRock artifact name should be necessary. The current hipBLASLt test
job already fetches:

```text
--blas --tests
```

That should continue to be enough once the files are included in the existing
BLAS test component.

## Test Script Changes

Extend both copies of the hipBLASLt test script if the repository still carries
the legacy and migrated paths:

- `sources/TheRock/build_tools/github_actions/test_executable_scripts/test_hipblaslt.py`
- `sources/TheRock/rocm-libraries/test/therock/test_hipblaslt.py`

The script should keep the existing `hipblaslt-test` execution unchanged, then
run TensileLite checks from the installed artifact.

Set these paths from `THEROCK_BIN_DIR`:

```python
rocm_path = Path(THEROCK_BIN_DIR).resolve().parent
tensilelite_root = rocm_path / "share" / "hipblaslt" / "tensilelite"
tensilelite_client = (
    rocm_path / "libexec" / "hipblaslt" / "tensilelite" / "tensilelite-client"
)
```

For Windows, append `.exe` by probing the filesystem rather than hard-coding a
platform branch:

```python
if not tensilelite_client.exists():
    candidate = tensilelite_client.with_suffix(".exe")
    if candidate.exists():
        tensilelite_client = candidate
```

Set one Python path:

```python
env = os.environ.copy()
existing_pythonpath = env.get("PYTHONPATH")
env["PYTHONPATH"] = (
    f"{tensilelite_root}{os.pathsep}{existing_pythonpath}"
    if existing_pythonpath
    else str(tensilelite_root)
)
env["ROCM_PATH"] = str(rocm_path)
```

Add a smoke import before running larger tests. This catches layout mistakes
quickly and gives an obvious failure if someone accidentally installs
`rocisa/rocisa` and requires two `PYTHONPATH` entries:

```python
subprocess.run(
    [
        sys.executable,
        "-c",
        "import Tensile, rocisa, rocisa.instruction; "
        "print(Tensile.ROOT_PATH); print(rocisa.__file__)",
    ],
    check=True,
    cwd=THEROCK_DIR,
    env=env,
)
```

Run the Python tests in tiers:

```python
subprocess.run(
    [sys.executable, "-m", "pytest", str(tensilelite_root / "rocisa_tests")],
    check=True,
    cwd=THEROCK_DIR,
    env=env,
)

subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        str(tensilelite_root / "Tensile" / "Tests" / "unit"),
    ],
    check=True,
    cwd=THEROCK_DIR,
    env=env,
)
```

Then add a selected generator validation subset. Keep the first implementation
small and explicit so failures are actionable:

```python
subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        str(tensilelite_root / "Tensile" / "Tests" / "common"),
        "-m",
        "common and not validateAll",
        "--prebuilt-client",
        str(tensilelite_client),
    ],
    check=True,
    cwd=THEROCK_DIR,
    env=env,
)
```

The exact marker expression can be tightened by the hipBLASLt team based on
runtime. The important contract is that common generator tests use the installed
prebuilt client rather than building a client in the test job.

## Test Dependencies

The TheRock test Python environment already includes `pytest`, `PyYAML`, and
`packaging`. Audit the actual selected tests for additional imports before
enabling them in CI. Likely candidates include:

- `msgpack`
- `joblib`
- `simplejson`
- `ujson`
- `orjson`
- `yappi`
- `filelock`
- `pytest-xdist`

Add only the dependencies needed by the enabled test subsets to TheRock's test
requirements. Do not install build-only packages such as `cmake` or `nanobind`
into the runtime test environment unless a selected test imports them directly.

## Validation Checklist

Before submitting the implementation, verify these cases:

- The hipBLASLt stage tree contains `share/hipblaslt/tensilelite/Tensile`.
- The hipBLASLt stage tree contains `share/hipblaslt/tensilelite/rocisa`.
- The hipBLASLt stage tree does not contain
  `share/hipblaslt/tensilelite/rocisa/rocisa`.
- With only `PYTHONPATH=$stage/share/hipblaslt/tensilelite`, these imports pass:

  ```bash
  python -c "import Tensile, rocisa, rocisa.instruction"
  ```

- On Linux, the installed `_rocisa` filename contains `abi3`.
- On Linux, `ldd` on `_rocisa.abi3.so` does not report missing libraries.
- On Windows, importing `rocisa` succeeds from Python 3.12 and at least one
  newer Python version available in CI.
- `artifact-blas.toml` includes the TensileLite tree only in the test component.
- A TheRock install using `install_rocm_from_artifacts.py --blas --tests`
  includes the TensileLite files.
- A TheRock install that omits `--tests` does not include the TensileLite test
  tree.

## Known Risks

- Stable ABI support depends on building with Python 3.12 or newer. If a builder
  silently finds Python 3.10 or 3.11, the build must fail instead of producing a
  version-specific extension.
- CMake 3.25 cannot use `Development.SABIModule`. Either rely on nanobind's
  high-level stable ABI support with an artifact suffix check, or raise the
  minimum/gate the option to CMake 3.26 or newer.
- The Python test subsets may expose missing pure-Python dependencies in TheRock
  test environments. Add them deliberately after auditing the selected tests.
- Generator validation can be expensive. Start with a small selected common-test
  subset and expand once runtime is measured on Linux and Windows runners.
- Keep the installed layout as the contract. Avoid test-script workarounds that
  append both the install root and a nested `rocisa` directory to `PYTHONPATH`;
  that would hide packaging mistakes.

## Acceptance Criteria

The implementation is complete when a TheRock hipBLASLt test artifact can be
installed on Linux and Windows, a test script can set exactly one `PYTHONPATH`
entry pointing at `share/hipblaslt/tensilelite`, and all selected `rocisa`,
TensileLite unit, and generator validation tests pass using the installed
prebuilt client.

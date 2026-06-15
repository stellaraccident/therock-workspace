#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Bootstrap a minimal TheRock core build with prebuilt nightly LLVM artifacts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence
import urllib.error
import urllib.parse
import urllib.request


os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

ARTIFACT_COMPONENTS = ["lib", "run", "dev", "dbg", "doc", "test"]
ARTIFACT_EXTENSIONS = [".tar.zst", ".tar.xz"]
ARTIFACT_TARGET_FAMILIES = ["generic"]
DEFAULT_AMDGPU_FAMILIES = "gfx1201"
DEFAULT_BRANCH = "main"
DEFAULT_BUILD_DIR = Path("build/rocm-core")
DEFAULT_EVENT = "schedule"
DEFAULT_PLATFORM = "linux"
DEFAULT_RELEASE_TYPE = "ci"
DEFAULT_REPO = "ROCm/TheRock"
DEFAULT_SOURCE_SETS = ["base", "rocm-systems"]
DEFAULT_WORKFLOW = "ci_nightly.yml"
DEFAULT_DOWNLOAD_CACHE_DIR = Path(".tmp/therock-minimal-core-artifacts")
MINIMAL_CORE_ARTIFACTS = [
    "amd-llvm",
    "base",
    "core-amdsmi",
    "core-hip",
    "core-kpack",
    "core-ocl",
    "core-ocl-icd",
    "core-runtime",
    "elfio",
    "flatbuffers",
    "nlohmann-json",
    "sysdeps",
    "sysdeps-expat",
    "sysdeps-gmp",
    "sysdeps-hwloc",
    "sysdeps-libpciaccess",
    "sysdeps-mpfr",
    "sysdeps-ncurses",
    "sysdeps-util-linux",
]
DEFAULT_BUILD_TARGETS = [
    f"artifact-{artifact_name}" for artifact_name in MINIMAL_CORE_ARTIFACTS
]
FORBIDDEN_DIST_GLOBS = [
    "*fftw*",
    "*blas*",
    "*rocprofiler-compute*",
    "*rocprof-compute*",
    "*rocprofiler-systems*",
    "*rocprof-sys*",
]
EXPECTED_COMPILER_MARKERS = [
    Path("compiler/amd-llvm/stage.prebuilt"),
    Path("compiler/amd-comgr/stage.prebuilt"),
    Path("compiler/hipcc/stage.prebuilt"),
]
EXPECTED_SYSDEPS_MARKERS = [
    Path("third-party/sysdeps/linux/zlib/build/stage.prebuilt"),
    Path("third-party/sysdeps/linux/zstd/build/stage.prebuilt"),
    Path("third-party/sysdeps/linux/numactl/build/stage.prebuilt"),
    Path("third-party/sysdeps/linux/elfutils/build/stage.prebuilt"),
    Path("third-party/sysdeps/linux/libdrm/build/stage.prebuilt"),
]


@dataclass(frozen=True)
class BootstrapArtifactSpec:
    """Artifact family to import before configuring the local build."""

    name: str
    required_components: tuple[str, ...]
    components: tuple[str, ...] = tuple(ARTIFACT_COMPONENTS)
    target_families: tuple[str, ...] = tuple(ARTIFACT_TARGET_FAMILIES)


BOOTSTRAP_ARTIFACTS = [
    BootstrapArtifactSpec(name="sysdeps", required_components=("dev", "lib")),
    BootstrapArtifactSpec(name="amd-llvm", required_components=("dev", "lib", "run")),
]


@dataclass(frozen=True)
class WorkflowRun:
    """GitHub Actions workflow run metadata needed for artifact lookup."""

    run_id: str
    run_number: int
    html_url: str
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    created_at: str
    updated_at: str
    raw: dict[str, object]


@dataclass(frozen=True)
class ArtifactSelection:
    """Selected workflow run and matching artifact archives."""

    run: WorkflowRun
    filenames: list[str]
    backend: object


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def workspace_root_from_cwd() -> Path:
    """Find a workspace root containing sources/TheRock."""
    current = Path.cwd().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "sources" / "TheRock").is_dir():
            return candidate
    return current


def require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"Expected {label} to be a JSON object")
    return {str(key): item for key, item in value.items()}


def require_sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        fail(f"Expected {label} to be a JSON array")
    return value


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def required_string(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if value is None:
        fail(f"GitHub response is missing required key: {key}")
    return str(value)


def optional_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def github_api_get(url: str) -> dict[str, object]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "therock-minimal-core-bootstrap",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        fail(f"GitHub API request failed for {url}: HTTP {exc.code}\n{details}")
    except urllib.error.URLError as exc:
        fail(f"GitHub API request failed for {url}: {exc}")

    return require_mapping(payload, "GitHub API response")


def query_workflow_runs(
    *,
    repo: str,
    workflow: str,
    branch: str,
    event: str,
    conclusion: str,
    max_runs: int,
) -> list[WorkflowRun]:
    owner_repo = urllib.parse.quote(repo, safe="/")
    workflow_name = urllib.parse.quote(workflow, safe="")
    query: dict[str, str] = {
        "branch": branch,
        "status": "completed",
        "per_page": str(min(max_runs, 100)),
    }
    if event:
        query["event"] = event
    url = (
        f"https://api.github.com/repos/{owner_repo}/actions/workflows/"
        f"{workflow_name}/runs?{urllib.parse.urlencode(query)}"
    )
    payload = github_api_get(url)
    runs_json = require_sequence(payload.get("workflow_runs"), "workflow_runs")

    runs: list[WorkflowRun] = []
    for item in runs_json[:max_runs]:
        run = require_mapping(item, "workflow run")
        run_conclusion = optional_string(run.get("conclusion"))
        if conclusion and run_conclusion != conclusion:
            continue
        runs.append(
            WorkflowRun(
                run_id=required_string(run, "id"),
                run_number=optional_int(run.get("run_number")),
                html_url=required_string(run, "html_url"),
                head_sha=required_string(run, "head_sha"),
                event=required_string(run, "event"),
                status=required_string(run, "status"),
                conclusion=run_conclusion,
                created_at=required_string(run, "created_at"),
                updated_at=required_string(run, "updated_at"),
                raw=run,
            )
        )
    return runs


def import_therock_artifact_tools(
    therock_dir: Path,
) -> tuple[object, tuple[object, object]]:
    build_tools_dir = therock_dir / "build_tools"
    if not build_tools_dir.is_dir():
        fail(f"TheRock build_tools directory not found: {build_tools_dir}")
    sys.path.insert(0, os.fspath(build_tools_dir))
    try:
        from artifact_manager import BootstrappingPopulator
        from _therock_utils.artifact_backend import S3Backend
        from _therock_utils.workflow_outputs import WorkflowOutputRoot
    except ModuleNotFoundError as exc:
        fail(
            "Failed to import TheRock artifact tooling. Use the workspace venv "
            "or install TheRock requirements first, for example:\n"
            "  ./.venv/bin/python -m pip install -r sources/TheRock/requirements.txt\n"
            f"Import error: {exc}"
        )
    return BootstrappingPopulator, (S3Backend, WorkflowOutputRoot)


def make_backend(
    *,
    s3_backend_class: object,
    workflow_output_root_class: object,
    run: WorkflowRun | None,
    run_id: str,
    platform: str,
    repo: str,
    release_type: str,
) -> object:
    if run is not None:
        output_root = workflow_output_root_class.from_workflow_run(
            run_id=run.run_id,
            platform=platform,
            github_repository=repo,
            workflow_run=run.raw,
            release_type=release_type,
        )
    else:
        output_root = workflow_output_root_class.from_workflow_run(
            run_id=run_id,
            platform=platform,
            github_repository=repo,
            lookup_workflow_run=True,
            release_type=release_type,
        )
    return s3_backend_class(output_root=output_root)


def find_matching_artifacts(
    *,
    backend: object,
) -> list[str]:
    matched: list[str] = []
    for spec in BOOTSTRAP_ARTIFACTS:
        artifact_matches: list[str] = []
        for component in spec.components:
            for target_family in spec.target_families:
                for extension in ARTIFACT_EXTENSIONS:
                    filename = f"{spec.name}_{component}_{target_family}{extension}"
                    if artifact_exists_https(backend, filename):
                        artifact_matches.append(filename)
                        break

        components = {
            filename.removeprefix(f"{spec.name}_").split("_", 1)[0]
            for filename in artifact_matches
        }
        missing = set(spec.required_components) - components
        if missing:
            fail(
                f"Found {spec.name} artifacts, but required components are missing: "
                f"{', '.join(sorted(missing))}. Matched: {artifact_matches}"
            )
        matched.extend(artifact_matches)
    return matched


def select_artifacts(args: argparse.Namespace, therock_dir: Path) -> ArtifactSelection:
    (
        _bootstrapping_populator_class,
        backend_classes,
    ) = import_therock_artifact_tools(therock_dir)
    s3_backend_class, workflow_output_root_class = backend_classes

    if args.run_id:
        synthetic_run = WorkflowRun(
            run_id=args.run_id,
            run_number=0,
            html_url=f"https://github.com/{args.repo}/actions/runs/{args.run_id}",
            head_sha="",
            event="",
            status="completed",
            conclusion=None,
            created_at="",
            updated_at="",
            raw={},
        )
        backend = make_backend(
            s3_backend_class=s3_backend_class,
            workflow_output_root_class=workflow_output_root_class,
            run=None,
            run_id=args.run_id,
            platform=args.platform,
            repo=args.repo,
            release_type=args.release_type,
        )
        filenames = find_matching_artifacts(backend=backend)
        return ArtifactSelection(run=synthetic_run, filenames=filenames, backend=backend)

    runs = query_workflow_runs(
        repo=args.repo,
        workflow=args.workflow,
        branch=args.branch,
        event=args.event,
        conclusion=args.conclusion,
        max_runs=args.max_runs,
    )
    if not runs:
        fail(
            f"No completed {args.workflow} runs found for {args.repo}/{args.branch} "
            f"with event={args.event!r} and conclusion={args.conclusion!r}"
        )

    for run in runs:
        log(
            f"Checking nightly run {run.run_id} "
            f"(#{run.run_number}, updated {run.updated_at})"
        )
        backend = make_backend(
            s3_backend_class=s3_backend_class,
            workflow_output_root_class=workflow_output_root_class,
            run=run,
            run_id=run.run_id,
            platform=args.platform,
            repo=args.repo,
            release_type=args.release_type,
        )
        try:
            filenames = find_matching_artifacts(backend=backend)
        except RuntimeError as exc:
            log(f"  Skipping run {run.run_id}: {exc}")
            continue
        log(f"Selected run {run.run_id}: {run.html_url}")
        return ArtifactSelection(run=run, filenames=filenames, backend=backend)

    fail(
        "No usable generic bootstrap artifacts found in the latest "
        f"{len(runs)} matching workflow run(s)"
    )


def retry(operation_name: str, attempts: int, action: Callable[[], object]) -> object:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = 2 ** (attempt - 1)
            log(f"{operation_name} failed on attempt {attempt}/{attempts}: {exc}")
            log(f"Retrying in {delay}s...")
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def artifact_https_url(backend: object, filename: str) -> str:
    return backend.output_root.artifact(filename).https_url


def artifact_exists_https(backend: object, filename: str) -> bool:
    request = urllib.request.Request(
        artifact_https_url(backend, filename), method="HEAD"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return False
        raise
    except urllib.error.URLError:
        return False


def download_artifact_https(backend: object, filename: str, archive_path: Path) -> None:
    url = artifact_https_url(backend, filename)
    request = urllib.request.Request(url)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = archive_path.with_suffix(archive_path.suffix + ".partial")
    if partial_path.exists():
        partial_path.unlink()
    with urllib.request.urlopen(request, timeout=60) as response:
        with open(partial_path, "wb") as output_file:
            shutil.copyfileobj(response, output_file)
    partial_path.replace(archive_path)


def bootstrap_artifacts(
    *,
    args: argparse.Namespace,
    selection: ArtifactSelection,
    therock_dir: Path,
    build_dir: Path,
) -> None:
    bootstrapping_populator_class, _backend_classes = import_therock_artifact_tools(
        therock_dir
    )
    download_dir = args.download_cache_dir
    download_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    cleaned_paths: set[str] = set()
    cleaned_paths_lock = threading.Lock()

    for filename in selection.filenames:
        archive_path = download_dir / filename
        if archive_path.exists() and archive_path.stat().st_size > 0:
            log(f"Using cached {archive_path}")
        else:
            log(f"Downloading {filename}")

            def download_one() -> None:
                download_artifact_https(selection.backend, filename, archive_path)

            retry(f"download {filename}", 3, download_one)
        if not archive_path.exists() or archive_path.stat().st_size == 0:
            fail(f"Downloaded artifact is missing or empty: {archive_path}")

        log(f"Bootstrapping {filename}")
        populator = bootstrapping_populator_class(
            output_path=build_dir,
            verbose=args.verbose,
            cleaned_paths=cleaned_paths,
            cleaned_paths_lock=cleaned_paths_lock,
        )
        populator(archive_path)

    expected_markers = [*EXPECTED_SYSDEPS_MARKERS, *EXPECTED_COMPILER_MARKERS]
    missing_markers = [
        marker for marker in expected_markers if not (build_dir / marker).exists()
    ]
    if missing_markers:
        formatted = "\n".join(
            f"  - {build_dir / marker}" for marker in missing_markers
        )
        fail(
            "Bootstrap did not create expected prebuilt markers:\n"
            f"{formatted}"
        )

    log("Bootstrap prebuilt markers:")
    for marker in expected_markers:
        log(f"  {build_dir / marker}")


def run_command(command: Sequence[str], *, cwd: Path, dry_run: bool) -> None:
    log(f"++ [{cwd}]$ {shlex.join(command)}")
    if dry_run:
        return
    subprocess.run(command, cwd=cwd, check=True)


def first_existing(paths: Sequence[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def check_host_prerequisites(args: argparse.Namespace) -> None:
    if args.dry_run or not sys.platform.startswith("linux"):
        return

    include_dirs = [Path("/usr/include")]
    library_dirs = [
        Path("/usr/lib64"),
        Path("/usr/lib"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/lib64"),
        Path("/lib/x86_64-linux-gnu"),
    ]
    required_files = {
        "GL/gl.h": [include_dir / "GL" / "gl.h" for include_dir in include_dirs],
        "EGL/egl.h": [include_dir / "EGL" / "egl.h" for include_dir in include_dirs],
        "libOpenGL.so": [
            library_dir / "libOpenGL.so" for library_dir in library_dirs
        ],
        "libGLX.so": [library_dir / "libGLX.so" for library_dir in library_dirs],
    }
    missing = [
        label
        for label, candidates in required_files.items()
        if first_existing(candidates) is None
    ]
    if missing:
        fail(
            "Missing OpenGL development files required by CLR: "
            f"{', '.join(missing)}\n"
            "On Fedora-like hosts, install them with:\n"
            "  sudo dnf install -y libglvnd-devel mesa-libGL-devel mesa-libEGL-devel"
        )


def fetch_sources(args: argparse.Namespace, therock_dir: Path) -> None:
    if args.skip_fetch_sources:
        log("Skipping source fetch")
        return
    source_sets = list(DEFAULT_SOURCE_SETS)
    if args.include_hrx:
        source_sets.append("optional-hrx")
    command = [
        sys.executable,
        "build_tools/fetch_sources.py",
        "--source-sets",
        ",".join(source_sets),
        "--no-include-system-projects",
        "--no-include-compilers",
        "--no-include-debug-tools",
        "--no-include-rocm-libraries",
        "--no-include-rocm-systems",
        "--no-include-ml-frameworks",
        "--no-include-media-libs",
        "--no-include-math-libraries",
        "--jobs",
        str(args.source_jobs),
    ]
    if args.source_depth:
        command.extend(["--depth", str(args.source_depth)])
    run_command(command, cwd=therock_dir, dry_run=args.dry_run)


def configure_build(
    args: argparse.Namespace, workspace_root: Path, therock_dir: Path, build_dir: Path
) -> None:
    cmake = shutil.which("cmake")
    if cmake is None:
        fail("cmake was not found on PATH")

    amdgpu_families = args.amdgpu_families
    dist_amdgpu_families = args.dist_amdgpu_families or args.amdgpu_families
    amdgpu_targets = args.amdgpu_targets
    dist_amdgpu_targets = args.dist_amdgpu_targets
    if amdgpu_targets:
        amdgpu_families = ""
        if dist_amdgpu_targets is None:
            dist_amdgpu_targets = amdgpu_targets
        if args.dist_amdgpu_families:
            fail("--dist-amdgpu-families cannot be combined with --amdgpu-targets")
        dist_amdgpu_families = ""
    else:
        if dist_amdgpu_targets:
            dist_amdgpu_families = ""

    command = [
        cmake,
        "-B",
        os.fspath(build_dir),
        "-S",
        os.fspath(therock_dir),
        "-GNinja",
        f"-DTHEROCK_AMDGPU_FAMILIES={amdgpu_families}",
        f"-DTHEROCK_AMDGPU_TARGETS={amdgpu_targets or ''}",
        f"-DTHEROCK_DIST_AMDGPU_FAMILIES={dist_amdgpu_families}",
        f"-DTHEROCK_DIST_AMDGPU_TARGETS={dist_amdgpu_targets or ''}",
        f"-DTHEROCK_TEST_AMDGPU_TARGETS={args.test_amdgpu_targets or ''}",
        f"-DTHEROCK_PACKAGE_VERSION={args.package_version}",
        f"-DCMAKE_BUILD_TYPE={args.cmake_build_type}",
        "-DTHEROCK_ENABLE_ALL=OFF",
        "-DTHEROCK_ENABLE_CORE=ON",
        "-DTHEROCK_ENABLE_COMM_LIBS=OFF",
        "-DTHEROCK_ENABLE_DEBUG_TOOLS=OFF",
        "-DTHEROCK_ENABLE_DC_TOOLS=OFF",
        "-DTHEROCK_ENABLE_EMULATION=OFF",
        "-DTHEROCK_ENABLE_HOST_MATH=OFF",
        "-DTHEROCK_ENABLE_MATH_LIBS=OFF",
        "-DTHEROCK_ENABLE_MEDIA_LIBS=OFF",
        "-DTHEROCK_ENABLE_ML_LIBS=OFF",
        "-DTHEROCK_ENABLE_PROFILER=OFF",
        "-DTHEROCK_ENABLE_ROCPROFILER_COMPUTE=OFF",
        "-DTHEROCK_ENABLE_ROCPROFSYS=OFF",
        f"-DBUILD_TESTING={'ON' if args.enable_tests else 'OFF'}",
        f"-DTHEROCK_FLAG_INCLUDE_HRX={'ON' if args.include_hrx else 'OFF'}",
    ]
    if args.dist_bundle_name:
        command.append(
            f"-DTHEROCK_AMDGPU_DIST_BUNDLE_NAME={args.dist_bundle_name}"
        )
    if args.use_ccache and shutil.which("ccache") is not None:
        command.extend(
            [
                "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            ]
        )
    for cmake_arg in args.extra_cmake_arg:
        command.append(cmake_arg)

    run_command(command, cwd=workspace_root, dry_run=args.dry_run)
    cache_file = build_dir / "CMakeCache.txt"
    if not args.dry_run and (
        not cache_file.exists() or cache_file.stat().st_size == 0
    ):
        fail(f"CMake configure did not create a non-empty cache: {cache_file}")


def build_targets(args: argparse.Namespace, build_dir: Path) -> None:
    if args.no_build:
        log("Skipping build")
        return
    cmake = shutil.which("cmake")
    if cmake is None:
        fail("cmake was not found on PATH")
    command = [
        cmake,
        "--build",
        os.fspath(build_dir),
        "--target",
        *args.target,
        "--",
        "-k",
        "0",
    ]
    run_command(command, cwd=build_dir.parent, dry_run=args.dry_run)


def artifact_dirs_for_names(build_dir: Path, artifact_names: Sequence[str]) -> list[Path]:
    artifacts_dir = build_dir / "artifacts"
    if not artifacts_dir.is_dir():
        fail(f"Artifact directory does not exist: {artifacts_dir}")

    artifact_dirs: list[Path] = []
    for artifact_name in artifact_names:
        matches = [
            candidate
            for candidate in sorted(artifacts_dir.glob(f"{artifact_name}_*"))
            if candidate.is_dir() and (candidate / "artifact_manifest.txt").is_file()
        ]
        if not matches:
            fail(f"No artifact component directories found for {artifact_name}")
        artifact_dirs.extend(matches)
    return artifact_dirs


def assemble_minimal_dist(
    args: argparse.Namespace, therock_dir: Path, build_dir: Path
) -> None:
    if args.no_build:
        return

    fileset_tool = therock_dir / "build_tools" / "fileset_tool.py"
    if not fileset_tool.is_file():
        fail(f"TheRock fileset tool not found: {fileset_tool}")
    cmake = shutil.which("cmake")
    if cmake is None:
        fail("cmake was not found on PATH")

    dist_dir = build_dir / "dist" / "rocm"
    artifact_dirs = artifact_dirs_for_names(build_dir, MINIMAL_CORE_ARTIFACTS)
    run_command(
        [cmake, "-E", "rm", "-rf", os.fspath(dist_dir)],
        cwd=build_dir.parent,
        dry_run=args.dry_run,
    )
    command = [
        sys.executable,
        os.fspath(fileset_tool),
        "artifact-flatten",
        "-o",
        os.fspath(dist_dir),
        *[os.fspath(artifact_dir) for artifact_dir in artifact_dirs],
    ]
    log("Assembling minimal core dist from explicit artifact allowlist")
    run_command(command, cwd=build_dir.parent, dry_run=args.dry_run)

    if args.dry_run:
        return
    if not dist_dir.is_dir() or not any(dist_dir.iterdir()):
        fail(f"Dist assembly did not create a populated directory: {dist_dir}")

    forbidden_matches: list[Path] = []
    for pattern in FORBIDDEN_DIST_GLOBS:
        forbidden_matches.extend(sorted(dist_dir.glob(f"**/{pattern}")))
    if forbidden_matches:
        formatted = "\n".join(
            f"  - {path.relative_to(dist_dir)}" for path in forbidden_matches[:50]
        )
        fail(
            "Minimal core dist contains excluded math/profiler files:\n"
            f"{formatted}"
        )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap a minimal TheRock core build using prebuilt nightly LLVM artifacts."
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=workspace_root_from_cwd(),
        help="Workspace root containing sources/TheRock (default: auto-detect from cwd)",
    )
    parser.add_argument(
        "--therock-dir",
        type=Path,
        default=None,
        help="TheRock checkout path (default: WORKSPACE_ROOT/sources/TheRock)",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=DEFAULT_BUILD_DIR,
        help=f"Build directory (default: {DEFAULT_BUILD_DIR})",
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO, help=f"GitHub repo (default: {DEFAULT_REPO})"
    )
    parser.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help=f"Workflow filename for discovery (default: {DEFAULT_WORKFLOW})",
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"Git branch (default: {DEFAULT_BRANCH})",
    )
    parser.add_argument(
        "--event",
        default=DEFAULT_EVENT,
        help="GitHub Actions event filter for discovery; use empty string to disable",
    )
    parser.add_argument(
        "--conclusion",
        default="",
        help="Workflow conclusion filter for discovery; use empty string to disable",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=20,
        help="Maximum workflow runs to inspect during latest-nightly discovery",
    )
    parser.add_argument("--run-id", default="", help="Use a specific workflow run id")
    parser.add_argument(
        "--release-type",
        default=DEFAULT_RELEASE_TYPE,
        choices=["ci", "dev", "nightly", "prerelease"],
        help=f"Artifact bucket release type (default: {DEFAULT_RELEASE_TYPE})",
    )
    parser.add_argument(
        "--platform",
        default=DEFAULT_PLATFORM,
        choices=["linux", "windows"],
        help=f"Artifact platform name (default: {DEFAULT_PLATFORM})",
    )
    parser.add_argument(
        "--amdgpu-families",
        default=DEFAULT_AMDGPU_FAMILIES,
        help=f"THEROCK_AMDGPU_FAMILIES value (default: {DEFAULT_AMDGPU_FAMILIES})",
    )
    parser.add_argument(
        "--amdgpu-targets",
        default=None,
        help=(
            "THEROCK_AMDGPU_TARGETS exact target list. When set, "
            "THEROCK_AMDGPU_FAMILIES is passed as an empty value."
        ),
    )
    parser.add_argument(
        "--dist-amdgpu-families",
        default="",
        help="THEROCK_DIST_AMDGPU_FAMILIES value (default: same as --amdgpu-families)",
    )
    parser.add_argument(
        "--dist-amdgpu-targets",
        default=None,
        help=(
            "THEROCK_DIST_AMDGPU_TARGETS exact target list "
            "(default: same as --amdgpu-targets when exact targets are used)"
        ),
    )
    parser.add_argument(
        "--test-amdgpu-targets",
        default=None,
        help="THEROCK_TEST_AMDGPU_TARGETS exact target list",
    )
    parser.add_argument(
        "--dist-bundle-name",
        default="",
        help=(
            "THEROCK_AMDGPU_DIST_BUNDLE_NAME. Required by TheRock when more "
            "than one target/family is selected."
        ),
    )
    parser.add_argument(
        "--package-version",
        default="ADHOCBUILD",
        help="THEROCK_PACKAGE_VERSION value",
    )
    parser.add_argument(
        "--cmake-build-type",
        default="RelWithDebInfo",
        help="CMAKE_BUILD_TYPE value",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help=(
            "CMake build target; may be repeated "
            "(default: explicit minimal core artifact targets)"
        ),
    )
    parser.add_argument(
        "--extra-cmake-arg",
        action="append",
        default=[],
        help="Additional raw CMake argument; may be repeated",
    )
    parser.add_argument(
        "--download-cache-dir",
        type=Path,
        default=None,
        help=(
            "Directory for downloaded artifact archives "
            f"(default: {DEFAULT_DOWNLOAD_CACHE_DIR})"
        ),
    )
    parser.add_argument(
        "--clean-build-dir",
        action="store_true",
        help="Remove the build directory before bootstrapping",
    )
    parser.add_argument(
        "--source-jobs",
        type=int,
        default=12,
        help="fetch_sources.py --jobs value",
    )
    parser.add_argument(
        "--source-depth",
        type=int,
        default=1,
        help="fetch_sources.py --depth value; use 0 to omit --depth",
    )
    parser.add_argument(
        "--include-hrx",
        action="store_true",
        help="Fetch optional HRX source and enable THEROCK_FLAG_INCLUDE_HRX",
    )
    parser.add_argument(
        "--skip-fetch-sources",
        action="store_true",
        help="Do not run build_tools/fetch_sources.py",
    )
    parser.add_argument("--no-build", action="store_true", help="Stop after configure")
    parser.add_argument(
        "--assemble-dist-only",
        action="store_true",
        help="Only reassemble build/dist/rocm from existing minimal core artifacts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected artifacts and commands without changing the build",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose artifact extraction")
    parser.add_argument(
        "--use-ccache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use ccache compiler launchers when ccache is on PATH",
    )
    parser.add_argument(
        "--enable-tests", action="store_true", help="Configure with BUILD_TESTING=ON"
    )
    args = parser.parse_args(argv)
    if not args.target:
        args.target = list(DEFAULT_BUILD_TARGETS)
    return args


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    workspace_root = args.workspace_root.resolve()
    therock_dir = (
        args.therock_dir or (workspace_root / "sources" / "TheRock")
    ).resolve()
    build_dir = args.build_dir
    if not build_dir.is_absolute():
        build_dir = (workspace_root / build_dir).resolve()
    if args.download_cache_dir is None:
        args.download_cache_dir = (
            workspace_root / DEFAULT_DOWNLOAD_CACHE_DIR
        ).resolve()
    elif not args.download_cache_dir.is_absolute():
        args.download_cache_dir = (workspace_root / args.download_cache_dir).resolve()

    if not therock_dir.is_dir():
        fail(f"TheRock checkout not found: {therock_dir}")
    if not (therock_dir / "BUILD_TOPOLOGY.toml").is_file():
        fail(f"Not a TheRock checkout: {therock_dir}")

    log(f"Workspace: {workspace_root}")
    log(f"TheRock:   {therock_dir}")
    log(f"Build:     {build_dir}")
    log(f"Cache:     {args.download_cache_dir}")

    if args.assemble_dist_only:
        assemble_minimal_dist(args, therock_dir, build_dir)
        log("Minimal core dist assembly complete")
        return 0

    check_host_prerequisites(args)

    if args.clean_build_dir:
        if args.download_cache_dir.is_relative_to(build_dir):
            fail(
                "--clean-build-dir cannot be used when --download-cache-dir is "
                "inside the build directory"
            )
        run_command(
            ["cmake", "-E", "rm", "-rf", os.fspath(build_dir)],
            cwd=workspace_root,
            dry_run=args.dry_run,
        )

    selection = select_artifacts(args, therock_dir)
    log("Artifacts selected for bootstrap:")
    for filename in selection.filenames:
        log(f"  {filename}")

    if args.dry_run:
        fetch_sources(args, therock_dir)
        configure_build(args, workspace_root, therock_dir, build_dir)
        build_targets(args, build_dir)
        return 0

    bootstrap_artifacts(
        args=args,
        selection=selection,
        therock_dir=therock_dir,
        build_dir=build_dir,
    )
    fetch_sources(args, therock_dir)
    configure_build(args, workspace_root, therock_dir, build_dir)
    build_targets(args, build_dir)
    assemble_minimal_dist(args, therock_dir, build_dir)
    log("Minimal core build complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

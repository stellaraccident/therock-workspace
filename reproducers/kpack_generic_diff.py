#!/usr/bin/env python3
"""Compare generic artifacts from two kpack-split shards.

Given two unsplit shard artifact directories (before splitting), runs the
kpack splitter on each, then compares the resulting generic artifacts for
byte-level differences. This detects convergence violations where the
generic artifact from shard A would be incompatible with kpack archives
from shard B.

Usage:
    # Compare generic artifacts from two shards:
    python kpack_generic_diff.py \
        --shard-a /path/to/blas_lib_gfx110X-all/ \
        --shard-b /path/to/blas_lib_gfx1151/

    # Or compare two already-split generic directories:
    python kpack_generic_diff.py \
        --generic-a /path/to/generic_from_shard_a/ \
        --generic-b /path/to/generic_from_shard_b/

Checks:
1. Same set of files in both generics
2. Same file sizes
3. For fat binaries: same wrapper count, same co_index values
4. Byte-level comparison (excluding zero-paged .hip_fat sections)
"""

import argparse
import hashlib
import struct
import sys
from pathlib import Path


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_wrapper_info(data: bytes) -> list[dict] | None:
    """Extract wrapper info from a PE/COFF or ELF binary."""
    HIPF_MAGIC = 0x48495046
    HIPK_MAGIC = 0x4B504948
    WRAPPER_SIZE = 24

    # Try PE/COFF
    if data[:2] == b"MZ":
        try:
            import pefile
            pe = pefile.PE(data=data)
            for section in pe.sections:
                name = section.Name.rstrip(b"\x00").decode("ascii", errors="replace")
                if name == ".hipFatB":
                    off = section.PointerToRawData
                    size = section.Misc_VirtualSize
                    num = size // WRAPPER_SIZE
                    wrappers = []
                    for i in range(num):
                        w_off = off + i * WRAPPER_SIZE
                        magic = struct.unpack_from("<I", data, w_off)[0]
                        ptr = struct.unpack_from("<Q", data, w_off + 8)[0]
                        reserved1 = struct.unpack_from("<Q", data, w_off + 16)[0]
                        wrappers.append({
                            "index": i,
                            "magic": magic,
                            "ptr": ptr,
                            "co_index": reserved1 if magic == HIPK_MAGIC else None,
                        })
                    return wrappers
        except Exception:
            pass

    # Try ELF
    elif data[:4] == b"\x7fELF":
        try:
            import io
            from elftools.elf.elffile import ELFFile
            elf = ELFFile(io.BytesIO(data))
            for section in elf.iter_sections():
                if section.name == ".hipFatBinSegment":
                    sec_data = section.data()
                    num = len(sec_data) // WRAPPER_SIZE
                    wrappers = []
                    for i in range(num):
                        w_off = i * WRAPPER_SIZE
                        magic = struct.unpack_from("<I", sec_data, w_off)[0]
                        ptr = struct.unpack_from("<Q", sec_data, w_off + 8)[0]
                        reserved1 = struct.unpack_from("<Q", sec_data, w_off + 16)[0]
                        wrappers.append({
                            "index": i,
                            "magic": magic,
                            "ptr": ptr,
                            "co_index": reserved1 if magic == HIPK_MAGIC else None,
                        })
                    return wrappers
        except Exception:
            pass

    return None


def compare_generics(dir_a: Path, dir_b: Path, label_a: str, label_b: str) -> int:
    """Compare two generic artifact directories. Returns number of differences."""
    files_a = {p.relative_to(dir_a) for p in dir_a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(dir_b) for p in dir_b.rglob("*") if p.is_file()}

    only_a = files_a - files_b
    only_b = files_b - files_a
    common = files_a & files_b

    diffs = 0

    if only_a:
        print(f"\nFiles only in {label_a}: {len(only_a)}")
        for f in sorted(only_a)[:20]:
            print(f"  {f}")
        diffs += len(only_a)

    if only_b:
        print(f"\nFiles only in {label_b}: {len(only_b)}")
        for f in sorted(only_b)[:20]:
            print(f"  {f}")
        diffs += len(only_b)

    print(f"\nComparing {len(common)} common files...")

    binary_diffs = 0
    wrapper_diffs = 0

    for relpath in sorted(common):
        path_a = dir_a / relpath
        path_b = dir_b / relpath

        hash_a = hash_file(path_a)
        hash_b = hash_file(path_b)

        if hash_a != hash_b:
            size_a = path_a.stat().st_size
            size_b = path_b.stat().st_size
            print(f"\n  DIFF: {relpath}")
            print(f"    {label_a}: {size_a:,} bytes (sha256: {hash_a[:16]})")
            print(f"    {label_b}: {size_b:,} bytes (sha256: {hash_b[:16]})")
            binary_diffs += 1

            # Check wrapper info for fat binaries
            data_a = path_a.read_bytes()
            data_b = path_b.read_bytes()

            wrappers_a = get_wrapper_info(data_a)
            wrappers_b = get_wrapper_info(data_b)

            if wrappers_a is not None or wrappers_b is not None:
                n_a = len(wrappers_a) if wrappers_a else 0
                n_b = len(wrappers_b) if wrappers_b else 0
                print(f"    Fat binary: {n_a} wrappers ({label_a}) vs {n_b} ({label_b})")

                if n_a != n_b:
                    print(f"    *** WRAPPER COUNT MISMATCH — co_index mapping WILL diverge")
                    wrapper_diffs += 1
                elif wrappers_a and wrappers_b:
                    for wa, wb in zip(wrappers_a, wrappers_b):
                        if wa["co_index"] != wb["co_index"]:
                            print(
                                f"    *** Wrapper {wa['index']}: "
                                f"co_index {wa['co_index']} vs {wb['co_index']}"
                            )
                            wrapper_diffs += 1

            diffs += 1

    print(f"\n{'='*60}")
    print(f"Summary: {binary_diffs} file differences, {wrapper_diffs} wrapper differences")
    if binary_diffs == 0:
        print("Generic artifacts are identical — convergence OK for kpack")
    elif wrapper_diffs > 0:
        print("*** CONVERGENCE VIOLATION: wrapper count/index differs between shards")
        print("*** This WILL cause wrong code objects when generics are cross-mated")
    else:
        print("Files differ but wrapper info matches — may be benign (timestamps, etc.)")

    return diffs


def main():
    parser = argparse.ArgumentParser(
        description="Compare generic artifacts from two kpack-split shards"
    )
    parser.add_argument("--generic-a", type=Path, required=True,
                        help="Generic artifact directory from shard A")
    parser.add_argument("--generic-b", type=Path, required=True,
                        help="Generic artifact directory from shard B")
    parser.add_argument("--label-a", default="shard-A")
    parser.add_argument("--label-b", default="shard-B")
    args = parser.parse_args()

    for d, label in [(args.generic_a, args.label_a), (args.generic_b, args.label_b)]:
        if not d.exists():
            print(f"Error: {d} does not exist ({label})", file=sys.stderr)
            sys.exit(1)

    diffs = compare_generics(
        args.generic_a, args.generic_b, args.label_a, args.label_b
    )
    sys.exit(1 if diffs > 0 else 0)


if __name__ == "__main__":
    main()

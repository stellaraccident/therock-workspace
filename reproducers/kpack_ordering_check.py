#!/usr/bin/env python3
"""Diagnostic tool: verify wrapper-to-bundle ordering in fat binaries.

For kpack correctness, wrapper i in .hipFatB/.hipFatBinSegment must
correspond to bundle i in .hip_fat/.hip_fatbin (physical byte order).

This tool reads a fat binary (ELF or COFF) and checks whether the
wrappers' original fatCubin pointers map to the expected bundles.
"""

import argparse
import hashlib
import struct
import sys
from pathlib import Path

# Try ELF support
try:
    from elftools.elf.elffile import ELFFile

    HAS_ELF = True
except ImportError:
    HAS_ELF = False

# Try PE/COFF support
try:
    import pefile

    HAS_PE = True
except ImportError:
    HAS_PE = False


HIPF_MAGIC = 0x48495046  # "HIPF"
HIPK_MAGIC = 0x4B504948  # "HIPK"
WRAPPER_SIZE = 24
CCOB_MAGIC = b"CCOB"
UNCOMPRESSED_BUNDLE_MAGIC = b"__CLANG_OFFLOAD_BUNDLE__"


def find_bundle_offsets(fatbin_data: bytes) -> list[int]:
    """Find byte offsets of each bundle in fatbin data."""
    offsets = []
    pos = 0
    while pos < len(fatbin_data):
        ccob_pos = fatbin_data.find(CCOB_MAGIC, pos)
        uncomp_pos = fatbin_data.find(UNCOMPRESSED_BUNDLE_MAGIC, pos)

        candidates = [c for c in [ccob_pos, uncomp_pos] if c != -1]
        if not candidates:
            break

        next_pos = min(candidates)
        offsets.append(next_pos)

        # Advance past this bundle
        if fatbin_data[next_pos : next_pos + 4] == CCOB_MAGIC:
            # CCOB header has total_size at offset 8
            if next_pos + 12 <= len(fatbin_data):
                total_size = struct.unpack_from("<Q", fatbin_data, next_pos + 4)[0]
                pos = next_pos + total_size
            else:
                pos = next_pos + 4
        else:
            # Uncompressed bundle - find next magic
            pos = next_pos + 24
            next_magic = -1
            c1 = fatbin_data.find(CCOB_MAGIC, pos)
            c2 = fatbin_data.find(UNCOMPRESSED_BUNDLE_MAGIC, pos)
            candidates2 = [c for c in [c1, c2] if c != -1]
            if candidates2:
                pos = min(candidates2)
            else:
                break

    return offsets


def sha256_short(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def analyze_elf(path: Path) -> None:
    with open(path, "rb") as f:
        elf = ELFFile(f)

        # Find sections
        wrapper_section = None
        fatbin_section = None
        kpack_ref_section = None
        for section in elf.iter_sections():
            if section.name == ".hipFatBinSegment":
                wrapper_section = section
            elif section.name == ".hip_fatbin":
                fatbin_section = section
            elif section.name == ".rocm_kpack_ref":
                kpack_ref_section = section

        if wrapper_section is None:
            print("  No .hipFatBinSegment section — not a fat binary")
            return
        if fatbin_section is None:
            print("  No .hip_fatbin section — wrappers exist but no data")
            return

        is_kpacked = kpack_ref_section is not None
        print(f"  Format: ELF {'(kpack-transformed)' if is_kpacked else '(original)'}")

        wrapper_data = wrapper_section.data()
        fatbin_data = fatbin_section.data()
        fatbin_vaddr = fatbin_section["sh_addr"]

        num_wrappers = len(wrapper_data) // WRAPPER_SIZE
        print(f"  Wrappers: {num_wrappers}")

        # Find bundle offsets in fatbin data
        bundle_offsets = find_bundle_offsets(fatbin_data)
        print(f"  Bundles in .hip_fatbin: {len(bundle_offsets)}")

        if num_wrappers != len(bundle_offsets):
            print(
                f"  *** MISMATCH: {num_wrappers} wrappers vs "
                f"{len(bundle_offsets)} bundles"
            )

        # Build bundle_offset → bundle_index map
        offset_to_idx = {off: i for i, off in enumerate(bundle_offsets)}

        # Read each wrapper's pointer and determine which bundle it targets
        print()
        print(f"  {'Wrapper':>8} {'Magic':>6} {'co_idx':>7} {'Ptr Offset':>12} "
              f"{'Bundle Idx':>11} {'Match':>6}")
        print(f"  {'--------':>8} {'------':>6} {'-------':>7} {'----------':>12} "
              f"{'----------':>11} {'-----':>6}")

        mismatches = 0
        for i in range(num_wrappers):
            off = i * WRAPPER_SIZE
            magic = struct.unpack_from("<I", wrapper_data, off)[0]
            # version at +4
            ptr_val = struct.unpack_from("<Q", wrapper_data, off + 8)[0]
            reserved1 = struct.unpack_from("<Q", wrapper_data, off + 16)[0]

            magic_str = "HIPK" if magic == HIPK_MAGIC else "HIPF" if magic == HIPF_MAGIC else f"0x{magic:08x}"
            co_idx = reserved1 if magic == HIPK_MAGIC else i

            if is_kpacked:
                # Pointer redirected to .kpackrf — can't determine original target
                print(
                    f"  {i:>8} {magic_str:>6} {co_idx:>7} {'(redirected)':>12} "
                    f"{'n/a':>11} {'n/a':>6}"
                )
            else:
                # Original pointer — compute offset into .hip_fatbin
                ptr_offset = ptr_val - fatbin_vaddr
                bundle_idx = offset_to_idx.get(ptr_offset, -1)
                match = "OK" if bundle_idx == i else "FAIL"
                if bundle_idx != i:
                    mismatches += 1
                print(
                    f"  {i:>8} {magic_str:>6} {co_idx:>7} {ptr_offset:>12} "
                    f"{bundle_idx:>11} {match:>6}"
                )

        if not is_kpacked:
            if mismatches > 0:
                print(f"\n  *** {mismatches} ORDERING MISMATCHES DETECTED")
                print("  *** Kpack co_index assignment will be WRONG for these wrappers")
            else:
                print(f"\n  All {num_wrappers} wrappers map to expected bundles — ordering OK")


def analyze_pe(path: Path) -> None:
    pe = pefile.PE(str(path))
    data = path.read_bytes()

    # Find sections
    wrapper_section = None
    fatbin_section = None
    kpack_ref_section = None

    for section in pe.sections:
        name = section.Name.rstrip(b"\x00").decode("ascii", errors="replace")
        if name == ".hipFatB":
            wrapper_section = section
        elif name == ".hip_fat":
            fatbin_section = section
        elif name == ".kpackrf":
            kpack_ref_section = section

    if wrapper_section is None:
        print("  No .hipFatB section — not a fat binary")
        return
    if fatbin_section is None:
        print("  No .hip_fat section — wrappers exist but no data")
        return

    is_kpacked = kpack_ref_section is not None
    print(f"  Format: PE/COFF {'(kpack-transformed)' if is_kpacked else '(original)'}")

    image_base = pe.OPTIONAL_HEADER.ImageBase

    # Read section data
    w_raw_offset = wrapper_section.PointerToRawData
    w_raw_size = wrapper_section.SizeOfRawData
    w_virtual_size = wrapper_section.Misc_VirtualSize
    wrapper_data = data[w_raw_offset : w_raw_offset + w_virtual_size]

    f_raw_offset = fatbin_section.PointerToRawData
    f_raw_size = fatbin_section.SizeOfRawData
    f_virtual_size = fatbin_section.Misc_VirtualSize
    fatbin_rva = fatbin_section.VirtualAddress
    fatbin_data = data[f_raw_offset : f_raw_offset + min(f_raw_size, f_virtual_size)]

    num_wrappers = w_virtual_size // WRAPPER_SIZE
    print(f"  Wrappers: {num_wrappers}")
    print(f"  Image base: 0x{image_base:x}")
    print(f"  .hip_fat RVA: 0x{fatbin_rva:x}, size: {f_virtual_size}")

    # Find bundle offsets
    bundle_offsets = find_bundle_offsets(fatbin_data)
    print(f"  Bundles in .hip_fat: {len(bundle_offsets)}")

    if num_wrappers != len(bundle_offsets):
        print(
            f"  *** MISMATCH: {num_wrappers} wrappers vs "
            f"{len(bundle_offsets)} bundles"
        )

    # Map fatbin-data-relative offsets to RVA-relative offsets
    offset_to_idx = {off: i for i, off in enumerate(bundle_offsets)}

    print()
    print(f"  {'Wrapper':>8} {'Magic':>6} {'co_idx':>7} {'Ptr->FatOff':>12} "
          f"{'Bundle Idx':>11} {'Match':>6}")
    print(f"  {'--------':>8} {'------':>6} {'-------':>7} {'-----------':>12} "
          f"{'----------':>11} {'-----':>6}")

    mismatches = 0
    for i in range(num_wrappers):
        off = w_raw_offset + i * WRAPPER_SIZE
        magic = struct.unpack_from("<I", data, off)[0]
        ptr_val = struct.unpack_from("<Q", data, off + 8)[0]
        reserved1 = struct.unpack_from("<Q", data, off + 16)[0]

        magic_str = "HIPK" if magic == HIPK_MAGIC else "HIPF" if magic == HIPF_MAGIC else f"0x{magic:08x}"
        co_idx = reserved1 if magic == HIPK_MAGIC else i

        if is_kpacked:
            print(
                f"  {i:>8} {magic_str:>6} {co_idx:>7} {'(redirected)':>12} "
                f"{'n/a':>11} {'n/a':>6}"
            )
        else:
            # Convert VA to offset within .hip_fat data
            ptr_rva = ptr_val - image_base
            fat_data_offset = ptr_rva - fatbin_rva
            bundle_idx = offset_to_idx.get(fat_data_offset, -1)
            match = "OK" if bundle_idx == i else "FAIL"
            if bundle_idx != i:
                mismatches += 1
            print(
                f"  {i:>8} {magic_str:>6} {co_idx:>7} {fat_data_offset:>12} "
                f"{bundle_idx:>11} {match:>6}"
            )

    if not is_kpacked:
        if mismatches > 0:
            print(f"\n  *** {mismatches} ORDERING MISMATCHES DETECTED")
            print("  *** Kpack co_index assignment will be WRONG for these wrappers")
        else:
            print(f"\n  All {num_wrappers} wrappers map to expected bundles — ordering OK")


def main():
    parser = argparse.ArgumentParser(
        description="Check wrapper-to-bundle ordering in fat binaries for kpack correctness"
    )
    parser.add_argument("binary", type=Path, help="Path to ELF or PE fat binary")
    args = parser.parse_args()

    if not args.binary.exists():
        print(f"Error: {args.binary} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing: {args.binary}")

    # Detect format by reading magic bytes
    with open(args.binary, "rb") as f:
        magic = f.read(4)

    if magic[:2] == b"MZ":
        if not HAS_PE:
            print("Error: pefile not installed (pip install pefile)", file=sys.stderr)
            sys.exit(1)
        analyze_pe(args.binary)
    elif magic[:4] == b"\x7fELF":
        if not HAS_ELF:
            print("Error: pyelftools not installed (pip install pyelftools)", file=sys.stderr)
            sys.exit(1)
        analyze_elf(args.binary)
    else:
        print(f"Error: unrecognized format (magic: {magic!r})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

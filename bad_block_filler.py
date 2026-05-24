#!/usr/bin/env python3
"""
bad_block_filler.py — Detect and neutralise slow/bad storage regions on any
macOS volume (SSD, HDD, USB drive) formatted as APFS, HFS+, ExFAT, or NTFS.

Copyright (C) 2026 artur-bortsov

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details.

You should have received a copy of the GNU General Public License along with
this program.  If not, see <https://www.gnu.org/licenses/>.

Algorithm
---------
1.  Pre-allocate a scan file that claims all free space (F_PREALLOCATE).
2.  Write-scan forward in 1 MiB blocks, flushing each block all the way to
    physical media with F_FULLFSYNC.
3.  If a block takes longer than --slow-threshold seconds:
        ▸ record the start of a bad region
        ▸ skip --skip-gib GiB forward
4.  When the scanner finds a fast block after a slow region:
        ▸ binary-search backward in 1 MiB steps to find the exact boundary
5.  Display an ASCII bad-block map.
6.  For each bad region, COW-clone the scan file (clonefile syscall) and
    punch holes (F_PUNCHHOLE) to keep only the bad region’s physical blocks.
    The resulting sparse filler physically occupies only the bad blocks.
7.  Delete the scan file (good blocks freed; bad blocks held by filler clones).
8.  Write a .badblocks/map.json describing every bad region.

Filler files live at  VOLUME_ROOT/.badblocks/filler000001, filler000002 …

Runs without sudo on volumes the current user can write to.
Requires: macOS 10.12+, Python 3.9+.

Usage
-----
    python3 bad_block_filler.py /Volumes/MyDrive
    python3 bad_block_filler.py /Volumes/MyDrive --force-remapping
    python3 bad_block_filler.py /Volumes/MyDrive --slow-threshold 0.5
    python3 bad_block_filler.py /Volumes/MyDrive --no-fillers
    python3 bad_block_filler.py /Volumes/MyDrive --api-check
"""

from __future__ import annotations

__version__ = "1.0.0"

import argparse
import ctypes
import errno
import fcntl as _py_fcntl   # Python's fcntl — correctly marshals struct buffers
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# ─────────────────────────── filesystem detection ──────────────────────────

# ─────────────────────────── instance lock ─────────────────────────────────

LOCK_FILE_NAME = "._bbf_lock"


def acquire_lock(volume: Path) -> int:
    """
    Acquire an exclusive advisory lock for this volume using flock(2).

    Returns the open file descriptor for the lock file.  The caller is
    responsible for keeping it open (the lock is released when the fd is
    closed or the process exits).

    Raises SystemExit if another instance already holds the lock.
    """
    lock_path = volume / LOCK_FILE_NAME
    lock_fd   = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        _py_fcntl.flock(lock_fd, _py_fcntl.LOCK_EX | _py_fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print(f"\u2717  Another bad_block_filler instance is already running on {volume}.")
        print(f"   If no other instance is running, delete the stale lock file:")
        print(f"   rm '{lock_path}'")
        sys.exit(1)
    return lock_fd


# ─────────────────────────── resumption helpers ───────────────────────────

def load_existing_state(badblocks_dir: Path) -> Tuple[List["BadRegion"], int]:
    """
    Read any previously found bad regions from .badblocks/map.json and
    determine the next available filler index by scanning existing filler files.

    Returns (existing_regions, next_filler_index).
    On any error the function returns empty state (safe to ignore).
    """
    existing: List[BadRegion] = []
    next_idx = 1

    # Load previous bad regions
    map_path = badblocks_dir / "map.json"
    if map_path.exists():
        try:
            data = json.loads(map_path.read_text())
            for r in data.get("bad_regions", []):
                existing.append(BadRegion(
                    start      = r["start_bytes"],
                    end        = r["end_bytes"],
                    phys_start = r.get("phys_start", -1),
                    filler     = r.get("filler", ""),
                ))
        except Exception:
            pass   # corrupt map.json — silently ignore, start fresh

    # Determine next filler index from files on disk (more reliable than map.json)
    if badblocks_dir.exists():
        for f in badblocks_dir.glob("filler??????"):
            try:
                next_idx = max(next_idx, int(f.name[6:]) + 1)
            except ValueError:
                pass

    return existing, next_idx


# ─────────────────────────── filesystem detection ──────────────────────────

def detect_drive_type(volume: Path) -> Tuple[str, str, int]:
    """
    Detect whether the drive backing `volume` is an SSD or HDD.
    Returns (drive_type, description, recommended_block_mib) where:
      drive_type            'ssd' | 'hdd' | 'unknown'
      description           human-readable string for the startup header
      recommended_block_mib optimal --block-mib value for this drive type

    Uses `diskutil info -plist` which provides a SolidState boolean.
    Falls back to protocol hints if SolidState is unavailable (some USB bridges).
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", str(volume)],
            capture_output=True, check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return "unknown", "type unknown", 1

        info = plistlib.loads(result.stdout)
        protocol = (
            info.get("BusProtocol") or info.get("Protocol", "")
        ).strip()

        solid = info.get("SolidState")   # True / False / None

        if solid is True:
            desc = f"SSD ({protocol})" if protocol else "SSD"
            return "ssd", desc, 1

        if solid is False:
            desc = f"HDD ({protocol})" if protocol else "HDD"
            return "hdd", desc, 16   # larger block amortises seek overhead

        # SolidState not reported (common on USB bridge enclosures).
        # Fall back to protocol heuristics.
        proto_low = protocol.lower()
        if any(kw in proto_low for kw in ("pci", "nvme", "thunderbolt")):
            return "ssd", f"likely SSD ({protocol})", 1
        if "usb" in proto_low:
            # USB could be SSD or HDD; stay conservative
            return "unknown", f"USB drive — SSD/HDD unknown", 1

        return "unknown", f"type unknown ({protocol})", 1

    except Exception:
        return "unknown", "type unknown", 1


def detect_fs_type(volume: Path) -> str:
    """
    Return the filesystem type string for the given mount point,
    e.g. 'apfs', 'hfs', 'exfat', 'msdos', 'ntfs', 'unknown'.
    Parsed from `mount` output on macOS; gracefully degrades on other OSes.
    """
    try:
        out = subprocess.check_output(["mount"], text=True, stderr=subprocess.DEVNULL)
        vol = str(volume).rstrip("/")
        for line in out.splitlines():
            # Format: /dev/diskXsY on /Volumes/Name (fstype, flags, …)
            if f" on {vol} (" in line or f" on {vol}/" in line:
                m = re.search(r"\(([^,)]+)", line)
                if m:
                    return m.group(1).strip().lower()
    except Exception:
        pass
    return "unknown"


def filler_capability(fs_type: str) -> Tuple[bool, bool, str]:
    """
    Returns (supports_clone, supports_punch_hole, description) for a filesystem.
    - supports_clone:      clonefile() works  → per-region COW filler (best)
    - supports_punch_hole: F_PUNCHHOLE works  → single sparse filler (good)
    - neither              → detection only; fillers not supported
    """
    if fs_type == "apfs":
        return True, True, "APFS — full filler support (COW clone + sparse holes)"
    if fs_type in ("hfs", "hfs+"):
        return False, True, "HFS+ — single sparse filler (no COW clone)"
    if fs_type in ("exfat", "msdos", "fat32", "vfat"):
        return False, False, f"{fs_type.upper()} — no sparse file support; detection only"
    if "ntfs" in fs_type:
        return False, False, f"NTFS — sparse support varies; detection only (safe fallback)"
    # Unknown: try and fall back gracefully
    return False, True, f"'{fs_type}' — unknown filesystem; will attempt sparse filler"


# ─────────────────────────── macOS fcntl constants ─────────────────────────

F_PREALLOCATE   = 42   # reserve physical NAND blocks
F_PUNCHHOLE     = 99   # create a sparse (hole) region in a file
F_LOG2PHYS_EXT  = 65   # map file offset → physical device offset
F_FULLFSYNC     = 51   # flush to physical media, bypassing drive cache
F_NOCACHE       = 48   # disable page-cache for this fd

F_ALLOCATECONTIG  = 0x00000002  # request contiguous allocation
F_ALLOCATEALL     = 0x00000004  # allocate all or fail
F_PEOFPOSMODE     = 3           # allocate relative to physical EOF

# ─────────────────────────── ctypes structures ─────────────────────────────

class Fstore(ctypes.Structure):
    """struct fstore — used with F_PREALLOCATE."""
    _fields_ = [
        ("fst_flags",       ctypes.c_uint32),
        ("fst_posmode",     ctypes.c_int32),
        ("fst_offset",      ctypes.c_int64),
        ("fst_length",      ctypes.c_int64),
        ("fst_bytesalloc",  ctypes.c_int64),
    ]


class Fpunchhole(ctypes.Structure):
    """struct fpunchhole — used with F_PUNCHHOLE."""
    _fields_ = [
        ("fp_flags",   ctypes.c_uint32),
        ("reserved",   ctypes.c_uint32),   # explicit padding from Apple header
        ("fp_offset",  ctypes.c_int64),
        ("fp_length",  ctypes.c_int64),
    ]


class Log2Phys(ctypes.Structure):
    """struct log2phys — used with F_LOG2PHYS_EXT."""
    # Note: ctypes inserts 4 bytes of natural-alignment padding after l2p_flags
    # to align the first off_t to an 8-byte boundary (matches the C struct).
    _fields_ = [
        ("l2p_flags",       ctypes.c_uint32),
        ("l2p_contigbytes", ctypes.c_int64),   # IN: bytes to query; OUT: contiguous bytes
        ("l2p_devoffset",   ctypes.c_int64),   # IN: file offset;    OUT: physical offset
    ]


# ─────────────────────────── libc bindings ─────────────────────────────────

_libc = ctypes.CDLL(None, use_errno=True)

# clonefile(2) — APFS copy-on-write clone
_libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
_libc.clonefile.restype  = ctypes.c_int

# NOTE: F_PREALLOCATE, F_PUNCHHOLE, F_LOG2PHYS_EXT all pass struct pointers
# through a variadic fcntl().  ctypes.byref() fails with EFAULT on macOS
# because the ABI doesn't convey pointer intent through variadic args.
# Python's own fcntl.fcntl(fd, cmd, bytes) correctly passes a mutable buffer
# and returns the kernel-modified result bytes — use that for all struct calls.
#
# F_FULLFSYNC takes a plain int (not a struct) so we call libc directly.
_c_fcntl         = _libc.fcntl
_c_fcntl.restype = ctypes.c_int

# ─────────────────────────── tunables ──────────────────────────────────────

GiB             = 1024 ** 3
MiB             = 1024 ** 2
KiB             = 1024

# Each individual write measurement covers WRITE_BLOCK bytes (≈ NAND erase block).
# When a WRITE_BLOCK write is slow, the scanner jumps SKIP_SIZE bytes forward.
# The binary-search backward also uses WRITE_BLOCK steps — no separate granularity.
WRITE_BLOCK     = 1 * MiB        # bytes per timed write (precision of detection)
SKIP_SIZE       = 1 * GiB        # bytes to skip when a slow block is found
HEADROOM        = 512 * MiB      # keep free to avoid cramping macOS
WRITE_CHUNK     = 1 * MiB        # pwrite() chunk size (= WRITE_BLOCK here)
_ZEROS          = bytearray(WRITE_CHUNK)   # reusable zero-filled buffer

# Maximum seconds to wait for a single write+sync before treating the block as
# dead.  On a failing drive F_FULLFSYNC can block indefinitely; SIGALRM fires
# after this interval, interrupts the syscall with EINTR, and lets the scan
# skip forward instead of hanging.  Updated from --write-timeout in main().
WRITE_TIMEOUT   = 60.0

PRINT_INTERVAL  = 1.0            # max seconds between progress-line updates

BADBLOCKS_DIR   = ".badblocks"
SCAN_TMP_NAME   = "._bbf_scan_temp"

# ─────────────────────────── data model ────────────────────────────────────

@dataclass
class BadRegion:
    """A contiguous range of slow/bad file offsets and its corresponding filler."""
    start:      int          # inclusive (bytes)
    end:        int          # exclusive (bytes)
    phys_start: int = -1     # physical disk offset (if available)
    filler:     str = ""     # assigned filler filename

    @property
    def size(self) -> int:
        return self.end - self.start

    def summary(self, file_size: int) -> str:
        pct_start = self.start / file_size * 100
        pct_end   = self.end   / file_size * 100
        phys = f"  phys ≈ {self.phys_start // GiB:.1f} GiB" if self.phys_start >= 0 else ""
        filler = f"  →  {self.filler}" if self.filler else ""
        return (
            f"  @{self.start // GiB:6.2f} GiB – {self.end // GiB:.2f} GiB"
            f"  ({self.size // MiB} MiB, {pct_start:.1f}%–{pct_end:.1f}%)"
            f"{phys}{filler}"
        )

# ─────────────────────────── low-level helpers ─────────────────────────────

def preallocate(fd: int, size: int) -> int:
    """
    Reserve physical NAND blocks for the open file descriptor fd.
    Returns the number of bytes actually allocated.

    Attempts in order:
    1. Contiguous + all-or-nothing (F_ALLOCATECONTIG | F_ALLOCATEALL): ideal for HDD.
       Almost always fails on APFS, which does not maintain large contiguous extents.
    2. Non-contiguous, all-or-nothing (F_ALLOCATEALL): works on HFS+.
       Fails on APFS when the requested size reaches the container's allocatable limit
       (APFS reserves some of the space shown by statvfs for metadata / purgeable data).
    3. Non-contiguous, best-effort (no flags): APFS-friendly; allocates as much as
       possible and returns the actual bytes allocated in fst_bytesalloc.  May return
       less than size on a nearly-full volume.

    Uses Python's fcntl.fcntl(fd, cmd, bytes) which correctly passes the
    struct as a mutable buffer and returns the kernel-modified bytes.
    """
    fs = Fstore()
    fs.fst_posmode    = F_PEOFPOSMODE
    fs.fst_offset     = 0
    fs.fst_length     = size
    fs.fst_bytesalloc = 0

    sz = ctypes.sizeof(Fstore)

    for flags in (F_ALLOCATECONTIG | F_ALLOCATEALL, F_ALLOCATEALL, 0):
        fs.fst_flags      = flags
        fs.fst_bytesalloc = 0
        try:
            result = _py_fcntl.fcntl(fd, F_PREALLOCATE, bytes(fs))
            fs2 = Fstore.from_buffer_copy(result[:sz])
            return fs2.fst_bytesalloc
        except OSError:
            if flags == 0:
                raise   # all three attempts failed; propagate to caller


def punch_hole(fd: int, offset: int, length: int) -> None:
    """
    Deallocate the physical blocks in [offset, offset+length) for file fd,
    making that region sparse.  On APFS this only removes this file's
    reference to those extents; other files sharing them via COW are unaffected.
    """
    if length <= 0:
        return
    ph = Fpunchhole()
    ph.fp_flags  = 0
    ph.reserved  = 0
    ph.fp_offset = offset
    ph.fp_length = length
    try:
        _py_fcntl.fcntl(fd, F_PUNCHHOLE, bytes(ph))
    except OSError as e:
        raise OSError(e.errno, os.strerror(e.errno), f"F_PUNCHHOLE @{offset}+{length}") from e


def get_phys_offset(fd: int, file_offset: int, query_len: int = WRITE_BLOCK) -> int:
    """
    Return the physical device offset that file_offset maps to.
    Returns -1 if unsupported (HFS+, network fs, USB bridge without pass-through, etc.).

    F_LOG2PHYS_EXT: l2p_devoffset is IN (file offset) and OUT (phys offset);
    l2p_contigbytes is IN (bytes to map) and OUT (contiguous physical bytes).
    """
    lp = Log2Phys()
    lp.l2p_flags       = 0
    lp.l2p_contigbytes = query_len
    lp.l2p_devoffset   = file_offset
    sz = ctypes.sizeof(Log2Phys)
    try:
        result = _py_fcntl.fcntl(fd, F_LOG2PHYS_EXT, bytes(lp))
        lp2 = Log2Phys.from_buffer_copy(result[:sz])
        return int(lp2.l2p_devoffset)
    except OSError:
        return -1


def fullfsync(fd: int) -> None:
    """
    Flush all pending writes all the way to the physical media.
    F_FULLFSYNC (macOS) is stronger than fsync(): it bypasses the drive's
    write-back cache and confirms data has reached persistent storage.
    This is necessary for accurate timing of per-block write latency.

    F_FULLFSYNC takes a plain int (not a struct pointer), so the C fcntl
    call works fine here — no buffer-marshalling needed.
    """
    ret = _c_fcntl(fd, F_FULLFSYNC, 0)
    if ret < 0:
        # Do NOT fall back to os.fsync() when interrupted by SIGALRM (EINTR).
        # The _WriteTimeoutError will propagate via the signal handler instead.
        if ctypes.get_errno() != errno.EINTR:
            os.fsync(fd)


def clonefile(src: Path, dst: Path) -> None:
    """
    APFS copy-on-write clone.  The clone shares the same physical extents as
    src; no data is copied.  Both files have independent references to the
    shared extents.  Punching a hole in one does not affect the other.
    """
    ret = _libc.clonefile(bytes(src), bytes(dst), ctypes.c_uint32(0))
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), f"clonefile {src} → {dst}")

# ─────────────────────────── write timeout ─────────────────────────────────

class _WriteTimeoutError(Exception):
    """Raised by the SIGALRM handler when a write+sync exceeds WRITE_TIMEOUT."""

# ─────────────────────────── timing write ──────────────────────────────────

def timed_write(fd: int, offset: int, size: int) -> float:
    """
    Write `size` zero bytes starting at `offset` in the file, then
    F_FULLFSYNC.  Returns the total elapsed time in seconds.

    The timer starts BEFORE the writes so that any slow write path
    (e.g. NAND erase cycles) is captured, not just the fsync wait.

    If the operation does not complete within WRITE_TIMEOUT seconds the
    SIGALRM handler raises _WriteTimeoutError, which interrupts the blocking
    F_FULLFSYNC syscall (via EINTR) and lets the scan continue.  Note:
    Python only skips the EINTR retry in os.write() when the signal handler
    itself raises an exception (PEP 475), so _WriteTimeoutError must be raised
    — not just set a flag.
    """
    t0 = time.monotonic()

    if WRITE_TIMEOUT > 0:
        def _on_alarm(signum, frame):
            raise _WriteTimeoutError()
        old_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, WRITE_TIMEOUT)

    try:
        written = 0
        os.lseek(fd, offset, os.SEEK_SET)
        while written < size:
            chunk = min(len(_ZEROS), size - written)
            n = os.write(fd, memoryview(_ZEROS)[:chunk])
            written += n
        fullfsync(fd)
    except _WriteTimeoutError:
        return WRITE_TIMEOUT   # caller sees maximally-slow block → skip forward
    finally:
        if WRITE_TIMEOUT > 0:
            signal.setitimer(signal.ITIMER_REAL, 0)   # cancel alarm
            signal.signal(signal.SIGALRM, old_handler)

    return time.monotonic() - t0

# ─────────────────────────── scanning ──────────────────────────────────────

def find_region_end(
    fd:          int,
    bad_start:   int,
    good_offset: int,
    threshold:   float,
    write_block: int = WRITE_BLOCK,
) -> int:
    """
    Binary-search between bad_start and good_offset in write_block steps
    to locate the exact offset where write speed returns to normal.
    Returns the first fast offset (= exclusive end of the bad region).

    Algorithm maintains two bounds:
      lo = highest offset confirmed SLOW  (bad region extends at least here)
      hi = lowest  offset confirmed FAST  (bad region ends before here)
    Each probe is the midpoint of [lo, hi]:
      → probe is SLOW: lo jumps UP   (boundary must be above this point)
      → probe is FAST: hi jumps DOWN (boundary must be below this point)
    Loop stops when hi - lo ≤ write_block (boundary located to 1-MiB precision).
    """
    lo = (bad_start   // write_block) * write_block
    hi = (good_offset // write_block) * write_block

    print(
        f"   ↳ Binary search for boundary  "
        f"[slow-side {lo / GiB:.4f} GiB … fast-side {hi / GiB:.4f} GiB]",
        flush=True,
    )

    while hi - lo > write_block:
        mid     = ((lo + hi) // 2 // write_block) * write_block
        elapsed = timed_write(fd, mid, write_block)
        is_slow = elapsed > threshold
        speed   = write_block / max(elapsed, 1e-9) / MiB

        if is_slow:
            lo = mid + write_block          # boundary is above mid
            arrow = f"lo→{lo / GiB:.4f}"
        else:
            hi = mid                        # boundary is below mid
            arrow = f"hi→{hi / GiB:.4f}"

        print(
            f"   ↳   probe @{mid / GiB:.4f} GiB  {elapsed * 1000:6.0f} ms  "
            f"{speed:5.0f} MiB/s  {'SLOW ⚠ ' if is_slow else 'fast   '}  "
            f"{arrow}  [{lo / GiB:.4f} … {hi / GiB:.4f}]",
            flush=True,
        )

    return hi


def scan(
    fd:          int,
    file_size:   int,
    threshold:   float,
    write_block: int = WRITE_BLOCK,
    skip_size:   int = SKIP_SIZE,
) -> List[BadRegion]:
    """
    Forward scan writing `write_block` bytes at a time.

    When a block's flush takes longer than `threshold` seconds:
      • record the start of a bad region (if not already in one)
      • jump `skip_size` bytes forward (skips over the presumably bad area)

    When the scanner finds a fast block after a skip:
      • binary-search backward in `write_block` steps to find the exact byte
        offset where write speed returned to normal (= end of bad region)

    Progress is printed via carriage-return for normal blocks (overwritten
    each second) and with a newline for notable events.
    """
    bad_regions: List[BadRegion] = []
    bad_start:   Optional[int]  = None
    last_print   = time.monotonic()
    blocks_done  = 0
    total_blocks = (file_size + write_block - 1) // write_block

    print(
        f"\n▶  Scan  ({file_size / GiB:.1f} GiB  |  "
        f"{write_block // MiB} MiB blocks  |  "
        f"{skip_size // GiB} GiB skip on slow)"
    )
    speed_label = write_block / threshold / MiB
    print(f"   Slow threshold: {threshold:.3f} s  (<{speed_label:.0f} MiB/s per block)\n")

    offset = 0
    while offset < file_size:
        block_end = min(offset + write_block, file_size)
        block_sz  = block_end - offset
        elapsed   = timed_write(fd, offset, block_sz)
        speed     = block_sz / max(elapsed, 1e-9) / MiB
        pct       = offset / file_size * 100
        is_slow   = elapsed > threshold
        now       = time.monotonic()
        blocks_done += 1

        if is_slow:
            # ── slow block detected ──────────────────────────────────────
            if bad_start is None:
                bad_start = offset
            phys = get_phys_offset(fd, offset)
            phys_str = f"  phys {phys / GiB:.3f} GiB" if phys >= 0 else ""
            next_off = min(offset + skip_size, file_size)
            slow_label = (
                "TIMEOUT ⚠" if (WRITE_TIMEOUT > 0 and elapsed >= WRITE_TIMEOUT)
                else "SLOW ⚠"
            )
            print(
                f"\r   [█] @{offset / GiB:.4f} GiB  "
                f"{elapsed:.2f} s  {speed:5.0f} MiB/s  {slow_label}"
                f"  → skip to {next_off / GiB:.2f} GiB{phys_str}",
                flush=True,
            )
            print()   # lock the event line
            offset    = next_off
            last_print = now

        else:
            # ── fast block ───────────────────────────────────────────────
            if bad_start is not None:
                # First fast block after a skip — pinpoint the boundary with binary search
                print(
                    f"\r   [░] @{offset / GiB:.4f} GiB  fast — "
                    f"pinpointing bad-region boundary (binary search) …",
                    flush=True,
                )
                print()
                exact_end  = find_region_end(fd, bad_start, offset, threshold, write_block)
                phys_start = get_phys_offset(fd, bad_start)
                region     = BadRegion(start=bad_start, end=exact_end, phys_start=phys_start)
                bad_regions.append(region)
                print(
                    f"   ↳ Bad region #{len(bad_regions)} boundary found:"
                    f"  @{bad_start / GiB:.4f} GiB (slow start)  →  "
                    f"@{exact_end / GiB:.4f} GiB (first clean)  "
                    f"= {(exact_end - bad_start) // MiB} MiB\n",
                    flush=True,
                )
                bad_start = None
                last_print = now

            # Rolling progress line (overwritten every second)
            if now - last_print >= PRINT_INTERVAL:
                phys = get_phys_offset(fd, offset)
                phys_str = f"  phys {phys / GiB:.3f}" if phys >= 0 else ""
                print(
                    f"\r   [░] @{offset / GiB:.4f} GiB  "
                    f"{elapsed * 1000:5.0f} ms  {speed:5.0f} MiB/s  "
                    f"{pct:5.1f}%  ({blocks_done}/{total_blocks}){phys_str}   ",
                    end="",
                    flush=True,
                )
                last_print = now

            offset += write_block

    # Trailing bad region that ran to end-of-file
    if bad_start is not None:
        phys_start = get_phys_offset(fd, bad_start)
        bad_regions.append(BadRegion(start=bad_start, end=file_size, phys_start=phys_start))

    print()   # end the rolling progress line
    return bad_regions

# ─────────────────────────── visualisation ─────────────────────────────────

def show_map(file_size: int, bad_regions: List[BadRegion], width: int = 64) -> None:
    """Print a colour-coded ASCII bad-block map and a region summary."""
    cells = ["░"] * width
    for r in bad_regions:
        lo = int(r.start / file_size * width)
        hi = max(lo + 1, int(r.end / file_size * width))
        for i in range(lo, min(hi, width)):
            cells[i] = "█"

    divider = "─" * 70
    print(f"\n{divider}")
    print("  BAD-BLOCK MAP")
    print(divider)
    scale = file_size / width / GiB
    print(f"\n  0 GiB {'─' * (width - 8)} {file_size / GiB:.0f} GiB")
    print(f"  [{''.join(cells)}]")
    print(f"  ↑ each character ≈ {scale:.2f} GiB   ░ = clean   █ = slow/bad\n")

    if not bad_regions:
        print("  ✓  No slow or bad blocks detected.\n")
    else:
        print(f"  {len(bad_regions)} bad region(s):")
        for i, r in enumerate(bad_regions, 1):
            print(f"    {i}.{r.summary(file_size)}")
        total = sum(r.size for r in bad_regions)
        print(f"\n  Total bad: {total // MiB} MiB ({total / file_size * 100:.1f}% of scanned area)")
    print(divider)

# ─────────────────────────── filler creation ───────────────────────────────

def create_fillers(
    scan_path:    Path,
    file_size:    int,
    bad_regions:  List[BadRegion],
    target_dir:   Path,
    try_clone:    bool = True,
    start_index:  int  = 1,
) -> None:
    """
    Create per-region filler files.  Never raises; degrades gracefully.

    start_index lets callers continue filler numbering from a previous run
    (e.g. if filler000005 already exists, pass start_index=6).

    Strategy A (APFS, try_clone=True):
      COW-clone the scan file per region, punch holes outside the bad zone.
      → per-region sparse files, physically occupying only bad NAND.

    Strategy B (HFS+ or after A fails):
      Punch holes in the scan file itself for all GOOD regions.
      → one monolithic sparse file covering only bad zones.

    Strategy C (any filesystem, if B also fails):
      Rename the full scan file as a single crude filler.
      It occupies ALL blocks (good + bad) but at least prevents new data
      from landing on bad blocks until the volume is full.
      A warning is printed explaining the limitation.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    # ── Strategy A: per-region COW clone + punch holes (APFS) ────────────────
    clone_ok = True
    if try_clone:
        for new_i, region in enumerate(bad_regions, 1):
            filler_idx  = start_index + new_i - 1
            filler_name = f"filler{filler_idx:06d}"
            filler_path = target_dir / filler_name
            size_mib    = region.size // MiB
            print(f"  [{new_i}/{len(bad_regions)}] {filler_name}  ({size_mib} MiB)  … ", end="", flush=True)
            try:
                # Never overwrite an existing filler from a previous run.
                if filler_path.exists():
                    print("⚠ exists, skipping")
                    region.filler = filler_name
                    continue
                clonefile(scan_path, filler_path)
                ffd = os.open(str(filler_path), os.O_RDWR)
                try:
                    if region.start > 0:
                        punch_hole(ffd, 0, region.start)
                    if region.end < file_size:
                        punch_hole(ffd, region.end, file_size - region.end)
                finally:
                    os.close(ffd)
                region.filler = filler_name
                print("✓")
            except Exception as exc:
                print(f"✗  ({exc})")
                filler_path.unlink(missing_ok=True)   # clean up partial clone
                clone_ok = False
                break   # one failure means clonefile/punchhole not supported here

        if clone_ok:
            scan_path.unlink(missing_ok=True)
            print("\n  Scan file deleted (good blocks freed, bad blocks held by fillers).")
            return

        print("\n  ⚠  COW clone not available — falling back to sparse single filler.")
        # Clean up any partial fillers created in this run before the failure
        for f in target_dir.glob("filler??????"):
            # Only remove fillers created by this run (index >= start_index)
            try:
                if int(f.name[6:]) >= start_index:
                    f.unlink(missing_ok=True)
            except ValueError:
                pass
        for r in bad_regions:
            r.filler = ""

    # ── Strategy B: punch holes in scan file (HFS+ or A fallback) ────────────
    fallback = target_dir / "filler_all.bin"
    scan_fd  = os.open(str(scan_path), os.O_RDWR)
    punch_ok = True
    try:
        sorted_regions = sorted(bad_regions, key=lambda r: r.start)
        cursor = 0
        for r in sorted_regions:
            if r.start > cursor:
                punch_hole(scan_fd, cursor, r.start - cursor)
            cursor = r.end
        if cursor < file_size:
            punch_hole(scan_fd, cursor, file_size - cursor)
    except OSError as exc:
        print(f"  ⚠  F_PUNCHHOLE not supported ({exc.strerror}) — using crude full filler.")
        punch_ok = False
    finally:
        os.close(scan_fd)

    scan_path.rename(fallback)
    filler_label = str(fallback.name)
    for r in bad_regions:
        r.filler = filler_label

    if punch_ok:
        print(f"  Sparse filler saved: {fallback}")
        print("  (Contains only bad-block data; good blocks freed as sparse holes.)")
    else:
        # ── Strategy C: crude full filler ─────────────────────────────────────
        print(f"  Crude filler saved: {fallback}")
        print(
            "  ⚠  This filler occupies ALL free space (good + bad blocks).\n"
            "  Bad blocks are blocked, but so are the good ones — no free space remains.\n"
            "  For precise per-block fillers on this filesystem, see FEATURE_rolling_split.md."
        )

# ─────────────────────────── API self-check ────────────────────────────────

def api_check(volume: Path) -> None:
    """
    Write a tiny test file and verify each macOS API works.
    Safe to run at any time; removes test files when done.
    """
    test = volume / "._bbf_api_test"
    print("\n▶  macOS API check …\n")

    results = {}

    # Struct sizes (must match the C definitions)
    results["sizeof(Fstore)=32"]      = ctypes.sizeof(Fstore)      == 32
    results["sizeof(Fpunchhole)=24"]  = ctypes.sizeof(Fpunchhole)  == 24
    # Log2Phys: uint32(4) + 4-byte pad + int64(8) + int64(8) = 24
    results["sizeof(Log2Phys)=24"]    = ctypes.sizeof(Log2Phys)    == 24

    # F_PREALLOCATE
    try:
        fd = os.open(str(test), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        allocated = preallocate(fd, 16 * MiB)
        os.ftruncate(fd, 16 * MiB)
        results["F_PREALLOCATE"] = allocated > 0
    except Exception as e:
        results[f"F_PREALLOCATE ({e})"] = False
        fd = os.open(str(test), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        os.ftruncate(fd, 16 * MiB)

    # F_FULLFSYNC
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\x00" * 4096)
        fullfsync(fd)
        results["F_FULLFSYNC"] = True
    except Exception as e:
        results[f"F_FULLFSYNC ({e})"] = False

    # F_LOG2PHYS_EXT
    try:
        phys = get_phys_offset(fd, 0)
        results["F_LOG2PHYS_EXT"] = phys >= 0
    except Exception as e:
        results[f"F_LOG2PHYS_EXT ({e})"] = False

    os.close(fd)

    # F_PUNCHHOLE
    try:
        fd2 = os.open(str(test), os.O_RDWR)
        punch_hole(fd2, 0, 4 * MiB)
        os.close(fd2)
        results["F_PUNCHHOLE"] = True
    except Exception as e:
        results[f"F_PUNCHHOLE ({e})"] = False

    # clonefile
    clone = volume / "._bbf_api_clone"
    try:
        clonefile(test, clone)
        results["clonefile"] = clone.exists()
        clone.unlink(missing_ok=True)
    except Exception as e:
        results[f"clonefile ({e})"] = False

    test.unlink(missing_ok=True)

    # Report
    all_pass = True
    for name, ok in results.items():
        icon = "✓" if ok else "✗"
        print(f"  {icon}  {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("  All APIs functional — ready to scan.\n")
    else:
        print("  ✗  Some APIs are unavailable on this volume/OS.\n")
        print("  F_PUNCHHOLE and clonefile require APFS (macOS 10.12+).")
        print("  The fallback single-filler mode will be used instead.\n")

# ─────────────────────────── main ──────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Detect and neutralise slow or unreadable storage regions on any writable\n"
            "macOS volume (SSD, HDD, USB drive) formatted as APFS, HFS+, ExFAT, etc.\n"
            "\n"
            "How it works:\n"
            "  Writes 1-MiB blocks sequentially and times each flush to physical media.\n"
            "  Blocks slower than --slow-threshold are flagged as bad; the scanner skips\n"
            "  --skip-gib forward, then binary-searches back to the exact boundary.\n"
            "  On APFS/HFS+ the bad regions are locked inside sparse filler files so no\n"
            "  new data can land there.  On ExFAT/NTFS a detection-only map is produced.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Recommended settings:\n"
            "\n"
            "  SSD (NVMe internal)   --slow-threshold 0.05  (50 ms  = <20 MiB/s)\n"
            "  SSD (USB, healthy)    --slow-threshold 0.1   (100 ms = <10 MiB/s)\n"
            "  SSD (USB, degraded)   --slow-threshold 1.0   ( 1 s   =  <1 MiB/s)  [default]\n"
            "\n"
            "  HDD — fragmentation warning:\n"
            "    On a fragmented HDD the scan file itself may be scattered across\n"
            "    non-contiguous extents, adding 15-50 ms of seek overhead per block.\n"
            "    Use a larger block size to amortise this cost, and a conservative\n"
            "    threshold to avoid flagging normal seek latency as a bad block.\n"
            "    Genuine HDD bad-sector retries take 1-30 s — far above seek overhead.\n"
            "\n"
            "  HDD (external, 5400 rpm)  --block-mib 16 --slow-threshold 2.0\n"
            "  HDD (internal, 7200 rpm)  --block-mib 8  --slow-threshold 1.0\n"
            "\n"
            "Examples:\n"
            "  python3 bad_block_filler.py /Volumes/MyDrive\n"
            "  python3 bad_block_filler.py /Volumes/MyHDD   --block-mib 16 --slow-threshold 2.0\n"
            "  python3 bad_block_filler.py /Volumes/MySSD   --slow-threshold 0.05\n"
            "  python3 bad_block_filler.py /Volumes/MyDrive --api-check\n"
            "  python3 bad_block_filler.py /Volumes/MyDrive --force-remapping\n"
        ),
    )
    p.add_argument("volume", type=Path, help="Mount point of the target volume.")
    p.add_argument(
        "--force-remapping",
        action="store_true",
        help=(
            "Delete all .badblocks/filler* files before scanning. "
            "Use after defragmentation moves data and invalidates old fillers."
        ),
    )
    p.add_argument(
        "--slow-threshold",
        type=float,
        default=1.0,
        metavar="SECS",
        help=(
            "Seconds per write-block to consider slow. Default: %(default)s (1 s). "
            "SSD: 0.05–0.1.  HDD: 1.0–2.0 (use --block-mib 8-16 to avoid "
            "fragmentation false positives — see --help epilog)."
        ),
    )
    p.add_argument(
        "--block-mib",
        type=int,
        default=None,
        metavar="MiB",
        help=(
            "Write-block size in MiB. Default: auto (1 for SSD, 16 for HDD). "
            "Override with an explicit value to disable auto-detection."
        ),
    )
    p.add_argument(
        "--skip-gib",
        type=int,
        default=1,
        metavar="GiB",
        help="GiB to skip forward when a slow block is found. Default: %(default)s.",
    )
    p.add_argument(
        "--headroom-mib",
        type=int,
        default=512,
        metavar="MiB",
        help="MiB to keep free on the volume so macOS stays healthy. Default: %(default)s.",
    )
    p.add_argument(
        "--write-timeout",
        type=float,
        default=60.0,
        metavar="SECS",
        help=(
            "Seconds before a write+sync with no drive response is abandoned and "
            "treated as a dead block.  Prevents the tool from freezing indefinitely "
            "on a completely unresponsive block (the process would otherwise enter "
            "uninterruptible I/O wait and require a force-unmount to escape). "
            "Default: %(default)s s.  Set to 0 to disable."
        ),
    )
    p.add_argument(
        "--no-fillers",
        action="store_true",
        help="Scan and report only; do not create filler files.",
    )
    p.add_argument(
        "--api-check",
        action="store_true",
        help="Run a self-check of macOS APIs on the volume and exit.",
    )
    return p.parse_args()


def main() -> None:
    args   = parse_args()
    volume = args.volume.resolve()

    # ── basic sanity ─────────────────────────────────────────────────────────
    if not volume.exists():
        print(f"✗  Path not found: {volume}")
        sys.exit(1)
    # Check with effective IDs so sudo is honoured correctly.
    # Fall back to a real write probe if os.access is inconclusive.
    has_write = os.access(volume, os.W_OK, effective_ids=True)
    if not has_write:
        # Double-check with an actual probe (catches noowners mounts)
        try:
            probe = volume / "._bbf_probe"
            probe.touch()
            probe.unlink()
            has_write = True
        except OSError:
            has_write = False
    if not has_write:
        print(f"✗  Cannot write to {volume}")
        print(f"   Fix 1: grant Full Disk Access to your terminal app:")
        print(f"          System Settings → Privacy & Security → Full Disk Access")
        print(f"          Add your terminal, then quit and relaunch it.")
        print(f"   Fix 2: run as root if the volume has restrictive permissions:")
        print(f"          sudo python3 bad_block_filler.py {volume}")
        sys.exit(1)

    badblocks_dir = volume / BADBLOCKS_DIR
    scan_file     = volume / SCAN_TMP_NAME
    skip_size     = args.skip_gib  * GiB
    headroom      = args.headroom_mib * MiB

    # ── drive type auto-detection ─────────────────────────────────────────────
    drive_type, drive_desc, auto_block_mib = detect_drive_type(volume)
    if args.block_mib is not None:
        # User supplied explicit value — respect it
        write_block   = args.block_mib * MiB
        block_mib_src = f"{args.block_mib} MiB (user-specified)"
    else:
        # Auto-select based on drive type
        write_block   = auto_block_mib * MiB
        block_mib_src = f"{auto_block_mib} MiB (auto: {drive_type})"

    # Rebuild _ZEROS if the write block changed from the module default
    global _ZEROS, WRITE_TIMEOUT
    if len(_ZEROS) != write_block:
        _ZEROS = bytearray(write_block)
    WRITE_TIMEOUT = args.write_timeout

    # ── acquire per-volume lock (prevents parallel runs) ─────────────────────
    # The lock fd must stay open until the process exits — closing it releases
    # the advisory lock.  We keep a reference in a local variable.
    lock_fd = acquire_lock(volume)
    # Ensure the lock file is removed on clean exit (it is harmless if left)
    lock_path = volume / LOCK_FILE_NAME

    # ── API check shortcut ───────────────────────────────────────────────
    if args.api_check:
        api_check(volume)
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
        return

    # ── force-remapping: delete entire .badblocks directory, start clean ─────────
    if args.force_remapping and badblocks_dir.exists():
        shutil.rmtree(badblocks_dir)
        print(f"✓  Deleted {badblocks_dir} (full clean — ready for fresh scan)")

    # ── load state from previous runs (resumption) ──────────────────────────
    existing_regions, next_filler_index = load_existing_state(badblocks_dir)

    # ── clean up stale scan file BEFORE measuring free space ─────────────────
    # The stale file may occupy physical blocks; deleting it first ensures that
    # statvfs reflects the correct available space for the new target calculation.
    if scan_file.exists():
        stale_gib = scan_file.stat().st_size / GiB
        print(
            f"\n  ⚠  Found leftover scan file from a previous interrupted run:"
            f"\n     {scan_file}  ({stale_gib:.1f} GiB)"
            f"\n     Deleting it to reclaim space before the new scan."
        )
        scan_file.unlink()

    # ── free space ────────────────────────────────────────────────────────
    st         = os.statvfs(volume)
    free_bytes = st.f_bavail * st.f_frsize
    # Align target size down to write_block boundary
    target     = ((free_bytes - headroom) // write_block) * write_block

    if target <= 0:
        print(
            f"✗  Not enough free space on {volume}\n"
            f"   Free: {free_bytes // MiB} MiB  Headroom: {headroom // MiB} MiB  "
            f"Need at least {(headroom + write_block) // MiB} MiB"
        )
        sys.exit(1)

    # ── filesystem type + filler capability ──────────────────────────────────
    fs_type = detect_fs_type(volume)
    supports_clone, supports_punch, fs_desc = filler_capability(fs_type)
    fillers_possible = supports_clone or supports_punch

    print(f"\n  Volume      : {volume}")
    print(f"  Drive       : {drive_desc}")
    print(f"  Filesystem  : {fs_desc}")
    print(f"  Free space  : {free_bytes / GiB:.1f} GiB")
    print(f"  Block size  : {block_mib_src}")
    print(f"  Scan file   : {target / GiB:.1f} GiB  ({target // write_block:,} blocks × {write_block // MiB} MiB)")
    print(f"  Threshold   : {args.slow_threshold:.3f} s / {write_block // MiB} MiB  "
          f"(<{write_block / args.slow_threshold / MiB:.0f} MiB/s = bad)")
    print(f"  Skip on slow: {skip_size // GiB} GiB forward")
    if existing_regions:
        total_prev = sum(r.size for r in existing_regions) // MiB
        print(
            f"  Resuming    : {len(existing_regions)} bad region(s) from previous scan"
            f"  ({total_prev} MiB already locked, next filler: filler{next_filler_index:06d})"
        )
    if drive_type == "hdd" and args.block_mib is None:
        print(
            f"  ⚠  HDD detected — block-mib auto-set to {auto_block_mib} to reduce seek-"
            f"overhead false positives.  Consider --slow-threshold 2.0."
        )

    # ── prominent notice when fillers cannot be created ────────────────────────
    if not fillers_possible and not args.no_fillers:
        est_hours = target / (50 * MiB) / 3600   # rough estimate at 50 MiB/s
        print(
            f"\n  ⚠⚠⚠  DETECTION-ONLY MODE  ⚠⚠⚠"
            f"\n  {fs_type.upper()} does not support sparse files."
            f"\n  The scan will write and time every MiB of free space"
            f"\n  (~{est_hours:.1f} h at 50 MiB/s)."
            f"\n  Result: a bad-block map only — you will be asked what to do"
            f"\n  with the scan file afterwards."
            f"\n"
            f"\n  To create precise fillers: reformat the volume as APFS or HFS+."
            f"\n  To skip the scan entirely: Ctrl-C now."
        )
        try:
            answer = input("\n  Proceed with detection-only scan? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)
        print()

    # ── create and pre-allocate the scan file ─────────────────────────────────
    print(f"\n▶  Pre-allocating {target / GiB:.1f} GiB scan file … ", end="", flush=True)

    try:
        fd = os.open(str(scan_file), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError as e:
        print(f"✗\n  Cannot create scan file: {e}")
        sys.exit(1)

    # Ctrl-C handler: leave scan file in place (holds physical blocks, resume later)
    def _sigint(_sig, _frame):
        try:
            os.close(fd)
        except Exception:
            pass
        print(f"\n\n  ⚠  Interrupted.  Scan file left at:\n     {scan_file}")
        print(f"  Remove to free space:  sudo rm {scan_file}")
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigint)

    try:
        allocated = preallocate(fd, target)
        if allocated >= target:
            alloc_label = f"{allocated / GiB:.1f} GiB reserved"
        else:
            lazy_gib    = (target - allocated) / GiB
            alloc_label = (
                f"{allocated / GiB:.1f} of {target / GiB:.1f} GiB pre-allocated; "
                f"{lazy_gib:.1f} GiB lazy"
            )
    except OSError as e:
        print(f"\n  ⚠  F_PREALLOCATE failed ({e}) — blocks will be allocated lazily on write.")
        alloc_label = f"{target / GiB:.1f} GiB target, lazy allocation"

    os.ftruncate(fd, target)
    print(f"✓  ({alloc_label})")

    # ── scan ───────────────────────────────────────────────────────────────
    bad_regions = scan(fd, target, args.slow_threshold, write_block, skip_size)
    os.close(fd)

    # ── display map ────────────────────────────────────────────────────────────
    show_map(target, bad_regions)

    # ── no bad blocks? ─────────────────────────────────────────────────────────
    if not bad_regions:
        scan_file.unlink(missing_ok=True)
        print("  Drive appears healthy — no filler files needed.\n")
        return

    # ── explicit --no-fillers flag ──────────────────────────────────────────────
    if args.no_fillers:
        scan_file.unlink(missing_ok=True)
        print("  --no-fillers set: scan file removed.  No fillers created.\n")

    # ── unsupported filesystem: ask user what to do with the scan file ───────────
    elif not fillers_possible:
        total_bad_mib = sum(r.size for r in bad_regions) // MiB
        scan_gib      = target / GiB
        print(
            f"\n  {fs_type.upper()} does not support sparse files, so precise"
            f" per-region fillers cannot be created."
            f"\n  The scan file ({scan_gib:.1f} GiB) currently occupies ALL free"
            f" space, including the {total_bad_mib} MiB of bad blocks above."
            f"\n"
            f"\n  [K]eep — rename to .badblocks/filler_crude.bin."
            f"\n          Bad blocks are blocked, but good blocks are also locked"
            f"\n          (no free space remains on the volume)."
            f"\n  [D]elete — remove the scan file, restore all free space."
            f"\n            Bad blocks are no longer blocked.  (default)"
        )
        try:
            answer = input("\n  Keep or Delete scan file? [k/D] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "d"
            print()
        if answer in ("k", "keep"):
            badblocks_dir.mkdir(parents=True, exist_ok=True)
            crude = badblocks_dir / "filler_crude.bin"
            scan_file.rename(crude)
            for r in bad_regions:
                r.filler = crude.name
            print(f"\n  Crude filler kept: {crude}\n")
        else:
            scan_file.unlink(missing_ok=True)
            print("\n  Scan file deleted.  No fillers created.\n")

    # ── create filler files (APFS / HFS+) ──────────────────────────────────────
    else:
        print(f"\n▶  Creating filler files in {badblocks_dir} …\n")
        create_fillers(
            scan_file, target, bad_regions, badblocks_dir,
            supports_clone, start_index=next_filler_index,
        )

    # ── write map.json ──────────────────────────────────────────────────────
    # Merge previous runs' regions with this run's new findings.
    # Existing regions already have their filler names from prior map.json;
    # new regions have just been assigned filler names by create_fillers.
    all_regions = existing_regions + bad_regions
    badblocks_dir.mkdir(parents=True, exist_ok=True)
    map_data = {
        "volume":             str(volume),
        "last_scan_date":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_scan_bytes":    target,
        "slow_threshold_sec": args.slow_threshold,
        "write_block_bytes":  write_block,
        "skip_size_bytes":    skip_size,
        "bad_regions": [
            {
                "index":         i,
                "start_bytes":   r.start,
                "end_bytes":     r.end,
                "size_bytes":    r.size,
                "start_gib":     round(r.start / GiB, 4),
                "end_gib":       round(r.end   / GiB, 4),
                "size_mib":      r.size // MiB,
                "phys_start":    r.phys_start,
                "filler":        r.filler,
            }
            for i, r in enumerate(all_regions, 1)
        ],
    }
    map_path = badblocks_dir / "map.json"
    map_path.write_text(json.dumps(map_data, indent=2) + "\n")

    print(f"\n  Map written: {map_path}")
    total_new = sum(r.size for r in bad_regions)
    total_all = sum(r.size for r in all_regions)
    if existing_regions:
        print(
            f"\n✓  Done.  {len(bad_regions)} new bad region(s) found  ({total_new // MiB} MiB locked this run)."
            f"\n   Total across all runs: {len(all_regions)} region(s)  ({total_all // MiB} MiB).\n"
        )
    else:
        print(f"\n✓  Done.  {len(bad_regions)} bad region(s) neutralised  ({total_all // MiB} MiB locked).\n")


if __name__ == "__main__":
    main()

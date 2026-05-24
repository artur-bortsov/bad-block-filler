# Feature: Rolling-Split Filler Creation (Filesystem-Agnostic)

## Problem

The current filler creation relies on two APFS-only syscalls:

| Syscall | Purpose | APFS | HFS+ | ExFAT | NTFS |
|---|---|---|---|---|---|
| `clonefile()` | COW clone — zero-copy snapshot of scan file | ✓ | ✗ | ✗ | ✗ |
| `F_PUNCHHOLE` | Sparse holes — free physical blocks in a file | ✓ | ✓ | ✗ | varies |

The current HFS+ fallback (punch holes in the full scan file) works but produces one monolithic
filler instead of per-region files.  ExFAT and NTFS cannot create sparse files at all, so both
syscalls fail and the script falls back to leaving the full scan file as a crude filler that
occupies good blocks as well as bad ones — not useful for a working drive.

## Proposed Solution: Rolling-Split Allocation

Instead of scanning one big file and doing post-scan surgery, split the free space into files
**while scanning**, using the filesystem's own allocator to separate good zones from bad zones.

### Core Insight

On a nearly-full volume, after you `ftruncate` a file to a smaller size, the freed physical
blocks become the **only available free space**.  A file created immediately after is almost
certain to be allocated to exactly those blocks.

### Algorithm

```
┌─────────────────────────────────────────────────────────────────────┐
│ Free space                                                          │
│ ════════════════════════════════════════════════════════════════════│
│                                                                     │
│ Step 1: create scan_0, ftruncate to ALL free space                 │
│ scan_0 [════════════════════════════════════════════════════════]   │
│                                                                     │
│ Step 2: write forward block-by-block → SLOW at position P          │
│ scan_0 [GOOD GOOD GOOD ✗slow …                                ]   │
│                                                                     │
│ Step 3: ftruncate(scan_0, P)  →  scan_0 becomes a "good file"     │
│ scan_0 [GOOD GOOD GOOD]                                            │
│                        [freed ─────────────────────────────────]   │
│                                                                     │
│ Step 4: create filler_1, ftruncate to (total − P)                 │
│         (nearly-full volume → allocator gives the just-freed space)│
│ scan_0 [GOOD GOOD GOOD]                                            │
│ filler_1               [════════════════════════════════════════]  │
│                                                                     │
│ Step 5: binary-search in filler_1 to locate bad_end = M           │
│         ftruncate(filler_1, M − P)                                 │
│ scan_0 [GOOD GOOD GOOD]                                            │
│ filler_1               [BAD BAD BAD]                               │
│                                     [freed ─────────────────────]  │
│                                                                     │
│ Step 6: create scan_1, ftruncate to (total − M)                   │
│ scan_0 [GOOD GOOD GOOD]                                            │
│ filler_1               [BAD BAD BAD]                               │
│ scan_1                              [════════════════════════════]  │
│                                                                     │
│ Step 7: continue writing forward in scan_1 from offset M …        │
│         repeat for every bad zone found                            │
│                                                                     │
│ ─────── END OF SCAN ──────────────────────────────────────────────│
│ scan_0, scan_1, … = GOOD files  → delete them                     │
│ filler_1, filler_2, … = BAD zones → rename to filler000001 …     │
└─────────────────────────────────────────────────────────────────────┘
```

### Pseudocode

```python
seg_start = 0
seg_fd    = open_and_ftruncate("scan_0", total_free)
good_segs = ["scan_0"]
bad_segs  = []
bad_start = None

while scan_offset < total_free:
    elapsed = timed_write(seg_fd, scan_offset - seg_start, WRITE_BLOCK)

    if elapsed > threshold:
        if bad_start is None:
            bad_start = scan_offset

            # Trim current segment to only the good zone before the bad block
            ftruncate(seg_fd, bad_start - seg_start)
            close(seg_fd)                    # scan_N is now a "good file"

        scan_offset += SKIP_SIZE

    else:
        if bad_start is not None:
            # Found fast block after skip — binary-search boundary in a fresh file

            # 1. Create filler_N claiming [bad_start .. total)
            filler_fd = open_and_ftruncate(f"filler_{n}", total - bad_start)

            # 2. Binary-search for bad_end (offsets relative to filler start)
            bad_end_rel = binary_search(filler_fd, 0, scan_offset - bad_start, threshold)
            bad_end = bad_start + bad_end_rel

            # 3. Trim filler to exactly the bad zone
            ftruncate(filler_fd, bad_end - bad_start)
            close(filler_fd)
            bad_segs.append(filler_fd.path)

            # 4. Open new scan segment claiming [bad_end .. total)
            seg_fd    = open_and_ftruncate(f"scan_{k}", total - bad_end)
            seg_start = bad_end
            good_segs.append(seg_fd.path)
            bad_start = None

        scan_offset += WRITE_BLOCK

# Cleanup
for path in good_segs: unlink(path)        # free good blocks
for i, path in enumerate(bad_segs):
    rename(path, f".badblocks/filler{i+1:06d}")  # keep bad zones
```

### Caveats

**Allocation guarantee.**  The approach relies on the filesystem allocator reusing just-freed
blocks immediately.  This holds on:

- **ExFAT** (FAT chain, first/next fit) on a nearly-full volume — very likely ✓
- **HFS+** (bitmap allocator, tends to reuse freed bitmap runs) — likely ✓
- **APFS** (complex space manager, multiple allocation tracks) — NOT reliable ✗
  → for APFS keep the current COW clone + punch-hole approach.
- **ext4** Linux (bitmap allocator, next-fit) — likely ✓
- **btrfs** Linux (extent tree) — less predictable, but clone ioctl is available anyway ✓

**Verification.**  Where `F_LOG2PHYS_EXT` is available (APFS, HFS+), verify after each
split that `filler_N`'s physical start matches `bad_start`.  Warn the user if it doesn't.

**Binary search inside segment.**  The `find_region_end` function currently uses absolute
file offsets into the single scan file.  With rolling files, offsets must be translated:
`file_offset = absolute_offset - seg_start`.

## Implementation Plan

### Phase 1 — Platform Detection (do first)
- `detect_fs_type(volume) -> str` using `mount` output parsing.
- Print FS type at startup.
- Choose filler strategy: `apfs/hfs` → current COW/punch approach; others → rolling split.

### Phase 2 — Refactor scan loop
- Extract per-segment write state into a `ScanSegment` dataclass.
- Track `seg_start`, `seg_fd`, `seg_path` as mutable scan state.
- On slow block: call `_close_good_segment()` + `_open_filler_segment()`.
- On fast-after-skip: call `_finalize_filler()` + `_open_scan_segment()`.

### Phase 3 — Segment-aware binary search
- Add `seg_start` parameter to `find_region_end`.
- Translate absolute offsets to segment-relative before each `timed_write`.

### Phase 4 — Cleanup
- Delete all good segment files.
- Rename bad segment files to `filler000001` etc.
- Write `map.json` as before.

### Phase 5 — Allocation verification (optional)
- After each filler segment is created, call `get_phys_offset(filler_fd, 0)`.
- Compare with expected `bad_start` physical address.
- Warn if mismatch; proceed anyway (filler is still a valid file even in wrong location).

## Platform Support Matrix

| Platform | FS | Detect | Current filler | Rolling-split filler | Notes |
|---|---|---|---|---|---|
| macOS | APFS | ✓ | ✓ (COW + punch) | not needed | COW is more reliable |
| macOS | HFS+ | ✓ | partial (punch only) | ✓ | rolling better for per-region files |
| macOS | ExFAT | ✓ | ✗ crashes | ✓ best-effort | needs rolling split |
| macOS | NTFS (Paragon) | ✓ | ✗ | try; warn on mismatch | sparse support varies |
| Linux | ext4 | ✓ | needs Linux port of punch_hole | ✓ | fallocate PUNCH_HOLE |
| Linux | btrfs | ✓ | ✓ via FICLONERANGE | ✓ | best Linux support |
| Linux | xfs | ✓ | ✓ via FICLONERANGE | ✓ | |
| Linux | exFAT | ✓ | ✗ | ✓ best-effort | same as macOS ExFAT |
| Linux | ntfs-3g | ✓ | try (ntfs-3g ≥2017) | ✓ best-effort | varies by driver version |

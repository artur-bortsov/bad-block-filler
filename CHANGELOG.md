# Changelog

## [1.1.0] – 2026-05-24

### Added
- **Mid-scan resumption.** Progress (position + found regions) is saved as an
  extended attribute (`xattr`) on the scan file after every bad-region boundary
  and every ~1 GiB of scanning.  Re-running the same command after an
  interruption resumes from the saved position automatically.
- **`--clean` flag.** Removes all files left by the tool on the volume: the
  scan temp file, the lock file, and the entire `.badblocks/` directory.
- **`tool_version` field in `map.json`.** A notice is printed when loading
  fillers created by a different version of the script.
- **map.json pre-allocation.** Before the scan starts, a placeholder is written
  to `.badblocks/map.json` with `F_FULLFSYNC` so the sectors used by the final
  map file are verified as good before any bad blocks are found.
- **`F_FULLFSYNC` on map.json write.** The final map file is now flushed to
  physical media after writing to confirm the commit.
- **AppleDouble companion cleanup.** On ExFAT/FAT32, macOS stores xattr data
  in a hidden `._filename` companion file.  All scan-file deletions now also
  remove the companion so a single `rm` always leaves a clean state.

### Changed
- **Write timeout is now automatic.** The `--write-timeout` flag has been
  removed.  The per-write SIGALRM fires after `slow_threshold × 3` seconds
  (minimum 0.5 s) and immediately interrupts the syscall.  There is no reason
  to wait longer than that — any block past threshold is already bad.
- **No countdown display.** The per-second `⏳ drive not responding — aborting
  in N s …` messages have been removed along with the repeating timer.  Slow
  blocks are skipped after `threshold × 3` seconds without any noise.
- **Stale scan file deleted before free-space measurement.** Previously the
  stale file was deleted after `statvfs`, which could cause the target size to
  be computed incorrectly when the stale file held physical blocks.
- **APFS lazy allocation is silent.** `F_PREALLOCATE` on APFS always falls back
  to lazy allocation; this is now reported as `✓  (N GiB, lazy allocation)`
  without any warning.  The best-effort `flags=0` attempt has been removed
  because it would physically map extents that might include bad sectors,
  causing uninterruptible kernel I/O waits.
- **Signal handler reentrancy fix.** The countdown used `print()`, which is not
  reentrant and raised `RuntimeError` when SIGALRM fired during a prior `print`.
  Replaced with `os.write(1, …)` (direct syscall, no lock).

### Fixed
- Binary search probe results no longer merge with countdown text on the same
  terminal line.
- Free space is now measured AFTER any stale scan file is deleted, giving an
  accurate target size for the new scan.

## [1.0.0] – 2026-05-24

### Added
- Initial release.
- Bad-block detection via timed writes and `F_FULLFSYNC` — works without SMART.
- 1 MiB precision binary search to pinpoint exact bad-region boundaries.
- APFS filler creation using `clonefile` + `F_PUNCHHOLE` (per-region sparse files).
- HFS+ fallback: single sparse filler via `F_PUNCHHOLE` on the scan file.
- ExFAT / NTFS detection-only mode with optional crude full filler.
- Drive-type auto-detection via `diskutil info` (SSD → 1 MiB blocks, HDD → 16 MiB).
- `--force-remapping`: delete all fillers and re-scan from scratch.
- `--no-fillers`: scan and report without creating filler files.
- `--api-check`: verify all required macOS APIs before a real scan.
- Resumable scans: loads existing `.badblocks/map.json` on re-run and continues
  filler numbering from where the previous run left off.
- Per-volume `flock(2)` lock prevents parallel runs on the same volume.
- Stale scan-file detection and cleanup on startup.
- Full Disk Access guidance in the write-permission error message.
- macOS 10.12 Sierra or later; Python 3.9+; no third-party dependencies.

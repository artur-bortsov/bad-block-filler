# Changelog

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

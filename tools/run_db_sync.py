#!/usr/bin/env python3
"""Entrypoint for database package sync.

Keeps the SHA256SUMS update scoped to the current ABI repository while the main
synchronizer remains usable as a standalone module.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_db_packages as sync  # noqa: E402


def update_sha256sums(repo_dir: Path, imported):
    sums_path = repo_dir / "SHA256SUMS"
    values = {}
    if sums_path.exists():
        for line in sums_path.read_text("utf-8", errors="replace").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                values[parts[1].strip()] = parts[0]
    for item in imported:
        values[f"All/{item.filename}"] = item.sha256
    sums_path.write_text(
        "".join(f"{values[path]}  {path}\n" for path in sorted(values)),
        encoding="utf-8",
    )


sync.update_sha256sums = update_sha256sums
raise SystemExit(sync.main())

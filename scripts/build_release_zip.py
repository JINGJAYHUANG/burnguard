"""Deterministic commercial release bundler (`dist/burnguard-pro-v1.0.0.zip` + SHA256)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile


EXCLUDE_DIRS = {".venv", ".tmp", "__pycache__", ".pytest_cache", "dist", ".git", "burnguard.egg-info"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".DS_Store"}


def build_release_bundle(project_root: Path) -> dict:
    dist_dir = project_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dist_dir / "burnguard-pro-v1.0.0.zip"

    included_files: list[Path] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_root)
        if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.name in EXCLUDE_SUFFIXES or path.suffix in EXCLUDE_SUFFIXES:
            continue
        included_files.append(path)

    # Write deterministic zip archive under clean top-level folder `burnguard-pro-v1.0.0/`
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file_path in included_files:
            rel_posix = file_path.relative_to(project_root).as_posix()
            arcname = f"burnguard-pro-v1.0.0/{rel_posix}"
            info = zipfile.ZipInfo(arcname)
            info.date_time = (2026, 11, 23, 19, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, file_path.read_bytes())

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_file = dist_dir / "burnguard-pro-v1.0.0.zip.sha256"
    sha_file.write_text(f"{digest}  burnguard-pro-v1.0.0.zip\n", encoding="utf-8")

    manifest = {
        "product": "BurnGuard Pro",
        "version": "1.0.0",
        "archive": str(zip_path),
        "archive_filename": zip_path.name,
        "size_bytes": zip_path.stat().st_size,
        "sha256": digest,
        "file_count": len(included_files),
        "files": [p.relative_to(project_root).as_posix() for p in included_files],
        "pricing_tiers_usd": {"solo": "29.00", "pro": "59.00", "team": "149.00"},
    }
    (dist_dir / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    result = build_release_bundle(root)
    print(f"Built {result['archive_filename']} ({result['size_bytes']:,} bytes, {result['file_count']} files)")
    print(f"SHA256: {result['sha256']}")

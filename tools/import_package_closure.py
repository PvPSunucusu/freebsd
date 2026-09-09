#!/usr/bin/env python3
"""Import locally built FreeBSD packages plus only their runtime dependency closure.

The build workflow can package every pkg-managed dependency in the VM. This tool
starts from named database roots, walks +COMPACT_MANIFEST deps, skips dependencies
already present in the target repository, and imports only what is actually needed.
Every imported package is rejected unless its embedded FreeBSD major ABI matches
the target repository.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple


def compact_manifest(path: Path) -> dict:
    proc = subprocess.run(
        ["bsdtar", "-xOf", str(path), "+COMPACT_MANIFEST"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{path}: +COMPACT_MANIFEST okunamadi: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def load_catalog(repo: Path) -> Dict[str, dict]:
    with tarfile.open(repo / "packagesite.txz", "r:xz") as tf:
        member = next((m for m in tf.getmembers() if m.isfile() and m.name.endswith("packagesite.yaml")), None)
        if member is None:
            raise RuntimeError("packagesite.yaml bulunamadi")
        fh = tf.extractfile(member)
        if fh is None:
            raise RuntimeError("packagesite.yaml okunamadi")
        rows = [json.loads(line) for line in fh.read().decode("utf-8").splitlines() if line.strip()]
    return {str(row["name"]): row for row in rows}


def write_catalog(repo: Path, catalog: Dict[str, dict]) -> None:
    payload = "".join(
        json.dumps(catalog[name], ensure_ascii=False, separators=(",", ":")) + "\n"
        for name in sorted(catalog)
    ).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix="packagesite-", suffix=".txz", dir=str(repo))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tarfile.open(tmp, "w:xz", format=tarfile.PAX_FORMAT) as tf:
            info = tarfile.TarInfo("packagesite.yaml")
            info.size = len(payload)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "wheel"
            info.mtime = 0
            tf.addfile(info, io.BytesIO(payload))
        os.replace(tmp, repo / "packagesite.txz")
    finally:
        tmp.unlink(missing_ok=True)


def parse_sums(repo: Path) -> Dict[str, str]:
    path = repo / "SHA256SUMS"
    result: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text("utf-8", errors="replace").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                result[parts[1].strip()] = parts[0]
    return result


def sha256(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def major_from_repo(repo: Path) -> int:
    for parent in (repo, *repo.parents):
        if parent.name.startswith("FreeBSD:") and parent.name.endswith(":amd64"):
            return int(parent.name.split(":")[1])
    raise RuntimeError(f"FreeBSD major repo yolundan belirlenemedi: {repo}")


def validate_abi(manifest: dict, major: int, path: Path) -> None:
    arch = str(manifest.get("arch", ""))
    abi = str(manifest.get("abi", ""))
    haystack = f"{arch} {abi}".lower()
    if f"freebsd:{major}:" not in haystack:
        raise RuntimeError(
            f"ABI uyusmazligi {path.name}: hedef FreeBSD:{major}, arch={arch!r}, abi={abi!r}"
        )


def index_source(source: Path, major: int) -> Dict[str, Tuple[Path, dict]]:
    indexed: Dict[str, Tuple[Path, dict]] = {}
    for path in sorted(source.glob("*")):
        if not path.is_file() or path.suffix not in (".pkg", ".txz"):
            continue
        try:
            manifest = compact_manifest(path)
            validate_abi(manifest, major, path)
        except Exception as exc:
            print(f"[SKIP] {path.name}: {exc}")
            continue
        name = str(manifest.get("name", ""))
        if name:
            indexed[name] = (path, manifest)
    return indexed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--source-dir", required=True)
    ap.add_argument("--root", action="append", required=True, dest="roots")
    ap.add_argument("--provenance-out")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    source = Path(args.source_dir).resolve()
    major = major_from_repo(repo)
    catalog = load_catalog(repo)
    source_index = index_source(source, major)

    queue: List[str] = list(args.roots)
    selected: List[str] = []
    unresolved: List[Tuple[str, str]] = []
    seen = set()

    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name in catalog:
            print(f"[LOCAL] {name}-{catalog[name].get('version', '?')}")
            continue
        row = source_index.get(name)
        if row is None:
            unresolved.append(("ROOT", name))
            continue
        path, manifest = row
        selected.append(name)
        deps = manifest.get("deps") or {}
        if isinstance(deps, dict):
            for dep_name in deps:
                dep_name = str(dep_name)
                if dep_name in catalog or dep_name in seen:
                    continue
                if dep_name not in source_index:
                    unresolved.append((name, dep_name))
                else:
                    queue.append(dep_name)

    if unresolved:
        details = ", ".join(f"{owner}->{dep}" for owner, dep in unresolved)
        raise RuntimeError(f"Runtime dependency closure eksik: {details}")

    all_dir = repo / "All"
    all_dir.mkdir(parents=True, exist_ok=True)
    sums = parse_sums(repo)
    provenance = []

    for name in selected:
        src, manifest = source_index[name]
        dst = all_dir / src.name
        shutil.copy2(src, dst)
        digest, size = sha256(dst)
        repopath = f"All/{dst.name}"
        row = dict(manifest)
        row["sum"] = digest
        row["pkgsize"] = size
        row["path"] = repopath
        row["repopath"] = repopath
        catalog[name] = row
        sums[repopath] = digest
        provenance.append(
            {
                "major": major,
                "name": name,
                "version": manifest.get("version"),
                "filename": dst.name,
                "sha256": digest,
                "size": size,
                "arch": manifest.get("arch"),
                "abi": manifest.get("abi"),
                "deps": sorted((manifest.get("deps") or {}).keys()),
            }
        )
        print(f"[ADD] FreeBSD {major}: {name}-{manifest.get('version', '?')} ({dst.name})")

    write_catalog(repo, catalog)
    (repo / "SHA256SUMS").write_text(
        "".join(f"{sums[path]}  {path}\n" for path in sorted(sums)),
        encoding="utf-8",
    )

    if args.provenance_out:
        out = Path(args.provenance_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"[OK] {len(selected)} paket ve gerekli runtime dependency closure repoya eklendi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

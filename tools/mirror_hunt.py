#!/usr/bin/env python3
"""Hunt missing legacy MariaDB/MySQL packages across public FreeBSD mirrors.

This script intentionally never copies packages across FreeBSD major ABIs.  Every
candidate package is opened with bsdtar and +COMPACT_MANIFEST must identify the
same FreeBSD major before it is added to the local static repository.

It is designed to run before sync_db_packages.py.  That script then produces the
final matrix/provenance report and can still use its own fallbacks.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

TARGET_FAMILIES = (
    "mariadb114",
    "mariadb106",
    "mariadb105",
    "mariadb103",
    "mysql56",
    "mysql55",
)

UA = "PvPSunucusu-FreeBSD-Mirror-Hunt/2.0"
PKG_EXTENSIONS = (".pkg", ".txz")

# Historical Nepustil snapshots that are known to contain useful FreeBSD trees.
NEPUSTIL_SNAPSHOTS = {
    11: ("114", "113", "112", "111", "110"),
    12: ("124", "123", "122", "121", "120"),
    13: ("135", "134", "133", "132", "131", "130"),
    14: ("145", "144", "143", "142", "141", "140"),
}

# xTom is especially valuable because it has historically retained old FreeBSD
# package trees.  The other mirrors provide independent copies/fallbacks.
COLON_MIRRORS = (
    ("xTom global", "https://mirrors.xtom.com/freebsd-pkg/{abi}/{branch}/"),
    ("xTom Estonia", "https://mirrors.xtom.ee/freebsd-pkg/{abi}/{branch}/"),
    ("Yandex", "https://mirror.yandex.ru/mirrors/freebsd-pkg/{abi}/{branch}/"),
    ("SGGS", "https://mirror.sg.gs/freebsd-pkg/{abi}/{branch}/"),
    ("OneAsiaHost", "https://mirror.oneasiahost.com/freebsd-pkg/{abi}/{branch}/"),
    ("USTC", "https://mirrors.ustc.edu.cn/freebsd-pkg/{abi}/{branch}/"),
    ("NJU", "https://mirrors.nju.edu.cn/freebsd-pkg/{abi}/{branch}/"),
    ("BJTU", "https://mirror.bjtu.edu.cn/freebsd-pkg/{abi}/{branch}/"),
)

# Aliyun uses FreeBSD/<major>/amd64 instead of the canonical ABI path.
ALIYUN = "https://mirrors.aliyun.com/freebsd-pkg/FreeBSD/{major}/amd64/{branch}/"


@dataclass
class Source:
    label: str
    base_url: str
    entries: Dict[str, str]


def request(url: str, *, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.7)
    raise RuntimeError(f"URL okunamadi: {url}: {last}")


def html_index(base_url: str) -> Dict[str, str]:
    """Read a browsable All/ directory without assuming Apache/nginx format."""
    url = urllib.parse.urljoin(base_url, "All/")
    try:
        raw = request(url).decode("utf-8", "replace")
    except Exception:
        return {}

    found: Dict[str, str] = {}
    for href in re.findall(r'href\s*=\s*["\']([^"\']+)["\']', raw, flags=re.I):
        href = html.unescape(href)
        full = urllib.parse.urljoin(url, href)
        name = urllib.parse.unquote(urllib.parse.urlsplit(full).path.rsplit("/", 1)[-1])
        if name.endswith(PKG_EXTENSIONS):
            found[name] = full
    return found


def extract_packagesite(raw: bytes, suffix: str) -> str:
    """Extract packagesite.yaml using bsdtar (supports xz/zstd pkg archives)."""
    if shutil.which("bsdtar") is None:
        raise RuntimeError("bsdtar bulunamadi")
    fd, tmp_name = tempfile.mkstemp(prefix="packagesite-", suffix=suffix)
    os.close(fd)
    p = Path(tmp_name)
    try:
        p.write_bytes(raw)
        proc = subprocess.run(
            ["bsdtar", "-xOf", str(p), "packagesite.yaml"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            # Some repositories store it as ./packagesite.yaml.
            proc = subprocess.run(
                ["bsdtar", "-xOf", str(p), "./packagesite.yaml"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
        return proc.stdout.decode("utf-8", "replace")
    finally:
        p.unlink(missing_ok=True)


def metadata_index(base_url: str) -> Dict[str, str]:
    """Read package paths from packagesite metadata when All/ listing is blocked."""
    for fn in ("packagesite.pkg", "packagesite.txz", "packagesite.tzst"):
        url = urllib.parse.urljoin(base_url, fn)
        try:
            text = extract_packagesite(request(url, timeout=40), Path(fn).suffix)
        except Exception:
            continue
        found: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = str(obj.get("path") or obj.get("repopath") or "")
            if not path.startswith("All/"):
                continue
            name = path.rsplit("/", 1)[-1]
            if name.endswith(PKG_EXTENSIONS):
                found[name] = urllib.parse.urljoin(base_url, path)
        if found:
            return found
    return {}


def source_entries(base_url: str) -> Dict[str, str]:
    entries = html_index(base_url)
    if entries:
        return entries
    return metadata_index(base_url)


def repo_branches(major: int) -> Tuple[str, ...]:
    # Include release repositories because EOL DB families often survive there
    # long after disappearing from latest/quarterly.
    releases = tuple(f"release_{n}" for n in range(0, 7))
    return ("latest", "quarterly") + releases


def candidate_urls(major: int) -> Iterable[Tuple[str, str]]:
    abi = f"FreeBSD:{major}:amd64"

    # Canonical official repositories first. Their All/ listing may return 403,
    # so source_entries() automatically falls back to packagesite metadata.
    for branch in ("latest", "quarterly") + tuple(f"release_{n}" for n in range(0, 7)):
        yield (f"FreeBSD official {branch}", f"https://pkg.freebsd.org/{abi}/{branch}/")

    # Nepustil current tree and historical snapshots.
    yield ("Nepustil current", f"https://repo.nepustil.net/{abi}/")
    yield ("Nepustil .latest", f"https://repo.nepustil.net/{abi}/.latest/")
    for snap in NEPUSTIL_SNAPSHOTS[major]:
        yield (f"Nepustil snapshot {snap}", f"https://repo.nepustil.net/{snap}/{abi}/")

    # Public FreeBSD package mirrors. xTom comes first among third-party mirrors
    # because its archive retention is the most useful for old package families.
    for label, template in COLON_MIRRORS:
        for branch in repo_branches(major):
            yield (f"{label} {branch}", template.format(abi=abi, branch=branch))

    for branch in ("latest", "quarterly"):
        yield (f"Aliyun {branch}", ALIYUN.format(major=major, branch=branch))


def load_sources(major: int) -> List[Source]:
    sources: List[Source] = []
    seen_urls = set()
    for label, base_url in candidate_urls(major):
        if base_url in seen_urls:
            continue
        seen_urls.add(base_url)
        entries = source_entries(base_url)
        if entries:
            print(f"[MIRROR] FreeBSD {major}: {label}: {len(entries)} paket")
            sources.append(Source(label, base_url, entries))
    return sources


def select_filename(entries: Dict[str, str], package_name: str) -> Optional[str]:
    prefix = package_name + "-"
    matches = [n for n in entries if n.startswith(prefix) and n.endswith(PKG_EXTENSIONS)]
    if not matches:
        return None
    # Prefer the lexicographically newest filename.  ABI validation still gates
    # every candidate before it is accepted.
    return sorted(matches)[-1]


def pair_for(source: Source, family: str) -> Optional[Tuple[str, str]]:
    client = select_filename(source.entries, f"{family}-client")
    server = select_filename(source.entries, f"{family}-server")
    return (client, server) if client and server else None


def read_local_catalog(repo_dir: Path) -> Dict[str, dict]:
    p = repo_dir / "packagesite.txz"
    with tarfile.open(p, "r:xz") as tf:
        members = [m for m in tf.getmembers() if m.isfile() and m.name.endswith("packagesite.yaml")]
        if len(members) != 1:
            raise RuntimeError(f"packagesite.yaml bulunamadi: {p}")
        data = tf.extractfile(members[0]).read().decode("utf-8", "replace")
    out: Dict[str, dict] = {}
    for line in data.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        name = str(obj.get("name", ""))
        if name:
            out[name] = obj
    return out


def write_local_catalog(repo_dir: Path, catalog: Dict[str, dict]) -> None:
    payload = "".join(
        json.dumps(catalog[name], ensure_ascii=False, separators=(",", ":")) + "\n"
        for name in sorted(catalog)
    ).encode("utf-8")
    p = repo_dir / "packagesite.txz"
    fd, tmp_name = tempfile.mkstemp(prefix="packagesite-", suffix=".txz", dir=str(repo_dir))
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
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def download(url: str, dest: Path) -> Tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=90) as r, part.open("wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
        os.replace(part, dest)
        return h.hexdigest(), size
    finally:
        part.unlink(missing_ok=True)


def manifest(path: Path) -> dict:
    proc = subprocess.run(
        ["bsdtar", "-xOf", str(path), "+COMPACT_MANIFEST"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or "manifest okunamadi")
    return json.loads(proc.stdout)


def validate_abi(obj: dict, major: int) -> Tuple[str, str]:
    arch = str(obj.get("arch", ""))
    abi = str(obj.get("abi", ""))
    blob = (arch + " " + abi).lower()
    if f"freebsd:{major}:" not in blob:
        raise RuntimeError(f"ABI uyusmazligi: arch={arch!r}, abi={abi!r}")
    return arch, abi


def repo_manifest(obj: dict, filename: str, digest: str, size: int) -> dict:
    out = dict(obj)
    repopath = f"All/{filename}"
    out["sum"] = digest
    out["pkgsize"] = size
    out["path"] = repopath
    out["repopath"] = repopath
    return out


def family_local(catalog: Dict[str, dict], family: str) -> bool:
    return f"{family}-client" in catalog and f"{family}-server" in catalog


def resolve_any(sources: List[Source], dep_name: str, preferred: Source) -> Optional[Tuple[Source, str, str]]:
    fn = select_filename(preferred.entries, dep_name)
    if fn:
        return preferred, fn, preferred.entries[fn]
    for src in sources:
        if src is preferred:
            continue
        fn = select_filename(src.entries, dep_name)
        if fn:
            return src, fn, src.entries[fn]
    return None


def import_family(
    major: int,
    family: str,
    source: Source,
    pair: Tuple[str, str],
    sources: List[Source],
    repo_dir: Path,
    catalog: Dict[str, dict],
) -> List[Tuple[str, str]]:
    all_dir = repo_dir / "All"
    added: List[Tuple[str, str]] = []
    queue: List[Tuple[Source, str, str, bool]] = [
        (source, pair[0], source.entries[pair[0]], False),
        (source, pair[1], source.entries[pair[1]], False),
    ]
    seen = set()

    while queue:
        src, filename, url, is_dep = queue.pop(0)
        key = (filename, url)
        if key in seen:
            continue
        seen.add(key)

        dest = all_dir / filename
        digest, size = download(url, dest)
        try:
            obj = manifest(dest)
            validate_abi(obj, major)
        except Exception:
            dest.unlink(missing_ok=True)
            raise

        name = str(obj.get("name") or filename)
        version = str(obj.get("version") or "?")
        if is_dep and name in catalog:
            dest.unlink(missing_ok=True)
            continue

        catalog[name] = repo_manifest(obj, filename, digest, size)
        added.append((filename, digest))
        print(f"[ADD] FreeBSD {major}: {name}-{version} <- {src.label}")

        deps = obj.get("deps") or {}
        if isinstance(deps, dict):
            for dep_name in deps:
                dep_name = str(dep_name)
                if dep_name in catalog:
                    continue
                resolved = resolve_any(sources, dep_name, src)
                if resolved:
                    dep_src, dep_fn, dep_url = resolved
                    queue.append((dep_src, dep_fn, dep_url, True))
                else:
                    print(f"[DEP-MISS] FreeBSD {major}: {name} -> {dep_name}")
    return added


def update_sums(repo_dir: Path, added: List[Tuple[str, str]]) -> None:
    p = repo_dir / "SHA256SUMS"
    values: Dict[str, str] = {}
    if p.exists():
        for line in p.read_text("utf-8", errors="replace").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                values[parts[1].strip()] = parts[0]
    for filename, digest in added:
        values[f"All/{filename}"] = digest
    p.write_text("".join(f"{values[k]}  {k}\n" for k in sorted(values)), encoding="utf-8")


def main() -> int:
    root = Path(".").resolve()
    total_added = 0

    for major in (11, 12, 13, 14):
        repo_dir = root / f"FreeBSD:{major}:amd64" / "latest"
        catalog = read_local_catalog(repo_dir)
        missing = [f for f in TARGET_FAMILIES if not family_local(catalog, f)]
        if not missing:
            print(f"[OK] FreeBSD {major}: hedef DB ailelerinin tamami zaten mevcut")
            continue

        print(f"[HUNT] FreeBSD {major}: eksikler: {', '.join(missing)}")
        sources = load_sources(major)
        print(f"[HUNT] FreeBSD {major}: {len(sources)} kullanilabilir repo agaci bulundu")
        added_for_major: List[Tuple[str, str]] = []

        for family in missing:
            if family_local(catalog, family):
                continue
            success = False
            for src in sources:
                pair = pair_for(src, family)
                if not pair:
                    continue
                try:
                    added = import_family(major, family, src, pair, sources, repo_dir, catalog)
                except Exception as exc:
                    print(f"[REJECT] FreeBSD {major}: {family} / {src.label}: {exc}")
                    # Pair files may be left from a partial attempt; remove only those.
                    for fn in pair:
                        (repo_dir / "All" / fn).unlink(missing_ok=True)
                    continue
                if family_local(catalog, family):
                    added_for_major.extend(added)
                    total_added += len(added)
                    success = True
                    print(f"[FOUND] FreeBSD {major}: {family} <- {src.label}")
                    break
            if not success:
                print(f"[NOT-FOUND] FreeBSD {major}: {family} hicbir taranan repoda bulunamadi")

        if added_for_major:
            write_local_catalog(repo_dir, catalog)
            update_sums(repo_dir, added_for_major)

    print(f"[DONE] mirror hunt tamamlandi; eklenen paket/dependency sayisi: {total_added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

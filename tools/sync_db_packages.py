#!/usr/bin/env python3
"""Synchronize legacy MariaDB/MySQL packages into the static FreeBSD repositories.

Policy:
- Never copy a package across FreeBSD major ABIs.
- Prefer official/current package directories, then historical archive mirrors.
- A package is accepted only when +COMPACT_MANIFEST reports the requested FreeBSD ABI.
- Existing package names are left untouched; missing direct dependencies are pulled from
  the same snapshot when possible.
- Legacy community fallbacks are checksum-pinned and ABI-validated before use.

This tool updates packagesite.txz and SHA256SUMS without requiring a FreeBSD host.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
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

SNAPSHOTS = {
    11: ("114", "113", "112"),
    12: ("123", "121"),
    13: ("132", "131", "130"),
    14: ("144", "143", "142", "141", "140"),
}

# The MySQL 5.6 fallback is intentionally checksum pinned. It is used only when
# no archive mirror contains a same-ABI package, and the embedded package ABI
# still has to match the target major.
MYSQL56_COMMUNITY = {
    "client": {
        "url": "https://github.com/seyfooksck/mysql56-freebsd-pkg/releases/download/v1.0/mysql56-client-5.6.51.pkg",
        "sha256": "6cd06878b9a6a14df165abf1f3d45f5de3e5b0786fb15c26293c55484c20af91",
    },
    "server": {
        "url": "https://github.com/seyfooksck/mysql56-freebsd-pkg/releases/download/v1.0/mysql56-server-5.6.51.pkg",
        "sha256": "80563f5c35169d014c89c053962ca69fe41a03cc5b881e3f86bca42357d95c62",
    },
}

UA = "PvPSunucusu-FreeBSD-DB-Sync/1.0"
PKG_EXTENSIONS = (".pkg", ".txz")


@dataclass
class Source:
    label: str
    base_url: str
    entries: Dict[str, str]


@dataclass
class Imported:
    major: int
    name: str
    version: str
    filename: str
    source: str
    source_url: str
    sha256: str
    size: int
    arch: str
    abi: str
    dependency: bool


def request(url: str, *, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"URL okunamadi: {url}: {last}")


def list_index(url: str) -> Dict[str, str]:
    try:
        raw = request(url).decode("utf-8", "replace")
    except Exception as exc:
        print(f"[WARN] indeks okunamadi: {url}: {exc}")
        return {}

    found: Dict[str, str] = {}
    for href in re.findall(r'href=["\']([^"\']+)["\']', raw, flags=re.I):
        href = html.unescape(href)
        full = urllib.parse.urljoin(url, href)
        name = urllib.parse.unquote(urllib.parse.urlsplit(full).path.rsplit("/", 1)[-1])
        if name.endswith(PKG_EXTENSIONS):
            found[name] = full
    return found


def sources_for_major(major: int) -> List[Tuple[str, str]]:
    abi = f"FreeBSD:{major}:amd64"
    out: List[Tuple[str, str]] = [
        ("FreeBSD official latest", f"https://pkg.freebsd.org/{abi}/latest/All/"),
        ("Nepustil archive current", f"https://repo.nepustil.net/{abi}/All/"),
    ]
    for snap in SNAPSHOTS[major]:
        out.append((f"Nepustil snapshot {snap}", f"https://repo.nepustil.net/{snap}/{abi}/All/"))
    out.extend(
        [
            ("SGGS archive latest", f"https://mirror.sg.gs/freebsd-pkg/{abi}/latest/All/"),
            ("SGGS archive quarterly", f"https://mirror.sg.gs/freebsd-pkg/{abi}/quarterly/All/"),
        ]
    )
    return out


def load_sources(major: int) -> List[Source]:
    result: List[Source] = []
    for label, url in sources_for_major(major):
        entries = list_index(url)
        if entries:
            print(f"[OK] {major}: {label}: {len(entries)} paket")
            result.append(Source(label, url, entries))
    return result


def select_filename(entries: Dict[str, str], package_name: str) -> Optional[str]:
    prefix = package_name + "-"
    matches = [n for n in entries if n.startswith(prefix) and n.endswith(PKG_EXTENSIONS)]
    if not matches:
        return None
    # An archive snapshot normally contains one version per package name. Sorting
    # is only a deterministic tie breaker when a mirror contains more than one.
    return sorted(matches)[-1]


def family_pair(source: Source, family: str) -> Optional[Tuple[str, str]]:
    client = select_filename(source.entries, f"{family}-client")
    server = select_filename(source.entries, f"{family}-server")
    if client and server:
        return client, server
    return None


def read_packagesite(repo_dir: Path) -> Dict[str, dict]:
    site = repo_dir / "packagesite.txz"
    if not site.exists():
        raise RuntimeError(f"packagesite.txz yok: {site}")
    with tarfile.open(site, "r:xz") as tf:
        members = [m for m in tf.getmembers() if m.isfile() and m.name.endswith("packagesite.yaml")]
        if not members:
            raise RuntimeError(f"packagesite.yaml bulunamadi: {site}")
        fh = tf.extractfile(members[0])
        if fh is None:
            raise RuntimeError(f"packagesite.yaml okunamadi: {site}")
        text = fh.read().decode("utf-8", "replace")
    catalog: Dict[str, dict] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{site}: satir {lineno} JSON degil: {exc}") from exc
        name = str(obj.get("name", ""))
        if name:
            catalog[name] = obj
    return catalog


def write_packagesite(repo_dir: Path, catalog: Dict[str, dict]) -> None:
    site = repo_dir / "packagesite.txz"
    payload = "".join(
        json.dumps(catalog[name], ensure_ascii=False, separators=(",", ":")) + "\n"
        for name in sorted(catalog)
    ).encode("utf-8")

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
        os.replace(tmp, site)
    finally:
        if tmp.exists():
            tmp.unlink()


def extract_compact_manifest(path: Path) -> dict:
    if shutil.which("bsdtar") is None:
        raise RuntimeError("bsdtar bulunamadi; Ubuntu'da libarchive-tools kurulmalidir")
    proc = subprocess.run(
        ["bsdtar", "-xOf", str(path), "+COMPACT_MANIFEST"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"+COMPACT_MANIFEST okunamadi: {path.name}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"+COMPACT_MANIFEST JSON degil: {path.name}: {exc}") from exc


def validate_abi(manifest: dict, major: int, *, strict: bool = True) -> Tuple[str, str]:
    arch = str(manifest.get("arch", ""))
    abi = str(manifest.get("abi", ""))
    blob = (arch + " " + abi).lower()
    expected = f"freebsd:{major}:"
    if expected not in blob:
        if strict:
            raise RuntimeError(
                f"ABI uyusmazligi: beklenen FreeBSD:{major}, paket arch={arch!r} abi={abi!r}"
            )
    return arch, abi


def stream_download(url: str, dest: Path, expected_sha256: Optional[str] = None) -> Tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        part.unlink()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=90) as r, part.open("wb") as out:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
        digest = h.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise RuntimeError(
                f"SHA256 uyusmadi: {dest.name}: beklenen {expected_sha256}, gelen {digest}"
            )
        os.replace(part, dest)
        return digest, size
    finally:
        if part.exists():
            part.unlink()


def update_sha256sums(repo_dir: Path, imported: Iterable[Imported]) -> None:
    sums_path = repo_dir / "SHA256SUMS"
    values: Dict[str, str] = {}
    if sums_path.exists():
        for line in sums_path.read_text("utf-8", errors="replace").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                values[parts[1].strip()] = parts[0]
    for item in imported:
        if item.major == int(repo_dir.parent.parent.name.split(":")[1]):
            values[f"All/{item.filename}"] = item.sha256
    text = "".join(f"{values[path]}  {path}\n" for path in sorted(values))
    sums_path.write_text(text, encoding="utf-8")


def repo_manifest(compact: dict, filename: str, sha256: str, size: int) -> dict:
    obj = dict(compact)
    repopath = f"All/{filename}"
    obj["sum"] = sha256
    obj["pkgsize"] = size
    obj["path"] = repopath
    obj["repopath"] = repopath
    return obj


def resolve_dep(source: Source, dep_name: str) -> Optional[Tuple[str, str]]:
    filename = select_filename(source.entries, dep_name)
    if not filename:
        return None
    return filename, source.entries[filename]


def package_name_version(manifest: dict, fallback_name: str) -> Tuple[str, str]:
    return str(manifest.get("name") or fallback_name), str(manifest.get("version") or "?")


def import_from_indexed_source(
    major: int,
    family: str,
    source: Source,
    pair: Tuple[str, str],
    repo_dir: Path,
    catalog: Dict[str, dict],
    provenance: List[Imported],
    unresolved: List[dict],
) -> List[Imported]:
    all_dir = repo_dir / "All"
    all_dir.mkdir(parents=True, exist_ok=True)
    added: List[Imported] = []
    queue: List[Tuple[str, str, bool]] = []
    for filename in pair:
        queue.append((filename, source.entries[filename], False))

    seen_files = set()
    while queue:
        filename, url, is_dep = queue.pop(0)
        if filename in seen_files:
            continue
        seen_files.add(filename)
        dest = all_dir / filename
        digest, size = stream_download(url, dest)
        try:
            manifest = extract_compact_manifest(dest)
            arch, abi = validate_abi(manifest, major)
        except Exception:
            dest.unlink(missing_ok=True)
            raise

        name, version = package_name_version(manifest, filename)
        if is_dep and name in catalog:
            # We discovered this through another dependency path after it was
            # already satisfied. Do not replace the repository's coherent package.
            dest.unlink(missing_ok=True)
            continue

        item = Imported(
            major=major,
            name=name,
            version=version,
            filename=filename,
            source=source.label,
            source_url=url,
            sha256=digest,
            size=size,
            arch=arch,
            abi=abi,
            dependency=is_dep,
        )
        added.append(item)
        provenance.append(item)
        catalog[name] = repo_manifest(manifest, filename, digest, size)
        print(f"[ADD] FreeBSD {major}: {name}-{version} <- {source.label}")

        deps = manifest.get("deps") or {}
        if isinstance(deps, dict):
            for dep_name in deps.keys():
                dep_name = str(dep_name)
                if dep_name in catalog:
                    continue
                resolved = resolve_dep(source, dep_name)
                if resolved:
                    queue.append((resolved[0], resolved[1], True))
                else:
                    unresolved.append(
                        {
                            "major": major,
                            "family": family,
                            "package": name,
                            "dependency": dep_name,
                            "source": source.label,
                        }
                    )
    return added


def import_mysql56_community(
    major: int,
    repo_dir: Path,
    catalog: Dict[str, dict],
    provenance: List[Imported],
) -> Optional[List[Imported]]:
    all_dir = repo_dir / "All"
    all_dir.mkdir(parents=True, exist_ok=True)
    staged: List[Tuple[Path, dict, str, int, str, str, str]] = []
    try:
        for component in ("client", "server"):
            cfg = MYSQL56_COMMUNITY[component]
            filename = urllib.parse.unquote(urllib.parse.urlsplit(cfg["url"]).path.rsplit("/", 1)[-1])
            dest = all_dir / filename
            digest, size = stream_download(cfg["url"], dest, cfg["sha256"])
            manifest = extract_compact_manifest(dest)
            arch, abi = validate_abi(manifest, major)
            name, version = package_name_version(manifest, f"mysql56-{component}")
            staged.append((dest, manifest, digest, size, arch, abi, name))
    except Exception as exc:
        print(f"[WARN] FreeBSD {major}: MySQL56 community fallback kabul edilmedi: {exc}")
        for row in staged:
            row[0].unlink(missing_ok=True)
        # The file that failed validation may not be in staged yet.
        for component in ("client", "server"):
            filename = urllib.parse.urlsplit(MYSQL56_COMMUNITY[component]["url"]).path.rsplit("/", 1)[-1]
            (all_dir / filename).unlink(missing_ok=True)
        return None

    added: List[Imported] = []
    for dest, manifest, digest, size, arch, abi, name in staged:
        version = str(manifest.get("version") or "?")
        item = Imported(
            major=major,
            name=name,
            version=version,
            filename=dest.name,
            source="Pinned GitHub community fallback",
            source_url=MYSQL56_COMMUNITY["client" if "client" in name else "server"]["url"],
            sha256=digest,
            size=size,
            arch=arch,
            abi=abi,
            dependency=False,
        )
        catalog[name] = repo_manifest(manifest, dest.name, digest, size)
        provenance.append(item)
        added.append(item)
        print(f"[ADD] FreeBSD {major}: {name}-{version} <- pinned community fallback")
    return added


def family_local(catalog: Dict[str, dict], family: str) -> bool:
    return f"{family}-client" in catalog and f"{family}-server" in catalog


def family_versions(catalog: Dict[str, dict], family: str) -> str:
    c = catalog.get(f"{family}-client", {}).get("version")
    s = catalog.get(f"{family}-server", {}).get("version")
    if c and s:
        return str(c) if c == s else f"client={c}, server={s}"
    return "-"


def render_report(matrix: Dict[int, Dict[str, dict]], unresolved: List[dict]) -> str:
    lines = [
        "# Database Package Matrix\n\n",
        "Bu dosya `tools/sync_db_packages.py` tarafindan uretilir. Paketler FreeBSD major ABI'leri arasinda kopyalanmaz.\n\n",
        "| FreeBSD | MariaDB 11.4 | MariaDB 10.6 | MariaDB 10.5 | MariaDB 10.3 | MySQL 5.6 | MySQL 5.5 |\n",
        "|---:|---|---|---|---|---|---|\n",
    ]
    labels = {
        "mariadb114": "MariaDB 11.4",
        "mariadb106": "MariaDB 10.6",
        "mariadb105": "MariaDB 10.5",
        "mariadb103": "MariaDB 10.3",
        "mysql56": "MySQL 5.6",
        "mysql55": "MySQL 5.5",
    }
    for major in (11, 12, 13, 14):
        cells = []
        for family in TARGET_FAMILIES:
            d = matrix[major][family]
            status = d["status"]
            version = d.get("version", "-")
            source = d.get("source", "")
            if status == "local":
                cells.append(f"✅ {version}")
            elif status == "imported":
                cells.append(f"✅ {version}<br><sub>{source}</sub>")
            else:
                cells.append("❌ same-ABI paket bulunamadi")
        lines.append(f"| {major} | " + " | ".join(cells) + " |\n")

    lines.append("\n## Kaynak ve guvenlik politikasi\n\n")
    lines.append(
        "- Resmi FreeBSD paket dizini once denenir; EOL surumlerde Nepustil ve SGGS tarihsel FreeBSD paket arsivleri kullanilir.\n"
    )
    lines.append(
        "- Her indirilen paketin `+COMPACT_MANIFEST` ABI degeri hedef FreeBSD major surumuyle eslesmeden repoya alinmaz.\n"
    )
    lines.append(
        "- MySQL 5.6 icin arsiv bulunamazsa yalniz SHA256'si sabitlenmis community paketi denenir; ABI uymuyorsa otomatik reddedilir.\n"
    )
    lines.append(
        "- `❌` olan kombinasyonlar baska FreeBSD major paketini zorla kopyalamak yerine o ABI icin source/ports build gerektirir.\n"
    )
    if unresolved:
        lines.append("\n## Ayni snapshotta bulunamayan bagimliliklar\n\n")
        for d in unresolved:
            lines.append(
                f"- FreeBSD {d['major']} / {d['family']}: `{d['package']}` -> `{d['dependency']}` ({d['source']})\n"
            )
    return "".join(lines)


def serialize_provenance(items: List[Imported], unresolved: List[dict]) -> dict:
    return {
        "generated_by": "tools/sync_db_packages.py",
        "policy": "same FreeBSD major ABI only; archive packages validated through +COMPACT_MANIFEST",
        "packages": [item.__dict__ for item in items],
        "unresolved_dependencies": unresolved,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--import-packages", action="store_true", help="download missing packages")
    ap.add_argument("--allow-community", action="store_true", help="allow checksum-pinned MySQL56 fallback")
    ap.add_argument("--report", default="DB_PACKAGE_MATRIX.md")
    ap.add_argument("--provenance", default="DB_PACKAGE_SOURCES.json")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    provenance: List[Imported] = []
    unresolved: List[dict] = []
    matrix: Dict[int, Dict[str, dict]] = {m: {} for m in (11, 12, 13, 14)}

    for major in (11, 12, 13, 14):
        repo_dir = root / f"FreeBSD:{major}:amd64" / "latest"
        catalog = read_packagesite(repo_dir)
        sources: Optional[List[Source]] = None
        imported_for_major: List[Imported] = []

        for family in TARGET_FAMILIES:
            if family_local(catalog, family):
                matrix[major][family] = {
                    "status": "local",
                    "version": family_versions(catalog, family),
                    "source": "repository",
                }
                print(f"[LOCAL] FreeBSD {major}: {family} {family_versions(catalog, family)}")
                continue

            if not args.import_packages:
                matrix[major][family] = {"status": "missing", "version": "-", "source": ""}
                continue

            if sources is None:
                sources = load_sources(major)

            chosen: Optional[Tuple[Source, Tuple[str, str]]] = None
            for source in sources:
                pair = family_pair(source, family)
                if pair:
                    chosen = (source, pair)
                    break

            added: Optional[List[Imported]] = None
            chosen_source = ""
            if chosen:
                source, pair = chosen
                chosen_source = source.label
                try:
                    added = import_from_indexed_source(
                        major, family, source, pair, repo_dir, catalog, provenance, unresolved
                    )
                except Exception as exc:
                    print(f"[WARN] FreeBSD {major}: {family} arsiv paketi reddedildi: {exc}")
                    # Remove only pair files that may have been left behind after a partial failure.
                    for fn in pair:
                        (repo_dir / "All" / fn).unlink(missing_ok=True)
                    added = None

            if not added and family == "mysql56" and args.allow_community:
                added = import_mysql56_community(major, repo_dir, catalog, provenance)
                if added:
                    chosen_source = "Pinned GitHub community fallback"

            if added and family_local(catalog, family):
                imported_for_major.extend(added)
                matrix[major][family] = {
                    "status": "imported",
                    "version": family_versions(catalog, family),
                    "source": chosen_source,
                }
            else:
                matrix[major][family] = {"status": "missing", "version": "-", "source": ""}
                print(f"[MISS] FreeBSD {major}: {family}: same-ABI client/server bulunamadi")

        if imported_for_major:
            write_packagesite(repo_dir, catalog)
            update_sha256sums(repo_dir, imported_for_major)

    report_path = root / args.report
    report_path.write_text(render_report(matrix, unresolved), encoding="utf-8")
    prov_path = root / args.provenance
    prov_path.write_text(
        json.dumps(serialize_provenance(provenance, unresolved), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    missing = [f"{major}:{fam}" for major in matrix for fam, d in matrix[major].items() if d["status"] == "missing"]
    if missing:
        print("[INFO] Source-build gereken kombinasyonlar: " + ", ".join(missing))
    else:
        print("[OK] Tum hedef database paketleri FreeBSD 11-14 icin mevcut.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

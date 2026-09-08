#!/usr/bin/env python3
"""Hunt legacy MariaDB/MySQL packages across public FreeBSD package mirrors.

Rules:
* Never copy a package across FreeBSD major ABIs.
* Validate every downloaded package through +COMPACT_MANIFEST.
* Search browsable All/ trees and packagesite metadata (pkg/txz/tzst).
* Search historical release_N trees, not only latest/quarterly.
* Import runtime dependency closure from the same FreeBSD major.

This tool runs before sync_db_packages.py and fills everything that still exists
as a binary package on public mirrors. Truly absent combinations are left for
native/source builds; they are never faked with a wrong ABI package.
"""

from __future__ import annotations

import concurrent.futures
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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

TARGET_FAMILIES = (
    "mariadb114", "mariadb106", "mariadb105", "mariadb103", "mysql56", "mysql55",
)
PKG_EXTENSIONS = (".pkg", ".txz")
UA = "PvPSunucusu-FreeBSD-Mirror-Hunt/3.0"

NEPUSTIL_SNAPSHOTS = {
    11: ("114", "113", "112", "111", "110"),
    12: ("124", "123", "122", "121", "120"),
    13: ("135", "134", "133", "132", "131", "130"),
    14: ("145", "144", "143", "142", "141", "140"),
}

COLON_MIRRORS = (
    ("xTom US", "https://mirrors.xtom.com/freebsd-pkg/{abi}/{branch}/"),
    ("xTom EE", "https://mirrors.xtom.ee/freebsd-pkg/{abi}/{branch}/"),
    ("xTom DE", "https://mirrors.xtom.de/freebsd-pkg/{abi}/{branch}/"),
    ("xTom NL", "https://mirrors.xtom.nl/freebsd-pkg/{abi}/{branch}/"),
    ("xTom HK", "https://mirrors.xtom.hk/freebsd-pkg/{abi}/{branch}/"),
    ("xTom SG", "https://mirrors.xtom.sg/freebsd-pkg/{abi}/{branch}/"),
    ("xTom JP", "https://mirrors.xtom.jp/freebsd-pkg/{abi}/{branch}/"),
    ("xTom AU", "https://mirrors.xtom.au/freebsd-pkg/{abi}/{branch}/"),
    ("Yandex", "https://mirror.yandex.ru/mirrors/freebsd-pkg/{abi}/{branch}/"),
    ("SGGS", "https://mirror.sg.gs/freebsd-pkg/{abi}/{branch}/"),
    ("OneAsiaHost", "https://mirror.oneasiahost.com/freebsd-pkg/{abi}/{branch}/"),
    ("USTC", "https://mirrors.ustc.edu.cn/freebsd-pkg/{abi}/{branch}/"),
    ("NJU", "https://mirrors.nju.edu.cn/freebsd-pkg/{abi}/{branch}/"),
    ("BJTU", "https://mirror.bjtu.edu.cn/freebsd-pkg/{abi}/{branch}/"),
    ("Debian CN legacy", "http://ftp.cn.debian.org/freebsd-pkg/{abi}/{branch}/"),
)
ALIYUN = "https://mirrors.aliyun.com/freebsd-pkg/FreeBSD/{major}/amd64/{branch}/"


@dataclass
class Source:
    label: str
    base_url: str
    entries: Dict[str, str]


def request(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise RuntimeError(f"{url}: {exc}") from exc


def html_index(base_url: str) -> Dict[str, str]:
    url = urllib.parse.urljoin(base_url, "All/")
    try:
        raw = request(url).decode("utf-8", "replace")
    except Exception:
        return {}
    found: Dict[str, str] = {}
    for href in re.findall(r'href\s*=\s*["\']([^"\']+)["\']', raw, flags=re.I):
        full = urllib.parse.urljoin(url, html.unescape(href))
        name = urllib.parse.unquote(urllib.parse.urlsplit(full).path.rsplit("/", 1)[-1])
        if name.endswith(PKG_EXTENSIONS):
            found[name] = full
    return found


def extract_packagesite(raw: bytes, suffix: str) -> str:
    if not shutil.which("bsdtar"):
        raise RuntimeError("bsdtar bulunamadi")
    fd, tmp_name = tempfile.mkstemp(prefix="packagesite-", suffix=suffix)
    os.close(fd)
    p = Path(tmp_name)
    try:
        p.write_bytes(raw)
        for member in ("packagesite.yaml", "./packagesite.yaml"):
            proc = subprocess.run(["bsdtar", "-xOf", str(p), member], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout.decode("utf-8", "replace")
        raise RuntimeError("packagesite.yaml acilamadi")
    finally:
        p.unlink(missing_ok=True)


def metadata_index(base_url: str) -> Dict[str, str]:
    for fn in ("packagesite.pkg", "packagesite.txz", "packagesite.tzst"):
        try:
            text = extract_packagesite(request(urllib.parse.urljoin(base_url, fn), 20), Path(fn).suffix)
        except Exception:
            continue
        found: Dict[str, str] = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = str(obj.get("path") or obj.get("repopath") or "")
            if path.startswith("All/"):
                name = path.rsplit("/", 1)[-1]
                if name.endswith(PKG_EXTENSIONS):
                    found[name] = urllib.parse.urljoin(base_url, path)
        if found:
            return found
    return {}


def source_entries(base_url: str) -> Dict[str, str]:
    entries = html_index(base_url)
    return entries if entries else metadata_index(base_url)


def branches() -> Tuple[str, ...]:
    return ("latest", "quarterly") + tuple(f"release_{n}" for n in range(0, 9))


def candidate_urls(major: int) -> Iterable[Tuple[str, str]]:
    abi = f"FreeBSD:{major}:amd64"
    for branch in branches():
        yield f"FreeBSD official {branch}", f"https://pkg.freebsd.org/{abi}/{branch}/"
    yield "Nepustil current", f"https://repo.nepustil.net/{abi}/"
    yield "Nepustil .latest", f"https://repo.nepustil.net/{abi}/.latest/"
    for snap in NEPUSTIL_SNAPSHOTS[major]:
        yield f"Nepustil snapshot {snap}", f"https://repo.nepustil.net/{snap}/{abi}/"
    for label, template in COLON_MIRRORS:
        for branch in branches():
            yield f"{label} {branch}", template.format(abi=abi, branch=branch)
    for branch in branches():
        yield f"Aliyun {branch}", ALIYUN.format(major=major, branch=branch)
    if major == 11:
        yield "OPNsense legacy root", f"https://pkg.opnsense.org/{abi}/"
        for branch in branches():
            yield f"OPNsense legacy {branch}", f"https://pkg.opnsense.org/{abi}/{branch}/"


def _load_one(item: Tuple[str, str]) -> Optional[Source]:
    label, url = item
    entries = source_entries(url)
    return Source(label, url, entries) if entries else None


def load_sources(major: int) -> List[Source]:
    seen = set(); candidates = []
    for item in candidate_urls(major):
        if item[1] not in seen:
            seen.add(item[1]); candidates.append(item)
    sources: List[Source] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        futures = {pool.submit(_load_one, item): item for item in candidates}
        for fut in concurrent.futures.as_completed(futures):
            try:
                src = fut.result()
            except Exception:
                src = None
            if src:
                print(f"[MIRROR] FreeBSD {major}: {src.label}: {len(src.entries)} paket", flush=True)
                sources.append(src)
    priority = {"FreeBSD": 0, "Nepustil": 1, "SGGS": 2, "xTom": 3, "Yandex": 4}
    sources.sort(key=lambda s: (next((v for k, v in priority.items() if s.label.startswith(k)), 9), s.label))
    return sources


def select_filename(entries: Dict[str, str], package_name: str) -> Optional[str]:
    prefix = package_name + "-"
    matches = [x for x in entries if x.startswith(prefix) and x.endswith(PKG_EXTENSIONS)]
    return sorted(matches)[-1] if matches else None


def pair_for(source: Source, family: str) -> Optional[Tuple[str, str]]:
    c = select_filename(source.entries, f"{family}-client")
    s = select_filename(source.entries, f"{family}-server")
    return (c, s) if c and s else None


def read_catalog(repo_dir: Path) -> Dict[str, dict]:
    p = repo_dir / "packagesite.txz"
    with tarfile.open(p, "r:xz") as tf:
        m = next((m for m in tf.getmembers() if m.isfile() and m.name.endswith("packagesite.yaml")), None)
        if m is None:
            raise RuntimeError(f"packagesite.yaml yok: {p}")
        text = tf.extractfile(m).read().decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        if line.strip():
            obj = json.loads(line)
            if obj.get("name"):
                out[str(obj["name"])] = obj
    return out


def write_catalog(repo_dir: Path, catalog: Dict[str, dict]) -> None:
    payload = "".join(json.dumps(catalog[n], ensure_ascii=False, separators=(",", ":")) + "\n" for n in sorted(catalog)).encode()
    fd, tmp_name = tempfile.mkstemp(prefix="packagesite-", suffix=".txz", dir=str(repo_dir)); os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tarfile.open(tmp, "w:xz", format=tarfile.PAX_FORMAT) as tf:
            info = tarfile.TarInfo("packagesite.yaml")
            info.size = len(payload); info.mode = 0o644; info.uid = 0; info.gid = 0; info.uname = "root"; info.gname = "wheel"; info.mtime = 0
            tf.addfile(info, io.BytesIO(payload))
        os.replace(tmp, repo_dir / "packagesite.txz")
    finally:
        tmp.unlink(missing_ok=True)


def download(url: str, dest: Path) -> Tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part"); part.unlink(missing_ok=True)
    h = hashlib.sha256(); size = 0; req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as r, part.open("wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk: break
                f.write(chunk); h.update(chunk); size += len(chunk)
        os.replace(part, dest); return h.hexdigest(), size
    finally:
        part.unlink(missing_ok=True)


def compact_manifest(path: Path) -> dict:
    proc = subprocess.run(["bsdtar", "-xOf", str(path), "+COMPACT_MANIFEST"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode or not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or "manifest okunamadi")
    return json.loads(proc.stdout)


def validate_abi(obj: dict, major: int) -> None:
    arch = str(obj.get("arch", "")); abi = str(obj.get("abi", ""))
    if f"freebsd:{major}:" not in (arch + " " + abi).lower():
        raise RuntimeError(f"ABI uyusmazligi: arch={arch!r} abi={abi!r}")


def repo_manifest(obj: dict, filename: str, digest: str, size: int) -> dict:
    out = dict(obj); path = f"All/{filename}"
    out.update({"sum": digest, "pkgsize": size, "path": path, "repopath": path}); return out


def local(catalog: Dict[str, dict], family: str) -> bool:
    return f"{family}-client" in catalog and f"{family}-server" in catalog


def resolve_dep(sources: List[Source], dep: str, preferred: Source) -> Optional[Tuple[Source, str, str]]:
    for src in [preferred] + [x for x in sources if x is not preferred]:
        fn = select_filename(src.entries, dep)
        if fn: return src, fn, src.entries[fn]
    return None


def import_family(major: int, family: str, src: Source, pair: Tuple[str, str], sources: List[Source], repo_dir: Path, catalog: Dict[str, dict]) -> List[Tuple[str, str]]:
    all_dir = repo_dir / "All"
    queue = [(src, pair[0], src.entries[pair[0]], False), (src, pair[1], src.entries[pair[1]], False)]
    seen = set(); added: List[Tuple[str, str]] = []
    while queue:
        cur_src, fn, url, is_dep = queue.pop(0)
        if (fn, url) in seen: continue
        seen.add((fn, url)); dest = all_dir / fn
        digest, size = download(url, dest)
        try:
            obj = compact_manifest(dest); validate_abi(obj, major)
        except Exception:
            dest.unlink(missing_ok=True); raise
        name = str(obj.get("name") or fn); version = str(obj.get("version") or "?")
        if is_dep and name in catalog:
            dest.unlink(missing_ok=True); continue
        catalog[name] = repo_manifest(obj, fn, digest, size); added.append((fn, digest))
        print(f"[ADD] FreeBSD {major}: {name}-{version} <- {cur_src.label}", flush=True)
        deps = obj.get("deps") or {}
        if isinstance(deps, dict):
            for dep in deps:
                dep = str(dep)
                if dep in catalog: continue
                resolved = resolve_dep(sources, dep, cur_src)
                if resolved:
                    ds, dfn, du = resolved; queue.append((ds, dfn, du, True))
                else:
                    print(f"[DEP-MISS] FreeBSD {major}: {name} -> {dep}", flush=True)
    return added


def update_sums(repo_dir: Path, added: List[Tuple[str, str]]) -> None:
    p = repo_dir / "SHA256SUMS"; values: Dict[str, str] = {}
    if p.exists():
        for line in p.read_text("utf-8", errors="replace").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2: values[parts[1].strip()] = parts[0]
    for fn, digest in added: values[f"All/{fn}"] = digest
    p.write_text("".join(f"{values[k]}  {k}\n" for k in sorted(values)), encoding="utf-8")


def main() -> int:
    root = Path(".").resolve(); total = 0
    for major in (11, 12, 13, 14):
        repo_dir = root / f"FreeBSD:{major}:amd64" / "latest"; catalog = read_catalog(repo_dir)
        missing = [f for f in TARGET_FAMILIES if not local(catalog, f)]
        if not missing:
            print(f"[OK] FreeBSD {major}: tum hedef DB aileleri mevcut"); continue
        print(f"[HUNT] FreeBSD {major}: eksikler: {', '.join(missing)}", flush=True)
        sources = load_sources(major)
        print(f"[HUNT] FreeBSD {major}: {len(sources)} kullanilabilir repo agaci bulundu", flush=True)
        added_major: List[Tuple[str, str]] = []
        for family in missing:
            if local(catalog, family): continue
            found = False
            for src in sources:
                pair = pair_for(src, family)
                if not pair: continue
                try:
                    added = import_family(major, family, src, pair, sources, repo_dir, catalog)
                except Exception as exc:
                    print(f"[REJECT] FreeBSD {major}: {family} / {src.label}: {exc}", flush=True)
                    for fn in pair: (repo_dir / "All" / fn).unlink(missing_ok=True)
                    continue
                if local(catalog, family):
                    added_major.extend(added); total += len(added); found = True
                    print(f"[FOUND] FreeBSD {major}: {family} <- {src.label}", flush=True); break
            if not found:
                print(f"[NOT-FOUND] FreeBSD {major}: {family} taranan binary repolarda yok", flush=True)
        if added_major:
            write_catalog(repo_dir, catalog); update_sums(repo_dir, added_major)
    print(f"[DONE] mirror hunt tamamlandi; eklenen paket/dependency: {total}", flush=True); return 0


if __name__ == "__main__":
    raise SystemExit(main())

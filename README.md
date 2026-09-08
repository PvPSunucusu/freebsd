# FreeBSD 9–14 Legacy Package Repository (amd64 only)

Static selected package repositories for FreeBSD 9, 10, 11, 12, 13 and 14 **amd64**. i386/x32 is not maintained. Packages are never copied across FreeBSD major versions or ABIs.

## pkg configuration

Create `/usr/local/etc/pkg/repos/FreeBSD.conf`:

```conf
FreeBSD: {
  url: "https://raw.githubusercontent.com/PvPSunucusu/freebsd/main/${ABI}/latest",
  mirror_type: "none",
  signature_type: "none",
  enabled: yes
}
```

Then run:

```sh
pkg update -f
```

Supported ABIs:

- `FreeBSD:9:amd64`
- `FreeBSD:10:amd64`
- `FreeBSD:11:amd64`
- `FreeBSD:12:amd64`
- `FreeBSD:13:amd64`
- `FreeBSD:14:amd64`

Each ABI directory contains a single canonical `latest/` repository. `latest/` contains package payloads in `All/`, checksums, and pkg repository metadata.

Package versions remain coherent with the verified snapshot used for that FreeBSD major. Exact versions can differ between majors; historical package snapshots are not mixed merely to force a version number.

## Database package matrix (FreeBSD 11–14)

The repository maintenance workflow explicitly tracks these database families for FreeBSD 11, 12, 13 and 14 amd64:

- MariaDB 11.4 (`mariadb114-client`, `mariadb114-server`)
- MariaDB 10.6 (`mariadb106-client`, `mariadb106-server`)
- MariaDB 10.5 (`mariadb105-client`, `mariadb105-server`)
- MariaDB 10.3 (`mariadb103-client`, `mariadb103-server`)
- MySQL 5.6 (`mysql56-client`, `mysql56-server`)
- MySQL 5.5 (`mysql55-client`, `mysql55-server`)

`tools/run_db_sync.py` checks the existing repository first, then looks for missing packages in the matching FreeBSD ABI from the official package service and historical FreeBSD package mirrors. Every downloaded package is inspected through `+COMPACT_MANIFEST`; a package is rejected if its FreeBSD major ABI does not match the target repository. Missing direct dependencies are pulled from the same historical snapshot when available.

For MySQL 5.6, a checksum-pinned community package may be tried only as a last fallback and is still rejected unless its embedded ABI matches. Packages from another FreeBSD major are **never** silently copied into `latest/`.

The generated `DB_PACKAGE_MATRIX.md` shows the exact result for every FreeBSD/database combination and `DB_PACKAGE_SOURCES.json` records source URLs, SHA256 hashes, package ABI values and imported dependencies. A combination that cannot be sourced safely is left marked as requiring a same-ABI source/ports build rather than publishing a knowingly incompatible package.

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

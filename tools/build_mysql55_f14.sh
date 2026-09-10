#!/bin/sh
set -eu

REPO_ROOT="${REPO_ROOT:-$PWD}"
WORK="/tmp/pvps-f14-mysql55"
PORTS="$WORK/ports"
DISTDIR="$WORK/distfiles"
OUT="$REPO_ROOT/.legacy-build/14/mysql55/all"
PORTS_COMMIT="9f0ff92f6deaa2187cdb33a27f5aebbdd2c0d71d"

rm -rf "$WORK" "$OUT"
mkdir -p "$WORK" "$DISTDIR" "$OUT"

ASSUME_ALWAYS_YES=yes pkg update -f >/dev/null
for p in ca_root_nss bash bison cmake cmake-core curl git gmake groff libedit libevent libfmt liblz4 ncurses pcre2 perl5 pkgconf readline rsync zstd; do
  ASSUME_ALWAYS_YES=yes pkg install -y "$p" >/dev/null 2>&1 || true
done

fetch --no-verify-peer -o "$WORK/ports.tar.gz" \
  "https://codeload.github.com/freebsd/freebsd-ports/tar.gz/$PORTS_COMMIT"
mkdir -p "$PORTS"
tar -xzf "$WORK/ports.tar.gz" -C "$PORTS" --strip-components=1

fetch --no-verify-peer -o "$DISTDIR/mysql-5.5.62.tar.gz" \
  "https://downloads.mysql.com/archives/get/p/23/file/mysql-5.5.62.tar.gz"
expected="b1e7853bc1f04aabf6771e0ad947f35ac8d237f4b35d0706d1095c9526ff99d7"
actual="$(sha256 -q "$DISTDIR/mysql-5.5.62.tar.gz")"
[ "$actual" = "$expected" ] || { echo "MySQL 5.5 source SHA256 mismatch" >&2; exit 1; }

for mf in "$PORTS/databases/mysql55-client/Makefile" "$PORTS/databases/mysql55-server/Makefile"; do
  sed -i '' \
    -e '/^[[:space:]]*DEPRECATED[?+:]*=/d' \
    -e '/^[[:space:]]*EXPIRATION_DATE[?+:]*=/d' \
    -e '/^[[:space:]]*IGNORE_SSL[?+:]*=/d' \
    -e '/^[[:space:]]*IGNORE_SSL_REASON[?+:]*=/d' \
    -e '/^[[:space:]]*BROKEN_FreeBSD_14[?+:]*=/d' \
    -e '/^[[:space:]]*IGNORE_FreeBSD_14[?+:]*=/d' \
    "$mf" || true
done

common="BATCH=yes DISABLE_VULNERABILITIES=yes ALLOW_UNSUPPORTED_SYSTEM=yes NO_IGNORE=yes TRYBROKEN=yes MAKE_JOBS_UNSAFE=yes"
cflags="-O2 -pipe -fcommon -fno-strict-aliasing -Wno-error"
cxxflags="-O2 -pipe -fcommon -fno-strict-aliasing -std=gnu++11 -Wno-error -Wno-deprecated-declarations -Wno-error=deprecated-declarations"
options_unset="ARCHIVE BLACKHOLE EXAMPLE FEDERATED AWS_KEY_MGMT CONNECT_EXTRA HASHICORP_VAULT COLUMNSTORE MROONGA OQGRAPH ROCKSDB S3 SPHINX SPIDER WSREP LZO SNAPPY ZMQ MSGPACK TOKUDB"

build_port() {
  port="$1"
  env $common DISTDIR="$DISTDIR" DEFAULT_VERSIONS="mysql=55" \
    OPTIONS_UNSET="$options_unset" CFLAGS="$cflags" CXXFLAGS="$cxxflags" \
    make -C "$port" clean package install
}

build_port "$PORTS/databases/mysql55-client"
build_port "$PORTS/databases/mysql55-server"

pkg info -e mysql55-client
pkg info -e mysql55-server
pkg create -a -o "$OUT"
ls -l "$OUT"/mysql55-client-* "$OUT"/mysql55-server-*

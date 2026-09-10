#!/bin/sh
set -eu

REPO_ROOT="${REPO_ROOT:-$PWD}"
WORK="/tmp/pvps-f14-mariadb103"
PORTS="$WORK/ports"
DISTDIR="$WORK/distfiles"
OPENSSL_PREFIX="$WORK/openssl111"
OUT="$REPO_ROOT/.legacy-build/14/mariadb103/all"
REPACK="$WORK/repacked"
PORTS_COMMIT="ff82663b674567c432167b1d3c78c1a4da1d20ed"

rm -rf "$WORK" "$OUT"
mkdir -p "$WORK" "$DISTDIR" "$OUT" "$REPACK"

ASSUME_ALWAYS_YES=yes pkg update -f >/dev/null
for p in ca_root_nss bash bison cmake cmake-core curl git gmake groff jq libedit libevent libfmt liblz4 ncurses pcre2 perl5 pkgconf readline rsync zstd; do
  ASSUME_ALWAYS_YES=yes pkg install -y "$p" >/dev/null 2>&1 || true
done

fetch --no-verify-peer -o "$WORK/ports.tar.gz" \
  "https://codeload.github.com/freebsd/freebsd-ports/tar.gz/$PORTS_COMMIT"
mkdir -p "$PORTS"
tar -xzf "$WORK/ports.tar.gz" -C "$PORTS" --strip-components=1

verify_sha256() {
  file="$1"
  expected="$2"
  actual="$(sha256 -q "$file")"
  if [ "$actual" != "$expected" ]; then
    echo "SHA256 mismatch: $file" >&2
    echo "expected=$expected" >&2
    echo "actual=$actual" >&2
    exit 1
  fi
}

fetch --no-verify-peer -o "$DISTDIR/mariadb-10.3.38.tar.gz" \
  "https://archive.mariadb.org/mariadb-10.3.38/source/mariadb-10.3.38.tar.gz"
verify_sha256 "$DISTDIR/mariadb-10.3.38.tar.gz" \
  "4afbeff86d996475bb2324db9845c0746ea6e128c129b86a4a0163e4dc93293c"

# MariaDB 10.3 only supports OpenSSL <= 1.1.x. Build a checksum-pinned
# OpenSSL 1.1.1t toolchain in an isolated temporary prefix. This avoids the
# removed/EOL security/openssl port and avoids polluting /usr/local.
if ! fetch --no-verify-peer -o "$WORK/openssl-1.1.1t.tar.gz" \
  "https://www.openssl.org/source/old/1.1.1/openssl-1.1.1t.tar.gz"; then
  fetch --no-verify-peer -o "$WORK/openssl-1.1.1t.tar.gz" \
    "https://ftp.openssl.org/source/old/1.1.1/openssl-1.1.1t.tar.gz"
fi
verify_sha256 "$WORK/openssl-1.1.1t.tar.gz" \
  "8dee9b24bdb1dcbf0c3d1e9b02fb8f6bf22165e807f45adeb7c9677536859d3b"
mkdir -p "$WORK/openssl-src"
tar -xzf "$WORK/openssl-1.1.1t.tar.gz" -C "$WORK/openssl-src" --strip-components=1
(
  cd "$WORK/openssl-src"
  ./config --prefix="$OPENSSL_PREFIX" --openssldir="$OPENSSL_PREFIX/ssl" shared no-tests
  gmake -j2
  gmake install_sw
)
test -f "$OPENSSL_PREFIX/include/openssl/ssl.h"
test -f "$OPENSSL_PREFIX/lib/libssl.so.111"
test -f "$OPENSSL_PREFIX/lib/libcrypto.so.111"

mf="$PORTS/databases/mariadb103-server/Makefile"
# Drop the ports-framework SSL dependency and point CMake directly at the
# isolated 1.1.1 build. The final package is repacked to depend on the
# repository's already runtime-validated OpenSSL 1.1 compatibility package.
sed -i '' \
  -e '/^[[:space:]]*DEPRECATED[?+:]*=/d' \
  -e '/^[[:space:]]*EXPIRATION_DATE[?+:]*=/d' \
  -e '/^[[:space:]]*IGNORE_SSL[?+:]*=/d' \
  -e '/^[[:space:]]*IGNORE_SSL_REASON[?+:]*=/d' \
  -e '/^[[:space:]]*BROKEN_FreeBSD_14[?+:]*=/d' \
  -e '/^[[:space:]]*IGNORE_FreeBSD_14[?+:]*=/d' \
  -e 's/[[:space:]]ssl[[:space:]]*$//' \
  "$mf"
sed -i '' \
  -e "s|-DWITH_SSL=\"\${OPENSSLBASE}\"|-DWITH_SSL=$OPENSSL_PREFIX|g" \
  "$mf"

grep -q -- "-DWITH_SSL=$OPENSSL_PREFIX" "$mf"
if grep -Eq '^[[:space:]]*USES=.*[[:space:]]ssl([[:space:]]|$)' "$mf"; then
  echo 'ports SSL dependency was not removed' >&2
  exit 1
fi

client="$PORTS/databases/mariadb103-client"
server="$PORTS/databases/mariadb103-server"
test -d "$client" && test -d "$server"

common="BATCH=yes DISABLE_VULNERABILITIES=yes ALLOW_UNSUPPORTED_SYSTEM=yes NO_IGNORE=yes TRYBROKEN=yes MAKE_JOBS_UNSAFE=yes"
cflags="-O2 -pipe -fcommon -fno-strict-aliasing -Wno-error"
cxxflags="-O2 -pipe -fcommon -fno-strict-aliasing -std=gnu++11 -Wno-error -Wno-deprecated-declarations -Wno-error=deprecated-declarations"
options_set="GSSAPI_NONE"
options_unset="GSSAPI_BASE GSSAPI_HEIMDAL GSSAPI_MIT ARCHIVE BLACKHOLE EXAMPLE FEDERATED AWS_KEY_MGMT CONNECT_EXTRA HASHICORP_VAULT COLUMNSTORE MROONGA OQGRAPH ROCKSDB S3 SPHINX SPIDER WSREP LZO SNAPPY ZMQ MSGPACK TOKUDB"

build_port() {
  port="$1"
  env $common DISTDIR="$DISTDIR" DEFAULT_VERSIONS="mysql=103m" \
    OPTIONS_SET="$options_set" OPTIONS_UNSET="$options_unset" \
    CFLAGS="$cflags" CXXFLAGS="$cxxflags" \
    make -C "$port" clean package install
}

build_port "$client"
build_port "$server"

pkg info -e mariadb103-client
pkg info -e mariadb103-server
pkg create -a -o "$OUT"

repack_root() {
  component="$1"
  original="$(find "$OUT" -maxdepth 1 -type f -name "mariadb103-${component}-*.pkg" -print | head -n 1)"
  test -n "$original"
  stage="$WORK/stage-${component}"
  manifest="$WORK/mariadb103-${component}.manifest"
  rm -rf "$stage"
  mkdir -p "$stage"
  tar -xf "$original" -C "$stage"
  jq '.deps = (.deps // {}) |
      del(.deps.openssl) |
      .deps["mysql56-openssl111-compat"]={"origin":"misc/mysql56-openssl111-compat","version":"1.0"}' \
    "$stage/+MANIFEST" > "$manifest"
  rm -f "$stage/+MANIFEST" "$stage/+COMPACT_MANIFEST"
  pkg create -f tzst -M "$manifest" -r "$stage" -o "$REPACK"
  rebuilt="$(find "$REPACK" -maxdepth 1 -type f -name "mariadb103-${component}-*.pkg" -print | head -n 1)"
  test -n "$rebuilt"
  cp -f "$rebuilt" "$OUT/$(basename "$rebuilt")"
}

repack_root client
repack_root server

COMPAT="$REPO_ROOT/FreeBSD:14:amd64/latest/All/mysql56-openssl111-compat-1.0.pkg"
CLIENT="$(find "$OUT" -maxdepth 1 -type f -name 'mariadb103-client-*.pkg' -print | head -n 1)"
SERVER="$(find "$OUT" -maxdepth 1 -type f -name 'mariadb103-server-*.pkg' -print | head -n 1)"
test -f "$COMPAT" && test -n "$CLIENT" && test -n "$SERVER"

# Remove the temporary compiler toolchain before runtime testing. Success from
# this point proves the database uses only the repository compatibility pkg.
rm -rf "$OPENSSL_PREFIX"
ASSUME_ALWAYS_YES=yes pkg delete -fy mariadb103-server mariadb103-client openssl >/dev/null 2>&1 || true
ASSUME_ALWAYS_YES=yes pkg install -y "$COMPAT" "$CLIENT" "$SERVER"
pkg info mysql56-openssl111-compat mariadb103-client mariadb103-server
mysql --version
MYSQLD="$(find /usr/local -type f -name mysqld -perm +111 -print 2>/dev/null | head -n 1)"
test -n "$MYSQLD"
"$MYSQLD" --version
if ldd "$MYSQLD" | grep -q 'not found'; then
  ldd "$MYSQLD"
  exit 1
fi
ldd "$MYSQLD"
echo 'FreeBSD 14 MariaDB 10.3 runtime validation OK'

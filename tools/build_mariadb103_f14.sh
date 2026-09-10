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

# MariaDB 10.3 supports OpenSSL only up to 1.1.x. Build a checksum-pinned
# OpenSSL 1.1.1t toolchain in a private prefix instead of asking the old ports
# tree to install the EOL security/openssl port on FreeBSD 14.
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
  env PERL=/usr/local/bin/perl ./config \
    --prefix="$OPENSSL_PREFIX" \
    --openssldir="$OPENSSL_PREFIX/ssl" \
    shared no-tests

  # Upstream OpenSSL emits libssl.so.1.1/libcrypto.so.1.1. FreeBSD's 1.1
  # port intentionally used SONAME 111. The repository compatibility package
  # provides libssl.so.111/libcrypto.so.111, so build with the same SONAME.
  sed -i '' \
    -e 's/SHLIB_VERSION_NUMBER=1\.1/SHLIB_VERSION_NUMBER=111/g' \
    Makefile
  sed -i '' \
    -e 's/SHLIB_VERSION_NUMBER "1\.1"/SHLIB_VERSION_NUMBER "111"/g' \
    include/openssl/opensslv.h
  grep -q 'SHLIB_VERSION_NUMBER=111' Makefile

  gmake -j2
  gmake install_sw
)
test -f "$OPENSSL_PREFIX/include/openssl/ssl.h"
test -f "$OPENSSL_PREFIX/lib/libssl.so.111"
test -f "$OPENSSL_PREFIX/lib/libcrypto.so.111"

mf="$PORTS/databases/mariadb103-server/Makefile"
# Remove the ports-framework SSL provider and make CMake use only the private
# build-time OpenSSL prefix. The produced packages are later repacked to use
# the repository's validated FreeBSD 14 OpenSSL 1.1 compatibility package.
sed -i '' \
  -e '/^[[:space:]]*DEPRECATED[?+:]*=/d' \
  -e '/^[[:space:]]*EXPIRATION_DATE[?+:]*=/d' \
  -e '/^[[:space:]]*IGNORE_SSL[?+:]*=/d' \
  -e '/^[[:space:]]*IGNORE_SSL_REASON[?+:]*=/d' \
  -e '/^[[:space:]]*BROKEN_FreeBSD_14[?+:]*=/d' \
  -e '/^[[:space:]]*IGNORE_FreeBSD_14[?+:]*=/d' \
  -e 's/[[:space:]]ssl[[:space:]]*$//' \
  -e "s|-DWITH_SSL=\"\${OPENSSLBASE}\"|-DWITH_SSL=$OPENSSL_PREFIX|g" \
  "$mf"

grep -q -- "-DWITH_SSL=$OPENSSL_PREFIX" "$mf"
if grep -Eq '^[[:space:]]*USES=.*[[:space:]]ssl([[:space:]]|$)|OPENSSLBASE|IGNORE_SSL' "$mf"; then
  echo 'historical ports SSL dependency was not fully removed' >&2
  grep -En 'ssl|SSL|OPENSSL' "$mf" || true
  exit 1
fi

client="$PORTS/databases/mariadb103-client"
server="$PORTS/databases/mariadb103-server"
test -d "$client" && test -d "$server"

common="BATCH=yes DISABLE_VULNERABILITIES=yes ALLOW_UNSUPPORTED_SYSTEM=yes NO_IGNORE=yes TRYBROKEN=yes MAKE_JOBS_UNSAFE=yes"
cflags="-O2 -pipe -fcommon -fno-strict-aliasing -Wno-error -I$OPENSSL_PREFIX/include"
cxxflags="-O2 -pipe -fcommon -fno-strict-aliasing -std=gnu++11 -Wno-error -Wno-deprecated-declarations -Wno-error=deprecated-declarations -I$OPENSSL_PREFIX/include"
ldflags="-L$OPENSSL_PREFIX/lib"
options_set="GSSAPI_NONE"
options_unset="GSSAPI_BASE GSSAPI_HEIMDAL GSSAPI_MIT ARCHIVE BLACKHOLE EXAMPLE FEDERATED AWS_KEY_MGMT CONNECT_EXTRA HASHICORP_VAULT COLUMNSTORE MROONGA OQGRAPH ROCKSDB S3 SPHINX SPIDER WSREP LZ4 LZO SNAPPY ZSTD ZMQ MSGPACK TOKUDB"

build_port() {
  port="$1"
  env $common DISTDIR="$DISTDIR" DEFAULT_VERSIONS="mysql=103m" \
    OPTIONS_SET="$options_set" OPTIONS_UNSET="$options_unset" \
    CPPFLAGS="-I$OPENSSL_PREFIX/include" \
    CFLAGS="$cflags" CXXFLAGS="$cxxflags" LDFLAGS="$ldflags" \
    LD_LIBRARY_PATH="$OPENSSL_PREFIX/lib" \
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

# The private OpenSSL prefix must not participate in the runtime proof.
ASSUME_ALWAYS_YES=yes pkg delete -fy mariadb103-server mariadb103-client openssl >/dev/null 2>&1 || true
rm -rf "$OPENSSL_PREFIX"
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
if ldd "$MYSQLD" | grep -F "$WORK/openssl111"; then
  echo 'mysqld still references the temporary build-only OpenSSL prefix' >&2
  ldd "$MYSQLD"
  exit 1
fi
ldd "$MYSQLD"
echo 'FreeBSD 14 MariaDB 10.3 runtime validation OK'

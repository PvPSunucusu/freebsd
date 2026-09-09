#!/bin/sh
set -eu

major="${1:?usage: build_legacy_db_native.sh <12|13|14>}"
case "$major" in
  12) families="mysql55" ;;
  13) families="mysql55 mysql56" ;;
  14) families="mysql55 mysql56" ;;
  *) echo "unsupported FreeBSD major: $major" >&2; exit 64 ;;
esac

REPO_ROOT="${REPO_ROOT:-$PWD}"
OUT="$REPO_ROOT/.legacy-build/$major"
WORK="/tmp/pvps-mysql-build-$major"
rm -rf "$OUT" "$WORK"
mkdir -p "$OUT" "$WORK"

# Historical package mirrors are bootstrap-only. The MySQL roots themselves are
# compiled inside the target FreeBSD major, so the generated package ABI is native.
mkdir -p /usr/local/etc/pkg/repos
if [ "$major" -le 13 ]; then
  cat >/usr/local/etc/pkg/repos/PvPArchive.conf <<EOF
FreeBSD: { enabled: no }
SGGS: {
  url: "https://mirror.sg.gs/freebsd-pkg/FreeBSD:${major}:amd64/latest",
  mirror_type: "none",
  signature_type: "none",
  priority: 30,
  enabled: yes
}
Nepustil: {
  url: "https://repo.nepustil.net/FreeBSD:${major}:amd64",
  mirror_type: "none",
  signature_type: "none",
  priority: 20,
  enabled: yes
}
EOF
fi

ASSUME_ALWAYS_YES=yes pkg update -f || true
for p in ca_root_nss bash cmake cmake-core bison libevent liblz4 libedit readline \
         groff perl5 gmake pkgconf rsync curl git; do
  ASSUME_ALWAYS_YES=yes pkg install -y "$p" >/dev/null 2>&1 || true
done

fetch_ports() {
  commit="$1"
  dest="$2"
  archive="$WORK/ports-$commit.tar.gz"
  rm -rf "$dest"
  fetch --no-verify-peer -o "$archive" \
    "https://codeload.github.com/freebsd/freebsd-ports/tar.gz/$commit"
  mkdir -p "$dest"
  tar -xzf "$archive" -C "$dest" --strip-components=1
}

ports_commit() {
  case "$1" in
    mysql55) echo 9f0ff92f6deaa2187cdb33a27f5aebbdd2c0d71d ;;
    mysql56) echo f8301140055f9b0fac6e3c23458e48c7cff2ef05 ;;
  esac
}

prepare_port() {
  tree="$1"
  family="$2"

  # These ports are deliberately EOL. Remove only release-age guards; package
  # ABI and source code are not rewritten or copied from another FreeBSD major.
  for mf in "$tree/databases/${family}-client/Makefile" "$tree/databases/${family}-server/Makefile"; do
    [ -f "$mf" ] || continue
    sed -i '' \
      -e '/^[[:space:]]*IGNORE_SSL[?+:]*=/d' \
      -e '/^[[:space:]]*BROKEN_FreeBSD_1[234][?+:]*=/d' \
      -e '/^[[:space:]]*IGNORE_FreeBSD_1[234][?+:]*=/d' \
      "$mf" || true
  done
}

build_one() {
  family="$1"
  commit="$(ports_commit "$family")"
  tree="$WORK/ports-$family"
  fetch_ports "$commit" "$tree"
  prepare_port "$tree" "$family"

  client="$tree/databases/${family}-client"
  server="$tree/databases/${family}-server"
  test -d "$client"
  test -d "$server"

  common_env="BATCH=yes DISABLE_VULNERABILITIES=yes ALLOW_UNSUPPORTED_SYSTEM=yes NO_IGNORE=yes TRYBROKEN=yes MAKE_JOBS_UNSAFE=yes"
  cflags="-O2 -pipe -fcommon -fno-strict-aliasing"
  cxxflags="-O2 -pipe -fno-strict-aliasing -std=gnu++11 -Wno-deprecated-declarations -Wno-error=deprecated-declarations"
  opts="SSL ARCHIVE BLACKHOLE EXAMPLE FEDERATED PARTITION"

  # Old ports frameworks can otherwise select a newer MySQL default while
  # building their own server slave port.
  defaults="mysql=${family#mysql}"

  echo "=== FreeBSD $major / $family client ==="
  env $common_env DEFAULT_VERSIONS="$defaults" OPTIONS_UNSET="$opts" \
    CFLAGS="$cflags" CXXFLAGS="$cxxflags" \
    make -C "$client" clean package install

  echo "=== FreeBSD $major / $family server ==="
  env $common_env DEFAULT_VERSIONS="$defaults" OPTIONS_UNSET="$opts" \
    CFLAGS="$cflags" CXXFLAGS="$cxxflags" \
    make -C "$server" clean package install

  pkg info -e "${family}-client"
  pkg info -e "${family}-server"

  dest="$OUT/$family/all"
  mkdir -p "$dest"
  pkg create -a -o "$dest"

  echo "=== Built roots ==="
  ls -l "$dest"/${family}-client-* "$dest"/${family}-server-*

  ASSUME_ALWAYS_YES=yes pkg delete -fy "${family}-server" "${family}-client" >/dev/null 2>&1 || true
}

for family in $families; do
  build_one "$family"
done

find "$OUT" -maxdepth 4 -type f \( -name '*.pkg' -o -name '*.txz' \) -print

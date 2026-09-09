#!/bin/sh
set -eu

major="${1:?usage: build_legacy_db_native.sh <12|13|14>}"
case "$major" in
  12) families="mysql55 mariadb103 mariadb106 mariadb114" ;;
  13) families="mysql55 mysql56 mariadb103" ;;
  14) families="mysql55 mysql56 mariadb103" ;;
  *) echo "unsupported FreeBSD major: $major" >&2; exit 64 ;;
esac

REPO_ROOT="${REPO_ROOT:-$PWD}"
OUT="$REPO_ROOT/.legacy-build/$major"
WORK="/tmp/pvps-db-build-$major"
rm -rf "$OUT" "$WORK"
mkdir -p "$OUT" "$WORK"

# EOL FreeBSD bootstrap repositories. SGGS is deliberately used here because it
# still carries FreeBSD 11/12/13 package trees; Nepustil is the second fallback.
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
         groff perl5 gmake pkgconf pcre2 boost-libs rsync curl git; do
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
    mariadb103) echo 06b18c35a7ac6d3bfda4a40cab1b3ff638b2ac0c ;;
    mariadb106) echo 0cc2151714f7a11a1367a5b2d4bc2cd18bc26dbd ;;
    mariadb114) echo 5a2bb7e61569f908f8a52dd4afd2998b86e9e54d ;;
  esac
}

family_options() {
  case "$1" in
    mysql55)
      echo "SSL ARCHIVE BLACKHOLE EXAMPLE FEDERATED PARTITION"
      ;;
    mysql56)
      echo "SSL ARCHIVE BLACKHOLE EXAMPLE FEDERATED PARTITION"
      ;;
    mariadb103)
      echo "WSREP CONNECT_EXTRA MROONGA OQGRAPH ROCKSDB SPHINX SPIDER"
      ;;
    mariadb106)
      echo "WSREP CONNECT_EXTRA MROONGA OQGRAPH ROCKSDB SPHINX SPIDER"
      ;;
    mariadb114)
      echo "WSREP CONNECT_EXTRA MROONGA OQGRAPH ROCKSDB S3 SPHINX SPIDER"
      ;;
  esac
}

build_family() {
  family="$1"
  commit="$(ports_commit "$family")"
  tree="$WORK/ports-$family"
  opts="$(family_options "$family")"
  fetch_ports "$commit" "$tree"

  client="$tree/databases/${family}-client"
  server="$tree/databases/${family}-server"
  test -d "$client"
  test -d "$server"

  # Removed/EOL ports are intentional here. Keep modern clang from promoting
  # legacy C/C++ warnings to hard errors while preserving the target ABI.
  common_env="BATCH=yes DISABLE_VULNERABILITIES=yes ALLOW_UNSUPPORTED_SYSTEM=yes NO_IGNORE=yes TRYBROKEN=yes"
  cflags="-O2 -pipe -fcommon -fno-strict-aliasing"
  cxxflags="-O2 -pipe -fno-strict-aliasing -Wno-register -Wno-deprecated-declarations -Wno-error=deprecated-declarations"

  echo "=== FreeBSD $major / $family client ==="
  env $common_env \
    OPTIONS_UNSET="$opts" CFLAGS="$cflags" CXXFLAGS="$cxxflags" \
    make -C "$client" clean package install

  echo "=== FreeBSD $major / $family server ==="
  env $common_env \
    OPTIONS_UNSET="$opts" CFLAGS="$cflags" CXXFLAGS="$cxxflags" \
    make -C "$server" clean package install

  pkg info -e "${family}-client"
  pkg info -e "${family}-server"

  dest="$OUT/$family/all"
  mkdir -p "$dest"
  pkg create -a -o "$dest"
  echo "=== closure snapshot: $family ==="
  ls -1 "$dest" | wc -l

  # Remove only the DB roots. Dependencies are intentionally retained so later
  # historical builds can reuse already-built compatible tool/runtime packages.
  ASSUME_ALWAYS_YES=yes pkg delete -fy "${family}-server" "${family}-client" >/dev/null 2>&1 || true
}

for family in $families; do
  build_family "$family"
done

find "$OUT" -maxdepth 4 -type f \( -name '*.pkg' -o -name '*.txz' \) -print

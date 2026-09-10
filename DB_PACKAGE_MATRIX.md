# Database Package Matrix

Bu dosya `tools/sync_db_packages.py` tarafindan uretilir. Paketler FreeBSD major ABI'leri arasinda kopyalanmaz.

| FreeBSD | MariaDB 11.4 | MariaDB 10.6 | MariaDB 10.5 | MariaDB 10.3 | MySQL 5.6 | MySQL 5.5 |
|---:|---|---|---|---|---|---|
| 11 | ❌ same-ABI paket bulunamadi | ❌ same-ABI paket bulunamadi | ✅ 10.5.5 | ✅ 10.3.31_1 | ✅ client=5.6.42_1, server=5.6.42_2 | ✅ 5.5.62_3 |
| 12 | ❌ same-ABI paket bulunamadi | ✅ 10.6.16 | ✅ 10.5.8 | ❌ same-ABI paket bulunamadi | ✅ 5.6.51 | ✅ 5.5.62_3 |
| 13 | ✅ 11.4.10 | ✅ 10.6.18_1 | ✅ 10.5.16 | ❌ same-ABI paket bulunamadi | ✅ 5.6.51 | ✅ 5.5.62_3 |
| 14 | ✅ 11.4.12 | ✅ 10.6.18 | ✅ 10.5.24 | ✅ 10.3.38 (F14 OpenSSL 1.1 compat) | ✅ 5.6.51 (F14 runtime-validated compat) | ✅ 5.5.62_3 |

## Kaynak ve guvenlik politikasi

- Resmi FreeBSD paket dizini once denenir; EOL surumlerde Nepustil ve SGGS tarihsel FreeBSD paket arsivleri kullanilir.
- Her indirilen paketin `+COMPACT_MANIFEST` ABI degeri hedef FreeBSD major surumuyle eslesmeden repoya alinmaz.
- MySQL 5.6 icin arsiv bulunamazsa yalniz SHA256'si sabitlenmis community paketi denenir; ABI uymuyorsa otomatik reddedilir.
- `❌` olan kombinasyonlar baska FreeBSD major paketini zorla kopyalamak yerine o ABI icin source/ports build gerektirir.

# `mobile_app` — Bağımsız Linux Mobil PDF Okuyucu

`mobile_app/`, Phosh ve KDE Plasma Mobile için tasarlanmış, sıfır ek Python bağımlılığı olan
(`sqlite3` + `GTK4` + `Libadwaita` + `Poppler` / `pdftoppm`) dokunmatik PDF manga okuyucusudur.

## Gerçek Linux Dizinleri (FHS & XDG)
- **Uygulama:** `/usr/bin/manga-reader` → `/usr/share/manga-reader/mobile_app/reader.py`
- **Masaüstü & İkon:** `/usr/share/applications/dev.hobi.MangaReader.desktop`, `/usr/share/icons/hicolor/scalable/apps/dev.hobi.MangaReader.svg`
- **Kullanıcı Ayarları:** `~/.config/manga-reader/config.json` (`XDG_CONFIG_HOME` uyumlu)
- **Varsayılan Kütüphane Kökü:** `MANGA_LIBRARY_ROOT` > `config.json` > `~/Manga` > `~/manga`

## Alpine Linux `.apk` Paketleme
```sh
./packaging/alpine/build_apk.sh
```
Çıktılar `dist/` altında üretilir:
- `dist/manga-reader-1.1.0-r0.apk`

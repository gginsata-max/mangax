# MangaX — Linux Mobil PDF Manga Okuyucu

![Python 3](https://img.shields.io/badge/Python-3.10%2B-blue)
![GTK 4](https://img.shields.io/badge/GTK-4.0-4a86cf)
![Libadwaita](https://img.shields.io/badge/Libadwaita-1-3584e4)
![Poppler](https://img.shields.io/badge/Poppler-0.18-orange)
![Alpine Linux / PostmarketOS](https://img.shields.io/badge/Alpine%20%2F%20postmarketOS-aarch64%20%7C%20noarch-0d597f)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

**MangaX** (`manga-reader`), Phosh, KDE Plasma Mobile ve Linux masaüstü ortamları için tasarlanmış, sıfır ek Python bağımlılığı olan **GTK4 + Libadwaita + Poppler** tabanlı dokunmatik PDF manga okuyucusudur.

---

## Özellikler

- **Dokunmatik Mobil Ergonomi:** Phosh / Plasma Mobile uyumlu `Adw.NavigationView` akışı, en az `46×48 px` dokunma hedefleri, sağdan sola (**RTL**) / soldan sağa (**LTR**) yön seçimi ve tam ekran okuma modu (ortaya dokunarak üst/alt çubukları gizle/göster).
- **Hızlı Çok Katmanlı PDF Çözücü:**
  - Gömülü görsel içeren sayfalarda `pdfimages -j` ile sıfır yeniden-kodlama çıkarımı
  - `pycairo` / `ctypes` (`libcairo.so.2` + `libpoppler-glib.so.8`) veya `pdftoppm -jpeg` geri düşüşü
  - Tek iş parçacıklı öncelikli arka plan çözücüsü + **5 sayfalık LRU `PageRenderCache`** ve komşu sayfa ön-yükleme (`+1`, `-1`, `+2`)
- **GPU Hızlandırmalı Yakınlaştırma:** `MangaPageWidget` (`Gtk.DrawingArea` + `Gsk GLRenderer`) ile yeniden render beklemeden anında ölçekleme (`%100`–`%300`).
- **Dahili Manga Yükleme & Kütüphane Dizini Yöneticisi:**
  - Uygulama içinden klasör veya çoklu `.pdf` seçip (`Gtk.FileDialog`) hedef dizini canlı önizleme ile kopyalama/taşıma
  - Yükleme sonrası otomatik olarak ilgili manganın bölüm sayfasına yönlendirme
  - Hedef ve kütüphane klasörünü tek dokunuşla sistem dosya yöneticisinde açma (`Gtk.FileLauncher` / `xdg-open`)
- **SQLite WAL Okuma Takibi:** Kaldığınız manga, bölüm ve sayfa `library.db` içinde güvenle saklanır; `--resume` ile doğrudan son sayfadan açılır.
- **Çok Dilli Klasör Desteği:** Düz ve dil alt klasörlü yapıları otomatik tanır.

---

## Desteklenen Klasör Düzenleri

```text
~/Manga/
├── Seri Adi A/
│   ├── 1.pdf
│   └── 2.pdf
└── Seri Adi B/
    ├── tr/
    │   ├── 1.pdf
    │   └── 2.pdf
    └── en/
        └── 1.pdf
```

- Tek dil klasörü varsa seri adı doğrudan gösterilir (`Seri Adi B`).
- Birden fazla dil klasörü varsa her dil ayrı kart olarak listelenir (`Seri Adi B [TR]`, `Seri Adi B [EN]`).

---

## Gerçek Linux FHS & XDG Yolları

| Bileşen | Yol |
| :--- | :--- |
| Çalıştırılabilir Komut | `/usr/bin/manga-reader` |
| Uygulama Modülü | `/usr/share/manga-reader/mobile_app/reader.py` |
| Masaüstü Başlatıcı | `/usr/share/applications/dev.hobi.MangaReader.desktop` |
| Uygulama Simgesi | `/usr/share/icons/hicolor/scalable/apps/dev.hobi.MangaReader.svg` |
| Kullanıcı Ayarları | `~/.config/manga-reader/config.json` (`XDG_CONFIG_HOME` uyumlu) |
| Varsayılan Kütüphane | `MANGA_LIBRARY_ROOT` > `config.json` > `~/Manga` > `~/manga` |

---

## Kurulum

### Sistem Bağımlılıkları (Alpine / PostmarketOS)

```sh
sudo apk add python3 py3-gobject3 gtk4.0 libadwaita poppler-glib poppler-utils cairo xdg-utils
```

### 1. Alpine Linux / PostmarketOS `.apk` Paketi ile Kurulum

Önceden derlenmiş `.apk` paketini [GitHub Releases](https://github.com/gginsata-max/mangax/releases) sayfasından indirebilir veya kaynaktan kendiniz derleyebilirsiniz:

```sh
./packaging/alpine/build_apk.sh
sudo apk add --allow-untrusted dist/manga-reader-1.1.0-r0.apk
```

### 2. Doğrudan Kaynaktan Çalıştırma

```sh
python3 mobile_app/reader.py
```

---

## Kullanım ve Kontroller

### CLI Seçenekleri

| Seçenek | Açıklama |
| :--- | :--- |
| `--resume` | `library.db` içindeki son kalınan manga, bölüm ve sayfadan açar |
| `--library-dir <yol>` | Kullanılacak manga kütüphanesi ana dizinini belirler |
| `--db <yol>` | Kullanılacak SQLite `library.db` dosya yolunu belirler |
| `--open <MANGA> <BOLUM> <SAYFA>` | Belirtilen manga, bölüm ve sayfayı doğrudan açar |

### Okuyucu Kontrolleri

| Eylem | Kontrol |
| :--- | :--- |
| Sonraki / Önceki Sayfa | Yatay kaydırma, sağ/sol `%30` alana dokunma, `←` / `→` / `Space` |
| Tam Ekran (Çubukları Gizle) | Ekranın orta `%40` alanına dokunma veya `F` tuşu |
| Yakınlaştırma | Üst çubuktaki büyüteç menüsü (`-`, `+`, `1:1`) |
| Okuma Yönü | Üst çubuktaki `RTL` düğmesi (tercih `config.json` içinde saklanır) |
| Geri Dön | Sol üst `←` düğmesi veya `Esc` |

---

## Testler

Tüm birim ve entegrasyon testleri harici bir PDF dosyasına ihtiyaç duymadan çalışır:

```sh
python3 -m pytest -v mobile_app/tests
```

---

## Lisans

Bu proje [MIT Lisansı](LICENSE) ile lisanslanmıştır.

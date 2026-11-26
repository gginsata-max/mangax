#!/usr/bin/env python3
"""Linux Mobile (Phosh / KDE Plasma Mobile) uyumlu GTK4 + Libadwaita + Poppler PDF Okuyucu.

Sifir ek Python bagimliligiyla (yalnizca stdlib + gi Gtk4/Adw1/Poppler)
hem masaustunde hem Linux telefonda dogrudan library.db ve Manga dizini uzerinden calisir.
"""

from __future__ import annotations

import argparse
import ctypes
import html
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Poppler", "0.18")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango, Poppler

try:
    from gi.repository import GLibUnix  # type: ignore
except ImportError:
    GLibUnix = None

try:
    import cairo  # type: ignore
except ImportError:
    cairo = None


_CAIRO_LOCK = threading.Lock()
_CTYPES_CAIRO: tuple[Any, Any] | None | bool = False


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "manga"


def _chapter_sort_key(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def _chapter_number_from_stem(stem: str) -> str:
    if stem.lower().startswith("chapter_"):
        return stem[8:] or stem
    return stem


def config_file_path() -> Path:
    """XDG Base Directory standardina uygun kullanici ayar dosyasi yolu."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_cfg).expanduser() if xdg_cfg else (Path.home() / ".config")
    return base / "manga-reader" / "config.json"


def load_app_config() -> dict[str, Any]:
    """Kullanici ayarlarini JSON dosyasindan guvenle okur."""
    cfg_path = config_file_path()
    if not cfg_path.is_file():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_app_config(updates: dict[str, Any]) -> Path:
    """Ayarlari mevcut config.json ile birlestirip atomik olarak kaydeder."""
    cfg_path = config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = load_app_config()
    data.update(updates)
    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cfg_path)
    return cfg_path


def resolve_library_root() -> Path:
    """Gercek Linux kullanici ve XDG kurallarina gore kutuphane kok dizinini cozer."""
    env_val = os.environ.get("MANGA_LIBRARY_ROOT", "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()
    saved = str(load_app_config().get("library_root", "") or "").strip()
    if saved:
        return Path(saved).expanduser().resolve()
    home = Path.home()
    for cand in (home / "Manga", home / "manga"):
        if cand.is_dir():
            return cand.resolve()
    local_default = Path(__file__).resolve().parent.parent / "MangaLibrary"
    if local_default.is_dir():
        return local_default.resolve()
    return (home / "Manga").resolve()


def resolve_db_path(
    db_path: str | Path | None = None, library_root: Path | None = None
) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser().resolve()
    return (library_root or resolve_library_root()) / "library.db"


def _safe_dir_name(title: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "_", title).strip().strip(".") or "Manga"


_LANG_DIR_RE = re.compile(r"[a-z]{2}(?:-[a-z]{2,4})?")
_LANG_TITLE_RE = re.compile(r"(.*) \[([A-Z]{2}(?:-[A-Z]{2,4})?)\]")


def _is_lang_dir(p: Path) -> bool:
    return p.is_dir() and bool(_LANG_DIR_RE.fullmatch(p.name.lower()))


def iter_library_groups(library_root: Path) -> list[tuple[str, Path, list[Path]]]:
    """Kutuphane kokunu tarar; (gorunen_baslik, klasor, pdf_listesi) gruplari dondurur.

    Desteklenen duzenler:
      - <Kok>/<Seri>/<bolum>.pdf                  (eski duzen)
      - <Kok>/<Seri>/<dil>/<bolum>.pdf            (cok dilli duzen)
    Tek dil klasoru varsa baslik seri adidir; birden fazla dil varsa "Seri [TR]" bicimindedir.
    """
    groups: list[tuple[str, Path, list[Path]]] = []
    if not library_root.is_dir():
        return groups
    for entry in sorted(library_root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        direct = sorted(entry.glob("*.pdf"), key=lambda p: _chapter_sort_key(p.stem))
        lang_dirs = [
            d for d in sorted(entry.iterdir(), key=lambda p: p.name.lower())
            if _is_lang_dir(d) and any(d.glob("*.pdf"))
        ]
        if direct:
            groups.append((entry.name, entry, direct))
        multi = len(lang_dirs) > 1 or bool(direct)
        for d in lang_dirs:
            pdfs = sorted(d.glob("*.pdf"), key=lambda p: _chapter_sort_key(p.stem))
            title = f"{entry.name} [{d.name.upper()}]" if multi else entry.name
            groups.append((title, d, pdfs))
    return groups


def resolve_chapter_pdf(
    library_root: Path, manga_title: str, chapter_number: str, stored_path: str = ""
) -> Path | None:
    m = _LANG_TITLE_RE.fullmatch(manga_title)
    if m:
        dirs = [library_root / _safe_dir_name(m.group(1)) / m.group(2).lower()]
    else:
        base_dir = library_root / _safe_dir_name(manga_title)
        dirs = [base_dir]
        if base_dir.is_dir():
            dirs += [d for d in sorted(base_dir.iterdir()) if _is_lang_dir(d)]
    for base in dirs:
        for name in (f"{chapter_number}.pdf", f"chapter_{chapter_number}.pdf"):
            cand = base / name
            if cand.exists():
                return cand
    if stored_path:
        p = Path(stored_path)
        if p.exists():
            return p
    return None


def _connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_reader_db(db_path: str | Path | None = None) -> Path:
    target = resolve_db_path(db_path)
    with _connect_db(target) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mangas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug VARCHAR(128) NOT NULL UNIQUE,
                title VARCHAR(256) NOT NULL,
                source_type VARCHAR(32) NOT NULL DEFAULT 'local',
                source_id VARCHAR(128) NOT NULL DEFAULT '',
                source_url VARCHAR(512) NOT NULL DEFAULT '',
                source_lang VARCHAR(16) NOT NULL DEFAULT 'en',
                target_lang VARCHAR(16) NOT NULL DEFAULT 'tr',
                library_dir VARCHAR(512) NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manga_id INTEGER NOT NULL REFERENCES mangas(id) ON DELETE CASCADE,
                chapter_number VARCHAR(32) NOT NULL,
                chapter_sort FLOAT NOT NULL DEFAULT 0.0,
                title VARCHAR(256) NOT NULL DEFAULT '',
                source_chapter_id VARCHAR(128) NOT NULL DEFAULT '',
                source_url VARCHAR(512) NOT NULL DEFAULT '',
                page_count INTEGER NOT NULL DEFAULT 0,
                orig_pdf_path VARCHAR(512) NOT NULL DEFAULT '',
                tr_pdf_path VARCHAR(512) NOT NULL DEFAULT '',
                library_pdf_path VARCHAR(512) NOT NULL DEFAULT '',
                transcript_path VARCHAR(512) NOT NULL DEFAULT '',
                download_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                translation_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                qc_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                qc_notes TEXT NOT NULL DEFAULT '',
                updated_at DATETIME NOT NULL,
                UNIQUE (manga_id, chapter_number)
            );
            CREATE TABLE IF NOT EXISTS reading_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manga_id INTEGER NOT NULL REFERENCES mangas(id) ON DELETE CASCADE,
                chapter_id INTEGER NOT NULL UNIQUE REFERENCES chapters(id) ON DELETE CASCADE,
                current_page INTEGER NOT NULL DEFAULT 1,
                total_pages INTEGER NOT NULL DEFAULT 0,
                is_completed BOOLEAN NOT NULL DEFAULT 0,
                last_read_at DATETIME NOT NULL
            );
            """
        )
    return target


def sync_disk_library(library_root: Path, db_path: Path) -> dict[str, int]:
    """Ana Manga klasorunu tarar; eksik manga/bolumleri DB'ye ekler, mevcut ilerlemeyi korur."""
    target = init_reader_db(db_path)
    added_mangas = 0
    added_chapters = 0
    if not library_root.is_dir():
        return {"added_mangas": 0, "added_chapters": 0}

    now = _utcnow_iso()
    with _connect_db(target) as conn:
        for title, entry, pdfs in iter_library_groups(library_root):
            slug = _slugify(title)
            row = conn.execute(
                "SELECT id FROM mangas WHERE title = ? OR slug = ?", (title, slug)
            ).fetchone()
            if row is None:
                base_slug = slug
                idx = 2
                while conn.execute("SELECT 1 FROM mangas WHERE slug = ?", (slug,)).fetchone():
                    slug = f"{base_slug}_{idx}"
                    idx += 1
                cur = conn.execute(
                    """
                    INSERT INTO mangas
                        (slug, title, source_type, source_id, source_url,
                         source_lang, target_lang, library_dir, status, created_at, updated_at)
                    VALUES (?, ?, 'local', '', '', 'en', 'tr', ?, 'active', ?, ?)
                    """,
                    (slug, title, str(entry), now, now),
                )
                manga_id = int(cur.lastrowid)
                added_mangas += 1
            else:
                manga_id = int(row["id"])

            for pdf_file in pdfs:
                ch_num = _chapter_number_from_stem(pdf_file.stem)
                existing_ch = conn.execute(
                    "SELECT id FROM chapters WHERE manga_id = ? AND chapter_number = ?",
                    (manga_id, ch_num),
                ).fetchone()
                if existing_ch is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO chapters
                        (manga_id, chapter_number, chapter_sort, title,
                         source_chapter_id, source_url, page_count,
                         orig_pdf_path, tr_pdf_path, library_pdf_path, transcript_path,
                         download_status, translation_status, qc_status, qc_notes, updated_at)
                    VALUES (?, ?, ?, ?, '', '', 0, '', '', ?, '', 'completed', 'pending', 'pending', '', ?)
                    """,
                    (
                        manga_id,
                        ch_num,
                        _chapter_sort_key(ch_num),
                        f"Bölüm {ch_num}",
                        str(pdf_file),
                        now,
                    ),
                )
                added_chapters += 1
        conn.commit()
    return {"added_mangas": added_mangas, "added_chapters": added_chapters}


def compute_page_step(current_idx: int, total_pages: int, delta: int) -> int:
    """Ilk ve son sayfa sinirini korur; baska bolume gecmez."""
    if total_pages <= 0:
        return 0
    return max(0, min(total_pages - 1, current_idx + delta))


def resolve_swipe_delta(
    dx: float, dy: float, rtl_mode: bool, threshold: float = 45.0
) -> int:
    """Yatay suruklemeden sayfa yonu (+1/-1/0) hesaplar."""
    if abs(dx) < threshold or abs(dx) <= abs(dy) * 1.2:
        return 0
    if dx < 0:
        return -1 if rtl_mode else 1
    return 1 if rtl_mode else -1


def resolve_tap_delta(x: float, width: float, rtl_mode: bool) -> int:
    """Dokunma X konumuna gore sayfa yonu (+1/-1/0) hesaplar."""
    w = max(1.0, float(width))
    if x < w * 0.30:
        return 1 if rtl_mode else -1
    if x > w * 0.70:
        return -1 if rtl_mode else 1
    return 0


def compute_zoom_dimensions(
    tex_w: int, tex_h: int, view_w: int, view_h: int, zoom_scale: float
) -> tuple[int, int] | None:
    """Zoom > 1.0 iken gercek hedef piksel boyutunu, 1.0 iken None (ekrana sigdir) doner."""
    if zoom_scale <= 1.01 or tex_w <= 0 or tex_h <= 0 or view_w <= 0 or view_h <= 0:
        return None
    fit_ratio = min(view_w / tex_w, view_h / tex_h)
    scaled_w = max(1, int(round(tex_w * fit_ratio * zoom_scale)))
    scaled_h = max(1, int(round(tex_h * fit_ratio * zoom_scale)))
    return scaled_w, scaled_h


def _get_tmp_dir() -> str | None:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir and os.path.isdir(runtime_dir) and os.access(runtime_dir, os.W_OK):
        return runtime_dir
    return None


def extract_single_page_image_bytes(pdf_path: Path, page_index: int) -> bytes | None:
    """PDF sayfasinda tek gomulu JPEG/PNG varsa onu sifir yeniden-encode ile cikarir."""
    page_num = page_index + 1
    try:
        with tempfile.TemporaryDirectory(dir=_get_tmp_dir()) as td:
            prefix = os.path.join(td, "p")
            subprocess.run(
                ["pdfimages", "-j", "-f", str(page_num), "-l", str(page_num), str(pdf_path), prefix],
                capture_output=True,
                check=True,
                timeout=10,
            )
            files = sorted(Path(td).iterdir())
            if len(files) != 1:
                return None
            img = files[0]
            if img.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                return None
            data = img.read_bytes()
            return data if data else None
    except Exception:
        return None


def _load_ctypes_cairo() -> tuple[Any, Any] | None:
    global _CTYPES_CAIRO
    with _CAIRO_LOCK:
        if _CTYPES_CAIRO is not False:
            return _CTYPES_CAIRO  # type: ignore[return-value]
        try:
            libcairo = ctypes.CDLL("libcairo.so.2")
            libpop = ctypes.CDLL("libpoppler-glib.so.8")
            libcairo.cairo_image_surface_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
            libcairo.cairo_image_surface_create.restype = ctypes.c_void_p
            libcairo.cairo_create.argtypes = [ctypes.c_void_p]
            libcairo.cairo_create.restype = ctypes.c_void_p
            libcairo.cairo_set_source_rgb.argtypes = [
                ctypes.c_void_p,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
            ]
            libcairo.cairo_paint.argtypes = [ctypes.c_void_p]
            libcairo.cairo_scale.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
            libcairo.cairo_surface_flush.argtypes = [ctypes.c_void_p]
            libcairo.cairo_image_surface_get_data.argtypes = [ctypes.c_void_p]
            libcairo.cairo_image_surface_get_data.restype = ctypes.c_void_p
            libcairo.cairo_image_surface_get_stride.argtypes = [ctypes.c_void_p]
            libcairo.cairo_image_surface_get_stride.restype = ctypes.c_int
            libcairo.cairo_destroy.argtypes = [ctypes.c_void_p]
            libcairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
            libpop.poppler_page_render.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            _CTYPES_CAIRO = (libcairo, libpop)
        except Exception:
            _CTYPES_CAIRO = None
        return _CTYPES_CAIRO  # type: ignore[return-value]


class _PyGObjectStruct(ctypes.Structure):
    _fields_ = [
        ("ob_refcnt", ctypes.c_ssize_t),
        ("ob_type", ctypes.c_void_p),
        ("obj", ctypes.c_void_p),
    ]


def _render_via_ctypes_cairo(
    page: Poppler.Page, pw: float, ph: float, target_width: int = 900
) -> tuple[Gdk.Texture, int, int] | None:
    libs = _load_ctypes_cairo()
    if not libs:
        return None
    libcairo, libpop = libs
    surf = None
    cr = None
    try:
        scale = max(0.5, min(2.5, target_width / max(1.0, pw)))
        iw = max(1, int(round(pw * scale)))
        ih = max(1, int(round(ph * scale)))
        surf = libcairo.cairo_image_surface_create(0, iw, ih)
        if not surf:
            return None
        cr = libcairo.cairo_create(surf)
        if not cr:
            return None
        libcairo.cairo_set_source_rgb(cr, 1.0, 1.0, 1.0)
        libcairo.cairo_paint(cr)
        libcairo.cairo_scale(cr, scale, scale)
        gobj_ptr = _PyGObjectStruct.from_address(id(page)).obj
        if not gobj_ptr:
            return None
        libpop.poppler_page_render(gobj_ptr, cr)
        libcairo.cairo_surface_flush(surf)
        stride = int(libcairo.cairo_image_surface_get_stride(surf))
        data_ptr = libcairo.cairo_image_surface_get_data(surf)
        if not data_ptr or stride <= 0:
            return None
        raw = ctypes.string_at(data_ptr, stride * ih)
        tex = Gdk.MemoryTexture.new(
            iw, ih, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, GLib.Bytes.new(raw), stride
        )
        return tex, iw, ih
    except Exception:
        return None
    finally:
        if cr:
            libcairo.cairo_destroy(cr)
        if surf:
            libcairo.cairo_surface_destroy(surf)


def _render_via_pdftoppm_jpeg(pdf_path: Path, page_index: int, max_dim: int = 1080) -> bytes:
    page_num = page_index + 1
    proc = subprocess.run(
        [
            "pdftoppm",
            "-jpeg",
            "-singlefile",
            "-scale-to",
            str(max_dim),
            "-f",
            str(page_num),
            "-l",
            str(page_num),
            str(pdf_path),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    if not proc.stdout:
        raise RuntimeError(f"pdftoppm bos cikti verdi: {pdf_path} sayfa {page_num}")
    return proc.stdout


def render_pdf_page_texture(
    pdf_path: Path,
    page_index: int,
    scale: float = 1.35,
    poppler_doc: Poppler.Document | None = None,
    target_width: int = 900,
) -> tuple[Gdk.Texture, int, int]:
    """PDF sayfasini en hizli uygun arka ucla Gdk.Texture olarak cozer."""
    doc = poppler_doc or Poppler.Document.new_from_file(pdf_path.resolve().as_uri(), None)
    page = doc.get_page(page_index)
    pw, ph = page.get_size()

    has_text = bool((page.get_text() or "").strip())
    if not has_text:
        img_bytes = extract_single_page_image_bytes(pdf_path, page_index)
        if img_bytes:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(img_bytes))
                return tex, tex.get_width(), tex.get_height()
            except Exception:
                pass

    if cairo is not None:
        iw = max(1, int(pw * scale))
        ih = max(1, int(ph * scale))
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, iw, ih)
        cr = cairo.Context(surf)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.scale(scale, scale)
        page.render(cr)
        surf.flush()
        gbytes = GLib.Bytes.new(bytes(surf.get_data()))
        tex = Gdk.MemoryTexture.new(
            iw, ih, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, gbytes, surf.get_stride()
        )
        return tex, iw, ih

    ctypes_res = _render_via_ctypes_cairo(page, pw, ph, target_width=target_width)
    if ctypes_res is not None:
        return ctypes_res

    jpeg_bytes = _render_via_pdftoppm_jpeg(pdf_path, page_index, max_dim=max(720, target_width))
    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(jpeg_bytes))
    return tex, tex.get_width(), tex.get_height()


def render_pdf_page_info(pdf_path: str | Path, page_index: int = 0) -> dict[str, Any]:
    """Poppler + hizli arka ucla PDF sayfasini acip boyut ve piksel dogrulamasi yapar."""
    p = Path(pdf_path).resolve()
    doc = Poppler.Document.new_from_file(p.as_uri(), None)
    n_pages = doc.get_n_pages()
    if n_pages <= 0:
        raise ValueError(f"PDF sayfa icermiyor: {p}")
    idx = max(0, min(n_pages - 1, page_index))
    page = doc.get_page(idx)
    w, h = page.get_size()

    raw_img = extract_single_page_image_bytes(p, idx)
    if raw_img:
        byte_len = len(raw_img)
        backend = "pdfimages"
    elif cairo is not None:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w)), max(1, int(h)))
        cr = cairo.Context(surf)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        page.render(cr)
        surf.flush()
        byte_len = len(surf.get_data())
        backend = "cairo"
    else:
        jpeg_bytes = _render_via_pdftoppm_jpeg(p, idx, max_dim=900)
        byte_len = len(jpeg_bytes)
        backend = "pdftoppm_jpeg"

    return {
        "pages": n_pages,
        "page_index": idx,
        "width": int(w),
        "height": int(h),
        "bytes": byte_len,
        "backend": backend,
    }


class PageRenderCache:
    """Tek is parcacikli oncelikli arka plan PDF cozucu ve 5 sayfalik LRU onbellek."""

    def __init__(self, max_entries: int = 5) -> None:
        self.max_entries = max(1, max_entries)
        self._cache: OrderedDict[tuple[str, int], tuple[Gdk.Texture, int, int]] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.PriorityQueue[tuple[Any, ...]] = queue.PriorityQueue()
        self._seq = 0
        self._stop = False
        self._worker_doc_path: str | None = None
        self._worker_doc: Poppler.Document | None = None
        self._thread = threading.Thread(target=self._worker_loop, name="pdf-render-worker", daemon=True)
        self._thread.start()

    def get_cached(self, pdf_path: Path, page_idx: int) -> tuple[Gdk.Texture, int, int] | None:
        key = (str(pdf_path), int(page_idx))
        with self._lock:
            val = self._cache.get(key)
            if val is not None:
                self._cache.move_to_end(key)
            return val

    def put_cached(
        self, pdf_path: Path, page_idx: int, tex: Gdk.Texture, w: int, h: int
    ) -> None:
        key = (str(pdf_path), int(page_idx))
        with self._lock:
            self._cache[key] = (tex, w, h)
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)

    def request_page(
        self,
        pdf_path: Path,
        page_idx: int,
        gen: int,
        priority: int = 0,
        target_width: int = 900,
        callback: Callable[[int, Path, int, tuple[Gdk.Texture, int, int] | None, Exception | None], None] | None = None,
    ) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._queue.put((priority, -seq, gen, str(pdf_path), int(page_idx), target_width, callback))

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def shutdown(self) -> None:
        self._stop = True
        self._queue.put((-1, 0, -1, "", -1, 0, None))
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _get_worker_doc(self, pdf_path: Path) -> Poppler.Document:
        key = str(pdf_path)
        if self._worker_doc_path != key or self._worker_doc is None:
            self._worker_doc = Poppler.Document.new_from_file(pdf_path.resolve().as_uri(), None)
            self._worker_doc_path = key
        return self._worker_doc

    def _worker_loop(self) -> None:
        while not self._stop:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            priority, _neg_seq, gen, path_str, page_idx, target_width, callback = item
            if self._stop or page_idx < 0 or not path_str:
                continue
            pdf_path = Path(path_str)
            cached = self.get_cached(pdf_path, page_idx)
            if cached is not None:
                if callback is not None:
                    GLib.idle_add(self._deliver, callback, gen, pdf_path, page_idx, cached, None)
                continue
            try:
                doc = self._get_worker_doc(pdf_path)
                if page_idx >= doc.get_n_pages():
                    continue
                res = render_pdf_page_texture(
                    pdf_path,
                    page_idx,
                    scale=1.35,
                    poppler_doc=doc,
                    target_width=target_width,
                )
                self.put_cached(pdf_path, page_idx, *res)
                if callback is not None:
                    GLib.idle_add(self._deliver, callback, gen, pdf_path, page_idx, res, None)
            except Exception as exc:
                if callback is not None:
                    GLib.idle_add(self._deliver, callback, gen, pdf_path, page_idx, None, exc)

    @staticmethod
    def _deliver(
        callback: Callable[[int, Path, int, tuple[Gdk.Texture, int, int] | None, Exception | None], None],
        gen: int,
        pdf_path: Path,
        page_idx: int,
        res: tuple[Gdk.Texture, int, int] | None,
        exc: Exception | None,
    ) -> bool:
        callback(gen, pdf_path, page_idx, res, exc)
        return False


def save_reading_progress(
    chapter_id: int,
    current_page: int,
    total_pages: int,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Kalinan sayfa ve bolumu dogrudan library.db'ye kaydeder."""
    target = init_reader_db(db_path)
    now = _utcnow_iso()
    is_completed = 1 if (total_pages > 0 and current_page >= total_pages) else 0
    with _connect_db(target) as conn:
        row = conn.execute("SELECT manga_id FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
        if row is None:
            raise ValueError(f"Bolum bulunamadi: {chapter_id}")
        manga_id = int(row["manga_id"])
        existing = conn.execute(
            "SELECT id FROM reading_progress WHERE chapter_id = ?", (chapter_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO reading_progress (manga_id, chapter_id, current_page, total_pages, is_completed, last_read_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (manga_id, chapter_id, current_page, total_pages, is_completed, now),
            )
        else:
            conn.execute(
                """
                UPDATE reading_progress
                SET current_page = ?, total_pages = ?, is_completed = ?, last_read_at = ?
                WHERE chapter_id = ?
                """,
                (current_page, total_pages, is_completed, now, chapter_id),
            )
        conn.commit()
    return {
        "manga_id": manga_id,
        "chapter_id": chapter_id,
        "current_page": current_page,
        "total_pages": total_pages,
        "is_completed": bool(is_completed),
    }


def get_resume_target(db_path: str | Path | None = None) -> dict[str, Any] | None:
    """En son okunan manga, bolum ve sayfa bilgisini getirir."""
    target = init_reader_db(db_path)
    lib_root = (
        resolve_library_root()
        if os.environ.get("MANGA_LIBRARY_ROOT", "").strip()
        else target.parent
    )
    with _connect_db(target) as conn:
        row = conn.execute(
            """
            SELECT rp.manga_id, m.title AS manga_title, rp.chapter_id, c.chapter_number,
                   c.library_pdf_path, rp.current_page, rp.total_pages
            FROM reading_progress rp
            JOIN mangas m ON m.id = rp.manga_id
            JOIN chapters c ON c.id = rp.chapter_id
            ORDER BY rp.last_read_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    pdf_path = resolve_chapter_pdf(
        lib_root, row["manga_title"], row["chapter_number"], row["library_pdf_path"]
    )
    if pdf_path is None:
        return None
    return {
        "manga_id": int(row["manga_id"]),
        "manga_title": row["manga_title"],
        "chapter_id": int(row["chapter_id"]),
        "chapter_number": row["chapter_number"],
        "pdf_path": str(pdf_path),
        "current_page": int(row["current_page"]),
        "total_pages": int(row["total_pages"]),
    }


def load_library_summary(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    target = init_reader_db(db_path)
    with _connect_db(target) as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.slug, m.title, m.source_type, COUNT(c.id) AS chapter_count,
                   COALESCE(SUM(CASE WHEN rp.is_completed = 1 THEN 1 ELSE 0 END), 0) AS completed_count,
                   COALESCE(SUM(CASE WHEN rp.id IS NOT NULL THEN 1 ELSE 0 END), 0) AS started_count,
                   MAX(rp.last_read_at) AS last_read_at
            FROM mangas m
            LEFT JOIN chapters c ON c.manga_id = m.id
            LEFT JOIN reading_progress rp ON rp.chapter_id = c.id
            GROUP BY m.id
            ORDER BY m.title
            """
        ).fetchall()
    return [dict(r) for r in rows]


def reading_fraction(current: int, total: int) -> float:
    """0.0-1.0 arasi okuma orani."""
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, float(current) / float(total)))


def filter_library(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Baslik/slug icinde buyuk-kucuk harf duyarsiz arama."""
    q = (query or "").strip().casefold()
    if not q:
        return list(items)
    return [m for m in items if q in str(m.get("title", "")).casefold() or q in str(m.get("slug", "")).casefold()]


def pick_continue_chapter(chapters: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Bolum listesinden 'Devam Et' hedefini (bolum_idx, sayfa_idx_0) secer."""
    if not chapters:
        return None
    read = [(i, c) for i, c in enumerate(chapters) if c.get("last_read_at")]
    if not read:
        return 0, 0
    i, c = max(read, key=lambda t: str(t[1]["last_read_at"]))
    if c.get("is_completed"):
        if i + 1 < len(chapters):
            nxt = chapters[i + 1]
            pg = int(nxt.get("current_page") or 0)
            return i + 1, (pg - 1 if pg > 0 and not nxt.get("is_completed") else 0)
        return i, 0
    return i, max(0, int(c.get("current_page") or 1) - 1)


def plan_import_destination(library_root: Path, manga_title: str, lang: str = "") -> Path:
    """Yuklenecek PDF'lerin kutuphane altinda yerlestirilecegi gercek hedef dizini hesaplar."""
    safe_title = _safe_dir_name(manga_title)
    clean_lang = (lang or "").strip().lower()
    if clean_lang and _LANG_DIR_RE.fullmatch(clean_lang):
        return library_root / safe_title / clean_lang
    return library_root / safe_title


def _find_manga_by_title(db_path: Path, title: str) -> tuple[int, str] | None:
    slug = _slugify(title)
    with _connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, title FROM mangas WHERE title = ? OR slug = ? LIMIT 1",
            (title, slug),
        ).fetchone()
    if row is None:
        return None
    return int(row["id"]), str(row["title"])


def _transfer_pdf(src: Path, dst: Path, move: bool) -> bool:
    """Tek PDF dosyasini hedefe kopyalar veya tasir; ayni dosyaysa False doner."""
    src_r = src.resolve()
    dst_r = dst.resolve()
    if src_r == dst_r:
        return False
    if move:
        shutil.move(str(src_r), str(dst_r))
    else:
        shutil.copy2(str(src_r), str(dst_r))
    return True


def import_pdf_files(
    pdf_paths: list[Path],
    library_root: Path,
    db_path: Path,
    manga_title: str,
    lang: str = "",
    move: bool = False,
) -> dict[str, Any]:
    """Secilen PDF dosyalarini kutuphane dizinine yukler ve library.db ile esitler."""
    clean_title = (manga_title or "").strip()
    if not clean_title:
        raise ValueError("Seri adı boş olamaz.")
    valid_pdfs = [Path(p).expanduser().resolve() for p in pdf_paths if Path(p).is_file() and Path(p).suffix.lower() == ".pdf"]
    if not valid_pdfs:
        raise ValueError("Yüklenecek geçerli bir PDF dosyası bulunamadı.")

    dest_dir = plan_import_destination(library_root, clean_title, lang)
    dest_dir.mkdir(parents=True, exist_ok=True)

    imported = 0
    skipped = 0
    for src in valid_pdfs:
        dst = dest_dir / src.name
        if _transfer_pdf(src, dst, move=move):
            imported += 1
        else:
            skipped += 1

    sync_disk_library(library_root, db_path)

    clean_lang = (lang or "").strip().lower()
    cand_titles = []
    if clean_lang and _LANG_DIR_RE.fullmatch(clean_lang):
        cand_titles.append(f"{_safe_dir_name(clean_title)} [{clean_lang.upper()}]")
    cand_titles.append(_safe_dir_name(clean_title))

    found: tuple[int, str] | None = None
    for cand in cand_titles:
        found = _find_manga_by_title(db_path, cand)
        if found is not None:
            break

    return {
        "imported_files": imported,
        "skipped_files": skipped,
        "dest_dir": str(dest_dir),
        "manga_title": found[1] if found else clean_title,
        "manga_id": found[0] if found else None,
    }


def import_manga_folder(
    source_dir: Path,
    library_root: Path,
    db_path: Path,
    manga_title: str = "",
    lang: str = "",
    move: bool = False,
) -> dict[str, Any]:
    """Bir manga klasorunu (duz PDF, <seri>/<dil>/*.pdf veya coklu seri) kutuphaneye yukler."""
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"Kaynak klasör bulunamadı: {source_dir}")

    direct_pdfs = sorted(src.glob("*.pdf"), key=lambda p: _chapter_sort_key(p.stem))
    if direct_pdfs:
        return import_pdf_files(
            direct_pdfs,
            library_root=library_root,
            db_path=db_path,
            manga_title=(manga_title or "").strip() or src.name,
            lang=lang,
            move=move,
        )

    lang_dirs = [
        d for d in sorted(src.iterdir(), key=lambda p: p.name.lower())
        if _is_lang_dir(d) and any(d.glob("*.pdf"))
    ]
    if lang_dirs:
        series_name = (manga_title or "").strip() or src.name
        total_imported = 0
        total_skipped = 0
        last_res: dict[str, Any] = {}
        for d in lang_dirs:
            pdfs = sorted(d.glob("*.pdf"), key=lambda p: _chapter_sort_key(p.stem))
            res = import_pdf_files(
                pdfs,
                library_root=library_root,
                db_path=db_path,
                manga_title=series_name,
                lang=d.name,
                move=move,
            )
            total_imported += res["imported_files"]
            total_skipped += res["skipped_files"]
            last_res = res
        return {
            "imported_files": total_imported,
            "skipped_files": total_skipped,
            "dest_dir": str(library_root / _safe_dir_name(series_name)),
            "manga_title": last_res.get("manga_title", series_name),
            "manga_id": last_res.get("manga_id"),
        }

    groups = iter_library_groups(src)
    if groups:
        total_imported = 0
        total_skipped = 0
        first_res: dict[str, Any] = {}
        for g_title, _g_dir, g_pdfs in groups:
            m = _LANG_TITLE_RE.fullmatch(g_title)
            base_t = m.group(1) if m else g_title
            g_lang = m.group(2).lower() if m else (_g_dir.name if _is_lang_dir(_g_dir) else lang)
            res = import_pdf_files(
                g_pdfs,
                library_root=library_root,
                db_path=db_path,
                manga_title=base_t,
                lang=g_lang,
                move=move,
            )
            total_imported += res["imported_files"]
            total_skipped += res["skipped_files"]
            if not first_res:
                first_res = res
        return {
            "imported_files": total_imported,
            "skipped_files": total_skipped,
            "dest_dir": str(library_root),
            "manga_title": first_res.get("manga_title", src.name),
            "manga_id": first_res.get("manga_id"),
        }

    raise ValueError("Seçilen klasörde veya alt klasörlerinde hiç .pdf dosyası bulunamadı.")


_APP_CSS = b"""
button.reader-touch-btn {
    min-height: 46px;
    min-width: 46px;
    padding: 6px 10px;
}
button.reader-nav-btn {
    min-height: 48px;
    min-width: 52px;
    padding: 8px 12px;
    font-weight: 600;
    border-radius: 14px;
}
scale.reader-slider trough {
    min-height: 8px;
    border-radius: 999px;
}
scale.reader-slider slider {
    min-height: 26px;
    min-width: 26px;
}
.hero-card {
    padding: 18px;
    border-radius: 20px;
    background-image: linear-gradient(135deg, alpha(@accent_bg_color, 0.95), alpha(@accent_bg_color, 0.65));
    color: @accent_fg_color;
}
.hero-eyebrow {
    font-size: 0.75em;
    font-weight: 800;
    letter-spacing: 0.08em;
    opacity: 0.85;
}
.hero-title {
    font-size: 1.3em;
    font-weight: 800;
}
.hero-sub {
    opacity: 0.9;
}
.hero-card progressbar trough {
    min-height: 6px;
    border-radius: 999px;
    background-color: alpha(@accent_fg_color, 0.25);
}
.hero-card progressbar progress {
    min-height: 6px;
    border-radius: 999px;
    background-color: @accent_fg_color;
}
.hero-card button.hero-btn {
    background: #ffffff;
    background-image: none;
    color: #1d1d1f;
    font-weight: 800;
    min-height: 48px;
    border-radius: 999px;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.18);
}
.hero-card button.hero-btn:hover {
    background: #f2f2f4;
    background-image: none;
    color: #1d1d1f;
}
.hero-card button.hero-btn:active {
    background: #e5e5ea;
    background-image: none;
    color: #1d1d1f;
}
.section-title {
    font-weight: 800;
    font-size: 1.05em;
}
.count-badge {
    padding: 2px 10px;
    border-radius: 999px;
    background-color: alpha(@window_fg_color, 0.08);
    font-size: 0.85em;
    font-weight: 700;
}
.pct-badge {
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 0.8em;
    font-weight: 800;
    background-color: alpha(@accent_bg_color, 0.35);
    color: @window_fg_color;
}
.pct-badge.done {
    background-color: alpha(@success_color, 0.18);
    color: @success_color;
}
row.big-row {
    min-height: 64px;
}
.chapter-num {
    font-weight: 800;
    min-width: 44px;
    opacity: 0.55;
}
progressbar.thin trough, progressbar.thin progress {
    min-height: 4px;
    border-radius: 999px;
}
.status-done {
    color: @success_color;
}
.status-reading {
    color: @accent_color;
}
.reader-surface {
    background-color: #121212;
}
.osd-badge {
    padding: 5px 12px;
    border-radius: 999px;
    background-color: alpha(black, 0.65);
    color: white;
    font-weight: 700;
}
"""


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


class MangaPageWidget(Gtk.DrawingArea):
    """GPU hizlandirmali (Gsk GLRenderer) ve gercek zoom olcekleme destekli sayfa widget'i."""

    def __init__(self) -> None:
        super().__init__()
        self._texture: Gdk.Texture | None = None

    def set_paintable(self, texture: Gdk.Texture | None) -> None:
        self._texture = texture
        self.queue_draw()

    def set_target_size(self, width: int, height: int) -> None:
        self.set_content_width(max(0, int(width)))
        self.set_content_height(max(0, int(height)))
        self.queue_resize()
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self._texture is None:
            return
        w = float(self.get_width())
        h = float(self.get_height())
        tw = float(self._texture.get_width())
        th = float(self._texture.get_height())
        if w <= 0 or h <= 0 or tw <= 0 or th <= 0:
            return
        scale = min(w / tw, h / th)
        dw = tw * scale
        dh = th * scale
        dx = (w - dw) / 2.0
        dy = (h - dh) / 2.0
        rect = Graphene.Rect().init(dx, dy, dw, dh)
        snapshot.append_texture(self._texture, rect)


class MangaReaderApp(Adw.Application):
    def __init__(
        self,
        db_path: Path | None = None,
        library_root: Path | None = None,
        open_target: tuple[str, str, int] | None = None,
        resume_on_start: bool = False,
    ) -> None:
        super().__init__(application_id="dev.hobi.MangaReader")
        if library_root is not None:
            self.library_root = Path(library_root).expanduser().resolve()
        elif db_path is not None and not os.environ.get("MANGA_LIBRARY_ROOT", "").strip():
            self.library_root = Path(db_path).expanduser().resolve().parent
        else:
            self.library_root = resolve_library_root()
        self.db_path = resolve_db_path(db_path, library_root=self.library_root)
        self.open_target = open_target
        self.resume_on_start = resume_on_start

        self.win: Adw.ApplicationWindow | None = None
        self.toast_overlay: Adw.ToastOverlay | None = None
        self.nav_view: Adw.NavigationView | None = None

        # Kutuphane
        self.library_list: Gtk.ListBox | None = None
        self.library_stack: Gtk.Stack | None = None
        self.empty_status_page: Adw.StatusPage | None = None
        self.library_count_label: Gtk.Label | None = None
        self.library_window_title: Adw.WindowTitle | None = None
        self.search_bar: Gtk.SearchBar | None = None
        self.search_entry: Gtk.SearchEntry | None = None
        self._library_items: list[dict[str, Any]] = []
        self._library_query: str = ""
        self._row_keys: dict[Any, str] = {}
        self.resume_card: Gtk.Box | None = None
        self.resume_title: Gtk.Label | None = None
        self.resume_label: Gtk.Label | None = None
        self.resume_progress: Gtk.ProgressBar | None = None
        self.resume_btn: Gtk.Button | None = None

        # Bolumler
        self.chapters_page: Adw.NavigationPage | None = None
        self.chapters_listbox: Gtk.ListBox | None = None
        self.chapters_manga_id: int | None = None
        self.chapters_manga_title: str = ""
        self.chapters_window_title: Adw.WindowTitle | None = None
        self.chapters_stats_label: Gtk.Label | None = None
        self.chapters_progress: Gtk.ProgressBar | None = None
        self.chapters_continue_btn: Gtk.Button | None = None
        self.chapters_desc: bool = False

        # Okuyucu
        self.reader_page: Adw.NavigationPage | None = None
        self.reader_toolbar: Adw.ToolbarView | None = None
        self.reader_title: Adw.WindowTitle | None = None
        self.page_picture: MangaPageWidget | None = None
        self.page_scroll: Gtk.ScrolledWindow | None = None
        self.page_osd: Gtk.Label | None = None
        self.page_label: Gtk.Label | None = None
        self.page_scale: Gtk.Scale | None = None
        self.prev_ch_btn: Gtk.Button | None = None
        self.prev_pg_btn: Gtk.Button | None = None
        self.next_pg_btn: Gtk.Button | None = None
        self.next_ch_btn: Gtk.Button | None = None
        self.zoom_out_btn: Gtk.Button | None = None
        self.zoom_reset_btn: Gtk.Button | None = None
        self.zoom_in_btn: Gtk.Button | None = None
        self.zoom_label: Gtk.Label | None = None
        self.rtl_btn: Gtk.ToggleButton | None = None

        self.current_manga_id: int | None = None
        self.current_manga_title: str = ""
        self.current_chapters: list[dict[str, Any]] = []
        self.current_chapter_idx: int = 0
        self.poppler_doc: Poppler.Document | None = None
        self.current_page_idx: int = 0
        self.total_pages: int = 0
        self.zoom_scale: float = 1.0
        self.rtl_mode: bool = bool(load_app_config().get("rtl_mode", False))

        self.render_cache = PageRenderCache(max_entries=5)
        self._render_generation: int = 0
        self._current_tex_size: tuple[int, int] = (0, 0)
        self._suppress_next_tap: bool = False
        self._slider_debounce_id: int = 0
        self._scale_handler: int = 0
        self._chrome_visible: bool = True
        self._boundary_toast: Adw.Toast | None = None

        if GLibUnix is not None:
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_sigterm)
        else:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_sigterm)

    # ------------------------------------------------------------------ yasam dongusu
    def _on_sigterm(self) -> bool:
        self._persist_active_page()
        self.render_cache.shutdown()
        if self.win is not None:
            self.win.close()
        self.quit()
        return GLib.SOURCE_REMOVE

    def _persist_active_page(self) -> None:
        if self.poppler_doc is not None and 0 <= self.current_chapter_idx < len(self.current_chapters):
            ch = self.current_chapters[self.current_chapter_idx]
            save_reading_progress(
                chapter_id=ch["id"],
                current_page=self.current_page_idx + 1,
                total_pages=self.total_pages,
                db_path=self.db_path,
            )

    def _toast(self, text: str, button: str | None = None, on_click: Callable[[], None] | None = None) -> Adw.Toast | None:
        if self.toast_overlay is None:
            return None
        toast = Adw.Toast(title=_esc(text))
        toast.set_timeout(3)
        if button and on_click is not None:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: on_click())
        self.toast_overlay.add_toast(toast)
        return toast

    def do_activate(self) -> None:
        init_reader_db(self.db_path)
        sync_disk_library(self.library_root, self.db_path)

        if self.win is not None:
            self.win.present()
            return

        css = Gtk.CssProvider()
        css.load_from_data(_APP_CSS)
        disp = Gdk.Display.get_default()
        if disp is not None:
            Gtk.StyleContext.add_provider_for_display(
                disp, css, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )

        self.win = Adw.ApplicationWindow(application=self)
        self.win.set_title("Manga Okuyucu")
        self.win.set_default_size(360, 720)
        self.win.connect("close-request", self._on_close_request)

        self.nav_view = Adw.NavigationView()
        self.nav_view.connect("popped", self._on_nav_popped)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.nav_view)
        self.win.set_content(self.toast_overlay)
        self.nav_view.push(self._build_library_page())
        self._refresh_library(resync=False)
        self.win.present()

        if self.open_target is not None:
            manga_q, ch_num, page_1based = self.open_target
            self._open_direct(manga_q, ch_num, page_1based)
        elif self.resume_on_start:
            resume = get_resume_target(self.db_path)
            if resume:
                self._open_chapters(resume["manga_id"], resume["manga_title"])
                for idx, ch in enumerate(self.current_chapters):
                    if ch["id"] == resume["chapter_id"]:
                        self._open_reader(idx, start_page=max(0, resume["current_page"] - 1))
                        print(
                            f"[RESUME] {resume['manga_title']} | Bolum {resume['chapter_number']} | "
                            f"Sayfa {resume['current_page']}/{self.total_pages}",
                            flush=True,
                        )
                        break

    def _on_close_request(self, *_args) -> bool:
        self._persist_active_page()
        self.render_cache.shutdown()
        return False

    def _on_nav_popped(self, _nav_view: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        if page is self.reader_page:
            self._persist_active_page()
            self.reader_page = None
            self.reader_toolbar = None
            if self.chapters_manga_id is not None:
                self._load_manga_chapters(self.chapters_manga_id, self.chapters_manga_title)
                self._populate_chapters_listbox()
        self._refresh_library(resync=False)

    def _exit_reader(self) -> None:
        self._persist_active_page()
        if self._boundary_toast is not None:
            self._boundary_toast.dismiss()
            self._boundary_toast = None
        if self.reader_page is not None:
            self.reader_page.set_can_pop(True)
        if self.nav_view is not None:
            self.nav_view.pop()

    def _open_direct(self, manga_query: str, chapter_num: str, page_1based: int) -> None:
        q_low = manga_query.strip().lower()
        target_m = None
        for m in load_library_summary(self.db_path):
            if m["slug"].lower() == q_low or m["title"].lower() == q_low:
                target_m = m
                break
        if target_m is None:
            print(f"[!] Manga bulunamadi: {manga_query}", file=sys.stderr, flush=True)
            return
        self._open_chapters(int(target_m["id"]), target_m["title"])
        for idx, ch in enumerate(self.current_chapters):
            if str(ch["chapter_number"]) == str(chapter_num):
                self._open_reader(idx, start_page=max(0, page_1based - 1))
                print(
                    f"[OPEN] {target_m['title']} | Bolum {ch['chapter_number']} | "
                    f"Sayfa {self.current_page_idx + 1}/{self.total_pages}",
                    flush=True,
                )
                return
        print(f"[!] Bolum bulunamadi: {chapter_num}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------ kutuphane
    def _build_library_page(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.library_window_title = Adw.WindowTitle(title="Kütüphane", subtitle="")
        header.set_title_widget(self.library_window_title)

        search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic")
        search_btn.add_css_class("reader-touch-btn")
        search_btn.set_tooltip_text("Seri ara")
        header.pack_start(search_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.add_css_class("reader-touch-btn")
        refresh_btn.set_tooltip_text("Klasörü yeniden tara")
        refresh_btn.connect("clicked", lambda *_: self._refresh_library(resync=True, announce=True))

        settings_btn = Gtk.Button(icon_name="folder-symbolic")
        settings_btn.add_css_class("reader-touch-btn")
        settings_btn.set_tooltip_text("Kütüphane dizini ve ayarlar")
        settings_btn.connect("clicked", lambda *_: self._open_settings_dialog())

        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("reader-touch-btn")
        add_btn.add_css_class("suggested-action")
        add_btn.set_tooltip_text("Manga / PDF yükle")
        add_btn.connect("clicked", lambda *_: self._open_import_dialog())

        header.pack_end(add_btn)
        header.pack_end(refresh_btn)
        header.pack_end(settings_btn)
        toolbar.add_top_bar(header)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Seri adı yaz…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_child(self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        search_btn.bind_property(
            "active", self.search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        toolbar.add_top_bar(self.search_bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(12)
        content.set_margin_bottom(24)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Kaldigin yerden karti
        self.resume_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.resume_card.add_css_class("hero-card")
        eyebrow = Gtk.Label(label="KALDIĞIN YERDEN", xalign=0)
        eyebrow.add_css_class("hero-eyebrow")
        self.resume_title = Gtk.Label(label="", xalign=0)
        self.resume_title.add_css_class("hero-title")
        self.resume_title.set_wrap(True)
        self.resume_label = Gtk.Label(label="Henüz okuma geçmişi yok", xalign=0)
        self.resume_label.add_css_class("hero-sub")
        self.resume_label.set_wrap(True)
        self.resume_progress = Gtk.ProgressBar()
        self.resume_progress.set_margin_top(4)
        self.resume_btn = Gtk.Button()
        self.resume_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Okumaya Devam Et"))
        self.resume_btn.add_css_class("hero-btn")
        self.resume_btn.add_css_class("opaque")
        self.resume_btn.set_margin_top(6)
        self.resume_btn.connect("clicked", self._on_resume_clicked)
        for w in (eyebrow, self.resume_title, self.resume_label, self.resume_progress, self.resume_btn):
            self.resume_card.append(w)
        self.resume_card.set_visible(False)
        content.append(self.resume_card)

        # Bolum basligi
        sec = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sec_title = Gtk.Label(label="Seriler", xalign=0)
        sec_title.add_css_class("section-title")
        self.library_count_label = Gtk.Label(label="0")
        self.library_count_label.add_css_class("count-badge")
        sec.append(sec_title)
        sec.append(self.library_count_label)
        content.append(sec)

        self.library_list = Gtk.ListBox()
        self.library_list.add_css_class("boxed-list")
        self.library_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.library_list.set_filter_func(self._library_filter)

        empty_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        empty_actions.set_halign(Gtk.Align.CENTER)
        empty_import_btn = Gtk.Button(label="Manga Yükle")
        empty_import_btn.add_css_class("pill")
        empty_import_btn.add_css_class("suggested-action")
        empty_import_btn.add_css_class("reader-nav-btn")
        empty_import_btn.connect("clicked", lambda *_: self._open_import_dialog())
        empty_dir_btn = Gtk.Button(label="Kütüphane Klasörünü Seç")
        empty_dir_btn.add_css_class("pill")
        empty_dir_btn.add_css_class("reader-nav-btn")
        empty_dir_btn.connect("clicked", lambda *_: self._open_settings_dialog())
        empty_actions.append(empty_import_btn)
        empty_actions.append(empty_dir_btn)

        empty = Adw.StatusPage(
            icon_name="folder-documents-symbolic",
            title="Kütüphane boş",
            description=_esc(f"PDF klasörlerini şuraya kopyala:\n{self.library_root}\n\nÖrn: <Seri>/tr/1.pdf"),
            child=empty_actions,
        )
        self.empty_status_page = empty
        no_hit = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="Sonuç yok",
            description="Farklı bir arama dene.",
        )
        self.library_stack = Gtk.Stack()
        self.library_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.library_stack.set_vhomogeneous(False)
        self.library_stack.set_hhomogeneous(False)
        self.library_stack.add_named(self.library_list, "list")
        self.library_stack.add_named(empty, "empty")
        self.library_stack.add_named(no_hit, "nohit")
        content.append(self.library_stack)

        clamp = Adw.Clamp(maximum_size=640)
        clamp.set_child(content)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        toolbar.set_content(scrolled)
        return Adw.NavigationPage(title="Kütüphane", child=toolbar)

    def _library_filter(self, row: Gtk.ListBoxRow) -> bool:
        q = self._library_query
        if not q:
            return True
        return q in self._row_keys.get(row, "")

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._library_query = entry.get_text().strip().casefold()
        if self.library_list is not None:
            self.library_list.invalidate_filter()
        self._update_library_stack()

    def _update_library_stack(self) -> None:
        if self.library_stack is None:
            return
        if not self._library_items:
            self.library_stack.set_visible_child_name("empty")
        elif not filter_library(self._library_items, self._library_query):
            self.library_stack.set_visible_child_name("nohit")
        else:
            self.library_stack.set_visible_child_name("list")

    def _refresh_library(self, resync: bool = True, announce: bool = False) -> None:
        if resync:
            res = sync_disk_library(self.library_root, self.db_path)
            if announce:
                if res["added_mangas"] or res["added_chapters"]:
                    self._toast(f"{res['added_mangas']} yeni seri, {res['added_chapters']} yeni bölüm eklendi")
                else:
                    self._toast("Kütüphane güncel")
        if self.library_list is None:
            return
        while (child := self.library_list.get_first_child()) is not None:
            self.library_list.remove(child)
        self._row_keys.clear()

        resume = get_resume_target(self.db_path)
        if self.resume_card is not None and self.resume_btn is not None:
            if resume:
                self.resume_title.set_text(resume["manga_title"])
                self.resume_label.set_text(
                    f"Bölüm {resume['chapter_number']} • Sayfa {resume['current_page']}/{resume['total_pages']}"
                )
                self.resume_progress.set_fraction(reading_fraction(resume["current_page"], resume["total_pages"]))
                self.resume_btn.set_sensitive(True)
                self.resume_card.set_visible(True)
            else:
                self.resume_label.set_text("Henüz okuma geçmişi yok")
                self.resume_btn.set_sensitive(False)
                self.resume_card.set_visible(False)

        self._library_items = load_library_summary(self.db_path)
        total_ch = 0
        for m in self._library_items:
            total = int(m["chapter_count"] or 0)
            done = int(m.get("completed_count") or 0)
            started = int(m.get("started_count") or 0)
            total_ch += total
            parts = [f"{total} bölüm"]
            if done:
                parts.append(f"{done} okundu")
            elif started:
                parts.append("okunuyor")
            else:
                parts.append("yeni")
            row = Adw.ActionRow(title=_esc(m["title"]), subtitle=_esc(" • ".join(parts)))
            row.add_css_class("big-row")
            row.set_activatable(True)
            avatar = Adw.Avatar(size=44, text=m["title"], show_initials=True)
            row.add_prefix(avatar)
            if total > 0 and (done or started):
                pct = int(round(reading_fraction(done, total) * 100))
                badge = Gtk.Label(label="✓" if done >= total else f"%{pct}")
                badge.add_css_class("pct-badge")
                if done >= total:
                    badge.add_css_class("done")
                badge.set_valign(Gtk.Align.CENTER)
                row.add_suffix(badge)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            mid = int(m["id"])
            mtitle = m["title"]
            row.connect("activated", lambda _r, i=mid, t=mtitle: self._open_chapters(i, t))
            self._row_keys[row] = f"{m['title']} {m['slug']}".casefold()
            self.library_list.append(row)

        n = len(self._library_items)
        if self.empty_status_page is not None:
            self.empty_status_page.set_description(
                _esc(f"PDF klasörlerini şuraya kopyala:\n{self.library_root}\n\nÖrn: <Seri>/tr/1.pdf")
            )
        if self.library_count_label is not None:
            self.library_count_label.set_text(str(n))
        if self.library_window_title is not None:
            self.library_window_title.set_subtitle(f"{n} seri • {total_ch} bölüm" if n else "")
        self.library_list.invalidate_filter()
        self._update_library_stack()

    def _on_resume_clicked(self, *_args) -> None:
        resume = get_resume_target(self.db_path)
        if not resume:
            return
        self._open_chapters(resume["manga_id"], resume["manga_title"])
        for idx, ch in enumerate(self.current_chapters):
            if ch["id"] == resume["chapter_id"]:
                self._open_reader(idx, start_page=max(0, resume["current_page"] - 1))
                return

    # ------------------------------------------------------------------ bolumler
    def _load_manga_chapters(self, manga_id: int, manga_title: str) -> None:
        self.current_manga_id = manga_id
        self.current_manga_title = manga_title
        with _connect_db(self.db_path) as conn:
            chapters = conn.execute(
                """
                SELECT c.id, c.chapter_number, c.page_count, c.library_pdf_path,
                       rp.current_page, rp.total_pages AS rp_total_pages, rp.is_completed,
                       rp.last_read_at
                FROM chapters c
                LEFT JOIN reading_progress rp ON rp.chapter_id = c.id
                WHERE c.manga_id = ?
                ORDER BY c.chapter_sort
                """,
                (manga_id,),
            ).fetchall()

        self.current_chapters = []
        for c in chapters:
            resolved = resolve_chapter_pdf(
                self.library_root, manga_title, c["chapter_number"], c["library_pdf_path"]
            )
            if resolved is None:
                continue
            pg_count = int(c["page_count"] or c["rp_total_pages"] or 0)
            self.current_chapters.append(
                {
                    "id": int(c["id"]),
                    "chapter_number": c["chapter_number"],
                    "page_count": pg_count,
                    "pdf_path": str(resolved),
                    "current_page": int(c["current_page"] or 0),
                    "is_completed": bool(c["is_completed"]),
                    "last_read_at": c["last_read_at"] or "",
                }
            )

    def _update_chapters_header(self) -> None:
        total = len(self.current_chapters)
        done = sum(1 for c in self.current_chapters if c["is_completed"])
        if self.chapters_window_title is not None:
            self.chapters_window_title.set_subtitle(f"{total} bölüm")
        if self.chapters_stats_label is not None:
            self.chapters_stats_label.set_text(f"{done} / {total} bölüm okundu")
        if self.chapters_progress is not None:
            self.chapters_progress.set_fraction(reading_fraction(done, total))
        if self.chapters_continue_btn is not None:
            target = pick_continue_chapter(self.current_chapters)
            started = any(c["last_read_at"] for c in self.current_chapters)
            if target is None:
                label = "Okunacak bölüm yok"
            elif not started:
                label = "Okumaya Başla"
            else:
                label = f"Devam Et • Bölüm {self.current_chapters[target[0]]['chapter_number']}"
            self.chapters_continue_btn.set_child(
                Adw.ButtonContent(icon_name="media-playback-start-symbolic", label=label)
            )
            self.chapters_continue_btn.set_sensitive(target is not None)

    def _populate_chapters_listbox(self) -> None:
        self._update_chapters_header()
        if self.chapters_listbox is None:
            return
        while (child := self.chapters_listbox.get_first_child()) is not None:
            self.chapters_listbox.remove(child)

        order = list(range(len(self.current_chapters)))
        if self.chapters_desc:
            order.reverse()
        for idx in order:
            ch = self.current_chapters[idx]
            pg_total = ch["page_count"] or 0
            if ch["is_completed"]:
                sub = f"Okundu • {pg_total} sayfa" if pg_total else "Okundu"
                icon, icon_css = "object-select-symbolic", "status-done"
            elif ch["current_page"] > 0:
                sub = f"Sayfa {ch['current_page']}/{pg_total or '?'}"
                icon, icon_css = "media-playback-start-symbolic", "status-reading"
            else:
                sub = f"{pg_total} sayfa" if pg_total else "Yeni"
                icon, icon_css = None, ""
            row = Adw.ActionRow(title=_esc(f"Bölüm {ch['chapter_number']}"), subtitle=_esc(sub))
            row.add_css_class("big-row")
            row.set_activatable(True)
            if icon:
                img = Gtk.Image.new_from_icon_name(icon)
                img.add_css_class(icon_css)
                row.add_suffix(img)
            if not ch["is_completed"] and ch["current_page"] > 0 and pg_total:
                bar = Gtk.ProgressBar()
                bar.add_css_class("thin")
                bar.set_fraction(reading_fraction(ch["current_page"], pg_total))
                bar.set_size_request(56, -1)
                bar.set_valign(Gtk.Align.CENTER)
                row.add_suffix(bar)
            if ch["is_completed"]:
                row.set_opacity(0.7)
            row.connect("activated", lambda _r, i=idx: self._open_chapter_from_list(i))
            self.chapters_listbox.append(row)

    def _open_chapter_from_list(self, idx: int) -> None:
        if 0 <= idx < len(self.current_chapters):
            ch = self.current_chapters[idx]
            start_pg = (
                max(0, ch["current_page"] - 1)
                if ch["current_page"] > 0 and not ch["is_completed"]
                else 0
            )
            self._open_reader(idx, start_pg)

    def _on_continue_clicked(self, *_args) -> None:
        target = pick_continue_chapter(self.current_chapters)
        if target is not None:
            self._open_reader(target[0], target[1])

    def _toggle_chapter_sort(self, btn: Gtk.Button) -> None:
        self.chapters_desc = not self.chapters_desc
        btn.set_icon_name("view-sort-descending-symbolic" if self.chapters_desc else "view-sort-ascending-symbolic")
        self._populate_chapters_listbox()

    def _open_chapters(self, manga_id: int, title: str) -> None:
        self.chapters_manga_id = manga_id
        self.chapters_manga_title = title
        self._load_manga_chapters(manga_id, title)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.chapters_window_title = Adw.WindowTitle(title=title, subtitle="")
        header.set_title_widget(self.chapters_window_title)
        sort_btn = Gtk.Button(
            icon_name="view-sort-descending-symbolic" if self.chapters_desc else "view-sort-ascending-symbolic"
        )
        sort_btn.add_css_class("reader-touch-btn")
        sort_btn.set_tooltip_text("Sıralamayı değiştir")
        sort_btn.connect("clicked", self._toggle_chapter_sort)

        m_lang = _LANG_TITLE_RE.fullmatch(title)
        preset_title = m_lang.group(1) if m_lang else title
        preset_lang = m_lang.group(2).lower() if m_lang else ""
        add_ch_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_ch_btn.add_css_class("reader-touch-btn")
        add_ch_btn.set_tooltip_text("Bu seriye bölüm ekle")
        add_ch_btn.connect(
            "clicked",
            lambda *_, pt=preset_title, pl=preset_lang: self._open_import_dialog(
                preset_title=pt, preset_lang=pl
            ),
        )

        header.pack_end(add_ch_btn)
        header.pack_end(sort_btn)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(12)
        content.set_margin_bottom(24)
        content.set_margin_start(12)
        content.set_margin_end(12)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hero.add_css_class("hero-card")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        top.append(Adw.Avatar(size=56, text=title, show_initials=True))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info.set_valign(Gtk.Align.CENTER)
        t_lbl = Gtk.Label(label=title, xalign=0)
        t_lbl.add_css_class("hero-title")
        t_lbl.set_wrap(True)
        self.chapters_stats_label = Gtk.Label(label="", xalign=0)
        self.chapters_stats_label.add_css_class("hero-sub")
        info.append(t_lbl)
        info.append(self.chapters_stats_label)
        top.append(info)
        hero.append(top)
        self.chapters_progress = Gtk.ProgressBar()
        self.chapters_progress.set_margin_top(4)
        hero.append(self.chapters_progress)
        self.chapters_continue_btn = Gtk.Button()
        self.chapters_continue_btn.add_css_class("hero-btn")
        self.chapters_continue_btn.add_css_class("opaque")
        self.chapters_continue_btn.set_margin_top(6)
        self.chapters_continue_btn.connect("clicked", self._on_continue_clicked)
        hero.append(self.chapters_continue_btn)
        content.append(hero)

        self.chapters_listbox = Gtk.ListBox()
        self.chapters_listbox.add_css_class("boxed-list")
        self.chapters_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        if self.current_chapters:
            content.append(self.chapters_listbox)
        else:
            content.append(
                Adw.StatusPage(
                    icon_name="dialog-warning-symbolic",
                    title="PDF bulunamadı",
                    description="Bu serinin bölüm dosyaları diskte yok.",
                )
            )
        self._populate_chapters_listbox()

        clamp = Adw.Clamp(maximum_size=640)
        clamp.set_child(content)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        toolbar.set_content(scrolled)
        self.chapters_page = Adw.NavigationPage(title=title, child=toolbar)
        if self.nav_view:
            self.nav_view.push(self.chapters_page)

    # ------------------------------------------------------------------ okuyucu
    def _open_reader(self, chapter_idx: int, start_page: int = 0) -> None:
        if not (0 <= chapter_idx < len(self.current_chapters)):
            return
        self.current_chapter_idx = chapter_idx
        ch = self.current_chapters[chapter_idx]
        pdf_uri = Path(ch["pdf_path"]).resolve().as_uri()
        self.poppler_doc = Poppler.Document.new_from_file(pdf_uri, None)
        self.total_pages = self.poppler_doc.get_n_pages()
        self.current_page_idx = max(0, min(max(0, self.total_pages - 1), start_page))
        self.zoom_scale = 1.0
        self._chrome_visible = True

        toolbar = Adw.ToolbarView()
        self.reader_toolbar = toolbar
        header = Adw.HeaderBar()
        header.set_show_back_button(False)

        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.add_css_class("reader-touch-btn")
        back_btn.set_tooltip_text("Bölüm listesine dön")
        back_btn.connect("clicked", lambda *_: self._exit_reader())
        header.pack_start(back_btn)

        self.reader_title = Adw.WindowTitle(
            title=self.current_manga_title, subtitle=f"Bölüm {ch['chapter_number']}"
        )
        header.set_title_widget(self.reader_title)

        # Zoom: header'i sikistirmamak icin acilir panelde
        self.zoom_out_btn = Gtk.Button(icon_name="zoom-out-symbolic")
        self.zoom_out_btn.add_css_class("reader-touch-btn")
        self.zoom_out_btn.connect("clicked", lambda *_: self._change_zoom(-0.25))
        self.zoom_label = Gtk.Label(label="100%")
        self.zoom_label.set_width_chars(5)
        self.zoom_in_btn = Gtk.Button(icon_name="zoom-in-symbolic")
        self.zoom_in_btn.add_css_class("reader-touch-btn")
        self.zoom_in_btn.connect("clicked", lambda *_: self._change_zoom(0.25))
        self.zoom_reset_btn = Gtk.Button(icon_name="zoom-original-symbolic")
        self.zoom_reset_btn.add_css_class("reader-touch-btn")
        self.zoom_reset_btn.set_tooltip_text("Ekrana sığdır")
        self.zoom_reset_btn.connect("clicked", lambda *_: self._reset_zoom())
        zbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for w in (self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.zoom_reset_btn):
            zbox.append(w)
        zoom_pop = Gtk.Popover()
        zoom_pop.set_child(zbox)
        zoom_menu = Gtk.MenuButton(icon_name="zoom-in-symbolic", popover=zoom_pop)
        zoom_menu.add_css_class("reader-touch-btn")
        zoom_menu.set_tooltip_text("Yakınlaştır")

        self.rtl_btn = Gtk.ToggleButton(label="RTL")
        self.rtl_btn.add_css_class("reader-touch-btn")
        self.rtl_btn.set_tooltip_text("Sağdan sola okuma (manga yönü)")
        self.rtl_btn.set_active(self.rtl_mode)
        self.rtl_btn.connect("toggled", self._on_rtl_toggled)

        header.pack_end(zoom_menu)
        header.pack_end(self.rtl_btn)
        toolbar.add_top_bar(header)

        self.page_picture = MangaPageWidget()
        self.page_picture.set_hexpand(True)
        self.page_picture.set_vexpand(True)

        self.page_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.page_scroll.add_css_class("reader-surface")
        self.page_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.page_scroll.set_kinetic_scrolling(False)
        self.page_scroll.set_child(self.page_picture)

        drag = Gtk.GestureDrag()
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-end", self._on_drag_end)
        self.page_scroll.add_controller(drag)

        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("released", self._on_picture_tap)
        self.page_scroll.add_controller(click)

        overlay = Gtk.Overlay()
        overlay.set_child(self.page_scroll)
        self.page_osd = Gtk.Label(label="")
        self.page_osd.add_css_class("osd-badge")
        self.page_osd.set_halign(Gtk.Align.END)
        self.page_osd.set_valign(Gtk.Align.END)
        self.page_osd.set_margin_end(14)
        self.page_osd.set_margin_bottom(14)
        self.page_osd.set_can_target(False)
        self.page_osd.set_visible(False)
        overlay.add_overlay(self.page_osd)
        toolbar.set_content(overlay)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        controls.set_margin_top(6)
        controls.set_margin_bottom(8)
        controls.set_margin_start(10)
        controls.set_margin_end(10)

        slider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        upper_bound = max(2, self.total_pages)
        self.page_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, upper_bound, 1)
        self.page_scale.add_css_class("reader-slider")
        self.page_scale.set_hexpand(True)
        self.page_scale.set_draw_value(False)
        self.page_scale.set_sensitive(self.total_pages > 1)
        self._scale_handler = self.page_scale.connect("value-changed", self._on_slider_changed)

        self.page_label = Gtk.Label(label="1 / 1")
        self.page_label.add_css_class("numeric")
        self.page_label.set_width_chars(8)
        slider_row.append(self.page_scale)
        slider_row.append(self.page_label)
        controls.append(slider_row)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.prev_ch_btn = Gtk.Button(icon_name="media-skip-backward-symbolic")
        self.prev_ch_btn.set_tooltip_text("Önceki bölüm")
        self.prev_ch_btn.add_css_class("reader-nav-btn")
        self.prev_ch_btn.connect("clicked", lambda *_: self._switch_chapter(-1))

        self.prev_pg_btn = Gtk.Button()
        self.prev_pg_btn.set_child(Adw.ButtonContent(icon_name="go-previous-symbolic", label="Önceki"))
        self.prev_pg_btn.add_css_class("reader-nav-btn")
        self.prev_pg_btn.set_hexpand(True)
        self.prev_pg_btn.connect("clicked", lambda *_: self._step_page(-1))

        self.next_pg_btn = Gtk.Button()
        self.next_pg_btn.set_child(Adw.ButtonContent(icon_name="go-next-symbolic", label="Sonraki"))
        self.next_pg_btn.add_css_class("reader-nav-btn")
        self.next_pg_btn.add_css_class("suggested-action")
        self.next_pg_btn.set_hexpand(True)
        self.next_pg_btn.connect("clicked", lambda *_: self._step_page(1))

        self.next_ch_btn = Gtk.Button(icon_name="media-skip-forward-symbolic")
        self.next_ch_btn.set_tooltip_text("Sonraki bölüm")
        self.next_ch_btn.add_css_class("reader-nav-btn")
        self.next_ch_btn.connect("clicked", lambda *_: self._switch_chapter(1))

        for w in (self.prev_ch_btn, self.prev_pg_btn, self.next_pg_btn, self.next_ch_btn):
            btn_row.append(w)
        controls.append(btn_row)
        toolbar.add_bottom_bar(controls)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        toolbar.add_controller(key_ctrl)

        self.reader_page = Adw.NavigationPage(
            title=f"Bölüm {ch['chapter_number']}", child=toolbar
        )
        self.reader_page.set_can_pop(False)
        if self.nav_view:
            self.nav_view.push(self.reader_page)

        self._render_current_page()

    def _on_rtl_toggled(self, btn: Gtk.ToggleButton) -> None:
        self.rtl_mode = btn.get_active()
        try:
            save_app_config({"rtl_mode": self.rtl_mode})
        except Exception:
            pass
        self._toast("Sağdan sola okuma açık" if self.rtl_mode else "Soldan sağa okuma açık")

    # ------------------------------------------------------------------ yukleme ve dizin yonetimi
    def _open_folder_externally(self, path: Path) -> None:
        """Verilen dizini olusturup sistem dosya yoneticisinde acar."""
        target = Path(path).expanduser().resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._toast(f"Dizin oluşturulamadı: {exc}")
            return

        if hasattr(Gtk, "FileLauncher") and self.win is not None:
            try:
                launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(target)))

                def _on_done(_obj: Any, res: Any) -> None:
                    try:
                        launcher.launch_finish(res)
                    except Exception:
                        try:
                            subprocess.Popen(["xdg-open", str(target)])
                        except Exception as exc2:
                            self._toast(f"Klasör açılamadı: {exc2}")

                launcher.launch(self.win, None, _on_done)
                return
            except Exception:
                pass
        try:
            subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            self._toast(f"Klasör açılamadı: {exc}")

    def _open_import_dialog(self, preset_title: str = "", preset_lang: str = "") -> None:
        """Kullanicinin klasor veya PDF secip hedef dizine yukleyebildigi diyalog."""
        if self.win is None:
            return

        state: dict[str, Any] = {"source_dir": None, "pdf_files": []}

        dialog = Adw.Dialog(title="Manga Yükle")
        dialog.set_content_width(360)
        dialog.set_content_height(560)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Manga Yükle", subtitle="Klasör veya PDF ekle"))
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(12)
        content.set_margin_bottom(24)
        content.set_margin_start(14)
        content.set_margin_end(14)

        # Kaynak grubu
        src_group = Adw.PreferencesGroup(title="Kaynak")
        pick_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pick_row.set_margin_bottom(8)

        pick_dir_btn = Gtk.Button()
        pick_dir_btn.set_child(Adw.ButtonContent(icon_name="folder-open-symbolic", label="Klasör Seç"))
        pick_dir_btn.add_css_class("reader-nav-btn")
        pick_dir_btn.set_hexpand(True)

        pick_pdf_btn = Gtk.Button()
        pick_pdf_btn.set_child(Adw.ButtonContent(icon_name="document-open-symbolic", label="PDF Seç"))
        pick_pdf_btn.add_css_class("reader-nav-btn")
        pick_pdf_btn.set_hexpand(True)

        pick_row.append(pick_dir_btn)
        pick_row.append(pick_pdf_btn)
        src_group.add(pick_row)

        src_entry = Adw.EntryRow(title="Kaynak dizin yolu")
        src_group.add(src_entry)

        src_status = Gtk.Label(label="Bir manga klasörü veya PDF dosyaları seçin.", xalign=0)
        src_status.add_css_class("dim-label")
        src_status.set_wrap(True)
        src_status.set_margin_top(6)
        src_group.add(src_status)
        content.append(src_group)

        # Hedef konum grubu
        dst_group = Adw.PreferencesGroup(title="Hedef Konum")
        title_entry = Adw.EntryRow(title="Seri Adı")
        if preset_title:
            title_entry.set_text(preset_title)
        lang_entry = Adw.EntryRow(title="Dil Klasörü (opsiyonel, örn: tr)")
        if preset_lang:
            lang_entry.set_text(preset_lang)
        move_switch = Adw.SwitchRow(
            title="Dosyaları taşı",
            subtitle="Kapalıysa güvenli kopya alınır",
        )
        preview_row = Adw.ActionRow(title="Yerleştirileceği Dizin", subtitle=str(self.library_root))
        preview_row.set_subtitle_lines(3)
        open_dst_btn = Gtk.Button(icon_name="folder-open-symbolic")
        open_dst_btn.add_css_class("flat")
        open_dst_btn.add_css_class("reader-touch-btn")
        open_dst_btn.set_tooltip_text("Hedef dizini dosya yöneticisinde aç")
        open_dst_btn.set_valign(Gtk.Align.CENTER)
        preview_row.add_suffix(open_dst_btn)

        for w in (title_entry, lang_entry, move_switch, preview_row):
            dst_group.add(w)
        content.append(dst_group)

        submit_btn = Gtk.Button()
        submit_btn.set_child(
            Adw.ButtonContent(icon_name="document-save-symbolic", label="Kütüphaneye Yükle ve Aç")
        )
        submit_btn.add_css_class("suggested-action")
        submit_btn.add_css_class("pill")
        submit_btn.add_css_class("reader-nav-btn")
        content.append(submit_btn)

        def _current_dest() -> Path:
            t_val = title_entry.get_text().strip()
            if not t_val and state["source_dir"] is not None:
                t_val = Path(state["source_dir"]).name
            if not t_val:
                return self.library_root
            return plan_import_destination(self.library_root, t_val, lang_entry.get_text().strip())

        def _refresh_preview(*_args) -> None:
            preview_row.set_subtitle(_esc(str(_current_dest())))

        title_entry.connect("changed", _refresh_preview)
        lang_entry.connect("changed", _refresh_preview)
        open_dst_btn.connect("clicked", lambda *_: self._open_folder_externally(_current_dest()))

        def _on_src_entry_changed(entry: Any) -> None:
            raw = entry.get_text().strip()
            if raw:
                p = Path(raw).expanduser()
                state["source_dir"] = p
                state["pdf_files"] = []
                if not title_entry.get_text().strip() and p.name:
                    title_entry.set_text(p.name)
                src_status.set_text(f"Klasör: {p}")
            else:
                state["source_dir"] = None
                if not state["pdf_files"]:
                    src_status.set_text("Bir manga klasörü veya PDF dosyaları seçin.")
            _refresh_preview()

        src_entry.connect("changed", _on_src_entry_changed)

        def _pick_folder(*_args) -> None:
            fd = Gtk.FileDialog(title="Manga Klasörü Seç")
            fd.select_folder(
                self.win,
                None,
                lambda d, res: _on_folder_picked(d, res),
            )

        def _on_folder_picked(fd: Gtk.FileDialog, res: Any) -> None:
            try:
                folder = fd.select_folder_finish(res)
            except Exception:
                return
            if folder is None or not folder.get_path():
                return
            p = Path(folder.get_path())
            state["pdf_files"] = []
            state["source_dir"] = p
            src_entry.set_text(str(p))
            if not title_entry.get_text().strip():
                title_entry.set_text(p.name)
            src_status.set_text(f"Seçilen klasör: {p.name}")
            _refresh_preview()

        def _pick_pdfs(*_args) -> None:
            fd = Gtk.FileDialog(title="PDF Dosyaları Seç")
            filt = Gtk.FileFilter()
            filt.set_name("PDF Dosyaları")
            filt.add_pattern("*.pdf")
            filt.add_pattern("*.PDF")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filt)
            fd.set_filters(filters)
            fd.open_multiple(
                self.win,
                None,
                lambda d, res: _on_pdfs_picked(d, res),
            )

        def _on_pdfs_picked(fd: Gtk.FileDialog, res: Any) -> None:
            try:
                files = fd.open_multiple_finish(res)
            except Exception:
                return
            paths = []
            for i in range(files.get_n_items()):
                gfile = files.get_item(i)
                if gfile is not None and gfile.get_path():
                    paths.append(Path(gfile.get_path()))
            if not paths:
                return
            state["source_dir"] = None
            state["pdf_files"] = paths
            src_entry.set_text("")
            if not title_entry.get_text().strip():
                title_entry.set_text(paths[0].parent.name or paths[0].stem)
            src_status.set_text(f"{len(paths)} PDF seçildi ({paths[0].parent.name})")
            _refresh_preview()

        pick_dir_btn.connect("clicked", _pick_folder)
        pick_pdf_btn.connect("clicked", _pick_pdfs)

        def _on_submit(*_args) -> None:
            move_mode = bool(move_switch.get_active())
            m_title = title_entry.get_text().strip()
            m_lang = lang_entry.get_text().strip()
            try:
                if state["pdf_files"]:
                    res = import_pdf_files(
                        state["pdf_files"],
                        library_root=self.library_root,
                        db_path=self.db_path,
                        manga_title=m_title or state["pdf_files"][0].parent.name,
                        lang=m_lang,
                        move=move_mode,
                    )
                elif state["source_dir"] is not None:
                    res = import_manga_folder(
                        Path(state["source_dir"]),
                        library_root=self.library_root,
                        db_path=self.db_path,
                        manga_title=m_title,
                        lang=m_lang,
                        move=move_mode,
                    )
                else:
                    self._toast("Önce bir klasör veya PDF dosyası seçin")
                    return
            except Exception as exc:
                self._toast(f"Yükleme hatası: {exc}")
                return

            dialog.close()
            self._refresh_library(resync=False)
            self._toast(f"{res['imported_files']} PDF yüklendi → {res['dest_dir']}")

            mid = res.get("manga_id")
            mname = res.get("manga_title") or ""
            if mid is not None:
                if self.chapters_page is not None and self.chapters_manga_id == mid:
                    self._load_manga_chapters(mid, mname)
                    self._populate_chapters_listbox()
                else:
                    self._open_chapters(int(mid), str(mname))

        submit_btn.connect("clicked", _on_submit)
        _refresh_preview()

        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(content)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)
        dialog.present(self.win)

    def _open_settings_dialog(self) -> None:
        """Gercek Linux kutuphane dizinini goruntuleme, secme ve acma diyalogu."""
        if self.win is None:
            return

        dialog = Adw.Dialog(title="Kütüphane Dizini")
        dialog.set_content_width(360)
        dialog.set_content_height(500)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Kütüphane Dizini", subtitle="Gerçek Linux yolu"))
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(12)
        content.set_margin_bottom(24)
        content.set_margin_start(14)
        content.set_margin_end(14)

        dir_group = Adw.PreferencesGroup(title="Aktif Dizinler")
        dir_entry = Adw.EntryRow(title="Kütüphane Ana Dizini")
        dir_entry.set_text(str(self.library_root))

        browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
        browse_btn.add_css_class("flat")
        browse_btn.add_css_class("reader-touch-btn")
        browse_btn.set_tooltip_text("Klasör seç")
        browse_btn.set_valign(Gtk.Align.CENTER)
        dir_entry.add_suffix(browse_btn)
        dir_group.add(dir_entry)

        open_row = Adw.ActionRow(
            title="Kütüphane Klasörünü Aç",
            subtitle=_esc(str(self.library_root)),
        )
        open_row.set_subtitle_lines(2)
        open_row.set_activatable(True)
        open_row.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        open_row.connect("activated", lambda *_: self._open_folder_externally(Path(dir_entry.get_text().strip() or self.library_root)))
        dir_group.add(open_row)

        db_row = Adw.ActionRow(
            title="Veritabanı (library.db)",
            subtitle=_esc(str(self.db_path)),
        )
        db_row.set_subtitle_lines(2)
        dir_group.add(db_row)

        cfg_row = Adw.ActionRow(
            title="Ayar Dosyası",
            subtitle=_esc(str(config_file_path())),
        )
        cfg_row.set_subtitle_lines(2)
        dir_group.add(cfg_row)
        content.append(dir_group)

        info_group = Adw.PreferencesGroup(title="Desteklenen Klasör Düzenleri")
        info_row = Adw.ActionRow(
            title="Otomatik Tanıma",
            subtitle=_esc("<Kök>/<Seri>/<bölüm>.pdf  veya  <Kök>/<Seri>/<dil>/<bölüm>.pdf"),
        )
        info_row.set_subtitle_lines(3)
        info_group.add(info_row)
        content.append(info_group)

        save_btn = Gtk.Button()
        save_btn.set_child(
            Adw.ButtonContent(icon_name="object-select-symbolic", label="Dizini Kaydet ve Tara")
        )
        save_btn.add_css_class("suggested-action")
        save_btn.add_css_class("pill")
        save_btn.add_css_class("reader-nav-btn")
        content.append(save_btn)

        def _pick_root(*_args) -> None:
            fd = Gtk.FileDialog(title="Kütüphane Ana Klasörünü Seç")
            fd.select_folder(
                self.win,
                None,
                lambda d, res: _on_root_picked(d, res),
            )

        def _on_root_picked(fd: Gtk.FileDialog, res: Any) -> None:
            try:
                folder = fd.select_folder_finish(res)
            except Exception:
                return
            if folder is not None and folder.get_path():
                dir_entry.set_text(folder.get_path())

        def _on_save(*_args) -> None:
            raw = dir_entry.get_text().strip()
            if not raw:
                self._toast("Geçerli bir dizin yolu girin")
                return
            try:
                new_root = Path(raw).expanduser().resolve()
                new_root.mkdir(parents=True, exist_ok=True)
                save_app_config({"library_root": str(new_root)})
                self.library_root = new_root
                self.db_path = resolve_db_path(None, library_root=new_root)
            except Exception as exc:
                self._toast(f"Dizin kaydedilemedi: {exc}")
                return
            dialog.close()
            self._refresh_library(resync=True, announce=True)

        browse_btn.connect("clicked", _pick_root)
        save_btn.connect("clicked", _on_save)

        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(content)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)
        dialog.present(self.win)

    def _set_chrome_visible(self, visible: bool) -> None:
        """Ortaya dokununca ust/alt cubuklari gizleyip tam ekran okuma saglar."""
        self._chrome_visible = visible
        if self.reader_toolbar is not None:
            self.reader_toolbar.set_reveal_top_bars(visible)
            self.reader_toolbar.set_reveal_bottom_bars(visible)
        if self.page_osd is not None:
            self.page_osd.set_visible(not visible)

    def _apply_zoom_layout(self) -> None:
        if self.page_picture is None or self.page_scroll is None:
            return
        tex_w, tex_h = self._current_tex_size
        view_w = self.page_scroll.get_width() or (self.win.get_width() if self.win else 576) or 576
        view_h = self.page_scroll.get_height() or 820
        dims = compute_zoom_dimensions(tex_w, tex_h, view_w, view_h, self.zoom_scale)
        if dims is None:
            self.page_scroll.set_kinetic_scrolling(False)
            self.page_picture.set_hexpand(True)
            self.page_picture.set_vexpand(True)
            self.page_picture.set_target_size(0, 0)
        else:
            sw, sh = dims
            self.page_scroll.set_kinetic_scrolling(True)
            self.page_picture.set_hexpand(False)
            self.page_picture.set_vexpand(False)
            self.page_picture.set_target_size(sw, sh)

    def _change_zoom(self, delta: float) -> None:
        self.zoom_scale = max(1.0, min(3.0, round(self.zoom_scale + delta, 2)))
        self._apply_zoom_layout()
        self._update_nav_buttons()

    def _reset_zoom(self) -> None:
        self.zoom_scale = 1.0
        self._apply_zoom_layout()
        self._update_nav_buttons()

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self.zoom_scale > 1.01:
            return
        delta = resolve_swipe_delta(offset_x, offset_y, self.rtl_mode)
        if delta != 0:
            self._suppress_next_tap = True
            self._step_page(delta)

    def _on_picture_tap(self, _gesture: Gtk.GestureClick, _n_press: int, x: float, _y: float) -> None:
        if self._suppress_next_tap:
            self._suppress_next_tap = False
            return
        if self.zoom_scale > 1.01 or self.page_scroll is None:
            return
        w = max(1, self.page_scroll.get_width())
        delta = resolve_tap_delta(x, w, self.rtl_mode)
        if delta != 0:
            self._step_page(delta)
        else:
            self._set_chrome_visible(not self._chrome_visible)

    def _on_key_pressed(self, _ctrl, keyval: int, _keycode: int, _state) -> bool:
        if keyval in (Gdk.KEY_Right, Gdk.KEY_space):
            self._step_page(-1 if self.rtl_mode else 1)
            return True
        if keyval == Gdk.KEY_Left:
            self._step_page(1 if self.rtl_mode else -1)
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self._set_chrome_visible(not self._chrome_visible)
            return True
        if keyval == Gdk.KEY_Escape:
            self._exit_reader()
            return True
        return False

    def _on_slider_changed(self, scale: Gtk.Scale) -> None:
        idx = compute_page_step(int(round(scale.get_value())) - 1, self.total_pages, 0)
        if idx == self.current_page_idx:
            return
        self.current_page_idx = idx
        self._update_page_labels()
        self._update_nav_buttons()

        if self._slider_debounce_id:
            GLib.source_remove(self._slider_debounce_id)
            self._slider_debounce_id = 0

        ch = self.current_chapters[self.current_chapter_idx]
        if self.render_cache.get_cached(Path(ch["pdf_path"]), idx) is not None:
            self._render_current_page()
        else:
            self._slider_debounce_id = GLib.timeout_add(60, self._flush_slider_render)

    def _flush_slider_render(self) -> bool:
        self._slider_debounce_id = 0
        self._render_current_page()
        return False

    def _update_page_labels(self) -> None:
        txt = f"{self.current_page_idx + 1} / {self.total_pages}"
        if self.page_label:
            self.page_label.set_text(txt)
        if self.page_osd:
            self.page_osd.set_text(txt)

    def _update_nav_buttons(self) -> None:
        if self.prev_pg_btn:
            self.prev_pg_btn.set_sensitive(self.current_page_idx > 0)
        if self.next_pg_btn:
            self.next_pg_btn.set_sensitive(self.current_page_idx < self.total_pages - 1)
        if self.prev_ch_btn:
            self.prev_ch_btn.set_sensitive(self.current_chapter_idx > 0)
        if self.next_ch_btn:
            self.next_ch_btn.set_sensitive(self.current_chapter_idx < len(self.current_chapters) - 1)
        if self.zoom_out_btn:
            self.zoom_out_btn.set_sensitive(self.zoom_scale > 1.01)
        if self.zoom_in_btn:
            self.zoom_in_btn.set_sensitive(self.zoom_scale < 2.99)
        if self.zoom_label:
            self.zoom_label.set_text(f"{int(round(self.zoom_scale * 100))}%")

    def _show_boundary_toast(self, delta: int) -> None:
        """Bolum sinirinda sessizce atlamak yerine kullaniciya secenek sunar."""
        if self._boundary_toast is not None:
            return
        if delta > 0 and self.current_chapter_idx < len(self.current_chapters) - 1:
            toast = self._toast("Bölüm bitti", "Sonraki Bölüm", lambda: self._switch_chapter(1))
        elif delta < 0 and self.current_chapter_idx > 0:
            toast = self._toast("Bölümün başındasın", "Önceki Bölüm", lambda: self._switch_chapter(-1))
        elif delta > 0:
            toast = self._toast("Serinin son sayfası")
        else:
            return
        if toast is not None:
            self._boundary_toast = toast
            toast.connect("dismissed", lambda *_: setattr(self, "_boundary_toast", None))

    def _step_page(self, delta: int) -> None:
        nxt = compute_page_step(self.current_page_idx, self.total_pages, delta)
        if nxt != self.current_page_idx:
            self.current_page_idx = nxt
            self._render_current_page()
        elif delta != 0:
            self._show_boundary_toast(delta)

    def _switch_chapter(self, delta: int) -> None:
        nxt_idx = self.current_chapter_idx + delta
        if not (0 <= nxt_idx < len(self.current_chapters)):
            return
        self._persist_active_page()
        self.current_chapter_idx = nxt_idx
        ch = self.current_chapters[nxt_idx]
        self.poppler_doc = Poppler.Document.new_from_file(Path(ch["pdf_path"]).resolve().as_uri(), None)
        self.total_pages = self.poppler_doc.get_n_pages()
        self.current_page_idx = 0
        self.zoom_scale = 1.0
        if self.reader_title:
            self.reader_title.set_subtitle(f"Bölüm {ch['chapter_number']}")
        if self.reader_page:
            self.reader_page.set_title(f"Bölüm {ch['chapter_number']}")
        if self.page_scale:
            self.page_scale.handler_block(self._scale_handler)
            self.page_scale.set_range(1, max(2, self.total_pages))
            self.page_scale.set_value(1)
            self.page_scale.set_sensitive(self.total_pages > 1)
            self.page_scale.handler_unblock(self._scale_handler)
        self._render_current_page()

    def _schedule_prefetch(self, pdf_path: Path, page_idx: int, gen: int, target_w: int) -> None:
        if page_idx + 1 < self.total_pages:
            self.render_cache.request_page(pdf_path, page_idx + 1, gen, priority=1, target_width=target_w)
        if page_idx - 1 >= 0:
            self.render_cache.request_page(pdf_path, page_idx - 1, gen, priority=2, target_width=target_w)
        if page_idx + 2 < self.total_pages:
            self.render_cache.request_page(pdf_path, page_idx + 2, gen, priority=2, target_width=target_w)

    def _on_page_rendered(
        self,
        gen: int,
        pdf_path: Path,
        page_idx: int,
        res: tuple[Gdk.Texture, int, int] | None,
        exc: Exception | None,
    ) -> None:
        if exc is not None:
            print(f"[!] Sayfa render hatasi ({pdf_path.name} s.{page_idx + 1}): {exc}", file=sys.stderr, flush=True)
            self._toast(f"Sayfa {page_idx + 1} açılamadı")
            return
        if gen != self._render_generation or res is None or self.page_picture is None:
            return
        tex, tw, th = res
        self._current_tex_size = (tw, th)
        self.page_picture.set_paintable(tex)
        self._apply_zoom_layout()

    def _render_current_page(self) -> None:
        if self.poppler_doc is None or self.total_pages <= 0:
            return
        ch = self.current_chapters[self.current_chapter_idx]
        pdf_path = Path(ch["pdf_path"])
        page_idx = self.current_page_idx

        self._render_generation += 1
        gen = self._render_generation

        self._update_page_labels()
        if self.page_scale:
            self.page_scale.handler_block(self._scale_handler)
            self.page_scale.set_value(page_idx + 1)
            self.page_scale.handler_unblock(self._scale_handler)
        self._update_nav_buttons()

        target_w = 900
        if self.win is not None and self.win.get_width() > 0:
            target_w = max(576, min(1200, int(self.win.get_width() * 1.5)))

        cached = self.render_cache.get_cached(pdf_path, page_idx)
        if cached is not None:
            tex, tw, th = cached
            self._current_tex_size = (tw, th)
            if self.page_picture is not None:
                self.page_picture.set_paintable(tex)
            self._apply_zoom_layout()
        else:
            self.render_cache.request_page(
                pdf_path,
                page_idx,
                gen,
                priority=0,
                target_width=target_w,
                callback=self._on_page_rendered,
            )

        self._schedule_prefetch(pdf_path, page_idx, gen, target_w)
        save_reading_progress(
            chapter_id=ch["id"],
            current_page=page_idx + 1,
            total_pages=self.total_pages,
            db_path=self.db_path,
        )


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description="Linux Mobil PDF Manga Okuyucu")
    parser.add_argument(
        "--open",
        nargs=3,
        metavar=("MANGA", "CHAPTER", "PAGE"),
        help="Belirtilen manga, bolum ve sayfayi dogrudan acar.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="library.db icindeki son kalinan manga, bolum ve sayfadan devam eder.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Kullanilacak SQLite library.db dosya yolu.",
    )
    parser.add_argument(
        "--library-dir",
        type=Path,
        default=None,
        help="Kullanilacak manga kutuphanesi ana dizini.",
    )
    return parser.parse_known_args(argv)


def main() -> None:
    args, gtk_args = parse_args(sys.argv[1:])
    open_target = None
    if args.open:
        open_target = (args.open[0], args.open[1], int(args.open[2]))
    app = MangaReaderApp(
        db_path=args.db,
        library_root=args.library_dir,
        open_target=open_target,
        resume_on_start=args.resume,
    )
    sys.exit(app.run([sys.argv[0], *gtk_args]))


if __name__ == "__main__":
    main()

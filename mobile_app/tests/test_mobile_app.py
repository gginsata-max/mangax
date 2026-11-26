"""mobile_app birim ve Poppler/SQLite dogrulama testleri."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mobile_app.reader import (
    PageRenderCache,
    compute_page_step,
    compute_zoom_dimensions,
    config_file_path,
    filter_library,
    get_resume_target,
    import_manga_folder,
    import_pdf_files,
    init_reader_db,
    load_app_config,
    load_library_summary,
    pick_continue_chapter,
    plan_import_destination,
    reading_fraction,
    render_pdf_page_info,
    render_pdf_page_texture,
    resolve_chapter_pdf,
    resolve_library_root,
    resolve_swipe_delta,
    resolve_tap_delta,
    save_app_config,
    save_reading_progress,
    sync_disk_library,
)


def _make_sample_pdf(path: Path, n_pages: int = 3) -> None:
    """Harici dosyaya bagimli olmadan Poppler ile acilabilen cok sayfali sentetik PDF uretir."""
    objs: list[tuple[int, bytes]] = []
    page_ids: list[int] = []
    next_id = 3
    for _ in range(n_pages):
        pid = next_id
        cid = next_id + 1
        next_id += 2
        page_ids.append(pid)
        stream = b"0.95 0.95 0.95 rg 0 0 200 300 re f 0.1 0.1 0.1 RG 10 10 180 280 re S\n"
        objs.append((pid, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Contents {cid} 0 R >>".encode("ascii")))
        objs.append((cid, f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"))
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    all_objs = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii")),
        *objs,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(all_objs) + 1)
    for oid, body in all_objs:
        offsets[oid] = len(out)
        out.extend(f"{oid} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(all_objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for oid in range(1, len(all_objs) + 1):
        out.extend(f"{offsets[oid]:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(all_objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    path.write_bytes(bytes(out))


class MobileAppTests(unittest.TestCase):
    def test_01_poppler_render_real_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "sample.pdf"
            _make_sample_pdf(pdf_path, n_pages=3)
            self.assertTrue(pdf_path.exists())
            info = render_pdf_page_info(pdf_path, 0)
            self.assertEqual(info["pages"], 3)
            self.assertGreater(info["width"], 0)
            self.assertGreater(info["height"], 0)
            self.assertGreater(info["bytes"], 0)

    def test_02_progress_save_and_resume_portable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            manga_dir = lib / "Demo Manga"
            manga_dir.mkdir(parents=True)
            (manga_dir / "1.pdf").write_bytes(b"%PDF-1.4\n")
            db_path = lib / "library.db"
            init_reader_db(db_path)

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO mangas (slug,title,source_type,source_id,source_url,source_lang,target_lang,library_dir,status,created_at,updated_at) "
                    "VALUES ('demo','Demo Manga','local','','','en','tr','/nonexistent/pc/path','active','2026-01-01','2026-01-01')"
                )
                mid = conn.execute("SELECT id FROM mangas WHERE slug='demo'").fetchone()[0]
                conn.execute(
                    "INSERT INTO chapters (manga_id,chapter_number,chapter_sort,title,page_count,library_pdf_path,updated_at) "
                    "VALUES (?,'1',1.0,'Bölüm 1',12,'/nonexistent/pc/path/1.pdf','2026-01-01')",
                    (mid,),
                )
                ch_id = conn.execute("SELECT id FROM chapters WHERE manga_id=?", (mid,)).fetchone()[0]
                conn.commit()

            resolved = resolve_chapter_pdf(lib, "Demo Manga", "1", "/nonexistent/pc/path/1.pdf")
            self.assertEqual(resolved, manga_dir / "1.pdf")

            save_reading_progress(ch_id, current_page=7, total_pages=12, db_path=db_path)
            resumed = get_resume_target(db_path=db_path)
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["current_page"], 7)
            self.assertEqual(resumed["chapter_number"], "1")
            self.assertEqual(len(load_library_summary(db_path)), 1)

    def test_03_fast_page_decode_and_lru_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "cache_sample.pdf"
            _make_sample_pdf(pdf_path, n_pages=2)
            cache = PageRenderCache(max_entries=5)
            try:
                self.assertIsNone(cache.get_cached(pdf_path, 0))
                tex, w, h = render_pdf_page_texture(pdf_path, 0)
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)
                cache.put_cached(pdf_path, 0, tex, w, h)
                hit = cache.get_cached(pdf_path, 0)
                self.assertIsNotNone(hit)
                self.assertEqual(hit[1], w)
                self.assertEqual(hit[2], h)
            finally:
                cache.shutdown()

    def test_04_page_boundaries_do_not_jump_chapters(self) -> None:
        self.assertEqual(compute_page_step(0, 11, -1), 0)
        self.assertEqual(compute_page_step(10, 11, 1), 10)
        self.assertEqual(compute_page_step(4, 11, 1), 5)
        self.assertEqual(compute_page_step(4, 11, -1), 3)
        self.assertEqual(compute_page_step(0, 1, 1), 0)
        self.assertEqual(compute_page_step(0, 1, -1), 0)

    def test_05_swipe_and_tap_direction_ltr_rtl(self) -> None:
        # LTR: sola kaydir = ileri (+1), saga kaydir = geri (-1)
        self.assertEqual(resolve_swipe_delta(-90.0, 5.0, rtl_mode=False), 1)
        self.assertEqual(resolve_swipe_delta(90.0, 5.0, rtl_mode=False), -1)
        # RTL: sola kaydir = geri (-1), saga kaydir = ileri (+1)
        self.assertEqual(resolve_swipe_delta(-90.0, 5.0, rtl_mode=True), -1)
        self.assertEqual(resolve_swipe_delta(90.0, 5.0, rtl_mode=True), 1)
        # Kucuk veya dikey hareketler sayfa cevirmez
        self.assertEqual(resolve_swipe_delta(-20.0, 2.0, rtl_mode=False), 0)
        self.assertEqual(resolve_swipe_delta(-60.0, 80.0, rtl_mode=False), 0)

        # Dokunma bolgeleri: sol %30, orta %40, sag %30
        self.assertEqual(resolve_tap_delta(50.0, 400.0, rtl_mode=False), -1)
        self.assertEqual(resolve_tap_delta(200.0, 400.0, rtl_mode=False), 0)
        self.assertEqual(resolve_tap_delta(350.0, 400.0, rtl_mode=False), 1)
        self.assertEqual(resolve_tap_delta(50.0, 400.0, rtl_mode=True), 1)
        self.assertEqual(resolve_tap_delta(350.0, 400.0, rtl_mode=True), -1)

    def test_06_zoom_target_size_scales_picture(self) -> None:
        self.assertIsNone(compute_zoom_dimensions(800, 1200, 576, 884, 1.0))
        z14 = compute_zoom_dimensions(800, 1200, 576, 884, 1.4)
        self.assertIsNotNone(z14)
        self.assertGreater(z14[0], 576)
        self.assertGreater(z14[1], 884)
        z20 = compute_zoom_dimensions(800, 1200, 576, 884, 2.0)
        self.assertIsNotNone(z20)
        self.assertGreater(z20[0], z14[0])

    def test_07_non_destructive_disk_sync_preserves_progress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            m1 = lib / "Seri A"
            m1.mkdir(parents=True)
            (m1 / "1.pdf").write_bytes(b"%PDF-1.4\n")
            (m1 / "2.pdf").write_bytes(b"%PDF-1.4\n")
            db_path = lib / "library.db"
            init_reader_db(db_path)

            res1 = sync_disk_library(lib, db_path)
            self.assertEqual(res1["added_mangas"], 1)
            self.assertEqual(res1["added_chapters"], 2)

            with sqlite3.connect(db_path) as conn:
                ch1_id = conn.execute("SELECT id FROM chapters WHERE chapter_number='1'").fetchone()[0]
            save_reading_progress(ch1_id, current_page=3, total_pages=10, db_path=db_path)

            # Yeni manga/bolum ekle, mevcut ilerleme korunmali
            m2 = lib / "Seri B"
            m2.mkdir(parents=True)
            (m2 / "5.pdf").write_bytes(b"%PDF-1.4\n")
            res2 = sync_disk_library(lib, db_path)
            self.assertEqual(res2["added_mangas"], 1)
            self.assertEqual(res2["added_chapters"], 1)

            resumed = get_resume_target(db_path=db_path)
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["manga_title"], "Seri A")
            self.assertEqual(resumed["chapter_number"], "1")
            self.assertEqual(resumed["current_page"], 3)

    def test_08_yeni_lang_layout_sync_and_resolve(self) -> None:
        """Cok dilli klasor duzeni: <Kok>/<Seri>/<dil>/<bolum>.pdf"""
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td)
            (lib / "Seri A" / "tr").mkdir(parents=True)
            (lib / "Seri A" / "tr" / "155.pdf").write_bytes(b"%PDF-1.4\n")
            (lib / "Seri A" / "tr" / "156.pdf.tmp").write_bytes(b"x")
            (lib / "Seri B" / "tr").mkdir(parents=True)
            (lib / "Seri B" / "de").mkdir(parents=True)
            (lib / "Seri B" / "tr" / "1.pdf").write_bytes(b"%PDF-1.4\n")
            (lib / "Seri B" / "de" / "1.pdf").write_bytes(b"%PDF-1.4\n")
            db_path = lib / "library.db"
            res = sync_disk_library(lib, db_path)
            self.assertEqual(res, {"added_mangas": 3, "added_chapters": 3})
            titles = sorted(m["title"] for m in load_library_summary(db_path))
            self.assertEqual(titles, ["Seri A", "Seri B [DE]", "Seri B [TR]"])
            self.assertEqual(resolve_chapter_pdf(lib, "Seri A", "155"), lib / "Seri A" / "tr" / "155.pdf")
            self.assertEqual(resolve_chapter_pdf(lib, "Seri B [DE]", "1"), lib / "Seri B" / "de" / "1.pdf")
            # Ikinci tarama yeni kayit eklememeli
            self.assertEqual(sync_disk_library(lib, db_path), {"added_mangas": 0, "added_chapters": 0})

    def test_09_ui_helpers(self) -> None:
        self.assertEqual(reading_fraction(3, 0), 0.0)
        self.assertAlmostEqual(reading_fraction(5, 10), 0.5)
        self.assertEqual(reading_fraction(20, 10), 1.0)
        items = [{"title": "Alpha Chronicles", "slug": "alpha_chronicles"}, {"title": "Beta Tales", "slug": "beta_tales"}]
        self.assertEqual(len(filter_library(items, "")), 2)
        self.assertEqual(filter_library(items, "CHRONICLES")[0]["slug"], "alpha_chronicles")
        chs = [
            {"current_page": 0, "is_completed": False, "last_read_at": ""},
            {"current_page": 10, "is_completed": True, "last_read_at": "2026-01-02"},
            {"current_page": 4, "is_completed": False, "last_read_at": ""},
        ]
        self.assertEqual(pick_continue_chapter([]), None)
        self.assertEqual(pick_continue_chapter([dict(c, last_read_at="") for c in chs]), (0, 0))
        self.assertEqual(pick_continue_chapter(chs), (2, 3))
        chs[0]["last_read_at"] = "2026-01-03"
        chs[0]["current_page"] = 6
        self.assertEqual(pick_continue_chapter(chs), (0, 5))

    def test_10_xdg_config_and_real_linux_library_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            cfg_home = td_p / "xdg_cfg"
            fake_home = td_p / "home"
            (fake_home / "Manga").mkdir(parents=True)
            custom_lib = td_p / "CustomManga"
            custom_lib.mkdir(parents=True)

            old_env = {
                "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
                "MANGA_LIBRARY_ROOT": os.environ.get("MANGA_LIBRARY_ROOT"),
                "HOME": os.environ.get("HOME"),
            }
            try:
                os.environ["XDG_CONFIG_HOME"] = str(cfg_home)
                os.environ["HOME"] = str(fake_home)
                os.environ.pop("MANGA_LIBRARY_ROOT", None)

                self.assertEqual(config_file_path(), cfg_home / "manga-reader" / "config.json")
                self.assertEqual(resolve_library_root(), (fake_home / "Manga").resolve())

                save_app_config({"library_root": str(custom_lib), "rtl_mode": True})
                loaded = load_app_config()
                self.assertEqual(loaded["library_root"], str(custom_lib))
                self.assertTrue(loaded["rtl_mode"])
                self.assertEqual(resolve_library_root(), custom_lib.resolve())

                env_lib = td_p / "EnvManga"
                os.environ["MANGA_LIBRARY_ROOT"] = str(env_lib)
                self.assertEqual(resolve_library_root(), env_lib.resolve())
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_11_import_folder_flat_and_yeni_multilang(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            lib = td_p / "Lib"
            lib.mkdir()
            db = lib / "library.db"

            # 1) Duz klasor kopyalama
            flat_src = td_p / "Downloads" / "Flat Manga"
            flat_src.mkdir(parents=True)
            (flat_src / "1.pdf").write_bytes(b"%PDF-1.4\n1")
            (flat_src / "2.pdf").write_bytes(b"%PDF-1.4\n2")

            res1 = import_manga_folder(flat_src, library_root=lib, db_path=db, lang="tr", move=False)
            self.assertEqual(res1["imported_files"], 2)
            self.assertIsNotNone(res1["manga_id"])
            self.assertTrue((lib / "Flat Manga" / "tr" / "1.pdf").exists())
            self.assertTrue((flat_src / "1.pdf").exists())  # kopyalandi, kaynak korundu

            # 2) Cok dilli alt klasor yapisi (<seri>/<dil>/*.pdf) tasima modu
            yeni_src = td_p / "yeni_out" / "Multi Manga"
            (yeni_src / "tr").mkdir(parents=True)
            (yeni_src / "en").mkdir(parents=True)
            (yeni_src / "tr" / "10.pdf").write_bytes(b"%PDF-1.4\ntr")
            (yeni_src / "en" / "10.pdf").write_bytes(b"%PDF-1.4\nen")

            res2 = import_manga_folder(yeni_src, library_root=lib, db_path=db, move=True)
            self.assertEqual(res2["imported_files"], 2)
            self.assertTrue((lib / "Multi Manga" / "tr" / "10.pdf").exists())
            self.assertTrue((lib / "Multi Manga" / "en" / "10.pdf").exists())
            self.assertFalse((yeni_src / "tr" / "10.pdf").exists())

            # 3) Bos klasor hata vermeli
            empty_dir = td_p / "EmptyDir"
            empty_dir.mkdir()
            with self.assertRaises(ValueError):
                import_manga_folder(empty_dir, library_root=lib, db_path=db)

    def test_12_import_pdf_files_and_plan_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            lib = td_p / "Lib"
            lib.mkdir()
            db = lib / "library.db"

            self.assertEqual(plan_import_destination(lib, "Deneme: Seri", "TR"), lib / "Deneme_ Seri" / "tr")
            self.assertEqual(plan_import_destination(lib, "Deneme Seri", ""), lib / "Deneme Seri")

            p1 = td_p / "5.pdf"
            p1.write_bytes(b"%PDF-1.4\n5")
            res = import_pdf_files([p1], library_root=lib, db_path=db, manga_title="Tekil Seri", lang="tr")
            self.assertEqual(res["imported_files"], 1)
            self.assertIsNotNone(res["manga_id"])
            self.assertEqual(res["dest_dir"], str(lib / "Tekil Seri" / "tr"))

            # Ayni hedef dosyayi tekrar yuklemeye calisinca skipped sayilmali
            same_file = lib / "Tekil Seri" / "tr" / "5.pdf"
            res_dup = import_pdf_files([same_file], library_root=lib, db_path=db, manga_title="Tekil Seri", lang="tr")
            self.assertEqual(res_dup["imported_files"], 0)
            self.assertEqual(res_dup["skipped_files"], 1)


if __name__ == "__main__":
    unittest.main()

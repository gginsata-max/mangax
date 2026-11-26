#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
STAGE_BASE="$(mktemp -d)"
trap 'rm -rf "$STAGE_BASE"' EXIT

mkdir -p "$DIST_DIR"

MR_ROOT="$STAGE_BASE/manga-reader"
install -d "$MR_ROOT/usr/bin" \
	"$MR_ROOT/usr/share/manga-reader/mobile_app" \
	"$MR_ROOT/usr/share/applications" \
	"$MR_ROOT/usr/share/icons/hicolor/scalable/apps" \
	"$MR_ROOT/usr/share/metainfo" \
	"$MR_ROOT/usr/share/doc/manga-reader"

install -m644 "$REPO_ROOT/mobile_app/__init__.py" "$MR_ROOT/usr/share/manga-reader/mobile_app/__init__.py"
install -m755 "$REPO_ROOT/mobile_app/reader.py" "$MR_ROOT/usr/share/manga-reader/mobile_app/reader.py"
install -m644 "$REPO_ROOT/mobile_app/README.md" "$MR_ROOT/usr/share/doc/manga-reader/README.md"
install -m644 "$SCRIPT_DIR/dev.hobi.MangaReader.desktop" "$MR_ROOT/usr/share/applications/dev.hobi.MangaReader.desktop"
install -m644 "$SCRIPT_DIR/dev.hobi.MangaReader.svg" "$MR_ROOT/usr/share/icons/hicolor/scalable/apps/dev.hobi.MangaReader.svg"
install -m644 "$SCRIPT_DIR/dev.hobi.MangaReader.metainfo.xml" "$MR_ROOT/usr/share/metainfo/dev.hobi.MangaReader.metainfo.xml"

cat > "$MR_ROOT/usr/bin/manga-reader" <<'EOF'
#!/bin/sh
PYTHONPATH=/usr/share/manga-reader exec python3 /usr/share/manga-reader/mobile_app/reader.py "$@"
EOF
chmod 0755 "$MR_ROOT/usr/bin/manga-reader"

apk mkpkg \
	--files "$MR_ROOT" \
	--info "name:manga-reader" \
	--info "version:1.1.0-r0" \
	--info "arch:noarch" \
	--info "license:MIT" \
	--info "description:Linux Mobile GTK4 + Libadwaita PDF Manga Okuyucu" \
	--info "url:https://github.com/gginsata-max/mangax" \
	--info "depends:python3 py3-gobject3 gtk4.0 libadwaita poppler-glib poppler-utils cairo xdg-utils" \
	--script "post-install:$SCRIPT_DIR/manga-reader.post-install" \
	--script "post-upgrade:$SCRIPT_DIR/manga-reader.post-upgrade" \
	--script "post-deinstall:$SCRIPT_DIR/manga-reader.post-deinstall" \
	--output "$DIST_DIR/manga-reader-1.1.0-r0.apk"

ls -lh "$DIST_DIR/manga-reader-1.1.0-r0.apk"

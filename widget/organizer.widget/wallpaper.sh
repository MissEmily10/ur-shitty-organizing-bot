#!/bin/bash
# Средний цвет обоев для виджета: печатает BMP 1×1 в hex или «ERR:причина».
# Ищем файл картинки по очереди: System Events (на новых macOS часто не работает) → все ссылки file:// в настройках
# обоев macOS 14+ (Index.plist), в том числе внутри закодированных блоков Configuration (там лежит своё фото).
# (на Mac bash 3.2: без ${x,,} — регистр приводим через tr; без strings — без инструментов разработчика он просит их установить)
image() {
  ext=$(printf '%s' "${1##*.}" | tr 'A-Z' 'a-z')
  case "$ext" in jpg|jpeg|png|heic|heif|tif|tiff|webp|gif|bmp) [ -f "$1" ];; *) false;; esac
}

f=$(osascript -e 'tell application "System Events" to get POSIX path of (picture of current desktop as alias)' 2>/dev/null)
image "$f" || f=$(osascript -e 'tell application "System Events" to get picture of current desktop' 2>/dev/null)
if ! image "$f"; then
  p="$HOME/Library/Application Support/com.apple.wallpaper/Store/Index.plist"
  tmp=$(mktemp -d)
  plutil -convert xml1 -o "$tmp/index.xml" "$p" 2>/dev/null
  urls=$(grep -o 'file://[^<]*' "$tmp/index.xml" 2>/dev/null)
  # закодированные блоки <data>…</data>: раскодировать и тоже поискать ссылки
  awk '/<data>/{f=1; buf=""; next} /<\/data>/{f=0; print buf; next} f{gsub(/[ \t]/, ""); buf=buf $0}' "$tmp/index.xml" 2>/dev/null |
    while read -r blob; do
      [ -n "$blob" ] || continue
      echo "$blob" | base64 -D > "$tmp/blob" 2>/dev/null || continue
      plutil -convert xml1 -o - "$tmp/blob" 2>/dev/null | grep -o 'file://[^<]*'
      LC_ALL=C grep -ao 'file://[[:print:]]*' "$tmp/blob" 2>/dev/null | sed 's/[^[:alnum:]%._~/:-]*$//'
    done > "$tmp/more"
  urls="$urls"$'\n'"$(cat "$tmp/more")"
  f=""
  while read -r u; do
    [ -n "$u" ] || continue
    c="${u#file://}"
    c=$(printf '%b' "${c//%/\\x}")  # %20 → пробел и т. п.
    if image "$c"; then f="$c"; break; fi
  done <<< "$urls"
  rm -rf "$tmp"
fi
image "$f" || { echo "ERR:не нашла файл обоев — может, это живые обои или «Фото» в режиме смены"; exit 0; }
sips -s format bmp -z 1 1 "$f" --out /tmp/organizer-wallpaper.bmp >/dev/null 2>&1 || { echo "ERR:не открыла картинку обоев ($f)"; exit 0; }
xxd -p /tmp/organizer-wallpaper.bmp | tr -d '\n'

#!/bin/bash
# Средний цвет обоев для виджета: печатает BMP 1×1 в hex или «ERR:причина».
# Пробуем по очереди: System Events (может не работать на новых macOS), настройки обоев macOS 14+ (Index.plist).
f=$(osascript -e 'tell application "System Events" to get POSIX path of (picture of current desktop as alias)' 2>/dev/null)
[ -f "$f" ] || f=$(osascript -e 'tell application "System Events" to get picture of current desktop' 2>/dev/null)
if [ ! -f "$f" ]; then
  p="$HOME/Library/Application Support/com.apple.wallpaper/Store/Index.plist"
  for key in AllSpacesAndDisplays SystemDefault; do
    u=$(plutil -extract "$key.Desktop.Content.Choices.0.Files.0.relative" raw -o - "$p" 2>/dev/null)
    [ -n "$u" ] && break
  done
  f="${u#file://}"
  f=$(printf '%b' "${f//%/\\x}")  # %20 → пробел и т. п.
fi
[ -f "$f" ] || { echo "ERR:не нашла файл обоев${f:+ ($f)} — может, это живые обои или «Фото» в режиме смены"; exit 0; }
sips -s format bmp -z 1 1 "$f" --out /tmp/organizer-wallpaper.bmp >/dev/null 2>&1 || { echo "ERR:не открыла картинку обоев ($f)"; exit 0; }
xxd -p /tmp/organizer-wallpaper.bmp | tr -d '\n'

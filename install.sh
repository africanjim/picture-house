#!/usr/bin/env bash
# Picture House installer: adds the app to your menu. No sudo needed.
#   ./install.sh              install (or update)
#   ./install.sh --uninstall  remove it again
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA/picture-house"
ICON="$DATA/icons/hicolor/scalable/apps/picture-house.svg"
DESKTOP="$DATA/applications/picture-house.desktop"
BIN="$HOME/.local/bin/picture-house"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/picture-house"

refresh_menu() {
  command -v update-desktop-database >/dev/null && update-desktop-database "$DATA/applications" 2>/dev/null || true
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$DATA/icons/hicolor" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$DESKTOP" "$ICON" "$BIN"
  rm -rf "$APP_DIR"
  refresh_menu
  if [[ -d "$CONFIG" ]]; then
    read -r -p "Also delete your saved Plex token and settings? [y/N] " a
    [[ "$a" =~ ^[Yy] ]] && rm -rf "$CONFIG" && echo "Settings deleted."
  fi
  echo "Picture House has been removed. Pages you made are still in your Documents folder."
  exit 0
fi

if ! command -v python3 >/dev/null; then
  echo "Picture House needs Python 3. Install it with:  sudo apt install python3" >&2
  exit 1
fi

install -Dm755 "$HERE/picture-house.py" "$APP_DIR/picture-house.py"
install -Dm644 "$HERE/icon.svg" "$ICON"
mkdir -p "$(dirname "$BIN")"
ln -sf "$APP_DIR/picture-house.py" "$BIN"
mkdir -p "$(dirname "$DESKTOP")"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=Picture House
GenericName=Plex library browser
Comment=Make a browsable page of your Plex movies and TV shows
Exec=python3 "$APP_DIR/picture-house.py"
Icon=picture-house
Terminal=false
Categories=AudioVideo;Video;
Keywords=plex;movies;films;library;catalogue;
DESK
chmod 644 "$DESKTOP"
refresh_menu

echo "Installed. Open Picture House from your app menu, or run:  picture-house"
if ! command -v zenity >/dev/null; then
  echo "Note: pop-up windows need zenity. Install it with:  sudo apt install zenity"
  echo "      (Picture House still works in a terminal without it.)"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  echo "Note: ~/.local/bin isn't on your PATH yet, so the 'picture-house' command works after you log out and back in." ;;
esac

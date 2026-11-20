# Changes

## 1.2.1 (2026-11-20)
- Setup finds your Plex server in a few seconds when Plex is on a different computer
  (it used to try each address in turn and could look stuck for about 2 minutes after signing in).
- Pasting a Plex token now finds your server too, instead of only trying this computer.
- Clearer messages on the setup page while it's checking.

## 1.2.0 (2026-11-20)
- **Set up in your browser.** The first time Picture House runs, it opens a setup page:
  sign in with your Plex account (no token hunting), it finds your server for you, and you choose a password.
- **Downloads for Windows, Mac and Linux**, plus a Docker image for NAS boxes and home servers.
- Works on plain `http://` at home too (sign-in used to need https). The barcode camera and offline mode still need https.
- Settings live in the normal place for each system (Windows: `%APPDATA%\Picture House`,
  Mac: `~/Library/Application Support/Picture House`).

## 1.1.0 (2026-11-04)
- Phone app: posters, watched status, TV episodes, barcode scanner, wish list, works offline.
- Needs-attention tick box on the desktop page and the phone app.

## 1.0.0 (2026-09-26)
- First release: a searchable web page and CSV of your Plex library.

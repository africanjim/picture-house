# Picture House

**Your Plex library as a good-looking app for your phone and computer.** Browse your movies and TV shows with posters, scan a DVD or Blu-ray in a shop to see straight away whether you already own it, and keep a wish list. It works with no signal too.

Picture House only *reads* from Plex. It never changes your library.

<p align="center"><b><a href="https://github.com/africanjim/picture-house/releases/latest">⬇ Download the latest version</a></b></p>

## What you get

- your whole library with **posters**, **watched** status and **TV episodes**
- search by title, director, genre, studio or country, and browse by A–Z, genre, decade, library, director or recently added
- filter by quality (4K / 1080p / 720p / SD), sort by title, year, rating, date added or runtime
- **scan a disc's barcode** to check whether you already have it
- a **wish list**: one tap adds a disc you don't have
- **works offline**: changes you make with no signal sync when you're back online
- a **Needs attention** list of titles Plex never matched, or looks to have matched to the wrong film
- light and dark mode

## Install

Picture House runs on a computer that's usually on (ideally the one with Plex). Then you open it on your phone or any other device.

| Your computer | What to download | How to start it |
|---|---|---|
| **Windows** | `picture-house-windows.exe` | Double-click it. If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**. Allow it through the firewall when asked, so your phone can reach it. |
| **Mac** (Apple silicon) | `picture-house-mac.zip` | Unzip it, then **right-click** `picture-house` and choose **Open** (the first time only; Mac warns about apps that aren't from the App Store). |
| **Linux** | `picture-house-linux.tar.gz` | Unpack it, then run `./picture-house` |
| **Docker / NAS** (Synology, Unraid, a home server) | nothing, see below | `docker compose up -d` |

The first time, a setup page opens in your browser:

1. **Sign in with Plex.** A Plex window opens. Sign in, and Picture House finds your server by itself.
2. **Check the server address** (it's filled in for you).
3. **Choose a password.** You type it once on each phone or computer.

Keep the Picture House window open while you use it. Closing it stops Picture House.

### Docker

Save [`docker-compose.yml`](docker-compose.yml) and run `docker compose up -d`, or:

```
docker run -d --name picturehouse --restart unless-stopped -p 8765:8765 \
  -v picturehouse-data:/data --add-host host.docker.internal:host-gateway \
  ghcr.io/africanjim/picture-house:latest
```

Then open `http://<the server's address>:8765` in a browser to finish setup. If Plex runs on a different computer, type its address on the setup page, e.g. `http://192.168.1.20:32400`.

## Put it on your phone

1. On your phone, open the Picture House address, for example `http://192.168.1.20:8765` (the computer's address on your home network, port 8765).
2. Sign in with your password.
3. **Android (Chrome):** tap **⋮**, then **Add to Home screen** (or **Install app**).
   **iPhone (Safari):** tap **Share**, then **Add to Home Screen**.

It now has its own icon and opens full-screen like any other app.

### Barcode scanner, offline mode, and using it away from home

On a plain `http://` address at home, browsing works, but phones only allow the **camera** and **offline mode** on a secure `https://` address. To get those, and to use Picture House away from home, choose one:

- **Tailscale** (easiest, free): install [Tailscale](https://tailscale.com) on the Picture House computer and your phone, turn on *HTTPS certificates* in its settings, then run `tailscale serve --bg 8765` on the computer. Your phone gets a private `https://` address that works anywhere.
- **Your own domain**: put Picture House behind a reverse proxy such as Nginx Proxy Manager or Caddy with a free Let's Encrypt certificate. Pass the `X-Forwarded-Proto` header (both do by default).

Barcode scanning uses the phone browser's built-in barcode reader, which Chrome on Android has. On iPhone, type the number under the barcode instead. Barcodes are looked up with the free [upcitemdb.com](https://www.upcitemdb.com) service (about 100 lookups a day). Some discs, especially Australian ones, aren't in it: type the title once and Picture House remembers that barcode.

## Good to know about privacy

- Your Plex sign-in, password (stored scrambled), wish list and remembered barcodes are kept **only on your computer**:
  Windows `%APPDATA%\Picture House`, Mac `~/Library/Application Support/Picture House`, Linux `~/.local/share/picture-house-server`, Docker the `/data` volume.
- The setup page only works from your home network, and only until setup is finished.
- To change the password, open a terminal and run `picture-house --password` (Docker: `docker exec -it picturehouse python3 /app/picture-house-server.py --password`). This signs every phone out.
- To start setup again from scratch, stop Picture House and delete `server.json` from the folder above.

## Have Claude set it up for you

If you use [Claude Code](https://claude.com/claude-code), paste this:

```
Please install Picture House for me from https://github.com/africanjim/picture-house
I have Plex Media Server on this computer (or tell me how to point it at another one).
Follow the install guide, help me sign in to Plex, and explain each step in plain language.
When it's running, tell me how to add the phone app to my phone.
```

## Extra: a page of your library you can send to people (Linux)

`picture-house.py` makes a **single web page** of your library that works offline. You can keep it, or send it to family and friends so they can see what's on your server. It also makes a **CSV file** for Excel, LibreOffice or Google Sheets. Download `picture-house-linux-desktop.zip` from the [latest release](https://github.com/africanjim/picture-house/releases/latest).

### Install (Ubuntu and other Linux desktops)

1. Download this folder and open a terminal in it.
2. Run:
   ```
   ./install.sh
   ```
3. Open **Picture House** from your app menu.

No `sudo` is needed. It uses Python 3 and `zenity`, which Ubuntu already includes.

The first time, it asks for your **Plex token**:

1. Open Plex in a web browser and go to any movie.
2. Click **⋯** (More), then **Get Info**, then **View XML**.
3. In the new tab's address, copy the text after `X-Plex-Token=`.

You can choose to have it remember the token. It's saved only on your computer in `~/.config/picture-house/config.json`, readable only by you, and only ever sent to your own Plex server.

Your page is saved in **Documents/Picture House** and opens in your browser automatically. Run it again any time to refresh the page.

### Run from a terminal

No install is needed:

```
python3 picture-house.py            # pop-up windows if you're on a desktop
python3 picture-house.py --cli      # terminal only (good for servers)
```

| Option | What it does |
|---|---|
| `--server URL` | Plex on another computer, e.g. `--server http://192.168.1.20:32400` |
| `--token TOKEN` | give the token directly (or set `PLEX_TOKEN`) |
| `--output FOLDER` | save somewhere other than Documents/Picture House |
| `--no-open` | don't open the page when finished |
| `--forget-token` | delete the saved token |

#### Keep it up to date automatically

To remake the page every night at 3am, run `crontab -e` and add:

```
0 3 * * * python3 ~/.local/share/picture-house/picture-house.py --cli --no-open
```

This needs the token to be saved.

### Uninstall

```
./install.sh --uninstall
```

Pages you've already made stay in your Documents folder.

## Run from source

Picture House is plain Python 3 with no extra packages:

```
python3 picture-house-server.py            # the app, on port 8765 (add --open to open your browser)
python3 picture-house-server.py --password # change the app password
packaging/build.sh                         # make a single-file program (needs: pip install pyinstaller)
```

New versions: update `VERSION` in `picture-house.py` and `CHANGELOG.md`, then push a tag like `v1.2.0`. GitHub builds the Windows, Mac and Linux downloads and the Docker image by itself.

## Good to know

- Details like genres, ratings and summaries come from Plex's own online matching. Titles Plex couldn't match show the file name instead and appear under **Needs attention**.
- The colour swatch beside each title comes from its poster's colours, as stored by Plex. Poster images aren't included, which keeps the page small and able to work offline.
- Only movie and TV libraries are included. Music and photos are not.
- If Plex is set to *Secure connections: Required*, Picture House switches to HTTPS automatically.

## Licence

MIT. See [LICENSE](LICENSE).

Picture House is an independent project and isn't made by or affiliated with Plex, Inc.

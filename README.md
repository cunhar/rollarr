# Rolarr — All-in-One Usenet Media Manager, Recycler & Power Saver 🎬⚡

**Rolarr** is a lightweight, self-contained Usenet media manager, downloader, recycler, and server power saver for **Plex** and **Jellyfin**. It replaces Sonarr, Radarr, and NZBGet with a single minimal container: search & add shows/movies, pick the best NZBgeek releases by resolution & audio with junk filtering, download & unpack natively over NNTP, delete watched media directly from disk, roll TV episodes forward automatically, and safely power down your host via SSH when idle.

---

## Features 🚀

- 🔍 **Built-in Search & Library**: Search TV shows via **TVMaze** (no API key needed) and Movies via **TMDB**, add them from the web UI, and auto-download.
- 🎯 **NZBgeek Release Selection Engine**: Sonarr/Radarr-inspired scoring with configurable **Preferred Resolution**, **Preferred Audio**, **Size Limits**, strict **Junk Filter** (blocks CAM/TS/HC/SAMPLE/3D/passworded), `PROPER`/`REPACK` bonuses, and automatic **blocklist + retry** of the next-best release on failure.
- 📥 **Native Usenet (NNTP) Downloader**: Built-in multi-connection TLS NNTP + yEnc downloader with `par2cmdline` repair, `unrar`/`7zip` extraction, clean renaming into `/tv` and `/movies`, and Plex/Jellyfin library refresh.
- 📦 **Direct Media Recycler & Rolling Window**: Detects watched TV episodes and movies in Plex/Jellyfin, deletes files directly from disk, and automatically queues the next $N$ aired episodes.
- ⚡ **Host Power Saver & Idle Shutdown**: Monitors active Plex/Jellyfin streams, built-in Usenet downloads, and background tasks. Powers down the host via SSH when idle for $N$ consecutive checks (includes Dry Run safe mode).
- 📺 **Smart TV D-Pad & Mobile PWA UI**: Spatial arrow-key navigation for Smart TVs and responsive mobile PWA support.
- 🔒 **Encrypted Storage & Secret Masking**: Persists credentials in AES-128 Fernet encrypted storage (`/config/settings.enc`).

---

## Quick Start / Deployment 🐳

### Docker Compose (Recommended)

```yaml
version: '3.8'

services:
  rolarr:
    image: ghcr.io/cunhar/rollarr:latest
    container_name: rolarr
    ports:
      - "5000:5000"
    volumes:
      - ./config/rolarr:/config
      - /path/to/downloads:/downloads
      - /path/to/tv:/tv
      - /path/to/movies:/movies
    environment:
      - TZ=Europe/Lisbon
    restart: unless-stopped
```

---

## Settings Reference ⚙️

All settings are configured via the web UI at `http://your-server:5000/#config`:

| Category | Setting | Description |
| :--- | :--- | :--- |
| **NZBgeek** | `NZBGEEK_URL` / `NZBGEEK_API_KEY` | NZBgeek Newznab URL & API key |
| **Usenet** | `NNTP_HOST` / `NNTP_PORT` / `NNTP_SSL` | Usenet server connection details |
| | `NNTP_USERNAME` / `NNTP_PASSWORD` / `NNTP_CONNECTIONS` | Usenet auth & concurrent connections |
| **Metadata** | `TMDB_API_KEY` | Free TMDB v3 API key for Movies (TV uses TVMaze with no key) |
| **Release** | `PREFERRED_RESOLUTION` / `PREFERRED_AUDIO` | Target resolution (`2160p`/`1080p`/`720p`/`any`) & audio (`atmos`/`dts`/`ddp`/`aac`/`any`) |
| | `MAX_EPISODE_SIZE_GB` / `MAX_MOVIE_SIZE_GB` | Maximum acceptable release size in GB |
| **Cleanup** | `DELETE_WATCHED_EPISODES` | Delete watched files directly from disk |
| | `ROLLING_WINDOW` | Number of upcoming aired episodes to keep downloaded ahead |
| **Media** | `PLEX_URL` / `PLEX_TOKEN` | Plex Media Server connection |
| | `JELLYFIN_URL` / `JELLYFIN_API_KEY` | Jellyfin Server connection |
| **Power Saver**| `PLEX_SHUTDOWN_MODE` / `PLEX_POLL_INTERVAL` / `PLEX_IDLE_POLLS` | Idle host shutdown policy |
| **Host SSH** | `SSH_HOST` / `SSH_PORT` / `SSH_USER` / `SSH_PASSWORD` | Host SSH credentials for shutdown |
| **Storage** | `PATH_DOWNLOADS` / `PATH_TV` / `PATH_MOVIES` | Mounted read/write directories |

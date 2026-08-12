# Rolarr — Media Recycler & Power Saver 🎬⚡

**Rolarr** is an automated media recycler and server power saver for **Sonarr**, **Radarr**, **Plex**, and **NZBGet**. It automatically deletes watched media to reclaim disk space, queues upcoming TV episodes using a configurable rolling window, and safely powers down your host server via SSH when idle.

---

## Features 🚀

- 📦 **Automated Episode Recycler (Sonarr)**: Detects watched TV episodes in Plex, unmonitors them, deletes media files from disk, and queues/searches the next $N$ episodes using a rolling window.
- 🍿 **Watched Movie Cleanup (Radarr)**: Automatically unmonitors and deletes watched movies from disk via Radarr upon completion in Plex.
- 💾 **Storage & Disk Space Monitor**: Live tracking of available storage across Downloads, TV Shows, and Movies directories, querying host disk metrics and Sonarr/Radarr API root folders.
- ⚡ **Host Power Saver & Idle Shutdown**: Monitors active Plex streams, NZBGet downloads, and background tasks. Powers down the host via SSH when idle for $N$ consecutive checks (includes Dry Run safe mode).
- 📺 **Smart TV & Remote D-Pad Navigation**: Built-in 2D spatial arrow key navigation engine and high-visibility focus indicators tailored for Smart TVs (LG WebOS, Android TV, Fire TV).
- 📱 **Mobile & Progressive Web App (PWA)**: Mobile-optimized responsive UI with PWA standalone mode (runs like a native mobile app without address bars).
- 🔒 **Encrypted Storage & Secret Masking**: Persists credentials in AES-128 Fernet encrypted storage (`/config/settings.enc`) with automatic DevTools secret masking sentinels (`••••••••`).
- 📥 **NZBGet & Service Connection Status**: Live status indicators for Sonarr, Radarr, NZBGet, and Plex with direct links and active download metrics.
- 📊 **Activity Audit Log**: Real-time log tracking processed media, status events, and payload inspections with state preservation across refreshes.

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
    environment:
      - TZ=Europe/Lisbon
    restart: unless-stopped
```

### Docker Run

```bash
docker run -d \
  --name rolarr \
  -p 5000:5000 \
  -v /your/host/config:/config \
  -e TZ="Europe/Lisbon" \
  ghcr.io/cunhar/rollarr:latest
```

---

## Web Dashboard Tabs 💻

1. **Dashboard**: View active Plex streams, current episode poller state, NZBGet active downloads, and host shutdown watcher status with a one-click manual shutdown trigger.
2. **Disk Space**: Monitor free, used, and total storage space across configured Downloads, TV Shows, and Movies storage paths.
3. **Activity**: Persistent audit log of processed media, webhook triggers, and system events with expandable payload views.
4. **Config**: Web UI form to securely manage encrypted settings without restarting containers.

---

## Settings Reference ⚙️

All settings can be configured via the web UI at `http://your-server:5000/#config`:

| Category | Setting | Description |
| :--- | :--- | :--- |
| **Cleanup** | `DELETE_WATCHED_EPISODES` | Enable/disable deleting media files from disk after watching |
| **Sonarr** | `SONARR_URL` / `SONARR_API_KEY` | Sonarr instance URL & API key |
| | `ROLLING_WINDOW` | Number of upcoming episodes to monitor and search (default `3`) |
| **Radarr** | `RADARR_URL` / `RADARR_API_KEY` | Radarr instance URL & API key |
| **NZBGet** | `NZBGET_URL` / `NZBGET_USERNAME` / `NZBGET_PASSWORD` | NZBGet connection credentials |
| **Plex** | `PLEX_URL` / `PLEX_TOKEN` | Plex Media Server URL & `X-Plex-Token` |
| | `PLEX_WATCH_INTERVAL` | Polling frequency for watched media cleanup in seconds (default `3600`) |
| **Power Saver**| `PLEX_POLL_INTERVAL` | Idle check frequency in seconds (default `1200`) |
| | `PLEX_IDLE_POLLS` | Consecutive idle checks required before triggering host shutdown (default `3`) |
| | `PLEX_SHUTDOWN_DRY_RUN` | Dry-run mode to test shutdown triggers safely without powering off host |
| **Host SSH** | `SSH_HOST` / `SSH_PORT` / `SSH_USER` / `SSH_PASSWORD` | Host SSH credentials for issuing shutdown commands |
| **Storage** | `PATH_DOWNLOADS` / `PATH_TV` / `PATH_MOVIES` | Local storage directory paths for disk monitoring |

---

## License 📜

MIT License. Developed for the *Arr & Plex ecosystem.

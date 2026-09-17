# qbit-storage-router

Automatically spread qBittorrent downloads across as many hard drives as you have, for a Jellyfin / Plex library.

If you have two, five, or ten drives full of shows and movies, this picks which drive each new download goes to so you stop thinking about free space.

## Requirements

- Jellyfin or Plex to watch
- Sonarr and Radarr to find and organize downloads, with hardlink imports turned on
- qBittorrent to download, reachable on loopback (`127.0.0.1:8080`)
- Python 3.10+
- More than one hard drive for downloads

Sonarr must label its downloads `tv` and Radarr must label its `movies` in qBittorrent. Those are the only two labels the router manages.

## Safety features

- **Leaves breathing room.** Each drive keeps a reserve you set, like 50 GB. Downloads already in progress count too.
- **Waits when full.** If nothing fits, the download stays stopped until space frees up.
- **Magnets are handled.** Magnets arrive with unknown size, so the router checks the size first on a staging drive, then places them like normal files.
- **Cleans up only after import.** After Sonarr/Radarr hardlinks the file into your library, the router stops the torrent, waits out a grace period, checks the download is gone from both Arr queues, then deletes the torrent and its download files. The library copy stays. Items that never import are stopped for you to check, never auto-deleted.
- **Stops instead of guessing.** Missing drive, wrong folder, or changed Sonarr/Radarr settings? It stops.
  
## Setup

```bash
sudo cp qbit-storage-router.py /usr/local/lib/qbit-storage-router.py
sudo cp qbit-storage-router.json.example /etc/qbit-storage-router.json
# edit /etc/qbit-storage-router.json to list your drives (see next section)
sudo cp qbit-storage-router.service /etc/systemd/system/qbit-storage-router.service
sudo systemctl daemon-reload && sudo systemctl enable --now qbit-storage-router.service
```

You must edit the service file for your drives. It ships with placeholders:

```ini
# Before:
# RequiresMountsFor=/mnt/drive-a /mnt/drive-b /mnt/pool
# ReadWritePaths=/mnt/drive-a/downloads /mnt/drive-b/downloads

# After (your drives, plus your pool):
RequiresMountsFor=/mnt/drive-a /mnt/drive-b /mnt/pool
ReadWritePaths=/mnt/drive-a/downloads /mnt/drive-b/downloads
```

Then reload and start as above. `RequiresMountsFor` makes systemd wait for your drives. `ReadWritePaths` is the only place the locked-down service is allowed to write.

Check it is running:

```bash
systemctl status qbit-storage-router.service
journalctl -u qbit-storage-router.service -f
```

The router expects new torrents to arrive stopped, with automatic management off. It sets this in qBittorrent itself. In Sonarr/Radarr, qBittorrent must be enabled at priority 1 with hardlink imports turned on. Other Usenet-style clients are ignored; extra enabled qBittorrent clients are blocked. An optional second qBittorrent-based client can be allowed with an env var. If any of this drifts later, the router pauses downloads until you fix it.

## Config: listing your drives

All you edit is `/etc/qbit-storage-router.json`. Start from the included example, which lists two drives. To add a drive, copy one of the blocks you already have and change the names and paths. Leave `"version": 1` alone — anything else refuses to start.

### Paths: separate drives vs mergerfs pools, RAID, zfs etc

- **Separate drives are where downloads go.** Each drive is its own filesystem with the same folder layout underneath, for example `downloads/tv` for shows and `downloads/movies` for movies.
- **The pool is the combined view.** For example `/mnt/pool` merges all drives so Jellyfin/Plex and the Arrs can read everything in one place. The router never downloads to the pool. It only checks the pool is mounted, then always writes to one separate drive.
- **Same drive, two names.** Your server and qBittorrent can see the same drive under different paths. Both must point at the same physical disk:
  - server: `/mnt/drive-a` = qBittorrent: `/qbit/drive-a`
  - server: `/mnt/drive-b` = qBittorrent: `/qbit/drive-b`
- **How it flows.** qBittorrent downloads to `/qbit/drive-b/downloads/tv/...` (which is `/mnt/drive-b/downloads/tv/...` on the server). Sonarr adds it to your library, which you read through the pool. The router then deletes the download copy on `drive-b`.

Two-drive example with generic paths:

```json
{
  "version": 1,
  "policy": "most_free",
  "metadata_branch": "drive-a",
  "branches": [
    {
      "name": "drive-a",
      "tag": "route_drive-a",
      "host_root": "/mnt/drive-a",
      "mount_root": "/mnt/drive-a",
      "qbit_prefix": "/qbit/drive-a",
      "reserve_bytes": 53687091200,
      "route_headroom_bytes": 0
    },
    {
      "name": "drive-b",
      "tag": "route_drive-b",
      "host_root": "/mnt/drive-b",
      "mount_root": "/mnt/drive-b",
      "qbit_prefix": "/qbit/drive-b",
      "reserve_bytes": 53687091200,
      "route_headroom_bytes": 0
    }
  ]
}
```

Each drive needs:

- `name`: lowercase letters, numbers, `-` or `_`, like `drive-a`. One of your drives is also picked as `metadata_branch` at the top of the file for checking magnet sizes first. That name must match one of your drives exactly, or the router exits.
- `host_root`: where your server sees the drive, like `/mnt/drive-a`. Must be under `/mnt/`.
- `qbit_prefix`: where qBittorrent sees that same drive, like `/qbit/drive-a`. Same disk, two views. They must point at the same physical drive.
- `mount_root`: almost always the same as `host_root`. Leave it the same unless you know why yours is different.
- `tag`: must start with `route_` and use only lowercase letters, numbers, `-` or `_`, like `route_drive-a`. You will see this label on torrents inside qBittorrent. Keep it unique per drive. A torrent with a missing or doubled tag is stopped with `route_error`.
- `reserve_bytes`: space to always leave free, in bytes. 50 GB = `53687091200`. Never set this to zero.
- `route_headroom_bytes`: extra buffer on drives that also hold apps or configs. Usually `0` except on crowded drives.
- `policy` at the top: `most_free` means always use the emptiest drive, `first_fit` means fill drives in order. Anything else refuses to start.

## If downloads all stop

Look in this order:

1. Logs: `journalctl -u qbit-storage-router.service -n 100` tells you why in plain words.
2. Labels in qBittorrent:
   - `route_waiting_space` — no drive has room right now. Free space or add a drive.
   - `route_overcommit` — a drive went over its safe limit. It resumes when space recovers.
   - `route_error` — folder or label doesn't match the config. Usually fixed by letting the router move it, or by re-checking your config.
   - `route_imported` / `route_import_failed` — finished, waiting for Sonarr/Radarr to finish adding it to your library.
3. Common causes: a drive unmounted, you changed a qBittorrent save path or category, or you changed a Sonarr/Radarr download client, path mapping, or turned off hardlinks.

License: MIT. See `LICENSE`.

#!/usr/bin/env python3
"""Route stopped qBittorrent downloads to a configured physical branch.

The router reserves each torrent's selected size before starting it. This
avoids the race where many sparse files are created on the first mergerfs
branch before their later writes consume space.

Magnets first run on the configured metadata branch with qBittorrent's native
``MetadataReceived`` stop condition and a moderate rate cap. The cap allows
metadata to arrive promptly while bounding payload that can race the native
stop event. Once the real size is known, they are routed using the same
reservation policy as .torrent submissions. Each configured branch uses one
stable route tag and qBittorrent prefix; tags from removed branches are
treated as unknown routes and fail closed.
"""

from __future__ import annotations

import json
import errno
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


QBIT_URL = os.environ.get("QBIT_URL", "http://127.0.0.1:8080/api/v2").rstrip("/")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "0.5"))
POOL_ROOT = os.environ.get("POOL_ROOT", "/mnt/pool")
ROUTER_CONFIG = os.environ.get(
    "QBIT_STORAGE_ROUTER_CONFIG", "/etc/qbit-storage-router.json"
)
# Download layout under each drive. Generic defaults; override with env vars
# to match an existing setup. "<drive>/downloads/tv" and "<drive>/downloads/movies".
DOWNLOAD_SUBPATH = os.environ.get(
    "QBIT_ROUTER_DOWNLOADS", "downloads/torrents"
).strip("/")
INCOMPLETE_SUBPATH = os.environ.get(
    "QBIT_ROUTER_INCOMPLETE", "downloads/incomplete"
).strip("/")
DOWNLOAD_ROOT = DOWNLOAD_SUBPATH.split("/")[0] + "/"
ARR_LOCAL_DOWNLOADS = os.environ.get(
    "QBIT_ROUTER_ARR_LOCAL_DOWNLOADS", "/data/downloads/"
)
if not ARR_LOCAL_DOWNLOADS.endswith("/"):
    ARR_LOCAL_DOWNLOADS += "/"
ROUTE_METADATA = "route_metadata"
ROUTE_METADATA_PENDING = "route_metadata_pending"
ROUTE_PENDING_START = "route_pending_start"
ROUTE_RECHECK_REQUESTED = "route_recheck_requested"
ROUTE_RECHECK_ACTIVE = "route_recheck_active"
ROUTE_WAITING = "route_waiting_space"
ROUTE_ERROR = "route_error"
ROUTE_OVERCOMMIT = "route_overcommit"
ROUTE_OVERCOMMIT_RESUME = "route_overcommit_resume"
ROUTE_CONFIG_RESUME = "route_config_resume"
ROUTE_MANAGED_ACTIVE = "route_managed_active"
ROUTE_IMPORT_THROTTLED = "route_import_throttled"
ROUTE_IMPORTED = "route_imported"
ROUTE_IMPORT_FAILED = "route_import_failed"
MANAGED_STOP_GRACE = float(os.environ.get("MANAGED_STOP_GRACE", "5"))
IMPORT_SETTLE_SECONDS = float(os.environ.get("IMPORT_SETTLE_SECONDS", "300"))
IMPORT_CHECK_INTERVAL = float(os.environ.get("IMPORT_CHECK_INTERVAL", "5"))
IMPORT_CLEANUP_GRACE_SECONDS = float(
    os.environ.get("IMPORT_CLEANUP_GRACE_SECONDS", "600")
)
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".wmv"}
METADATA_DL_LIMIT = int(os.environ.get("METADATA_DL_LIMIT", str(512 * 1024)))
METADATA_CLEANUP_LIMIT = int(
    os.environ.get("METADATA_CLEANUP_LIMIT", str(64 * 1024**2))
)
ARR_AUDIT_INTERVAL = float(os.environ.get("ARR_AUDIT_INTERVAL", "15"))
# Arr config.xml locations are host paths. Override with env vars to match
# your setup. Defaults are generic placeholders.
ARR_CONFIGS = {
    "sonarr": os.environ.get(
        "QBIT_ROUTER_SONARR_CONFIG",
        "/mnt/drive-a/configs/sonarr/config.xml",
    ),
    "radarr": os.environ.get(
        "QBIT_ROUTER_RADARR_CONFIG",
        "/mnt/drive-a/configs/radarr/config.xml",
    ),
}
ARR_EXPECTED_ROOTS = {
    "sonarr": os.environ.get("QBIT_ROUTER_SONARR_ROOT", "/data/media/shows"),
    "radarr": os.environ.get("QBIT_ROUTER_RADARR_ROOT", "/data/media/movies"),
}
ARR_CATEGORY_FIELDS = {
    "sonarr": ("tvCategory", "tv"),
    "radarr": ("movieCategory", "movies"),
}
# Optional second qBittorrent-based download client. Empty by default, which
# means qBittorrent direct is the only managed client and strangers never see
# anyone's personal setup. Set a name to allow one extra client alongside it;
# qBittorrent direct always stays first choice (priority 1).
GATEWAY_NAME = os.environ.get("QBIT_ROUTER_GATEWAY_NAME", "").strip()
REQUIRE_GATEWAY = os.environ.get("QBIT_ROUTER_REQUIRE_GATEWAY", "0").lower() in {
    "1",
    "true",
    "yes",
}
QBIT_ROUTER_QBIT_PORT = int(os.environ.get("QBIT_ROUTER_QBIT_PORT", "8080"))
QBIT_ROUTER_GATEWAY_PORT = int(os.environ.get("QBIT_ROUTER_GATEWAY_PORT", "8283"))
BASE_REQUIRED_PREFS = {
    "add_stopped_enabled": True,
    "temp_path_enabled": False,
    "preallocate_all": True,
    "auto_tmm_enabled": False,
    "torrent_changed_tmm_enabled": False,
    "save_path_changed_tmm_enabled": False,
    "category_changed_tmm_enabled": False,
    "torrent_stop_condition": "MetadataReceived",
    # Keep completed downloads in a seeding state while Arr imports them.
    # A zero ratio/time limit makes Arr treat them as movable and defeats
    # copyUsingHardlinks, doubling I/O and breaking the intended lifecycle.
    "max_ratio_enabled": False,
    "max_seeding_time_enabled": False,
    "max_inactive_seeding_time_enabled": False,
}


def load_router_config(path: str) -> tuple[tuple[dict, ...], dict, str]:
    """Load and strictly validate the persistent branch registry."""
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1:
        raise RuntimeError("router config version must be 1")
    policy = config.get("policy", "most_free")
    if policy not in {"most_free", "first_fit"}:
        raise RuntimeError(f"unsupported router policy: {policy!r}")
    raw_branches = config.get("branches")
    if not isinstance(raw_branches, list) or not raw_branches:
        raise RuntimeError("router config must contain at least one branch")
    branches: list[dict] = []
    seen_names: set[str] = set()
    seen_tags: set[str] = set()
    seen_prefixes: set[str] = set()
    seen_roots: set[str] = set()
    for raw in raw_branches:
        if not isinstance(raw, dict):
            raise RuntimeError("every router branch must be an object")
        name = raw.get("name", "")
        tag = raw.get("tag", "")
        host_root = os.path.normpath(raw.get("host_root", ""))
        mount_root = os.path.normpath(raw.get("mount_root", host_root))
        prefix = os.path.normpath(raw.get("qbit_prefix", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise RuntimeError(f"invalid branch name: {name!r}")
        if not re.fullmatch(r"route_[a-z0-9][a-z0-9_-]*", tag):
            raise RuntimeError(f"invalid route tag for {name}: {tag!r}")
        if not host_root.startswith("/mnt/") or host_root == "/mnt":
            raise RuntimeError(f"unsafe host root for {name}: {host_root!r}")
        if not mount_root.startswith("/mnt/") or mount_root == "/mnt":
            raise RuntimeError(f"unsafe mount root for {name}: {mount_root!r}")
        if not prefix.startswith("/") or prefix == "/":
            raise RuntimeError(f"invalid qBittorrent prefix for {name}: {prefix!r}")
        reserve = int(raw.get("reserve_bytes", 50 * 1024**3))
        headroom = int(raw.get("route_headroom_bytes", 0))
        if reserve < 0 or headroom < 0:
            raise RuntimeError(f"negative capacity reserve for {name}")
        if name in seen_names or tag in seen_tags or prefix in seen_prefixes:
            raise RuntimeError(f"duplicate branch identity for {name}")
        if host_root in seen_roots:
            raise RuntimeError(f"duplicate branch root for {name}: {host_root}")
        seen_names.add(name)
        seen_tags.add(tag)
        seen_prefixes.add(prefix)
        seen_roots.add(host_root)
        branches.append(
            {
                "name": name,
                "tag": tag,
                "host_root": host_root,
                "mount_root": mount_root,
                "qbit_prefix": prefix.rstrip("/"),
                "reserve_bytes": reserve,
                "route_headroom_bytes": headroom,
            }
        )
    metadata_name = config.get("metadata_branch")
    metadata = next((branch for branch in branches if branch["name"] == metadata_name), None)
    if metadata is None:
        raise RuntimeError(f"metadata branch is not configured: {metadata_name!r}")
    return tuple(branches), metadata, policy


BRANCHES, METADATA_BRANCH, ROUTE_POLICY = load_router_config(ROUTER_CONFIG)
BRANCH_BY_TAG = {branch["tag"]: branch for branch in BRANCHES}
ROUTE_TAGS = set(BRANCH_BY_TAG)
METADATA_ROOT = METADATA_BRANCH["host_root"]
METADATA_PREFIX = METADATA_BRANCH["qbit_prefix"]
REQUIRED_PREFS = {
    **BASE_REQUIRED_PREFS,
    "save_path": f"{METADATA_PREFIX}/{DOWNLOAD_SUBPATH}",
    "temp_path": f"{METADATA_PREFIX}/{INCOMPLETE_SUBPATH}",
}
REQUIRED_CATEGORY_PATHS = {
    "tv": f"{METADATA_PREFIX}/{DOWNLOAD_SUBPATH}/tv",
    "movies": f"{METADATA_PREFIX}/{DOWNLOAD_SUBPATH}/movies",
}
_last_messages: dict[str, float] = {}
_last_arr_audit = 0.0
_last_arr_result = False
_managed_stopped_since: dict[str, float] = {}
_import_next_check: dict[str, float] = {}


class MetadataCleanupRetryable(RuntimeError):
    pass


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), message, flush=True)


def log_throttled(key: str, message: str, interval: float = 60) -> None:
    now = time.monotonic()
    if now - _last_messages.get(key, 0) >= interval:
        log(message)
        _last_messages[key] = now


def api_get(path: str, params: dict[str, str] | None = None):
    url = f"{QBIT_URL}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def api_post(path: str, data: dict[str, str]) -> None:
    request = urllib.request.Request(
        f"{QBIT_URL}/{path.lstrip('/')}",
        data=urllib.parse.urlencode(data).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace").strip()
        raise RuntimeError(
            f"qBittorrent POST {path} failed HTTP {error.code}: {body}"
        ) from error


def get_json_url(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def stop_all(reason: str) -> None:
    try:
        api_post("torrents/stop", {"hashes": "all"})
    finally:
        log_throttled(f"stop:{reason}", f"fail-closed: stopped all torrents: {reason}")


def fail_closed_after_error(reason: str) -> None:
    """Best-effort stop with restart intent after an unexpected router fault."""
    try:
        pause_for_config_repair(reason)
    except Exception as pause_error:
        try:
            stop_all(reason)
        except Exception as stop_error:
            log_throttled(
                "unexpected-error-stop",
                "fail-closed stop unavailable after router error: "
                f"pause={type(pause_error).__name__}: {pause_error}; "
                f"stop={type(stop_error).__name__}: {stop_error}",
                30,
            )


def pause_for_config_repair(reason: str) -> None:
    torrents = api_get("torrents/info")
    active_hashes = [
        torrent["hash"]
        for torrent in torrents
        if not (
            torrent.get("state", "").startswith("stopped")
            or torrent.get("state", "").startswith("paused")
        )
    ]
    if active_hashes:
        api_post(
            "torrents/addTags",
            {"hashes": "|".join(active_hashes), "tags": ROUTE_CONFIG_RESUME},
        )
    stop_all(reason)


def resume_after_config_repair(torrents: list[dict]) -> bool:
    paused = [torrent for torrent in torrents if ROUTE_CONFIG_RESUME in tags(torrent)]
    if not paused:
        return False
    resumed = 0
    for torrent in paused:
        torrent_tags = tags(torrent)
        routes = torrent_tags & ROUTE_TAGS
        safe = False
        if len(routes) == 1 and not torrent_tags & {
            ROUTE_PENDING_START,
            ROUTE_RECHECK_REQUESTED,
            ROUTE_RECHECK_ACTIVE,
            ROUTE_WAITING,
            ROUTE_ERROR,
            ROUTE_OVERCOMMIT,
            ROUTE_IMPORTED,
            ROUTE_IMPORT_FAILED,
        }:
            route_tag = next(iter(routes))
            safe = route_path_matches(route_tag, torrent.get("save_path", ""))
        if safe:
            api_post(
                "torrents/addTags",
                {"hashes": torrent["hash"], "tags": ROUTE_MANAGED_ACTIVE},
            )
            api_post("torrents/start", {"hashes": torrent["hash"]})
            resumed += 1
        api_post(
            "torrents/removeTags",
            {"hashes": torrent["hash"], "tags": ROUTE_CONFIG_RESUME},
        )
    log(
        f"configuration repair recovery processed={len(paused)} "
        f"resumed={resumed} held={len(paused) - resumed}"
    )
    return True


def mounts_ready() -> bool:
    required_mounts = tuple(
        dict.fromkeys([POOL_ROOT, *(branch["mount_root"] for branch in BRANCHES)])
    )
    missing = [path for path in required_mounts if not os.path.ismount(path)]
    missing_roots = [
        branch["host_root"]
        for branch in BRANCHES
        if not os.path.isdir(branch["host_root"])
    ]
    if missing or missing_roots:
        pause_for_config_repair(
            f"storage mounts unavailable: {missing or missing_roots}"
        )
        return False
    devices: dict[int, str] = {}
    for branch in BRANCHES:
        device = os.stat(branch["host_root"]).st_dev
        if device in devices:
            pause_for_config_repair(
                "storage branches share one filesystem: "
                f"{devices[device]} and {branch['host_root']}"
            )
            return False
        devices[device] = branch["host_root"]
    return True


def preferences_ready() -> bool:
    preferences = api_get("app/preferences")
    drift = {
        key: (preferences.get(key), expected)
        for key, expected in REQUIRED_PREFS.items()
        if preferences.get(key) != expected
    }
    if not drift:
        return True
    pause_for_config_repair(f"qBittorrent preference drift: {drift}")
    api_post("app/setPreferences", {"json": json.dumps(REQUIRED_PREFS)})
    log(f"repaired qBittorrent preferences: {sorted(drift)}")
    return False


def categories_ready() -> bool:
    categories = api_get("torrents/categories")
    drift = {}
    for category, save_path in REQUIRED_CATEGORY_PATHS.items():
        actual = categories.get(category)
        if (
            not actual
            or actual.get("savePath") != save_path
            or actual.get("download_path") is not False
        ):
            drift[category] = {
                "actual": actual,
                "expected_save": save_path,
                "expected_download": False,
            }
    if not drift:
        return True
    pause_for_config_repair(f"qBittorrent category path drift: {sorted(drift)}")
    for category in drift:
        save_path = REQUIRED_CATEGORY_PATHS[category]
        endpoint = "torrents/editCategory" if category in categories else "torrents/createCategory"
        api_post(
            endpoint,
            {
                "category": category,
                "savePath": save_path,
                "downloadPathEnabled": "false",
                "downloadPath": "",
            },
        )
    log(f"repaired qBittorrent category paths: {sorted(drift)}")
    return False


def arr_ready() -> bool:
    global _last_arr_audit, _last_arr_result
    now = time.monotonic()
    if now - _last_arr_audit < ARR_AUDIT_INTERVAL:
        return _last_arr_result
    _last_arr_audit = now
    required_mappings = {
        (
            "127.0.0.1",
            f"{branch['qbit_prefix']}/{DOWNLOAD_ROOT}",
            ARR_LOCAL_DOWNLOADS,
        )
        for branch in BRANCHES
    }
    try:
        for name, config_path in ARR_CONFIGS.items():
            config = ET.parse(config_path).getroot()
            api_key = config.findtext("ApiKey")
            port = config.findtext("Port")
            if not api_key or not port:
                raise RuntimeError(f"{name} API configuration missing")
            base = f"http://127.0.0.1:{port}/api/v3"
            headers = {"X-Api-Key": api_key}
            clients = get_json_url(f"{base}/downloadclient", headers)
            enabled_qbit = [
                item
                for item in clients
                if item.get("implementation") == "QBittorrent" and item.get("enable")
            ]
            client = next(
                (
                    item
                    for item in enabled_qbit
                    if {
                        field.get("name"): field.get("value")
                        for field in item.get("fields", [])
                    }.get("port")
                    == QBIT_ROUTER_QBIT_PORT
                ),
                None,
            )
            if not client or not client.get("enable"):
                raise RuntimeError(f"{name} qBittorrent client disabled or missing")
            gateway = None
            if GATEWAY_NAME:
                gateway = next(
                    (
                        item
                        for item in enabled_qbit
                        if item.get("name") == GATEWAY_NAME
                    ),
                    None,
                )
            if REQUIRE_GATEWAY and gateway is None:
                if not GATEWAY_NAME:
                    raise RuntimeError(
                        f"{name} gateway required but QBIT_ROUTER_GATEWAY_NAME is unset"
                    )
                raise RuntimeError(f"{name} gateway {GATEWAY_NAME!r} disabled or missing")
            allowed_ids = {client.get("id")}
            if gateway is not None:
                allowed_ids.add(gateway.get("id"))
            # Only other *qBittorrent* clients can steal routed downloads.
            # Unrelated clients (Usenet, etc.) are ignored.
            other_enabled = [
                item.get("name", item.get("implementation", "unknown"))
                for item in enabled_qbit
                if item.get("enable") and item.get("id") not in allowed_ids
            ]
            if other_enabled:
                raise RuntimeError(
                    f"{name} has non-routed download clients enabled: {other_enabled}"
                )
            fields = {field.get("name"): field.get("value") for field in client.get("fields", [])}
            category_field, category = ARR_CATEGORY_FIELDS[name]
            required_fields = {
                "host": "127.0.0.1",
                "port": QBIT_ROUTER_QBIT_PORT,
                "useSsl": False,
                "initialState": 2,
                category_field: category,
            }
            field_drift = {
                key: (fields.get(key), expected)
                for key, expected in required_fields.items()
                if fields.get(key) != expected
            }
            if field_drift:
                raise RuntimeError(
                    f"{name} qBittorrent client field drift: {field_drift}"
                )
            if client.get("priority") != 1:
                raise RuntimeError(
                    f"{name} qBittorrent must be first choice "
                    f"(priority 1, got {client.get('priority')!r})"
                )
            if gateway is not None:
                gateway_fields = {
                    field.get("name"): field.get("value")
                    for field in gateway.get("fields", [])
                }
                gateway_required_fields = {
                    "host": "127.0.0.1",
                    "port": QBIT_ROUTER_GATEWAY_PORT,
                    "useSsl": False,
                    "initialState": 2,
                    category_field: category,
                }
                gateway_drift = {
                    key: (gateway_fields.get(key), expected)
                    for key, expected in gateway_required_fields.items()
                    if gateway_fields.get(key) != expected
                }
                if gateway_drift:
                    raise RuntimeError(
                        f"{name} gateway drift: fields={gateway_drift}"
                    )
                if gateway.get("priority") == 1:
                    raise RuntimeError(
                        f"{name} gateway must not outrank qBittorrent "
                        f"(priorities={(gateway.get('priority'), client.get('priority'))})"
                    )
            mappings = get_json_url(f"{base}/remotepathmapping", headers)
            actual_mappings = {
                (item.get("host"), item.get("remotePath"), item.get("localPath"))
                for item in mappings
            }
            if not required_mappings.issubset(actual_mappings):
                raise RuntimeError(f"{name} remote path mappings are incomplete")
            roots = get_json_url(f"{base}/rootfolder", headers)
            expected_root = ARR_EXPECTED_ROOTS[name]
            if len(roots) != 1 or roots[0].get("path") != expected_root:
                raise RuntimeError(
                    f"{name} root folders must be exactly {[expected_root]!r}"
                )
            if not roots[0].get("accessible", False):
                raise RuntimeError(f"{name} root folder is inaccessible")
            media_management = get_json_url(
                f"{base}/config/mediamanagement", headers
            )
            if media_management.get("copyUsingHardlinks") is not True:
                raise RuntimeError(f"{name} hardlink imports are disabled")
    except Exception as error:
        _last_arr_result = False
        try:
            pause_for_config_repair(f"arr audit failed: {error}")
        except Exception as pause_error:
            log_throttled(
                "arr-audit-pause",
                "fail-closed: could not stop transfers after arr audit failure: "
                f"{type(pause_error).__name__}: {pause_error}",
                30,
            )
        log_throttled("arr-audit", f"fail-closed: arr audit failed: {error}", 30)
        return False
    _last_arr_result = True
    return True


def arr_active_download_ids() -> set[str]:
    """Return hashes still present in an Arr activity queue.

    Cleanup is fail-closed: an incomplete or unreadable queue prevents
    qBittorrent payload deletion.
    """
    active: set[str] = set()
    for name, config_path in ARR_CONFIGS.items():
        config = ET.parse(config_path).getroot()
        api_key = config.findtext("ApiKey")
        port = config.findtext("Port")
        if not api_key or not port:
            raise RuntimeError(f"{name} API configuration missing")
        base = f"http://127.0.0.1:{port}/api/v3"
        queue = get_json_url(
            f"{base}/queue?page=1&pageSize=1000&"
            "includeUnknownMovieItems=true&includeUnknownSeriesItems=true",
            {"X-Api-Key": api_key},
        )
        records = queue.get("records", [])
        total = int(queue.get("totalRecords", len(records)))
        if total > len(records):
            raise RuntimeError(
                f"{name} activity queue exceeds cleanup audit page: "
                f"records={len(records)} total={total}"
            )
        active.update(
            str(record["downloadId"]).upper()
            for record in records
            if record.get("downloadId")
        )
    return active


def tags(torrent: dict) -> set[str]:
    return {tag.strip() for tag in torrent.get("tags", "").split(",") if tag.strip()}


def share_limits_ready(torrents: list[dict]) -> bool:
    fields = ("ratio_limit", "seeding_time_limit", "inactive_seeding_time_limit")
    drift = [
        torrent
        for torrent in torrents
        if tags(torrent) & ROUTE_TAGS
        and (
            any(int(torrent.get(field, -2)) != -2 for field in fields)
            or torrent.get("share_limit_action", "Default") != "Default"
        )
    ]
    if not drift:
        return True
    hashes = "|".join(torrent["hash"] for torrent in drift)
    pause_for_config_repair(
        f"per-torrent seed limit drift affected={len(drift)}"
    )
    api_post(
        "torrents/setShareLimits",
        {
            "hashes": hashes,
            "ratioLimit": "-2",
            "seedingTimeLimit": "-2",
            "inactiveSeedingTimeLimit": "-2",
            "shareLimitAction": "Default",
        },
    )
    log(f"repaired per-torrent seed limits affected={len(drift)}")
    return False


def sync_managed_active(torrents: list[dict]) -> bool:
    """Persist router start intent without overriding a stable user stop.

    qBittorrent can briefly return a stopped snapshot immediately after an
    accepted start request. The persistent marker closes that race for later
    fail-closed capacity recovery. A routed torrent that remains manually
    stopped for the grace interval loses the marker and is never auto-resumed.
    """
    now = time.monotonic()
    transient = {
        ROUTE_PENDING_START,
        ROUTE_RECHECK_REQUESTED,
        ROUTE_RECHECK_ACTIVE,
        ROUTE_WAITING,
        ROUTE_ERROR,
        ROUTE_OVERCOMMIT,
        ROUTE_CONFIG_RESUME,
        ROUTE_METADATA,
        ROUTE_IMPORT_THROTTLED,
        ROUTE_IMPORTED,
        ROUTE_IMPORT_FAILED,
    }
    add_hashes: list[str] = []
    remove_hashes: list[str] = []
    routed_hashes: set[str] = set()
    for torrent in torrents:
        torrent_hash = torrent["hash"]
        torrent_tags = tags(torrent)
        if len(torrent_tags & ROUTE_TAGS) != 1:
            _managed_stopped_since.pop(torrent_hash, None)
            continue
        routed_hashes.add(torrent_hash)
        if torrent_tags & transient:
            _managed_stopped_since.pop(torrent_hash, None)
            continue
        state = torrent.get("state", "")
        stopped = state.startswith("stopped") or state.startswith("paused")
        if not stopped:
            _managed_stopped_since.pop(torrent_hash, None)
            if ROUTE_MANAGED_ACTIVE not in torrent_tags:
                add_hashes.append(torrent_hash)
            continue
        if ROUTE_MANAGED_ACTIVE not in torrent_tags:
            _managed_stopped_since.pop(torrent_hash, None)
            continue
        stopped_since = _managed_stopped_since.setdefault(torrent_hash, now)
        if now - stopped_since >= MANAGED_STOP_GRACE:
            remove_hashes.append(torrent_hash)
            _managed_stopped_since.pop(torrent_hash, None)

    for torrent_hash in set(_managed_stopped_since) - routed_hashes:
        _managed_stopped_since.pop(torrent_hash, None)
    if add_hashes:
        api_post(
            "torrents/addTags",
            {"hashes": "|".join(add_hashes), "tags": ROUTE_MANAGED_ACTIVE},
        )
        log(f"recorded managed-active intent affected={len(add_hashes)}")
    if remove_hashes:
        api_post(
            "torrents/removeTags",
            {"hashes": "|".join(remove_hashes), "tags": ROUTE_MANAGED_ACTIVE},
        )
        log(f"respected stable user stop affected={len(remove_hashes)}")
    return bool(add_hashes or remove_hashes)


def branch_free(path: str) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def torrent_has_import_hardlink(torrent: dict, route_tag: str) -> bool:
    """Return true once Arr has hardlinked at least one selected video file.

    The source and library live on the same physical branch. A source video
    link count of two or more is therefore a direct, filesystem-level import
    acknowledgement that does not depend on Arr queue display semantics.
    """
    path = host_content_path(torrent, route_tag)
    if not path:
        return False
    candidates: list[str] = []
    if os.path.isfile(path):
        candidates = [path]
    elif os.path.isdir(path):
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not os.path.islink(os.path.join(root, name))
            ]
            candidates.extend(
                os.path.join(root, name)
                for name in files
                if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS
            )
    for candidate in candidates:
        try:
            if os.stat(candidate, follow_symlinks=False).st_nlink >= 2:
                return True
        except (FileNotFoundError, PermissionError):
            continue
    return False


def quiesce_completed_torrents(torrents: list[dict]) -> bool:
    """Clean completed torrents only after Arr import, or stop on timeout.

    A filesystem hardlink is the import acknowledgement. Imported torrents
    are stopped immediately, retained for a cleanup grace period, and deleted
    with their qBittorrent payload only after their hash has also disappeared
    from both Arr activity queues. Removing the source link leaves the library
    hardlink intact. A completed item that Arr cannot import is stopped after
    the settle window and tagged for intervention; it is never auto-deleted.
    While Arr is creating the hardlink, cap upload to one byte per second.
    """
    now_wall = time.time()
    now_mono = time.monotonic()
    imported: list[str] = []
    failed: list[str] = []
    recovered: list[str] = []
    throttle_hashes: list[str] = []
    clear_throttle: list[str] = []
    stop_hashes: list[str] = []
    remove_managed: list[str] = []
    cleanup_candidates: list[str] = []
    live_hashes: set[str] = set()

    for torrent in torrents:
        torrent_hash = torrent["hash"]
        live_hashes.add(torrent_hash)
        if float(torrent.get("progress", 0)) < 1:
            _import_next_check.pop(torrent_hash, None)
            continue
        torrent_tags = tags(torrent)
        routes = torrent_tags & ROUTE_TAGS
        if len(routes) != 1 or torrent_tags & {
            ROUTE_PENDING_START,
            ROUTE_RECHECK_REQUESTED,
            ROUTE_RECHECK_ACTIVE,
            ROUTE_ERROR,
            ROUTE_OVERCOMMIT,
            ROUTE_CONFIG_RESUME,
            ROUTE_METADATA,
        }:
            continue
        route_tag = next(iter(routes))
        state = torrent.get("state", "")
        stopped = state.startswith("stopped") or state.startswith("paused")

        if ROUTE_IMPORTED in torrent_tags:
            _import_next_check.pop(torrent_hash, None)
            if not stopped:
                stop_hashes.append(torrent_hash)
            else:
                completion_on = int(torrent.get("completion_on", -1))
                if (
                    completion_on > 0
                    and now_wall - completion_on >= IMPORT_CLEANUP_GRACE_SECONDS
                ):
                    cleanup_candidates.append(torrent_hash)
            if ROUTE_IMPORT_THROTTLED in torrent_tags:
                clear_throttle.append(torrent_hash)
            if ROUTE_MANAGED_ACTIVE in torrent_tags:
                remove_managed.append(torrent_hash)
            continue

        due = _import_next_check.get(torrent_hash, 0)
        has_hardlink = False
        if now_mono >= due:
            has_hardlink = torrent_has_import_hardlink(torrent, route_tag)
            _import_next_check[torrent_hash] = now_mono + IMPORT_CHECK_INTERVAL

        if has_hardlink:
            imported.append(torrent_hash)
            if ROUTE_IMPORT_THROTTLED in torrent_tags:
                clear_throttle.append(torrent_hash)
            if ROUTE_IMPORT_FAILED in torrent_tags:
                recovered.append(torrent_hash)
            stop_hashes.append(torrent_hash)
            if ROUTE_MANAGED_ACTIVE in torrent_tags:
                remove_managed.append(torrent_hash)
            _import_next_check.pop(torrent_hash, None)
            continue

        completion_on = int(torrent.get("completion_on", -1))
        if (
            ROUTE_IMPORT_FAILED in torrent_tags
            or (
                completion_on > 0
                and now_wall - completion_on >= IMPORT_SETTLE_SECONDS
            )
        ):
            if ROUTE_IMPORT_FAILED not in torrent_tags:
                failed.append(torrent_hash)
            if ROUTE_IMPORT_THROTTLED in torrent_tags:
                clear_throttle.append(torrent_hash)
            if not stopped:
                stop_hashes.append(torrent_hash)
            if ROUTE_MANAGED_ACTIVE in torrent_tags:
                remove_managed.append(torrent_hash)
            continue

        if not stopped and ROUTE_IMPORT_THROTTLED not in torrent_tags:
            throttle_hashes.append(torrent_hash)

    for torrent_hash in set(_import_next_check) - live_hashes:
        _import_next_check.pop(torrent_hash, None)
    delete_hashes: list[str] = []
    held_active: list[str] = []
    if cleanup_candidates:
        active_download_ids = arr_active_download_ids()
        for torrent_hash in cleanup_candidates:
            if torrent_hash.upper() in active_download_ids:
                held_active.append(torrent_hash)
            else:
                delete_hashes.append(torrent_hash)
    if throttle_hashes:
        hashes = "|".join(throttle_hashes)
        api_post("torrents/setUploadLimit", {"hashes": hashes, "limit": "1"})
        api_post(
            "torrents/addTags",
            {"hashes": hashes, "tags": ROUTE_IMPORT_THROTTLED},
        )
    if imported:
        api_post(
            "torrents/addTags",
            {"hashes": "|".join(imported), "tags": ROUTE_IMPORTED},
        )
    if recovered:
        api_post(
            "torrents/removeTags",
            {"hashes": "|".join(recovered), "tags": ROUTE_IMPORT_FAILED},
        )
    if failed:
        api_post(
            "torrents/addTags",
            {"hashes": "|".join(failed), "tags": ROUTE_IMPORT_FAILED},
        )
    if clear_throttle:
        api_post(
            "torrents/removeTags",
            {
                "hashes": "|".join(dict.fromkeys(clear_throttle)),
                "tags": ROUTE_IMPORT_THROTTLED,
            },
        )
    if stop_hashes:
        api_post("torrents/stop", {"hashes": "|".join(dict.fromkeys(stop_hashes))})
    if remove_managed:
        api_post(
            "torrents/removeTags",
            {
                "hashes": "|".join(dict.fromkeys(remove_managed)),
                "tags": ROUTE_MANAGED_ACTIVE,
            },
        )
    if delete_hashes:
        api_post(
            "torrents/delete",
            {
                "hashes": "|".join(delete_hashes),
                "deleteFiles": "true",
            },
        )
    if imported:
        log(f"post-import stop confirmed hardlinks affected={len(imported)}")
    if throttle_hashes:
        log(
            "post-completion upload throttled pending import "
            f"affected={len(throttle_hashes)} limit=1"
        )
    if failed:
        log(
            "post-import timeout stopped without hardlink "
            f"affected={len(failed)} settle_seconds={IMPORT_SETTLE_SECONDS:g}"
        )
    if recovered:
        log(f"post-import failure recovered affected={len(recovered)}")
    if delete_hashes:
        log(
            "post-import cleanup deleted torrent payloads "
            f"affected={len(delete_hashes)} grace_seconds="
            f"{IMPORT_CLEANUP_GRACE_SECONDS:g}"
        )
    if held_active:
        log_throttled(
            "post-import-cleanup-held",
            "post-import cleanup held for active Arr queue "
            f"affected={len(held_active)}",
            30,
        )
    return bool(
        imported
        or failed
        or recovered
        or throttle_hashes
        or clear_throttle
        or stop_hashes
        or remove_managed
        or delete_hashes
    )


def allocated_bytes(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.stat(path, follow_symlinks=False).st_blocks * 512
        if not os.path.isdir(path):
            return 0
        total = 0
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
            for name in files:
                file_path = os.path.join(root, name)
                try:
                    total += os.stat(file_path, follow_symlinks=False).st_blocks * 512
                except FileNotFoundError:
                    pass
        return total
    except (FileNotFoundError, PermissionError):
        return 0


def metadata_host_content_path(torrent: dict) -> str | None:
    """Return a verified host path for this torrent's metadata payload."""
    content_path = os.path.normpath(torrent.get("content_path", ""))
    prefix = METADATA_PREFIX + "/"
    if not content_path.startswith(prefix):
        return None
    host_path = os.path.normpath(
        METADATA_ROOT + content_path[len(METADATA_PREFIX):]
    )
    staging_root = os.path.realpath(
        os.path.join(METADATA_ROOT, *DOWNLOAD_SUBPATH.split("/"))
    )
    resolved = os.path.realpath(host_path)
    if resolved == staging_root or not resolved.startswith(staging_root + os.sep):
        return None
    return host_path


def remove_metadata_payload(torrent: dict) -> None:
    """Remove only the bounded payload created while fetching magnet metadata.

    qBittorrent creates full-logical-size sparse placeholders as soon as magnet
    metadata arrives. Asking it to move those placeholders between filesystems
    expands the holes and serializes all later routing. The torrent is stopped
    before this function runs; after cleanup, a forced recheck resets its resume
    state before it is ever started at the selected destination.
    """
    if ROUTE_METADATA not in tags(torrent):
        return
    state = torrent.get("state", "")
    if not (state.startswith("stopped") or state.startswith("paused")):
        raise RuntimeError(f"metadata payload is not stopped (state={state})")
    if float(torrent.get("progress", 0)) >= 1:
        raise RuntimeError("refusing to clean a completed torrent")
    path = metadata_host_content_path(torrent)
    if path is None:
        raise RuntimeError(
            f"unsafe metadata content path: {torrent.get('content_path', '')!r}"
        )
    if not os.path.lexists(path):
        log(f"metadata payload already absent hash={torrent['hash'][:12]} path={path!r}")
        return
    if os.path.islink(path):
        raise RuntimeError(f"refusing to clean symlink metadata path: {path!r}")
    allocated = allocated_bytes(path)
    downloaded = max(0, int(torrent.get("downloaded", 0)))
    if allocated > METADATA_CLEANUP_LIMIT or downloaded > METADATA_CLEANUP_LIMIT:
        raise RuntimeError(
            f"metadata payload exceeds cleanup limit allocated={allocated} "
            f"downloaded={downloaded} limit={METADATA_CLEANUP_LIMIT}"
        )
    if os.path.isdir(path):
        for attempt in range(8):
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if error.errno not in {errno.ENOTEMPTY, errno.EBUSY}:
                    raise
                if attempt == 7:
                    raise MetadataCleanupRetryable(
                        f"metadata directory remained busy after retries: {path!r}"
                    ) from error
                # NTFS and qBittorrent can briefly race on sparse placeholder
                # creation even after the stop state is visible. Revalidate the
                # cleanup bounds before each bounded retry.
                time.sleep(0.25)
                if allocated_bytes(path) > METADATA_CLEANUP_LIMIT:
                    raise RuntimeError(
                        f"metadata payload grew beyond cleanup limit: {path!r}"
                    )
    else:
        os.unlink(path)
    log(
        f"removed metadata placeholder hash={torrent['hash'][:12]} "
        f"allocated={allocated} downloaded={downloaded} path={path!r}"
    )


def host_content_path(torrent: dict, route_tag: str) -> str | None:
    branch = BRANCH_BY_TAG.get(route_tag)
    if branch is None:
        return None
    content_path = torrent.get("content_path", "")
    prefix = branch["qbit_prefix"]
    if content_path.startswith(prefix + "/"):
        return branch["host_root"] + content_path[len(prefix):]
    return None


def outstanding_reservations(torrents: list[dict]) -> dict[str, int]:
    result = {route_tag: 0 for route_tag in ROUTE_TAGS}
    for torrent in torrents:
        if float(torrent.get("progress", 0)) >= 1:
            continue
        torrent_tags = tags(torrent)
        route_tag = next((tag for tag in ROUTE_TAGS if tag in torrent_tags), None)
        if not route_tag:
            continue
        selected_size = max(0, int(torrent.get("size", 0)))
        path = host_content_path(torrent, route_tag)
        allocated = allocated_bytes(path) if path else 0
        result[route_tag] += max(0, selected_size - allocated)
    return result


def safe_category(value: str) -> str:
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z0-9._-]+", value) else ""


def target_path(prefix: str, torrent: dict) -> str:
    base = f"{prefix}/{DOWNLOAD_SUBPATH}"
    category = safe_category(torrent.get("category", ""))
    return f"{base}/{category}" if category else base


def route_location(route_tag: str, torrent: dict) -> str:
    return target_path(BRANCH_BY_TAG[route_tag]["qbit_prefix"], torrent)


def route_path_matches(route_tag: str, save_path: str) -> bool:
    branch = BRANCH_BY_TAG.get(route_tag)
    if branch is None:
        return False
    prefix = branch["qbit_prefix"]
    base = prefix + "/" + DOWNLOAD_SUBPATH
    return save_path == base or save_path.startswith(base + "/")


def metadata_path_matches(save_path: str) -> bool:
    base = METADATA_PREFIX + "/" + DOWNLOAD_SUBPATH
    return save_path == base or save_path.startswith(base + "/")


def begin_metadata_discovery(torrent: dict) -> None:
    """Stage a magnet on the metadata branch at a bounded rate."""
    torrent_hash = torrent["hash"]
    location = target_path(METADATA_PREFIX, torrent)
    api_post("torrents/stop", {"hashes": torrent_hash})
    api_post("torrents/setAutoManagement", {"hashes": torrent_hash, "enable": "false"})
    api_post(
        "torrents/setDownloadLimit",
        {"hashes": torrent_hash, "limit": str(METADATA_DL_LIMIT)},
    )
    api_post(
        "torrents/addTags",
        {"hashes": torrent_hash, "tags": f"{ROUTE_METADATA},{ROUTE_METADATA_PENDING}"},
    )
    api_post("torrents/removeTags", {"hashes": torrent_hash, "tags": ROUTE_WAITING})
    api_post("torrents/removeTags", {"hashes": torrent_hash, "tags": ROUTE_ERROR})
    api_post("torrents/setLocation", {"hashes": torrent_hash, "location": location})
    log(
        f"metadata staging pending hash={torrent_hash[:12]} location={location} "
        f"payload_limit={METADATA_DL_LIMIT}"
    )


def route_torrent(torrent: dict, route_tag: str) -> None:
    torrent_hash = torrent["hash"]
    location = route_location(route_tag, torrent)
    api_post("torrents/stop", {"hashes": torrent_hash})
    api_post("torrents/setAutoManagement", {"hashes": torrent_hash, "enable": "false"})
    api_post(
        "torrents/setShareLimits",
        {
            "hashes": torrent_hash,
            "ratioLimit": "-2",
            "seedingTimeLimit": "-2",
            "inactiveSeedingTimeLimit": "-2",
            "shareLimitAction": "Default",
        },
    )
    needs_recheck = ROUTE_METADATA in tags(torrent)
    if needs_recheck:
        try:
            remove_metadata_payload(torrent)
        except MetadataCleanupRetryable as error:
            log_throttled(
                f"metadata-cleanup-retry:{torrent_hash}",
                f"metadata cleanup deferred hash={torrent_hash[:12]} error={error}",
                10,
            )
            return
        except Exception as error:
            api_post("torrents/addTags", {"hashes": torrent_hash, "tags": ROUTE_ERROR})
            log(
                f"fail-closed: metadata cleanup failed hash={torrent_hash[:12]} "
                f"error={type(error).__name__}: {error}"
            )
            return
    pending_tags = [route_tag, ROUTE_PENDING_START]
    if needs_recheck:
        pending_tags.append(ROUTE_RECHECK_REQUESTED)
    api_post(
        "torrents/addTags",
        {"hashes": torrent_hash, "tags": ",".join(pending_tags)},
    )
    api_post("torrents/removeTags", {"hashes": torrent_hash, "tags": ROUTE_WAITING})
    api_post("torrents/removeTags", {"hashes": torrent_hash, "tags": ROUTE_ERROR})
    api_post("torrents/removeTags", {"hashes": torrent_hash, "tags": ROUTE_METADATA_PENDING})
    api_post("torrents/setDownloadLimit", {"hashes": torrent_hash, "limit": "0"})
    api_post("torrents/setLocation", {"hashes": torrent_hash, "location": location})
    log(
        f"routing pending hash={torrent_hash[:12]} name={torrent.get('name', '')!r} "
        f"size={int(torrent.get('size', 0))} branch={route_tag} location={location}"
    )


def process_once() -> None:
    if not mounts_ready() or not preferences_ready() or not categories_ready():
        return
    torrents = sorted(
        api_get("torrents/info"),
        key=lambda torrent: (int(torrent.get("added_on", 0)), torrent.get("hash", "")),
    )

    if any(torrent.get("state") == "checkingResumeData" for torrent in torrents):
        log_throttled(
            "resume-data",
            "waiting for qBittorrent resume-data restoration before auditing routes",
            30,
        )
        return
    if not share_limits_ready(torrents):
        return
    # Audit Arr before recovering transfers that were stopped for a prior
    # configuration failure. Otherwise an unrepaired Arr failure would cause
    # a start/stop loop and briefly run downloads through an unsafe mapping.
    if not arr_ready():
        return
    if resume_after_config_repair(torrents):
        return

    route_errors = False
    route_recoveries = False
    for torrent in torrents:
        torrent_tags = tags(torrent)
        routes = torrent_tags & ROUTE_TAGS
        save_path = torrent.get("save_path", "")
        expected_route = None
        expected_prefix = None
        valid_route = True
        if len(routes) == 1:
            expected_route = next(iter(routes))
        elif len(routes) > 1:
            valid_route = False
        elif ROUTE_METADATA in torrent_tags:
            expected_prefix = METADATA_PREFIX + "/" + DOWNLOAD_SUBPATH
        if expected_route is not None and not route_path_matches(expected_route, save_path):
            if ROUTE_PENDING_START in torrent_tags:
                log_throttled(
                    f"pending-move:{torrent['hash']}",
                    f"waiting for route move hash={torrent['hash'][:12]} "
                    f"branch={expected_route} state={torrent.get('state', '')} "
                    f"save_path={save_path!r}",
                    30,
                )
                continue
            valid_route = False
        elif expected_prefix is not None and not metadata_path_matches(save_path):
            if ROUTE_METADATA_PENDING in torrent_tags:
                log_throttled(
                    f"pending-metadata-move:{torrent['hash']}",
                    f"waiting for metadata staging move hash={torrent['hash'][:12]} "
                    f"state={torrent.get('state', '')} save_path={save_path!r}",
                    30,
                )
                continue
            valid_route = False
        if not valid_route:
            api_post("torrents/stop", {"hashes": torrent["hash"]})
            api_post("torrents/addTags", {"hashes": torrent["hash"], "tags": ROUTE_ERROR})
            log_throttled(
                f"route-error:{torrent['hash']}",
                f"fail-closed: route tag/path mismatch hash={torrent['hash'][:12]} "
                f"tags={sorted(routes)} save_path={save_path!r}",
            )
            route_errors = True
        elif ROUTE_ERROR in torrent_tags and len(routes) == 1:
            api_post(
                "torrents/addTags",
                {"hashes": torrent["hash"], "tags": ROUTE_PENDING_START},
            )
            api_post(
                "torrents/removeTags",
                {"hashes": torrent["hash"], "tags": ROUTE_ERROR},
            )
            log(f"recovered corrected route hash={torrent['hash'][:12]}")
            route_recoveries = True
    if route_errors or route_recoveries:
        return

    if quiesce_completed_torrents(torrents):
        return

    # Metadata discovery must not start until qBittorrent confirms the direct
    # metadata-branch path. This prevents preallocation through pooled /data.
    metadata_pending_changed = False
    for torrent in torrents:
        torrent_tags = tags(torrent)
        if ROUTE_METADATA_PENDING not in torrent_tags:
            continue
        location = target_path(METADATA_PREFIX, torrent)
        save_path = torrent.get("save_path", "")
        state = torrent.get("state", "")
        if state == "moving":
            log_throttled(
                f"metadata-pending-moving:{torrent['hash']}",
                f"waiting for metadata staging hash={torrent['hash'][:12]} "
                f"save_path={save_path!r}",
                30,
            )
            continue
        if not metadata_path_matches(save_path):
            api_post("torrents/stop", {"hashes": torrent["hash"]})
            api_post(
                "torrents/setLocation",
                {"hashes": torrent["hash"], "location": location},
            )
            metadata_pending_changed = True
            continue
        selected_size = max(0, int(torrent.get("size", 0)))
        if selected_size == 0 and (state.startswith("stopped") or state.startswith("paused")):
            api_post("torrents/start", {"hashes": torrent["hash"]})
            log(
                f"metadata staging finalized hash={torrent['hash'][:12]} "
                f"location={location} stop_condition=MetadataReceived"
            )
        else:
            log(
                f"metadata staging already active hash={torrent['hash'][:12]} "
                f"location={location} size={selected_size} state={state}"
            )
        api_post(
            "torrents/removeTags",
            {"hashes": torrent["hash"], "tags": ROUTE_METADATA_PENDING},
        )
        metadata_pending_changed = True
    if metadata_pending_changed:
        return

    # A location change may be asynchronous. Keep the torrent stopped and
    # tagged pending until qBittorrent reports the final path and exits Moving.
    # Metadata-routed torrents are rechecked after their sparse staging payload
    # is removed, so qBittorrent cannot retain stale completed-piece state.
    pending_changed = False
    for torrent in torrents:
        torrent_tags = tags(torrent)
        if ROUTE_PENDING_START in torrent_tags:
            routes = torrent_tags & ROUTE_TAGS
            if len(routes) != 1:
                continue
            route_tag = next(iter(routes))
            location = route_location(route_tag, torrent)
            save_path = torrent.get("save_path", "")
            state = torrent.get("state", "")
            if state == "moving":
                log_throttled(
                    f"pending-moving:{torrent['hash']}",
                    f"waiting for move completion hash={torrent['hash'][:12]} "
                    f"branch={route_tag} save_path={save_path!r}",
                    30,
                )
                continue
            if not route_path_matches(route_tag, save_path):
                api_post("torrents/stop", {"hashes": torrent["hash"]})
                api_post(
                    "torrents/setLocation",
                    {"hashes": torrent["hash"], "location": location},
                )
                log_throttled(
                    f"pending-location:{torrent['hash']}",
                    f"waiting for destination hash={torrent['hash'][:12]} "
                    f"branch={route_tag} state={state} save_path={save_path!r}",
                    30,
                )
                pending_changed = True
                continue
            # A start request is a two-phase handshake. Keep the pending tag
            # until a later snapshot confirms a non-stopped state; qBittorrent
            # may accept start while a completed recheck is still settling.
            if (
                ROUTE_MANAGED_ACTIVE in torrent_tags
                and ROUTE_RECHECK_REQUESTED not in torrent_tags
                and ROUTE_RECHECK_ACTIVE not in torrent_tags
            ):
                if state.startswith("stopped") or state.startswith("paused"):
                    api_post("torrents/start", {"hashes": torrent["hash"]})
                    log_throttled(
                        f"pending-start:{torrent['hash']}",
                        f"waiting for confirmed start hash={torrent['hash'][:12]} "
                        f"branch={route_tag} state={state}",
                        10,
                    )
                else:
                    api_post(
                        "torrents/removeTags",
                        {"hashes": torrent["hash"], "tags": ROUTE_METADATA},
                    )
                    api_post(
                        "torrents/removeTags",
                        {"hashes": torrent["hash"], "tags": ROUTE_PENDING_START},
                    )
                    log(
                        f"route finalized hash={torrent['hash'][:12]} "
                        f"branch={route_tag} location={location} state={state}"
                    )
                pending_changed = True
                continue
            if ROUTE_RECHECK_REQUESTED in torrent_tags:
                if not (state.startswith("stopped") or state.startswith("paused")):
                    api_post("torrents/stop", {"hashes": torrent["hash"]})
                    pending_changed = True
                    continue
                # Add the active marker first. If the service exits before the
                # request marker is removed, the next pass safely rechecks again.
                api_post(
                    "torrents/addTags",
                    {"hashes": torrent["hash"], "tags": ROUTE_RECHECK_ACTIVE},
                )
                api_post("torrents/recheck", {"hashes": torrent["hash"]})
                api_post(
                    "torrents/removeTags",
                    {"hashes": torrent["hash"], "tags": ROUTE_RECHECK_REQUESTED},
                )
                log(
                    f"forced post-routing recheck hash={torrent['hash'][:12]} "
                    f"branch={route_tag} location={location}"
                )
                pending_changed = True
                continue
            if ROUTE_RECHECK_ACTIVE in torrent_tags:
                if state.startswith("checking"):
                    log_throttled(
                        f"pending-recheck:{torrent['hash']}",
                        f"waiting for post-routing recheck hash={torrent['hash'][:12]} "
                        f"branch={route_tag} state={state}",
                        30,
                    )
                    continue
                if not (state.startswith("stopped") or state.startswith("paused")):
                    api_post("torrents/stop", {"hashes": torrent["hash"]})
                    pending_changed = True
                    continue
                api_post(
                    "torrents/removeTags",
                    {"hashes": torrent["hash"], "tags": ROUTE_RECHECK_ACTIVE},
                )
            if (
                float(torrent.get("progress", 0)) < 1
                and not (state.startswith("stopped") or state.startswith("paused"))
            ):
                api_post("torrents/stop", {"hashes": torrent["hash"]})
                pending_changed = True
                continue
            api_post(
                "torrents/setDownloadLimit",
                {"hashes": torrent["hash"], "limit": "0"},
            )
            api_post(
                "torrents/addTags",
                {"hashes": torrent["hash"], "tags": ROUTE_MANAGED_ACTIVE},
            )
            # Record intent before the start call. If qBittorrent returns a
            # stale stopped snapshot, overcommit recovery still knows this
            # router-managed torrent was meant to be active.
            api_post("torrents/start", {"hashes": torrent["hash"]})
            log(
                f"route start requested hash={torrent['hash'][:12]} "
                f"branch={route_tag} location={location}"
            )
            pending_changed = True
    if pending_changed:
        return

    # Quiesce every newly submitted torrent before calculating a batch. This
    # prevents a later-ready torrent from jumping ahead while qBittorrent is
    # still loading resume data for an earlier addition.
    quiescing = False
    for torrent in torrents:
        if (
            float(torrent.get("progress", 0)) >= 1
            or tags(torrent) & (ROUTE_TAGS | {ROUTE_METADATA})
        ):
            continue
        state = torrent.get("state", "")
        if not (state.startswith("stopped") or state.startswith("paused")):
            api_post("torrents/stop", {"hashes": torrent["hash"]})
            log(f"stopped unrouted torrent hash={torrent['hash'][:12]} state={state}")
            quiescing = True
    if quiescing:
        return

    # A stopped magnet has no usable size. Fetch only its metadata on the
    # direct metadata path, then stop it when qBittorrent reports the real
    # size. The final route is chosen only after that stop is confirmed.
    metadata_changed = False
    for torrent in torrents:
        torrent_tags = tags(torrent)
        if ROUTE_METADATA not in torrent_tags or torrent_tags & ROUTE_TAGS:
            continue
        if ROUTE_METADATA_PENDING in torrent_tags:
            continue
        selected_size = max(0, int(torrent.get("size", 0)))
        state = torrent.get("state", "")
        if selected_size == 0:
            if int(torrent.get("dl_limit", 0)) != METADATA_DL_LIMIT:
                api_post(
                    "torrents/setDownloadLimit",
                    {"hashes": torrent["hash"], "limit": str(METADATA_DL_LIMIT)},
                )
            if state.startswith("stopped") or state.startswith("paused"):
                api_post("torrents/start", {"hashes": torrent["hash"]})
                log(f"recovered metadata discovery hash={torrent['hash'][:12]}")
            continue
        if not (state.startswith("stopped") or state.startswith("paused")):
            api_post("torrents/stop", {"hashes": torrent["hash"]})
            log(
                f"metadata acquired; stopped for routing hash={torrent['hash'][:12]} "
                f"size={selected_size} state={state}"
            )
            metadata_changed = True

    for torrent in torrents:
        if float(torrent.get("progress", 0)) >= 1:
            continue
        torrent_tags = tags(torrent)
        if torrent_tags & (ROUTE_TAGS | {ROUTE_METADATA}) or ROUTE_ERROR in torrent_tags:
            continue
        if max(0, int(torrent.get("size", 0))) == 0:
            begin_metadata_discovery(torrent)
            metadata_changed = True
    if metadata_changed:
        return

    if sync_managed_active(torrents):
        return

    reservations = outstanding_reservations(torrents)
    raw_budgets = {
        branch["tag"]: (
            branch_free(branch["host_root"])
            - branch["reserve_bytes"]
            - reservations[branch["tag"]]
        )
        for branch in BRANCHES
    }
    for route_tag, budget in raw_budgets.items():
        affected = [
            torrent
            for torrent in torrents
            if float(torrent.get("progress", 0)) < 1 and route_tag in tags(torrent)
        ]
        if budget < 0:
            for torrent in affected:
                # Always issue stop: a just-routed torrent can still report a
                # stale stopped state while its earlier start request is racing.
                api_post("torrents/stop", {"hashes": torrent["hash"]})
                if ROUTE_OVERCOMMIT not in tags(torrent):
                    state = torrent.get("state", "")
                    overcommit_tags = [ROUTE_OVERCOMMIT]
                    if (
                        ROUTE_MANAGED_ACTIVE in tags(torrent)
                        or not (
                            state.startswith("stopped")
                            or state.startswith("paused")
                        )
                    ):
                        overcommit_tags.append(ROUTE_OVERCOMMIT_RESUME)
                    api_post(
                        "torrents/addTags",
                        {
                            "hashes": torrent["hash"],
                            "tags": ",".join(overcommit_tags),
                        },
                    )
            if affected:
                log_throttled(
                    f"overcommit:{route_tag}",
                    f"fail-closed: {route_tag} reservations exceed safe capacity "
                    f"by={-budget} affected={len(affected)}",
                    30,
                )
        else:
            for torrent in affected:
                if ROUTE_OVERCOMMIT in tags(torrent):
                    should_resume = ROUTE_OVERCOMMIT_RESUME in tags(torrent)
                    if should_resume:
                        api_post("torrents/start", {"hashes": torrent["hash"]})
                    api_post(
                        "torrents/removeTags",
                        {
                            "hashes": torrent["hash"],
                            "tags": (
                                f"{ROUTE_OVERCOMMIT},{ROUTE_OVERCOMMIT_RESUME}"
                            ),
                        },
                    )
                    action = "resumed" if should_resume else "kept-stopped"
                    log(
                        f"capacity recovered; {action} hash={torrent['hash'][:12]}"
                    )
    # Optional per-branch headroom sits above its hard reserve. This protects
    # branches that also hold application configuration or other unmanaged data.
    budgets = {
        branch["tag"]: max(
            0, raw_budgets[branch["tag"]] - branch["route_headroom_bytes"]
        )
        for branch in BRANCHES
    }

    for torrent in torrents:
        torrent_tags = tags(torrent)
        if (
            float(torrent.get("progress", 0)) >= 1
            and ROUTE_METADATA not in torrent_tags
        ):
            continue
        if torrent_tags & ROUTE_TAGS or ROUTE_ERROR in torrent_tags:
            continue

        selected_size = max(0, int(torrent.get("size", 0)))
        if selected_size == 0:
            # A size-zero item should have entered metadata discovery above.
            # Leave it stopped if qBittorrent returned an inconsistent snapshot.
            continue
        eligible = [
            branch
            for branch in BRANCHES
            if selected_size <= budgets[branch["tag"]]
        ]
        if not eligible:
            if ROUTE_WAITING not in torrent_tags:
                api_post(
                    "torrents/addTags",
                    {"hashes": torrent["hash"], "tags": ROUTE_WAITING},
                )
                log(
                    f"waiting for space hash={torrent['hash'][:12]} "
                    f"size={selected_size} budgets="
                    + ",".join(
                        f"{branch['name']}:{budgets[branch['tag']]}"
                        for branch in BRANCHES
                    )
                )
            continue

        if ROUTE_POLICY == "most_free":
            branch = max(eligible, key=lambda item: budgets[item["tag"]])
        else:
            branch = eligible[0]
        route_tag = branch["tag"]

        route_torrent(torrent, route_tag)
        if selected_size:
            budgets[route_tag] = max(0, budgets[route_tag] - selected_size)


def main() -> int:
    once = "--once" in sys.argv
    log(
        f"starting policy={ROUTE_POLICY} metadata={METADATA_BRANCH['name']} "
        f"branches="
        + ",".join(
            f"{branch['name']}:{branch['qbit_prefix']}:"
            f"reserve={branch['reserve_bytes']}:"
            f"headroom={branch['route_headroom_bytes']}"
            for branch in BRANCHES
        )
        + f" poll={POLL_SECONDS}s"
    )
    while True:
        try:
            process_once()
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace").strip()
            reason = f"qBittorrent HTTP {error.code}: {body or error.reason}"
            log(f"router HTTP error: {reason}")
            fail_closed_after_error(reason)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            log(f"qBittorrent unavailable: {error}")
        except Exception as error:
            reason = f"router error: {type(error).__name__}: {error}"
            log(reason)
            fail_closed_after_error(reason)
        if once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())

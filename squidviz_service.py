#!/usr/bin/env python3
"""SquidViz Python data service.

This is the Python backend for the pybackend branch of SquidViz. It serves the
JSON endpoints consumed by the static wallboard, runs read-only Ceph CLI
commands, and caches results so multiple open displays do not repeatedly query
the cluster for the same data.

Exposed endpoints:
  /healthz
  /json/config
  /json/osdtree
  /json/pgmap
  /json/osdmap
  /json/pgdump
  /json/iops

Run example:
  python3 squidviz_service.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


LOG = logging.getLogger("squidviz_service")


# =============================================================================
# SquidViz Service Settings
# =============================================================================
# Edit this section for normal deployments. The intended startup is:
#
#   python3 squidviz_service.py
#

# Network listener. Keep 127.0.0.1 when Apache reverse-proxies /json/.
# Use 0.0.0.0 only when another internal web server must reach this service.
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8081
CORS_ORIGIN = "*"
LOG_LEVEL = "INFO"
EXPOSE_ERROR_DETAILS = False

CEPH_COMMAND_TIMEOUT = 30.0

# Multi-cluster support. The UI cluster selector is populated from this map.
# Put each cluster's ceph binary, config, client name, and keyring directly in
# its entry so operators only edit one place per cluster.
DEFAULT_CLUSTER = "prod"
CLUSTERS = {
    "prod": {
        "label": "prod",
        "ceph_bin": "/usr/bin/ceph",
        "ceph_conf": "/etc/ceph/ceph.conf",
        "ceph_name": "client.squidviz",
        "ceph_keyring": "/etc/ceph/ceph.client.squidviz.keyring",
    },
}

# Cache lifetimes in seconds. These are shared by every wallboard using this
# service, so many monitors do not multiply Ceph command execution.
IOPS_TTL = 2.0
PGMAP_TTL = 10.0
OSDMAP_TTL = 10.0
OSDTREE_TTL = 600.0
PGDUMP_TTL = 10.0
PGDUMP_TOO_MANY_TTL = 30.0

# Logical view safety limit. If more unhealthy PGs exist than this value,
# SquidViz returns affected pools and state counts instead of every PG.
MAX_UNHEALTHY_PGS = 2500

# Default frontend limit for auto-expanding affected failure-domain branches.
# If a branch contains this many affected OSDs or more, the UI collapses it at
# a useful failure-domain boundary instead of expanding every OSD endpoint.
AFFECTED_TREE_ENDPOINT_LIMIT = 20

# Optional latency warning threshold. This is only used when a wallboard has
# the Latency checkbox enabled, which is the only time ceph osd perf is called.
LATENCY_WARNING_MS = 20.0

# How long a request waits when another thread is already refreshing the same
# expired cache entry and no stale value exists yet.
REFRESH_WAIT_SECONDS = 5.0


@dataclass
class CephConfig:
    cluster_id: str
    label: str
    ceph_bin: str
    ceph_conf: str | None = None
    ceph_name: str | None = None
    ceph_keyring: str | None = None


class CephCommandError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.status_code = status_code


def load_cluster_configs() -> dict[str, CephConfig]:
    configs: dict[str, CephConfig] = {}

    for cluster_id, entry in CLUSTERS.items():
        configs[cluster_id] = CephConfig(
            cluster_id=cluster_id,
            label=str(entry.get("label") or cluster_id),
            ceph_bin=str(entry.get("ceph_bin") or "/usr/bin/ceph"),
            ceph_conf=str(entry.get("ceph_conf") or "/etc/ceph/ceph.conf") or None,
            ceph_name=str(entry.get("ceph_name") or "client.squidviz") or None,
            ceph_keyring=str(entry.get("ceph_keyring") or "/etc/ceph/ceph.client.squidviz.keyring") or None,
        )

    if DEFAULT_CLUSTER not in configs:
        raise ValueError(f"DEFAULT_CLUSTER '{DEFAULT_CLUSTER}' is not present in CLUSTERS.")

    return configs


CLUSTER_CONFIGS = load_cluster_configs()

ENDPOINT_TTLS: dict[str, float] = {
    "osdtree": OSDTREE_TTL,
    "pgmap": PGMAP_TTL,
    "osdmap": OSDMAP_TTL,
    "pgdump": PGDUMP_TTL,
    "iops": IOPS_TTL,
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_REFRESH_EVENTS: dict[str, threading.Event] = {}


def json_response(payload: dict[str, Any], status_code: int = 200) -> tuple[int, bytes]:
    return status_code, json.dumps(payload).encode("utf-8")


def error_response(message: str, details: dict[str, Any] | None = None, status_code: int = 500) -> tuple[int, bytes]:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details and EXPOSE_ERROR_DETAILS:
        payload["details"] = details
    return json_response(payload, status_code)


def clean_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return value.strip()


def to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def annotate_cache_payload(payload: dict[str, Any], expires_at: float, stale: bool) -> dict[str, Any]:
    annotated = dict(payload)
    annotated["_cache"] = {
        "stale": stale,
        "expires_in": max(0.0, expires_at - time.monotonic()),
    }
    return annotated


def get_cached_payload(cache_key: str, allow_stale: bool = False) -> dict[str, Any] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is None:
            return None

        expires_at, payload = entry
        if now >= expires_at:
            if allow_stale:
                return annotate_cache_payload(payload, expires_at, True)
            return None

        return annotate_cache_payload(payload, expires_at, False)


def set_cached_payload(cache_key: str, payload: dict[str, Any], ttl: float) -> dict[str, Any]:
    expires_at = time.monotonic() + ttl
    with _CACHE_LOCK:
        _CACHE[cache_key] = (expires_at, payload)
    return annotate_cache_payload(payload, expires_at, False)


def cached_endpoint(name: str, key_suffix: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    cache_key = f"{name}:{key_suffix}"
    cached = get_cached_payload(cache_key)
    if cached is not None:
        return cached

    with _CACHE_LOCK:
        refresh_event = _REFRESH_EVENTS.get(cache_key)
        if refresh_event is None:
            refresh_event = threading.Event()
            _REFRESH_EVENTS[cache_key] = refresh_event
            should_refresh = True
        else:
            should_refresh = False

    if not should_refresh:
        stale = get_cached_payload(cache_key, allow_stale=True)
        if stale is not None:
            return stale

        refresh_event.wait(REFRESH_WAIT_SECONDS)
        refreshed = get_cached_payload(cache_key, allow_stale=True)
        if refreshed is not None:
            return refreshed

        raise CephCommandError(
            "Timed out waiting for cached data refresh.",
            {"endpoint": name, "cache_key": cache_key, "wait_seconds": REFRESH_WAIT_SECONDS},
            503,
        )

    try:
        payload = factory()
        ttl = ENDPOINT_TTLS[name]
        if name == "pgdump" and payload.get("too_many_problem_pgs"):
            ttl = max(ttl, PGDUMP_TOO_MANY_TTL)

        return set_cached_payload(cache_key, payload, ttl)
    except Exception:
        stale = get_cached_payload(cache_key, allow_stale=True)
        if stale is not None:
            stale["_cache"]["refresh_error"] = True
            LOG.exception("Refresh failed for %s; serving stale cache.", cache_key)
            return stale
        raise
    finally:
        with _CACHE_LOCK:
            event = _REFRESH_EVENTS.pop(cache_key, None)
            if event is not None:
                event.set()


def resolve_cluster_id(query: dict[str, list[str]]) -> str:
    requested = query.get("cluster", [DEFAULT_CLUSTER])[0]
    cluster_id = str(requested or DEFAULT_CLUSTER)

    if cluster_id not in CLUSTER_CONFIGS:
        raise CephCommandError(
            "Unknown cluster.",
            {"cluster": cluster_id, "available_clusters": sorted(CLUSTER_CONFIGS.keys())},
            400,
        )

    return cluster_id


def get_cluster_config(cluster_id: str) -> CephConfig:
    return CLUSTER_CONFIGS[cluster_id]


def ceph_command_prefix(config: CephConfig, arguments: list[str]) -> list[str]:
    command = [config.ceph_bin]

    if config.ceph_conf:
        command.extend(["-c", config.ceph_conf])

    if config.ceph_name:
        command.extend(["--name", config.ceph_name])

    if config.ceph_keyring:
        command.extend(["--keyring", config.ceph_keyring])

    command.extend(arguments)
    return command


def run_ceph_json(config: CephConfig, arguments: list[str], optional: bool = False) -> Any | None:
    command = ceph_command_prefix(config, arguments)

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=CEPH_COMMAND_TIMEOUT,
        )
    except OSError as exc:
        if optional:
            LOG.warning("Unable to start optional Ceph command %s: %s", command, exc)
            return None
        raise CephCommandError("Unable to start ceph command.", {"command": " ".join(command), "error": str(exc)})
    except subprocess.TimeoutExpired as exc:
        if optional:
            LOG.warning("Optional Ceph command timed out after %ss: %s", CEPH_COMMAND_TIMEOUT, " ".join(command))
            return None
        raise CephCommandError(
            "Ceph command timed out.",
            {"command": " ".join(command), "timeout_seconds": CEPH_COMMAND_TIMEOUT, "stderr": clean_subprocess_output(exc.stderr)},
            504,
        )

    if result.returncode != 0:
        if optional:
            LOG.warning("Optional Ceph command failed: %s", " ".join(command))
            return None
        raise CephCommandError(
            "Ceph command failed.",
            {
                "command": " ".join(command),
                "stderr": result.stderr.strip(),
                "exit_code": result.returncode,
            },
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if optional:
            LOG.warning("Optional Ceph command returned invalid JSON: %s", " ".join(command))
            return None
        raise CephCommandError(
            "Ceph returned invalid JSON.",
            {
                "command": " ".join(command),
                "json_error": str(exc),
            },
        )


def run_ceph_json_fallback(config: CephConfig, commands: list[list[str]]) -> Any:
    last_error: dict[str, Any] | None = None

    for arguments in commands:
        command = ceph_command_prefix(config, arguments)
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=CEPH_COMMAND_TIMEOUT)
        except OSError as exc:
            last_error = {"command": " ".join(command), "error": str(exc)}
            continue
        except subprocess.TimeoutExpired as exc:
            last_error = {
                "command": " ".join(command),
                "timeout_seconds": CEPH_COMMAND_TIMEOUT,
                "stderr": clean_subprocess_output(exc.stderr),
            }
            continue

        if result.returncode != 0:
            last_error = {
                "command": " ".join(command),
                "stderr": result.stderr.strip(),
                "exit_code": result.returncode,
            }
            continue

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            last_error = {"command": " ".join(command), "json_error": str(exc)}

    raise CephCommandError("All Ceph command fallbacks failed.", {"last_error": last_error})


def get_osdtree_payload(config: CephConfig) -> dict[str, Any]:
    input_json = run_ceph_json(config, ["osd", "tree", "--format=json"])
    nodes = input_json.get("nodes", [])
    if not nodes:
        raise CephCommandError("Ceph OSD tree did not contain any nodes.")

    node_map = {node["id"]: node for node in nodes if "id" in node}
    root_id = next((node["id"] for node in nodes if node.get("type") == "root"), None)
    if root_id is None and -1 in node_map:
        root_id = -1
    if root_id is None:
        raise CephCommandError("Unable to determine the root of the Ceph OSD tree.")

    def build_node(node_id: int) -> dict[str, Any] | None:
        node = node_map.get(node_id)
        if node is None:
            return None

        children: list[dict[str, Any]] = []
        for child_id in node.get("children", []):
            child = build_node(child_id)
            if child is not None:
                children.append(child)

        return {
            "id": node.get("id"),
            "name": node.get("name", str(node_id)),
            "type": node.get("type", "unknown"),
            "status": node.get("status", "unknown"),
            "children": children,
        }

    return build_node(root_id) or {}


def get_pgmap_payload(config: CephConfig) -> dict[str, Any]:
    status = run_ceph_json(config, ["-s", "-f", "json"])
    pgmap = status.get("pgmap", {})
    version = pgmap.get("version")
    pgs_by_state = pgmap.get("pgs_by_state", [])

    unhealthy_pgs = 0
    if isinstance(pgs_by_state, list):
        for entry in pgs_by_state:
            if entry.get("state_name") != "active+clean":
                unhealthy_pgs += int(entry.get("count", 0))

    if version is None:
        summary = {
            "num_pgs": pgmap.get("num_pgs", 0),
            "read_op_per_sec": pgmap.get("read_op_per_sec", 0),
            "write_op_per_sec": pgmap.get("write_op_per_sec", 0),
            "recovering_objects_per_sec": pgmap.get("recovering_objects_per_sec", 0),
            "pgs_by_state": pgs_by_state,
        }
        version = hashlib.sha1(json.dumps(summary, sort_keys=True).encode("utf-8")).hexdigest()

    return {
        "ok": True,
        "pgmap": str(version),
        "unhealthy_pgs": unhealthy_pgs,
        "has_unhealthy": unhealthy_pgs > 0,
    }


def get_osdmap_payload(config: CephConfig) -> dict[str, Any]:
    osd_dump = run_ceph_json(config, ["osd", "dump", "--format=json"])
    osdmap = osd_dump.get("osdmap", osd_dump)
    version = osdmap.get("epoch")
    osds = osd_dump.get("osds", osdmap.get("osds", []))
    osd_states: dict[str, dict[str, Any]] = {}

    if isinstance(osds, list):
        for osd in osds:
            osd_id = osd.get("osd", osd.get("id"))
            if osd_id is None:
                continue

            is_up = bool(osd.get("up", 0))
            is_in = bool(osd.get("in", 0))
            osd_states[str(osd_id)] = {
                "id": osd_id,
                "up": is_up,
                "in": is_in,
                "status": "up" if is_up and is_in else ("out" if is_up else "down"),
            }

    state_summary = {
        "version": version,
        "states": osd_states,
    }
    state_version = hashlib.sha1(json.dumps(state_summary, sort_keys=True).encode("utf-8")).hexdigest()
    topology_summary = {
        "num_osds": len(osd_states) if osd_states else osdmap.get("num_osds", 0),
        "osd_ids": sorted(osd_states.keys()),
    }
    topology_version = hashlib.sha1(json.dumps(topology_summary, sort_keys=True).encode("utf-8")).hexdigest()

    if version is None:
        summary = {
            "num_osds": osdmap.get("num_osds", 0),
            "num_up_osds": osdmap.get("num_up_osds", 0),
            "num_in_osds": osdmap.get("num_in_osds", 0),
            "num_remapped_pgs": osdmap.get("num_remapped_pgs", 0),
        }
        version = hashlib.sha1(json.dumps(summary, sort_keys=True).encode("utf-8")).hexdigest()

    return {
        "ok": True,
        "osdmap": str(version),
        "osd_state": state_version,
        "osd_topology": topology_version,
        "num_osds": len(osd_states) if osd_states else osdmap.get("num_osds", 0),
        "osds": osd_states,
    }


def get_pgdump_payload(config: CephConfig) -> dict[str, Any]:
    osd_dump = run_ceph_json(config, ["osd", "dump", "--format=json"])
    pools = {pool["pool"]: pool["pool_name"] for pool in osd_dump.get("pools", [])}

    pg_dump = run_ceph_json_fallback(
        config,
        [
            ["pg", "dump_json", "--dumpcontents=pgs"],
            ["pg", "dump", "--format=json"],
        ]
    )

    if isinstance(pg_dump.get("pg_stats"), list):
        pg_stats = pg_dump["pg_stats"]
        version = pg_dump.get("version", "unknown")
    else:
        pg_map = pg_dump.get("pg_map", {})
        pg_stats = pg_map.get("pg_stats", [])
        version = pg_map.get("version", "unknown")

    pg_tree: dict[str, Any] = {
        "name": "cluster",
        "version": version,
        "children": [],
        "summary": {
            "total_pgs": len(pg_stats),
            "problem_pgs": 0,
            "max_problem_pgs": MAX_UNHEALTHY_PGS,
            "state_counts": {},
        },
    }

    pool_indexes: dict[str, int] = {}
    affected_pools: dict[str, dict[str, Any]] = {}

    for pg in pg_stats:
        state = pg.get("state", "unknown")
        if state == "active+clean":
            continue

        pg_tree["summary"]["problem_pgs"] += 1
        state_counts = pg_tree["summary"]["state_counts"]
        state_counts[state] = state_counts.get(state, 0) + 1
        pgid = pg.get("pgid", "")
        pool_id = pgid.split(".", 1)[0]
        pool_key = pool_id if pool_id else "unknown"
        pool_name = pools.get(int(pool_key) if pool_key.isdigit() else pool_key, f"pool {pool_key}")

        if pool_key not in affected_pools:
            affected_pools[pool_key] = {
                "name": pool_key,
                "pool_name": pool_name,
                "problem_pgs": 0,
                "state_counts": {},
                "children": [],
            }

        affected_pool = affected_pools[pool_key]
        affected_pool["problem_pgs"] += 1
        affected_pool["value"] = affected_pool["problem_pgs"]
        affected_pool["state_counts"][state] = affected_pool["state_counts"].get(state, 0) + 1

    if pg_tree["summary"]["problem_pgs"] > MAX_UNHEALTHY_PGS:
        pg_tree["too_many_problem_pgs"] = True
        pg_tree["children"] = sorted(
            affected_pools.values(),
            key=lambda pool: pool["problem_pgs"],
            reverse=True,
        )
        pg_tree["message"] = (
            f"Too many unhealthy PGs to list safely "
            f"({pg_tree['summary']['problem_pgs']} found, limit {MAX_UNHEALTHY_PGS})."
        )
        return pg_tree

    for pg in pg_stats:
        state = pg.get("state", "unknown")
        if state == "active+clean":
            continue

        pgid = pg.get("pgid", "")
        parts = pgid.split(".", 1)
        pool_id = parts[0]
        group = parts[1] if len(parts) > 1 else pgid
        pool_name = pools.get(int(pool_id) if pool_id.isdigit() else pool_id, f"pool {pool_id}")

        pg_node = {
            "name": group,
            "pool_name": pool_name,
            "pgid": pgid,
            "objects": pg.get("stat_sum", {}).get("num_objects", 0),
            "state": state,
        }

        if pool_id not in pool_indexes:
            pool_indexes[pool_id] = len(pg_tree["children"])
            pg_tree["children"].append(
                {
                    "name": pool_id,
                    "pool_name": pool_name,
                    "children": [],
                }
            )

        pool_index = pool_indexes[pool_id]
        pg_tree["children"][pool_index]["children"].append(pg_node)

    return pg_tree


def get_iops_payload(config: CephConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    status = run_ceph_json(config, ["-s", "-f", "json"])
    pgmap = status.get("pgmap", {})

    include_latency = query.get("latency", ["0"])[0] == "1"
    iops_read = float(pgmap.get("read_op_per_sec", 0))
    iops_write = float(pgmap.get("write_op_per_sec", 0))
    bytes_read = float(pgmap.get("read_bytes_sec", 0))
    bytes_write = float(pgmap.get("write_bytes_sec", 0))
    commit_latency_ms = None
    apply_latency_ms = None
    high_latency_osds: list[dict[str, Any]] = []

    if include_latency:
        osd_perf = run_ceph_json(config, ["osd", "perf", "--format=json"], optional=True)
        if osd_perf is not None:
            if isinstance(osd_perf, list):
                perf_entries = osd_perf
            elif isinstance(osd_perf.get("osdstats", {}).get("osd_perf_infos"), list):
                perf_entries = osd_perf["osdstats"]["osd_perf_infos"]
            else:
                perf_entries = osd_perf.get("osd_perf_infos", [])

            commit_sum = 0.0
            apply_sum = 0.0
            count = 0

            for entry in perf_entries:
                perf_stat = entry.get("perf_stat", {})
                perf_stats = entry.get("perf_stats", {})
                commit_value = entry.get("commit_latency_ms")
                apply_value = entry.get("apply_latency_ms")
                osd_id = entry.get("id", entry.get("osd"))

                if commit_value is None:
                    commit_value = perf_stat.get("commit_latency_ms", perf_stats.get("commit_latency_ms"))
                if apply_value is None:
                    apply_value = perf_stat.get("apply_latency_ms", perf_stats.get("apply_latency_ms"))

                commit_value = to_float_or_none(commit_value)
                apply_value = to_float_or_none(apply_value)

                if commit_value is not None or apply_value is not None:
                    commit_sum += commit_value or 0.0
                    apply_sum += apply_value or 0.0
                    count += 1
                    max_latency = max(value for value in [commit_value, apply_value] if value is not None)

                    if max_latency > LATENCY_WARNING_MS:
                        high_latency_osds.append(
                            {
                                "id": osd_id,
                                "commit_latency_ms": commit_value,
                                "apply_latency_ms": apply_value,
                                "max_latency_ms": max_latency,
                            }
                        )

            if count > 0:
                commit_latency_ms = commit_sum / count
                apply_latency_ms = apply_sum / count

    return {
        "ok": True,
        "bytes_rd": bytes_read,
        "bytes_wr": bytes_write,
        "ops_read": iops_read,
        "ops_write": iops_write,
        "ops": iops_read + iops_write,
        "latency_enabled": include_latency,
        "commit_latency_ms": commit_latency_ms,
        "apply_latency_ms": apply_latency_ms,
        "latency_warning_threshold_ms": LATENCY_WARNING_MS,
        "high_latency_osds": high_latency_osds,
    }


def get_config_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "default_cluster": DEFAULT_CLUSTER,
        "clusters": [
            {"id": cluster.cluster_id, "label": cluster.label}
            for cluster in sorted(CLUSTER_CONFIGS.values(), key=lambda entry: entry.label.lower())
        ],
        "affected_tree_endpoint_limit": AFFECTED_TREE_ENDPOINT_LIMIT,
        "cache_ttls": {
            "iops": ENDPOINT_TTLS["iops"],
            "pgmap": ENDPOINT_TTLS["pgmap"],
            "osdmap": ENDPOINT_TTLS["osdmap"],
            "osdtree": ENDPOINT_TTLS["osdtree"],
            "pgdump": ENDPOINT_TTLS["pgdump"],
            "pgdump_too_many": PGDUMP_TOO_MANY_TTL,
        },
        "max_unhealthy_pgs": MAX_UNHEALTHY_PGS,
        "latency_warning_ms": LATENCY_WARNING_MS,
    }


class SquidVizHandler(BaseHTTPRequestHandler):
    server_version = "SquidVizService/0.1"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.server.cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        try:
            if route == "/healthz":
                status_code, payload = json_response({"ok": True, "service": "squidviz"})
            elif route == "/json/config":
                status_code, payload = json_response(get_config_payload())
            elif route == "/json/osdtree":
                cluster_id = resolve_cluster_id(query)
                config = get_cluster_config(cluster_id)
                force_refresh = query.get("refresh", ["0"])[0] == "1"
                status_code, payload = json_response(
                    set_cached_payload(f"osdtree:{cluster_id}:default", get_osdtree_payload(config), ENDPOINT_TTLS["osdtree"])
                    if force_refresh
                    else cached_endpoint("osdtree", f"{cluster_id}:default", lambda: get_osdtree_payload(config))
                )
            elif route == "/json/pgmap":
                cluster_id = resolve_cluster_id(query)
                config = get_cluster_config(cluster_id)
                status_code, payload = json_response(
                    cached_endpoint("pgmap", f"{cluster_id}:default", lambda: get_pgmap_payload(config))
                )
            elif route == "/json/osdmap":
                cluster_id = resolve_cluster_id(query)
                config = get_cluster_config(cluster_id)
                status_code, payload = json_response(
                    cached_endpoint("osdmap", f"{cluster_id}:default", lambda: get_osdmap_payload(config))
                )
            elif route == "/json/pgdump":
                cluster_id = resolve_cluster_id(query)
                config = get_cluster_config(cluster_id)
                status_code, payload = json_response(
                    cached_endpoint("pgdump", f"{cluster_id}:default", lambda: get_pgdump_payload(config))
                )
            elif route == "/json/iops":
                cluster_id = resolve_cluster_id(query)
                config = get_cluster_config(cluster_id)
                latency_flag = "1" if query.get("latency", ["0"])[0] == "1" else "0"
                status_code, payload = json_response(
                    cached_endpoint("iops", f"{cluster_id}:latency={latency_flag}", lambda: get_iops_payload(config, query))
                )
            else:
                status_code, payload = error_response("Not found.", status_code=404)
        except CephCommandError as exc:
            LOG.warning("Ceph command error serving %s: %s details=%s", self.path, exc.message, exc.details)
            status_code, payload = error_response(exc.message, exc.details, exc.status_code)
        except Exception as exc:  # pragma: no cover - defensive fallback
            LOG.exception("Unhandled error serving %s", self.path)
            status_code, payload = error_response("Unhandled server error.", {"error": str(exc)}, 500)

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


class SquidVizServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], cors_origin: str):
        super().__init__(server_address, handler_class)
        self.cors_origin = cors_origin


def validate_settings() -> None:
    if SERVICE_PORT < 1 or SERVICE_PORT > 65535:
        raise ValueError("SERVICE_PORT must be between 1 and 65535.")
    if CEPH_COMMAND_TIMEOUT < 1:
        raise ValueError("CEPH_COMMAND_TIMEOUT must be at least 1 second.")
    if MAX_UNHEALTHY_PGS < 1:
        raise ValueError("MAX_UNHEALTHY_PGS must be at least 1.")
    if AFFECTED_TREE_ENDPOINT_LIMIT < 1:
        raise ValueError("AFFECTED_TREE_ENDPOINT_LIMIT must be at least 1.")
    if REFRESH_WAIT_SECONDS < 0:
        raise ValueError("REFRESH_WAIT_SECONDS must be 0 or greater.")
    if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, or ERROR.")

    for endpoint, ttl in ENDPOINT_TTLS.items():
        if ttl < 0:
            raise ValueError(f"{endpoint} TTL must be 0 or greater.")

    if PGDUMP_TOO_MANY_TTL < 0:
        raise ValueError("PGDUMP_TOO_MANY_TTL must be 0 or greater.")

    if not CLUSTER_CONFIGS:
        raise ValueError("CLUSTERS must define at least one cluster.")

    for cluster_id, cluster in CLUSTER_CONFIGS.items():
        if not cluster.ceph_bin:
            raise ValueError(f"Cluster '{cluster_id}' is missing ceph_bin.")


def main() -> None:
    validate_settings()
    logging.basicConfig(level=getattr(logging, LOG_LEVEL), format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("Starting SquidViz service on %s:%s", SERVICE_HOST, SERVICE_PORT)
    for cluster_id in sorted(CLUSTER_CONFIGS.keys()):
        cluster = CLUSTER_CONFIGS[cluster_id]
        LOG.info(
            "Cluster %s (%s): bin=%s conf=%s name=%s keyring=%s",
            cluster.cluster_id,
            cluster.label,
            cluster.ceph_bin,
            cluster.ceph_conf or "-",
            cluster.ceph_name or "-",
            cluster.ceph_keyring or "-",
        )
    LOG.info("Using Ceph command timeout: %ss", CEPH_COMMAND_TIMEOUT)
    LOG.info("Using CORS origin: %s", CORS_ORIGIN)
    LOG.info("Using cache TTLs: %s", ENDPOINT_TTLS)
    LOG.info("Using PG dump too-many TTL: %s", PGDUMP_TOO_MANY_TTL)
    LOG.info("Using max unhealthy PG visualization limit: %s", MAX_UNHEALTHY_PGS)
    LOG.info("Using affected tree endpoint limit: %s", AFFECTED_TREE_ENDPOINT_LIMIT)
    LOG.info("Using OSD latency warning threshold: %sms", LATENCY_WARNING_MS)

    server = SquidVizServer((SERVICE_HOST, SERVICE_PORT), SquidVizHandler, CORS_ORIGIN)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

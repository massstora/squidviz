#!/usr/bin/env python3
"""SquidViz Python data service.

This is the Python backend for the pybackend branch of SquidViz. It serves the
JSON endpoints consumed by the static wallboard, runs read-only Ceph CLI
commands, and caches results so multiple open displays do not repeatedly query
the cluster for the same data.

Exposed endpoints:
  /healthz
  /json/osdtree
  /json/pgmap
  /json/osdmap
  /json/pgdump
  /json/iops

Local run example:
  python3 squidviz_service.py --host 127.0.0.1 --port 8081

Remote test example:
  python3 squidviz_service.py --host 0.0.0.0 --port 8081 --cors-origin "*"

Remote locked-down example:
  python3 squidviz_service.py --host 0.0.0.0 --port 8081 --cors-origin "http://squidviz.example.com"

Cache timing example:
  python3 squidviz_service.py --iops-ttl 2 --pgmap-ttl 8 --pgdump-ttl 10 --osdtree-ttl 10 --osdmap-ttl 10

Ceph client example:
  python3 squidviz_service.py --ceph-name client.squidviz --ceph-keyring /etc/ceph/ceph.client.squidviz.keyring
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


LOG = logging.getLogger("squidviz_service")


@dataclass
class CephConfig:
    ceph_bin: str = "/usr/bin/ceph"
    ceph_name: str | None = None
    ceph_keyring: str | None = None


class CephCommandError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.status_code = status_code


def load_config() -> CephConfig:
    return CephConfig(
        ceph_bin=os.getenv("SQUIDVIZ_CEPH_BIN", "/usr/bin/ceph"),
        ceph_name=os.getenv("SQUIDVIZ_CEPH_NAME") or None,
        ceph_keyring=os.getenv("SQUIDVIZ_CEPH_KEYRING") or None,
    )


CONFIG = load_config()

ENDPOINT_TTLS: dict[str, float] = {
    "osdtree": 10.0,
    "pgmap": 10.0,
    "osdmap": 10.0,
    "pgdump": 10.0,
    "iops": 2.0,
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def json_response(payload: dict[str, Any], status_code: int = 200) -> tuple[int, bytes]:
    return status_code, json.dumps(payload).encode("utf-8")


def error_response(message: str, details: dict[str, Any] | None = None, status_code: int = 500) -> tuple[int, bytes]:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details:
        payload["details"] = details
    return json_response(payload, status_code)


def get_cached_payload(cache_key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is None:
            return None

        expires_at, payload = entry
        if now >= expires_at:
            _CACHE.pop(cache_key, None)
            return None

        return payload


def set_cached_payload(cache_key: str, payload: dict[str, Any], ttl: float) -> dict[str, Any]:
    expires_at = time.monotonic() + ttl
    with _CACHE_LOCK:
        _CACHE[cache_key] = (expires_at, payload)
    return payload


def cached_endpoint(name: str, key_suffix: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    cache_key = f"{name}:{key_suffix}"
    cached = get_cached_payload(cache_key)
    if cached is not None:
        return cached

    payload = factory()
    return set_cached_payload(cache_key, payload, ENDPOINT_TTLS[name])


def ceph_command_prefix(arguments: list[str]) -> list[str]:
    command = [CONFIG.ceph_bin]

    if CONFIG.ceph_name:
        command.extend(["--name", CONFIG.ceph_name])

    if CONFIG.ceph_keyring:
        command.extend(["--keyring", CONFIG.ceph_keyring])

    command.extend(arguments)
    return command


def run_ceph_json(arguments: list[str], optional: bool = False) -> Any | None:
    command = ceph_command_prefix(arguments)

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        if optional:
            LOG.warning("Unable to start optional Ceph command %s: %s", command, exc)
            return None
        raise CephCommandError("Unable to start ceph command.", {"command": " ".join(command), "error": str(exc)})

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


def run_ceph_json_fallback(commands: list[list[str]]) -> Any:
    last_error: dict[str, Any] | None = None

    for arguments in commands:
        command = ceph_command_prefix(arguments)
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True)
        except OSError as exc:
            last_error = {"command": " ".join(command), "error": str(exc)}
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


def get_osdtree_payload() -> dict[str, Any]:
    input_json = run_ceph_json(["osd", "tree", "--format=json"])
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
            "name": node.get("name", str(node_id)),
            "type": node.get("type", "unknown"),
            "status": node.get("status", "unknown"),
            "children": children,
        }

    return build_node(root_id) or {}


def get_pgmap_payload() -> dict[str, Any]:
    status = run_ceph_json(["-s", "-f", "json"])
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


def get_osdmap_payload() -> dict[str, Any]:
    status = run_ceph_json(["-s", "-f", "json"])
    osdmap = status.get("osdmap", {})
    version = osdmap.get("epoch")

    if version is None:
        summary = {
            "num_osds": osdmap.get("num_osds", 0),
            "num_up_osds": osdmap.get("num_up_osds", 0),
            "num_in_osds": osdmap.get("num_in_osds", 0),
            "num_remapped_pgs": osdmap.get("num_remapped_pgs", 0),
        }
        version = hashlib.sha1(json.dumps(summary, sort_keys=True).encode("utf-8")).hexdigest()

    return {"ok": True, "osdmap": str(version)}


def get_pgdump_payload() -> dict[str, Any]:
    osd_dump = run_ceph_json(["osd", "dump", "--format=json"])
    pools = {pool["pool"]: pool["pool_name"] for pool in osd_dump.get("pools", [])}

    pg_dump = run_ceph_json_fallback(
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
        },
    }

    pool_indexes: dict[str, int] = {}

    for pg in pg_stats:
        state = pg.get("state", "unknown")
        if state == "active+clean":
            continue

        pg_tree["summary"]["problem_pgs"] += 1
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


def get_iops_payload(query: dict[str, list[str]]) -> dict[str, Any]:
    status = run_ceph_json(["-s", "-f", "json"])
    pgmap = status.get("pgmap", {})

    include_latency = query.get("latency", ["0"])[0] == "1"
    iops_read = float(pgmap.get("read_op_per_sec", 0))
    iops_write = float(pgmap.get("write_op_per_sec", 0))
    bytes_read = float(pgmap.get("read_bytes_sec", 0))
    bytes_write = float(pgmap.get("write_bytes_sec", 0))
    commit_latency_ms = None
    apply_latency_ms = None

    if include_latency:
        osd_perf = run_ceph_json(["osd", "perf", "--format=json"], optional=True)
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

                if commit_value is None:
                    commit_value = perf_stat.get("commit_latency_ms", perf_stats.get("commit_latency_ms"))
                if apply_value is None:
                    apply_value = perf_stat.get("apply_latency_ms", perf_stats.get("apply_latency_ms"))

                if commit_value is not None or apply_value is not None:
                    commit_sum += float(commit_value or 0.0)
                    apply_sum += float(apply_value or 0.0)
                    count += 1

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
            elif route == "/json/osdtree":
                status_code, payload = json_response(
                    cached_endpoint("osdtree", "default", get_osdtree_payload)
                )
            elif route == "/json/pgmap":
                status_code, payload = json_response(
                    cached_endpoint("pgmap", "default", get_pgmap_payload)
                )
            elif route == "/json/osdmap":
                status_code, payload = json_response(
                    cached_endpoint("osdmap", "default", get_osdmap_payload)
                )
            elif route == "/json/pgdump":
                status_code, payload = json_response(
                    cached_endpoint("pgdump", "default", get_pgdump_payload)
                )
            elif route == "/json/iops":
                latency_flag = query.get("latency", ["0"])[0]
                status_code, payload = json_response(
                    cached_endpoint("iops", f"latency={latency_flag}", lambda: get_iops_payload(query))
                )
            else:
                status_code, payload = error_response("Not found.", status_code=404)
        except CephCommandError as exc:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SquidViz backend microservice.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Use 0.0.0.0 to listen remotely. Default: 127.0.0.1",
    )
    parser.add_argument("--port", type=int, default=8081, help="Bind port. Default: 8081")
    parser.add_argument("--ceph-bin", default=None, help="Path to the ceph binary. Overrides config/env.")
    parser.add_argument("--ceph-name", default=None, help="Ceph client name, for example client.squidviz. Overrides config/env.")
    parser.add_argument("--ceph-keyring", default=None, help="Path to the Ceph keyring. Overrides config/env.")
    parser.add_argument(
        "--cors-origin",
        default="*",
        help='Allowed browser origin for cross-host requests. Default: "*"',
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity. Default: INFO",
    )
    parser.add_argument("--iops-ttl", type=float, default=ENDPOINT_TTLS["iops"], help="IOPS cache lifetime in seconds. Default: 2")
    parser.add_argument("--pgmap-ttl", type=float, default=ENDPOINT_TTLS["pgmap"], help="PG map check cache lifetime in seconds. Default: 10")
    parser.add_argument("--pgdump-ttl", type=float, default=ENDPOINT_TTLS["pgdump"], help="Full PG dump cache lifetime in seconds. Default: 10")
    parser.add_argument("--osdtree-ttl", type=float, default=ENDPOINT_TTLS["osdtree"], help="OSD tree cache lifetime in seconds. Default: 10")
    parser.add_argument("--osdmap-ttl", type=float, default=ENDPOINT_TTLS["osdmap"], help="OSD map check cache lifetime in seconds. Default: 10")
    return parser.parse_args()


def apply_cache_ttls(args: argparse.Namespace) -> None:
    requested_ttls = {
        "iops": args.iops_ttl,
        "pgmap": args.pgmap_ttl,
        "pgdump": args.pgdump_ttl,
        "osdtree": args.osdtree_ttl,
        "osdmap": args.osdmap_ttl,
    }

    for endpoint, ttl in requested_ttls.items():
        if ttl < 0:
            raise ValueError(f"{endpoint} TTL must be 0 or greater.")
        ENDPOINT_TTLS[endpoint] = ttl


def apply_ceph_overrides(args: argparse.Namespace) -> None:
    if args.ceph_bin:
        CONFIG.ceph_bin = args.ceph_bin
    if args.ceph_name:
        CONFIG.ceph_name = args.ceph_name
    if args.ceph_keyring:
        CONFIG.ceph_keyring = args.ceph_keyring


def main() -> None:
    args = parse_args()
    apply_ceph_overrides(args)
    apply_cache_ttls(args)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("Starting SquidViz service on %s:%s", args.host, args.port)
    LOG.info("Using Ceph binary: %s", CONFIG.ceph_bin)
    if CONFIG.ceph_name:
        LOG.info("Using Ceph client: %s", CONFIG.ceph_name)
    if CONFIG.ceph_keyring:
        LOG.info("Using Ceph keyring: %s", CONFIG.ceph_keyring)
    LOG.info("Using CORS origin: %s", args.cors_origin)
    LOG.info("Using cache TTLs: %s", ENDPOINT_TTLS)

    server = SquidVizServer((args.host, args.port), SquidVizHandler, args.cors_origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

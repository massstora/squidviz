#!/usr/bin/env python3
"""SquidViz Prometheus-backed data service.

This service is a sibling to squidviz_service.py. It keeps the same HTTP
endpoint shape expected by the current SquidViz HTML, but pulls data from the
Prometheus HTTP API instead of the ceph CLI.

The HTML can stay unchanged as long as this service returns the same payloads:
  /healthz
  /json/config
  /json/osdtree
  /json/pgmap
  /json/osdmap
  /json/pgdump
  /json/iops

Important:
This file is intentionally a structured scaffold until we know the actual Ceph
metric names and labels in your Prometheus deployment. The top-of-file query
templates are where those mappings belong.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


LOG = logging.getLogger("squidviz_prometheus_service")


# =============================================================================
# SquidViz Prometheus Service Settings
# =============================================================================
# Edit this section for normal deployments. The intended startup is:
#
#   python3 squidviz_prometheus_service.py
#

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8082
CORS_ORIGIN = "*"
LOG_LEVEL = "INFO"
EXPOSE_ERROR_DETAILS = False

PROMETHEUS_TIMEOUT = 15.0
PROMETHEUS_VERIFY_TLS = True

DEFAULT_CLUSTER = "prod"
CLUSTERS = {
    "prod": {
        "label": "prod",
        "prometheus_url": "http://127.0.0.1:9090",
        "prometheus_cluster_label": "prod",
        "headers": {},
    },
}

# Cache lifetimes in seconds. These mirror the CLI-backed service defaults so
# the wallboard behavior stays familiar.
IOPS_TTL = 2.0
PGMAP_TTL = 10.0
OSDMAP_TTL = 10.0
OSDTREE_TTL = 600.0
PGDUMP_TTL = 10.0
PGDUMP_TOO_MANY_TTL = 30.0

MAX_UNHEALTHY_PGS = 2500
AFFECTED_TREE_ENDPOINT_LIMIT = 20
LATENCY_WARNING_MS = 20.0
REFRESH_WAIT_SECONDS = 5.0

# Query templates to be mapped to your real Prometheus metrics or recording
# rules. Templates may use:
#   {cluster_label}
# which resolves to the cluster-specific prometheus_cluster_label above.
#
# Until these are populated, the matching endpoints will return a clear 501.
PROMETHEUS_QUERIES = {
    "pgmap_version": "",
    "pgmap_unhealthy_pgs": "",
    "osdmap_version": "",
    "osdmap_states": "",
    "osdtree_payload": "",
    "pgdump_payload": "",
    "iops_read": "",
    "iops_write": "",
    "latency_commit_ms": "",
    "latency_apply_ms": "",
    "latency_high_osds": "",
}


@dataclass
class PrometheusClusterConfig:
    cluster_id: str
    label: str
    prometheus_url: str
    prometheus_cluster_label: str
    headers: dict[str, str]


class ServiceError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.status_code = status_code


def load_cluster_configs() -> dict[str, PrometheusClusterConfig]:
    configs: dict[str, PrometheusClusterConfig] = {}

    for cluster_id, entry in CLUSTERS.items():
        configs[cluster_id] = PrometheusClusterConfig(
            cluster_id=cluster_id,
            label=str(entry.get("label") or cluster_id),
            prometheus_url=str(entry.get("prometheus_url") or "").rstrip("/"),
            prometheus_cluster_label=str(entry.get("prometheus_cluster_label") or cluster_id),
            headers=dict(entry.get("headers") or {}),
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
        raise ServiceError(
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
        raise ServiceError(
            "Unknown cluster.",
            {"cluster": cluster_id, "available_clusters": sorted(CLUSTER_CONFIGS.keys())},
            400,
        )
    return cluster_id


def get_cluster_config(cluster_id: str) -> PrometheusClusterConfig:
    return CLUSTER_CONFIGS[cluster_id]


def require_query(name: str) -> str:
    query = str(PROMETHEUS_QUERIES.get(name) or "").strip()
    if not query:
        raise ServiceError(
            "Prometheus query is not configured for this endpoint yet.",
            {"query_name": name},
            501,
        )
    return query


def render_query(template: str, cluster: PrometheusClusterConfig) -> str:
    return template.format(cluster_label=cluster.prometheus_cluster_label)


def fetch_prometheus_json(cluster: PrometheusClusterConfig, api_path: str, params: dict[str, Any]) -> dict[str, Any]:
    base = cluster.prometheus_url.rstrip("/") + "/"
    url = urljoin(base, api_path.lstrip("/"))
    query_string = urlencode({key: value for key, value in params.items() if value is not None})
    request = Request(url + ("?" + query_string if query_string else ""))
    request.add_header("Accept", "application/json")
    for header, value in cluster.headers.items():
        request.add_header(header, value)

    context = None
    if not PROMETHEUS_VERIFY_TLS and urlparse(url).scheme == "https":
        context = ssl._create_unverified_context()

    try:
        with urlopen(request, timeout=PROMETHEUS_TIMEOUT, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ServiceError("Prometheus returned an HTTP error.", {"status": exc.code, "url": url}, 502) from exc
    except URLError as exc:
        raise ServiceError("Unable to reach Prometheus.", {"reason": str(exc.reason), "url": url}, 502) from exc
    except TimeoutError as exc:
        raise ServiceError("Prometheus query timed out.", {"url": url, "timeout_seconds": PROMETHEUS_TIMEOUT}, 504) from exc
    except json.JSONDecodeError as exc:
        raise ServiceError("Prometheus returned invalid JSON.", {"url": url, "json_error": str(exc)}, 502) from exc

    if payload.get("status") != "success":
        raise ServiceError("Prometheus query failed.", {"url": url, "response": payload}, 502)

    return payload


def prometheus_query(cluster: PrometheusClusterConfig, query_name: str, ts: float | None = None) -> dict[str, Any]:
    expression = render_query(require_query(query_name), cluster)
    return fetch_prometheus_json(cluster, "/api/v1/query", {"query": expression, "time": ts})


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
        "source": "prometheus",
    }


def not_yet_mapped_payload(endpoint_name: str) -> dict[str, Any]:
    raise ServiceError(
        "This Prometheus-backed endpoint is not mapped yet.",
        {"endpoint": endpoint_name, "hint": "Populate PROMETHEUS_QUERIES in squidviz_prometheus_service.py."},
        501,
    )


def get_pgmap_payload(cluster: PrometheusClusterConfig) -> dict[str, Any]:
    not_yet_mapped_payload("pgmap")


def get_osdmap_payload(cluster: PrometheusClusterConfig) -> dict[str, Any]:
    not_yet_mapped_payload("osdmap")


def get_osdtree_payload(cluster: PrometheusClusterConfig) -> dict[str, Any]:
    not_yet_mapped_payload("osdtree")


def get_pgdump_payload(cluster: PrometheusClusterConfig) -> dict[str, Any]:
    not_yet_mapped_payload("pgdump")


def get_iops_payload(cluster: PrometheusClusterConfig, query: dict[str, list[str]]) -> dict[str, Any]:
    not_yet_mapped_payload("iops")


class SquidVizPrometheusHandler(BaseHTTPRequestHandler):
    server_version = "SquidVizPrometheusService/0.1"

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
                status_code, payload = json_response({"ok": True, "service": "squidviz-prometheus"})
            elif route == "/json/config":
                status_code, payload = json_response(get_config_payload())
            elif route == "/json/osdtree":
                cluster_id = resolve_cluster_id(query)
                cluster = get_cluster_config(cluster_id)
                force_refresh = query.get("refresh", ["0"])[0] == "1"
                status_code, payload = json_response(
                    set_cached_payload(f"osdtree:{cluster_id}:default", get_osdtree_payload(cluster), ENDPOINT_TTLS["osdtree"])
                    if force_refresh
                    else cached_endpoint("osdtree", f"{cluster_id}:default", lambda: get_osdtree_payload(cluster))
                )
            elif route == "/json/pgmap":
                cluster_id = resolve_cluster_id(query)
                cluster = get_cluster_config(cluster_id)
                status_code, payload = json_response(
                    cached_endpoint("pgmap", f"{cluster_id}:default", lambda: get_pgmap_payload(cluster))
                )
            elif route == "/json/osdmap":
                cluster_id = resolve_cluster_id(query)
                cluster = get_cluster_config(cluster_id)
                status_code, payload = json_response(
                    cached_endpoint("osdmap", f"{cluster_id}:default", lambda: get_osdmap_payload(cluster))
                )
            elif route == "/json/pgdump":
                cluster_id = resolve_cluster_id(query)
                cluster = get_cluster_config(cluster_id)
                status_code, payload = json_response(
                    cached_endpoint("pgdump", f"{cluster_id}:default", lambda: get_pgdump_payload(cluster))
                )
            elif route == "/json/iops":
                cluster_id = resolve_cluster_id(query)
                cluster = get_cluster_config(cluster_id)
                latency_flag = "1" if query.get("latency", ["0"])[0] == "1" else "0"
                status_code, payload = json_response(
                    cached_endpoint("iops", f"{cluster_id}:latency={latency_flag}", lambda: get_iops_payload(cluster, query))
                )
            else:
                status_code, payload = error_response("Not found.", status_code=404)
        except ServiceError as exc:
            LOG.warning("Prometheus service error serving %s: %s details=%s", self.path, exc.message, exc.details)
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


class SquidVizPrometheusServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], cors_origin: str):
        super().__init__(server_address, handler_class)
        self.cors_origin = cors_origin


def validate_settings() -> None:
    if SERVICE_PORT < 1 or SERVICE_PORT > 65535:
        raise ValueError("SERVICE_PORT must be between 1 and 65535.")
    if PROMETHEUS_TIMEOUT < 1:
        raise ValueError("PROMETHEUS_TIMEOUT must be at least 1 second.")
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
        if not cluster.prometheus_url:
            raise ValueError(f"Cluster '{cluster_id}' is missing prometheus_url.")


def main() -> None:
    validate_settings()
    logging.basicConfig(level=getattr(logging, LOG_LEVEL), format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("Starting SquidViz Prometheus service on %s:%s", SERVICE_HOST, SERVICE_PORT)
    for cluster_id in sorted(CLUSTER_CONFIGS.keys()):
        cluster = CLUSTER_CONFIGS[cluster_id]
        LOG.info(
            "Cluster %s (%s): prometheus=%s cluster_label=%s",
            cluster.cluster_id,
            cluster.label,
            cluster.prometheus_url,
            cluster.prometheus_cluster_label,
        )
    LOG.info("Using Prometheus timeout: %ss", PROMETHEUS_TIMEOUT)
    LOG.info("Using CORS origin: %s", CORS_ORIGIN)
    LOG.info("Using cache TTLs: %s", ENDPOINT_TTLS)
    LOG.info("Using PG dump too-many TTL: %s", PGDUMP_TOO_MANY_TTL)
    LOG.info("Using max unhealthy PG visualization limit: %s", MAX_UNHEALTHY_PGS)
    LOG.info("Using affected tree endpoint limit: %s", AFFECTED_TREE_ENDPOINT_LIMIT)
    LOG.info("Using OSD latency warning threshold: %sms", LATENCY_WARNING_MS)

    server = SquidVizPrometheusServer((SERVICE_HOST, SERVICE_PORT), SquidVizPrometheusHandler, CORS_ORIGIN)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

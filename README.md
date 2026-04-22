# SquidViz

SquidViz is a lightweight Ceph dashboard built from static HTML, D3.js, jQuery, and a small Python data service that shells out to the `ceph` CLI. It is meant for quick operator visibility rather than full cluster administration.

The current version keeps the original spirit of the project, but uses a Python backend for modern Ceph output, improves the page layout for current browsers, and adds cleaner runtime error handling when the data service cannot talk to the cluster.

Original software was created by Ross Turk:
https://github.com/rossturk/squidviz

## What It Shows

- Physical cluster layout from `ceph osd tree --format=json`
- PG trouble grouped by pool using `ceph pg dump_json --dumpcontents=pgs` with a fallback to `ceph pg dump --format=json`
- Rolling client IOPS from `ceph -s -f json`

The logical view focuses on unhealthy or otherwise interesting PG states. If the cluster is clean, the view will tell you that there are no unhealthy PGs to display.

## Setup Requirements

- A web server for the static SquidViz files
- Python 3 on the machine running `squidviz_service.py`
- The `ceph` CLI installed on the machine running `squidviz_service.py`
- A working `ceph.conf` on the data-service host
- A client keyring or other auth configuration that allows the Python service user to run read-only Ceph commands

The static website and the Python data service may run on the same machine, or the Python service may run on a separate internal host that has Ceph access.

At minimum, the user running `squidviz_service.py` needs to be able to run these commands successfully:

```bash
ceph -s -f json
ceph osd tree --format=json
ceph osd dump --format=json
ceph pg dump_json --dumpcontents=pgs
```

If `pg dump_json` is not available on your cluster, SquidViz falls back to:

```bash
ceph pg dump --format=json
```

## Step-By-Step Install

### 1. Install Packages

Debian or Ubuntu example:

apt-get update
apt-get install -y apache2 python3 ceph-common

If the web server and Python data service run on different machines, the web server only needs to serve static files. The machine running `squidviz_service.py` needs Python 3, `ceph-common`, `ceph.conf`, and the read-only keyring.

### 2. Copy SquidViz Into Your Web Root

Example Apache document root:

cp -a squidviz /var/www/html/

### 3. Make Sure Ceph Config Exists

SquidViz expects the data-service host to be able to talk to the cluster using the normal Ceph CLI.

At minimum, verify this file exists:

/etc/ceph/ceph.conf

### 4. Create A Read-Only Ceph Client

Do this on a host that already has Ceph admin access:

ceph auth get-or-create client.squidviz mon 'allow r' mgr 'allow r' osd 'allow r' \
  -o /etc/ceph/ceph.client.squidviz.keyring

This creates the recommended client name and keyring filename used by SquidViz:

- client name: `client.squidviz`
- keyring path: `/etc/ceph/ceph.client.squidviz.keyring`

Copy that keyring to the host running `squidviz_service.py` at the same path:

/etc/ceph/ceph.client.squidviz.keyring

### 5. Set Permissions

The user running `squidviz_service.py` must be able to read the keyring.

If you run the service as `www-data`, the permissions usually look like:

chgrp www-data /etc/ceph/ceph.client.squidviz.keyring
chmod 640 /etc/ceph/ceph.client.squidviz.keyring
chmod 644 /etc/ceph/ceph.conf

If you run the service as another user, adjust the group accordingly.

### 6. Configure The Python Data Service

Edit the clearly marked `SquidViz Service Settings` block near the top of [squidviz_service.py]. This is the normal configuration method.

The defaults match the recommended read-only Ceph client:

```python
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8081
CORS_ORIGIN = "*"
LOG_LEVEL = "INFO"
EXPOSE_ERROR_DETAILS = False

CEPH_BIN = "/usr/bin/ceph"
CEPH_NAME = "client.squidviz"
CEPH_KEYRING = "/etc/ceph/ceph.client.squidviz.keyring"
CEPH_COMMAND_TIMEOUT = 30.0

IOPS_TTL = 2.0
PGMAP_TTL = 10.0
OSDMAP_TTL = 10.0
OSDTREE_TTL = 300.0
PGDUMP_TTL = 10.0
PGDUMP_TOO_MANY_TTL = 30.0

MAX_UNHEALTHY_PGS = 2500
LATENCY_WARNING_MS = 20.0
REFRESH_WAIT_SECONDS = 5.0
```

If the Python service runs on the same machine as the web server, keep `SERVICE_HOST` set to `127.0.0.1`.

If the Python service runs on a separate internal Ceph host, set:

```python
SERVICE_HOST = "0.0.0.0"
```

and allow only the SquidViz web server through your firewall.

After the settings are correct, start the service. No command-line options are required for a normal deployment:

```bash
python3 squidviz_service.py
```

The backend caches command output so multiple wallboards do not all run the same Ceph commands at once. Cache timing is controlled by the `*_TTL` settings in [squidviz_service.py].

The CRUSH failure-domain topology uses a longer `OSDTREE_TTL` because bucket/host/OSD placement changes are usually rare. OSD up/down/in/out state is checked through the lighter `/json/osdmap` path, so failure-domain colors can update without repeatedly rebuilding the full tree.

When the Latency checkbox is enabled, SquidViz also checks OSD latency against `LATENCY_WARNING_MS`. If the same OSD stays above that threshold for three IOPS polls, the IOPS panel shows a small red warning.

If more than `MAX_UNHEALTHY_PGS` PGs are unhealthy, SquidViz returns a summary and the affected pools instead of trying to send and render every unhealthy PG in the logical view. The Python service also keeps that capped `/json/pgdump` response cached for at least `PGDUMP_TOO_MANY_TTL` seconds. This keeps very large unhealthy clusters from overwhelming the browser or repeatedly running full PG dumps while still showing which pools are involved.

The Python service coalesces cache refreshes per endpoint. If a cached response expires and many wallboards request it at the same time, only one request runs the real Ceph command while the others receive stale cached data or briefly wait for the refresh to complete.

### 7. Reverse Proxy `/json/` To The Data Service

The recommended layout is for browsers to talk only to the SquidViz web server. The web server should reverse-proxy `/json/` to `squidviz_service.py`.

With this layout, keep [squidviz_config.js] blank so the browser uses same-origin `/json/...` URLs:

```js
window.SQUIDVIZ_BACKEND_URL = "";
```

For Apache on Debian or Ubuntu, put the proxy lines inside the site config that serves SquidViz. This is commonly one of:

```text
/etc/apache2/sites-available/000-default.conf
/etc/apache2/sites-available/squidviz.conf
```

Enable Apache proxy support:

```bash
a2enmod proxy proxy_http
systemctl reload apache2
```

Apache example when the Python service is on the same machine:

```apache
<VirtualHost *:80>
    DocumentRoot /var/www/html/squidviz

    ProxyPass        /json/ http://127.0.0.1:8081/json/
    ProxyPassReverse /json/ http://127.0.0.1:8081/json/
</VirtualHost>
```

Apache proxy lines when the Python service is on the same machine:

```apache
ProxyPass        /json/ http://127.0.0.1:8081/json/
ProxyPassReverse /json/ http://127.0.0.1:8081/json/
```

Apache proxy lines when the Python service is on another internal host:

```apache
ProxyPass        /json/ http://cephmachine:8081/json/
ProxyPassReverse /json/ http://cephmachine:8081/json/
```

If you intentionally want browsers to call the Python service directly, set [squidviz_config.js] to a browser-reachable URL:

```js
window.SQUIDVIZ_BACKEND_URL = "http://cephmachine:8081";
```

### 8. Verify The Setup

Before opening the dashboard, test Ceph access as the same user that runs `squidviz_service.py`.

```bash
ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring -s -f json
ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring osd tree --format=json
ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring osd dump --format=json
ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring pg dump_json --dumpcontents=pgs
```

If `pg dump_json` is unavailable on your Ceph version, test this instead:

```bash
ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring pg dump --format=json
```

Then test the Python service directly from the data-service host:

```bash
curl http://127.0.0.1:8081/healthz
curl http://127.0.0.1:8081/json/pgmap
```

Finally, test through the web server reverse proxy. This is the same path the browser will use:

```bash
curl http://your-squidviz-webserver/json/pgmap
curl http://your-squidviz-webserver/json/osdtree
```

## Suggested Server Setup

- Keep SquidViz behind your VPN or internal admin network.
- Do not expose the dashboard publicly without authentication. I cannot and will not guarantee security.
- Prefer a dedicated read-only Ceph client identity for the Python data service.

The dashboard can only be as healthy as the CLI access behind `squidviz_service.py`.

## Troubleshooting

- If the widgets say they cannot load data, try the matching Python endpoint directly, such as `/json/pgmap` or `/json/osdtree`.
- If the browser cannot load data, check the web server reverse proxy for `/json/` first.
- If you are using direct browser-to-service access, check [squidviz_config.js], firewall rules, and `--cors-origin`.
- If the Python endpoint returns JSON errors mentioning Ceph, check the `squidviz_service.py` logs and test the same command from the shell as the service user.
- If the service user gets `RADOS permission denied`, the keyring path, Ceph user, or file permissions are wrong.
- If the service user reports `no keyring found`, make sure `/etc/ceph/ceph.client.squidviz.keyring` exists and matches the service startup options.
- If the logical view stays empty, the cluster may simply have no unhealthy PGs right now.

## Notes

- This project intentionally remains simple: no build step, no package manager, no frontend framework.
- The single-page wrappers `logical-single.html` and `iops-single.html` are convenience embeds for wallboard-style displays.

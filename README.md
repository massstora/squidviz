# SquidViz

SquidViz is a lightweight Ceph dashboard built from static HTML, D3.js, jQuery, and a few PHP endpoints that shell out to the local `ceph` CLI. It is meant for quick operator visibility rather than full cluster administration.

The current version keeps the original spirit of the project, but hardens the PHP side for modern Ceph output, improves the page layout for current browsers, and adds cleaner runtime error handling when the host cannot talk to the cluster.

Original software was created by Ross Turk:
https://github.com/rossturk/squidviz

## What It Shows

- Physical cluster layout from `ceph osd tree --format=json`
- PG trouble grouped by pool using `ceph pg dump_json --dumpcontents=pgs` with a fallback to `ceph pg dump --format=json`
- Rolling client IOPS from `ceph -s -f json`

The logical view focuses on unhealthy or otherwise interesting PG states. If the cluster is clean, the view will tell you that there are no unhealthy PGs to display.

## Setup Requirements

- A web server that can execute PHP
- PHP with `proc_open` enabled
- The `ceph` CLI installed on the same host as the web server
- A working `ceph.conf`
- A client keyring or other auth configuration that allows the web server user to run read-only Ceph commands

At minimum, the server user needs to be able to run these commands successfully:

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
apt-get install -y apache2 libapache2-mod-php php ceph-common

If you use nginx instead of Apache, install nginx plus PHP-FPM and make sure PHP files in `json/` are executed by PHP rather than served as plain text.

### 2. Copy SquidViz Into Your Web Root

Example Apache document root:

cp -a squidviz /var/www/html/

### 3. Make Sure Ceph Config Exists

SquidViz expects the local server to be able to talk to the cluster using the normal Ceph CLI.

At minimum, verify this file exists:

/etc/ceph/ceph.conf

### 4. Create A Read-Only Ceph Client

Do this on a host that already has Ceph admin access:

ceph auth get-or-create client.squidviz mon 'allow r' mgr 'allow r' osd 'allow r' \
  -o /etc/ceph/ceph.client.squidviz.keyring

This creates the recommended client name and keyring filename used by SquidViz:

- client name: `client.squidviz`
- keyring path: `/etc/ceph/ceph.client.squidviz.keyring`

Copy that keyring to the SquidViz web server at the same path:

/etc/ceph/ceph.client.squidviz.keyring

### 5. Set Permissions

The web server user must be able to read the keyring.

Apache on Debian or Ubuntu usually runs as `www-data`:

chgrp www-data /etc/ceph/ceph.client.squidviz.keyring
chmod 640 /etc/ceph/ceph.client.squidviz.keyring
chmod 644 /etc/ceph/ceph.conf

If your web server runs as another user, adjust the group accordingly.

### 6. Configure SquidViz To Use That Client

Edit [json/config.php] so it points to the Ceph binary and the read-only client you actually created.

If you used the recommended defaults above, this file can stay as-is.

If you used a different Ceph client name or a different keyring path, you must change `json/config.php` to match.

return array(
    "ceph_bin" => "/usr/bin/ceph",
    "ceph_name" => "client.squidviz",
    "ceph_keyring" => "/etc/ceph/ceph.client.squidviz.keyring",
);

For example, if you created `client.cephwatch` instead, your config would look like:

return array(
    "ceph_bin" => "/usr/bin/ceph",
    "ceph_name" => "client.cephwatch",
    "ceph_keyring" => "/etc/ceph/ceph.client.cephwatch.keyring",
);

### 7. Verify Ceph Access As The Web User

Before opening the dashboard, test the same commands the PHP endpoints use.

Example for Apache on Debian or Ubuntu:

sudo -u www-data ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring -s -f json
sudo -u www-data ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring osd tree --format=json
sudo -u www-data ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring osd dump --format=json
sudo -u www-data ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring pg dump_json --dumpcontents=pgs

If `pg dump_json` is unavailable on your Ceph version, test this instead:

sudo -u www-data ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring pg dump --format=json

## Suggested Server Setup

- Keep SquidViz behind your VPN or internal admin network.
- Do not expose the dashboard publicly without authentication. I cannot and will not guarentee security.
- Prefer a dedicated read-only Ceph client identity for the web server.

For Apache, a quick sanity check often looks like:
sudo -u www-data ceph --name client.squidviz --keyring /etc/ceph/ceph.client.squidviz.keyring -s -f json

If that fails, fix Ceph config or auth first. The dashboard can only be as healthy as the CLI access behind it.

## Troubleshooting

- If the widgets say they cannot load data, try the matching PHP endpoint directly in the browser under `json/`.
- If the browser downloads PHP instead of executing it, PHP is not wired into the web server.
- If the PHP endpoints return JSON errors mentioning Ceph, test the same command from the shell as the web server user.
- If the web server user gets `RADOS permission denied`, the keyring path, Ceph user, or file permissions are wrong.
- If the web server user reports `no keyring found`, make sure `/etc/ceph/ceph.client.squidviz.keyring` exists and matches [json/config.php].
- If PHP cannot execute Ceph commands, check whether `proc_open` is disabled in your PHP configuration.
- If the logical view stays empty, the cluster may simply have no unhealthy PGs right now.

## Notes

- This project intentionally remains simple: no build step, no package manager, no frontend framework.
- The single-page wrappers `logical-single.html` and `iops-single.html` are convenience embeds for wallboard-style displays.

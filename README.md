# Lamplight

A localhost web UI for getting a LAMP stack onto Ubuntu and Debian — without a hosting control panel.

XAMPP on Windows bundled everything and gave you a small manager. Lamplight is the same idea, fitted
to how Debian actually works: packages from the distro, services in systemd, document root at
`/var/www/html`. You open `http://127.0.0.1:3847` and install Apache, PHP, MariaDB, phpMyAdmin, and
Mailpit **one component at a time**, watching the apt output as it happens. Each one gets its own
page in the left nav, with its own status, controls, and job history.

The panel is a small Flask + SQLite service. It starts fine on a machine where none of that is
installed yet — that is the point.

## The stack it installs

| Component  | Source                        | Service              | Ports      |
| ---------- | ----------------------------- | -------------------- | ---------- |
| Apache     | `apache2`                     | `apache2`            | 80, 443    |
| PHP        | `php`, `libapache2-mod-php`   | — (runs in Apache)   | —          |
| MariaDB    | `mariadb-server`              | `mariadb`            | 3306       |
| phpMyAdmin | `phpmyadmin`                  | — (served by Apache) | —          |
| Mailpit    | pinned GitHub release         | `lamplight-mailpit`  | 1025, 8025 |

Everything except Mailpit is a plain distro package. Mailpit is not in the Ubuntu archive, so
Lamplight downloads a **pinned release binary and verifies its SHA256** before installing it to
`/usr/local/bin` under a hardened systemd unit.

## Requirements

- Ubuntu 24.04 / 26.04, Debian 12 / 13, or another Debian-family distro with `apt` and `systemd`
- Git, to clone the repository. A fresh Ubuntu install does not include it: `sudo apt install git`
- Python 3.11+
- Root for the systemd service — it runs `apt-get` and `systemctl` for you

## Install Lamplight

```bash
git clone https://github.com/michaelstaake/lamplight.git
cd lamplight
sudo ./install.sh
```

`install.sh` puts the app in `/opt/lamplight`, data in `/var/lib/lamplight`, config in
`/etc/lamplight`, and enables `lamplight.service`. It does **not** install the LAMP stack - you do that using the panel afterward

When it finishes it prints the URL with the access token in it:

```
http://localhost:3847/?token=…
```

Lost it later?

```bash
sudo /opt/lamplight/.venv/bin/lamplight-token
```

To change the port, edit `/etc/lamplight/lamplight.env` and run `systemctl restart lamplight`.

### Run it from a checkout

For working on Lamplight itself:

```bash
make venv
make run
```

That binds to `127.0.0.1:3847` and prints the token. Run unprivileged it can only read status —
installs need root, so use `sudo .venv/bin/lamplight` if you have to, but prefer the unit file.

Put your project files in `/var/www/html`, or add an Apache vhost yourself. Lamplight does not invent
a second document-root layout.

## Reaching the site from the LAN

If `ufw` is running, the Apache page grows one more button next to **Disable at boot**: **Allow
80/443 in UFW**. It runs `ufw allow 80/tcp` and `ufw allow 443/tcp` and nothing else. Once the rules
are in place the same button reads **Block 80/443 in UFW** and deletes them again.

The button appears only when ufw is installed, enabled, and readable — a panel running unprivileged
cannot read `ufw status`, so it does not offer to change it. No other component gets this button:
your database and your mail catcher have no business being reachable from the network, and neither
does the Lamplight panel itself.

## Managing PHP

The PHP page has two things the other components do not. Extensions are a list of tags — drop one
with its ×, or add one by its package name (`php-imap`, not `imap`) — and common PHP options like
memory limit have their own fields.

Lamplight never edits your `php.ini`. It writes a
single drop-in instead:

```
/etc/php/<version>/apache2/conf.d/99-lamplight.ini
/etc/php/<version>/cli/conf.d/99-lamplight.ini
```

PHP reads `conf.d` after `php.ini` and in alphabetical order, so `99-` wins. Every installed SAPI
gets the same file. Clear a field and that directive stops being overridden; clear all of them and
the drop-in is deleted outright. Under each field the page shows the value actually in effect and
which file set it, so you can see what you are overriding before you override it.

Both live in `/etc/lamplight/php.json`:

```json
{
  "extensions": ["mysql", "curl", "mbstring", "redis"],
  "options": { "memory_limit": "512M", "date.timezone": "Europe/Berlin" }
}
```

Values are validated before anything is written — an option only accepts the shape its directive
takes, so nothing typed into the browser can become a second line of the `.ini`.

## Removing things

**Remove** on a card runs `apt-get remove` (not `purge`) and then `apt-get autoremove`. Your
databases in `/var/lib/mysql`, your site files, and captured mail in `/var/lib/lamplight-mailpit` are
all left alone. Delete those yourself if you actually want them gone. Removing PHP also deletes the
`99-lamplight.ini` drop-in, so nothing of ours is left behind in `/etc/php`.

## Security

The daemon runs as root so it can install packages. That is the same trust model as typing
`sudo apt install apache2`, and the same care applies: the token in `/etc/lamplight/auth.json` is
**equivalent to root** for the actions in the catalog. Treat it like a root password, and do not
proxy this panel to the internet.

- **Reachability.** Binds to `127.0.0.1`, which `install.sh` writes into
  `/etc/lamplight/lamplight.env`. The bind address is deliberately not editable from the browser —
  changing it there is exactly how a localhost panel accidentally becomes a public one. Edit the env
  file and restart instead.
- **The token.** Created mode 0600, written atomically, never logged, compared with
  `secrets.compare_digest`. The session cookie is `HttpOnly` and `SameSite=Strict`, so another site
  open in your browser cannot POST an install through your session.
- **Headers and assets.** `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, and a `default-src 'self'` CSP. No web fonts, no CDNs, no external
  images — the panel works on a machine with no internet and has nothing to exfiltrate to.
- **Commands.** Every command is an argv list passed to `Popen` without `shell=True`. Only the
  directives in `php.OPTIONS` can be set, every value is validated against its kind before being
  written, and an extension name is only ever used as `php-<name>` in an apt argument list — so
  nothing typed into the browser can become a second line of the `.ini`.
- **The firewall button.** The ports come from `Component.firewall_ports` in the catalog, never from
  the request, so only Apache's 80/443 can be touched: `POST /api/components/mariadb/firewall-open`
  is refused rather than opening 3306. Lamplight never enables or disables ufw itself and never
  removes a rule it did not add.
- **Mailpit.** The release version and each architecture's SHA256 are pinned in
  `lamplight/installer.py`; a mismatch aborts the install and deletes the download. Exactly one file
  is extracted from the archive, by name, so a crafted tarball cannot drop files elsewhere. The unit
  runs under `DynamicUser=yes` with `NoNewPrivileges`, `ProtectHome` and `PrivateDevices`, and both
  its SMTP and web listeners are loopback-only. `scripts/update-mailpit.sh` regenerates both pins.
- **Job logs.** Raw apt output, the most recent 200 jobs, in
  `/var/lib/lamplight/lamplight.sqlite3`. phpMyAdmin's debconf answers go through
  `debconf-set-selections` rather than the command line, so they stay out of the process table — but
  assume anything apt prints ends up in the log.

On a shared machine: keep the bind on loopback, run it under a dedicated admin account, and put an
authenticating proxy in front of it if it has to be reachable at all.

## Uninstall the panel

```bash
sudo ./uninstall.sh          # removes /opt/lamplight, keeps data and config
sudo PURGE=1 ./uninstall.sh  # also deletes /var/lib/lamplight and /etc/lamplight
```

Either way Apache, MariaDB, PHP, phpMyAdmin, and Mailpit stay installed. The script prints the
commands to remove those too.

## License

GPL-3.0. See [LICENSE](LICENSE).

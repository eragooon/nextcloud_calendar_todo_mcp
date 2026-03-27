# Nextcloud CalDAV MCP Server

An MCP (Model Context Protocol) server that lets Claude create calendar events and tasks in Nextcloud via CalDAV.

**Tools exposed:**
| Tool | Description |
|------|-------------|
| `create_event` | Writes a `VEVENT` into a Nextcloud Calendar |
| `create_todo`  | Writes a `VTODO`  into Nextcloud Tasks |

**Stack:** Python 3 · `mcp` SDK 1.x · `caldav` · uvicorn · Tailscale Funnel (HTTPS)

---

## Prerequisites

Install on the Proxmox host (Debian/Ubuntu):

```bash
apt update
apt install -y python3 python3-venv python3-pip git
```

No nginx, no certbot needed — Tailscale handles TLS automatically.

---

## 1 — Deploy the server files

```bash
# Files already live in /root/calendar_todo_mcp
# Copy the service file to systemd
mkdir -p /root/calendar_todo_mcp
cp server.py requirements.txt calendar_todo_mcp.service /root/calendar_todo_mcp/
```

---

## 2 — Python virtual environment

```bash
python3 -m venv /root/calendar_todo_mcp/venv
/root/calendar_todo_mcp/venv/bin/pip install --upgrade pip
/root/calendar_todo_mcp/venv/bin/pip install -r /root/calendar_todo_mcp/requirements.txt
```

---

## 3 — Configure `.env`

```bash
cp /root/calendar_todo_mcp/.env.example /root/calendar_todo_mcp/.env
nano /root/calendar_todo_mcp/.env       # or your preferred editor
chmod 600 /root/calendar_todo_mcp/.env
```

**Required values to fill in:**

| Variable | Example | Notes |
|----------|---------|-------|
| `NEXTCLOUD_URL` | `https://cloud.example.com` | No trailing slash |
| `NEXTCLOUD_USER` | `youruser` | Your login username |
| `NEXTCLOUD_APP_PASSWORD` | `xxxx-xxxx-xxxx` | See step below |
| `CALENDAR_NAME` | `Personal` | Exact display name in Nextcloud Calendar |
| `TASKS_CALENDAR_NAME` | `Tasks` | Exact display name in Nextcloud Tasks |
| `OAUTH_CLIENT_ID` | `calendar-todo-client` | Any identifier you choose |
| `OAUTH_CLIENT_SECRET` | *(generated)* | Run `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `RESOURCE_SERVER_URL` | `https://mymachine.tail-abc123.ts.net` | Your Tailscale Funnel URL (see step 4) |

### Create a Nextcloud App Password

1. Log into Nextcloud → **Settings** → **Security**
2. Scroll to **App passwords** → enter a name (e.g. `mcp-server`) → click **Generate new app password**
3. Copy the password into `NEXTCLOUD_APP_PASSWORD`

---

## 4 — Tailscale einrichten

### Tailscale installieren

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

Anmelden mit deinem Tailscale-Account im Browser, wenn der Link erscheint.

### MagicDNS und HTTPS in der Tailscale-Konsole aktivieren

1. Öffne [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
2. **MagicDNS** aktivieren
3. **HTTPS Certificates** aktivieren

### Funnel aktivieren (öffentlich erreichbar via HTTPS)

Tailscale Funnel macht den Server über das öffentliche Internet erreichbar —
nötig, damit Claude.ai (Cloud) den MCP-Server erreichen kann.

Der mitgelieferte `tailscale_funnel.service` startet den Funnel automatisch beim Booten (siehe Schritt 5). Zum Testen vorab:

```bash
tailscale funnel 8000
```

Danach die URL deines Servers ermitteln:

```bash
tailscale funnel status
# Ausgabe z.B.: https://mymachine.tail-abc123.ts.net/ proxies to http://127.0.0.1:8000
```

Diesen HTTPS-Hostnamen (z.B. `https://mymachine.tail-abc123.ts.net`) als
`RESOURCE_SERVER_URL` in die `.env` eintragen.

> **Nur im Tailnet (kein öffentlicher Zugriff)?**
> Wenn Claude Desktop auf einem Gerät im selben Tailnet läuft,
> reicht `tailscale serve 8000` statt `funnel`.

---

## 5 — Install and start the systemd services

Both service files need to be copied and enabled: the MCP server and the Tailscale Funnel.

```bash
# MCP server
cp /root/calendar_todo_mcp/calendar_todo_mcp.service \
   /etc/systemd/system/calendar-todo-mcp.service

# Tailscale Funnel (keeps the funnel running across reboots)
cp /root/calendar_todo_mcp/tailscale_funnel.service \
   /etc/systemd/system/tailscale-funnel-mcp.service

systemctl daemon-reload
systemctl enable --now calendar-todo-mcp
systemctl enable --now tailscale-funnel-mcp

# Verify both are running
systemctl status calendar-todo-mcp
systemctl status tailscale-funnel-mcp
journalctl -u calendar-todo-mcp -f
```

---

## 6 — Smoke test

Ersetze `mymachine.tail-abc123.ts.net` mit deiner tatsächlichen Tailscale-URL:

```bash
# OAuth-Metadaten-Endpoint — sollte JSON mit issuer/scopes zurückgeben
curl https://mymachine.tail-abc123.ts.net/.well-known/oauth-protected-resource \
  | python3 -m json.tool

# MCP-Endpoint ohne Token — sollte 401 zurückgeben
curl -s -o /dev/null -w "%{http_code}" \
     https://mymachine.tail-abc123.ts.net/mcp
```

---

## 7 — Connect Claude

MCP-Server in Claude.ai oder `claude_desktop_config.json` eintragen:

```json
{
  "mcpServers": {
    "calendar-todo": {
      "url": "https://mymachine.tail-abc123.ts.net/mcp"
    }
  }
}
```

Claude startet beim ersten Verbinden automatisch den OAuth-Flow (Browser-Redirect). Danach ist keine weitere Konfiguration nötig.

Claude kann dann Dinge sagen wie:
> *"Trag mir einen Zahnarzttermin am 3. April um 10 Uhr für 45 Minuten ein"*
> *"Erinnerung: Bank anrufen — fällig morgen"*

---

## Operations

```bash
# View live logs
journalctl -u calendar-todo-mcp -f

# Restart after config changes
systemctl restart calendar-todo-mcp

# Stop / disable
systemctl stop calendar-todo-mcp
systemctl disable calendar-todo-mcp
```

---

## Troubleshooting

| Problem | What to check |
|---------|--------------|
| `401 Unauthorized` | OAuth-Flow noch nicht abgeschlossen — Claude erneut verbinden und Autorisierung im Browser bestätigen |
| `Calendar 'X' not found` | The `CALENDAR_NAME` / `TASKS_CALENDAR_NAME` must match the **exact display name** in Nextcloud |
| CalDAV `401` | App password entered correctly? User has CalDAV access enabled? |
| `Connection refused` | Is the service running? `systemctl status calendar-todo-mcp` |
| Tailscale `502 Bad Gateway` | MCP server not running, or wrong `PORT` in `.env` |
| Funnel antwortet nicht | `tailscale funnel status` prüfen; ggf. `tailscale funnel 8000` erneut ausführen |
| Tailscale HTTPS geht nicht | MagicDNS und HTTPS Certificates in der Tailscale-Admin-Console aktiviert? |

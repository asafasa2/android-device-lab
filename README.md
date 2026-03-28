# Android Device Lab

A self-hosted server that turns a single Ubuntu PC with USB-connected Android phones
into a shared device farm for your whole team — with a web UI, real browser terminals,
and a CI/CD API for Jenkins pipelines.

---

## What it does

- **Web UI** — see all connected devices, battery levels, who reserved what
- **Reserve/Release** — prevent two engineers from using the same device simultaneously
- **Browser terminal** — full `adb shell` session in the browser via xterm.js
- **Screen mirror** — live PNG-stream screencap with tap/swipe input
- **CI API** — Jenkins pipelines reserve devices, run tests, release via REST
- **Remote ADB** — any machine on the network runs `adb -H <server> -P 5037` commands

## Architecture

```
                          ┌─────────────────────────────────────────┐
  Browser / Jenkins       │   Ubuntu PC  (172.31.254.224)           │
  ──────────────────      │                                         │
  :8000  Web UI           │   FastAPI (uvicorn :8000)               │
  :8000  REST API  ◄──────►     backend/main.py                     │
  :8000  WebSocket        │     ├── auth.py      (sessions)         │
                          │     ├── devices.py   (polls adb)        │
  adb -H host -P 5037     │     ├── reservations.py                 │
         ◄──────────────► │   adb server (:5037, all interfaces)    │
                          │     │                                   │
                          │     └── USB ──► Phone A, Phone B, …     │
                          └─────────────────────────────────────────┘
```

---

## Quick Start — bare-metal install

```bash
git clone <repo-url> ~/android-device-lab
cd ~/android-device-lab

# Non-root: installs venv + deps, prints manual start command
bash install.sh

# Root / sudo: also installs systemd service, enables it on boot
sudo bash install.sh
```

Open **http://\<server-ip\>:8000** in a browser.

Default password: `adblab123` — **change it** (see [Configuration](#configuration)).

### Start manually (no systemd)

```bash
~/android-device-lab/venv/bin/uvicorn backend.main:app \
    --host 0.0.0.0 --port 8000 --log-level info
```

### Service commands

```bash
sudo systemctl status  adb-lab
sudo systemctl stop    adb-lab
sudo systemctl restart adb-lab
sudo journalctl -u adb-lab -f        # live logs
```

---

## Docker deployment

```bash
cd ~/android-device-lab

# Build and start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

**Important:** the container runs privileged so it can access USB devices.
Edit `docker-compose.yml` to change passwords before exposing to the network.

### Environment overrides

```bash
docker compose up -d \
  -e ADB_LAB_PASSWORD=mysecret \
  -e ADB_LAB_CI_API_KEY=my-ci-key \
  -e ADB_LAB_SERVER_IP=172.31.254.224
```

---

## Ansible — deploy to multiple PCs

```bash
# 1. Copy and fill in the inventory
cp ansible/inventory.example ansible/inventory.ini
$EDITOR ansible/inventory.ini

# 2. Run
ansible-playbook -i ansible/inventory.ini ansible/playbook.yml

# 3. Dry-run first (no changes)
ansible-playbook -i ansible/inventory.ini ansible/playbook.yml --check

# 4. Only restart the service (after config changes)
ansible-playbook -i ansible/inventory.ini ansible/playbook.yml --tags service
```

The playbook:
- Installs `python3`, `adb`, `ffmpeg` via apt
- Creates a dedicated `adb-lab` system user in the `plugdev` group
- Copies the project to `/opt/android-device-lab`
- Creates a venv and installs Python dependencies
- Writes and enables the systemd service
- Opens UFW ports 8000 and 5037

---

## Uninstall

```bash
sudo bash uninstall.sh
```

Stops the service, removes the systemd unit, and optionally deletes the project directory.

---

## Configuration

All settings are environment variables with safe defaults.
Set them in:
- `/etc/systemd/system/adb-lab.service` → `Environment=` lines (then `sudo systemctl daemon-reload && sudo systemctl restart adb-lab`)
- `docker-compose.yml` → `environment:` block
- Shell: `export ADB_LAB_PASSWORD=...` before starting

| Variable | Default | Description |
|---|---|---|
| `ADB_LAB_PASSWORD` | `adblab123` | Web UI login password |
| `ADB_LAB_PORT` | `8000` | HTTP port for the web server |
| `ADB_PATH` | `adb` | Path to the adb binary |
| `RESERVATION_TIMEOUT_HOURS` | `2` | Auto-release devices reserved longer than this |
| `DEVICE_POLL_INTERVAL` | `5` | Seconds between `adb devices` polls |
| `ADB_LAB_CI_API_KEY` | `ci-key-change-me` | API key for CI endpoints |
| `ADB_LAB_SERVER_IP` | *(auto-detected)* | LAN IP shown in "ADB Connect" buttons |
| `DB_PATH` | `data/lab.db` | SQLite database path |

---

## Jenkins Integration

### Option A — Environment variables (simplest)

Set `ANDROID_ADB_SERVER_ADDRESS` on the agent; every `adb` call uses the remote server.

```groovy
pipeline {
    environment {
        ANDROID_ADB_SERVER_ADDRESS = '172.31.254.224'
        ANDROID_ADB_SERVER_PORT    = '5037'
        ANDROID_SERIAL             = 'YOUR_SERIAL'
    }
    stages {
        stage('Test') {
            steps {
                sh 'adb -s ${ANDROID_SERIAL} shell am instrument -w ...'
            }
        }
    }
}
```

### Option B — REST API (recommended for parallel pipelines)

Reserve a device at the start; auto-select by capability; release in `post { always }`.

```groovy
stage('Reserve device') {
    steps {
        script {
            def resp = sh(returnStdout: true, script: """
                curl -sf -X POST http://172.31.254.224:8000/api/ci/reserve \
                    -H 'Content-Type: application/json' \
                    -H "X-API-Key: ${CI_API_KEY}" \
                    -d '{"job_name":"${JOB_NAME}#${BUILD_NUMBER}","device_filter":{"min_api":30}}'
            """).trim()
            env.ANDROID_SERIAL = readJSON(text: resp).serial
        }
    }
}
post {
    always {
        sh """curl -sf -X POST http://172.31.254.224:8000/api/ci/release \
            -H 'Content-Type: application/json' \
            -H "X-API-Key: ${CI_API_KEY}" \
            -d '{"serial":"${env.ANDROID_SERIAL}"}'"""
    }
}
```

Full examples in `ci-examples/`:

| File | Description |
|---|---|
| `Jenkinsfile-env-vars` | Env-var approach, no reservation API |
| `Jenkinsfile-api` | REST reserve/release with device filter |
| `Jenkinsfile-appium` | Appium pipeline with remote ADB |
| `jenkins-shared-lib.groovy` | `androidLab.withDevice { … }` shared library |

See `ci-examples/README.md` for the full CI guide.

### Jenkins credentials setup

1. Go to **Manage Jenkins → Credentials → (global) → Add Credential**
2. Kind: **Secret text**
3. ID: `adb-lab-ci-api-key`
4. Secret: the value of `ADB_LAB_CI_API_KEY` on your server

---

## API Reference

### Authentication

| Endpoint prefix | Auth method |
|---|---|
| `/api/login`, `/api/status` | None (public) |
| `/api/devices`, `/ws/devices`, `/ws/terminal/*` | Cookie (`adb_lab_session`) set at login |
| `/api/ci/*` | `X-API-Key` header or `"api_key"` in JSON body |

### Browser API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/login` | `{password, display_name}` → sets session cookie |
| `POST` | `/api/logout` | Clears session |
| `GET`  | `/api/status` | Server info, device count (public) |
| `GET`  | `/api/devices` | All devices + active reservations |
| `POST` | `/api/devices/{serial}/reserve` | Reserve a device |
| `POST` | `/api/devices/{serial}/release` | Release your reservation |
| `GET`  | `/api/devices/{serial}/history` | Last 50 reservation events |
| `WS`   | `/ws/devices` | Live device list, updated every 3 s |
| `WS`   | `/ws/terminal/{serial}` | Interactive adb shell (PTY) |
| `WS`   | `/ws/mirror/{serial}` | Screen mirror stream |

### CI API (`/api/ci/`)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/ci/reserve` | Reserve by serial or filter |
| `POST` | `/api/ci/release` | Release a CI reservation |
| `GET/POST` | `/api/ci/devices` | List devices with availability |
| `POST` | `/api/ci/execute` | Run adb command, returns stdout/stderr |

**Reserve request:**
```json
{
  "api_key": "...",
  "job_name": "smoke-test#42",
  "device_filter": {"min_api": 30}
}
```

**Reserve response:**
```json
{
  "ok": true,
  "serial": "R3CT10XXXXX",
  "model": "Galaxy S21",
  "android_version": "13",
  "android_api_level": 33,
  "adb_connect_command": "adb -H 172.31.254.224 -P 5037 -s R3CT10XXXXX shell",
  "env": {
    "ANDROID_ADB_SERVER_ADDRESS": "172.31.254.224",
    "ANDROID_ADB_SERVER_PORT": "5037",
    "ANDROID_SERIAL": "R3CT10XXXXX"
  }
}
```

**Device filter fields:**

| Field | Type | Example |
|---|---|---|
| `min_api` | int | `30` (Android 11+) |
| `model` | string | `"Pixel"` (partial match) |
| `manufacturer` | string | `"Samsung"` |
| `android_version` | string | `"13"` (exact) |

**Execute — blocked commands:** `reboot`, `kill-server`, `emu`, shell `halt`/`poweroff`, `rm -rf /`, `mkfs`, `dd of=/dev/`, `wipe`.

---

## Troubleshooting

### No devices listed in the web UI

```bash
# On the server
adb devices
```

- If empty: check USB cables, enable Developer Mode + USB debugging on each phone
- If `unauthorized`: tap "Allow" on the phone's USB debugging dialog
- If `offline`: try `adb kill-server && adb start-server`

### `adb server version mismatch` on Jenkins agent

The agent's `adb` binary is a different version than the server's.

**Fix 1:** Install the same adb version on the agent as on the server.
**Fix 2:** Use `/api/ci/execute` instead of local adb — the command runs on the server where versions always match.

### `Connection refused` on port 5037

```bash
# Verify adb is bound to all interfaces
ss -tlnp | grep 5037   # should show 0.0.0.0:5037

# If not, restart the service
sudo systemctl restart adb-lab

# Check firewall
sudo ufw status
sudo ufw allow 5037/tcp
```

### Device stuck "reserved" after pipeline failure

```bash
# Release via API
curl -X POST http://172.31.254.224:8000/api/ci/release \
    -H 'Content-Type: application/json' \
    -H 'X-API-Key: ci-key-change-me' \
    -d '{"serial": "DEVICE_SERIAL"}'
```

Or log into the web UI and click **Release** on the device card.
Devices also auto-release after `RESERVATION_TIMEOUT_HOURS` (default 2 h).

### Service fails to start

```bash
sudo journalctl -u adb-lab -n 50 --no-pager
```

Common causes:
- `adb` not found → `sudo apt install adb` and restart the service
- Port 8000 already in use → `sudo lsof -i :8000` to find the conflicting process
- Python dependency missing → `cd /opt/android-device-lab && venv/bin/pip install -r requirements.txt`

### `plugdev` group — USB permission denied

```bash
# Add the service user to the plugdev group
sudo usermod -aG plugdev adb-lab   # or whatever RUN_USER is

# Restart
sudo systemctl restart adb-lab
```

---

## Project layout

```
android-device-lab/
├── install.sh              # Installer (idempotent)
├── uninstall.sh            # Removes service and optionally the project
├── adb-lab.service         # Systemd unit template
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── backend/
│   ├── main.py             # FastAPI app, lifespan, WebSocket /ws/devices
│   ├── auth.py             # Login/logout, in-memory sessions
│   ├── config.py           # All env-var settings
│   ├── database.py         # aiosqlite connection factory, schema
│   ├── devices.py          # adb polling, device cache
│   ├── reservations.py     # Reserve/release routes + auto-release loop
│   ├── terminal.py         # PTY-backed adb shell via WebSocket
│   ├── mirror.py           # screencap frame stream via WebSocket
│   └── ci.py               # CI/CD REST API (/api/ci/*)
├── frontend/
│   ├── index.html          # Device dashboard (vanilla JS + CSS)
│   └── terminal.html       # xterm.js browser terminal
├── data/                   # SQLite DB lives here (gitignored)
├── ci-examples/
│   ├── Jenkinsfile-env-vars
│   ├── Jenkinsfile-api
│   ├── Jenkinsfile-appium
│   ├── jenkins-shared-lib.groovy
│   └── README.md
└── ansible/
    ├── playbook.yml
    ├── inventory.example
    └── templates/
        └── service.j2
```

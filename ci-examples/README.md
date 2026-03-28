# CI/CD Integration Guide

This guide explains how to integrate Jenkins pipelines with the Android Device Lab so
automation jobs can run `adb` commands on real devices connected to the lab server at
`172.31.254.224`.

---

## Architecture

```
Jenkins Agent                    Device Lab Server (172.31.254.224)
─────────────────────            ──────────────────────────────────────
                                 adb server  :5037   ← devices connect here
  adb -H 172.31.254.224          FastAPI app :8000   ← REST/WS API
      -P 5037                    │
      -s SERIAL                  ├── /api/ci/reserve
      shell ...                  ├── /api/ci/release
                                 ├── /api/ci/devices
  curl :8000/api/ci/*            └── /api/ci/execute
```

The device lab server runs an `adb server` bound to `0.0.0.0:5037`.  Jenkins agents
connect to it remotely using the `-H` / `-P` flags (or the
`ANDROID_ADB_SERVER_ADDRESS` / `ANDROID_ADB_SERVER_PORT` environment variables).

---

## Authentication

All CI endpoints require an API key, supplied via:

- HTTP header: `X-API-Key: <key>`
- JSON body field: `"api_key": "<key>"`

Default key: `ci-key-change-me`
**Change it** by setting `ADB_LAB_CI_API_KEY=your-secret` in the server's environment.

Store the key in Jenkins as a **Secret Text** credential named `adb-lab-ci-api-key`.

---

## Two approaches

### Approach 1 — Environment variables (simple)

Set `ANDROID_ADB_SERVER_ADDRESS` and `ANDROID_ADB_SERVER_PORT` in the pipeline
`environment {}` block. Every `adb` command automatically uses the remote server.

**When to use:**
- You already know the device serial (no dynamic selection needed).
- Your existing scripts already use `adb` — no code changes required.
- You're running a single-device pipeline with no parallelism.

```groovy
environment {
    ANDROID_ADB_SERVER_ADDRESS = '172.31.254.224'
    ANDROID_ADB_SERVER_PORT    = '5037'
    ANDROID_SERIAL             = 'YOUR_DEVICE_SERIAL'
}
stages {
    stage('Test') {
        steps {
            sh 'adb devices'
            sh 'adb -s ${ANDROID_SERIAL} shell am instrument -w ...'
        }
    }
}
```

See full example: `Jenkinsfile-env-vars`

### Approach 2 — REST API reservation (recommended)

Call `/api/ci/reserve` at the start of the pipeline and `/api/ci/release` in
`post { always {} }`. The API auto-selects an available device and returns its serial.

**When to use:**
- Multiple pipelines run in parallel (prevents device conflicts).
- You want automatic device selection by capability (API level, model, etc.).
- You want the web UI to show which job owns which device.
- You need the `/api/ci/execute` endpoint (run commands without adb installed on agent).

```groovy
stage('Reserve device') {
    steps {
        script {
            def resp = sh(returnStdout: true, script: """
                curl -sf -X POST http://172.31.254.224:8000/api/ci/reserve \\
                    -H 'Content-Type: application/json' \\
                    -H "X-API-Key: ${CI_API_KEY}" \\
                    -d '{"job_name": "${JOB_NAME}#${BUILD_NUMBER}", "device_filter": {"min_api": 30}}'
            """).trim()
            env.ANDROID_SERIAL = readJSON(text: resp).serial
        }
    }
}
post {
    always {
        sh """curl -sf -X POST http://172.31.254.224:8000/api/ci/release \\
            -H 'Content-Type: application/json' \\
            -H "X-API-Key: ${CI_API_KEY}" \\
            -d '{"serial": "${env.ANDROID_SERIAL}"}'"""
    }
}
```

See full example: `Jenkinsfile-api`

---

## API Reference

### POST /api/ci/reserve

Reserve a device. Returns device info including a ready-to-use `adb_connect_command`.

**Request body:**
```json
{
  "api_key": "...",
  "job_name": "smoke-tests#42",
  "device_serial": "ABC123",        // specific serial — OR use device_filter
  "device_filter": {"min_api": 30}  // filter — see below
}
```

Use `"device_serial": "any"` or `"device_filter": "any"` to pick the first available device.

**Device filter fields:**

| Field | Type | Description |
|---|---|---|
| `min_api` | int | Minimum Android API level (e.g. `30` = Android 11+) |
| `model` | string | Partial model name match, case-insensitive (e.g. `"Pixel"`) |
| `manufacturer` | string | Partial manufacturer match (e.g. `"Samsung"`) |
| `android_version` | string | Exact version string (e.g. `"13"`) |

**Response:**
```json
{
  "ok": true,
  "serial": "R3CT10XXXXX",
  "model": "Galaxy S21",
  "manufacturer": "Samsung",
  "android_version": "13",
  "android_api_level": 33,
  "battery_level": 87,
  "reserved_at": "2024-01-15T10:23:00.123456",
  "adb_connect_command": "adb -H 172.31.254.224 -P 5037 -s R3CT10XXXXX shell",
  "env": {
    "ANDROID_ADB_SERVER_ADDRESS": "172.31.254.224",
    "ANDROID_ADB_SERVER_PORT": "5037",
    "ANDROID_SERIAL": "R3CT10XXXXX"
  }
}
```

**Error codes:**
- `401` — invalid API key
- `404` — requested serial not online
- `409` — requested serial already reserved
- `503` — no available device matches the filter

---

### POST /api/ci/release

```json
{ "api_key": "...", "serial": "R3CT10XXXXX", "job_name": "smoke-tests#42" }
```

`job_name` is optional; if provided it's verified against the stored reservation.

---

### GET /api/ci/devices

List all online devices with availability status. Authenticate via `X-API-Key` header.

```bash
curl http://172.31.254.224:8000/api/ci/devices -H "X-API-Key: ci-key-change-me"
```

Useful in pipeline scripts to check availability before reserving, or to build a
dynamic parallel matrix.

---

### POST /api/ci/execute

Run an adb command on a reserved device without needing adb on the Jenkins agent.

```json
{
  "api_key": "...",
  "serial": "R3CT10XXXXX",
  "command": "shell getprop ro.build.version.release",
  "timeout": 30
}
```

The `command` is the adb subcommand + arguments (without `adb -s <serial>`).

**Blocked commands:** `reboot`, `kill-server`, `emu`, shell `reboot`/`halt`/`poweroff`,
`rm -rf /`, `mkfs`, `dd of=/dev/`, `wipe`.

**Response:**
```json
{
  "ok": true,
  "exit_code": 0,
  "stdout": "13\n",
  "stderr": "",
  "serial": "R3CT10XXXXX",
  "command": "shell getprop ro.build.version.release"
}
```

---

## Appium configuration

To run Appium tests against a device on the lab server, set the
`ANDROID_ADB_SERVER_ADDRESS` env var before starting Appium.  Appium calls
`adb` internally; with this env var set, `adb` automatically connects to the
remote server.

```groovy
sh """
    ANDROID_ADB_SERVER_ADDRESS=172.31.254.224 \\
    ANDROID_ADB_SERVER_PORT=5037 \\
    appium --port 4723 &
"""
```

In your Appium capabilities, set `udid` to the device serial:

```java
// Java/TestNG example
DesiredCapabilities caps = new DesiredCapabilities();
caps.setCapability("platformName",     "Android");
caps.setCapability("deviceName",       "Android Device");
caps.setCapability("udid",             System.getenv("ANDROID_SERIAL"));
caps.setCapability("automationName",   "UiAutomator2");
caps.setCapability("app",              "/path/to/app.apk");

// Point Appium at the local agent's Appium server
AppiumDriver driver = new AndroidDriver(
    new URL("http://localhost:4723"), caps
);
```

See full example: `Jenkinsfile-appium`

---

## Shared library

Copy `jenkins-shared-lib.groovy` to your shared library repository as
`vars/androidLab.groovy`. Then in any Jenkinsfile:

```groovy
@Library('android-device-lab') _

pipeline {
    agent any
    stages {
        stage('Test') {
            steps {
                script {
                    androidLab.withDevice(filter: [min_api: 30]) {
                        sh 'adb -s ${ANDROID_SERIAL} shell echo hello'
                    }
                }
            }
        }
    }
}
```

`withDevice` handles reserve → run → release automatically, even on failure.

---

## Jenkins setup checklist

- [ ] Create credential `adb-lab-ci-api-key` (Secret Text) in Jenkins
- [ ] Set `ADB_LAB_CI_API_KEY` env var on the lab server to match
- [ ] Ensure agent has `adb` installed: `apt install adb` or `brew install android-platform-tools`
- [ ] Verify agent can reach `172.31.254.224:5037` — check firewall
- [ ] Test from agent: `adb -H 172.31.254.224 -P 5037 devices`

---

## Troubleshooting

### `adb server version (X) doesn't match this client (Y)`

The `adb` binary on your Jenkins agent is a different version than the one
running on the lab server. Solutions (pick one):

1. **Install matching adb version** on the agent.
   Check server version: `ssh 172.31.254.224 'adb version'`

2. **Use the REST API execute endpoint** (`/api/ci/execute`) instead of local adb.
   The command runs on the lab server where adb versions always match.

3. **Use the shared adb server mode** (already configured):
   The server starts adb with `-a -P 5037` which accepts remote connections.
   Version mismatches still surface unless client = server version.

---

### `ECONNREFUSED` / `Connection refused` to port 5037

Check the firewall on `172.31.254.224`:
```bash
# On the lab server:
sudo ufw allow 5037/tcp
sudo ufw allow 8000/tcp

# Or for a specific subnet:
sudo ufw allow from 10.0.0.0/8 to any port 5037
```

Verify adb is listening:
```bash
ss -tlnp | grep 5037
# Should show: 0.0.0.0:5037
```

---

### `error: device 'SERIAL' not found`

The device serial you reserved went offline between reservation and test execution.
The web UI will show it as offline-but-reserved.

Solutions:
1. Check the device is plugged in and `adb devices` shows it on the lab server.
2. Use `GET /api/ci/devices` to check availability before reserving.
3. Retry logic: release and re-reserve if the device goes offline.

---

### Device is stuck reserved (previous pipeline failed to release)

The lab auto-releases devices after 2 hours (configurable via
`RESERVATION_TIMEOUT_HOURS` on the server). To release immediately:

```bash
curl -X POST http://172.31.254.224:8000/api/ci/release \
    -H 'Content-Type: application/json' \
    -H 'X-API-Key: ci-key-change-me' \
    -d '{"serial": "YOUR_SERIAL"}'
```

Or use the web UI at `http://172.31.254.224:8000` — log in and click Release.

---

### `curl: (22) The requested URL returned error: 503`

No available device matches your filter.  Options:
1. Widen the filter (lower `min_api`, remove model constraint).
2. Wait and retry — another pipeline may be about to release a device.
3. Check `GET /api/ci/devices` to see what's currently available and reserved by whom.

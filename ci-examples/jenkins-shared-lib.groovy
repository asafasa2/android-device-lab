// ─────────────────────────────────────────────────────────────────────────────
// Jenkins Shared Library — Android Device Lab
//
// Installation:
//   1. Create a new Pipeline Library in Jenkins:
//      Manage Jenkins → Configure System → Global Pipeline Libraries
//      Name: android-device-lab
//      Default version: main
//      Source: this repository (or copy this file to vars/androidLab.groovy)
//
//   2. In your Jenkinsfile:
//      @Library('android-device-lab') _
//
//      androidLab.withDevice(filter: [min_api: 30]) {
//          sh 'adb -s ${ANDROID_SERIAL} shell ...'
//      }
//
// Configuration (Jenkins credentials):
//   - 'adb-lab-ci-api-key' : Secret text — CI API key for the device lab
//
// Configuration (Jenkins global env vars, or override per pipeline):
//   ADB_LAB_URL = http://172.31.254.224:8000
//   ANDROID_ADB_SERVER_ADDRESS = 172.31.254.224
//   ANDROID_ADB_SERVER_PORT    = 5037
// ─────────────────────────────────────────────────────────────────────────────

// ── Reserve a device ──────────────────────────────────────────────────────────
/**
 * Reserve a device from the lab.
 *
 * @param filter  Map of filter criteria, or the string "any".
 *                Keys: min_api (int), model (String), manufacturer (String),
 *                android_version (String).
 *                Example: [min_api: 30]  |  [model: 'Pixel 6']  |  'any'
 * @param serial  Specific device serial to reserve (optional; overrides filter).
 * @return        Map with keys: serial, model, manufacturer, android_version,
 *                android_api_level, battery_level, adb_connect_command, env
 */
def reserveDevice(Map args = [:]) {
    def labUrl    = env.ADB_LAB_URL ?: 'http://172.31.254.224:8000'
    def apiKey    = _getApiKey()
    def jobLabel  = "${env.JOB_NAME}#${env.BUILD_NUMBER}"

    def filter    = args.get('filter', 'any')
    def serial    = args.get('serial', null)

    def body
    if (serial) {
        body = groovy.json.JsonOutput.toJson([
            job_name     : jobLabel,
            device_serial: serial,
        ])
    } else {
        body = groovy.json.JsonOutput.toJson([
            job_name     : jobLabel,
            device_filter: filter,
        ])
    }

    def response = sh(
        returnStdout: true,
        script: """
            curl -sf -X POST ${labUrl}/api/ci/reserve \\
                -H 'Content-Type: application/json' \\
                -H "X-API-Key: ${apiKey}" \\
                -d '${body}'
        """
    ).trim()

    def json = readJSON text: response
    if (!json.ok) {
        error("[androidLab] Device reservation failed: ${response}")
    }

    echo "[androidLab] Reserved ${json.serial} — ${json.manufacturer} ${json.model}, Android ${json.android_version} (API ${json.android_api_level})"
    if (json.battery_level != null) {
        echo "[androidLab] Battery: ${json.battery_level}%"
    }

    return json
}

// ── Release a device ──────────────────────────────────────────────────────────
/**
 * Release a previously reserved device.
 *
 * @param serial  Serial number returned by reserveDevice().
 */
def releaseDevice(String serial) {
    def labUrl   = env.ADB_LAB_URL ?: 'http://172.31.254.224:8000'
    def apiKey   = _getApiKey()
    def jobLabel = "${env.JOB_NAME}#${env.BUILD_NUMBER}"

    try {
        sh """
            curl -sf -X POST ${labUrl}/api/ci/release \\
                -H 'Content-Type: application/json' \\
                -H "X-API-Key: ${apiKey}" \\
                -d '{"serial": "${serial}", "job_name": "${jobLabel}"}'
        """
        echo "[androidLab] Released ${serial}"
    } catch (e) {
        echo "[androidLab] Warning: could not release ${serial}: ${e.message}"
        echo "[androidLab] The server will auto-release it after the reservation timeout."
    }
}

// ── withDevice — reserve, run body, always release ────────────────────────────
/**
 * High-level helper: reserves a device, exposes it via env.ANDROID_SERIAL,
 * runs the closure, then releases regardless of success or failure.
 *
 * Usage:
 *   androidLab.withDevice(filter: [min_api: 30]) {
 *       sh 'adb -s ${ANDROID_SERIAL} shell ...'
 *   }
 *
 *   androidLab.withDevice(serial: 'ABC123') {
 *       sh 'adb -s ${ANDROID_SERIAL} install app.apk'
 *   }
 *
 * @param filter   Device filter map or "any" (passed to reserveDevice).
 * @param serial   Specific serial (passed to reserveDevice).
 * @param body     Closure to execute with device reserved.
 */
def withDevice(Map args = [:], Closure body) {
    def deviceInfo = null
    try {
        deviceInfo = reserveDevice(args)

        // Expose as environment variables for shell steps
        env.ANDROID_SERIAL              = deviceInfo.serial
        env.ANDROID_ADB_SERVER_ADDRESS  = env.ANDROID_ADB_SERVER_ADDRESS ?: '172.31.254.224'
        env.ANDROID_ADB_SERVER_PORT     = env.ANDROID_ADB_SERVER_PORT    ?: '5037'

        body()
    } finally {
        if (deviceInfo?.serial) {
            releaseDevice(deviceInfo.serial)
        }
    }
}

// ── List available devices ────────────────────────────────────────────────────
/**
 * Returns the list of devices from /api/ci/devices.
 * Useful for dynamic parallel matrix pipelines.
 *
 * @return List of device maps.
 */
def listDevices() {
    def labUrl = env.ADB_LAB_URL ?: 'http://172.31.254.224:8000'
    def apiKey = _getApiKey()

    def response = sh(
        returnStdout: true,
        script: """
            curl -sf ${labUrl}/api/ci/devices \\
                -H "X-API-Key: ${apiKey}"
        """
    ).trim()

    def json = readJSON text: response
    return json.devices ?: []
}

// ── Run adb command via REST API ──────────────────────────────────────────────
/**
 * Execute an adb command on a reserved device via the REST API.
 * Useful when the Jenkins agent doesn't have adb installed.
 *
 * @param serial   Device serial.
 * @param command  adb command (without 'adb -s <serial>').
 * @param timeout  Timeout in seconds (default 60).
 * @return         Map with ok, exit_code, stdout, stderr.
 */
def execute(String serial, String command, int timeout = 60) {
    def labUrl   = env.ADB_LAB_URL ?: 'http://172.31.254.224:8000'
    def apiKey   = _getApiKey()
    def jobLabel = "${env.JOB_NAME}#${env.BUILD_NUMBER}"

    def body = groovy.json.JsonOutput.toJson([
        serial  : serial,
        command : command,
        job_name: jobLabel,
        timeout : timeout,
    ])

    def response = sh(
        returnStdout: true,
        script: """
            curl -sf -X POST ${labUrl}/api/ci/execute \\
                -H 'Content-Type: application/json' \\
                -H "X-API-Key: ${apiKey}" \\
                -d '${body}'
        """
    ).trim()

    def json = readJSON text: response
    if (json.stdout) { echo json.stdout }
    if (json.stderr) { echo "[stderr] ${json.stderr}" }
    return json
}

// ── Private: resolve API key ──────────────────────────────────────────────────
private def _getApiKey() {
    // First try a Jenkins credential bound to 'adb-lab-ci-api-key'
    // If the calling pipeline hasn't bound it, fall back to env var
    try {
        return credentials('adb-lab-ci-api-key')
    } catch (e) {
        def key = env.ADB_LAB_CI_API_KEY ?: env.CI_API_KEY
        if (!key) {
            error("[androidLab] No API key found. Bind 'adb-lab-ci-api-key' credential or set ADB_LAB_CI_API_KEY env var.")
        }
        return key
    }
}

# VisionEdge

**VisionEdge** is the standalone AI-Camera application built from `LIVE_FLOW_GRAPH_REVIEW`.
It keeps the existing Product / Template / Rule / SOP / Flow Studio data model and adds a
single-camera runtime for live preview, real-time inference, snapshots and recording.
`smartcam_webserver` is used as the Qualcomm camera / HTTPS / capture behavior reference.

## Current release

`VisionEdge 2.1.0-edge.8`

## 2.1.0-edge.8 Video Label / Flow consistency fixes

- Video Label saves now synchronize regions by diff instead of deleting and recreating every row. Existing templates keep stable `region.id` values, so Flow/SOP `source_region_id` references do not churn on every label save.
- Deleting the final label from a Workspace frame now removes the orphan capture group on save. Pre-existing orphan Workspace frames from older releases are hidden immediately and pruned on the next save.
- Workspace thumbnails have an edit-mode remove action for deleting a frame and its labels together.
- Flow Designer scratch labels use the additive `/api/products/<pid>/regions/append` endpoint. The old one-element bulk-save path could accidentally replace the product's entire template library.
- Flow/SOP Designer reloads the template library with `no-store` and re-validates source template IDs immediately before save, producing a clear message when a truly deleted template has no local-copy fallback.
- Added `label_flow_regression_test.py` covering stable region IDs, orphan Workspace cleanup, additive region creation, and immediate SOP save of a new label.

### Main runtime architecture

```text
AI Camera sensor (qtiqmmfsrc)
        |
        +--> JPEG/appsink --> Preview --> Template/SOP inference --> Result view
        |
        +--> H.264 encoder --> on-demand MP4 recording
```

One process owns the physical camera. Preview/inference/snapshot/recording do not hand the
camera back and forth.


## 2.1.0-edge.5 UX / default revision

The camera operator UI was simplified again after validating the settings against the supplied SmartCam project and the QTI device test:

- Camera sensor target stays internally at `30/1`; the one-choice **Frame rate** setting is no longer shown.
- **AI Analysis Rate** is a preset instead of a free-form number: Low load 2 FPS, Balanced 5 FPS (default), Fast 10 FPS.
- Product 0 is shown as **No inspection product · Camera only**; AI-only controls are disabled in that mode.
- Live status labels Stream FPS and AI FPS as **actual** measured rates.
- Recording bitrate defaults to **Automatic**: 8 Mbps at 1080p / 16 Mbps at 4K.
- Storage safety reserve defaults to **10 GB**.
- Live operator status shows Camera / Recording / Inspection / Storage. Backend, hardware-recorder capability, and frame sequence are kept under Developer diagnostics.
- Stop Camera and Apply Settings are blocked during active recording. Active MP4 files are not offered for open/download/delete until finalized.
- Settings now track unsaved changes; Apply / Discard stay disabled until something really changed.
- Software AI-overlay recording uses the measured received-frame rate rather than always declaring 30 FPS, avoiding fast playback when the appsink path is closer to ~24 FPS.

Default runtime profile:

```text
1920 × 1080
Camera sensor target: 30 FPS (internal)
AI analysis target:   5 FPS
Recording bitrate:    Automatic (8 Mbps 1080p / 16 Mbps 4K)
Storage reserve:      10 GB
```


## 2.1.0-edge.4 UI revision

The main UI now separates operating the camera from configuring it:

```text
Live       Camera / current display / snapshot / recording / SOP
Media      Photos and recordings
Settings   Camera / AI / recording / developer configuration
Flow Studio existing Product / Template / Rule / SOP editor
```

`Raw / AI Result` is a **display selector** in the Live Camera header. The single
`Take Snapshot` action captures whichever source is currently displayed. `AI Result`
is unavailable in Product 0 preview-only mode.

Camera Width and Height are no longer independent text fields. They are represented by
a single Resolution selector, following the provided SmartCam Capture design:

```text
1920 × 1080
3840 × 2160
```

If an existing INI contains another resolution, the UI preserves that value as the
current selection so merely opening Settings cannot rewrite it accidentally.

Settings use one `Apply Settings` action. If the camera is live, applying settings
restarts the camera automatically; if it is stopped, settings are only saved.

## 2.1 UI + HTTPS revision

### HTTPS

VisionEdge now serves HTTPS directly by default, using the certificate/key copied from the
provided `smartcam_webserver` project:

```ini
[server]
host = 0.0.0.0
port = 8080
tls = on
certfile = config/certs/CRS0000000616.cert
keyfile = config/certs/CRS0000000616.key
```

Start with:

```sh
cd /var/rootdirs/opt/VisionEdge
python3 -u visionedge_server.py
```

Open:

```text
https://<camera-hostname>:8080/edge
```

The supplied certificate SAN is for the HPH AI-camera DNS names in the SmartCam project.
Using a raw IP address may therefore produce a browser certificate-name warning even though
TLS itself is working. Prefer the DNS hostname that matches the certificate.

For development only, HTTPS can be disabled explicitly:

```sh
python3 -u visionedge_server.py --no-tls --port 8080
```

`VISIONEDGE_TLS=off` is also supported.

### Startup preflight

VisionEdge validates the TLS certificate/key and checks whether the TCP port is available
**before starting qtiqmmfsrc**. A port conflict therefore no longer opens the Qualcomm camera
and then tears it down abruptly.

### UI

The `/edge` UI is reorganized around state instead of independent Start/Stop buttons:

- Camera Runtime: one `Start Camera / Stop Camera` toggle.
- Recording: one `Start Recording / Stop Recording` toggle.
- Large LIVE / REC state badges on the video.
- Top-level Camera / Recording / AI status chips.
- Separate Raw / Result view selector.
- Snapshot buttons are disabled when no frame is available.
- Recording mode cannot be changed while recording.
- System status is shown separately from SOP Flow.
- Media manager is full-width and separated from Advanced Configuration.
- Advanced settings are collapsed by default.

### Media deletion fix

Media management now follows the same consistency idea used by `smartcam_webserver`:

- All `/api/edge/*` dynamic responses use `Cache-Control: no-store`.
- Every media create/delete/record-stop operation bumps `media_version`.
- The browser polls `media_version` and reloads the gallery only when it changes.
- Delete uses `POST /api/edge/media/delete` with a JSON path to avoid URL/proxy ambiguity.
- The server verifies `unlink()` actually removed the directory entry before returning success.
- Deleting the file currently being recorded returns `409 recording_active`.
- The old `DELETE /api/edge/media/<path>` route remains for compatibility.

## Device deployment

Recommended target path:

```text
/var/rootdirs/opt/VisionEdge
```

If replacing an existing VisionEdge installation, preserve any runtime data you care about,
especially:

```text
db/inspection.db
runtime_data/
uploads/
```

Then start manually first:

```sh
cd /var/rootdirs/opt/VisionEdge
python3 -u visionedge_server.py
```

Verify:

```sh
curl -k https://127.0.0.1:8080/api/edge/status | python3 -m json.tool
```

Expected camera fields on Qualcomm hardware include:

```text
backend = qti
status = LIVE
running = true
has_raw = true
frame_seq increasing
hardware_recording = true
```

## systemd

A sample unit is included as `visionedge.service` and points at:

```text
/var/rootdirs/opt/VisionEdge
```

Install it with:

```sh
cp visionedge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable visionedge
systemctl restart visionedge
journalctl -u visionedge -f
```

## URLs

```text
VisionEdge UI : https://<camera-hostname>:8080/edge
Alias         : https://<camera-hostname>:8080/visionedge
Flow Studio   : https://<camera-hostname>:8080/flow-studio
Status API    : https://<camera-hostname>:8080/api/edge/status
```

## Tests

Host-side camera/runtime smoke test:

```sh
python3 edge_smoke_test.py
```

VisionEdge regression test:

```sh
python3 visionedge_regression_test.py
```

The regression test covers:

- TLS cert/key loading.
- snapshot create/delete.
- delete verification.
- media revision updates.
- active-recording delete guard.
- recording stop followed by deletion.

## Important files

```text
visionedge_server.py           HTTPS Web/API entrypoint
edge_server.py                 backward-compatible launcher
edge_runtime.py                single-camera runtime
static/edge_dashboard.html     VisionEdge UI
config/edge.ini                HTTPS + camera/runtime configuration
config/certs/                  certificate/key
visionedge.service             systemd sample
run_visionedge.sh              Linux launcher
visionedge_regression_test.py  regression checks
```

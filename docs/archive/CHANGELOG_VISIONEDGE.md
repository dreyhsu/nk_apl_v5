# VisionEdge 2.1.0-edge.8

## Video Label Studio / Vision Flow data consistency

- Fixed orphan **Workspace frames** remaining after their final label was deleted. Capture-group listing now excludes orphan groups and region save prunes them from the database.
- Replaced destructive `DELETE all regions -> INSERT all regions` label saves with stable-ID diff synchronization. Retained templates keep the same `regions.id`, preventing Flow/SOP source references from becoming stale after ordinary label edits.
- Added edit-mode Workspace-frame removal; removing a frame also removes its labels when the user saves.
- Fixed Flow Designer's scratch-label **Add to Step** path: it now uses an additive `/regions/append` API instead of sending a one-region payload to the full-library sync endpoint. The old behavior could delete unrelated templates.
- Flow/SOP Designer now fetches the template library with `cache: no-store` and validates source references immediately before saving.
- Added regression coverage for delete/relabel/new-label -> SOP save sequences.

---

# VisionEdge 2.1.0-edge.7

## Qualcomm QIR camera startup hardening

- Rebuilt the QTI camera pipeline programmatically instead of using `Gst.parse_launch`.
- Requests `qtiqmmfsrc.video_0`, sets `type=PREVIEW (0)`, and only then links the pad, matching the known-good QIR/vendor camera path already present in the LIVE_FLOW_GRAPH_REVIEW base.
- Keeps the single-source tee architecture for concurrent JPEG preview/inference and hardware H.264 recording.
- Adds synchronous NULL-state cleanup when QMMF startup fails so a failed attempt does not leave a half-open RecorderClient session.
- Adds startup bus error extraction for more actionable `/api/edge/status` diagnostics.
- Retains edge.6 behavior: on Qualcomm devices, QTI acquisition failures do not fall through to misleading `/dev/video0` OpenCV errors.

---

# VisionEdge 2.1.0-edge.6

## QTI camera acquisition / startup diagnostics

- Changed `backend=auto` behavior on Qualcomm devices: when `qtiqmmfsrc` exists, QTI is authoritative. A QTI camera acquisition failure no longer falls through to OpenCV `/dev/video0`.
- OpenCV fallback remains automatic on PC/development hosts where the QTI plugin is absent.
- QTI start failures now surface a clear hint that camera 0 may already be owned by another VisionEdge/SmartCam process or service.
- This removes the misleading V4L2/obsensor warning flood seen after Qualcomm `StartCamera` fails and keeps the original camera error visible in `/api/edge/status`.

---

# VisionEdge 2.1.0-edge.5

## UX / defaults / recording timing

- Removed the one-option **Frame rate** control from Settings; camera sensor target remains internally validated at `30/1`.
- Replaced free-form Inference rate with **AI Analysis Rate** presets: 2 / 5 / 10 FPS; default is **Balanced · 5 FPS**.
- AI Analysis Rate is disabled in camera-only mode and Live status explicitly shows measured **actual** AI FPS.
- Recording bitrate defaults to **Automatic**: 8 Mbps at 1080p and 16 Mbps at 4K, following SmartCam's capture policy.
- Storage safety reserve default increased from 1 GB to **10 GB**, matching the supplied SmartCam safeguard.
- Main Live status now prioritizes Camera / Recording / Inspection / Storage; technical backend/frame diagnostics moved to Developer settings.
- Stop Camera and Apply Settings are guarded while a recording is active.
- Active recording files cannot be opened/downloaded/deleted until finalized.
- SOP actions are disabled when no inspection product is active.
- Settings now track unsaved changes and only enable Apply / Discard when needed.
- Result-overlay software recording uses measured source FPS to avoid playback-speed mismatch when appsink delivers less than the 30 FPS sensor target.
- Cleaned mixed terminology and language in the Edge operator UI.

---

# VisionEdge 2.1.0-edge.4

## UX review / changes

- Removed the ambiguous **View & snapshot** control group. Live-view source selection now belongs in the Live Camera header; Snapshot is one action that captures the currently displayed source.
- `Raw` is the default display. `AI Result` is disabled while Product 0 / Preview-only mode is active.
- Replaced independent Width / Height text inputs with a single **Resolution** select, matching the SmartCam Capture UX. Known sensor modes exposed in the UI are `1920 × 1080` and `3840 × 2160`; an existing non-standard configured mode is preserved as a read-only-style current option instead of being silently changed.
- Reorganized the app into **Live / Media / Settings** pages. Operational controls no longer share the same page with engineering configuration.
- Settings are grouped as **Camera**, **AI & Inspection**, **Recording & Storage**, and **Developer / Compatibility**.
- Replaced raw bitrate bytes and storage MB entry with user-facing Mbps / GB fields.
- Replaced separate Save / Save + Restart buttons with one **Apply Settings** action. If the camera is running it is restarted automatically; if stopped, settings are saved without starting it.
- OpenCV Source is shown only when the OpenCV fallback backend is selected.
- Media manager remains a dedicated page and keeps the deletion consistency fixes from edge.3.

---

# VisionEdge 2.1.0-edge.3

## Fixed / changed

- Project/product renamed to **VisionEdge**.
- Added direct HTTPS configuration using the certificate/key mechanism from `smartcam_webserver`.
- Default endpoint is `https://<camera>:8080/edge`.
- Added TLS + port preflight before Qualcomm camera auto-start.
- Redesigned Edge UI so Camera and Recording each have one stateful toggle.
- Added visible LIVE / REC / AI states, recording timer, recording path, disabled invalid actions, and a cleaner layout.
- Reworked Local Media into a dedicated manager with All / Recordings / Photos views.
- Fixed stale deleted-file display using no-cache responses + `media_version` refresh signaling.
- Added verified server-side deletion and an active-recording delete guard.
- Added POST `/api/edge/media/delete`; legacy DELETE route remains supported.
- Config saves now preserve `[server]` HTTPS settings instead of overwriting the entire INI.
- Added `visionedge_server.py`, `visionedge.service`, and `run_visionedge.sh`.
- Added media/TLS regression test.

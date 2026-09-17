# VisionEdge UI review — edge.5

## Changes validated and applied

1. **Frame rate was not a meaningful user setting.**
   - The UI exposed a 30 FPS select with only one choice.
   - SmartCam keeps the validated 30/1 sensor rate in configuration rather than exposing a one-choice control.
   - VisionEdge now keeps 30 FPS internal and removes the field from normal Settings.

2. **AI analysis rate is useful, but the old free-form 2 FPS default was too engineering-oriented.**
   - Default is now **Balanced · 5 FPS**.
   - The UI offers Low load 2 FPS / Balanced 5 FPS / Fast 10 FPS.
   - The field is disabled when no inspection product is selected.
   - Live status labels measured AI FPS as **actual**, so target vs measured rate is clear.

3. **Raw bitrate and storage numbers were too low-level for the normal Settings page.**
   - Recording bitrate defaults to **Automatic**, following SmartCam: 8 Mbps at 1080p and 16 Mbps at 4K.
   - Storage reserve defaults to **10 GB**, matching SmartCam's rootfs safeguard.
   - Both are presented as clear presets rather than byte/MB text input.

4. **Live status exposed engineering diagnostics instead of operator information.**
   - Removed Frame Seq and Backend from the main operational status.
   - Main status now emphasizes Camera, Recording, Inspection and Storage.
   - Backend / hardware recording / frame sequence moved to Developer diagnostics.

5. **Unsafe/ambiguous actions were still possible.**
   - Stop Camera is disabled while recording.
   - Apply Settings is blocked while recording.
   - Active recordings cannot be opened/downloaded/deleted until finalized.
   - SOP actions are disabled when no inspection product is active.

6. **Settings did not communicate unsaved state.**
   - Apply / Discard are disabled until a real change is made.
   - Settings tab shows a dot when changes are pending.
   - Leaving the browser with unsaved settings triggers the standard browser warning.

7. **Software result recording could play too fast.**
   - Result/overlay recording now uses the measured received-frame FPS (for example ~24 FPS) instead of always writing a 30 FPS container.

8. **Language and labels were inconsistent.**
   - Operator UI terminology is now consistent: Camera only, AI Overlay, Inspection, Stream FPS actual, AI FPS actual.
   - Mixed Chinese fallback messages inside the otherwise-English Edge UI were removed.

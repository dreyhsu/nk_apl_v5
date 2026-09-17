# VisionEdge UI review — edge.4

## Validated issues

1. **“View & snapshot” mixed two different concepts.**
   - Raw / AI Result selects the live display source.
   - Snapshot is an action against that source.
   - In Preview-only (Product 0), AI Result is not a meaningful choice.

   **Change:** display selector moved to the Live Camera header; there is one Snapshot button that captures the current view. AI Result is disabled in Product 0.

2. **Independent Width / Height text fields were inappropriate for a camera sensor mode.**
   - The supplied SmartCam Capture UI uses a Resolution select.
   - The supplied SmartCam choices are 1920×1080 and 3840×2160.

   **Change:** VisionEdge now uses a Resolution select with those modes. A pre-existing non-standard INI value is preserved as the current option rather than overwritten.

3. **Configuration was mixed into the operational dashboard.**
   - The old bottom “Advanced Configuration” exposed deployment parameters next to everyday camera controls.
   - Separate Save and Save + Restart actions forced the operator to understand implementation details.

   **Change:** Live / Media / Settings are separate pages. Settings are grouped into Camera, AI & Inspection, Recording & Storage, and Developer / Compatibility. Apply Settings automatically restarts the camera only if it is already running.

# Logic and Bug Review

## High-severity issues found in the first build and corrected in 1.1

1. Standalone SOP config loaded `/tm-app` firmware defaults and could call the wrong API path on a PC deployment.
2. Offline SOP summary used wall-clock time after virtual video-timeline updates, causing incorrect or negative elapsed time.
3. NG-only groups could complete a SOP step even though no positive visual evidence was seen.
4. Skip alarms were raised on a single later-step frame and could also be raised by an Optional Step.
5. Alarm Latch was cleared automatically when all steps later became DONE, allowing an alarmed unit to become PASS without acknowledgement.
6. Non-latched steps were never actually returned to PENDING.
7. A long stream gap could be counted as continuous hold time.
8. A local VIDEO stream was decoded as fast as possible; a two-minute file could be consumed almost instantly.
9. A non-loop VIDEO source was treated like a disconnected RTSP source and reopened indefinitely.
10. Saving or adding one stream restarted every stream and reset unrelated SOP sessions.
11. Browser alarm sound had no explicit user activation and could be blocked by autoplay policy.
12. Dashboard polling returned every camera image as Base64 inside one JSON payload, creating poor multi-camera scaling.
13. SOP alarm acknowledgement had no runtime API or dashboard button.
14. Multi-stream SOP sessions were not persisted to `sop_run_logs`.
15. SQLite foreign-key cascades were declared but not enabled; concurrent workers also lacked WAL/busy timeout settings.
16. PC labeling transform settings were not consistently applied to multi-stream/offline runtime frames.
17. The annotation preview and multi-stream runtime used separate matcher implementations, risking different results.
18. The SOP page claimed that three-color-light control existed even though the PC build had no hardware output implementation.

## Remaining design limitations

1. Every stream owns an independent SOP session. Cross-camera fusion is not implemented.
2. No operator ID, product serial-number scan, MES/SFIS gate, or role-based access control is included.
3. Template Matching remains sensitive to scale, perspective, theme, font, and UI layout changes.
4. Incoming frames are normalized to the product reference resolution; a large aspect-ratio/viewpoint difference can still distort the image and reduce accuracy.
5. OpenCV RTSP timeout support depends on the installed OpenCV/FFmpeg backend.
6. `vision_core.py` still contains inherited Smart Camera/ZLAN/GStreamer code that is unused by this PC edition; it should eventually be split into a smaller reusable core package.
7. The API requires deployment behind an authenticated reverse proxy before use outside a trusted production network.

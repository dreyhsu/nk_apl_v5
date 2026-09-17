@echo off
setlocal
cd /d "%~dp0"
echo === Python resolution ===
where python
where python3 2>nul
echo.
echo === Versions ===
python --version
python3 --version 2>nul
echo.
echo === Import check ===
python -c "import sys; print('Executable:', sys.executable); import flask, cv2, numpy; print('Flask: OK'); print('OpenCV:', cv2.__version__); print('NumPy:', numpy.__version__)"
echo.
echo === VisionEdge import check ===
python -c "import visionedge_server as v; print('VisionEdge import OK'); print('backend requested =', v.EDGE.cfg.backend); print('source =', v.EDGE.cfg.source)"
echo.
echo === TLS certificate/key check ===
python -c "import ssl; c=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); c.load_cert_chain('config/certs/CRS0000000616.cert','config/certs/CRS0000000616.key'); print('TLS cert/key OK')"
echo.
pause

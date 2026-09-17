#!/usr/bin/env python3
"""Headless Chromium regression test for Video Label playback.

The test injects video_label.html with CDP, selects a generated moving WebM,
verifies that the real <video> becomes visible while playing, time advances,
and pausing captures the current frame back to the labeling canvas.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def main() -> int:
    try:
        import websocket
    except ImportError:
        print('SKIP: websocket-client is not installed')
        return 0

    chromium = shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
    ffmpeg = shutil.which('ffmpeg')
    if not chromium or not ffmpeg:
        print('SKIP: Chromium or ffmpeg is not installed')
        return 0

    root = Path(__file__).resolve().parents[2]
    html = (root / 'static' / 'video_label.html').read_text(encoding='utf-8')
    work = Path(tempfile.mkdtemp(prefix='tm-video-label-test-'))
    video = work / 'moving.webm'
    profile = work / 'chrome-profile'
    subprocess.run(
        [
            ffmpeg, '-y', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc=size=640x360:rate=30', '-t', '4',
            '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuv420p', str(video),
        ],
        check=True,
    )

    chrome = subprocess.Popen(
        [
            chromium, '--headless=new', '--no-sandbox', '--disable-gpu',
            '--autoplay-policy=no-user-gesture-required', '--remote-allow-origins=*',
            '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    ws_browser = ''
    assert chrome.stderr is not None
    deadline = time.time() + 10
    while time.time() < deadline:
        line = chrome.stderr.readline()
        if 'DevTools listening on' in line:
            ws_browser = line.strip().split('DevTools listening on ', 1)[1]
            break
        if not line:
            time.sleep(0.05)
    if not ws_browser:
        chrome.terminate()
        raise RuntimeError('Chromium DevTools endpoint did not start')

    ws = websocket.create_connection(ws_browser, origin='http://localhost')
    sequence = 1

    def call(method: str, params: dict | None = None, session: str | None = None) -> dict:
        nonlocal sequence
        request_id = sequence
        sequence += 1
        message = {'id': request_id, 'method': method}
        if params is not None:
            message['params'] = params
        if session:
            message['sessionId'] = session
        ws.send(json.dumps(message))
        while True:
            response = json.loads(ws.recv())
            if response.get('id') == request_id:
                if 'error' in response:
                    raise RuntimeError(response['error'])
                return response.get('result', {})

    try:
        target = call('Target.createTarget', {'url': 'about:blank'})['targetId']
        session = call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        for method in ('Runtime.enable', 'Page.enable', 'DOM.enable'):
            call(method, session=session)
        frame_id = call('Page.getFrameTree', session=session)['frameTree']['frame']['id']
        call('Page.setDocumentContent', {'frameId': frame_id, 'html': html}, session)
        time.sleep(1.2)

        def evaluate(expression: str, await_promise: bool = False):
            result = call(
                'Runtime.evaluate',
                {'expression': expression, 'returnByValue': True, 'awaitPromise': await_promise},
                session,
            ).get('result', {})
            if result.get('subtype') == 'error':
                raise RuntimeError(result.get('description', 'JavaScript error'))
            return result.get('value')

        evaluate(
            "S.products=[{id:1,serial:'TEST-1',name:'Test Product'}];"
            "renderProductList();refreshInferList();"
            "document.querySelector('.product-item').click();"
            "document.getElementById('editModeBtn').click();"
        )
        document = call('DOM.getDocument', session=session)['root']['nodeId']
        node = call('DOM.querySelector', {'nodeId': document, 'selector': '#mediaInput'}, session)['nodeId']
        if not node:
            raise RuntimeError('mediaInput was not found')
        call('DOM.setFileInputFiles', {'nodeId': node, 'files': [str(video)]}, session)
        time.sleep(2)

        loaded = evaluate(
            "({ready:S.lblVideoReady,src:!!S.srcB64,duration:LV.duration,"
            "video:LV.style.display,canvas:MC.style.display})"
        )
        evaluate('toggleLabelPlay()', True)
        time.sleep(1.2)
        playing = evaluate(
            "({paused:LV.paused,time:LV.currentTime,video:LV.style.display,"
            "canvas:MC.style.display,seek:Number(document.getElementById('videoSeek').value)})"
        )
        evaluate('toggleLabelPlay()', True)
        time.sleep(0.5)
        paused = evaluate(
            "({paused:LV.paused,video:LV.style.display,canvas:MC.style.display,"
            "srcLen:(S.srcB64||'').length,imgLoaded:S.imgLoaded})"
        )

        assert loaded['ready'] and loaded['src'] and loaded['duration'] > 3
        assert not playing['paused'] and playing['time'] > 0.5
        assert playing['video'] == 'block' and playing['canvas'] == 'none'
        assert paused['paused'] and paused['video'] == 'none' and paused['canvas'] == 'block'
        assert paused['imgLoaded'] and paused['srcLen'] > 1000
        print('Video Label Chromium E2E: PASS')
        return 0
    finally:
        try:
            ws.close()
        except Exception:
            pass
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())

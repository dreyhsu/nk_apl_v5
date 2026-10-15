"""Static operator UI checks that do not require the Flask runtime."""

import shutil
import subprocess
import unittest
from pathlib import Path


class OperatorInputUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_release_status_controls_execute_without_a_browser(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        functions = html.split('function stateClass(status)', 1)[1].split('function stateLabel', 1)[0]
        script = '''
const assert=require('node:assert/strict');
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{style:{},classList:{toggle(){},add(){},remove(){}}});return nodes.get(id)};
let lastStatus={},cameraBusy=false,recordBusy=false,viewMode='raw',recSince=null,lastWarning='';
const fmtDuration=()=>'',fmtGb=()=>'',renderFlow=()=>{},renderStation=()=>{},renderStreamStatus=()=>{},updateSettingsActionState=()=>{},message=()=>{},updateSnapshotLabel=()=>{},reloadStream=()=>{};
''' + 'function stateClass(status)' + functions + '''
renderStatus({status:'RELEASE_FAILED',running:true,has_raw:true});
assert.equal(stateClass('RELEASE_FAILED'),'bad');
assert.equal($('cameraToggle').disabled,true);
assert.equal($('snapshotBtn').disabled,true);
assert.equal($('recordToggle').disabled,true);
assert.match($('cameraHint').textContent,/unconfirmed/);
renderStatus({status:'RELEASE_FAILED',running:false,reopen_available:true});
assert.equal($('cameraToggle').disabled,false);
assert.match($('cameraToggle').textContent,/Recover Camera/);
assert.equal($('snapshotBtn').disabled,true);
assert.equal($('recordToggle').disabled,true);
renderStatus({status:'STOPPING',running:true,has_raw:true});
assert.equal($('snapshotBtn').disabled,true);
renderStatus({status:'LIVE',running:true,has_raw:true});
assert.equal($('cameraToggle').disabled,false);
assert.equal($('snapshotBtn').disabled,false);
assert.equal($('recordToggle').disabled,false);
renderStatus({status:'STARTING',running:true});
assert.equal($('cameraToggle').disabled,false);
assert.equal($('cameraToggle').textContent,'Cancel Camera Start');
renderStatus({status:'LIVE',running:true,has_raw:true,recording:true});
assert.equal($('cameraToggle').disabled,false);
renderStatus({status:'LIVE',running:true,has_raw:true,recording_state:'FINALIZING'});
assert.equal($('recordToggle').disabled,true);
assert.equal($('recordState').textContent,'FINALIZING');
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_camera_release_disables_camera_controls(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        self.assertIn("status==='RELEASE_FAILED'||status==='ALARM')return'bad'", html)
        self.assertIn("cameraUnavailable=releaseFailed||status==='STOPPING'", html)
        self.assertIn("camBtn.disabled=cameraBusy||(cameraUnavailable&&!d.reopen_available)", html)
        self.assertIn("$('snapshotBtn').disabled=!hasFrame||cameraBusy||cameraUnavailable", html)
        self.assertIn('recBtn.disabled=!running||!hasFrame||recordBusy||cameraUnavailable', html)

    def test_station_errors_do_not_disable_camera_stop(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn("station?.status_stale!==true", script)
        self.assertIn("if((!fresh||activeCycle)&&!lastStatus.running&&lastStatus.status!=='STARTING')", script)
        self.assertNotIn("if(!fresh||activeCycle)$('cameraToggle').disabled=true", script)

    def test_sn_input_is_read_only_and_ime_mutations_are_blocked(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="snInput"', html)
        self.assertIn('readonly disabled', html)
        self.assertIn("'beforeinput','compositionstart','compositionupdate','compositionend','drop'", script)
        self.assertIn("attachFixedScanner('snInput')", script)
        self.assertIn("attachFixedScanner('mcioInput')", script)
        self.assertIn("/^[\\x21-\\x7e]+$/", script)

    def test_operator_employee_number_field_is_rendered_in_every_mode(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="operatorSignIn"', html)
        self.assertIn("$('operatorSignIn').classList.toggle('hidden',signedIn)", script)
        self.assertNotIn('showSignIn', script)
        self.assertIn("const sifiBypass=station?.dmz?.bypass===true", script)

    def test_video_ui_uses_local_browser_playback_and_canvas_capture(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('accept=".mp4,video/mp4"', html)
        self.assertIn('id="testVideo"', html)
        self.assertIn('id="testFrameCanvas"', html)
        self.assertIn('file stays on this PC', html)
        self.assertIn('URL.createObjectURL(file)', script)
        self.assertIn('context.drawImage(video', script)
        self.assertIn("canvas.toBlob(resolve,'image/png')", script)
        self.assertIn('/api/apl/capture-inspect-frame', script)
        self.assertIn('data.attempt?.result_image', script)
        self.assertNotIn('data.attempt?.result_path', script)
        self.assertIn('textValue(scanner.last_error)', script)
        self.assertNotIn("data.append('video',file)", script)
        self.assertNotIn('/video-test/control', script)
        self.assertIn('id="videoTestToolbar" class="inputToolbar hidden"', html)
        self.assertIn('apl_ui.js?v=20261015-4-station-display', html)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_cached_station_status_does_not_flash_connecting_after_first_connection(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        station_fresh = source.split('function stationFresh(){', 1)[1].split('\n', 1)[0]
        connection_state = source.split('function stationConnectionState(station){', 1)[1].split('\n}', 1)[0]
        script = '''
const assert=require('node:assert/strict');
let lastStatusAt=Date.now(),statusPollError='',statusPollFailureCount=0,stationConnectedOnce=false;
''' + 'function stationFresh(){' + station_fresh + '\nfunction stationConnectionState(station){' + connection_state + '\n}' + '''
let state=stationConnectionState({status_stale:true});
assert.equal(state.hidden,false);assert.equal(state.displayStation,false);assert.equal(state.text,'Connecting to station...');
state=stationConnectionState({status_stale:false});
assert.equal(state.hidden,true);assert.equal(state.displayStation,true);assert.equal(stationConnectedOnce,true);
state=stationConnectionState({status_stale:true});
assert.equal(state.hidden,true);assert.equal(state.displayStation,true);assert.equal(state.freshStation,false);
statusPollError='Station status request timed out';
statusPollFailureCount=1;
state=stationConnectionState({status_stale:true});
assert.equal(state.hidden,true);assert.equal(state.displayStation,true);assert.equal(state.freshStation,false);
statusPollFailureCount=2;
state=stationConnectionState({status_stale:true});
assert.equal(state.hidden,false);assert.equal(state.text,'Connection lost. Reconnecting...');
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_status_poll_requires_repeated_failures_before_ui_warning(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        poll = html.split('async function pollOnce(){', 1)[1].split('\nasync function pollLoop()', 1)[0]
        script = '''
const assert=require('node:assert/strict');
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id)};
let statusPollBusy=false,statusPollFailureCount=0,statusPollError='',lastStatusAt=0;
let lastStatus={station:{state:'WAITING_SN'}},mediaVersion=0,lastMediaRefresh=0;
const A='/api/edge',events=[];
const responses=[new Error('Failed to fetch'),new Error('Failed to fetch'),{status:'LIVE',station:{state:'WAITING_SN'},media_version:0}];
async function req(){const next=responses.shift();if(next instanceof Error)throw next;return next}
function renderStatus(d){lastStatus=d;$('topCamera').textContent=d.status}
function syncStream(){}
function renderStation(){}
function renderStreamStatus(){}
function loadMedia(){}
function diagnosticVideoEvent(event,reason){events.push({event,reason})}
''' + 'async function pollOnce(){' + poll + '''
(async()=>{
  await pollOnce();
  assert.equal(statusPollFailureCount,1);
  assert.notEqual($('topCamera').textContent,'API ERROR');
  await pollOnce();
  assert.equal(statusPollFailureCount,2);
  assert.equal($('topCamera').textContent,'API ERROR');
  await pollOnce();
  assert.equal(statusPollFailureCount,0);
  assert.equal(statusPollError,'');
  assert.equal($('topCamera').textContent,'LIVE');
  assert.deepEqual(events.map(x=>x.event),['STATUS_POLL_FAILURE','STATUS_POLL_FAILURE','STATUS_POLL_RECOVERED']);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_cached_station_presentation_keeps_labels_but_guards_controls(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        station_fresh = source.split('function stationFresh(){', 1)[1].split('\n', 1)[0]
        connection_state = source.split('function stationConnectionState(station){', 1)[1].split('\n}', 1)[0]
        render_station = source.split('function renderStation(station){', 1)[1].split('\n}', 1)[0]
        script = '''
const assert=require('node:assert/strict');
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{classList:{toggle(name,on){this[name]=on}},style:{}});return nodes.get(id)};
let lastStatusAt=Date.now(),statusPollError='',statusPollFailureCount=0,stationConnectedOnce=false;
let stationBusy=false,stationLocalError='',autoCommitSeen='',step4Recovery='';
let fixedBuffers={snInput:'',mcioInput:''},scanFieldError={snInput:'',mcioInput:''};
let lastStatus={running:true,status:'LIVE',has_raw:true,auto_capture:{enabled:true,armed:true,score:.9,reason:'searching'}};
const STATION_LABELS={WAITING_CAPTURE:'Ready to inspect'},STATION_HINTS_AUTO={},STATION_HINTS={};
const STEP1_RETRY_LABEL='',STEP1_RETRY_HINT='';
let stepVisible=false;
function logStationState(){}function pollSfisLog(){}function message(){}
function clearScanBuffers(){}function scanPlaceholder(s){return s}function scanHint(){return ''}
function browserVideoActive(){return false}function renderStepFlow(s,visible){stepVisible=visible}
function autoCaptureText(){return 'Automatic inspection running'}
function textValue(v){return String(v||'')}function fmtScore(v){return String(v)}
function renderScannerSelection(){}function renderInput(){}function renderTemperature(){}
''' + 'function stationFresh(){' + station_fresh + '\nfunction stationConnectionState(station){' + connection_state + '\n}\nfunction renderStation(station){' + render_station + '\n}' + '''
const station={state:'WAITING_CAPTURE',operator:'OP',operator_required:true,can_capture:true,can_terminate:true,
  sn:'SN',mcio_sn:'MCIO',scanner:{enabled:false},dmz:{configured:true,step4_enabled:true},matching:{stable:'MATCH',raw:'MATCH'}};
$('verdict').textContent='READY';$('flowPct').textContent='50%';
renderStation(station);
assert.equal($('stationState').textContent,'Ready to inspect');
assert.equal($('captureInspect').disabled,false);
assert.equal($('autoCaptureStatus').textContent,'Automatic inspection running');
const stable=$('stableLabel').textContent;
station.status_stale=true;lastStatus.auto_capture.armed=false;
renderStation(station);
assert.equal($('connectionStrip').classList.hidden,true);
assert.equal($('stationState').textContent,'Ready to inspect');
assert.equal($('verdict').textContent,'READY');
assert.equal($('flowPct').textContent,'50%');
assert.equal($('stableLabel').textContent,stable);
assert.equal($('autoCaptureStatus').textContent,'Automatic inspection running');
assert.equal(stepVisible,true);
assert.equal($('captureInspect').disabled,true);
assert.equal($('terminateInspection').disabled,true);
statusPollError='Failed to fetch';statusPollFailureCount=1;
renderStation(station);
assert.equal($('connectionStrip').classList.hidden,true);
statusPollFailureCount=2;
renderStation(station);
assert.equal($('connectionStrip').classList.hidden,false);
assert.equal($('stationState').textContent,'Ready to inspect');
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_station_stale_details_are_kept_in_diagnostics(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('station_status_stale:lastStatus.station?.status_stale===true', script)
        self.assertIn("station_status_stale_reason:lastStatus.station?.status_stale_reason||''", script)
        self.assertIn('station_snapshot_age_ms:lastStatus.station?.status_snapshot_age_ms??null', script)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_transient_status_failure_keeps_existing_webrtc_session(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        station_fresh = source.split('function stationFresh(){', 1)[1].split('\n', 1)[0]
        hold_stream = source.split('function holdWebRTCOnStaleStatus(){', 1)[1].split('\n}', 1)[0]
        wanted_key = source.split('function wantedStreamKey(){', 1)[1].split('\n}', 1)[0]
        sync_stream = source.split('function syncStream(force=false){', 1)[1].split('\n}', 1)[0]
        script = '''
const assert=require('node:assert/strict');
let lastStatusAt=Date.now(),statusPollError='',lastStatus={running:true,backend:'qti',stream:{enabled:true}};
let streamKey='webrtc:http://station/api/stream/whep',streamPC={connectionState:'connected'},viewMode='result',streamDecodedAt=0;
let streamRetryAt=0,streamMessage='',closeCount=0;
function browserVideoActive(){return false}
function localStreamURL(){return 'http://station/api/stream/whep'}
function closeStream(){closeCount++}
function renderStreamStatus(){}
''' + 'function stationFresh(){' + station_fresh + '\nfunction holdWebRTCOnStaleStatus(){' + hold_stream + '\n}' + 'function wantedStreamKey(){' + wanted_key + '\n}' + 'function syncStream(force=false){' + sync_stream + '\n}' + '''
assert.equal(wantedStreamKey(),streamKey);
statusPollError='Failed to fetch';lastStatusAt=Date.now()-6000;
assert.equal(wantedStreamKey(),streamKey);
syncStream();assert.equal(closeCount,0);
statusPollError='';
assert.equal(wantedStreamKey(),streamKey);
lastStatusAt=Date.now()-16000;
streamDecodedAt=Date.now()-1000;
assert.equal(wantedStreamKey(),streamKey);
syncStream();assert.equal(closeCount,0);
streamDecodedAt=Date.now()-6000;
assert.equal(wantedStreamKey(),'');
syncStream();assert.equal(closeCount,1);
lastStatusAt=Date.now()-6000;streamPC.connectionState='failed';
assert.equal(wantedStreamKey(),'');
streamPC.connectionState='connected';lastStatus.running=false;
assert.equal(wantedStreamKey(),'');
lastStatus.running=true;streamPC=null;
assert.equal(wantedStreamKey(),'');
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_live_webrtc_video_is_not_hidden_by_stale_status(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        station_fresh = source.split('function stationFresh(){', 1)[1].split('\n', 1)[0]
        render_status = source.split('function renderStreamStatus(){', 1)[1].split('\n}', 1)[0]
        script = '''
const assert=require('node:assert/strict');
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{style:{},classList:{toggle(){}},getAttribute(){return ''}});return nodes.get(id)};
let lastStatusAt=Date.now()-7000,statusPollError='Failed to fetch';
let lastStatus={running:true,status:'LIVE',frame_seq:100},streamFrameSeq=100,streamFrameAt=Date.now()-10000;
let streamKind='webrtc',streamDecodedAt=Date.now()-1000,streamReady=true,streamMessage='Connected';
let browserVideoReady=false,videoChoice=null,viewMode='result';
''' + 'function stationFresh(){' + station_fresh + '\nfunction renderStreamStatus(){' + render_status + '\n}' + '''
renderStreamStatus();
assert.equal($('liveVideo').style.display,'block');
streamDecodedAt=Date.now()-6000;
renderStreamStatus();
assert.equal($('liveVideo').style.display,'none');
lastStatus.running=false;streamDecodedAt=Date.now()-1000;
renderStreamStatus();
assert.equal($('liveVideo').style.display,'none');
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scanner_search_is_collapsed_and_operator_gate_clears_sn(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('<details id="scannerSettings"', html)
        self.assertIn('id="scannerRefresh"', html)
        self.assertIn('id="scannerApply"', html)
        self.assertIn("if(operatorRequired&&!signedIn&&(fixedBuffers.snInput||fixedBuffers.mcioInput))clearScanBuffers()", script)
        self.assertIn('function refreshScannerDevices()', script)
        self.assertIn('function saveScannerSelection()', script)

    def test_active_browser_video_can_be_reattached_after_scanning(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn("const VIDEO_TEST_STATES=['WAITING_SN','WAITING_MCIO_SN','WAITING_CAPTURE','NOT_MATCHED']", script)
        self.assertIn('VIDEO_TEST_STATES.includes(stationState)', script)
        self.assertIn("$('videoFile').disabled=!canAttachVideo", script)
        self.assertIn("browserActive&&videoChoice?'Replace Test Video':'Choose Test Video'", script)
        self.assertIn("const replacing=browserVideoActive()", script)
        self.assertIn("A+'/browser-video/start'", script)
        self.assertIn('Test video attached to the current inspection.', script)
        self.assertIn('id="videoBack"', html)
        self.assertIn('id="videoForward"', html)
        self.assertIn('skipBrowserVideo(-5)', html)
        self.assertIn('skipBrowserVideo(5)', html)
        self.assertIn('function updateVideoProgress', script)

    def test_video_test_feature_gate_hides_ui_and_guards_active_sn_start(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn("lastStatus.features?.video_test_enabled===true", script)
        self.assertIn("$('videoTestToolbar').classList.toggle('hidden',!videoTestEnabled)", script)
        self.assertIn("const canStartVideo=videoTestEnabled", script)
        self.assertIn("Browser video test started for the current SN.", script)


    def test_mcio_card_serial_number_has_its_own_guarded_scan_field(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="mcioInput"', html)
        # The MCIO field must carry the same IME and keyboard-layout guards as the SN field.
        self.assertEqual(html.count('inputmode="none"'), 2)
        self.assertEqual(html.count('readonly disabled'), 2)
        self.assertIn("attachFixedScanner('mcioInput')", script)
        self.assertIn("mcioInput:{field:'mcio_sn'", script)
        self.assertIn("WAITING_MCIO_SN:'Scan MCIO Card SN'", script)
        self.assertIn("WAITING_MCIO_SN:'Product serial number accepted by SFIS Step1.", script)
        self.assertIn("expect:spec.field", script)
        # The Step1 retry surface is driven by the backend flag, not re-derived.
        self.assertIn("step1_retry_required", script)
        self.assertIn("STEP1_RETRY_LABEL", script)
        self.assertIn("active_scan_field", script)
        self.assertNotIn("rolled_back", script)
        # The Step1 reason is rendered on the Employee-number (step 2) row.
        self.assertIn('id="step2Error"', html)
        self.assertIn("step2Error", script)

    def test_the_employee_number_is_never_gated_on_a_station_state(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        # Both the button's disabled flag AND the click handlers must be free of
        # a state gate, or Switch operator renders enabled and silently no-ops
        # exactly when it is needed most: a NO EMP Step1 rejection.
        self.assertIn("const canEditOperator=freshStation&&!busy;", script)
        for action in ('submitOperator', 'switchOperator'):
            body = script.split('async function %s(){' % action, 1)[1].split('\n}', 1)[0]
            self.assertIn("if(!stationFresh()||stationBusy||station?.request_in_flight)return;", body)
            self.assertNotIn("WAITING_SN", body)

    def test_guided_steps_dim_without_disabling_terminate(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        for step in range(1, 6):
            self.assertIn('id="step%d"' % step, html)
        # Terminate sits outside the step list so a dimmed step cannot hide it.
        footer = html.split('<div class="stationFooterActions">')[1]
        self.assertIn('id="terminateInspection"', footer)
        # Dimming must stay visual: a pending step still has to accept clicks.
        self.assertNotIn('.stepRow.pending{opacity:.45;pointer-events:none}', html)
        self.assertIn('.stepRow.pending{opacity:.45}', html)
        self.assertIn('function stepStatuses(station,displayStation)', script)
        # A visible step 5 always means unfinished: a successful cycle resets them all.
        self.assertIn('5:false};', script)

    def test_a_pass_sends_step4_and_hands_the_station_back_automatically(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('async function sendStep4(station)', script)
        self.assertIn("else if(lastStatus.station?.can_confirm_result===true){", script)
        self.assertIn('await sendStep4(lastStatus.station)', script)
        self.assertIn("{'confirm-result':'can_confirm_result',terminate:'can_terminate'}", script)
        # The next round starts by itself: buffers cleared, focus back on the SN field.
        self.assertIn("if(data.station?.state==='WAITING_SN')clearScanBuffers();", script)
        self.assertIn("finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);syncScanFocus(lastStatus.station,true)}\n}\n\nasync function toggleBrowserVideo", script)
        self.assertIn("Ready for the next serial number.", script)
        self.assertIn('id="retryStep4"', html)

    def test_automatic_capture_shows_live_feedback_and_closes_the_cycle(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        # A server-side capture never runs captureInspection, so the poll path sends Step4.
        self.assertIn('async function maybeAutoStep4()', script)
        self.assertIn('if(autoStep4Cycle===station.cycle_id)return;', script)
        self.assertIn('maybeAutoStep4()', script)
        self.assertIn('function autoCaptureText(auto)', script)
        self.assertIn('const STATION_HINTS_AUTO=', script)
        self.assertIn('lastStatus.auto_capture', script)
        self.assertIn('Auto-inspecting\u2026', script)
        # The match labels follow the live probe instead of sitting on UNKNOWN.
        self.assertIn("autoLive?auto2.reason||'searching'", script)
        self.assertIn('id="autoCaptureStatus"', html)
        self.assertIn('.autoCaptureHint:empty{display:none}', html)
        # One toast per automatic attempt, guarded by the committed attempt_id.
        self.assertIn('commit.attempt_id!==autoCommitSeen', script)
        self.assertIn("commit.trigger==='timeout'", script)
        # The toast still clears itself after 4.2 seconds; nobody touched it.
        self.assertIn("setTimeout(()=>x.className='toast',4200)", html)
        # An auto-passed unit never matched, so it is never announced as an OK.
        self.assertIn("commit.trigger==='timeout'&&commit.result==='PASS'", script)
        self.assertIn('Auto-passed on timeout', html)
        self.assertIn('auto-pass on timeout', script)
        self.assertIn('last_commit?.timeout_pass===true', script)
        self.assertIn('not visually matched', script)
        # The manual button stays an override and is never disabled by the probe.
        self.assertIn("$('captureInspect').disabled=!freshStation||busy||station.can_capture!==true||!captureReady;", script)

    def test_step4_failures_tell_the_operator_what_to_do(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="stationRecovery"', html)
        self.assertIn('.stationRecovery:empty{display:none}', html)
        self.assertIn('function step4RecoveryText(detail,timedOut)', script)
        self.assertIn('function applyStep4Failure(error)', script)
        # A timeout may still be in flight server-side, so retrying is wrong there.
        self.assertIn('Do not retry yet', script)
        self.assertIn('SFIS refused this unit. Retrying will not help.', script)
        self.assertIn('contact the web developer SMD (Dre Hsu / TW031561)', script)
        self.assertIn('Select Retry SFIS Step4. If it keeps failing', script)
        self.assertIn('business validation failed|rejected request|does not confirm PASS', script)
        # The retry button is hidden outside the failure case, not merely disabled.
        self.assertIn("$('retryStep4').classList.toggle('hidden',!awaitingStep4)", script)
        # The SFIS text arrives both in the response and in station.last_error.
        # During a Step1 retry the station error moves to the step-2 row, so it
        # is suppressed from the banner here (shown via step2Error instead).
        self.assertIn("[...new Set([stationLocalError,retry?'':textValue(station?.last_error)].filter(Boolean))]", script)
        self.assertIn("const awaitingStep4=freshStation&&station.state==='PASS_CONFIRM'&&station.can_confirm_result===true", script)

    def test_scan_focus_follows_step_transitions_only(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('if(!force&&step===lastScanStep)return;', script)
        self.assertIn("if(currentPage!=='live'||$(target).disabled)return;", script)
        self.assertIn("function clearScanBuffers(){setFixedValue('snInput','');setFixedValue('mcioInput','')}", script)


if __name__ == '__main__':
    unittest.main()

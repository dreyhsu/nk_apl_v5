'use strict';

let lastStatusAt=0,statusPollBusy=false,statusPollError='',stationBusy=false,stationLocalError='';
let streamGeneration=0,streamKey='',streamPC=null,streamLocation='',streamRequest=null;
let streamRetryAt=0,streamStartedAt=0,streamDecodedAt=0,streamDecodedFrames=0,streamFrameSeq=null,streamFrameAt=0;
let streamMessage='Waiting for video.',streamReady=false,streamKind='',streamStatsBusy=false;
const VIDEO_TEST_STATES=['WAITING_SN','WAITING_MCIO_SN','WAITING_CAPTURE','NOT_MATCHED'];
const STATION_LABELS={WAITING_SN:'Waiting for SN',WAITING_MCIO_SN:'Scan MCIO Card SN',SN_CONFIRM:'Validating with SFIS',WAITING_DMZ:'Validating with SFIS',WAITING_CAPTURE:'Ready to Capture',JUDGING:'Inspecting Image',NOT_MATCHED:'Not Matched',PASS_CONFIRM:'OK',UPLOADING:'Uploading',RECOVERY_REQUIRED:'Recovery Required'};
const STATION_HINTS={WAITING_SN:'Scan the next product serial number.',WAITING_MCIO_SN:'Product serial number accepted by SFIS Step1. Scan the MCIO card serial number.',SN_CONFIRM:'Sending SFIS Step1 for this product serial number.',WAITING_DMZ:'Waiting for the SFIS Step1 answer.',WAITING_CAPTURE:'Both serial numbers accepted. Close the cover and install the MCIO card, then select Capture & Inspect.',JUDGING:'Checking the captured image.',NOT_MATCHED:'The required cover-and-card condition was not matched. Adjust the product and capture again.',PASS_CONFIRM:'Inspection OK.',UPLOADING:'Sending SFIS Step4.',RECOVERY_REQUIRED:'The inspection was interrupted. Check the product, then terminate this inspection.'};
// SN_CONFIRM is momentary while Step1 is sent, but sticky when Step1 is refused;
// the backend's step1_retry_required flag picks these out for that sticky case.
const STEP1_RETRY_LABEL='SFIS Step1 Failed';
const STEP1_RETRY_HINT='SFIS did not accept this product serial number. Scan the same product serial number again to retry, or select Terminate Inspection. If the employee number was rejected, select Switch operator first, then sign in with the correct number.';
let videoChoice=null,videoBusy=false,videoSeeking=false,historyBusy=false;
let browserVideoURL='',browserVideoReady=false,browserCaptureTime=null;
const SCAN_FIELDS={snInput:{field:'sn',label:'product serial number',expect:'WAITING_SN'},mcioInput:{field:'mcio_sn',label:'MCIO card serial number',expect:'WAITING_MCIO_SN'}};
const fixedBuffers={snInput:'',mcioInput:''};
// Scan-label validation errors render in the field's own hint, not #stationError.
const scanFieldError={snInput:'',mcioInput:''};
// Set when a scan is submitted so the next keystroke starts a clean value instead
// of appending to a rejected one.
const scanResetPending={snInput:false,mcioInput:false};
let lastScanStep='';
let step4Recovery='';
let scannerSettingsBusy=false;

// What the operator should do when Step4 does not go through. The raw SFIS text
// stays in #stationError; this is the "what now" line next to it.
const STEP4_REFUSED_GUIDANCE='SFIS refused this unit. Retrying will not help. Select Terminate Inspection and handle the unit per the line SOP. The captured evidence is kept.';
const STEP4_CONFIG_GUIDANCE='This station cannot post Step4 with the current configuration. Terminate this inspection and contact the web developer SMD (Dre Hsu / TW031561).';
// A timeout leaves the station possibly still mid-request: _confirm refuses a
// second attempt while request_token is set, so retrying now only confuses.
const STEP4_TIMEOUT_GUIDANCE='The SFIS answer timed out. Do not retry yet — wait for the station status to settle. If it still shows OK, select Retry SFIS Step4.';
const STEP4_RETRY_GUIDANCE='SFIS did not answer. Select Retry SFIS Step4. If it keeps failing, select Terminate Inspection — the evidence and the same report ID are kept — and report the network outage.';
const STEP4_GUIDANCE=[
  [/business validation failed|rejected request|does not confirm PASS/i,STEP4_REFUSED_GUIDANCE],
  [/disabled by configuration|not configured|MCIO card serial number is missing|line, station, and employee number/i,STEP4_CONFIG_GUIDANCE],
];

function step4RecoveryText(detail,timedOut){
  if(timedOut)return STEP4_TIMEOUT_GUIDANCE;
  for(const [pattern,text] of STEP4_GUIDANCE)if(pattern.test(String(detail||'')))return text;
  return STEP4_RETRY_GUIDANCE;
}

function step4BlockedGuidance(station){
  if(station?.test_mode||browserVideoActive())return 'Browser test data is never uploaded to SFIS. Select Terminate Inspection to finish this test.';
  return STEP4_CONFIG_GUIDANCE;
}

function applyStep4Failure(error){
  const timedOut=error.name==='AbortError';
  stationLocalError=timedOut?'SFIS Step4 did not answer within 30 seconds.':error.message;
  step4Recovery=step4RecoveryText(error.message,timedOut);
}

const FIXED_DIGIT_KEYS={Digit1:['1','!'],Digit2:['2','@'],Digit3:['3','#'],Digit4:['4','$'],Digit5:['5','%'],Digit6:['6','^'],Digit7:['7','&'],Digit8:['8','*'],Digit9:['9','('],Digit0:['0',')']};
const FIXED_PUNCTUATION_KEYS={Minus:['-','_'],Equal:['=','+'],BracketLeft:['[','{'],BracketRight:[']','}'],Semicolon:[';',':'],Quote:["'",'"'],Backquote:['`','~'],Backslash:['\\','|'],Comma:[',','<'],Period:['.','>'],Slash:['/','?']};

function fixedScannerCharacter(event){
  const code=String(event.code||'');
  if(/^Key[A-Z]$/.test(code)){const letter=code.slice(3),caps=event.getModifierState&&event.getModifierState('CapsLock');return event.shiftKey!==!!caps?letter:letter.toLowerCase()}
  if(FIXED_DIGIT_KEYS[code])return FIXED_DIGIT_KEYS[code][event.shiftKey?1:0];
  if(FIXED_PUNCTUATION_KEYS[code])return FIXED_PUNCTUATION_KEYS[code][event.shiftKey?1:0];
  if(/^Numpad[0-9]$/.test(code))return code.slice(6);
  if(code==='NumpadDecimal')return '.';
  return null;
}

function fixedMaxLength(id){return Number($(id).maxLength||128)}

function replaceSNSelection(input,text){
  const start=input.selectionStart??input.value.length;
  const end=input.selectionEnd??start;
  input.setRangeText(text,start,end,'end');
  fixedBuffers[input.id]=input.value.slice(0,fixedMaxLength(input.id));
  input.value=fixedBuffers[input.id];
}

function setFixedValue(id,value){
  fixedBuffers[id]=String(value||'').slice(0,fixedMaxLength(id));
  $(id).value=fixedBuffers[id];
  scanResetPending[id]=false;scanFieldError[id]='';
}

function clearScanBuffers(){setFixedValue('snInput','');setFixedValue('mcioInput','')}

function captureFixedSNKey(id,event){
  const input=$(id);
  if(input.disabled)return;
  if(event.ctrlKey||event.altKey||event.metaKey)return;
  if(event.code==='Enter'||event.code==='NumpadEnter'){
    event.preventDefault();event.stopImmediatePropagation();scanResetPending[id]=true;submitScan(id);return;
  }
  if(event.code==='Backspace'){
    event.preventDefault();event.stopImmediatePropagation();
    const start=input.selectionStart??input.value.length;
    const end=input.selectionEnd??start;
    if(start!==end)input.setRangeText('',start,end,'end');
    else if(start>0)input.setRangeText('',start-1,start,'end');
    fixedBuffers[id]=input.value;
    return;
  }
  if(event.code==='Delete'){
    event.preventDefault();event.stopImmediatePropagation();
    const start=input.selectionStart??input.value.length;
    const end=input.selectionEnd??start;
    if(start!==end)input.setRangeText('',start,end,'end');
    else if(start<input.value.length)input.setRangeText('',start,start+1,'end');
    fixedBuffers[id]=input.value;
    return;
  }
  const character=fixedScannerCharacter(event);
  if(character!==null){
    event.preventDefault();event.stopImmediatePropagation();
    if(scanResetPending[id])setFixedValue(id,'');
    if(input.value.length<fixedMaxLength(id))replaceSNSelection(input,character);
    return;
  }
  if(event.isComposing||event.keyCode===229||event.code==='Space'){
    event.preventDefault();event.stopImmediatePropagation();
  }
}

function captureFixedSNPaste(id,event){
  const input=$(id);
  event.preventDefault();event.stopImmediatePropagation();
  if(input.disabled)return;
  const text=String(event.clipboardData?.getData('text/plain')||'').replace(/[\r\n]+$/,'');
  if(!text||!/^[\x21-\x7e]+$/.test(text)){
    scanFieldError[id]='Pasted '+SCAN_FIELDS[id].label+' must contain printable ASCII characters without spaces.';
    renderStation(lastStatus.station);return;
  }
  if(scanResetPending[id])setFixedValue(id,'');
  scanFieldError[id]='';replaceSNSelection(input,text);
}

function blockNativeSNInput(id,event){
  event.preventDefault();event.stopImmediatePropagation();
  $(id).value=fixedBuffers[id];
}

function attachFixedScanner(id){
  $(id).addEventListener('keydown',event=>captureFixedSNKey(id,event),true);
  $(id).addEventListener('paste',event=>captureFixedSNPaste(id,event),true);
  for(const eventName of ['beforeinput','compositionstart','compositionupdate','compositionend','drop'])$(id).addEventListener(eventName,event=>blockNativeSNInput(id,event),true);
  $(id).addEventListener('input',event=>{event.stopImmediatePropagation();$(id).value=fixedBuffers[id]},true);
}

// ---------------------------------------------------------------------------
// F12 console tracing for SFIS Step1 / Step4.
// The station keeps the last 60 attempts (URL, body, raw answer); this mirrors
// them into the browser console and logs every station state transition, so a
// rolled-back cycle can be explained without shell access to the station.
// ---------------------------------------------------------------------------
const SFIS_TAG='font-weight:bold;color:#0b8457';
const SFIS_TAG_BAD='font-weight:bold;color:#c0392b';
const SFIS_TAG_WARN='font-weight:bold;color:#b8860b';
const SFIS_PHASE_STYLE={error:SFIS_TAG_BAD,failed:SFIS_TAG_BAD,retry_required:SFIS_TAG_BAD,
                        bypass:SFIS_TAG_WARN,local:SFIS_TAG_WARN,ignored:SFIS_TAG_WARN};
let sfisLogCursor=0,sfisLogBusy=false,sfisLogAt=0,sfisLogDisabled=false,sfisHistory=[];
let lastLoggedStationState=null;

function sfisStamp(ts){const value=Number(ts);return Number.isFinite(value)?new Date(value*1000).toLocaleTimeString('en-GB',{hour12:false})+'.'+String(Math.floor((value%1)*1000)).padStart(3,'0'):'--'}

function logSfisEvent(entry){
  const step=String(entry.step||'sfis').toUpperCase(),phase=String(entry.phase||'');
  const style=SFIS_PHASE_STYLE[phase]||SFIS_TAG;
  const headline=`%c[SFIS ${step}] ${phase}%c ${sfisStamp(entry.ts)} · SN ${entry.sn||'—'} · MCIO ${entry.mcio_sn||'—'} · empNo ${entry.emp_no||'—'}`;
  const bad=style===SFIS_TAG_BAD;
  (bad?console.group:console.groupCollapsed).call(console,headline,style,'color:inherit;font-weight:normal');
  if(entry.url)console.log('URL        :',entry.url);
  if(entry.request_body)console.log('Request    :',entry.request_body);
  if(entry.response!==undefined&&entry.response!==null)console.log('Response   :',entry.response);
  const sfis=entry.response&&entry.response.sfis_response;
  if(sfis)console.log('SFIS data  :',sfis.data??'(none)','· errorCode',sfis.errorCode??'(none)','· message',sfis.message||'(none)');
  if(entry.detail)(bad?console.error:console.log)('Detail     :',entry.detail);
  if(entry.hint)console.warn('Hint       :',entry.hint);
  console.log('Context    :',{seq:entry.seq,cycle_id:entry.cycle_id||'',station_state:entry.station_state||'',
                              request_id:entry.request_id||'',transport_mode:entry.transport_mode||'',
                              bypass:entry.bypass===true,path:entry.path||''});
  console.groupEnd();
}

async function pollSfisLog(force=false){
  if(sfisLogDisabled||sfisLogBusy)return;
  if(!force&&Date.now()-sfisLogAt<1000)return;
  sfisLogBusy=true;
  try{
    const data=await req(APP_BASE+'/api/apl/sfis-log?after='+sfisLogCursor,{timeoutMs:4000});
    sfisLogAt=Date.now();
    for(const entry of data.events||[]){
      sfisLogCursor=Math.max(sfisLogCursor,Number(entry.seq)||0);
      sfisHistory.push(entry);if(sfisHistory.length>200)sfisHistory.shift();
      logSfisEvent(entry);
    }
  }catch(error){
    // A station build without the endpoint must not spam the console.
    sfisLogAt=Date.now();
    if(/404|Not Found/i.test(error.message||'')){sfisLogDisabled=true;console.warn('%c[SFIS]%c tracing endpoint /api/apl/sfis-log is unavailable on this station build.',SFIS_TAG_WARN,'color:inherit')}
  }finally{sfisLogBusy=false}
}

let lastStep1Retry=false;
function logStationState(station){
  const state=station?.state??null;
  // Step1 keeps the product SN when it fails, so the console cue is the
  // step1_retry_required flag rising rather than a cycle-dropping transition.
  // The sticky SN_CONFIRM shares its state string with the momentary one, so
  // the retry edge is watched independently of the state-change log.
  const retry=station?.step1_retry_required===true;
  const enteredRetry=retry&&!lastStep1Retry;lastStep1Retry=retry;
  if(enteredRetry)console.error('%c[APL]%c SFIS Step1 refused this product serial number. The SN is kept: scan the SAME product serial number again to retry, or terminate. If the employee number was rejected, select Switch operator and sign in with the correct number first. Reason: '+(station?.last_error||'unknown'),SFIS_TAG_BAD,'color:inherit');
  if(state!==lastLoggedStationState){
    const previous=lastLoggedStationState;lastLoggedStationState=state;
    const style=enteredRetry?SFIS_TAG_BAD:SFIS_TAG;
    console.log(`%c[APL] state%c ${previous===null?'(page load)':previous||'offline'} → ${state||'offline'}`,style,'color:inherit;font-weight:normal',
      {sn:station?.sn||'',mcio_sn:station?.mcio_sn||'',operator:station?.operator||'',
       cycle_id:station?.cycle_id||'',last_error:station?.last_error||'',
       dmz_mode:station?.dmz?.mode||'',bypass:station?.dmz?.bypass===true});
  }else if(!enteredRetry)return;
  pollSfisLog(true);
}

// Console helpers for manual inspection from F12.
window.aplSfis={
  log:()=>{console.table(sfisHistory.map(e=>({seq:e.seq,time:sfisStamp(e.ts),step:e.step,phase:e.phase,sn:e.sn,mcio_sn:e.mcio_sn,emp_no:e.emp_no,detail:e.detail||'',url:e.url||''})));return sfisHistory},
  last:()=>sfisHistory[sfisHistory.length-1],
  refresh:()=>pollSfisLog(true),
  station:()=>lastStatus.station,
  reset:()=>{sfisLogCursor=0;sfisHistory=[];sfisLogDisabled=false;return pollSfisLog(true)},
};

function stationFresh(){return !!lastStatusAt&&Date.now()-lastStatusAt<5000&&!statusPollError}
function textValue(value){if(value==null)return '';return typeof value==='string'?value:JSON.stringify(value)}
function browserVideoActive(){return lastStatus.input?.mode==='browser_video'}
function renderScannerSelection(scanner){
  const selected=scanner?.selected||{};
  const known=Number(scanner?.known_count||0);
  $('scannerSelected').textContent=scanner?.configured?`${selected.name||'USB scanner'} · VID ${selected.vendor_id} · PID ${selected.product_id} · Serial ${selected.serial||'unavailable'} · ${scanner.device||selected.phys||'disconnected'} · ${known} remembered`:'No scanner selected. A single USB keyboard candidate is selected automatically; choose manually if there are several.';
  $('scannerRefresh').disabled=scannerSettingsBusy||!scanner?.enabled||!scanner?.platform_supported;
  $('scannerApply').disabled=scannerSettingsBusy||!scanner?.enabled||!scanner?.platform_supported||!$('scannerDevices').value;
}

async function refreshScannerDevices(){
  if(scannerSettingsBusy)return;
  scannerSettingsBusy=true;$('scannerSettingsError').textContent='';
  renderScannerSelection(lastStatus.station?.scanner);
  try{
    const data=await req(APP_BASE+'/api/apl/scanner');
    const list=$('scannerDevices'),previous=list.value,selected=data.scanner?.selected||{};
    list.replaceChildren(new Option('Select a device',''));
    for(const device of data.devices||[]){
      const option=new Option(`${device.name||'USB keyboard'} · ${device.vendor_id}:${device.product_id} · ${device.path}`,device.path);
      list.add(option);
      if(!previous&&selected.vendor_id===device.vendor_id&&selected.product_id===device.product_id&&selected.name===device.name&&selected.serial===device.serial&&(selected.serial||selected.phys===device.phys))list.value=device.path;
    }
    if([...list.options].some(option=>option.value===previous))list.value=previous;
    if(lastStatus.station)lastStatus.station.scanner=data.scanner;
    if(!data.scanner.platform_supported)$('scannerSettingsError').textContent='Direct USB scanner discovery is available on Linux SmartCam. On a PC, use the SN field after signing in.';
    else if(!(data.devices||[]).length)$('scannerSettingsError').textContent='No USB keyboard candidate found. Check the powered hub and USB host mode.';
  }catch(error){$('scannerSettingsError').textContent=error.message}
  finally{scannerSettingsBusy=false;renderStation(lastStatus.station)}
}

async function saveScannerSelection(){
  const path=$('scannerDevices').value;
  if(scannerSettingsBusy||!path)return;
  scannerSettingsBusy=true;$('scannerSettingsError').textContent='';
  renderScannerSelection(lastStatus.station?.scanner);
  try{
    const data=await req(APP_BASE+'/api/apl/scanner',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
    if(lastStatus.station)lastStatus.station.scanner=data.scanner;
    message('Scanner selected. It will be remembered after the USB connection succeeds.','ok');
  }catch(error){$('scannerSettingsError').textContent=error.message}
  finally{scannerSettingsBusy=false;renderStation(lastStatus.station)}
}

function renderStation(station){
  logStationState(station);pollSfisLog();
  const fresh=stationFresh()&&station?.status_stale!==true,available=!!station,freshStation=fresh&&available;
  const busy=stationBusy||!!station?.request_in_flight;
  const signedIn=!!station?.operator;
  const operatorRequired=station?.operator_required!==false;
  if(operatorRequired&&!signedIn&&(fixedBuffers.snInput||fixedBuffers.mcioInput))clearScanBuffers();
  // A refused Step1 keeps the SN field live so the same label can be re-scanned.
  const retry=station?.step1_retry_required===true;
  const snEditable=freshStation&&!busy&&(station.state==='WAITING_SN'||retry)&&(!operatorRequired||signedIn);
  const mcioEditable=freshStation&&!busy&&station.state==='WAITING_MCIO_SN';
  const connection=$('connectionStrip');connection.className='connectionStrip'+(freshStation?' good':'');
  connection.textContent=freshStation?'Station connected':statusPollError?'Connection lost. Reconnecting...':'Connecting to station...';
  connection.classList.toggle('hidden',freshStation);
  const canEditOperator=freshStation&&!busy&&(station.state==='WAITING_SN'||retry);
  $('operatorSignIn').classList.toggle('hidden',signedIn);
  $('operatorBadge').classList.toggle('hidden',!signedIn);
  $('opName').textContent=station?.operator||'—';
  $('opInput').disabled=!canEditOperator;$('opSubmit').disabled=!canEditOperator;
  $('opSwitch').disabled=!canEditOperator;
  $('snInput').disabled=!snEditable;
  $('mcioInput').disabled=!mcioEditable;
  $('snInput').placeholder=operatorRequired&&!signedIn?'Enter the employee number first':scanPlaceholder('Scan or enter serial number',station?.sn_prefix);
  $('mcioInput').placeholder=scanPlaceholder('Scan or enter MCIO card serial number',station?.mcio_sn_prefix);
  $('snHint').textContent=scanFieldError.snInput||scanHint('product label',station?.sn_prefix);
  $('snHint').classList.toggle('scanHintError',!!scanFieldError.snInput);
  $('mcioHint').textContent=scanFieldError.mcioInput||scanHint('MCIO card label',station?.mcio_sn_prefix);
  $('mcioHint').classList.toggle('scanHintError',!!scanFieldError.mcioInput);
  const captureReady=browserVideoActive()?browserVideoReady&&!!videoChoice:!!lastStatus.has_raw;
  $('captureInspect').disabled=!freshStation||busy||station.can_capture!==true||!captureReady;
  $('terminateInspection').disabled=!freshStation||busy||station.can_terminate!==true;
  // The automatic send makes PASS_CONFIRM a momentary state, so this button only
  // surfaces when a send failed or an interrupted cycle was recovered.
  const awaitingStep4=freshStation&&station.state==='PASS_CONFIRM'&&station.can_confirm_result===true;
  $('retryStep4').classList.toggle('hidden',!awaitingStep4);
  $('retryStep4').disabled=!awaitingStep4||busy;
  if(freshStation&&station.state!=='PASS_CONFIRM'&&step4Recovery)step4Recovery='';
  $('stationRecovery').textContent=step4Recovery;
  $('stationState').textContent=freshStation?(retry?STEP1_RETRY_LABEL:STATION_LABELS[station.state]||station.state):'Offline';
  $('stationState').className='state '+(!freshStation?'warn':retry||station.state==='NOT_MATCHED'||station.state==='RECOVERY_REQUIRED'?'bad':station.state==='PASS_CONFIRM'?'good':station.state==='JUDGING'||station.state==='UPLOADING'||busy?'warn':'idle');
  const instruction=retry?STEP1_RETRY_HINT:operatorRequired&&!signedIn&&station?.state==='WAITING_SN'?'Enter the operator employee number before scanning the product serial number.':station?STATION_HINTS[station.state]||station.state:'Waiting for station status';
  $('stationInstruction').textContent=instruction;
  $('stationCycle').textContent=station?.cycle_id?'Cycle: '+station.cycle_id:'';
  renderStepFlow(station,freshStation);
  $('captureInspect').textContent=station?.state==='JUDGING'?'Inspecting...':'Capture & Inspect';
  const step4Enabled=station?.dmz?.step4_enabled!==false;
  $('terminateInspection').textContent='Terminate Inspection';
  const sifiConfigured=station?.dmz?.configured===true;
  const sifiBypass=station?.dmz?.bypass===true;
  const step4Blocked=!browserVideoActive()&&!step4Enabled;
  $('sifiStatus').textContent=browserVideoActive()?(sifiBypass?'Browser test · SFIS BYPASS — results are saved locally only':'Browser test: SFIS upload disabled'):step4Blocked?'SFIS Step4 not enabled — cannot post. Contact the web developer SMD (Dre Hsu / TW031561).':sifiBypass?'SFIS BYPASS — Step1/Step4 not sent (test only)':sifiConfigured?'SFIS configured':'SFIS not configured';
  $('sifiStatus').className='stationMeta '+(sifiConfigured&&!step4Blocked&&!sifiBypass?'sifiGood':'sifiWarn');
  // The station echoes the same text in last_error that the failed request just
  // returned, so dedupe instead of showing the operator the line twice.
  $('stationError').textContent=[...new Set([stationLocalError,textValue(station?.last_error)].filter(Boolean))].join('\n');
  const scanner=station?.scanner,panel=$('scannerPanel');
  let scannerClass=' manual',scannerTitle='Scan or type a serial number',scannerDetail='Focus the SN field when using a scanner connected to this PC.';
  if(scanner?.connected){
    scannerClass=' good';scannerTitle='Scanner connected';
    const device=scanner.selected||{};
    scannerDetail=`${device.name||'USB scanner'} · VID ${device.vendor_id||'—'} · PID ${device.product_id||'—'} · ${scanner.scan_count||0} scans accepted`;
  }else if(scanner?.enabled){
    scannerClass=' warn';scannerTitle=scanner?.known_count?'Reconnecting saved scanner':'SmartCam scanner not connected';
    scannerDetail=textValue(scanner.last_error)||'Known scanners reconnect automatically. For a new scanner, open Find or change scanner.';
  }
  panel.className='scannerPanel'+scannerClass;$('scannerTitle').textContent=scannerTitle;$('scannerDetail').textContent=scannerDetail;
  renderScannerSelection(scanner);
  const matching=station?.matching||{};$('stableLabel').textContent=freshStation?matching.stable||'UNKNOWN':'Unavailable';$('rawLabel').textContent=freshStation?matching.raw||'UNKNOWN':'—';
  const result=station?.last_result;
  $('lastResult').textContent=result?'Last '+(result.test_mode?'test':'inspection')+': '+result.sn+' · '+(result.visual_result||result.result||result.cycle_status||'CLOSED')+' · '+(result.upload_status||result.reason||'Saved locally'):'';
  const activeCycle=available&&station.state!=='WAITING_SN';
  if((!fresh||activeCycle)&&!lastStatus.running&&lastStatus.status!=='STARTING')$('cameraToggle').disabled=true;
  if(!fresh){$('snapshotBtn').disabled=true;$('recordToggle').disabled=true}
  if(!freshStation&&station){$('verdict').textContent='Offline';$('verdict').className='state warn';$('flowPct').textContent='Unavailable'}
  renderInput();
  renderTemperature();
}

function scanPlaceholder(base,prefix){return prefix?base+' ('+prefix+'…)':base}
function scanHint(label,prefix){
  return 'Scan the '+label+(prefix?' (starts with '+prefix+')':'')+'. Press Enter to continue.';
}

// One pure function decides every step's state, so the markup, the dimming and
// the auto-focus can never disagree about which step the operator is on. Exactly
// one of steps 2-5 is active: the first one that is not finished yet.
function stepStatuses(station,freshStation){
  if(!freshStation)return {1:'pending',2:'pending',3:'pending',4:'pending',5:'pending'};
  const mcioRequired=station.inspection_mode==='single_snapshot_cover_card';
  // A disconnected scanner warns but never blocks: the fields still accept typing.
  const scannerReady=station.scanner?.connected===true||station.scanner?.enabled!==true;
  const done={2:station.operator_required===false||!!station.operator,
              // A failed Step1 keeps station.sn, but the operator is back on the
              // SN field retrying it, so step 3 is not done until Step1 passes.
              3:!!station.sn&&station.step1_retry_required!==true,
              4:!mcioRequired||!!station.mcio_sn,
              // Step 5 closes the cycle: on success every step resets at once, so a
              // visible PASS_CONFIRM means the unit is NOT finished yet.
              5:false};
  const statuses={1:scannerReady?'done':'warn'};
  let active=false;
  for(const step of [2,3,4,5]){
    if(done[step]&&!active){statuses[step]='done';continue}
    statuses[step]=active?'pending':'active';
    active=true;
  }
  return statuses;
}

function renderStepFlow(station,freshStation){
  const statuses=stepStatuses(station,freshStation);
  const values={1:'',2:station?.operator||'',3:station?.sn||'',4:station?.mcio_sn||'',
                5:station?.state==='PASS_CONFIRM'?'OK':''};
  for(const step of [1,2,3,4,5]){
    const row=$('step'+step);
    row.className='stepRow '+statuses[step];
    $('step'+step+'Value').textContent=values[step];
  }
  // A legacy station never collects an MCIO card serial number.
  $('step4').classList.toggle('hidden',freshStation&&station.inspection_mode!=='single_snapshot_cover_card');
}

// renderStation runs on every poll and on a 1s timer, so focus must follow step
// transitions only; grabbing it every tick would fight the operator's caret.
function activeScanStep(station){
  if(!stationFresh()||!station)return '';
  if(station.operator_required!==false&&!station.operator)return 'operator';
  if(station.active_scan_field==='sn')return 'sn';
  if(station.active_scan_field==='mcio_sn')return 'mcio';
  return 'other';
}

function syncScanFocus(station,force=false){
  const step=activeScanStep(station);
  const target={operator:'opInput',sn:'snInput',mcio:'mcioInput'}[step]||'';
  if(!force&&step===lastScanStep)return;
  if(!target){lastScanStep=step;return}
  // Commit only once the focus really moved: a step whose input is still
  // disabled must keep retrying on the next tick instead of being skipped.
  if(currentPage!=='live'||$(target).disabled)return;
  if(document.activeElement!==$(target))$(target).focus({preventScroll:true});
  lastScanStep=step;
}

function renderTemperature(){
  const t=lastStatus.temperature,valid=stationFresh()&&t?.available&&Number.isFinite(t.celsius);
  $('temperatureValue').textContent=valid?t.celsius.toFixed(1)+' °C':'Unavailable';
  $('temperatureLabel').textContent=t?.label||'Temperature';
  $('temperatureState').textContent=valid?'LIVE':'N/A';
  $('temperatureState').className='state '+(valid?'good':'idle');
  $('temperatureValue').title=valid?t.sensor:'No temperature reading is available on this host';
}
function renderInput(){
  const input=lastStatus.input||{},browserActive=input.mode==='browser_video';
  const testing=browserActive||['video','demo'].includes(input.mode);
  const stationState=lastStatus.station?.state;
  const videoTestEnabled=lastStatus.features?.video_test_enabled===true;
  const controlsReady=stationFresh()&&!lastStatus.recording&&!videoBusy;
  const canSwitchInput=videoTestEnabled&&controlsReady&&stationState==='WAITING_SN';
  const canStartVideo=videoTestEnabled&&controlsReady&&!browserActive&&!!videoChoice&&VIDEO_TEST_STATES.includes(stationState);
  const canAttachVideo=videoTestEnabled&&controlsReady&&VIDEO_TEST_STATES.includes(stationState);
  $('videoTestToolbar').classList.toggle('hidden',!videoTestEnabled);
  $('videoFile').disabled=!canAttachVideo;$('uploadVideo').disabled=!canAttachVideo;
  $('uploadVideo').textContent=browserActive&&videoChoice?'Replace Test Video':'Choose Test Video';
  $('startVideo').disabled=!canStartVideo;
  $('cameraInput').disabled=!canSwitchInput||!browserActive;
  $('inputSourceName').textContent=testing?'Video Test · '+input.name:'Live Camera';
  $('inputSourceName').className='inputName'+(testing?' test':'');
  const video=$('testVideo'),duration=Number(videoChoice?.duration_seconds||input.duration_seconds||0);
  const position=browserActive&&videoChoice?Number(video.currentTime||0):0;
  updateVideoProgress(position,duration);
  $('videoFileName').textContent=videoBusy?'Reading video metadata in this browser...':videoChoice?'Selected: '+videoChoice.name+' · '+videoChoice.width+' × '+videoChoice.height+' · '+fmtDuration(duration):browserActive?'Choose the local test video again to restore playback.':'1920 × 1080 MP4 · played locally · file stays on this PC';
  const activeVideo=browserActive&&browserVideoReady&&!!videoChoice;
  $('videoPlayPause').disabled=!activeVideo||videoBusy;
  $('videoPlayPause').textContent=video.paused?'▶ Play':'Ⅱ Pause';
  $('videoBack').disabled=!activeVideo||videoBusy;
  $('videoForward').disabled=!activeVideo||videoBusy;
  $('videoSeek').disabled=!activeVideo||videoBusy||!Number.isFinite(duration)||duration<=0;
  $('videoSeek').max=String(Math.max(0,Number(duration)||0));
  if(!videoSeeking)$('videoSeek').value=String(Math.max(0,position));
}

function updateVideoProgress(position,duration){
  const current=Math.max(0,Number(position)||0),total=Math.max(0,Number(duration)||0);
  $('videoPosition').textContent=fmtDuration(current)+' / '+fmtDuration(total);
  const percent=total>0?Math.max(0,Math.min(100,current/total*100)):0;
  $('videoSeek').style.setProperty('--video-progress',percent+'%');
}

function waitForBrowserVideo(video){
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>finish(new Error('The browser could not load this video within 15 seconds.')),15000);
    function finish(error){clearTimeout(timer);video.removeEventListener('loadeddata',loaded);video.removeEventListener('error',failed);error?reject(error):resolve()}
    function loaded(){finish()}
    function failed(){finish(new Error('This browser cannot decode the selected MP4 video.'))}
    video.addEventListener('loadeddata',loaded,{once:true});video.addEventListener('error',failed,{once:true});video.load();
  });
}

function clearBrowserResult(){
  browserCaptureTime=null;$('testResult').removeAttribute('src');$('testResult').style.display='none';
}

async function chooseBrowserVideo(){
  const file=$('videoFile').files[0];if(!file){$('videoFile').click();return}
  if(!/\.mp4$/i.test(file.name)){message('Choose an MP4 video.','err');return}
  const replacing=browserVideoActive();
  const previousChoice=videoChoice,previousTime=Number($('testVideo').currentTime||0);
  videoBusy=true;renderInput();
  const video=$('testVideo'),nextURL=URL.createObjectURL(file),previousURL=browserVideoURL;
  try{
    video.pause();clearBrowserResult();browserVideoReady=false;video.src=nextURL;
    await waitForBrowserVideo(video);
    const width=Number(video.videoWidth),height=Number(video.videoHeight),duration=Number(video.duration);
    if(width!==1920||height!==1080)throw Error('Video must be 1920 x 1080 to match the golden samples.');
    if(!Number.isFinite(duration)||duration<=0)throw Error('The browser could not read the video duration.');
    const nextChoice={file,name:file.name,width,height,duration_seconds:duration};
    if(replacing){
      const r=await req(A+'/browser-video/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nextChoice.name,width:nextChoice.width,height:nextChoice.height,duration_seconds:nextChoice.duration_seconds}),timeoutMs:30000});
      if(!r.success)throw Error(r.error||'The replacement video could not be selected.');
      if(r.station)lastStatus.station=r.station;if(r.input)lastStatus.input=r.input;
    }
    if(previousURL)URL.revokeObjectURL(previousURL);
    browserVideoURL=nextURL;browserVideoReady=true;videoChoice=nextChoice;
    video.currentTime=0;clearBrowserResult();
    if(replacing){await pollOnce();message(lastStatus.station?.state==='WAITING_SN'?'Test video replaced. Ready for the next serial number.':'Test video attached to the current inspection. Select a frame, then Capture & Inspect.','ok')}
    else message('Video ready on this PC. Select Start Browser Test.','ok');
  }
  catch(error){
    URL.revokeObjectURL(nextURL);
    if(previousURL&&previousChoice){video.src=previousURL;video.load();videoChoice=previousChoice;browserVideoReady=true;try{video.currentTime=previousTime}catch(_){} }
    else{video.removeAttribute('src');video.load();videoChoice=null;browserVideoReady=false}
    message(error.message,'err');
  }
  finally{videoBusy=false;renderInput()}
}

async function selectBrowserInput(camera=false){
  if(!camera&&!videoChoice)return;
  videoBusy=true;renderInput();
  try{
    const path=camera?'/browser-video/stop':'/browser-video/start';
    const body=camera?{}:{name:videoChoice.name,width:videoChoice.width,height:videoChoice.height,duration_seconds:videoChoice.duration_seconds};
    const r=await req(A+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),timeoutMs:30000});
    if(!r.success)throw Error(r.error||'Input switch failed');
    if(r.station)lastStatus.station=r.station;if(r.input)lastStatus.input=r.input;
    $('testVideo').pause();clearBrowserResult();closeStream();streamKey='';await pollOnce();
    message(camera?'Live camera selected.':lastStatus.station?.state==='WAITING_SN'?'Browser video test started. Scan an SN to begin inspection.':'Browser video test started for the current SN. Select a frame, then Capture & Inspect.','ok');
  }
  catch(error){message(error.message,'err')}
  finally{videoBusy=false;renderInput();loadConfig(false)}
}

async function submitScan(id){
  const spec=SCAN_FIELDS[id],station=lastStatus.station;
  // Snapshot mode publishes active_scan_field (it reads 'sn' both at WAITING_SN
  // and in the Step1 retry state); legacy has no such key, so fall back to the
  // raw state the way the server's /api/apl/scan guard does.
  const fieldOk=station?.active_scan_field!==undefined?station.active_scan_field===spec.field:station?.state===spec.expect;
  if(!stationFresh()||stationBusy||station?.request_in_flight||!fieldOk)return;
  if(spec.field==='sn'&&station.operator_required!==false&&!station.operator){clearScanBuffers();$('opInput').focus();return}
  const value=fixedBuffers[id];if(!value){$(id).focus();return}
  const prefix=spec.field==='sn'?station.sn_prefix:station.mcio_sn_prefix;
  if(prefix&&!value.toUpperCase().startsWith(String(prefix).toUpperCase())){
    scanFieldError[id]='Wrong label: the '+spec.label+' must start with '+prefix+'. Scanned: '+value.slice(0,32);
    renderStation(station);return;
  }
  scanFieldError[id]='';stationBusy=true;stationLocalError='';renderStation(station);
  // expect lets the station refuse a scan whose step already advanced, which a
  // grabbed HID scanner can do without this tab noticing.
  try{const data=await req(APP_BASE+'/api/apl/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sn:value,expect:spec.field})});if(data.success===false)throw Error(data.error||'The station did not accept this serial number');if(data.station)lastStatus.station=data.station;setFixedValue(id,'');step4Recovery='';message(scanAcceptedMessage(data.station),'ok')}
  catch(error){stationLocalError=error.name==='AbortError'?'Request timed out. Check the current serial number before retrying.':error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);syncScanFocus(lastStatus.station,true)}
}

function scanAcceptedMessage(station){
  if(station?.state==='WAITING_MCIO_SN')return 'Product serial number accepted by SFIS Step1. Scan the MCIO card serial number.';
  if(station?.state==='WAITING_CAPTURE')return 'Both serial numbers accepted. Close the cover and install the MCIO card, then select Capture & Inspect.';
  return 'Serial number accepted.';
}

async function submitOperator(){
  const station=lastStatus.station;
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.state!=='WAITING_SN')return;
  const emp=$('opInput').value.trim();if(!emp){$('opInput').focus();return}
  stationBusy=true;stationLocalError='';renderStation(station);
  try{const data=await req(APP_BASE+'/api/apl/operator',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emp_no:emp})});if(data.success===false)throw Error(data.error||'Employee number was not accepted');if(data.station)lastStatus.station=data.station;clearScanBuffers();$('opInput').value='';message('Operator signed in. Scan the product serial number.','ok')}
  catch(error){stationLocalError=error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);syncScanFocus(lastStatus.station,true)}
}

async function switchOperator(){
  const station=lastStatus.station;
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.state!=='WAITING_SN')return;
  stationBusy=true;stationLocalError='';renderStation(station);
  try{const data=await req(APP_BASE+'/api/apl/operator/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(data.success===false)throw Error(data.error||'Could not switch operator');if(data.station)lastStatus.station=data.station;clearScanBuffers();message('Enter the next operator employee number.','ok')}
  catch(error){stationLocalError=error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);syncScanFocus(lastStatus.station,true)}
}

async function sendStep4(station){
  const data=await req(APP_BASE+'/api/apl/confirm-result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cycle_id:station.cycle_id}),timeoutMs:30000});
  if(data.success===false)throw Error(data.error||'SFIS Step4 did not complete');
  if(data.station)lastStatus.station=data.station;
  if(data.station?.state==='WAITING_SN')clearScanBuffers();
  step4Recovery='';
  message(station?.dmz?.bypass?'Test result saved locally. SFIS was not contacted. Ready for the next serial number.':'SFIS Step4 accepted. Ready for the next serial number.','ok');
  if(currentPage==='history')loadHistory();
}

async function stationAction(action){
  const station=lastStatus.station,permission={'confirm-result':'can_confirm_result',terminate:'can_terminate'}[action];
  if(!permission||!stationFresh()||stationBusy||station?.request_in_flight||station?.[permission]!==true)return;
  if(action==='terminate'&&!confirm('Terminate this inspection without uploading?\nSN: '+(station.sn||'—')+'\nMCIO SN: '+(station.mcio_sn||'—')+'\nThe serial numbers will be cleared. The saved inspection evidence will be retained.'))return;
  stationBusy=true;stationLocalError='';renderStation(station);
  try{
    if(action==='confirm-result'){await sendStep4(station)}
    else{
      const data=await req(APP_BASE+'/api/apl/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cycle_id:station.cycle_id}),timeoutMs:30000});
      if(data.success===false)throw Error(data.error||'Action did not complete');
      if(data.station)lastStatus.station=data.station;
      if(data.station?.state==='WAITING_SN')clearScanBuffers();
      step4Recovery='';
      message('Inspection terminated without upload.','ok');
      if(currentPage==='history')loadHistory();
    }
  }
  catch(error){
    if(action==='confirm-result')applyStep4Failure(error);
    else stationLocalError=error.name==='AbortError'?'Request timed out. Wait for the current status before retrying.':error.message;
  }
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);syncScanFocus(lastStatus.station,true)}
}

async function captureInspection(){
  const station=lastStatus.station;
  const browserCapture=browserVideoActive();
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.can_capture!==true)return;
  if(browserCapture&&(!browserVideoReady||!videoChoice))return;
  if(!browserCapture&&!lastStatus.has_raw)return;
  stationBusy=true;stationLocalError='';step4Recovery='';renderStation(station);
  try{
    let data;
    if(browserCapture){
      const video=$('testVideo');video.pause();
      if(video.readyState<2)throw Error('Wait for the selected video frame to finish loading.');
      const canvas=$('testFrameCanvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
      const context=canvas.getContext('2d',{alpha:false});
      if(!context)throw Error('This browser cannot capture the selected video frame.');
      context.drawImage(video,0,0,canvas.width,canvas.height);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      if(!blob)throw Error('The browser could not encode the selected video frame.');
      const form=new FormData();
      form.append('frame',blob,'browser_capture.png');
      form.append('test_id',String(lastStatus.input?.id||''));
      form.append('selected_seconds',Number(video.currentTime||0).toFixed(6));
      data=await req(APP_BASE+'/api/apl/capture-inspect-frame',{method:'POST',body:form,timeoutMs:30000});
      browserCaptureTime=Number(video.currentTime||0);
      const resultImage=data.attempt?.result_image;
      if(!resultImage)throw Error('Inspection completed without a result image.');
      $('testResult').src=evidenceUrl(resultImage)+'?v='+Date.now();
      setView('result');
    }else{
      data=await req(APP_BASE+'/api/apl/capture-inspect',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',timeoutMs:30000});
    }
    if(data.success===false)throw Error(data.error||'Inspection did not complete');
    if(data.station)lastStatus.station=data.station;
    const accepted=data.station?.state==='PASS_CONFIRM';
    if(!accepted){message('Not matched. Adjust the product and capture again.','warn')}
    else if(lastStatus.station?.can_confirm_result===true){
      // A PASS closes itself: send Step4 and hand the station straight back for
      // the next unit, so a normal round needs no click after Capture & Inspect.
      message(station?.dmz?.bypass?'Inspection OK. Closing this unit (SFIS bypass)...':'Inspection OK. Sending SFIS Step4...','ok');
      renderStation(lastStatus.station);
      try{await sendStep4(lastStatus.station)}
      catch(error){applyStep4Failure(error)}
    }
    else{
      // Step4 is off, unconfigured, or this is browser test data: PASS_CONFIRM
      // is the end of the line and only Terminate can clear it.
      step4Recovery=step4BlockedGuidance(lastStatus.station);
      message('Inspection OK. This station cannot post Step4; terminate this inspection to finish.','warn');
    }
  }catch(error){stationLocalError=error.name==='AbortError'?'Inspection timed out. Check the current status before retrying.':error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);syncScanFocus(lastStatus.station,true)}
}

async function toggleBrowserVideo(){
  const video=$('testVideo');if(!browserVideoActive()||!browserVideoReady||videoBusy)return;
  try{if(video.paused){clearBrowserResult();await video.play()}else video.pause()}
  catch(error){message('Video playback failed: '+error.message,'err')}
  finally{renderInput();renderStreamStatus()}
}
function previewVideoSeek(value){videoSeeking=true;const duration=Number(videoChoice?.duration_seconds||lastStatus.input?.duration_seconds||0);updateVideoProgress(value,duration)}
function skipBrowserVideo(seconds){
  if(!browserVideoActive()||!browserVideoReady||videoBusy)return;
  const video=$('testVideo'),duration=Number(videoChoice?.duration_seconds||0);
  seekVideo(Math.max(0,Math.min(duration,Number(video.currentTime||0)+Number(seconds||0))));
}
async function seekVideo(value){
  const video=$('testVideo');if(!browserVideoActive()||!browserVideoReady||videoBusy)return;
  const duration=Number(videoChoice?.duration_seconds||0),target=Math.max(0,Math.min(duration,Number(value)||0));
  video.pause();clearBrowserResult();videoBusy=true;renderInput();
  try{
    if(Math.abs(Number(video.currentTime||0)-target)>0.001){
      const done=new Promise((resolve,reject)=>{const timer=setTimeout(()=>finish(new Error('The browser could not seek to this position.')),10000);function finish(error){clearTimeout(timer);video.removeEventListener('seeked',seeked);video.removeEventListener('error',failed);error?reject(error):resolve()}function seeked(){finish()}function failed(){finish(new Error('Video decoding failed while seeking.'))}video.addEventListener('seeked',seeked,{once:true});video.addEventListener('error',failed,{once:true})});
      video.currentTime=target;await done;
    }
  }catch(error){message(error.message,'err')}
  finally{videoBusy=false;videoSeeking=false;renderInput();renderStreamStatus()}
}

function evidenceUrl(path){return APP_BASE+'/api/apl/evidence/'+String(path||'').split('/').map(encodeURIComponent).join('/')}
function historyDate(value){return value?new Date(Number(value)*1000).toLocaleString('en-GB'):'—'}
async function loadHistory(){
  if(historyBusy)return;historyBusy=true;$('historyRefresh').disabled=true;
  try{const params=new URLSearchParams({sn:$('historySN').value.trim(),visual_result:$('historyResult').value,limit:'200'});const data=await req(APP_BASE+'/api/apl/history?'+params.toString());const rows=data.items||[];$('historyCount').textContent=rows.length+' inspections';$('historyBody').innerHTML=rows.length?rows.map(item=>`<tr><td>${esc(historyDate(item.updated_at))}</td><td class="mediaName">${esc(item.sn)}</td><td>${esc(item.visual_result||'NOT_INSPECTED')}</td><td>${esc(item.state)}</td><td>${esc(item.source_mode||'—')}</td><td>${Number(item.attempt_count||0)}</td><td><button class="mini" onclick='loadHistoryDetail(${JSON.stringify(item.cycle_id)})'>View</button></td></tr>`).join(''):'<tr><td colspan="7" class="emptyMedia">No inspection history found.</td></tr>'}
  catch(error){message('History: '+error.message,'err')}
  finally{historyBusy=false;$('historyRefresh').disabled=false}
}
async function loadHistoryDetail(cycleId){
  try{const data=await req(APP_BASE+'/api/apl/history/'+encodeURIComponent(cycleId)),item=data.inspection,attempts=item.attempts||[];$('historyDetail').innerHTML=`<div class="historyDetailHead"><strong>${esc(item.sn)}</strong><span>${esc(item.state)} · ${esc(historyDate(item.updated_at))}</span></div>${attempts.length?attempts.map((attempt,index)=>`<div class="historyAttempt"><strong>Attempt ${index+1} · ${esc(attempt.visual_result)}</strong><span>${esc(historyDate(attempt.captured_at))} · ${esc(attempt.source_mode)}</span><div class="mediaActions"><a class="mini" href="${evidenceUrl(attempt.raw_path)}" target="_blank" rel="noopener">Raw image</a><a class="mini" href="${evidenceUrl(attempt.result_path)}" target="_blank" rel="noopener">Result image</a><a class="mini" href="${evidenceUrl(attempt.json_path)}" target="_blank" rel="noopener">JSON</a></div></div>`).join(''):'<div class="emptyMedia">No capture attempts were saved for this inspection.</div>'}`;$('historyDetail').scrollIntoView({behavior:'smooth',block:'nearest'})}
  catch(error){message('History detail: '+error.message,'err')}
}

function localStreamURL(path){
  const raw=String(path||'/api/stream/whep');
  const prefixed=APP_BASE&&raw.startsWith('/api/')?APP_BASE+raw:raw;
  const url=new URL(prefixed,location.href);if(url.origin!==location.origin)throw Error('Video must use the station connection');return url.href;
}
function closeStream(){
  streamGeneration++;if(streamRequest)streamRequest.abort();streamRequest=null;
  if(streamPC)streamPC.close();streamPC=null;
  if(streamLocation){fetch(streamLocation,{method:'DELETE',keepalive:true,credentials:'same-origin'}).catch(()=>{});streamLocation=''}
  $('liveVideo').pause();$('liveVideo').srcObject=null;$('liveVideo').style.display='none';$('live').removeAttribute('src');$('live').style.display='none';
  streamReady=false;streamDecodedAt=0;streamDecodedFrames=0;streamStartedAt=0;
}
function wantedStreamKey(){
  if(!stationFresh())return '';
  if(browserVideoActive())return 'browser:'+String(lastStatus.input?.id||'missing');
  if(!lastStatus.running)return '';
  if(viewMode==='raw')return 'mjpeg:raw';
  if(lastStatus.backend==='opencv')return 'mjpeg:result:pc';
  return lastStatus.stream?.enabled===false?'disabled':'webrtc:'+localStreamURL(lastStatus.stream?.whep_url);
}
function syncStream(force=false){
  let key='';try{key=wantedStreamKey()}catch(error){streamMessage=error.message;renderStreamStatus();return}
  if(!force&&key===streamKey){if(key&&streamRetryAt&&Date.now()>=streamRetryAt)force=true;else{renderStreamStatus();return}}
  closeStream();streamKey=key;streamRetryAt=0;
  if(!key){streamMessage=stationFresh()?'Camera is stopped.':'Station disconnected. Waiting for fresh video.';renderStreamStatus();return}
  if(key.startsWith('browser:')){
    streamKind='browser';streamReady=browserVideoReady&&!!videoChoice;
    streamMessage=streamReady?'Browser video test is ready.':'The local test video is unavailable in this browser tab.';
    renderStreamStatus();return;
  }
  if(key==='disabled'){streamMessage='Live video is disabled. Check the device settings.';renderStreamStatus();return}
  streamStartedAt=Date.now();
  if(key.startsWith('mjpeg:')){
    streamKind='mjpeg';streamMessage=viewMode==='raw'?'Connecting to raw video...':'Connecting to video...';
    const generation=streamGeneration;$('live').onload=()=>{if(generation!==streamGeneration)return;streamReady=true;renderStreamStatus()};$('live').onerror=()=>{if(generation!==streamGeneration)return;streamReady=false;streamMessage='Video disconnected. Reconnecting...';streamRetryAt=Date.now()+3000;renderStreamStatus()};
    $('live').src=A+'/live.mjpg?mode='+viewMode+'&t='+Date.now();$('live').style.display='block';
  }else{streamKind='webrtc';streamMessage='Connecting to live video...';connectWHEP(key.slice(7),streamGeneration)}
  renderStreamStatus();
}
function waitForICE(pc,signal){
  if(pc.iceGatheringState==='complete')return Promise.resolve();
  return new Promise((resolve,reject)=>{const timer=setTimeout(()=>finish(new Error('Video connection timed out')),8000);function finish(error){clearTimeout(timer);pc.removeEventListener('icegatheringstatechange',changed);signal.removeEventListener('abort',aborted);error?reject(error):resolve()}function changed(){if(pc.iceGatheringState==='complete')finish()}function aborted(){finish(new Error('Video input changed'))}pc.addEventListener('icegatheringstatechange',changed);signal.addEventListener('abort',aborted,{once:true})});
}
async function connectWHEP(url,generation){
  if(!window.RTCPeerConnection){streamMessage='This browser cannot display live video. Use a supported browser.';renderStreamStatus();return}
  const pc=new RTCPeerConnection({iceServers:[]}),controller=new AbortController();streamPC=pc;streamRequest=controller;
  const timeout=setTimeout(()=>controller.abort(),15000);
  try{
    pc.addTransceiver('video',{direction:'recvonly'});
    pc.ontrack=event=>{if(generation!==streamGeneration)return;$('liveVideo').srcObject=event.streams[0]||new MediaStream([event.track]);$('liveVideo').play().catch(()=>{streamMessage='Video playback failed. Reconnect to the station.';renderStreamStatus()});event.track.onended=()=>failStream('Video ended',generation)};
    pc.onconnectionstatechange=()=>{if(['failed','closed'].includes(pc.connectionState))failStream('Video disconnected',generation)};
    await pc.setLocalDescription(await pc.createOffer());await waitForICE(pc,controller.signal);
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp,signal:controller.signal,credentials:'same-origin'});
    if(!response.ok)throw Error('Video connection failed ('+response.status+')');
    const locationHeader=response.headers.get('Location');let session='';
    if(locationHeader){session=new URL(locationHeader,url).href;if(new URL(session).origin!==location.origin)throw Error('Video session must use this station')}
    if(generation!==streamGeneration){if(session)fetch(session,{method:'DELETE',keepalive:true}).catch(()=>{});return}
    streamLocation=session;const sdp=await response.text();if(generation!==streamGeneration)return;
    await pc.setRemoteDescription({type:'answer',sdp});streamMessage='Connected. Waiting for video.';
  }catch(error){if(generation===streamGeneration)failStream(error.name==='AbortError'?'Video connection timed out':error.message,generation)}
  finally{clearTimeout(timeout);if(generation===streamGeneration){streamRequest=null;renderStreamStatus()}}
}
function failStream(reason,generation){if(generation!==streamGeneration)return;closeStream();streamMessage=reason+'. Reconnecting shortly.';streamRetryAt=Date.now()+3000;renderStreamStatus()}
async function checkStreamFrames(){
  if(streamStatsBusy||!streamPC||streamPC.connectionState!=='connected')return;
  streamStatsBusy=true;const pc=streamPC,generation=streamGeneration;
  try{const stats=await pc.getStats();if(generation!==streamGeneration)return;stats.forEach(report=>{if(report.type==='inbound-rtp'&&(report.kind==='video'||report.mediaType==='video')){const frames=Number(report.framesDecoded||0);if(frames>streamDecodedFrames){streamDecodedFrames=frames;streamDecodedAt=Date.now();streamReady=$('liveVideo').readyState>=2&&!$('liveVideo').paused}}})}catch(_){}finally{streamStatsBusy=false;renderStreamStatus()}
}
function renderStreamStatus(){
  const now=Date.now(),fresh=stationFresh();
  if(lastStatus.frame_seq!==undefined&&lastStatus.frame_seq!==streamFrameSeq){streamFrameSeq=lastStatus.frame_seq;streamFrameAt=now}
  const sourceFresh=!!streamFrameAt&&now-streamFrameAt<5000;
  const browser=streamKind==='browser';
  const decodedFresh=streamKind==='webrtc'?!!streamDecodedAt&&now-streamDecodedAt<5000:sourceFresh;
  const visible=browser?fresh&&browserVideoReady&&!!videoChoice:fresh&&!!lastStatus.running&&streamReady&&decodedFresh&&sourceFresh;
  $('viewer').classList.toggle('empty',!visible);
  const showBrowserResult=visible&&browser&&viewMode==='result'&&$('testResult').getAttribute('src');
  $('testVideo').style.display=visible&&browser&&!showBrowserResult?'block':'none';
  $('testResult').style.display=showBrowserResult?'block':'none';
  $('liveVideo').style.display=visible&&streamKind==='webrtc'?'block':'none';$('live').style.display=visible&&streamKind==='mjpeg'?'block':'none';
  let label=streamMessage;
  if(!fresh)label='Station offline. Waiting for fresh video.';
  else if(browser&&!videoChoice)label='This tab no longer has the local test video. Return to Live Camera, then choose it again.';
  else if(!browser&&!lastStatus.running)label='Camera is stopped.';
  else if(!browser&&streamReady&&!sourceFresh)label='Camera frames stopped updating.';
  else if(!browser&&streamReady&&!decodedFresh)label='Video playback stalled.';
  $('emptyText').textContent=label;$('liveBadge').textContent=visible?(browser?'VIDEO TEST':streamKind==='webrtc'?'LIVE':viewMode==='raw'?'RAW':'LIVE'):'NO VIDEO';$('liveBadge').className='badge '+(visible?'live':'warn');
  $('streamWarning').textContent=visible?(browser?'Browser test mode: the video remains on this PC; only Capture & Inspect sends one image.':viewMode==='raw'?'Raw view does not include inspection overlays.':''):label;
}

$('flowStudioLink').href=APP_BASE+'/flow-studio';
attachFixedScanner('snInput');
attachFixedScanner('mcioInput');
$('scannerSettings').addEventListener('toggle',()=>{if($('scannerSettings').open)refreshScannerDevices()});
$('scannerDevices').addEventListener('change',()=>renderScannerSelection(lastStatus.station?.scanner));
const browserVideo=$('testVideo');
browserVideo.addEventListener('timeupdate',()=>{if(!videoSeeking){$('videoSeek').value=String(browserVideo.currentTime||0);updateVideoProgress(browserVideo.currentTime,videoChoice?.duration_seconds||0)}if(browserCaptureTime!==null&&Math.abs(browserVideo.currentTime-browserCaptureTime)>0.001)clearBrowserResult()});
browserVideo.addEventListener('play',()=>{clearBrowserResult();renderInput();renderStreamStatus()});
browserVideo.addEventListener('pause',()=>{renderInput();renderStreamStatus()});
browserVideo.addEventListener('ended',()=>{renderInput();renderStreamStatus()});
window.addEventListener('pagehide',()=>{closeStream();browserVideo.pause();if(browserVideoURL)URL.revokeObjectURL(browserVideoURL)});
setInterval(()=>{renderStation(lastStatus.station);syncScanFocus(lastStatus.station);renderStreamStatus();updateSettingsActionState();checkStreamFrames();if(streamKind==='webrtc'&&streamStartedAt&&!streamRetryAt&&Date.now()-(streamDecodedAt||streamStartedAt)>15000)failStream('No video received for 15 seconds',streamGeneration);syncStream()},1000);
$('opInput').addEventListener('paste',event=>{const text=String(event.clipboardData?.getData('text/plain')||'').replace(/\s+/g,'');event.preventDefault();$('opInput').value=text.slice(0,Number($('opInput').maxLength||32))},true);
(async()=>{await pollOnce();pollLoop();await Promise.all([loadConfig(false),loadMedia(false)]);updateSnapshotLabel();syncScanFocus(lastStatus.station)})().catch(error=>message(error.message,'err'));

'use strict';

let lastStatusAt=0,statusPollBusy=false,statusPollError='',stationBusy=false,stationLocalError='';
let streamGeneration=0,streamKey='',streamPC=null,streamLocation='',streamRequest=null;
let streamRetryAt=0,streamStartedAt=0,streamDecodedAt=0,streamDecodedFrames=0,streamFrameSeq=null,streamFrameAt=0;
let streamMessage='Waiting for video.',streamReady=false,streamKind='',streamStatsBusy=false;
const STATION_LABELS={WAITING_SN:'Waiting for SN',WAITING_CAPTURE:'Ready to Capture',JUDGING:'Inspecting Image',NOT_MATCHED:'Not Matched',PASS_CONFIRM:'OK',UPLOADING:'Uploading',RECOVERY_REQUIRED:'Recovery Required'};
const STATION_HINTS={WAITING_SN:'Scan the next product serial number.',WAITING_CAPTURE:'SFIS Step1 accepted. Close the cover and install the MCIO card, then select Capture & Inspect.',JUDGING:'Checking the captured image.',NOT_MATCHED:'The required cover-and-card condition was not matched. Adjust the product and capture again.',PASS_CONFIRM:'Inspection OK. Send SFIS Step2 or terminate this inspection.',UPLOADING:'Sending SFIS Step2.',RECOVERY_REQUIRED:'The inspection was interrupted. Check the product, then terminate this inspection.'};
let videoChoice=null,videoBusy=false,videoSeeking=false,historyBusy=false;
let browserVideoURL='',browserVideoReady=false,browserCaptureTime=null;
let fixedSNBuffer='';

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

function replaceSNSelection(input,text){
  const start=input.selectionStart??input.value.length;
  const end=input.selectionEnd??start;
  input.setRangeText(text,start,end,'end');
  fixedSNBuffer=input.value.slice(0,Number(input.maxLength||128));
  input.value=fixedSNBuffer;
}

function setFixedSN(value){
  fixedSNBuffer=String(value||'').slice(0,Number($('snInput').maxLength||128));
  $('snInput').value=fixedSNBuffer;
}

function captureFixedSNKey(event){
  const input=$('snInput');
  if(input.disabled)return;
  if(event.ctrlKey||event.altKey||event.metaKey)return;
  if(event.code==='Enter'||event.code==='NumpadEnter'){
    event.preventDefault();event.stopImmediatePropagation();submitSN();return;
  }
  if(event.code==='Backspace'){
    event.preventDefault();event.stopImmediatePropagation();
    const start=input.selectionStart??input.value.length;
    const end=input.selectionEnd??start;
    if(start!==end)input.setRangeText('',start,end,'end');
    else if(start>0)input.setRangeText('',start-1,start,'end');
    fixedSNBuffer=input.value;
    return;
  }
  if(event.code==='Delete'){
    event.preventDefault();event.stopImmediatePropagation();
    const start=input.selectionStart??input.value.length;
    const end=input.selectionEnd??start;
    if(start!==end)input.setRangeText('',start,end,'end');
    else if(start<input.value.length)input.setRangeText('',start,start+1,'end');
    fixedSNBuffer=input.value;
    return;
  }
  const character=fixedScannerCharacter(event);
  if(character!==null){
    event.preventDefault();event.stopImmediatePropagation();
    if(input.value.length<Number(input.maxLength||128))replaceSNSelection(input,character);
    return;
  }
  if(event.isComposing||event.keyCode===229||event.code==='Space'){
    event.preventDefault();event.stopImmediatePropagation();
  }
}

function captureFixedSNPaste(event){
  const input=$('snInput');
  event.preventDefault();event.stopImmediatePropagation();
  if(input.disabled)return;
  const text=String(event.clipboardData?.getData('text/plain')||'').replace(/[\r\n]+$/,'');
  if(!text||!/^[\x21-\x7e]+$/.test(text)){
    stationLocalError='Pasted SN must contain printable ASCII characters without spaces.';
    renderStation(lastStatus.station);return;
  }
  stationLocalError='';replaceSNSelection(input,text);
}

function blockNativeSNInput(event){
  event.preventDefault();event.stopImmediatePropagation();
  $('snInput').value=fixedSNBuffer;
}

function stationFresh(){return !!lastStatusAt&&Date.now()-lastStatusAt<5000&&!statusPollError}
function textValue(value){if(value==null)return '';return typeof value==='string'?value:JSON.stringify(value)}
function browserVideoActive(){return lastStatus.input?.mode==='browser_video'}
function renderStation(station){
  const fresh=stationFresh(),available=!!station,freshStation=fresh&&available;
  const busy=stationBusy||!!station?.request_in_flight;
  const signedIn=!!station?.operator;
  const operatorRequired=station?.operator_required!==false;
  const snEditable=freshStation&&!busy&&station.state==='WAITING_SN'&&(!operatorRequired||signedIn);
  const connection=$('connectionStrip');connection.className='connectionStrip'+(freshStation?' good':'');
  connection.textContent=freshStation?'Station connected':statusPollError?'Connection lost. Reconnecting...':'Connecting to station...';
  connection.classList.toggle('hidden',freshStation);
  const canEditOperator=freshStation&&!busy&&station.state==='WAITING_SN';
  const showSignIn=freshStation&&operatorRequired&&!signedIn;
  $('operatorSignIn').classList.toggle('hidden',!showSignIn);
  $('operatorBadge').classList.toggle('hidden',!signedIn);
  $('opName').textContent=station?.operator||'—';
  $('opInput').disabled=!canEditOperator;$('opSubmit').disabled=!canEditOperator;
  $('opSwitch').disabled=!canEditOperator;
  $('snInput').disabled=!snEditable;$('scanSubmit').disabled=!snEditable;
  $('snInput').placeholder=operatorRequired&&!signedIn?'Enter the employee number first':'Scan or enter serial number';
  const captureReady=browserVideoActive()?browserVideoReady&&!!videoChoice:!!lastStatus.has_raw;
  $('captureInspect').disabled=!freshStation||busy||station.can_capture!==true||!captureReady;
  $('confirmResult').disabled=!freshStation||busy||station.can_confirm_result!==true;
  $('terminateInspection').disabled=!freshStation||busy||station.can_terminate!==true;
  $('stationState').textContent=freshStation?(STATION_LABELS[station.state]||station.state):'Offline';
  $('stationState').className='state '+(!freshStation?'warn':station.state==='NOT_MATCHED'||station.state==='RECOVERY_REQUIRED'?'bad':station.state==='PASS_CONFIRM'?'good':station.state==='JUDGING'||station.state==='UPLOADING'||busy?'warn':'idle');
  const instruction=browserVideoActive()&&station?.state==='PASS_CONFIRM'?'Inspection OK. Test data cannot be uploaded; select Terminate Inspection to finish.':station?STATION_HINTS[station.state]||station.state:'Waiting for station status';
  $('stationInstruction').textContent=instruction;
  $('stationSN').textContent=station?.sn||'—';$('stationCycle').textContent=station?.cycle_id?'Cycle: '+station.cycle_id:'';
  $('captureInspect').textContent=station?.state==='JUDGING'?'Inspecting...':'Capture & Inspect';
  const step2Enabled=station?.dmz?.step2_enabled!==false;
  $('confirmResult').textContent=station?.state==='UPLOADING'?'Sending Step2...':step2Enabled?'Confirm OK & Send SFIS Step2':'SFIS Step2 disabled';
  $('terminateInspection').textContent='Terminate Inspection';
  const sifiConfigured=station?.dmz?.configured===true;
  const step2Blocked=!browserVideoActive()&&!step2Enabled;
  $('sifiStatus').textContent=browserVideoActive()?'Browser test: SFIS upload disabled':step2Blocked?'SFIS Step2 not enabled — cannot post. Contact the web developer SMD (Dre Hsu / TW031561).':sifiConfigured?'SFIS configured':'SFIS not configured';
  $('sifiStatus').className='stationMeta '+(sifiConfigured&&!step2Blocked?'sifiGood':'sifiWarn');
  $('stationError').textContent=[stationLocalError,textValue(station?.last_error)].filter(Boolean).join('\n');
  const scanner=station?.scanner,panel=$('scannerPanel');
  let scannerClass=' manual',scannerTitle='Scan or type a serial number',scannerDetail='Focus the SN field when using a scanner connected to this PC.';
  if(scanner?.connected){
    scannerClass=' good';scannerTitle='Scanner connected';
    scannerDetail=scanner.scan_count?scanner.scan_count+' scans received':'Ready for the next serial number';
  }else if(scanner?.enabled){
    scannerClass=' warn';scannerTitle='SmartCam scanner not connected';
    scannerDetail=textValue(scanner.last_error)||'Check the powered USB hub, or scan into the SN field from this PC.';
  }
  panel.className='scannerPanel'+scannerClass;$('scannerTitle').textContent=scannerTitle;$('scannerDetail').textContent=scannerDetail;
  const matching=station?.matching||{};$('stableLabel').textContent=freshStation?matching.stable||'UNKNOWN':'Unavailable';$('rawLabel').textContent=freshStation?matching.raw||'UNKNOWN':'—';
  const result=station?.last_result;
  $('lastResult').textContent=result?'Last '+(result.test_mode?'test':'inspection')+': '+result.sn+' · '+(result.visual_result||result.result||result.cycle_status||'CLOSED')+' · '+(result.upload_status||result.reason||'Saved locally'):'';
  const activeCycle=available&&station.state!=='WAITING_SN';
  if(!fresh||activeCycle)$('cameraToggle').disabled=true;
  if(!fresh){$('snapshotBtn').disabled=true;$('recordToggle').disabled=true}
  if(!freshStation&&station){$('verdict').textContent='Offline';$('verdict').className='state warn';$('flowPct').textContent='Unavailable'}
  renderInput();
  renderTemperature();
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
  const canStartVideo=videoTestEnabled&&controlsReady&&!browserActive&&!!videoChoice&&['WAITING_SN','WAITING_CAPTURE','NOT_MATCHED'].includes(stationState);
  const canAttachVideo=videoTestEnabled&&controlsReady&&['WAITING_SN','WAITING_CAPTURE','NOT_MATCHED'].includes(stationState);
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

async function submitSN(){
  const station=lastStatus.station;
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.state!=='WAITING_SN')return;
  const sn=fixedSNBuffer;if(!sn){$('snInput').focus();return}
  stationBusy=true;stationLocalError='';renderStation(station);
  try{const data=await req(APP_BASE+'/api/apl/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sn})});if(data.success===false)throw Error(data.error||'SFIS Step1 did not accept the serial number');if(data.station)lastStatus.station=data.station;setFixedSN('');message('SFIS Step1 accepted. Complete the fixture, then select Capture & Inspect.','ok')}
  catch(error){stationLocalError=error.name==='AbortError'?'Request timed out. Check the current serial number before retrying.':error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station)}
}

async function submitOperator(){
  const station=lastStatus.station;
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.state!=='WAITING_SN')return;
  const emp=$('opInput').value.trim();if(!emp){$('opInput').focus();return}
  stationBusy=true;stationLocalError='';renderStation(station);
  try{const data=await req(APP_BASE+'/api/apl/operator',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emp_no:emp})});if(data.success===false)throw Error(data.error||'Employee number was not accepted');if(data.station)lastStatus.station=data.station;$('opInput').value='';message('Operator signed in. Scan a serial number.','ok')}
  catch(error){stationLocalError=error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);if(!$('snInput').disabled)$('snInput').focus()}
}

async function switchOperator(){
  const station=lastStatus.station;
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.state!=='WAITING_SN')return;
  stationBusy=true;stationLocalError='';renderStation(station);
  try{const data=await req(APP_BASE+'/api/apl/operator/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(data.success===false)throw Error(data.error||'Could not switch operator');if(data.station)lastStatus.station=data.station;message('Enter the next operator employee number.','ok')}
  catch(error){stationLocalError=error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);if(!$('opInput').disabled)$('opInput').focus()}
}

async function stationAction(action){
  const station=lastStatus.station,permission={'confirm-result':'can_confirm_result',terminate:'can_terminate'}[action];
  if(!permission||!stationFresh()||stationBusy||station?.request_in_flight||station?.[permission]!==true)return;
  if(action==='terminate'&&!confirm('Terminate this inspection without uploading?\nSN: '+(station.sn||'—')+'\nThe saved inspection evidence will be retained.'))return;
  let focusNextSN=false;
  stationBusy=true;stationLocalError='';renderStation(station);
  try{const data=await req(APP_BASE+'/api/apl/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cycle_id:station.cycle_id}),timeoutMs:30000});if(data.success===false)throw Error(data.error||'Action did not complete');if(data.station)lastStatus.station=data.station;if(data.station?.state==='WAITING_SN'){setFixedSN('');focusNextSN=true}message(action==='confirm-result'?'SFIS Step2 accepted. Ready for the next serial number.':'Inspection terminated without upload.','ok');if(currentPage==='history')loadHistory()}
  catch(error){stationLocalError=error.name==='AbortError'?'Request timed out. Wait for the current status before retrying.':error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station);if(focusNextSN&&!$('snInput').disabled)$('snInput').focus()}
}

async function captureInspection(){
  const station=lastStatus.station;
  const browserCapture=browserVideoActive();
  if(!stationFresh()||stationBusy||station?.request_in_flight||station?.can_capture!==true)return;
  if(browserCapture&&(!browserVideoReady||!videoChoice))return;
  if(!browserCapture&&!lastStatus.has_raw)return;
  stationBusy=true;stationLocalError='';renderStation(station);
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
    message(accepted?(browserCapture?'Inspection OK. Terminate this test when finished.':'Inspection OK. Confirm upload or terminate.'):'Not matched. Adjust the product and capture again.',accepted?'ok':'warn');
  }catch(error){stationLocalError=error.name==='AbortError'?'Inspection timed out. Check the current status before retrying.':error.message}
  finally{stationBusy=false;await pollOnce();renderStation(lastStatus.station)}
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
$('snInput').addEventListener('keydown',captureFixedSNKey,true);
$('snInput').addEventListener('paste',captureFixedSNPaste,true);
for(const eventName of ['beforeinput','compositionstart','compositionupdate','compositionend','drop'])$('snInput').addEventListener(eventName,blockNativeSNInput,true);
$('snInput').addEventListener('input',event=>{event.stopImmediatePropagation();$('snInput').value=fixedSNBuffer},true);
const browserVideo=$('testVideo');
browserVideo.addEventListener('timeupdate',()=>{if(!videoSeeking){$('videoSeek').value=String(browserVideo.currentTime||0);updateVideoProgress(browserVideo.currentTime,videoChoice?.duration_seconds||0)}if(browserCaptureTime!==null&&Math.abs(browserVideo.currentTime-browserCaptureTime)>0.001)clearBrowserResult()});
browserVideo.addEventListener('play',()=>{clearBrowserResult();renderInput();renderStreamStatus()});
browserVideo.addEventListener('pause',()=>{renderInput();renderStreamStatus()});
browserVideo.addEventListener('ended',()=>{renderInput();renderStreamStatus()});
window.addEventListener('pagehide',()=>{closeStream();browserVideo.pause();if(browserVideoURL)URL.revokeObjectURL(browserVideoURL)});
setInterval(()=>{renderStation(lastStatus.station);renderStreamStatus();updateSettingsActionState();checkStreamFrames();if(streamKind==='webrtc'&&streamStartedAt&&!streamRetryAt&&Date.now()-(streamDecodedAt||streamStartedAt)>15000)failStream('No video received for 15 seconds',streamGeneration);syncStream()},1000);
$('opInput').addEventListener('paste',event=>{const text=String(event.clipboardData?.getData('text/plain')||'').replace(/\s+/g,'');event.preventDefault();$('opInput').value=text.slice(0,Number($('opInput').maxLength||32))},true);
(async()=>{await pollOnce();pollLoop();await Promise.all([loadConfig(false),loadMedia(false)]);updateSnapshotLabel();const first=!$('opInput').disabled&&!$('operatorSignIn').classList.contains('hidden')?'opInput':!$('snInput').disabled?'snInput':'';if(first)$(first).focus()})().catch(error=>message(error.message,'err'));

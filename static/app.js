
/* ---------------- theme ---------------- */
try{const t=localStorage.getItem('mock-theme');if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}
/* Icons are decorative; labels and business state remain on their controls. */
function uiIcon(name,size=16,className=''){
  return window.MelodyIcons?window.MelodyIcons.svg(name,{size,className}):'';
}
function setActionLabel(element,name,label){
  if(!element)return;
  if(window.MelodyIcons)element.innerHTML=uiIcon(name)+esc(label);
  else element.textContent=label;
}
function beginTaskElapsed(render,current=()=>true){
  const started=Date.now();let timer=null,stopped=false;
  const stop=()=>{stopped=true;if(timer!==null){clearInterval(timer);timer=null;}};
  const tick=()=>{if(stopped)return;if(!current()){stop();return;}render(Math.max(0,Math.floor((Date.now()-started)/1000)));};
  tick();if(!stopped)timer=setInterval(tick,1000);
  return stop;
}

/* ---------------- toast ---------------- */
function toast(title,desc,kind){
  const tone=kind==='ok'?'success':kind==='warn'?'warning':kind==='danger'?'danger':'neutral';
  if(window.MelodyUI)return window.MelodyUI.toast(title+(desc?' · '+desc:''),{tone,duration:5000});
  const el=document.createElement('div');el.className='toast me-toast';el.dataset.tone=tone;el.setAttribute('role','status');
  el.textContent=title+(desc?' · '+desc:'');document.getElementById('toaster').appendChild(el);
  setTimeout(()=>el.remove(),5000);
}
/* ---------------- api helpers ---------------- */
const C=ExamContracts;
async function apiError(res){
  let body={};try{body=await res.json();}catch{}
  const info=C.error(body,res.status);return Object.assign(new Error(info.message),info);
}
async function apiGet(url,timeout=90000,signal){
  const deadline=AbortSignal.timeout(timeout);
  const res=await fetch(url,{cache:'no-store',signal:signal?AbortSignal.any([signal,deadline]):deadline});
  if(!res.ok)throw await apiError(res);return res.json();
}
async function apiSend(url,method,body){
  const res=await fetch(url,{method,headers:body!==undefined?{'Content-Type':'application/json'}:{},body:body!==undefined?JSON.stringify(body):undefined,signal:AbortSignal.timeout(900000)});
  if(!res.ok)throw await apiError(res);return res.status===204?null:res.json();
}
function fieldError(id,message){
  const el=document.getElementById(id);if(!el)return;
  el.setAttribute('aria-invalid',String(!!message));el.setCustomValidity?.(message);
  const note=document.getElementById(id+'-error');if(note)note.textContent=message;
}
let notificationEpoch=0;
const errorRegions=['appError','vocabularyFilesError','generationError','worksheetListError','editorError','previewError'];
const generationFields=['vocab','day','dayFrom','dayTo','groupFrom','groupTo','count','reps'];
function currentErrorEpoch(){return notificationEpoch;}
function showApiError(e,target,epoch){
  if(epoch!==undefined&&epoch!==notificationEpoch)return;
  const message=C.safeMessage(e.message,'요청을 완료하지 못했습니다. 연결 상태와 작업 목록을 확인해 주세요.');
  let placed=false;
  for(const [field,value] of Object.entries(e.fields||{})){
    const id=field==='vocab_file_id'?'vocab':field==='day_end'?(mode==='bulk'?'dayTo':'groupTo'):field==='day'?(mode==='group'?'groupFrom':mode==='bulk'?'dayFrom':'day'):field;
    if(generationFields.includes(id)&&document.getElementById(id+'-error')){fieldError(id,C.safeMessage(value,message));placed=true;}
  }
  if(placed)return;
  const box=document.getElementById(target||'appError');if(!box)return;
  box.textContent=message;box.hidden=false;
}
function clearError(target){
  if(!target){notificationEpoch++;generationFields.forEach(id=>fieldError(id,''));}
  for(const id of target?[target,'appError']:errorRegions){const box=document.getElementById(id);if(box){box.hidden=true;box.textContent='';}}
}

/* ---------------- drive CTA ---------------- */
let driveFolderLink=null;
async function initDriveChip(){
  try{
    const r=await apiGet('/api/drive-folder');
    driveFolderLink=r.web_link;
    setActionLabel(document.getElementById('driveChip'),'folder',r.email||'드라이브 연동됨');
  }catch(e){
    setActionLabel(document.getElementById('driveChip'),'warning','드라이브 연동 실패');
  }
}
async function openDrive(){
  clearError();const epoch=currentErrorEpoch();
  try{
    if(!driveFolderLink){const r=await apiGet('/api/drive-folder');driveFolderLink=r.web_link;}
    openDriveLink(driveFolderLink);
  }catch(e){showApiError(e,undefined,epoch);}
}
function openDriveLink(url){
  if(!url){openDrive();return;}
  const safe=C.driveURL(url);if(!safe){showApiError(Error('드라이브 링크 형식을 확인할 수 없습니다.'));return;}
  window.open(safe,'_blank','noopener,noreferrer');
}

/* ---------------- nav ---------------- */
let screenName='overview',navigationBusy=false,workspaceNavigationRevision=0;
function syncWorkspaceNavigation(){
  const current=isWordMeaningDocument()?wordMeaningHome:['editor','preview'].includes(screenName)?'dashboard':screenName;
  document.getElementById('shell').dataset.workspaceView=current;
  document.querySelectorAll('[data-screen]').forEach(button=>{
    const active=button.dataset.screen===current;
    button.classList.toggle('active',active);
    if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
  });
}
async function go(name,replaceEditor=false){
  if(!['overview','vocabulary','management','dashboard','editor','preview'].includes(name))return false;
  if(navigationBusy)return false;
  if(questionRegenerationBusy){toast('생성 중','문항 생성이 끝날 때까지 기다려 주세요.','warn');return false;}
  if(saveBusy){toast('저장 중','저장이 끝날 때까지 기다려 주세요.','warn');return false;}
  navigationBusy=true;
  try{
    if(isFeatureWorksheetKind(worksheetKind)&&((['overview','vocabulary','management','dashboard'].includes(name)&&!replaceEditor)||isWordMeaningDocument())){
      const embedded=getWordMeaningFrame().contentWindow?.melodyWordMeaning;
      if(embedded&&await embedded.home()===false)return false;
    }
    if(name===screenName&&!replaceEditor){
      workspaceNavigationRevision++;
      clearError();
      if(name==='dashboard'||name==='overview'){wordMeaningHome=name;setWordMeaningDocumentView('home');}
      if(name==='management')window.loadManagedWorksheets?.().catch(()=>{});
      if(name==='vocabulary'||name==='overview')window.loadVocabularyBanks?.().catch(()=>{});
      return true;
    }
    if(screenName==='editor' && isEditorDirty()){
      const choice=await askUnsavedChanges();
      if(choice==='stay')return false;
      if(choice==='save' && !(await save()))return false;
      if(choice==='discard'){
        questions=JSON.parse(savedQuestions);editorDocument.questions=questions;renderMini();renderEdit();updateEditStatus();
      }
    }
  clearError();
  if(screenName==='editor')invalidateEditor();
  if(screenName==='preview')stopLegacyPdfPreview();
  ['overview','vocabulary','management','dashboard','editor','preview'].forEach(s=>{
    const inOverview=name==='overview'&&(s==='vocabulary'||s==='dashboard');
    document.getElementById('screen-'+s).hidden=s!==name&&!inOverview;
  });
  document.getElementById('screen-dashboard').classList.toggle('meaning-document-active',false);
  screenName=name;
  workspaceNavigationRevision++;
  if(name==='overview'||name==='dashboard')wordMeaningHome=name;
  syncWorkspaceNavigation();
  if(name==='management')window.loadManagedWorksheets?.().catch(()=>{});
  if(name==='vocabulary'||name==='overview')window.loadVocabularyBanks?.().catch(()=>{});
  window.scrollTo(0,0);
  return true;
  }finally{navigationBusy=false;}
}

function askUnsavedChanges(){
  return new Promise(resolve=>{
    const dialog=document.getElementById('unsavedDialog');
    dialog.returnValue='stay';
    dialog.addEventListener('close',()=>resolve(dialog.returnValue||'stay'),{once:true});
    if(window.MelodyUI)window.MelodyUI.openDialog(dialog);else dialog.showModal();
  });
}
document.querySelectorAll('[data-leave]').forEach(button=>button.addEventListener('click',()=>document.getElementById('unsavedDialog').close(button.dataset.leave)));
document.getElementById('unsavedDialog').addEventListener('cancel',()=>{document.getElementById('unsavedDialog').returnValue='stay';});

/* ---------------- day selects ---------------- */
let mode='single';
function setMode(m){
  if(generationBusy||!['single','group','bulk'].includes(m))return;
  clearError();
  mode=m;
  for(const option of ['single','group','bulk']){
    document.getElementById('mode-'+option).setAttribute('aria-pressed',m===option);
    document.getElementById('panel-'+option).style.display=m===option?'flex':'none';
  }
  document.getElementById('modeHint').textContent=m==='group'?'선택한 범위의 어휘를 합쳐 시험지 한 부를 만듭니다.':m==='bulk'?'각 챕터마다 별도의 시험지를 만듭니다.':'선택한 챕터의 어휘로 시험지 한 부를 만듭니다.';
  updateGenerationSummary();updateLegacyCapabilities();
}
function scopeLabel(day,end=day){return (chapterUnits[day-1]?.label||'챕터 '+day)+(end!==day?' ~ '+(chapterUnits[end-1]?.label||'챕터 '+end):'');}
function bankRead(key,loader,force=false){return window.MelodyBankData?window.MelodyBankData.get(key,loader,{force}):loader();}
function updateGenerationSummary(){
  const fromId=mode==='group'?'groupFrom':mode==='bulk'?'dayFrom':'day';
  const toId=mode==='group'?'groupTo':mode==='bulk'?'dayTo':fromId;
  const from=C.integer(document.getElementById(fromId).value,1,365),to=C.integer(document.getElementById(toId).value,1,365);
  const count=C.integer(document.getElementById('count').value,1,20),reps=mode==='bulk'?C.integer(document.getElementById('reps').value,1,5):1;
  const summary=document.getElementById('generationSummary');
  if(daysLoading||!availableDays.length){summary.textContent='단어장의 챕터를 불러온 뒤 범위를 선택해 주세요.';return;}
  if([from,to,count,reps].includes(null)||to<from||Array.from({length:to-from+1},(_,i)=>from+i).some(d=>!availableDays.includes(d))){summary.textContent='파일에 있는 올바른 챕터 범위와 문항 수를 선택해 주세요.';return;}
  summary.textContent=scopeLabel(from,to)+' · '+(mode==='bulk'?'각 '+count+'문항 · 시험지 '+((to-from+1)*reps)+'부':'총 '+count+'문항 · 시험지 1부');
}
for(const id of ['day','dayFrom','dayTo','groupFrom','groupTo','count','reps'])document.getElementById(id).addEventListener('input',()=>{clearError();updateGenerationSummary();});
let legacyCapabilitiesSerial=0,legacySelectionReady=null,legacyPreparationMessage='',legacyCapabilitiesLoading=false;
function syncLegacyGenerationAvailability(){
  for(const id of ['genBtn','genGroupBtn','genBulkBtn']){
    const button=document.getElementById(id);if(button)button.disabled=generationBusy||legacyCapabilitiesLoading||legacySelectionReady===false;
  }
}
function legacyPreparationText(result){
  if(result.selection_ready===true)return '선택 챕터 준비 완료';
  if(result.preparation_state==='needs_review')return '학습자료 확인이 필요합니다. 단어장 수정 화면에서 확인해 주세요.';
  if(result.preparation_state==='failed')return '학습자료 준비에 실패했습니다. 작업 상태를 확인한 뒤 재시도해 주세요.';
  if(result.preparation_state==='unavailable')return '학습자료 준비 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.';
  return '기존 어휘 시험용 학습자료를 준비 중입니다.';
}
async function updateLegacyCapabilities({background=false}={}){
  const serial=++legacyCapabilitiesSerial,notice=document.getElementById('legacyCapabilitiesNotice'),bankId=document.getElementById('vocab').value;
  if(!notice)return false;
  const fromId=mode==='group'?'groupFrom':mode==='bulk'?'dayFrom':'day',toId=mode==='group'?'groupTo':mode==='bulk'?'dayTo':fromId;
  const from=Number(document.getElementById(fromId).value),to=Number(document.getElementById(toId).value);
  const units=chapterUnits.slice(Math.max(0,from-1),to);
  if(!bankId||daysLoading||!units.length){legacySelectionReady=false;legacyCapabilitiesLoading=false;syncLegacyGenerationAvailability();notice.textContent='단어장과 챕터를 선택하면 출제 가능한 유형을 확인합니다.';return false;}
  if(!background){legacyCapabilitiesLoading=true;syncLegacyGenerationAvailability();notice.textContent='선택한 챕터의 출제 가능 유형을 확인하고 있습니다…';}
  const query=new URLSearchParams();for(const unit of units)query.append('unit_ids',unit.id);
  try{
    const result=await apiGet('/api/legacy/banks/'+encodeURIComponent(bankId)+'/capabilities?'+query);if(serial!==legacyCapabilitiesSerial)return false;
    legacySelectionReady=result.selection_ready!==false;
    legacyPreparationMessage=Object.hasOwn(result,'selection_ready')?legacyPreparationText(result):(result.notice||'선택한 단어장 자료로 '+(result.available_types?.length||0)+'개 유형을 출제할 수 있습니다.');
    notice.dataset.preparationState=result.preparation_state||'legacy';
    notice.textContent=legacyPreparationMessage+(result.requires_review?' 원문 확인 필요 '+(result.review_count||0)+'개 · 페이지 확인 '+(result.page_issue_count||0)+'개. 단어장을 검토하거나 아래 제외 옵션을 선택하세요.':'');
    return legacySelectionReady;
  }catch(error){if(serial===legacyCapabilitiesSerial){legacySelectionReady=false;legacyPreparationMessage='학습자료 준비 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.';notice.dataset.preparationState='unavailable';notice.textContent=legacyPreparationMessage;}return false;}
  finally{if(serial===legacyCapabilitiesSerial){legacyCapabilitiesLoading=false;syncLegacyGenerationAvailability();}}
}
for(const id of ['day','dayFrom','dayTo','groupFrom','groupTo'])document.getElementById(id).addEventListener('change',updateLegacyCapabilities);
let legacyPreparationPolling=false;
setInterval(async()=>{
  if(legacyPreparationPolling||document.visibilityState==='hidden'||worksheetKind!=='standard'||!['overview','dashboard'].includes(screenName)||generationBusy||legacyCapabilitiesLoading)return;
  legacyPreparationPolling=true;try{await updateLegacyCapabilities({background:true});}finally{legacyPreparationPolling=false;}
},3000);
/* ---------------- 어휘 파일 ---------------- */
let vocabFiles=[];
let vocabSerial=0;
let availableDays=[],chapterUnits=[],daysLoading=false,daysSerial=0,loadedDaysBank='';
async function syncVocabSelection(force=false){
  const sel=document.getElementById('vocab'),previous=sel.value;sel.replaceChildren();
  for(const f of vocabFiles){const option=document.createElement('option');option.value=f.id;option.textContent=f.name;sel.appendChild(option);}
  if(!vocabFiles.length){const option=document.createElement('option');option.textContent='등록된 단어장 없음';option.value='';sel.appendChild(option);}
  sel.value=vocabFiles.some(f=>f.id===previous)?previous:(vocabFiles[0]?.id||'');
  if(force||previous!==sel.value||(!daysLoading&&!availableDays.length))await loadDays({preserve:previous===sel.value});
}
async function loadVocabList(force=false){
  const serial=++vocabSerial,epoch=currentErrorEpoch();clearError('vocabularyFilesError');
  try{
    const banks=await bankRead('/banks',()=>apiGet('/api/word-meaning/banks'),force);
    const list=banks.map(bank=>({...bank,name:bank.title||bank.source?.filename||'단어장'}));
    if(serial!==vocabSerial)return;
    if(!Array.isArray(list)||!list.every(f=>typeof f.id==='string'&&typeof f.name==='string'))throw Error('어휘 파일 목록을 확인할 수 없습니다.');
    vocabFiles=list;
    await syncVocabSelection(force);
  }catch(e){if(serial===vocabSerial)showApiError(e,'vocabularyFilesError',epoch);}
}
async function loadDays({preserve=false,bank:providedBank=null}={}){
  clearError('generationError');generationFields.forEach(id=>fieldError(id,''));
  const serial=++daysSerial,selected=document.getElementById('vocab').value,epoch=currentErrorEpoch();
  const previous=Object.fromEntries(['day','dayFrom','dayTo','groupFrom','groupTo'].map(id=>[id,preserve&&loadedDaysBank===selected?chapterUnits[Number(document.getElementById(id).value)-1]?.id:null]));
  availableDays=[];chapterUnits=[];daysLoading=true;
  ['day','dayFrom','dayTo','groupFrom','groupTo'].forEach(id=>{document.getElementById(id).replaceChildren();fieldError(id,'');});
  updateGenerationSummary();
  try{
    if(!selected)return;
    const bank=providedBank?.id===selected?providedBank:await bankRead('/banks/'+selected,()=>apiGet('/api/word-meaning/banks/'+encodeURIComponent(selected)+'?view=management'));
    const units=[...(bank.units||[])].sort((a,b)=>(a.order??0)-(b.order??0));
    const data={days:units.map((_,i)=>i+1)};
    if(serial!==daysSerial)return;
    if(!Array.isArray(data.days)||!data.days.length||!data.days.every(d=>Number.isInteger(d)&&d>=1&&d<=365))throw Error('이 단어장에서 사용할 수 있는 챕터를 찾지 못했습니다.');
    chapterUnits=units;availableDays=data.days;
    loadedDaysBank=selected;
    for(const id of ['day','dayFrom','dayTo','groupFrom','groupTo'])for(const d of availableDays){const o=document.createElement('option');o.value=d;o.textContent=chapterUnits[d-1]?.label||'챕터 '+d;document.getElementById(id).appendChild(o);}
    const contiguous=availableDays.filter((d,i)=>d===availableDays[0]+i);
    for(const id of ['dayTo','groupTo'])document.getElementById(id).value=contiguous[Math.min(9,contiguous.length-1)];
    for(const [id,unitId] of Object.entries(previous)){const index=units.findIndex(unit=>unit.id===unitId);if(index>=0)document.getElementById(id).value=String(index+1);}
    fieldError('vocab','');
  }catch(e){if(serial===daysSerial&&epoch===currentErrorEpoch())fieldError('vocab',C.safeMessage(e.message,'챕터 목록을 불러오지 못했습니다. 파일을 다시 선택해 주세요.'));}
  finally{if(serial===daysSerial){daysLoading=false;updateGenerationSummary();updateLegacyCapabilities();}}
}
document.getElementById('vocab').addEventListener('change',()=>{clearError();loadDays();});
function isFeatureWorksheetKind(kind){return kind==='word-meaning'||kind==='meaning-writing';}
function getWordMeaningFrame(kind=worksheetKind){return document.getElementById(kind==='meaning-writing'?'meaningWritingFrame':'wordMeaningFrame');}
function getWordMeaningFrames(){return ['wordMeaningFrame','meaningWritingFrame'].map(id=>document.getElementById(id)).filter(Boolean);}
async function notifyWordMeaningFrames(method,...args){
  await Promise.all(getWordMeaningFrames().map(frame=>frame.contentWindow?.melodyWordMeaning?.[method]?.(...args)));
}
let bankUpdateSerial=0;
window.addEventListener('melody-bank-updated',event=>{
  const bank=event.detail;
  if(!bank||typeof bank.id!=='string'||!Number.isInteger(bank.version)||!Array.isArray(bank.units)||!Array.isArray(bank.entries))return;
  const serial=++bankUpdateSerial;
  window.MelodyBankData?.invalidate('/banks/'+bank.id);
  (async()=>{
    const previous=vocabFiles.find(item=>item.id===bank.id);
    if(!previous||previous.version<=bank.version){
      vocabSerial++;
      vocabFiles=vocabFiles.map(item=>item.id===bank.id?{...item,title:bank.title,name:bank.title,version:bank.version,counts:bank.counts}:item);
      const select=document.getElementById('vocab'),option=[...select.options].find(item=>item.value===bank.id);
      if(option)option.textContent=bank.title;
      await Promise.all([
        select.value===bank.id?loadDays({preserve:true,bank}):undefined,
        notifyWordMeaningFrames('bankUpdated',bank),
      ]);
    }
  })().catch(()=>{if(serial===bankUpdateSerial)toast('단어장 저장 완료','출제 화면의 새로고침이 필요합니다.','warn');});
});
async function openWordMeaning(target,kind='word-meaning'){
  if(!isFeatureWorksheetKind(kind))return false;
  if(generationBusy||saveBusy||questionRegenerationBusy){toast('처리 중','진행 중인 작업이 끝난 뒤 시험지 유형을 변경해 주세요.','warn');return false;}
  if(meaningTransition)return false;
  meaningTransition=true;
  const frame=getWordMeaningFrame(kind),status=document.getElementById('wordMeaningStatus'),label=kind==='meaning-writing'?'뜻 쓰기 시험':'단어–뜻 시험';
  let navigation=workspaceNavigationRevision;
  const current=()=>navigation===workspaceNavigationRevision&&worksheetKind===kind;
  try{
    if(worksheetKind!==kind&&isFeatureWorksheetKind(worksheetKind)){
      const previous=getWordMeaningFrame().contentWindow?.melodyWordMeaning;
      if(previous&&await previous.leave()===false)return false;
    }
    if(!['overview','dashboard'].includes(screenName)&&!isWordMeaningDocument()&&!(await go('dashboard')))return false;
    if(['overview','dashboard'].includes(screenName))wordMeaningHome=screenName;
    navigation=workspaceNavigationRevision;
    applyWorksheetKind(kind);
    const params=new URLSearchParams({embedded:'1',worksheet_kind:kind==='meaning-writing'?'meaning_writing_test':'word_meaning_test'});
    if(target){for(const key of ['bank_id','job_id','source_file_id'])if(target[key])params.set(key,target[key]);}
    if(!frame.getAttribute('src')){
      if(!target){const fileId=document.getElementById('vocab').value;if(fileId)params.set('bank_id',fileId);}
      status.textContent=label+'을'+' 불러오는 중…';status.hidden=false;
      await new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{cleanup();reject(Error('화면을 불러오지 못했습니다. '+label+'을 다시 눌러 주세요.'));},30000);
        const cleanup=()=>{clearTimeout(timer);frame.removeEventListener('load',loaded);frame.removeEventListener('error',failed);};
        const loaded=()=>{cleanup();if(frame.contentWindow?.melodyWordMeaning){syncWordMeaningTheme();resolve();}else reject(Error(label+' 화면에 연결하지 못했습니다. 다시 눌러 주세요.'));};
        const failed=()=>{cleanup();reject(Error(label+' 화면에 연결하지 못했습니다. 다시 눌러 주세요.'));};
        frame.addEventListener('load',loaded);frame.addEventListener('error',failed);
        frame.src='/word-meaning?'+params.toString();
      });
    }else if(target){
      const child=frame.contentWindow?.melodyWordMeaning;
      if(!child)throw Error(label+' 화면을 불러오는 중입니다. 잠시 후 다시 눌러 주세요.');
      await child.ready.catch(()=>{});
      if(!current())return false;
      if(await child.openTarget(params)===false)return false;
    }
    if(!current())return false;
    if(frame.contentWindow?.melodyWordMeaning)frame.contentWindow.melodyWordMeaning.reportLayout?.();
    setWordMeaningDocumentView(frame.dataset.view||'home');
    status.hidden=true;
    return true;
  }catch(error){
    if(!current())return false;
    if(!frame.contentWindow?.melodyWordMeaning)frame.removeAttribute('src');
    status.textContent=error.message;status.hidden=false;return false;
  }finally{meaningTransition=false;}
}

let worksheetKind='standard',meaningTransition=false,kindTransition=false,wordMeaningHome='dashboard';
function syncWordMeaningTheme(){
  const theme=document.documentElement.getAttribute('data-theme');
  getWordMeaningFrames().forEach(frame=>frame.contentWindow?.melodyWordMeaning?.syncTheme?.(theme));
}
function isWordMeaningDocument(){return screenName==='word-meaning-editor'||screenName==='word-meaning-preview';}
function setWordMeaningDocumentView(view){
  const documentView=view==='editor'||view==='preview';
  const next=documentView?'word-meaning-'+view:wordMeaningHome;
  const main=documentView?'dashboard':wordMeaningHome;
  ['overview','vocabulary','management','dashboard','editor','preview'].forEach(s=>{
    const inOverview=main==='overview'&&(s==='vocabulary'||s==='dashboard');
    document.getElementById('screen-'+s).hidden=s!==main&&!inOverview;
  });
  document.getElementById('screen-dashboard').classList.toggle('meaning-document-active',documentView);
  if(screenName!==next){screenName=next;window.scrollTo(0,0);}
  syncWorkspaceNavigation();
}
function updateWordMeaningLayout(info,sourceWindow){
  const frame=getWordMeaningFrame();
  if(sourceWindow!==frame.contentWindow||!info||!Number.isFinite(info.height)||info.height<1)return;
  const view=info.view||(info.editor?'editor':'home');
  if(!['home','editor','preview'].includes(view))return;
  frame.dataset.view=view;frame.style.height=Math.ceil(info.height)+'px';frame.style.minHeight='0';
  if(!navigationBusy&&isFeatureWorksheetKind(worksheetKind)&&(['overview','dashboard'].includes(screenName)||isWordMeaningDocument())){
    if(screenName==='overview'||screenName==='dashboard')wordMeaningHome=screenName;
    setWordMeaningDocumentView(view);
  }
}
function applyWorksheetKind(kind){
  const meaning=isFeatureWorksheetKind(kind),next=meaning?kind:'standard';
  if(worksheetKind!==next)clearError();
  if(!meaning&&isWordMeaningDocument())setWordMeaningDocumentView('home');
  worksheetKind=next;
  document.getElementById('kind-standard').setAttribute('aria-pressed',String(!meaning));
  document.getElementById('kind-word-meaning').setAttribute('aria-pressed',String(kind==='word-meaning'));
  document.getElementById('kind-meaning-writing')?.setAttribute('aria-pressed',String(kind==='meaning-writing'));
  getWordMeaningFrames().forEach(frame=>{frame.hidden=frame!==getWordMeaningFrame(kind);});
  document.getElementById('standardGenerationPanel').hidden=meaning;
  document.getElementById('standardWorksheetList').hidden=meaning;
  document.getElementById('wordMeaningPanel').hidden=!meaning;
  document.getElementById('generationDescription').textContent=kind==='meaning-writing'?'등록한 단어장의 단어를 보고 뜻을 직접 쓰는 시험지를 만듭니다.':meaning?'등록한 단어장의 뜻으로 시험지를 자동 생성합니다.':'등록한 단어장으로 문항을 생성하고 저장합니다';
  document.getElementById('generationSection').classList.toggle('meaning-active',meaning);
  const help=document.getElementById('worksheetKindHelp');if(help)help.textContent=kind==='meaning-writing'?'영단어를 보고 한국어 뜻을 직접 씁니다. 선택 범위에서 단어와 문항 순서를 항상 무작위로 정합니다.':'단어–뜻 시험은 영단어 아래 보기에서 맞는 뜻을 모두 고릅니다. Excel과 PDF 단어장을 사용할 수 있습니다.';
}
async function setWorksheetKind(kind){
  if(isFeatureWorksheetKind(kind))return openWordMeaning(undefined,kind);
  if(kindTransition)return false;kindTransition=true;
  try{
    const child=getWordMeaningFrame().contentWindow?.melodyWordMeaning;
    if(isFeatureWorksheetKind(worksheetKind)&&child&&await child.leave()===false)return false;
    applyWorksheetKind('standard');return true;
  }finally{kindTransition=false;}
}
window.addEventListener('pageshow',()=>applyWorksheetKind(worksheetKind));

/* ---------------- 만들어진 시험지 목록 ---------------- */
const TRASH_SVG='<svg class="me-icon" data-size="16" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="/static/design-system/icons.svg#me-icon-trash"></use></svg>';
let sheets=[];
let worksheetSortKey='created',worksheetSortDirection='desc';
function setWorksheetSort(value){
  if(!['latest','created','name'].includes(value))return;
  worksheetSortKey=value;renderList();
}
function toggleWorksheetSortDirection(){
  worksheetSortDirection=worksheetSortDirection==='desc'?'asc':'desc';
  const button=document.getElementById('worksheetSortDirection');
  window.MelodyWorksheetList.direction(button,worksheetSortDirection);
  renderList();
}
function sortedWorksheets(items=sheets){return window.MelodyWorksheetList.sort(items,worksheetSortKey,worksheetSortDirection);}
const pdfRebuilds=new Map();
let worksheetListSerial=0;
async function loadWorksheets(){
  const serial=++worksheetListSerial,epoch=currentErrorEpoch();clearError('worksheetListError');
  try{
    const response=await apiGet('/api/worksheets');if(serial!==worksheetListSerial)return;if(!Array.isArray(response))throw Error('시험지 목록 형식을 확인할 수 없습니다.');sheets=response;
    renderList();
  }catch(e){if(serial===worksheetListSerial)showApiError(e,'worksheetListError',epoch);}
}
function fmtDate(iso){
  if(!iso) return '-';
  const d=new Date(iso);
  if(!Number.isFinite(d.getTime()))return '-';
  return (d.getMonth()+1)+'/'+String(d.getDate()).padStart(2,'0');
}
function worksheetDateLabel(sheet){
  const created=Date.parse(sheet.created_at||sheet.created),updated=Date.parse(sheet.updated_at||sheet.updated);
  if(Number.isFinite(updated)&&(!Number.isFinite(created)||updated>created))return '수정 '+fmtDate(sheet.updated_at||sheet.updated);
  if(Number.isFinite(created))return '생성 '+fmtDate(sheet.created_at||sheet.created);
  return '날짜 미기록';
}
function renderList(){
  const box=document.getElementById('wslist');box.replaceChildren();
  if(!sheets.length){const p=document.createElement('p');p.className='empty-list me-empty';p.textContent='표시할 시험지가 없습니다. 위에서 문항을 생성하세요.';box.appendChild(p);return;}
  sortedWorksheets().forEach(s=>{
    if(!Number.isInteger(s.day)||!Number.isInteger(s.version)||typeof s.title!=='string'){showApiError(Error('시험지 목록의 일부 항목 형식을 확인할 수 없습니다.'),'worksheetListError');return;}
    const row=document.createElement('div');row.className='wsrow';row.dataset.key=s.day+'-'+s.version;
    const end=Number.isInteger(s.day_end)&&s.day_end>s.day?s.day_end:s.day;
    row.innerHTML='<div class="daybadge'+(end!==s.day?' range-badge':'')+'"><div><b>'+String(s.day).padStart(2,'0')+(end!==s.day?'~'+String(end).padStart(2,'0'):'')+'</b><span>DAY</span></div></div><div class="meta"><div class="t"></div><div class="s"></div><div class="source"></div></div><div class="acts" data-acts></div>';
    if(s.source_unit_labels?.length){row.querySelector('.daybadge b').textContent=s.source_unit_labels.length+'개';row.querySelector('.daybadge span').textContent='챕터';}
    row.querySelector('.t').textContent=s.title;
    row.querySelector('.s').textContent=(s.range_label||s.source_unit_labels?.join(' · ')||'DAY '+s.day)+' · '+worksheetDateLabel(s)+' · 문항 '+s.count+'개';
    row.querySelector('.source').textContent=s.source_name||'원본 파일명 미기록';
    const acts=row.querySelector('[data-acts]');
    const actionIcons={'미리보기':'preview','수정':'edit','PDF 다운로드':'download','드라이브에서 열기':'folder','취소':'close','삭제':'trash'};
    const add=(text,cls,action)=>{const button=document.createElement('button');button.type='button';button.className=cls+(cls.includes('icon-btn')?' me-icon-button':' me-button');button.dataset.size='sm';button.dataset.variant=cls.includes('danger')?'danger':cls.includes('accent')?'edit':'secondary';setActionLabel(button,actionIcons[text]||'trash',text);button.addEventListener('click',()=>action(button));acts.appendChild(button);return button;};
    add('미리보기','btn sm',()=>openPreview(s.day,s.version)).disabled=!!s.deletion_pending;
    add('수정','btn sm accent',()=>openEditor(s.day,s.version)).disabled=!!s.deletion_pending;
    for(const [kind,label] of [['student','문제지'],['answer','정답지'],['combined','전체']]){
      const download=add(label,'btn sm outline',button=>downloadPdf(s.day,s.version,button,kind));
      setActionLabel(download,'download',label);download.dataset.pdfKey=row.dataset.key;download.dataset.pdfKind=kind;
      download.disabled=!!s.deletion_pending||!s.file_id||pdfRebuilds.has(row.dataset.key);
      if(!s.file_id)download.title='미리보기에서 PDF를 먼저 재생성해 주세요.';
    }
    const drive=add('','icon-btn sm',async()=>{try{await window.MelodyWorksheetDownload.openDrive({...s,kind:'standard'});}catch(error){showApiError(error,'worksheetListError');}});
    drive.dataset.variant='secondary';drive.title='구글 드라이브에서 열기';drive.setAttribute('aria-label',s.title+' · 구글 드라이브에서 열기');drive.innerHTML=window.MelodyIcons.svg('google-drive',{size:20});drive.disabled=!!s.deletion_pending;
    const del=add('','icon-btn sm danger',()=>openWorksheetDeletion({...s,kind:'standard'}));del.title='시험지 삭제';del.setAttribute('aria-label',s.title+' 삭제');del.innerHTML=TRASH_SVG;
    box.appendChild(row);
  });
}
async function deleteWorksheetRecord(item,mode){
  const meaning=isFeatureWorksheetKind(item.kind);
  const path=meaning?'/api/word-meaning/worksheets/'+encodeURIComponent(item.id):'/api/worksheet/'+item.day+'/'+item.version;
  const result=await apiSend(path,'DELETE',meaning?{mode,version:item.version}:{mode,token:item.deletion_token});
  if(!result?.deleted)throw Error('삭제 결과를 확인하지 못했습니다. 목록을 새로고침해 주세요.');
  if(!meaning){sheets=sheets.filter(sheet=>!(sheet.day===item.day&&sheet.version===item.version));renderList();}
  window.removeManagedWorksheet?.(item);
  if(meaning)await notifyWordMeaningFrames('worksheetDeleted',item.id);
  return result;
}
function openWorksheetDeletion(item){
  return window.MelodyWorksheetDelete.open({title:item.title,onDelete:async mode=>{
    try{await deleteWorksheetRecord(item,mode);}
    catch(error){await Promise.allSettled([loadWorksheets(),window.loadManagedWorksheets?.()]);throw error;}
  }});
}

/* ---------------- validated generation + durable retry ---------------- */
let generationBusy=false,batch=null;
window.addEventListener('beforeunload',e=>{if(generationBusy||questionRegenerationBusy||saveBusy||isEditorDirty()){e.preventDefault();e.returnValue='';}});
function readInteger(id,min,max,label){
  const field=document.getElementById(id),value=C.integer(field.value,min,max);
  fieldError(id,value===null?label+'는 '+min+'~'+max+' 사이의 정수로 입력해 주세요.':'');
  if(value===null)field.focus();return value;
}
['count','reps'].forEach(id=>document.getElementById(id).addEventListener('input',()=>fieldError(id,'')));
function generationInput(kind){
  const bulk=kind===true,group=kind==='group';
  clearError();
  const count=readInteger('count',1,20,'문항 수'),vocabId=document.getElementById('vocab').value;
  const file=vocabFiles.find(f=>f.id===vocabId);
  if(!file||daysLoading||!availableDays.length){fieldError('vocab','챕터 목록을 불러온 단어장을 선택해 주세요.');return null;}
  const from=readInteger(group?'groupFrom':bulk?'dayFrom':'day',1,365,'시작 챕터'),to=group||bulk?readInteger(group?'groupTo':'dayTo',1,365,'끝 챕터'):from;
  const reps=bulk?readInteger('reps',1,5,'챕터당 부수'):1;
  if([count,from,to,reps].includes(null))return null;
  const days=availableDays.filter(d=>d>=from&&d<=to);
  if(to<from||!availableDays.includes(from)||!availableDays.includes(to)||days.length!==to-from+1){fieldError(group?'groupTo':bulk?'dayTo':'day','선택한 파일에 있는 올바른 챕터 범위를 선택해 주세요.');return null;}
  if(!group&&days.length*reps>100){fieldError('reps','한 번에 최대 100부까지 생성할 수 있습니다.');return null;}
  return {count,bank_id:vocabId,provider:'script',...(document.getElementById('legacyAllowPartial').checked?{allow_partial:true}:{}),days:group?[from]:days,unit_groups:group?{[from]:days.map(day=>chapterUnits[day-1].id)}:Object.fromEntries(days.map(day=>[day,[chapterUnits[day-1].id]])),unit_labels:Object.fromEntries(days.map(day=>[day,chapterUnits[day-1].label])),...(group&&to!==from?{day_end:to}:{}),reps,source_name:file.name};
}
function persistBatch(){try{localStorage.setItem('melody-batch-v2',JSON.stringify(batch));}catch{}}
function renderJobs(){
  const panel=document.getElementById('jobPanel');panel.hidden=!batch;
  if(!batch){document.getElementById('bulkProgress').style.display='none';return;}
  const list=document.getElementById('jobList');list.replaceChildren();
  const labels={pending:'대기',running:'생성 중',done:'저장 완료',failed:'실패 · 재시도 가능',uncertain:'완료 여부 확인 필요',cancelled:'취소됨'};
  for(const task of batch.tasks){const li=document.createElement('li');li.dataset.state=task.state;li.textContent=(task.scope_label||scopeLabel(task.day,task.day_end??task.day))+' · '+task.copy+'부: '+labels[task.state]+(task.result?' (v'+task.result.version+')':'')+(task.error?' — '+task.error:'');list.appendChild(li);}
  document.getElementById('retryFailed').hidden=!batch.tasks.some(t=>t.state==='failed');
  document.getElementById('continuePending').hidden=!batch.tasks.some(t=>t.state==='pending');
  document.getElementById('checkJobs').hidden=!batch.tasks.some(t=>t.state==='uncertain');
  for(const id of ['retryFailed','continuePending','checkJobs'])document.getElementById(id).disabled=generationBusy;
  const cancel=document.getElementById('cancelJobs');
  cancel.hidden=!batch.tasks.some(t=>['pending','failed'].includes(t.state));
  cancel.disabled=batch.cancelRequested===true&&generationBusy;
  cancel.textContent=generationBusy?'현재 작업 완료 후 나머지 취소':'남은 작업 취소';
  document.getElementById('closeJobs').hidden=generationBusy||batch.tasks.some(t=>!['done','cancelled'].includes(t.state));
  const count=state=>batch.tasks.filter(t=>t.state===state).length,done=count('done');
  document.getElementById('bulkProgress').style.display='block';
  const percent=(done+count('failed')+count('cancelled'))/batch.tasks.length*100;
  document.getElementById('bulkBar').style.width=percent+'%';
  document.getElementById('bulkProgressMeter')?.setAttribute('aria-valuenow',String(Math.round(percent)));
  document.getElementById('bulkText').textContent=done+'/'+batch.tasks.length+'부 저장 완료'+['failed','cancelled','pending','running','uncertain'].filter(s=>count(s)).map(s=>' · '+labels[s]+' '+count(s)+'부').join('')+' · '+batch.source_name;
}
function lockGeneration(busy){
  generationBusy=busy;
  document.querySelectorAll('.generation-section button,.generation-section input,.generation-section select,#xlsx').forEach(el=>{
    if(el.closest('.job-actions'))return;
    if(busy){el.dataset.wasDisabled=String(el.disabled);el.disabled=true;}else{el.disabled=el.dataset.wasDisabled==='true';delete el.dataset.wasDisabled;}
  });
  syncLegacyGenerationAvailability();
  renderJobs();
}
function statusLine(message){document.getElementById('generationStatus').textContent=message;}
function updateBatchStatus(){
  if(!batch){statusLine('');return;}
  const count=state=>batch.tasks.filter(task=>task.state===state).length;
  const details=[['done','부 저장 완료'],['failed','부 실패 · 재시도 가능'],['uncertain','부 완료 여부 확인 필요'],['pending','부 대기'],['running','부 생성 중'],['cancelled','부 취소됨']]
    .filter(([state])=>count(state)).map(([state,label])=>count(state)+label);
  statusLine(details.join(' · '));
}
async function startBatch(bulk){
  if(generationBusy)return;
  if(!await updateLegacyCapabilities()){toast(legacyPreparationMessage||'단어장과 챕터를 선택해 주세요.','','warn');return;}
  const input=generationInput(bulk);if(!input)return;
  if(batch?.tasks.some(t=>['running','uncertain','failed','pending'].includes(t.state))){
    showApiError(Error('이전 작업이 남아 있습니다. 먼저 아래에서 이어 처리하거나 작업 상태를 확인해 주세요.'),'generationError');return;
  }
  const {days,reps,source_name,day_end,unit_groups,unit_labels,...params}=input;
  batch={params,source_name,tasks:days.flatMap(day=>Array.from({length:reps},(_,i)=>({day,unit_ids:unit_groups[day],scope_label:unit_labels[day]+(day_end?' ~ '+unit_labels[day_end]:''),...(day_end?{day_end}:{}),copy:i+1,request_id:crypto.randomUUID(),state:'pending'})))};
  persistBatch();
  const trigger=document.getElementById(bulk==='group'?'genGroupBtn':bulk?'genBulkBtn':'genBtn');
  window.MelodyUI?.setBusy(trigger,true,'생성 중…');
  try{await runBatch('pending');}finally{window.MelodyUI?.setBusy(trigger,false);}
}
async function reconcileTask(task){
  const status=await apiGet('/api/operations/'+task.request_id);
  if(status.state==='done'){task.result=C.result(status.result);task.state='done';task.error='';}
  else if(['generated','failed'].includes(status.state)){task.state='failed';task.error=status.state==='generated'?'문항 생성 완료 · 저장부터 재시도합니다.':'다시 시도할 수 있습니다.';}
  else if(status.state==='not_found'){task.state='pending';task.error='';}
  else{task.state='uncertain';task.error='서버 처리 중 또는 완료 여부 확인이 필요합니다. 중복 생성을 막기 위해 자동 재요청하지 않습니다.';}
}
async function runBatch(state){
  if(generationBusy||!batch)return;
  batch.cancelRequested=false;persistBatch();clearError();lockGeneration(true);
  const epoch=currentErrorEpoch();
  try{
    for(const task of batch.tasks.filter(t=>t.state===state)){
      if(batch.cancelRequested||task.state!==state)break;
      task.state='running';task.error='';persistBatch();renderJobs();
      const stopElapsed=beginTaskElapsed(seconds=>{
        const scope=task.scope_label||scopeLabel(task.day,task.day_end??task.day);
        const notice=batch.cancelRequested?'남은 작업은 취소했습니다. 현재 결과를 저장할 때까지 창을 열어두세요.':'완료될 때까지 창을 열어두세요.';
        statusLine(scope+' · 스크립트 출제 · 생성 및 저장 처리 중 · '+seconds+'초 경과. '+notice);
      });
      try{
        task.result=C.result(await apiSend('/api/generate','POST',{...batch.params,...(batch.params.bank_id?{unit_ids:task.unit_ids}:{day:task.day,...(task.day_end?{day_end:task.day_end}:{})}),request_id:task.request_id}));task.state='done';
      }catch(e){
        const message=C.safeMessage(e.message);
        task.state='uncertain';task.error=message;
        try{await reconcileTask(task);}catch{}
        if(task.state!=='done'){
          if(!task.error.includes(message))task.error=[task.error,message].filter(Boolean).join(' · ');
          throw e;
        }
      }finally{stopElapsed();persistBatch();renderJobs();}
    }
    statusLine(batch.cancelRequested?'현재 작업을 마쳤습니다. 나머지 작업은 취소됐으며 저장된 시험지는 유지됩니다.':'선택한 작업의 생성·저장을 마쳤습니다.');
    toast(batch.cancelRequested?'남은 작업 취소 완료':'저장 완료','시험지 목록에서 저장된 PDF와 문항을 확인하세요.','ok');
  }catch(e){
    updateBatchStatus();
    const message=C.safeMessage(e.message);
    if(!batch.tasks.some(task=>task.error&&task.error.includes(message)))showApiError(e,'generationError',epoch);
  }
  finally{await loadWorksheets();lockGeneration(false);renderJobs();}
}
async function checkJobs(){
  if(generationBusy||!batch)return;clearError();const epoch=currentErrorEpoch();lockGeneration(true);
  try{for(const task of batch.tasks.filter(t=>['running','uncertain'].includes(t.state)))await reconcileTask(task);persistBatch();}
  catch(e){showApiError(e,'generationError',epoch);}finally{lockGeneration(false);renderJobs();updateBatchStatus();await loadWorksheets();}
}
function generate(){return startBatch(false);}
function generateBulk(){return startBatch(true);}
function generateGrouped(){return startBatch('group');}
document.getElementById('retryFailed').addEventListener('click',()=>runBatch('failed'));
document.getElementById('continuePending').addEventListener('click',()=>runBatch('pending'));
document.getElementById('checkJobs').addEventListener('click',checkJobs);
function cancelRemainingJobs(){
  if(!batch)return;
  const remaining=batch.tasks.filter(t=>['pending','failed'].includes(t.state));
  if(!remaining.length)return;
  batch.cancelRequested=true;
  for(const task of remaining){task.state='cancelled';task.error='';}
  persistBatch();renderJobs();
  statusLine(generationBusy?'남은 '+remaining.length+'부를 취소했습니다. 현재 작업의 생성·저장은 계속됩니다. 창을 열어두세요.':'남은 '+remaining.length+'부를 취소했습니다. 이미 저장한 시험지는 유지됩니다.');
}
document.getElementById('cancelJobs').addEventListener('click',cancelRemainingJobs);
document.getElementById('closeJobs').addEventListener('click',()=>{
  if(generationBusy||!batch||batch.tasks.some(t=>!['done','cancelled'].includes(t.state)))return;
  batch=null;persistBatch();renderJobs();statusLine('완료 내역을 닫았습니다. 저장된 시험지는 아래 목록에 유지됩니다.');
});
try{
  const saved=JSON.parse(localStorage.getItem('melody-batch-v2')||'null');
  if(saved?.tasks?.length&&saved.tasks.length<=100&&['script','gpt','gemini'].includes(saved.params?.provider)&&saved.tasks.every(t=>Number.isInteger(t.day)&&t.day>=1&&t.day<=365&&(t.day_end===undefined||(Number.isInteger(t.day_end)&&t.day_end>=t.day&&t.day_end<=365))&&typeof t.request_id==='string'&&/^[a-zA-Z0-9-]{20,80}$/.test(t.request_id)&&['pending','running','done','failed','uncertain','cancelled'].includes(t.state))){
    batch=saved;for(const t of batch.tasks)if(t.state==='running'){t.state='uncertain';t.error='새로고침 전 진행 작업 · 서버 상태를 확인해 주세요.';}renderJobs();
  }
}catch{}

/* ---------------- 문항 렌더링 (편집 화면 실시간 미리보기) ---------------- */
const CIR=['','①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩'];
let questions=[];
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function instHTML(s){return esc(s).replace(/&lt;u&gt;(.*?)&lt;\/u&gt;/gs,'<u>$1</u>');}
function underlineWord(sentence,word){
  if(!word) return esc(sentence);
  const escaped=String(word).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const match=sentence.match(new RegExp('(?<![A-Za-z])'+escaped+'(?![A-Za-z])','i'));
  if(!match) return esc(sentence);
  const idx=match.index;
  const a=sentence.slice(0,idx),b=sentence.slice(idx,idx+word.length),c=sentence.slice(idx+word.length);
  return esc(a)+'<u>'+esc(b)+'</u>'+esc(c);
}

const NS='mini-';
function choicesHTML(cs){return '<div class="'+NS+'choices">'+cs.map((c,i)=>'<span class="c"><span class="'+NS+'cir">'+CIR[i+1]+'</span>'+esc(c)+'</span>').join('')+'</div>';}
function qBlock(q,no){
  let body='';
  if(q.type==='word_meaning'){
    if(q.payload.sentence)body='<div class="'+NS+'rule"></div><div class="'+NS+'ex">'+underlineWord(q.payload.sentence,q.payload.target||'')+'</div><div class="'+NS+'rule"></div>';
    body+=choicesHTML(q.payload.choices);
  }else if(q.type==='pair_ko'){
    body='<div class="'+NS+'pairs">'+q.payload.pairs.map((p,i)=>'<div class="p"><span class="'+NS+'cir">'+CIR[i+1]+'</span><span>'+esc(p.word)+'</span><span>–</span><span>'+esc(p.ko)+'</span></div>').join('')+'</div>';
  }else if(q.type==='blank_ko'){
    body='<div class="'+NS+'rule"></div><div class="'+NS+'ex">'+esc(q.payload.sentence)+'</div><div class="'+NS+'ex" style="color:#444">'+esc(q.payload.ko)+'</div><div class="'+NS+'rule"></div>'+choicesHTML(q.payload.choices);
  }else if(q.type==='def_match'){
    body='<div class="'+NS+'defg">'+q.payload.pairs.map((p,i)=>'<span class="'+NS+'cir">'+CIR[i+1]+'</span><span>'+esc(p.word)+'</span><span class="colon">:</span><span>'+esc(p.def)+'</span>').join('')+'</div>';
  }else if(q.type==='common_blank'){
    body='<div class="'+NS+'rule"></div>'+q.payload.sentences.map(s=>'<div class="'+NS+'ex">- '+esc(s)+'</div>').join('')+'<div class="'+NS+'rule"></div>'+choicesHTML(q.payload.choices);
  }else if(q.type==='passage_blank'){
    body='<div class="'+NS+'rule"></div><div class="'+NS+'ex">'+esc(q.payload.passage)+'</div><div class="'+NS+'rule"></div>'+choicesHTML(q.payload.choices);
  }else if(q.type==='sentence_match'){
    body='<div class="'+NS+'rule"></div>'+q.payload.items.map((it,i)=>
      '<div class="'+NS+'ex"><span class="'+NS+'cir">'+CIR[i+1]+'</span>'+underlineWord(it.en,it.u)+'</div>'+
      '<div class="'+NS+'ex" style="color:#444;padding-left:16px">'+esc(it.ko)+'</div>'
    ).join('')+'<div class="'+NS+'rule"></div>';
  }else if(q.type==='count_correct'){
    body='<table style="width:100%;border-collapse:collapse;margin:4px 0 6px;font-size:11px">'+
      q.payload.table.map((x,i)=>'<tr><td style="border:1px solid #111;padding:3px 5px;width:1%;white-space:nowrap;text-align:center">'+String.fromCharCode(97+i)+'.</td><td style="border:1px solid #111;padding:3px 6px;width:1%;white-space:nowrap;overflow-wrap:normal;word-break:normal">'+esc(x.word)+'</td><td style="border:1px solid #111;padding:3px 6px">'+esc(x.ko)+'</td></tr>').join('')+
      '</table>'+choicesHTML(q.payload.choices);
  }
  return '<div class="'+NS+'qb"><div class="'+NS+'qh"><span class="no">'+no+'.</span><span>'+instHTML(q.instruction)+'</span></div>'+body+'</div>';
}
function answerKeyHTML(){
  const l=questions.map((q,i)=>'<div class="'+NS+'ans-line"><b>'+(i+1)+'.</b> '+CIR[q.answer]+'</div>').join('');
  const r=questions.map((q,i)=>'<div class="'+NS+'ans-exp"><span class="an">'+(i+1)+'.</span><span>'+esc(q.explanation||'(해설 없음)')+'</span></div>').join('');
  return '<div class="'+NS+'ans-head"><b>[정답 및 해설]</b></div><div class="'+NS+'rule"></div><div class="'+NS+'ans-two"><div class="'+NS+'ans-l">'+l+'</div><div class="'+NS+'ans-r">'+r+'</div></div>';
}

function renderMini(){
  const qs=questions.map((q,i)=>qBlock(q,i+1)).join('');
  document.getElementById('miniPaper').innerHTML=
    '<div class="paper-mini">'+
    '<div style="text-align:right;font-size:9px;font-weight:800">Melody English</div>'+
    '<div style="background:#111;color:#fff;text-align:center;font-weight:800;font-size:13px;padding:6px 0;letter-spacing:1px;border-radius:2px">'+esc(editorDocument?.title||'')+'</div>'+
    '<div style="display:grid;grid-template-columns:1fr 1.8fr 1fr 1.8fr;border:1px solid #111;border-top:none;font-size:10px">'+
      '<div style="background:#8f8f8f;color:#fff;font-weight:700;text-align:center;padding:4px 9px">이름</div><div></div>'+
      '<div style="background:#8f8f8f;color:#fff;font-weight:700;text-align:center;padding:4px 9px">점수</div><div></div></div>'+
    '<div class="mini-cols">'+qs+'</div>'+
    '<div class="mini-rule" style="margin-top:10px"></div>'+answerKeyHTML()+
    '</div>';
}

/* ---------------- edit panel (editor screen) ---------------- */
function renderEdit(focusIndex){
  const box=document.getElementById('editList');
  const expanded=new Set([...box.querySelectorAll('details[open]')].map(d=>Number(d.dataset.qi)));
  box.innerHTML='';
  questions.forEach((q,qi)=>{
    const d=document.createElement('details');d.className='qcard me-panel';d.dataset.qi=qi;if(qi===focusIndex||expanded.has(qi)||(focusIndex===undefined&&!expanded.size&&qi===0))d.open=true;
    let fields='<div><span class="lbl">지시문</span><textarea class="ta me-input" rows="2" data-qi="'+qi+'" data-f="instruction">'+esc(q.instruction)+'</textarea></div>';
    if(q.type==='word_meaning'){
      const context=C.wordContext(q);
      if(context.structured||(context.required&&!context.sentence)){
        fields+='<div><span class="lbl">예문(영문)</span><textarea class="ta me-input" rows="3" data-qi="'+qi+'" data-f="sentence">'+esc(q.payload.sentence||'')+'</textarea></div>';
        fields+='<div><span class="lbl">대상 단어 · 예문에 밑줄로 표시</span><input class="cin me-input" data-qi="'+qi+'" data-f="target" value="'+esc(context.target||'')+'"></div>';
      }
    }
    if(q.type==='blank_ko'){
      fields+='<div><span class="lbl">예문(영문)</span><textarea class="ta me-input" rows="2" data-qi="'+qi+'" data-f="sentence">'+esc(q.payload.sentence)+'</textarea></div>';
      fields+='<div><span class="lbl">해석(국문)</span><textarea class="ta me-input" rows="1" data-qi="'+qi+'" data-f="ko">'+esc(q.payload.ko)+'</textarea></div>';
    }
    if(q.type==='passage_blank'){
      fields+='<div><span class="lbl">지문</span><textarea class="ta me-input" rows="4" data-qi="'+qi+'" data-f="passage">'+esc(q.payload.passage)+'</textarea></div>';
    }
    if(q.type==='common_blank'){
      q.payload.sentences.forEach((s,si)=>{fields+='<div><span class="lbl">예문 '+(si+1)+'</span><textarea class="ta me-input" rows="1" data-qi="'+qi+'" data-f="sent" data-i="'+si+'">'+esc(s)+'</textarea></div>';});
    }
    if(q.type==='sentence_match'){
      let sm='<div><span class="lbl">문장 보기 (라디오 = 해석이 불일치하는 문항)</span>';
      q.payload.items.forEach((it,ii)=>{
        sm+='<div class="sentence-edit-item">'
          +'<div class="sentence-edit-head">'
            +'<input type="radio" name="ans-'+qi+'" '+(q.answer===ii+1?'checked':'')+' data-qi="'+qi+'" data-ans="'+(ii+1)+'">'
            +'<span class="cirlab">'+CIR[ii+1]+'</span>'
          +'</div>'
          +'<textarea class="ta me-input" rows="2" data-qi="'+qi+'" data-f="item-en" data-i="'+ii+'" placeholder="영어 문장">'+esc(it.en)+'</textarea>'
          +'<div class="sentence-edit-translation">'
            +'<input class="cin me-input target-word-input" value="'+esc(it.u)+'" data-qi="'+qi+'" data-f="item-u" data-i="'+ii+'" placeholder="밑줄 단어">'
            +'<input class="cin me-input" value="'+esc(it.ko)+'" data-qi="'+qi+'" data-f="item-ko" data-i="'+ii+'" placeholder="(우리말 해석)">'
          +'</div>'
        +'</div>';
      });
      sm+='</div>';fields+=sm;
    }
    if(q.type==='count_correct'){
      let tb='<div><span class="lbl">단어-뜻 표</span>';
      q.payload.table.forEach((x,ti)=>{
        tb+='<div class="word-pair-edit">'
          +'<input class="cin me-input" value="'+esc(x.word)+'" data-qi="'+qi+'" data-f="table-word" data-i="'+ti+'">'
          +'<input class="cin me-input" value="'+esc(x.ko)+'" data-qi="'+qi+'" data-f="table-ko" data-i="'+ti+'">'
        +'</div>';
      });
      tb+='</div>';fields+=tb;
    }
    if(q.payload.choices){
      let ch='<div><span class="lbl">보기 (라디오 = 정답)</span>';
      q.payload.choices.forEach((c,ci)=>{
        ch+='<div class="choice-edit"><input type="radio" name="ans-'+qi+'" '+(q.answer===ci+1?'checked':'')+' data-qi="'+qi+'" data-ans="'+(ci+1)+'"><span class="cirlab">'+CIR[ci+1]+'</span><input class="cin me-input" value="'+esc(c)+'" data-qi="'+qi+'" data-f="choice" data-i="'+ci+'"></div>';
      });
      ch+='</div>';fields+=ch;
    }
    if(q.payload.pairs){
      const kk=q.type==='def_match'?'def':'ko';const kl=C.typeInfo(q)[1].replace('단어 / ','');
      let pr='<div><span class="lbl">보기 (라디오 = 정답) · 단어 / '+kl+'</span>';
      q.payload.pairs.forEach((p,pi)=>{
        pr+='<div class="pair-edit"><input type="radio" name="ans-'+qi+'" '+(q.answer===pi+1?'checked':'')+' data-qi="'+qi+'" data-ans="'+(pi+1)+'">'
          +'<input class="me-input" value="'+esc(p.word)+'" data-qi="'+qi+'" data-f="pword" data-i="'+pi+'">'
          +'<span class="sep">–</span>'
          +'<input class="me-input" value="'+esc(p[kk])+'" data-qi="'+qi+'" data-f="pval" data-i="'+pi+'"></div>';
      });
      pr+='</div>';fields+=pr;
    }
    fields+='<div><span class="lbl">해설</span><textarea class="ta me-input" rows="2" data-qi="'+qi+'" data-f="explanation">'+esc(q.explanation)+'</textarea></div>';
    const tabs=(q.alternatives?.length||0)>1?'<div class="candidate-tabs me-segmented" role="group" aria-label="'+(qi+1)+'번 문항 후보">'+q.alternatives.map((_,i)=>'<button type="button" data-candidate="'+i+'" data-qi="'+qi+'" aria-label="'+(qi+1)+'번 문항 후보 '+(i+1)+'" aria-pressed="'+(i===q.candidate_selected)+'">'+(i+1)+'</button>').join('')+'</div>':'';
    const busy=questionRegenerationBusy&&questionRegenerationTarget?.index===qi;
    d.innerHTML='<summary><span class="qn">'+(qi+1)+'</span><span class="tt">'+instHTML(q.instruction).replace(/<[^>]+>/g,'')+'</span><span class="typebadge me-badge" data-tone="neutral">'+esc(C.typeInfo(q)[0])+'</span><button type="button" class="question-ai me-icon-button" data-variant="soft" data-size="compact" data-regenerate="'+qi+'" title="스크립트로 새 문항 생성" aria-label="'+(qi+1)+'번 문항 스크립트 생성" aria-busy="'+busy+'">'+QUESTION_AI_ICON+'</button><span class="chev" aria-hidden="true"><svg class="me-icon" data-size="16" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="/static/design-system/icons.svg#me-icon-chevron-right"></use></svg></span></summary><div class="qbody">'+tabs+(busy?'<p id="questionGenerationStatus" class="candidate-status" role="timer" aria-live="off">생성 및 저장 처리 중 · 0초 경과</p>':'')+fields+'</div>';
    box.appendChild(d);
  });
  const fieldLabels={instruction:'지시문',sentence:'영어 예문',target:'밑줄 대상 단어',ko:'우리말 해석',passage:'영어 지문',explanation:'해설',sent:'예문',choice:'보기 내용',pword:'보기 영단어',pval:'보기 뜻 또는 정의','item-en':'보기 영어 문장','item-u':'보기 밑줄 단어','item-ko':'보기 우리말 해석','table-word':'단어 뜻 표 영단어','table-ko':'단어 뜻 표 뜻'};
  document.querySelectorAll('#editList input, #editList textarea, #editList button').forEach(el=>{
    el.disabled=saveBusy||questionRegenerationBusy;
    if(el.dataset.ans)el.setAttribute('aria-label',(+el.dataset.qi+1)+'번 문항 '+el.dataset.ans+'번 보기 정답 선택');
    else if(fieldLabels[el.dataset.f])el.setAttribute('aria-label',(+el.dataset.qi+1)+'번 문항 '+fieldLabels[el.dataset.f]+(el.dataset.i!==undefined?' '+(+el.dataset.i+1):''));
  });
}
document.getElementById('editList').addEventListener('input',e=>{
  if(editorLoading||saveBusy||questionRegenerationBusy||!editorDocument||questions!==editorDocument.questions)return;
  const t=e.target,qi=+t.dataset.qi;if(isNaN(qi))return;const q=questions[qi];const f=t.dataset.f,i=+t.dataset.i;
  if(!q)return;
  if(f==='instruction')q.instruction=t.value;
  else if(f==='sentence'){if(q.type==='word_meaning'&&!q.payload.target)q.payload.target=C.wordContext(q).target;q.payload.sentence=t.value;}
  else if(f==='target')q.payload.target=t.value;
  else if(f==='ko')q.payload.ko=t.value;
  else if(f==='passage')q.payload.passage=t.value;
  else if(f==='explanation')q.explanation=t.value;
  else if(f==='sent')q.payload.sentences[i]=t.value;
  else if(f==='choice')q.payload.choices[i]=t.value;
  else if(f==='pword')q.payload.pairs[i].word=t.value;
  else if(f==='pval')q.payload.pairs[i][q.type==='def_match'?'def':'ko']=t.value;
  else if(f==='item-en')q.payload.items[i].en=t.value;
  else if(f==='item-u')q.payload.items[i].u=t.value;
  else if(f==='item-ko')q.payload.items[i].ko=t.value;
  else if(f==='table-word')q.payload.table[i].word=t.value;
  else if(f==='table-ko')q.payload.table[i].ko=t.value;
  clearError('editorError');syncSelectedCandidate(q);saveError='';renderMini();
  updateEditStatus();
});
document.getElementById('editList').addEventListener('change',e=>{
  if(editorLoading||saveBusy||questionRegenerationBusy||!editorDocument||questions!==editorDocument.questions)return;
  const t=e.target;if(t.type!=='radio')return;const qi=+t.dataset.qi;if(!questions[qi])return;clearError('editorError');questions[qi].answer=+t.dataset.ans;syncSelectedCandidate(questions[qi]);saveError='';renderMini();updateEditStatus();
});

/* ---------------- open screens ---------------- */
let editorDocument=null,editorSerial=0,editorController=null,previewTarget=null;
let savedQuestions=null,saveBusy=false,editorLoading=false,saveError='';
let editorWorkStarted=null,stopEditorWorkElapsed=null;
function stopEditorElapsed(){stopEditorWorkElapsed?.();stopEditorWorkElapsed=null;editorWorkStarted=null;}
function startEditorElapsed(){
  stopEditorElapsed();editorWorkStarted=Date.now();
  stopEditorWorkElapsed=beginTaskElapsed(updateEditStatus,()=>screenName==='editor'&&(editorLoading||saveBusy));
}
function isEditorDirty(){return editorDocument!==null && savedQuestions!==null && JSON.stringify(editorDocument.questions)!==savedQuestions;}
function updateEditStatus(){
  const status=document.getElementById('editStatus');
  const elapsed=Math.max(0,Math.floor((Date.now()-(editorWorkStarted??Date.now()))/1000))+'초 경과';
  status.textContent=editorLoading?'시험지를 불러오는 중 · '+elapsed:questionRegenerationBusy?'문항 생성 중…':saveBusy?'시험지를 저장하는 중 · '+elapsed:saveError||(!editorDocument?'시험지를 선택해 주세요.':isEditorDirty()?'저장되지 않은 변경사항':'저장됨');
  const saveProgress=document.getElementById('editorSaveProgress'),saveHint=document.getElementById('editorSaveHint');
  if(saveProgress){saveProgress.hidden=!saveBusy;saveProgress.textContent=saveBusy?'시험지를 저장하고 있습니다 · '+elapsed:'';}
  if(saveHint)saveHint.hidden=saveBusy;
  status.dataset.dirty=String(isEditorDirty());
}
function invalidateEditor(){
  stopEditorElapsed();
  editorSerial++;editorController?.abort();editorController=null;
  editorDocument=null;savedQuestions=null;questions=[];editorLoading=false;saveError='';
  document.getElementById('saveBtn').disabled=true;
}
function isCurrentEditorLoad(token){return token===editorSerial&&screenName==='editor';}

async function openEditor(day,version){
  if(saveBusy||!(await go('editor',true))||screenName!=='editor')return;
  invalidateEditor();
  const token=editorSerial,controller=new AbortController();editorController=controller;
  editorLoading=true;clearError();
  document.getElementById('saveBtn').disabled=true;
  startEditorElapsed();
  document.getElementById('ed-title').textContent='시험지 불러오는 중…';
  document.getElementById('miniPaper').replaceChildren();
  document.getElementById('editList').innerHTML='<p class="editor-loading me-help" role="status">불러오는 중…</p>';
  try{
    const data=await apiGet('/api/worksheet/'+day+'/'+version,90000,controller.signal);
    if(!isCurrentEditorLoad(token))return;
    if(data.day!==day||data.version!==version)throw Error('요청한 시험지와 불러온 문서가 다릅니다. 목록에서 다시 열어 주세요.');
    if(typeof data.title!=='string'||!data.title)throw Error('시험지 제목을 확인할 수 없습니다.');
    const loaded=C.questions(data.questions,{checkContext:false});
    loaded.forEach(prepareCandidates);
    editorDocument=Object.seal({day,version,title:data.title,questions:loaded,loadToken:token});questions=editorDocument.questions;
    savedQuestions=JSON.stringify(editorDocument.questions);
    document.getElementById('ed-title').textContent=editorDocument.title;
    document.getElementById('editList').dataset.documentKey=day+'/'+version;
    document.getElementById('editList').dataset.loadToken=String(token);
    renderMini();renderEdit();
  }catch(e){if(isCurrentEditorLoad(token)){await go('dashboard');showApiError(e,'worksheetListError');}}
  finally{if(isCurrentEditorLoad(token)){stopEditorElapsed();editorLoading=false;editorController=null;document.getElementById('saveBtn').disabled=!editorDocument;updateEditStatus();}}
}
let previewSerial=0,legacyPreviewTask=null,legacyPreviewController=null,legacyPreviewRenderer=null;
function stopLegacyPdfPreview(){
  previewSerial++;legacyPreviewController?.abort();legacyPreviewController=null;
  legacyPreviewRenderer?.destroy();legacyPreviewRenderer=null;
  const task=legacyPreviewTask;legacyPreviewTask=null;task?.destroy().catch(()=>{});
}
async function openPreview(day,version){
  if(!(await go('preview')))return;
  clearError('previewError');
  stopLegacyPdfPreview();const serial=previewSerial,epoch=currentErrorEpoch();
  previewTarget={day,version};
  const entry=sheets.find(s=>s.day===day&&s.version===version);
  syncPreviewActions();
  document.getElementById('pv-title').textContent=(entry&&entry.title)||('DAY '+day);
  const pages=document.getElementById('pdfPages');
  pages.textContent='PDF를 불러오는 중입니다…';
  if(!(entry&&entry.file_id)){
    pages.textContent='저장된 PDF가 없습니다. 위의 PDF 재생성 버튼으로 만들어 주세요.';
    return;
  }
  const loadingMessage=document.createElement('p');loadingMessage.className='me-help';loadingMessage.setAttribute('role','status');pages.replaceChildren(loadingMessage);
  const stopLoading=beginTaskElapsed(seconds=>{loadingMessage.textContent='PDF를 불러오는 중입니다 · '+seconds+'초 경과';},()=>serial===previewSerial&&screenName==='preview');
  const controller=new AbortController();legacyPreviewController=controller;
  const failed=error=>{if(serial===previewSerial&&error.name!=='AbortError'){legacyPreviewRenderer?.destroy();legacyPreviewRenderer=null;const loaded=legacyPreviewTask;legacyPreviewTask=null;loaded?.destroy().catch(()=>{});pages.replaceChildren();showApiError(error,'previewError',epoch);}};
  try{
    const pdfjs=await import('/static/vendor/pdfjs/pdf.min.mjs');
    if(serial!==previewSerial)return;
    pdfjs.GlobalWorkerOptions.workerSrc='/static/vendor/pdfjs/pdf.worker.min.mjs';
    const response=await fetch('/api/worksheet/'+day+'/'+version+'/pdf?t='+Date.now(),{cache:'no-store',signal:AbortSignal.any([controller.signal,AbortSignal.timeout(90000)])});
    if(!response.ok)throw new Error('PDF를 불러오지 못했습니다.');
    const data=new Uint8Array(await response.arrayBuffer());if(serial!==previewSerial)return;
    const loadingTask=pdfjs.getDocument({data,isEvalSupported:false});legacyPreviewTask=loadingTask;
    const pdf=await loadingTask.promise;
    if(serial!==previewSerial)return;
    stopLoading();
    legacyPreviewRenderer=window.MelodyPdfPreview.create({pdf,pages,isCurrent:()=>serial===previewSerial&&screenName==='preview',onError:failed});
    await legacyPreviewRenderer.render();
  }catch(e){failed(e);}finally{
    stopLoading();
    if(legacyPreviewController===controller)legacyPreviewController=null;
  }
}
function openPreviewFromEditor(){if(editorDocument&&!editorLoading)return openPreview(editorDocument.day,editorDocument.version);}
function openEditorFromPreview(){if(previewTarget)return openEditor(previewTarget.day,previewTarget.version);}

/* PDF actions capture their target before any asynchronous work. */
function syncPreviewActions(){
  document.querySelectorAll('[data-pdf-key]').forEach(button=>{
    const key=button.dataset.pdfKey,entry=sheets.find(s=>s.day+'-'+s.version===key);
    button.disabled=!!entry?.deletion_pending||!entry?.file_id||pdfRebuilds.has(key);
    setActionLabel(button,'download',({student:'문제지',answer:'정답지',combined:'전체'})[button.dataset.pdfKind]||'PDF 다운로드');
  });
  const target=previewTarget,key=target?target.day+'-'+target.version:'';
  const entry=target&&sheets.find(s=>s.day===target.day&&s.version===target.version);
  const rebuilding=pdfRebuilds.has(key);
  const download=document.getElementById('pvDownload'),rebuild=document.getElementById('pvRebuild');
  download.disabled=!entry?.file_id||rebuilding;
  setActionLabel(download,'download','PDF 다운로드');
  rebuild.disabled=!target||rebuilding;
  setActionLabel(rebuild,'refresh',rebuilding?'PDF 재생성 중 · '+pdfRebuilds.get(key)+'초 경과':'PDF 재생성');
}
function downloadPdfFromPreview(){if(previewTarget)return downloadPdf(previewTarget.day,previewTarget.version,document.getElementById('pvDownload'));}
function rebuildPdfFromPreview(){if(previewTarget)return rebuildPdf(previewTarget.day,previewTarget.version);}
async function downloadPdf(day,version,button,pdfKind='combined'){
  const key=day+'-'+version,entry=sheets.find(s=>s.day===day&&s.version===version);
  if(pdfRebuilds.has(key)||entry?.deletion_pending)return;
  if(!entry?.file_id){toast('PDF 없음','미리보기에서 PDF를 먼저 재생성해 주세요.','warn');return;}
  return window.MelodyWorksheetDownload.open({items:[{...entry,kind:'standard'}],pdfKind,trigger:button});
}

async function save(){
  if(saveBusy||questionRegenerationBusy||editorLoading||savedQuestions===null||!editorDocument||screenName!=='editor')return false;
  clearError('editorError');
  const doc=editorDocument,list=document.getElementById('editList');
  if(doc.loadToken!==editorSerial||questions!==doc.questions||list.dataset.documentKey!==doc.day+'/'+doc.version||list.dataset.loadToken!==String(doc.loadToken)){
    showApiError(Error('편집 문서와 저장 대상이 일치하지 않습니다. 목록에서 다시 열어 주세요.'),'editorError');return false;
  }
  let snapshot,payload;
  try{
    doc.questions.forEach(syncSelectedCandidate);
    snapshot=JSON.stringify(C.questions(doc.questions));
    payload={questions:JSON.parse(snapshot).map(q=>({...questionCore(q),alternatives:q.alternatives,candidate_selected:q.candidate_selected,candidate_token:q.candidate_token}))};
  }catch(e){showApiError(e,'editorError');return false;}
  clearError();saveError='';saveBusy=true;startEditorElapsed();
  const epoch=currentErrorEpoch();
  document.querySelectorAll('#editList input, #editList textarea, #editList button').forEach(el=>el.disabled=true);
  const btn=document.getElementById('saveBtn');
  if(window.MelodyUI)window.MelodyUI.setBusy(btn,true,'저장 중…');else{btn.disabled=true;btn.innerHTML='<span class="spinner"></span> 저장 중…';}
  try{
    const result=C.result(await apiSend('/api/worksheet/'+doc.day+'/'+doc.version,'PUT',payload));
    if(result.day!==doc.day||result.version!==doc.version)throw Error('저장 결과의 시험지 번호가 다릅니다. 목록에서 저장 상태를 확인해 주세요.');
    if(Array.isArray(result.candidate_tokens)&&result.candidate_tokens.length===doc.questions.length)doc.questions.forEach((q,i)=>q.candidate_token=result.candidate_tokens[i]);
    savedQuestions=JSON.stringify(doc.questions);
    await loadWorksheets();
    toast('저장 완료','구글 시트를 갱신하고, PDF를 드라이브에 다시 저장했습니다.','ok');
    return true;
  }catch(e){saveError=e.code==='pdf_pending'?'시트 저장됨 · PDF 저장 실패':'저장 실패 · 입력 내용 유지';showApiError(e,'editorError',epoch);return false;}
  finally{stopEditorElapsed();saveBusy=false;updateEditStatus();document.querySelectorAll('#editList input, #editList textarea, #editList button').forEach(el=>el.disabled=false);if(window.MelodyUI)window.MelodyUI.setBusy(btn,false);else{btn.disabled=false;setActionLabel(btn,'save','저장');}}
}

/* ---------------- single-question candidates ---------------- */
const QUESTION_AI_ICON='<svg class="me-icon" data-size="16" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="/static/design-system/icons.svg#me-icon-sparkles"></use></svg>';
let questionRegenerationBusy=false,questionRegenerationTarget=null;
let questionRequests={};
try{const stored=JSON.parse(localStorage.getItem('melody-question-requests-v1')||'{}');if(stored&&typeof stored==='object'&&!Array.isArray(stored))questionRequests=stored;}catch{}
function persistQuestionRequests(){try{localStorage.setItem('melody-question-requests-v1',JSON.stringify(questionRequests));}catch{}}
function questionCore(q){return {type:q.type,instruction:q.instruction,answer:q.answer,explanation:q.explanation||'',payload:JSON.parse(JSON.stringify(q.payload))};}
function prepareCandidates(q){
  if(!q.alternatives)q.alternatives=[{...questionCore(q),candidate_id:'original'}];
  if(!Array.isArray(q.alternatives)||!q.alternatives.length||q.alternatives.length>10||!q.alternatives.every(x=>typeof x.candidate_id==='string'))throw Error('문항 후보 정보를 확인할 수 없습니다.');
  C.questions(q.alternatives,{checkContext:false});
  q.alternatives=q.alternatives.map(value=>({...questionCore(value),candidate_id:value.candidate_id}));
  q.candidate_selected??=0;
  if(!Number.isInteger(q.candidate_selected)||q.candidate_selected<0||q.candidate_selected>=q.alternatives.length)throw Error('선택된 문항 후보를 확인할 수 없습니다.');
  syncSelectedCandidate(q);
}
function syncSelectedCandidate(q){
  if(!q.alternatives)return;
  const at=q.candidate_selected||0,id=q.alternatives[at]?.candidate_id;
  q.alternatives[at]={...questionCore(q),candidate_id:id};
}
function selectQuestionCandidate(index,candidate){
  if(saveBusy||questionRegenerationBusy||editorLoading||!editorDocument||questions!==editorDocument.questions)return;
  const q=questions[index];if(!q||!Number.isInteger(candidate)||!q.alternatives?.[candidate])return;
  syncSelectedCandidate(q);Object.assign(q,questionCore(q.alternatives[candidate]),{candidate_selected:candidate});
  clearError('editorError');saveError='';renderMini();renderEdit(index);updateEditStatus();
}
document.getElementById('editList').addEventListener('click',event=>{
  const button=event.target.closest('button[data-regenerate], button[data-candidate]');if(!button)return;
  event.preventDefault();event.stopPropagation();
  if(button.dataset.regenerate!==undefined)requestQuestionRegeneration(Number(button.dataset.regenerate));
  else selectQuestionCandidate(Number(button.dataset.qi),Number(button.dataset.candidate));
});
function requestQuestionRegeneration(index){
  if(saveBusy||questionRegenerationBusy||generationBusy||editorLoading||!editorDocument||screenName!=='editor'||questions!==editorDocument.questions)return;
  const q=questions[index];if(!q)return;
  if(q.alternatives.length>=10){toast('더 이상 생성할 수 없습니다.','문항마다 현재 문항을 포함해 최대 10개 후보를 보관합니다.','warn');return;}
  if(!q.candidate_token){showApiError(Error('문항을 다시 열어 후보 정보를 불러와 주세요.'),'editorError');return;}
  const key=editorDocument.day+'/'+editorDocument.version+'/'+index;
  if(questionRequests[key]&&q.alternatives.some(v=>v.candidate_id===questionRequests[key].request_id)){delete questionRequests[key];persistQuestionRequests();}
  questionRegenerationTarget={doc:editorDocument,index,key,token:editorSerial};
  syncQuestionModelOptions();
  document.getElementById('questionGenerateError').textContent='';
  const dialog=document.getElementById('questionGenerateDialog');if(window.MelodyUI)window.MelodyUI.openDialog(dialog);else dialog.showModal();
}
function syncQuestionModelOptions(){
  const target=questionRegenerationTarget;if(!target)return;
  document.getElementById('questionGenerateInfo').textContent=(target.index+1)+'번 문항 · '+(questionRegenerationBusy?'스크립트로 생성하고 저장하는 중입니다.':questionRequests[target.key]?'이전 작업의 결과 확인 또는 저장을 이어갑니다.':'단어장의 자료로 새 후보를 추가합니다. 선택한 후보는 저장하면 시험지에 반영됩니다.');
}
function cancelQuestionRegeneration(){if(!questionRegenerationBusy){questionRegenerationTarget=null;document.getElementById('questionGenerateDialog').close();}}
document.getElementById('questionGenerateDialog').addEventListener('cancel',e=>{if(questionRegenerationBusy)e.preventDefault();else questionRegenerationTarget=null;});
async function confirmQuestionRegeneration(){
  if(questionRegenerationBusy||saveBusy||!questionRegenerationTarget)return;
  const target=questionRegenerationTarget,{doc,index,key}=target,q=doc.questions[index];
  if(doc!==editorDocument||target.token!==editorSerial||screenName!=='editor'||questions!==doc.questions){cancelQuestionRegeneration();return;}
  if(q.alternatives.length>=10){document.getElementById('questionGenerateError').textContent='더 이상 생성할 수 없습니다.';return;}
  if(!questionRequests[key]){
    try{C.questions([questionCore(q)],{checkContext:false});}
    catch(e){document.getElementById('questionGenerateError').textContent=e.message;return;}
  }
  syncSelectedCandidate(q);
  const request=questionRequests[key]||{request_id:crypto.randomUUID(),candidate_token:q.candidate_token,question:questionCore(q),provider:'script'};
  questionRequests[key]=request;persistQuestionRequests();
  questionRegenerationBusy=true;clearError();saveError='';
  syncQuestionModelOptions();
  const confirm=document.getElementById('questionGenerateConfirm'),cancel=document.getElementById('questionGenerateCancel'),error=document.getElementById('questionGenerateError');
  confirm.disabled=cancel.disabled=true;confirm.textContent='생성 중…';error.textContent='';
  document.getElementById('saveBtn').disabled=true;renderEdit(index);updateEditStatus();
  const progress=document.getElementById('questionGenerateProgress');
  progress.hidden=false;
  const stopElapsed=beginTaskElapsed(seconds=>{
    const text='생성 및 저장 처리 중 · '+seconds+'초 경과';
    progress.textContent=text;
    const status=document.getElementById('questionGenerationStatus');if(status)status.textContent=text;
  },()=>questionRegenerationBusy&&doc===editorDocument&&target.token===editorSerial);
  try{
    const result=await apiSend('/api/worksheet/'+doc.day+'/'+doc.version+'/questions/'+index+'/regenerate','POST',request);
    if(doc!==editorDocument||target.token!==editorSerial||screenName!=='editor')return;
    if(result.day!==doc.day||result.version!==doc.version||result.question_index!==index||result.request_id!==request.request_id||typeof result.candidate_token!=='string'||!Array.isArray(result.alternatives)||result.alternatives.length>10||result.alternatives.length<2||!Number.isInteger(result.generated_index)||result.alternatives[result.generated_index]?.candidate_id!==request.request_id)throw Error('생성 결과를 확인할 수 없습니다. 같은 작업을 다시 확인해 주세요.');
    C.questions(result.alternatives,{checkContext:false});
    result.alternatives=result.alternatives.map(value=>({...questionCore(value),candidate_id:value.candidate_id}));
    if(!q.alternatives.every((v,i)=>result.alternatives[i]?.candidate_id===v.candidate_id))throw Error('문항 후보 목록이 변경됐습니다. 시험지를 다시 열어 주세요.');
    // The server saved the candidates, but the active question/PDF changes only on Save.
    const baseline=JSON.parse(savedQuestions);baseline[index].alternatives=JSON.parse(JSON.stringify(result.alternatives));baseline[index].candidate_token=result.candidate_token;savedQuestions=JSON.stringify(baseline);
    const local=q.alternatives; q.alternatives=result.alternatives.map((candidate,i)=>local[i]||candidate);q.candidate_token=result.candidate_token;
    Object.assign(q,questionCore(q.alternatives[result.generated_index]),{candidate_selected:result.generated_index});
    delete questionRequests[key];persistQuestionRequests();
    document.getElementById('questionGenerateDialog').close();
    toast('새 문항 생성 완료',(index+1)+'번 문항에 후보 '+(result.generated_index+1)+'을 추가했습니다. 저장하면 시험지와 PDF에 반영됩니다.','ok');
  }catch(e){
    if(['candidate_limit','candidate_conflict','source_missing','subscription','invalid_ai_result','validation'].includes(e.code)){delete questionRequests[key];persistQuestionRequests();}
    error.textContent=C.safeMessage(e.message,'문항 생성을 완료하지 못했습니다. 같은 작업을 다시 확인해 주세요.');
  }finally{
    stopElapsed();
    progress.hidden=true;progress.textContent='';
    questionRegenerationBusy=false;confirm.disabled=cancel.disabled=false;confirm.textContent='생성';
    syncQuestionModelOptions();
    if(doc===editorDocument&&target.token===editorSerial){document.getElementById('saveBtn').disabled=false;renderMini();renderEdit(index);updateEditStatus();}
  }
}

/* init */
initDriveChip();
loadVocabList();
loadWorksheets();

async function rebuildPdf(day,version){
  const key=day+'-'+version,serial=previewSerial;
  if(pdfRebuilds.has(key))return;
  clearError();
  const stopElapsed=beginTaskElapsed(seconds=>{pdfRebuilds.set(key,seconds);syncPreviewActions();});
  const epoch=currentErrorEpoch(),errorTarget=screenName==='preview'?'previewError':'worksheetListError';
  try{
    const result=await apiSend('/api/worksheet/'+day+'/'+version+'/render','POST',{});
    const entry=sheets.find(s=>s.day===day&&s.version===version);
    if(entry&&result?.file_id)Object.assign(entry,result);
    await loadWorksheets();
    if(screenName==='preview'&&previewSerial===serial&&previewTarget?.day===day&&previewTarget?.version===version)await openPreview(day,version);
    toast('PDF 저장 완료','현재 시트 내용으로 PDF를 다시 만들었습니다.','ok');
  }catch(e){showApiError(e,errorTarget,epoch);}finally{stopElapsed();pdfRebuilds.delete(key);syncPreviewActions();}
}

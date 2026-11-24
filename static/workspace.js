/* The workspace reads the same registered banks used by worksheet generation. */
const vocabularyColumnLabels={number:'연번',word:'단어',meaning:'뜻',synonyms:'유의어',antonyms:'반의어',derived:'파생어',review:'확인 필요 사유'};
let vocabularyBanks=[],currentVocabularyBank=null,selectedVocabularyBook='',selectedVocabularyLesson='';
let vocabularyListSerial=0,vocabularyBankSerial=0,vocabularyUploadBusy=false;
let vocabularyDeleteTarget=null,vocabularyDeleteBusy=false;
let vocabularyRenameBusy=false;
const vocabularyImportStorageKey='melody-vocabulary-import-active-v1',vocabularyImportMonitors=new Map();
const vocabularyRegistrationNoticeKey='melody-vocabulary-registered-v1';
let vocabularyRegistrationNotices=new Set();
try{vocabularyRegistrationNotices=new Set(JSON.parse(sessionStorage.getItem(vocabularyRegistrationNoticeKey)||'[]'));}catch{}
window.MelodyVocabularyImports={active:id=>vocabularyImportMonitors.get(id)||null,monitor:monitorVocabularyImport,notifyRegistered:notifyVocabularyRegistered};

function vocabularyImportRegistered(job){return job?.registration_status==='complete'||job?.status==='complete';}
function vocabularyImportBankId(job){return job?.bank_id||job?.result_id;}
function vocabularyImportVisible(){return document.visibilityState!=='hidden';}
function vocabularyRegisteredMessage(job){
  if(job?.registration_status!=='complete')return '단어장 등록이 완료되었습니다.';
  if(job.status==='complete')return '단어장 등록이 완료되었습니다. 기존 어휘 시험도 사용할 수 있습니다.';
  if(['failed','interrupted'].includes(job.status))return '단어장 등록이 완료되었습니다. 학습자료 준비 상태를 확인해 주세요.';
  return '단어장 등록이 완료되었습니다. 기존 어휘 시험용 학습자료를 준비 중입니다.';
}
function notifyVocabularyRegistered(job){
  if(!vocabularyImportRegistered(job)||!job.id||vocabularyRegistrationNotices.has(job.id))return;
  vocabularyRegistrationNotices.add(job.id);
  try{sessionStorage.setItem(vocabularyRegistrationNoticeKey,JSON.stringify([...vocabularyRegistrationNotices].slice(-200)));}catch{}
  toast('단어장 등록이 완료되었습니다.','','ok');
}
async function refreshVocabularyImportBanks(){
  window.MelodyBankData?.invalidate();
  await Promise.allSettled([loadVocabularyBanks(undefined,true),loadVocabList(true),notifyWordMeaningFrames('refreshBanks',true)]);
}
async function restoreVocabularyImports(){
  if(!vocabularyImportVisible())return;
  try{
    const jobs=await apiGet('/api/word-meaning/jobs');
    for(const job of jobs.filter(item=>item.kind==='import'&&item.engine_version===2&&['queued','running'].includes(item.status))){
      const monitor=monitorVocabularyImport(job.id,Date.parse(job.started_at||job.created_at)||Date.now());
      monitor.registration.then(notifyVocabularyRegistered).catch(()=>{});
    }
  }catch{}
}

function vocabularyImportTransient(error){return !error.status||[408,409,425,429,500,502,503,504].includes(error.status);}
function vocabularyImportWait(milliseconds){return new Promise(resolve=>setTimeout(resolve,milliseconds));}
function vocabularyImportDelay(job){
  const next=Date.parse(job.recovery?.next_attempt_at||'');
  return Number.isFinite(next)?Math.max(0,next-Date.now()):0;
}
function vocabularyImportMessage(job){
  const parts=[job.message||'단어장 등록 상태를 확인하고 있습니다.'],progress=job.progress||{},recovery=job.recovery||{};
  if(progress.completed!=null&&progress.total!=null)parts.push(progress.completed+'/'+progress.total+'개');
  if(recovery.active&&recovery.attempt)parts.push('자동 재시도 '+recovery.attempt+'회');
  const wait=vocabularyImportDelay(job);if(wait>0)parts.push(Math.ceil(wait/1000)+'초 후 계속합니다');
  return parts.join(' · ');
}
function vocabularyImportWorking(job){
  const stage=job.progress?.stage;
  const message=job.recovery?.active?'원문을 다시 확인하며 자동 재시도하고 있습니다.':stage==='saving'?'원문 단어와 뜻을 단어은행에 저장하고 있습니다.':stage==='learning'?'AI가 원문 뜻에 맞는 학습자료를 정리하고 있습니다.':stage==='source_read'?'원문 파일의 단어와 뜻을 읽고 있습니다.':job.message||'원문 단어와 뜻을 확인하고 있습니다.';
  return {...job,status:'running',message};
}
function rememberVocabularyImport(value){try{if(value)sessionStorage.setItem(vocabularyImportStorageKey,JSON.stringify(value));else sessionStorage.removeItem(vocabularyImportStorageKey);}catch{}}
function savedVocabularyImport(){
  try{const value=JSON.parse(sessionStorage.getItem(vocabularyImportStorageKey)||'null');return value&&/^j_[a-f0-9]{32}$/.test(value.id)&&Number.isFinite(value.startedAt)&&value.startedAt>0?value:null;}catch{return null;}
}
function monitorVocabularyImport(id,startedAt){
  if(vocabularyImportMonitors.has(id))return vocabularyImportMonitors.get(id);
  let registered,registrationFailed;
  const monitor={id,startedAt,job:{id,kind:'import',status:'queued',message:'저장된 단어장 등록 상태를 확인하고 있습니다.'},promise:null,registration:new Promise((resolve,reject)=>{registered=resolve;registrationFailed=reject;}),registrationObserved:false};
  monitor.registration.catch(()=>{});
  vocabularyImportMonitors.set(id,monitor);
  monitor.promise=(async()=>{
    let job=null,transportFailures=0;
    for(;;){
      try{
        if(!vocabularyImportVisible()){await vocabularyImportWait(3000);continue;}
        if(!job)job=await apiGet('/api/word-meaning/jobs/'+encodeURIComponent(id));
        monitor.job=job;
        if(vocabularyImportRegistered(job)&&!monitor.registrationObserved){
          monitor.registrationObserved=true;registered(job);
          refreshVocabularyImportBanks().catch(()=>{});
        }
        if(job.status==='complete')return job;
        if(['failed','interrupted'].includes(job.status))throw Object.assign(Error(job.message||'원문을 확인하지 못해 등록을 완료하지 못했습니다.'),{job,permanent:true});
        const delay=vocabularyImportDelay(job);
        if(delay>0){await vocabularyImportWait(Math.min(delay,5000));job=null;continue;}
        if(job.engine_version===2){await vocabularyImportWait(3000);job=null;transportFailures=0;continue;}
        // Every retry after an uncertain POST first re-reads this durable job.
        monitor.job=vocabularyImportWorking(job);
        job=await apiSend('/api/word-meaning/jobs/'+encodeURIComponent(id)+'/step','POST');
        transportFailures=0;
        if(job.status==='running'){await vocabularyImportWait(1200);job=null;}
      }catch(error){
        if(error.permanent||!vocabularyImportTransient(error))throw error;
        transportFailures++;job=null;
        monitor.job={...monitor.job,status:'running',message:error.status===409?'진행 중인 작업의 결과를 확인하고 있습니다.':'연결을 다시 확인하고 있습니다. 저장된 작업을 이어갑니다.',recovery:{active:false}};
        await vocabularyImportWait(Math.min(15000,1000*2**Math.min(transportFailures-1,4)));
      }
    }
  })().catch(error=>{if(!monitor.registrationObserved)registrationFailed(error);throw error;}).finally(()=>{
    if(vocabularyImportMonitors.get(id)===monitor)vocabularyImportMonitors.delete(id);
    if(monitor.registrationObserved){
      refreshVocabularyImportBanks().catch(()=>{});
      const status=document.getElementById('vocabularyUploadStatus');
      if(status?.dataset.importJobId===id&&!vocabularyUploadBusy&&!document.getElementById('xlsx').files?.length)status.textContent=vocabularyRegisteredMessage(monitor.job);
    }
  });
  // A registered bank is usable even if later learning preparation fails.
  monitor.promise.catch(()=>{});
  return monitor;
}
async function acceptVocabularyImport(file,report){
  let failures=0;
  for(;;){
    try{
      const form=new FormData();form.append('file',file);
      const response=await fetch('/api/word-meaning/imports',{method:'POST',body:form,signal:AbortSignal.timeout(900000)});
      if(!response.ok)throw await apiError(response);
      return await response.json();
    }catch(error){
      if(!vocabularyImportTransient(error))throw error;
      failures++;report('파일 접수 상태를 다시 확인하고 있습니다. 같은 원본으로 등록을 이어갑니다.');
      // Uploads are content-hash deduplicated by the server, including an ACK loss.
      await vocabularyImportWait(Math.min(15000,1000*2**Math.min(failures-1,4)));
    }
  }
}

function vocabularyError(message=''){
  const error=document.getElementById('vocabularyBankError');
  error.textContent=message;error.hidden=!message;
}
async function loadVocabularyBanks(preferredId,force=false){
  if(vocabularyDeleteBusy||vocabularyRenameBusy)return;
  const serial=++vocabularyListSerial;
  try{
    const banks=await bankRead('/banks',()=>apiGet('/api/word-meaning/banks'),force);
    if(serial!==vocabularyListSerial)return;
    if(!Array.isArray(banks))throw Error('단어장 목록을 확인하지 못했습니다.');
    vocabularyBanks=banks;
    const previous=preferredId??selectedVocabularyBook;
    renderVocabularyBankOptions();
    vocabularyError();
    await selectVocabularyBook(banks.some(bank=>bank.id===previous)?previous:'',force);
  }catch(error){if(serial===vocabularyListSerial)vocabularyError(error.message||'단어장을 불러오지 못했습니다. 새로고침해 주세요.');}
}
function renderVocabularyBankOptions(){
  const select=document.getElementById('vocabularyBook');select.replaceChildren();
  const blank=document.createElement('option');blank.value='';blank.textContent=vocabularyBanks.length?'단어장을 선택하세요':'등록된 단어장이 없습니다';select.appendChild(blank);
  for(const bank of vocabularyBanks){const option=document.createElement('option');option.value=bank.id;option.textContent=bank.title||bank.source?.filename||'단어장';select.appendChild(option);}
}
function syncVocabularyDeletionControls(){
  const busy=vocabularyDeleteBusy||vocabularyRenameBusy;
  const available=!!currentVocabularyBank&&Number.isInteger(currentVocabularyBank.version);
  const button=document.getElementById('vocabularyDeleteButton');
  button.disabled=busy||vocabularyUploadBusy||!available;
  button.setAttribute('aria-label',currentVocabularyBank?currentVocabularyBank.title+' 삭제':'선택한 단어장 삭제');
  const rename=document.getElementById('vocabularyRenameButton');rename.disabled=busy||vocabularyUploadBusy||!available;
  rename.setAttribute('aria-label',currentVocabularyBank?currentVocabularyBank.title+' 수정':'선택한 단어장 수정');
  document.getElementById('vocabularyBook').disabled=busy;
  document.getElementById('vocabularyRefreshButton').disabled=busy;
}
function vocabularyDeletionGuard(bankId){
  for(const frame of getWordMeaningFrames()){const status=frame.contentWindow?.melodyWordMeaning?.canDeleteBank?.(bankId);if(status?.allowed===false)return status.message||'단어장 작업이 끝난 뒤 삭제해 주세요.';}
  return '';
}
function openVocabularyBankDeletion(){
  if(vocabularyDeleteBusy||vocabularyRenameBusy||vocabularyUploadBusy||!currentVocabularyBank||!Number.isInteger(currentVocabularyBank.version))return;
  const reason=vocabularyDeletionGuard(selectedVocabularyBook);if(reason){vocabularyError(reason);return;}
  vocabularyDeleteTarget={id:selectedVocabularyBook,title:currentVocabularyBank.title,version:currentVocabularyBank.version};
  document.getElementById('vocabularyDeleteName').textContent=vocabularyDeleteTarget.title;
  document.getElementById('vocabularyDeleteError').textContent='';
  const dialog=document.getElementById('vocabularyDeleteDialog');
  if(window.MelodyUI)window.MelodyUI.openDialog(dialog);else dialog.showModal();
}
async function openVocabularyBankEditor(){
  if(vocabularyDeleteBusy||vocabularyRenameBusy||vocabularyUploadBusy||!currentVocabularyBank||!Number.isInteger(currentVocabularyBank.version))return;
  if(!window.MelodyVocabularyEditor){vocabularyError('수정 화면을 불러오지 못했습니다. 새로고침해 주세요.');return;}
  vocabularyRenameBusy=true;vocabularyListSerial++;vocabularyBankSerial++;syncVocabularyDeletionControls();
  try{await window.MelodyVocabularyEditor.open({bankId:selectedVocabularyBook,trigger:document.getElementById('vocabularyRenameButton')});}
  finally{vocabularyRenameBusy=false;syncVocabularyDeletionControls();document.getElementById('vocabularyRenameButton').focus({preventScroll:true});}
}
function applyVocabularyBankChange(bank){
  if(!bank?.id||!Number.isInteger(bank.version)||!Array.isArray(bank.entries)||!Array.isArray(bank.units))return;
  const previousBank=vocabularyBanks.find(item=>item.id===bank.id);
  if(previousBank?.version>bank.version)return;
  vocabularyListSerial++;vocabularyBankSerial++;
  vocabularyBanks=vocabularyBanks.map(item=>item.id===bank.id?{...item,title:bank.title,version:bank.version,units:bank.units,counts:bank.counts}:item);
  renderVocabularyBankOptions();document.getElementById('vocabularyBook').value=selectedVocabularyBook;
  if(selectedVocabularyBook===bank.id){
    const previous=selectedVocabularyLesson,book=vocabularyView(bank);currentVocabularyBank=book;
    const lessons=document.getElementById('vocabularyLessons');lessons.replaceChildren();
    for(const lesson of book.lessons){
      const button=document.createElement('button');button.type='button';button.className='lesson-button me-button';button.dataset.variant='secondary';button.dataset.size='md';button.dataset.lesson=lesson.id;button.textContent=lesson.label;button.setAttribute('aria-controls','vocabularyTable');button.addEventListener('click',()=>selectVocabularyLesson(lesson.id));lessons.appendChild(button);
    }
    document.getElementById('vocabularyLessonCount').textContent=book.lessons.length+'개 챕터';
    document.getElementById('vocabularyListSection').hidden=!book.lessons.length;
    document.getElementById('vocabularyLessonPlaceholder').hidden=!!book.lessons.length;
    selectVocabularyLesson(book.lessons.some(lesson=>lesson.id===previous)?previous:book.lessons[0]?.id);
  }
  syncVocabularyDeletionControls();
}
window.applyVocabularyBankChange=applyVocabularyBankChange;
window.addEventListener('melody-bank-updated',event=>applyVocabularyBankChange(event.detail));
function cancelVocabularyBankDeletion(){
  if(vocabularyDeleteBusy)return;
  vocabularyDeleteTarget=null;document.getElementById('vocabularyDeleteDialog').close();
}
document.getElementById('vocabularyDeleteDialog').addEventListener('cancel',event=>{if(vocabularyDeleteBusy)event.preventDefault();else vocabularyDeleteTarget=null;});
async function confirmVocabularyBankDeletion(mode='soft'){
  if(!['soft','permanent'].includes(mode))return;
  if(vocabularyDeleteBusy||!vocabularyDeleteTarget)return;
  const target={...vocabularyDeleteTarget},error=document.getElementById('vocabularyDeleteError');
  const reason=vocabularyDeletionGuard(target.id);if(reason){error.textContent=reason;return;}
  vocabularyDeleteBusy=true;vocabularyListSerial++;vocabularyBankSerial++;error.textContent='';syncVocabularyDeletionControls();
  const dialog=document.getElementById('vocabularyDeleteDialog'),confirm=document.getElementById('vocabularyDeleteConfirm'),permanent=document.getElementById('vocabularyDeletePermanent'),cancel=document.getElementById('vocabularyDeleteCancel');
  confirm.disabled=permanent.disabled=cancel.disabled=true;const active=mode==='permanent'?permanent:confirm;active.textContent='삭제 중…';dialog.setAttribute('aria-busy','true');
  const began=Date.now(),progress=document.getElementById('vocabularyDeleteProgress');progress.hidden=false;const tick=()=>{progress.textContent=(mode==='permanent'?'단어장과 저장 파일을 완전히 삭제하고 있습니다':'단어장을 목록에서 삭제하고 있습니다')+' · '+Math.floor((Date.now()-began)/1000)+'초 경과';};tick();const timer=setInterval(tick,1000);
  try{
    const result=await apiSend('/api/word-meaning/banks/'+encodeURIComponent(target.id),'DELETE',{version:target.version,mode});
    if(result?.id!==target.id||result?.deleted!==true)throw Error('삭제 결과를 확인하지 못했습니다. 다시 시도하거나 목록을 새로고침해 주세요.');
    window.MelodyBankData?.invalidate('/banks/'+target.id);
    vocabularyBanks=vocabularyBanks.filter(bank=>bank.id!==target.id);renderVocabularyBankOptions();
    await selectVocabularyBook(selectedVocabularyBook===target.id?'':selectedVocabularyBook);
    vocabularyDeleteTarget=null;dialog.close();loadVocabList();toast('단어장 삭제',mode==='permanent'?'선택한 단어장을 완전히 삭제했습니다.':'선택한 단어장을 목록에서 삭제했습니다.','ok');
    try{await notifyWordMeaningFrames('bankDeleted',target.id);}
    catch{vocabularyError('단어장은 삭제했습니다. 출제 화면의 목록을 새로고침해 주세요.');}
  }catch(reason){error.textContent=reason.message||'단어장을 삭제하지 못했습니다. 다시 시도해 주세요.';}
  finally{clearInterval(timer);progress.hidden=true;vocabularyDeleteBusy=false;confirm.disabled=permanent.disabled=cancel.disabled=false;confirm.textContent='목록에서 삭제';permanent.textContent='완전히 삭제';dialog.removeAttribute('aria-busy');syncVocabularyDeletionControls();}
}

function previewVocabularyUpload(input){
  if(vocabularyUploadBusy)return;
  const file=input.files?.[0],valid=!!file&&/\.(pdf|xlsx)$/i.test(file.name);
  const status=document.getElementById('vocabularyUploadStatus');
  status.textContent=valid?'선택한 파일: '+file.name
    :'PDF 또는 Excel(.xlsx) 파일을 선택해 주세요.';
  document.getElementById('vocabularyUploadButton').disabled=!valid;
}
function setVocabularyUploadBusy(busy){
  vocabularyUploadBusy=busy;document.getElementById('xlsx').disabled=busy;
  const file=document.getElementById('xlsx').files?.[0];
  document.getElementById('vocabularyUploadButton').disabled=busy||!file||!/\.(pdf|xlsx)$/i.test(file.name);
  syncVocabularyDeletionControls();
}
async function uploadVocabularyBank(resume=null){
  if(vocabularyUploadBusy||vocabularyDeleteBusy||vocabularyRenameBusy)return;
  const input=document.getElementById('xlsx'),file=input.files?.[0];if(!file&&!resume)return;
  const status=document.getElementById('vocabularyUploadStatus');setVocabularyUploadBusy(true);clearError('vocabularyFilesError');
  const began=resume?.startedAt||Date.now();let progress='원문 단어와 뜻을 등록할 준비를 하고 있습니다.',monitor=null,completedJob=null,knownJob=resume?.id||null;
  status.setAttribute('role','timer');status.setAttribute('aria-live','off');
  const tick=()=>{status.textContent=(monitor?vocabularyImportMessage(monitor.job):progress)+' · '+Math.max(0,Math.floor((Date.now()-began)/1000))+'초 경과';};tick();const timer=setInterval(tick,1000);
  try{
    progress=resume?'저장된 단어장 등록 작업을 이어가고 있습니다.':'단어장 파일을 등록하고 있습니다…';tick();
    const result=resume?{job_id:resume.id}:await acceptVocabularyImport(file,text=>{progress=text;tick();});let bankId=result.bank_id;
    if(result.job_id){
      knownJob=result.job_id;
      status.dataset.importJobId=result.job_id;
      rememberVocabularyImport({id:result.job_id,startedAt:began});
      monitor=monitorVocabularyImport(result.job_id,began);tick();
      const job=await monitor.registration;bankId=vocabularyImportBankId(job);completedJob=job;monitor=null;
    }
    if(!bankId)throw Error('단어장 등록 결과를 확인하지 못했습니다.');
    rememberVocabularyImport(null);progress='단어장 등록을 완료했습니다. 목록을 연결하고 있습니다.';tick();
    let listPending=false;
    try{window.MelodyBankData?.invalidate();await loadVocabularyBanks(bankId);await loadVocabList();await notifyWordMeaningFrames('refreshBanks');}
    catch{listPending=true;}
    clearInterval(timer);input.value='';status.textContent=listPending?'단어장 등록이 완료되었습니다. 목록을 새로고침해 주세요.':vocabularyRegisteredMessage(completedJob);
    if(completedJob)notifyVocabularyRegistered(completedJob);else toast('단어장 등록이 완료되었습니다.','','ok');
  }catch(error){if(error.permanent||error.job?.requires_attention)rememberVocabularyImport(null);clearInterval(timer);status.textContent=error.job?.requires_attention?'등록을 계속하려면 원본 또는 계정 확인이 필요합니다.':knownJob?'등록 상태를 확인하지 못했습니다. 저장된 작업 정보는 유지됩니다.':'단어장 파일 접수를 완료하지 못했습니다.';showApiError(error,'vocabularyFilesError');}
  finally{clearInterval(timer);status.setAttribute('role','status');status.setAttribute('aria-live','polite');setVocabularyUploadBusy(false);}
}
function vocabularyView(bank){
  const entries=bank.entries||[],columns=['number','word','meaning'];
  for(const [column,label]of [['synonyms','유의어'],['antonyms','반의어'],['derived','파생어']])if(entries.some(entry=>entry.related?.[label]))columns.push(column);
  if(entries.some(entry=>entry.status==='review'||entry.issues?.length))columns.push('review');
  return{id:bank.id,version:bank.version,title:bank.title||bank.source?.filename||'단어장',columns,lessons:(bank.units||[]).map(unit=>({id:unit.id,label:unit.label,words:entries.filter(entry=>entry.unit_id===unit.id).map(entry=>({number:entry.source_number,word:entry.word,meaning:entry.meaning??entry.meaning_raw,synonyms:entry.related?.['유의어'],antonyms:entry.related?.['반의어'],derived:entry.related?.['파생어'],review:window.ExamContracts.reviewText(entry.issues)||(entry.status==='review'?'원본 확인 필요':'')}))}))};
}
async function selectVocabularyBook(bookId,force=false){
  const serial=++vocabularyBankSerial,previousLesson=selectedVocabularyBook===bookId?selectedVocabularyLesson:'';
  selectedVocabularyBook=vocabularyBanks.some(bank=>bank.id===bookId)?bookId:'';
  currentVocabularyBank=null;selectedVocabularyLesson='';
  document.getElementById('vocabularyBook').value=selectedVocabularyBook;
  document.getElementById('vocabularyListSection').hidden=true;
  document.getElementById('vocabularyLessonPlaceholder').hidden=false;
  const lessons=document.getElementById('vocabularyLessons');lessons.replaceChildren();
  for(const id of ['vocabularyLessonCount','vocabularyScope','vocabularyCaption','vocabularyWordCount'])document.getElementById(id).textContent='';
  document.getElementById('vocabularyTableHead').replaceChildren();document.getElementById('vocabularyTableBody').replaceChildren();
  const placeholder=document.getElementById('vocabularyLessonPlaceholder');placeholder.textContent=selectedVocabularyBook?'챕터를 불러오는 중…':'단어장을 선택하면 챕터 목록이 나타납니다.';
  syncVocabularyDeletionControls();vocabularyError();if(!selectedVocabularyBook)return;
  const began=Date.now();let timer=null;
  const tick=()=>{if(serial!==vocabularyBankSerial){clearInterval(timer);return;}placeholder.textContent='챕터를 불러오는 중 · '+Math.max(0,Math.floor((Date.now()-began)/1000))+'초 경과';};
  tick();timer=setInterval(tick,1000);
  try{
  const raw=await bankRead('/banks/'+selectedVocabularyBook,()=>apiGet('/api/word-meaning/banks/'+encodeURIComponent(selectedVocabularyBook)+'?view=management'),force);
  if(serial!==vocabularyBankSerial)return;
  const book=vocabularyView(raw);currentVocabularyBank=book;
  syncVocabularyDeletionControls();
  document.getElementById('vocabularyListSection').hidden=!book.lessons.length;placeholder.hidden=!!book.lessons.length;
  if(!book.lessons.length)placeholder.textContent='등록된 챕터가 없습니다. 출제 화면에서 원본을 확인해 주세요.';
  document.getElementById('vocabularyLessonCount').textContent=book.lessons.length+'개 챕터';
  for(const lesson of book.lessons){
    const button=document.createElement('button');button.type='button';button.className='lesson-button me-button';button.dataset.variant='secondary';button.dataset.size='md';
    button.dataset.lesson=lesson.id;button.textContent=lesson.label;
    button.setAttribute('aria-controls','vocabularyTable');
    button.addEventListener('click',()=>selectVocabularyLesson(lesson.id));lessons.appendChild(button);
  }
  selectVocabularyLesson(book.lessons.some(lesson=>lesson.id===previousLesson)?previousLesson:book.lessons[0]?.id);
  }catch(error){if(serial===vocabularyBankSerial){placeholder.textContent='단어장을 다시 선택하거나 새로고침해 주세요.';vocabularyError(error.message);}}
  finally{clearInterval(timer);}
}

function selectVocabularyLesson(lessonId){
  const book=currentVocabularyBank;
  if(!book)return;
  const lesson=book.lessons.find(item=>item.id===lessonId);if(!lesson)return;
  const columns=book.columns.filter(column=>column!=='review'||lesson.words.some(word=>String(word.review??'').trim()));
  selectedVocabularyLesson=lessonId;
  document.querySelectorAll('[data-lesson]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.lesson===lessonId)));
  const scope=book.title+' / '+lesson.label;
  document.getElementById('vocabularyScope').textContent=scope;
  document.getElementById('vocabularyCaption').textContent=scope;
  document.getElementById('vocabularyWordCount').textContent='어휘 '+lesson.words.length+'개';
  const head=document.createElement('tr');
  for(const column of columns){const cell=document.createElement('th');cell.scope='col';cell.dataset.column=column;cell.textContent=vocabularyColumnLabels[column];head.appendChild(cell);}
  document.getElementById('vocabularyTableHead').replaceChildren(head);
  const body=document.getElementById('vocabularyTableBody');body.replaceChildren();
  for(const word of lesson.words){
    const row=document.createElement('tr');
    for(const column of columns){const cell=document.createElement('td'),text=document.createElement('span');cell.dataset.column=column;text.className='vocabulary-cell-text';text.textContent=word[column]??'—';cell.appendChild(text);if(column==='word')cell.className='word-cell';row.appendChild(cell);}
    body.appendChild(row);
  }
}
async function openSelectedVocabulary(){
  if(selectedVocabularyBook)await openWordMeaning({bank_id:selectedVocabularyBook});
}

let managedWorksheets=[],managementLoadSerial=0,managedWorksheetOpenSerial=0;
let managementLoadState='idle',managementLoadStarted=0,managementLoadTimer=null;
let managementSortKey='created',managementSortDirection='desc';
const managementSelected=new Set();
const managedKey=item=>isFeatureWorksheetKind(item.kind)?item.kind+':'+item.id:'standard:'+item.day+':'+item.version;
async function loadManagedWorksheets(){
  const serial=++managementLoadSerial,error=document.getElementById('managementFeedback');
  clearInterval(managementLoadTimer);managementLoadState='loading';managementLoadStarted=Date.now();
  if(error){error.hidden=true;error.textContent='';}
  renderManagementExamples();managementLoadTimer=setInterval(syncManagementLoading,1000);
  try{
    const [standard,meaning]=await Promise.all([apiGet('/api/worksheets'),apiGet('/api/word-meaning/worksheets')]);
    if(serial!==managementLoadSerial)return;
    managedWorksheets=[...standard.map(item=>({...item,kind:'standard',kindLabel:'기존 어휘 시험',range:item.range_label||item.source_unit_labels?.join(' · ')||'DAY '+item.day+(item.day_end>item.day?'~'+item.day_end:'')})),...meaning.map(item=>({...item,kind:item.worksheet_kind==='meaning_writing_test'?'meaning-writing':'word-meaning',kindLabel:item.worksheet_kind==='meaning_writing_test'?'뜻 쓰기 시험':'단어–뜻 시험',range:item.range_label,created:item.created_at,updated:item.updated_at}))];
    for(const key of managementSelected)if(!managedWorksheets.some(item=>managedKey(item)===key))managementSelected.delete(key);
    if(error){error.hidden=true;error.textContent='';}
    managementLoadState='ready';
  }catch(reason){if(serial!==managementLoadSerial)return;managementLoadState='error';if(error){error.textContent=reason.message||'시험지 목록을 불러오지 못했습니다.';error.hidden=false;}}
  finally{if(serial===managementLoadSerial){clearInterval(managementLoadTimer);managementLoadTimer=null;renderManagementExamples();}}
}
function syncManagementLoading(){
  const status=document.getElementById('managementLoading'),loading=managementLoadState==='loading';
  status.hidden=!loading;
  document.getElementById('managementLoadingText').textContent=loading?'시험지 목록을 불러오는 중입니다 · '+Math.floor((Date.now()-managementLoadStarted)/1000)+'초 경과':'';
  document.getElementById('managementExamples').setAttribute('aria-busy',String(loading));
}
async function openManagedWorksheet(item,edit){
  if(item.kind==='standard')return edit?openEditor(item.day,item.version):openPreview(item.day,item.version);
  const request=++managedWorksheetOpenSerial;
  if(await go('dashboard')===false)return false;
  const navigation=workspaceNavigationRevision;
  const current=()=>request===managedWorksheetOpenSerial&&navigation===workspaceNavigationRevision;
  if(await openWordMeaning(undefined,item.kind)===false)return;
  if(!current())return false;
  const child=getWordMeaningFrame(item.kind).contentWindow?.melodyWordMeaning;
  await child.ready;
  if(!current())return false;
  const result=await (edit?child.openWorksheet(item.id):child.openPreview(item.id));
  if(!current()||result===false)return false;
  wordMeaningHome='management';
  setWordMeaningDocumentView(edit?'editor':'preview');
  return result;
}
function visibleManagedWorksheets(){
  const query=document.getElementById('managementSearch').value.trim().toLocaleLowerCase('ko-KR');
  const kind=document.getElementById('managementKind').value;
  return window.MelodyWorksheetList.sort(managedWorksheets.filter(item=>(kind==='all'||item.kind===kind)&&item.title.toLocaleLowerCase('ko-KR').includes(query)),managementSortKey,managementSortDirection);
}
function renderManagementExamples(){
  const items=visibleManagedWorksheets();
  const body=document.getElementById('managementExamples');body.replaceChildren();
  for(const [index,item]of items.entries()){
    const row=document.createElement('tr'),selectCell=document.createElement('td'),number=document.createElement('td'),name=document.createElement('td');
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=managementSelected.has(managedKey(item));checkbox.setAttribute('aria-label',item.title+' 선택');checkbox.addEventListener('change',()=>{checkbox.checked?managementSelected.add(managedKey(item)):managementSelected.delete(managedKey(item));syncManagementSelection();});selectCell.appendChild(checkbox);number.textContent=index+1;row.append(selectCell,number);
    const title=document.createElement('strong');title.textContent=item.title;
    const subtitle=document.createElement('span');subtitle.className='table-subtitle';subtitle.textContent=item.kindLabel;name.append(title,subtitle);row.appendChild(name);
    for(const value of [item.range,item.count+'문항']){const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}
    const state=document.createElement('td');state.textContent=item.deletion_pending?'삭제 미완료':worksheetDateLabel(item);row.appendChild(state);
    const actions=document.createElement('td'),group=document.createElement('div');group.className='table-actions';
    for(const label of ['미리보기','수정']){const button=document.createElement('button');button.type='button';button.className='btn sm outline me-button';button.dataset.variant=label==='수정'?'edit':'secondary';button.dataset.size='sm';button.textContent=label;if(window.MelodyIcons)button.insertAdjacentHTML('afterbegin',window.MelodyIcons.svg(label==='수정'?'edit':'preview',{size:16}));button.disabled=!!item.deletion_pending;button.addEventListener('click',async()=>{button.disabled=true;try{await openManagedWorksheet(item,label==='수정');}catch(error){const target=document.getElementById('managementFeedback');target.textContent=error.message;target.hidden=false;}finally{button.disabled=!!item.deletion_pending;}});group.appendChild(button);}
    const drive=document.createElement('button');drive.type='button';drive.className='me-icon-button';drive.dataset.variant='secondary';drive.dataset.size='sm';drive.setAttribute('aria-label',item.title+' · 구글 드라이브에서 열기');drive.title='구글 드라이브에서 열기';drive.innerHTML=window.MelodyIcons.svg('google-drive',{size:20});drive.disabled=!!item.deletion_pending;drive.addEventListener('click',async()=>{drive.disabled=true;try{await window.MelodyWorksheetDownload.openDrive(item);}catch(error){const target=document.getElementById('managementFeedback');target.textContent=error.message;target.hidden=false;}finally{drive.disabled=!!item.deletion_pending;}});group.appendChild(drive);
    const remove=document.createElement('button');remove.type='button';remove.className='me-icon-button';remove.dataset.variant='danger';remove.dataset.size='sm';remove.setAttribute('aria-label',item.title+' 삭제');remove.title='시험지 삭제';remove.innerHTML=TRASH_SVG;remove.addEventListener('click',()=>openWorksheetDeletion(item));group.appendChild(remove);
    actions.appendChild(group);row.appendChild(actions);body.appendChild(row);
  }
  if(!items.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=7;cell.className='table-empty';cell.textContent=managementLoadState==='loading'?'시험지 목록을 불러오는 중입니다.':managementLoadState==='error'?'시험지 목록을 불러오지 못했습니다. 새로고침해 주세요.':managementLoadState==='idle'?'시험지 목록을 불러올 준비 중입니다.':'표시할 시험지가 없습니다.';row.appendChild(cell);body.appendChild(row);}
  document.getElementById('managementCount').textContent=managementLoadState==='loading'?'시험지 목록 확인 중':managementLoadState==='error'&&!items.length?'목록 확인 실패':'시험지 '+items.length+'개';syncManagementSelection();syncManagementLoading();
}

function setManagementSort(key){if(!['latest','created','name'].includes(key))return;managementSortKey=key;renderManagementExamples();}
function toggleManagementSortDirection(){managementSortDirection=managementSortDirection==='desc'?'asc':'desc';window.MelodyWorksheetList.direction(document.getElementById('managementSortDirection'),managementSortDirection);renderManagementExamples();}
function syncManagementSelection(){
  const visible=visibleManagedWorksheets(),selected=visible.filter(item=>managementSelected.has(managedKey(item))).length,all=document.getElementById('managementSelectAll');
  all.checked=!!visible.length&&selected===visible.length;all.indeterminate=selected>0&&selected<visible.length;all.disabled=!visible.length;
  const button=document.getElementById('managementDeleteSelected');button.disabled=!managementSelected.size;button.textContent='삭제';
  document.getElementById('managementDownloadSelected').disabled=!managementSelected.size;
}
function downloadSelectedManaged(){
  const items=managedWorksheets.filter(item=>managementSelected.has(managedKey(item)));
  if(items.length)return window.MelodyWorksheetDownload.open({items,trigger:document.getElementById('managementDownloadSelected')});
}
function selectAllManaged(checked){for(const item of visibleManagedWorksheets())checked?managementSelected.add(managedKey(item)):managementSelected.delete(managedKey(item));renderManagementExamples();}
function removeManagedWorksheet(item){const key=managedKey(item);managedWorksheets=managedWorksheets.filter(value=>managedKey(value)!==key);managementSelected.delete(key);renderManagementExamples();}
async function deleteSelectedManaged(){
  const pending=managedWorksheets.filter(item=>managementSelected.has(managedKey(item)));if(!pending.length)return;
  const selectedKeys=new Set(pending.map(managedKey)),total=pending.length;let completed=0;
  return window.MelodyWorksheetDelete.open({title:'선택한 시험지 '+total+'개',onDelete:async(mode,report)=>{
    const failed=[];
    for(const item of managedWorksheets.filter(item=>selectedKeys.has(managedKey(item))&&managementSelected.has(managedKey(item)))){
      report(total+'개 중 '+completed+'개 삭제 완료 · '+item.title+' 처리 중');
      try{await deleteWorksheetRecord(item,mode);completed++;}catch(error){failed.push(error.message||'삭제 실패');}
    }
    if(failed.length)throw Error(completed+'개 삭제 완료 · '+failed.length+'개 삭제하지 못했습니다. 완료된 항목은 제외하고 다시 시도할 수 있습니다. '+failed[0]);
  }});
}

loadVocabularyBanks();
const pendingVocabularyImport=savedVocabularyImport();if(pendingVocabularyImport)uploadVocabularyBank(pendingVocabularyImport);
restoreVocabularyImports();
document.addEventListener('visibilitychange',()=>{if(vocabularyImportVisible())restoreVocabularyImports();});
renderManagementExamples();
syncWorkspaceNavigation();

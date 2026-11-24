/* Parent-document editor for the registered vocabulary bank. */
(() => {
  'use strict';
  const API='/api/word-meaning/banks/',PAGE_SIZE=50;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  const entryValues=entry=>({word:String(entry.word??''),meaning:String(entry.meaning??entry.meaning_raw??'')});
  const statusLabel=status=>({review:'확인 필요',ready:'등록 완료',excluded:'출제 제외'}[status]||'확인 필요');
  const needsReview=entry=>entry.status==='review'||Boolean(entry.learning_review);
  let state=null;
  function validBank(bank,id){return bank?.id===id&&Number.isInteger(bank.version)&&bank.version>0&&Array.isArray(bank.units)&&Array.isArray(bank.entries);}
  function node(id){return state?.dialog.querySelector('#'+id);}
  function entryStatus(entry){return state.entryStatuses.get(entry.id)??entry.status;}
  function dirtyCount(){return state?(state.titleDraft!==null?1:0)+new Set([...state.units.keys(),...state.unitConfirm]).size+new Set([...state.entries.keys(),...state.entryStatuses.keys()]).size:0;}
  function tell(id,text='',tone='neutral'){
    const el=node(id);if(!el)return;el.textContent=text;el.hidden=!text;el.dataset.tone=tone;
    el.setAttribute('role',tone==='danger'?'alert':'status');
  }
  function issueText(value){return window.ExamContracts.reviewText(value);}
  function field(label,id,value,max,tag='input'){
    return '<label class="me-field me-label" for="'+id+'">'+escape(label)+(tag==='textarea'?'<textarea':'<input type="text"')+' id="'+id+'" class="me-input" maxlength="'+max+'" '+(tag==='textarea'?'>':'value="'+escape(value)+'">')+(tag==='textarea'?escape(value)+'</textarea>':'')+'</label>';
  }
  function button(label,action,variant='secondary',extra=''){
    return '<button type="button" class="me-button" data-variant="'+variant+'" data-action="'+action+'" '+extra+'>'+label+'</button>';
  }
  function syncControls(){
    if(!state)return;
    const locked=state.loading||state.busy||state.readUncertain;
    state.dialog.querySelectorAll('[data-save],.ve-entry-fields input,.ve-entry-fields textarea,.ve-unit-fields input,[data-action=confirm-unit]').forEach(el=>el.disabled=locked);
    node('ve-save-all').disabled=locked||!dirtyCount();
    state.dialog.querySelectorAll('[data-action=source-ai]').forEach(el=>el.disabled=locked||!state.bank||!!dirtyCount()||!!state.reviewRunning);
    state.dialog.querySelectorAll('[data-action=close]').forEach(el=>el.disabled=state.busy);
    const title=node('ve-title');if(title)title.disabled=locked;
    node('ve-reload').disabled=state.loading||state.busy;
    node('ve-dirty-count').textContent=dirtyCount()?dirtyCount()+'개 항목에 저장하지 않은 수정이 있습니다.':'수정한 내용을 마지막에 한 번에 저장할 수 있습니다.';
    state.dialog.setAttribute('aria-busy',String(state.loading||state.busy));
    syncSelection();
  }
  function sourceMarkup(entry){
    const source=entry.source||{},parts=[];
    if(source.page!=null)parts.push(source.page+'쪽');
    if(source.sheet)parts.push('시트 '+source.sheet);
    if(source.row!=null)parts.push(source.row+'행');
    for(const [key,label] of [['word_cell','단어 셀'],['meaning_cell','뜻 셀'],['cell','셀']])if(source[key])parts.push(label+' '+source[key]);
    if(source.word_cells)parts.push('단어 셀 '+(Array.isArray(source.word_cells)?source.word_cells.join(', '):source.word_cells));
    if(source.meaning_cells)parts.push('뜻 셀 '+(Array.isArray(source.meaning_cells)?source.meaning_cells.join(', '):source.meaning_cells));
    const rawWord=entry.word_raw??entry.original_word??entry.word,rawMeaning=entry.meaning_raw??entry.meaning;
    const path=API+encodeURIComponent(state.bank.id);
    const image=Number.isInteger(source.page)&&state.bank.source?.format!=='xlsx'?'<img class="ve-source-image" loading="lazy" data-source-image="'+path+'/entries/'+encodeURIComponent(entry.id)+'/image" alt="원본 '+escape(source.page)+'쪽 '+escape(entry.source_number??'')+'번 어휘 부분"><p class="me-help ve-image-error" hidden>원본 이미지를 불러오지 못했습니다. 전체 원본을 열어 확인해 주세요.</p>':'';
    return '<details class="ve-source"><summary>원문과 출처 보기</summary><p class="me-help ve-source-copy">'+escape(parts.join(' · ')||state.bank.source?.filename||'원본 위치 정보 없음')+'</p><p class="ve-source-copy">원문 단어: '+escape(rawWord)+'\n원문 뜻: '+escape(rawMeaning)+'</p>'+image+'<a href="'+path+'/source" target="_blank" rel="noopener">전체 원본 열기</a></details>';
  }
  function filteredEntries(){
    if(!state?.bank)return [];
    const search=state.search.trim().toLocaleLowerCase();
    return state.bank.entries.filter(entry=>{
      if(state.filter!=='all'&&(state.filter==='review'?!needsReview(entry):entry.status!==state.filter))return false;
      if(state.chapter&&entry.unit_id!==state.chapter)return false;
      const values=state.entries.get(entry.id)||entryValues(entry);
      return !search||[values.word,values.meaning,entry.word_raw,entry.meaning_raw,entry.source_number].some(value=>String(value??'').toLocaleLowerCase().includes(search));
    });
  }
  function pageEntries(){return filteredEntries().slice((state.page-1)*PAGE_SIZE,state.page*PAGE_SIZE);}
  function setOptions(open,focus=false){
    if(!state)return;
    state.optionsOpen=Boolean(open);node('ve-options-menu').hidden=!state.optionsOpen;
    node('ve-options').setAttribute('aria-expanded',String(state.optionsOpen));
    if(focus)(state.optionsOpen?node('ve-options-menu').querySelector('button'):node('ve-options')).focus();
  }
  function syncSelection(){
    if(!state?.bank)return;
    const eligible=new Set(state.bank.entries.map(entry=>entry.id));
    for(const id of state.selectedEntries)if(!eligible.has(id))state.selectedEntries.delete(id);
    const page=pageEntries(),selected=state.selectedEntries,locked=state.loading||state.busy||state.readUncertain;
    const rows=filteredEntries(),toggle=node('ve-select-all'),count=rows.filter(entry=>selected.has(entry.id)).length;
    toggle.checked=rows.length>0&&count===rows.length;toggle.indeterminate=count>0&&count<rows.length;toggle.disabled=locked||!rows.length;
    const reviewSelected=state.bank.entries.some(entry=>selected.has(entry.id)&&entryStatus(entry)==='review');
    node('ve-confirm-selected').hidden=state.tab!=='words'||!reviewSelected;
    node('ve-confirm-selected').disabled=locked||!reviewSelected;
    node('ve-options-wrap').hidden=state.tab!=='words';node('ve-options').disabled=locked||!selected.size;
    node('ve-options-menu').querySelectorAll('button').forEach(el=>el.disabled=locked||!selected.size);
    if(locked||!selected.size||state.tab!=='words')setOptions(false);
    const outside=selected.size-page.filter(entry=>selected.has(entry.id)).length;
    node('ve-selection-count').textContent='선택 '+selected.size+'개'+(outside?' · 다른 페이지·검색 결과 '+outside+'개 포함':'');
    state.dialog.querySelectorAll('[data-entry-select]').forEach(input=>{input.checked=selected.has(input.closest('[data-entry]').dataset.entry);input.disabled=locked;});
  }
  function selectEntries(scope,checked=true){
    if(!state?.bank||state.loading||state.busy||state.readUncertain)return;
    const rows=scope==='results'?filteredEntries():pageEntries();
    for(const entry of rows)if(checked)state.selectedEntries.add(entry.id);else state.selectedEntries.delete(entry.id);
    syncSelection();
  }
  function setSelectedStatus(status){
    if(!state?.bank||state.loading||state.busy||state.readUncertain)return;
    const selected=state.bank.entries.filter(entry=>state.selectedEntries.has(entry.id)&&(status!=='ready'||entryStatus(entry)==='review'));
    if(!selected.length)return;
    for(const entry of selected)if(status===entry.status)state.entryStatuses.delete(entry.id);else state.entryStatuses.set(entry.id,status);
    setOptions(false);
    renderEntries();
    tell('ve-bulk-feedback',status==='ready'?selected.length+'개 단어를 확인 완료로 표시했습니다. ‘저장’ 버튼을 누르면 적용됩니다.':'옵션이 설정되었습니다. 선택한 '+selected.length+'개 단어의 '+(status==='review'?'확인 필요 재설정':'출제 제외')+'는 ‘저장’ 버튼을 누르면 적용됩니다.');
  }
  function renderEntries(){
    const rows=filteredEntries(),pages=Math.max(1,Math.ceil(rows.length/PAGE_SIZE));
    state.page=Math.min(state.page,pages);
    const start=(state.page-1)*PAGE_SIZE;
    node('ve-result-count').textContent='검색 결과 '+rows.length+'개'+(rows.length?' · '+(start+1)+'–'+Math.min(start+PAGE_SIZE,rows.length)+'개 표시':'');
    node('ve-entry-list').innerHTML=rows.slice(start,start+PAGE_SIZE).map((entry,index)=>{
      const values=state.entries.get(entry.id)||entryValues(entry),unit=state.bank.units.find(item=>item.id===entry.unit_id),key='ve-entry-'+index;
      const reason=[issueText(entry.issues)||issueText(entry.review_reason),issueText(entry.learning_review)].filter(Boolean).join(' · ');
      const learning=entry.learning_review?'학습자료 확인 필요':entry.learning_stale?'학습자료 갱신 필요':entry.learning_pending?'학습자료 준비 중':'';
      const status=entryStatus(entry),pending=state.entryStatuses.has(entry.id);
      return '<article class="ve-entry" data-entry="'+escape(entry.id)+'"><div class="ve-entry-head"><label class="ve-select-word"><input type="checkbox" data-entry-select aria-label="'+escape(entry.source_number??start+index+1)+'번 '+escape(values.word)+' 선택"></label><strong>'+escape(unit?.label||'챕터 미지정')+' · '+escape(entry.source_number??start+index+1)+'번</strong><span class="ve-entry-status"><span class="me-badge" data-tone="'+(status==='review'?'warning':'neutral')+'">'+(pending&&status==='ready'?'확인 완료 예정':statusLabel(status))+'</span>'+(reason?'<span class="me-help ve-review-reason">'+escape(reason)+'</span>':'')+'</span>'+(pending?'<span class="me-help ve-pending">저장 시 반영</span>':'')+(learning?'<span class="me-help">'+learning+'</span>':'')+'</div><div class="ve-entry-fields">'+field('영단어',key+'-word',values.word,300)+field('뜻',key+'-meaning',values.meaning,4000,'textarea')+'</div>'+sourceMarkup(entry)+'</article>';
    }).join('')||'<p class="me-help">'+(state.filter==='review'?'확인이 필요한 어휘가 없습니다. 모든 단어를 선택하면 등록된 어휘를 수정할 수 있습니다.':'조건에 맞는 어휘가 없습니다.')+'</p>';
    node('ve-page-label').textContent=state.page+' / '+pages;
    node('ve-prev').disabled=state.page<=1;node('ve-next').disabled=state.page>=pages;
    node('ve-pagination').hidden=pages===1;
    node('ve-entry-list').querySelectorAll('details').forEach(details=>details.addEventListener('toggle',()=>{
      if(!details.open)return;const image=details.querySelector('[data-source-image]');
      if(image&&!image.getAttribute('src')){image.addEventListener('error',()=>{image.hidden=true;details.querySelector('.ve-image-error').hidden=false;},{once:true});image.src=image.dataset.sourceImage;}
    }));
    syncControls();
  }
  function renderUnits(){
    node('ve-unit-list').innerHTML=state.bank.units.map((unit,index)=>{
      const key='ve-unit-'+index;
      return '<div class="ve-unit" data-unit="'+escape(unit.id)+'"><div class="ve-unit-fields">'+field('챕터 '+(index+1)+' 이름',key,state.units.get(unit.id)??unit.label,160)+'</div>'+(unit.status==='review'?'<div class="ve-entry-actions"><span class="me-badge" data-tone="warning">'+(state.unitConfirm.has(unit.id)?'확인 완료 예정':'확인 필요')+'</span>'+button(state.unitConfirm.has(unit.id)?'확인 취소':'확인 완료','confirm-unit')+'</div><p class="me-help">원문의 챕터 이름과 범위를 확인한 뒤 확인 완료를 누르세요. ‘저장’ 버튼을 누르면 반영됩니다.</p>':'')+'<p class="me-help">어휘 '+state.bank.entries.filter(entry=>entry.unit_id===unit.id).length+'개'+(unit.original_label?' · 원래 이름: '+escape(unit.original_label):'')+'</p></div>';
    }).join('')||'<p class="me-help">등록된 챕터가 없습니다.</p>';
    syncControls();
  }
  function renderBank(){
    if(!state?.bank)return;
    const active=document.activeElement,entry=active?.closest?.('[data-entry]'),unit=active?.closest?.('[data-unit]');
    const focus=state.dialog.contains(active)?{id:active.id,entry:entry?.dataset.entry,unit:unit?.dataset.unit,tag:active.tagName,start:active.selectionStart,end:active.selectionEnd}:null;
    const scroll=state.dialog.querySelector('.vocabulary-editor-body').scrollTop;
    node('ve-title').value=state.titleDraft??state.bank.title;
    node('ve-bank-counts').textContent='전체 '+state.bank.entries.length+'개 · 확인 필요 '+state.bank.entries.filter(needsReview).length+'개';
    const construction=state.bank.construction,progress=node('ve-construction');
    progress.hidden=construction?.engine_version!==2;
    if(!progress.hidden){
      const source=construction.source_status==='needs_review'?'원문 확인 필요':'원문 확인 완료';
      const learning=construction.learning_status==='complete'?'추가 학습자료 준비 완료':construction.learning_status==='needs_review'?'추가 학습자료 확인 필요':'추가 학습자료 준비 중';
      progress.textContent=(construction.registration_status==='complete'||construction.publishing_status==='complete'?'단어장 등록 완료':'단어장 등록 진행 중')+' · '+source+' · '+learning;
    }
    const audit=state.bank.registration_audit;
    node('ve-source-audit').textContent=audit?.state==='passed'?'원본 자동 검수 통과 · '+audit.checked_entries+'개 · '+({automatic:'자동 대조',hybrid:'자동 대조 + AI 검수',agent:'AI 재판독 + 독립 검수'}[audit.method]||'원본 대조'):'이 단어장은 새 원본 검수를 아직 받지 않았습니다.';
    const select=node('ve-chapter');select.innerHTML='<option value="">모든 챕터</option>'+state.bank.units.map(unit=>'<option value="'+escape(unit.id)+'">'+escape(unit.label)+'</option>').join('');
    if(!state.bank.units.some(unit=>unit.id===state.chapter))state.chapter='';select.value=state.chapter;
    renderUnits();renderEntries();syncControls();
    if(focus){
      let target=focus.id?node(focus.id):null;
      if(focus.entry){const row=[...state.dialog.querySelectorAll('[data-entry]')].find(item=>item.dataset.entry===focus.entry);target=row?.querySelector(focus.tag==='TEXTAREA'?'textarea':'input[type=text]');}
      else if(focus.unit){const row=[...state.dialog.querySelectorAll('[data-unit]')].find(item=>item.dataset.unit===focus.unit);target=row?.querySelector('input');}
      if(target&&!target.disabled){target.focus({preventScroll:true});if(Number.isInteger(focus.start)&&typeof target.setSelectionRange==='function')target.setSelectionRange(focus.start,focus.end);}
    }
    state.dialog.querySelector('.vocabulary-editor-body').scrollTop=scroll;
  }
  function setTab(tab){
    state.tab=tab;
    for(const name of ['words','chapters']){
      node('ve-tab-'+name).setAttribute('aria-selected',String(name===tab));
      node('ve-tab-'+name).tabIndex=name===tab?0:-1;
      node('ve-panel-'+name).hidden=name!==tab;
    }
    syncControls();
  }
  function publish(bank){
    window.dispatchEvent(new CustomEvent('melody-bank-updated',{detail:bank}));
  }
  function matches(bank,operation){
    const values=operation.values;
    return (values.title===undefined||bank.title===values.title)&&values.units.every(value=>{
      const unit=bank.units.find(item=>item.id===value.id);
      return unit&&unit.label===value.label&&(!value.status||unit.status===value.status);
    })&&values.entries.every(value=>{
      const entry=bank.entries.find(item=>item.id===value.id);
      return entry&&entry.word===value.word&&entry.meaning===value.meaning&&entry.status===value.status;
    });
  }
  function retireDraft(){
    state.titleDraft=null;state.units.clear();state.entries.clear();state.unitConfirm.clear();state.entryStatuses.clear();state.selectedEntries.clear();setOptions(false);tell('ve-bulk-feedback');
  }
  function footerResult(text=''){
    if(!state)return;
    const owner=state;clearTimeout(owner.resultTimer);
    tell('ve-save-result',text,'success');
    if(text)owner.resultTimer=setTimeout(()=>{if(state===owner)tell('ve-save-result');},5000);
  }
  async function acceptSave(bank,operation,recovered=false){
    state.bank=bank;state.readUncertain=false;state.pending=null;retireDraft();
    const text='수정한 '+operation.count+'개 항목을 모두 저장했습니다.';
    tell('ve-global-feedback',text+(recovered?' 저장 결과를 다시 확인했습니다.':''),'success');
    publish(bank);renderBank();footerResult(text);
  }

  async function loadBank(reconcile=false){
    if(!state||state.loading||state.busy)return;
    const owner=state;owner.loading=true;tell('ve-global-feedback');node('ve-loading').hidden=false;syncControls();
    try{
      const bank=await apiGet(API+encodeURIComponent(owner.options.bankId)+'?view=management');
      if(state!==owner)return;if(!validBank(bank,owner.options.bankId))throw Error('단어장 내용을 확인하지 못했습니다. 다시 불러와 주세요.');
      const firstLoad=!owner.bank;
      owner.bank=bank;owner.readUncertain=false;
      if(firstLoad&&!bank.entries.some(needsReview)){owner.filter='all';node('ve-filter').value='all';}
      if(reconcile&&owner.pending&&matches(bank,owner.pending))await acceptSave(bank,owner.pending,true);
      else {if(reconcile&&owner.pending){tell('ve-global-feedback','서버의 최신 상태를 확인했습니다. 입력한 내용을 확인한 뒤 다시 저장해 주세요.','danger');owner.pending=null;}renderBank();}
      node('ve-content').hidden=false;
      if(bank.registration_review_job&&!owner.reviewPolling&&!owner.reviewFinished?.has(bank.registration_review_job))watchSourceReview(owner,bank.registration_review_job);
    }catch(error){if(state===owner)tell('ve-global-feedback',error.message||'단어장을 불러오지 못했습니다. 다시 불러오기를 눌러 주세요.','danger');}
    finally{if(state===owner){owner.loading=false;node('ve-loading').hidden=true;syncControls();}}
  }
  async function watchSourceReview(owner,jobId){
    if(state!==owner||owner.reviewPolling)return;owner.reviewPolling=true;owner.reviewRunning=true;syncControls();
    try{
      const job=await apiGet('/api/word-meaning/jobs/'+encodeURIComponent(jobId));
      if(state!==owner)return;
      tell('ve-source-feedback',job.message||'원본 검수 중입니다. 화면을 닫아도 계속 진행됩니다.',job.requires_attention?'danger':'neutral');
      if(job.registration_status==='complete'||['complete','failed','interrupted'].includes(job.status)){
        if(job.registration_status==='complete'){
          const bank=await apiGet(API+encodeURIComponent(owner.options.bankId)+'?view=management');
          if(!validBank(bank,owner.options.bankId))throw Error('검수된 단어장을 확인하지 못했습니다.');
          if(state===owner&&!dirtyCount()&&!owner.busy&&owner.bank.version<=bank.version){
            owner.bank=bank;
            renderBank();
          }
          publish(bank);
        }
        owner.reviewRunning=false;(owner.reviewFinished??=new Set()).add(jobId);
      }else owner.reviewTimer=setTimeout(()=>watchSourceReview(owner,jobId),3000);
    }catch(error){if(state===owner){tell('ve-source-feedback','검수 상태를 확인하지 못했습니다. 서버 작업은 계속됩니다. 다시 불러오기로 확인해 주세요.','danger');owner.reviewRunning=false;}}
    finally{owner.reviewPolling=false;if(state===owner)syncControls();}
  }
  async function reviewSource(){
    const mode='ai';
    if(!state?.bank||state.busy||state.loading||state.reviewRunning||dirtyCount())return;
    const owner=state;owner.busy=true;syncControls();
    // Reuse the same id after a lost response; a second click cannot duplicate work.
    owner.reviewRequest=owner.reviewRequest?.mode===mode?owner.reviewRequest:{mode,id:crypto.randomUUID()};
    try{
      const result=await apiSend(API+encodeURIComponent(owner.bank.id)+'/source-review','POST',{version:owner.bank.version,mode,request_id:owner.reviewRequest.id});
      if(state!==owner)return;owner.busy=false;owner.reviewRequest=null;
      await watchSourceReview(owner,result.job_id);
    }catch(error){if(state===owner)tell('ve-source-feedback',error.message||'검수를 시작하지 못했습니다. 다시 시도해 주세요.','danger');}
    finally{if(state===owner){owner.busy=false;syncControls();}}
  }
  async function save(operation){
    if(!state?.bank||state.busy||state.loading||state.readUncertain)return;
    const owner=state;owner.busy=true;footerResult();tell('ve-global-feedback','저장 중…','neutral');syncControls();
    const started=Date.now(),progress=()=>tell('ve-global-feedback','저장 중 · '+Math.floor((Date.now()-started)/1000)+'초 경과');
    progress();const timer=setInterval(progress,1000);
    const path=API+encodeURIComponent(owner.bank.id)+'/edit';
    try{
      const bank=await apiSend(path,'PATCH',{version:owner.bank.version,...operation.values});
      if(state!==owner)return;if(!validBank(bank,owner.bank.id))throw Error('저장 응답을 확인하지 못했습니다.');
      if(!matches(bank,operation))throw Error('저장 응답에서 수정 내용을 확인하지 못했습니다.');
      await acceptSave(bank,operation);
    }catch(error){
      if(state!==owner)return;
      if(!error.status||error.status===409||error.status>=500||[408,425,429].includes(error.status)){
        owner.pending=operation;
        try{
          const latest=await apiGet(API+encodeURIComponent(owner.bank.id)+'?view=management');
          if(!validBank(latest,owner.bank.id))throw Error('조회 결과 확인 실패');
          owner.bank=latest;
          if(matches(latest,operation))await acceptSave(latest,operation,true);
          else {owner.pending=null;tell('ve-global-feedback',error.status===409?'다른 곳에서 단어장이 수정되었습니다. 입력은 보존했습니다. 최신 내용을 확인한 뒤 다시 저장해 주세요.':'저장 완료를 확인하지 못했습니다. 입력은 보존했습니다. 다시 저장해 주세요.','danger');renderBank();}
        }catch{owner.readUncertain=true;tell('ve-global-feedback','저장 결과를 확인하지 못했습니다. 입력은 보존했습니다. 다시 불러오기로 저장 결과를 확인한 뒤 추가 저장할 수 있습니다.','danger');renderBank();}
      }else {tell('ve-global-feedback',error.message||'저장하지 못했습니다. 입력을 확인해 주세요.','danger');renderBank();}
    }finally{clearInterval(timer);if(state===owner){owner.busy=false;syncControls();}}
  }
  function saveAll(){
    if(!state?.bank||state.busy||state.loading||state.readUncertain||!dirtyCount())return;
    const values={units:[],entries:[]};
    if(state.titleDraft!==null){
      const title=state.titleDraft.trim();
      if(!title||title.length>160){tell('ve-global-feedback','단어장 이름을 1~160자로 입력해 주세요.','danger');return;}
      values.title=title;
    }
    for(const id of new Set([...state.units.keys(),...state.unitConfirm])){
      const unit=state.bank.units.find(item=>item.id===id),label=(state.units.get(id)??unit?.label??'').trim();
      if(!unit||!label||label.length>160){tell('ve-global-feedback','챕터 이름을 1~160자로 입력해 주세요.','danger');return;}
      values.units.push({id,label,...(state.unitConfirm.has(id)?{status:'ready'}:{})});
    }
    for(const id of new Set([...state.entries.keys(),...state.entryStatuses.keys()])){
      const entry=state.bank.entries.find(item=>item.id===id);
      if(!entry){tell('ve-global-feedback','수정할 단어를 찾을 수 없습니다. 다시 불러와 주세요.','danger');return;}
      const draft=state.entries.get(id)||entryValues(entry),word=draft.word.trim(),meaning=draft.meaning.trim();
      if(!word||!meaning||word.length>300||meaning.length>4000){tell('ve-global-feedback',(entry.source_number??'')+'번: 영단어는 1~300자, 뜻은 1~4,000자로 입력해 주세요.','danger');return;}
      values.entries.push({id,word,meaning,status:entryStatus(entry)});
    }
    return save({kind:'batch',values,count:dirtyCount()});
  }
  function close(){
    if(!state||state.busy)return;
    const owner=state;clearTimeout(owner.resultTimer);clearTimeout(owner.reviewTimer);owner.dialog.close();owner.dialog.remove();state=null;
    owner.options.trigger?.focus?.({preventScroll:true});owner.resolve(true);
  }
  function requestClose(){
    if(!state||state.busy)return;
    if(dirtyCount()||state.readUncertain){node('ve-close-confirm').hidden=false;node('ve-keep-editing').focus();return;}
    close();
  }
  function onInput(event){
    if(!state?.bank||state.busy||state.loading||state.readUncertain)return;const target=event.target;
    if(target.id==='ve-select-all'){selectEntries('results',target.checked);return;}
    if(target.matches('[data-entry-select]')){const id=target.closest('[data-entry]').dataset.entry;if(target.checked)state.selectedEntries.add(id);else state.selectedEntries.delete(id);syncSelection();return;}
    if(target.id==='ve-title'){state.titleDraft=target.value===state.bank.title?null:target.value;}
    const unit=target.closest('[data-unit]');if(unit){const original=state.bank.units.find(item=>item.id===unit.dataset.unit);if(target.value===original.label)state.units.delete(original.id);else state.units.set(original.id,target.value);}
    const entry=target.closest('[data-entry]');if(entry){const original=state.bank.entries.find(item=>item.id===entry.dataset.entry),values={word:entry.querySelector('input[type=text]').value,meaning:entry.querySelector('textarea').value};if(equal(values,entryValues(original)))state.entries.delete(original.id);else state.entries.set(original.id,values);}
    if(node('ve-global-feedback').dataset.tone==='success')tell('ve-global-feedback');
    if(target.id==='ve-search'){state.search=target.value;state.page=1;renderEntries();}
    syncControls();
  }
  function onClick(event){
    if(state?.optionsOpen&&!event.target.closest('.ve-options'))setOptions(false);
    const control=event.target.closest('[data-action]');if(!control||control.disabled)return;
    const action=control.dataset.action;
    if(action==='close')requestClose();
    else if(action==='keep'){node('ve-close-confirm').hidden=true;node('ve-title').focus();}
    else if(action==='discard')close();
    else if(action==='reload')loadBank(true);
    else if(action==='save-all')saveAll();
    else if(action==='source-ai')reviewSource();
    else if(action==='confirm-selected')setSelectedStatus('ready');
    else if(action==='options')setOptions(!state.optionsOpen,true);
    else if(action==='reset-selected'||action==='exclude-selected'){setSelectedStatus(action==='reset-selected'?'review':'excluded');node('ve-options').focus();}
    else if(action==='confirm-unit'){if(state.loading||state.busy||state.readUncertain)return;const id=control.closest('[data-unit]').dataset.unit;if(state.unitConfirm.has(id))state.unitConfirm.delete(id);else state.unitConfirm.add(id);renderUnits();}
    else if(action==='tab')setTab(control.dataset.tab);
    else if(action==='prev'||action==='next'){state.page+=action==='next'?1:-1;renderEntries();node('ve-result-count').focus({preventScroll:true});node('ve-panel-words').scrollIntoView({block:'start'});}
  }
  function open(options){
    if(state)return Promise.resolve(false);
    if(!options?.bankId)return Promise.resolve(false);
    const dialog=document.createElement('dialog');dialog.className='me-dialog vocabulary-editor';dialog.setAttribute('aria-labelledby','ve-heading');dialog.setAttribute('aria-describedby','ve-description');
    dialog.innerHTML='<div class="vocabulary-editor-head"><h2 id="ve-heading">단어장 수정</h2>'+button('AI 단어장 재생성','source-ai','secondary','aria-describedby="ve-source-description"')+button('×','close','ghost','aria-label="단어장 수정 닫기"')+'</div><div class="vocabulary-editor-body"><p id="ve-description" class="me-help">단어장 이름, 챕터 이름과 어휘를 수정할 수 있습니다. 수정을 마친 뒤 하단의 ‘저장’을 한 번 눌러 반영하세요.</p><p id="ve-loading" class="ve-loading me-help" role="status"><span class="me-spinner" aria-hidden="true"></span>단어장을 불러오는 중입니다.</p><p id="ve-global-feedback" class="ve-global-feedback ve-feedback me-help" role="status" hidden></p><div id="ve-content" hidden><div class="ve-title-row">'+field('단어장 이름','ve-title','',160)+'</div><p id="ve-bank-counts" class="me-help"></p><p id="ve-construction" class="me-help" hidden></p><p id="ve-source-audit" class="me-help"></p><p id="ve-source-description" class="me-help">AI 단어장 재생성은 저장된 원본 전체를 다시 읽고, 검수 후 같은 단어장에 반영합니다.</p><p id="ve-source-feedback" class="me-help ve-feedback" role="status" hidden></p><div class="me-tabs ve-tabs" role="tablist" aria-label="단어장 수정 항목"><button type="button" id="ve-tab-words" role="tab" aria-controls="ve-panel-words" aria-selected="true" data-action="tab" data-tab="words">단어</button><button type="button" id="ve-tab-chapters" role="tab" aria-controls="ve-panel-chapters" aria-selected="false" tabindex="-1" data-action="tab" data-tab="chapters">챕터</button></div><section id="ve-panel-words" role="tabpanel" aria-labelledby="ve-tab-words"><div class="ve-filters">'+field('단어·뜻 검색','ve-search','',4000)+'<label class="me-field me-label" for="ve-chapter">챕터<select class="me-input" id="ve-chapter"><option value="">모든 챕터</option></select></label><label class="me-field me-label" for="ve-filter">표시할 단어<select class="me-input" id="ve-filter"><option value="review">확인 필요</option><option value="all">모든 단어</option><option value="ready">등록 완료</option><option value="excluded">출제 제외</option></select></label></div><p class="me-help">단어를 선택한 뒤 확인 완료 또는 옵션을 설정하세요. 수정한 내용은 하단의 ‘저장’ 버튼으로 함께 반영됩니다.</p><div class="ve-bulk-review" role="group" aria-label="여러 단어 선택"><label class="ve-select-word me-help"><input type="checkbox" id="ve-select-all"> 전체 선택</label><span id="ve-selection-count" class="me-help" aria-live="polite"></span><span class="me-help">현재 검색·챕터 범위에 적용</span></div><p id="ve-result-count" class="ve-result-count me-help" tabindex="-1" aria-live="polite"></p><div id="ve-entry-list"></div><div id="ve-pagination" class="ve-pagination">'+button('이전','prev','secondary','id="ve-prev"')+'<span id="ve-page-label" class="me-help" aria-live="polite"></span>'+button('다음','next','secondary','id="ve-next"')+'</div></section><section id="ve-panel-chapters" role="tabpanel" aria-labelledby="ve-tab-chapters" hidden><div id="ve-unit-list"></div></section></div></div><div id="ve-close-confirm" class="ve-close-confirm" role="alert" hidden><p class="me-help">저장하지 않은 수정을 폐기하고 닫을까요? 이미 저장한 내용은 유지됩니다.</p><div class="me-dialog-actions">'+button('계속 편집','keep','secondary','id="ve-keep-editing"')+button('폐기하고 닫기','discard','danger')+'</div></div><div class="vocabulary-editor-footer"><div class="ve-footer-status"><div class="ve-footer-status-row"><span id="ve-dirty-count" class="me-help" aria-live="polite"></span>'+button('다시 불러오기','reload','ghost','id="ve-reload"')+'</div><span id="ve-save-result" class="me-help" role="status" aria-live="polite" hidden></span><p id="ve-bulk-feedback" class="me-help ve-feedback" role="status" hidden></p></div><div class="ve-footer-actions">'+button('확인 완료','confirm-selected','secondary','id="ve-confirm-selected" hidden')+'<div class="ve-options" id="ve-options-wrap">'+button('옵션','options','secondary','id="ve-options" aria-haspopup="menu" aria-expanded="false" aria-controls="ve-options-menu"')+'<div id="ve-options-menu" class="ve-options-menu" role="menu" aria-label="선택한 단어 옵션" hidden>'+button('확인 필요 재설정','reset-selected','ghost','role="menuitem"')+button('출제 제외','exclude-selected','ghost','role="menuitem"')+'</div></div>'+button('저장','save-all','primary','id="ve-save-all" data-save')+button('닫기','close','secondary')+'</div></div>';
    const promise=new Promise(resolve=>{state={dialog,options,resolve,bank:null,titleDraft:null,units:new Map(),entries:new Map(),unitConfirm:new Set(),entryStatuses:new Map(),optionsOpen:false,selectedEntries:new Set(),tab:'words',filter:'review',chapter:'',search:'',page:1,busy:false,loading:false,readUncertain:false,pending:null};});
    dialog.addEventListener('input',onInput);dialog.addEventListener('click',onClick);dialog.addEventListener('cancel',event=>{event.preventDefault();if(state.optionsOpen)setOptions(false,true);else requestClose();});
    dialog.addEventListener('change',event=>{if(event.target.id==='ve-filter')state.filter=event.target.value;else if(event.target.id==='ve-chapter')state.chapter=event.target.value;else return;state.page=1;renderEntries();});
    dialog.addEventListener('keydown',event=>{
      if(state.optionsOpen&&event.key==='Escape'){event.preventDefault();event.stopPropagation();setOptions(false,true);return;}
      if(event.target.closest('#ve-options-menu')){
        if(event.key==='Tab'){setOptions(false);node('ve-options').focus();}
        else if(['ArrowUp','ArrowDown','Home','End'].includes(event.key)){event.preventDefault();const items=[...node('ve-options-menu').querySelectorAll('button')],index=items.indexOf(event.target),next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowUp'?-1:1)+items.length)%items.length;items[next].focus();return;}
      }
      if(event.target.id==='ve-options'&&['ArrowUp','ArrowDown'].includes(event.key)&&!event.target.disabled){event.preventDefault();setOptions(true,true);if(event.key==='ArrowUp')node('ve-options-menu').lastElementChild.focus();return;}
      if(event.target.getAttribute('role')==='tab'&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?'words':event.key==='End'?'chapters':state.tab==='words'?'chapters':'words';setTab(next);node('ve-tab-'+next).focus();}});
    document.body.append(dialog);if(window.MelodyUI)window.MelodyUI.openDialog(dialog,options.trigger);else dialog.showModal();loadBank();
    return promise;
  }
  window.addEventListener('beforeunload',event=>{if(dirtyCount()||state?.busy||state?.readUncertain){event.preventDefault();event.returnValue='';}});
  window.MelodyVocabularyEditor=Object.freeze({open,isOpen:()=>!!state,currentEditedBank:()=>state?.bank||null,hasDrafts:()=>dirtyCount()>0});
})();

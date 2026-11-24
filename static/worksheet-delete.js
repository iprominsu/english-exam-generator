/* Shared worksheet deletion dialog; storage operations are supplied by callers. */
(function(){
  'use strict';
  let active=false;
  function open({title,onDelete}){
    if(active)return Promise.resolve(false);
    active=true;
    return new Promise(resolve=>{
      const dialog=document.createElement('dialog');dialog.className='me-dialog';dialog.setAttribute('aria-labelledby','worksheet-delete-heading');dialog.setAttribute('aria-describedby','worksheet-delete-description');
      const heading=document.createElement('h2');heading.id='worksheet-delete-heading';heading.textContent='시험지 삭제';
      const name=document.createElement('p');name.textContent=title;
      const description=document.createElement('p');description.id='worksheet-delete-description';description.className='me-help';description.textContent='목록에서 삭제하면 웹사이트에서만 숨겨지고 파일은 보존됩니다. 완전히 삭제하면 시험지 기록과 생성된 문제지·정답지·시트 파일이 저장소에서 제거되며 되돌릴 수 없습니다.';
      const error=document.createElement('p');error.className='me-help';error.dataset.tone='danger';error.setAttribute('role','alert');error.hidden=true;
      const progress=document.createElement('p');progress.className='worksheet-delete-progress me-help';progress.setAttribute('role','status');progress.setAttribute('aria-live','polite');progress.hidden=true;
      const actions=document.createElement('div');actions.className='me-dialog-actions';
      const buttons=[];let busy=false,finished=false,timer=null;
      function finish(result){if(finished)return;finished=true;if(timer)clearInterval(timer);dialog.close();dialog.remove();active=false;resolve(result);}
      function button(label,variant,handler){const element=document.createElement('button');element.type='button';element.className='me-button';element.dataset.variant=variant;element.textContent=label;element.addEventListener('click',handler);actions.appendChild(element);buttons.push(element);return element;}
      button('취소','ghost',()=>finish(false));
      async function remove(mode){
        if(busy)return;busy=true;error.hidden=true;buttons.forEach(button=>button.disabled=true);dialog.setAttribute('aria-busy','true');
        const began=Date.now();let state=mode==='permanent'?'시험지 기록과 파일을 삭제하고 있습니다.':'시험지 목록에서 삭제하고 있습니다.';
        const tick=()=>{progress.textContent=state+' · '+Math.floor((Date.now()-began)/1000)+'초 경과';};
        progress.hidden=false;tick();timer=setInterval(tick,1000);
        try{await onDelete(mode,text=>{state=text;tick();});finish(true);}catch(reason){error.textContent=reason.message||'삭제하지 못했습니다. 다시 시도해 주세요.';error.hidden=false;state='삭제 처리 중 확인이 필요합니다.';tick();}
        finally{clearInterval(timer);timer=null;busy=false;buttons.forEach(button=>button.disabled=false);dialog.removeAttribute('aria-busy');}
      }
      button('목록에서 삭제','secondary',()=>remove('soft'));
      button('완전히 삭제','danger',()=>remove('permanent'));
      dialog.addEventListener('cancel',event=>{event.preventDefault();if(!busy)finish(false);});
      dialog.append(heading,name,description,progress,error,actions);document.body.appendChild(dialog);
      if(window.MelodyUI)window.MelodyUI.openDialog(dialog);else dialog.showModal();
    });
  }
  window.MelodyWorksheetDelete=Object.freeze({open});
})();

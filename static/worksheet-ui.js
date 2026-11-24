/* Shared list ordering and short-lived bank reads across the parent/child UI. */
(function(){
  'use strict';
  const names=new Intl.Collator('ko',{numeric:true,sensitivity:'base'});
  function time(value){const parsed=Date.parse(value);return Number.isFinite(parsed)?parsed:null;}
  function sort(items,key='created',direction='desc'){
    const sign=direction==='asc'?1:-1;
    return items.slice().sort((a,b)=>{
      const title=names.compare(String(a.title||''),String(b.title||''));
      let compared=title;
      if(key!=='name'){
        const timestamp=item=>{
          const created=time(item.created_at||item.created),updated=time(item.updated_at||item.updated);
          return key==='latest'?Math.max(created??-Infinity,updated??-Infinity):created??-Infinity;
        };
        const left=timestamp(a),right=timestamp(b);
        if(Number.isFinite(left)!==Number.isFinite(right))return Number.isFinite(left)?-1:1;
        compared=Number.isFinite(left)?left-right:0;
      }
      return sign*(compared||title||names.compare(String(a.id||a.day+'-'+a.version),String(b.id||b.day+'-'+b.version)));
    });
  }
  function direction(button,value){
    const label=value==='desc'?'내림차순 · 오름차순으로 변경':'오름차순 · 내림차순으로 변경';
    button.dataset.direction=value;button.title=label;button.setAttribute('aria-label',label);
  }
  window.MelodyWorksheetList=Object.freeze({sort,direction});
  try{if(window.parent!==window&&window.parent.MelodyBankData){window.MelodyBankData=window.parent.MelodyBankData;return;}}catch{}
  const values=new Map(),pending=new Map(),epochs=new Map(),TTL=15000;
  function invalidate(key){
    const keys=key?[key,'/banks']:[...new Set([...values.keys(),...pending.keys()])];
    for(const item of keys){values.delete(item);pending.delete(item);epochs.set(item,(epochs.get(item)||0)+1);}
  }
  async function get(key,loader,{force=false}={}){
    if(force)invalidate(key);
    const cached=values.get(key);
    if(cached&&Date.now()-cached.at<TTL)return structuredClone(cached.value);
    if(pending.has(key))return structuredClone(await pending.get(key));
    const epoch=epochs.get(key)||0;
    const promise=Promise.resolve().then(loader).then(value=>{
      if((epochs.get(key)||0)===epoch)values.set(key,{at:Date.now(),value:structuredClone(value)});
      return value;
    }).finally(()=>{if(pending.get(key)===promise)pending.delete(key);});
    pending.set(key,promise);return structuredClone(await promise);
  }
  window.MelodyBankData=Object.freeze({get,invalidate});
})();

/* Keep the active PDF document while its canvases adapt to the available width. */
(function(){
  'use strict';
  function create({pdf,pages,isCurrent,onError,onLayout=()=>{}}){
    let disposed=false,generation=0,task=null,timer=null,width=0,ratio=0;
    const active=()=>!disposed&&isCurrent();
    const size=()=>({width:Math.max(240,Math.min(820,pages.clientWidth||820)),ratio:Math.min(window.devicePixelRatio||1,2)});
    async function render(){
      if(!active())return;
      const token=++generation,next=size();width=next.width;ratio=next.ratio;
      task?.cancel();task=null;
      if(!pages.querySelector('canvas'))pages.replaceChildren();
      try{
        for(let number=1;number<=pdf.numPages;number++){
          const page=await pdf.getPage(number);if(!active()||token!==generation)return;
          const base=page.getViewport({scale:1}),viewport=page.getViewport({scale:next.width/base.width});
          const canvas=document.createElement('canvas'),previous=pages.children[number-1];
          canvas.width=Math.ceil(viewport.width*next.ratio);canvas.height=Math.ceil(viewport.height*next.ratio);
          canvas.setAttribute('role','img');canvas.setAttribute('aria-label','시험지 PDF '+number+'페이지');
          if(!previous)pages.appendChild(canvas);
          const rendering=page.render({canvas,canvasContext:canvas.getContext('2d'),viewport,transform:[next.ratio,0,0,next.ratio,0,0]});task=rendering;
          try{await rendering.promise;}finally{if(task===rendering)task=null;}
          if(!active()||token!==generation)return;
          canvas.dataset.rendered='true';
          if(previous)previous.replaceWith(canvas);
          onLayout();
        }
      }catch(error){if(active()&&token===generation&&error.name!=='RenderingCancelledException')throw error;}
    }
    function resize(){
      if(!active()||pages.clientWidth<=0)return;
      const next=size();if(next.width===width&&next.ratio===ratio)return;
      clearTimeout(timer);timer=setTimeout(()=>{timer=null;render().catch(onError);},150);
    }
    const observer=typeof ResizeObserver==='function'?new ResizeObserver(resize):null;
    observer?.observe(pages);window.addEventListener('resize',resize);
    function destroy(){
      disposed=true;generation++;clearTimeout(timer);observer?.disconnect();window.removeEventListener('resize',resize);task?.cancel();task=null;
    }
    return Object.freeze({render,destroy});
  }
  window.MelodyPdfPreview=Object.freeze({create});
})();

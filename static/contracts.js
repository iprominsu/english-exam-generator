/* Shared, dependency-free input/response guards; also exercised by Node regression tests. */
(function(root){
  const labels={count:'문항 수는 1~20 사이의 정수로 입력해 주세요.',provider:'선택한 모델을 현재 사용할 수 없습니다. 다른 모델을 선택해 주세요.',day:'파일에 있는 Day를 선택해 주세요.',reasoning:'추론 수준을 다시 선택해 주세요.',vocab_file_id:'등록된 어휘 파일을 선택해 주세요.'};
  function safeMessage(value,fallback='요청을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.'){
    return typeof value==='string'&&value.length<=280&&!/https?:|Traceback|API_KEY|token|\[object Object\]|undefined/i.test(value)&&/[가-힣]/.test(value)?value:fallback;
  }
  function error(body,status){
    const detail=body?.detail??body;
    let fields={},message;
    if(Array.isArray(detail)){
      for(const row of detail){const key=(row?.loc||[]).at(-1);if(key)fields[key]=labels[key]||'입력 형식을 확인해 주세요.';}
      message=Object.values(fields).join(' / ');
    }else if(detail&&typeof detail==='object'){
      message=detail.message;
      for(const [key,value] of Object.entries(detail.fields||{}))fields[key]=safeMessage(value,labels[key]||'입력 형식을 확인해 주세요.');
    }else message=detail;
    const fallback=status===401?'접속 인증이 필요합니다. 페이지를 다시 열고 로그인해 주세요.':status===429||status===503?'외부 서비스가 일시적으로 지연되고 있습니다. 잠시 후 다시 시도해 주세요.':'요청을 완료하지 못했습니다. 작업 상태를 확인해 주세요.';
    return {message:safeMessage(message,fallback),fields,retryable:detail?.retryable===true,code:detail?.code||'',status};
  }
  function integer(raw,min,max){const n=Number(raw);return String(raw).trim()!==''&&Number.isFinite(n)&&Number.isInteger(n)&&n>=min&&n<=max?n:null;}
  function wordContext(q){
    const p=q.payload||{},instruction=(q.instruction||'').replace(/<\/?u>/gi,''),match=instruction.match(/([A-Za-z][A-Za-z'’-]*(?: [A-Za-z][A-Za-z'’-]*){0,3})의\s*뜻/);
    const structured=('sentence' in p)||('target' in p);
    const promised=/(?:문장|예문|문맥|지문)(?:에서|에서는|의|\s*속|\s*안)/.test(instruction+' '+(q.explanation||''));
    return {structured,required:structured||promised,target:structured?p.target:(match?.[1]||''),sentence:structured?p.sentence:(instruction.includes('?')?instruction.slice(instruction.indexOf('?')+1).trim():'')};
  }
  function validateContext(q,i){
    if(q.type!=='word_meaning')return;
    const c=wordContext(q);if(!c.required)return;
    const s=c.sentence,t=c.target;
    const text=v=>typeof v==='string'&&v.trim().length>0&&v.length<=15000;
    const escaped=typeof t==='string'?t.trim().replace(/[.*+?^${}()|[\]\\]/g,'\\$&'):'';
    const asked=(q.instruction||'').replace(/<\/?u>/gi,'').match(/([A-Za-z][A-Za-z'’-]*)의\s*뜻/);
    if(!text(s)||!text(t)||!/[.!?][\s"'”’]*$/.test(s.trim())||(s.match(/[A-Za-z]+/g)||[]).length<3||/<[^>]*>/.test(s)||!new RegExp('(?<![A-Za-z])'+escaped+'(?![A-Za-z])','i').test(s)||!(/^[A-Za-z][A-Za-z'’-]*(?: [A-Za-z][A-Za-z'’-]*){0,5}$/).test(t.trim())||(asked&&asked[1].toLowerCase()!==t.trim().toLowerCase()))throw Error((i+1)+'번 문항의 영어 예문과 대상 단어를 확인해 주세요. 대상 단어가 예문 안에 있어야 합니다.');
  }
  function questions(qs,{checkContext=true}={}){
    if(!Array.isArray(qs)||!qs.length||qs.length>50)throw Error('문항 목록 형식을 확인할 수 없습니다.');
    const text=(v)=>typeof v==='string'&&v.trim().length>0&&v.length<=15000;
    qs.forEach((q,i)=>{
      let valid=q&&text(q.instruction)&&Number.isInteger(q.answer)&&q.answer>=1&&q.answer<=5&&q.payload&&typeof q.payload==='object'&&typeof(q.explanation??'')==='string';
      const p=q?.payload||{},t=q?.type;
      const arr=(v,n)=>Array.isArray(v)&&v.length===n;
      if(['def_match','pair_ko','sentence_match'].includes(t)){
        const vals=t==='sentence_match'?p.items:p.pairs,fields=t==='sentence_match'?['en','u','ko']:['word',t==='def_match'?'def':'ko'];
        valid&&=arr(vals,5)&&vals.every(v=>v&&fields.every(f=>text(v[f])));
      }else if(['word_meaning','blank_ko','common_blank','passage_blank','count_correct'].includes(t)){
        valid&&=arr(p.choices,5)&&p.choices.every(text);
        if(t==='blank_ko')valid&&=text(p.sentence)&&text(p.ko);
        if(t==='passage_blank')valid&&=text(p.passage);
        if(t==='common_blank')valid&&=Array.isArray(p.sentences)&&p.sentences.length>=2&&p.sentences.every(text);
        if(t==='count_correct')valid&&=Array.isArray(p.table)&&p.table.length>0&&p.table.every(v=>v&&text(v.word)&&text(v.ko));
      }else valid=false;
      if(!valid)throw Error((i+1)+'번 문항의 유형 또는 내용 형식이 올바르지 않습니다.');
      if(checkContext)validateContext(q,i);
    });return qs;
  }
  function result(r){
    if(!r||!Number.isInteger(r.day)||r.day<1||!Number.isInteger(r.version)||r.version<1||!Number.isInteger(r.count)||r.count<1||typeof r.title!=='string'||!r.title||typeof r.file_id!=='string'||!r.file_id)throw Error('저장 결과를 확인할 수 없습니다. 작업 상태를 다시 확인해 주세요.');
    return r;
  }
  function driveURL(value){try{const u=new URL(value);return u.protocol==='https:'&&['drive.google.com','docs.google.com'].includes(u.hostname)&&!u.username&&!u.password?u.href:null;}catch{return null;}}
  function typeInfo(q){
    if(q.type==='def_match'){
      if(/철자|스펠링/.test(q.instruction))return ['철자 판별','단어 / 뜻 (철자 판별)'];
      if(q.payload.pairs.every(p=>/[가-힣]/.test(p.def)))return ['단어·한국어 뜻','단어 / 한국어 뜻'];
      if(q.payload.pairs.every(p=>!/[가-힣]/.test(p.def)))return ['영영풀이','단어 / 영어 정의'];
      return ['단어·뜻 판별','단어 / 뜻'];
    }
    return [{word_meaning:'단어 뜻',pair_ko:'단어·한국어 뜻',blank_ko:'빈칸(국문)',common_blank:'공통 빈칸',sentence_match:'문장·해석',count_correct:'개수 세기',passage_blank:'지문 빈칸'}[q.type]||'문항','단어 / 뜻'];
  }
  const reviewLabels={
    unit_label_review:'단원 이름과 단어가 속한 범위를 확인해 주세요.',
    korean_wrap_review:'줄바꿈된 뜻이 빠짐없이 연결되었는지 원본과 비교해 주세요.',
    ocr_layout_review:'이미지에서 읽은 단어와 뜻이 올바르게 짝지어졌는지 확인해 주세요.',
    unruled_layout_review:'표의 구분선이 없어 단어와 뜻이 올바르게 짝지어졌는지 확인이 필요합니다.',
    empty_word:'영단어가 비어 있습니다. 원본을 확인해 입력해 주세요.',
    word_not_english:'영단어를 올바르게 읽었는지 원본과 비교해 주세요.',
    empty_meaning:'뜻이 비어 있습니다. 원본을 확인해 입력해 주세요.',
    meaning_not_korean:'한국어 뜻을 올바르게 읽었는지 원본과 비교해 주세요.',
    broken_characters:'깨진 글자가 있습니다. 원본과 비교해 수정해 주세요.',
    word_contains_korean:'영단어에 한글이 섞여 있습니다. 단어와 뜻이 분리되었는지 확인해 주세요.',
    missing_source_number:'원본의 단어 번호를 읽지 못했습니다. 원본 번호를 확인해 주세요.',
    low_ocr_confidence:'이미지의 글자가 불분명합니다. 단어와 뜻을 원본과 비교해 주세요.',
    duplicate_source_number:'같은 단원에 단어 번호가 중복되어 있습니다. 원본 번호와 단원 범위를 확인해 주세요.',
    number_sequence_gap:'단어 번호가 이어지지 않습니다. 시작 번호나 빠진 항목을 확인해 주세요.',
    ai_source_review:'자동으로 읽은 단어와 뜻을 원본과 비교해 주세요.',
    source_reference_review:'원본의 단어·모든 뜻·항목 수가 일치하는지 확인해 주세요.',
    source_reference_unresolved:'원본에서 읽지 못한 부분이 있습니다. 해당 부분을 확인해 주세요.',
    no_vocabulary_rows:'단어와 뜻을 구분하지 못했습니다. 원본 페이지를 확인해 주세요.',
    unparsed_vocabulary_text:'표 밖에도 단어와 뜻으로 보이는 내용이 있습니다. 빠진 항목이 없는지 확인해 주세요.',
    page_extraction_failed:'페이지를 읽지 못했습니다. 원본 페이지를 확인해 주세요.',
    empty_pdf:'PDF에 페이지가 없습니다. 원본 파일을 확인해 주세요.',
    manual_input_changed:'단어 또는 뜻이 수정되었습니다. 학습자료를 다시 확인해 주세요.'
  };
  function reviewText(value){
    const rows=Array.isArray(value)?value:value?[value]:[];
    return [...new Set(rows.map(issue=>{
      const code=typeof issue==='string'?issue:issue?.code;
      const message=typeof issue==='string'?issue:issue?.message;
      return (Object.prototype.hasOwnProperty.call(reviewLabels,code)?reviewLabels[code]:null)||safeMessage(message,'원본과 비교해 단어·뜻·단원 정보를 확인해 주세요.');
    }))].join(' · ');
  }
  root.ExamContracts={error,integer,questions,result,driveURL,typeInfo,safeMessage,wordContext,reviewText};
  if(typeof module!=='undefined')module.exports=root.ExamContracts;
})(typeof window!=='undefined'?window:globalThis);

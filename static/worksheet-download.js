/* Read-only PDF exports shared by worksheet lists and previews. */
(() => {
  'use strict';
  let active = null, directory = null;
  const kinds = new Set(['standard', 'word-meaning', 'meaning-writing']);
  const labels = {student: '문제지', answer: '정답지', combined: '전체'};
  const key = item => item.kind === 'standard' ? `standard:${item.day}:${item.version}` : `${item.kind}:${item.id}`;
  const icon = name => window.MelodyIcons.svg(name, {size: 20});
  function driveURL(item) {
    const drive = item?.drive || item?.drive_links || {}, links = drive.links || {};
    return [item?.web_link, links.student, drive.student_pdf_url, links.sheet, links.bank, drive.sheet_url, drive.bank_sheet_url, item?.sheet_url, item?.drive_folder_url, drive.folder_url].map(window.ExamContracts.driveURL).find(Boolean) || null;
  }
  async function responseError(response) {
    let data;try {data = await response.json();} catch {}
    const detail = data?.detail;
    return Error(typeof detail === 'string' ? detail : detail?.message || data?.message || '파일을 다운로드하지 못했습니다. 다시 시도해 주세요.');
  }
  async function openDrive(item) {
    if (item.deletion_pending) throw Error('삭제 미완료 시험지는 열 수 없습니다.');
    const known = driveURL(item);
    if (known) {window.open(known, '_blank', 'noopener,noreferrer');return;}
    // Reserve the window while the user gesture is active; only a validated Google URL is assigned.
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    try {
      const path = item.kind === 'standard' ? `/api/worksheet/${item.day}/${item.version}` : `/api/word-meaning/worksheets/${encodeURIComponent(item.id)}`;
      const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(90000)});
      if (!response.ok) throw await responseError(response);
      const url = driveURL(await response.json());
      if (!url) throw Error('이 시험지의 구글 드라이브 링크를 확인할 수 없습니다.');
      if (tab) tab.location.replace(url);else throw Error('새 창을 허용한 뒤 다시 열어 주세요.');
    } catch (error) {tab?.close();throw error;}
  }
  function fileName(response, fallback) {
    const disposition = response.headers.get('Content-Disposition') || '';
    let name;
    try {name = decodeURIComponent(disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1] || '');} catch {}
    if (!name) name = disposition.match(/filename="([^"]+)"/i)?.[1];
    return (name || fallback).replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/[. ]+$/, '').slice(0, 180) || fallback;
  }
  function target(item) {
    if (!kinds.has(item.kind)) throw Error('시험지 유형을 확인하지 못했습니다.');
    return item.kind === 'standard' ? {kind: item.kind, day: item.day, version: item.version} : {kind: item.kind, id: item.id};
  }
  function open({items, pdfKind = 'combined', trigger} = {}) {
    if (active || !Array.isArray(items) || !items.length) return Promise.resolve(false);
    const selected = [...new Map(items.map(item => [key(item), {...item}])).values()];
    selected.forEach(target);
    const dialog = document.createElement('dialog');
    dialog.className = 'me-dialog worksheet-download-dialog';
    dialog.setAttribute('aria-labelledby', 'worksheet-download-heading');
    dialog.setAttribute('aria-describedby', 'worksheet-download-target');
    dialog.innerHTML = `<div class="worksheet-download-head"><h2 id="worksheet-download-heading">시험지 다운로드</h2><button type="button" class="me-icon-button" data-variant="ghost" data-size="sm" aria-label="닫기" title="닫기" data-download-close>${icon('close')}</button></div><p id="worksheet-download-target" class="worksheet-download-target"></p><div class="me-field"><label for="worksheet-download-folder">다운로드 경로</label><div class="worksheet-download-folder"><input id="worksheet-download-folder" class="me-input" type="text" readonly placeholder="저장 폴더를 선택하세요"><button type="button" class="me-icon-button" data-variant="secondary" aria-label="저장 폴더 선택" title="저장 폴더 선택" data-download-folder>${icon('folder')}</button></div><p class="me-help" data-download-fallback hidden>브라우저의 기본 다운로드 위치에 저장됩니다.</p></div><label class="me-check worksheet-download-archive"><input type="checkbox" data-download-archive>압축하여 다운로드</label><p class="me-help worksheet-download-progress" role="status" aria-live="polite" hidden></p><p class="me-help worksheet-download-error" data-tone="danger" role="alert" hidden></p><div class="me-dialog-actions"></div>`;
    dialog.querySelector('#worksheet-download-target').textContent = selected.length === 1 ? selected[0].title || '시험지' : `${selected.length}개의 시험지를 다운로드 합니다.`;
    const field = dialog.querySelector('#worksheet-download-folder'), picker = dialog.querySelector('[data-download-folder]'), zip = dialog.querySelector('[data-download-archive]'), progress = dialog.querySelector('.worksheet-download-progress'), error = dialog.querySelector('.worksheet-download-error'), actions = dialog.querySelector('.me-dialog-actions');
    const supportsDirectory = typeof window.showDirectoryPicker === 'function';
    if (!supportsDirectory) {picker.disabled = true;field.placeholder = '브라우저 기본 다운로드 위치';dialog.querySelector('[data-download-fallback]').hidden = false;}
    if (directory) field.value = directory.name;
    let busy = false, picking = false, finished = false, timer = null, controller = null, writable = null, message = '', began = 0;
    const completed = new Set(), usedNames = new Set();
    const buttons = [];
    let resolve;
    const promise = new Promise(done => {resolve = done;});
    active = dialog;
    function tick() {progress.textContent = message + (busy ? ` · ${Math.floor((Date.now() - began) / 1000)}초 경과` : '');}
    function finish(result) {
      if (finished) return;
      finished = true;controller?.abort();clearInterval(timer);
      if (writable) writable.abort().catch(() => {});
      dialog.close();dialog.remove();active = null;resolve(result);
    }
    function sync() {buttons.forEach(button => {button.disabled = busy || picking;});picker.disabled = !supportsDirectory || busy || picking;zip.disabled = busy || picking;dialog.setAttribute('aria-busy', String(busy));}
    picker.addEventListener('click', async () => {
      if (busy || picking) return;
      picking = true;sync();error.hidden = true;
      try {
        const chosen = await window.showDirectoryPicker({mode: 'readwrite'});
        if (finished) return;
        directory = chosen;field.value = chosen.name;dialog.querySelector('[data-download-fallback]').hidden = true;
      } catch (reason) {
        if (finished || reason.name === 'AbortError') return;
        if (['SecurityError', 'NotSupportedError'].includes(reason.name)) {
          directory = null;field.value = '';field.placeholder = '브라우저 기본 다운로드 위치';dialog.querySelector('[data-download-fallback]').hidden = false;
        } else {error.textContent = '폴더를 선택하지 못했습니다. 폴더 접근 권한을 확인해 주세요.';error.hidden = false;}
      } finally {picking = false;if (!finished) sync();}
    });
    async function save(blob, proposed, signal) {
      signal.throwIfAborted();
      const dot = proposed.lastIndexOf('.'), stem = dot > 0 ? proposed.slice(0, dot) : proposed, extension = dot > 0 ? proposed.slice(dot) : '';
      let name = proposed, suffix = 1, handle;
      while (true) {
        signal.throwIfAborted();
        if (usedNames.has(name)) {name = `${stem} (${suffix++})${extension}`;continue;}
        if (directory) {
          try {await directory.getFileHandle(name);name = `${stem} (${suffix++})${extension}`;continue;} catch (reason) {if (reason.name !== 'NotFoundError') throw reason;}
          signal.throwIfAborted();handle = await directory.getFileHandle(name, {create: true});
        }
        break;
      }
      if (directory) {
        try {
          signal.throwIfAborted();writable = await handle.createWritable();
          signal.throwIfAborted();await writable.write(blob);signal.throwIfAborted();await writable.close();writable = null;
        } catch (reason) {if (writable) {try {await writable.abort();} catch {}writable = null;}throw reason;}
      } else {
        signal.throwIfAborted();const url = URL.createObjectURL(blob), link = document.createElement('a');link.href = url;link.download = name;document.body.appendChild(link);link.click();link.remove();setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
      usedNames.add(name);
    }
    async function run(kind) {
      if (busy || picking || finished) return;
      const pending = selected.filter(item => !completed.has(`${kind}:${key(item)}`));
      if (!pending.length) {message = `${labels[kind]} ${selected.length}개 ${directory ? '저장 완료' : '다운로드 요청 완료'}`;progress.hidden = false;tick();return;}
      busy = true;sync();error.hidden = true;progress.hidden = false;began = Date.now();controller = new AbortController();const signal = controller.signal;
      message = `0 / ${pending.length}개 준비 중`;tick();timer = setInterval(tick, 1000);
      let done = 0;const failed = [];
      try {
        // Request permission during the CTA gesture, before the network request.
        if (directory?.requestPermission && await directory.requestPermission({mode: 'readwrite'}) !== 'granted') throw Error('선택한 폴더의 저장 권한을 허용해 주세요.');
        const archive = zip.checked && pending.length >= 2;
        const batches = archive ? [pending] : pending.map(item => [item]);
        for (const batch of batches) {
          signal.throwIfAborted();message = `${done} / ${pending.length}개 준비 중`;tick();
          try {
            const response = await fetch('/api/worksheet-downloads', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({items: batch.map(target), pdf_kind: kind, archive}), cache: 'no-store', signal: AbortSignal.any([signal, AbortSignal.timeout(180000)])});
            if (!response.ok) throw await responseError(response);
            const blob = await response.blob();signal.throwIfAborted();
            const magic = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
            if (archive ? magic[0] !== 80 || magic[1] !== 75 : String.fromCharCode(...magic) !== '%PDF-') throw Error('다운로드 파일의 형식을 확인하지 못했습니다.');
            const fallback = archive ? '시험지.zip' : `${batch[0].title || '시험지'} ${labels[kind]}.pdf`;
            await save(blob, fileName(response, fallback), signal);
            batch.forEach(item => completed.add(`${kind}:${key(item)}`));done += batch.length;
          } catch (reason) {if (signal.aborted) throw reason;failed.push({title: batch.length === 1 ? batch[0].title : '압축 파일', message: reason.message || '저장하지 못했습니다.'});}
        }
        const total = selected.filter(item => completed.has(`${kind}:${key(item)}`)).length;
        message = `${total} / ${selected.length}개 ${directory ? '저장 완료' : '다운로드 요청 완료'}`;
        if (failed.length) {error.textContent = `${failed.length}건 실패 · ${failed[0].title}: ${failed[0].message} 같은 버튼으로 실패한 파일만 다시 시도할 수 있습니다.`;error.hidden = false;}
      } catch (reason) {if (!finished) {error.textContent = reason.message || '저장하지 못했습니다. 다시 시도해 주세요.';error.hidden = false;message = '다운로드 확인 필요';}}
      finally {clearInterval(timer);timer = null;busy = false;if (!finished) {sync();tick();}}
    }
    Object.entries(labels).forEach(([kind, label]) => {
      const button = document.createElement('button');button.type = 'button';button.className = 'me-button';button.dataset.variant = kind === 'combined' ? 'primary' : 'secondary';button.dataset.downloadKind = kind;button.textContent = label;button.addEventListener('click', () => run(kind));actions.appendChild(button);buttons.push(button);
    });
    dialog.querySelector('[data-download-close]').addEventListener('click', () => finish(false));
    dialog.addEventListener('cancel', event => {event.preventDefault();finish(false);});
    document.body.appendChild(dialog);
    if (window.MelodyUI) window.MelodyUI.openDialog(dialog, trigger);else dialog.showModal();
    buttons.find(button => button.dataset.downloadKind === pdfKind)?.focus();
    return promise;
  }
  window.MelodyWorksheetDownload = Object.freeze({open, openDrive, driveURL});
})();

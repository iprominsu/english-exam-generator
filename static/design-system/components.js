/* Melody English internal UI helpers. No network, data, or theme side effects. */
(() => {
  'use strict';
  const busyButtons = new WeakMap();
  const dialogTriggers = new WeakMap();
  const initialized = new WeakSet();
  const wiredDialogs = new WeakSet();
  const handledClicks = new WeakSet();
  const handledKeys = new WeakSet();
  let liveRegion;
  let toastStack;
  let announceTimer;
  const activeToasts = new Map();
  let pendingUnsaved;

  function element(value) {
    return typeof value === 'string' ? document.getElementById(value) : value;
  }

  function announce(message) {
    if (!document.body) return;
    if (!liveRegion || !liveRegion.isConnected) {
      liveRegion = document.createElement('div');
      liveRegion.className = 'me-visually-hidden';
      liveRegion.setAttribute('role', 'status');
      liveRegion.setAttribute('aria-live', 'polite');
      liveRegion.setAttribute('aria-atomic', 'true');
      document.body.append(liveRegion);
    }
    clearTimeout(announceTimer);
    liveRegion.textContent = '';
    announceTimer = setTimeout(() => { liveRegion.textContent = String(message); }, 30);
  }

  function setBusy(target, busy, label) {
    const button = element(target);
    if (!button || !('disabled' in button)) return;
    const saved = busyButtons.get(button);
    if (busy) {
      if (saved) return;
      const state = {
        nodes: Array.from(button.childNodes),
        disabled: button.disabled,
        busy: button.getAttribute('aria-busy'),
        label: button.getAttribute('aria-label'),
        minWidth: button.style.minWidth,
        width: button.style.width
      };
      busyButtons.set(button, state);
      const width = button.getBoundingClientRect().width;
      if (width > 0) {
        button.style.minWidth = `${width}px`;
        button.style.width = `${width}px`;
      }
      const spinner = document.createElement('span');
      spinner.className = 'me-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      const busyLabel = label || button.textContent.trim() || button.getAttribute('aria-label') || '처리 중';
      button.replaceChildren(spinner);
      if (!button.classList.contains('me-icon-button')) {
        const text = document.createElement('span');
        text.textContent = busyLabel;
        text.style.overflow = 'hidden';
        text.style.textOverflow = 'ellipsis';
        button.append(text);
      }
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.setAttribute('aria-label', busyLabel);
      announce(busyLabel);
      return;
    }
    if (!saved) return;
    button.replaceChildren(...saved.nodes);
    button.disabled = saved.disabled;
    button.style.minWidth = saved.minWidth;
    button.style.width = saved.width;
    if (saved.busy === null) button.removeAttribute('aria-busy');
    else button.setAttribute('aria-busy', saved.busy);
    if (saved.label === null) button.removeAttribute('aria-label');
    else button.setAttribute('aria-label', saved.label);
    busyButtons.delete(button);
  }

  function wireDialog(dialog) {
    if (wiredDialogs.has(dialog)) return;
    wiredDialogs.add(dialog);
    dialog.addEventListener('close', () => {
      const trigger = dialogTriggers.get(dialog);
      dialogTriggers.delete(dialog);
      if (trigger && trigger.isConnected && !trigger.disabled) trigger.focus({ preventScroll: true });
    });
  }

  function openDialog(target, trigger) {
    const dialog = element(target);
    if (!dialog || typeof dialog.showModal !== 'function' || dialog.open) return;
    wireDialog(dialog);
    const origin = element(trigger) || document.activeElement;
    if (origin && typeof origin.focus === 'function') dialogTriggers.set(dialog, origin);
    dialog.showModal();
  }

  function closeDialog(target, value = '') {
    const dialog = element(target);
    if (dialog && dialog.open && typeof dialog.close === 'function') dialog.close(String(value));
  }

  function toast(message, options = {}) {
    if (!document.body) return null;
    const key = options.key || String(message);
    activeToasts.get(key)?.dismiss();
    if (!toastStack || !toastStack.isConnected) {
      toastStack = document.createElement('div');
      toastStack.className = 'me-toast-stack';
      toastStack.setAttribute('aria-label', '알림');
      document.body.append(toastStack);
    }
    const item = document.createElement('div');
    item.className = 'me-toast';
    item.dataset.tone = ['success', 'warning', 'danger'].includes(options.tone) ? options.tone : 'neutral';
    const text = document.createElement('span');
    text.className = 'me-toast-message';
    text.textContent = String(message);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'me-icon-button';
    close.dataset.variant = 'ghost';
    close.dataset.size = 'sm';
    close.setAttribute('aria-label', '알림 닫기');
    if (window.MelodyIcons) close.innerHTML = window.MelodyIcons.svg('close', { size: 16 });
    else close.textContent = '×';
    item.append(text, close);
    toastStack.append(item);
    announce(message);
    let timer, hovered = false, focused = false;
    const duration = Number.isFinite(options.duration) ? Math.max(0, options.duration) : 5000;
    const remove = () => { clearTimeout(timer); item.remove(); if (activeToasts.get(key)?.element === item) activeToasts.delete(key); };
    const start = () => { clearTimeout(timer); if (duration > 0 && !hovered && !focused) timer = setTimeout(remove, duration); };
    close.addEventListener('click', remove);
    item.addEventListener('mouseenter', () => { hovered = true; clearTimeout(timer); });
    item.addEventListener('mouseleave', () => { hovered = false; start(); });
    item.addEventListener('focusin', () => { focused = true; clearTimeout(timer); });
    item.addEventListener('focusout', event => { if (!item.contains(event.relatedTarget)) { focused = false; start(); } });
    const handle = { element: item, dismiss: remove };
    activeToasts.set(key, handle);
    while (activeToasts.size > 3) activeToasts.values().next().value.dismiss();
    start();
    return handle;
  }

  function confirmUnsaved(options = {}) {
    if (pendingUnsaved) return pendingUnsaved;
    const dialog = document.createElement('dialog');
    dialog.className = 'me-dialog';
    dialog.setAttribute('aria-labelledby', 'melody-unsaved-heading');
    const heading = document.createElement('h2');
    heading.id = 'melody-unsaved-heading';heading.textContent = '저장하지 않은 변경';
    const description = document.createElement('p');
    description.textContent = '수정한 내용을 저장한 뒤 이동할 수 있습니다.';
    const actions = document.createElement('div');actions.className = 'me-dialog-actions';
    for (const [value, label, variant] of [['keep','계속 수정','secondary'],['discard','저장하지 않고 이동','danger'],['save','저장 후 이동','primary']]) {
      const button = document.createElement('button');button.type = 'button';button.className = 'me-button';button.dataset.variant = variant;
      button.textContent = label;button.addEventListener('click', () => dialog.close(value));actions.append(button);
    }
    dialog.append(heading, description, actions);document.body.append(dialog);
    pendingUnsaved = new Promise(resolve => {
      dialog.addEventListener('cancel', event => { event.preventDefault();dialog.close('keep'); });
      dialog.addEventListener('close', () => { const value = dialog.returnValue || 'keep';dialog.remove();pendingUnsaved = null;resolve(value); }, { once: true });
      openDialog(dialog, options.trigger);
    });
    return pendingUnsaved;
  }

  function init(root = document) {
    if (!root || initialized.has(root)) return;
    initialized.add(root);
    root.addEventListener('click', event => {
      if (handledClicks.has(event)) return;
      handledClicks.add(event);
      const target = event.target instanceof Element ? event.target : null;
      const disabled = target?.closest('[aria-disabled="true"]');
      if (disabled?.matches('.me-button, .me-icon-button')) { event.preventDefault(); return; }
      const opener = target?.closest('[data-me-dialog-open]');
      if (opener && !opener.disabled) openDialog(opener.dataset.meDialogOpen, opener);
      const closer = target?.closest('[data-me-dialog-close]');
      if (closer && !closer.disabled) closeDialog(closer.closest('dialog'), closer.dataset.meDialogClose);
      const notification = target?.closest('[data-me-toast]');
      if (notification && !notification.disabled) toast(notification.dataset.meToast, { tone: notification.dataset.tone });
      const choice = target?.closest('.me-segmented[data-me-selectable] button');
      if (choice && !choice.disabled) {
        const group = choice.closest('.me-segmented');
        group.querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button === choice)));
        group.dispatchEvent(new CustomEvent('me:change', { bubbles: true, detail: { value: choice.dataset.value || choice.textContent.trim(), button: choice } }));
      }
    });
    root.addEventListener('keydown', event => {
      if (handledKeys.has(event)) return;
      handledKeys.add(event);
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const target = event.target instanceof Element ? event.target : null;
      const group = target?.closest('.me-segmented[data-me-selectable]');
      if (!group || target.tagName !== 'BUTTON') return;
      const buttons = Array.from(group.querySelectorAll('button:not(:disabled)'));
      const index = buttons.indexOf(target);
      if (index < 0 || !buttons.length) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
      buttons[next].click();
    });
  }

  window.MelodyUI = Object.freeze({ setBusy, openDialog, closeDialog, toast, confirmUnsaved, announce, init });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => init(), { once: true });
  else init();
})();

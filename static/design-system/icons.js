/* Internal icons: fixed local sprite, decorative SVG, no observer or network API.
 * Unknown names and sizes outside 16/20/24 throw RangeError.
 * Accessible names belong to the enclosing button/link, not to the icon.
 */
(() => {
  'use strict';
  const names = Object.freeze([
    'overview', 'vocabulary', 'worksheet-create', 'worksheet-manage',
    'search', 'upload', 'download', 'preview', 'edit', 'trash', 'save', 'refresh',
    'chevron-down', 'chevron-right', 'chevron-left', 'arrow-left', 'arrow-right',
    'external-link', 'check', 'warning', 'close', 'clock', 'folder', 'google-drive',
    'sort-asc', 'sort-desc', 'sparkles', 'user', 'appearance', 'message', 'file'
  ]);
  const registry = new Set(names);
  const sizes = new Set([16, 20, 24]);
  const namespace = 'http://www.w3.org/2000/svg';
  const sprite = '/static/design-system/icons.svg#me-icon-';
  const escapeAttribute = value => String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);

  function configuration(name, { size = 20, className = '' } = {}) {
    if (!registry.has(name)) throw new RangeError(`Unknown Melody icon: ${String(name)}`);
    if (!sizes.has(size)) throw new RangeError('Melody icon size must be 16, 20, or 24.');
    return {
      name, size,
      className: 'me-icon' + (className ? ' ' + String(className).trim() : '')
    };
  }

  function svg(name, options) {
    const icon = configuration(name, options);
    return `<svg xmlns="${namespace}" class="${escapeAttribute(icon.className)}" data-size="${icon.size}" width="${icon.size}" height="${icon.size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="${sprite}${icon.name}"></use></svg>`;
  }

  function create(name, options) {
    const icon = configuration(name, options);
    const result = document.createElementNS(namespace, 'svg');
    const attributes = {
      class: icon.className, 'data-size': icon.size,
      width: icon.size, height: icon.size, viewBox: '0 0 24 24',
      fill: 'none', stroke: 'currentColor', 'stroke-width': '1.75',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      'aria-hidden': 'true', focusable: 'false'
    };
    for (const [attribute, value] of Object.entries(attributes)) result.setAttribute(attribute, String(value));
    const use = document.createElementNS(namespace, 'use');
    use.setAttribute('href', sprite + icon.name);
    result.append(use);
    return result;
  }

  window.MelodyIcons = Object.freeze({ names, svg, create });
})();

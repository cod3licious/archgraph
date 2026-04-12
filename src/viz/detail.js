// Very simple inline markdown: handles `code`, **bold**, paragraph breaks
export function renderMarkdown(text) {
  // Escape HTML
  let s = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // `code`
  s = s.replace(/`([^`\n]*)`/g, '<code>$1</code>');

  // **bold**
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // Split into paragraphs on blank lines
  const paras = s.split(/\n\n+/).map(p => p.trim()).filter(Boolean);
  return paras.map(p => `<p>${p.replace(/\n/g, ' ')}</p>`).join('');
}

export function setDetail(html) {
  const placeholder = document.getElementById('detail-placeholder');
  const content     = document.getElementById('detail-content');
  placeholder.style.display = 'none';
  content.style.display = '';
  content.innerHTML = html;
}

export function clearDetail() {
  document.getElementById('detail-placeholder').style.display = '';
  document.getElementById('detail-content').style.display = 'none';
}

export function showSubmoduleDetail(sm, smData, units) {
  const unitNames = smData?.units || [];
  const parts = [`<div class="detail-title">${sm}</div>`];
  for (const name of unitNames) {
    const u = units[`${sm}.${name}`];
    if (u) parts.push(`<h3>${name}</h3>` + renderMarkdown(u.description || ''));
  }
  setDetail(parts.join(''));
}

export function showUnitDetail(unitPath, unitData) {
  const name = unitData.name || unitPath.split('.').pop();
  setDetail(
    `<div class="detail-title">${unitData.submodule}</div>` +
    `<h3>${name}</h3>` + renderMarkdown(unitData.description || '')
  );
}

export function svgEl(tag) {
  return document.createElementNS('http://www.w3.org/2000/svg', tag);
}

export function loadCss(href) {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  document.head.appendChild(link);
}

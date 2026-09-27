// Shared display-name helpers for the graph views.
//
// A shared enclosing package (e.g. `verimo` in `verimo.core`, `verimo.backend`)
// is just the project container, not an architectural layer, so it is stripped
// from the displayed module/submodule names. Full ids are kept for tooltips,
// data attributes, and lookups.

// Number of leading dotted segments shared by all module ids, capped so a module
// sitting at the shared level still keeps at least one segment of its own.
export function commonPrefixLen(moduleIds) {
  const ids = moduleIds.map(m => m.split('.'));
  if (!ids.length) return 0;
  const minLen = Math.min(...ids.map(s => s.length));
  let common = 0;
  for (let i = 0; i < minLen; i++) {
    if (ids.every(s => s[i] === ids[0][i])) common++; else break;
  }
  return Math.min(common, minLen - 1);
}

export function stripPrefix(id, prefixLen) {
  return id.split('.').slice(prefixLen).join('.');
}

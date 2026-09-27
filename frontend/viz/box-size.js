// Pure (DOM-free) sizing of the submodule boxes in the box graph. Text widths come
// from an injected measure(text) function, so this can be tested without a browser.

export const BOX_BORDER = 2;   // must match .submodule-box border width in box-graph.css
export const TITLE_H = 18;
export const UNIT_ROW_H = 18;
export const UNITS_PAD_Y = 10; // top + bottom padding of the units area
export const COL_GAP = 12;
const SIDE_PAD = 28;           // borders + horizontal padding + some slack
const BOX_MIN_W = 130;
const MAX_COL_ROWS = 10;       // add columns before a column grows longer than this...
const MAX_AUTO_COLS = 4;       // ...up to this many (more only if the box is wider anyway)

// Units flow top to bottom, then into the next column (keeping their file order readable)
function columnsWidth(nameWidths, cols) {
  const rows = Math.ceil(nameWidths.length / cols);
  const colWidths = Array.from({ length: cols }, (_, c) => Math.max(0, ...nameWidths.slice(c * rows, (c + 1) * rows)));
  return colWidths.reduce((s, w) => s + w, 0) + (cols - 1) * COL_GAP + SIDE_PAD;
}

// Size of a box with the given title and unit names. minW / minH are lower bounds (e.g. to
// fit the port symbols); extra width is used for more columns, which makes the box shorter.
export function boxSize(title, unitNames, measureUnit, measureTitle, minW = 0, minH = 0) {
  const n = unitNames.length;
  const nameWidths = unitNames.map(measureUnit);
  let cols = Math.min(MAX_AUTO_COLS, Math.max(1, Math.ceil(n / MAX_COL_ROWS)));
  const w = Math.max(BOX_MIN_W, measureTitle(title) + SIDE_PAD, columnsWidth(nameWidths, cols), minW);
  while (cols < n && columnsWidth(nameWidths, cols + 1) <= w) cols++;
  const rows = Math.ceil(n / cols);
  if (rows) cols = Math.ceil(n / rows); // fewest columns with the same number of rows
  const h = Math.max(minH, 2 * BOX_BORDER + TITLE_H + UNITS_PAD_Y + rows * UNIT_ROW_H);
  return { w, h, cols, rows };
}

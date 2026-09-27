import { describe, expect, test } from 'bun:test';
import { boxSize, BOX_BORDER, TITLE_H, UNIT_ROW_H, UNITS_PAD_Y } from './box-size.js';

const measure = text => text.length * 7;
const names = n => Array.from({ length: n }, (_, i) => `unit_${String(i).padStart(2, '0')}`);
const heightFor = rows => 2 * BOX_BORDER + TITLE_H + UNITS_PAD_Y + rows * UNIT_ROW_H;

describe('boxSize', () => {
  test('small boxes use a single column', () => {
    const sz = boxSize('core.db', names(10), measure, measure);
    expect(sz).toMatchObject({ cols: 1, rows: 10, h: heightFor(10) });
  });

  test('adds columns before a column exceeds 10 rows, up to 4', () => {
    expect(boxSize('m', names(11), measure, measure)).toMatchObject({ cols: 2, rows: 6 });
    expect(boxSize('m', names(40), measure, measure)).toMatchObject({ cols: 4, rows: 10 });
    expect(boxSize('m', names(60), measure, measure)).toMatchObject({ cols: 4, rows: 15 });
  });

  test('extra width from port symbols is filled with columns, making the box shorter', () => {
    const narrow = boxSize('m', names(60), measure, measure);
    const wide = boxSize('m', names(60), measure, measure, 900);
    expect(wide.w).toBe(900);
    expect(wide.cols).toBeGreaterThan(narrow.cols);
    expect(wide.h).toBeLessThan(narrow.h);
    expect(wide.h).toBe(heightFor(wide.rows));
    expect(wide.cols).toBe(Math.ceil(60 / wide.rows)); // no needless trailing column
  });

  test('a long title widens the box and its units spread into columns', () => {
    const sz = boxSize('x'.repeat(60), names(4), measure, measure);
    expect(sz.w).toBe(60 * 7 + 28);
    expect(sz).toMatchObject({ cols: 4, rows: 1 });
  });

  test('minimum height for side symbols is kept', () => {
    expect(boxSize('m', names(2), measure, measure, 0, 500).h).toBe(500);
  });

  test('empty submodule', () => {
    expect(boxSize('m', [], measure, measure)).toMatchObject({ w: 130, h: heightFor(0), rows: 0 });
  });
});

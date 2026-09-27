import { describe, expect, test } from 'bun:test';
import { submoduleRows, flattenLayers, dependencyRoles } from './graph-model.js';

const layers = {
  root_layers: [['api'], [], ['core', 'util']],
  submodule_layers: {
    api: [['api.orders', 'api.users'], [], ['api.auth']],
    core: [['core.service'], ['core'], ['core.db']],
  },
};

describe('submoduleRows', () => {
  test('returns the submodule rows without empty rows', () => {
    expect(submoduleRows(layers, 'api')).toEqual([['api.orders', 'api.users'], ['api.auth']]);
  });

  test('treats a module without submodule_layers as its own submodule', () => {
    expect(submoduleRows(layers, 'util')).toEqual([['util']]);
    expect(submoduleRows({ root_layers: [['util']] }, 'util')).toEqual([['util']]);
  });

  test('keeps a module listed as its own submodule', () => {
    expect(submoduleRows(layers, 'core')).toEqual([['core.service'], ['core'], ['core.db']]);
  });

  test('returns no rows for a module with empty submodule_layers', () => {
    expect(submoduleRows({ root_layers: [['x']], submodule_layers: { x: [] } }, 'x')).toEqual([]);
  });
});

describe('flattenLayers', () => {
  test('lists all submodules in layer order, skipping empty rows', () => {
    expect(flattenLayers(layers)).toEqual([
      'api.orders', 'api.users', 'api.auth', 'core.service', 'core', 'core.db', 'util',
    ]);
  });

  test('handles empty layers', () => {
    expect(flattenLayers({ root_layers: [] })).toEqual([]);
  });
});

describe('dependencyRoles', () => {
  const units = {
    'a.x': { dependencies: { 'b.y': true, 'a.z': true } },
    'a.z': { dependencies: {} },
    'b.y': { dependencies: { 'c.w': false } },
    'c.w': { dependencies: { 'a.x': false } },
    'd.v': { dependencies: { 'a.z': true, 'b.y': true } },
  };

  test('single unit: direct callees and callers', () => {
    const { callees, callers } = dependencyRoles(units, ['b.y']);
    expect([...callees]).toEqual(['c.w']);
    expect([...callers].sort()).toEqual(['a.x', 'd.v']);
  });

  test('multiple units: dependencies within the selection are excluded', () => {
    const { callees, callers } = dependencyRoles(units, ['a.x', 'a.z']);
    expect([...callees]).toEqual(['b.y']);
    expect([...callers].sort()).toEqual(['c.w', 'd.v']);
  });

  test('empty selection and unknown units yield no roles', () => {
    expect(dependencyRoles(units, [])).toEqual({ callees: new Set(), callers: new Set() });
    expect(dependencyRoles(units, ['nope.nope'])).toEqual({ callees: new Set(), callers: new Set() });
  });
});

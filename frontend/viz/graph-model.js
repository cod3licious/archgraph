// Pure (DOM-free) data helpers shared by the graph views.

// Submodule rows of a root module. A module without submodule_layers is its own
// single submodule. Empty rows are dropped so they take up no layout space.
export function submoduleRows(layers, mod) {
  return (layers.submodule_layers?.[mod] ?? [[mod]]).filter(row => row.length);
}

// All submodules in layer order (top to bottom, left to right).
export function flattenLayers(layers) {
  return layers.root_layers.flat().flatMap(mod => submoduleRows(layers, mod).flat());
}

// Units the selected units depend on (callees) and units depending on them
// (callers), both excluding the selection itself.
export function dependencyRoles(units, selectedUnits) {
  const selected = new Set(selectedUnits);
  const callees = new Set(
    [...selected].flatMap(u => Object.keys(units[u]?.dependencies || {})).filter(d => !selected.has(d))
  );
  const callers = new Set(
    Object.keys(units).filter(u =>
      !selected.has(u) && Object.keys(units[u].dependencies || {}).some(d => selected.has(d)))
  );
  return { callees, callers };
}

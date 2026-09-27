import { showModuleDetail, showSubmoduleDetail, showUnitDetail, clearDetail } from './detail.js';
import { commonPrefixLen, stripPrefix } from './labels.js';
import { submoduleRows, dependencyRoles } from './graph-model.js';
import { svgEl, loadCss } from './dom.js';

loadCss('viz/pearl-graph.css');

// ── Constants ────────────────────────────────────────────────────────────────
const ROW_H = 28;
const PEARL_R_MODULE = 10;
const PEARL_R_SUBMODULE = 7;
const PEARL_R_UNIT = 4;
const TREE_WIDTH = 250;
const TREE_PAD_TOP = 20;

// ── Placeholder HTML ─────────────────────────────────────────────────────────
export const placeholderHTML = `
  <div>This graph shows the layered architecture as a vertical hierarchy. Each row is a <strong>module</strong>, <strong>submodule</strong>, or <strong>unit</strong> (function/class). Modules and submodules can be expanded to reveal their contents.</div>

  <h4>Visual encoding</h4>
  <div class="legend">
    <svg width="14" height="14" viewBox="0 0 14 14"><circle cx="7" cy="7" r="6" fill="#bab7e0" stroke="#706da6" stroke-width="1"/></svg>
    <span>Pearl: sized by hierarchy level (module > submodule > unit)</span>
    <svg width="18" height="10" viewBox="0 0 18 10"><path d="M2,5 Q9,-3 16,5" fill="none" stroke="#2a7a2a" stroke-width="2"/></svg>
    <span>Green arc: valid dependency (points down, drawn on the left)</span>
    <svg width="18" height="10" viewBox="0 0 18 10"><path d="M2,5 Q9,-3 16,5" fill="none" stroke="#b03a2e" stroke-width="2"/></svg>
    <span>Red arc: layer violation, drawn on the right if it points up, on the left if it points down (e.g., between siblings in the same layer)</span>
  </div>

  <h4>Interactions</h4>
  <div>Click [+]/[-] to expand or collapse modules and submodules. Click any name or pearl to focus it. Unrelated nodes fade out, and only the arcs to connected nodes remain. The detail panel on the right shows the description.</div>
  <div style="margin-top:6px"><a href="#" id="pearl-collapse-all">Collapse all</a></div>
  <div style="margin-top:6px">Within the highlighted nodes, names are styled to show their role:</div>
  <div class="legend" style="margin-top:6px">
    <span></span><span><span class="pearl-label is-selected">name</span>: the selected node(s)</span>
    <span></span><span><span class="pearl-label is-callee">name</span>: a dependency (callee)</span>
    <span></span><span><span class="pearl-label is-caller">name</span>: a dependent (caller)</span>
  </div>`;

// ── Main render ──────────────────────────────────────────────────────────────
export function render(data) {
  const { layers, submodules, units, high_level_units_first } = data;
  const hierarchy = buildHierarchy(layers, submodules, high_level_units_first);

  // Default: modules expanded, submodules collapsed
  const expanded = new Set(hierarchy.filter(n => n.level === 'module' && n.children[0]?.level === 'submodule').map(n => n.id));

  const container = document.getElementById('graph-container');
  container.innerHTML = '';
  container.style.width = '';
  container.style.height = '';

  const wrapper = document.createElement('div');
  wrapper.className = 'pearl-graph';
  container.appendChild(wrapper);

  const treeDiv = document.createElement('div');
  treeDiv.className = 'pearl-tree';
  treeDiv.style.width = TREE_WIDTH + 'px';
  wrapper.appendChild(treeDiv);

  const canvasDiv = document.createElement('div');
  canvasDiv.className = 'pearl-canvas';
  wrapper.appendChild(canvasDiv);

  // Build node lookup and module color cache
  const nodeById = new Map(hierarchy.map(n => [n.id, n]));
  const moduleColors = {};
  for (const [sm, smData] of Object.entries(submodules))
    moduleColors[smData.module] = smData.color;

  const state = {
    hierarchy, nodeById, moduleColors, expanded, units,
    treeDiv, canvasDiv, selection: null,
  };

  renderAll(state);

  const collapseLink = document.getElementById('pearl-collapse-all');
  if (collapseLink) {
    collapseLink.addEventListener('click', e => {
      e.preventDefault();
      expanded.clear();
      state.selection = null;
      clearDetail();
      renderAll(state);
    });
  }
}

// ── Build hierarchy ──────────────────────────────────────────────────────────
// Node ids are namespaced by level, since a module, one of its submodules, and a
// unit may share the same path (e.g. a module listed as its own submodule).
const nodeId = (level, path) => `${level}:${path}`;

function buildHierarchy(layers, submodules, highLevelUnitsFirst) {
  // Pearl graph renders top-to-bottom (high-level at top), so unit lists
  // need to be in high-level-first order. Reverse when data is low-level-first.
  const graphUnits = (sm) => {
    const u = submodules[sm]?.units || [];
    return highLevelUnitsFirst ? u : [...u].reverse();
  };

  const nodes = [];
  const addNode = (level, path, module, parent) => {
    const node = { id: nodeId(level, path), level, path, module, parentId: parent?.id ?? null, children: [] };
    parent?.children.push(node);
    nodes.push(node);
    return node;
  };
  const addUnits = (parent, sm) => {
    for (const u of graphUnits(sm)) addNode('unit', `${sm}.${u}`, parent.module, parent);
  };

  for (const mod of layers.root_layers.flat()) {
    const moduleNode = addNode('module', mod, mod, null);
    const sms = submoduleRows(layers, mod).flat();
    // A module that is its only submodule gets its units attached directly
    if (sms.length === 1 && sms[0] === mod) addUnits(moduleNode, mod);
    else for (const sm of sms) addUnits(addNode('submodule', sm, mod, moduleNode), sm);
  }
  return nodes;
}

function ancestorIds(nodeById, node) {
  const ids = [];
  for (let cur = nodeById.get(node.parentId); cur; cur = nodeById.get(cur.parentId)) ids.push(cur.id);
  return ids;
}

// ── Visible rows ─────────────────────────────────────────────────────────────
function getVisibleRows(hierarchy, expanded) {
  const rows = [];
  for (const mod of hierarchy.filter(n => n.level === 'module')) {
    if (!expanded.has(mod.id)) {
      rows.push({ node: mod, showPearl: true });
      continue;
    }
    rows.push({ node: mod, showPearl: false });
    for (const sm of mod.children) {
      if (!expanded.has(sm.id)) {
        rows.push({ node: sm, showPearl: true });
        continue;
      }
      rows.push({ node: sm, showPearl: false });
      for (const unit of sm.children)
        rows.push({ node: unit, showPearl: true });
    }
  }
  return rows;
}

// ── Collect edges between visible pearls ─────────────────────────────────────
function collectEdges(rows, nodeById, units) {
  const pearlRows = [];
  const pearlVisualIdx = new Map();
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].showPearl) { pearlRows.push(rows[i]); pearlVisualIdx.set(rows[i].node.id, i); }
  }
  const pearlIds = new Set(pearlRows.map(r => r.node.id));

  // Map every node to the visible pearl that represents it
  const nodeToVisiblePearl = new Map();
  for (const [id, node] of nodeById) {
    const pearlId = [id, ...ancestorIds(nodeById, node)].find(a => pearlIds.has(a));
    if (pearlId) nodeToVisiblePearl.set(id, pearlId);
  }

  // Aggregate unit-level deps to visible pearls
  const edgeMap = new Map();
  for (const [unitId, unitData] of Object.entries(units)) {
    const fromPearl = nodeToVisiblePearl.get(nodeId('unit', unitId));
    if (!fromPearl) continue;
    for (const [depId, valid] of Object.entries(unitData.dependencies || {})) {
      const toPearl = nodeToVisiblePearl.get(nodeId('unit', depId));
      if (!toPearl || fromPearl === toPearl) continue;
      const key = `${fromPearl}->${toPearl}`;
      const existing = edgeMap.get(key);
      if (existing) { if (!valid) existing.valid = false; }
      else edgeMap.set(key, { from: fromPearl, to: toPearl, valid });
    }
  }

  return { edges: [...edgeMap.values()], pearlRows, pearlVisualIdx };
}

// ── Display labels ───────────────────────────────────────────────────────────
// Modules drop the shared enclosing package (see labels.js); submodules are shown
// relative to their module (a module listed as its own submodule keeps the module
// label); units show only their own name. Full paths stay in tooltips.
function nodeLabel(node, modPrefixLen) {
  if (node.level === 'unit') return node.path.split('.').pop();
  if (node.level === 'submodule' && node.path !== node.module) return node.path.slice(node.module.length + 1);
  return stripPrefix(node.path, modPrefixLen);
}

// ── Full re-render ───────────────────────────────────────────────────────────
function renderAll(state) {
  const { hierarchy, nodeById, moduleColors, expanded, units, treeDiv, canvasDiv } = state;
  const rows = getVisibleRows(hierarchy, expanded);
  const modPrefixLen = commonPrefixLen(hierarchy.filter(n => n.level === 'module').map(n => n.path));
  const { edges, pearlRows, pearlVisualIdx } = collectEdges(rows, nodeById, units);

  treeDiv.innerHTML = '';
  canvasDiv.innerHTML = '';
  // Remove previous band container (lives in wrapper, not in tree/canvas)
  const wrapper = treeDiv.parentElement;
  const oldBands = wrapper.querySelector('.pearl-band-container');
  if (oldBands) oldBands.remove();

  const rowEls = {}, labelEls = {};

  // ── Tree rows ──
  for (const { node } of rows) {
    const row = document.createElement('div');
    row.className = 'pearl-row';
    row.dataset.id = node.id;
    row.style.paddingLeft = (node.level === 'module' ? 0 : node.level === 'submodule' ? 20 : 40) + 'px';

    // Toggle
    const toggle = document.createElement('span');
    toggle.className = 'pearl-toggle';
    if (node.children.length > 0) {
      toggle.textContent = expanded.has(node.id) ? '\u2212' : '+';
      toggle.addEventListener('click', e => {
        e.stopPropagation();
        if (expanded.has(node.id)) expanded.delete(node.id);
        else expanded.add(node.id);
        // If selection is inside the toggled subtree, move it to the toggled node
        const selNode = state.nodeById.get(state.selection);
        if (selNode && ancestorIds(state.nodeById, selNode).includes(node.id)) state.selection = node.id;
        renderAll(state);
      });
    } else {
      toggle.classList.add('placeholder');
    }
    row.appendChild(toggle);

    // Label
    const label = document.createElement('span');
    label.className = `pearl-label level-${node.level}`;
    label.textContent = nodeLabel(node, modPrefixLen);
    row.appendChild(label);

    row.addEventListener('click', () => toggleSelection(state, node.id));

    treeDiv.appendChild(row);
    rowEls[node.id] = row;
    labelEls[node.id] = label;
  }

  // ── Module bands (in wrapper so they span tree + canvas) ──
  const bandContainer = document.createElement('div');
  bandContainer.className = 'pearl-band-container';
  wrapper.insertBefore(bandContainer, wrapper.firstChild);

  for (const mod of hierarchy.filter(n => n.level === 'module')) {
    const modRowIdxs = rows.map((r, i) => r.node.module === mod.path ? i : -1).filter(i => i >= 0);
    if (!modRowIdxs.length) continue;
    const band = document.createElement('div');
    band.className = 'pearl-band';
    band.style.top = (Math.min(...modRowIdxs) * ROW_H + TREE_PAD_TOP) + 'px';
    band.style.height = ((Math.max(...modRowIdxs) - Math.min(...modRowIdxs) + 1) * ROW_H) + 'px';
    bandContainer.appendChild(band);
  }

  // ── Prepare edge lists (needed for SVG sizing) ──
  const leftEdges = [], rightEdges = [];
  for (const edge of edges) {
    const fi = pearlVisualIdx.get(edge.from), ti = pearlVisualIdx.get(edge.to);
    if (fi < ti) leftEdges.push({ ...edge, topIdx: fi, botIdx: ti });
    else         rightEdges.push({ ...edge, topIdx: ti, botIdx: fi });
  }
  leftEdges.sort((a, b) => (a.botIdx - a.topIdx) - (b.botIdx - b.topIdx));
  rightEdges.sort((a, b) => (a.botIdx - a.topIdx) - (b.botIdx - b.topIdx));

  // ── SVG ──
  const totalH = rows.length * ROW_H + TREE_PAD_TOP * 2;
  const MIN_BULGE = 12;
  // Arc bulge is purely span-based (matching DePyTree approach)
  // Cubic Bezier max extent is ~75% of control point offset,
  // so use 2/3 of span to get a visually semicircular arc.
  const edgeBulge = e => Math.max(MIN_BULGE, (e.botIdx - e.topIdx) * ROW_H * 2 / 3);
  // Cubic bezier max visible extent is ~75% of control point offset
  const edgeExtent = e => edgeBulge(e) * 0.75;
  const calcMaxExtent = list =>
    list.length ? Math.max(...list.map(edgeExtent)) : 0;
  const arcAreaLeft = Math.max(40, calcMaxExtent(leftEdges) + PEARL_R_MODULE);
  const arcAreaRight = Math.max(40, calcMaxExtent(rightEdges) + PEARL_R_MODULE);
  const pearlCX = arcAreaLeft;
  const svgW = pearlCX + arcAreaRight + 20;

  const svg = svgEl('svg');
  svg.setAttribute('width', svgW);
  svg.setAttribute('height', totalH);
  svg.style.cssText = 'position:absolute;top:0;left:0';
  canvasDiv.appendChild(svg);
  canvasDiv.style.minHeight = totalH + 'px';
  // Make canvas wide enough so the user can scroll the pearls next to the tree
  const graphPane = document.getElementById('graph-pane');
  const visibleW = graphPane.clientWidth - TREE_WIDTH;
  canvasDiv.style.width = Math.max(svgW, pearlCX + visibleW) + 'px';
  // Ensure wrapper (and its band container) spans the full scrollable width
  wrapper.style.minWidth = (TREE_WIDTH + parseInt(canvasDiv.style.width)) + 'px';

  const rowY = idx => idx * ROW_H + ROW_H / 2 + TREE_PAD_TOP;

  // ── Pearls ──
  const pearlEls = {};
  for (const { node } of pearlRows) {
    const cy = rowY(pearlVisualIdx.get(node.id));
    const r = node.level === 'module' ? PEARL_R_MODULE
            : node.level === 'submodule' ? PEARL_R_SUBMODULE : PEARL_R_UNIT;
    const color = moduleColors[node.module] || '#999';
    const circle = svgEl('circle');
    circle.setAttribute('cx', pearlCX);
    circle.setAttribute('cy', cy);
    circle.setAttribute('r', r);
    circle.setAttribute('fill', color);
    circle.setAttribute('stroke', darken(color));
    circle.setAttribute('stroke-width', '1.5');
    circle.classList.add('pearl-circle');
    circle.style.cursor = 'pointer';
    const title = svgEl('title');
    title.textContent = node.path;
    circle.appendChild(title);
    circle.addEventListener('click', () => toggleSelection(state, node.id));
    svg.appendChild(circle);
    pearlEls[node.id] = circle;
  }

  // ── Arcs ──
  const arcEls = {};

  // Cubic Bezier arcs: control points at (cx +/- offset, y1) and (cx +/- offset, y2)
  // produces natural rounded arcs. Offset scales with span; side controls direction.
  const drawArcs = (edgeList, side) => {
    const sign = side === 'left' ? -1 : 1;
    for (let i = 0; i < edgeList.length; i++) {
      const e = edgeList[i];
      const y1 = rowY(e.topIdx), y2 = rowY(e.botIdx);
      const bulge = Math.max(MIN_BULGE, (y2 - y1) * 2 / 3);
      const cpx = pearlCX + sign * bulge;
      const color = e.valid ? '#2a7a2a' : '#b03a2e';

      const path = svgEl('path');
      path.setAttribute('d', `M${pearlCX},${y1} C${cpx},${y1} ${cpx},${y2} ${pearlCX},${y2}`);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', color);
      path.setAttribute('stroke-width', '1.5');
      path.classList.add('pearl-arc');
      if (!e.valid) path.classList.add('violation');
      svg.insertBefore(path, svg.firstChild);
      arcEls[`${e.from}->${e.to}`] = path;
    }
  };
  drawArcs(leftEdges, 'left');
  drawArcs(rightEdges, 'right');

  // Store artifacts for highlighting
  Object.assign(state, {
    _rowEls: rowEls, _labelEls: labelEls, _pearlEls: pearlEls,
    _arcEls: arcEls, _rows: rows, _pearlRows: pearlRows, _edges: edges,
  });

  if (state.selection) applySelection(state);

  // Background click clears selection
  if (state._bgClickHandler) graphPane.removeEventListener('click', state._bgClickHandler);
  state._bgClickHandler = e => {
    if (!e.target.closest('.pearl-row') && !e.target.classList.contains('pearl-circle')) {
      state.selection = null;
      clearHighlights(state);
      clearDetail();
    }
  };
  graphPane.addEventListener('click', state._bgClickHandler);
  graphPane._viewCleanup = () => graphPane.removeEventListener('click', state._bgClickHandler);
}

// ── Selection ────────────────────────────────────────────────────────────────
function toggleSelection(state, nodeId) {
  if (state.selection === nodeId) {
    state.selection = null;
    clearHighlights(state);
    clearDetail();
  } else {
    state.selection = nodeId;
    applySelection(state);
  }
}

function applySelection(state) {
  const { nodeById, units, _pearlEls, _arcEls, _rows, _rowEls, _labelEls, _edges } = state;
  const selNode = nodeById.get(state.selection);
  if (!selNode) return;

  // The selection includes all descendants (for expanded modules/submodules)
  const descendants = node => [node, ...node.children.flatMap(descendants)];
  const selNodes = descendants(selNode);
  const selIds = new Set(selNodes.map(n => n.id));

  // Connected pearls and arcs
  const connectedPearls = new Set(selIds);
  const connectedArcs = new Set();
  for (const edge of _edges) {
    if (selIds.has(edge.from) || selIds.has(edge.to)) {
      connectedPearls.add(edge.from);
      connectedPearls.add(edge.to);
      connectedArcs.add(`${edge.from}->${edge.to}`);
    }
  }

  // Expanded module/submodule rows have no pearl, so they stay focused via
  // the connected pearls (incl. the selection) they contain
  const withAncestors = ids => new Set([...ids].flatMap(id => {
    const node = nodeById.get(id);
    return node ? [id, ...ancestorIds(nodeById, node)] : [];
  }));
  const focusedIds = withAncestors(connectedPearls);

  // Callee/caller label styling, rolled up to submodule and module level
  const { callees, callers } = dependencyRoles(units, selNodes.filter(n => n.level === 'unit').map(n => n.path));
  const calleeIds = withAncestors([...callees].map(u => nodeId('unit', u)));
  const callerIds = withAncestors([...callers].map(u => nodeId('unit', u)));

  // Highlight rows
  for (const { node } of _rows) {
    const row = _rowEls[node.id], label = _labelEls[node.id];
    const isFocused = focusedIds.has(node.id);
    row.classList.toggle('dimmed', !isFocused);
    row.classList.toggle('focused', isFocused);
    label.classList.remove('is-selected', 'is-callee', 'is-caller');
    if (!isFocused) continue;
    if (selIds.has(node.id))         label.classList.add('is-selected');
    else if (calleeIds.has(node.id)) label.classList.add('is-callee');
    else if (callerIds.has(node.id)) label.classList.add('is-caller');
  }

  // Highlight arcs
  for (const [key, el] of Object.entries(_arcEls)) {
    el.classList.toggle('highlighted', connectedArcs.has(key));
    el.classList.toggle('dimmed', !connectedArcs.has(key));
  }

  // Highlight pearls
  for (const [id, el] of Object.entries(_pearlEls))
    el.classList.toggle('dimmed', !connectedPearls.has(id));

  // Detail panel (units in tree order)
  const unitPaths = node => node.children.map(c => c.path);
  if (selNode.level === 'unit')
    showUnitDetail(selNode.path, units[selNode.path]);
  else if (selNode.children[0]?.level === 'submodule')
    showModuleDetail(selNode.path, selNode.children.map(sm => [sm.path, unitPaths(sm)]), units);
  else
    showSubmoduleDetail(selNode.path, unitPaths(selNode), units);
}

function clearHighlights(state) {
  for (const { node } of state._rows || []) {
    state._rowEls[node.id]?.classList.remove('dimmed', 'focused');
    state._labelEls[node.id]?.classList.remove('is-selected', 'is-callee', 'is-caller');
  }
  for (const el of Object.values(state._arcEls || {}))
    el.classList.remove('dimmed', 'highlighted');
  for (const el of Object.values(state._pearlEls || {}))
    el.classList.remove('dimmed');
}

// ── Utilities ────────────────────────────────────────────────────────────────
function darken(hex) {
  const m = hex.match(/^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i);
  if (!m) return '#666';
  return '#' + [m[1], m[2], m[3]]
    .map(c => Math.max(0, Math.round(parseInt(c, 16) * 0.6)).toString(16).padStart(2, '0'))
    .join('');
}

/* Coverage lab dashboard.
 *
 * Rendering rules worth keeping in mind while reading this file:
 *  - Intervals arrive already normalized from the server. The UI never adds
 *    lengths of overlapping ranges itself; it draws what it is given and
 *    labels what it cannot establish as "unknown".
 *  - All server-provided strings go into the DOM via textContent.
 *  - Charts are plain DOM/SVG; tooltips enhance but never gate values, and a
 *    table view exists for both charts.
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  data: null,
  selectedNode: null,
  selectedGap: null,
  nodeDetail: null,
  evidence: null,
  compareRunId: null,
};

/* ------------------------------------------------------------------ helpers */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value; // only for static markup
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "style") Object.assign(node.style, value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function ticks(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("en-US");
}

function fractionLabel(row) {
  if (!row || row.state === "unknown" || !row.ratio_fraction) return null;
  return row.ratio_fraction;
}

function pct(row) {
  if (!row || row.ratio === null || row.ratio === undefined) return null;
  return `${(row.ratio * 100).toFixed(1)}%`;
}

function scrollToCard(target) {
  if (target && typeof target.scrollIntoView === "function") {
    target.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function setStatus(message, isError = false) {
  const line = $("#status-line");
  line.textContent = message || "";
  line.classList.toggle("error", Boolean(isError));
}

function signed(n, digits = 0) {
  if (n === null || n === undefined) return "—";
  const v = digits ? n.toFixed(digits) : String(n);
  return n > 0 ? `+${v}` : v;
}

function deltaClass(n) {
  if (n === null || n === undefined) return "delta-none";
  if (n > 0) return "delta-up";
  if (n < 0) return "delta-down";
  return "delta-none";
}

/* ------------------------------------------------------------------ tooltip */

const tooltip = {
  node: null,
  show(evt, value, label) {
    const tip = $("#tooltip");
    tip.replaceChildren();
    const strong = el("div", { class: "tt-value", text: value });
    tip.append(strong);
    if (label) {
      const lines = Array.isArray(label) ? label : [label];
      for (const line of lines) tip.append(el("div", { class: "tt-label", text: line }));
    }
    tip.hidden = false;
    this.move(evt);
  },
  move(evt) {
    const tip = $("#tooltip");
    if (tip.hidden) return;
    const pad = 14;
    const x = Math.min(evt.clientX + pad, window.innerWidth - tip.offsetWidth - 8);
    const y = Math.min(evt.clientY + pad, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${Math.max(8, y)}px`;
  },
  hide() {
    $("#tooltip").hidden = true;
  },
};

/* ------------------------------------------------------------------ API */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (err) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && payload.error) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

async function refresh(message) {
  $("#tiles").classList.add("skeleton");
  setStatus(message || "Loading current snapshot…");
  try {
    state.data = await api("/api/state");
    renderAll();
    setStatus(
      `View computed from revision ${state.data.revision} at ${state.data.generated_at}.`
    );
  } catch (err) {
    setStatus(`Could not load snapshot: ${err.message}`, true);
  } finally {
    $("#tiles").classList.remove("skeleton");
  }
}

/* ------------------------------------------------------------------ tiles */

function renderTiles() {
  const data = state.data;
  const nodes = data.nodes;
  const known = nodes.filter((n) => n.state !== "unknown");
  const unknown = nodes.filter((n) => n.state === "unknown");
  const declared = known.reduce((sum, n) => sum + n.declared_ticks, 0);
  const covered = known.reduce((sum, n) => sum + n.covered_ticks, 0);
  const gapTicks = known.reduce((sum, n) => sum + n.gap_ticks, 0);
  const gapCount = known.reduce((sum, n) => sum + n.gap_count, 0);
  const stopped = nodes.filter((n) => n.status === "stopped").length;
  const outOfWindow = nodes.reduce((sum, n) => sum + n.out_of_window_ticks, 0);
  const comparison = data.comparison;
  const biggestGroup = comparison && comparison.groups
    ? comparison.groups.reduce(
        (best, g) => (g.covered_delta > (best?.covered_delta ?? -Infinity) ? g : best),
        null
      )
    : null;

  const tiles = [];

  tiles.push(
    el(
      "div",
      { class: "tile" },
      el("span", { class: "tile-label", text: "Covered / declared ticks (nodes with a window)" }),
      el(
        "span",
        { class: "tile-value" },
        ticks(covered),
        el("small", { text: ` / ${ticks(declared)}` })
      ),
      el("span", {
        class: "tile-delta",
        text:
          declared > 0
            ? `${((covered / declared) * 100).toFixed(1)}% of declared ticks — exact union, overlaps counted once`
            : "No node declares a window yet",
      })
    )
  );

  tiles.push(
    el(
      "div",
      { class: "tile" },
      el("span", { class: "tile-label", text: "Gaps" }),
      el("span", { class: "tile-value" }, ticks(gapCount)),
      el("span", {
        class: "tile-delta",
        text: `${ticks(gapTicks)} uncovered ticks inside declared windows`,
      })
    )
  );

  const unknownTile = el(
    "div",
    { class: "tile" },
    el("span", { class: "tile-label", text: "Nodes without a declared window" }),
    el("span", { class: "tile-value" }, ticks(unknown.length)),
    el("span", {
      class: "tile-delta unknown",
      text: unknown.length
        ? `Excluded from all ratios: ${unknown.map((n) => n.node_id).join(", ")}`
        : "Every node declares a window",
    })
  );
  tiles.push(unknownTile);

  tiles.push(
    el(
      "div",
      { class: "tile" },
      el("span", { class: "tile-label", text: "Stopped nodes" }),
      el(
        "span",
        { class: "tile-value" },
        ticks(stopped),
        el("small", { text: ` / ${nodes.length}` })
      ),
      el("span", {
        class: "tile-delta",
        text: "Stopped nodes reject live events; manifests still accepted",
      })
    )
  );

  if (comparison) {
    const totalDelta = comparison.groups.reduce(
      (sum, g) => sum + (g.covered_delta || 0),
      0
    );
    const groupDelta = biggestGroup
      ? `largest group change: ${biggestGroup.group} ${signed(biggestGroup.covered_delta)} ticks`
      : "";
    tiles.push(
      el(
        "div",
        { class: "tile" },
        el("span", { class: "tile-label", text: "Change since previous stored run" }),
        el("span", { class: "tile-value" }, `${signed(totalDelta)}`),
        el("span", {
          class: `tile-delta ${deltaClass(totalDelta)}`,
          text: `covered ticks across windowed nodes; ${groupDelta}`,
        })
      )
    );
  }

  if (outOfWindow) {
    tiles.push(
      el(
        "div",
        { class: "tile" },
        el("span", { class: "tile-label", text: "Claimed outside declared windows" }),
        el("span", { class: "tile-value" }, ticks(outOfWindow)),
        el("span", {
          class: "tile-delta unknown",
          text: "Visible but excluded from ratios — not evidence of window coverage",
        })
      )
    );
  }

  $("#tiles").replaceChildren(...tiles);
}

/* ------------------------------------------------------------------ groups */

function groupSegmentedBar(group) {
  const track = el("div", { class: "group-bar" });
  const declared = group.declared_ticks;
  if (group.state === "unknown" || !declared) {
    const band = el("div", { class: "part declared-only" });
    band.style.width = "100%";
    band.style.background =
      "repeating-linear-gradient(45deg, color-mix(in srgb, var(--unknown) 45%, transparent) 0 3px, transparent 3px 8px)";
    track.append(band);
    track.title = "Ratio withheld: at least one node has no declared window";
    return track;
  }
  const covered = group.covered_ticks;
  const share = Math.max(0, Math.min(1, covered / declared));
  if (share > 0) {
    const have = el("div", { class: "part have" });
    have.style.width = `calc(${(share * 100).toFixed(4)}% - 2px)`;
    track.append(have);
  }
  if (share < 1) {
    const miss = el("div", { class: "part miss" });
    miss.style.width = `${((1 - share) * 100).toFixed(4)}%`;
    miss.style.background =
      "repeating-linear-gradient(45deg, color-mix(in srgb, var(--critical) 40%, transparent) 0 2px, transparent 2px 6px)";
    miss.style.border = "1px solid color-mix(in srgb, var(--critical) 50%, transparent)";
    track.append(miss);
  }
  return track;
}

function renderGroups() {
  const groups = state.data.groups;
  const container = $("#groups-chart");
  container.replaceChildren();
  if (!groups.length) {
    container.append(el("p", { class: "card-sub", text: "No nodes yet." }));
  }
  for (const group of groups) {
    const known = group.state !== "unknown";
    const nameCell = el(
      "div",
      { class: "group-name" },
      group.group,
      el("span", {
        class: "meta",
        text: `${group.node_count} node${group.node_count === 1 ? "" : "s"}${
          group.unknown_nodes.length ? ` · ${group.unknown_nodes.length} unknown` : ""
        }`,
      })
    );
    const bar = groupSegmentedBar(group);
    bar.addEventListener("pointermove", (evt) => {
      if (!known) {
        tooltip.show(evt, "Ratio unknown", "At least one node has no declared window; no ratio is shown.");
        return;
      }
      tooltip.show(
        evt,
        `${fractionLabel(group)} covered`,
        [
          `${ticks(group.covered_ticks)} of ${ticks(group.declared_ticks)} declared ticks`,
          `${group.gap_count} gap${group.gap_count === 1 ? "" : "s"} · ${ticks(group.gap_ticks)} uncovered ticks`,
        ]
      );
    });
    bar.addEventListener("pointerleave", () => tooltip.hide());

    const stats = el("div", { class: "group-stats" });
    if (known) {
      stats.append(
        el("strong", { text: fractionLabel(group) }),
        el("span", { text: ` covered · ${pct(group)}` }),
        el("div", {
          text: `${group.gap_count} gap${group.gap_count === 1 ? "" : "s"}`,
        })
      );
    } else {
      stats.append(
        el("span", { class: "state-unknown", text: "ratio unknown" }),
        el("div", {
          text: `${ticks(group.known_covered_ticks ?? 0)} covered ticks reported`,
        })
      );
    }

    container.append(el("div", { class: "group-row" }, nameCell, bar, stats));
  }

  // Table view twin
  const table = el(
    "table",
    {},
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Group" }),
        el("th", { class: "num", text: "Declared ticks" }),
        el("th", { class: "num", text: "Covered ticks" }),
        el("th", { class: "num", text: "Ratio" }),
        el("th", { class: "num", text: "Gaps" }),
        el("th", { class: "num", text: "Uncovered ticks" }),
        el("th", { text: "Nodes" }),
        el("th", { text: "State" })
      )
    )
  );
  const body = el("tbody");
  for (const group of groups) {
    body.append(
      el(
        "tr",
        {},
        el("td", { text: group.group }),
        el("td", { class: "num", text: ticks(group.declared_ticks) }),
        el("td", { class: "num", text: ticks(group.covered_ticks) }),
        el("td", {
          class: `num ${group.state === "unknown" ? "state-unknown" : ""}`,
          text: fractionLabel(group) || "unknown",
        }),
        el("td", { class: "num", text: ticks(group.gap_count) }),
        el("td", { class: "num", text: ticks(group.gap_ticks) }),
        el("td", {
          text: group.nodes
            .map((n) => `${n.node_id}${n.state === "unknown" ? " (no window)" : ""}`)
            .join(", "),
        }),
        el("td", { class: group.state === "unknown" ? "state-unknown" : "", text: group.state })
      )
    );
  }
  table.append(body);
  $("#groups-table").replaceChildren(table);
}

/* ------------------------------------------------------------------ nodes */

function nodeDomain(node) {
  const points = [0];
  if (node.window) points.push(node.window[0], node.window[1]);
  for (const [start, end] of node.coverage) points.push(start, end);
  for (const r of node.retractions || []) points.push(r.start, r.end);
  return [0, Math.max(1, ...points)];
}

/** Round tick positions (1, 2 or 5 × 10^n) that fit in ~4 steps. */
function niceTicks(max, count = 4) {
  if (max <= 0) return [0];
  const rawStep = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const residual = rawStep / magnitude;
  // Smallest 1/2/5 × 10^n step that is at least the raw step.
  const step = (residual <= 1 ? 1 : residual <= 2 ? 2 : residual <= 5 ? 5 : 10) * magnitude;
  const out = [];
  for (let value = 0; value <= max + 1e-9; value += step) out.push(Math.round(value));
  if (out[out.length - 1] < max) out.push(max);
  return out;
}

function renderTrack(node) {
  const track = el("div", { class: "track", role: "group", "aria-label": `${node.node_id} timeline` });
  const [lo, hi] = nodeDomain(node);
  const span = hi - lo || 1;
  const pctOf = (v) => ((v - lo) / span) * 100;

  if (node.window) {
    // Declared window: a quiet surface step behind the data, not a data mark.
    const band = el("div", { class: "window-band", "aria-hidden": "true" });
    band.style.left = `${pctOf(node.window[0])}%`;
    band.style.width = `${pctOf(node.window[1]) - pctOf(node.window[0])}%`;
    track.append(band);
  } else {
    const band = el("div", { class: "nowindow-band", "aria-hidden": "true" });
    band.style.left = "0%";
    band.style.width = "100%";
    track.append(band);
  }

  // covered segments
  for (const [start, end] of node.coverage) {
    const seg = el("div", { class: "seg" });
    seg.style.left = `${pctOf(start)}%`;
    seg.style.width = `${Math.max(0.4, pctOf(end) - pctOf(start))}%`;
    if (node.window && (start < node.window[0] || end > node.window[1])) {
      seg.style.opacity = "0.45";
    }
    seg.dataset.node = node.node_id;
    seg.addEventListener("pointermove", (evt) =>
      tooltip.show(evt, `${ticks(end - start)} ticks covered`, [
        `[${start}, ${end})`,
        node.window ? `inside declared window [${node.window[0]}, ${node.window[1]})` : "no declared window",
      ])
    );
    seg.addEventListener("pointerleave", () => tooltip.hide());
    track.append(seg);
  }

  // Operator retractions: claims may exist underneath, but they no longer
  // count. Drawn beneath segments and gap buttons; the reason is surfaced in
  // each overlapping gap's tooltip and evidence panel, never by colour alone.
  const overlappingRetractions = (gap) =>
    gap.retractions ||
    (node.retractions || []).filter((r) => r.start < gap.end && r.end > gap.start);
  for (const r of node.retractions || []) {
    const mark = el("div", { class: "retract", "aria-hidden": "true" });
    mark.style.left = `${pctOf(r.start)}%`;
    mark.style.width = `${Math.max(0.6, pctOf(r.end) - pctOf(r.start))}%`;
    track.append(mark);
  }

  // gaps inside the declared window
  for (const gap of node.gaps || []) {
    const button = el("button", {
      class: "void",
      type: "button",
      title: `Gap [${gap.start}, ${gap.end}) — open evidence`,
      "aria-label": `${node.node_id} gap from ${gap.start} to ${gap.end}, ${gap.length} ticks. Show evidence.`,
    });
    button.style.left = `${pctOf(gap.start)}%`;
    button.style.width = `${Math.max(0.8, pctOf(gap.end) - pctOf(gap.start))}%`;
    button.addEventListener("click", () => inspectGap(node, gap));
    const reasons = overlappingRetractions(gap).map(
      (r) => `retracted: ${r.reason}`
    );
    button.addEventListener("pointermove", (evt) =>
      tooltip.show(evt, `${ticks(gap.length)} tick gap`, [
        `[${gap.start}, ${gap.end})`,
        ...reasons.slice(0, 2),
        "Click to expand the evidence",
      ])
    );
    button.addEventListener("pointerleave", () => tooltip.hide());
    button.addEventListener("focus", (evt) => {
      const rect = evt.target.getBoundingClientRect();
      tooltip.show({ clientX: rect.left, clientY: rect.bottom }, `${ticks(gap.length)} tick gap`, [
        `[${gap.start}, ${gap.end})`,
        "Press Enter to expand the evidence",
      ]);
    });
    button.addEventListener("blur", () => tooltip.hide());
    track.append(button);
  }

  const axis = el("div", { class: "axis-line" });
  track.append(axis);
  return track;
}

function renderAxis(node) {
  const labels = el("div", { class: "axis-row" });
  const [lo, hi] = nodeDomain(node);
  const span = hi - lo || 1;
  const pctOf = (v) => ((v - lo) / span) * 100;
  // A few round ticks cover the values no direct label carries.
  for (const value of niceTicks(hi)) {
    const tick = el("span", { class: "tick", text: ticks(value) });
    tick.style.left = `${pctOf(value)}%`;
    labels.append(tick);
  }
  return labels;
}

function renderNodes() {
  const nodes = state.data.nodes;
  const container = $("#nodes-chart");
  container.replaceChildren();
  if (!nodes.length) {
    container.append(el("p", { class: "card-sub", text: "No nodes yet." }));
  }
  for (const node of nodes) {
    const row = el("div", { class: "node-row" });
    if (state.selectedNode === node.node_id) row.classList.add("selected");

    const stopBtn = el(
      "button",
      {
        class: "ghost small",
        type: "button",
        title:
          node.status === "running"
            ? "Stop node: live events rejected, manifests still accepted"
            : "Restart node",
      },
      node.status === "running" ? "Stop" : "Start"
    );
    stopBtn.addEventListener("click", () => toggleNode(node));

    const inspectBtn = el("button", { class: "ghost small", type: "button", text: "Inspect" });
    inspectBtn.addEventListener("click", () => inspectNode(node));

    const nameCell = el(
      "div",
      {},
      el(
        "div",
        { class: "node-id" },
        el("span", { class: `status-dot ${node.status}` }),
        node.node_id,
        el("span", { class: "group-tag", text: node.group })
      ),
      el("div", {
        class: "node-meta",
        text:
          node.status === "stopped"
            ? `stopped · ${node.retraction_count} retraction${node.retraction_count === 1 ? "" : "s"}`
            : `${node.state}${node.window ? ` · window [${node.window[0]}, ${node.window[1]})` : " · no declared window"}`,
      }),
      node.notes && node.notes.length
        ? el("div", { class: "node-meta note", text: node.notes.join(" ") })
        : null,
      el("div", { class: "node-meta" }, stopBtn, inspectBtn)
    );

    const ratio = el("div", { class: "node-ratio" });
    if (node.state === "unknown") {
      ratio.append(
        el("span", { class: "frac state-unknown", text: "unknown" }),
        el("span", { class: "sub", text: "no declared window" })
      );
    } else {
      ratio.append(
        el("span", { class: "frac", text: fractionLabel(node) }),
        el("span", { class: "sub", text: `${ticks(node.covered_ticks)} / ${ticks(node.declared_ticks)} ticks` }),
        el("span", {
          class: "sub",
          text: `${node.gap_count} gap${node.gap_count === 1 ? "" : "s"}${node.out_of_window_ticks ? ` · ${ticks(node.out_of_window_ticks)} outside` : ""}`,
        })
      );
    }

    const trackWrap = el(
      "div",
      { class: "track-wrap" },
      renderTrack(node),
      renderAxis(node)
    );
    row.append(nameCell, trackWrap, ratio);
    container.append(row);
  }

  // Table view twin
  const table = el(
    "table",
    {},
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Node" }),
        el("th", { text: "Group" }),
        el("th", { text: "Status" }),
        el("th", { text: "Window" }),
        el("th", { class: "num", text: "Declared" }),
        el("th", { class: "num", text: "Covered" }),
        el("th", { class: "num", text: "Ratio" }),
        el("th", { class: "num", text: "Gaps" }),
        el("th", { text: "Segments" })
      )
    )
  );
  const body = el("tbody");
  for (const node of nodes) {
    body.append(
      el(
        "tr",
        {},
        el("td", { text: node.node_id }),
        el("td", { text: node.group }),
        el("td", { text: node.status }),
        el("td", { text: node.window ? `[${node.window[0]}, ${node.window[1]})` : "none" }),
        el("td", { class: "num", text: ticks(node.declared_ticks) }),
        el("td", { class: "num", text: ticks(node.covered_ticks) }),
        el("td", {
          class: `num ${node.state === "unknown" ? "state-unknown" : ""}`,
          text: fractionLabel(node) || "unknown",
        }),
        el("td", { class: "num", text: ticks(node.gap_count) }),
        el("td", { class: "mono", text: node.coverage.map(([s, e]) => `[${s},${e})`).join(" ") || "—" })
      )
    );
  }
  table.append(body);
  $("#nodes-table").replaceChildren(table);
}

/* ------------------------------------------------------------------ inspector */

async function toggleNode(node) {
  const target = node.status === "running" ? "stopped" : "running";
  const verb = target === "stopped" ? "Stopping" : "Starting";
  setStatus(`${verb} ${node.node_id}…`);
  try {
    await api(`/api/nodes/${encodeURIComponent(node.node_id)}/${target === "stopped" ? "stop" : "start"}`, {
      method: "POST",
      body: "{}",
    });
    await refresh(`${node.node_id} is now ${target}. Recomputing coverage…`);
  } catch (err) {
    setStatus(`Could not change ${node.node_id}: ${err.message}`, true);
  }
}

function evidenceRange(evidence) {
  const [start, end] = evidence.range;
  return `[${start}, ${end})`;
}

function miniTimeline(node, evidence) {
  const wrap = el("div", { class: "mini-timeline" });
  const [lo, hi] = nodeDomain(node);
  const span = hi - lo || 1;
  const pctOf = (v) => ((v - lo) / span) * 100;
  for (const [start, end] of node.coverage) {
    const seg = el("div", { class: "seg" });
    seg.style.left = `${pctOf(start)}%`;
    seg.style.width = `${Math.max(0.4, pctOf(end) - pctOf(start))}%`;
    seg.style.opacity = "0.5";
    wrap.append(seg);
  }
  for (const [start, end] of evidence.uncovered) {
    const seg = el("div", { class: "void" });
    seg.style.left = `${pctOf(start)}%`;
    seg.style.width = `${Math.max(0.8, pctOf(end) - pctOf(start))}%`;
    wrap.append(seg);
  }
  const frame = el("div", { class: "frame" });
  frame.style.left = `${pctOf(evidence.range[0])}%`;
  frame.style.width = `${Math.max(0.8, pctOf(evidence.range[1]) - pctOf(evidence.range[0]))}%`;
  wrap.append(frame);
  return wrap;
}

function evidenceList(evidence) {
  const list = el("ul", { class: "evidence-list" });
  for (const line of evidence.explanation) {
    list.append(el("li", {}, el("span", { class: "fact", text: line })));
  }
  for (const r of evidence.retractions) {
    list.append(
      el(
        "li",
        { class: "retract" },
        el("strong", { text: "Retraction " }),
        el("span", { class: "mono", text: `[${r.start}, ${r.end})` }),
        el("div", { class: "fact", text: r.reason }),
        el("div", { class: "fact", text: `recorded ${r.created_at}` })
      )
    );
  }
  for (const m of evidence.withdrawn_manifests) {
    list.append(
      el(
        "li",
        { class: "withdrawn" },
        el("strong", { text: `Manifest ${m.name} r${m.revision} withdrawn ` }),
        el("span", { class: "fact", text: `superseded by r${m.superseded_by} at ${m.superseded_at}` }),
        m.note ? el("div", { class: "fact", text: `reason: ${m.note}` }) : null
      )
    );
  }
  for (const m of evidence.manifest_revisions) {
    const isLatest = !evidence.withdrawn_manifests.some(
      (w) => w.name === m.name && w.revision === m.revision
    );
    list.append(
      el(
        "li",
        {},
        el("strong", { text: `Manifest ${m.name} r${m.revision}` }),
        el("span", { class: "pill", text: isLatest ? "active" : "historical" }),
        el("div", {
          class: "mono fact",
          text: m.intervals.map(([s, e]) => `[${s},${e})`).join(" ") || "(no intervals)",
        }),
        m.note ? el("div", { class: "fact", text: m.note }) : null
      )
    );
  }
  if (evidence.events.length) {
    const live = evidence.live_events.length;
    const archived = evidence.archived_events.length;
    list.append(
      el(
        "li",
        {},
        el("strong", { text: "Claiming events " }),
        el("div", {
          class: "fact",
          text: `${evidence.events.length} show; ${live} read from live rows, ${archived} decompressed from archive chunks. They are merged before measuring.`,
        }),
        el(
          "div",
          { class: "mono fact" },
          evidence.events
            .slice(0, 8)
            .map((e) => `${e.event_id} [${e.start},${e.end})`)
            .join(" · ")
        )
      )
    );
  }
  for (const item of evidence.unknown) {
    list.append(el("li", { class: "unknown" }, el("strong", { text: "Unknown: " }), item));
  }
  return list;
}

function actionForms(detail) {
  const node = detail.node;
  const latestByName = new Map();
  for (const m of detail.manifests) {
    const current = latestByName.get(m.name);
    if (!current || m.revision > current.revision) latestByName.set(m.name, m);
  }
  const defaultName = "operator-backfill";
  const nextRevision = (latestByName.get(defaultName)?.revision || 0) + 1;

  const nameInput = el("input", { type: "text", value: defaultName, "aria-label": "Manifest name" });
  const revInput = el("input", {
    type: "number",
    min: "1",
    value: String(nextRevision),
    "aria-label": "Manifest revision",
  });
  const rangeInput = el("textarea", {
    placeholder: "intervals, e.g. 120-180, 200-210",
    "aria-label": "Manifest intervals, start-end pairs separated by commas",
  });
  const noteInput = el("input", { type: "text", placeholder: "note shown as evidence", "aria-label": "Manifest note" });
  const submit = el("button", { class: "primary", type: "button", text: "Submit manifest revision" });
  submit.addEventListener("click", async () => {
    setStatus(`Submitting manifest for ${node.node_id}…`);
    try {
      const result = await api("/api/manifests", {
        method: "POST",
        body: JSON.stringify({
          node_id: node.node_id,
          name: nameInput.value.trim(),
          revision: Number(revInput.value),
          intervals: rangeInput.value.trim(),
          note: noteInput.value.trim(),
        }),
      });
      await refresh(
        result.state === "duplicate"
          ? `Manifest ${result.manifest.name} r${result.manifest.revision} was already present.`
          : `Manifest ${result.manifest.name} r${result.manifest.revision} accepted; revision ${result.revision}.`
      );
      if (state.selectedNode === node.node_id) await inspectNode({ node_id: node.node_id });
    } catch (err) {
      setStatus(`Manifest rejected: ${err.message}`, true);
    }
  });

  const rStart = el("input", { type: "number", min: "0", placeholder: "start", "aria-label": "Retraction start tick" });
  const rEnd = el("input", { type: "number", min: "1", placeholder: "end", "aria-label": "Retraction end tick" });
  const rReason = el("input", { type: "text", placeholder: "reason (required)", "aria-label": "Retraction reason" });
  const retractBtn = el("button", { class: "ghost", type: "button", text: "Retract range" });
  retractBtn.addEventListener("click", async () => {
    setStatus(`Recording retraction for ${node.node_id}…`);
    try {
      const result = await api("/api/retractions", {
        method: "POST",
        body: JSON.stringify({
          node_id: node.node_id,
          start: Number(rStart.value),
          end: Number(rEnd.value),
          reason: rReason.value.trim(),
        }),
      });
      await refresh(
        result.created
          ? `Retraction [${result.start}, ${result.end}) recorded; revision ${result.revision}.`
          : "An identical retraction already exists; nothing changed."
      );
      if (state.selectedNode === node.node_id) await inspectNode({ node_id: node.node_id });
    } catch (err) {
      setStatus(`Retraction rejected: ${err.message}`, true);
    }
  });

  return el(
    "div",
    { class: "stack" },
    el("h3", { text: "Backfill with a manifest" }),
    el("p", {
      class: "card-sub",
      text: "Manifests are versioned per name. A newer revision replaces older ones for coverage, but older ones stay as evidence of what was withdrawn.",
    }),
    el("div", { class: "form-row" }, nameInput, revInput),
    rangeInput,
    noteInput,
    submit,
    el("h3", { text: "Retract a range" }),
    el("p", {
      class: "card-sub",
      text: "Retractions always win over event and manifest claims, and appear as gaps with the operator's reason.",
    }),
    el("div", { class: "form-row" }, rStart, rEnd),
    rReason,
    retractBtn
  );
}

async function inspectNode(node) {
  state.selectedNode = node.node_id;
  state.selectedGap = null;
  renderNodes();
  const card = $("#inspector-card");
  card.hidden = false;
  $("#inspector-title").textContent = `Node ${node.node_id}`;
  $("#inspector-sub").textContent = "Current claims, stored manifest revisions and retractions.";
  $("#inspector-body").replaceChildren(
    el("p", { class: "card-sub", text: "Loading node detail… (open a gap on the timeline to see its evidence)" })
  );
  try {
    const detail = await api(`/api/nodes/${encodeURIComponent(node.node_id)}`);
    state.nodeDetail = detail;
    if (state.selectedNode !== node.node_id) return;
    const latest = detail.node.window
      ? `declared window [${detail.node.window[0]}, ${detail.node.window[1]})`
      : "no declared window — ratios stay unknown";
    const left = el(
      "div",
      {},
      el("p", { class: "card-sub", text: `${latest} · store revision ${detail.revision}` }),
      miniTimeline(detail.analysis, {
        range: detail.node.window || [0, Math.max(1, ...detail.segments.map((s) => s[1]))],
        uncovered: (detail.analysis.gaps || []).map((g) => [g.start, g.end]),
      }),
      el("h3", { text: "Stored manifest revisions" }),
      detail.manifests.length
        ? el(
            "ul",
            { class: "evidence-list" },
            ...detail.manifests.map((m) => {
              const active = detail.latest_manifests.some(
                (l) => l.name === m.name && l.revision === m.revision
              );
              return el(
                "li",
                { class: active ? "" : "withdrawn" },
                el("strong", { text: `${m.name} r${m.revision} ` }),
                el("span", { class: "pill", text: active ? "active claim" : "superseded" }),
                el("div", {
                  class: "mono fact",
                  text: m.intervals.map(([s, e]) => `[${s},${e})`).join(" ") || "(empty)",
                }),
                m.note ? el("div", { class: "fact", text: m.note }) : null,
                el("div", { class: "fact", text: `submitted ${m.submitted_at}` })
              );
            })
          )
        : el("p", { class: "card-sub", text: "No manifests submitted for this node." }),
      el("h3", { text: "Retractions" }),
      detail.retractions.length
        ? el(
            "ul",
            { class: "evidence-list" },
            ...detail.retractions.map((r) =>
              el(
                "li",
                { class: "retract" },
                el("span", { class: "mono", text: `[${r.start}, ${r.end})` }),
                el("div", { class: "fact", text: r.reason })
              )
            )
          )
        : el("p", { class: "card-sub", text: "None." }),
      el("h3", { text: "Storage" }),
      el("p", {
        class: "card-sub",
        text: `${detail.archive_stats.live_events} live events, ${detail.archive_stats.archived_events} archived in ${detail.archive_stats.archived_chunks} compressed chunk(s). Archived claims are still counted once and readable on demand.`,
      })
    );
    const right = actionForms(detail);
    $("#inspector-body").replaceChildren(left, right);
    scrollToCard(card);
  } catch (err) {
    $("#inspector-body").replaceChildren(
      el("p", { class: "card-sub", text: `Could not load node: ${err.message}` })
    );
  }
}

async function inspectGap(node, gap) {
  state.selectedGap = gap;
  state.selectedNode = node.node_id;
  renderNodes();
  const card = $("#inspector-card");
  card.hidden = false;
  $("#inspector-title").textContent = `Gap evidence — ${node.node_id}`;
  $("#inspector-sub").textContent = `Range [${gap.start}, ${gap.end}) · ${gap.length} ticks.`;
  $("#inspector-body").replaceChildren(el("p", { class: "card-sub", text: "Loading evidence…" }));
  try {
    const [evidence, detail] = await Promise.all([
      api(`/api/gap?node=${encodeURIComponent(node.node_id)}&start=${gap.start}&end=${gap.end}`),
      api(`/api/nodes/${encodeURIComponent(node.node_id)}`),
    ]);
    state.evidence = evidence;
    state.nodeDetail = detail;
    if (state.selectedNode !== node.node_id || state.selectedGap !== gap) return;
    const coverageNote =
      evidence.in_declared_window === null
        ? "Node has no declared window, so whether this range was supposed to be covered is unknown."
        : evidence.in_declared_window
          ? "Range lies inside the declared window."
          : "Range is partially or fully outside the declared window: it cannot be scored against the window ratio.";
    const left = el(
      "div",
      {},
      el("p", {
        class: "card-sub",
        text: `${coverageNote} Store revision ${evidence.revision}; current claims are authoritative.`,
      }),
      miniTimeline(
        {
          coverage: detail.analysis.coverage,
          window: detail.node.window,
        },
        { range: evidence.range, uncovered: evidence.uncovered }
      ),
      evidenceList(evidence)
    );
    const right = el(
      "div",
      {},
      el("h3", { text: "Facts" }),
      el(
        "div",
        { class: "facts" },
        el("div", { class: "fact", text: `Range ${evidenceRange(evidence)}` }),
        el("div", {
          class: "fact",
          text: `Covered inside range: ${evidence.covered.length ? evidence.covered.map(([s, e]) => `[${s},${e})`).join(" ") : "none"}`,
        }),
        el("div", {
          class: "fact",
          text: `Uncovered inside range: ${evidence.uncovered.length ? evidence.uncovered.map(([s, e]) => `[${s},${e})`).join(" ") : "none"}`,
        }),
        el("div", {
          class: "fact",
          text: `Events overlapping: ${evidence.events.length}; retractions overlapping: ${evidence.retractions.length}`,
        })
      ),
      el("h3", { text: "Revise coverage" }),
      actionForms(detail)
    );
    $("#inspector-body").replaceChildren(left, right);
    scrollToCard(card);
  } catch (err) {
    $("#inspector-body").replaceChildren(
      el("p", { class: "card-sub", text: `Could not load evidence: ${err.message}` })
    );
  }
}

/* ------------------------------------------------------------------ comparison */

function renderComparison(comparison) {
  const body = $("#compare-body");
  const sub = $("#compare-sub");
  body.replaceChildren();
  if (!comparison) {
    sub.textContent = "Store at least two analysis runs to compare them.";
    body.append(
      el("p", {
        class: "card-sub",
        text: "Use “Store snapshot” before and after a restart, backfill or retraction; both runs are kept with their store revision.",
      })
    );
    return;
  }
  const a = comparison.run_a;
  const b = comparison.run_b;
  sub.textContent = `Run ${a.run_id} (rev ${a.revision}, ${a.note}) → run ${b.run_id} (rev ${b.revision}, ${b.note}).`;

  const table = el(
    "table",
    {},
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Group" }),
        el("th", { text: "State" }),
        el("th", { class: "num", text: "Covered A" }),
        el("th", { class: "num", text: "Covered B" }),
        el("th", { class: "num", text: "Δ covered" }),
        el("th", { class: "num", text: "Δ gap" }),
        el("th", { text: "Ratio A → B" }),
        el("th", { text: "Unknown nodes" })
      )
    )
  );
  const tbody = el("tbody");
  for (const row of comparison.groups) {
    const known = row.state === "known";
    const delta = row.covered_delta ?? null;
    const ratioText = known
      ? `${row.ratio_a_fraction ?? "—"} → ${row.ratio_b_fraction ?? "—"}${
          row.ratio_delta_fraction ? ` (${row.ratio_delta_fraction})` : ""
        }`
      : "unknown — not computed";
    tbody.append(
      el(
        "tr",
        {},
        el("td", { text: row.group }),
        el(
          "td",
          { class: known ? "" : "state-unknown" },
          known
            ? `${row.state_a} → ${row.state_b}`
            : row.unknown_reason || `${row.state_a} → ${row.state_b} (ratio withheld)`
        ),
        el("td", { class: "num", text: ticks(row.covered_a) }),
        el("td", { class: "num", text: ticks(row.covered_b) }),
        el("td", { class: `num ${deltaClass(delta)}`, text: signed(delta) }),
        el("td", { class: `num ${deltaClass(-(row.gap_delta ?? 0))}`, text: signed(row.gap_delta) }),
        el("td", { class: known ? "mono" : "state-unknown", text: ratioText }),
        el("td", {
          class: row.unknown_nodes && row.unknown_nodes.length ? "state-unknown" : "",
          text: row.unknown_nodes && row.unknown_nodes.length ? row.unknown_nodes.join(", ") : "none",
        })
      )
    );
  }
  table.append(tbody);
  body.append(table);

  if (comparison.nodes.length) {
    const detail = el("details", { class: "node-changes", open: "" });
    detail.append(el("summary", { text: `${comparison.nodes.length} node-level change(s)` }));
    const ntable = el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", { text: "Node" }),
          el("th", { text: "Group" }),
          el("th", { text: "Change" }),
          el("th", { class: "num", text: "Δ covered" }),
          el("th", { class: "num", text: "Δ gap" }),
          el("th", { text: "Ratio A → B" })
        )
      )
    );
    const nbody = el("tbody");
    for (const row of comparison.nodes) {
      if (row.change === "added" || row.change === "removed") {
        nbody.append(
          el(
            "tr",
            {},
            el("td", { text: row.node_id }),
            el("td", { text: "—" }),
            el("td", { text: row.change }),
            el("td", { class: "num", text: "—" }),
            el("td", { class: "num", text: "—" }),
            el("td", { text: "—" })
          )
        );
        continue;
      }
      nbody.append(
        el(
          "tr",
          {},
          el("td", { text: row.node_id }),
          el("td", { text: row.group }),
          el("td", { text: `${row.change} (${row.state_a} → ${row.state_b})` }),
          el("td", { class: `num ${deltaClass(row.covered_delta)}`, text: signed(row.covered_delta) }),
          el("td", { class: `num ${deltaClass(-row.gap_delta)}`, text: signed(row.gap_delta) }),
          el("td", {
            class: "mono",
            text: `${row.ratio_a_fraction ?? "—"} → ${row.ratio_b_fraction ?? "—"}`,
          })
        )
      );
    }
    ntable.append(nbody);
    detail.append(ntable);
    body.append(detail);
  }
}

function renderRuns() {
  const select = $("#run-b-select");
  const runs = state.data.runs;
  const previous = select.value;
  select.replaceChildren();
  if (!runs.length) {
    select.append(el("option", { value: "", text: "No stored runs yet" }));
    return;
  }
  for (const run of runs) {
    select.append(
      el("option", {
        value: String(run.run_id),
        text: `run ${run.run_id} · rev ${run.revision} · ${run.note}`,
      })
    );
  }
  if (previous && runs.some((r) => String(r.run_id) === previous)) {
    select.value = previous;
  }
}

/* ------------------------------------------------------------------ wiring */

function renderAll() {
  if (!state.data) return;
  $("#revision-chip").textContent = `rev ${state.data.revision}`;
  renderTiles();
  renderGroups();
  renderNodes();
  renderRuns();
  renderComparison(state.data.comparison);
}

function wire() {
  $("#refresh-btn").addEventListener("click", () =>
    refresh("Recomputing from the store…")
  );
  $("#theme-btn").addEventListener("click", () => {
    const root = document.documentElement;
    const current =
      root.dataset.theme ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    root.dataset.theme = current === "dark" ? "light" : "dark";
  });
  $("#snapshot-btn").addEventListener("click", async () => {
    const note = $("#run-note").value.trim() || "manual snapshot";
    setStatus("Storing analysis snapshot…");
    try {
      const result = await api("/api/analyze", {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      $("#run-note").value = "";
      await refresh(`Stored run ${result.run_id} at revision ${result.revision}.`);
    } catch (err) {
      setStatus(`Could not store snapshot: ${err.message}`, true);
    }
  });
  $("#compare-btn").addEventListener("click", async () => {
    const value = $("#run-b-select").value;
    if (!value) return;
    setStatus("Comparing stored runs…");
    try {
      const runs = state.data.runs;
      const latest = runs[0];
      const selected = Number(value);
      if (latest && latest.run_id === selected) {
        setStatus("The selected run is already the latest; store a new snapshot after a change.", true);
        return;
      }
      const result = await api(`/api/compare?a=${selected}&b=${latest.run_id}`);
      renderComparison(result);
      scrollToCard($("#compare-title"));
      setStatus(`Comparison: run ${selected} → run ${latest.run_id}.`);
    } catch (err) {
      setStatus(`Comparison failed: ${err.message}`, true);
    }
  });
  for (const [button, target] of [
    ["#groups-table-btn", "#groups-table"],
    ["#nodes-table-btn", "#nodes-table"],
  ]) {
    const btn = $(button);
    const table = $(target);
    if (!btn) continue;
    btn.addEventListener("click", () => {
      const showing = !table.hidden;
      table.hidden = showing;
      btn.setAttribute("aria-pressed", String(!showing));
      btn.textContent = showing ? "Table view" : "Chart view";
    });
  }
  $("#close-inspector").addEventListener("click", () => {
    $("#inspector-card").hidden = true;
    state.selectedNode = null;
    state.selectedGap = null;
    renderNodes();
  });
}

wire();
refresh();

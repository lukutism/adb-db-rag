/* Object Explorer — dashboard for an Android app's on-device SQLite. */
let NAV = 0, MY_NAV = 0;
// Views await the network. If the user navigates away meanwhile, the late render must not paint
// over the new view — it writes into a detached node instead, and its listeners bind to nothing.
function newNav() { MY_NAV = ++NAV; return MY_NAV; }
function viewEl(token) {
  return token === NAV ? document.getElementById("view") : document.createElement("div");
}
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = n => n == null ? "—" : Number(n).toLocaleString();
const bytes = n => n == null ? "—" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
const ago = ms => { if (!ms) return "—"; const s = (Date.now() - ms) / 1000; if (s < 0 || s > 3.2e7) return new Date(ms).toLocaleString();
  return s < 60 ? Math.round(s) + "s ago" : s < 3600 ? Math.round(s / 60) + "m ago" : s < 86400 ? Math.round(s / 3600) + "h ago" : Math.round(s / 86400) + "d ago"; };
const store = { get: (k, d) => { try { return JSON.parse(localStorage.getItem("ox:" + k)) ?? d; } catch { return d; } },
                set: (k, v) => { try { localStorage.setItem("ox:" + k, JSON.stringify(v)); } catch {} } };

let STATE = null, NAVQ = "";
let DENSITY = store.get("density", "compact");

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json();
  if (j && j.error) throw new Error(j.error);
  return j;
}
async function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}
function toast(msg, ms = 2200) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function copy(text) { navigator.clipboard?.writeText(String(text)).then(() => toast("Copied")); }

/* ------------------------------------------------------------------ chrome */

function applyTheme(t) {
  document.documentElement.dataset.theme = t === "auto" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : t;
  store.set("theme", t);
}
function renderChrome() {
  const s = STATE;
  $("#c-device").textContent = `${s.device.model || "device"} · ${s.device.serial || "?"}`;
  $("#c-tenant").textContent = s.tenants[0] || "(app private)";
  $("#c-backend").textContent = `${s.backend?.effective || "?"} · ${s.live ? "live" : "snapshot"}`;
  $("#side-stats").textContent = `${fmt(s.objects)} objects · ${fmt(s.edges.length)} edges · ${s.load_s}s`;
}

function renderNav() {
  const s = STATE, q = NAVQ.toLowerCase();
  const hit = n => !q || n.toLowerCase().includes(q);
  const pools = s.nodes.filter(n => n.rows > 0 && hit(n.name)).sort((a, b) => b.rows - a.rows);
  const empty = s.nodes.filter(n => !n.rows && hit(n.name)).sort((a, b) => a.name.localeCompare(b.name));
  const dbs = (s.databases || []).filter(d => hit(d.name));
  const here = location.hash;
  const item = (href, name, count, has, weak) =>
    `<div class="nav-item${here.startsWith(href) ? " sel" : ""}${has ? " has" : ""}${weak ? " weak" : ""}" data-href="${href}"`
    + (weak ? ` title="${weak} reference${weak > 1 ? "s" : ""} here do not all resolve"` : "") + `>`
    + `<span class="dot"></span><span class="nm">${esc(name)}</span>`
    + (count != null ? `<span class="ct">${fmt(count)}</span>` : "") + `</div>`;
  $("#nav").innerHTML =
    `<div class="nav-group">Dashboard</div>`
    + item("#/overview", "Overview")
    + item("#/erd", "Relationships", s.edges.length, true)
    + item("#/sql", "SQL console")
    + item("#/changes", "Changes")
    + `<div class="nav-group">Pools with data <span class="n">${pools.length}</span></div>`
    + pools.map(p => item(`#/pool/${encodeURIComponent(p.name)}`, p.name, p.rows, true, p.weak)).join("")
    + (dbs.length ? `<div class="nav-group">Databases <span class="n">${dbs.length}</span></div>`
        + dbs.map(d => item(`#/db/${encodeURIComponent(d.db)}`, d.name, d.tables, d.tables > 0)).join("") : "")
    + (() => { const r = store.get("recent", []).filter(x => !q || x.label.toLowerCase().includes(q));
        return r.length ? `<div class="nav-group">Recent <span class="n">${r.length}</span></div>`
          + r.map(x => item(x.href, x.label, null, true)).join("") : ""; })()
    + (empty.length ? `<div class="nav-group">Empty pools <span class="n">${empty.length}</span></div>`
        + empty.map(p => item(`#/pool/${encodeURIComponent(p.name)}`, p.name, 0, false)).join("") : "");
  $$("#nav .nav-item").forEach(el => el.addEventListener("click", () => { location.hash = el.dataset.href; }));
}

/* ------------------------------------------------------------------ grid */

function cellClass(v) {
  if (v == null || v === "") return "null";
  if (typeof v === "number") return "num";
  if (typeof v === "string" && /^[0-9a-f-]{16,}$|^\d+$/.test(v)) return "mono";
  return "";
}
function cellText(v) {
  if (v == null) return "null";
  if (Array.isArray(v)) return v.length ? v.map(x => String(x)).join(" · ") : "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
const TIME_COL = /lastchange|timestamp|_at$|time$|date$/i;
// Credentials sit in these databases in the clear. Mask them by default and make revealing a
// deliberate act, so a screen-share or a screenshot does not leak a session token.
// Whole words only: matching a substring would hide "Passes", "Bypass" and "CompassBearing" too.
const SECRET_WORDS = new Set(["password","passwd","passphrase","secret","token","apikey","credential",
  "credentials","session","sessionid","jwt","bearer","signature","privatekey","auth","accesskey",
  "refreshtoken","clientsecret"]);
const NAME_WORDS = /[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+/g;
let SHOW_SECRETS = store.get("secrets", false);
function isSecret(name) {
  if (typeof name !== "string" || !name) return false;
  const w = (name.match(NAME_WORDS) || []).map(x => x.toLowerCase());
  if (w.some(x => SECRET_WORDS.has(x))) return true;
  return w.some((x, i) => i < w.length - 1 && SECRET_WORDS.has(x + w[i + 1]));
}
function maskCell(col, text) {
  if (SHOW_SECRETS || !isSecret(col) || text == null || text === "" || text === "null") return null;
  return `<span class="secret" data-v="${esc(text)}" title="Click to reveal">${"•".repeat(Math.min(12, Math.max(6, String(text).length)))}</span>`;
}
function gridHTML(columns, rows, opts = {}) {
  const { sort, dir, sortable = true, rowKey, widthKey, sortableCols } = opts;
  const canSort = c => sortable && (!sortableCols || sortableCols.includes(c));
  const widths = widthKey ? store.get("w:" + widthKey, {}) : {};
  const cg = `<colgroup>` + columns.map(c => `<col${widths[c] ? ` style="width:${widths[c]}px"` : ""}>`).join("") + `</colgroup>`;
  const head = columns.map(c =>
    `<th><div class="th${canSort(c) ? "" : " nosort"}" data-col="${esc(c)}"${canSort(c) ? "" : ' title="not sortable"'}>${esc(c)}`
    + (sort === c ? `<span class="ar">${dir === "desc" ? "▼" : "▲"}</span>` : "") + `</div>`
    + `<span class="colgrip" data-col="${esc(c)}"></span></th>`).join("");
  const body = rows.map((r, i) => {
    const cells = columns.map((c, ci) => {
      const v = Array.isArray(r) ? r[ci] : r[c];   // by position: two columns can share a name
      const raw = cellText(v);
      const isTime = TIME_COL.test(c) && typeof v === "number" && v > 1e11;
      const t = isTime ? ago(v) : raw;
      const masked = maskCell(c, raw);
      return `<td class="${c === "#key" ? "key" : isTime ? "mono" : cellClass(v)}"`
        + (masked ? "" : ` title="${esc(raw).slice(0, 400)}"`) + `>${masked || esc(t)}</td>`;
    }).join("");
    return `<tr data-i="${i}"${rowKey ? ` data-key="${esc(rowKey(r))}"` : ""}>${cells}</tr>`;
  }).join("");
  setTimeout(() => wireGrips(widthKey), 0);
  return `<table class="grid"${widthKey ? ` data-wk="${esc(widthKey)}"` : ""}>${cg}<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function wireGrips(widthKey) {
  $$(".colgrip").forEach(g => {
    if (g.dataset.wired) return;
    g.dataset.wired = "1";
    g.addEventListener("mousedown", e => {
      e.preventDefault(); e.stopPropagation();
      const table = g.closest("table"), th = g.closest("th");
      const i = [...th.parentNode.children].indexOf(th);
      const col = table.querySelector("colgroup").children[i];
      const startX = e.clientX, startW = th.getBoundingClientRect().width;
      const move = ev => { col.style.width = Math.max(56, startW + ev.clientX - startX) + "px"; };
      const up = () => {
        window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
        const wk = table.dataset.wk || widthKey;
        if (wk) { const w = store.get("w:" + wk, {}); w[g.dataset.col] = parseInt(col.style.width); store.set("w:" + wk, w); }
      };
      window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
    });
  });
}

/* ------------------------------------------------------------------ views */

const views = {};

views.overview = async () => {
  const myNav = MY_NAV;
  const d = await api("/api/overview");
  const s = STATE;
  const rate = d.link_total ? (1 - d.unresolved_total / d.link_total) : 1;
  const card = (k, v, sub, cls = "", href = "") =>
    `<div class="card ${cls}${href ? " go" : ""}"${href ? ` data-href="${href}"` : ""}>`
    + `<div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub || ""}</div></div>`;
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>Overview</h1><span class="meta">${esc(s.package)} · ${esc(s.tenants[0] || "app private")}</span></div>`
    + `<div class="vbody pad">`
    + `<div class="cards">`
    + card("Objects", fmt(d.objects), `${fmt(d.pools_with_data)} of ${fmt(d.pools_total)} pools`)
    + card("References", fmt(d.link_total), `${fmt(d.edges)} distinct edges`, "", "#/erd")
    + card("Resolve rate", (rate * 100).toFixed(1) + "%", `${fmt(d.unresolved_total)} unresolved`, rate < 1 ? "warn" : "ok", "#/erd")
    + card("Titled", fmt(d.titled), `${(100 * d.titled / Math.max(1, d.objects)).toFixed(0)}% have a label`)
    + card("Databases", fmt(d.databases.length), bytes(d.databases.reduce((a, x) => a + (x.bytes || 0), 0)) + " total")
    + `</div>`
    + `<div class="cols2">`
    + `<div class="panel"><h2>Databases<span class="hint">size · tables</span></h2>`
      + gridHTML(["name", "tenant", "bytes", "tables", "indexes"],
          d.databases.map(x => ({ ...x, tenant: x.tenant || "—", bytes: bytes(x.bytes) })),
          { sortable: false, rowKey: r => r.db }) + `</div>`
    + `<div class="panel"><h2>Largest pools</h2>`
      + gridHTML(["pool", "rows"], d.top_pools, { sortable: false, rowKey: r => r.pool }) + `</div>`
    + `</div>`
    + `<div class="panel"><h2>Unresolved references<span class="hint">ids that point at nothing — usually a sync gap</span></h2>`
      + (d.dangling.length ? gridHTML(["from", "path", "to", "links", "resolved", "rate"], d.dangling, { sortable: false })
         : `<div class="empty">Every reference resolves to an existing object.</div>`) + `</div>`
    + `<div class="panel" id="idxpanel"></div>`
    + `<div class="panel"><h2>Recently changed</h2>`
      + gridHTML(["pool", "key", "title", "changed"],
          d.recent.map(r => ({ ...r, title: r.title || "—", changed: ago(r.lastChange) })),
          { sortable: false, rowKey: r => r.pool + "/" + r.key }) + `</div>`
    + `</div>`;
  try {
    const ix = await api("/api/indexes");
    const panel = document.getElementById("idxpanel");
    if (!panel) return;                       // navigated away while this was in flight
    panel.innerHTML = `<h2>Retrieval indexes<span class="hint">built by search() over the MCP tools</span></h2>`
      + (ix.indexes.length ? gridHTML(["pool", "chunk", "docs", "embedded", "embed_model", "age_s"],
          ix.indexes.map(x => ({ pool: x.pool || x.table || "—", chunk: x.chunk || "—", docs: x.docs ?? "—",
            embedded: x.embedded ? "yes" : "no", embed_model: x.embed_model || "—", age_s: Math.round(x.age_s) + "s" })),
          { sortable: false })
        : `<div class="empty">No indexes built yet. They appear once you use search() from Claude.</div>`);
  } catch { document.getElementById("idxpanel")?.classList.add("hidden"); }
  $$("#view .card.go").forEach(c => c.addEventListener("click", () => location.hash = c.dataset.href));
  $$("#view .panel .grid tbody tr").forEach(tr => tr.addEventListener("click", () => {
    const k = tr.dataset.key || "";
    if (k.includes("/")) location.hash = "#/object/" + k.split("/").map(encodeURIComponent).join("/");
    else if (k.startsWith("#")) {} else if (k) location.hash = (STATE.databases || []).some(x => x.db === k)
      ? "#/db/" + encodeURIComponent(k) : "#/pool/" + encodeURIComponent(k);
  }));
};

const SORTABLE = ["#key", "#title", "#lastChange", "#refs"];
views.pool = async (name) => {
  const myNav = MY_NAV;
  const node = STATE.nodes.find(n => n.name === name);
  const key = "pool:" + name;
  const st = Object.assign({ offset: 0, limit: 100, sort: "#lastChange", dir: "desc", q: "", columns: null, filters: {} }, store.get(key, {}));
  if (!st.sort) st.sort = "#lastChange";
  const render = async () => {
    const qs = new URLSearchParams({ name, offset: st.offset, limit: st.limit, sort: st.sort, dir: st.dir, q: st.q });
    if (st.columns) qs.set("columns", JSON.stringify(st.columns));
    if (st.filters && Object.keys(st.filters).length) qs.set("filters", JSON.stringify(st.filters));
    const d = await api("/api/pool?" + qs);
    store.set(key, st);
    const from = d.matched ? st.offset + 1 : 0, to = Math.min(st.offset + st.limit, d.matched);
    viewEl(myNav).innerHTML =
      `<div class="vhead"><h1>${esc(name)}</h1>`
      + `<span class="meta">${fmt(d.total)} objects${d.matched !== d.total ? ` · ${fmt(d.matched)} matching` : ""}</span>`
      + `<span class="grow"></span>`
      + `<a href="#/fields/${encodeURIComponent(name)}">fields</a>`
      + (node && node.table ? `<a href="#/structure/${encodeURIComponent(node.db)}/${encodeURIComponent(node.table)}">table</a>` : "")
      + `<a href="#/erd" data-pool="${esc(name)}">relationships</a></div>`
      + `<div class="toolbar">`
      + `<input type="search" id="pq" placeholder="Filter rows…" value="${esc(st.q)}" style="min-width:210px">`
      + `<span class="sp"></span><button id="cols">Columns</button><button id="filt">Filter</button>`
      + `<button id="exp-csv">CSV</button><button id="exp-json">JSON</button>`
      + `<button id="tosql">SQL</button>`
      + `<span class="grow"></span>`
      + `<select id="lim">${[25, 50, 100, 250, 500].map(n => `<option${n === st.limit ? " selected" : ""}>${n}</option>`).join("")}</select>`
      + `</div>`
      + (Object.keys(st.filters).length ? `<div class="fbar">` + Object.entries(st.filters).map(([path, f]) =>
          `<span class="fchip" data-path="${esc(path)}">${esc(path)} <b>${esc(typeof f === "object" ? f.op : "=")}</b> `
          + `${esc(typeof f === "object" ? (f.value ?? "") : f)} <span class="x">✕</span></span>`).join("")
          + `<button class="ghost" id="fclear">clear all</button></div>` : "")
      + `<div id="bulk" class="bulkbar hidden"><span class="n"></span>`
      + `<button class="ghost keys">Copy keys</button><button class="ghost json">Copy JSON</button>`
      + `<button class="ghost clear">Clear</button></div>`
      + `<div class="vbody gridwrap">${d.rows.length ? gridHTML(d.columns, d.rows, { sort: st.sort, dir: st.dir, rowKey: r => r["#key"],
            widthKey: "pool:" + name })
          : `<div class="empty">No rows${st.q ? " match that filter" : ""}.</div>`}</div>`
      + `<div class="pager"><button id="prev"${st.offset ? "" : " disabled"}>‹ Prev</button>`
      + `<span>${fmt(from)}–${fmt(to)} of ${fmt(d.matched)}</span>`
      + `<button id="next"${to >= d.matched ? " disabled" : ""}>Next ›</button>`
      + `<span class="grow"></span><span>sorted by ${esc(st.sort)} ${st.dir}</span></div>`;

    $$("#view .grid th .th:not(.nosort)").forEach(th => th.addEventListener("click", () => {
      const c = th.dataset.col;
      // keep the column name exactly as shown, so the arrow matches and a second click flips
      // direction; the server strips the leading '#'
      if (c === st.sort) st.dir = st.dir === "desc" ? "asc" : "desc";
      else { st.sort = c; st.dir = c === "#lastChange" || c === "#refs" ? "desc" : "asc"; }
      st.offset = 0; render();
    }));
    const sel = () => $$("#view .grid tbody tr.sel").map(x => x.dataset.key);
    const syncBulk = () => {
      const n = sel().length;
      const bar = document.getElementById("bulk");
      if (!bar) return;
      bar.classList.toggle("hidden", n < 2);
      if (n >= 2) bar.querySelector(".n").textContent = `${n} selected`;
    };
    $$("#view .grid tbody tr").forEach((tr, i) => tr.addEventListener("click", e => {
      const rows = $$("#view .grid tbody tr");
      if (e.shiftKey && render._anchor != null) {           // range, like any table tool
        const [a, b] = [render._anchor, i].sort((x, y) => x - y);
        rows.forEach((x, j) => x.classList.toggle("sel", j >= a && j <= b));
      } else if (e.metaKey || e.ctrlKey) {
        tr.classList.toggle("sel"); render._anchor = i;
      } else {
        rows.forEach(x => x.classList.remove("sel"));
        tr.classList.add("sel"); render._anchor = i;
        peek(name, tr.dataset.key);
      }
      syncBulk();
    }));
    syncBulk();
    const bulk = document.getElementById("bulk");
    if (bulk) {
      bulk.querySelector(".keys").addEventListener("click", () => copy(sel().join("\n")));
      bulk.querySelector(".json").addEventListener("click", async () => {
        const out = [];
        for (const k of sel()) {
          try { out.push(await api(`/api/object?pool=${encodeURIComponent(name)}&key=${encodeURIComponent(k)}`)); }
          catch { /* skip */ }
        }
        copy(JSON.stringify(out, null, 1));
      });
      bulk.querySelector(".clear").addEventListener("click", () => {
        $$("#view .grid tbody tr").forEach(x => x.classList.remove("sel")); syncBulk();
      });
    }
    $$("#view .fchip .x").forEach(x => x.addEventListener("click", e => {
      delete st.filters[e.target.closest(".fchip").dataset.path]; st.offset = 0; render();
    }));
    if ($("#fclear")) $("#fclear").addEventListener("click", () => { st.filters = {}; st.offset = 0; render(); });
    $("#filt").addEventListener("click", () => filterBuilder(name, st, render));
    $("#pq").addEventListener("input", e => { clearTimeout(render._t); render._t = setTimeout(() => { st.q = e.target.value; st.offset = 0; render(); }, 260); });
    $("#lim").addEventListener("change", e => { st.limit = +e.target.value; st.offset = 0; render(); });
    $("#prev").addEventListener("click", () => { st.offset = Math.max(0, st.offset - st.limit); render(); });
    $("#next").addEventListener("click", () => { st.offset += st.limit; render(); });
    $("#exp-csv").addEventListener("click", () => location.href = `/api/export?kind=pool&format=csv&${qs}`);
    $("#view .vhead").insertAdjacentHTML("beforeend", "");
    $("#exp-json").addEventListener("click", () => location.href = `/api/export?kind=pool&format=json&${qs}`);
    $("#cols").addEventListener("click", () => pickColumns(name, st, render));
    $("#tosql").addEventListener("click", () => {
      store.set("sql", { mode: "decoded", pool: name, db: node?.db || "", limit: 500,
        sql: `SELECT key, title, json_extract(json, '$.ExternalId') AS external\nFROM decoded\nORDER BY last_change DESC\nLIMIT 50` });
      location.hash = "#/sql";
    });
  };
  await render();
};

async function peek(pool, key) {
  $("#drawer-title").textContent = `${pool} · ${key}`;
  $("#drawer-body").innerHTML = `<div class="loading">Loading…</div>`;
  $("#drawer").classList.remove("hidden");
  try {
    const o = await api(`/api/object?pool=${encodeURIComponent(pool)}&key=${encodeURIComponent(key)}`);
    const chip = r => `<div class="ref${r.resolved === false ? " dead" : ""}"${r.resolved === false ? "" : ` data-pool="${esc(r.pool)}" data-key="${esc(r.key)}"`}>`
      + `<span class="p">${esc(r.field || r.path)}</span><span class="v">${esc(r.pool)}: ${esc(r.title || r.key)}</span></div>`;
    $("#drawer-body").innerHTML =
      `<button id="pk-open">Open full view →</button>`
      + `<div class="sec">${esc(o.title || "(no title)")}</div>`
      + (o.refs_out.length ? `<div class="sec">references</div>` + o.refs_out.map(chip).join("") : "")
      + (o.refs_in.length ? `<div class="sec">referenced by</div>` + o.refs_in.map(r =>
          `<div class="ref" data-pool="${esc(r.pool)}" data-key="${esc(r.key)}"><span class="v">${esc(r.pool)}: ${esc(r.title || r.key)}</span></div>`).join("") : "")
      + `<div class="sec">fields</div><table class="kv">`
      + o.fields.map(f => `<tr><td class="p">${esc(f.path)}</td><td class="v">${maskCell(f.path, f.text) || esc(f.text)}</td></tr>`).join("")
      + `</table>`;
    $("#pk-open").addEventListener("click", () => openObject(pool, key));
    $$("#drawer-body .ref[data-key]").forEach(r => r.addEventListener("click", () => openObject(r.dataset.pool, r.dataset.key)));
  } catch (e) { $("#drawer-body").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}

const OPS = ["eq", "ne", "contains", "in", "gt", "lt", "ge", "le", "exists", "missing"];
async function filterBuilder(name, st, render) {
  const d = await api("/api/fields?name=" + encodeURIComponent(name));
  const rows = Object.entries(st.filters).map(([path, f]) =>
    ({ path, op: typeof f === "object" ? f.op : "eq", value: typeof f === "object" ? (f.value ?? "") : f }));
  if (!rows.length) rows.push({ path: d.fields[0]?.path || "", op: "eq", value: "" });
  const line = (r, i) =>
    `<div class="frow" data-i="${i}">`
    + `<select class="fp">${d.fields.map(f => `<option value="${esc(f.path)}"${f.path === r.path ? " selected" : ""}>${esc(f.path)} (${(f.fill_rate * 100).toFixed(0)}%)</option>`).join("")}</select>`
    + `<select class="fo">${OPS.map(o => `<option${o === r.op ? " selected" : ""}>${o}</option>`).join("")}</select>`
    + `<input class="fv" value="${esc(r.value)}" placeholder="value">`
    + `<button class="ghost fx">✕</button></div>`;
  $("#drawer-title").textContent = "Filter · " + name;
  $("#drawer-body").innerHTML =
    `<div class="empty" style="padding:0 0 10px">Filters run on the decoded object, so nested paths work.
      Percentages show how many of the ${fmt(d.objects)} objects carry that path at all.</div>`
    + `<div id="frows">${rows.map(line).join("")}</div>`
    + `<div style="display:flex;gap:8px;margin-top:12px"><button id="fadd" class="ghost">+ condition</button>`
    + `<span class="grow"></span><button id="fapply">Apply</button></div>`;
  $("#drawer").classList.remove("hidden");
  const wire = () => $$("#frows .fx").forEach(b => b.addEventListener("click", e => { e.target.closest(".frow").remove(); }));
  wire();
  $("#fadd").addEventListener("click", () => { $("#frows").insertAdjacentHTML("beforeend", line({ path: d.fields[0]?.path || "", op: "eq", value: "" }, 99)); wire(); });
  $("#fapply").addEventListener("click", () => {
    const out = {};
    $$("#frows .frow").forEach(r => {
      const path = r.querySelector(".fp").value, op = r.querySelector(".fo").value, value = r.querySelector(".fv").value;
      if (path) out[path] = { op, value };
    });
    st.filters = out; st.offset = 0; $("#drawer").classList.add("hidden"); render();
  });
}

async function pickColumns(name, st, render) {
  const d = await api("/api/fields?name=" + encodeURIComponent(name));
  const cur = new Set(st.columns || d.fields.slice(0, 8).map(f => f.path));
  $("#drawer-title").textContent = "Columns · " + name;
  $("#drawer-body").innerHTML =
    `<div class="empty" style="padding:0 0 10px">Pick the decoded paths to show as columns. Fill rate is how many of the
     ${fmt(d.objects)} objects actually carry a value there.</div>`
    + d.fields.map(f => `<label style="display:flex;gap:9px;align-items:center;padding:4px 0;cursor:pointer">
        <input type="checkbox" value="${esc(f.path)}"${cur.has(f.path) ? " checked" : ""} style="width:auto">
        <span style="flex:1;font:12px var(--mono)">${esc(f.path)}</span>
        <span class="pill">${(f.fill_rate * 100).toFixed(0)}%</span>
        <span class="pill">${esc(f.kind)}</span></label>`).join("")
    + `<div style="display:flex;gap:8px;margin-top:14px"><button id="col-apply">Apply</button><button id="col-reset" class="ghost">Reset to defaults</button></div>`;
  $("#drawer").classList.remove("hidden");
  $("#col-apply").addEventListener("click", () => {
    st.columns = $$("#drawer-body input:checked").map(i => i.value);
    $("#drawer").classList.add("hidden"); render();
  });
  $("#col-reset").addEventListener("click", () => { st.columns = null; $("#drawer").classList.add("hidden"); render(); });
}

views.fields = async (name) => {
  const myNav = MY_NAV;
  const d = await api("/api/fields?name=" + encodeURIComponent(name));
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>${esc(name)} · fields</h1><span class="meta">${fmt(d.objects)} objects</span>`
    + `<span class="grow"></span><a href="#/pool/${encodeURIComponent(name)}">back to data</a></div>`
    + `<div class="vbody gridwrap">`
    + gridHTML(["path", "kind", "filled", "fill_rate", "example"],
        d.fields.map(f => ({ ...f, fill_rate: (f.fill_rate * 100).toFixed(0) + "%" })), { sortable: false })
    + `</div>`;
};

views.object = async (pool, key) => {
  const myNav = MY_NAV;
  remember("object", `${pool}/${key}`, `#/object/${encodeURIComponent(pool)}/${encodeURIComponent(key)}`);
  const o = await api(`/api/object?pool=${encodeURIComponent(pool)}&key=${encodeURIComponent(key)}`);
  const rel = await api(`/api/related?pool=${encodeURIComponent(pool)}&key=${encodeURIComponent(key)}&depth=2`);
  const chip = r => `<div class="ref${r.resolved === false ? " dead" : ""}"${r.resolved === false ? "" : ` data-pool="${esc(r.pool)}" data-key="${esc(r.key)}"`}>`
    + `<span class="p">${esc(r.field || r.path)}</span><span class="v">${esc(r.pool)}: ${esc(r.title || r.key)}</span>`
    + (r.resolved === false ? `<span class="pill warn">missing</span>` : "") + `</div>`;
  const hop2 = rel.nodes.filter(n => n.hop === 2);
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>${esc(o.title || o.key)}</h1>`
    + `<span class="crumbs"><a href="#/pool/${encodeURIComponent(pool)}">${esc(pool)}</a> › ${esc(o.key)}</span>`
    + `<span class="grow"></span><button class="ghost" id="json-btn">JSON</button>`
    + `<button class="ghost" id="copy-key">Copy key</button>`
    + `<button class="ghost" id="copy-json">Copy JSON</button></div>`
    + `<div class="vbody pad">`
    + `<div class="kv" style="margin-bottom:6px"><table class="kv">`
      + `<tr><td class="p">pool</td><td class="v">${esc(o.pool)}</td></tr>`
      + `<tr><td class="p">key</td><td class="v">${esc(o.key)}</td></tr>`
      + `<tr><td class="p">thingId</td><td class="v">${esc(o.thingId)}</td></tr>`
      + `<tr><td class="p">lastChange</td><td class="v">${esc(o.lastChange)} · ${ago(o.lastChange)}</td></tr>`
      + `<tr><td class="p">table</td><td class="v">${esc(o.table)}</td></tr></table></div>`
    + (o.refs_out.length ? `<div class="sec">references (${o.refs_out.length})</div>` + o.refs_out.map(chip).join("") : "")
    + (o.refs_in.length ? `<div class="sec">referenced by (${o.refs_in.length})</div>`
        + o.refs_in.map(r => `<div class="ref" data-pool="${esc(r.pool)}" data-key="${esc(r.key)}">`
          + `<span class="v">${esc(r.pool)}: ${esc(r.title || r.key)}</span><span class="p">${esc(r.path)}</span></div>`).join("") : "")
    + (hop2.length ? `<div class="sec">two hops away (${hop2.length})</div>` + hop2Groups(hop2) : "")
    + `<div class="sec">neighbourhood</div><div id="mini"></div>`
    + `<div class="sec">fields (${o.fields.length})</div>`
    + `<table class="kv">` + o.fields.map(f => `<tr><td class="p">${esc(f.path)}</td>`
        + `<td class="v">${maskCell(f.path, f.text) || esc(f.text)}</td></tr>`).join("") + `</table>`
    + `</div>`;
  $$("#view .ref[data-key]").forEach(r => r.addEventListener("click", () => openObject(r.dataset.pool, r.dataset.key)));
  drawMiniGraph(rel, pool, key);
  $("#json-btn").addEventListener("click", () => {
    $("#drawer-title").textContent = `${o.pool} · ${o.key} · raw decoded`;
    $("#drawer-body").innerHTML = `<pre style="margin:0;font:12px var(--mono);white-space:pre-wrap;word-break:break-word">`
      + esc(JSON.stringify(o.value, null, 2)) + `</pre>`;
    $("#drawer").classList.remove("hidden");
  });
  $("#copy-key").addEventListener("click", () => copy(o.key));
  $("#copy-json").addEventListener("click", () => copy(JSON.stringify(o.value, null, 1)));
};

function drawMiniGraph(rel, pool, key) {
  const el = document.getElementById("mini");
  if (!el || !rel.nodes.length) return;
  const root = `${pool}/${key}`;
  const ring1 = rel.nodes.filter(n => n.hop === 1);
  // the second ring can be dozens of objects; showing them all turns the map into a hairball, so
  // the closest few are drawn and the rest are counted in the caption
  const ring2all = rel.nodes.filter(n => n.hop === 2);
  const RING2_MAX = 7;
  const ring2 = ring2all.slice(0, RING2_MAX), hidden2 = ring2all.length - ring2.length;
  const rows = Math.max(ring1.length, ring2.length, 1);
  const W = 760, H = Math.max(140, 30 + rows * 40), cx = 90, cy = H / 2;
  const place = (list, x) => list.map((n, i) => ({ ...n, x,
    y: list.length === 1 ? cy : 26 + i * ((H - 52) / Math.max(1, list.length - 1)) }));
  const a = place(ring1, 330), b = place(ring2, 640);
  const pos = new Map([[root, { x: cx, y: cy }]]);
  a.concat(b).forEach(n => pos.set(`${n.pool}/${n.key}`, n));
  const line = (p, q, dim) => {
    const s = pos.get(p), t = pos.get(q);
    if (!s || !t) return "";
    const mx = (s.x + t.x) / 2;
    return `<path class="mini-edge${dim ? " dim" : ""}" d="M${s.x + 6},${s.y} C${mx},${s.y} ${mx},${t.y} ${t.x - 6},${t.y}"/>`;
  };
  const chip = (n, isRoot) => {
    const label = (n.title || n.key || "").toString();
    const w = Math.min(230, Math.max(90, label.length * 6.6 + 16));
    return `<g class="mini-node${isRoot ? " root" : ""}" data-pool="${esc(n.pool)}" data-key="${esc(n.key)}"`
      + ` transform="translate(${n.x},${n.y})"><rect x="0" y="-13" rx="6" width="${w}" height="26"/>`
      + `<text x="9" y="4">${esc(label.length > 32 ? label.slice(0, 31) + "…" : label)}</text>`
      + `<text class="p" x="9" y="-17">${esc(n.pool)}</text></g>`;
  };
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="mini">`
    + rel.links.filter(l => pos.has(l.from) && pos.has(l.to)).map(l => line(l.from, l.to, false)).join("")
    + chip({ ...rel.nodes[0], x: cx - 6, y: cy }, true)
    + a.map(n => chip(n)).join("") + b.map(n => chip(n)).join("")
    + `</svg>`
    + `<div class="mini-note">${ring1.length} directly connected`
    + (ring2all.length ? `, ${ring2all.length} two hops away${hidden2 > 0 ? ` (${hidden2} not drawn)` : ""}` : "")
    + ` — click any to open it</div>`;
  el.querySelectorAll(".mini-node[data-key]").forEach(g => g.addEventListener("click", () => {
    if (!g.classList.contains("root")) openObject(g.dataset.pool, g.dataset.key);
  }));
}

function hop2Groups(hop2) {
  const by = Object.create(null);
  hop2.forEach(n => (by[n.pool] = by[n.pool] || []).push(n));
  return Object.entries(by).sort((a, b) => b[1].length - a[1].length).map(([pool, list]) =>
    `<details class="hop"><summary><b>${esc(pool)}</b> <span class="pill">${list.length}</span></summary>`
    + list.map(n => `<div class="ref" data-pool="${esc(n.pool)}" data-key="${esc(n.key)}">`
        + `<span class="v">${esc(n.title || n.key)}</span></div>`).join("") + `</details>`).join("");
}

views.db = async (db) => {
  const myNav = MY_NAV;
  const d = await api("/api/schema?db=" + encodeURIComponent(db) + "&stats=0");
  const info = (STATE.databases || []).find(x => x.db === db) || {};
  const rows = d.tables.map(t => ({ table: t.name, rows: t.row_count, columns: t.columns.length,
                                    pool: t.pool || "—", fks: t.foreign_keys.length }))
                       .sort((a, b) => b.rows - a.rows);
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>${esc(info.name || db)}</h1>`
    + `<span class="meta">${fmt(d.tables.length)} tables · ${bytes(info.bytes)} · ${esc(d.backend || "")}</span></div>`
    + `<div class="toolbar"><span class="meta">${esc(db)}</span><span class="grow"></span>`
    + `<button id="sqlhere">Query this database</button></div>`
    + `<div class="vbody gridwrap">` + gridHTML(["table", "pool", "rows", "columns", "fks"], rows,
        { sortable: false, rowKey: r => r.table }) + `</div>`;
  $$("#view .grid tbody tr").forEach(tr => tr.addEventListener("click", () =>
    location.hash = `#/table/${encodeURIComponent(db)}/${encodeURIComponent(tr.dataset.key)}`));
  $("#sqlhere").addEventListener("click", () => { store.set("sql:db", db); location.hash = "#/sql"; });
};

views.structure = async (db, name) => {
  const myNav = MY_NAV;
  const d = await api("/api/schema?db=" + encodeURIComponent(db));
  const t = (d.tables || []).find(x => x.name === name);
  if (!t) { viewEl(myNav).innerHTML = `<div class="err">No table ${esc(name)} in this database.</div>`; return; }
  const cols = t.columns.map(c => {
    const filled = c.filled ? +String(c.filled).split("/")[0] : null;
    const total = c.filled ? +String(c.filled).split("/")[1] : t.row_count;
    return { column: c.name, type: c.type || "—", pk: c.pk ? "PK" : "", notnull: c.notnull ? "NOT NULL" : "",
             filled: c.filled || "—", fill: total ? Math.round(100 * (filled ?? 0) / total) + "%" : "—",
             distinct: c.distinct ?? "—", "default": c.default ?? "" };
  });
  const dead = cols.filter(c => c.fill === "0%").length;
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>${esc(name)}</h1><span class="meta">${fmt(t.row_count)} rows · ${t.columns.length} columns`
    + (t.pool ? ` · pool <b>${esc(t.pool)}</b>` : "") + `</span>`
    + `<span class="grow"></span><a href="#/table/${encodeURIComponent(db)}/${encodeURIComponent(name)}">data</a>`
    + `<a href="#/db/${encodeURIComponent(db)}">database</a></div>`
    + `<div class="vbody pad">`
    + (t.note ? `<div class="panel"><h2>Note</h2><div class="empty" style="padding:11px 13px">${esc(t.note)}</div></div>` : "")
    + (dead ? `<div class="panel"><h2>${dead} of ${t.columns.length} columns are always empty<span class="hint">promoted columns the app never writes — read the decoded object instead</span></h2></div>` : "")
    + `<div class="panel"><h2>Columns</h2>`
    + gridHTML(["column", "type", "pk", "notnull", "filled", "fill", "distinct", "default"], cols, { sortable: false }) + `</div>`
    + (t.foreign_keys.length ? `<div class="panel"><h2>Foreign keys</h2>`
        + gridHTML(["from", "to_table", "to"], t.foreign_keys, { sortable: false }) + `</div>` : "")
    + (d.indexes.filter(i => i.table === name).length ? `<div class="panel"><h2>Indexes</h2>`
        + gridHTML(["name", "sql"], d.indexes.filter(i => i.table === name), { sortable: false }) + `</div>` : "")
    + `<div class="panel"><h2>DDL</h2><pre style="margin:0;padding:12px 13px;font:12px var(--mono);white-space:pre-wrap;color:var(--ink-dim)">${esc(t.sql || "")}</pre></div>`
    + `</div>`;
};

views.table = async (db, name) => {
  const myNav = MY_NAV;
  const key = "table:" + db + ":" + name;
  const st = Object.assign({ offset: 0, limit: 100, order: null, dir: "asc" }, store.get(key, {}));
  const render = async () => {
    const qs = new URLSearchParams({ db, name, offset: st.offset, limit: st.limit, dir: st.dir });
    if (st.order) qs.set("order", st.order);
    const d = await api("/api/table?" + qs);
    store.set(key, st);
    const to = Math.min(st.offset + st.limit, d.total);
    viewEl(myNav).innerHTML =
      `<div class="vhead"><h1>${esc(name)}</h1><span class="meta">${fmt(d.total)} rows · ${esc(d.backend || "")}</span>`
      + `<span class="crumbs"><a href="#/db/${encodeURIComponent(db)}">${esc(db.split("/").pop())}</a></span></div>`
      + `<div class="toolbar"><a href="#/structure/${encodeURIComponent(db)}/${encodeURIComponent(name)}"><button>Structure</button></a>`
      + `<span class="sp"></span><button id="exp-csv">CSV</button><button id="exp-json">JSON</button>`
      + `<span class="grow"></span><select id="lim">${[25, 50, 100, 250, 500].map(n => `<option${n === st.limit ? " selected" : ""}>${n}</option>`).join("")}</select></div>`
      + `<div class="vbody gridwrap">${gridHTML(d.columns, d.rows, { sort: st.order, dir: st.dir, widthKey: "tbl:" + db + ":" + name })}</div>`
      + `<div class="pager"><button id="prev"${st.offset ? "" : " disabled"}>‹ Prev</button>`
      + `<span>${fmt(d.total ? st.offset + 1 : 0)}–${fmt(to)} of ${fmt(d.total)}</span>`
      + `<button id="next"${to >= d.total ? " disabled" : ""}>Next ›</button></div>`;
    $$("#view .grid th .th").forEach(th => th.addEventListener("click", () => {
      const c = th.dataset.col;
      if (c === st.order) st.dir = st.dir === "desc" ? "asc" : "desc"; else { st.order = c; st.dir = "asc"; }
      st.offset = 0; render();
    }));
    $$("#view .grid tbody tr").forEach((tr, i) => tr.addEventListener("click", () => {
      $$("#view .grid tbody tr").forEach(x => x.classList.remove("sel"));
      tr.classList.add("sel");
      $("#drawer-title").textContent = `${name} · row ${st.offset + i + 1}`;
      $("#drawer-body").innerHTML = `<table class="kv">` + d.columns.map((c, j) => {
        const raw = cellText(d.rows[i][j]);
        return `<tr><td class="p">${esc(c)}</td><td class="v">${maskCell(c, raw) || esc(raw)}</td></tr>`;
      }).join("") + `</table>`;
      $("#drawer").classList.remove("hidden");
    }));
    $("#lim").addEventListener("change", e => { st.limit = +e.target.value; st.offset = 0; render(); });
    $("#prev").addEventListener("click", () => { st.offset = Math.max(0, st.offset - st.limit); render(); });
    $("#next").addEventListener("click", () => { st.offset += st.limit; render(); });
    $("#exp-csv").addEventListener("click", () => location.href = `/api/export?kind=table&format=csv&db=${encodeURIComponent(db)}&name=${encodeURIComponent(name)}`);
    $("#exp-json").addEventListener("click", () => location.href = `/api/export?kind=table&format=json&db=${encodeURIComponent(db)}&name=${encodeURIComponent(name)}`);
  };
  await render();
};

views.sql = async () => {
  const myNav = MY_NAV;
  const dbs = STATE.databases || [];
  const pools = STATE.nodes.filter(n => n.rows).map(n => n.name).sort();
  const st = Object.assign({ mode: "raw", db: dbs[0]?.db || "", pool: pools[0] || "", sql: "", limit: 500 }, store.get("sql", {}));
  if (store.get("sql:db")) { st.db = store.get("sql:db"); store.set("sql:db", null); }
  const hist = store.get("sqlhist", []);
  const draw = (result, err, ms) => {
    $("#sqlres").innerHTML = err ? `<div class="err">${esc(err)}</div>`
      : !result ? `<div class="empty">Results appear here. ⌘/Ctrl+Enter runs the query.</div>`
      : `<div class="sqlfoot"><b>${fmt(result.row_count ?? result.rows.length)}</b> rows in ${ms ?? result.elapsed_ms}ms`
        + (result.truncated ? ` · <span class="pill warn">truncated at ${st.limit}</span>` : "")
        + (result.source ? ` · ${esc(result.source)}` : "")
        + ` · <a id="sql-copy">copy JSON</a> · <a id="sql-csv">copy CSV</a></div>`
        + `<div class="gridwrap" style="max-height:100%">${gridHTML(result.columns, result.rows, { sortable: false })}</div>`;
    if (result) {
      const rowsCsv = () => [result.columns.join(","), ...result.rows.map(r => r.map(c =>
        `"${String(c == null ? "" : typeof c === "object" ? JSON.stringify(c) : c).replace(/"/g, '""')}"`).join(","))].join("\n");
      $("#sql-copy")?.addEventListener("click", () => copy(JSON.stringify(result.rows, null, 1)));
      $("#sql-csv")?.addEventListener("click", () => copy(rowsCsv()));
    }
  };
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>SQL console</h1><span class="meta" id="sqlhint"></span></div>`
    + `<div class="toolbar">`
    + `<select id="mode"><option value="raw"${st.mode === "raw" ? " selected" : ""}>Raw database</option>`
      + `<option value="decoded"${st.mode === "decoded" ? " selected" : ""}>Decoded objects (JSON)</option></select>`
    + `<select id="target"></select>`
    + `<span class="sp"></span><button id="run">Run ⌘↵</button>`
    + `<select id="lim">${[100, 500, 2000].map(n => `<option${n === st.limit ? " selected" : ""}>${n}</option>`).join("")}</select>`
    + `<button class="ghost" id="explain">Explain</button>`
    + `<span class="grow"></span><button class="ghost" id="save-btn">Save</button>`
    + `<button class="ghost" id="saved-btn">Saved</button><button class="ghost" id="hist-btn">History</button></div>`
    + `<textarea id="sqltext" spellcheck="false" placeholder="SELECT …">${esc(st.sql)}</textarea>`
    + `<div class="hist hidden" id="hist"></div>`
    + `<div class="vbody" id="sqlres"></div>`;
  const target = $("#target"), hint = $("#sqlhint");
  // a database or pool saved in localStorage may no longer exist; without this the dropdown shows
  // the first entry while the query still runs against the stale target
  if (!dbs.some(d => d.db === st.db)) st.db = dbs[0]?.db || "";
  if (!pools.includes(st.pool)) st.pool = pools[0] || "";
  const fillTarget = () => {
    target.innerHTML = st.mode === "raw"
      ? dbs.map(d => `<option value="${esc(d.db)}"${d.db === st.db ? " selected" : ""}>${esc(d.name)}</option>`).join("")
      : pools.map(p => `<option value="${esc(p)}"${p === st.pool ? " selected" : ""}>${esc(p)}</option>`).join("");
    hint.textContent = st.mode === "raw"
      ? "read-only SQL on the device, through the runner"
      : "SQL over the decoded mirror: decoded(src_key,key,thing,last_change,title,json), paths, docs — json_each / json_extract work";
  };
  fillTarget(); draw(null);
  const run = async () => {
    const sql = $("#sqltext").value.trim();
    if (!sql) return;
    st.sql = sql; store.set("sql", st);
    $("#run").disabled = true; $("#sqlres").innerHTML = `<div class="loading">Running…</div>`;
    const t0 = performance.now();
    try {
      const r = st.mode === "raw" ? await post("/api/sql", { db: st.db, sql, limit: st.limit })
                                  : await post("/api/sql_decoded", { pool: st.pool, sql, limit: st.limit });
      draw(r, null, Math.round(performance.now() - t0));
      const h = [{ sql, mode: st.mode, target: st.mode === "raw" ? st.db : st.pool, at: Date.now() },
                 ...hist.filter(x => x.sql !== sql)].slice(0, 40);
      store.set("sqlhist", h); hist.length = 0; hist.push(...h);
    } catch (e) { draw(null, e.message); }
    finally { $("#run").disabled = false; }
  };
  $("#run").addEventListener("click", run);
  $("#explain").addEventListener("click", async () => {
    const sql = $("#sqltext").value.trim(); if (!sql) return;
    $("#sqlres").innerHTML = `<div class="loading">Planning…</div>`;
    try {
      const qs = new URLSearchParams({ sql });
      if (st.mode === "raw") qs.set("db", st.db); else qs.set("pool", st.pool);
      draw(await api("/api/explain?" + qs));
    } catch (e) { draw(null, e.message); }
  });
  const saved = () => store.get("sqlsaved", []);
  $("#save-btn").addEventListener("click", () => {
    const sql = $("#sqltext").value.trim(); if (!sql) return;
    const name = prompt("Name this query:", sql.slice(0, 40).replace(/\s+/g, " "));
    if (!name) return;
    store.set("sqlsaved", [{ name, sql, mode: st.mode, target: st.mode === "raw" ? st.db : st.pool }, ...saved().filter(x => x.name !== name)]);
    toast("Saved");
  });
  $("#saved-btn").addEventListener("click", () => {
    const h = $("#hist"), list = saved();
    h.classList.remove("hidden");
    h.innerHTML = list.length ? list.map((x, i) => `<div class="h" data-s="${i}"><span class="q"><b>${esc(x.name)}</b> — ${esc(x.sql)}</span>`
      + `<span class="t">${esc(x.target)} <a data-del="${i}">✕</a></span></div>`).join("")
      : `<div class="h">Nothing saved yet — write a query and press Save.</div>`;
    $$("#hist .h[data-s]").forEach(el => el.addEventListener("click", e => {
      if (e.target.dataset.del != null) { const l = saved(); l.splice(+e.target.dataset.del, 1); store.set("sqlsaved", l); $("#saved-btn").click(); return; }
      const x = list[+el.dataset.s];
      $("#sqltext").value = x.sql; st.mode = x.mode; $("#mode").value = x.mode; fillTarget(); h.classList.add("hidden");
    }));
  });
  $("#sqltext").addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); } });
  $("#mode").addEventListener("change", e => { st.mode = e.target.value; fillTarget(); store.set("sql", st); });
  target.addEventListener("change", e => { if (st.mode === "raw") st.db = e.target.value; else st.pool = e.target.value; store.set("sql", st); });
  $("#lim").addEventListener("change", e => { st.limit = +e.target.value; store.set("sql", st); });
  $("#hist-btn").addEventListener("click", () => {
    const h = $("#hist"); h.classList.toggle("hidden");
    h.innerHTML = hist.length ? hist.map((x, i) => `<div class="h" data-i="${i}"><span class="q">${esc(x.sql)}</span>`
      + `<span class="t">${esc(x.target)} · ${ago(x.at)}</span></div>`).join("")
      : `<div class="h">No queries yet.</div>`;
    $$("#hist .h[data-i]").forEach(el => el.addEventListener("click", () => {
      const x = hist[+el.dataset.i];
      $("#sqltext").value = x.sql; st.mode = x.mode; $("#mode").value = x.mode; fillTarget();
      h.classList.add("hidden");
    }));
  });
};

views.changes = async () => {
  const myNav = MY_NAV;
  const st = Object.assign({ auto: false, every: 5 }, store.get("changes", {}));
  const draw = (d) => {
    const s = d.summary;
    const row = c => {
      const diff = c.diff && (c.diff.changed.length || c.diff.added.length || c.diff.removed.length)
        ? `<table class="kv" style="margin-top:6px">`
          + c.diff.changed.map(x => `<tr><td class="p">${esc(x.path)}</td><td class="v"><span style="color:var(--bad)">${esc(x.before)}</span> → <span style="color:var(--ok)">${esc(x.after)}</span></td></tr>`).join("")
          + c.diff.added.map(x => `<tr><td class="p">${esc(x.path)}</td><td class="v"><span class="pill ok">added</span> ${esc(x.after)}</td></tr>`).join("")
          + c.diff.removed.map(x => `<tr><td class="p">${esc(x.path)}</td><td class="v"><span class="pill warn">removed</span> ${esc(x.before)}</td></tr>`).join("")
          + `</table>` : "";
      const badge = { added: "ok", removed: "warn", changed: "" }[c.change] || "";
      return `<div class="chg"><div class="chg-h" data-pool="${esc(c.pool)}" data-key="${esc(c.key)}">`
        + `<span class="pill ${badge}">${c.change}</span><b>${esc(c.pool)}</b>`
        + `<span class="mono">${esc(c.key)}</span><span style="color:var(--ink-dim)">${esc(c.title || "")}</span></div>${diff}</div>`;
    };
    $("#chg").innerHTML = !d.marked ? `<div class="empty">${esc(d.note)}</div>`
      : `<div class="cards" style="margin:14px">`
        + `<div class="card ok"><div class="k">Added</div><div class="v">${s.added}</div></div>`
        + `<div class="card"><div class="k">Changed</div><div class="v">${s.changed}</div></div>`
        + `<div class="card warn"><div class="k">Removed</div><div class="v">${s.removed}</div></div>`
        + `<div class="card"><div class="k">Unchanged</div><div class="v">${fmt(s.unchanged)}</div><div class="s">since ${ago(d.at * 1000)}</div></div></div>`
        + (d.changes.length ? `<div class="pad" style="padding-top:0">${d.changes.map(row).join("")}</div>`
           : `<div class="empty">Nothing has changed since the mark.</div>`);
    $$("#chg .chg-h").forEach(el => el.addEventListener("click", () => openObject(el.dataset.pool, el.dataset.key)));
  };
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>Changes</h1>`
    + `<span class="meta">mark the current state, act in the app, then refresh</span><span class="grow"></span>`
    + `<button id="mark">Mark now</button><button id="refresh">Refresh</button>`
    + `<button class="ghost${st.auto ? " on" : ""}" id="auto">Auto ${st.every}s</button></div>`
    + `<div class="vbody" id="chg"><div class="empty">Loading…</div></div>`;
  let inflight = false;
  const refresh = async (reload = false) => {
    if (inflight) return;                  // a slow poll must not queue more work on the device
    inflight = true;
    try { draw(await api("/api/changes?reload=" + (reload ? 1 : 0))); }
    catch (e) { const el = document.getElementById("chg"); if (el) el.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
    finally { inflight = false; }
  };
  $("#mark").addEventListener("click", async () => {
    const r = await post("/api/mark", { label: "dashboard" });
    toast(`Marked ${fmt(r.marked)} objects`); refresh();
  });
  $("#refresh").addEventListener("click", () => refresh(true));   // explicit: re-read the device
  $("#auto").addEventListener("click", () => {
    st.auto = !st.auto; store.set("changes", st); $("#auto").classList.toggle("on", st.auto);
    clearInterval(views.changes._t);
    if (st.auto) views.changes._t = setInterval(() => { if (location.hash.startsWith("#/changes")) refresh(); else clearInterval(views.changes._t); }, st.every * 1000);
  });
  if (st.auto) $("#auto").click(), $("#auto").click();
  await refresh();
};

/* ------------------------------------------------------------------ erd */

let ERD = Object.assign({ cam: { x: 0, y: 0, k: 1 }, sel: null, dataOnly: false, focus: false }, store.get("erd", {}));
ERD.cam = { x: 0, y: 0, k: 1 };
views.erd = async (pool) => {
  const myNav = MY_NAV;
  ERD.sel = pool || ERD.sel;
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>Relationships</h1>`
    + `<span class="meta">${fmt(STATE.edges.length)} verified edges · derived from the data, not the schema</span>`
    + `<span class="grow"></span><span class="meta" style="color:var(--warn)">╌ some ids don't resolve</span>`
    + `<button class="ghost${ERD.dataOnly ? " on" : ""}" id="dataonly" title="Hide pools that hold no objects">With data</button>`
    + `<button class="ghost${ERD.focus ? " on" : ""}" id="focus" title="Show only the selected pool and its neighbours">Focus</button>`
    + `<button class="ghost" id="fit">Fit</button><button class="ghost" id="svg-dl" title="Download this diagram">SVG</button></div>`
    + `<div id="graph"><svg id="svg"></svg><div id="loose"></div></div>`;
  $("#fit").addEventListener("click", () => { ERD.cam = { x: 0, y: 0, k: 1 }; applyCam(); });
  $("#svg-dl").addEventListener("click", () => {
    const svg = $("#svg").cloneNode(true);
    const cs = getComputedStyle(document.documentElement);
    const vars = ["--ink", "--line-2", "--panel-3", "--accent", "--warn", "--bg"]
      .map(v => `${v}:${cs.getPropertyValue(v).trim()}`).join(";");
    const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
    style.textContent = `svg{${vars};background:var(--bg)}`
      + `.node text{font:13px sans-serif;fill:var(--ink)}.node circle{fill:var(--panel-3);stroke:var(--line-2);stroke-width:1.5}`
      + `.node.sel circle{fill:var(--accent);stroke:var(--accent)}.edge{stroke:var(--line-2);fill:none;stroke-width:1.5}`
      + `.edge.partial{stroke:var(--warn);stroke-dasharray:4 3}.edge.hot{stroke:var(--accent);stroke-width:2}.node.dim,.edge.dim{opacity:.15}`;
    svg.insertBefore(style, svg.firstChild);
    svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const blob = new Blob([svg.outerHTML], { type: "image/svg+xml" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "relationships.svg"; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast("Diagram downloaded");
  });
  $("#dataonly").addEventListener("click", () => { ERD.dataOnly = !ERD.dataOnly; store.set("erd", ERD); views.erd(ERD.sel); });
  $("#focus").addEventListener("click", () => { ERD.focus = !ERD.focus; store.set("erd", ERD); views.erd(ERD.sel); });
  drawGraph();
};
function radius(n) { return 7 + Math.min(17, Math.sqrt(n.rows || 1) * 2.1); }
function labelW(n) { return String(n.name).length * 7.2; }
function layout(nodes, edges, w, h) {
  const idx = new Map(nodes.map(n => [n.name, n]));
  nodes.forEach((n, i) => { const a = 2 * Math.PI * i / nodes.length, r = Math.min(w, h) * 0.36;
    n.x = w / 2 + Math.cos(a) * r; n.y = h / 2 + Math.sin(a) * r; n.vx = n.vy = 0; });
  const links = edges.map(e => [idx.get(e.from), idx.get(e.to)]).filter(([a, b]) => a && b && a !== b);
  for (let it = 0; it < 420; it++) {
    const cool = 1 - it / 420;
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 0.01;
      const d = Math.sqrt(d2), f = 14000 / d2;
      a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (const [a, b] of links) {
      let dx = b.x - a.x, dy = b.y - a.y; const d = Math.hypot(dx, dy) || 0.01, f = (d - 165) * 0.022;
      a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
    }
    for (const n of nodes) {
      n.vx += (w / 2 - n.x) * 0.0022; n.vy += (h / 2 - n.y) * 0.0022;
      n.x += n.vx * cool * 0.5; n.y += n.vy * cool * 0.5; n.vx *= 0.86; n.vy *= 0.86;
      const m = 20 + labelW(n) / 2;
      n.x = Math.max(m, Math.min(w - m, n.x)); n.y = Math.max(26, Math.min(h - 26, n.y));
    }
  }
}
function separateLabels(nodes) {
  for (let it = 0; it < 140; it++) {
    let moved = false;
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      const need = labelW(a) / 2 + labelW(b) / 2 + 8, dx = b.x - a.x, dy = b.y - a.y;
      const ox = need - Math.abs(dx), oy = 22 - Math.abs(dy);
      if (ox > 0 && oy > 0) {
        const px = Math.min(ox, 11) / 2 * (dx < 0 ? -1 : 1), py = Math.min(oy, 7) / 2 * (dy < 0 ? -1 : 1);
        a.x -= px; b.x += px; a.y -= py; b.y += py; moved = true;
      }
    }
    if (!moved) break;
  }
}
function drawGraph() {
  const svg = $("#svg"); if (!svg) return;
  let nodes = STATE.nodes.filter(n => n.in > 0 || n.out > 0);
  const loose = STATE.nodes.filter(n => n.in === 0 && n.out === 0 && n.rows > 0);
  drawLoose(loose);
  if (ERD.dataOnly) nodes = nodes.filter(n => n.rows > 0);
  if (ERD.focus && ERD.sel) {
    const near = new Set([ERD.sel]);
    STATE.edges.forEach(e => { if (e.from === ERD.sel) near.add(e.to); if (e.to === ERD.sel) near.add(e.from); });
    nodes = nodes.filter(n => near.has(n.name));
  }
  const visible = new Set(nodes.map(n => n.name));
  const edges = STATE.edges.filter(e => visible.has(e.from) && visible.has(e.to));
  const pane = svg.getBoundingClientRect();
  const w = 1500, h = Math.max(620, Math.round(w * (pane.height || 700) / (pane.width || 900)));
  if (!nodes.length) { svg.innerHTML = ""; return; }
  layout(nodes, edges, w, h); separateLabels(nodes);
  const pos = new Map(nodes.map(n => [n.name, n]));
  let paths = "", circles = "";
  edges.forEach((e, i) => {
    const a = pos.get(e.from), b = pos.get(e.to); if (!a || !b || a === b) return;
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
    const bow = 22 * (i % 2 ? 1 : -1), cx = mx - dy / d * bow, cy = my + dx / d * bow;
    const rb = radius(b) + 6, ex = b.x - dx / d * rb, ey = b.y - dy / d * rb;
    paths += `<path class="edge${e.resolved_rate < 1 ? " partial" : ""}" data-from="${esc(e.from)}" data-to="${esc(e.to)}" marker-end="url(#a)" `
      + `d="M${a.x.toFixed(1)},${a.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}">`
      + `<title>${esc(e.from)}.${esc(e.path)} → ${esc(e.to)}${e.many ? " (many)" : ""}\n${e.links} links, ${Math.round(e.resolved_rate * 100)}% resolve</title></path>`;
  });
  nodes.forEach(n => {
    circles += `<g class="node" data-pool="${esc(n.name)}" transform="translate(${n.x.toFixed(1)},${n.y.toFixed(1)})">`
      + `<circle r="${radius(n).toFixed(1)}"><title>${esc(n.name)} — ${n.rows} objects, ${n.out} out, ${n.in} in</title></circle>`
      + `<text y="${(radius(n) + 15).toFixed(1)}" text-anchor="middle">${esc(n.name)}</text></g>`;
  });
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  nodes.forEach(n => { const hw = Math.max(radius(n), labelW(n) / 2), r = radius(n);
    x0 = Math.min(x0, n.x - hw); x1 = Math.max(x1, n.x + hw); y0 = Math.min(y0, n.y - r); y1 = Math.max(y1, n.y + r + 20); });
  const pad = 30;
  svg.innerHTML = `<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">`
    + `<path d="M0,1 L9,5 L0,9 z" fill="var(--line-2)"/></marker></defs><g id="cam">${paths}${circles}</g>`;
  svg.setAttribute("viewBox", `${(x0 - pad).toFixed(0)} ${(y0 - pad).toFixed(0)} ${(x1 - x0 + pad * 2).toFixed(0)} ${(y1 - y0 + pad * 2).toFixed(0)}`);
  $$("#svg .node").forEach(g => g.addEventListener("click", () => { ERD.sel = g.dataset.pool; highlight(); showPoolCard(g.dataset.pool); }));
  highlight(); installPanZoom();
}
function drawLoose(loose) {
  const el = $("#loose"); if (!el) return;
  if (!loose.length) { el.innerHTML = ""; el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.innerHTML = `<div class="lbl">${loose.length} pools with no references in or out</div><div class="chips">`
    + loose.sort((a, b) => b.rows - a.rows).map(n => `<span class="c" data-pool="${esc(n.name)}"><b>${esc(n.name)}</b><span class="n">${n.rows}</span></span>`).join("") + `</div>`;
  $$("#loose .c").forEach(c => c.addEventListener("click", () => location.hash = "#/pool/" + encodeURIComponent(c.dataset.pool)));
}
function highlight() {
  const sel = ERD.sel;
  $$(".node").forEach(g => {
    const on = !sel || g.dataset.pool === sel ||
      STATE.edges.some(e => (e.from === sel && e.to === g.dataset.pool) || (e.to === sel && e.from === g.dataset.pool));
    g.classList.toggle("sel", g.dataset.pool === sel); g.classList.toggle("dim", !!sel && !on);
  });
  $$(".edge").forEach(p => {
    const on = !sel || p.dataset.from === sel || p.dataset.to === sel;
    p.classList.toggle("hot", !!sel && on); p.classList.toggle("dim", !!sel && !on);
  });
  $$("#loose .c").forEach(c => c.classList.toggle("sel", c.dataset.pool === sel));
}
function applyCam() { const g = $("#cam"); if (g) g.setAttribute("transform", `translate(${ERD.cam.x},${ERD.cam.y}) scale(${ERD.cam.k})`); }
let _panHandlers = null;
function installPanZoom() {
  const svg = $("#svg"); let dragging = false, sx = 0, sy = 0;
  svg.onmousedown = e => { if (e.target.closest(".node")) return; dragging = true; sx = e.clientX - ERD.cam.x; sy = e.clientY - ERD.cam.y; svg.classList.add("drag"); };
  // drawGraph runs on every toggle, theme change and window resize; without removing the previous
  // pair, each redraw left two more window listeners pinning a detached <svg>
  if (_panHandlers) {
    window.removeEventListener("mouseup", _panHandlers.up);
    window.removeEventListener("mousemove", _panHandlers.move);
  }
  _panHandlers = {
    up: () => { dragging = false; svg.classList.remove("drag"); },
    move: e => { if (!dragging) return; ERD.cam.x = e.clientX - sx; ERD.cam.y = e.clientY - sy; applyCam(); },
  };
  window.addEventListener("mouseup", _panHandlers.up);
  window.addEventListener("mousemove", _panHandlers.move);
  svg.onwheel = e => { e.preventDefault(); const f = e.deltaY < 0 ? 1.12 : 0.89; ERD.cam.k = Math.max(0.3, Math.min(3, ERD.cam.k * f)); applyCam(); };
  applyCam();
}
function showPoolCard(pool) {
  const outs = STATE.edges.filter(e => e.from === pool), ins = STATE.edges.filter(e => e.to === pool);
  const n = STATE.nodes.find(x => x.name === pool) || {};
  $("#drawer-title").textContent = pool;
  $("#drawer-body").innerHTML =
    `<div class="sec">${fmt(n.rows)} objects</div>`
    + `<button id="dr-open">Browse data →</button>`
    + (outs.length ? `<div class="sec">references out</div>` + outs.map(e =>
        `<div class="ref" data-pool="${esc(e.to)}"><span class="p">${esc(e.path)}</span>→<span class="v">${esc(e.to)}</span>`
        + `<span class="pill">${e.many ? "many" : "one"}</span>`
        + (e.resolved_rate < 1 ? `<span class="pill warn">${Math.round(e.resolved_rate * 100)}%</span>` : "") + `</div>`).join("") : "")
    + (ins.length ? `<div class="sec">referenced by</div>` + ins.map(e =>
        `<div class="ref" data-pool="${esc(e.from)}"><span class="v">${esc(e.from)}</span><span class="p">${esc(e.path)}</span></div>`).join("") : "");
  $("#drawer").classList.remove("hidden");
  $("#dr-open").addEventListener("click", () => { $("#drawer").classList.add("hidden"); location.hash = "#/pool/" + encodeURIComponent(pool); });
  $$("#drawer-body .ref[data-pool]").forEach(r => r.addEventListener("click", () => { ERD.sel = r.dataset.pool; highlight(); showPoolCard(r.dataset.pool); }));
}

views.search = async (q) => {
  const myNav = MY_NAV;
  const d = await api("/api/find?q=" + encodeURIComponent(q));
  const byPool = Object.create(null);
  d.hits.forEach(h => (byPool[h.pool] = (byPool[h.pool] || 0) + 1));
  const chips = Object.entries(byPool).sort((a, b) => b[1] - a[1])
    .map(([p, n]) => `<span class="pill">${esc(p)} ${n}</span>`).join(" ");
  viewEl(myNav).innerHTML =
    `<div class="vhead"><h1>Search</h1><span class="meta">${fmt(d.total)} matches for “${esc(q)}”</span>`
    + `<span class="grow"></span><span style="display:flex;gap:5px;flex-wrap:wrap">${chips}</span></div>`
    + `<div class="vbody gridwrap">` + (d.hits.length
      ? gridHTML(["pool", "key", "title", "why", "snippet"], d.hits.map(h => ({ ...h, title: h.title || "—", snippet: h.snippet || "" })),
          { sortable: false, rowKey: r => r.pool + "/" + r.key })
      : `<div class="empty">Nothing matched. Search covers keys, titles and every decoded field value.</div>`) + `</div>`;
  $$("#view .grid tbody tr").forEach(tr => tr.addEventListener("click", () => {
    const [p, k] = tr.dataset.key.split("/"); openObject(p, k);
  }));
};

function remember(kind, label, href) {
  const list = store.get("recent", []).filter(x => x.href !== href);
  list.unshift({ kind, label, href, at: Date.now() });
  store.set("recent", list.slice(0, 12));
}

function openObject(pool, key) { location.hash = `#/object/${encodeURIComponent(pool)}/${encodeURIComponent(key)}`; }

/* ------------------------------------------------------------------ router */

async function route() {
  newNav();
  $("#drawer").classList.add("hidden");
  // one decode only: decodeURI turns %25 back into '%', and decodeURIComponent then throws
  // URIError on it — any key, pool or search term containing '%' used to wedge the router
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(x => {
    try { return decodeURIComponent(x); } catch { return x; }
  });
  const [v, a, b] = parts;
  renderNav();
  try {
    if (!v || v === "overview") await views.overview();
    else if (v === "pool") await views.pool(a);
    else if (v === "fields") await views.fields(a);
    else if (v === "object") await views.object(a, b);
    else if (v === "db") await views.db(a);
    else if (v === "table") await views.table(a, b);
    else if (v === "structure") await views.structure(a, b);
    else if (v === "changes") await views.changes();
    else if (v === "sql") await views.sql();
    else if (v === "erd") await views.erd(a);
    else if (v === "search") await views.search(parts.slice(1).join("/"));
    else viewEl(myNav).innerHTML = `<div class="err">Unknown view: ${esc(v)}</div>`;
  } catch (e) {
    const offline = /no device|not connected|adb|device selected|FileNotFound/i.test(e.message);
    viewEl(myNav).innerHTML = `<div class="err">${esc(e.message)}</div>`
      + (offline ? `<div class="empty">The emulator or phone is not answering. Everything already loaded still works —
          browsing, search and the relationship graph read from memory. Reload once the device is back.
          <div style="margin-top:12px"><button id="retry">Retry</button></div></div>` : "");
    if ($("#retry")) $("#retry").addEventListener("click", route);
  }
}

/* ------------------------------------------------------------------ palette */

function paletteItems() {
  const out = [{ g: "view", n: "Overview", h: "#/overview" }, { g: "view", n: "Relationships", h: "#/erd" },
               { g: "view", n: "SQL console", h: "#/sql" }];
  STATE.nodes.filter(n => n.rows).sort((a, b) => b.rows - a.rows)
    .forEach(p => out.push({ g: "pool", n: p.name, h: "#/pool/" + encodeURIComponent(p.name), c: p.rows }));
  (STATE.databases || []).forEach(d => out.push({ g: "database", n: d.name, h: "#/db/" + encodeURIComponent(d.db), c: d.tables }));
  STATE.nodes.filter(n => !n.rows).forEach(p => out.push({ g: "empty pool", n: p.name, h: "#/pool/" + encodeURIComponent(p.name), c: 0 }));
  return out;
}
let PAL = { items: [], i: 0 };
function openPalette() {
  PAL.items = paletteItems(); PAL.i = 0;
  $("#palette").classList.remove("hidden");
  $("#pal-input").value = ""; $("#pal-input").focus();
  drawPalette("");
}
function drawPalette(q) {
  const ql = q.toLowerCase();
  const list = PAL.items.filter(x => !ql || x.n.toLowerCase().includes(ql)).slice(0, 60);
  PAL.shown = list; PAL.i = Math.min(PAL.i, Math.max(0, list.length - 1));
  $("#pal-list").innerHTML = list.map((x, i) =>
    `<div class="pal-i${i === PAL.i ? " on" : ""}" data-i="${i}"><span class="g">${esc(x.g)}</span><span>${esc(x.n)}</span>`
    + (x.c != null ? `<span class="n">${fmt(x.c)}</span>` : "") + `</div>`).join("")
    || `<div class="pal-i"><span class="g">no match</span></div>`;
  $$("#pal-list .pal-i[data-i]").forEach(el => el.addEventListener("click", () => {
    location.hash = PAL.shown[+el.dataset.i].h; $("#palette").classList.add("hidden");
  }));
}

/* ------------------------------------------------------------------ boot */

function shortcuts() {
  $("#drawer-title").textContent = "Keyboard";
  $("#drawer-body").innerHTML = `<table class="kv">` + [
    ["⌘K / Ctrl+K", "command palette — jump to any pool, database or view"],
    ["/", "focus the global search"],
    ["?", "this list"],
    ["Esc", "close the palette or this panel"],
    ["⌘↵ / Ctrl+↵", "run the query, in the SQL console"],
    ["click a row", "peek at the object without leaving the grid"],
    ["click a masked value", "reveal that one secret"],
  ].map(([k, v]) => `<tr><td class="p">${esc(k)}</td><td class="v" style="font-family:var(--sans)">${esc(v)}</td></tr>`).join("") + `</table>`;
  $("#drawer").classList.remove("hidden");
}

function applyDensity(d) { DENSITY = d; document.documentElement.dataset.density = d; store.set("density", d); }

async function boot() {
  applyTheme(store.get("theme", "auto"));
  applyDensity(DENSITY);
  try { STATE = await api("/api/state"); }
  catch (e) { viewEl(myNav).innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  if (!STATE.loaded) { viewEl(myNav).innerHTML = `<div class="err">${esc(STATE.error || "load failed")}</div>`; return; }
  renderChrome();
  $("#secrets").classList.toggle("on", SHOW_SECRETS);
  if (!location.hash) location.hash = "#/overview";
  await route();
}

window.addEventListener("hashchange", route);
$("#navq").addEventListener("input", e => { NAVQ = e.target.value; renderNav(); });
$("#q").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.value.trim()) location.hash = "#/search/" + encodeURIComponent(e.target.value.trim()); });
$("#secrets").addEventListener("click", () => {
  SHOW_SECRETS = !SHOW_SECRETS; store.set("secrets", SHOW_SECRETS);
  $("#secrets").classList.toggle("on", SHOW_SECRETS);
  $("#secrets").title = SHOW_SECRETS ? "Secrets are visible — click to mask" : "Secrets are masked — click to reveal";
  route();
});
$("#density").addEventListener("click", () => {
  applyDensity(DENSITY === "compact" ? "roomy" : "compact");
  $("#density").title = `Row height: ${DENSITY}`;
});
$("#theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme;
  applyTheme(cur === "light" ? "dark" : "light");
  if ($("#svg")) drawGraph();
});
$("#reload").addEventListener("click", async () => {
  $("#reload").textContent = "…"; $("#reload").disabled = true;
  try { STATE = await post("/api/reload"); renderChrome(); await route(); toast("Reloaded from device"); }
  catch (e) { toast("Reload failed: " + e.message, 4000); }
  finally { $("#reload").textContent = "Reload"; $("#reload").disabled = false; }
});
$("#drawer-close").addEventListener("click", () => $("#drawer").classList.add("hidden"));
$("#palette-btn").addEventListener("click", openPalette);
$("#palette").addEventListener("click", e => { if (e.target.id === "palette") $("#palette").classList.add("hidden"); });
$("#pal-input").addEventListener("input", e => drawPalette(e.target.value));
$("#pal-input").addEventListener("keydown", e => {
  if (e.key === "ArrowDown") { PAL.i = Math.min(PAL.i + 1, (PAL.shown?.length || 1) - 1); drawPalette($("#pal-input").value); e.preventDefault(); }
  else if (e.key === "ArrowUp") { PAL.i = Math.max(0, PAL.i - 1); drawPalette($("#pal-input").value); e.preventDefault(); }
  else if (e.key === "Enter" && PAL.shown?.[PAL.i]) { location.hash = PAL.shown[PAL.i].h; $("#palette").classList.add("hidden"); }
});
document.addEventListener("click", e => {
  const el = e.target.closest(".secret");
  if (el) { el.outerHTML = `<span class="revealed">${esc(el.dataset.v)}</span>`; e.stopPropagation(); }
}, true);
window.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  else if (e.key === "Escape") { $("#palette").classList.add("hidden"); $("#drawer").classList.add("hidden"); }
  else if (e.key === "/" && !/input|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); }
  else if (e.key === "?" && !/input|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); shortcuts(); }
});
(() => {  // sidebar resize
  const r = $("#resizer"); let on = false;
  r.addEventListener("mousedown", () => { on = true; document.body.style.userSelect = "none"; });
  window.addEventListener("mouseup", () => { if (on) { on = false; document.body.style.userSelect = ""; store.set("side", parseInt(getComputedStyle(document.documentElement).getPropertyValue("--side"))); } });
  window.addEventListener("mousemove", e => { if (on) document.documentElement.style.setProperty("--side", Math.max(170, Math.min(460, e.clientX)) + "px"); });
  const saved = store.get("side"); if (saved) document.documentElement.style.setProperty("--side", saved + "px");
})();
let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if ($("#svg")) drawGraph(); }, 220); });
boot();

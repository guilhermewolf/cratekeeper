/* cratekeeper dashboard */

const state = {
  search: "",
  mik: "",
  rekordbox: "",
  dupes: "",
  added: "",
  issue: "",
  mismatch: "",
  sort: "artist",
  order: "asc",
  page: 1,
  perPage: 100,
};

const $ = (sel) => document.querySelector(sel);

/* ---- desktop bridge (present only inside cratekeeper.app) ---- */

const desktop = () => (window.pywebview && window.pywebview.api) || null;
window.addEventListener("pywebviewready", () => {
  document.body.classList.add("desktop");
});

function fmtBytes(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtNum(n) { return (n ?? 0).toLocaleString(); }

function pct(part, total) {
  if (!total) return "0%";
  return `${Math.round((part / total) * 100)}%`;
}

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

/* ---- stats tiles ---- */

async function loadStats() {
  const res = await fetch("/api/stats");
  const s = await res.json();

  $("#library-path").textContent = s.library_path;
  renderSetup(s);
  $("#stat-total").textContent = fmtNum(s.total);
  $("#stat-total-sub").textContent =
    s.missing ? `${fmtNum(s.missing)} file(s) missing from disk` : "all files present";

  const analyzed = s.mik.confirmed + s.mik.likely;
  $("#stat-mik").innerHTML =
    `${fmtNum(analyzed)} <span class="pct">${pct(analyzed, s.total)}</span>`;
  $("#stat-mik-sub").textContent =
    `${fmtNum(s.mik.confirmed)} confirmed · ${fmtNum(s.mik.likely)} likely · ${fmtNum(s.mik.none)} missing`;
  renderProportion("#mik-bar", s.total, [
    [s.mik.confirmed, "var(--series-2)"],
    [s.mik.likely, "var(--series-3)"],
  ]);

  const rb = s.rekordbox;
  $("#stat-rb").innerHTML =
    `${fmtNum(rb.in_rekordbox)} <span class="pct">${pct(rb.in_rekordbox, s.total)}</span>`;
  $("#stat-rb-sub").textContent = rb.available === false
    ? `rekordbox db unavailable: ${rb.error ?? "not found"}`
    : `${fmtNum(rb.not_in_rekordbox)} not imported · ${fmtNum(rb.not_analyzed)} imported but unanalyzed`
      + (rb.relocate ? ` · ${fmtNum(rb.relocate)} to relocate` : "")
      + (rb.key_mismatch || rb.bpm_mismatch
        ? ` · MIK disagrees: ${fmtNum(rb.key_mismatch)} key, ${fmtNum(rb.bpm_mismatch)} BPM` : "");
  renderProportion("#rb-bar", s.total, [[rb.in_rekordbox, "var(--series-1)"]]);

  const d = s.duplicates;
  $("#stat-dupes").textContent = fmtNum(d.exact_groups);
  $("#stat-dupes-sub").textContent =
    `${fmtNum(d.exact_files)} files · ${fmtBytes(d.wasted_bytes)} reclaimable · ${fmtNum(d.meta_groups)} same-song groups`;

  const mikBtn = $("#btn-mik");
  mikBtn.hidden = !s.mik.none;
  mikBtn.textContent = `Analyze ${fmtNum(Math.min(s.mik.none, 100))} in MIK`;
  const rbxmlBtn = $("#btn-rbxml");
  rbxmlBtn.hidden = !rb.not_in_rekordbox;
  rbxmlBtn.textContent = `rekordbox XML (${fmtNum(rb.not_in_rekordbox)})`;

  $("#stat-attention").textContent = fmtNum(s.needs_attention);
  $("#stat-attention-sub").textContent =
    `no MIK, not in rekordbox, unanalyzed or unreadable`;

  const scanning = s.scan.state !== "idle";
  $("#scan-indicator").hidden = !scanning;
  $("#btn-rescan").disabled = scanning;
  if (scanning) {
    const label = s.scan.state === "scanning" && s.scan.total
      ? `scanning ${fmtNum(s.scan.done)}/${fmtNum(s.scan.total)}`
      : s.scan.state === "hashing" ? "hashing duplicates…"
      : s.scan.state === "grouping" ? "grouping duplicates…" : "working…";
    $("#scan-indicator-text").textContent = label;
  }

  renderNew(s.new);

  const syncBits = [];
  if (s.last_scan) syncBits.push(`library scanned ${relTime(s.last_scan)}`);
  if (s.last_rekordbox_sync) syncBits.push(`rekordbox synced ${relTime(s.last_rekordbox_sync)}`);
  $("#footer-sync").textContent = syncBits.join(" · ");

  return scanning;
}

function renderProportion(sel, total, parts) {
  const el = $(sel);
  el.innerHTML = "";
  if (!total) return;
  for (const [value, color] of parts) {
    if (!value) continue;
    const span = document.createElement("span");
    span.style.width = `${(value / total) * 100}%`;
    span.style.background = color;
    el.appendChild(span);
  }
}

function relTime(ts) {
  const secs = Date.now() / 1000 - parseFloat(ts);
  if (secs < 90) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86400)} d ago`;
}

/* ---- new files since the last push ---- */

let newWindow = null;  // {since, until} of the list currently shown

function renderNew(n) {
  $("#new-files").hidden = !n.count;
  if (!n.count) return;
  if (!newWindow || newWindow.since !== n.since || newWindow.until !== n.until)
    newWindow = { since: n.since, until: n.until, mikSent: false };
  $("#new-title").textContent = `${fmtNum(n.count)} new file${n.count === 1 ? "" : "s"} since last push`;
  $("#new-sub").textContent =
    `${fmtNum(n.unanalyzed)} not analyzed in Mixed In Key · ${fmtNum(n.not_in_rekordbox)} not in rekordbox`;
}

function showNewError(msg) {
  const err = $("#new-error");
  err.textContent = msg;
  err.hidden = !msg;
}

async function ackNew(until) {
  await fetch("/api/new/ack", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ until }),
  });
  await loadStats();
  if (state.added === "new") loadTracks();
}

async function pushNew() {
  if (!newWindow) return;
  const { since, until } = newWindow;
  const qs = new URLSearchParams({ since, until });
  const btn = $("#btn-new-push");
  btn.disabled = true;
  showNewError("");
  try {
    const stats = await (await fetch("/api/new")).json();
    // a retry (after a cancelled XML save) must not reopen the same files in MIK
    if (stats.unanalyzed && !newWindow.mikSent) {
      // ponytail: API caps at 2000; a bigger drop gets acked with the rest unsent — loop if that happens
      qs.set("limit", 2000);
      const res = await fetch(`/api/actions/mik?${qs}`, { method: "POST" });
      const data = await res.json();
      if (!data.ok) { showNewError(`Mixed In Key: ${data.error}`); return; }
      newWindow.mikSent = true;
      qs.delete("limit");
    }
    if (stats.not_in_rekordbox) {
      const api = desktop();
      if (api) {
        const result = await api.save_rekordbox_xml(since, until);
        if (!result.ok) {
          if (!result.cancelled) showNewError(`rekordbox XML: ${result.error}`);
          return;  // keep the list so the push can be retried
        }
      } else {
        const a = document.createElement("a");
        a.href = `/api/export/rekordbox.xml?${qs}`;
        a.download = "cratekeeper-new-rekordbox.xml";
        a.click();
      }
    }
    await ackNew(until);
  } catch (err) {
    showNewError(`Push failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

$("#btn-new-push").addEventListener("click", pushNew);
$("#btn-new-ack").addEventListener("click", () => newWindow && ackNew(newWindow.until));
$("#btn-new-show").addEventListener("click", () => {
  document.querySelector('.chip[data-group="added"][data-value="new"]').click();
  document.querySelector(".filters").scrollIntoView({ behavior: "smooth" });
});

/* ---- library folder setup ---- */

let setupForced = false;  // user clicked the path to change it

function renderSetup(s) {
  const box = $("#setup");
  const needs = !s.configured || !s.library_exists;
  box.hidden = !(needs || setupForced);
  $("#btn-setup-cancel").hidden = needs;
  if (!s.configured) {
    $("#setup-title").textContent = "Where is your music?";
    $("#setup-sub").textContent =
      "Pick the folder cratekeeper should index. It only reads your files — nothing is moved or rewritten.";
  } else if (!s.library_exists) {
    $("#setup-title").textContent = "Music folder not found";
    $("#setup-sub").textContent =
      `${s.library_path} is missing (unmounted drive?). Reconnect it, or pick another folder.`;
  } else {
    $("#setup-title").textContent = "Change music folder";
    $("#setup-sub").textContent = `Currently indexing ${s.library_path}.`;
  }
  if (!$("#setup-path").value && s.configured) $("#setup-path").value = s.library_path;
}

async function setLibrary(path) {
  const err = $("#setup-error");
  err.hidden = true;
  const res = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ library_path: path }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    err.textContent = body.detail ?? `Could not use that folder (${res.status})`;
    err.hidden = false;
    return false;
  }
  setupForced = false;
  $("#setup").hidden = true;
  await loadStats();
  loadTracks();
  return true;
}

async function chooseFolderNative() {
  const api = desktop();
  if (!api) return;
  const path = await api.choose_library();
  if (path) await setLibrary(path);
}

$("#btn-choose-folder").addEventListener("click", chooseFolderNative);
$("#setup-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const path = $("#setup-path").value.trim();
  if (path) setLibrary(path);
});
$("#btn-setup-cancel").addEventListener("click", () => {
  setupForced = false;
  $("#setup").hidden = true;
});
$("#library-path").addEventListener("click", () => {
  if (desktop()) { chooseFolderNative(); return; }
  setupForced = true;
  loadStats();
  $("#setup-path").focus();
});

/* ---- tracks table ---- */

function mismatchMark(title) {
  return ` <span class="mismatch" title="${esc(title)}" aria-label="${esc(title)}">≠</span>`;
}

function tempoNote(t) {
  const ratio = parseFloat(t.bpm) / t.rb_bpm;
  return Math.abs(ratio - 2) < 0.02 || Math.abs(ratio - 0.5) < 0.005 ? " (half/double tempo)" : "";
}

function mikBadge(t) {
  if (t.mik_status === "confirmed")
    return `<span class="badge badge-good">Analyzed</span>`;
  if (t.mik_status === "likely")
    return `<span class="badge badge-warning">Likely</span>`;
  return `<span class="badge badge-critical">Not analyzed</span>`;
}

function rbBadge(t) {
  if (!t.rb_id)
    return `<span class="badge badge-critical">Not imported</span>`;
  if (t.rb_match === "dup_of")
    return `<span class="badge badge-warning" title="Same filename and size as a file rekordbox already has">Copy of imported</span>`;
  if (t.rb_analyzed === 0)
    return `<span class="badge badge-warning">Imported, unanalyzed</span>`;
  const via = t.rb_match === "name_size" ? " (moved)" : "";
  return `<span class="badge badge-good">Imported${via}</span>`;
}

let tracksSeq = 0;  // drops out-of-order responses (search debounce + scan polling)

async function loadTracks() {
  const params = new URLSearchParams({
    search: state.search,
    mik: state.mik,
    rekordbox: state.rekordbox,
    dupes: state.dupes,
    added: state.added,
    issue: state.issue,
    mismatch: state.mismatch,
    sort: state.sort,
    order: state.order,
    page: state.page,
    per_page: state.perPage,
  });
  const seq = ++tracksSeq;
  let data;
  try {
    const res = await fetch(`/api/tracks?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    if (seq === tracksSeq) $("#result-count").textContent = `Could not load tracks (${err.message})`;
    return;
  }
  if (seq !== tracksSeq) return;  // a newer request already rendered
  const lastPage = Math.max(1, Math.ceil(data.total / data.per_page));
  if (data.total && data.page > lastPage) {  // list shrank under us (ack, push, rescan)
    state.page = lastPage;
    return loadTracks();
  }

  pageIds = data.tracks.map((t) => t.id);
  const tbody = $("#tracks-body");
  if (!data.tracks.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No tracks match these filters.</td></tr>`;
  } else {
    let lastGroup = null;
    tbody.innerHTML = data.tracks.map((t) => {
      const grp = state.dupes === "exact" ? t.dup_group
        : state.dupes === "meta" ? t.meta_dup_group : null;
      const isLead = grp !== null && grp !== lastGroup;
      if (grp !== null) lastGroup = grp;
      const dupTag = (grp !== null && isLead ? `<span class="dup-tag">group ${grp}</span>` : "")
        + (grp === null ? "" : t.dup_keep
          ? ` <span class="badge badge-good" title="Suggested copy to keep: in rekordbox, analyzed, best quality">Keep</span>`
          : ` <span class="badge badge-warning" title="Another copy in this group is the better one to keep">Extra</span>`);
      const sel = selected.has(t.id);
      return `<tr class="${isLead ? "dup-lead" : ""}${sel ? " selected" : ""}" data-id="${t.id}" title="${esc(t.path)}">
        <td class="sel"><input type="checkbox" class="row-sel" data-id="${t.id}" ${sel ? "checked" : ""}
          aria-label="Select ${esc(t.artist ?? "")} ${esc(t.title ?? t.filename)}"></td>
        <td class="primary">${esc(t.artist ?? "—")}${dupTag}</td>
        <td>${esc(t.title ?? t.filename)}</td>
        <td class="num">${esc(t.key ?? "")}${t.key_mismatch ? mismatchMark(`rekordbox: ${t.rb_key}`) : ""}</td>
        <td class="num">${t.energy ?? ""}</td>
        <td class="num">${esc(t.bpm ?? "")}${t.bpm_mismatch ? mismatchMark(`rekordbox: ${t.rb_bpm}`) : ""}</td>
        <td>${mikBadge(t)}</td>
        <td>${rbBadge(t)}</td>
        <td>${esc((t.ext ?? "").replace(".", "").toUpperCase())}</td>
        <td class="num">${fmtBytes(t.size)}</td>
      </tr>`;
    }).join("");
  }

  const from = (data.page - 1) * data.per_page + 1;
  const to = Math.min(data.page * data.per_page, data.total);
  $("#result-count").textContent = data.total
    ? `${fmtNum(from)}–${fmtNum(to)} of ${fmtNum(data.total)} tracks` : "0 tracks";
  $("#page-label").textContent = `${data.page} / ${lastPage}`;
  $("#prev").disabled = data.page <= 1;
  $("#next").disabled = data.page >= lastPage;
  $("#dup-actions").hidden = !(state.dupes && data.total);
  const forget = $("#btn-forget");
  forget.hidden = !(state.issue === "missing" && data.total);
  forget.dataset.armed = "";
  forget.textContent = `Forget ${fmtNum(data.total)} missing file${data.total === 1 ? "" : "s"}`;
  renderSelection();
}

async function duplicateExtras() {
  const res = await fetch(`/api/duplicates/extras?kind=${state.dupes}`);
  return res.json();
}

$("#btn-dup-select").addEventListener("click", async () => {
  const { ids } = await duplicateExtras();
  ids.forEach((id) => selected.add(id));
  setSelStatus(`${fmtNum(ids.length)} extra cop${ids.length === 1 ? "y" : "ies"} selected`);
  renderSelection();
});

$("#btn-dup-copy").addEventListener("click", async () => {
  const btn = $("#btn-dup-copy");
  const { paths } = await duplicateExtras();
  try {
    await navigator.clipboard.writeText(paths.join("\n"));
    btn.textContent = `Copied ${fmtNum(paths.length)} paths ✓`;
  } catch {
    btn.textContent = "Copy failed";
  }
  setTimeout(() => { btn.textContent = "Copy extra paths"; }, 2500);
});

// Two-step instead of confirm(): native dialogs are unreliable in the webview.
$("#btn-forget").addEventListener("click", async () => {
  const btn = $("#btn-forget");
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1";
    btn.textContent = "Click again to forget them (files on disk are not touched)";
    setTimeout(() => { if (btn.dataset.armed) loadTracks(); }, 5000);
    return;
  }
  btn.disabled = true;
  try {
    await fetch("/api/missing/forget", { method: "POST" });
  } finally {
    btn.disabled = false;
    await loadStats();
    loadTracks();
  }
});

/* ---- selection (persists across pages and filters) ---- */

const MAX_SELECTION = 5000;  // matches the API limit
const selected = new Set();
let pageIds = [];

function renderSelection() {
  const n = selected.size;
  $("#selection-bar").hidden = !n;
  $("#sel-count").textContent = `${fmtNum(n)} selected`;
  const onPage = pageIds.filter((id) => selected.has(id)).length;
  const pageBox = $("#sel-page");
  pageBox.checked = pageIds.length > 0 && onPage === pageIds.length;
  pageBox.indeterminate = onPage > 0 && onPage < pageIds.length;
  document.querySelectorAll("#tracks-body .row-sel").forEach((box) => {
    const on = selected.has(Number(box.dataset.id));
    box.checked = on;
    box.closest("tr").classList.toggle("selected", on);
  });
}

function setSelStatus(msg) {
  $("#sel-status").textContent = msg;
}

function selectionIds() {
  if (selected.size > MAX_SELECTION) {
    setSelStatus(`Select at most ${fmtNum(MAX_SELECTION)} tracks at once`);
    return null;
  }
  return [...selected];
}

$("#sel-page").addEventListener("change", (e) => {
  for (const id of pageIds) {
    if (e.target.checked) selected.add(id); else selected.delete(id);
  }
  setSelStatus("");
  renderSelection();
});

$("#btn-sel-clear").addEventListener("click", () => {
  selected.clear();
  setSelStatus("");
  renderSelection();
});

$("#btn-sel-mik").addEventListener("click", async () => {
  const ids = selectionIds();
  if (!ids) return;
  const btn = $("#btn-sel-mik");
  btn.disabled = true;
  try {
    const res = await fetch("/api/actions/mik/selection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    const data = await res.json();
    setSelStatus(data.ok ? `Sent ${fmtNum(data.sent)} to ${data.app ?? "Mixed In Key"} ✓`
      : `Mixed In Key: ${data.error ?? res.status}`);
  } catch (err) {
    setSelStatus(`Mixed In Key: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
});

$("#btn-sel-xml").addEventListener("click", async () => {
  const ids = selectionIds();
  if (!ids) return;
  const api = desktop();
  if (api) {
    const result = await api.save_rekordbox_xml(null, null, ids);
    if (result.ok) setSelStatus("rekordbox XML saved ✓");
    else if (!result.cancelled) setSelStatus(`Export failed: ${result.error}`);
    return;
  }
  const a = document.createElement("a");
  a.href = `/api/export/rekordbox.xml?${new URLSearchParams({ ids: ids.join(",") })}`;
  a.download = "cratekeeper-selection.xml";
  a.click();
});

/* ---- track detail modal ---- */

function fmtDuration(secs) {
  if (!secs) return "";
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function pathRow(t, current = false) {
  const meta = [
    (t.ext ?? "").replace(".", "").toUpperCase(),
    fmtBytes(t.size),
    t.bitrate ? `${Math.round(t.bitrate / 1000)} kbps` : null,
    fmtDuration(t.duration),
    current ? null : (t.mik_status === "confirmed" ? "MIK ✓" : "no MIK"),
    current ? null : (t.rb_id ? "in rekordbox" : "not in rekordbox"),
  ].filter(Boolean).join(" · ");
  return `<div class="path-row ${current ? "current" : ""}">
    <code>${esc(t.path)}</code>
    <span class="path-meta">${meta}</span>
    <button class="btn btn-small copy-btn" data-path="${esc(t.path)}">Copy</button>
    <button class="btn btn-small reveal-btn desktop-only" data-path="${esc(t.path)}">Reveal</button>
  </div>`;
}

async function openDetail(id) {
  const res = await fetch(`/api/tracks/${id}`);
  if (!res.ok) return;
  const t = await res.json();

  $("#detail-title").textContent = [t.artist, t.title ?? t.filename]
    .filter(Boolean).join(" — ");

  const fields = [
    ["Album", t.album], ["Genre", t.genre],
    ["Key", t.key], ["Energy", t.energy], ["BPM", t.bpm],
    ["Duration", fmtDuration(t.duration)],
    ["Bitrate", t.bitrate ? `${Math.round(t.bitrate / 1000)} kbps` : null],
    ["Size", fmtBytes(t.size)],
    ["Format", (t.ext ?? "").replace(".", "").toUpperCase()],
    ["rekordbox key", t.rb_key && (t.key_mismatch ? `${t.rb_key} ≠ ${t.key}` : t.rb_key)],
    ["rekordbox BPM", t.rb_bpm && (t.bpm_mismatch ? `${t.rb_bpm} ≠ ${t.bpm}${tempoNote(t)}` : t.rb_bpm)],
    ["Tag error", t.tag_error],
  ].filter(([, v]) => v != null && v !== "");

  let html = `<p class="detail-sub">${mikBadge(t)} &nbsp; ${rbBadge(t)}</p>`;
  html += `<dl class="detail-grid">${fields.map(([k, v]) =>
    `<div><dt>${k}</dt><dd>${esc(String(v))}</dd></div>`).join("")}</dl>`;

  html += `<div class="detail-section"><h3>File</h3>${pathRow(t, true)}</div>`;

  if (t.exact_duplicates.length) {
    html += `<div class="detail-section">
      <h3>Exact duplicates (identical content)</h3>
      ${t.exact_duplicates.map((d) => pathRow(d)).join("")}</div>`;
  }
  const sameSong = t.same_song.filter(
    (d) => !t.exact_duplicates.some((e) => e.id === d.id));
  if (sameSong.length) {
    html += `<div class="detail-section">
      <h3>Same song, different file</h3>
      ${sameSong.map((d) => pathRow(d)).join("")}</div>`;
  }
  if (t.rekordbox && t.rekordbox.folder_path !== t.path) {
    const gone = t.rekordbox.file_exists === 0;
    html += `<div class="detail-section"><h3>rekordbox location${gone ? " — file gone" : ""}</h3>
      <div class="path-row"><code>${esc(t.rekordbox.folder_path)}</code></div>
      ${gone ? `<p class="detail-hint">rekordbox shows this track as missing. In rekordbox, right-click it →
        Relocate, and pick the file path above.</p>` : ""}</div>`;
  }

  $("#detail-body").innerHTML = html;
  $("#detail-overlay").hidden = false;

  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.path);
        btn.textContent = "Copied";
      } catch {
        btn.textContent = "Copy failed";
      }
      setTimeout(() => { btn.textContent = "Copy"; }, 1500);
    });
  });
  document.querySelectorAll(".reveal-btn").forEach((btn) => {
    btn.addEventListener("click", () => desktop()?.reveal(btn.dataset.path));
  });
}

function closeDetail() { $("#detail-overlay").hidden = true; }
$("#detail-close").addEventListener("click", closeDetail);
$("#detail-overlay").addEventListener("click", (e) => {
  if (e.target === $("#detail-overlay")) closeDetail();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDetail();
});

/* ---- events ---- */

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    state.page = 1;
    loadTracks();
  }, 250);
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const group = chip.dataset.group;
    document.querySelectorAll(`.chip[data-group="${group}"]`)
      .forEach((c) => c.classList.toggle("active", c === chip));
    state[group] = chip.dataset.value;
    state.page = 1;
    loadTracks();
  });
});

document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (state.sort === col) {
      state.order = state.order === "asc" ? "desc" : "asc";
    } else {
      state.sort = col;
      state.order = "asc";
    }
    document.querySelectorAll("th[data-sort]").forEach((h) => {
      h.classList.toggle("sorted", h === th);
      h.classList.toggle("desc", h === th && state.order === "desc");
    });
    state.page = 1;
    loadTracks();
  });
});

$("#tracks-body").addEventListener("click", (e) => {
  const box = e.target.closest(".row-sel");
  if (box) {
    const id = Number(box.dataset.id);
    if (box.checked) selected.add(id); else selected.delete(id);
    setSelStatus("");
    renderSelection();
    return;
  }
  if (e.target.closest("td.sel")) return;  // a near-miss on the checkbox
  const row = e.target.closest("tr[data-id]");
  if (row) openDetail(row.dataset.id);
});

$("#prev").addEventListener("click", () => { state.page--; loadTracks(); });
$("#next").addEventListener("click", () => { state.page++; loadTracks(); });

$("#btn-rescan").addEventListener("click", async () => {
  await fetch("/api/scan", { method: "POST" });
  loadStats();
});

$("#btn-mik").addEventListener("click", async () => {
  const btn = $("#btn-mik");
  btn.disabled = true;
  try {
    const res = await fetch("/api/actions/mik", { method: "POST" });
    const data = await res.json();
    btn.textContent = data.ok
      ? `Sent ${fmtNum(data.sent)} to ${data.app ?? "MIK"} ✓`
      : `MIK error: ${data.error}`;
  } finally {
    setTimeout(() => { btn.disabled = false; loadStats(); }, 4000);
  }
});

$("#btn-rbxml").addEventListener("click", async (e) => {
  const api = desktop();
  if (!api) return;  // browser: plain download link
  e.preventDefault();
  const btn = $("#btn-rbxml");
  const label = btn.textContent;
  const result = await api.save_rekordbox_xml();
  if (result.ok) {
    btn.textContent = "Saved ✓";
    setTimeout(() => { btn.textContent = label; }, 2500);
  } else if (!result.cancelled) {
    btn.textContent = `Export failed: ${result.error}`;
    setTimeout(() => { btn.textContent = label; }, 4000);
  }
});

document.querySelectorAll(".tile-clickable").forEach((tile) => {
  tile.addEventListener("click", () => {
    const group = tile.dataset.filter;
    const value = group === "mik" ? "none" : group === "rekordbox" ? "out" : "exact";
    const chip = document.querySelector(`.chip[data-group="${group}"][data-value="${value}"]`);
    if (chip) chip.click();
    document.querySelector(".filters").scrollIntoView({ behavior: "smooth" });
  });
});

/* ---- boot + poll ---- */

/* ---- updates (GitHub releases) ---- */

function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode etc. */ }
}

let updateInfo = null;
let updatePoll = null;

async function loadUpdate() {
  let u;
  try {
    u = await (await fetch("/api/update")).json();
  } catch {
    return;
  }
  updateInfo = u;
  $("#opt-check-updates").checked = u.check_updates;
  $("#opt-auto-update").checked = u.auto_update;
  $("#opt-auto-update-wrap").hidden = !u.can_install;
  $("#footer-version").textContent = `v${u.current}`;

  const dismissed = storageGet("cratekeeper.updateDismissed") === u.latest;
  const ready = u.state === "ready";
  $("#update-banner").hidden = !(u.available && (!dismissed || ready));
  $("#update-title").textContent = ready
    ? `cratekeeper ${u.latest} is ready — restart to finish updating`
    : `cratekeeper ${u.latest} is available (you have ${u.current})`;
  $("#update-sub").textContent = u.state === "downloading"
    ? "Downloading and verifying the update…"
    : ready ? (desktop() ? "Or keep working: it installs the next time you quit." : "Quit cratekeeper to finish.")
    : u.can_install ? "Install it now, or turn on automatic updates at the bottom of the page."
    : "Download it from the release page.";
  const notes = $("#update-notes");
  notes.hidden = !u.url;
  if (u.url) notes.href = u.url;
  $("#btn-update-install").hidden = !(u.can_install && ["idle", "error"].includes(u.state));
  $("#btn-update-restart").hidden = !(ready && desktop());
  $("#update-error").hidden = !u.error;
  $("#update-error").textContent = u.error ?? "";

  clearTimeout(updatePoll);
  if (u.state === "downloading") updatePoll = setTimeout(loadUpdate, 2000);
}

$("#btn-update-install").addEventListener("click", async () => {
  await fetch("/api/update/install", { method: "POST" });
  loadUpdate();
});
$("#btn-update-restart").addEventListener("click", async () => {
  const result = await desktop()?.restart_to_update();
  if (result && !result.ok) {
    $("#update-error").textContent = result.error;
    $("#update-error").hidden = false;
  }
});
$("#btn-update-later").addEventListener("click", () => {
  if (updateInfo?.latest) storageSet("cratekeeper.updateDismissed", updateInfo.latest);
  $("#update-banner").hidden = true;
});

async function saveUpdateSettings(changes) {
  await fetch("/api/update/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  if (changes.check_updates) await fetch("/api/update/check", { method: "POST" });
  loadUpdate();
}
$("#opt-check-updates").addEventListener("change", (e) =>
  saveUpdateSettings({ check_updates: e.target.checked }));
$("#opt-auto-update").addEventListener("change", (e) =>
  saveUpdateSettings({ auto_update: e.target.checked }));

async function refresh() {
  loadUpdate();
  try {
    const scanning = await loadStats();
    // Poll faster while a scan is running so the tiles track progress.
    setTimeout(refresh, scanning ? 2000 : 30000);
    if (scanning) loadTracks();
  } catch {
    setTimeout(refresh, 5000);
  }
}

refresh();
loadTracks();

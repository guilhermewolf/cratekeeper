/* liborg dashboard */

const state = {
  search: "",
  mik: "",
  rekordbox: "",
  dupes: "",
  sort: "artist",
  order: "asc",
  page: 1,
  perPage: 100,
};

const $ = (sel) => document.querySelector(sel);

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
    : `${fmtNum(rb.not_in_rekordbox)} not imported · ${fmtNum(rb.not_analyzed)} imported but unanalyzed`;
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

  const attention = s.mik.none + rb.not_in_rekordbox + rb.not_analyzed + s.tag_errors;
  $("#stat-attention").textContent = fmtNum(attention);
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

/* ---- tracks table ---- */

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
  if (t.rb_analyzed === 0)
    return `<span class="badge badge-warning">Imported, unanalyzed</span>`;
  const via = t.rb_match === "name_size" ? " (moved)" : "";
  return `<span class="badge badge-good">Imported${via}</span>`;
}

async function loadTracks() {
  const params = new URLSearchParams({
    search: state.search,
    mik: state.mik,
    rekordbox: state.rekordbox,
    dupes: state.dupes,
    sort: state.sort,
    order: state.order,
    page: state.page,
    per_page: state.perPage,
  });
  const res = await fetch(`/api/tracks?${params}`);
  const data = await res.json();

  const tbody = $("#tracks-body");
  if (!data.tracks.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">No tracks match these filters.</td></tr>`;
  } else {
    let lastGroup = null;
    tbody.innerHTML = data.tracks.map((t) => {
      const grp = state.dupes === "exact" ? t.dup_group
        : state.dupes === "meta" ? t.meta_dup_group : null;
      const isLead = grp !== null && grp !== lastGroup;
      if (grp !== null) lastGroup = grp;
      const dupTag = grp !== null && isLead ? `<span class="dup-tag">group ${grp}</span>` : "";
      return `<tr class="${isLead ? "dup-lead" : ""}" data-id="${t.id}" title="${esc(t.path)}">
        <td class="primary">${esc(t.artist ?? "—")}${dupTag}</td>
        <td>${esc(t.title ?? t.filename)}</td>
        <td class="num">${esc(t.key ?? "")}</td>
        <td class="num">${t.energy ?? ""}</td>
        <td class="num">${esc(t.bpm ?? "")}</td>
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
  const lastPage = Math.max(1, Math.ceil(data.total / data.per_page));
  $("#page-label").textContent = `${data.page} / ${lastPage}`;
  $("#prev").disabled = data.page <= 1;
  $("#next").disabled = data.page >= lastPage;
}

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
    ["rekordbox key", t.rb_key], ["rekordbox BPM", t.rb_bpm],
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
    html += `<div class="detail-section"><h3>rekordbox location</h3>
      <div class="path-row"><code>${esc(t.rekordbox.folder_path)}</code></div></div>`;
  }

  $("#detail-body").innerHTML = html;
  $("#detail-overlay").hidden = false;

  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(btn.dataset.path);
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = "Copy"; }, 1500);
    });
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

async function refresh() {
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

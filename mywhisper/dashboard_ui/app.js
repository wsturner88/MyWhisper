// MyWhisper dashboard — sidebar shell, meeting reader, dictation,
// import, settings. Boot data is injected as window.BOOT by dashboard.py.

"use strict";

const state = BOOT.state;
const MEETINGS = BOOT.meetings;     // [{title, when, filename, summary_md, notes_md, transcript_md, content}]
const DICTATIONS = BOOT.dictations; // [{timestamp, text}]

let currentView = "meetings";
let selectedMeeting = MEETINGS.length ? 0 : -1;
let readerTab = "notes";

function $(id) { return document.getElementById(id); }

function send(action, value) {
  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.bridge) {
    window.webkit.messageHandlers.bridge.postMessage({ action: action, value: value });
  }
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// ---- tiny markdown renderer (headings, bold, bullets, quotes, rules) ----

function mdToHtml(md) {
  const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = s => s
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let para = [], inList = false;
  const flushPara = () => {
    if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; }
  };
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (const raw of md.split("\n")) {
    let t = inline(esc(raw)).trim();
    if (!t) { flushPara(); closeList(); continue; }
    if (/^---+$/.test(t)) { flushPara(); closeList(); out.push("<hr>"); continue; }
    let m;
    if ((m = t.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); closeList();
      const lvl = Math.min(m[1].length + 1, 4);
      out.push("<h" + lvl + ">" + m[2] + "</h" + lvl + ">");
      continue;
    }
    if ((m = t.match(/^[-*•]\s+(.*)$/))) {
      flushPara();
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + m[1] + "</li>");
      continue;
    }
    if ((m = t.match(/^&gt;\s?(.*)$/))) {
      flushPara(); closeList();
      out.push("<blockquote>" + m[1] + "</blockquote>");
      continue;
    }
    if (/^_.+_$/.test(t)) t = "<i>" + t.slice(1, -1) + "</i>";
    para.push(t);
  }
  flushPara(); closeList();
  return out.join("\n");
}

function copyToClipboard(text, event) {
  if (event) event.stopPropagation();
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
  if (event && event.target && event.target.classList) {
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1400);
  }
}

// ---- navigation ----

function switchView(name) {
  currentView = name;
  document.querySelectorAll(".nav-item").forEach(n =>
    n.classList.toggle("active", n.dataset.view === name));
  document.querySelectorAll(".view").forEach(v =>
    v.classList.toggle("active", v.id === "view-" + name));
  if (name === "meetings") renderReader();
  if (name === "dictation") renderDictations();
}

function selectMeeting(i) {
  selectedMeeting = i;
  readerTab = "notes";
  editingPart = null;
  redoOpen = false;
  switchView("meetings");
  renderSidebarList();
  renderReader();
}

// ---- sidebar list + search ----

function matchesQuery(m, q) {
  return (m.title + " " + m.when + " " + m.content).toLowerCase().includes(q);
}

function renderSidebarList() {
  const q = ($("side-search").value || "").trim().toLowerCase();
  const list = $("side-list");
  list.innerHTML = "";
  let shown = 0;
  MEETINGS.forEach((m, i) => {
    if (q && !matchesQuery(m, q)) return;
    shown++;
    const row = document.createElement("div");
    row.className = "meeting-row" + (i === selectedMeeting ? " sel" : "");
    const t = document.createElement("div");
    t.className = "t";
    t.textContent = m.title;
    const w = document.createElement("div");
    w.className = "w";
    w.textContent = m.when;
    row.appendChild(t);
    row.appendChild(w);
    row.onclick = () => selectMeeting(i);
    list.appendChild(row);
  });
  if (!shown) {
    const d = document.createElement("div");
    d.className = "empty-side";
    d.textContent = MEETINGS.length ? "No meetings match your search." : "No meetings yet.";
    list.appendChild(d);
  }
}

// ---- meeting reader ----

let editingPart = null;   // null | "notes" | "mynotes"
let redoOpen = false;

function renderReader() {
  const el = $("view-meetings");
  if (selectedMeeting < 0 || !MEETINGS[selectedMeeting]) {
    el.innerHTML = '<div class="empty">No meetings yet.<br>Hit “Record a meeting” to capture your first one.</div>';
    return;
  }
  const m = MEETINGS[selectedMeeting];
  const tabs = [["notes", "Notes"], ["transcript", "Transcript"]];
  if ((m.notes_md || "").trim() || editingPart === "mynotes") tabs.push(["mynotes", "My Notes"]);
  if (!tabs.some(t => t[0] === readerTab)) readerTab = "notes";

  const raw = readerTab === "notes" ? m.summary_md
    : readerTab === "transcript" ? m.transcript_md
    : m.notes_md;
  const editable = readerTab !== "transcript";
  const isEditing = editingPart === readerTab;

  let bodyHtml;
  if (isEditing) {
    bodyHtml =
      '<textarea id="edit-area" style="min-height: 340px;"></textarea>' +
      '<div class="field-row" style="margin-top: 10px;">' +
        '<button class="btn primary" id="btn-save-edit">Save</button>' +
        '<button class="btn" id="btn-cancel-edit">Cancel</button>' +
        '<span class="field-hint">Plain text with markdown — ## headings, ' +
        '**bold**, - bullets.</span>' +
      "</div>";
  } else {
    bodyHtml = '<div class="md">' + mdToHtml(raw || "_(empty)_") + "</div>";
  }

  const redoHtml = !redoOpen ? "" :
    '<div class="settings-section" id="redo-panel" style="margin-bottom: 14px;">' +
      "<h3>Redo the AI summary</h3>" +
      '<div class="section-desc">Re-runs the AI on the saved transcript. ' +
        "Your typed notes and the transcript stay untouched — only the " +
        "summary is replaced.</div>" +
      '<div class="field"><label class="field-label">Meeting type</label>' +
        '<select id="redo-preset"></select></div>' +
      '<div class="field-row">' +
        '<button class="btn primary" id="btn-redo-go">Regenerate summary</button>' +
        '<button class="btn" id="btn-redo-cancel">Cancel</button>' +
      "</div></div>";

  el.innerHTML =
    '<div class="reader-top">' +
      '<div class="reader-meta">' + escapeHtml(m.when) +
        (m.when ? " · " : "") + escapeHtml(m.filename) + "</div>" +
      (editable && !isEditing ? '<button class="btn" id="btn-edit">Edit</button>' : "") +
      '<button class="btn" id="btn-copy">Copy</button>' +
      '<button class="btn" id="btn-redo">Redo summary</button>' +
      '<button class="btn" id="btn-open">Open file</button>' +
    "</div>" +
    '<h1 class="reader-title">' + escapeHtml(m.title) + "</h1>" +
    '<div class="seg" id="reader-tabs">' +
      tabs.map(([id, label]) =>
        '<button data-tab="' + id + '"' + (id === readerTab ? ' class="on"' : "") + ">" +
        label + "</button>").join("") +
    "</div>" +
    redoHtml +
    '<div class="status-line" id="reader-status"></div>' +
    bodyHtml;

  $("btn-copy").onclick = (e) => copyToClipboard(m.content, e);
  $("btn-open").onclick = () => send("open_meeting", m.filename);
  $("btn-redo").onclick = () => { redoOpen = !redoOpen; renderReader(); };
  el.querySelectorAll("#reader-tabs button").forEach(b => {
    b.onclick = () => { readerTab = b.dataset.tab; editingPart = null; renderReader(); };
  });

  if (editable && !isEditing) {
    $("btn-edit").onclick = () => { editingPart = readerTab; redoOpen = false; renderReader(); };
  }

  if (isEditing) {
    const area = $("edit-area");
    area.value = raw || "";
    area.focus();
    $("btn-save-edit").onclick = () => {
      $("btn-save-edit").disabled = true;
      send("save_meeting_part", {
        filename: m.filename,
        part: readerTab === "mynotes" ? "notes" : "summary",
        text: area.value
      });
    };
    $("btn-cancel-edit").onclick = () => { editingPart = null; renderReader(); };
  }

  if (redoOpen) {
    const sel = $("redo-preset");
    const addOpt = (p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.label;
      if (p.id === state.meeting_preset) o.selected = true;
      sel.appendChild(o);
    };
    state.builtin_presets.forEach(addOpt);
    (state.custom_presets || []).forEach(addOpt);
    $("btn-redo-go").onclick = () => {
      redoOpen = false;
      renderReader();
      const s = $("reader-status");
      s.className = "status-line";
      s.textContent = "↻ Re-summarizing…";
      send("resummarize_meeting", { filename: m.filename, preset: sel.value });
    };
    $("btn-redo-cancel").onclick = () => { redoOpen = false; renderReader(); };
  }
}

window.onSavePartDone = function (p) {
  const idx = MEETINGS.findIndex(m => m.filename === p.filename);
  if (p.ok && idx !== -1) {
    Object.assign(MEETINGS[idx], p.meeting || {});
    editingPart = null;
    if (idx === selectedMeeting) renderReader();
    renderSidebarList();
    const s = $("reader-status");
    if (s) { s.className = "status-line ok"; s.textContent = "✓ Saved"; setTimeout(renderReader, 2500); }
  } else {
    const s = $("reader-status");
    if (s) { s.className = "status-line err"; s.textContent = "✗ " + (p.error || "Save failed."); }
    const b = $("btn-save-edit");
    if (b) b.disabled = false;
  }
};

window.onResummarizeStage = function (p) {
  const s = $("reader-status");
  if (s) { s.className = "status-line"; s.textContent = "↻ " + (p.stage || "Working…"); }
};

window.onResummarizeDone = function (p) {
  const idx = MEETINGS.findIndex(m => m.filename === p.filename);
  if (p.ok && idx !== -1) {
    // The backend sends refreshed parsed parts along with raw content.
    Object.assign(MEETINGS[idx], p.meeting || {});
    if (idx === selectedMeeting) renderReader();
    renderSidebarList();
    const s = $("reader-status");
    if (s) { s.className = "status-line ok"; s.textContent = "✓ Summary updated"; setTimeout(renderReader, 4000); }
  } else if (!p.ok) {
    const s = $("reader-status");
    if (s) { s.className = "status-line err"; s.textContent = "✗ " + (p.error || "Re-summarize failed."); }
    const btn = $("btn-redo");
    if (btn) { btn.disabled = false; btn.textContent = "Redo summary"; }
  }
};

// ---- dictation view ----

function renderDictations() {
  const el = $("dict-list");
  el.innerHTML = "";
  if (!DICTATIONS.length) {
    el.innerHTML = '<div class="empty">No dictations yet. Hold the push-to-talk key and they\'ll appear here.</div>';
    return;
  }
  DICTATIONS.forEach((d) => {
    const row = document.createElement("div");
    row.className = "dict-row";
    const when = document.createElement("div");
    when.className = "when";
    when.textContent = d.timestamp;
    const txt = document.createElement("div");
    txt.className = "txt";
    txt.textContent = d.text;
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = "Copy";
    btn.onclick = (e) => copyToClipboard(d.text, e);
    row.appendChild(when);
    row.appendChild(txt);
    row.appendChild(btn);
    el.appendChild(row);
  });
}

// ---- import view ----

function runImport() {
  const text = $("import-text").value.trim();
  const status = $("import-status");
  if (!text) { status.textContent = "Paste some text first."; return; }
  $("import-btn").disabled = true;
  status.textContent = "Summarizing — this can take a minute…";
  send("import_transcript", {
    text: text,
    title: $("import-title").value.trim(),
    preset: $("import-preset").value
  });
}

window.onImportStage = function (p) { $("import-status").textContent = p.stage || ""; };

window.onImportDone = function (p) {
  $("import-btn").disabled = false;
  const status = $("import-status");
  if (p.ok) {
    status.textContent = "✓ Saved — adding to your meetings…";
    $("import-text").value = "";
    $("import-title").value = "";
  } else {
    status.textContent = "✗ " + (p.error || "Failed — see the log.");
  }
};

// ---- record button ----

function recordMeeting() {
  send("record_meeting", null);
  const b = $("record-btn-label");
  if (b) {
    b.textContent = "Starting…";
    setTimeout(() => { b.textContent = "Record a meeting"; }, 2500);
  }
}

// ---- settings (ported from the previous dashboard; same element ids) ----

function saveApiKey() {
  const v = $("api-key-input").value.trim();
  if (v && !v.includes("•")) send("set_api_key", v);
  $("api-key-input").value = "";
}

function saveCustomUrl() { send("set_custom_url", $("custom-url-input").value.trim()); }
function saveModel() { send("set_model", $("model-input").value.trim()); }

function saveVocab() {
  send("save_vocab", $("vocab-text").value);
  const s = $("vocab-status");
  s.textContent = "Saved.";
  setTimeout(() => { s.textContent = ""; }, 2000);
}

function pickFolder() { send("pick_folder", null); }

function testLLM() {
  const r = $("test-result");
  r.className = "";
  r.textContent = "Testing…";
  send("test_llm", null);
}

window.onTestResult = function (p) {
  const r = $("test-result");
  r.className = p.ok ? "ok" : "err";
  r.textContent = (p.ok ? "✓ Connected to " : "✗ ") + p.message;
};

window.onState = function (s) { renderState(s); };

function renderState(s) {
  $("folder-display").textContent = s.data_dir;

  const psel = $("provider-select");
  psel.innerHTML = "";
  s.llm_providers.forEach(p => {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.label;
    if (p.id === s.llm_provider) o.selected = true;
    psel.appendChild(o);
  });

  $("api-key-row").style.display = s.llm_needs_key ? "block" : "none";
  $("custom-url-row").style.display = s.llm_needs_url ? "block" : "none";

  if (s.llm_needs_url) {
    $("custom-url-input").value = s.custom_llm_url || "";
    const urlPill = $("url-status");
    urlPill.className = "pill-status " + (s.custom_llm_url ? "ok" : "missing");
    urlPill.textContent = s.custom_llm_url ? "Set" : "Not set";
  }

  const keyLabel = $("api-key-label");
  const pill = $("key-status");
  const keyHint = $("api-key-hint");
  if (s.llm_key_optional) {
    keyLabel.firstChild.textContent = "Auth Token (optional) ";
    keyHint.textContent = "Most local LLM servers do not need this. Set only if your server requires Bearer authentication.";
    pill.className = s.api_key_set ? "pill-status ok" : "";
    pill.textContent = s.api_key_set ? "Set" : "";
    $("api-key-input").placeholder = s.api_key_set ? (s.api_key_masked || "Saved") : "No auth needed for most servers";
  } else {
    keyLabel.firstChild.textContent = "API Key ";
    keyHint.textContent = "Stored securely in macOS Keychain — never written to disk in plain text.";
    pill.className = "pill-status " + (s.api_key_set ? "ok" : "missing");
    pill.textContent = s.api_key_set ? "Set" : "Not set";
    $("api-key-input").placeholder = s.api_key_set ? (s.api_key_masked || "Saved — paste a new key to replace") : "Paste your key…";
  }

  $("model-input").value = s.llm_model || "";
  syncModelDropdown(s.llm_model || "");

  const msel = $("mic-select");
  msel.innerHTML = "";
  const def = document.createElement("option");
  def.value = "";
  def.textContent = "System Default";
  if (!s.mic) def.selected = true;
  msel.appendChild(def);
  s.mic_devices.forEach(name => {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    if (name === s.mic) o.selected = true;
    msel.appendChild(o);
  });

  $("viz-select").value = s.visualization;
  $("autostart-toggle").checked = s.autostart;

  const srPill = $("sr-status");
  const srHint = $("sr-hint");
  if (s.screen_recording_granted) {
    srPill.className = "pill-status ok";
    srPill.textContent = "Granted";
    srHint.textContent = "Both sides of your calls will be captured. ✓";
  } else {
    srPill.className = "pill-status missing";
    srPill.textContent = "Not granted";
    srHint.textContent = "Click Request/Test to trigger the macOS prompt. If it doesn't appear, use Open System Settings (under Screen Recording).";
  }
  $("vocab-text").value = s.vocabulary;

  const bi = $("builtin-list");
  bi.innerHTML = "";
  s.builtin_presets.forEach(p => renderPresetRow(bi, p, s.meeting_preset, false));

  const cu = $("custom-list");
  cu.innerHTML = "";
  if (!s.custom_presets || s.custom_presets.length === 0) {
    const empty = document.createElement("div");
    empty.className = "field-hint";
    empty.style.padding = "8px 0";
    empty.textContent = "No custom presets yet. Add one below.";
    cu.appendChild(empty);
  } else {
    s.custom_presets.forEach(p => renderPresetRow(cu, p, s.meeting_preset, true));
  }

  // Import-panel meeting-type dropdown
  const isel = $("import-preset");
  if (isel) {
    const prev = isel.value;
    isel.innerHTML = "";
    const addOpt = (p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.label;
      if (p.id === (prev || s.meeting_preset)) o.selected = true;
      isel.appendChild(o);
    };
    s.builtin_presets.forEach(addOpt);
    (s.custom_presets || []).forEach(addOpt);
  }

  const sb = $("starter-buttons");
  sb.innerHTML = "";
  (s.starter_presets || []).forEach(starter => {
    const btn = document.createElement("button");
    btn.className = "starter-btn";
    btn.textContent = "+ " + starter.label;
    btn.onclick = () => send("add_custom_preset", { label: starter.label, focus: starter.focus });
    sb.appendChild(btn);
  });
}

function renderPresetRow(container, preset, selectedId, editable) {
  const row = document.createElement("div");
  row.className = "preset-row" + (preset.id === selectedId ? " selected" : "");

  const pick = document.createElement("div");
  pick.className = "preset-pick";
  pick.innerHTML = '<div class="preset-radio"></div>' +
    '<div class="preset-label">' + escapeHtml(preset.label) + "</div>";
  pick.onclick = () => send("set_preset", preset.id);
  row.appendChild(pick);

  if (editable) {
    const actions = document.createElement("div");
    actions.className = "preset-actions";
    const editBtn = document.createElement("button");
    editBtn.className = "icon-btn";
    editBtn.textContent = "Edit";
    editBtn.onclick = (e) => { e.stopPropagation(); openEditPreset(preset); };
    const delBtn = document.createElement("button");
    delBtn.className = "icon-btn delete";
    delBtn.textContent = "Delete";
    delBtn.onclick = (e) => {
      e.stopPropagation();
      if (delBtn.dataset.armed) {
        send("delete_custom_preset", { id: preset.id });
      } else {
        delBtn.dataset.armed = "1";
        delBtn.textContent = "Click again to delete";
        setTimeout(() => { delBtn.dataset.armed = ""; delBtn.textContent = "Delete"; }, 2500);
      }
    };
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);
    row.appendChild(actions);
  }
  container.appendChild(row);
}

let _editingId = null;

function startNewPreset() {
  _editingId = null;
  $("editor-label").value = "";
  $("editor-focus").value = "";
  $("editor-save-btn").textContent = "Add Preset";
  $("preset-editor").style.display = "block";
  $("editor-label").focus();
}

function openEditPreset(preset) {
  _editingId = preset.id;
  $("editor-label").value = preset.label;
  $("editor-focus").value = preset.focus;
  $("editor-save-btn").textContent = "Save Changes";
  $("preset-editor").style.display = "block";
  $("editor-label").focus();
}

function cancelPresetEdit() {
  _editingId = null;
  $("preset-editor").style.display = "none";
}

function savePresetEdit() {
  const label = $("editor-label").value;
  const focus = $("editor-focus").value;
  if (_editingId) {
    send("update_custom_preset", { id: _editingId, label: label, focus: focus });
  } else {
    send("add_custom_preset", { label: label, focus: focus });
  }
  _editingId = null;
  $("preset-editor").style.display = "none";
}

// ---- model dropdown ----

let _modelList = [];
let _lastProvider = "";

function syncModelDropdown(currentId) {
  const sel = $("model-select");
  sel.innerHTML = "";
  if (_modelList.length === 0) {
    const opt = document.createElement("option");
    opt.value = currentId || "";
    opt.textContent = currentId ? currentId + " (saved)" : "Loading models…";
    sel.appendChild(opt);
  } else {
    let matched = false;
    _modelList.forEach(m => {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.label;
      if (m.id === currentId) { opt.selected = true; matched = true; }
      sel.appendChild(opt);
    });
    if (currentId && !matched) {
      const opt = document.createElement("option");
      opt.value = currentId;
      opt.textContent = currentId + " (saved, not in current list)";
      opt.selected = true;
      sel.insertBefore(opt, sel.firstChild);
    }
    const other = document.createElement("option");
    other.value = "__other__";
    other.textContent = "Other… (type a model name)";
    sel.appendChild(other);
  }
  $("model-manual-row").style.display = (sel.value === "__other__") ? "block" : "none";
}

function refreshModels() {
  $("model-status").textContent = "Loading…";
  send("fetch_models", null);
}

window.onModelsLoaded = function (payload) {
  if (payload.ok) {
    _modelList = payload.models || [];
    $("model-status").textContent = _modelList.length + " available";
    const currentModel = $("model-input").value;
    const matched = _modelList.some(m => m.id === currentModel);
    if (_modelList.length > 0 && (!currentModel || !matched)) {
      const auto = _modelList[0].id;
      $("model-input").value = auto;
      send("set_model", auto);
      syncModelDropdown(auto);
    } else {
      syncModelDropdown(currentModel);
    }
  } else {
    _modelList = [];
    $("model-status").textContent = "⚠ " + (payload.error || "load failed");
    syncModelDropdown($("model-input").value);
  }
};

function onModelPicked() {
  const sel = $("model-select");
  const v = sel.value;
  if (v === "__other__") {
    $("model-manual-row").style.display = "block";
    $("model-input").focus();
    return;
  }
  $("model-manual-row").style.display = "none";
  if (v) {
    $("model-input").value = v;
    send("set_model", v);
  }
}

// ---- boot ----

document.querySelectorAll(".nav-item").forEach(n => {
  n.onclick = () => switchView(n.dataset.view);
});
$("side-search").addEventListener("input", renderSidebarList);
$("side-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    // Jump to the first match.
    const q = ($("side-search").value || "").trim().toLowerCase();
    const i = MEETINGS.findIndex(m => !q || matchesQuery(m, q));
    if (i !== -1) selectMeeting(i);
  }
});
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "f")) {
    e.preventDefault();
    $("side-search").focus();
    $("side-search").select();
  }
});

renderState(state);
renderSidebarList();
renderReader();
renderDictations();
refreshModels();

_lastProvider = state.llm_provider;
const _origOnState = window.onState;
window.onState = function (s) {
  if (s.llm_provider !== _lastProvider) {
    _lastProvider = s.llm_provider;
    _modelList = [];
    refreshModels();
  }
  _origOnState(s);
};

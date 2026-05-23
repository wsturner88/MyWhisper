"""Native floating dashboard window — meetings, dictation history, settings.

Built with NSPanel + WKWebView via PyObjC. A two-way bridge lets the HTML UI
read and write settings through a tiny Python message handler.
"""

import html
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import objc
from AppKit import (
    NSPanel, NSScreen, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskUtilityWindow,
    NSBackingStoreBuffered, NSFloatingWindowLevel, NSColor,
    NSWindowStyleMaskFullSizeContentView, NSApp,
    NSMakeRect, NSMakeSize, NSOpenPanel, NSObject, NSURL,
)
from Foundation import NSURL as FNSURL

from . import config, dictation_log, autostart, vocab, recorder

log = logging.getLogger("mywhisper")

objc.loadBundle(
    "WebKit", globals(),
    bundle_path="/System/Library/Frameworks/WebKit.framework",
)
WKWebView = objc.lookUpClass("WKWebView")
WKWebViewConfiguration = objc.lookUpClass("WKWebViewConfiguration")
WKUserContentController = objc.lookUpClass("WKUserContentController")

_panel = None
_webview = None
_bridge = None  # NSObject delegate; keep a reference or PyObjC will GC it
_PANEL_W = 620
_PANEL_H = 720


# -- Data gathering for the dashboard ---------------------------------------

def _meeting_files():
    out_dir = config.app_dir()
    if not out_dir.exists():
        return []
    files = sorted(out_dir.glob("meeting_*.md"), reverse=True)
    meetings = []
    for f in files[:50]:
        try:
            raw = f.read_text()
            name = f.stem.replace("meeting_", "").replace("_", " @ ")
            meetings.append({"name": name, "filename": f.name, "content": raw})
        except Exception:
            pass
    return meetings


def _state_snapshot():
    """Everything the JS side needs to render the settings form."""
    provider = config.get_llm_provider()
    info = config.LLM_PROVIDERS[provider]
    api_key = config.get_secret(info["key_name"]) or ""
    masked = (api_key[:4] + "•" * 6 + api_key[-4:]) if len(api_key) >= 12 else ""

    try:
        mic_devices = [name for _, name in recorder.input_devices()]
    except Exception:
        mic_devices = []

    return {
        "data_dir": str(config.app_dir()),
        "llm_provider": provider,
        "llm_providers": [
            {"id": pid, "label": info["label"]}
            for pid, info in config.LLM_PROVIDERS.items()
        ],
        "llm_model": config.get_llm_model(provider),
        "api_key_masked": masked,
        "api_key_set": bool(api_key),
        "mic": config.get_selected_mic() or "",
        "mic_devices": mic_devices,
        "visualization": config.get_visualization(),
        "autostart": autostart.is_enabled(),
        "vocabulary": _load_vocab_text(),
        "meeting_preset": config.get_meeting_preset(),
        "meeting_presets": [
            {"id": pid, "label": p["label"], "description": p["description"]}
            for pid, p in config.MEETING_PRESETS.items()
        ],
        "custom_preset": config.get_custom_preset(),
    }


def _load_vocab_text():
    try:
        path = vocab.ensure_file()
        return path.read_text()
    except Exception:
        return ""


def _save_vocab_text(text):
    try:
        path = vocab.ensure_file()
        path.write_text(text)
        return True
    except Exception:
        log.exception("dashboard: save vocab failed")
        return False


# -- Bridge: messages from JavaScript --------------------------------------

class _BridgeHandler(NSObject):
    """Receives postMessage() calls from the dashboard HTML."""

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = dict(message.body())
            action = body.get("action")
            log.info("dashboard bridge: %s", action)
            handler = _ACTIONS.get(action)
            if handler is None:
                log.warning("dashboard bridge: unknown action %r", action)
                return
            handler(body)
        except Exception:
            log.exception("dashboard bridge: failed")


def _act_set_provider(body):
    config.set_llm_provider(body.get("value", "openrouter"))
    _push_state()


def _act_set_model(body):
    provider = config.get_llm_provider()
    config.set_llm_model(provider, body.get("value", "").strip())
    _push_state()


def _act_set_api_key(body):
    provider = config.get_llm_provider()
    info = config.LLM_PROVIDERS[provider]
    val = body.get("value", "").strip()
    if val and "•" not in val:
        config.set_secret(info["key_name"], val)
    _push_state()


def _act_test_llm(body):
    from . import llm
    ok, msg = llm.test_connection()
    _call_js("onTestResult", {"ok": bool(ok), "message": str(msg)})


def _act_set_mic(body):
    val = body.get("value", "")
    config.set_selected_mic(val)
    _push_state()


def _act_set_visualization(body):
    config.set_visualization(body.get("value", "waveform"))
    _push_state()


def _act_set_autostart(body):
    try:
        if body.get("value"):
            autostart.enable()
        else:
            autostart.disable()
    except Exception:
        log.exception("dashboard: autostart toggle failed")
    _push_state()


def _act_save_vocab(body):
    _save_vocab_text(body.get("value", ""))


def _act_set_preset(body):
    config.set_meeting_preset(body.get("value", "general"))
    _push_state()


def _act_save_custom_preset(body):
    val = body.get("value") or {}
    config.set_custom_preset(val.get("label", ""), val.get("focus", ""))
    _push_state()


def _act_pick_folder(body):
    panel = NSOpenPanel.openPanel()
    panel.setCanChooseDirectories_(True)
    panel.setCanChooseFiles_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setPrompt_("Use This Folder")
    panel.setMessage_("Choose where MyWhisper should store recordings, "
                      "transcripts, and settings.")
    panel.setDirectoryURL_(FNSURL.fileURLWithPath_(str(config.app_dir().parent)))
    if panel.runModal() == 1:  # NSModalResponseOK
        url = panel.URL()
        new_path = url.path()
        # If the user picked the parent of an existing MyWhisper dir, append
        # MyWhisper so we don't dump files into Documents itself.
        if Path(new_path).name != "MyWhisper":
            new_path = str(Path(new_path) / "MyWhisper")
        try:
            old = config.app_dir()
            resolved = config.set_app_dir(new_path, move_existing=True)
            log.info("dashboard: data folder moved %s -> %s", old, resolved)
        except Exception:
            log.exception("dashboard: folder move failed")
    _push_state()


_ACTIONS = {
    "set_provider": _act_set_provider,
    "set_model": _act_set_model,
    "set_api_key": _act_set_api_key,
    "test_llm": _act_test_llm,
    "set_mic": _act_set_mic,
    "set_visualization": _act_set_visualization,
    "set_autostart": _act_set_autostart,
    "save_vocab": _act_save_vocab,
    "set_preset": _act_set_preset,
    "save_custom_preset": _act_save_custom_preset,
    "pick_folder": _act_pick_folder,
}


def _call_js(fn_name, payload):
    if _webview is None:
        return
    js = f"window.{fn_name} && window.{fn_name}({json.dumps(payload)});"
    _webview.evaluateJavaScript_completionHandler_(js, None)


def _push_state():
    _call_js("onState", _state_snapshot())


# -- HTML / CSS / JS for the dashboard --------------------------------------

def _build_html():
    meetings = _meeting_files()
    dictations = dictation_log.recent()
    state = _state_snapshot()

    meetings_html = ""
    if not meetings:
        meetings_html = '<p class="empty">No meetings recorded yet.</p>'
    else:
        for i, m in enumerate(meetings):
            safe_content = html.escape(m["content"])
            meetings_html += f"""
            <div class="card" onclick="toggleMeeting({i})">
                <div class="card-header">
                    <span class="card-title">{html.escape(m['name'])}</span>
                    <span class="card-file">{html.escape(m['filename'])}</span>
                    <span class="chevron" id="chev-{i}">&#9654;</span>
                </div>
                <pre class="card-body" id="meeting-{i}">{safe_content}</pre>
            </div>"""

    dictations_html = ""
    if not dictations:
        dictations_html = '<p class="empty">No dictations yet. Use push-to-talk and they\'ll appear here.</p>'
    else:
        for i, d in enumerate(dictations):
            safe_text = html.escape(d["text"])
            dictations_html += f"""
            <div class="card">
                <div class="card-header">
                    <span class="card-title">{html.escape(d['timestamp'])}</span>
                    <button class="copy-btn" onclick="copyText({i}, event)">Copy</button>
                </div>
                <div class="dict-text" id="dict-{i}">{safe_text}</div>
            </div>"""

    now = datetime.now().strftime("%I:%M %p")
    state_json = json.dumps(state)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MyWhisper</title>
<style>
    :root {{
        --bg: #1a1a2e;
        --surface: #222244;
        --surface2: #2a2a4a;
        --surface3: #303050;
        --accent: #e94560;
        --accent2: #0f3460;
        --text: #eee;
        --text2: #aaa;
        --text3: #777;
        --green: #4ecca3;
        --red: #e94560;
        --radius: 10px;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
        background: var(--bg);
        color: var(--text);
        -webkit-user-select: none;
        user-select: none;
    }}
    .header {{
        background: linear-gradient(135deg, var(--accent2), #16213e);
        padding: 18px 20px 12px;
        border-bottom: 2px solid var(--accent);
        -webkit-app-region: drag;
    }}
    .header h1 {{ font-size: 18px; font-weight: 600; }}
    .header h1 span {{ color: var(--accent); }}
    .header .updated {{
        font-size: 11px;
        color: var(--text2);
        margin-top: 3px;
    }}
    .tabs {{
        display: flex;
        background: var(--surface);
        border-bottom: 1px solid #333;
    }}
    .tab {{
        flex: 1;
        padding: 12px 0;
        text-align: center;
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        color: var(--text2);
        transition: all 0.2s;
        border-bottom: 3px solid transparent;
    }}
    .tab:hover {{ color: var(--text); background: var(--surface2); }}
    .tab.active {{
        color: var(--accent);
        border-bottom-color: var(--accent);
    }}
    .tab-count {{
        display: inline-block;
        background: var(--accent2);
        color: var(--text2);
        font-size: 10px;
        padding: 1px 6px;
        border-radius: 8px;
        margin-left: 5px;
    }}
    .tab.active .tab-count {{
        background: var(--accent);
        color: white;
    }}
    .scroll-area {{
        overflow-y: auto;
        max-height: calc(100vh - 110px);
        padding: 14px 16px 24px;
    }}
    .content {{ display: none; }}
    .content.active {{ display: block; }}
    .card {{
        background: var(--surface);
        border-radius: var(--radius);
        margin-bottom: 10px;
        overflow: hidden;
        border: 1px solid #333;
    }}
    .card:hover {{ border-color: #555; }}
    .card-header {{
        display: flex;
        align-items: center;
        padding: 12px 14px;
        cursor: pointer;
        gap: 10px;
    }}
    .card-title {{ font-weight: 500; font-size: 13px; flex-shrink: 0; }}
    .card-file {{
        color: var(--text2);
        font-size: 11px;
        flex: 1;
        text-align: right;
    }}
    .chevron {{
        color: var(--text2);
        font-size: 11px;
        transition: transform 0.2s;
    }}
    .chevron.open {{ transform: rotate(90deg); }}
    .card-body {{
        display: none;
        padding: 0 14px 14px;
        font-size: 12px;
        line-height: 1.6;
        color: var(--text2);
        white-space: pre-wrap;
        word-wrap: break-word;
        font-family: inherit;
        max-height: 350px;
        overflow-y: auto;
        -webkit-user-select: text;
        user-select: text;
    }}
    .card-body.open {{ display: block; }}
    .dict-text {{
        padding: 0 14px 12px;
        font-size: 13px;
        line-height: 1.5;
        -webkit-user-select: text;
        user-select: text;
    }}
    .copy-btn, .btn {{
        background: var(--accent2);
        color: var(--text);
        border: 1px solid #444;
        padding: 6px 14px;
        border-radius: 6px;
        font-size: 12px;
        cursor: pointer;
        transition: all 0.15s;
        flex-shrink: 0;
    }}
    .btn:hover, .copy-btn:hover {{
        background: var(--accent);
        border-color: var(--accent);
    }}
    .copy-btn.copied {{
        background: var(--green);
        border-color: var(--green);
        color: #111;
    }}
    .empty {{
        text-align: center;
        color: var(--text2);
        padding: 50px 16px;
        font-size: 14px;
    }}

    /* ----- Settings ----- */
    .settings-section {{
        background: var(--surface);
        border-radius: var(--radius);
        padding: 16px 18px;
        margin-bottom: 14px;
        border: 1px solid #333;
    }}
    .settings-section h3 {{
        font-size: 13px;
        font-weight: 600;
        color: var(--accent);
        margin-bottom: 4px;
        text-transform: uppercase;
        letter-spacing: 0.6px;
    }}
    .settings-section .section-desc {{
        font-size: 11px;
        color: var(--text3);
        margin-bottom: 14px;
    }}
    .field {{ margin-bottom: 14px; }}
    .field:last-child {{ margin-bottom: 0; }}
    .field-label {{
        display: block;
        font-size: 12px;
        color: var(--text2);
        margin-bottom: 6px;
        font-weight: 500;
    }}
    .field-hint {{
        font-size: 10px;
        color: var(--text3);
        margin-top: 4px;
    }}
    .field-row {{
        display: flex;
        gap: 8px;
        align-items: center;
    }}
    input[type=text], input[type=password], select, textarea {{
        background: var(--bg);
        color: var(--text);
        border: 1px solid #444;
        padding: 7px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-family: inherit;
        width: 100%;
        outline: none;
    }}
    input[type=text]:focus, input[type=password]:focus,
    select:focus, textarea:focus {{
        border-color: var(--accent);
    }}
    select {{
        cursor: pointer;
        appearance: none;
        -webkit-appearance: none;
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'><polygon points='0,0 10,0 5,6' fill='%23aaa'/></svg>");
        background-repeat: no-repeat;
        background-position: right 10px center;
        background-size: 10px 6px;
        padding-right: 28px;
    }}
    textarea {{
        min-height: 110px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 11px;
        line-height: 1.5;
        resize: vertical;
    }}
    .folder-display {{
        background: var(--bg);
        border: 1px solid #444;
        padding: 7px 10px;
        border-radius: 6px;
        font-size: 11px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        color: var(--text2);
        flex: 1;
        overflow-x: auto;
        white-space: nowrap;
    }}
    .toggle {{
        display: flex;
        align-items: center;
        gap: 8px;
        cursor: pointer;
    }}
    .toggle input {{ accent-color: var(--accent); }}
    .toggle-label {{ font-size: 12px; color: var(--text); }}
    .pill-status {{
        display: inline-block;
        padding: 2px 8px;
        border-radius: 10px;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        margin-left: 6px;
    }}
    .pill-status.ok {{ background: var(--green); color: #111; }}
    .pill-status.missing {{ background: var(--red); color: white; }}
    #test-result {{
        font-size: 11px;
        margin-top: 8px;
        min-height: 16px;
    }}
    #test-result.ok {{ color: var(--green); }}
    #test-result.err {{ color: var(--red); }}
    .preset-card {{
        background: var(--bg);
        border: 1px solid #444;
        border-radius: 6px;
        padding: 10px 12px;
        margin-bottom: 6px;
        cursor: pointer;
        transition: all 0.15s;
    }}
    .preset-card:hover {{ border-color: #666; }}
    .preset-card.selected {{
        border-color: var(--accent);
        background: var(--surface2);
    }}
    .preset-name {{
        font-size: 12px;
        font-weight: 600;
        color: var(--text);
    }}
    .preset-desc {{
        font-size: 11px;
        color: var(--text2);
        margin-top: 2px;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>&#127908; <span>MyWhisper</span></h1>
    <div class="updated">Updated {now}</div>
</div>

<div class="tabs">
    <div class="tab active" data-tab="meetings" onclick="switchTab('meetings')">
        Meetings <span class="tab-count">{len(meetings)}</span>
    </div>
    <div class="tab" data-tab="dictation" onclick="switchTab('dictation')">
        Dictation <span class="tab-count">{len(dictations)}</span>
    </div>
    <div class="tab" data-tab="settings" onclick="switchTab('settings')">
        Settings
    </div>
</div>

<div class="scroll-area">

<div class="content active" id="meetings-content">
    {meetings_html}
</div>

<div class="content" id="dictation-content">
    {dictations_html}
</div>

<div class="content" id="settings-content">

    <div class="settings-section">
        <h3>Data Folder</h3>
        <div class="section-desc">Where recordings, transcripts, and your config are stored.</div>
        <div class="field-row">
            <div class="folder-display" id="folder-display"></div>
            <button class="btn" onclick="pickFolder()">Change…</button>
        </div>
        <div class="field-hint">Changing the folder copies your existing files to the new spot.</div>
    </div>

    <div class="settings-section">
        <h3>LLM (for Meeting Summaries)</h3>
        <div class="section-desc">Which cloud model writes your meeting notes.</div>

        <div class="field">
            <label class="field-label">Provider</label>
            <select id="provider-select" onchange="send('set_provider', this.value)"></select>
        </div>

        <div class="field">
            <label class="field-label">
                API Key <span id="key-status" class="pill-status"></span>
            </label>
            <div class="field-row">
                <input type="password" id="api-key-input" placeholder="Paste your key…">
                <button class="btn" onclick="saveApiKey()">Save</button>
            </div>
            <div class="field-hint">Stored securely in macOS Keychain — never written to disk in plain text.</div>
        </div>

        <div class="field">
            <label class="field-label">Model</label>
            <div class="field-row">
                <input type="text" id="model-input" placeholder="e.g. anthropic/claude-sonnet-4-6">
                <button class="btn" onclick="saveModel()">Save</button>
            </div>
        </div>

        <div class="field">
            <button class="btn" onclick="testLLM()">Test Connection</button>
            <div id="test-result"></div>
        </div>
    </div>

    <div class="settings-section">
        <h3>Meeting Type Preset</h3>
        <div class="section-desc">You'll be asked which to use each time you start a meeting — the selection here is just the default.</div>
        <div id="preset-list"></div>

        <div id="custom-editor" style="display: none; margin-top: 14px; padding-top: 14px; border-top: 1px solid #333;">
            <div class="field">
                <label class="field-label">Custom Preset Name</label>
                <input type="text" id="custom-label" placeholder="e.g. Board Meeting, Vendor Call">
            </div>
            <div class="field">
                <label class="field-label">What should the AI focus on?</label>
                <textarea id="custom-focus" placeholder="e.g. budget figures, vendor commitments, regulatory mentions, and any deadlines with owners"></textarea>
                <div class="field-hint">Plain English — describe what matters most. The AI will lean into this when writing the summary.</div>
            </div>
            <div class="field-row">
                <button class="btn" onclick="saveCustomPreset()">Save Custom Preset</button>
                <span id="custom-status" class="field-hint"></span>
            </div>
        </div>
    </div>

    <div class="settings-section">
        <h3>Microphone &amp; Visualization</h3>

        <div class="field">
            <label class="field-label">Input Device</label>
            <select id="mic-select" onchange="send('set_mic', this.value)"></select>
        </div>

        <div class="field">
            <label class="field-label">Live Indicator Style</label>
            <select id="viz-select" onchange="send('set_visualization', this.value)">
                <option value="waveform">Waveform</option>
                <option value="vu_meter">VU Meter (Retro)</option>
            </select>
        </div>
    </div>

    <div class="settings-section">
        <h3>Vocabulary</h3>
        <div class="section-desc">Custom terms — one per line. Whisper uses these as a hint so it gets names and abbreviations right.</div>
        <textarea id="vocab-text" placeholder="# One term per line — company names, initials, jargon."></textarea>
        <div class="field-row" style="margin-top: 8px;">
            <button class="btn" onclick="saveVocab()">Save Vocabulary</button>
            <span id="vocab-status" class="field-hint"></span>
        </div>
    </div>

    <div class="settings-section">
        <h3>System</h3>
        <label class="toggle">
            <input type="checkbox" id="autostart-toggle" onchange="send('set_autostart', this.checked)">
            <span class="toggle-label">Start MyWhisper automatically at login</span>
        </label>
    </div>

</div>

</div>

<script>
const initialState = {state_json};

function $(id) {{ return document.getElementById(id); }}

function send(action, value) {{
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.bridge) {{
        window.webkit.messageHandlers.bridge.postMessage({{ action: action, value: value }});
    }}
}}

function switchTab(name) {{
    document.querySelectorAll('.tab').forEach(t => {{
        t.classList.toggle('active', t.dataset.tab === name);
    }});
    document.querySelectorAll('.content').forEach(c => {{
        c.classList.toggle('active', c.id === name + '-content');
    }});
}}

function toggleMeeting(i) {{
    $('meeting-' + i).classList.toggle('open');
    $('chev-' + i).classList.toggle('open');
}}

function copyText(i, event) {{
    event.stopPropagation();
    const text = $('dict-' + i).innerText;
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    const btn = event.target;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1500);
}}

function saveApiKey() {{
    const v = $('api-key-input').value.trim();
    if (v && !v.includes('•')) send('set_api_key', v);
    $('api-key-input').value = '';
}}

function saveModel() {{
    send('set_model', $('model-input').value.trim());
}}

function saveVocab() {{
    send('save_vocab', $('vocab-text').value);
    const s = $('vocab-status');
    s.textContent = 'Saved.';
    setTimeout(() => s.textContent = '', 2000);
}}

function pickFolder() {{ send('pick_folder', null); }}

function testLLM() {{
    const r = $('test-result');
    r.className = '';
    r.textContent = 'Testing…';
    send('test_llm', null);
}}

window.onTestResult = function(payload) {{
    const r = $('test-result');
    if (payload.ok) {{
        r.className = 'ok';
        r.textContent = '✓ Connected to ' + payload.message;
    }} else {{
        r.className = 'err';
        r.textContent = '✗ ' + payload.message;
    }}
}};

window.onState = function(state) {{ renderState(state); }};

function renderState(s) {{
    $('folder-display').textContent = s.data_dir;

    // Provider dropdown
    const psel = $('provider-select');
    psel.innerHTML = '';
    s.llm_providers.forEach(p => {{
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = p.label;
        if (p.id === s.llm_provider) o.selected = true;
        psel.appendChild(o);
    }});

    // API key status pill + masked placeholder
    const pill = $('key-status');
    if (s.api_key_set) {{
        pill.className = 'pill-status ok';
        pill.textContent = 'Set';
        $('api-key-input').placeholder = s.api_key_masked || 'Saved — paste a new key to replace';
    }} else {{
        pill.className = 'pill-status missing';
        pill.textContent = 'Not set';
        $('api-key-input').placeholder = 'Paste your key…';
    }}

    $('model-input').value = s.llm_model || '';

    // Mic dropdown
    const msel = $('mic-select');
    msel.innerHTML = '';
    const def = document.createElement('option');
    def.value = '';
    def.textContent = 'System Default';
    if (!s.mic) def.selected = true;
    msel.appendChild(def);
    s.mic_devices.forEach(name => {{
        const o = document.createElement('option');
        o.value = name;
        o.textContent = name;
        if (name === s.mic) o.selected = true;
        msel.appendChild(o);
    }});

    $('viz-select').value = s.visualization;
    $('autostart-toggle').checked = s.autostart;
    $('vocab-text').value = s.vocabulary;

    // Meeting presets
    const list = $('preset-list');
    list.innerHTML = '';
    s.meeting_presets.forEach(p => {{
        const card = document.createElement('div');
        card.className = 'preset-card' + (p.id === s.meeting_preset ? ' selected' : '');
        card.innerHTML = '<div class="preset-name">' + p.label + '</div>' +
                         '<div class="preset-desc">' + p.description + '</div>';
        card.onclick = () => send('set_preset', p.id);
        list.appendChild(card);
    }});

    // Custom preset editor: show only when 'custom' is the selected default
    const cust = s.custom_preset || {{}};
    $('custom-label').value = cust.label || '';
    $('custom-focus').value = cust.focus || '';
    $('custom-editor').style.display =
        (s.meeting_preset === 'custom') ? 'block' : 'none';
}}

function saveCustomPreset() {{
    const label = $('custom-label').value;
    const focus = $('custom-focus').value;
    send('save_custom_preset', {{ label: label, focus: focus }});
    const s = $('custom-status');
    s.textContent = 'Saved.';
    setTimeout(() => s.textContent = '', 2000);
}}

renderState(initialState);
</script>
</body>
</html>"""


# -- Panel creation ----------------------------------------------------------

def _create_panel():
    global _panel, _webview, _bridge

    screen = NSScreen.mainScreen().frame()
    x = screen.size.width - _PANEL_W - 12
    y = screen.size.height - _PANEL_H - 36

    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable
        | NSWindowStyleMaskUtilityWindow
        | NSWindowStyleMaskFullSizeContentView
    )
    rect = NSMakeRect(x, y, _PANEL_W, _PANEL_H)
    _panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False,
    )
    _panel.setTitle_("MyWhisper")
    _panel.setTitleVisibility_(1)  # NSWindowTitleVisibilityHidden
    _panel.setTitlebarAppearsTransparent_(True)
    _panel.setLevel_(NSFloatingWindowLevel)
    _panel.setBecomesKeyOnlyIfNeeded_(False)
    _panel.setHidesOnDeactivate_(False)
    _panel.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(
        0.102, 0.102, 0.18, 1.0
    ))
    _panel.setMinSize_(NSMakeSize(480, 480))

    # Configure WKWebView with a script message handler so JS can call us.
    wk_config = WKWebViewConfiguration.alloc().init()
    controller = WKUserContentController.alloc().init()
    _bridge = _BridgeHandler.alloc().init()
    controller.addScriptMessageHandler_name_(_bridge, "bridge")
    wk_config.setUserContentController_(controller)

    content_rect = _panel.contentView().bounds()
    _webview = WKWebView.alloc().initWithFrame_configuration_(
        content_rect, wk_config,
    )
    _webview.setAutoresizingMask_(0x12)  # flexible width + height
    _panel.contentView().addSubview_(_webview)


def _load_content():
    html_str = _build_html()
    _webview.loadHTMLString_baseURL_(html_str, None)


def open_dashboard():
    """Toggle the dashboard panel — show it (refreshed) or hide it."""
    global _panel, _webview
    try:
        if _panel is not None and _panel.isVisible():
            _panel.orderOut_(None)
            return

        if _panel is None:
            _create_panel()

        _load_content()
        _panel.makeKeyAndOrderFront_(None)
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        log.info("dashboard: opened")
    except Exception:
        log.exception("dashboard: failed to open")

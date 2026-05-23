import html
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import objc
from AppKit import (
    NSPanel, NSScreen, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskUtilityWindow,
    NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSColor, NSWindowStyleMaskFullSizeContentView, NSApp,
    NSMakeRect, NSMakePoint, NSMakeSize,
)

from . import config, dictation_log

log = logging.getLogger("mywhisper")

objc.loadBundle(
    "WebKit", globals(),
    bundle_path="/System/Library/Frameworks/WebKit.framework",
)
WKWebView = objc.lookUpClass("WKWebView")
WKWebViewConfiguration = objc.lookUpClass("WKWebViewConfiguration")
NSURL = objc.lookUpClass("NSURL")
NSURLRequest = objc.lookUpClass("NSURLRequest")

_panel = None
_webview = None
_PANEL_W = 560
_PANEL_H = 650


def _meeting_files():
    out_dir = Path(config.load()["output"]["dir"]).expanduser()
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


def _build_html():
    meetings = _meeting_files()
    dictations = dictation_log.recent()

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
        --accent: #e94560;
        --accent2: #0f3460;
        --text: #eee;
        --text2: #aaa;
        --green: #4ecca3;
        --radius: 10px;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
        background: var(--bg);
        color: var(--text);
        padding: 0;
        -webkit-user-select: none;
        user-select: none;
    }}
    .header {{
        background: linear-gradient(135deg, var(--accent2), #16213e);
        padding: 20px 20px 14px;
        border-bottom: 2px solid var(--accent);
        -webkit-app-region: drag;
    }}
    .header h1 {{
        font-size: 18px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }}
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
        max-height: calc(100vh - 130px);
        padding: 14px 16px;
    }}
    .content {{ display: none; }}
    .content.active {{ display: block; }}
    .card {{
        background: var(--surface);
        border-radius: var(--radius);
        margin-bottom: 10px;
        overflow: hidden;
        border: 1px solid #333;
        transition: border-color 0.2s;
    }}
    .card:hover {{ border-color: #555; }}
    .card-header {{
        display: flex;
        align-items: center;
        padding: 12px 14px;
        cursor: pointer;
        gap: 10px;
    }}
    .card-title {{
        font-weight: 500;
        font-size: 13px;
        flex-shrink: 0;
    }}
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
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
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
        color: var(--text);
        -webkit-user-select: text;
        user-select: text;
    }}
    .copy-btn {{
        background: var(--accent2);
        color: var(--text);
        border: 1px solid #444;
        padding: 4px 12px;
        border-radius: 6px;
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s;
        flex-shrink: 0;
        -webkit-app-region: no-drag;
    }}
    .copy-btn:hover {{ background: var(--accent); border-color: var(--accent); }}
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
</style>
</head>
<body>

<div class="header">
    <h1>&#127908; <span>MyWhisper</span></h1>
    <div class="updated">Updated {now}</div>
</div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('meetings')">
        Meetings <span class="tab-count">{len(meetings)}</span>
    </div>
    <div class="tab" onclick="switchTab('dictation')">
        Dictation History <span class="tab-count">{len(dictations)}</span>
    </div>
</div>

<div class="scroll-area">
    <div class="content active" id="meetings-content">
        {meetings_html}
    </div>
    <div class="content" id="dictation-content">
        {dictations_html}
    </div>
</div>

<script>
function switchTab(name) {{
    document.querySelectorAll('.tab').forEach((t, i) => {{
        t.classList.toggle('active', (name === 'meetings' ? i === 0 : i === 1));
    }});
    document.getElementById('meetings-content').classList.toggle('active', name === 'meetings');
    document.getElementById('dictation-content').classList.toggle('active', name !== 'meetings');
}}

function toggleMeeting(i) {{
    var body = document.getElementById('meeting-' + i);
    var chev = document.getElementById('chev-' + i);
    body.classList.toggle('open');
    chev.classList.toggle('open');
}}

function copyText(i, event) {{
    event.stopPropagation();
    var text = document.getElementById('dict-' + i).innerText;
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    var btn = event.target;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(function() {{
        btn.textContent = 'Copy';
        btn.classList.remove('copied');
    }}, 1500);
}}
</script>
</body>
</html>"""


def _create_panel():
    global _panel, _webview

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
    _panel.setMinSize_(NSMakeSize(400, 400))

    wk_config = WKWebViewConfiguration.alloc().init()
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
        NSApp.activateIgnoringOtherApps_(True)
        log.info("dashboard: opened")
    except Exception:
        log.exception("dashboard: failed to open")

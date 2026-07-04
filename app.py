#!/usr/bin/env python3
"""iMessage Search - Flask web app

FORKED from https://github.com/mbaran5/imessage-exporter-viewer to work with
a different attachment-storage layout: the upstream project assumes the
standard `imessage-exporter -c clone/basic/full` layout (attachments
physically copied into a lowercase "attachments/" folder next to the HTML).
This fork's source pipeline instead uses `-c disabled`, with attachments
referenced in place under separately-named "Attachments/" and
"StickerCache/" folders (capitalized, matching Apple's own naming).

Search "FORK:" for every change from the verified upstream source. All
patches are additive/flexible (checking multiple recognized folder names)
rather than replacing upstream's own behavior, so this also still works
correctly against the standard layout if ever pointed at one.
"""

import os
import re
import sqlite3
import io
import mimetypes
from pathlib import Path
from urllib.parse import quote
from flask import Flask, request, jsonify, Response, redirect

DB_PATH      = os.environ.get("DB_PATH", "/data/imessage.db")
ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", "/archives")
MODEL_DIR    = os.environ.get("MODEL_DIR", "/data/models")
app = Flask(__name__)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_phone_to_name():
    """
    Build a phone->name map from resolved 1:1 conversations, cached on app.
    Any conversation whose filename stem is a bare E.164 phone number
    (+ followed by 7-15 digits) and
    whose stored name differs from that number is a resolved mapping.
    """
    if not hasattr(app, '_phone_to_name'):
        conn = get_db()
        rows = conn.execute("SELECT filename, name FROM conversations").fetchall()
        conn.close()
        mapping = {}
        for row in rows:
            stem = row['filename'].replace('.html', '')
            if re.fullmatch(r'\+\d{7,15}', stem) and row['name'] != stem:
                mapping[stem] = row['name']
        app._phone_to_name = mapping
    return app._phone_to_name

def resolve_sender(sender):
    """Substitute a raw phone number sender with a resolved name if known."""
    if not sender or sender == 'Me':
        return sender
    return get_phone_to_name().get(sender, sender)

# FORK: this project's source pipeline uses `imessage-exporter -c disabled`,
# which stores attachments under separately-named "Attachments/" and
# "StickerCache/" folders (capitalized, matching Apple's own naming),
# instead of the standard `-c clone/basic/full` layout's single lowercase
# "attachments/" folder this upstream project assumes everywhere. These
# three helpers centralize the folder-name flexibility needed at every
# point that previously hardcoded a literal "attachments/" prefix.
ATTACHMENT_DIR_NAMES = ("attachments", "Attachments", "StickerCache")
ATTACHMENT_PREFIX_RE = re.compile(r'^(?:attachments|Attachments|StickerCache)/')
ATTACHMENT_SRC_RE = re.compile(r'src="((?:attachments|Attachments|StickerCache)/[^"]+)"')
ATTACHMENT_HREF_RE = re.compile(r'href="((?:attachments|Attachments|StickerCache)/[^"]+)"')

def strip_attachment_prefix(path):
    """Strip whichever of the recognized attachment-folder prefixes is present."""
    return ATTACHMENT_PREFIX_RE.sub('', path, count=1)

# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #1c1c1e; color: #f2f2f7; height: 100vh; display: flex; flex-direction: column; }
.header { background: #2c2c2e; padding: 12px 20px; display: flex; align-items: center;
          gap: 16px; border-bottom: 1px solid #3a3a3c; flex-shrink: 0; }
.header h1 { font-size: 18px; font-weight: 600; }
.header h1 a { text-decoration: none; cursor: pointer; }
.search-bar { flex: 1; display: flex; gap: 8px; max-width: 600px; }
.search-input-wrap { position: relative; flex: 1; }
.search-input-wrap input { width: 100%; background: #3a3a3c; border: none; border-radius: 10px;
                    padding: 8px 34px 8px 14px; color: #f2f2f7; font-size: 15px; outline: none; }
.search-input-wrap input::placeholder { color: #8e8e93; }
.search-input-clear { display: none; position: absolute; right: 6px; top: 50%;
                      transform: translateY(-50%); align-items: center; justify-content: center;
                      width: 22px; height: 22px; padding: 0; background: none; border: none;
                      color: #8e8e93; font-size: 18px; line-height: 1; cursor: pointer; }
.search-input-clear:hover { color: #f2f2f7; }
.search-input-wrap input:not(:placeholder-shown) + .search-input-clear { display: flex; }
.search-bar button { background: #48484a; border: none; border-radius: 10px;
                     padding: 8px 16px; color: white; font-size: 14px; cursor: pointer; }
.search-bar button:hover { background: #636366; }
.search-bar button.active { background: #0a84ff; }
.search-bar button.active:hover { background: #0071e3; }
.main { display: flex; flex: 1; overflow: hidden; }
.conv-list { width: 280px; background: #2c2c2e; border-right: 1px solid #3a3a3c;
             overflow-y: auto; flex-shrink: 0; }
.conv-item { padding: 12px 16px; border-bottom: 1px solid #3a3a3c; cursor: pointer; }
.conv-item:hover { background: #3a3a3c; }
.conv-item.active { background: #0a84ff20; border-left: 3px solid #0a84ff; }
.conv-name { font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-meta { font-size: 11px; color: #8e8e93; margin-top: 3px; }
.conv-count { font-size: 11px; color: #636366; }
.msg-pane { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.pane-header { padding: 10px 16px; background: #2c2c2e; border-bottom: 1px solid #3a3a3c;
               display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 8px; }
.pane-title { font-size: 15px; font-weight: 600; }
.pane-sub { font-size: 11px; color: #8e8e93; margin-top: 2px; }
.page-controls { display: flex; gap: 6px; align-items: center; flex-shrink: 0; }
.btn { background: #3a3a3c; border: none; border-radius: 8px; padding: 5px 10px;
       color: #f2f2f7; cursor: pointer; font-size: 13px; white-space: nowrap; }
.btn:hover { background: #48484a; }
.btn:disabled { opacity: 0.35; cursor: default; }
.btn.primary { background: #0a84ff; }
.page-input { width: 52px; background: #3a3a3c; border: none; border-radius: 8px;
              padding: 5px 8px; color: #f2f2f7; font-size: 13px; text-align: center; }
.page-info { font-size: 12px; color: #8e8e93; white-space: nowrap; }
.messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; overflow-anchor: none; }
.msg { display: flex; flex-direction: column; max-width: 65%; }
.msg.sent { align-self: flex-end; align-items: flex-end; }
.msg.received { align-self: flex-start; }
.msg-meta { font-size: 10px; color: #8e8e93; margin-bottom: 3px; padding: 0 4px; }
.msg-bubble { padding: 10px 14px; border-radius: 18px; font-size: 14px; line-height: 1.4; word-break: break-word; }
.sent .msg-bubble { background: #0a84ff; color: white; border-bottom-right-radius: 4px; }
.received .msg-bubble { background: #3a3a3c; color: #f2f2f7; border-bottom-left-radius: 4px; }
.msg-time { font-size: 10px; color: #8e8e93; margin-top: 3px; padding: 0 4px; }
.attach-note { font-size: 12px; color: #8e8e93; font-style: italic; }
.loading-bar { text-align: center; padding: 8px; color: #8e8e93; font-size: 12px; flex-shrink: 0; }
.date-jump { display: flex; gap: 6px; align-items: center; padding: 6px 16px;
             background: #2c2c2e; border-bottom: 1px solid #3a3a3c; flex-shrink: 0; }
.conv-search { display: flex; gap: 8px; align-items: center; padding: 6px 16px;
             background: #2c2c2e; border-bottom: 1px solid #3a3a3c; flex-shrink: 0; }
.conv-search input { flex: 1; background: #3a3a3c; border: none; border-radius: 8px;
             padding: 6px 12px; color: #f2f2f7; font-size: 13px; outline: none; }
.conv-search-count { font-size: 12px; color: #8e8e93; white-space: nowrap; min-width: 70px; text-align: right; }
.attachments-viewer { display: none; flex: 1; flex-direction: column; overflow: hidden; min-height: 0; }
.attachments-viewer-header { padding: 10px 16px; background: #2c2c2e; border-bottom: 1px solid #3a3a3c;
                             display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
.attachments-viewer-header span { font-size: 14px; font-weight: 600; }
.attachments-grid { flex: 1; overflow-y: auto; padding: 16px; display: grid; min-width: 0;
                    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
                    grid-auto-rows: 120px; gap: 8px; align-content: start; }
.attachment-thumb { border-radius: 8px; overflow: hidden; cursor: pointer;
                    background: #3a3a3c; border: 1px solid #3a3a3c; }
.attachment-thumb:hover { border-color: #0a84ff; }
.attachment-thumb img, .attachment-thumb video { width: 100%; height: 100%; object-fit: cover; display: block; }
.attachment-thumb.attachment-file { display: flex; flex-direction: column; align-items: center;
                    justify-content: center; gap: 6px; padding: 10px; }
.attachment-file-icon { font-size: 28px; line-height: 1; }
.attachment-file-name { font-size: 11px; color: #8e8e93; text-align: center; word-break: break-word;
                    overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.date-jump label { font-size: 11px; color: #8e8e93; white-space: nowrap; }
.date-jump select { background: #3a3a3c; border: none; border-radius: 8px;
  padding: 4px 8px; color: #f2f2f7; font-size: 12px; outline: none; cursor: pointer; }
.date-jump .btn { margin-left: auto; }
.msg-highlight { outline: 2px solid #ffd60a !important; border-radius: 20px !important; outline-offset: 3px !important; }
.results-pane { flex: 1; overflow-y: auto; padding: 16px; }
.sort-bar { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }
.sort-bar span { font-size: 12px; color: #8e8e93; }
.result-count { font-size: 13px; color: #8e8e93; margin-bottom: 12px; }
.result-item { background: #2c2c2e; border-radius: 12px; padding: 14px; margin-bottom: 10px;
               cursor: pointer; border: 1px solid #3a3a3c; }
.result-item:hover { background: #3a3a3c; }
.result-conv { font-size: 12px; color: #0a84ff; margin-bottom: 4px; font-weight: 500; }
.result-text { font-size: 14px; line-height: 1.4; }
.result-text mark { background: #ffd60a30; color: #ffd60a; border-radius: 3px; padding: 0 2px; }
.result-meta { font-size: 11px; color: #8e8e93; margin-top: 6px; }
.empty { display: flex; align-items: center; justify-content: center; height: 100%;
         color: #48484a; font-size: 15px; flex-direction: column; gap: 8px; }
.empty-icon { font-size: 48px; }
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #48484a; border-radius: 3px; }

/* imessage-exporter raw HTML rendering */
.message { margin: 4px 0; overflow-wrap: break-word; }
.message .sent, .message .received { border-radius: 25px; padding: 15px; max-width: 60%; width: fit-content; }
.message .sent { background-color: #1982FC; color: white; margin-left: auto; margin-right: 0; }
.message .sent.iMessage { background-color: #1982FC; }
.message .sent.sms, .message .sent.SMS { background-color: #65c466; }
.message .received { background-color: #d8d8d8; color: black; margin-right: auto; margin-left: 0; }
.message .sent .replies { border-left: dotted white; border-bottom: dotted white; border-bottom-left-radius: 25px; }
.message .received .replies { border-left: dotted dimgray; border-bottom: dotted dimgray; border-bottom-left-radius: 25px; }
.received .replies, .sent .replies { margin-top: 1%; padding-left: 1%; padding-right: 1%; }
.app { background: white; border-radius: 25px; }
.app a { text-decoration: none; }
.app_header { border-top-left-radius: 25px; border-top-right-radius: 25px; color: black; }
.app_header img { border-top-left-radius: 25px; border-top-right-radius: 25px; width: 100%; }
.app_header .name { color: black; font-weight: 600; padding: 8px 15px; }
.app_footer { border-bottom-left-radius: 25px; border-bottom-right-radius: 25px; border: thin solid darkgray; color: black; background: lightgray; padding-bottom: 1%; }
.app_footer .caption { margin-top: 1%; padding: 2px 15px; }
.app_footer .subcaption { padding: 2px 15px; color: #555; font-size: 0.85em; }
.app_footer .trailing_caption { text-align: right; padding: 2px 15px; }
span.timestamp a { color: inherit; text-decoration: none; opacity: 0.6; font-size: 0.8em; }
span.timestamp { opacity: 0.6; font-size: 0.8em; }
span.sender { font-weight: 500; font-size: 0.85em; }
span.bubble { white-space: pre-wrap; overflow-wrap: break-word; }
span.reply_context { opacity: 0.6; font-size: 0.85em; font-style: italic; display: block; margin-bottom: 4px; }
.tapbacks { font-size: 0.85em; opacity: 0.75; margin-top: 6px; }
.tapbacks p { font-size: 0.8em; color: #555; margin-bottom: 2px; }
.tapback { display: inline-block; margin-right: 6px; }
.announcement { text-align: center; padding: 8px; color: #666; font-size: 0.85em; }
.edited { font-size: 0.85em; opacity: 0.8; }

/* Find-a-thread box, sits above the sort bar in the sidebar */
.thread-finder { padding: 10px 10px 0; flex-shrink: 0; }
.thread-finder-wrap { position: relative; }
.thread-finder input { width: 100%; background: #3a3a3c; border: none; border-radius: 8px;
                       padding: 6px 28px 6px 10px; color: #f2f2f7; font-size: 13px; outline: none; }
.thread-finder input::placeholder { color: #8e8e93; }
.thread-finder-clear { display: none; position: absolute; right: 4px; top: 50%;
                       transform: translateY(-50%); align-items: center; justify-content: center;
                       width: 20px; height: 20px; padding: 0; background: none; border: none;
                       color: #8e8e93; font-size: 16px; line-height: 1; cursor: pointer; }
.thread-finder-clear:hover { color: #f2f2f7; }
/* Shown only once the input actually has content -- :placeholder-shown is
   well-supported cross-browser (Safari 9.1+), unlike <input type="month">,
   so this is safe to rely on without a JS-driven visibility toggle. */
.thread-finder input:not(:placeholder-shown) + .thread-finder-clear { display: flex; }

/* Conv list sort bar */
.conv-sort { display: flex; gap: 4px; padding: 8px 10px; border-bottom: 1px solid #3a3a3c; flex-shrink: 0; }
.conv-sort button { flex: 1; background: #3a3a3c; border: none; border-radius: 6px; padding: 5px 4px;
                    color: #8e8e93; font-size: 11px; cursor: pointer; }
.conv-sort button.active { background: #0a84ff; color: white; }
.conv-list { display: flex; flex-direction: column; }
.conv-list .conv-items { overflow-y: auto; flex: 1; }

/* Lightbox */
#lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.88);
            z-index: 1000; align-items: center; justify-content: center; cursor: zoom-out; }
#lightbox.open { display: flex; }
#lightbox img { max-width: 95vw; max-height: 95vh; border-radius: 8px; object-fit: contain;
                box-shadow: 0 8px 40px rgba(0,0,0,0.6); }
.lightbox-nav { display: none; position: absolute; top: 50%; transform: translateY(-50%);
                background: rgba(255,255,255,0.12); border: none; color: white; font-size: 28px;
                width: 48px; height: 48px; border-radius: 50%; cursor: pointer; align-items: center;
                justify-content: center; z-index: 1001; }
.lightbox-nav:hover { background: rgba(255,255,255,0.22); }
#lightboxPrev { left: 20px; }
#lightboxNext { right: 20px; }

/* Constrain media to reasonable sizes */
.message img { max-width: min(360px, 65vw) !important; max-height: 50vh !important; height: auto !important; width: auto !important; border-radius: 12px; display: block; }
.message video { max-width: min(360px, 65vw) !important; max-height: 50vh !important; border-radius: 12px; display: block; }
.message .sent, .message .received { max-width: min(500px, 70vw) !important; }
.message .app_header img { max-width: 100% !important; width: 100% !important; max-height: 200px !important; object-fit: cover; }
/* Nested reply previews inside a parent bubble: left-aligned, full width */
.message .replies .message { margin: 0; }
/* Nested preview bubbles inside a replies div: always left-aligned, full-width.
   Sent previews inside a received bubble get a slightly lighter blue so text stays readable.
   Received previews inside a sent bubble get a slightly lighter gray. */
.message .replies .message .sent,
.message .replies .message .received { margin-left: 0 !important; margin-right: 0 !important; max-width: 100% !important; width: 100% !important; border-radius: 12px; padding: 6px 10px; }
.message .sent .replies .message .received { background: rgba(255,255,255,0.25) !important; color: white !important; }
.message .received .replies .message .sent { background: rgba(25,130,252,0.35) !important; color: white !important; }

@media (prefers-color-scheme: dark) {
  .message .received { background-color: #3a3a3c; color: #f2f2f7; }
  .app { background: #2c2c2e; }
  .app_header { color: #f2f2f7; }
  .app_footer { background: #3a3a3c; color: #f2f2f7; border-color: #48484a; }
  .app_footer .caption, .app_footer .subcaption { color: #ebebf5; }
  .tapbacks p { color: #8e8e93; }
  .announcement { color: #8e8e93; }
}

/* Image search */
.img-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.img-card { background: #2c2c2e; border-radius: 12px; overflow: hidden;
            border: 1px solid #3a3a3c; text-decoration: none; color: inherit; display: block; }
.img-card:hover { border-color: #0a84ff; }
.img-card img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; background: #3a3a3c; }
.img-card-meta { padding: 8px 10px; }
.img-card-conv { font-size: 12px; color: #0a84ff; font-weight: 500;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.img-card-info { font-size: 11px; color: #8e8e93; margin-top: 2px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.img-pagination { display: flex; gap: 10px; align-items: center; margin: 10px 0; }

/* ── Mobile back button: hidden on desktop ── */
.back-btn { display: none !important; }

@media (max-width: 768px) {
  /* Header: search bar wraps to its own line */
  .header { flex-wrap: wrap; padding: 8px 12px; gap: 6px; }
  .search-bar { order: 3; width: 100%; max-width: none; flex: none; }

  /* Sidebar: full-width, toggled by JS class */
  .conv-list { width: 100%; border-right: none; }
  .conv-list.mobile-hidden { display: none !important; }

  /* Message pane: full-width, hidden until a conversation is selected */
  .msg-pane { width: 100%; }
  .msg-pane:not(.mobile-visible) { display: none; }

  /* Back button visible on mobile */
  .back-btn { display: inline-flex !important; }

  /* Wider bubbles on mobile */
  .msg { max-width: 85%; }

  /* Date-jump bar: allow year pills to wrap */
  .date-jump { flex-wrap: wrap; }

  /* Tighter pane header padding on small screens */
  .pane-header { padding: 8px 12px; }
}
"""

# ── Image search helpers ──────────────────────────────────────────────────────

_clip_model     = None
_clip_tokenizer = None
_emb_cache      = None   # (meta_list, ndarray, count) — invalidated by count change

def _mobileclip_checkpoint():
    """Return local path to MobileCLIP-S0 weights, downloading if needed."""
    ckpt = Path(MODEL_DIR) / "mobileclip_s0.pt"
    if not ckpt.exists():
        import urllib.request
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            "https://docs-assets.developer.apple.com/ml-research/datasets/mobileclip/mobileclip_s0.pt",
            str(ckpt),
        )
    return str(ckpt)


def _load_clip_model():
    global _clip_model, _clip_tokenizer
    if _clip_model is None:
        import mobileclip
        model, _, _ = mobileclip.create_model_and_transforms(
            'mobileclip_s0', pretrained=_mobileclip_checkpoint()
        )
        model.eval()
        _clip_model     = model
        _clip_tokenizer = mobileclip.get_tokenizer('mobileclip_s0')
    return _clip_model, _clip_tokenizer


def _get_emb_matrix():
    """Return (meta_list, float32 ndarray shape (N,512)) cached in-process."""
    global _emb_cache
    import numpy as np

    conn  = get_db()
    count = conn.execute("SELECT COUNT(*) FROM image_embeddings").fetchone()[0]

    if _emb_cache is not None and _emb_cache[2] == count:
        conn.close()
        return _emb_cache[0], _emb_cache[1]

    if count == 0:
        conn.close()
        _emb_cache = ([], None, 0)
        return [], None

    rows = conn.execute("""
        SELECT e.attachment_path, e.archive_id, e.message_id, e.embedding,
               m.timestamp, m.sender, c.filename, c.name
        FROM image_embeddings e
        JOIN messages m ON m.id = e.message_id
        JOIN conversations c ON c.id = m.conversation_id
    """).fetchall()
    conn.close()

    meta       = [dict(r) for r in rows]
    all_bytes  = b''.join(bytes(r['embedding']) for r in rows)
    matrix     = np.frombuffer(all_bytes, dtype=np.float32).reshape(len(rows), 512).copy()
    _emb_cache = (meta, matrix, count)
    return meta, matrix


# ── Index page ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = get_db()
    convs_recent = conn.execute(
        "SELECT filename, name, msg_count, last_date FROM conversations ORDER BY last_date DESC NULLS LAST"
    ).fetchall()
    convs_alpha = sorted(convs_recent, key=lambda c: (c["name"] or "").lower())
    conn.close()

    def make_conv_items(convs):
        return "".join(
            '<div class="conv-item" data-fn="{fn}" onclick="loadConv(this)">'
            '<div class="conv-name">{name}</div>'
            '<div class="conv-meta">{ld}</div>'
            '<div class="conv-count">{cnt:,} messages</div></div>'.format(
                fn=c["filename"].replace('"', '&quot;'),
                name=c["name"],
                ld=(c["last_date"] or "?")[:10],
                cnt=c["msg_count"]
            )
            for c in convs
        )

    conv_items_recent = make_conv_items(convs_recent)
    conv_items_alpha  = make_conv_items(convs_alpha)

    return """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="format-detection" content="telephone=no">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>💬</text></svg>">
<title>iMessage Search</title><style>""" + CSS + """</style></head><body>
<div class="header">
  <h1><a href="/">💬</a></h1>
  <div class="search-bar">
    <div class="search-input-wrap">
      <input type="text" id="searchInput" placeholder="Search messages..."
             onkeydown="if(event.key==='Enter')setSearchMode(searchMode)">
      <button type="button" class="search-input-clear" onclick="clearMainSearch()" title="Clear" tabindex="-1">&times;</button>
    </div>
    <button id="modeMessagesBtn" class="active" onclick="setSearchMode('messages')">Messages</button>
    <button id="modeImagesBtn" onclick="setSearchMode('images')">Images</button>
  </div>
</div>
<div class="main">
  <div class="conv-list">
    <div class="thread-finder">
      <div class="thread-finder-wrap">
        <input type="text" id="recipientInput" placeholder="Find a thread (name or number)"
               oninput="filterThreads()">
        <button type="button" class="thread-finder-clear" onclick="clearThreadFilter()" title="Clear" tabindex="-1">&times;</button>
      </div>
    </div>
    <div class="conv-sort">
      <button id="sortRecent" class="active" onclick="setSort('recent')">Recent</button>
      <button id="sortAlpha" onclick="setSort('alpha')">A – Z</button>
    </div>
    <div class="conv-items" id="convItemsRecent">""" + conv_items_recent + """</div>
    <div class="conv-items" id="convItemsAlpha" style="display:none">""" + conv_items_alpha + """</div>
  </div>
  <div class="msg-pane">
    <div class="pane-header" id="paneHeader" style="display:none">
      <button class="btn back-btn" id="backBtn" onclick="showConvList()" title="Back to conversations">&#8592; Convos</button>
      <div style="min-width:0;flex:1">
        <div class="pane-title" id="paneTitle"></div>
        <div class="pane-sub" id="paneSub"></div>
      </div>
      <div class="page-controls">
        <button class="btn" id="convSearchToggleBtn" onclick="toggleConvSearch()" title="Search this conversation">&#128269;</button>
        <button class="btn" id="attachmentsViewerToggleBtn" onclick="toggleAttachmentsViewer()" title="View all photos & videos">&#128247;</button>
        <button class="btn" id="dateJumpToggleBtn" onclick="toggleDateJump()" title="Jump to date" disabled>&#128197;</button>
        <button class="btn" id="jumpTopBtn" onclick="jumpToTop()" title="Jump to top">&#8607;</button>
        <button class="btn" id="jumpBottomBtn" onclick="jumpToBottom()" title="Jump to bottom">&#8609;</button>
      </div>
    </div>
    <div class="conv-search" id="convSearch" style="display:none">
      <input type="text" id="convSearchInput" placeholder="Search this conversation..."
             oninput="debounceConvSearch()" onkeydown="handleConvSearchKey(event)">
      <span class="conv-search-count" id="convSearchCount"></span>
      <button class="btn" onclick="convSearchPrev()" title="Previous match (Shift+Enter)">&#8593;</button>
      <button class="btn" onclick="convSearchNext()" title="Next match (Enter)">&#8595;</button>
      <button class="btn" onclick="closeConvSearch()" title="Close">&times;</button>
    </div>
    <div class="date-jump" id="dateJump" style="display:none">
      <label>Jump to:</label>
      <select id="monthSelect" onchange="jumpToMonthYear()" title="Month">
        <option value="01">Jan</option>
        <option value="02">Feb</option>
        <option value="03">Mar</option>
        <option value="04">Apr</option>
        <option value="05">May</option>
        <option value="06">Jun</option>
        <option value="07">Jul</option>
        <option value="08">Aug</option>
        <option value="09">Sep</option>
        <option value="10">Oct</option>
        <option value="11">Nov</option>
        <option value="12">Dec</option>
      </select>
      <select id="yearSelect" onchange="jumpToMonthYear()" title="Year"></select>
      <button class="btn" onclick="closeDateJump()" title="Close">&times;</button>
    </div>
    <div class="messages" id="messages">
      <div class="empty"><div class="empty-icon">💬</div><div>Select a conversation</div></div>
    </div>
    <div class="attachments-viewer" id="attachmentsViewer" style="display:none">
      <div class="attachments-viewer-header">
        <span>Attachments</span>
        <button class="btn" onclick="closeAttachmentsViewer()" title="Close">&times;</button>
      </div>
      <div class="attachments-grid" id="attachmentsGrid"></div>
    </div>
    <div class="loading-bar" id="loadingBar" style="display:none">Loading...</div>
  </div>
</div>
<script>
// ── State ─────────────────────────────────────────────────────────────────────
var currentFn   = null;   // active conversation filename
var totalMsgs   = 0;      // total messages in conversation
var domStart    = 0;      // row index of first message currently in DOM (0-based)
var domEnd      = 0;      // row index past last message currently in DOM
var loading     = false;
var hlTs        = null;
var hlMid       = null;

var WIN   = 150;   // messages to fetch per load
var MAX   = 300;   // max messages to keep in DOM before culling the far end
var TRIM  = 150;   // how many to cull when MAX is exceeded

// ── Mobile navigation ─────────────────────────────────────────────────────────
function isMobile() { return window.innerWidth <= 768; }

function showMsgPane() {
  if (!isMobile()) return;
  document.querySelector('.msg-pane').classList.add('mobile-visible');
  document.querySelector('.conv-list').classList.add('mobile-hidden');
}

function showConvList() {
  if (!isMobile()) return;
  document.querySelector('.msg-pane').classList.remove('mobile-visible');
  document.querySelector('.conv-list').classList.remove('mobile-hidden');
}

// ── Search mode toggle ────────────────────────────────────────────────────────
// Messages/Images act as a persistent mode selector rather than an
// immediate-navigate button: clicking one always updates which mode is
// "armed", but only actually navigates to results if there's a query
// typed. With nothing typed, clicking Images just marks it active and
// stays right here -- there's nothing to search yet, so there's no
// separate "enter a query" page to land on.
var searchMode = 'messages';

function setSearchMode(mode) {
  searchMode = mode;
  var msgBtn = document.getElementById('modeMessagesBtn');
  var imgBtn = document.getElementById('modeImagesBtn');
  if (msgBtn) msgBtn.classList.toggle('active', mode === 'messages');
  if (imgBtn) imgBtn.classList.toggle('active', mode === 'images');
  var q = document.getElementById('searchInput').value.trim();
  if (!q) return;
  window.location.href = (mode === 'messages' ? '/search?q=' : '/search/images?q=') + encodeURIComponent(q);
}

function clearMainSearch() {
  var input = document.getElementById('searchInput');
  input.value = '';
  if (window.location.pathname !== '/') {
    window.location.href = '/';
  } else {
    input.focus();
  }
}

function setSort(mode) {
  document.getElementById('convItemsRecent').style.display = mode === 'recent' ? '' : 'none';
  document.getElementById('convItemsAlpha').style.display  = mode === 'alpha'  ? '' : 'none';
  document.getElementById('sortRecent').className = mode === 'recent' ? 'active' : '';
  document.getElementById('sortAlpha').className  = mode === 'alpha'  ? 'active' : '';
}

// ── Find a thread ─────────────────────────────────────────────────────────────
// Live, client-side filter of the sidebar as you type -- no server round
// trip, since every conversation is already rendered in the DOM (in both
// the Recent and A-Z lists). Matches against the visible display name
// (resolved contact/group name) or the raw filename (which for 1:1 threads
// is the phone number/handle itself, so partial digits work too).
function filterThreads() {
  var recip = document.getElementById('recipientInput').value.trim().toLowerCase();
  document.querySelectorAll('.conv-item').forEach(function(item) {
    if (!recip) { item.style.display = ''; return; }
    var nameEl = item.querySelector('.conv-name');
    var name = nameEl ? nameEl.textContent.toLowerCase() : '';
    var fn = (item.getAttribute('data-fn') || '').toLowerCase();
    var match = name.indexOf(recip) !== -1 || fn.indexOf(recip) !== -1;
    item.style.display = match ? '' : 'none';
  });
}

function clearThreadFilter() {
  var input = document.getElementById('recipientInput');
  input.value = '';
  filterThreads();
  input.focus();
}

// ── Conversation load ─────────────────────────────────────────────────────────
function loadConv(el) {
  var fn = el.getAttribute('data-fn');
  currentFn = fn; hlTs = null; hlMid = null;
  closeConvSearch();
  closeDateJump();
  closeAttachmentsViewer();
  document.getElementById('dateJumpToggleBtn').disabled = true;
  dateJumpInitializedFor = null;
  document.querySelectorAll('.conv-item').forEach(function(e){ e.classList.remove('active'); });
  el.classList.add('active');
  el.scrollIntoView({block: 'nearest'});
  resetPane();
  // Load last WIN messages so we start at the bottom
  fetchRows(null, 'initial-bottom');
  showMsgPane();
}

function resetPane() {
  domStart = 0; domEnd = 0; totalMsgs = 0;
  var c = document.getElementById('messages');
  c.innerHTML = '';
}

// ── Core fetch ────────────────────────────────────────────────────────────────
// mode: 'initial-bottom' | 'initial-row:N' | 'prepend' | 'append'
// N in initial-row:N is the 0-based target row to center the viewport on.
// All fetches use ?offset= so the server returns exactly the rows we want
// and we always know the precise row index of what was returned.
function fetchRows(onComplete, mode) {
  if (loading || !currentFn) return;
  loading = true;
  showLoading(true);

  var fetchOffset;  // 0-based row index of first message to fetch

  if (mode === 'initial-bottom') {
    fetchOffset = totalMsgs > 0 ? Math.max(0, totalMsgs - WIN) : -1;
  } else if (typeof mode === 'string' && mode.startsWith('initial-row:')) {
    var targetRow = parseInt(mode.split(':')[1]);
    fetchOffset = Math.max(0, targetRow - Math.floor(WIN / 2));
  } else if (typeof mode === 'string' && mode.startsWith('initial-top:')) {
    // Put the target row at the TOP of the rendered window, not the center.
    // Used for date/year jumps where you want to read forward from that point.
    var targetRow = parseInt(mode.split(':')[1]);
    fetchOffset = Math.max(0, targetRow - 1);  // target becomes first visible row
  } else if (mode === 'prepend') {
    fetchOffset = Math.max(0, domStart - WIN);
  } else { // append
    fetchOffset = domEnd;
  }

  console.log('[vscroll] fetchRows mode=' + mode + ' fetchOffset=' + fetchOffset + ' totalMsgs=' + totalMsgs + ' domStart=' + domStart + ' domEnd=' + domEnd);

  var url = '/api/conversation?filename=' + encodeURIComponent(currentFn)
          + '&per_page=' + WIN
          + '&offset=' + Math.max(0, fetchOffset);

  // priority:'high' is just a hint (ignored harmlessly on browsers that
  // don't support it), but it helps this take precedence over any
  // straggling attachment-grid image/video requests still being torn
  // down in the background right after closing that grid.
  fetch(url, {priority: 'high'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      totalMsgs = d.total;
      updateHeader(d.name, d.total);
      if (d.first_date) updateDateJump(d.first_date, d.last_date);

      console.log('[vscroll] response: total=' + d.total + ' offset=' + d.offset + ' count=' + d.count + ' mode=' + mode);

      // If initial-bottom and we fetched offset=0 as probe, re-fetch real tail
      if (mode === 'initial-bottom' && fetchOffset === -1) {
        loading = false;
        fetchOffset = Math.max(0, totalMsgs - WIN);
        fetchRows(onComplete, 'initial-bottom');
        return;
      }

      // d.offset is the actual offset the server used (may differ if clamped)
      var rowOffset = d.offset;
      var c = document.getElementById('messages');
      var html = d.messages.map(function(m){ return renderMsg(m); }).join('');

      if (mode === 'initial-bottom' || (typeof mode === 'string' && (mode.startsWith('initial-row:') || mode.startsWith('initial-top:')))) {
        c.innerHTML = html;
        domStart = rowOffset;
        domEnd   = rowOffset + d.count;
        if (mode === 'initial-bottom') {
          attachSentinels();
          pinToBottom(c);
        } else {
          // For initial-row: scroll to target first, THEN attach sentinels.
          // This prevents the top sentinel from firing before the user has
          // even seen the highlighted message.
          var target = c.querySelector('.msg-highlight');
          if (!target && hlMid) target = c.querySelector('[data-mid="' + hlMid + '"]');
          if (!target && hlTs) {
            // Find by timestamp prefix match across all message-rows
            c.querySelectorAll('.message-row').forEach(function(el) {
              if (!target) {
                var ts = el.getAttribute('data-ts') || '';
                if (ts && hlTs && ts.startsWith(hlTs.slice(0,16))) target = el;
              }
            });
          }
          console.log('[vscroll] highlight target:', target, 'hlMid=', hlMid);
          if (target) {
            target.scrollIntoView({block: 'center'});
            target.classList.add('msg-highlight');
            setTimeout(function(){ target.classList.remove('msg-highlight'); }, 2500);
            // Hold scroll position while images above load and expand layout
            var targetEl = target;
            var deadline = Date.now() + 3000;
            var lastTop = targetEl.getBoundingClientRect().top;
            function holdPosition() {
              if (Date.now() > deadline) return;
              var newTop = targetEl.getBoundingClientRect().top;
              if (Math.abs(newTop - lastTop) > 2) {
                targetEl.scrollIntoView({block: 'center'});
                lastTop = targetEl.getBoundingClientRect().top;
              }
              requestAnimationFrame(holdPosition);
            }
            requestAnimationFrame(holdPosition);
          }
          // For date jumps (initial-top), scroll to top so target is first
          // visible. Date/year jumps always target "the first message at
          // or after a given row" -- since initial-top mode fetches a
          // window STARTING exactly at that row, the first rendered
          // message-row IS the target. It's highlighted directly here
          // rather than through the hlMid/hlTs matching above, since that
          // matching only works when the target's own id or exact
          // timestamp is already known (search results, reply arrows) --
          // neither of which applies to "whatever's first on or after this
          // month", so without this, a date/year jump would silently
          // scroll with no visual confirmation anything happened.
          if (typeof mode === 'string' && mode.startsWith('initial-top:')) {
            document.getElementById('messages').scrollTop = 0;
            if (!target) {
              var firstRow = c.querySelector('.message-row');
              if (firstRow) {
                firstRow.classList.add('msg-highlight');
                setTimeout(function(){ firstRow.classList.remove('msg-highlight'); }, 2500);
              }
            }
          }
          // Clear state and stale classes AFTER we've captured target above
          hlTs = null; hlMid = null;
          document.querySelectorAll('.message-row.msg-highlight').forEach(function(el){
            if (el !== target) el.classList.remove('msg-highlight');
          });
          // Attach sentinels after a short delay so layout is stable
          setTimeout(function(){ attachSentinels(); }, 200);
        }
      } else if (mode === 'prepend') {
        var prevH = c.scrollHeight;
        var s = document.getElementById('topSentinel');
        if (s) s.remove();
        c.insertAdjacentHTML('afterbegin', html);
        domStart = rowOffset;
        c.scrollTop += c.scrollHeight - prevH;
        if (domEnd - domStart > MAX) cullBottom(c);
        attachTopSentinel();
      } else { // append
        var s = document.getElementById('botSentinel');
        if (s) s.remove();
        c.insertAdjacentHTML('beforeend', html);
        domEnd = rowOffset + d.count;
        if (domEnd - domStart > MAX) cullTop(c);
        attachBotSentinel();
      }

      loading = false;
      showLoading(false);
      if (typeof onComplete === 'function') onComplete();
    })
    .catch(function(){ loading = false; showLoading(false); });
}

// ── Sentinel-based IntersectionObserver ───────────────────────────────────────
var observer = null;

function attachSentinels() {
  attachTopSentinel();
  attachBotSentinel();
}

function attachTopSentinel() {
  if (domStart <= 0) return;  // nothing above
  var el = document.createElement('div');
  el.id = 'topSentinel';
  el.style.height = '1px';
  var c = document.getElementById('messages');
  c.insertAdjacentElement('afterbegin', el);
  observe(el, function(){ fetchRows(null, 'prepend'); });
}

function attachBotSentinel() {
  if (domEnd >= totalMsgs) return;  // nothing below
  var el = document.createElement('div');
  el.id = 'botSentinel';
  el.style.height = '1px';
  var c = document.getElementById('messages');
  c.insertAdjacentElement('beforeend', el);
  observe(el, function(){ fetchRows(null, 'append'); });
}

function observe(el, cb) {
  var io = new IntersectionObserver(function(entries) {
    if (entries[0].isIntersecting && !loading) {
      io.disconnect();
      cb();
    }
  }, { root: document.getElementById('messages'), rootMargin: '200px' });
  io.observe(el);
}

// ── DOM culling ───────────────────────────────────────────────────────────────
function cullBottom(c) {
  // Remove last TRIM message divs, update domEnd
  var msgs = c.querySelectorAll(':scope > .message-row');
  var remove = msgs.length - (MAX - TRIM);
  if (remove <= 0) return;
  for (var i = msgs.length - 1; i >= msgs.length - remove; i--) {
    msgs[i].remove();
  }
  domEnd -= remove;
  attachBotSentinel();
}

function cullTop(c) {
  // Remove first TRIM message divs, update domStart
  var msgs = c.querySelectorAll(':scope > .message-row');
  var remove = Math.min(TRIM, msgs.length - (MAX - TRIM));
  if (remove <= 0) return;
  var prevH = c.scrollHeight;
  for (var i = 0; i < remove; i++) {
    msgs[i].remove();
  }
  domStart += remove;
  c.scrollTop -= prevH - c.scrollHeight;
  attachTopSentinel();
}

// ── Render ────────────────────────────────────────────────────────────────────
function renderMsg(m) {
  var isHl = (hlMid && m.id === hlMid) ||
             (!hlMid && hlTs && m.timestamp && m.timestamp.slice(0,19) === hlTs.slice(0,19));

  var inner;
  if (m.raw_html) {
    inner = m.raw_html;
  } else {
    var dir = m.direction || 'received';
    var time = m.timestamp
      ? new Date(m.timestamp).toLocaleString('en-US', {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'})
      : (m.timestamp_raw || '');
    var text = m.text ? esc(m.text) : '';
    var att = '';
    if (m.has_attachment && m.attachment_url) {
      var url = esc(m.attachment_url);
      var ap = (m.attachment_path || '').toLowerCase();
      if (ap.match(/[.](mov|mp4|m4v|avi)$/)) {
        att = '<video controls style="max-width:100%;border-radius:12px;margin-top:4px;" preload="none">'
            + '<source src="' + url + '" type="video/mp4">'
            + '<a href="' + url + '" style="color:#0a84ff">&#9654; Download video</a></video>';
      } else if (ap.match(/[.](heic|heif|jpg|jpeg|png|gif|webp|bmp)$/)) {
        att = '<img src="' + url + '" style="max-width:100%;border-radius:12px;margin-top:4px;" loading="lazy">';
      } else {
        att = '<a href="' + url + '" style="color:#0a84ff">&#128206; ' + esc(m.attachment_path || 'Attachment') + '</a>';
      }
    } else if (m.has_attachment) {
      att = '<div class="attach-note">&#128206; Attachment</div>';
    }
    var body = text ? (m.has_attachment ? text + '<br>' + att : text) : (att || '<em style="opacity:0.4">&#8212;</em>');
    inner = '<div class="msg ' + dir + (isHl ? ' msg-highlight' : '') + '">'
          + '<div class="msg-meta">' + esc(m.sender || '') + '</div>'
          + '<div class="msg-bubble">' + body + '</div>'
          + '<div class="msg-time">' + esc(time) + '</div></div>';
  }

  var hlClass = isHl ? ' msg-highlight' : '';
  return '<div class="message-row' + hlClass + '" data-mid="' + esc(m.id || '') + '" data-ts="' + esc(m.timestamp || '') + '">' + inner + '</div>';
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── Reply anchor clicks ────────────────────────────────────────────────────────
// ⇱ up arrow:   href="#GUID"    → find the PARENT message containing this reply
// ⇲ down arrow: href="#r-GUID"  → find the standalone reply entry (data-mid=GUID)
document.addEventListener('click', function(e) {
  var a = e.target.closest('a[href^="#"]');
  if (!a || !a.closest('#messages')) return;
  var href = a.getAttribute('href').slice(1); // strip leading #
  if (!href || !currentFn) return;
  e.preventDefault();

  var isUpArrow = !href.startsWith('r-');
  var lookupId  = href.replace(/^r-/, '');  // plain GUID either way

  if (isUpArrow) {
    // ⇱ — jump to the PARENT message that contains this GUID in its replies div
    fetch('/api/reply_parent?filename=' + encodeURIComponent(currentFn) + '&guid=' + encodeURIComponent(lookupId))
      .then(function(r){ return r.json(); })
      .then(function(d) {
        console.log('[vscroll] up-arrow parent lookup: guid=' + lookupId + ' parent_id=' + d.parent_id + ' row=' + d.row);
        if (!d.row) return;
        // Check if parent already in DOM
        var existing = document.querySelector('.message-row[data-mid="' + d.parent_id + '"]');
        if (existing) { existing.scrollIntoView({block: 'center'}); flashHighlight(existing); return; }
        hlMid = d.parent_id;
        resetPane();
        fetchRows(null, 'initial-row:' + (d.row - 1));
      });
  } else {
    // ⇲ — jump to the standalone reply entry
    var existing = document.querySelector('.message-row[data-mid="' + lookupId + '"]');
    if (existing) { existing.scrollIntoView({block: 'center'}); flashHighlight(existing); return; }
    fetch('/api/message_page?filename=' + encodeURIComponent(currentFn) + '&msg_id=' + encodeURIComponent(lookupId) + '&per_page=' + WIN)
      .then(function(r){ return r.json(); })
      .then(function(d) {
        console.log('[vscroll] down-arrow lookup: lookupId=' + lookupId + ' d.row=' + d.row);
        if (!d.row) return;
        hlMid = lookupId;
        resetPane();
        fetchRows(null, 'initial-row:' + (d.row - 1));
      });
  }
});

function flashHighlight(el) {
  el.classList.add('msg-highlight');
  setTimeout(function(){ el.classList.remove('msg-highlight'); }, 2500);
}

// ── Search within conversation ────────────────────────────────────────────────
// Reuses the exact same jump mechanism as global search results and reply
// arrows: look up a message's row number via /api/message_page, then fetch
// a window of messages centered on that row (fetchRows('initial-row:...')),
// then scroll to and highlight the target once it renders. See
// jumpToConvSearchResult() below -- it's essentially the same code that
// already runs after a global search result click or a reply-arrow click,
// just triggered from a different place.
var convSearchResults = [];
var dateJumpInitializedFor = null; // currentFn value the month/year selects were last populated for
var convSearchIdx = -1;
var convSearchDebounce = null;

function toggleConvSearch() {
  var bar = document.getElementById('convSearch');
  if (bar.style.display === 'none') {
    bar.style.display = 'flex';
    document.getElementById('convSearchInput').focus();
  } else {
    closeConvSearch();
  }
}

function closeConvSearch() {
  document.getElementById('convSearch').style.display = 'none';
  document.getElementById('convSearchInput').value = '';
  document.getElementById('convSearchCount').textContent = '';
  convSearchResults = [];
  convSearchIdx = -1;
}

function handleConvSearchKey(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    if (e.shiftKey) convSearchPrev(); else convSearchNext();
  } else if (e.key === 'Escape') {
    closeConvSearch();
  }
}

function debounceConvSearch() {
  clearTimeout(convSearchDebounce);
  convSearchDebounce = setTimeout(runConvSearch, 250);
}

function runConvSearch() {
  var q = document.getElementById('convSearchInput').value.trim();
  convSearchResults = [];
  convSearchIdx = -1;
  if (!q || !currentFn) {
    updateConvSearchCount();
    return;
  }
  fetch('/api/conversation_search?filename=' + encodeURIComponent(currentFn) + '&q=' + encodeURIComponent(q))
    .then(function(r){ return r.json(); })
    .then(function(d) {
      convSearchResults = d.results || [];
      convSearchIdx = convSearchResults.length ? 0 : -1;
      updateConvSearchCount();
      if (convSearchIdx >= 0) jumpToConvSearchResult(convSearchIdx);
    });
}

function updateConvSearchCount() {
  var el = document.getElementById('convSearchCount');
  var q = document.getElementById('convSearchInput').value.trim();
  if (!convSearchResults.length) {
    el.textContent = q ? 'No matches' : '';
  } else {
    el.textContent = (convSearchIdx + 1) + ' of ' + convSearchResults.length;
  }
}

function convSearchNext() {
  if (!convSearchResults.length) return;
  convSearchIdx = (convSearchIdx + 1) % convSearchResults.length;
  updateConvSearchCount();
  jumpToConvSearchResult(convSearchIdx);
}

function convSearchPrev() {
  if (!convSearchResults.length) return;
  convSearchIdx = (convSearchIdx - 1 + convSearchResults.length) % convSearchResults.length;
  updateConvSearchCount();
  jumpToConvSearchResult(convSearchIdx);
}

// Shared by every "jump to a specific message" caller (conv search
// results, the attachment grid, the lightbox on close): if the message is
// already rendered, just scroll to it directly; otherwise ask the server
// which row it's on and fetch a window centered there. This works
// regardless of whether the message is currently in the DOM at all --
// unlike relying on a live element reference, which breaks the moment the
// virtual scroll culls that element to keep the DOM bounded.
function jumpToMessageId(msgId) {
  if (!currentFn || !msgId) return;
  var existing = document.querySelector('.message-row[data-mid="' + msgId + '"]');
  if (existing) { existing.scrollIntoView({block: 'center'}); flashHighlight(existing); return; }
  fetch('/api/message_page?filename=' + encodeURIComponent(currentFn) + '&msg_id=' + encodeURIComponent(msgId) + '&per_page=' + WIN, {priority: 'high'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (!d.row) return;
      hlMid = msgId;
      resetPane();
      fetchRows(null, 'initial-row:' + (d.row - 1));
    });
}

function jumpToConvSearchResult(idx) {
  var target = convSearchResults[idx];
  if (!target) return;
  jumpToMessageId(target.id);
}

// ── Attachments viewer ────────────────────────────────────────────────────────
// Shows every image/video in the current conversation as a grid. Clicking a
// thumbnail closes the grid and jumps to that message using the exact same
// row-lookup-and-scroll mechanism as everything else (conv search, date
// jump, global search results) -- it does NOT open the lightbox directly.
// The lightbox's own click handler is scoped to images inside #messages, so
// once jumpToAttachment() lands you on the real message with the image
// rendered inline, clicking THAT image is what opens the lightbox -- the
// existing behavior, untouched.
function toggleAttachmentsViewer() {
  var viewer = document.getElementById('attachmentsViewer');
  if (viewer.style.display === 'flex') {
    closeAttachmentsViewer();
  } else {
    openAttachmentsViewer();
  }
}

function openAttachmentsViewer() {
  if (!currentFn) return;
  var viewer = document.getElementById('attachmentsViewer');
  var grid = document.getElementById('attachmentsGrid');
  grid.innerHTML = '<div class="empty"><div>Loading...</div></div>';
  viewer.style.display = 'flex';
  document.getElementById('messages').style.display = 'none';
  fetch('/api/conversation_attachments?filename=' + encodeURIComponent(currentFn))
    .then(function(r){ return r.json(); })
    .then(function(d) { renderAttachmentsGrid(d.attachments || []); })
    .catch(function() {
      grid.innerHTML = '<div class="empty"><div>Could not load attachments</div></div>';
    });
}

function closeAttachmentsViewer() {
  document.getElementById('attachmentsViewer').style.display = 'none';
  document.getElementById('messages').style.display = 'flex';
  // display:none only hides the grid -- it does NOT cancel in-flight
  // network requests for thumbnails still loading. Clearing src directly
  // on each element is a more immediate abort signal than just removing
  // them from the document and waiting on garbage collection to trigger
  // it; doing both together is the most reliable way to actually stop
  // that background traffic before it competes with the conversation
  // fetch that's about to happen.
  var grid = document.getElementById('attachmentsGrid');
  grid.querySelectorAll('img, video').forEach(function(el) {
    el.removeAttribute('src');
    el.src = '';
  });
  grid.innerHTML = '';
}

function renderAttachmentsGrid(attachments) {
  var grid = document.getElementById('attachmentsGrid');
  if (!attachments.length) {
    grid.innerHTML = '<div class="empty"><div class="empty-icon">&#128206;</div><div>No attachments in this conversation</div></div>';
    return;
  }
  grid.innerHTML = attachments.map(function(a) {
    var path = a.attachment_path || '';
    var isImage = /[.](heic|heif|jpg|jpeg|png|gif|webp|bmp)$/i.test(path);
    var isVideo = /[.](mov|mp4|m4v|avi)$/i.test(path);
    var url = esc(a.attachment_url);
    var mid = esc(a.id);
    var media, cls;
    if (isImage) {
      media = '<img src="' + url + '" loading="lazy">';
      cls = 'attachment-thumb';
    } else if (isVideo) {
      media = '<video src="' + url + '" muted preload="none"></video>';
      cls = 'attachment-thumb';
    } else {
      var fname = path.split('/').pop() || 'Attachment';
      media = '<div class="attachment-file-icon">&#128196;</div><div class="attachment-file-name">' + esc(fname) + '</div>';
      cls = 'attachment-thumb attachment-file';
    }
    return '<div class="' + cls + '" onclick="jumpToAttachment(`' + mid + '`)">' + media + '</div>';
  }).join('');
}

function jumpToAttachment(msgId) {
  closeAttachmentsViewer();
  jumpToMessageId(msgId);
}

// ── Jump to top / bottom ──────────────────────────────────────────────────────
function jumpToTop() {
  // Reuses 'initial-top' mode (built for date/year jumps) targeting row 0 --
  // this also gets the first-message highlight flash for free, since that
  // mode already highlights whatever rendered first when no specific
  // hlMid/hlTs target is set.
  hlTs = null; hlMid = null;
  resetPane();
  fetchRows(null, 'initial-top:0');
}

function jumpToBottom() {
  resetPane();
  fetchRows(null, 'initial-bottom');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function updateHeader(name, total) {
  document.getElementById('paneHeader').style.display = 'flex';
  document.getElementById('paneTitle').textContent = name;
  document.getElementById('paneSub').textContent = total.toLocaleString() + ' messages';
}

function toggleDateJump() {
  var bar = document.getElementById('dateJump');
  if (bar.style.display === 'flex') closeDateJump();
  else bar.style.display = 'flex';
}

function closeDateJump() {
  document.getElementById('dateJump').style.display = 'none';
}

function updateDateJump(firstDate, lastDate) {
  if (!firstDate || !lastDate) return;

  // Visibility is controlled entirely by toggleDateJump() (the calendar
  // icon button) rather than being forced open here -- this function's
  // only job is keeping the selects populated and correct for the current
  // conversation. The toggle button gets enabled here since only now do we
  // know this conversation actually has a usable date range.
  document.getElementById('dateJumpToggleBtn').disabled = false;

  // fetchRows() calls this on EVERY response, including the one triggered
  // by jumpToMonth() itself when the user picks a date -- without this
  // guard, that follow-up call would immediately reset both selects back
  // to the most recent month/year, undoing the user's own selection. Only
  // (re)populate when the conversation actually changed.
  if (dateJumpInitializedFor === currentFn) return;
  dateJumpInitializedFor = currentFn;

  var monthSel = document.getElementById('monthSelect');
  var yearSel = document.getElementById('yearSelect');

  var firstYear = parseInt(firstDate.slice(0, 4));
  var lastYear  = parseInt(lastDate.slice(0, 4));
  var lastMonth = lastDate.slice(5, 7);

  monthSel.value = lastMonth;

  // Populate the year dropdown. Defaults to the most recent year, since
  // that's usually where you're already reading -- picking a month then
  // just needs one more selection instead of two.
  yearSel.innerHTML = '';
  for (var y = firstYear; y <= lastYear; y++) {
    var opt = document.createElement('option');
    opt.value = y;
    opt.textContent = y;
    if (y === lastYear) opt.selected = true;
    yearSel.appendChild(opt);
  }
}

function jumpToMonthYear() {
  var m = document.getElementById('monthSelect').value;
  var y = document.getElementById('yearSelect').value;
  if (!m || !y) return;
  jumpToMonth(y + '-' + m);
}

function jumpToMonth(val) {
  // val is "YYYY-MM"
  if (!val || !currentFn) return;
  var ts = val + '-01T00:00:00';
  fetch('/api/message_page?filename=' + encodeURIComponent(currentFn)
      + '&timestamp=' + encodeURIComponent(ts) + '&per_page=' + WIN)
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if (!d.row) return;
      resetPane();
      fetchRows(null, 'initial-top:' + (d.row - 1));
    });
}

function showLoading(on) {
  document.getElementById('loadingBar').style.display = on ? 'block' : 'none';
}

function pinToBottom(c) {
  // Immediately scroll to bottom, then observe size changes for up to 3s
  // to handle lazy images and layout settling (browser may defer load events).
  c.scrollTop = c.scrollHeight;
  var deadline = Date.now() + 3000;
  var lastH = c.scrollHeight;
  function repin() {
    if (Date.now() > deadline) return;
    if (c.scrollHeight !== lastH) {
      lastH = c.scrollHeight;
      var dist = c.scrollHeight - c.scrollTop - c.clientHeight;
      if (dist < 600) c.scrollTop = c.scrollHeight;
    }
    requestAnimationFrame(repin);
  }
  requestAnimationFrame(repin);
}

// ── Lightbox ──────────────────────────────────────────────────────────────────
var currentLightboxImg = null;

function lightboxImages() {
  // The navigable set is whatever's currently rendered in #messages -- since
  // the app uses virtual scroll, this naturally matches what a user could
  // otherwise reach by scrolling, without needing to fetch beyond the
  // currently-loaded window just to support arrow navigation.
  return Array.prototype.slice.call(document.querySelectorAll('#messages img')).filter(function(img) {
    return !img.closest('.replies');
  });
}

function openLightbox(img) {
  currentLightboxImg = img;
  document.getElementById('lightboxImg').src = img.src;
  document.getElementById('lightbox').classList.add('open');
  updateLightboxNav();
}

function updateLightboxNav() {
  var images = lightboxImages();
  var idx = images.indexOf(currentLightboxImg);
  // Nav stays available not just when the next/prev image is already
  // rendered, but also when there's simply more conversation to load in
  // that direction -- we won't know if it contains another image until we
  // actually fetch and look, but "might be more" is the right default
  // rather than treating "not loaded yet" as "doesn't exist".
  var hasPrev = idx > 0 || domStart > 0;
  var hasNext = (idx !== -1 && idx < images.length - 1) || domEnd < totalMsgs;
  document.getElementById('lightboxPrev').style.display = hasPrev ? 'flex' : 'none';
  document.getElementById('lightboxNext').style.display = hasNext ? 'flex' : 'none';
}

var lightboxLoading = false;

function lightboxNav(direction) {
  if (lightboxLoading) return;
  var images = lightboxImages();
  var idx = images.indexOf(currentLightboxImg);
  if (idx !== -1) {
    var next = images[idx + direction];
    if (next) {
      openLightbox(next);
      return;
    }
  }
  // Either idx was valid but nothing adjacent is loaded yet, or the
  // tracked image has been culled from the DOM entirely -- the virtual
  // scroll keeps the DOM bounded, and during a long multi-extend search
  // (a long stretch of text-only messages before the next/prev image
  // turns up) the image we started from can legitimately get culled
  // before the search finishes. Either way, extend the loaded window
  // further in this direction and look again.
  var canLoadMore = direction > 0 ? domEnd < totalMsgs : domStart > 0;
  // fetchRows() silently no-ops if a fetch is already in flight for some
  // other reason (e.g. a scroll-triggered sentinel) -- without this check,
  // that would mean our callback never fires and lightboxLoading gets
  // stuck true forever. Bailing here just means this attempt does nothing;
  // pressing the arrow again a moment later works normally.
  if (!canLoadMore || loading) return;
  var imagesBefore = images;
  lightboxLoading = true;
  fetchRows(function() {
    lightboxLoading = false;
    // Diff against the pre-fetch snapshot rather than just re-deriving an
    // index -- this correctly identifies genuinely new arrivals even if
    // the tracked reference was culled during this same extend (a simple
    // "did the count go up" check would be fooled if old images were
    // culled at the same time new ones arrived).
    var newImages = lightboxImages();
    var brandNew = newImages.filter(function(img) { return imagesBefore.indexOf(img) === -1; });
    if (brandNew.length > 0) {
      // newImages preserves document order, so for a forward search the
      // first brand-new arrival is the closest one; for a backward
      // search (new content prepended at the top) the last one is.
      openLightbox(direction > 0 ? brandNew[0] : brandNew[brandNew.length - 1]);
    } else {
      lightboxNav(direction);
    }
  }, direction > 0 ? 'append' : 'prepend');
}

document.addEventListener('click', function(e) {
  var img = e.target;
  if (img.tagName !== 'IMG') return;
  if (!img.closest('#messages')) return;
  // Don't lightbox if it's inside a reply preview
  if (img.closest('.replies')) return;
  openLightbox(img);
  e.stopPropagation();
});
function closeLightbox() {
  var lastViewed = currentLightboxImg;
  document.getElementById('lightbox').classList.remove('open');
  document.getElementById('lightboxImg').src = '';
  currentLightboxImg = null;
  // Jump back to wherever the last-viewed image actually is -- without
  // this, closing after browsing through several images via next/prev
  // would leave the conversation sitting at whatever position it was at
  // before the lightbox ever opened, disconnected from what was just
  // being looked at. Reading data-mid off the ancestor .message-row works
  // even if that row has since been culled from the DOM by the virtual
  // scroll -- a detached element still has its own attributes intact, and
  // jumpToMessageId() re-fetches by id from the server rather than
  // needing a live element reference, so this always resolves to the
  // right place regardless of current DOM state.
  if (lastViewed) {
    var row = lastViewed.closest('.message-row');
    var msgId = row ? row.getAttribute('data-mid') : null;
    if (msgId) jumpToMessageId(msgId);
  }
}
document.addEventListener('keydown', function(e) {
  if (!document.getElementById('lightbox').classList.contains('open')) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowLeft') lightboxNav(-1);
  else if (e.key === 'ArrowRight') lightboxNav(1);
});

// ── Handle URL hash on page load (from search result click) ──────────────────
document.addEventListener('DOMContentLoaded', function() {
  var hash = window.location.hash;
  if (!hash || hash.length < 2) return;
  var params = new URLSearchParams(hash.slice(1));
  var fn  = params.get('conv');
  var ts  = params.get('ts');
  var mid = params.get('mid');
  if (!fn) return;

  document.querySelectorAll('.conv-item').forEach(function(el) {
    if (el.getAttribute('data-fn') === fn) {
      el.classList.add('active');
      el.scrollIntoView({block: 'center'});
    }
  });

  currentFn = fn;
  hlTs  = ts  || null;
  hlMid = mid || null;

  showMsgPane();

  if (ts || mid) {
    var qs = 'filename=' + encodeURIComponent(fn) + '&per_page=' + WIN;
    if (mid) qs += '&msg_id=' + encodeURIComponent(mid);
    else     qs += '&timestamp=' + encodeURIComponent(ts);
    fetch('/api/message_page?' + qs)
      .then(function(r){ return r.json(); })
      .then(function(d) {
        console.log('[vscroll] message_page response: d.row=' + d.row + ' d.page=' + d.page + ' hlMid=' + hlMid + ' hlTs=' + hlTs);
        fetchRows(null, 'initial-row:' + (d.row ? d.row - 1 : 0));
      });
  } else {
    fetchRows(null, 'initial-bottom');
  }
});
</script>

<div id="lightbox" onclick="closeLightbox()">
  <button id="lightboxPrev" class="lightbox-nav" onclick="event.stopPropagation(); lightboxNav(-1)" title="Previous">&#8249;</button>
  <img id="lightboxImg" src="" alt="">
  <button id="lightboxNext" class="lightbox-nav" onclick="event.stopPropagation(); lightboxNav(1)" title="Next">&#8250;</button>
</div>
</body></html>"""


# ── API: conversation messages ────────────────────────────────────────────────

@app.route("/api/conversation")
def api_conversation():
    filename = request.args.get("filename", "")
    per_page = min(500, max(10, int(request.args.get("per_page", 100))))

    conn = get_db()
    conv = conn.execute("SELECT * FROM conversations WHERE filename=?", (filename,)).fetchone()
    if not conv:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    total = conv["msg_count"]

    # Support direct offset param (preferred) or legacy page param
    if request.args.get("offset") is not None:
        offset = max(0, int(request.args.get("offset")))
    else:
        page   = max(1, int(request.args.get("page", 1)))
        # Clamp page to valid range server-side
        total_pages = max(1, (total + per_page - 1) // per_page)
        page   = min(page, total_pages)
        offset = (page - 1) * per_page

    msgs = conn.execute("""
        SELECT id, timestamp, timestamp_raw, sender, text, direction, has_attachment, archive_id, attachment_path, raw_html
        FROM messages WHERE conversation_id=?
        ORDER BY timestamp ASC NULLS FIRST, rowid ASC
        LIMIT ? OFFSET ?
    """, (conv["id"], per_page, offset)).fetchall()
    conn.close()

    def msg_dict(m):
        d = dict(m)
        aid = str(d.get("archive_id", ""))
        if d.get("attachment_path") and aid:
            # FORK: use strip_attachment_prefix() instead of a literal
            # .replace("attachments/", "", 1) so this also works for
            # attachment_path values under "Attachments/" or "StickerCache/".
            d["attachment_url"] = "/attachments/" + aid + "/" + strip_attachment_prefix(d["attachment_path"])
        else:
            d["attachment_url"] = None
        if d.get("raw_html") and aid:
            def rewrite_src(m2):
                p = m2.group(1)
                return 'src="/attachments/' + aid + '/' + strip_attachment_prefix(p) + '"'
            def rewrite_href(m2):
                p = m2.group(1)
                return 'href="/attachments/' + aid + '/' + strip_attachment_prefix(p) + '"'
            # FORK: these regexes now also match Attachments/StickerCache,
            # not just a literal lowercase "attachments/" prefix.
            d["raw_html"] = ATTACHMENT_SRC_RE.sub(rewrite_src, d["raw_html"])
            d["raw_html"] = ATTACHMENT_HREF_RE.sub(rewrite_href, d["raw_html"])
            # Substitute raw phone numbers with resolved names in sender spans
            def rewrite_sender(m2):
                return '<span class="sender">' + resolve_sender(m2.group(1)) + '</span>'
            d["raw_html"] = re.sub(r'<span class="sender">(\+\d{7,15})</span>', rewrite_sender, d["raw_html"])
        # Also resolve sender field used by the fallback (non-raw_html) renderer
        d["sender"] = resolve_sender(d.get("sender"))
        return d

    return jsonify({
        "name":       conv["name"],
        "total":      total,
        "offset":     offset,
        "count":      len(msgs),
        "first_date": conv["first_date"],
        "last_date":  conv["last_date"],
        "messages":   [msg_dict(m) for m in msgs]
    })


# ── API: find which page a message is on ─────────────────────────────────────

@app.route("/api/message_page")
def api_message_page():
    filename  = request.args.get("filename", "")
    msg_id    = request.args.get("msg_id", "")
    timestamp = request.args.get("timestamp", "")
    per_page  = min(500, max(10, int(request.args.get("per_page", 100))))

    conn = get_db()
    conv = conn.execute("SELECT * FROM conversations WHERE filename=?", (filename,)).fetchone()
    if not conv:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    cid = conv["id"]

    if msg_id:
        target = conn.execute(
            "SELECT timestamp, rowid FROM messages WHERE id=? AND conversation_id=?",
            (msg_id, cid)
        ).fetchone()
        if target:
            row_num = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND "
                "(timestamp < ? OR (timestamp = ? AND rowid <= ?) OR timestamp IS NULL)",
                (cid, target["timestamp"], target["timestamp"], target["rowid"])
            ).fetchone()[0]
        else:
            row_num = 1
    elif timestamp:
        # Find the first message on or after the target timestamp.
        # COUNT of messages with timestamp < target gives us the 0-based row
        # index of that first match, which we return as 1-based row_num.
        row_num = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND "
            "timestamp IS NOT NULL AND timestamp < ?",
            (cid, timestamp)
        ).fetchone()[0] + 1  # +1 converts to 1-based
    else:
        conn.close()
        return jsonify({"page": 1, "highlight_ts": None})

    conn.close()
    page = max(1, (row_num + per_page - 1) // per_page)
    return jsonify({"page": page, "highlight_ts": timestamp, "row": row_num})


# ── API: find parent message of a reply ──────────────────────────────────────

@app.route("/api/reply_parent")
def api_reply_parent():
    """Given a reply GUID, find the parent message that contains it in its replies div."""
    filename = request.args.get("filename", "")
    guid     = request.args.get("guid", "")
    if not filename or not guid:
        return jsonify({"error": "Missing params"}), 400

    conn = get_db()
    conv = conn.execute("SELECT id FROM conversations WHERE filename=?", (filename,)).fetchone()
    if not conv:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    # Find the message whose raw_html contains this GUID inside a replies div.
    # The reply div has: id="GUID" on the outer .reply wrapper inside .replies.
    replies_pat = '%class="replies"%'
    guid_pat    = f'%id="{guid}"%'
    row = conn.execute(
        "SELECT id, rowid FROM messages WHERE conversation_id=? "
        "AND raw_html LIKE ? AND raw_html LIKE ?",
        (conv["id"], guid_pat, replies_pat)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"row": None})

    row_num = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND rowid <= ?",
        (conv["id"], row["rowid"])
    ).fetchone()[0]
    conn.close()
    return jsonify({"parent_id": row["id"], "row": row_num})


# ── API: all attachments in one conversation ──────────────────────────────────
# FORK addition: powers the attachment viewer. Returns every attachment in a
# conversation, of any type, so the frontend can render a grid; clicking a
# thumbnail reuses the same jump-to-message machinery as everything else
# (see jumpToAttachment() in the frontend below), not the lightbox -- the
# lightbox only ever applies to images already rendered inline in the
# conversation, which is a separate click handler scoped to #messages.

@app.route("/api/conversation_attachments")
def api_conversation_attachments():
    filename = request.args.get("filename", "")
    if not filename:
        return jsonify({"attachments": []})

    conn = get_db()
    conv = conn.execute("SELECT id FROM conversations WHERE filename=?", (filename,)).fetchone()
    if not conv:
        conn.close()
        return jsonify({"attachments": []})

    rows = conn.execute("""
        SELECT id, timestamp, archive_id, attachment_path
        FROM messages
        WHERE conversation_id = ? AND has_attachment = 1 AND attachment_path IS NOT NULL
        ORDER BY timestamp ASC NULLS LAST
    """, (conv["id"],)).fetchall()
    conn.close()

    results = []
    for r in rows:
        path = r["attachment_path"] or ""
        aid = str(r["archive_id"])
        results.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "attachment_url": "/attachments/" + aid + "/" + strip_attachment_prefix(path),
            "attachment_path": path,
        })

    return jsonify({"attachments": results})


# ── API: search within one conversation ──────────────────────────────────────
# FORK addition: reuses the exact same "find message -> look up its row
# number -> fetch a window centered there -> scroll and highlight" machinery
# that already powers global search results and reply-arrow navigation
# (see /api/message_page and fetchRows('initial-row:...') in the frontend
# below). This endpoint's only job is finding WHICH messages match within
# one conversation; the frontend does the row lookup + jump itself, exactly
# like it already does after a global search result click.

@app.route("/api/conversation_search")
def api_conversation_search():
    filename = request.args.get("filename", "")
    query = request.args.get("q", "").strip()
    if not filename or not query:
        return jsonify({"results": []})

    conn = get_db()
    conv = conn.execute("SELECT id FROM conversations WHERE filename=?", (filename,)).fetchone()
    if not conv:
        conn.close()
        return jsonify({"results": []})

    import shlex
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    terms = ' '.join('"' + t.replace('"', '') + '"*' for t in tokens if t)
    # Scoped to the text column only (FTS5 column-filter syntax) -- the
    # messages_fts table also indexes sender, and an unscoped MATCH would
    # otherwise return messages just because they were SENT BY someone
    # whose name happens to match the search term, not because their
    # content does.
    fts_query = ('text: (' + terms + ')') if terms else ''
    if not fts_query:
        conn.close()
        return jsonify({"results": []})

    try:
        rows = conn.execute(
            "SELECT m.id, m.timestamp, "
            "snippet(messages_fts, 0, '<mark>', '</mark>', '...', 12) as snip "
            "FROM messages_fts f "
            "JOIN messages m ON m.rowid = f.rowid "
            "WHERE messages_fts MATCH ? AND m.conversation_id = ? "
            "ORDER BY m.timestamp ASC NULLS LAST "
            "LIMIT 200",
            (fts_query, conv["id"])
        ).fetchall()
    except sqlite3.OperationalError as e:
        conn.close()
        return jsonify({"error": str(e), "results": []}), 400
    conn.close()

    return jsonify({"results": [
        {"id": r["id"], "timestamp": r["timestamp"], "snippet": r["snip"]} for r in rows
    ]})


# ── Search page ───────────────────────────────────────────────────────────────

@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    sort  = request.args.get("sort", "relevance")
    results_html = ""
    count = 0
    capped = ""

    if query:
        conn = get_db()
        order = {
            "date_desc": "m.timestamp DESC NULLS LAST",
            "date_asc":  "m.timestamp ASC NULLS FIRST",
        }.get(sort, "rank")

        # Build FTS query: quote each token and append * for prefix matching.
        # The porter tokenizer handles stemming (run/running/ran), prefix *
        # handles partial words (taco/tacos/tacobella). Scoped to the text
        # column only (FTS5 column-filter syntax) -- messages_fts also
        # indexes sender, and an unscoped MATCH would otherwise surface
        # messages just because they were SENT BY someone whose name
        # matches the search term, not because their content does.
        import shlex
        try:
            tokens = shlex.split(query)
        except ValueError:
            tokens = query.split()
        terms = ' '.join('"' + t.replace('"', '') + '"*' for t in tokens if t)
        fts_query = 'text: (' + terms + ')'

        rows = conn.execute(
            "SELECT m.id, m.timestamp, m.sender, m.text, c.filename, c.name, "
            "snippet(messages_fts, 0, '<mark>', '</mark>', '...', 25) as snip "
            "FROM messages_fts f "
            "JOIN messages m ON m.rowid = f.rowid "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY " + order + " LIMIT 500",
            (fts_query,)
        ).fetchall()
        conn.close()

        count = len(rows)
        if count == 500:
            capped = " (showing first 500)"

        def card(r):
            ts     = r["timestamp"][:10] if r["timestamp"] else "?"
            fn     = r["filename"]
            rts    = r["timestamp"] or ""
            rid    = r["id"] or ""
            name   = r["name"] or ""
            snip   = r["snip"] or ""
            sender = resolve_sender(r["sender"] or "") or "Unknown"
            href = "/#conv=" + quote(fn) + ("&ts=" + quote(rts) if rts else "") + ("&mid=" + quote(rid) if rid else "")
            return (
                '<a class="result-item" href="' + href + '" style="display:block;text-decoration:none;color:inherit;">'
                '<div class="result-conv">' + name + '</div>'
                '<div class="result-text">' + snip + '</div>'
                '<div class="result-meta">' + sender + ' &middot; ' + ts + '</div>'
                '</a>'
            )

        results_html = "".join(card(r) for r in rows)
        if not rows:
            results_html = '<div class="empty"><div class="empty-icon">&#128269;</div><div>No results</div></div>'

    qenc = quote(query)
    sort_bar = (
        '<div class="sort-bar"><span>Sort by:</span>'
        '<a href="/search?q={q}&sort=relevance" class="btn {r}">Relevance</a>'
        '<a href="/search?q={q}&sort=date_desc" class="btn {dd}">Newest</a>'
        '<a href="/search?q={q}&sort=date_asc" class="btn {da}">Oldest</a></div>'
    ).format(
        q=qenc,
        r="primary" if sort == "relevance" else "",
        dd="primary" if sort == "date_desc" else "",
        da="primary" if sort == "date_asc" else "",
    )

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="format-detection" content="telephone=no">'
        '<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>💬</text></svg>">'
        '<title>Search: ' + query + '</title><style>' + CSS + '</style></head><body>'
        '<div class="header">'
        '<h1><a href="/">&#128172;</a></h1>'
        '<div class="search-bar">'
        '<div class="search-input-wrap">'
        '<input type="text" id="searchInput" placeholder="Search messages..." value="' + query.replace('"','&quot;') + '"'
        ' onkeydown="if(event.key===\'Enter\')setSearchMode(searchMode)">'
        '<button type="button" class="search-input-clear" onclick="clearMainSearch()" title="Clear" tabindex="-1">&times;</button>'
        '</div>'
        '<button id="modeMessagesBtn" class="active" onclick="setSearchMode(\'messages\')">Messages</button>'
        '<button id="modeImagesBtn" onclick="setSearchMode(\'images\')">Images</button>'
        '</div>'
        '</div>'
        '<div class="main"><div class="results-pane">'
        + sort_bar +
        '<div class="result-count">' + str(count) + ' results for &ldquo;' + query + '&rdquo;' + capped + '</div>'
        + results_html +
        '</div></div>'
        '<script>'
        'var searchMode = "messages";'
        'function setSearchMode(mode){'
        'searchMode=mode;'
        'var msgBtn=document.getElementById("modeMessagesBtn"),imgBtn=document.getElementById("modeImagesBtn");'
        'if(msgBtn)msgBtn.classList.toggle("active",mode==="messages");'
        'if(imgBtn)imgBtn.classList.toggle("active",mode==="images");'
        'var q=document.getElementById("searchInput").value.trim();'
        'if(!q)return;'
        'window.location.href=(mode==="messages"?"/search?q=":"/search/images?q=")+encodeURIComponent(q);}'
        'function clearMainSearch(){'
        'var input=document.getElementById("searchInput");'
        'input.value="";'
        'window.location.href="/";}'
        '</script>'
        '</body></html>'
    )


# ── Image search ─────────────────────────────────────────────────────────────

@app.route("/search/images")
def search_images():
    import numpy as np
    from urllib.parse import quote as _q

    query    = request.args.get("q", "").strip()

    # No query means there's nothing to rank images against -- rather than
    # showing an "enter a query" landing page, just send this back to the
    # conversation view. The frontend's setSearchMode() already avoids
    # navigating here at all when the search box is empty; this redirect
    # covers the remaining paths (bookmarks, back/forward, typed URLs).
    if not query:
        return redirect('/')

    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(20, int(request.args.get("per_page", 100))))

    all_results = []
    status_msg  = ""

    conn           = get_db()
    count_embedded = conn.execute("SELECT COUNT(*) FROM image_embeddings").fetchone()[0]
    conn.close()

    if count_embedded == 0:
        status_msg = ("Image embeddings not yet available. "
                      "The indexer is still processing images in the background — "
                      "check back later.")
    else:
        try:
            model, tokenizer = _load_clip_model()
            import torch
            tokens = tokenizer([query])
            with torch.inference_mode():
                text_feat = model.encode_text(tokens)
                text_feat = text_feat / text_feat.norm(dim=-1, keepdim=True)
                text_vec  = text_feat.numpy()[0]

            meta, matrix = _get_emb_matrix()
            if matrix is not None and len(meta) > 0:
                sims       = matrix @ text_vec
                THRESHOLD  = 0.15
                above      = np.where(sims >= THRESHOLD)[0]
                sorted_idx = above[np.argsort(sims[above])[::-1]]
                for idx in sorted_idx:
                    r = meta[idx]
                    all_results.append({
                        # FORK: strip_attachment_prefix() instead of a literal
                        # .replace('attachments/', '', 1).
                        'att_url':    "/attachments/{}/{}".format(
                                          r['archive_id'],
                                          strip_attachment_prefix(r['attachment_path'])),
                        'conv_name':  r['name'] or '',
                        'filename':   r['filename'] or '',
                        'timestamp':  r['timestamp'] or '',
                        'sender':     resolve_sender(r['sender'] or '') or 'Unknown',
                        'message_id': r['message_id'] or '',
                    })
        except Exception as e:
            status_msg = f"Image search error: {e}"

    total_results = len(all_results)
    total_pages   = max(1, (total_results + per_page - 1) // per_page)
    page          = min(page, total_pages)
    start         = (page - 1) * per_page
    results       = all_results[start:start + per_page]

    def page_url(p):
        return '/search/images?q={}&page={}'.format(_q(query), p)

    def pagination_bar():
        if total_results <= per_page:
            return ''
        prev_lnk = ('<a href="{}" class="btn">&#8592; Prev</a>'.format(page_url(page - 1))
                    if page > 1 else '<span class="btn" style="opacity:.35">&#8592; Prev</span>')
        next_lnk = ('<a href="{}" class="btn">Next &#8594;</a>'.format(page_url(page + 1))
                    if page < total_pages else '<span class="btn" style="opacity:.35">Next &#8594;</span>')
        return (
            '<div class="img-pagination">'
            '{prev}<span class="page-info">Page {cur} of {tot}</span>{next}'
            '</div>'
        ).format(prev=prev_lnk, next=next_lnk, cur=page, tot=total_pages)

    # Build results HTML
    if results:
        cards = []
        for r in results:
            ts   = r['timestamp'][:10] if r['timestamp'] else ''
            href = "/#conv={}&ts={}&mid={}".format(
                _q(r['filename']), _q(r['timestamp']), _q(r['message_id']),
            )
            cards.append(
                '<a class="img-card" href="{href}">'
                '<img src="{img}" loading="lazy"'
                ' onerror="this.closest(\'.img-card\').style.display=\'none\'">'
                '<div class="img-card-meta">'
                '<div class="img-card-conv">{conv}</div>'
                '<div class="img-card-info">{sender} &middot; {ts}</div>'
                '</div></a>'.format(
                    href=href, img=r['att_url'],
                    conv=r['conv_name'], sender=r['sender'], ts=ts,
                )
            )
        showing = '{}&ndash;{}'.format(start + 1, start + len(results))
        pag     = pagination_bar()
        body_html = (
            '<div class="result-count">'
            'Showing {showing} of {n:,} images for &ldquo;{q}&rdquo;'
            ' <span style="color:#636366">(of {total:,} indexed)</span>'
            '</div>'
            '{pag}'
            '<div class="img-grid">{cards}</div>'
            '{pag}'
        ).format(showing=showing, n=total_results, q=query,
                 total=count_embedded, cards=''.join(cards), pag=pag)
    elif status_msg:
        body_html = (
            '<div class="empty"><div class="empty-icon">&#128444;</div>'
            '<div style="max-width:360px;text-align:center">{}</div></div>'
        ).format(status_msg)
    else:
        body_html = (
            '<div class="empty"><div class="empty-icon">&#128444;</div>'
            '<div>No images matched &ldquo;{}&rdquo;</div></div>'
        ).format(query)

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="format-detection" content="telephone=no">'
        '<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>💬</text></svg>">'
        '<title>Image Search: ' + query + '</title>'
        '<style>' + CSS + '</style></head><body>'
        '<div class="header">'
        '<h1><a href="/">&#128172;</a></h1>'
        '<div class="search-bar">'
        '<div class="search-input-wrap">'
        '<input type="text" id="searchInput" placeholder="Search images..." value="'
        + query.replace('"', '&quot;') +
        '" onkeydown="if(event.key===\'Enter\')setSearchMode(searchMode)">'
        '<button type="button" class="search-input-clear" onclick="clearMainSearch()" title="Clear" tabindex="-1">&times;</button>'
        '</div>'
        '<button id="modeMessagesBtn" onclick="setSearchMode(\'messages\')">Messages</button>'
        '<button id="modeImagesBtn" class="active" onclick="setSearchMode(\'images\')">Images</button>'
        '</div>'
        '</div>'
        '<div class="main"><div class="results-pane">'
        + body_html +
        '</div></div>'
        '<script>'
        'var searchMode = "images";'
        'function setSearchMode(mode){'
        'searchMode=mode;'
        'var msgBtn=document.getElementById("modeMessagesBtn"),imgBtn=document.getElementById("modeImagesBtn");'
        'if(msgBtn)msgBtn.classList.toggle("active",mode==="messages");'
        'if(imgBtn)imgBtn.classList.toggle("active",mode==="images");'
        'var q=document.getElementById("searchInput").value.trim();'
        'if(!q)return;'
        'window.location.href=(mode==="messages"?"/search?q=":"/search/images?q=")+encodeURIComponent(q);}'
        'function clearMainSearch(){'
        'var input=document.getElementById("searchInput");'
        'input.value="";'
        'window.location.href="/";}'
        '</script>'
        '</body></html>'
    )


# ── Stats ─────────────────────────────────────────────────────────────────────


# MIME type overrides for browser compatibility
MIME_OVERRIDES = {
    '.mov': 'video/mp4',
    '.MOV': 'video/mp4',
    '.heic': 'image/jpeg',
    '.HEIC': 'image/jpeg',
    '.heif': 'image/jpeg',
    '.HEIF': 'image/jpeg',
}

def convert_heic_to_jpeg(path):
    """Convert HEIC file to JPEG bytes. Returns None if conversion fails."""
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        from PIL import Image
        img = Image.open(str(path))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"HEIC conversion failed for {path}: {e}")
        return None


@app.route("/attachments/<int:archive_id>/<path:filepath>")
def serve_attachment(archive_id, filepath):
    """Serve attachment files, converting HEIC to JPEG for browser compatibility."""
    from flask import send_file, abort
    conn = get_db()
    row = conn.execute("SELECT path FROM archives WHERE id=?", (archive_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    # FORK: check every recognized attachment-folder name instead of only
    # the literal "attachments" -- see the ATTACHMENT_DIR_NAMES comment
    # near the top of this file for why.
    candidates = [Path(row["path"]) / name / filepath for name in ATTACHMENT_DIR_NAMES]
    full_path = next((c for c in candidates if c.is_file()), None)
    if full_path is None:
        abort(404)

    suffix = full_path.suffix
    # Convert HEIC to JPEG on the fly
    if suffix.lower() in ('.heic', '.heif'):
        buf = convert_heic_to_jpeg(full_path)
        if buf:
            return Response(buf, mimetype='image/jpeg')
        # Fall through to serve raw if conversion fails

    # Override MIME type for MOV → video/mp4 so Chrome attempts H.264 playback
    mime = MIME_OVERRIDES.get(suffix, None) or mimetypes.guess_type(str(full_path))[0] or 'application/octet-stream'
    return send_file(str(full_path), mimetype=mime)


@app.route("/api/stats")
def stats():
    conn = get_db()
    msgs  = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    conn.close()
    return jsonify({"messages": msgs, "conversations": convs})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6333, debug=False)
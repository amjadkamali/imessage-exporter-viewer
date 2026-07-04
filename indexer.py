#!/usr/bin/env python3
"""
iMessage Archive Indexer

- Auto-discovers export directories under ARCHIVE_ROOT
- Indexes all of them into SQLite, deduplicating across archives
- Stores archive_dir per message for dynamic attachment serving

FORKED to work with a different attachment-storage layout: the upstream
project (https://github.com/mbaran5/imessage-exporter-viewer) assumes the
standard `imessage-exporter -c clone/basic/full` layout, where attachments
are physically copied into a lowercase "attachments/" folder next to the
HTML. This fork's source pipeline instead uses `-c disabled` with
attachments referenced in place under separately-named "Attachments/" and
"StickerCache/" folders (capitalized, matching Apple's own naming). Three
things were changed to support that -- search "FORK:" for each one.
"""
import os
import re
import sqlite3
import hashlib
import html as _html
import time
import json
from pathlib import Path
from datetime import datetime

ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", "/archives")
DB_PATH = os.environ.get("DB_PATH", "/data/imessage.db")
MODEL_DIR = os.environ.get("MODEL_DIR", "/data/models")
ADDRESSBOOK_CACHE_DIR = os.environ.get("ADDRESSBOOK_CACHE_DIR", "/addressbook-cache")
MY_HANDLES = os.environ.get("IMESSAGE_MY_HANDLES", "")

IMAGE_EXTS = frozenset({'.heic', '.heif', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'})

MOBILECLIP_S0_URL = "https://docs-assets.developer.apple.com/ml-research/datasets/mobileclip/mobileclip_s0.pt"
MOBILECLIP_S0_PATH = Path(MODEL_DIR) / "mobileclip_s0.pt"

# FORK: recognize either attachment-folder naming convention. Case matters
# on Linux/Docker (unlike default macOS), so "Attachments" and "attachments"
# are genuinely different names, not just a style choice.
ATTACHMENT_DIR_NAMES = ("attachments", "Attachments", "StickerCache")
ATTACHMENT_SRC_RE = re.compile(r'src="((?:attachments|Attachments|StickerCache)/[^"]+)"')
ATTACHMENT_HREF_RE = re.compile(r'href="((?:attachments|Attachments|StickerCache)/[^"]+)"')


def ensure_mobileclip_checkpoint():
    """Download the MobileCLIP-S0 weights on first use (~30 MB)."""
    MOBILECLIP_S0_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not MOBILECLIP_S0_PATH.exists():
        import urllib.request
        print(f"Downloading MobileCLIP-S0 checkpoint to {MOBILECLIP_S0_PATH} ...")
        urllib.request.urlretrieve(MOBILECLIP_S0_URL, str(MOBILECLIP_S0_PATH))
        print("Download complete.")
    return str(MOBILECLIP_S0_PATH)


MONTHS = {
    "Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
    "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12
}

# ── Discovery ────────────────────────────────────────────────────────────────

def is_export_dir(path, shared_attachment_root=None):
    """
    Return True if path looks like an imessage-exporter output directory:
    it has .html files, and either has its own attachments folder OR
    (when shared_attachment_root is given) a shared attachments folder
    exists at that root instead. The latter supports a layout where
    attachments are kept in one folder outside imessage-exporter's own
    control entirely (e.g. an independent rsync from the live Messages
    Attachments folder), with multiple separate export runs' .html files
    referencing back into it, rather than each run copying its own.
    """
    p = Path(path)
    has_html = any(p.glob("*.html"))
    if not has_html:
        return False
    if any((p / name).is_dir() for name in ATTACHMENT_DIR_NAMES):
        return True
    if shared_attachment_root is not None:
        shared = Path(shared_attachment_root)
        return any((shared / name).is_dir() for name in ATTACHMENT_DIR_NAMES)
    return False


def discover_archives(root, quiet=False):
    """
    Recursively scan root for imessage export directories.
    Returns list of Path objects sorted by directory mtime (oldest first).
    quiet=True suppresses the discovery log lines -- used when this gets
    called repeatedly from the file-change watch loop, where printing the
    full archive list on every poll would spam the container logs.
    """
    root = Path(root)
    if not root.exists():
        if not quiet:
            print(f"WARNING: ARCHIVE_ROOT not found: {root}")
        return []

    found = []
    if is_export_dir(root, shared_attachment_root=root):
        found.append(root)

    for child in sorted(root.iterdir()):
        if child.is_dir() and is_export_dir(child, shared_attachment_root=root):
            found.append(child)
        elif child.is_dir():
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and is_export_dir(grandchild, shared_attachment_root=root):
                    found.append(grandchild)

    found.sort(key=lambda p: p.stat().st_mtime)
    if not quiet:
        print(f"Discovered {len(found)} archive(s) under {root}:")
        for i, p in enumerate(found):
            html_count = len(list(p.glob("*.html")))
            print(f"  [{i}] {p} ({html_count} conversations)")
    return found

# ── Timestamp parsing ────────────────────────────────────────────────────────

def parse_timestamp(ts_str):
    if not ts_str or 'invalid' in ts_str.lower():
        return None
    match = re.match(
        r'(\w+)\s+(\d+),\s+(\d{4})\s+(\d+):(\d+):(\d+)\s+(AM|PM)',
        ts_str.strip()
    )
    if not match:
        return None
    mon, day, year, hour, minute, second, ampm = match.groups()
    if mon not in MONTHS:
        return None
    hour = int(hour)
    if ampm == 'PM' and hour != 12: hour += 12
    elif ampm == 'AM' and hour == 12: hour = 0
    try:
        dt = datetime(int(year), MONTHS[mon], int(day), hour, int(minute), int(second))
        if dt.year < 2005 or dt.year > 2035:
            return None
        return dt.isoformat()
    except ValueError:
        return None

# ── HTML parsing ─────────────────────────────────────────────────────────────

def is_phone_number(s):
    """
    Return True for E.164-style phone numbers: a leading "+" followed by
    7-15 digits (E.164's own length bounds for a full international
    number). Covers North American numbers (+1 plus 10 digits) as well as
    any other country's numbers, without assuming a fixed digit count.
    Rejects short codes and alphanumeric sender IDs (no leading "+" in
    this system's exports) and email handles.
    """
    return bool(re.fullmatch(r'\+\d{7,15}', s.strip()))


def is_group_filename(filename):
    """
    Return True if the filename (stem) represents a group conversation.
    Two cases:
      1. Comma-separated phone numbers: '+15551234567, +15559876543.html'
      2. A named group (contains spaces / non-phone characters):
         'My Group Chat Name - 8.html'
    A single-person conversation is ONLY a bare phone number or email handle
    with no commas and no spaces.
    """
    stem = Path(filename).stem
    if ',' in stem:
        return True
    if ' ' in stem:
        return True
    return False


def extract_contact_name(html_content, filename):
    """
    Extract the first non-Me sender name from RECEIVED messages in a
    SINGLE-PERSON conversation. Only runs on files whose stem is a bare
    phone number (no commas, no spaces). Returns None for group
    conversations and named groups. By only scanning received messages
    (not sent), we always find the contact's name, even if the user ran
    imessage-exporter with a custom -m name replacing "Me".
    """
    if is_group_filename(filename):
        return None

    blocks = re.split(r'(?=<div class="message")', html_content)
    for block in blocks:
        if '<div class="message">' not in block:
            continue
        if 'class="sent' in block:
            continue
        match = re.search(r'<span class="sender">([^<]+)</span>', block)
        if match:
            name = match.group(1).strip()
            if name and name != "Me":
                return name
    return None


def build_group_display_name(filename, phone_to_name, my_handles_norm=frozenset()):
    """
    Given a group filename like '+15551234567, +15559876543.html' and a
    mapping of phone -> resolved name, return a human-readable group name.
    - Named groups (no commas): preserve the stem name as-is.
    - Phone-number groups: substitute only standard NA numbers
      (+1XXXXXXXXXX); deduplicate resolved names so two numbers for the
      same person don't produce 'Jane Smith, Jane Smith'.
    my_handles_norm, when given, strips your own handle out of the
    participant list first -- see strip_self_handles() for why this
    matters (a Messages glitch, not a real extra participant).
    """
    stem = Path(filename).stem
    if ',' not in stem:
        return stem

    parts = effective_group_participants(filename, my_handles_norm)
    if parts is None:
        parts = [p.strip() for p in stem.split(',')]
    resolved = []
    seen_names = set()
    for part in parts:
        if is_phone_number(part):
            name = phone_to_name.get(part, part)
        else:
            name = part
        if name not in seen_names:
            resolved.append(name)
            seen_names.add(name)
    return ', '.join(resolved)


def parse_messages(html_content):
    messages = []

    def strip_replies(html):
        """Remove <div class="replies">...</div> subtrees, returning cleaned html."""
        result = []
        pos = 0
        while pos < len(html):
            start = html.find('<div class="replies">', pos)
            if start == -1:
                result.append(html[pos:])
                break
            result.append(html[pos:start])
            depth = 1
            i = start + len('<div class="replies">')
            while i < len(html) and depth > 0:
                o = html.find('<div', i)
                c = html.find('</div>', i)
                if o != -1 and (c == -1 or o < c):
                    depth += 1
                    i = o + 4
                elif c != -1:
                    depth -= 1
                    i = c + 6
                else:
                    break
            pos = i
        return ''.join(result)

    def parse_block(block, raw_html_override=None):
        """
        Extract message fields from a stripped block.
        raw_html_override: if provided, store this as raw_html instead of block.
        Returns a dict or None if the block has no timestamp.
        """
        # FORK: the original regex here required <a ...> to appear with
        # ZERO characters between it and <span class="timestamp">. Real
        # imessage-exporter output always has whitespace/newlines there
        # (the <a> tag is indented on its own line), so this never matched
        # -- it silently fell through to the fallback below, which then
        # only captured that leading whitespace (empty after .strip()),
        # producing a None timestamp for every message. This is what was
        # causing the "? -> ?" date range and probably degraded search
        # sort-by-date too. Added \s* to match real exporter output.
        ts_raw = None
        m = re.search(r'<span class="timestamp">\s*<a[^>]*>([^<]+)</a>', block)
        if m:
            ts_raw = m.group(1).strip()
        else:
            m = re.search(r'<span class="timestamp">([^<(]+)', block)
            if m:
                ts_raw = m.group(1).strip()

        sender = None
        m = re.search(r'<span class="sender">([^<]+)</span>', block)
        if m:
            sender = m.group(1).strip()

        text_parts = []
        for bubble in re.findall(r'<span class="bubble">(.*?)</span>', block, re.DOTALL):
            text_parts.append(re.sub(r'<[^>]+>', '', bubble))
        text = _html.unescape(' '.join(text_parts)).strip() or None

        # FORK: originally hardcoded to only recognize 'src="attachments/...'.
        # Now matches this fork's "Attachments/"/"StickerCache/" folders too.
        # Also matches href= links, not just src= -- image attachments are
        # rendered as <img src="...">, but non-image attachments (PDFs,
        # documents, anything imessage-exporter doesn't inline-preview) are
        # rendered as <a href="...">filename</a>. The original upstream
        # indexer only ever checked src=, so non-image attachments were
        # silently never recorded as attachments at all -- has_attachment
        # and attachment_path stayed unset for them even though app.py's
        # own URL-rewriting logic already handled href= links correctly.
        attachments = ATTACHMENT_SRC_RE.findall(block) + ATTACHMENT_HREF_RE.findall(block)

        guid = None
        m = re.search(r'message-guid=([A-F0-9a-f-]+)', block)
        if m:
            guid = m.group(1)

        direction = 'sent' if 'class="sent' in block else 'received'
        ts_iso = parse_timestamp(ts_raw)
        ts_stable = re.sub(r'\s*\(.*$', '', ts_raw or '').strip()
        ts_stable = re.sub(r'\s+', ' ', ts_stable)
        content_hash = hashlib.md5(
            f"{ts_stable}{direction}{text or ''}".encode()
        ).hexdigest()
        msg_id = guid if guid else content_hash

        return {
            'id': msg_id,
            'content_hash': content_hash,
            'timestamp_raw': ts_raw,
            'timestamp': ts_iso,
            'sender': sender,
            'text': text,
            'attachments': attachments,
            'direction': direction,
            'raw_html': raw_html_override if raw_html_override is not None else block,
        }

    def parse_announcement_block(block):
        """
        Parse a <div class="announcement">...</div> block: a system event
        (renamed conversation, added/removed participant, deleted message,
        etc), not a real message. These carry no message-guid at all, so
        they're given a stable pseudo-GUID derived from their own full
        text -- which already includes their own timestamp text, exactly
        matching merge_html_exports.py's identical approach to this
        identical problem (see find_announcement_containers() there),
        including deliberately hashing the FULL text (timestamp included):
        two "Bob left the conversation" events at genuinely different
        times must hash differently, or the second would be wrongly
        dropped as an incorrect duplicate of the first.
        raw_html is preserved as-is and rendered directly by the frontend
        -- the app's own CSS already has a dedicated `.announcement` rule
        (centered, muted, small) that's simply never been exercised until
        now, since this is the first time these blocks get parsed as
        their own entries instead of silently vanishing into whichever
        message happened to sit next to them.
        """
        ts_raw = None
        m = re.search(r'<span class="timestamp">\s*<a[^>]*>([^<]+)</a>', block)
        if m:
            ts_raw = m.group(1).strip()
        else:
            m = re.search(r'<span class="timestamp">([^<(]+)', block)
            if m:
                ts_raw = m.group(1).strip()

        text = re.sub(r'<[^>]+>', ' ', block)
        text = _html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            return None

        pseudo_guid = "ANNOUNCEMENT:" + hashlib.sha1(text.encode('utf-8')).hexdigest()
        ts_iso = parse_timestamp(ts_raw)

        return {
            'id': pseudo_guid,
            'content_hash': pseudo_guid,
            'timestamp_raw': ts_raw,
            'timestamp': ts_iso,
            'sender': None,
            'text': text,
            'attachments': [],
            'direction': 'announcement',
            'raw_html': block,
        }

    guid_to_block_start = {}
    for gm in re.finditer(r'<div class="message">(?:(?!<div class="message">).)*?message-guid=([A-F0-9a-f-]+)',
                          html_content, re.DOTALL):
        guid_to_block_start[gm.group(1)] = gm.start()

    def extract_original_block_fast(guid):
        start = guid_to_block_start.get(guid)
        if start is None:
            return None
        depth = 1
        i = start + len('<div class="message">')
        while i < len(html_content) and depth > 0:
            o = html_content.find('<div', i)
            c = html_content.find('</div>', i)
            if o != -1 and (c == -1 or o < c):
                depth += 1
                i = o + 4
            elif c != -1:
                depth -= 1
                i = c + 6
            else:
                break
        return html_content[start:i]

    stripped = strip_replies(html_content)
    # FORK: also split on announcement divs, not just message divs -- see
    # parse_announcement_block() for why these need their own handling.
    # Real imessage-exporter output renders this as `<div class ="announcement">`
    # (note the space BEFORE the equals sign, not after) -- confirmed directly
    # from the exporter's own source (format_announcement() in html.rs), not
    # guessed, since getting this wrong here means silently matching nothing.
    blocks = re.split(r'(?=<div class="message">|<div class\s*=\s*"announcement">)', stripped)
    seen_guids = set()
    for block in blocks:
        if '<div class="message">' in block:
            msg = parse_block(block)
        elif re.match(r'\s*<div class\s*=\s*"announcement">', block):
            msg = parse_announcement_block(block)
        else:
            continue
        if msg is None:
            continue
        guid = msg['id'] if re.fullmatch(r'[A-F0-9a-f-]{36}', msg['id']) else None
        if guid:
            orig_block = extract_original_block_fast(guid)
            if orig_block:
                msg['raw_html'] = orig_block
            seen_guids.add(guid)
        messages.append(msg)

    for m_outer in re.finditer(r'<div class="message" id="r-([A-F0-9a-f-]+)">', html_content):
        guid = m_outer.group(1)
        if guid in seen_guids:
            continue
        start = m_outer.start()
        depth = 1
        i = start + len(m_outer.group(0))
        while i < len(html_content) and depth > 0:
            o = html_content.find('<div', i)
            c = html_content.find('</div>', i)
            if o != -1 and (c == -1 or o < c):
                depth += 1
                i = o + 4
            elif c != -1:
                depth -= 1
                i = c + 6
            else:
                break
        standalone_block = html_content[start:i]
        stripped_block = strip_replies(standalone_block)
        msg = parse_block(stripped_block, raw_html_override=standalone_block)
        if msg:
            seen_guids.add(guid)
            messages.append(msg)

    messages.sort(key=lambda x: (x['timestamp'] or ''))
    return messages

# ── Database ─────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS archives (
        id INTEGER PRIMARY KEY,
        path TEXT UNIQUE NOT NULL,
        indexed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY,
        filename TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        msg_count INTEGER DEFAULT 0,
        first_date TEXT,
        last_date TEXT,
        indexed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id INTEGER NOT NULL,
        archive_id INTEGER NOT NULL,
        timestamp TEXT,
        timestamp_raw TEXT,
        sender TEXT,
        text TEXT,
        direction TEXT,
        has_attachment INTEGER DEFAULT 0,
        attachment_path TEXT,
        raw_html TEXT,
        content_hash TEXT,
        FOREIGN KEY (conversation_id) REFERENCES conversations(id),
        FOREIGN KEY (archive_id) REFERENCES archives(id)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        text,
        sender,
        content=messages,
        content_rowid=rowid,
        tokenize="porter unicode61"
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
    CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);
    CREATE INDEX IF NOT EXISTS idx_conv_last_date ON conversations(last_date DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_content_hash
        ON messages(conversation_id, content_hash);
    CREATE TABLE IF NOT EXISTS image_embeddings (
        attachment_path TEXT NOT NULL,
        archive_id INTEGER NOT NULL,
        message_id TEXT NOT NULL,
        embedding BLOB NOT NULL,
        PRIMARY KEY (attachment_path, archive_id)
    );
    CREATE INDEX IF NOT EXISTS idx_embeddings_msg ON image_embeddings(message_id);

    -- FORK addition: contact-based conversation grouping. A 1:1
    -- conversation whose handle resolves to a known Address Book contact
    -- gets linked here to every OTHER conversation that resolves to the
    -- SAME contact (e.g. their phone number's file and their email's
    -- file), so the app can present them as one merged conversation.
    -- Deliberately additive and separate from conversations/messages,
    -- which are never modified by this -- see populate_contact_groups().
    CREATE TABLE IF NOT EXISTS contact_groups (
        contact_key TEXT PRIMARY KEY,
        display_name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS conversation_contact_group (
        conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id),
        contact_key TEXT NOT NULL REFERENCES contact_groups(contact_key)
    );
    CREATE INDEX IF NOT EXISTS idx_ccg_contact_key ON conversation_contact_group(contact_key);

    -- FORK addition: real participant handles for a group chat, extracted
    -- from chat.db's own chat_handle_join table (imessage-exporter's
    -- filenames carry none for a named group, and only an unreliable
    -- auto-generated guess for a "guessed name" group) -- see
    -- populate_raw_participants(). Purely descriptive/display data by
    -- default; ALSO consulted (not required, just consulted) by
    -- populate_contact_groups() for two things built on top of it: a
    -- named-group collision sanity check, and resolved participant-set
    -- matching for guessed-name groups -- see those sections there for
    -- exactly what each does and doesn't require.
    CREATE TABLE IF NOT EXISTS conversation_raw_participants (
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        handle TEXT NOT NULL,
        PRIMARY KEY (conversation_id, handle)
    );
    """)
    conn.commit()


def get_or_create_archive(conn, path):
    row = conn.execute("SELECT id FROM archives WHERE path=?", (str(path),)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO archives (path, indexed_at) VALUES (?, datetime('now'))",
        (str(path),)
    )
    conn.commit()
    return cur.lastrowid


def index_file(conn, html_path, archive_id, phone_to_name, my_handles_norm=frozenset()):
    filename = Path(html_path).name
    name = Path(html_path).stem
    content = Path(html_path).read_text(encoding='utf-8', errors='replace')
    messages = parse_messages(content)
    if not messages:
        return 0

    if is_group_filename(filename):
        display_name = build_group_display_name(filename, phone_to_name, my_handles_norm)
    else:
        contact_name = extract_contact_name(content, filename)
        display_name = contact_name if contact_name else name

    row = conn.execute("SELECT id FROM conversations WHERE filename=?", (filename,)).fetchone()
    if row:
        conv_id = row[0]
        current = conn.execute("SELECT name FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if current and (current[0].startswith('+') or current[0] == name):
            conn.execute("UPDATE conversations SET name=? WHERE id=?", (display_name, conv_id))
    else:
        cur = conn.execute(
            "INSERT INTO conversations (filename, name, msg_count, indexed_at) VALUES (?,?,0,datetime('now'))",
            (filename, display_name)
        )
        conv_id = cur.lastrowid

    batch = []
    for msg in messages:
        att_path = msg['attachments'][0] if msg['attachments'] else None
        batch.append((
            msg['id'], conv_id, archive_id,
            msg['timestamp'], msg['timestamp_raw'],
            msg['sender'], msg['text'], msg['direction'],
            1 if msg['attachments'] else 0,
            att_path,
            msg['raw_html'],
            msg['content_hash'],
        ))
        if len(batch) >= 1000:
            conn.executemany(
                "INSERT OR IGNORE INTO messages "
                "(id,conversation_id,archive_id,timestamp,timestamp_raw,sender,text,direction,has_attachment,attachment_path,raw_html,content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                batch
            )
            batch = []
    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO messages "
            "(id,conversation_id,archive_id,timestamp,timestamp_raw,sender,text,direction,has_attachment,attachment_path,raw_html,content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            batch
        )
    conn.commit()
    return len(messages)


def dedup_cross_archive_messages(conn):
    """
    Remove duplicate messages caused by indexing the same conversation from
    multiple archive snapshots.

    Safe dedup rules -- ALL conditions must be true to delete a message:
      1. Same conversation_id and direction
      2. Same text content (or both empty/attachment-only)
      3. Timestamps within 5 seconds of each other
      4. DIFFERENT archive_id <- critical: never dedupe within the same
         archive, which would collapse genuine rapid-fire messages sent
         1-2s apart

    When a GUID and MD5 pair match, the MD5 (older exporter, no GUID) is
    dropped. When two GUIDs match (both newer exporter, small timestamp
    drift), the later timestamp is dropped -- keeping the earlier (more
    authoritative) record. When two MD5s match (rare), the duplicate is
    dropped arbitrarily.
    """
    print("Deduplicating cross-archive duplicate messages...")
    from datetime import datetime as _dt
    from itertools import groupby as _groupby

    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, conversation_id, archive_id, direction, text, timestamp
        FROM messages
        WHERE timestamp IS NOT NULL
        ORDER BY conversation_id, direction, COALESCE(text, ''), timestamp
    """).fetchall()

    to_delete = set()

    def _key(r):
        return (r['conversation_id'], r['direction'], r['text'] or '')

    for _, group in _groupby(rows, key=_key):
        group = list(group)
        for i in range(len(group) - 1):
            a, b = group[i], group[i + 1]
            if a['id'] in to_delete or b['id'] in to_delete:
                continue
            if a['archive_id'] == b['archive_id']:
                continue
            try:
                t1 = _dt.fromisoformat(a['timestamp'])
                t2 = _dt.fromisoformat(b['timestamp'])
            except ValueError:
                continue
            if abs((t2 - t1).total_seconds()) > 5:
                continue

            a_is_guid = '-' in a['id'] and len(a['id']) == 36
            b_is_guid = '-' in b['id'] and len(b['id']) == 36
            if a_is_guid and not b_is_guid:
                to_delete.add(b['id'])
            elif b_is_guid and not a_is_guid:
                to_delete.add(a['id'])
            else:
                to_delete.add(b['id'])

    if to_delete:
        to_delete = list(to_delete)
        for i in range(0, len(to_delete), 500):
            chunk = to_delete[i:i+500]
            conn.execute(
                "DELETE FROM messages WHERE id IN ({})".format(
                    ','.join('?' * len(chunk))), chunk)
        conn.commit()
        print(f"  Removed {len(to_delete)} cross-archive duplicate messages.")
    else:
        print("  No cross-archive duplicates found.")


def update_conversation_stats(conn):
    """Update message counts and date ranges for all conversations."""
    print("Updating conversation stats...")
    conn.execute("""
        UPDATE conversations SET
            msg_count = (SELECT COUNT(*) FROM messages WHERE conversation_id = conversations.id),
            first_date = (SELECT MIN(timestamp) FROM messages WHERE conversation_id = conversations.id AND timestamp IS NOT NULL),
            last_date = (SELECT MAX(timestamp) FROM messages WHERE conversation_id = conversations.id AND timestamp IS NOT NULL),
            indexed_at = datetime('now')
    """)
    conn.commit()

# ── Image embedding ──────────────────────────────────────────────────────────

def embed_images(conn):
    """
    Embed all un-processed image attachments using MobileCLIP-S0.
    Embeddings are stored as raw float32 blobs (512 dims) in image_embeddings.
    Incremental: already-embedded (attachment_path, archive_id) pairs are skipped.
    """
    try:
        import torch
        import numpy as np
        import mobileclip
        from PIL import Image
    except ImportError as e:
        print(f"Skipping image embedding: {e}")
        return

    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.id, m.attachment_path, m.archive_id, a.path AS archive_path
        FROM messages m
        JOIN archives a ON a.id = m.archive_id
        WHERE m.has_attachment = 1
          AND m.attachment_path IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM image_embeddings e
              WHERE e.attachment_path = m.attachment_path
                AND e.archive_id = m.archive_id
          )
    """).fetchall()

    image_rows = [r for r in rows
                  if Path(r['attachment_path']).suffix.lower() in IMAGE_EXTS]
    if not image_rows:
        print("Image embedding: nothing new to embed.")
        return

    total = len(image_rows)
    ckpt = ensure_mobileclip_checkpoint()
    print(f"Loading MobileCLIP-S0 for {total:,} images...")
    model, _, preprocess = mobileclip.create_model_and_transforms(
        'mobileclip_s0', pretrained=ckpt
    )
    model.eval()

    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    BATCH = 32
    done = errors = 0
    for i in range(0, total, BATCH):
        chunk = image_rows[i:i + BATCH]
        tensors, valid = [], []
        for row in chunk:
            full_path = Path(row['archive_path']) / row['attachment_path']
            if not full_path.exists():
                errors += 1
                continue
            try:
                img = Image.open(str(full_path)).convert('RGB')
                tensors.append(preprocess(img))
                valid.append(row)
            except Exception:
                errors += 1
        if not tensors:
            continue
        with torch.inference_mode():
            feats = model.encode_image(torch.stack(tensors))
            feats = feats / feats.norm(dim=-1, keepdim=True)
            arr = feats.numpy().astype(np.float32)
        conn.executemany(
            "INSERT OR REPLACE INTO image_embeddings "
            "(attachment_path, archive_id, message_id, embedding) VALUES (?,?,?,?)",
            [(valid[j]['attachment_path'], valid[j]['archive_id'],
              valid[j]['id'], arr[j].tobytes())
             for j in range(len(valid))]
        )
        conn.commit()
        done += len(valid)
        if done % 1000 < BATCH or done >= total:
            pct = 100 * done / total
            print(f"  Image embedding: {done:,}/{total:,} ({pct:.0f}%)", flush=True)
    print(f"Image embedding: {done:,} embedded, {errors} skipped")

# ── Contact grouping ─────────────────────────────────────────────────────────
# FORK addition: links 1:1 conversations that resolve to the same Address
# Book contact (e.g. someone's phone number file and their email file) so
# the app can present them as one merged conversation. Ported directly
# from merge_by_contact.py's own handle-resolution logic (norm_phone,
# norm_email, norm_handle, build_handle_map) rather than reimplemented,
# since that logic is already correct and already tested; only the
# Address Book path source changes here, pointed at the read-only cache
# mount this container gets instead of the live system path merge_by_
# contact.py itself uses. See conversation_contact_group in init_db() for
# why this is additive rather than a rewrite of conversations/messages.

def _strip_stray_quotes(s):
    """
    Strip any leading and/or trailing quote character (single or double),
    independently of each other -- NOT as a matched pair. Guards against a
    common, easy mistake when setting IMESSAGE_MY_HANDLES: quoting the
    whole KEY=VALUE line in a docker-compose.yml `environment:` list entry
    doesn't do what it looks like it does -- YAML only treats a value as
    quoted when the quote is the very FIRST character of the scalar, not
    when it appears after a literal `KEY=` prefix, so the quote characters
    end up baked into the value verbatim. Worse, once a multi-handle
    string like `"+1555 user@x.com"` gets split on whitespace, the
    surviving quote characters land on DIFFERENT tokens -- the first
    token keeps only the leading quote, the last token keeps only the
    trailing one -- so neither token has a matched pair at its own
    boundaries; each end has to be checked and stripped independently.
    A phone number survives this by accident anyway (norm_phone strips
    every non-digit character regardless), but an email address doesn't,
    since email normalization was never written to expect a literal quote
    character -- "user@x.com\"" then silently fails to match the clean
    "user@x.com" computed from an actual filename, with no error anywhere
    to point at why.
    """
    s = s.strip()
    if s and s[0] in ('"', "'"):
        s = s[1:]
    if s and s[-1] in ('"', "'"):
        s = s[:-1]
    return s.strip()


def norm_phone(s):
    d = re.sub(r"\D", "", _strip_stray_quotes(s or ""))
    if not d:
        return _strip_stray_quotes(s or "").lower()
    if len(d) == 11 and d[0] == "1":
        return d[1:]
    return d

def norm_email(s):
    return _strip_stray_quotes(s or "").lower()

def norm_handle(h):
    return norm_email(h) if "@" in h else norm_phone(h)


def strip_self_handles(participants, my_handles_norm):
    """
    Messages occasionally glitches and inserts your own number/email into
    a group chat's participant list -- a known, occasional bug, not a
    real additional participant. Left alone, this can make an entirely
    normal 1:1 conversation look like a 2-person group, or add a spurious
    extra name to a real group's display name.
    If every participant is one of your own handles, this is a genuine
    chat-with-yourself and is left completely untouched. Otherwise, your
    own handles are dropped before anything else happens.
    Ported directly from merge_by_contact.py's function of the same name;
    same behavior, since it's already correct there.
    """
    if not my_handles_norm:
        return participants
    filtered = [p for p in participants if norm_handle(p) not in my_handles_norm]
    return filtered if filtered else participants


GUESSED_NAME_RE = re.compile(r'\d+\s+others?\b', re.IGNORECASE)

def looks_like_guessed_name(stem):
    """
    macOS/imessage-exporter sometimes names a chat using an auto-generated
    summary like "John, Jane & 3 others" when the group has no custom
    name set. That summary is itself a guess about membership -- not a
    stable identifier -- so treating it as a comma-separated handle list
    would mean trying to look up nonsense like "John" or "Jane & 3
    others" as if they were phone numbers or emails. Harmless in
    practice (they simply won't resolve), but wasted work, and not
    impossible in principle for one to accidentally collide with some
    unrelated real handle. Files with this kind of name still merge with
    each other via the ordinary exact-filename match every conversation
    already gets (see conversations.filename's UNIQUE constraint) --
    they just never become eligible for the participant-set matching
    effective_group_participants() enables for genuine handle-based
    groups. Ported directly from merge_by_contact.py's function of the
    same name and same purpose.
    """
    return bool(GUESSED_NAME_RE.search(stem))


def effective_group_participants(filename, my_handles_norm):
    """
    For a comma-separated group filename, return the participant list
    after stripping any of your own handles that snuck in via the
    glitch strip_self_handles() guards against. Returns None for named
    groups (e.g. "Book Club - 5.html" -- no individual handles are
    encoded in a named group's filename to strip in the first place),
    auto-generated "guessed name" groups (see looks_like_guessed_name()),
    and non-group filenames.
    """
    stem = Path(filename).stem
    if ',' not in stem or looks_like_guessed_name(stem):
        return None
    participants = [p.strip() for p in stem.split(',') if p.strip()]
    return strip_self_handles(participants, my_handles_norm)


def get_addressbook_cache_paths(cache_dir):
    """
    Mirror merge_by_contact.py's default_addressbook_paths(), but pointed
    at this container's read-only Address Book cache mount instead of the
    live system path -- the cache is what update-addressbook-cache logic
    (see imessage-incremental-sync.sh) keeps current, since the live path
    is TCC-protected and unreachable from here regardless.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return []
    found = []
    main = cache_dir / "AddressBook-v22.abcddb"
    if main.is_file():
        found.append(str(main))
    for src in sorted(cache_dir.glob("Sources/*/AddressBook-v22.abcddb")):
        found.append(str(src))
    return found


def build_handle_map(ab_paths):
    """
    Return {normalized_handle: (contact_key, display_name)} across every
    given Address Book DB. contact_key is namespaced by db path so
    records from different source DBs (iCloud/Exchange/On My Mac) can
    never collide just because they happen to share the same internal
    row number. Schema is introspected defensively since ZABCDRECORD's
    exact columns can vary slightly across macOS versions.
    """
    handle_map = {}
    for dbp in ab_paths:
        try:
            con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        except Exception as e:
            print(f"  (skipping unreadable Address Book: {dbp}: {e})")
            continue
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "ZABCDRECORD" not in tables:
                con.close(); continue
            names = {}
            for pk, fn, ln, org in con.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"):
                nm = " ".join(x for x in (fn, ln) if x) or (org or f"record{pk}")
                names[pk] = nm
            def add(owner, raw):
                if owner is None or not raw:
                    return
                key = norm_handle(raw)
                if key:
                    handle_map[key] = (f"{dbp}#{owner}", names.get(owner, f"record{owner}"))
            if "ZABCDPHONENUMBER" in tables:
                for owner, num in con.execute(
                    "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    add(owner, num)
            if "ZABCDEMAILADDRESS" in tables:
                for owner, addr in con.execute(
                    "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    add(owner, addr)
        except Exception as e:
            print(f"  (error reading {dbp}: {e})")
        finally:
            con.close()
    return handle_map


NAMED_GROUP_SUFFIX_RE = re.compile(r'^(.+?)\s*-\s*(\d+)$')


def _raw_participant_sets(conn):
    """
    Return {conversation_id: set(normalized handles)} for every
    conversation that has ANY rows in conversation_raw_participants.
    Normalized (not raw) so a real overlap isn't missed just because two
    exports formatted the same phone number slightly differently, and so
    this can be compared directly against handle_map's own normalized
    keys for guessed-name participant-set matching.
    Deliberately returns nothing for a conversation with no rows at all,
    rather than an empty set -- see how this is used in
    populate_contact_groups()'s named-group collision check, where "no
    data" and "confirmed zero members" need to be treated differently:
    the former can't be used to rule a merge out, the latter can.
    """
    result = {}
    for row in conn.execute("SELECT conversation_id, handle FROM conversation_raw_participants"):
        result.setdefault(row["conversation_id"], set()).add(norm_handle(row["handle"]))
    return result


def populate_contact_groups(conn, cache_dir=ADDRESSBOOK_CACHE_DIR):
    """
    (Re)build contact_groups / conversation_contact_group from the Address
    Book cache. Three cases for a conversation's filename:
      1. Bare handle (1:1): looked up directly in the Address Book.
      2. Comma-separated "group" that collapses to one real participant
         after stripping your own handle (see strip_self_handles(),
         guarding against a real Messages glitch): treated exactly like
         case 1 -- it's a disguised 1:1, not an actual group.
      3. A genuine multi-person group (2+ participants remain after
         stripping): matched by its RESOLVED participant set rather than
         its literal handles, so the same real group of people is
         recognized as one conversation even if a member's handle
         changed between exports (a new phone number, say) -- as long as
         the Address Book has both their old and new handle on file. An
         unresolved participant falls back to their own normalized
         handle as their "identity" for this matching, which still
         correctly matches an identical later export of that same
         unresolved handle, just won't survive that specific person's
         handle changing later (the same known limitation as case 1).
    Named groups (no commas at all) are mostly untouched by any of this --
    there's no handle information in a named group's filename to resolve
    or match by, so they continue to be matched only by exact filename --
    EXCEPT for one specific, narrow case: two or more named groups whose
    stems are identical except for a trailing " - <number>"
    imessage-exporter itself appended to keep filenames unique (see the
    collision-detection block below for why this signals the same
    real-world group split across two internal chat rooms far more often
    than it signals two coincidentally-same-named but genuinely unrelated
    groups). Since a name collision alone can't fully rule out the
    latter, real participant data (see conversation_raw_participants /
    populate_raw_participants()) is consulted as a sanity check where
    available: candidates sharing a name are only actually merged
    together if they share at least ONE known member in common. This is
    deliberately NOT a strict requirement that their full membership
    matches -- real groups gain and lose members over time without
    becoming a different conversation, so demanding total agreement would
    incorrectly split an ordinary, still-ongoing group the moment
    anyone's membership ever changed. It's also deliberately NOT enforced
    at all for a candidate with no participant data on file yet (chat.db
    had no matching GUID to correlate against for it) -- absence of data
    is not evidence of disjoint membership, so that candidate falls back
    to the plain name-based match rather than being excluded on no real
    basis. Only candidates where BOTH have known, disjoint member sets
    get split apart from each other.
    Separately, an auto-generated "guessed name" group (e.g. "John, Jane
    & 3 others.html" -- see looks_like_guessed_name()) is now ALSO
    eligible for the same resolved participant-set matching an ordinary
    comma-separated group gets, using conversation_raw_participants as
    its source of participants instead of its own unreliable filename
    summary. This is what lets a guessed-name file merge with another
    guessed-name file, or with an ordinary comma-separated file, that
    resolves to the exact same contacts -- something that was previously
    impossible, since a guessed-name filename was never parsed for
    participants at all. A guessed-name file with no sidecar data on file
    falls back to matching only by exact filename, same as always.
    Wipes and rebuilds every time so a renamed contact or an updated
    cache take effect immediately, without touching conversations or
    messages at all -- if the cache is stale or missing entirely, this
    just leaves both tables empty and every conversation behaves exactly
    as if grouping didn't exist, the same graceful degradation the sync
    script's own Address Book handling already has.
    """
    ab_paths = get_addressbook_cache_paths(cache_dir)
    if not ab_paths:
        print(f"No address book cache found at {cache_dir}; handle-based contact grouping skipped "
              f"(named-group name-collision merging, which doesn't need it, still runs).")
    handle_map = build_handle_map(ab_paths) if ab_paths else {}

    my_handles_norm = frozenset(norm_handle(h) for h in MY_HANDLES.split() if h.strip())

    # Named-group collision detection: imessage-exporter appends " - <N>"
    # to a group's filename when its custom display_name collides with a
    # DIFFERENT internal chat_identifier that happens to share the exact
    # same name -- a real, documented scenario (macOS's own `chat` table
    # can have multiple rows sharing one display_name with different
    # chat_identifier values, typically from the same real-world group
    # getting split into a new internal chat room after a participant
    # change, an SMS/iMessage transition, or a sync quirk). That can't be
    # told apart from a coincidental, genuinely-unrelated name collision
    # by filename alone, so this block gathers, for every stripped name
    # shared by 2+ named-group filenames, which of those candidates
    # should actually be merged together -- via connected components over
    # "shares at least one known member", not just grouped by name
    # directly. See the docstring above for the full reasoning.
    raw_sets = _raw_participant_sets(conn)

    id_by_filename = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM conversations")}

    prefix_to_ids = {}
    for filename, conv_id in id_by_filename.items():
        stem = Path(filename).stem
        if "," in stem:
            continue
        m = NAMED_GROUP_SUFFIX_RE.match(stem)
        if m:
            prefix_to_ids.setdefault(m.group(1), []).append(conv_id)

    # union-find over conversation_ids sharing a prefix
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    named_group_component = {}  # conv_id -> (prefix, component_root), only for 2+ member components
    for prefix, ids in prefix_to_ids.items():
        if len(ids) < 2:
            continue

        known_ids = [cid for cid in ids if raw_sets.get(cid) is not None]
        unknown_ids = [cid for cid in ids if raw_sets.get(cid) is None]

        # Build overlap-based clusters ONLY among candidates with known
        # participant data first. This matters: if a no-data candidate
        # were unioned in during this same pass, union-find's own
        # transitivity would let it silently bridge two OTHERWISE PROVEN
        # DISJOINT clusters into one (no-data candidate X "can't rule out"
        # joining known cluster A, and separately "can't rule out" joining
        # known cluster B, would transitively merge A and B together even
        # though A and B themselves have confirmed, zero-overlap
        # membership) -- confirmed directly: an earlier version of this
        # check did exactly that when tested against three groups sharing
        # a name, two of which genuinely overlapped and one of which was
        # a completely disjoint, unrelated group, plus a fourth with no
        # participant data on file. The no-data candidate silently pulled
        # the disjoint group into the same merge purely by transitivity.
        for cid in known_ids:
            parent.setdefault(cid, cid)
        for i in range(len(known_ids)):
            for j in range(i + 1, len(known_ids)):
                a, b = known_ids[i], known_ids[j]
                if raw_sets[a] & raw_sets[b]:
                    union(a, b)

        known_components = {}
        for cid in known_ids:
            known_components.setdefault(find(cid), []).append(cid)

        if not known_ids:
            # No evidence at all for this prefix -- nothing to weigh a
            # merge against, so fall back entirely to the plain
            # name-based match this check didn't used to make any
            # exception to.
            for cid in ids:
                parent.setdefault(cid, cid)
            for i in range(1, len(ids)):
                union(ids[0], ids[i])
        elif len(known_components) == 1:
            # Exactly one plausible group under this name -- a no-data
            # candidate can't be ruled out against it, so it joins that
            # one cluster.
            only_root = next(iter(known_components))
            for cid in unknown_ids:
                parent.setdefault(cid, cid)
                union(cid, only_root)
        else:
            # Multiple, mutually-disjoint known clusters share this name
            # -- a no-data candidate has no real basis to be assigned to
            # one over another, so it's left standalone rather than
            # guessed at.
            for cid in unknown_ids:
                parent.setdefault(cid, cid)

        components = {}
        for cid in ids:
            components.setdefault(find(cid), []).append(cid)
        for root, members in components.items():
            if len(members) >= 2:
                for cid in members:
                    named_group_component[cid] = (prefix, root)

    conn.execute("DELETE FROM conversation_contact_group")
    conn.execute("DELETE FROM contact_groups")

    grouped = 0
    group_matched = 0
    for row in conn.execute("SELECT id, filename FROM conversations").fetchall():
        stem = Path(row["filename"]).stem
        is_guessed = looks_like_guessed_name(stem)
        if "," in stem and not is_guessed:
            participants = effective_group_participants(row["filename"], my_handles_norm)
            if not participants:
                continue
            if len(participants) == 1:
                # Disguised 1:1 -- same handling as a bare handle below.
                person = handle_map.get(norm_handle(participants[0]))
                if not person:
                    continue
                contact_key, display_name = person
            else:
                # Genuine multi-person group: build a canonical identity
                # for each participant (their contact_key if known,
                # otherwise their own normalized handle), so the group's
                # key is deterministic and identical no matter which of
                # its several possible export filenames produced it.
                identities = set()
                names = set()
                for p in participants:
                    person = handle_map.get(norm_handle(p))
                    if person:
                        identities.add(person[0])
                        names.add(person[1])
                    else:
                        identities.add("unresolved:" + norm_handle(p))
                        names.add(p)
                contact_key = "group:" + "|".join(sorted(identities))
                display_name = ", ".join(sorted(names))
                group_matched += 1
        elif is_guessed:
            # A "guessed name" group (e.g. "John, Jane & 3 others.html")
            # has an auto-generated, unreliable summary as its filename --
            # never parsed as real participant data (see
            # looks_like_guessed_name()), so it's historically only ever
            # matched by exact filename. conversation_raw_participants
            # (from the sync pipeline's chat.db-derived sidecar -- see
            # populate_raw_participants()) now gives real membership for
            # exactly this kind of file when the sidecar has it, so THAT
            # is used here instead of the filename -- same "group:"
            # resolved-identity contact_key as an ordinary comma-separated
            # group, just sourced from real chat.db data rather than
            # parsed from the filename. This is what lets a guessed-name
            # file merge with another guessed-name file, OR with an
            # ordinary comma-separated file, that resolves to the exact
            # same contacts. No sidecar data at all for this file -> falls
            # through to matching only by exact filename, same as always.
            norm_participants = raw_sets.get(row["id"])
            if not norm_participants:
                continue
            stripped = norm_participants - my_handles_norm
            norm_participants = stripped if stripped else norm_participants
            if len(norm_participants) == 1:
                only = next(iter(norm_participants))
                person = handle_map.get(only)
                if not person:
                    continue
                contact_key, display_name = person
            else:
                identities = set()
                names = set()
                for norm_p in norm_participants:
                    person = handle_map.get(norm_p)
                    if person:
                        identities.add(person[0])
                        names.add(person[1])
                    else:
                        identities.add("unresolved:" + norm_p)
                        names.add(norm_p)
                if len(identities) < 2:
                    continue
                contact_key = "group:" + "|".join(sorted(identities))
                display_name = ", ".join(sorted(names))
                group_matched += 1
        else:
            comp = named_group_component.get(row["id"])
            if comp:
                # A genuine collision exists for this exact stripped name
                # -- treat all of them as the same real-world group. root
                # is a conversation_id; namespacing the key by it keeps
                # two DIFFERENT, non-overlapping components that happen to
                # share the same stripped name (the exact case this check
                # exists to catch) from colliding into the same
                # contact_key.
                prefix, root = comp
                contact_key = f"namedgroup:{prefix}#{root}"
                display_name = prefix
            else:
                person = handle_map.get(norm_handle(stem))
                if not person:
                    continue
                contact_key, display_name = person

        conn.execute(
            "INSERT OR IGNORE INTO contact_groups (contact_key, display_name) VALUES (?,?)",
            (contact_key, display_name)
        )
        conn.execute(
            "INSERT OR REPLACE INTO conversation_contact_group (conversation_id, contact_key) VALUES (?,?)",
            (row["id"], contact_key)
        )
        grouped += 1
    conn.commit()
    print(f"Contact grouping: {grouped} conversation(s) linked ({group_matched} via group participant-set matching) "
          f"to {len(ab_paths)} address book source(s).")
    return grouped


def populate_raw_participants(conn):
    """
    (Re)build conversation_raw_participants from each archive's own
    group_participants.json, if the sync pipeline wrote one (see
    imessage-incremental-sync.sh's participant-extraction step). This is
    the only source of participant information for a NAMED group -- its
    filename carries none at all -- and it's real data pulled directly
    from chat.db's chat_handle_join table rather than guessed from
    anything in the export itself.
    Wipes and rebuilds every time, same as populate_contact_groups(): if
    an archive's sidecar file is missing (an older export predating this
    feature, or chat.db simply didn't have a matching GUID to correlate
    against), that archive's named groups just have no participant data,
    the same graceful degradation as everywhere else this project reads
    optional sidecar data.
    Runs BEFORE populate_contact_groups() (see run_indexer()) specifically
    so that function's own named-group collision check, and its
    guessed-name participant-set matching, both have this run's freshly
    rebuilt participant data available to consult, rather than whatever
    was left over from the previous indexing pass.
    """
    conn.execute("DELETE FROM conversation_raw_participants")

    archive_rows = conn.execute("SELECT id, path FROM archives").fetchall()
    filename_to_convid = {
        r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM conversations")
    }

    written = 0
    for arch in archive_rows:
        sidecar = Path(arch["path"]) / "group_participants.json"
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  (skipping unreadable {sidecar}: {e})")
            continue
        if not isinstance(data, dict):
            print(f"  (skipping {sidecar}: expected a JSON object, got {type(data).__name__})")
            continue
        for filename, handles in data.items():
            conv_id = filename_to_convid.get(filename)
            if conv_id is None:
                continue
            if not isinstance(handles, list):
                print(f"  (skipping malformed entry for {filename!r} in {sidecar}: "
                      f"expected a list of handles, got {type(handles).__name__})")
                continue
            for h in handles:
                if not isinstance(h, str) or not h:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_raw_participants (conversation_id, handle) VALUES (?,?)",
                    (conv_id, h)
                )
                written += 1
    conn.commit()
    if written:
        print(f"Raw participants: {written} handle(s) recorded from chat.db-derived sidecar files.")
    return written

# ── Main ─────────────────────────────────────────────────────────────────────

def run_indexer():
    print("=== iMessage Indexer ===")
    print(f"Archive root: {ARCHIVE_ROOT}")
    print(f"Database: {DB_PATH}")
    print()

    archives = discover_archives(ARCHIVE_ROOT)
    if not archives:
        print("No archives found. Check ARCHIVE_ROOT.")
        return

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)

    print("Pass 1: indexing 1:1 conversations to build name map...")
    phone_to_name = {}
    total_msgs = 0
    for archive_path in archives:
        archive_id = get_or_create_archive(conn, archive_path)
        single_files = [f for f in sorted(archive_path.glob("*.html"))
                        if not is_group_filename(f.name)]
        for html_path in single_files:
            stem = html_path.stem
            if not is_phone_number(stem):
                continue
            content = html_path.read_text(encoding='utf-8', errors='replace')
            contact_name = extract_contact_name(content, html_path.name)
            if contact_name:
                phone_to_name[stem] = contact_name
    print(f"  Resolved {len(phone_to_name)} phone numbers to names.")

    print("Pass 2: indexing all conversations...")
    my_handles_norm = frozenset(norm_handle(h) for h in MY_HANDLES.split() if h.strip())
    for archive_path in archives:
        archive_id = get_or_create_archive(conn, archive_path)
        html_files = sorted(archive_path.glob("*.html"))
        print(f"\n[{archive_path.name}] {len(html_files)} conversations")
        for html_path in html_files:
            n = index_file(conn, html_path, archive_id, phone_to_name, my_handles_norm)
            total_msgs += n
            if n:
                print(f"  {html_path.name}: {n} messages", end='\r')
        print(f"  Done.{' '*40}")

    dedup_cross_archive_messages(conn)
    update_conversation_stats(conn)

    print("\nRebuilding FTS index...")
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    conn.commit()

    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    archs = conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
    print(f"\nDone: {archs} archives, {convs} conversations, {msgs:,} messages")

    print()
    populate_raw_participants(conn)
    populate_contact_groups(conn)

    conn.close()


# ── Watch for changes and reindex ────────────────────────────────────────────
# FORK addition: periodically re-runs the indexer when it detects that
# conversation HTML files have changed, so an external incremental-sync
# pipeline writing into the same ARCHIVE_ROOT gets picked up automatically
# without needing to restart this container.
#
# This polls file mtimes rather than using filesystem events (inotify /
# the `watchdog` package). That's a deliberate choice, not a simplification
# for its own sake: this project runs on macOS, bind-mounting a host
# directory into the container, and Docker Desktop on macOS has a long,
# still-ongoing history of not reliably propagating inotify events from a
# bind-mounted host directory into the container (see docker/for-mac
# issues #2216, #681, #2417, #5755, and similar reports against
# docker/for-win -- the underlying VM/VirtioFS layer is the common
# thread). An event-based watcher could silently miss changes in exactly
# this project's own setup. Polling mtimes has no such dependency -- it
# works the same way regardless of how the volume is mounted.

def get_latest_mtime(root):
    """
    Return the most recent mtime among all .html files across every
    discovered archive directory, or 0 if none found. Reuses
    discover_archives() (quietly) so this respects the same layout rules
    as normal indexing. Only checks the .html files directly inside each
    archive's own top level -- never recurses into Attachments/
    StickerCache, which sit alongside them and can be far larger, since a
    changed .html file is always what actually indicates new or edited
    messages.
    """
    latest = 0.0
    for archive_path in discover_archives(root, quiet=True):
        for html_path in archive_path.glob("*.html"):
            try:
                m = html_path.stat().st_mtime
                if m > latest:
                    latest = m
            except OSError:
                continue
    return latest


def watch_and_reindex(poll_interval):
    """
    Poll ARCHIVE_ROOT every poll_interval seconds; if any .html file's
    mtime is newer than the last time we checked, re-run the full indexer.
    Re-running is safe and idempotent (existing messages are never
    duplicated -- see index_file()'s INSERT OR IGNORE), so catching a sync
    that's still mid-write and re-checking again next interval is a
    harmless, expected outcome rather than something that needs special
    handling.
    """
    print(f"=== Watching {ARCHIVE_ROOT} for changes (checking every {poll_interval}s) ===")
    last_seen = get_latest_mtime(ARCHIVE_ROOT)
    while True:
        time.sleep(poll_interval)
        try:
            current = get_latest_mtime(ARCHIVE_ROOT)
            if current > last_seen:
                print(f"\nDetected changes under {ARCHIVE_ROOT}, reindexing...")
                run_indexer()
                last_seen = current
        except Exception as e:
            print(f"Watch loop error (will retry in {poll_interval}s): {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'embed':
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        init_db(conn)
        embed_images(conn)
        conn.close()
    elif len(sys.argv) > 1 and sys.argv[1] == 'watch':
        watch_and_reindex(int(os.environ.get("WATCH_INTERVAL_SECONDS", "3600")))
    else:
        run_indexer()
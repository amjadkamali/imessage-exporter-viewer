#!/usr/bin/env python3
"""
iMessage Archive Indexer
- Auto-discovers export directories under ARCHIVE_ROOT
- Indexes all of them into SQLite, deduplicating across archives
- Stores archive_dir per message for dynamic attachment serving
"""

import os
import re
import sqlite3
import hashlib
import html as _html
from pathlib import Path
from datetime import datetime

ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", "/archives")
DB_PATH      = os.environ.get("DB_PATH", "/data/imessage.db")
MODEL_DIR    = os.environ.get("MODEL_DIR", "/data/models")

IMAGE_EXTS = frozenset({'.heic', '.heif', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'})

MOBILECLIP_S0_URL  = "https://docs-assets.developer.apple.com/ml-research/datasets/mobileclip/mobileclip_s0.pt"
MOBILECLIP_S0_PATH = Path(MODEL_DIR) / "mobileclip_s0.pt"

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

# ── Discovery ─────────────────────────────────────────────────────────────────

def is_export_dir(path):
    """Return True if path looks like an imessage-exporter output directory."""
    p = Path(path)
    has_html = any(p.glob("*.html"))
    has_attachments = (p / "attachments").is_dir()
    return has_html and has_attachments

def discover_archives(root):
    """
    Recursively scan root for imessage export directories.
    Returns list of Path objects sorted by directory mtime (oldest first).
    """
    root = Path(root)
    if not root.exists():
        print(f"WARNING: ARCHIVE_ROOT not found: {root}")
        return []

    found = []
    # Check root itself
    if is_export_dir(root):
        found.append(root)
    # Check immediate children
    for child in sorted(root.iterdir()):
        if child.is_dir() and is_export_dir(child):
            found.append(child)
        # One level deeper
        elif child.is_dir():
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and is_export_dir(grandchild):
                    found.append(grandchild)

    # Sort by directory modification time (oldest first = earliest export first)
    found.sort(key=lambda p: p.stat().st_mtime)

    print(f"Discovered {len(found)} archive(s) under {root}:")
    for i, p in enumerate(found):
        html_count = len(list(p.glob("*.html")))
        print(f"  [{i}] {p} ({html_count} conversations)")

    return found

# ── Timestamp parsing ─────────────────────────────────────────────────────────

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

# ── HTML parsing ──────────────────────────────────────────────────────────────

def is_phone_number(s):
    """
    Return True only for standard North American E.164 numbers: +1XXXXXXXXXX
    (plus sign, country code 1, 10 digits = 12 chars total).
    Rejects short codes, email handles, international non-NA numbers, etc.
    """
    return bool(re.fullmatch(r'\+1\d{10}', s.strip()))


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
    # If it doesn't look like a single phone/email handle, treat as named group
    # A single handle has no spaces and matches either a phone or email pattern
    if ' ' in stem:
        return True
    return False


def extract_contact_name(html_content, filename):
    """
    Extract the first non-Me sender name from RECEIVED messages in a SINGLE-PERSON conversation.
    Only runs on files whose stem is a bare phone number (no commas, no spaces).
    Returns None for group conversations and named groups.

    By only scanning received messages (not sent), we always find the contact's name,
    even if the user ran imessage-exporter with a custom -m name replacing "Me".
    """
    if is_group_filename(filename):
        return None

    # Split by message block start markers and process each block
    blocks = re.split(r'(?=<div class="message")', html_content)

    for block in blocks:
        if '<div class="message">' not in block:
            continue

        # Skip sent messages — only extract from received messages
        if 'class="sent' in block:
            continue

        # Extract sender name from this received message block
        match = re.search(r'<span class="sender">([^<]+)</span>', block)
        if match:
            name = match.group(1).strip()
            if name and name != "Me":
                return name

    return None


def build_group_display_name(filename, phone_to_name):
    """
    Given a group filename like '+15551234567, +15559876543.html' and a
    mapping of phone -> resolved name, return a human-readable group name.

    - Named groups (no commas): preserve the stem name as-is.
    - Phone-number groups: substitute only standard NA numbers (+1XXXXXXXXXX);
      deduplicate resolved names so two numbers for the same person don't
      produce 'Jane Smith, Jane Smith'.
    """
    stem = Path(filename).stem

    # Named group chat — keep the name exactly as imessage-exporter wrote it
    if ',' not in stem:
        return stem

    parts = [p.strip() for p in stem.split(',')]
    resolved = []
    seen_names = set()
    for part in parts:
        if is_phone_number(part):
            name = phone_to_name.get(part, part)
        else:
            name = part  # email handle or unknown — keep as-is
        # Deduplicate: if the same resolved name appears more than once
        # (two numbers for one person), only include it once
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
        # Timestamp
        ts_raw = None
        m = re.search(r'<span class="timestamp"><a[^>]*>([^<]+)</a>', block)
        if m:
            ts_raw = m.group(1).strip()
        else:
            m = re.search(r'<span class="timestamp">([^<(]+)', block)
            if m:
                ts_raw = m.group(1).strip()

        # Sender
        sender = None
        m = re.search(r'<span class="sender">([^<]+)</span>', block)
        if m:
            sender = m.group(1).strip()

        # Text (from stripped block — no nested reply text)
        text_parts = []
        for bubble in re.findall(r'<span class="bubble">(.*?)</span>', block, re.DOTALL):
            text_parts.append(re.sub(r'<[^>]+>', '', bubble))
        text = _html.unescape(' '.join(text_parts)).strip() or None

        # Attachments (from stripped block)
        attachments = re.findall(r'src="(attachments/[^"]+)"', block)

        # GUID
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
            'id':            msg_id,
            'content_hash':  content_hash,
            'timestamp_raw': ts_raw,
            'timestamp':     ts_iso,
            'sender':        sender,
            'text':          text,
            'attachments':   attachments,
            'direction':     direction,
            'raw_html':      raw_html_override if raw_html_override is not None else block,
        }

    # ── Build a guid→start_offset map in one pass over original html ────────────
    # extract_original_block previously re-scanned the full html per message,
    # making parse_messages O(n²) on large conversations. Instead we find every
    # message-guid anchor once and record its block start position so lookups
    # are O(1).
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

    # ── Pass 1: regular messages (stripped html, plain split) ─────────────────
    # Strip replies subtrees so nested preview copies don't become extra blocks.
    # Use the stripped html ONLY for splitting and field extraction; we look up
    # the original block (with replies intact) to store as raw_html so the
    # inline reply preview renders correctly in the UI.
    stripped = strip_replies(html_content)
    blocks = re.split(r'(?=<div class="message">)', stripped)

    seen_guids = set()
    for block in blocks:
        if '<div class="message">' not in block:
            continue
        msg = parse_block(block)
        if msg is None:
            continue
        guid = msg['id'] if re.fullmatch(r'[A-F0-9a-f-]{36}', msg['id']) else None
        if guid:
            orig_block = extract_original_block_fast(guid)
            if orig_block:
                msg['raw_html'] = orig_block
            seen_guids.add(guid)
        messages.append(msg)

    # ── Pass 2: standalone reply entries (id="r-GUID") ────────────────────────
    # These use <div class="message" id="r-GUID"> and are NOT split by the plain
    # regex above. Extract them directly from the original html.
    for m_outer in re.finditer(r'<div class="message" id="r-([A-F0-9a-f-]+)">', html_content):
        guid = m_outer.group(1)
        if guid in seen_guids:
            continue  # already indexed (shouldn't happen, but be safe)
        start = m_outer.start()
        # Find matching close
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
        # Use stripped version of this block for field extraction
        stripped_block = strip_replies(standalone_block)
        msg = parse_block(stripped_block, raw_html_override=standalone_block)
        if msg:
            seen_guids.add(guid)
            messages.append(msg)

    # Sort by timestamp so the combined list is in chronological order
    messages.sort(key=lambda x: (x['timestamp'] or ''))

    return messages


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS archives (
            id          INTEGER PRIMARY KEY,
            path        TEXT UNIQUE NOT NULL,
            indexed_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY,
            filename    TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            msg_count   INTEGER DEFAULT 0,
            first_date  TEXT,
            last_date   TEXT,
            indexed_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id              TEXT PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            archive_id      INTEGER NOT NULL,
            timestamp       TEXT,
            timestamp_raw   TEXT,
            sender          TEXT,
            text            TEXT,
            direction       TEXT,
            has_attachment  INTEGER DEFAULT 0,
            attachment_path TEXT,
            raw_html        TEXT,
            content_hash    TEXT,
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

        CREATE INDEX IF NOT EXISTS idx_messages_conv  ON messages(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_messages_ts    ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_conv_last_date ON conversations(last_date DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_content_hash
            ON messages(conversation_id, content_hash);

        CREATE TABLE IF NOT EXISTS image_embeddings (
            attachment_path TEXT NOT NULL,
            archive_id      INTEGER NOT NULL,
            message_id      TEXT NOT NULL,
            embedding       BLOB NOT NULL,
            PRIMARY KEY (attachment_path, archive_id)
        );
        CREATE INDEX IF NOT EXISTS idx_embeddings_msg ON image_embeddings(message_id);
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

def index_file(conn, html_path, archive_id, phone_to_name):
    filename = Path(html_path).name
    name     = Path(html_path).stem

    content  = Path(html_path).read_text(encoding='utf-8', errors='replace')
    messages = parse_messages(content)

    if not messages:
        return 0

    if is_group_filename(filename):
        # For group chats, substitute known phone numbers with resolved names
        display_name = build_group_display_name(filename, phone_to_name)
    else:
        # For 1:1 chats, try to pull the real contact name from message senders
        contact_name = extract_contact_name(content, filename)
        display_name = contact_name if contact_name else name

    # Get or create conversation
    row = conn.execute("SELECT id FROM conversations WHERE filename=?", (filename,)).fetchone()
    if row:
        conv_id = row[0]
        # Update name if it looks like a raw phone number and we now have better info
        current = conn.execute("SELECT name FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if current and (current[0].startswith('+') or current[0] == name):
            conn.execute("UPDATE conversations SET name=? WHERE id=?", (display_name, conv_id))
    else:
        cur = conn.execute(
            "INSERT INTO conversations (filename, name, msg_count, indexed_at) VALUES (?,?,0,datetime('now'))",
            (filename, display_name)
        )
        conv_id = cur.lastrowid

    # Insert messages - OR IGNORE deduplicates by id across archives
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

    Safe dedup rules — ALL conditions must be true to delete a message:
      1. Same conversation_id and direction
      2. Same text content (or both empty/attachment-only)
      3. Timestamps within 5 seconds of each other
      4. DIFFERENT archive_id  ← critical: never dedupe within the same archive,
         which would collapse genuine rapid-fire messages sent 1-2s apart

    When a GUID and MD5 pair match, the MD5 (older exporter, no GUID) is dropped.
    When two GUIDs match (both newer exporter, small timestamp drift), the later
    timestamp is dropped — keeping the earlier (more authoritative) record.
    When two MD5s match (rare), the duplicate is dropped arbitrarily.
    """
    print("Deduplicating cross-archive duplicate messages...")
    from datetime import datetime as _dt
    from itertools import groupby as _groupby

    conn.row_factory = sqlite3.Row

    # Fetch all messages with enough info to detect cross-archive duplicates.
    # We need archive_id to enforce the different-archive requirement.
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

            # Rule 4: must be from different archives
            if a['archive_id'] == b['archive_id']:
                continue

            # Rule 3: timestamps within 5 seconds
            try:
                t1 = _dt.fromisoformat(a['timestamp'])
                t2 = _dt.fromisoformat(b['timestamp'])
            except ValueError:
                continue
            if abs((t2 - t1).total_seconds()) > 5:
                continue

            # Decide which to drop: prefer GUID over MD5, earlier timestamp over later
            a_is_guid = '-' in a['id'] and len(a['id']) == 36
            b_is_guid = '-' in b['id'] and len(b['id']) == 36

            if a_is_guid and not b_is_guid:
                to_delete.add(b['id'])   # keep GUID (a), drop MD5 (b)
            elif b_is_guid and not a_is_guid:
                to_delete.add(a['id'])   # keep GUID (b), drop MD5 (a)
            else:
                to_delete.add(b['id'])   # both same style — drop the later one

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
          msg_count  = (SELECT COUNT(*) FROM messages WHERE conversation_id = conversations.id),
          first_date = (SELECT MIN(timestamp) FROM messages WHERE conversation_id = conversations.id AND timestamp IS NOT NULL),
          last_date  = (SELECT MAX(timestamp) FROM messages WHERE conversation_id = conversations.id AND timestamp IS NOT NULL),
          indexed_at = datetime('now')
    """)
    conn.commit()

# ── Image embedding ───────────────────────────────────────────────────────────

def embed_images(conn):
    """Embed all un-processed image attachments using MobileCLIP-S0.

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
            arr   = feats.numpy().astype(np.float32)

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

    print(f"✓ Image embedding: {done:,} embedded, {errors} skipped")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_indexer():
    print(f"=== iMessage Indexer ===")
    print(f"Archive root: {ARCHIVE_ROOT}")
    print(f"Database:     {DB_PATH}")
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

    # ── Pass 1: index all single-person conversations first to build
    #            the phone → name map before tackling group chats ──────────────
    print("Pass 1: indexing 1:1 conversations to build name map...")
    phone_to_name = {}  # e.g. {'+15551234567': 'Jane Smith'}
    total_msgs = 0

    for archive_path in archives:
        archive_id = get_or_create_archive(conn, archive_path)
        single_files = [f for f in sorted(archive_path.glob("*.html"))
                        if not is_group_filename(f.name)]
        for html_path in single_files:
            stem = html_path.stem
            # Only map standard NA phone numbers; skip email handles, etc.
            if not is_phone_number(stem):
                continue
            content = html_path.read_text(encoding='utf-8', errors='replace')
            contact_name = extract_contact_name(content, html_path.name)
            if contact_name:
                phone_to_name[stem] = contact_name

    print(f"  Resolved {len(phone_to_name)} phone numbers to names.")

    # ── Pass 2: index everything (1:1 and group) with the name map available ──
    print("Pass 2: indexing all conversations...")
    for archive_path in archives:
        archive_id = get_or_create_archive(conn, archive_path)
        html_files = sorted(archive_path.glob("*.html"))
        print(f"\n[{archive_path.name}] {len(html_files)} conversations")

        for html_path in html_files:
            n = index_file(conn, html_path, archive_id, phone_to_name)
            total_msgs += n
            if n:
                print(f"  {html_path.name}: {n} messages", end='\r')

        print(f"  Done.{' '*40}")

    dedup_cross_archive_messages(conn)
    update_conversation_stats(conn)

    print("\nRebuilding FTS index...")
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    conn.commit()

    msgs  = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    archs = conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
    print(f"\n✓ Done: {archs} archives, {convs} conversations, {msgs:,} messages")
    conn.close()

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
    else:
        run_indexer()

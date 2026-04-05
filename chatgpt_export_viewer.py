#!/usr/bin/env python3
"""
ChatGPT Export Viewer & Audio Stitcher  (v6)
=============================================
v6: Rewrote file discovery — single global asset index replaces per-conversation
    image maps and weak attachment matching. All file-* uploads and file_* system
    assets are now indexed from both export root and conv subdirectories.

Parses a standard ChatGPT data export, generates a dark-themed HTML viewer
with all conversation logs, and stitches together voice-mode audio files
into a single MP3 per conversation.

The export folder should contain:
  - conversations-*.json  (or a single conversations.json)
  - {conversation_id}/audio/*.wav   (voice-mode audio clips, if any)
  - {conversation_id}/image/*.png   (images shared in conversations)

Output structure:
  output-dir/
    index.html              — the viewer (lightweight, loads data on demand)
    data/                   — one JSON file per conversation (lazy-loaded)
    audio/                  — stitched MP3 files
    images/                 — copied image files
    attachments/            — copied attachment files (PDFs, zips, etc.)
    userdata/               — persistent user annotations (auto-created)
    serve.py                — HTTP server with Range request support

Requirements:
  - Python 3.9+
  - pydub   (pip install pydub)   — only needed if stitching audio
  - ffmpeg  (must be on PATH)     — only needed if stitching audio

Usage:
  # Full run (stitch audio + generate viewer):
  python chatgpt_export_viewer.py --export-dir ./my-export --output-dir ./viewer

  # Viewer only (skip slow audio stitching):
  python chatgpt_export_viewer.py --export-dir ./my-export --output-dir ./viewer --skip-audio

  # Then serve locally and open in browser:
  cd ./viewer && python serve.py 8000
  # Visit http://localhost:8000

Notes:
  - Use serve.py (not python -m http.server) for audio scrubbing support
    and persistent annotations (bookmarks, comments, etc.)
  - Audio stitching can be slow for many conversations. Use --skip-audio
    to regenerate just the HTML viewer without re-processing audio.
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone


# ===========================================================================
# Parsing helpers
# ===========================================================================

def load_conversations(export_dir: str) -> list[dict]:
    """Load all conversations from split or single JSON files."""
    pattern = os.path.join(export_dir, "conversations-*.json")
    json_files = sorted(glob.glob(pattern))

    if not json_files:
        single = os.path.join(export_dir, "conversations.json")
        if os.path.isfile(single):
            json_files = [single]

    if not json_files:
        print(f"ERROR: No conversations JSON files found in {export_dir}")
        print("       Expected conversations.json or conversations-000.json, etc.")
        sys.exit(1)

    all_conversations = []
    for jf in json_files:
        print(f"  Loading {os.path.basename(jf)}...")
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            all_conversations.extend(data)
        elif isinstance(data, dict):
            all_conversations.append(data)
        else:
            print(f"  WARNING: {jf} has unexpected format, skipping.")

    print(f"  Loaded {len(all_conversations)} conversations")
    return all_conversations


def find_audio_files(export_dir: str, conversation_id: str) -> list[str]:
    """Find all audio files for a given conversation."""
    audio_dir = os.path.join(export_dir, conversation_id, "audio")
    if not os.path.isdir(audio_dir):
        return []
    files = []
    for ext in ("*.wav", "*.ogg", "*.mp3", "*.webm", "*.m4a"):
        files.extend(glob.glob(os.path.join(audio_dir, ext)))
    return files


def build_global_asset_index(export_dir: str) -> dict:
    """
    Build a comprehensive index of ALL files in the export.
    Returns a dict with:
      'by_sediment_hex': {hex_key_lower: filepath}    — for sediment:// pointers
      'by_attachment_id': {file-ID: filepath}          — for metadata.attachments[].id
      'by_filename': {full_filename: filepath}         — fallback
    """
    index = {
        "by_sediment_hex": {},   # hex key -> path  (for file_ assets)
        "by_attachment_id": {},  # file-XXXXX -> path  (for file- uploads)
        "by_filename": {},       # full filename -> path
    }

    if not os.path.isdir(export_dir):
        return index

    # --- 1. Index ALL files in export root ---
    for entry in os.listdir(export_dir):
        full = os.path.join(export_dir, entry)
        if not os.path.isfile(full):
            continue

        if entry.startswith("file_"):
            # System asset: file_HEXKEY-description.ext
            # sediment:// references use the hex key
            stem = os.path.splitext(entry)[0]
            hex_part = stem[5:].split("-", 1)[0].lower()
            index["by_sediment_hex"][hex_part] = full
            index["by_filename"][entry] = full

        elif entry.startswith("file-"):
            # User upload: file-BASE64ID-originalname.ext
            # metadata.attachments[].id = "file-BASE64ID"
            parts = entry.split("-", 2)
            if len(parts) >= 2:
                att_id = f"file-{parts[1]}"
                index["by_attachment_id"][att_id] = full
            index["by_filename"][entry] = full

    # --- 2. Index ALL images in {conv_id}/image/ subdirectories ---
    for entry in os.listdir(export_dir):
        subdir = os.path.join(export_dir, entry)
        if not os.path.isdir(subdir):
            continue
        image_dir = os.path.join(subdir, "image")
        if not os.path.isdir(image_dir):
            continue
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg"):
            for fp in glob.glob(os.path.join(image_dir, ext)):
                basename = os.path.basename(fp)
                stem = os.path.splitext(basename)[0]
                if stem.startswith("file_"):
                    hex_part = stem[5:].split("-", 1)[0].lower()
                    # Prefer conv-specific image over root (more specific)
                    index["by_sediment_hex"][hex_part] = fp
                index["by_filename"][basename] = fp

    return index


def extract_sort_key(filepath: str) -> str:
    """Extract the hex sort key from an audio filename for chronological ordering."""
    basename = os.path.basename(filepath)
    stem = os.path.splitext(basename)[0]
    if stem.startswith("file_"):
        stem = stem[5:]
    match = re.match(
        r"^([0-9a-fA-F]+)-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
        stem,
    )
    if match:
        return match.group(1).lower()
    return stem.lower()


def sanitize_filename(title: str) -> str:
    """Convert a conversation title into a filesystem-safe name."""
    if not title:
        return "untitled"
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)
    sanitized = re.sub(r"[\s_]+", "_", sanitized).strip("_")
    if len(sanitized) > 120:
        sanitized = sanitized[:120]
    return sanitized or "untitled"


def format_timestamp(ts) -> str:
    """Convert a Unix timestamp to a human-readable string."""
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OSError):
        return ""


# ===========================================================================
# Message extraction — walk the canonical conversation path
# ===========================================================================

def extract_messages(conversation: dict, asset_index: dict,
                     images_out_dir: str, attachments_out_dir: str) -> list[dict]:
    """
    Walk the canonical (non-branched) message path for a conversation.
    Returns a list of message dicts suitable for JSON serialisation.
    Copies referenced images and attachments to the output directory.
    """
    mapping = conversation.get("mapping", {})
    if not mapping:
        return []

    current_node_id = conversation.get("current_node")

    # Walk from current_node back to root, then reverse for chronological order.
    if current_node_id and current_node_id in mapping:
        path_ids = []
        node_id = current_node_id
        while node_id and node_id in mapping:
            path_ids.append(node_id)
            node_id = mapping[node_id].get("parent")
        path_ids.reverse()
    else:
        # Fallback: find root -> follow children[0]
        root_id = None
        for nid, node in mapping.items():
            parent = node.get("parent")
            if parent is None or parent not in mapping:
                root_id = nid
                break
        if not root_id:
            return []
        path_ids = []
        nid = root_id
        while nid:
            path_ids.append(nid)
            children = mapping.get(nid, {}).get("children", [])
            nid = children[0] if children else None

    messages = []
    for nid in path_ids:
        node = mapping.get(nid, {})
        msg = node.get("message")
        if not msg:
            continue

        meta = msg.get("metadata", {})
        if meta.get("is_visually_hidden_from_conversation"):
            continue
        if msg.get("weight", 1.0) == 0.0:
            continue

        role = msg.get("author", {}).get("role", "unknown")
        content = msg.get("content", {})
        content_type = content.get("content_type", "")
        parts = content.get("parts", [])
        create_time = msg.get("create_time")

        text_segments = []
        is_audio = False
        images = []       # list of relative image paths for this message
        attachments = []   # list of {name, path} for attachments

        if content_type == "text":
            for part in parts:
                if isinstance(part, str) and part.strip():
                    text_segments.append(part)

        elif content_type == "multimodal_text":
            for part in parts:
                if isinstance(part, str) and part.strip():
                    text_segments.append(part)
                elif isinstance(part, dict):
                    ct = part.get("content_type", "")
                    if ct == "audio_transcription":
                        t = part.get("text", "").strip()
                        if t:
                            text_segments.append(t)
                            is_audio = True
                    elif ct == "image_asset_pointer":
                        # Try to find and copy the image
                        asset_ptr = part.get("asset_pointer", "")
                        img_path = _resolve_image(asset_ptr, asset_index,
                                                  images_out_dir)
                        if img_path:
                            images.append(img_path)
                        else:
                            w = part.get("width", "?")
                            h = part.get("height", "?")
                            text_segments.append(f"[Image ({w}x{h})]")
                    elif ct in ("audio_asset_pointer",
                                "real_time_user_audio_video_asset_pointer"):
                        is_audio = True

        elif content_type == "thoughts":
            thoughts = content.get("thoughts", [])
            for thought in thoughts:
                summary = thought.get("summary", "")
                body = thought.get("content", "")
                if summary:
                    text_segments.append(f"[Thinking: {summary}]")
                elif body:
                    text_segments.append(f"[Thinking]\n{body}")

        elif content_type == "code":
            lang = content.get("language", "")
            code_text = content.get("text", "")
            if code_text:
                text_segments.append(f"```{lang}\n{code_text}\n```")

        # Process metadata attachments (user-uploaded files)
        msg_attachments = meta.get("attachments", [])
        for att in msg_attachments:
            att_name = att.get("name", "unknown_file")
            att_id = att.get("id", "")
            att_mime = att.get("mime_type", "")

            # Try to find the file
            att_path = _resolve_attachment(att_id, att_name, asset_index,
                                          attachments_out_dir)
            if att_path:
                if att_mime and att_mime.startswith("image/"):
                    images.append(att_path)
                else:
                    attachments.append({"name": att_name, "path": att_path})
            else:
                attachments.append({"name": att_name, "path": ""})

        combined_text = "\n".join(text_segments).strip()
        if not combined_text and not images and not attachments:
            if role == "system":
                continue
            # Allow messages that only have images/attachments
            if not images and not attachments:
                continue

        if role == "system":
            continue

        msg_data = {
            "role": role,
            "text": combined_text,
            "time_str": format_timestamp(create_time),
            "is_audio": is_audio,
        }
        if images:
            msg_data["images"] = images
        if attachments:
            msg_data["attachments"] = attachments

        messages.append(msg_data)

    return messages


def _resolve_image(asset_pointer: str, asset_index: dict,
                   images_out_dir: str) -> str:
    """Try to find an image file for a sediment:// asset pointer and copy it."""
    if not asset_pointer:
        return ""

    src_path = None

    if asset_pointer.startswith("sediment://"):
        ref = asset_pointer[11:]  # strip sediment://
        # Handle both "file_HEXKEY" and bare "HEXKEY"
        if ref.startswith("file_"):
            ref = ref[5:]
        hex_key = ref.split("-", 1)[0].lower()

        # Look up in the global index
        src_path = asset_index["by_sediment_hex"].get(hex_key)

        # Prefix fallback — some keys are truncated
        if not src_path:
            for k, v in asset_index["by_sediment_hex"].items():
                if k.startswith(hex_key) or hex_key.startswith(k):
                    src_path = v
                    break

    if not src_path or not os.path.isfile(src_path):
        return ""

    # Copy to output
    os.makedirs(images_out_dir, exist_ok=True)
    dest_name = os.path.basename(src_path)
    dest_path = os.path.join(images_out_dir, dest_name)
    if not os.path.exists(dest_path):
        shutil.copy2(src_path, dest_path)

    return f"images/{dest_name}"


def _resolve_attachment(att_id: str, att_name: str,
                        asset_index: dict,
                        attachments_out_dir: str) -> str:
    """Try to find an attachment file and copy it to output."""
    src_path = None

    # Try by attachment ID (file-BASE64ID)
    if att_id:
        src_path = asset_index["by_attachment_id"].get(att_id)

    # Fallback: try sediment hex index (some attachments reference system assets)
    if not src_path and att_id and att_id.startswith("file_"):
        hex_part = att_id[5:].split("-", 1)[0].lower()
        src_path = asset_index["by_sediment_hex"].get(hex_part)

    # Fallback: try matching by filename
    if not src_path and att_name:
        for fname, fpath in asset_index["by_filename"].items():
            if att_name in fname or fname.endswith(att_name):
                src_path = fpath
                break

    if not src_path or not os.path.isfile(src_path):
        return ""

    os.makedirs(attachments_out_dir, exist_ok=True)
    # Use the original name if possible
    safe_name = sanitize_filename(os.path.splitext(att_name)[0])
    ext = os.path.splitext(att_name)[1] or os.path.splitext(src_path)[1]
    dest_name = f"{safe_name}{ext}"
    dest_path = os.path.join(attachments_out_dir, dest_name)

    # Avoid overwrites
    counter = 1
    while os.path.exists(dest_path):
        dest_name = f"{safe_name}_{counter}{ext}"
        dest_path = os.path.join(attachments_out_dir, dest_name)
        counter += 1

    shutil.copy2(src_path, dest_path)
    return f"attachments/{dest_name}"


# ===========================================================================
# Audio stitching
# ===========================================================================

def stitch_audio(wav_files: list[str], output_path: str) -> bool:
    """Concatenate audio files in order and export as a single MP3."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("ERROR: pydub is not installed. Run: pip install pydub")
        print("       Also ensure ffmpeg is on your PATH.")
        sys.exit(1)

    combined = AudioSegment.empty()
    for f in wav_files:
        try:
            clip = AudioSegment.from_file(f)
            combined += clip
        except Exception as e:
            print(f"    WARNING: Skipping {os.path.basename(f)}: {e}")

    if len(combined) == 0:
        return False

    try:
        combined.export(output_path, format="mp3")
        secs = len(combined) / 1000.0
        print(f"    -> {os.path.basename(output_path)} ({secs:.1f}s, {len(wav_files)} clips)")
        return True
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


# ===========================================================================
# HTML generation  (v3 — attachments, date search, persistent annotations)
# ===========================================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ChatGPT Export Viewer</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #1a1a2e;
  --bg-side:   #16213e;
  --bg-card:   #0f3460;
  --bg-user:   #1a5276;
  --bg-asst:   #2c2c3e;
  --bg-tool:   #2a2a38;
  --text:      #e0e0e0;
  --text-dim:  #8a8a9a;
  --accent:    #4fc3f7;
  --accent2:   #81c784;
  --border:    #2a2a4a;
  --hover:     #1e3a5f;
  --scrollbar: #3a3a5a;
  --star:      #ffd54f;
  --pin:       #ff8a65;
  --highlight: rgba(255,235,59,0.15);
  --comment:   #ce93d8;
}

html, body {
  height: 100%;
  /* Comprehensive font stack for ALL unicode including Em Quad, symbols, emoji */
  font-family: 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, Roboto,
               'Noto Sans', 'Noto Sans CJK SC', 'Noto Sans Arabic', 'Noto Sans Devanagari',
               'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol',
               'Noto Color Emoji', 'Noto Emoji',
               'DejaVu Sans', 'Lucida Sans Unicode', 'Arial Unicode MS',
               sans-serif;
  background: var(--bg); color: var(--text);
}

/* Force unicode whitespace characters to render as spaces rather than boxes */
body { font-variant-ligatures: common-ligatures; }

/* --- Layout --- */
.app { display: flex; height: 100vh; }

/* --- Sidebar --- */
.sidebar {
  width: 320px; min-width: 260px; max-width: 400px;
  background: var(--bg-side); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
  resize: horizontal;
}
.sidebar-header { padding: 16px; border-bottom: 1px solid var(--border); }
.sidebar-header h1 { font-size: 16px; font-weight: 600; margin-bottom: 10px; color: var(--accent); }
.search-area { display: flex; flex-direction: column; gap: 6px; }
.search-box {
  width: 100%; padding: 8px 12px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text); font-size: 14px; outline: none;
}
.search-box:focus { border-color: var(--accent); }
.search-row { display: flex; gap: 6px; align-items: center; }
.search-toggle {
  font-size: 11px; color: var(--text-dim); cursor: pointer;
  display: flex; align-items: center; gap: 4px; user-select: none;
}
.search-toggle input { margin: 0; }
.date-inputs { display: none; gap: 4px; }
.date-inputs.show { display: flex; }
.date-inputs input {
  flex: 1; padding: 5px 8px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text); font-size: 12px; outline: none;
}
.date-inputs input:focus { border-color: var(--accent); }
.filter-row { display: flex; gap: 6px; flex-wrap: wrap; }
.filter-btn {
  font-size: 11px; padding: 3px 8px; border-radius: 4px;
  border: 1px solid var(--border); background: transparent;
  color: var(--text-dim); cursor: pointer; transition: all 0.15s;
}
.filter-btn:hover { border-color: var(--accent); color: var(--text); background: rgba(79,195,247,0.05); }
.filter-btn.active {
  border-color: var(--accent); color: #fff; font-weight: 700;
  background: linear-gradient(135deg, rgba(79,195,247,0.25), rgba(79,195,247,0.1));
  box-shadow: 0 0 6px rgba(79,195,247,0.3);
}
.sidebar-stats {
  padding: 6px 16px; font-size: 12px; color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}
.conv-list { flex: 1; overflow-y: auto; padding: 4px 0; }
.conv-item {
  padding: 10px 16px; cursor: pointer;
  border-bottom: 1px solid var(--border); transition: background 0.15s;
  position: relative;
}
.conv-item:hover { background: var(--hover); }
.conv-item.active { background: var(--bg-card); border-left: 3px solid var(--accent); }
.conv-item.pinned { border-left: 3px solid var(--pin); }
.conv-item.active.pinned { border-left: 3px solid var(--accent); }
.conv-title {
  font-size: 14px; font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  display: flex; align-items: center; gap: 4px;
}
.conv-title .pin-icon { color: var(--pin); font-size: 12px; }
.conv-title .star-icon { color: var(--star); font-size: 12px; }
.conv-meta {
  font-size: 11px; color: var(--text-dim); margin-top: 3px;
  display: flex; align-items: center;
}
.conv-meta .conv-date { margin-right: auto; }
.conv-meta .conv-badges { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
.conv-summary {
  font-size: 11px; color: var(--text-dim); margin-top: 2px;
  font-style: italic; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; }
.badge-audio { background: #1b5e20; color: #a5d6a7; }
.badge-msgs  { background: #4a148c44; color: #ce93d8; }
.badge-imgs  { background: #0d47a144; color: #90caf9; }
.badge-att   { background: #4e342e44; color: #bcaaa4; }
.badge-comment { background: #4a148c44; color: var(--comment); }

/* --- Context menu --- */
.ctx-menu {
  position: fixed; z-index: 1000; background: #1e1e3e; border: 1px solid var(--border);
  border-radius: 8px; padding: 4px 0; min-width: 180px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
}
.ctx-menu-item {
  padding: 8px 16px; cursor: pointer; font-size: 13px; color: var(--text);
  display: flex; align-items: center; gap: 8px;
}
.ctx-menu-item:hover { background: var(--hover); }
.ctx-menu-sep { height: 1px; background: var(--border); margin: 4px 0; }

/* --- Main panel --- */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.main-header {
  padding: 16px 24px; border-bottom: 1px solid var(--border);
  background: var(--bg-side);
}
.main-header h2 { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
.main-header .meta { font-size: 12px; color: var(--text-dim); }
.audio-player {
  padding: 12px 24px; background: var(--bg-side);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.audio-player audio { flex: 1; min-width: 200px; height: 44px; }
.audio-player a {
  color: var(--accent); text-decoration: none; font-size: 13px;
  padding: 4px 10px; border: 1px solid var(--accent); border-radius: 6px;
}
.audio-player a:hover { background: var(--accent); color: var(--bg); }
#convView {
  display: none; flex-direction: column;
  flex: 1; min-height: 0; overflow: hidden;
}
#convView.visible { display: flex; }
.messages { flex: 1; overflow-y: auto; padding: 16px 24px 32px; min-height: 0; position: relative; }
.empty-state {
  display: flex; align-items: center; justify-content: center;
  height: 100%; color: var(--text-dim); font-size: 16px; text-align: center;
  padding: 24px; line-height: 1.6;
}
.loading { color: var(--accent); font-style: italic; padding: 32px; text-align: center; }

/* --- Chat bubbles --- */
.msg {
  margin-bottom: 16px; max-width: 85%; padding: 12px 16px;
  border-radius: 12px; line-height: 1.55; font-size: 14px;
  position: relative; word-wrap: break-word;
}
.msg.highlighted {
  box-shadow: inset 0 0 0 2px var(--star);
}
.msg.highlighted .msg-highlight-pip {
  position: absolute; top: 6px; right: 6px;
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--star); pointer-events: none;
  box-shadow: 0 0 4px var(--star);
}
.msg.has-comment {
  border-left: 3px solid var(--comment);
}
.msg .msg-comment-pip {
  position: absolute; top: 6px; right: 18px;
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--comment); pointer-events: none;
  box-shadow: 0 0 4px var(--comment);
}

/* --- Annotation minimap (scrollbar-area markers) --- */
.annotation-track {
  position: absolute; top: 0; right: 0; width: 6px;
  height: 100%; pointer-events: none; z-index: 5;
}
.annotation-pip {
  position: absolute; right: 0; width: 6px; height: 4px;
  border-radius: 2px; pointer-events: auto; cursor: pointer;
  opacity: 0.8;
}
.annotation-pip:hover { opacity: 1; width: 10px; }
.annotation-pip.pip-highlight { background: var(--star); }
.annotation-pip.pip-comment { background: var(--comment); }
.msg-user { background: var(--bg-user); margin-left: auto; border-bottom-right-radius: 4px; }
.msg-assistant { background: var(--bg-asst); margin-right: auto; border-bottom-left-radius: 4px; }
.msg-tool {
  background: var(--bg-tool); margin-right: auto; border-bottom-left-radius: 4px;
  font-size: 13px; opacity: 0.8; border-left: 3px solid #666;
}
.msg-role {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  margin-bottom: 4px; display: flex; align-items: center; gap: 6px;
}
.msg-user .msg-role { color: #90caf9; }
.msg-assistant .msg-role { color: var(--accent2); }
.msg-tool .msg-role { color: #ffab91; }
.msg-time { font-size: 10px; color: var(--text-dim); margin-top: 6px; text-align: right; }
.msg-audio-badge {
  display: inline-block; font-size: 10px; padding: 1px 5px;
  background: #1b5e20; color: #a5d6a7; border-radius: 3px;
}
.msg-actions {
  position: absolute; top: 6px; right: 8px; display: none;
  gap: 4px; align-items: center;
}
.msg:hover .msg-actions { display: flex; }
.msg-action-btn {
  background: rgba(0,0,0,0.3); border: none; color: var(--text-dim);
  font-size: 14px; cursor: pointer; padding: 2px 5px; border-radius: 4px;
  line-height: 1;
}
.msg-action-btn:hover { color: var(--text); background: rgba(0,0,0,0.5); }
.msg-action-btn.active { color: var(--star); }
.msg-comment {
  margin-top: 8px; padding: 6px 10px; background: rgba(0,0,0,0.2);
  border-radius: 6px; font-size: 12px; color: var(--accent);
  border-left: 2px solid var(--accent);
}
.msg-comment-input {
  margin-top: 6px; width: 100%; padding: 6px 10px;
  background: rgba(0,0,0,0.3); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-size: 12px; outline: none;
  resize: vertical; min-height: 30px; font-family: inherit;
}

/* --- Images in messages --- */
.msg-images { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px; }
.msg-images img {
  max-width: 300px; max-height: 300px; border-radius: 8px;
  cursor: pointer; object-fit: cover; border: 1px solid var(--border);
  transition: transform 0.2s;
}
.msg-images img:hover { transform: scale(1.02); }

/* --- Image lightbox --- */
.lightbox {
  display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0,0,0,0.9); z-index: 2000; align-items: center;
  justify-content: center; cursor: zoom-out;
}
.lightbox.show { display: flex; }
.lightbox img { max-width: 95vw; max-height: 95vh; object-fit: contain; }

/* --- Attachments in messages --- */
.msg-attachments { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.msg-attachment {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; background: rgba(0,0,0,0.2); border-radius: 6px;
  font-size: 12px; color: var(--text-dim); text-decoration: none;
  border: 1px solid var(--border); transition: all 0.15s;
}
.msg-attachment:hover { border-color: var(--accent); color: var(--accent); }
.msg-attachment .att-icon { font-size: 14px; }

/* --- Markdown-ish --- */
.msg-body pre {
  background: #111; padding: 10px; border-radius: 6px;
  overflow-x: auto; margin: 8px 0; font-size: 13px;
  font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
}
.msg-body code {
  background: #222; padding: 1px 4px; border-radius: 3px;
  font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 13px;
}
.msg-body pre code { background: none; padding: 0; }
.msg-body p { margin-bottom: 8px; }
.msg-body p:last-child { margin-bottom: 0; }
.msg-body ul, .msg-body ol { margin: 6px 0 6px 20px; }
.msg-body li { margin-bottom: 3px; }
.msg-body strong { color: #fff; }
.msg-body a { color: var(--accent); }
.msg-body blockquote {
  border-left: 3px solid var(--accent); padding-left: 12px;
  margin: 8px 0; color: var(--text-dim);
}

/* --- Summary modal --- */
.modal-overlay {
  display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0,0,0,0.6); z-index: 1500; align-items: center; justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal {
  background: var(--bg-side); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px; min-width: 350px; max-width: 500px;
}
.modal h3 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }
.modal textarea {
  width: 100%; min-height: 80px; padding: 10px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text); font-size: 14px; outline: none; resize: vertical;
  font-family: inherit;
}
.modal-btns { display: flex; gap: 8px; margin-top: 12px; justify-content: flex-end; }
.modal-btn {
  padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border);
  background: transparent; color: var(--text); cursor: pointer; font-size: 13px;
}
.modal-btn.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.modal-btn:hover { opacity: 0.85; }

/* --- Scrollbar --- */
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--scrollbar); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #5a5a7a; }

/* --- Responsive --- */
@media (max-width: 768px) {
  .sidebar { width: 100%; max-width: 100%; position: absolute; z-index: 10; height: 100%; }
  .sidebar.hidden { display: none; }
  .mobile-toggle {
    display: block; position: fixed; bottom: 16px; left: 16px; z-index: 20;
    background: var(--accent); color: var(--bg); border: none;
    padding: 10px 14px; border-radius: 50%; cursor: pointer; font-size: 18px;
  }
}
@media (min-width: 769px) { .mobile-toggle { display: none; } }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <h1>ChatGPT Export Viewer</h1>
      <div class="search-area">
        <input type="text" class="search-box" id="search"
               placeholder="Search conversations..." autocomplete="off">
        <div class="search-row">
          <label class="search-toggle">
            <input type="checkbox" id="dateToggle"> Search by date
          </label>
        </div>
        <div class="date-inputs" id="dateInputs">
          <input type="date" id="dateFrom" placeholder="From">
          <input type="date" id="dateTo" placeholder="To">
        </div>
        <div class="filter-row">
          <button class="filter-btn" data-filter="pinned">Pinned</button>
          <button class="filter-btn" data-filter="starred">Starred</button>
          <button class="filter-btn" data-filter="audio">Audio</button>
          <button class="filter-btn" data-filter="images">Images</button>
          <button class="filter-btn" data-filter="files">Files</button>
          <button class="filter-btn" data-filter="comments">Comments</button>
        </div>
      </div>
    </div>
    <div class="sidebar-stats" id="stats"></div>
    <div class="conv-list" id="convList"></div>
  </aside>
  <main class="main" id="main">
    <div class="empty-state" id="emptyState">
      Select a conversation from the sidebar
    </div>
    <div id="convView">
      <div class="main-header" id="mainHeader"></div>
      <div class="audio-player" id="audioPlayer" style="display:none;"></div>
      <div class="messages" id="messages"></div>
    </div>
  </main>
</div>
<button class="mobile-toggle" id="mobileToggle">&#9776;</button>

<!-- Lightbox for full-size images -->
<div class="lightbox" id="lightbox">
  <img id="lightboxImg" src="" alt="">
</div>

<!-- Context menu -->
<div class="ctx-menu" id="ctxMenu" style="display:none;"></div>

<!-- Summary modal -->
<div class="modal-overlay" id="summaryModal">
  <div class="modal">
    <h3>Conversation Summary</h3>
    <textarea id="summaryText" placeholder="Write a summary for this conversation..."></textarea>
    <div class="modal-btns">
      <button class="modal-btn" id="summaryCancel">Cancel</button>
      <button class="modal-btn" id="summaryClear">Clear</button>
      <button class="modal-btn primary" id="summarySave">Save</button>
    </div>
  </div>
</div>

<script>
// ---- Sidebar metadata ----
const INDEX = %%INDEX_JSON%%;

// ================================================================
// Persistent user data (bookmarks, stars, comments, pins, summaries)
// Stored via serve.py PUT/GET to userdata/ directory
// ================================================================
const userData = {
  pins: {},        // conv_id -> true
  stars: {},       // conv_id -> true
  summaries: {},   // conv_id -> string
  highlights: {},  // conv_id -> { msgIdx: true }
  comments: {},    // conv_id -> { msgIdx: string }
  session: { selectedConv: -1, scrollPositions: {} }
};

// Load all user data from server
async function loadUserData() {
  const files = ['pins', 'stars', 'summaries', 'highlights', 'comments', 'session'];
  for (const f of files) {
    try {
      const resp = await fetch('userdata/' + f + '.json');
      if (resp.ok) {
        userData[f] = await resp.json();
      }
    } catch (e) { /* first run, no data yet */ }
  }
}

async function saveUserData(key) {
  try {
    await fetch('userdata/' + key + '.json', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(userData[key])
    });
  } catch (e) {
    // Fallback: localStorage
    try { localStorage.setItem('cgpt_' + key, JSON.stringify(userData[key])); } catch(e2) {}
  }
}

// Also try localStorage as fallback read
function loadLocalStorageFallback() {
  const files = ['pins', 'stars', 'summaries', 'highlights', 'comments', 'session'];
  for (const f of files) {
    try {
      const saved = localStorage.getItem('cgpt_' + f);
      if (saved && Object.keys(userData[f]).length === 0) {
        userData[f] = JSON.parse(saved);
      }
    } catch (e) {}
  }
}

// ---- Markdown renderer ----
function renderMarkdown(text) {
  let h = escapeHtml(text);
  h = h.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    '<pre><code' + (lang ? ' class="lang-' + lang + '"' : '') + '>' + code + '</code></pre>');
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  h = h.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  h = h.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  h = h.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
  h = h.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
  h = h.replace(/^### (.+)$/gm, '<strong>$1</strong>');
  h = h.replace(/^## (.+)$/gm, '<strong>$1</strong>');
  h = h.replace(/^# (.+)$/gm, '<strong>$1</strong>');
  h = h.replace(/\n\n/g, '</p><p>');
  h = h.replace(/\n/g, '<br>');
  h = '<p>' + h + '</p>';
  h = h.replace(/<p>\s*<\/p>/g, '');
  h = h.replace(/<p>(<pre>)/g, '$1');
  h = h.replace(/(<\/pre>)<\/p>/g, '$1');
  return h;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ---- File icon helper ----
function fileIcon(name) {
  const ext = (name || '').split('.').pop().toLowerCase();
  const icons = {
    pdf: '\u{1F4C4}', zip: '\u{1F4E6}', txt: '\u{1F4DD}', py: '\u{1F40D}',
    js: '\u{1F4DC}', json: '\u{1F4DC}', md: '\u{1F4DD}', csv: '\u{1F4CA}',
    doc: '\u{1F4C4}', docx: '\u{1F4C4}', xls: '\u{1F4CA}', xlsx: '\u{1F4CA}',
  };
  return icons[ext] || '\u{1F4CE}';
}

// ---- Sort by date descending ----
INDEX.sort((a, b) => (b.create_time || 0) - (a.create_time || 0));

// ---- Build sidebar ----
const convList = document.getElementById('convList');
const statsEl = document.getElementById('stats');
const searchBox = document.getElementById('search');
const dateToggle = document.getElementById('dateToggle');
const dateInputs = document.getElementById('dateInputs');
const dateFrom = document.getElementById('dateFrom');
const dateTo = document.getElementById('dateTo');

const audioCount = INDEX.filter(c => c.audio_file).length;
const imgCount = INDEX.filter(c => c.has_images).length;
statsEl.textContent = INDEX.length + ' conversations | ' + audioCount + ' with audio' +
  (imgCount ? ' | ' + imgCount + ' with images' : '');

// Active filters
let activeFilters = new Set();

function buildList(filter) {
  convList.innerHTML = '';
  const lc = (filter || '').toLowerCase();
  const useDate = dateToggle.checked;
  const fromDate = dateFrom.value ? new Date(dateFrom.value + 'T00:00:00').getTime() / 1000 : 0;
  const toDate = dateTo.value ? new Date(dateTo.value + 'T23:59:59').getTime() / 1000 : Infinity;

  // Sort: pinned first, then by date
  const sorted = INDEX.map((c, i) => ({ ...c, _origIdx: i }));
  sorted.sort((a, b) => {
    const ap = userData.pins[a.id] ? 1 : 0;
    const bp = userData.pins[b.id] ? 1 : 0;
    if (ap !== bp) return bp - ap;
    return (b.create_time || 0) - (a.create_time || 0);
  });

  sorted.forEach(conv => {
    const idx = conv._origIdx;

    // Text filter
    if (lc && !(conv.title || '').toLowerCase().includes(lc)) return;

    // Date filter
    if (useDate) {
      const ct = conv.create_time || 0;
      if (ct < fromDate || ct > toDate) return;
    }

    // Category filters
    if (activeFilters.has('pinned') && !userData.pins[conv.id]) return;
    if (activeFilters.has('starred') && !userData.stars[conv.id]) return;
    if (activeFilters.has('audio') && !conv.audio_file) return;
    if (activeFilters.has('images') && !conv.has_images) return;
    if (activeFilters.has('files') && !conv.has_attachments) return;
    if (activeFilters.has('comments') && !userData.comments[conv.id]) return;
    if (activeFilters.has('comments') && userData.comments[conv.id] && Object.keys(userData.comments[conv.id]).length === 0) return;

    const div = document.createElement('div');
    div.className = 'conv-item';
    if (userData.pins[conv.id]) div.classList.add('pinned');
    div.dataset.idx = idx;

    const badges = [];
    if (conv.audio_file) badges.push('<span class="badge badge-audio">audio</span>');
    badges.push('<span class="badge badge-msgs">' + conv.msg_count + ' msgs</span>');
    if (conv.has_images) badges.push('<span class="badge badge-imgs">img</span>');
    if (conv.has_attachments) badges.push('<span class="badge badge-att">files</span>');

    const icons = [];
    if (userData.pins[conv.id]) icons.push('<span class="pin-icon">\u{1F4CC}</span>');
    if (userData.stars[conv.id]) icons.push('<span class="star-icon">\u2B50</span>');

    let summaryLine = '';
    if (userData.summaries[conv.id]) {
      summaryLine = '<div class="conv-summary">' + escapeHtml(userData.summaries[conv.id]) + '</div>';
    }

    div.innerHTML =
      '<div class="conv-title">' + icons.join('') +
      escapeHtml(conv.title || 'Untitled') + '</div>' +
      '<div class="conv-meta"><span class="conv-date">' + escapeHtml(conv.date || '') +
      '</span><span class="conv-badges">' + badges.join('') + '</span></div>' +
      summaryLine;
    div.onclick = () => selectConv(idx);
    div.oncontextmenu = (e) => showContextMenu(e, idx);

    // Show summary on hover via title attr
    if (userData.summaries[conv.id]) {
      div.title = userData.summaries[conv.id];
    }

    convList.appendChild(div);
  });
}

// Date toggle
dateToggle.addEventListener('change', () => {
  dateInputs.classList.toggle('show', dateToggle.checked);
  buildList(searchBox.value);
});
dateFrom.addEventListener('change', () => buildList(searchBox.value));
dateTo.addEventListener('change', () => buildList(searchBox.value));

// Filter buttons
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const f = btn.dataset.filter;
    if (activeFilters.has(f)) {
      activeFilters.delete(f);
      btn.classList.remove('active');
    } else {
      activeFilters.add(f);
      btn.classList.add('active');
    }
    buildList(searchBox.value);
  });
});

let debounceTimer;
searchBox.addEventListener('input', () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => buildList(searchBox.value), 150);
});

// ---- Context menu ----
const ctxMenu = document.getElementById('ctxMenu');
let ctxConvIdx = -1;

function showContextMenu(e, idx) {
  e.preventDefault();
  ctxConvIdx = idx;
  const conv = INDEX[idx];
  const isPinned = userData.pins[conv.id];
  const isStarred = userData.stars[conv.id];

  ctxMenu.innerHTML = [
    '<div class="ctx-menu-item" data-action="pin">' +
      (isPinned ? '\u{1F4CC} Unpin' : '\u{1F4CC} Pin to top') + '</div>',
    '<div class="ctx-menu-item" data-action="star">' +
      (isStarred ? '\u2B50 Unstar' : '\u2B50 Star') + '</div>',
    '<div class="ctx-menu-sep"></div>',
    '<div class="ctx-menu-item" data-action="summary">\u{1F4DD} Write summary</div>',
  ].join('');

  ctxMenu.style.display = 'block';
  ctxMenu.style.left = Math.min(e.clientX, window.innerWidth - 200) + 'px';
  ctxMenu.style.top = Math.min(e.clientY, window.innerHeight - 150) + 'px';

  ctxMenu.querySelectorAll('.ctx-menu-item').forEach(item => {
    item.onclick = () => handleCtxAction(item.dataset.action);
  });
}

document.addEventListener('click', () => { ctxMenu.style.display = 'none'; });

function handleCtxAction(action) {
  ctxMenu.style.display = 'none';
  const conv = INDEX[ctxConvIdx];
  if (action === 'pin') {
    if (userData.pins[conv.id]) delete userData.pins[conv.id];
    else userData.pins[conv.id] = true;
    saveUserData('pins');
    buildList(searchBox.value);
  } else if (action === 'star') {
    if (userData.stars[conv.id]) delete userData.stars[conv.id];
    else userData.stars[conv.id] = true;
    saveUserData('stars');
    buildList(searchBox.value);
  } else if (action === 'summary') {
    openSummaryModal(conv);
  }
}

// ---- Summary modal ----
const summaryModal = document.getElementById('summaryModal');
const summaryText = document.getElementById('summaryText');
let summaryConv = null;

function openSummaryModal(conv) {
  summaryConv = conv;
  summaryText.value = userData.summaries[conv.id] || '';
  summaryModal.classList.add('show');
  summaryText.focus();
}

document.getElementById('summarySave').onclick = () => {
  if (summaryConv) {
    const txt = summaryText.value.trim();
    if (txt) userData.summaries[summaryConv.id] = txt;
    else delete userData.summaries[summaryConv.id];
    saveUserData('summaries');
    buildList(searchBox.value);
  }
  summaryModal.classList.remove('show');
};
document.getElementById('summaryClear').onclick = () => {
  if (summaryConv) {
    delete userData.summaries[summaryConv.id];
    saveUserData('summaries');
    buildList(searchBox.value);
  }
  summaryModal.classList.remove('show');
};
document.getElementById('summaryCancel').onclick = () => {
  summaryModal.classList.remove('show');
};
summaryModal.onclick = (e) => {
  if (e.target === summaryModal) summaryModal.classList.remove('show');
};

// ---- Lightbox ----
const lightbox = document.getElementById('lightbox');
const lightboxImg = document.getElementById('lightboxImg');

function openLightbox(src) {
  lightboxImg.src = src;
  lightbox.classList.add('show');
}
lightbox.onclick = () => lightbox.classList.remove('show');
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    lightbox.classList.remove('show');
    summaryModal.classList.remove('show');
  }
});

// ---- Conversation loader (lazy fetch) ----
const convCache = {};
let currentIdx = -1;

async function selectConv(idx) {
  // Save scroll position of previous conversation
  if (currentIdx >= 0) {
    const msgDiv = document.getElementById('messages');
    const prevConv = INDEX[currentIdx];
    if (prevConv) {
      if (!userData.session.scrollPositions) userData.session.scrollPositions = {};
      userData.session.scrollPositions[prevConv.id] = msgDiv.scrollTop;
    }
  }

  currentIdx = idx;
  userData.session.selectedConv = idx;
  saveUserData('session');

  const meta = INDEX[idx];

  // Highlight in sidebar
  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.idx) === idx);
  });

  // Show header
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('convView').classList.add('visible');
  const hdr = document.getElementById('mainHeader');
  hdr.innerHTML =
    '<h2>' + escapeHtml(meta.title || 'Untitled') + '</h2>' +
    '<div class="meta">' + escapeHtml(meta.date || '') +
    (meta.model ? ' &middot; ' + escapeHtml(meta.model) : '') +
    ' &middot; ' + meta.msg_count + ' messages</div>';

  // Audio player
  const ap = document.getElementById('audioPlayer');
  if (meta.audio_file) {
    ap.style.display = '';
    ap.innerHTML =
      '<audio controls preload="auto" src="' + escapeHtml(meta.audio_file) + '"></audio>' +
      '<a href="' + escapeHtml(meta.audio_file) + '" download>Download MP3</a>';
  } else {
    ap.style.display = 'none';
    ap.innerHTML = '';
  }

  // Show loading state
  const msgDiv = document.getElementById('messages');
  msgDiv.innerHTML = '<div class="loading">Loading conversation...</div>';

  // Fetch conversation data (with cache)
  let messages;
  if (convCache[meta.data_file]) {
    messages = convCache[meta.data_file];
  } else {
    try {
      const resp = await fetch(meta.data_file);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      messages = await resp.json();
      convCache[meta.data_file] = messages;
    } catch (err) {
      msgDiv.innerHTML =
        '<div class="empty-state">Failed to load conversation data.<br><br>' +
        '<small style="color:#f48fb1">Error: ' + escapeHtml(String(err)) + '</small><br><br>' +
        '<small>Make sure you are serving this folder with serve.py:<br>' +
        '<code style="background:#111;padding:4px 8px;border-radius:4px">python serve.py 8000</code></small></div>';
      return;
    }
  }

  // Bail if user clicked a different conversation while loading
  if (currentIdx !== idx) return;

  // Render messages
  msgDiv.innerHTML = '';
  if (!messages || messages.length === 0) {
    msgDiv.innerHTML = '<div class="empty-state">No messages in this conversation</div>';
    return;
  }

  const convHighlights = userData.highlights[meta.id] || {};
  const convComments = userData.comments[meta.id] || {};

  messages.forEach((m, mIdx) => {
    const bubble = document.createElement('div');
    let cls = 'msg msg-assistant';
    if (m.role === 'user') cls = 'msg msg-user';
    else if (m.role === 'tool') cls = 'msg msg-tool';
    if (convHighlights[mIdx]) cls += ' highlighted';
    if (convComments[mIdx]) cls += ' has-comment';
    bubble.className = cls;
    bubble.dataset.msgIdx = mIdx;

    const roleLabel = m.role === 'user' ? 'You' : m.role === 'assistant' ? 'ChatGPT' : m.role;
    const audioBadge = m.is_audio ? ' <span class="msg-audio-badge">voice</span>' : '';

    // Persistent pip indicators
    const highlightPip = convHighlights[mIdx] ? '<div class="msg-highlight-pip"></div>' : '';
    const commentPip = convComments[mIdx] ? '<div class="msg-comment-pip"></div>' : '';

    // Action buttons (visible on hover)
    const isHL = convHighlights[mIdx];
    const actions = '<div class="msg-actions">' +
      '<button class="msg-action-btn' + (isHL ? ' active' : '') + '" data-action="highlight" title="Highlight">&#9733;</button>' +
      '<button class="msg-action-btn" data-action="comment" title="Comment">&#9998;</button>' +
      '</div>';

    let imagesHtml = '';
    if (m.images && m.images.length > 0) {
      imagesHtml = '<div class="msg-images">' +
        m.images.map(src => '<img src="' + escapeHtml(src) + '" alt="Image" onclick="openLightbox(\'' + escapeHtml(src).replace(/'/g, "\\'") + '\')">').join('') +
        '</div>';
    }

    let attachmentsHtml = '';
    if (m.attachments && m.attachments.length > 0) {
      attachmentsHtml = '<div class="msg-attachments">' +
        m.attachments.map(att => {
          if (att.path) {
            return '<a class="msg-attachment" href="' + escapeHtml(att.path) + '" target="_blank" download>' +
              '<span class="att-icon">' + fileIcon(att.name) + '</span>' +
              escapeHtml(att.name) + '</a>';
          }
          return '<span class="msg-attachment"><span class="att-icon">' + fileIcon(att.name) + '</span>' +
            escapeHtml(att.name) + ' <small>(not in export)</small></span>';
        }).join('') +
        '</div>';
    }

    let commentHtml = '';
    if (convComments[mIdx]) {
      commentHtml = '<div class="msg-comment">' + escapeHtml(convComments[mIdx]) + '</div>';
    }

    bubble.innerHTML =
      highlightPip + commentPip + actions +
      '<div class="msg-role">' + escapeHtml(roleLabel) + audioBadge + '</div>' +
      '<div class="msg-body">' + renderMarkdown(m.text || '') + '</div>' +
      imagesHtml + attachmentsHtml + commentHtml +
      (m.time_str ? '<div class="msg-time">' + escapeHtml(m.time_str) + '</div>' : '');

    // Wire up action buttons
    bubble.querySelectorAll('.msg-action-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        handleMsgAction(btn.dataset.action, meta.id, mIdx, bubble);
      };
    });

    msgDiv.appendChild(bubble);
  });

  // Build annotation minimap (colored pips on the scrollbar edge)
  let oldTrack = msgDiv.querySelector('.annotation-track');
  if (oldTrack) oldTrack.remove();
  const totalMsgs = messages.length;
  const hasAnnotations = Object.keys(convHighlights).length > 0 || Object.keys(convComments).length > 0;
  if (hasAnnotations && totalMsgs > 0) {
    const track = document.createElement('div');
    track.className = 'annotation-track';
    for (let i = 0; i < totalMsgs; i++) {
      const pct = (i / totalMsgs) * 100;
      if (convHighlights[i]) {
        const pip = document.createElement('div');
        pip.className = 'annotation-pip pip-highlight';
        pip.style.top = pct + '%';
        pip.title = 'Highlighted message #' + (i + 1);
        pip.onclick = () => {
          const el = msgDiv.querySelector('[data-msg-idx="' + i + '"]');
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        };
        track.appendChild(pip);
      }
      if (convComments[i]) {
        const pip = document.createElement('div');
        pip.className = 'annotation-pip pip-comment';
        pip.style.top = 'calc(' + pct + '% + 5px)';
        pip.title = 'Comment: ' + convComments[i].substring(0, 50);
        pip.onclick = () => {
          const el = msgDiv.querySelector('[data-msg-idx="' + i + '"]');
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        };
        track.appendChild(pip);
      }
    }
    msgDiv.appendChild(track);
  }

  // Restore scroll position
  const savedScroll = (userData.session.scrollPositions || {})[meta.id];
  if (savedScroll) {
    msgDiv.scrollTop = savedScroll;
  } else {
    msgDiv.scrollTop = 0;
  }

  // Mobile: hide sidebar
  if (window.innerWidth < 769) {
    document.getElementById('sidebar').classList.add('hidden');
  }
}

// ---- Message annotation actions ----
function handleMsgAction(action, convId, msgIdx, bubble) {
  if (action === 'highlight') {
    if (!userData.highlights[convId]) userData.highlights[convId] = {};
    if (userData.highlights[convId][msgIdx]) {
      delete userData.highlights[convId][msgIdx];
      bubble.classList.remove('highlighted');
      bubble.querySelector('[data-action="highlight"]').classList.remove('active');
    } else {
      userData.highlights[convId][msgIdx] = true;
      bubble.classList.add('highlighted');
      bubble.querySelector('[data-action="highlight"]').classList.add('active');
    }
    saveUserData('highlights');
  } else if (action === 'comment') {
    // Toggle comment input
    let existing = bubble.querySelector('.msg-comment-input');
    if (existing) {
      existing.remove();
      return;
    }
    const input = document.createElement('textarea');
    input.className = 'msg-comment-input';
    input.placeholder = 'Add a comment... (Enter to save, Esc to cancel)';
    if (!userData.comments[convId]) userData.comments[convId] = {};
    input.value = userData.comments[convId][msgIdx] || '';
    bubble.appendChild(input);
    input.focus();

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const val = input.value.trim();
        if (val) {
          userData.comments[convId][msgIdx] = val;
        } else {
          delete userData.comments[convId][msgIdx];
        }
        saveUserData('comments');
        // Refresh the comment display and indicator
        let cd = bubble.querySelector('.msg-comment');
        if (val) {
          if (!cd) {
            cd = document.createElement('div');
            cd.className = 'msg-comment';
            bubble.insertBefore(cd, bubble.querySelector('.msg-time'));
          }
          cd.textContent = val;
          bubble.classList.add('has-comment');
          // Add pip indicator if missing
          if (!bubble.querySelector('.msg-comment-pip')) {
            const pip = document.createElement('div');
            pip.className = 'msg-comment-pip';
            bubble.appendChild(pip);
          }
        } else {
          if (cd) cd.remove();
          bubble.classList.remove('has-comment');
          const pip = bubble.querySelector('.msg-comment-pip');
          if (pip) pip.remove();
        }
        input.remove();
      } else if (e.key === 'Escape') {
        input.remove();
      }
    });
  }
}

// ---- Save scroll on scroll ----
let scrollSaveTimer;
document.getElementById('messages').addEventListener('scroll', () => {
  clearTimeout(scrollSaveTimer);
  scrollSaveTimer = setTimeout(() => {
    if (currentIdx >= 0) {
      const conv = INDEX[currentIdx];
      if (conv) {
        if (!userData.session.scrollPositions) userData.session.scrollPositions = {};
        userData.session.scrollPositions[conv.id] = document.getElementById('messages').scrollTop;
        saveUserData('session');
      }
    }
  }, 500);
});

// Mobile toggle
document.getElementById('mobileToggle').onclick = () => {
  document.getElementById('sidebar').classList.toggle('hidden');
};

// ---- Init ----
async function init() {
  loadLocalStorageFallback();
  await loadUserData();
  buildList('');

  // Restore last session state
  const savedIdx = userData.session.selectedConv;
  if (typeof savedIdx === 'number' && savedIdx >= 0 && savedIdx < INDEX.length) {
    selectConv(savedIdx);
  }

  // Detect file:// and show warning
  if (location.protocol === 'file:') {
    document.getElementById('emptyState').innerHTML =
      '<div>' +
      '<strong style="color:#f48fb1;font-size:18px">Local server required</strong><br><br>' +
      'This viewer loads conversation data on demand and needs HTTP.<br>' +
      'Open a terminal in this folder and run:<br><br>' +
      '<code style="background:#111;padding:6px 12px;border-radius:6px;font-size:14px">' +
      'python serve.py 8000</code><br><br>' +
      'Then open <a href="http://localhost:8000" style="color:var(--accent)">http://localhost:8000</a>' +
      '</div>';
  }
}

init();
</script>
</body>
</html>"""


def generate_viewer(index_data: list[dict], output_dir: str):
    """Generate the index.html with embedded sidebar metadata."""
    # Escape </ sequences that would break the <script> tag
    json_str = json.dumps(index_data, ensure_ascii=False)
    json_str = json_str.replace("</", "<\\/")

    html_content = HTML_TEMPLATE.replace("%%INDEX_JSON%%", json_str)
    html_path = os.path.join(output_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    size_kb = os.path.getsize(html_path) / 1024
    print(f"  Generated index.html ({size_kb:.0f} KB)")


def write_conversation_json(messages: list[dict], output_path: str):
    """Write a single conversation's messages to a JSON file."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False)


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "ChatGPT Export Viewer & Audio Stitcher (v6)\n"
            "Generates a dark-themed HTML conversation viewer and optionally\n"
            "stitches voice-mode audio clips into single MP3 files.\n"
            "Full image/attachment discovery and persistent annotations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python chatgpt_export_viewer.py --export-dir ./my-export --output-dir ./viewer
  python chatgpt_export_viewer.py --export-dir ./export --output-dir ./out --skip-audio

After running, serve the output folder with Range support:
  cd ./viewer && python serve.py 8000
  # Open http://localhost:8000 in your browser
        """,
    )
    parser.add_argument(
        "--export-dir", required=True,
        help="Path to the ChatGPT export folder (contains conversations-*.json).",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Path for the output (index.html + data/ + audio/ + images/).",
    )
    parser.add_argument(
        "--skip-audio", action="store_true",
        help="Skip audio stitching (only regenerate the HTML viewer).",
    )
    args = parser.parse_args()

    export_dir = os.path.abspath(args.export_dir)
    output_dir = os.path.abspath(args.output_dir)

    if not os.path.isdir(export_dir):
        print(f"ERROR: Export directory not found: {export_dir}")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    audio_out_dir = os.path.join(output_dir, "audio")
    data_out_dir = os.path.join(output_dir, "data")
    images_out_dir = os.path.join(output_dir, "images")
    attachments_out_dir = os.path.join(output_dir, "attachments")
    userdata_dir = os.path.join(output_dir, "userdata")
    os.makedirs(data_out_dir, exist_ok=True)
    os.makedirs(userdata_dir, exist_ok=True)

    # Copy serve.py to output dir if not already there
    serve_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py")
    serve_dst = os.path.join(output_dir, "serve.py")
    if os.path.isfile(serve_src) and not os.path.isfile(serve_dst):
        shutil.copy2(serve_src, serve_dst)
        print("  Copied serve.py to output directory")

    # ---- Step 1: Load conversations ----
    print("\n[1/4] Loading conversations...")
    conversations = load_conversations(export_dir)

    # ---- Pre-compute safe filenames (unique per conversation) ----
    used_names: dict[str, str] = {}   # conv_id -> safe_name
    name_set: set[str] = set()

    for conv in conversations:
        conv_id = conv.get("id") or conv.get("conversation_id", "")
        title = conv.get("title", "Untitled")
        safe = sanitize_filename(title)
        base = safe
        counter = 1
        while base in name_set:
            base = f"{safe}_{counter}"
            counter += 1
        name_set.add(base)
        used_names[conv_id] = base

    # ---- Step 2: Stitch audio ----
    print("\n[2/4] Processing audio...")
    audio_map: dict[str, str] = {}   # conv_id -> relative audio path
    audio_stitched = 0
    audio_failed = 0

    if not args.skip_audio:
        os.makedirs(audio_out_dir, exist_ok=True)

    for conv in conversations:
        conv_id = conv.get("id") or conv.get("conversation_id", "")
        if not conv_id:
            continue
        audio_files = find_audio_files(export_dir, conv_id)
        if not audio_files:
            continue

        safe_name = used_names.get(conv_id, conv_id)
        title = conv.get("title", "Untitled")

        if args.skip_audio:
            # Still record existing MP3s from a previous run
            existing_mp3 = os.path.join(audio_out_dir, f"{safe_name}.mp3")
            if os.path.isfile(existing_mp3):
                audio_map[conv_id] = f"audio/{safe_name}.mp3"
                audio_stitched += 1
            continue

        print(f"  Stitching \"{title}\" ({len(audio_files)} clips)...")
        sorted_audio = sorted(audio_files, key=extract_sort_key)
        mp3_path = os.path.join(audio_out_dir, f"{safe_name}.mp3")

        if stitch_audio(sorted_audio, mp3_path):
            audio_map[conv_id] = f"audio/{safe_name}.mp3"
            audio_stitched += 1
        else:
            audio_failed += 1

    print(f"  Audio: {audio_stitched} stitched" + (f", {audio_failed} failed" if audio_failed else ""))

    # ---- Step 3: Build global asset index ----
    print("\n[3/4] Indexing all files in export...")
    asset_index = build_global_asset_index(export_dir)
    print(f"  Sediment assets (file_):  {len(asset_index['by_sediment_hex'])}")
    print(f"  User uploads (file-):     {len(asset_index['by_attachment_id'])}")
    print(f"  Total indexed filenames:  {len(asset_index['by_filename'])}")

    # ---- Step 4: Extract messages, copy images/attachments, build index ----
    print("\n[4/4] Generating viewer...")

    index_data = []
    total_images = 0
    total_attachments = 0

    for conv in conversations:
        conv_id = conv.get("id") or conv.get("conversation_id", "")
        title = conv.get("title") or "Untitled"
        create_time = conv.get("create_time")
        model = conv.get("default_model_slug", "")
        safe_name = used_names.get(conv_id, conv_id)

        messages = extract_messages(conv, asset_index, images_out_dir,
                                    attachments_out_dir)
        audio_file = audio_map.get(conv_id, "")

        # Count images and attachments in messages
        has_images = any(m.get("images") for m in messages)
        has_attachments = any(m.get("attachments") for m in messages)
        img_count = sum(len(m.get("images", [])) for m in messages)
        att_count = sum(len(m.get("attachments", [])) for m in messages)
        total_images += img_count
        total_attachments += att_count

        # Write per-conversation JSON
        data_file = f"data/{safe_name}.json"
        write_conversation_json(messages, os.path.join(output_dir, data_file))

        # Sidebar index entry (lightweight — no message text)
        index_data.append({
            "id": conv_id,
            "title": title,
            "date": format_timestamp(create_time),
            "create_time": create_time or 0,
            "model": model,
            "msg_count": len(messages),
            "audio_file": audio_file,
            "data_file": data_file,
            "has_images": has_images,
            "has_attachments": has_attachments,
        })

    generate_viewer(index_data, output_dir)

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("  DONE!")
    print(f"  Conversations:  {len(index_data)}")
    print(f"  With audio:     {audio_stitched}")
    print(f"  Images copied:  {total_images}")
    print(f"  Attachments:    {total_attachments}")
    print(f"  Output:         {output_dir}")
    print()
    print("  To view, run:")
    print(f"    cd \"{output_dir}\"")
    print(f"    python serve.py 8000")
    print(f"    # Then open http://localhost:8000")
    print("=" * 60)


if __name__ == "__main__":
    main()

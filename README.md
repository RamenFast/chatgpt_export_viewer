# ChatGPT Export Viewer & Audio Stitcher

Parse your ChatGPT data export into a browsable, dark-themed web viewer with stitched audio, inline images, and persistent annotations.



---

<img width="1172" height="1401" alt="ViewerScreenshot" src="https://github.com/user-attachments/assets/c205ea6f-6942-4566-a70b-e78fb3aa2dd5" />


---

## File Output Structure

```
your-output-dir/
|
|-- index.html          The viewer UI (small — loads data on demand)
|-- serve.py            Local server (required for audio scrubbing + annotations)
|
|-- data/               One JSON file per conversation (lazy-loaded by viewer)
|   |-- My_Chat.json
|   |-- Another_Chat.json
|
|-- audio/              Stitched MP3s — one per voice conversation
|   |-- My_Chat.mp3     (generated from individual WAV clips)
|
|-- images/             Copied from export — displayed inline in viewer
|   |-- file_00...png
|
|-- attachments/        Copied from export — downloadable from viewer
|   |-- my_document.pdf
|
|-- userdata/           Your annotations (auto-created, persistent across sessions)
    |-- pins.json       Pinned conversations
    |-- stars.json      Starred conversations
    |-- highlights.json Highlighted messages
    |-- comments.json   Your notes on messages
    |-- summaries.json  Conversation summaries you wrote
    |-- session.json    Last viewed chat + scroll position
```

**`data/`** is regenerated every run. **`audio/`** is only regenerated without `--skip-audio`. **`userdata/`** is never overwritten — your annotations persist across regenerations.

---

## Features

- Dark-themed conversation viewer with user/GPT message attribution
- Voice audio clips stitched into single MP3 per conversation, playable + scrubbable + downloadable
- Inline image display with lightbox zoom
- File attachments shown with download links
- Search by title or date range
- Filter by: Pinned, Starred, Audio, Images, Files, Comments
- Right-click conversations to Pin, Star, or write a Summary (shown on hover)
- Highlight messages (golden border + dot indicator visible while scrolling)
- Comment on messages (purple border + dot indicator visible while scrolling)
- Annotation minimap on scrollbar edge — click pips to jump to annotated messages
- Session persistence — remembers which chat you were in and your scroll position
- All annotations stored as JSON files in `userdata/` — survives regeneration

---

## Quick Start

### 1. Set up Python environment

**macOS / Linux:**
```bash
cd /path/to/your/export
python3 -m venv .venv
source .venv/bin/activate
pip install pydub
```

**Windows (WSL):**
```bash
cd /mnt/c/Users/You/path/to/export
python3 -m venv .venv
source .venv/bin/activate
pip install pydub
```

You also need **ffmpeg** on your PATH (only for audio stitching):
```bash
# Ubuntu/Debian/WSL:
sudo apt install ffmpeg

# macOS:
brew install ffmpeg
```

### 2. Run the script

```bash
python chatgpt_export_viewer.py \
  --export-dir /path/to/your/chatgpt-export-folder \
  --output-dir /path/to/output
```

The `--export-dir` should point to the folder containing `conversations.json` (or `conversations-*.json` split files) and the conversation UUID subfolders.

### 3. Serve and view

```bash
cd /path/to/output
python serve.py 8000
```

Open **http://localhost:8000** in your browser.

> **The server may take 10-30 seconds to start** on first load, especially with large exports. This is normal — it's indexing files. Wait for the "Serving on http://localhost:8000" message before opening the browser.

**Important:** Use `serve.py`, not `python -m http.server`. The custom server provides:
- HTTP Range requests (required for audio scrubbing/seeking)
- PUT/DELETE support (required for saving your annotations)

---

## Regenerating the Viewer

If you update your export or want to regenerate the HTML without re-stitching audio:

```bash
python chatgpt_export_viewer.py \
  --export-dir /path/to/export \
  --output-dir /path/to/output \
  --skip-audio
```

**Tips:**
- `--skip-audio` skips the slow audio stitching step — it picks up existing MP3s from a previous run. Use this when you only need to update the HTML/data.
- Audio stitching can take several minutes for 100+ conversations. Run it once, then use `--skip-audio` for subsequent regenerations.
- Your `userdata/` folder is never touched by regeneration — annotations are safe.
- If you get a new ChatGPT export, point `--export-dir` at the new folder and regenerate. The output dir structure is replaced (except `userdata/`).

---

## Managing Your venv

```bash
# Activate (do this each time before running the script):
source .venv/bin/activate       # macOS/Linux/WSL

# Deactivate when done:
deactivate

# If pydub is missing:
pip install pydub

# If you need to recreate the venv:
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install pydub
```

The venv only needs `pydub` installed. Everything else uses the Python standard library.

---

## Requirements

- Python 3.9+
- pydub (`pip install pydub`) — audio stitching only
- ffmpeg on PATH — audio stitching only
- A modern browser (Chrome, Firefox, Edge, Safari)

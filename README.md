# Renderable

Renderable is a Flask-based backend for hosting and sharing real estate virtual tours. Agents upload a ZIP of ordered room-capture images with a `manifest.json`, and Renderable serves a full-screen panoramic viewer, a public share page, a lead capture form, analytics dashboard, and PDF/CSV exports — all with no database, using flat JSON and JSONL files on disk.

---

## Requirements

- **Python 3.10+**
- **pip packages:**
  ```
  flask
  reportlab      # optional — only needed for PDF export
  ```

Install:
```bash
pip install flask
pip install reportlab   # optional
```

---

## Running locally

```bash
python app.py
```

Starts on **http://localhost:5050**. Debug mode is on by default.

Open the scan library at: **http://localhost:5050/scans**

---

## Folder structure

```
renderable-backend/
├── app.py                  # Flask backend — all routes and logic
├── README.md               # This file
├── DEVELOPER.md            # Deeper technical reference for contributors
├── templates/
│   ├── viewer.html         # Panoramic frame viewer (all JS inline)
│   ├── share.html          # Public share / listing page
│   ├── library.html        # Scan library dashboard
│   ├── listing_edit.html   # Edit listing metadata
│   ├── scan_edit.html      # Edit rooms and annotations
│   ├── analytics.html      # Engagement analytics
│   └── leads.html          # Lead management
└── uploads/
    └── <scan_id>/          # One folder per scan (8-char hex ID)
        ├── manifest.json
        ├── frame_001.jpg   # Frame images (flat — no subdirectory)
        ├── analytics.jsonl
        └── leads.jsonl
```

---

## How uploads are stored

Each scan lives in `uploads/<scan_id>/`. The `scan_id` is a random 8-character hex string generated at upload time.

All frame images are stored flat in the scan folder — there is no `frames/` subdirectory on disk. The URL `/uploads/<scan_id>/frames/<filename>` is served by a Flask route that maps to `uploads/<scan_id>/<filename>`.

**`manifest.json`** is the single source of truth for each scan:

```json
{
  "scan_name": "3BR Apartment — Central",
  "created_at": "2025-01-01T10:00:00Z",
  "frame_count": 42,
  "frames": [
    {
      "filename": "frame_001.jpg",
      "heading": 180.0,
      "neighbors": { "forward": 1, "back": null },
      "discarded": false
    }
  ],
  "thumbnail_filename": "frame_001.jpg",
  "rooms": [],
  "annotations": [],
  "listing": {}
}
```

Frame entries may also be plain strings (`"frame_001.jpg"`) — the legacy format is still supported.

Analytics events are appended to `analytics.jsonl` (one JSON object per line). Leads are stored the same way in `leads.jsonl`.

---

## Route reference

### Pages

| Route | Template | Description |
|---|---|---|
| `GET /scans` | library.html | Scan library — list all uploads |
| `GET /view/<scan_id>` | viewer.html | Full-screen panoramic viewer |
| `GET /share/<scan_id>` | share.html | Public listing page with lead form |
| `GET /listing/<scan_id>/edit` | listing_edit.html | Edit listing metadata |
| `GET /scan/<scan_id>/edit` | scan_edit.html | Edit rooms and annotations |
| `GET /scan/<scan_id>/analytics` | analytics.html | Engagement analytics |
| `GET /scan/<scan_id>/leads` | leads.html | Lead inbox |

### API — read

| Route | Returns |
|---|---|
| `GET /api/scans` | Array of scan summaries (sorted newest first) |
| `GET /api/manifest/<scan_id>` | Full manifest + `frameURLs[]` + `thumbnailURL` |
| `GET /api/analytics/<scan_id>` | Aggregated analytics summary |
| `GET /api/scans/<scan_id>/leads` | Array of lead objects, newest first |
| `GET /api/scans/<scan_id>/rooms` | Rooms array from manifest |
| `GET /api/scans/<scan_id>/annotations` | Annotations array from manifest |

### API — write

| Route | Method | Body | Effect |
|---|---|---|---|
| `/scan/upload` | POST | multipart `file` (.zip) | Unzip, validate, assign scan_id |
| `/api/scans/<scan_id>/rename` | PATCH | `{ "scan_name": "..." }` | Rename scan |
| `/api/scans/<scan_id>/listing` | PATCH | listing fields | Update listing block |
| `/api/scans/<scan_id>/rooms` | PUT | JSON array | Replace rooms |
| `/api/scans/<scan_id>/annotations` | PUT | JSON array | Replace annotations |
| `/api/scans/<scan_id>` | DELETE | — | Delete scan folder |
| `/api/leads/<scan_id>/<lead_id>/status` | PATCH | `{ "status": "..." }` | Update lead status |
| `/analytics/event` | POST | event object | Append analytics event |
| `/scan/<scan_id>/lead` | POST | lead fields | Save new lead |

### Exports

| Route | Returns |
|---|---|
| `GET /scan/<scan_id>/export` | ZIP package of full scan |
| `GET /scan/<scan_id>/export/report` | PDF listing report (requires `reportlab`) |
| `GET /scan/<scan_id>/export/leads` | Leads as CSV |
| `GET /scan/<scan_id>/export/analytics` | Full event log as JSON |

### Static files

| Route | Serves |
|---|---|
| `GET /uploads/<scan_id>/frames/<filename>` | Frame image (JPEG) |
| `GET /uploads/<scan_id>/scan.usdz` | USDZ file if present |

---

## How to add a new feature

**Add a listing field:**
1. Add the key to the `allowed` list in `update_listing()` in `app.py`
2. Add the `<input>` to `listing_edit.html` and include it in the `PATCH` payload
3. Consume the new field in `share.html` or `viewer.html`

**Add an analytics event type:**
1. Add the string to `ALLOWED_EVENT_TYPES` in `app.py`
2. Call `track("your_event_type", {...})` from the relevant template

**Add a new page:**
1. Create the template in `templates/`
2. Add a `@app.route(...)` in `app.py` returning `render_template("yourpage.html", scan_id=scan_id)`
3. Link to it from `library.html` or the scan card

**Add a new per-scan data store:**
Follow the JSONL pattern used by leads and analytics — add `yourdata_path()`, `load_yourdata()`, `save_yourdata()` helpers, then add GET + write routes under `/api/scans/<scan_id>/yourdata`.

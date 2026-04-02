# Renderable — Developer Reference

## Running locally

```bash
pip install flask reportlab   # reportlab only needed for PDF export
python app.py                 # starts on http://localhost:5050
```

Open the scan library at `http://localhost:5050/scans`.

No database. All state lives in `uploads/<scan_id>/`. Flask debug mode is on by default in `app.py`.

---

## Project structure

```
renderable-backend/
├── app.py                  # Flask backend — all routes
├── templates/
│   ├── viewer.html         # Panoramic tour viewer (all JS inline)
│   ├── share.html          # Public share page + lead capture form
│   ├── library.html        # Scan library dashboard
│   ├── listing_edit.html   # Listing metadata editor
│   ├── scan_edit.html      # Room + annotation editor
│   ├── analytics.html      # Engagement analytics dashboard
│   └── leads.html          # Lead management table
└── uploads/
    └── <scan_id>/
        ├── manifest.json   # Source of truth for this scan (see below)
        ├── *.jpg           # Frame images (flat directory, no subdirectory)
        ├── analytics.jsonl # Append-only event log
        └── leads.jsonl     # Append-only lead log
```

### manifest.json schema

```json
{
  "scan_name": "My Listing",
  "created_at": "2025-01-01T00:00:00Z",
  "frame_count": 42,
  "frames": [
    {
      "filename": "frame_001.jpg",
      "heading": 180.0,
      "neighbors": { "forward": 1, "back": null },
      "discarded": false,
      "blur": 12.4,
      "rotation_rate": 0.3,
      "acceleration": 0.1,
      "delta_yaw": 8.5,
      "exposure_score": 5.2,
      "polar_x": 0.2,
      "polar_y": 0.1,
      "angle_from_start": 12.0
    }
  ],
  "thumbnail_filename": "frame_001.jpg",
  "rooms": [{ "id": "abc", "name": "Kitchen", "start_frame": 0, "end_frame": 10, "icon": "🍳" }],
  "annotations": [{ "id": "xyz", "frame_index": 5, "title": "Note", "body": "...", "type": "info" }],
  "listing": {
    "listing_title": "3BR Apartment",
    "listing_subtitle": "Central location",
    "address": "123 Main St",
    "description": "...",
    "property_type": "apartment",
    "room_count": 3,
    "area_sqm": 85,
    "contact_name": "Agent Name",
    "contact_email": "agent@example.com",
    "contact_phone": "+1 555 0100",
    "branding_name": "Agency Name"
  },
  "capture_mode": "standard",
  "lens_factor": 1.0,
  "target_frame_count": 50
}
```

Frame entries may also be plain strings (`"frame_001.jpg"`) — legacy format, still supported.
Fields under `frames[]` other than `filename` are all optional; older manifests omit most of them.

---

## Route map

### Pages (HTML)

| Route | Template | Description |
|---|---|---|
| `GET /scans` | library.html | Scan library |
| `GET /view/<scan_id>` | viewer.html | Panoramic tour viewer |
| `GET /share/<scan_id>` | share.html | Public share page |
| `GET /listing/<scan_id>/edit` | listing_edit.html | Edit listing metadata |
| `GET /scan/<scan_id>/edit` | scan_edit.html | Edit rooms + annotations |
| `GET /scan/<scan_id>/analytics` | analytics.html | Engagement analytics |
| `GET /scan/<scan_id>/leads` | leads.html | Lead management |

### API — read

| Route | Returns |
|---|---|
| `GET /api/scans` | JSON array of all scan summaries |
| `GET /api/manifest/<scan_id>` | Full manifest + computed `frameURLs[]` + `thumbnailURL` |
| `GET /api/analytics/<scan_id>` | Aggregated analytics summary |
| `GET /api/scans/<scan_id>/leads` | Array of lead objects, newest first |
| `GET /api/scans/<scan_id>/rooms` | Rooms array from manifest |
| `GET /api/scans/<scan_id>/annotations` | Annotations array from manifest |

### API — write

| Route | Body | Effect |
|---|---|---|
| `POST /scan/upload` | multipart `file` (.zip) | Unzips, validates manifest, assigns scan_id |
| `PATCH /api/scans/<scan_id>/rename` | `{ scan_name }` | Renames scan |
| `PATCH /api/scans/<scan_id>/listing` | listing fields (any subset) | Updates listing block in manifest |
| `PUT /api/scans/<scan_id>/rooms` | JSON array of room objects | Replaces rooms in manifest |
| `PUT /api/scans/<scan_id>/annotations` | JSON array of annotation objects | Replaces annotations in manifest |
| `PATCH /api/leads/<scan_id>/<lead_id>/status` | `{ status }` | Updates lead status (new/contacted/archived) |
| `DELETE /api/scans/<scan_id>` | — | Deletes scan folder entirely |
| `POST /analytics/event` | event object | Appends one analytics event |
| `POST /scan/<scan_id>/lead` | lead fields | Saves a new lead |

### Exports

| Route | Returns |
|---|---|
| `GET /scan/<scan_id>/export` | ZIP package of the scan |
| `GET /scan/<scan_id>/export/report` | PDF listing report (requires `reportlab`) |
| `GET /scan/<scan_id>/export/leads` | Leads as CSV |
| `GET /scan/<scan_id>/export/analytics` | Full event log as JSON |

### Static files

| Route | Serves |
|---|---|
| `GET /uploads/<scan_id>/frames/<filename>` | Frame image (maps to `uploads/<scan_id>/<filename>` — no `frames/` subdirectory on disk) |
| `GET /uploads/<scan_id>/scan.usdz` | USDZ file if present |

---

## How to add a new feature

### Add a new listing field

1. Add the field name to the `allowed` list in `update_listing()` in `app.py`.
2. Add the `<input>` to `templates/listing_edit.html` and include it in the `PATCH` payload.
3. Consume it in `templates/share.html` or `viewer.html` as needed.

### Add a new analytics event type

1. Add the string to `ALLOWED_EVENT_TYPES` in `app.py`.
2. Call `track("your_event_type", { ... })` from the relevant template JS.
3. Add aggregation logic to `analytics_summary()` if you want it surfaced in the dashboard.

### Add a new page

1. Add the template to `templates/`.
2. Add a `@app.route(...)` in `app.py` that returns `render_template("yourpage.html", scan_id=scan_id)`.
3. Add a link to it from `library.html` or the relevant scan card.

### Add a new per-scan data store

Follow the JSONL pattern used by leads and analytics:
1. Add `def yourdata_path(scan_id)` helper.
2. Add `load_yourdata()` / `save_yourdata()` helpers (see `load_leads` / `save_leads`).
3. Add GET + PATCH routes under `/api/scans/<scan_id>/yourdata`.

---

## Key implementation notes

- **Frame URL vs filesystem path**: The URL `/uploads/<scan_id>/frames/<filename>` is served by `serve_frame()` which reads from `uploads/<scan_id>/<filename>` (no `frames/` directory on disk). This is intentional — the URL segment is cosmetic.
- **Discarded frames**: When any frame has `"discarded": true`, the viewer filters them out before rendering. Room `start_frame`/`end_frame` indices are remapped from original to filtered indices automatically.
- **Circular scan detection**: `isCircular = true` when the scan has a navigation graph and the angular difference between the first and last frame heading is < 30°. The strip viewer loops seamlessly in this case.
- **Panoramic strip mode**: Active when `frameURLs.length >= 4`. Drag drives `scrubTo()` directly at sub-frame resolution; release triggers exponential momentum decay then a spring snap. For < 4 frames the old parallax-preview + threshold-snap is used.
- **sendBeacon content type**: Analytics events use `new Blob([JSON.stringify(payload)], {type: "application/json"})` — plain string sends as `text/plain` and Flask's `request.get_json()` returns `null`.
- **No database**: Manifests, leads, and analytics are all flat files. Concurrent writes are not safe beyond single-process use; add a write lock or migrate to SQLite before multi-worker deployment.

---

## Cleanup notes

- `test.usdz` in the project root is a leftover test artifact — safe to delete.
- `uploads/` is gitignored (or should be) — contains real scan data, never commit it.

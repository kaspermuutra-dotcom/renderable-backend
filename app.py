from flask import Flask, request, jsonify, render_template, send_file
import os, uuid, zipfile, json, shutil, logging, re, io, csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_manifest(scan_id):
    path = os.path.join(UPLOAD_FOLDER, scan_id, "manifest.json")
    if not os.path.exists(path): return None
    try:
        with open(path) as f: return json.load(f)
    except Exception: return None

def save_manifest(scan_id, manifest):
    path = os.path.join(UPLOAD_FOLDER, scan_id, "manifest.json")
    with open(path, "w") as f: json.dump(manifest, f, indent=2)

def scan_summary(scan_id, manifest):
    thumbnail    = manifest.get("thumbnail_filename")
    listing      = manifest.get("listing", {})
    frames       = manifest.get("frames", [])
    total_frames = manifest.get("frame_count", 0)
    dict_frames   = [f for f in frames if isinstance(f, dict)]
    active_frames = (
        sum(1 for f in dict_frames if not f.get("discarded", False))
        if dict_frames else total_frames
    )
    return {
        "scan_id":            scan_id,
        "scan_name":          manifest.get("scan_name") or scan_id,
        "created_at":         manifest.get("created_at", ""),
        "frame_count":        total_frames,
        "active_frame_count": active_frames,
        "thumbnail_url":      f"/uploads/{scan_id}/frames/{thumbnail}" if thumbnail else None,
        "viewer_url":         f"/view/{scan_id}",
        "share_url":          f"/share/{scan_id}",
        "listing":            listing
    }

def analytics_path(scan_id):
    return os.path.join(UPLOAD_FOLDER, scan_id, "analytics.jsonl")

def leads_path(scan_id):
    return os.path.join(UPLOAD_FOLDER, scan_id, "leads.jsonl")

def append_event(scan_id, event):
    path = analytics_path(scan_id)
    try:
        with open(path, "a") as f: f.write(json.dumps(event) + "\n")
    except Exception as e:
        log.warning(f"[{scan_id}] Failed to write analytics event: {e}")

def load_events(scan_id):
    path = analytics_path(scan_id)
    if not os.path.exists(path): return []
    events = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: events.append(json.loads(line))
                except: pass
    except Exception: pass
    return events

def load_leads(scan_id):
    path = leads_path(scan_id)
    if not os.path.exists(path): return []
    leads = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: leads.append(json.loads(line))
                except: pass
    except Exception: pass
    return leads

def save_leads(scan_id, leads):
    path = leads_path(scan_id)
    try:
        with open(path, "w") as f:
            for lead in leads:
                f.write(json.dumps(lead) + "\n")
    except Exception as e:
        log.warning(f"[{scan_id}] Failed to save leads: {e}")

def sanitize(val, max_len=500):
    if not isinstance(val, str): return ""
    return val.strip()[:max_len]

def valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))

# ── Analytics ingestion ───────────────────────────────────────────────────────

ALLOWED_EVENT_TYPES = {
    "share_view", "viewer_open", "session_start", "session_end",
    "frame_view", "cta_click", "copy_link", "contact_click",
    "lead_form_opened", "lead_submitted"
}

@app.route("/analytics/event", methods=["POST"])
def ingest_event():
    body = request.get_json(silent=True) or {}
    scan_id    = body.get("scan_id", "").strip()
    event_type = body.get("event_type", "").strip()
    session_id = body.get("session_id", "").strip()

    if not scan_id or not event_type:
        return jsonify({"error": "scan_id and event_type required"}), 400
    if event_type not in ALLOWED_EVENT_TYPES:
        return jsonify({"error": f"unknown event_type: {event_type}"}), 400
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return jsonify({"error": "scan not found"}), 404

    # Validate and sanitize frame_index
    raw_fi = body.get("frame_index")
    frame_index = None
    if raw_fi is not None:
        try:
            frame_index = int(raw_fi)
            if frame_index < 0:
                frame_index = None
            elif event_type == "frame_view":
                manifest = load_manifest(scan_id)
                if manifest:
                    fc = manifest.get("frame_count", 0)
                    if fc > 0 and frame_index >= fc:
                        return jsonify({"error": "frame_index out of range"}), 400
        except (TypeError, ValueError):
            frame_index = None

    event = {
        "event_id":    str(uuid.uuid4())[:8],
        "scan_id":     scan_id,
        "session_id":  session_id or str(uuid.uuid4())[:8],
        "event_type":  event_type,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "frame_index": frame_index,
        "room_name":   body.get("room_name"),
        "source_page": body.get("source_page", ""),
        "meta":        body.get("meta", {})
    }
    append_event(scan_id, event)
    return jsonify({"ok": True})

# ── Analytics summary ─────────────────────────────────────────────────────────

@app.route("/api/analytics/<scan_id>")
def analytics_summary(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return jsonify({"error": "scan not found"}), 404

    events = load_events(scan_id)
    leads  = load_leads(scan_id)

    if not events:
        return jsonify({
            "scan_id": scan_id, "total_share_views": 0,
            "total_viewer_opens": 0, "unique_sessions": 0,
            "avg_session_duration_sec": 0, "avg_frames_per_session": 0,
            "most_viewed_frames": [], "most_viewed_rooms": [],
            "recent_activity": [], "total_leads": len(leads), "empty": True
        })

    share_views  = sum(1 for e in events if e.get("event_type") == "share_view")
    viewer_opens = sum(1 for e in events if e.get("event_type") == "viewer_open")
    all_sessions = set(e.get("session_id") for e in events if e.get("session_id"))

    session_events = defaultdict(list)
    for e in events:
        sid = e.get("session_id"); ts = e.get("timestamp")
        if sid and ts:
            try: session_events[sid].append(datetime.fromisoformat(ts))
            except: pass

    durations = []
    for sid, timestamps in session_events.items():
        if len(timestamps) >= 2:
            timestamps.sort()
            durations.append((timestamps[-1] - timestamps[0]).total_seconds())
    avg_duration = round(sum(durations) / len(durations)) if durations else 0

    session_frames = defaultdict(set)
    for e in events:
        if e.get("event_type") == "frame_view" and e.get("frame_index") is not None:
            session_frames[e["session_id"]].add(e["frame_index"])
    frames_per_session = [len(v) for v in session_frames.values()]
    avg_frames = round(sum(frames_per_session) / len(frames_per_session), 1) if frames_per_session else 0

    frame_counts = defaultdict(int)
    for e in events:
        if e.get("event_type") == "frame_view" and e.get("frame_index") is not None:
            frame_counts[str(e["frame_index"])] += 1
    most_viewed_frames = sorted(
        [{"frame_index": int(k), "views": v} for k, v in frame_counts.items()],
        key=lambda x: x["views"], reverse=True)[:10]

    room_counts = defaultdict(int)
    for e in events:
        if e.get("event_type") == "frame_view" and e.get("room_name"):
            room_counts[e["room_name"]] += 1
    most_viewed_rooms = sorted(
        [{"room_name": k, "views": v} for k, v in room_counts.items()],
        key=lambda x: x["views"], reverse=True)[:5]

    now = datetime.now(timezone.utc)
    day_counts = defaultdict(int)
    for e in events:
        ts = e.get("timestamp")
        if not ts: continue
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt).days <= 6: day_counts[dt.strftime("%Y-%m-%d")] += 1
        except: pass

    recent_activity = []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        recent_activity.append({"date": day, "events": day_counts.get(day, 0)})

    return jsonify({
        "scan_id": scan_id,
        "total_share_views": share_views,
        "total_viewer_opens": viewer_opens,
        "unique_sessions": len(all_sessions),
        "avg_session_duration_sec": avg_duration,
        "avg_frames_per_session": avg_frames,
        "most_viewed_frames": most_viewed_frames,
        "most_viewed_rooms": most_viewed_rooms,
        "recent_activity": recent_activity,
        "total_events": len(events),
        "total_leads": len(leads),
        "empty": False
    })

@app.route("/scan/<scan_id>/analytics")
def scan_analytics(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404
    return render_template("analytics.html", scan_id=scan_id)

# ── Lead submission ───────────────────────────────────────────────────────────

@app.route("/scan/<scan_id>/lead", methods=["POST"])
def submit_lead(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return jsonify({"error": "scan not found"}), 404

    body = request.get_json(silent=True) or {}

    if body.get("website", ""):
        return jsonify({"ok": True})

    full_name    = sanitize(body.get("full_name", ""), 200)
    email        = sanitize(body.get("email", ""), 200)
    phone        = sanitize(body.get("phone", ""), 50)
    message      = sanitize(body.get("message", ""), 2000)
    inquiry_type = sanitize(body.get("inquiry_type", "general"), 50)
    source_page  = sanitize(body.get("source_page", "share"), 20)
    session_id   = sanitize(body.get("session_id", ""), 20)
    source_frame = body.get("source_frame_index")

    if not full_name:
        return jsonify({"error": "full_name is required"}), 400
    if not email or not valid_email(email):
        return jsonify({"error": "a valid email is required"}), 400

    lead = {
        "lead_id":            str(uuid.uuid4())[:8],
        "scan_id":            scan_id,
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "full_name":          full_name,
        "email":              email,
        "phone":              phone,
        "message":            message,
        "inquiry_type":       inquiry_type,
        "source_page":        source_page,
        "source_frame_index": source_frame,
        "session_id":         session_id,
        "status":             "new"
    }

    path = leads_path(scan_id)
    try:
        with open(path, "a") as f: f.write(json.dumps(lead) + "\n")
    except Exception as e:
        log.error(f"[{scan_id}] Failed to save lead: {e}")
        return jsonify({"error": "failed to save lead"}), 500

    append_event(scan_id, {
        "event_id": str(uuid.uuid4())[:8],
        "scan_id": scan_id,
        "session_id": session_id,
        "event_type": "lead_submitted",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "frame_index": source_frame,
        "source_page": source_page,
        "meta": {"inquiry_type": inquiry_type}
    })

    log.info(f"[{scan_id}] New lead from {email} ({inquiry_type})")
    return jsonify({"ok": True, "lead_id": lead["lead_id"]})

# ── Leads API ─────────────────────────────────────────────────────────────────

@app.route("/api/scans/<scan_id>/leads", methods=["GET"])
def api_get_leads(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return jsonify({"error": "scan not found"}), 404
    leads = load_leads(scan_id)
    leads.sort(key=lambda l: l.get("created_at", ""), reverse=True)
    return jsonify(leads)

@app.route("/api/leads/<scan_id>/<lead_id>/status", methods=["PATCH"])
def update_lead_status(scan_id, lead_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return jsonify({"error": "scan not found"}), 404
    body   = request.get_json(silent=True) or {}
    status = sanitize(body.get("status", ""), 20)
    if status not in ("new", "contacted", "archived"):
        return jsonify({"error": "status must be new, contacted, or archived"}), 400

    leads = load_leads(scan_id)
    found = False
    for lead in leads:
        if lead.get("lead_id") == lead_id:
            lead["status"] = status
            found = True
            break

    if not found: return jsonify({"error": "lead not found"}), 404
    save_leads(scan_id, leads)
    return jsonify({"ok": True, "status": status})

@app.route("/scan/<scan_id>/leads")
def scan_leads(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404
    return render_template("leads.html", scan_id=scan_id)

# ── Scan library ──────────────────────────────────────────────────────────────

@app.route("/scans")
def scan_library():
    return render_template("library.html")

@app.route("/api/scans")
def api_list_scans():
    if not os.path.exists(UPLOAD_FOLDER): return jsonify([])
    scans = []
    for scan_id in os.listdir(UPLOAD_FOLDER):
        folder = os.path.join(UPLOAD_FOLDER, scan_id)
        if not os.path.isdir(folder): continue
        manifest = load_manifest(scan_id)
        if manifest is None: continue
        summary = scan_summary(scan_id, manifest)
        summary["lead_count"] = len(load_leads(scan_id))
        scans.append(summary)

    def sort_key(s):
        try: return datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
        except: return datetime.min.replace(tzinfo=timezone.utc)

    scans.sort(key=sort_key, reverse=True)
    return jsonify(scans)

# ── Scan management ───────────────────────────────────────────────────────────

@app.route("/api/scans/<scan_id>/rename", methods=["PATCH"])
def rename_scan(scan_id):
    folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(folder): return jsonify({"error": "scan not found"}), 404
    body = request.get_json(silent=True)
    new_name = (body or {}).get("scan_name", "").strip()
    if not new_name: return jsonify({"error": "scan_name is required"}), 400
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    manifest["scan_name"] = new_name
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "scan_name": new_name})

@app.route("/api/scans/<scan_id>/listing", methods=["PATCH"])
def update_listing(scan_id):
    folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(folder): return jsonify({"error": "scan not found"}), 404
    body = request.get_json(silent=True) or {}
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    allowed = [
        "listing_title","listing_subtitle","address","description",
        "property_type","room_count","area_sqm",
        "contact_name","contact_email","contact_phone","branding_name"
    ]
    listing = manifest.get("listing", {})
    for key in allowed:
        if key in body: listing[key] = body[key]
    manifest["listing"] = listing
    if "listing_title" in body and body["listing_title"]:
        manifest["scan_name"] = body["listing_title"]
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "listing": listing})

# ── Rooms ─────────────────────────────────────────────────────────────────────

@app.route("/api/scans/<scan_id>/rooms", methods=["GET"])
def get_rooms(scan_id):
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    return jsonify(manifest.get("rooms", []))

@app.route("/api/scans/<scan_id>/rooms", methods=["PUT"])
def set_rooms(scan_id):
    folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(folder): return jsonify({"error": "scan not found"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "expected a JSON array of rooms"}), 400
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    rooms = []
    for r in body:
        if not isinstance(r, dict): continue
        rooms.append({
            "id":          r.get("id") or str(uuid.uuid4())[:8],
            "name":        str(r.get("name", "Room")).strip(),
            "start_frame": int(r.get("start_frame", 0)),
            "end_frame":   int(r.get("end_frame", 0)),
            "icon":        str(r.get("icon", "🏠"))
        })
    manifest["rooms"] = rooms
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "rooms": rooms})

# ── Annotations ───────────────────────────────────────────────────────────────

@app.route("/api/scans/<scan_id>/annotations", methods=["GET"])
def get_annotations(scan_id):
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    return jsonify(manifest.get("annotations", []))

@app.route("/api/scans/<scan_id>/annotations", methods=["PUT"])
def set_annotations(scan_id):
    folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(folder): return jsonify({"error": "scan not found"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "expected a JSON array of annotations"}), 400
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    annotations = []
    for a in body:
        if not isinstance(a, dict): continue
        annotations.append({
            "id":          a.get("id") or str(uuid.uuid4())[:8],
            "frame_index": int(a.get("frame_index", 0)),
            "title":       str(a.get("title", "")).strip(),
            "body":        str(a.get("body", "")).strip(),
            "type":        str(a.get("type", "info"))
        })
    manifest["annotations"] = annotations
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "annotations": annotations})

# ── Scan edit page ────────────────────────────────────────────────────────────

@app.route("/scan/<scan_id>/edit")
def scan_edit(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404
    return render_template("scan_edit.html", scan_id=scan_id)

# ── Delete ────────────────────────────────────────────────────────────────────

@app.route("/api/scans/<scan_id>", methods=["DELETE"])
def delete_scan(scan_id):
    if not all(c in "abcdef0123456789-" for c in scan_id.lower()):
        return jsonify({"error": "invalid scan_id"}), 400
    folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(folder): return jsonify({"error": "scan not found"}), 404
    shutil.rmtree(folder)
    return jsonify({"ok": True})

# ── Share page ────────────────────────────────────────────────────────────────

@app.route("/share/<scan_id>")
def share_page(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404
    return render_template("share.html", scan_id=scan_id)

@app.route("/listing/<scan_id>/edit")
def listing_edit(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404
    return render_template("listing_edit.html", scan_id=scan_id)

# ── Upload ────────────────────────────────────────────────────────────────────

@app.route("/scan/upload", methods=["POST"])
def upload():
    if "file" not in request.files: return jsonify({"error": "no file field"}), 400
    file = request.files["file"]
    if file.filename == "": return jsonify({"error": "empty filename"}), 400

    scan_id = str(uuid.uuid4())[:8]
    scan_folder = os.path.join(UPLOAD_FOLDER, scan_id)
    os.makedirs(scan_folder, exist_ok=True)

    zip_path = os.path.join(scan_folder, "scan.zip")
    file.save(zip_path)

    try:
        with zipfile.ZipFile(zip_path, "r") as z: z.extractall(scan_folder)
    except zipfile.BadZipFile:
        return jsonify({"error": "invalid zip file"}), 400

    manifest_path = os.path.join(scan_folder, "manifest.json")
    if not os.path.exists(manifest_path):
        return jsonify({"error": "manifest.json not found"}), 400

    try:
        with open(manifest_path) as f: manifest = json.load(f)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"manifest.json malformed: {e}"}), 400

    for field in ["frame_count", "frames", "created_at"]:
        if field not in manifest:
            return jsonify({"error": f"manifest missing field: {field}"}), 400

    declared = manifest["frame_count"]
    listed   = manifest["frames"]
    if len(listed) != declared:
        return jsonify({"error": f"frame_count mismatch: {declared} vs {len(listed)}"}), 400

    missing = [
        (f.get("filename") if isinstance(f, dict) else f)
        for f in listed
        if not os.path.exists(os.path.join(scan_folder,
            f.get("filename") if isinstance(f, dict) else f))
    ]
    if missing: return jsonify({"error": f"missing frames: {missing}"}), 400

    thumbnail = manifest.get("thumbnail_filename")
    if thumbnail and not os.path.exists(os.path.join(scan_folder, thumbnail)):
        manifest["thumbnail_filename"] = None
        save_manifest(scan_id, manifest)

    log.info(f"[{scan_id}] ✅ Validated — {declared} frames")
    return jsonify({
        "scan_id":     scan_id,
        "viewer_url":  f"/view/{scan_id}",
        "share_url":   f"/share/{scan_id}",
        "frame_count": declared
    })

# ── Viewer ────────────────────────────────────────────────────────────────────

@app.route("/view/<scan_id>")
def view(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)): return "Not found", 404
    return render_template("viewer.html", scan_id=scan_id)

# ── Manifest API ──────────────────────────────────────────────────────────────

@app.route("/api/manifest/<scan_id>")
def get_manifest(scan_id):
    manifest = load_manifest(scan_id)
    if manifest is None: return jsonify({"error": "manifest not found"}), 404
    frames = manifest.get("frames", [])
    manifest["frameURLs"] = [
        f"/uploads/{scan_id}/frames/{f['filename'] if isinstance(f, dict) else f}"
        for f in frames
    ]
    thumbnail = manifest.get("thumbnail_filename")
    if thumbnail:
        manifest["thumbnailURL"] = f"/uploads/{scan_id}/frames/{thumbnail}"
    return jsonify(manifest)

# ── Legacy session API ────────────────────────────────────────────────────────

@app.route("/api/session/<scan_id>")
def session_meta(scan_id):
    scan_folder  = os.path.join(UPLOAD_FOLDER, scan_id)
    session_json = os.path.join(scan_folder, "session.json")
    if not os.path.exists(session_json):
        frames = sorted([f for f in os.listdir(scan_folder) if f.endswith(".jpg")])
        if not frames: return jsonify({"error": "session not found"}), 404
        return jsonify({
            "sessionID": scan_id, "frameCount": len(frames), "frames": frames,
            "frameURLs": [f"/uploads/{scan_id}/frames/{f}" for f in frames]
        })
    with open(session_json) as f: meta = json.load(f)
    meta["frameURLs"] = [f"/uploads/{scan_id}/frames/{fr}" for fr in meta.get("frames", [])]
    return jsonify(meta)

# ── Serve files ───────────────────────────────────────────────────────────────

@app.route("/uploads/<scan_id>/frames/<filename>")
def serve_frame(scan_id, filename):
    path = os.path.join(UPLOAD_FOLDER, scan_id, filename)
    if not os.path.exists(path): return "Not found", 404
    return send_file(path, mimetype="image/jpeg")

@app.route("/uploads/<scan_id>/scan.usdz")
def serve_usdz(scan_id):
    path = os.path.join(UPLOAD_FOLDER, scan_id, "scan.usdz")
    if not os.path.exists(path): return "Not found", 404
    return send_file(path)

# ── Export helpers ────────────────────────────────────────────────────────────

def build_readme(scan_id, manifest):
    listing = manifest.get("listing", {})
    title   = listing.get("listing_title") or manifest.get("scan_name") or scan_id
    lines   = [
        "RENDERABLE SCAN EXPORT",
        "=" * 40,
        f"Scan ID:     {scan_id}",
        f"Title:       {title}",
        f"Created:     {manifest.get('created_at', 'Unknown')}",
        f"Frames:      {manifest.get('frame_count', 0)}",
        f"Device:      {manifest.get('device', 'Unknown')}",
        "",
        "CONTENTS",
        "-" * 40,
        "manifest.json       — scan metadata and navigation graph",
        "frames/             — ordered capture images",
        "thumbnail.jpg       — cover image (if available)",
        "session.json        — legacy session metadata",
        "analytics.jsonl     — engagement events (if available)",
        "leads_summary.json  — lead count summary (if available)",
        "README.txt          — this file",
        "",
        "VIEWER URL",
        "-" * 40,
        f"Share page: /share/{scan_id}",
        f"Viewer:     /view/{scan_id}",
        "",
        "Generated by Renderable",
    ]
    return "\n".join(lines)

# ── Tour package ZIP export ───────────────────────────────────────────────────

@app.route("/scan/<scan_id>/export")
def export_package(scan_id):
    scan_folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(scan_folder):
        return "Scan not found", 404

    manifest = load_manifest(scan_id)
    if manifest is None:
        return "Manifest not found", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("README.txt", build_readme(scan_id, manifest))

        frames = manifest.get("frames", [])
        for frame in frames:
            filename = frame.get("filename") if isinstance(frame, dict) else frame
            src = os.path.join(scan_folder, filename)
            if os.path.exists(src):
                zf.write(src, f"frames/{filename}")

        thumb = manifest.get("thumbnail_filename")
        if thumb:
            src = os.path.join(scan_folder, thumb)
            if os.path.exists(src):
                zf.write(src, thumb)

        session_path = os.path.join(scan_folder, "session.json")
        if os.path.exists(session_path):
            zf.write(session_path, "session.json")

        analytics_p = analytics_path(scan_id)
        if os.path.exists(analytics_p):
            zf.write(analytics_p, "analytics.jsonl")

        leads = load_leads(scan_id)
        if leads:
            summary = {
                "total_leads": len(leads),
                "new":         sum(1 for l in leads if l.get("status") == "new"),
                "contacted":   sum(1 for l in leads if l.get("status") == "contacted"),
                "archived":    sum(1 for l in leads if l.get("status") == "archived"),
            }
            zf.writestr("leads_summary.json", json.dumps(summary, indent=2))

    buf.seek(0)
    filename = f"renderable_scan_{scan_id}.zip"
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=filename)

# ── PDF report ────────────────────────────────────────────────────────────────

@app.route("/scan/<scan_id>/export/report")
def export_report(scan_id):
    scan_folder = os.path.join(UPLOAD_FOLDER, scan_id)
    if not os.path.isdir(scan_folder):
        return "Scan not found", 404

    manifest = load_manifest(scan_id)
    if manifest is None:
        return "Manifest not found", 404

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle, Image as RLImage,
                                        HRFlowable)
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        return "reportlab not installed. Run: pip3 install reportlab", 500

    listing  = manifest.get("listing", {})
    rooms    = manifest.get("rooms", [])
    anns     = manifest.get("annotations", [])
    title    = listing.get("listing_title") or manifest.get("scan_name") or scan_id
    address  = listing.get("address", "")
    desc     = listing.get("description", "")
    created  = manifest.get("created_at", "")[:10]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    style_title   = ParagraphStyle("title",   fontSize=22, fontName="Helvetica-Bold",
                                    spaceAfter=4, textColor=colors.HexColor("#0f0f0f"))
    style_sub     = ParagraphStyle("sub",     fontSize=12, fontName="Helvetica",
                                    textColor=colors.HexColor("#666666"), spaceAfter=2)
    style_section = ParagraphStyle("section", fontSize=11, fontName="Helvetica-Bold",
                                    spaceBefore=14, spaceAfter=6,
                                    textColor=colors.HexColor("#1a1a1a"))
    style_body    = ParagraphStyle("body",    fontSize=10, fontName="Helvetica",
                                    leading=15, textColor=colors.HexColor("#333333"))
    style_muted   = ParagraphStyle("muted",   fontSize=9,  fontName="Helvetica",
                                    textColor=colors.HexColor("#888888"))
    style_brand   = ParagraphStyle("brand",   fontSize=9,  fontName="Helvetica-Bold",
                                    textColor=colors.HexColor("#4f8ef7"),
                                    alignment=TA_CENTER)

    story = []

    story.append(Paragraph("RENDERABLE", style_brand))
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor("#4f8ef7")))
    story.append(Spacer(1, 0.4*cm))

    thumb = manifest.get("thumbnail_filename")
    if thumb:
        thumb_path = os.path.join(scan_folder, thumb)
        if os.path.exists(thumb_path):
            try:
                img = RLImage(thumb_path, width=17*cm, height=9*cm)
                img.hAlign = "CENTER"
                story.append(img)
                story.append(Spacer(1, 0.4*cm))
            except Exception:
                pass

    story.append(Paragraph(title, style_title))
    if address:
        story.append(Paragraph(f"📍 {address}", style_sub))
    story.append(Spacer(1, 0.3*cm))

    facts = []
    if listing.get("property_type"): facts.append(("Type",    listing["property_type"]))
    if listing.get("room_count"):    facts.append(("Rooms",   str(listing["room_count"])))
    if listing.get("area_sqm"):      facts.append(("Area",    f"{listing['area_sqm']} m²"))
    facts.append(("Frames",   str(manifest.get("frame_count", 0))))
    facts.append(("Captured", created))

    if facts:
        story.append(Paragraph("Property Details", style_section))
        tdata = [[Paragraph(k, style_muted), Paragraph(v, style_body)]
                 for k, v in facts]
        table = Table(tdata, colWidths=[4*cm, 13*cm])
        table.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0,0), (-1,-1),
             [colors.HexColor("#f8f8f8"), colors.white]),
            ("GRID",    (0,0), (-1,-1), 0.5, colors.HexColor("#e0e0e0")),
            ("PADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(table)

    if desc:
        story.append(Paragraph("Description", style_section))
        story.append(Paragraph(desc, style_body))

    if rooms:
        story.append(Paragraph("Rooms & Areas", style_section))
        for room in rooms:
            icon  = room.get("icon", "🏠")
            name  = room.get("name", "Room")
            start = room.get("start_frame", 0)
            end   = room.get("end_frame", 0)
            story.append(Paragraph(
                f"{icon} <b>{name}</b> — frames {start}–{end}", style_body))

    if anns:
        story.append(Paragraph("Highlights & Notes", style_section))
        type_icons = {"info": "i", "feature": "*", "upgrade": "+", "caution": "!"}
        for ann in anns:
            icon   = type_icons.get(ann.get("type", "info"), "-")
            atitle = ann.get("title", "")
            abody  = ann.get("body", "")
            text   = f"[{icon}] <b>{atitle}</b>"
            if abody: text += f" — {abody}"
            text += f" <font color='#888888'>(frame {ann.get('frame_index', 0)})</font>"
            story.append(Paragraph(text, style_body))

    contact_lines = []
    if listing.get("contact_name"):  contact_lines.append(listing["contact_name"])
    if listing.get("contact_email"): contact_lines.append(listing["contact_email"])
    if listing.get("contact_phone"): contact_lines.append(listing["contact_phone"])
    if contact_lines:
        story.append(Paragraph("Contact", style_section))
        for line in contact_lines:
            story.append(Paragraph(line, style_body))

    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        f"Generated by Renderable · Scan ID: {scan_id}", style_muted))

    doc.build(story)
    buf.seek(0)
    filename = f"renderable_report_{scan_id}.pdf"
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=filename)

# ── Leads CSV export ──────────────────────────────────────────────────────────

@app.route("/scan/<scan_id>/export/leads")
def export_leads(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404

    leads = load_leads(scan_id)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "lead_id", "created_at", "full_name", "email", "phone",
        "message", "inquiry_type", "source_page", "status", "scan_id"
    ], extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        writer.writerow(lead)

    out = io.BytesIO(buf.getvalue().encode("utf-8"))
    filename = f"renderable_leads_{scan_id}.csv"
    return send_file(out, mimetype="text/csv",
                     as_attachment=True, download_name=filename)

# ── Analytics JSON export ─────────────────────────────────────────────────────

@app.route("/scan/<scan_id>/export/analytics")
def export_analytics(scan_id):
    if not os.path.isdir(os.path.join(UPLOAD_FOLDER, scan_id)):
        return "Scan not found", 404

    events = load_events(scan_id)
    leads  = load_leads(scan_id)

    export = {
        "scan_id":      scan_id,
        "exported_at":  datetime.now(timezone.utc).isoformat(),
        "total_events": len(events),
        "total_leads":  len(leads),
        "events":       events
    }

    out = io.BytesIO(json.dumps(export, indent=2).encode("utf-8"))
    filename = f"renderable_analytics_{scan_id}.json"
    return send_file(out, mimetype="application/json",
                     as_attachment=True, download_name=filename)

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(host="0.0.0.0", port=5050, debug=True)
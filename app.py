from flask import Flask, request, jsonify, render_template, send_file
import os, uuid, zipfile, json, shutil, logging, re, io, csv, mimetypes
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

# Absolute path so the app works regardless of working directory.
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

# Reject uploads larger than 500 MB.
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Scan-id validation ────────────────────────────────────────────────────────

_SCAN_ID_RE = re.compile(r'^[0-9a-zA-Z_-]{1,64}$')

def valid_scan_id(scan_id: str) -> bool:
    """Accept alphanumeric IDs, hyphens, and underscores (up to 64 chars)."""
    return bool(_SCAN_ID_RE.match(scan_id)) if scan_id else False

def scan_folder(scan_id: str) -> str:
    return os.path.join(UPLOAD_FOLDER, scan_id)

def scan_exists(scan_id: str) -> bool:
    return os.path.isdir(scan_folder(scan_id))

# ── Frame filename helper ─────────────────────────────────────────────────────

def frame_filename(f) -> str:
    """Return filename string from a frame entry; never raises KeyError."""
    if isinstance(f, dict):
        return (f.get("filename") or "").strip()
    if isinstance(f, str):
        return f.strip()
    return ""

# ── Manifest helpers ──────────────────────────────────────────────────────────

def load_manifest(scan_id: str):
    path = os.path.join(scan_folder(scan_id), "manifest.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning("[%s] Failed to load manifest: %s", scan_id, exc)
        return None

def save_manifest(scan_id: str, manifest: dict) -> None:
    path = os.path.join(scan_folder(scan_id), "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

def scan_summary(scan_id: str, manifest: dict) -> dict:
    thumbnail    = manifest.get("thumbnail_filename") or ""
    listing      = manifest.get("listing") or {}
    frames       = manifest.get("frames") or []
    total_frames = manifest.get("frame_count") or 0
    dict_frames  = [f for f in frames if isinstance(f, dict)]
    active_frames = (
        sum(1 for f in dict_frames if not f.get("discarded", False))
        if dict_frames else total_frames
    )
    return {
        "scan_id":            scan_id,
        "scan_name":          manifest.get("scan_name") or scan_id,
        "created_at":         manifest.get("created_at") or "",
        "frame_count":        total_frames,
        "active_frame_count": active_frames,
        "thumbnail_url":      f"/uploads/{scan_id}/frames/{thumbnail}" if thumbnail else None,
        "viewer_url":         f"/view/{scan_id}",
        "share_url":          f"/share/{scan_id}",
        "listing":            listing,
    }

# ── Analytics helpers ─────────────────────────────────────────────────────────

def analytics_path(scan_id: str) -> str:
    return os.path.join(scan_folder(scan_id), "analytics.jsonl")

def leads_path(scan_id: str) -> str:
    return os.path.join(scan_folder(scan_id), "leads.jsonl")

def append_event(scan_id: str, event: dict) -> None:
    try:
        with open(analytics_path(scan_id), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception as exc:
        log.warning("[%s] Failed to write analytics event: %s", scan_id, exc)

def load_events(scan_id: str) -> list:
    path = analytics_path(scan_id)
    if not os.path.exists(path):
        return []
    events = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return events

def load_leads(scan_id: str) -> list:
    path = leads_path(scan_id)
    if not os.path.exists(path):
        return []
    leads = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    leads.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return leads

def save_leads(scan_id: str, leads: list) -> None:
    try:
        with open(leads_path(scan_id), "w", encoding="utf-8") as fh:
            for lead in leads:
                fh.write(json.dumps(lead) + "\n")
    except Exception as exc:
        log.warning("[%s] Failed to save leads: %s", scan_id, exc)

# ── Input sanitisation ────────────────────────────────────────────────────────

def sanitize(val, max_len: int = 500) -> str:
    if not isinstance(val, str):
        return ""
    return val.strip()[:max_len]

def valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))

def safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

# ── Path traversal guard ──────────────────────────────────────────────────────

def safe_file_path(base_dir: str, filename: str):
    """
    Resolve filename relative to base_dir.
    Returns the absolute path only when it stays inside base_dir;
    returns None otherwise (traversal attempt).
    """
    base_real = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base_real, filename))
    if candidate.startswith(base_real + os.sep) or candidate == base_real:
        return candidate
    return None

# ══════════════════════════════════════════════════════════════════════════════
# UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scan/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "no file field"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "empty filename"}), 400

    scan_id     = str(uuid.uuid4())[:8]
    sfolder     = scan_folder(scan_id)
    os.makedirs(sfolder, exist_ok=True)
    zip_path    = os.path.join(sfolder, "_upload.zip")

    try:
        file.save(zip_path)

        # ── Extract with ZIP-slip protection ──────────────────────────────────
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                safe_root = os.path.realpath(sfolder)
                for member in zf.namelist():
                    dest = os.path.realpath(os.path.join(safe_root, member))
                    if not dest.startswith(safe_root + os.sep):
                        log.warning("[%s] Skipping unsafe ZIP entry: %s", scan_id, member)
                        continue
                    zf.extract(member, sfolder)
        except zipfile.BadZipFile:
            return jsonify({"error": "invalid zip file"}), 400
        finally:
            # Always remove the raw ZIP — frames are now extracted.
            try:
                os.remove(zip_path)
            except OSError:
                pass

        # ── Validate manifest ─────────────────────────────────────────────────
        manifest_path = os.path.join(sfolder, "manifest.json")
        if not os.path.exists(manifest_path):
            return jsonify({"error": "manifest.json not found in ZIP"}), 400

        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            return jsonify({"error": f"manifest.json malformed: {exc}"}), 400

        if not isinstance(manifest, dict):
            return jsonify({"error": "manifest.json must be a JSON object"}), 400

        for required in ("frames",):
            if required not in manifest:
                return jsonify({"error": f"manifest missing required field: {required}"}), 400

        frames = manifest.get("frames") or []
        if not isinstance(frames, list):
            return jsonify({"error": "manifest.frames must be an array"}), 400

        # Validate / warn on missing frame files — do not crash on missing filename.
        missing = []
        for f in frames:
            fname = frame_filename(f)
            if not fname:
                log.warning("[%s] Frame entry has no filename: %s", scan_id, f)
                missing.append("<no filename>")
                continue
            fpath = safe_file_path(sfolder, fname)
            if fpath is None or not os.path.exists(fpath):
                log.warning("[%s] Frame file missing on disk: %s", scan_id, fname)
                missing.append(fname)

        if missing:
            log.warning("[%s] %d frame(s) missing: %s", scan_id, len(missing), missing[:10])

        # Ensure frame_count is set (tolerate its absence).
        if "frame_count" not in manifest:
            manifest["frame_count"] = len(frames)
            save_manifest(scan_id, manifest)

        # Clear thumbnail ref if file is absent.
        thumbnail = manifest.get("thumbnail_filename") or ""
        if thumbnail:
            tpath = safe_file_path(sfolder, thumbnail)
            if tpath is None or not os.path.exists(tpath):
                manifest["thumbnail_filename"] = None
                save_manifest(scan_id, manifest)

        declared = manifest.get("frame_count") or len(frames)
        log.info("[%s] Upload accepted — %d frames (%d missing)", scan_id, declared, len(missing))

        return jsonify({
            "scan_id":     scan_id,
            "frame_count": declared,
            "missing":     missing,
            "status":      "ok",
            # Convenience URLs for the iOS app.
            "viewer_url":  f"/view/{scan_id}",
            "share_url":   f"/share/{scan_id}",
        })

    except Exception as exc:
        # Clean up on any unexpected failure so we don't leave orphan folders.
        log.error("[%s] Upload failed: %s", scan_id, exc, exc_info=True)
        try:
            shutil.rmtree(sfolder, ignore_errors=True)
        except Exception:
            pass
        return jsonify({"error": "upload failed", "detail": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# MANIFEST API  —  /api/scan/<scan_id>/manifest  (primary)
#                  /api/manifest/<scan_id>         (legacy alias, keeps templates working)
# ══════════════════════════════════════════════════════════════════════════════

def _build_manifest_response(scan_id: str):
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404

    frames = manifest.get("frames") or []

    # Build frameURLs using .get() — never direct subscript.
    frame_urls = []
    for f in frames:
        fname = frame_filename(f)
        frame_urls.append(f"/uploads/{scan_id}/frames/{fname}" if fname else "")

    # Attach computed fields without mutating the stored manifest.
    response = dict(manifest)
    response["frameURLs"] = frame_urls

    thumbnail = manifest.get("thumbnail_filename") or ""
    if thumbnail:
        response["thumbnailURL"] = f"/uploads/{scan_id}/frames/{thumbnail}"

    return jsonify(response)


@app.route("/api/scan/<scan_id>/manifest")
def get_manifest_v2(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    return _build_manifest_response(scan_id)


@app.route("/api/manifest/<scan_id>")
def get_manifest_legacy(scan_id):
    """Legacy path — viewer.html and other templates call this."""
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    return _build_manifest_response(scan_id)


# ══════════════════════════════════════════════════════════════════════════════
# FILE SERVING
# ══════════════════════════════════════════════════════════════════════════════

def _serve_scan_file(scan_id: str, filename: str):
    """
    Shared implementation for all file-serving routes.
    Validates scan_id format and guards against path traversal on filename.
    """
    if not valid_scan_id(scan_id):
        return jsonify({"error": "invalid scan_id"}), 400

    sfolder = scan_folder(scan_id)
    if not os.path.isdir(sfolder):
        return jsonify({"error": "scan not found"}), 404

    abs_path = safe_file_path(sfolder, filename)
    if abs_path is None:
        log.warning("Path traversal attempt: scan=%s filename=%s", scan_id, filename)
        return jsonify({"error": "not found"}), 404

    if not os.path.isfile(abs_path):
        return jsonify({"error": "not found"}), 404

    mime, _ = mimetypes.guess_type(filename)
    return send_file(abs_path, mimetype=mime or "image/jpeg")


# Primary frame-serving route (no subdir in URL, matches new spec).
@app.route("/uploads/<scan_id>/<filename>")
def serve_file(scan_id, filename):
    return _serve_scan_file(scan_id, filename)


# Legacy route — viewer.html generates URLs like /uploads/<id>/frames/<file>.
# The "frames/" segment in the URL is decorative; files live directly in the
# scan folder.  We just strip it and serve normally.
@app.route("/uploads/<scan_id>/frames/<filename>")
def serve_frame_legacy(scan_id, filename):
    return _serve_scan_file(scan_id, filename)


@app.route("/uploads/<scan_id>/scan.usdz")
def serve_usdz(scan_id):
    return _serve_scan_file(scan_id, "scan.usdz")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/view/<scan_id>")
@app.route("/scan/<scan_id>/view")
def view(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "not found"}), 404
    return render_template("viewer.html", scan_id=scan_id)


@app.route("/share/<scan_id>")
@app.route("/scan/<scan_id>/share")
def share_page(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "not found"}), 404
    return render_template("share.html", scan_id=scan_id)


@app.route("/scans")
@app.route("/scan/<scan_id>/library")
def scan_library(scan_id=None):
    return render_template("library.html")


@app.route("/scan/<scan_id>/analytics")
def scan_analytics(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "not found"}), 404
    return render_template("analytics.html", scan_id=scan_id)


@app.route("/scan/<scan_id>/leads")
def scan_leads(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "not found"}), 404
    return render_template("leads.html", scan_id=scan_id)


@app.route("/scan/<scan_id>/edit")
def scan_edit(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "not found"}), 404
    return render_template("scan_edit.html", scan_id=scan_id)


@app.route("/listing/<scan_id>/edit")
def listing_edit(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "not found"}), 404
    return render_template("listing_edit.html", scan_id=scan_id)


# ══════════════════════════════════════════════════════════════════════════════
# ANALYTICS INGESTION
# Two routes:
#   POST /analytics/<scan_id>        — new path, iOS app v2
#   POST /analytics/event            — legacy path, existing templates
# ══════════════════════════════════════════════════════════════════════════════

ALLOWED_EVENT_TYPES = {
    "share_view", "viewer_open", "session_start", "session_end",
    "frame_view", "cta_click", "copy_link", "contact_click",
    "lead_form_opened", "lead_submitted",
}


def _ingest_analytics_event(scan_id: str, body: dict):
    event_type = sanitize(body.get("event_type") or "", 50)
    session_id = sanitize(body.get("session_id") or "", 64)

    if not event_type:
        return jsonify({"error": "event_type required"}), 400
    if event_type not in ALLOWED_EVENT_TYPES:
        return jsonify({"error": f"unknown event_type: {event_type}"}), 400
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404

    raw_fi = body.get("frame_index")
    frame_index = None
    if raw_fi is not None:
        fi = safe_int(raw_fi, -1)
        if fi >= 0:
            frame_index = fi

    event = {
        "event_id":    str(uuid.uuid4())[:8],
        "scan_id":     scan_id,
        "session_id":  session_id or str(uuid.uuid4())[:8],
        "event_type":  event_type,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "frame_index": frame_index,
        "room_name":   sanitize(body.get("room_name") or "", 100),
        "source_page": sanitize(body.get("source_page") or "", 30),
        "meta":        body.get("meta") if isinstance(body.get("meta"), dict) else {},
    }
    append_event(scan_id, event)
    return jsonify({"ok": True})


@app.route("/analytics/<scan_id>", methods=["POST"])
def ingest_event_v2(scan_id):
    body = request.get_json(silent=True) or {}
    return _ingest_analytics_event(scan_id, body)


@app.route("/analytics/event", methods=["POST"])
def ingest_event_legacy():
    """Legacy path — existing viewer.html and share.html call this."""
    body    = request.get_json(silent=True) or {}
    scan_id = sanitize(body.get("scan_id") or "", 36)
    if not scan_id:
        return jsonify({"error": "scan_id required"}), 400
    return _ingest_analytics_event(scan_id, body)


# ── Analytics summary ─────────────────────────────────────────────────────────

@app.route("/api/analytics/<scan_id>")
def analytics_summary(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404

    events = load_events(scan_id)
    leads  = load_leads(scan_id)

    if not events:
        return jsonify({
            "scan_id": scan_id, "total_share_views": 0,
            "total_viewer_opens": 0, "unique_sessions": 0,
            "avg_session_duration_sec": 0, "avg_frames_per_session": 0,
            "most_viewed_frames": [], "most_viewed_rooms": [],
            "recent_activity": [], "total_leads": len(leads), "empty": True,
        })

    share_views  = sum(1 for e in events if e.get("event_type") == "share_view")
    viewer_opens = sum(1 for e in events if e.get("event_type") == "viewer_open")
    all_sessions = {e.get("session_id") for e in events if e.get("session_id")}

    session_events: dict = defaultdict(list)
    for e in events:
        sid = e.get("session_id")
        ts  = e.get("timestamp")
        if sid and ts:
            try:
                session_events[sid].append(datetime.fromisoformat(ts))
            except Exception:
                pass

    durations = []
    for timestamps in session_events.values():
        if len(timestamps) >= 2:
            timestamps.sort()
            durations.append((timestamps[-1] - timestamps[0]).total_seconds())
    avg_duration = round(sum(durations) / len(durations)) if durations else 0

    session_frames: dict = defaultdict(set)
    for e in events:
        if e.get("event_type") == "frame_view" and e.get("frame_index") is not None:
            session_frames[e.get("session_id", "")].add(e["frame_index"])
    fps_list = [len(v) for v in session_frames.values()]
    avg_frames = round(sum(fps_list) / len(fps_list), 1) if fps_list else 0

    frame_counts: dict = defaultdict(int)
    for e in events:
        if e.get("event_type") == "frame_view" and e.get("frame_index") is not None:
            frame_counts[str(e["frame_index"])] += 1
    most_viewed_frames = sorted(
        [{"frame_index": safe_int(k), "views": v} for k, v in frame_counts.items()],
        key=lambda x: x["views"], reverse=True,
    )[:10]

    room_counts: dict = defaultdict(int)
    for e in events:
        if e.get("event_type") == "frame_view" and e.get("room_name"):
            room_counts[e["room_name"]] += 1
    most_viewed_rooms = sorted(
        [{"room_name": k, "views": v} for k, v in room_counts.items()],
        key=lambda x: x["views"], reverse=True,
    )[:5]

    now = datetime.now(timezone.utc)
    day_counts: dict = defaultdict(int)
    for e in events:
        ts = e.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt).days <= 6:
                day_counts[dt.strftime("%Y-%m-%d")] += 1
        except Exception:
            pass

    recent_activity = [
        {"date": (now - timedelta(days=i)).strftime("%Y-%m-%d"),
         "events": day_counts.get((now - timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
        for i in range(6, -1, -1)
    ]

    return jsonify({
        "scan_id":                  scan_id,
        "total_share_views":        share_views,
        "total_viewer_opens":       viewer_opens,
        "unique_sessions":          len(all_sessions),
        "avg_session_duration_sec": avg_duration,
        "avg_frames_per_session":   avg_frames,
        "most_viewed_frames":       most_viewed_frames,
        "most_viewed_rooms":        most_viewed_rooms,
        "recent_activity":          recent_activity,
        "total_events":             len(events),
        "total_leads":              len(leads),
        "empty":                    False,
    })


# ══════════════════════════════════════════════════════════════════════════════
# LEAD SUBMISSION
# Two routes:
#   POST /leads/<scan_id>        — new path
#   POST /scan/<scan_id>/lead    — legacy path, share.html calls this
# ══════════════════════════════════════════════════════════════════════════════

def _submit_lead(scan_id: str, body: dict):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404

    # Honeypot — silent accept so bots don't know they were blocked.
    if body.get("website"):
        return jsonify({"ok": True})

    full_name    = sanitize(body.get("full_name")    or "", 200)
    email        = sanitize(body.get("email")        or "", 200)
    phone        = sanitize(body.get("phone")        or "", 50)
    message      = sanitize(body.get("message")      or "", 2000)
    inquiry_type = sanitize(body.get("inquiry_type") or "general", 50)
    source_page  = sanitize(body.get("source_page")  or "share",   20)
    session_id   = sanitize(body.get("session_id")   or "",         64)

    # Coerce source_frame_index to int or None — never store raw type.
    raw_sfi = body.get("source_frame_index")
    source_frame_index = safe_int(raw_sfi) if raw_sfi is not None else None

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
        "source_frame_index": source_frame_index,
        "session_id":         session_id,
        "status":             "new",
    }

    try:
        with open(leads_path(scan_id), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(lead) + "\n")
    except Exception as exc:
        log.error("[%s] Failed to save lead: %s", scan_id, exc)
        return jsonify({"error": "failed to save lead"}), 500

    append_event(scan_id, {
        "event_id":    str(uuid.uuid4())[:8],
        "scan_id":     scan_id,
        "session_id":  session_id,
        "event_type":  "lead_submitted",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "frame_index": source_frame_index,
        "source_page": source_page,
        "meta":        {"inquiry_type": inquiry_type},
    })

    log.info("[%s] New lead from %s (%s)", scan_id, email, inquiry_type)
    return jsonify({"ok": True, "lead_id": lead["lead_id"]})


@app.route("/leads/<scan_id>", methods=["POST"])
def submit_lead_v2(scan_id):
    return _submit_lead(scan_id, request.get_json(silent=True) or {})


@app.route("/scan/<scan_id>/lead", methods=["POST"])
def submit_lead_legacy(scan_id):
    """Legacy path — share.html calls this."""
    return _submit_lead(scan_id, request.get_json(silent=True) or {})


# ── Leads API ─────────────────────────────────────────────────────────────────

@app.route("/api/scans/<scan_id>/leads", methods=["GET"])
def api_get_leads(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    leads = load_leads(scan_id)
    leads.sort(key=lambda l: l.get("created_at") or "", reverse=True)
    return jsonify(leads)


@app.route("/api/leads/<scan_id>/<lead_id>/status", methods=["PATCH"])
def update_lead_status(scan_id, lead_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    body   = request.get_json(silent=True) or {}
    status = sanitize(body.get("status") or "", 20)
    if status not in ("new", "contacted", "archived"):
        return jsonify({"error": "status must be new, contacted, or archived"}), 400

    leads = load_leads(scan_id)
    found = False
    for lead in leads:
        if lead.get("lead_id") == lead_id:
            lead["status"] = status
            found = True
            break

    if not found:
        return jsonify({"error": "lead not found"}), 404
    save_leads(scan_id, leads)
    return jsonify({"ok": True, "status": status})


# ══════════════════════════════════════════════════════════════════════════════
# SCAN LIBRARY API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/scans")
def api_list_scans():
    if not os.path.exists(UPLOAD_FOLDER):
        return jsonify([])
    scans = []
    for sid in os.listdir(UPLOAD_FOLDER):
        folder = os.path.join(UPLOAD_FOLDER, sid)
        if not os.path.isdir(folder):
            continue
        manifest = load_manifest(sid)
        if manifest is None:
            continue
        summary = scan_summary(sid, manifest)
        summary["lead_count"] = len(load_leads(sid))
        scans.append(summary)

    def sort_key(s):
        try:
            return datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    scans.sort(key=sort_key, reverse=True)
    return jsonify(scans)


# ══════════════════════════════════════════════════════════════════════════════
# SCAN MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/scans/<scan_id>/rename", methods=["PATCH"])
def rename_scan(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    body     = request.get_json(silent=True) or {}
    new_name = sanitize(body.get("scan_name") or "", 200)
    if not new_name:
        return jsonify({"error": "scan_name is required"}), 400
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404
    manifest["scan_name"] = new_name
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "scan_name": new_name})


@app.route("/api/scans/<scan_id>/listing", methods=["PATCH"])
def update_listing(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    body     = request.get_json(silent=True) or {}
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404

    allowed_keys = [
        "listing_title", "listing_subtitle", "address", "description",
        "property_type", "room_count", "area_sqm",
        "contact_name", "contact_email", "contact_phone", "branding_name",
    ]
    listing = manifest.get("listing") or {}
    for key in allowed_keys:
        if key in body:
            listing[key] = body[key]
    manifest["listing"] = listing
    if body.get("listing_title"):
        manifest["scan_name"] = body["listing_title"]
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "listing": listing})


@app.route("/api/scans/<scan_id>", methods=["DELETE"])
def delete_scan(scan_id):
    # Strict validation: only allow 8-char lowercase hex IDs.
    if not re.fullmatch(r"[0-9a-f]{8}", scan_id.lower()):
        return jsonify({"error": "invalid scan_id"}), 400
    sfolder = scan_folder(scan_id)
    # Confirm the resolved path stays under UPLOAD_FOLDER.
    safe_root = os.path.realpath(UPLOAD_FOLDER)
    if not os.path.realpath(sfolder).startswith(safe_root + os.sep):
        return jsonify({"error": "invalid scan_id"}), 400
    if not os.path.isdir(sfolder):
        return jsonify({"error": "scan not found"}), 404
    shutil.rmtree(sfolder)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ROOMS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/scans/<scan_id>/rooms", methods=["GET"])
def get_rooms(scan_id):
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404
    return jsonify(manifest.get("rooms") or [])


@app.route("/api/scans/<scan_id>/rooms", methods=["PUT"])
def set_rooms(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "expected a JSON array of rooms"}), 400
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404

    rooms = []
    for r in body:
        if not isinstance(r, dict):
            continue
        rooms.append({
            "id":          r.get("id") or str(uuid.uuid4())[:8],
            "name":        sanitize(r.get("name") or "Room", 100),
            "start_frame": safe_int(r.get("start_frame"), 0),
            "end_frame":   safe_int(r.get("end_frame"),   0),
            "icon":        sanitize(r.get("icon") or "🏠", 10),
        })
    manifest["rooms"] = rooms
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "rooms": rooms})


# ══════════════════════════════════════════════════════════════════════════════
# ANNOTATIONS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/scans/<scan_id>/annotations", methods=["GET"])
def get_annotations(scan_id):
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404
    return jsonify(manifest.get("annotations") or [])


@app.route("/api/scans/<scan_id>/annotations", methods=["PUT"])
def set_annotations(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "expected a JSON array of annotations"}), 400
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404

    annotations = []
    for a in body:
        if not isinstance(a, dict):
            continue
        ann_type = sanitize(a.get("type") or "info", 20)
        if ann_type not in ("info", "feature", "upgrade", "caution"):
            ann_type = "info"
        annotations.append({
            "id":          a.get("id") or str(uuid.uuid4())[:8],
            "frame_index": safe_int(a.get("frame_index"), 0),
            "title":       sanitize(a.get("title") or "", 200),
            "body":        sanitize(a.get("body")  or "", 1000),
            "type":        ann_type,
        })
    manifest["annotations"] = annotations
    save_manifest(scan_id, manifest)
    return jsonify({"ok": True, "annotations": annotations})


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def _build_readme(scan_id: str, manifest: dict) -> str:
    listing = manifest.get("listing") or {}
    title   = listing.get("listing_title") or manifest.get("scan_name") or scan_id
    return "\n".join([
        "RENDERABLE SCAN EXPORT",
        "=" * 40,
        f"Scan ID:     {scan_id}",
        f"Title:       {title}",
        f"Created:     {manifest.get('created_at') or 'Unknown'}",
        f"Frames:      {manifest.get('frame_count') or 0}",
        f"Device:      {manifest.get('device') or 'Unknown'}",
        "",
        "CONTENTS",
        "-" * 40,
        "manifest.json       — scan metadata",
        "frames/             — ordered capture images",
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
    ])


@app.route("/scan/<scan_id>/export")
def export_package(scan_id):
    sfolder = scan_folder(scan_id)
    if not os.path.isdir(sfolder):
        return jsonify({"error": "scan not found"}), 404
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("README.txt", _build_readme(scan_id, manifest))

        for f in (manifest.get("frames") or []):
            fname = frame_filename(f)
            if not fname:
                continue
            abs_src = safe_file_path(sfolder, fname)
            if abs_src and os.path.isfile(abs_src):
                zf.write(abs_src, f"frames/{fname}")

        thumb = manifest.get("thumbnail_filename") or ""
        if thumb:
            abs_thumb = safe_file_path(sfolder, thumb)
            if abs_thumb and os.path.isfile(abs_thumb):
                zf.write(abs_thumb, thumb)

        for extra in ("session.json",):
            p = os.path.join(sfolder, extra)
            if os.path.exists(p):
                zf.write(p, extra)

        ap = analytics_path(scan_id)
        if os.path.exists(ap):
            zf.write(ap, "analytics.jsonl")

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
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"renderable_scan_{scan_id}.zip")


@app.route("/scan/<scan_id>/export/report")
def export_report(scan_id):
    sfolder = scan_folder(scan_id)
    if not os.path.isdir(sfolder):
        return jsonify({"error": "scan not found"}), 404
    manifest = load_manifest(scan_id)
    if manifest is None:
        return jsonify({"error": "manifest not found"}), 404

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle, Image as RLImage,
                                        HRFlowable)
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        return "reportlab not installed. Run: pip3 install reportlab", 500

    listing = manifest.get("listing") or {}
    rooms   = manifest.get("rooms")   or []
    anns    = manifest.get("annotations") or []
    title   = listing.get("listing_title") or manifest.get("scan_name") or scan_id
    address = listing.get("address") or ""
    desc    = listing.get("description") or ""
    created = (manifest.get("created_at") or "")[:10]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    s_title   = ParagraphStyle("t",  fontSize=22, fontName="Helvetica-Bold",
                                spaceAfter=4,  textColor=colors.HexColor("#0f0f0f"))
    s_sub     = ParagraphStyle("s",  fontSize=12, fontName="Helvetica",
                                textColor=colors.HexColor("#666666"), spaceAfter=2)
    s_section = ParagraphStyle("sc", fontSize=11, fontName="Helvetica-Bold",
                                spaceBefore=14, spaceAfter=6,
                                textColor=colors.HexColor("#1a1a1a"))
    s_body    = ParagraphStyle("b",  fontSize=10, fontName="Helvetica",
                                leading=15, textColor=colors.HexColor("#333333"))
    s_muted   = ParagraphStyle("m",  fontSize=9,  fontName="Helvetica",
                                textColor=colors.HexColor("#888888"))
    s_brand   = ParagraphStyle("br", fontSize=9,  fontName="Helvetica-Bold",
                                textColor=colors.HexColor("#4f8ef7"),
                                alignment=TA_CENTER)

    story = [
        Paragraph("RENDERABLE", s_brand),
        Spacer(1, 0.3*cm),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#4f8ef7")),
        Spacer(1, 0.4*cm),
    ]

    thumb = manifest.get("thumbnail_filename") or ""
    if thumb:
        abs_thumb = safe_file_path(sfolder, thumb)
        if abs_thumb and os.path.isfile(abs_thumb):
            try:
                img = RLImage(abs_thumb, width=17*cm, height=9*cm)
                img.hAlign = "CENTER"
                story += [img, Spacer(1, 0.4*cm)]
            except Exception:
                pass

    story.append(Paragraph(title, s_title))
    if address:
        story.append(Paragraph(f"📍 {address}", s_sub))
    story.append(Spacer(1, 0.3*cm))

    facts = []
    if listing.get("property_type"): facts.append(("Type",    listing["property_type"]))
    if listing.get("room_count"):    facts.append(("Rooms",   str(listing["room_count"])))
    if listing.get("area_sqm"):      facts.append(("Area",    f"{listing['area_sqm']} m²"))
    facts.append(("Frames",   str(manifest.get("frame_count") or 0)))
    facts.append(("Captured", created))

    if facts:
        story.append(Paragraph("Property Details", s_section))
        tdata = [[Paragraph(k, s_muted), Paragraph(v, s_body)] for k, v in facts]
        table = Table(tdata, colWidths=[4*cm, 13*cm])
        table.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0, 0), (-1, -1),
             [colors.HexColor("#f8f8f8"), colors.white]),
            ("GRID",    (0, 0), (-1, -1), 0.5, colors.HexColor("#e0e0e0")),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(table)

    if desc:
        story += [Paragraph("Description", s_section), Paragraph(desc, s_body)]

    if rooms:
        story.append(Paragraph("Rooms & Areas", s_section))
        for room in rooms:
            story.append(Paragraph(
                f"{room.get('icon') or '🏠'} <b>{room.get('name') or 'Room'}</b> "
                f"— frames {room.get('start_frame') or 0}–{room.get('end_frame') or 0}",
                s_body,
            ))

    if anns:
        story.append(Paragraph("Highlights & Notes", s_section))
        icons = {"info": "i", "feature": "*", "upgrade": "+", "caution": "!"}
        for ann in anns:
            icon  = icons.get(ann.get("type") or "info", "-")
            atitle = ann.get("title") or ""
            abody  = ann.get("body")  or ""
            text   = f"[{icon}] <b>{atitle}</b>"
            if abody: text += f" — {abody}"
            text += f" <font color='#888888'>(frame {ann.get('frame_index') or 0})</font>"
            story.append(Paragraph(text, s_body))

    contact_lines = [
        v for v in (listing.get(k) for k in
                    ("contact_name", "contact_email", "contact_phone"))
        if v
    ]
    if contact_lines:
        story.append(Paragraph("Contact", s_section))
        story += [Paragraph(line, s_body) for line in contact_lines]

    story += [
        Spacer(1, 0.5*cm),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")),
        Spacer(1, 0.2*cm),
        Paragraph(f"Generated by Renderable · Scan ID: {scan_id}", s_muted),
    ]

    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"renderable_report_{scan_id}.pdf")


@app.route("/scan/<scan_id>/export/leads")
def export_leads(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    leads = load_leads(scan_id)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "lead_id", "created_at", "full_name", "email", "phone",
        "message", "inquiry_type", "source_page", "status", "scan_id",
    ], extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        writer.writerow(lead)
    out = io.BytesIO(buf.getvalue().encode("utf-8"))
    return send_file(out, mimetype="text/csv", as_attachment=True,
                     download_name=f"renderable_leads_{scan_id}.csv")


@app.route("/scan/<scan_id>/export/analytics")
def export_analytics(scan_id):
    if not scan_exists(scan_id):
        return jsonify({"error": "scan not found"}), 404
    events = load_events(scan_id)
    leads  = load_leads(scan_id)
    export = {
        "scan_id":      scan_id,
        "exported_at":  datetime.now(timezone.utc).isoformat(),
        "total_events": len(events),
        "total_leads":  len(leads),
        "events":       events,
    }
    out = io.BytesIO(json.dumps(export, indent=2).encode("utf-8"))
    return send_file(out, mimetype="application/json", as_attachment=True,
                     download_name=f"renderable_analytics_{scan_id}.json")


# ══════════════════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(host="0.0.0.0", port=5050, debug=False)

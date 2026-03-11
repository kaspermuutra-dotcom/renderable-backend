from flask import Flask, request, jsonify, render_template
import os, uuid

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"

@app.route("/scan/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400
    scan_id = str(uuid.uuid4())[:8]
    os.makedirs(f"{UPLOAD_FOLDER}/{scan_id}", exist_ok=True)
    file.save(f"{UPLOAD_FOLDER}/{scan_id}/scan.usdz")
    return jsonify({"scan_id": scan_id})

@app.route("/view/<scan_id>")
def view(scan_id):
    return render_template("viewer.html", scan_id=scan_id)

@app.route("/uploads/<scan_id>/scan.usdz")
def serve_usdz(scan_id):
    from flask import send_file
    return send_file(f"uploads/{scan_id}/scan.usdz")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
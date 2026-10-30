#!/usr/bin/env python3
"""
Local web dashboard for FCC Robot Framework regression runs.

Usage:
    python web_dashboard.py
    Then open http://localhost:5000 in a browser.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import shutil
import sys
import time
import threading
import uuid
import zipfile
import io
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

ROOT_DIR = Path(__file__).parent.resolve()
REPO_DIR = ROOT_DIR / "gmtools_rtostestautomation"
if not REPO_DIR.exists():
  REPO_DIR = ROOT_DIR
RESULTS_DIR = ROOT_DIR / "results"
CONFIG_PY = (
  REPO_DIR
    / "RTOSAutomation"
    / "src"
    / "rtos"
    / "rtos_configuration.py"
)
VEHICLE_SUITES_JSON = ROOT_DIR / "vehicle_suites.json"
SUITE_DIR = (
  REPO_DIR
    / "RTOSAutomation"
    / "src"
    / "rtos"
    / "robot_scripts"
    / "Vcumate"
)


def _set_repo_dir(repo_path: Path) -> None:
    """Point dashboard discovery at a local automation repository folder."""
    global REPO_DIR, CONFIG_PY, SUITE_DIR
    REPO_DIR = repo_path.resolve()
    CONFIG_PY = REPO_DIR / "RTOSAutomation" / "src" / "rtos" / "rtos_configuration.py"
    SUITE_DIR = REPO_DIR / "RTOSAutomation" / "src" / "rtos" / "robot_scripts" / "Vcumate"


def _resolve_repo_path(raw_path: str) -> Path | None:
    path = Path(raw_path.strip().strip('"')).expanduser()
    if not path.is_dir():
        return None
    if path.name.lower() == "rtosautomation" and (path / "src").is_dir():
        path = path.parent
    if not (path / "RTOSAutomation" / "src" / "rtos").is_dir():
        return None
    return path.resolve()

app = Flask(__name__)

# ── Run state ──────────────────────────────────────────────────────────────────
_run_lock = threading.Lock()
_active_run: dict | None = None


def _read_vehicle_program() -> str:
    if not CONFIG_PY.exists():
        return "UNKNOWN"
    for line in CONFIG_PY.read_text(encoding="utf-8").splitlines():
        m = re.match(r"""^\s*vehicle_program\s*=\s*['"](.+)['"]\s*""", line)
        if m:
            return m.group(1).strip()
    return "UNKNOWN"


def _write_vehicle_program(value: str) -> None:
    text = CONFIG_PY.read_text(encoding="utf-8")
    new_text = re.sub(
        r"""(^\s*vehicle_program\s*=\s*)['"][^'"]+['"]""",
        rf"\g<1>'{value}'",
        text,
        flags=re.MULTILINE,
    )
    CONFIG_PY.write_text(new_text, encoding="utf-8")


def _read_display_size() -> str:
    if not CONFIG_PY.exists():
        return ""
    for line in CONFIG_PY.read_text(encoding="utf-8").splitlines():
        m = re.match(r"""^\s*display_size\s*=\s*['"](.+)['"]\s*""", line)
        if m:
            return m.group(1).strip()
    return ""


def _write_display_size(value: str) -> None:
    text = CONFIG_PY.read_text(encoding="utf-8")
    new_text = re.sub(
        r"""(^\s*display_size\s*=\s*)['"][^'"]+['"]""",
        rf"\g<1>'{value}'",
        text,
        flags=re.MULTILINE,
    )
    CONFIG_PY.write_text(new_text, encoding="utf-8")


def _load_suite_mapping() -> dict:
    if not VEHICLE_SUITES_JSON.exists():
        return {}
    return json.loads(VEHICLE_SUITES_JSON.read_text(encoding="utf-8"))


def _get_allowed_variants(mapping: dict, display_size: str) -> list[str]:
    return mapping.get("_display_variants", {}).get(display_size, [])


def _get_suite_list(vehicle_program: str, display_size: str = "") -> list[str]:
    if not VEHICLE_SUITES_JSON.exists():
      return []
    mapping = _load_suite_mapping()
    if display_size and vehicle_program not in _get_allowed_variants(mapping, display_size):
      return []
    suites = list(mapping.get(vehicle_program, mapping.get("FCC_ALL", [])))
    if vehicle_program.endswith("_FCC"):
      suites.extend(mapping.get("_FCC_SANITY", []))
    elif vehicle_program.endswith("_RCC"):
      suites.extend(mapping.get("_RCC_SANITY", []))
      suites.extend(mapping.get("RCC_ALL", []))
    return list(dict.fromkeys(s for s in suites if not s.startswith("_")))


def _resolve_single_suite(value: str) -> str | None:
    """Resolve a dashboard suite name to an existing Robot file."""
    candidate = Path(value)
    if candidate.is_file() and candidate.suffix.lower() == ".robot":
        return str(candidate.resolve())

    matches = list(SUITE_DIR.rglob(value)) if SUITE_DIR.exists() else []
    robot_matches = [path for path in matches if path.is_file() and path.suffix.lower() == ".robot"]
    if len(robot_matches) == 1:
        return str(robot_matches[0].resolve())
    return None


def _stream_run(run_id: str, cmd: list[str], q: queue.Queue) -> None:
    global _active_run
    status_stop = None
    status_thread = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT_DIR),
        )
        with _run_lock:
            if _active_run and _active_run["id"] == run_id:
                _active_run["proc"] = proc
                _active_run["started_at"] = time.monotonic()

        started_at = _active_run["started_at"]
        status_stop = threading.Event()

        def emit_status() -> None:
            while not status_stop.wait(2):
                if proc.poll() is not None:
                    return
                elapsed = int(time.monotonic() - started_at)
                q.put(f"__DEBUG__STATUS: RUNNING | PID={proc.pid} | elapsed={elapsed}s")

        q.put(f"__DEBUG__STATUS: STARTED | PID={proc.pid} | elapsed=0s")
        status_thread = threading.Thread(target=emit_status, daemon=True)
        status_thread.start()
        for line in proc.stdout:
          line = line.rstrip("\r\n")
          if line.startswith("__RF_TEST__"):
            q.put("__TEST__" + line[len("__RF_TEST__"):])
          elif line.startswith("__RF_DEBUG__"):
            q.put("__DEBUG__" + line[len("__RF_DEBUG__"):])
          else:
            q.put(line)
        proc.wait()
        status_stop.set()
        if status_thread:
            status_thread.join(timeout=1)
        elapsed = int(time.monotonic() - started_at)
        q.put(f"__DEBUG__STATUS: EXITED | code={proc.returncode} | elapsed={elapsed}s")
        q.put(f"__DEBUG__Process exited with code {proc.returncode}")
        q.put(f"__EXIT__{proc.returncode}")
    except Exception as exc:
        q.put("__DEBUG__Runner exception: " + str(exc))
        q.put(f"[ERROR] {exc}")
        q.put("__EXIT__1")
    finally:
        if status_stop:
            status_stop.set()
        with _run_lock:
            if _active_run and _active_run["id"] == run_id:
                _active_run["done"] = True


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return _HTML_PAGE, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
    }


@app.route("/api/config")
def api_config():
    vp = _read_vehicle_program()
    ds = _read_display_size()
    mapping = _load_suite_mapping()
    allowed = _get_allowed_variants(mapping, ds)
    if allowed and vp not in allowed:
        vp = allowed[0]
        _write_vehicle_program(vp)
    suites = _get_suite_list(vp, ds)
    vehicles = allowed or [k for k in mapping if not k.startswith("_") and k not in {"FCC_ALL", "RCC_ALL"}]
    resp = jsonify({
      "vehicle_program": vp,
      "display_size": ds,
      "suites": suites,
      "vehicles": vehicles,
      "repo_path": str(REPO_DIR),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/config", methods=["POST"])
def api_config_update():
    data = request.get_json(force=True) or {}
    vp = data.get("vehicle_program", "").strip()
    ds = data.get("display_size", "").strip()
    if not vp and not ds:
        return jsonify({"error": "vehicle_program or display_size is required"}), 400
    if not CONFIG_PY.exists():
        return jsonify({"error": "rtos_configuration.py not found"}), 404
    mapping = _load_suite_mapping()
    current_ds = ds or _read_display_size()
    if vp and current_ds and vp not in _get_allowed_variants(mapping, current_ds):
        return jsonify({"error": f"{vp} is not applicable for display size {current_ds}"}), 400
    current_vp = vp or _read_vehicle_program()
    original_vp = current_vp
    allowed = _get_allowed_variants(mapping, current_ds)
    if ds and not vp and current_vp not in allowed:
      if not allowed:
        return jsonify({"error": f"No vehicle variants configured for display size {ds}"}), 400
      current_vp = allowed[0]
    elif ds and vp and current_vp not in allowed:
      return jsonify({"error": f"{current_vp} is not applicable for display size {ds}"}), 400
    if vp or current_vp != original_vp:
      _write_vehicle_program(current_vp)
    if ds:
        _write_display_size(ds)
    suites = _get_suite_list(current_vp, current_ds)
    vehicles = allowed or [k for k in mapping if not k.startswith("_") and k not in {"FCC_ALL", "RCC_ALL"}]
    return jsonify({"ok": True, "vehicle_program": current_vp, "display_size": ds or _read_display_size(), "suites": suites, "vehicles": vehicles})


import py7zr
import rarfile


SUPPORTED_ARCHIVE_EXTS = (".zip", ".7z", ".rar")


def _configure_rar_extractor() -> str | None:
    """Find a RAR extractor on Windows, even when it is not on PATH."""
    candidates = []

    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "")

        candidates.extend([
            Path(program_files) / "WinRAR" / "UnRAR.exe",
            Path(program_files) / "WinRAR" / "WinRAR.exe",
            Path(program_files_x86) / "WinRAR" / "UnRAR.exe",
            Path(program_files_x86) / "WinRAR" / "WinRAR.exe",
        ])

        if local_app_data:
            candidates.extend([
                Path(local_app_data) / "Programs" / "WinRAR" / "UnRAR.exe",
                Path(local_app_data) / "Programs" / "WinRAR" / "WinRAR.exe",
            ])

    # First prefer an extractor already available through PATH.
    for tool in ("unrar", "UnRAR.exe", "winrar", "WinRAR.exe"):
        found = shutil.which(tool)
        if found:
            rarfile.UNRAR_TOOL = found
            return found

    # Then check common WinRAR installation locations.
    for candidate in candidates:
        if candidate.is_file():
            rarfile.UNRAR_TOOL = str(candidate)
            return str(candidate)

    return None


def _safe_dest(root: Path, arc_path: str) -> Path | None:
    """Resolve destination and guard against zip-slip."""
    dest = (root / arc_path).resolve()
    return dest if str(dest).startswith(str(root)) else None


def _strip_single_root(names: list[str]) -> str | None:
    roots = {n.replace("\\", "/").split("/")[0] for n in names if n.strip("/\\")}
    return next(iter(roots)) if len(roots) == 1 else None


def _extract_arc_path(raw: str, single_root: str | None) -> str:
    path = raw.replace("\\", "/")
    if single_root and path.startswith(single_root + "/"):
        path = path[len(single_root) + 1:]
    return path


def _extract_zip(data: bytes, dest_root: Path) -> int:
    zf = zipfile.ZipFile(io.BytesIO(data))
    single_root = _strip_single_root(zf.namelist())
    extracted = 0
    for member in zf.infolist():
        arc_path = _extract_arc_path(member.filename, single_root)
        if not arc_path:
            continue
        dest = _safe_dest(dest_root, arc_path)
        if dest is None:
            continue
        if member.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member.filename))
            extracted += 1
    return extracted


def _extract_7z(data: bytes, dest_root: Path) -> int:
    buf = io.BytesIO(data)
    with py7zr.SevenZipFile(buf, mode="r") as zf:
        names = zf.getnames()
        single_root = _strip_single_root(names)
        extracted = 0
        for fname, bio in zf.read(names).items():
            arc_path = _extract_arc_path(fname, single_root)
            if not arc_path or arc_path.endswith("/"):
                continue
            dest = _safe_dest(dest_root, arc_path)
            if dest is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bio.read())
            extracted += 1
    return extracted


def _extract_rar(data: bytes, dest_root: Path) -> int:
    # rarfile needs an external RAR extractor. Configure it automatically
    # so WinRAR works even when its folder is not present in PATH.
    extractor = _configure_rar_extractor()
    if not extractor:
        raise rarfile.RarCannotExec(
            "No RAR extractor found. Install WinRAR/UnRAR or add it to PATH."
        )

    buf = io.BytesIO(data)
    try:
        rf = rarfile.RarFile(buf)
    except rarfile.NeedFirstVolume:
        raise ValueError("Multi-volume RAR archives are not supported")
    names = rf.namelist()
    single_root = _strip_single_root(names)
    extracted = 0
    for member in rf.infolist():
        arc_path = _extract_arc_path(member.filename, single_root)
        if not arc_path or member.is_dir():
            continue
        dest = _safe_dest(dest_root, arc_path)
        if dest is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(rf.read(member.filename))
        extracted += 1
    return extracted


@app.route("/api/upload/repo", methods=["POST"])
def api_upload_repo():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in SUPPORTED_ARCHIVE_EXTS:
        return jsonify({"error": f"Unsupported format '{ext}'. Use .zip, .7z or .rar"}), 400
    try:
        data = f.read()
        if ext == ".zip":
            extracted = _extract_zip(data, ROOT_DIR)
        elif ext == ".7z":
            extracted = _extract_7z(data, ROOT_DIR)
        else:  # .rar
            extracted = _extract_rar(data, ROOT_DIR)
        return jsonify({"ok": True, "message": f"Extracted {extracted} files to workspace"})
    except rarfile.RarCannotExec:
        return jsonify({"error": "RAR extraction requires unrar/WinRAR installed and on PATH"}), 500
    except (zipfile.BadZipFile, py7zr.exceptions.Bad7zFile) as exc:
        return jsonify({"error": f"Invalid or corrupted archive: {exc}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/repo-path", methods=["POST"])
def api_repo_path():
    data = request.get_json(force=True, silent=True) or {}
    repo_path = _resolve_repo_path(data.get("path", ""))
    if not repo_path:
        return jsonify({
            "error": "Invalid automation folder. Select the folder containing RTOSAutomation\\src\\rtos."
        }), 400
    _set_repo_dir(repo_path)
    return jsonify({"ok": True, "repo_path": str(REPO_DIR), "message": "Local automation folder connected"})


@app.route("/api/delete/repo", methods=["POST"])
def api_delete_repo():
    """Delete uploaded automation-repo content without touching dashboard files."""
    try:
        with _run_lock:
            if _active_run and not _active_run.get("done"):
                return jsonify({"error": "Stop the active Robot run before deleting the repository"}), 409

        protected_names = {
            "web_dashboard.py",
            "vehicle_suites.json",
            "run_robot_protected.py",
            "run_regressions.bat",
            "disable_testscript_signals.py",
            "disable_testscript_signals.bat",
            "results",
            "continue_on_failure_modifier.py",
            "generate_testcase_wise_report.py",
            "fix_sanity_screenshot_names.ps1",
            "get_suites_for_vehicle.py",
            "listener.py",
            "run_tests.ps1",
        }
        deleted = []
        for path in ROOT_DIR.iterdir():
            if path.name in protected_names:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(path.name)

        if not deleted:
            return jsonify({"ok": True, "message": "Workspace already contains only protected dashboard files"})
        return jsonify({"ok": True, "message": f"Deleted {len(deleted)} non-dashboard item(s): {', '.join(deleted)}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Repository deletion failed: {exc}"}), 500


@app.route("/api/upload/vehicle-suites", methods=["POST"])
def api_upload_vehicle_suites():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        content = f.read().decode("utf-8")
        json.loads(content)  # validate JSON before saving
    except Exception as exc:
        return jsonify({"error": f"Invalid JSON: {exc}"}), 400
    VEHICLE_SUITES_JSON.write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "message": "vehicle_suites.json updated"})


@app.route("/api/run", methods=["POST"])
def api_run():
    global _active_run
    with _run_lock:
        if _active_run and not _active_run.get("done"):
            return jsonify({"error": "A run is already in progress"}), 409

    data = request.get_json(force=True, silent=True) or {}
    single_suite = data.get("suite", "").strip()
    selected_suites = data.get("suites")

    if single_suite:
      resolved_suite = _resolve_single_suite(single_suite)
      if not resolved_suite:
        return jsonify({"error": f"Robot suite not found or not unique: {single_suite}"}), 400
      single_suite = resolved_suite

    if selected_suites is not None:
      if not isinstance(selected_suites, list) or not selected_suites:
        return jsonify({"error": "Select at least one robot suite"}), 400
      resolved_selected = []
      for suite in selected_suites:
        if not isinstance(suite, str):
          return jsonify({"error": "Invalid suite selection"}), 400
        resolved_suite = _resolve_single_suite(suite.strip())
        if not resolved_suite:
          return jsonify({"error": f"Robot suite not found or not unique: {suite}"}), 400
        if resolved_suite not in resolved_selected:
          resolved_selected.append(resolved_suite)
      selected_suites = resolved_selected

    run_id = str(uuid.uuid4())[:8]
    q: queue.Queue = queue.Queue()
    RESULTS_DIR.mkdir(exist_ok=True)

    if single_suite:
        cmd = [
            sys.executable,
            str(ROOT_DIR / "run_robot_protected.py"),
            "--root-dir", str(ROOT_DIR),
            "--output-dir", str(RESULTS_DIR),
            "--single-robot", single_suite,
            "--report-script", str(ROOT_DIR / "generate_testcase_wise_report.py"),
            "--html-only",
            "--robot-args",
            "--listener", "listener.ContinueOnFailureListener",
            "--prerunmodifier", "continue_on_failure_modifier.ContinueOnFailureModifier",
            "--outputdir", str(RESULTS_DIR),
            "--output", "output.xml",
            "--log", "log.html",
            "--report", "report.html",
            single_suite,
        ]
    else:
        # Resolve suite list for current vehicle program
        try:
            res = subprocess.run(
                [sys.executable, str(ROOT_DIR / "get_suites_for_vehicle.py"), str(SUITE_DIR)],
                capture_output=True, text=True, cwd=str(ROOT_DIR),
            )
            suite_paths = (
              selected_suites
              if selected_suites is not None
              else [p.strip() for p in res.stdout.splitlines() if p.strip()]
            )
            if not suite_paths:
                err = res.stderr.strip() or "No suites found for vehicle program"
                return jsonify({"error": err}), 400

            suite_list_txt = ROOT_DIR / "temp_suite_list_for_combined.txt"
            suite_list_txt.write_text("\n".join(suite_paths), encoding="utf-8")
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        cmd = [
            sys.executable,
            str(ROOT_DIR / "run_robot_protected.py"),
            "--root-dir", str(ROOT_DIR),
            "--output-dir", str(RESULTS_DIR),
            "--suite-list-txt", str(suite_list_txt),
            "--report-script", str(ROOT_DIR / "generate_testcase_wise_report.py"),
            "--html-only",
            "--robot-args",
            "--listener", "listener.ContinueOnFailureListener",
            "--prerunmodifier", "continue_on_failure_modifier.ContinueOnFailureModifier",
            "--outputdir", str(RESULTS_DIR),
            "--output", "output.xml",
            "--log", "log.html",
            "--report", "report.html",
        ] + suite_paths

    with _run_lock:
        _active_run = {"id": run_id, "proc": None, "queue": q, "done": False}

    threading.Thread(target=_stream_run, args=(run_id, cmd, q), daemon=True).start()
    return jsonify({"run_id": run_id})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _run_lock:
        run = _active_run
    if run and run.get("proc") and not run.get("done"):
        proc = run["proc"]
        try:
            if sys.platform == "win32":
                # Kill entire process tree so Robot Framework children are also terminated
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                import os, signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        with _run_lock:
            if _active_run and _active_run["id"] == run["id"]:
                _active_run["done"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "No active run"}), 404


@app.route("/api/stream/<run_id>")
def api_stream(run_id: str):
    with _run_lock:
        run = _active_run
    if not run or run["id"] != run_id:
        return jsonify({"error": "Run not found"}), 404

    def generate():
        q = run["queue"]
        while True:
            try:
                line = q.get(timeout=30)
                if line.startswith("__TEST__"):
                  yield f"data: {json.dumps({'type': 'test', 'test': json.loads(line[8:])})}\n\n"
                  continue
                if line.startswith("__DEBUG__"):
                    yield f"data: {json.dumps({'type': 'debug', 'line': line[9:]})}\n\n"
                    continue
                if line.startswith("__EXIT__"):
                    code = line[8:]
                    yield f"data: {json.dumps({'type': 'exit', 'code': code})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
                if run.get("done"):
                    break

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/status")
def api_status():
    with _run_lock:
        run = _active_run
    if not run:
        return jsonify({"status": "idle"})
    proc = run.get("proc")
    started_at = run.get("started_at")
    elapsed = int(time.monotonic() - started_at) if started_at and not run.get("done") else None
    return jsonify({
        "status": "done" if run.get("done") else "running",
        "run_id": run["id"],
      "pid": proc.pid if proc else None,
      "elapsed_seconds": elapsed,
    })


@app.route("/api/results")
def api_results():
    if not RESULTS_DIR.exists():
        return jsonify([])
    files = []
    for p in RESULTS_DIR.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in {".html", ".xml"}:
            continue
        relative_name = p.relative_to(RESULTS_DIR).as_posix()
        stat = p.stat()
        files.append({
            "name": relative_name,
            "label": relative_name if p.parent == RESULTS_DIR else f"{p.parent.name} / {p.name}",
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    files.sort(key=lambda item: (item["name"].count("/"), item["name"]))
    return jsonify(files)


@app.route("/results/<path:filename>")
def serve_result(filename: str):
    p = (RESULTS_DIR / filename).resolve()
    if not str(p).startswith(str(RESULTS_DIR)):
        return "Forbidden", 403
    if not p.exists():
        return "Not found", 404
    return send_file(p)


@app.route("/artifacts/<kind>/<path:filename>")
def serve_image_artifact(kind: str, filename: str):
  resource_root = REPO_DIR / "gmtools_rtos_resources" / "resources"
  roots = {
    "comparison": resource_root / "results",
    "reference": resource_root / "references",
    "capture": resource_root / "screenshot_comparison",
  }
  root = roots.get(kind)
  if root is None:
    return "Not found", 404
  root = root.resolve()
  path = (root / filename).resolve()
  try:
    path.relative_to(root)
  except ValueError:
    return "Forbidden", 403
  if path.suffix.lower() != ".png":
    return "Forbidden", 403
  if not path.is_file():
    return "Not found", 404
  return send_file(path, mimetype="image/png", conditional=True)


@app.route("/gmtools_rtostestautomation/gmtools_rtos_resources/resources/<kind>/<path:filename>")
@app.route("/gmtools_rtos_resources/resources/<kind>/<path:filename>")
def serve_repo_resource_image(kind: str, filename: str):
  resource_root = REPO_DIR / "gmtools_rtos_resources" / "resources"
  roots = {
    "results": resource_root / "results",
    "references": resource_root / "references",
    "screenshot_comparison": resource_root / "screenshot_comparison",
  }
  root = roots.get(kind)
  if root is None:
    return "Not found", 404
  root = root.resolve()
  path = (root / filename).resolve()
  try:
    path.relative_to(root)
  except ValueError:
    return "Forbidden", 403
  if path.suffix.lower() != ".png":
    return "Forbidden", 403
  if not path.is_file():
    return "Not found", 404
  return send_file(path, mimetype="image/png", conditional=True)


# ── Embedded HTML ──────────────────────────────────────────────────────────────

_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FRCC RobotFramework Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --ink:#193238; --muted:#60757a; --navy:#193238; --teal:#087f8c; --teal-dark:#06636d;
    --amber:#e8ad35; --coral:#d96c58; --line:#d8e4e1; --panel:#ffffff;
    --page:#edf3f1; --surface:#f7faf9; --surface-soft:#e6f1ee; --accent-soft:#d8eeea;
    --shadow:rgba(24,58,63,.10);
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --ink:#e4efed; --muted:#a0b4b4; --navy:#dce9e7; --teal:#55c3c4; --teal-dark:#2da6aa;
    --amber:#f2c15b; --coral:#f08b76; --line:#34494b; --panel:#1d2b2e;
    --page:#142124; --surface:#243538; --surface-soft:#2b4143; --accent-soft:#29494a;
    --shadow:rgba(0,0,0,.24);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: "Aptos", "Segoe UI", Arial, sans-serif; background-color: var(--page); background-image: linear-gradient(135deg, rgba(8,127,140,.045), transparent 48%), linear-gradient(315deg, rgba(232,173,53,.055), transparent 52%); color: var(--ink); font-size: 14px; min-height: 100vh; }

  /* ── Top bar ── */
  .topbar {
    background: var(--panel); color: var(--ink); display: flex;
    align-items: center; justify-content: space-between;
    padding: 0 clamp(16px, 2.5vw, 40px); height: 62px; position: sticky; top: 0; z-index: 100;
    border-bottom: 1px solid var(--line);
    box-shadow: 0 4px 16px var(--shadow);
  }
  .topbar h1 { font-family: "Trebuchet MS", "Segoe UI", sans-serif; font-size: 18px; font-weight: 800; letter-spacing: 1.3px; text-transform: uppercase; color: var(--ink); }
  .topbar-actions { display:flex; align-items:center; gap:12px; }
  .theme-toggle { min-height:34px; padding:6px 11px; border:1px solid var(--line); border-radius:6px; background:var(--surface); color:var(--ink); font-family:inherit; font-size:12px; font-weight:600; cursor:pointer; }
  .theme-toggle:hover { border-color:var(--teal); background:var(--accent-soft); }
  .topbar .badge {
    font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px;
    text-transform: uppercase; letter-spacing: .5px;
  }
  .badge.idle   { background:var(--accent-soft); color:var(--teal-dark); }
  .badge.running{ background:var(--amber); color:#193238; animation: pulse 1.2s infinite; }
  .badge.done   { background:#d8e9c9; color:#456d34; }
  .badge.error  { background:var(--coral); color:#fff; }
  .badge.stopped { background:var(--surface-soft); color:var(--muted); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.6} }

  /* ── Layout ── */
    .main { width: 100%; min-height: calc(100vh - 62px); margin: 0; padding: 24px clamp(16px, 2.5vw, 40px) 38px; display: grid;
      grid-template-columns: minmax(310px, 360px) minmax(0, 1fr); gap: 22px; align-items: start; }
    .main > div:first-child { min-width: 0; }
    @media(min-width:1500px){ .main { grid-template-columns: 390px minmax(0, 1fr); } }
    @media(max-width:860px){ .main{ grid-template-columns:1fr; } }

  /* ── Cards ── */
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 23px 24px;
    box-shadow: 0 12px 30px var(--shadow); position: relative; overflow: hidden;
  }
  .card::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 5px; background: var(--coral); }
  .main > div:first-child .card:nth-child(2)::before { background: var(--amber); }
  .main > div:first-child .card:nth-child(3)::before { background: var(--teal); }
  .right-col .card:first-child::before { background: var(--amber); }
  .right-col .card:nth-child(2)::before { background: var(--teal); }
  .right-col .card:nth-child(3)::before { background: var(--coral); }
  .card h2 { font-family: "Trebuchet MS", "Segoe UI", sans-serif; font-size: 15px; font-weight: 800;
             letter-spacing: 1px; text-transform: uppercase; color: var(--ink); margin-bottom: 16px;
             padding-bottom: 11px; border-bottom: 1px solid var(--line); }

  /* ── Form controls ── */
  select, input[type=text], input[type=search] {
    width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 5px;
    font-size: 13px; background: var(--surface); outline: none; color: var(--ink);
  }
  select:focus, input[type=text]:focus { border-color: var(--teal); box-shadow: 0 0 0 3px rgba(8,127,140,.12); }
  label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; margin-top: 10px; }
  label:first-child { margin-top: 0; }

  /* ── Buttons ── */
  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    padding: 8px 18px; border: none; border-radius: 5px; font-size: 13px;
    font-weight: 600; cursor: pointer; transition: background .15s, opacity .15s;
  }
  .btn:disabled { opacity: .45; cursor: default; }
  .btn-primary  { background: var(--teal); color: #fff; }
  .btn-primary:hover:not(:disabled)  { background: var(--teal-dark); }
  .btn-success  { background: var(--amber); color: #193238; }
  .btn-success:hover:not(:disabled)  { background: #e5a50e; }
  .btn-danger   { background: var(--coral); color: #fff; }
  .btn-danger:hover:not(:disabled)   { background: #dc777e; }
  .btn-secondary{ background: var(--panel); color: var(--ink); border: 1px solid var(--line); }
  .btn-secondary:hover:not(:disabled){ background: var(--accent-soft); }
  .btn-sm { padding: 5px 12px; font-size: 12px; }

  .btn-row { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }

  /* ── Suite list ── */
  #suite-list {
    max-height: 280px; overflow-y: auto; font-size: 12px;
    background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 0; margin-top: 6px;
  }
  .suite-item {
    display: flex; align-items: center; gap: 8px; padding: 5px 12px; cursor: pointer;
    border-left: 3px solid transparent; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    transition: background .1s;
  }
  .suite-item[hidden] { display:none; }
  .suite-item:hover { background: var(--accent-soft); border-left-color: var(--coral); }
  .suite-item input { flex: 0 0 auto; accent-color: var(--teal); }
  .suite-item span { overflow: hidden; text-overflow: ellipsis; }
  .suite-count { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .suite-mode { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px; margin-top: 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
  .suite-mode button { border: 0; border-radius: 4px; padding: 7px 10px; background: transparent; color: var(--muted); font: inherit; font-weight: 600; cursor: pointer; }
  .suite-mode button[aria-pressed="true"] { background: var(--teal); color: #fff; }
  .suite-mode button:hover:not([aria-pressed="true"]) { background: var(--accent-soft); color: var(--ink); }
  #suite-filter { margin-top:8px; }
  .run-context { font-size:12px; color:var(--muted); margin-bottom:12px; }
  :focus-visible { outline:3px solid var(--amber); outline-offset:2px; }

  /* ── Upload area ── */
  .drop-zone {
    border: 2px dashed var(--line); border-radius: 8px; padding: 16px;
    text-align: center; font-size: 12px; color: var(--muted); cursor: pointer;
    transition: border-color .2s, background .2s; margin-top: 6px;
  }
  .drop-zone:hover, .drop-zone.drag-over { border-color: var(--teal); background: var(--accent-soft); color: var(--teal-dark); }
  .drop-zone input[type=file] { display: none; }

  /* ── Console ── */
  #console {
    background: #25262a; color: #f8e9e8; font-family: Consolas, monospace;
    font-size: 13px; line-height: 1.55; padding: 12px 14px;
    border-radius: 8px; min-height: 460px; height: clamp(460px, 58vh, 760px); overflow-y: auto;
    white-space: pre-wrap; word-break: break-all;
  }
  #console .log-pass   { color: #7eff9e; }
  #console .log-fail   { color: #ff7b7b; }
  #console .log-warn   { color: #ffd87b; }
  #console .log-info   { color: #79c0ff; }
  #console .log-dim    { color: #6e7681; }
  #console .log-exit-ok{ color: #7eff9e; font-weight: bold; }
  #console .log-exit-err{color: #ff7b7b; font-weight: bold; }
  .console-tabs { display:flex; gap:4px; margin-bottom:8px; }
  .console-controls { display:flex; align-items:center; gap:8px; margin:0 0 8px; flex-wrap:wrap; }
  .console-controls input[type=search] { width:min(240px,100%); }
  .console-controls select { width:auto; min-width:120px; }
  .console-controls .follow-control { display:inline-flex; align-items:center; gap:6px; margin:0; white-space:nowrap; }
  .console-controls .follow-control input { accent-color:var(--teal); }
  .console-action-msg { color:var(--muted); font-size:11px; }
  #console span[hidden] { display:none; }
  .console-tab { border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:4px;
    padding:5px 11px; font-size:12px; cursor:pointer; }
  .console-tab.active { background:var(--teal); border-color:var(--teal); color:#fff; }
  .debug-console { display:none; background:#25262a; color:#d8dee9; font-family:Consolas, monospace;
    font-size:12px; line-height:1.55; padding:12px 14px; border-radius:8px; min-height:460px;
    height:clamp(460px,58vh,760px); overflow-y:auto; white-space:pre-wrap; word-break:break-word; }
  .debug-console.visible { display:block; }
  .debug-entry { color:#b8c0cc; }
  .debug-entry .debug-time { color:#79c0ff; }
  .debug-entry .debug-label { color:#ffd87b; }
  .test-status-panel { display:none; }
  .test-status-panel.visible { display:block; }
  .test-status-tools { display:flex; gap:8px; margin-bottom:8px; flex-wrap:wrap; }
  .test-status-tools input[type=search] { width:min(280px,100%); }
  .test-status-tools select { width:auto; min-width:130px; }
  .test-summary { color:var(--muted); font-size:12px; margin:0 0 8px; }
  #test-list { border:1px solid var(--line); border-radius:6px; max-height:clamp(320px,48vh,620px); overflow:auto; }
  .test-list-empty { padding:14px; color:var(--muted); text-align:center; }
  .test-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px 12px; padding:9px 12px; border-bottom:1px solid var(--line); }
  .test-row:last-child { border-bottom:0; }
  .test-row-heading { display:flex; align-items:center; gap:8px; min-width:0; }
  .test-name { overflow-wrap:anywhere; color:var(--ink); font-size:12px; }
  .test-duration { color:var(--muted); font-size:11px; white-space:nowrap; }
  .test-status { border-radius:4px; padding:2px 6px; font-size:10px; font-weight:700; white-space:nowrap; }
  .test-status.running { background:var(--accent-soft); color:var(--teal-dark); }
  .test-status.pass { background:#d8e9c9; color:#456d34; }
  .test-status.fail { background:#fce0dc; color:#9b3328; }
  .test-status.skip { background:#fff1cc; color:#805c13; }
  .test-status.stopped { background:var(--surface-soft); color:var(--muted); }
  .test-message { grid-column:1 / -1; color:#b8c0cc; font-size:11px; white-space:pre-wrap; overflow-wrap:anywhere; }
  :root[data-theme="dark"] .test-status.pass { background:#29452d; color:#a8dfa7; }
  :root[data-theme="dark"] .test-status.fail { background:#512d2b; color:#ffaaa0; }
  :root[data-theme="dark"] .test-status.skip { background:#514329; color:#f5d88f; }

  /* ── Results tabs ── */
  .tabs { display: flex; gap: 4px; margin-bottom: 10px; flex-wrap: wrap; }
  .tab {
    padding: 6px 14px; border-radius: 5px 5px 0 0; font-size: 12px; font-weight: 600;
    font-family:inherit;
    cursor: pointer; border: 1px solid var(--line); border-bottom: none;
    background: var(--surface-soft); color: var(--muted); transition: background .1s;
  }
  .tab.active { background: var(--teal); color: #fff; border-color: var(--teal); }
  .tab:hover:not(.active) { background: var(--accent-soft); }
  .results-tools { display:flex; align-items:center; gap:8px; margin:0 0 10px; flex-wrap:wrap; }
  .results-tools input[type=search] { width:min(280px,100%); }
  .results-tools select { width:auto; min-width:150px; }
  .results-tools[hidden] { display:none; }
  .results-count { color:var(--muted); font-size:11px; }
  .result-frame {
    width: 100%; height: clamp(520px, 68vh, 900px); border: 1px solid var(--line); border-radius: 0 6px 6px 6px;
    background: var(--panel);
  }
  .no-results {
    height: 200px; display: flex; align-items: center; justify-content: center;
    color: var(--muted); font-size: 13px; border: 1px dashed var(--line); border-radius: 5px;
  }

  /* ── Misc ── */
  .msg { font-size: 12px; margin-top: 8px; padding: 6px 10px; border-radius: 4px; }
  .msg.ok  { background: #f0fdf4; color: #166534; }
  .msg.err { background: #fef2f2; color: #991b1b; }
  .divider { border: none; border-top: 1px solid var(--line); margin: 14px 0; }
  .right-col { display: flex; flex-direction: column; gap: 22px; min-width: 0; }
  .repo-card > summary { display:flex; align-items:center; justify-content:space-between; cursor:pointer; list-style:none; }
  .repo-card > summary::-webkit-details-marker { display:none; }
  .repo-card > summary h2 { margin:0; padding:0; border:0; }
  .repo-card > summary::after { content:"+"; color:var(--teal); font-size:20px; font-weight:700; }
  .repo-card[open] > summary::after { content:"−"; }
  .repo-content { margin-top:18px; }
  :root[data-theme="dark"] .main p { color: var(--muted) !important; }
  :root[data-theme="dark"] #suite-list > div,
  :root[data-theme="dark"] #repo-status-label,
  :root[data-theme="dark"] #repo-pct,
  :root[data-theme="dark"] #repo-filename { color:var(--muted) !important; }
  :root[data-theme="dark"] #repo-progress { background:var(--surface) !important; border-color:var(--line) !important; }
  :root[data-theme="dark"] #repo-progress > div:nth-child(2) { background:var(--line) !important; }
  @media(max-width:600px) { .topbar { padding:0 14px; } .topbar h1 { font-size:14px; } .topbar-actions { gap:8px; } .theme-toggle { padding:6px 8px; } }
</style>
</head>
<body>

<div class="topbar">
  <h1>&#127939; FRCC RobotFramework Dashboard</h1>
  <div class="topbar-actions">
    <button id="theme-toggle" class="theme-toggle" type="button" onclick="toggleTheme()" aria-label="Switch to dark mode"></button>
    <span id="status-badge" class="badge idle" aria-live="polite">Idle</span>
  </div>
</div>

<div class="main">
  <!-- ── LEFT COLUMN ── -->
  <div style="display:flex;flex-direction:column;gap:16px;">

    <!-- Config card -->
    <div class="card">
      <h2>&#9881; Configuration</h2>
      <label>Vehicle Program</label>
      <select id="vp-select" onchange="changeVehicle()">
        <option value="">Loading...</option>
      </select>

      <label style="margin-top:10px;">Display Size</label>
      <div style="display:flex;gap:6px;align-items:center;">
        <select id="display-size" onchange="changeDisplaySize()" style="flex:1;">
          <option value="8in">8in</option>
          <option value="11in">11in</option>
        </select>
      </div>
      <div id="display-size-msg" style="font-size:11px;margin-top:3px;min-height:14px;"></div>

      <label>Suites for selected vehicle</label>
      <div class="suite-mode" role="group" aria-label="Suite category">
        <button id="suite-mode-regression" type="button" aria-pressed="true" onclick="setSuiteMode('regression')">Regression</button>
        <button id="suite-mode-sanity" type="button" aria-pressed="false" onclick="setSuiteMode('sanity')">Sanity</button>
      </div>
      <input id="suite-filter" type="search" placeholder="Search suites" aria-label="Search suites" oninput="filterSuites()">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;font-size:11px;color:#6f6970;">
        <span>Select visible suites</span>
        <span>
          <button class="btn btn-secondary btn-sm" onclick="selectAllSuites(true)" type="button" title="Select visible suites">All visible</button>
          <button class="btn btn-secondary btn-sm" onclick="selectAllSuites(false)" type="button">None</button>
        </span>
      </div>
      <div id="suite-list" role="group" aria-label="Robot suites"><div style="padding:8px 12px;color:#aaa">Loading...</div></div>
      <div id="suite-count" class="suite-count" aria-live="polite"></div>

      <hr class="divider">
      <label>Update vehicle_suites.json</label>
      <div class="drop-zone" id="drop-zone" onclick="document.getElementById('vsfile').click()"
           ondragover="event.preventDefault();this.classList.add('drag-over')"
           ondragleave="this.classList.remove('drag-over')"
           ondrop="handleDrop(event)">
        <input type="file" id="vsfile" accept=".json" onchange="uploadVehicleSuites(this.files[0])">
        &#128196; Drop vehicle_suites.json here or click to browse
      </div>
      <div id="upload-msg"></div>
    </div>

    <!-- Repo upload card -->
    <details class="card repo-card">
      <summary><h2>&#128230; Automation Repository</h2></summary>
      <div class="repo-content">
      <label>Use local automation folder</label>
      <div style="display:flex;gap:6px;">
        <input type="text" id="repo-path" placeholder="D:\\path\\to\\gmtools_rtostestautomation">
        <button class="btn btn-primary btn-sm" onclick="connectRepoPath()">Use Folder</button>
      </div>
      <div id="repo-path-msg" class="msg"></div>
      <p style="font-size:12px;color:#555;margin-bottom:6px;">
        Paste a local folder path to use the automation repo directly. It must contain
        <code>RTOSAutomation\\src\\rtos</code>.
      </p>
      <p style="font-size:12px;color:#777;margin-bottom:6px;">
        Archive upload remains available when you need to replace the local repo.
        All other dashboard files are stable and do not need to be re-uploaded.
      </p>
      <p style="font-size:11px;color:#888;margin-bottom:10px;">
        &#9432; Zip the <code>gmtools_rtostestautomation</code> folder directly &mdash; files are extracted into the workspace, replacing existing ones.
      </p>
      <div class="drop-zone" id="repo-drop-zone"
           onclick="document.getElementById('repofile').click()"
           ondragover="event.preventDefault();this.classList.add('drag-over')"
           ondragleave="this.classList.remove('drag-over')"
           ondrop="handleRepoDrop(event)">
        <input type="file" id="repofile" accept=".zip,.7z,.rar" onchange="uploadRepo(this.files[0])">
        &#128230; Drop automation repo (.zip / .7z / .rar) here or click to browse
      </div>
      <div id="repo-progress" style="margin-top:12px;background:#f8f9fa;border:1px solid #e5e7eb;border-radius:6px;padding:10px 12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <span id="repo-status-label" style="font-size:12px;font-weight:600;color:#888;">&#9711; Ready — no file selected</span>
          <span id="repo-pct" style="font-size:11px;color:#888;"></span>
        </div>
        <div style="background:#e5e7eb;border-radius:4px;height:10px;overflow:hidden;">
          <div id="repo-progress-bar" style="height:100%;background:#2563eb;width:0%;transition:width .25s;"></div>
        </div>
        <div id="repo-filename" style="font-size:11px;color:#aaa;margin-top:5px;min-height:14px;"></div>
      </div>
      <button class="btn btn-secondary btn-sm" onclick="deleteRepo()" style="margin-top:10px;color:#b91c1c;border-color:#fecaca;">
        &#128465; Delete Automation Repo
      </button>
      <div id="repo-msg"></div>
      </div>
    </details>

    <!-- Run single suite card -->
    <div class="card">
      <h2>&#9654; Run Single Suite</h2>
      <label>Suite name (from list above, click to fill)</label>
      <input type="text" id="single-suite" placeholder="e.g. FCC_Seat_Driver_Adjustment.robot">
      <div class="btn-row">
        <button class="btn btn-primary btn-sm" onclick="runSingle()" id="btn-run-single">Run Suite</button>
        <button class="btn btn-secondary btn-sm" onclick="clearSingle()">Clear</button>
      </div>
    </div>

  </div>

  <!-- ── RIGHT COLUMN ── -->
  <div class="right-col">

    <!-- Bulk run card -->
    <div class="card">
      <h2>&#128640; Bulk Run</h2>
      <p id="run-context" class="run-context" aria-live="polite"></p>
      <div class="btn-row">
        <button class="btn btn-success" onclick="runBulk()" id="btn-run-bulk">
          &#9654;&nbsp; Run All Suites
        </button>
        <button class="btn btn-primary" onclick="runSelected()" id="btn-run-selected">
          &#9654;&nbsp; Run Selected
        </button>
        <button class="btn btn-danger" onclick="stopRun()" id="btn-stop" disabled>
          &#9632;&nbsp; Stop
        </button>
        <button class="btn btn-secondary btn-sm" onclick="clearConsole()">Clear Log</button>
      </div>
    </div>

    <!-- Live console card -->
    <div class="card" style="padding-bottom:14px;">
      <h2>&#128196; Live Output</h2>
      <div class="console-tabs">
        <button id="output-tab" class="console-tab active" onclick="showConsole('output')">Output</button>
        <button id="debug-tab" class="console-tab" onclick="showConsole('debug')">Debug Console</button>
        <button id="tests-tab" class="console-tab" onclick="showConsole('tests')">Tests <span id="test-tab-count">0</span></button>
        <button class="console-tab" onclick="checkRunStatus()">Check Status</button>
      </div>
      <div class="console-controls" aria-label="Output controls">
        <input id="console-search" type="search" placeholder="Search output" aria-label="Search console output" oninput="filterConsole()">
        <select id="console-level" aria-label="Filter output level" onchange="filterConsole()">
          <option value="all">All lines</option><option value="pass">Pass</option>
          <option value="fail">Fail / error</option><option value="warn">Warnings</option>
          <option value="info">Info</option><option value="other">Other</option>
        </select>
        <label class="follow-control"><input id="console-follow" type="checkbox" checked> Auto-follow</label>
        <button class="btn btn-secondary btn-sm" type="button" onclick="copyConsole()">Copy</button>
        <button class="btn btn-secondary btn-sm" type="button" onclick="downloadConsole()">Download</button>
        <span id="console-action-msg" class="console-action-msg" aria-live="polite"></span>
      </div>
      <div id="console"><span class="log-dim">Console output will appear here when a run starts...</span></div>
      <div id="debug-console" class="debug-console"><span class="debug-entry">Robot keyword and test events will appear here when a run starts...</span></div>
      <div id="test-status-panel" class="test-status-panel">
        <div class="test-status-tools">
          <input id="test-search" type="search" placeholder="Search tests" aria-label="Search test results" oninput="renderTestStatus()">
          <select id="test-filter" aria-label="Filter test status" onchange="renderTestStatus()">
            <option value="all">All statuses</option><option value="running">Running</option>
            <option value="pass">Passed</option><option value="fail">Failed</option>
            <option value="skip">Skipped</option><option value="stopped">Stopped</option>
          </select>
        </div>
        <div id="test-summary" class="test-summary" aria-live="polite">Waiting for Robot tests...</div>
        <div id="test-list" role="list" aria-label="Robot test results">
          <div class="test-list-empty">Test results will appear here when a run starts.</div>
        </div>
      </div>
    </div>

    <!-- Results card -->
    <div class="card">
      <h2>&#128202; Results</h2>
      <div class="results-tools" id="results-tools" hidden>
        <input id="results-search" type="search" placeholder="Search result files" aria-label="Search result files" oninput="filterResults()">
        <select id="results-kind" aria-label="Filter result type" onchange="filterResults()">
          <option value="all">All result types</option><option value="testcase">Test-case reports</option>
          <option value="report">Robot reports</option><option value="log">Logs</option>
          <option value="raw">XML output</option><option value="other">Other files</option>
        </select>
        <span id="results-count" class="results-count" aria-live="polite"></span>
      </div>
      <div class="tabs" id="result-tabs" role="tablist" aria-label="Result files"></div>
      <div id="result-area" role="tabpanel" aria-live="polite"><div class="no-results">No results yet &mdash; run a suite first.</div></div>
      <div class="btn-row" style="margin-top:10px;">
        <button class="btn btn-secondary btn-sm" onclick="refreshResults()">&#8635; Refresh Results</button>
      </div>
    </div>

  </div>
</div>

<script>
let activeRunId = null;
let sse = null;
let availableVehicles = [];
let availableSuites = [];
let selectedSuitePaths = new Set();
let suiteMode = 'regression';
const testStatuses = new Map();
let _logBuffer = [];
let _flushTimer = null;
const MAX_CONSOLE_LINES = 800;
const MAX_DEBUG_LINES = 1200;

function applyTheme(theme) {
  const selectedTheme = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = selectedTheme;
  const button = document.getElementById('theme-toggle');
  if (button) {
    const nextTheme = selectedTheme === 'dark' ? 'light' : 'dark';
    button.innerHTML = selectedTheme === 'dark' ? '&#9728; Light mode' : '&#9790; Dark mode';
    button.setAttribute('aria-label', `Switch to ${nextTheme} mode`);
    button.setAttribute('aria-pressed', String(selectedTheme === 'dark'));
  }
}

function toggleTheme() {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
  try { localStorage.setItem('dashboard-theme', theme); } catch (error) { /* Storage may be unavailable. */ }
}

function initTheme() {
  let savedTheme = null;
  try { savedTheme = localStorage.getItem('dashboard-theme'); } catch (error) { /* Storage may be unavailable. */ }
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(savedTheme || systemTheme);
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  await loadConfig();
  await refreshResults();
  await pollStatus();
}

async function loadConfig() {
  const r = await fetch('/api/config?t=' + Date.now());
  const data = await r.json();
  availableVehicles = data.vehicles || [];
  if (data.repo_path) document.getElementById('repo-path').value = data.repo_path;

  const sel = document.getElementById('vp-select');
  sel.innerHTML = '';
  availableVehicles.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    if (v === data.vehicle_program) o.selected = true;
    sel.appendChild(o);
  });
  if (availableVehicles.includes(data.vehicle_program)) {
    sel.value = data.vehicle_program;
  }

  const dsInput = document.getElementById('display-size');
  if (dsInput && data.display_size) {
    // Set dropdown to current value; add as option if not in list
    if (![...dsInput.options].find(o => o.value === data.display_size)) {
      const o = document.createElement('option');
      o.value = data.display_size; o.textContent = data.display_size;
      dsInput.insertBefore(o, dsInput.firstChild);
    }
    dsInput.value = data.display_size;
  }

  renderSuiteList(data.suites);
}

function renderSuiteList(suites) {
  availableSuites = suites || [];
  selectedSuitePaths = new Set(availableSuites);
  renderVisibleSuites();
}

function suitesForCurrentMode() {
  return availableSuites.filter(suite => {
    const isSanity = /(?:^|[\\/])sanity(?:[\\/]|$)/i.test(suite);
    return suiteMode === 'sanity' ? isSanity : !isSanity;
  });
}

function setSuiteMode(mode) {
  if (mode !== 'regression' && mode !== 'sanity') return;
  syncSuiteSelection();
  suiteMode = mode;
  document.getElementById('suite-mode-regression').setAttribute('aria-pressed', String(mode === 'regression'));
  document.getElementById('suite-mode-sanity').setAttribute('aria-pressed', String(mode === 'sanity'));
  renderVisibleSuites();
}

function renderVisibleSuites() {
  const list = document.getElementById('suite-list');
  const suites = suitesForCurrentMode();
  if (!suites.length) {
    list.innerHTML = `<div style="padding:8px 12px;color:#aaa">No ${suiteMode} suites for this vehicle</div>`;
    updateSuiteCount();
    return;
  }
  list.innerHTML = suites.map(s => {
    const encoded = escapeHtml(s);
    return `<label class="suite-item" title="${encoded}">
      <input type="checkbox" class="suite-checkbox" value="${encoded}" ${selectedSuitePaths.has(s) ? 'checked' : ''} onchange="updateSuiteCount()">
      <span onclick="event.preventDefault();document.getElementById('single-suite').value=this.textContent">${encoded}</span>
    </label>`;
  }).join('');
  filterSuites();
}

function selectedSuites() {
  return suitesForCurrentMode().filter(suite => selectedSuitePaths.has(suite));
}

function syncSuiteSelection() {
  document.querySelectorAll('.suite-checkbox').forEach(input => {
    if (input.checked) selectedSuitePaths.add(input.value);
    else selectedSuitePaths.delete(input.value);
  });
}

function updateSuiteCount() {
  syncSuiteSelection();
  const rows = [...document.querySelectorAll('.suite-item')];
  const total = rows.length;
  const selected = selectedSuites().length;
  const visible = rows.filter(row => !row.hidden).length;
  document.getElementById('suite-count').textContent = `${selected} of ${total} selected · ${visible} shown`;
  updateRunContext();
}

function filterSuites() {
  const query = document.getElementById('suite-filter').value.trim().toLowerCase();
  document.querySelectorAll('.suite-item').forEach(row => {
    row.hidden = !row.textContent.toLowerCase().includes(query);
  });
  updateSuiteCount();
}

function updateRunContext() {
  const vehicle = document.getElementById('vp-select').value || 'No vehicle selected';
  const displaySize = document.getElementById('display-size').value || 'display size not set';
  const total = suitesForCurrentMode().length;
  const selected = selectedSuites().length;
  document.getElementById('run-context').textContent = `${vehicle} · ${displaySize} · ${suiteMode} · ${selected} of ${total} suites selected`;
}

function selectAllSuites(checked) {
  document.querySelectorAll('.suite-item').forEach(row => {
    if (!row.hidden) row.querySelector('.suite-checkbox').checked = checked;
  });
  updateSuiteCount();
}

async function changeVehicle() {
  const vp = document.getElementById('vp-select').value;
  const r = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({vehicle_program: vp})
  });
  const data = await r.json();
  if (data.ok) renderSuiteList(data.suites);
}

async function changeDisplaySize() {
  const ds = document.getElementById('display-size').value.trim();
  const msg = document.getElementById('display-size-msg');
  if (!ds) return;
  const r = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({display_size: ds})
  });
  const data = await r.json();
  if (data.ok) {
    availableVehicles = data.vehicles || availableVehicles;
    const sel = document.getElementById('vp-select');
    sel.innerHTML = '';
    availableVehicles.forEach(v => {
      const o = document.createElement('option');
      o.value = v; o.textContent = v;
      if (v === data.vehicle_program) o.selected = true;
      sel.appendChild(o);
    });
    renderSuiteList(data.suites);
    msg.style.color = '#16a34a'; msg.textContent = 'Saved: ' + data.display_size;
  } else {
    msg.style.color = '#dc2626'; msg.textContent = data.error || 'Failed';
  }
  setTimeout(() => { msg.textContent = ''; }, 3000);
}

// ── Run controls ──────────────────────────────────────────────────────────────
async function runBulk() {
  const suites = suitesForCurrentMode();
  if (!suites.length) { alert(`No ${suiteMode} suites are available.`); return; }
  await startRun({ suites });
}

async function runSelected() {
  const suites = selectedSuites();
  if (!suites.length) { alert('Select at least one robot suite.'); return; }
  await startRun({ suites: suites });
}

async function runSingle() {
  const suite = document.getElementById('single-suite').value.trim();
  if (!suite) { alert('Enter a suite name or path first.'); return; }
  await startRun({ suite: suite });
}

function clearSingle() {
  document.getElementById('single-suite').value = '';
}

async function startRun(body) {
  const r = await fetch('/api/run', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  const data = await r.json();
  if (!r.ok) { appendLog('[ERROR] ' + (data.error || 'Failed to start run'), 'fail'); return; }

  activeRunId = data.run_id;
  clearConsole();
  resetTestStatus();
  appendLog('Run started (id: ' + activeRunId + ')', 'info');
  setRunning(true);
  startSSE(activeRunId);
}

async function stopRun() {
  if (sse) { sse.close(); sse = null; }
  const r = await fetch('/api/stop', {method:'POST'});
  const data = await r.json();
  if (r.ok) {
    appendLog('[STOPPED] Run was terminated by user.', 'warn');
    markRunningTestsStopped();
    setRunning(false, 'stopped');
    setTimeout(refreshResults, 2000);
  }
}

function startSSE(runId) {
  if (sse) sse.close();
  sse = new EventSource('/api/stream/' + runId);
  sse.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'test') {
      updateTestStatus(msg.test);
      return;
    }
    if (msg.type === 'debug') {
      appendDebug(msg.line);
      return;
    }
    if (msg.type === 'exit') {
      const code = parseInt(msg.code);
      markRunningTestsStopped();
      if (code === 0) appendLog('\\n✔ Run completed successfully (exit 0)', 'exit-ok');
      else appendLog('\\n✘ Run finished with exit code ' + code, 'exit-err');
      setRunning(false, code === 0 ? 'done' : 'error');
      sse.close(); sse = null;
      setTimeout(refreshResults, 1500);
      return;
    }
    appendLog(msg.line);
  };
  sse.onerror = () => { sse.close(); sse = null; setRunning(false, 'error'); };
}

function setRunning(running, finalState = 'idle') {
  document.getElementById('btn-run-bulk').disabled = running;
  document.getElementById('btn-run-selected').disabled = running;
  document.getElementById('btn-run-single').disabled = running;
  document.getElementById('btn-stop').disabled = !running;
  const badge = document.getElementById('status-badge');
  const state = running ? 'running' : finalState;
  const labels = {idle:'Idle', running:'Running', done:'Passed', error:'Failed', stopped:'Stopped'};
  badge.className = 'badge ' + state;
  badge.textContent = labels[state] || 'Idle';
}

async function pollStatus() {
  const r = await fetch('/api/status');
  const data = await r.json();
  if (data.status === 'running' && data.run_id !== activeRunId) {
    activeRunId = data.run_id;
    setRunning(true);
    startSSE(activeRunId);
  }
}

// ── Console ───────────────────────────────────────────────────────────────────
function appendLog(line, type) {
  const c = document.getElementById('console');
  if (c.querySelector('.log-dim') && c.querySelector('.log-dim').textContent.includes('output will appear')) {
    c.innerHTML = '';
  }
  const span = document.createElement('span');
  const cls = type ? 'log-' + type : classifyLine(line);
  if (cls) span.className = cls;
  span.textContent = line + '\\n';
  c.appendChild(span);
  filterConsole();
  if (document.getElementById('console-follow').checked) c.scrollTop = c.scrollHeight;
}

function filterConsole() {
  const query = document.getElementById('console-search').value.trim().toLowerCase();
  const level = document.getElementById('console-level').value;
  document.querySelectorAll('#console > span').forEach(line => {
    const classes = [...line.classList];
    const lineLevel = classes.find(name => name.startsWith('log-'))?.slice(4) || 'other';
    const matchesLevel = level === 'all' || lineLevel === level || (level === 'fail' && lineLevel === 'exit-err') || (level === 'pass' && lineLevel === 'exit-ok');
    line.hidden = !matchesLevel || !line.textContent.toLowerCase().includes(query);
  });
}

async function copyConsole() {
  const message = document.getElementById('console-action-msg');
  try {
    await navigator.clipboard.writeText(document.getElementById('console').textContent);
    message.textContent = 'Copied';
  } catch (error) {
    message.textContent = 'Clipboard access unavailable';
  }
  setTimeout(() => { message.textContent = ''; }, 2500);
}

function downloadConsole() {
  const content = document.getElementById('console').textContent;
  const url = URL.createObjectURL(new Blob([content], {type:'text/plain'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = `dashboard-output-${new Date().toISOString().replace(/[:.]/g, '-')}.log`;
  link.click();
  URL.revokeObjectURL(url);
}

function showConsole(name) {
  const output = name === 'output';
  const debug = name === 'debug';
  document.getElementById('console').style.display = output ? '' : 'none';
  document.getElementById('debug-console').classList.toggle('visible', debug);
  document.getElementById('test-status-panel').classList.toggle('visible', name === 'tests');
  document.getElementById('output-tab').classList.toggle('active', output);
  document.getElementById('debug-tab').classList.toggle('active', debug);
  document.getElementById('tests-tab').classList.toggle('active', name === 'tests');
}

function resetTestStatus() {
  testStatuses.clear();
  document.getElementById('test-search').value = '';
  document.getElementById('test-filter').value = 'all';
  renderTestStatus();
}

function updateTestStatus(test) {
  const key = test.full_name || test.name;
  const current = testStatuses.get(key) || {};
  const status = test.event === 'start' ? 'RUNNING' : String(test.status || 'UNKNOWN').toUpperCase();
  testStatuses.set(key, {...current, ...test, status});
  renderTestStatus();
}

function markRunningTestsStopped() {
  let changed = false;
  testStatuses.forEach(test => {
    if (test.status === 'RUNNING') {
      test.status = 'STOPPED';
      changed = true;
    }
  });
  if (changed) renderTestStatus();
}

function renderTestStatus() {
  const list = document.getElementById('test-list');
  const query = document.getElementById('test-search').value.trim().toLowerCase();
  const filter = document.getElementById('test-filter').value;
  const allTests = [...testStatuses.values()];
  const counts = allTests.reduce((result, test) => {
    const status = test.status.toLowerCase();
    result[status] = (result[status] || 0) + 1;
    return result;
  }, {});
  document.getElementById('test-tab-count').textContent = String(allTests.length);
  document.getElementById('test-summary').textContent = allTests.length
    ? `${allTests.length} tests · ${counts.pass || 0} passed · ${counts.fail || 0} failed · ${counts.running || 0} running · ${counts.skip || 0} skipped`
    : 'Waiting for Robot tests...';

  const visibleTests = allTests.filter(test => {
    const matchesStatus = filter === 'all' || test.status.toLowerCase() === filter;
    const matchesSearch = `${test.full_name || test.name} ${test.message || ''}`.toLowerCase().includes(query);
    return matchesStatus && matchesSearch;
  });
  list.replaceChildren();
  if (!visibleTests.length) {
    const empty = document.createElement('div');
    empty.className = 'test-list-empty';
    empty.textContent = allTests.length ? 'No tests match these filters.' : 'Test results will appear here when a run starts.';
    list.appendChild(empty);
    return;
  }

  visibleTests.forEach(test => {
    const row = document.createElement('div');
    row.className = 'test-row';
    row.setAttribute('role', 'listitem');
    const heading = document.createElement('div');
    heading.className = 'test-row-heading';
    const badge = document.createElement('span');
    badge.className = `test-status ${test.status.toLowerCase()}`;
    badge.textContent = test.status;
    const name = document.createElement('span');
    name.className = 'test-name';
    name.textContent = test.full_name || test.name;
    heading.append(badge, name);
    row.appendChild(heading);
    const duration = document.createElement('span');
    duration.className = 'test-duration';
    duration.textContent = Number.isFinite(test.elapsed_seconds) ? `${test.elapsed_seconds.toFixed(1)}s` : '';
    row.appendChild(duration);
    if (test.message) {
      const message = document.createElement('div');
      message.className = 'test-message';
      message.textContent = test.message;
      row.appendChild(message);
    }
    list.appendChild(row);
  });
}

function appendDebug(line) {
  const c = document.getElementById('debug-console');
  if (c.querySelector('.debug-entry') && c.textContent.includes('Robot keyword and test events will appear')) c.innerHTML = '';
  const entry = document.createElement('div');
  entry.className = 'debug-entry';
  const time = new Date().toLocaleTimeString();
  const trimmedLine = line.trimStart();
  const category = trimmedLine.startsWith('KEYWORD ') || trimmedLine.startsWith('TEST ') ? 'ROBOT' : 'RUNNER';
  entry.innerHTML = `<span class="debug-time">${time}</span> <span class="debug-label">${category}</span> ${escapeHtml(line)}`;
  c.appendChild(entry);
  while (c.children.length > MAX_DEBUG_LINES) c.removeChild(c.firstChild);
  c.scrollTop = c.scrollHeight;
}

async function checkRunStatus() {
  try {
    const r = await fetch('/api/status?t=' + Date.now());
    const data = await r.json();
    const details = data.status === 'running'
      ? `STATUS CHECK: RUNNING | run=${data.run_id} | PID=${data.pid ?? 'starting'} | elapsed=${data.elapsed_seconds ?? 0}s`
      : `STATUS CHECK: ${String(data.status).toUpperCase()}${data.run_id ? ` | run=${data.run_id}` : ''}`;
    appendDebug(details);
    showConsole('debug');
  } catch (error) {
    appendDebug('STATUS CHECK FAILED: ' + error.message);
    showConsole('debug');
  }
}

function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
}

function classifyLine(line) {
  const l = line.toUpperCase();
  if (l.includes('| PASS |') || l.includes('PASS')) return 'log-pass';
  if (l.includes('| FAIL |') || l.includes('FAIL') || l.includes('ERROR')) return 'log-fail';
  if (l.includes('WARN') || l.includes('WARNING')) return 'log-warn';
  if (l.startsWith('[RUNNER]') || l.startsWith('GENERATING')) return 'log-info';
  return '';
}

function clearConsole() {
  document.getElementById('console').innerHTML = '<span class="log-dim">Console cleared.</span>';
  document.getElementById('debug-console').innerHTML = '<span class="debug-entry">Debug console cleared.</span>';
  filterConsole();
}

// ── Results ───────────────────────────────────────────────────────────────────
let currentTab = null;
let resultFiles = [];

async function refreshResults() {
  const r = await fetch('/api/results');
  const files = await r.json();
  resultFiles = files;
  const tabs = document.getElementById('result-tabs');
  const area = document.getElementById('result-area');
  const tools = document.getElementById('results-tools');

  if (!files.length) {
    tools.hidden = true;
    tabs.innerHTML = '';
    area.innerHTML = '<div class="no-results">No results yet &mdash; run a suite first.</div>';
    return;
  }
  tools.hidden = false;

  const labels = {
    'ALL_TESTCASE_WISE_REPORT.html': '&#128203; TC Report',
    'report.html': '&#128200; RF Report',
    'log.html': '&#128196; RF Log',
    'output.xml': '&#128196; output.xml',
  };

  tabs.innerHTML = files.map((f, index) => {
    const label = labels[f.name] || escapeHtml(f.label || f.name);
    return `<button class="tab" type="button" role="tab" aria-controls="result-area" data-index="${index}" data-kind="${getResultKind(f.name)}">${label}</button>`;
  }).join('');
  tabs.querySelectorAll('.tab').forEach(button => {
    button.addEventListener('click', () => showResult(resultFiles[Number(button.dataset.index)].name));
    button.addEventListener('keydown', event => {
      const visibleTabs = [...tabs.querySelectorAll('.tab:not([hidden])')];
      const index = visibleTabs.indexOf(button);
      let nextIndex = null;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % visibleTabs.length;
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + visibleTabs.length) % visibleTabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = visibleTabs.length - 1;
      if (nextIndex !== null) {
        event.preventDefault();
        visibleTabs[nextIndex].focus();
        visibleTabs[nextIndex].click();
      }
    });
  });

  if (!currentTab || !files.find(f => f.name === currentTab)) {
    currentTab = files[0].name;
  }
  filterResults(true);
}

function showResult(name) {
  currentTab = name;
  filterResults(true);
}

function getResultKind(name) {
  const file = name.split('/').pop().toLowerCase();
  if (file.includes('testcase')) return 'testcase';
  if (file.endsWith('.xml')) return 'raw';
  if (file.includes('report')) return 'report';
  if (file.includes('log')) return 'log';
  return 'other';
}

function filterResults(forceRender = false) {
  const query = document.getElementById('results-search').value.trim().toLowerCase();
  const kind = document.getElementById('results-kind').value;
  const visibleFiles = [];
  document.querySelectorAll('#result-tabs .tab').forEach(button => {
    const file = resultFiles[Number(button.dataset.index)];
    const matches = (kind === 'all' || button.dataset.kind === kind) &&
      `${file.name} ${file.label || ''}`.toLowerCase().includes(query);
    button.hidden = !matches;
    if (matches) visibleFiles.push(file);
  });
  document.getElementById('results-count').textContent = `${visibleFiles.length} of ${resultFiles.length} files`;
  if (!visibleFiles.length) {
    currentTab = null;
    document.getElementById('result-area').innerHTML = '<div class="no-results">No matching result files</div>';
    return;
  }
  const previousTab = currentTab;
  if (!visibleFiles.some(file => file.name === currentTab)) currentTab = visibleFiles[0].name;
  document.querySelectorAll('#result-tabs .tab').forEach(button => {
    const file = resultFiles[Number(button.dataset.index)];
    button.classList.toggle('active', file.name === currentTab);
    button.setAttribute('aria-selected', String(file.name === currentTab));
    button.tabIndex = file.name === currentTab ? 0 : -1;
  });
  if (forceRender || currentTab !== previousTab) renderResultFrame(currentTab);
}

function renderResultFrame(name) {
  const area = document.getElementById('result-area');
  if (name.endsWith('.xml')) {
    area.innerHTML = `<div style="padding:14px;font-size:12px;color:#555">
      <a href="/results/${name}" target="_blank" style="color:#2563eb">&#128196; Open ${name} in new tab</a>
    </div>`;
  } else {
    area.innerHTML = `<iframe class="result-frame" src="/results/${name}?t=${Date.now()}" title="${name}"></iframe>`;
  }
}

// ── Upload ────────────────────────────────────────────────────────────────────
function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) uploadVehicleSuites(file);
}

function handleRepoDrop(e) {
  e.preventDefault();
  document.getElementById('repo-drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) uploadRepo(file);
}

async function uploadRepo(file) {
  if (!file) return;
  const bar    = document.getElementById('repo-progress-bar');
  const label  = document.getElementById('repo-status-label');
  const pct    = document.getElementById('repo-pct');
  const fname  = document.getElementById('repo-filename');
  const msg    = document.getElementById('repo-msg');

  const setBar = (w, color) => {
    bar.style.width = w + '%';
    bar.style.background = color || '#2563eb';
  };

  msg.textContent = ''; msg.className = '';
  fname.textContent = '[FILE] ' + file.name + '  (' + (file.size / 1024 / 1024).toFixed(2) + ' MB)';
  label.style.color = '#2563eb';
  label.textContent = '\u23F3 Uploading...';
  setBar(0, '#2563eb'); pct.textContent = '0%';

  // Animate bar to 80% during upload, then jump to 95% during server extraction
  let w = 0;
  const ticker = setInterval(() => {
    w = Math.min(w + 2, 80);
    setBar(w); pct.textContent = w + '%';
  }, 100);

  const fd = new FormData();
  fd.append('file', file);
  try {
    label.textContent = '\u23F3 Uploading...';
    const r = await fetch('/api/upload/repo', { method: 'POST', body: fd });
    clearInterval(ticker);

    label.textContent = '\u23F3 Extracting files...';
    setBar(95); pct.textContent = '95%';
    await new Promise(res => setTimeout(res, 400)); // brief pause so user sees extracting state

    const data = await r.json();
    setBar(100); pct.textContent = '100%';

    if (r.ok) {
      label.style.color = '#16a34a';
      label.textContent = '\u2714 Done';
      bar.style.background = '#16a34a';
      fname.textContent += '  \u2192  ' + data.message;
      await loadConfig();
    } else {
      label.style.color = '#dc2626';
      label.textContent = '\u2718 Failed';
      bar.style.background = '#dc2626';
      msg.className = 'msg err';
      msg.textContent = '\u2718 ' + data.error;
    }
  } catch (err) {
    clearInterval(ticker);
    label.style.color = '#dc2626';
    label.textContent = '\u2718 Upload error';
    bar.style.background = '#dc2626';
    setBar(100); pct.textContent = '';
    msg.className = 'msg err';
    msg.textContent = '\u2718 ' + err.message;
  }
  // Reset to ready after 8 seconds
  setTimeout(() => {
    label.style.color = '#888';
    label.textContent = '\u29BF Ready — select another file to upload';
    bar.style.background = '#2563eb';
    setBar(0); pct.textContent = '';
    fname.textContent = '';
    msg.textContent = ''; msg.className = '';
  }, 8000);
}

async function connectRepoPath() {
  const input = document.getElementById('repo-path');
  const msg = document.getElementById('repo-path-msg');
  const path = input.value.trim();
  if (!path) return;
  msg.className = 'msg'; msg.textContent = 'Connecting local folder...';
  try {
    const r = await fetch('/api/repo-path', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path})
    });
    const data = await r.json();
    msg.className = r.ok ? 'msg ok' : 'msg err';
    msg.textContent = r.ok ? '[OK] ' + data.message : '[ERROR] ' + data.error;
    if (r.ok) await loadConfig();
  } catch (error) {
    msg.className = 'msg err';
    msg.textContent = '[ERROR] ' + error;
  }
}

async function deleteRepo() {
  if (!confirm('Delete the uploaded automation repository? Dashboard files, mappings, and results will be kept.')) return;
  const msg = document.getElementById('repo-msg');
  msg.className = ''; msg.textContent = 'Deleting automation repository...';
  try {
    const r = await fetch('/api/delete/repo', { method: 'POST' });
    const contentType = r.headers.get('content-type') || '';
    const data = contentType.includes('application/json')
      ? await r.json()
      : { error: `Server returned HTTP ${r.status} instead of JSON` };
    msg.className = r.ok ? 'msg ok' : 'msg err';
    msg.textContent = r.ok ? '[OK] ' + data.message : '[ERROR] ' + (data.error || 'Delete failed');
    if (r.ok) {
      document.getElementById('repo-status-label').textContent = '[EMPTY] No repository uploaded';
      document.getElementById('repo-filename').textContent = '';
      document.getElementById('repo-progress-bar').style.width = '0%';
      document.getElementById('repo-pct').textContent = '';
    }
  } catch (error) {
    msg.className = 'msg err';
    msg.textContent = '[ERROR] ' + error;
  }
}

async function uploadVehicleSuites(file) {
  if (!file) return;
  const msg = document.getElementById('upload-msg');
  msg.className = 'msg ok'; msg.textContent = '⏳ Uploading...';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/upload/vehicle-suites', { method: 'POST', body: fd });
    const data = await r.json();
    if (r.ok) {
      await loadConfig();
      msg.className = 'msg ok'; msg.textContent = '✔ ' + data.message;
    } else {
      msg.className = 'msg err'; msg.textContent = '✘ ' + (data.error || 'Upload failed');
    }
  } catch (err) {
    msg.className = 'msg err'; msg.textContent = '✘ Network error: ' + err.message;
  }
  setTimeout(() => { msg.textContent = ''; msg.className = ''; }, 5000);
}

initTheme();
init();
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  FCC Robot Dashboard")
    print(f"  Open: http://localhost:{port}\n")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

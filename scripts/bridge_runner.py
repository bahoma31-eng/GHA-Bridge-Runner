#!/usr/bin/env python3
"""
scripts/bridge_runner.py
~~~~~~~~~~~~~~~~~~~~~~~~
Cloud Bridge Runner V3.0 — GitHub Actions Native Mode

يعمل داخل بيئة GitHub Actions فقط.
لا يحتاج جهازًا محليًا، ولا وكيل خارجي (Notion/السحابة).

التدفق:
1. يُشغَّل الـ workflow يدويًا (workflow_dispatch) أو بـ push على inbox/
2. يقرأ المهمة من inbox/local_task.json في المستودع
3. ينفذها داخل بيئة Actions Runner (Ubuntu)
4. يرفع النتيجة إلى outbox/bridge_result_<timestamp>.json
5. يمسح inbox/ استعدادًا للمهمة القادمة

المحركات المدعومة:
- PYTHON  : تنفيذ كود Python 3
- BASH    : تنفيذ أوامر Bash/Shell
- CMD     : محاكاة CMD عبر bash
- NODEJS  : تنفيذ JavaScript عبر Node.js
- POWERSHELL: تنفيذ PowerShell Core (pwsh)

SECURITY:
- التوكن مخزّن في GitHub Secrets (لا يظهر في السجلات)
- كل نتيجة تُرفع مباشرةً إلى المستودع عبر GitHub API
- يمكن تقييد من يستطيع trigger الـ workflow عبر branch protection
"""

import os
import sys
import json
import time
import base64
import hashlib
import subprocess
import shutil
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("[ERROR] requests not found. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config — تُقرأ من environment variables المُحقنة بواسطة GitHub Actions
# ---------------------------------------------------------------------------
GITHUB_TOKEN   = os.environ.get("GH_TOKEN", "")
REPO_OWNER     = os.environ.get("REPO_OWNER", "")
REPO_NAME      = os.environ.get("REPO_NAME", "")
BRANCH_NAME    = os.environ.get("BRANCH_NAME", "main")
TASK_FILE      = os.environ.get("TASK_FILE", "inbox/local_task.json")
RESULT_DIR     = os.environ.get("RESULT_DIR", "outbox")
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "300"))

GITHUB_API_BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _ts():
    return datetime.now().strftime("%H:%M:%S")

def log(icon, msg):
    print(f"[{_ts()}] {icon}  {msg}", flush=True)

def section(title):
    print(f"\n{'='*65}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'-'*65}", flush=True)

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
def _headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "GHA-Bridge-Runner-V3",
    }

def _url(path):
    return f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"

def gh_get(path):
    """يقرأ ملفًا من المستودع، يُرجع (content_str, sha) أو (None, None)"""
    r = requests.get(_url(path), headers=_headers(), timeout=20)
    if r.status_code == 200:
        data = r.json()
        raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return raw, data.get("sha")
    if r.status_code != 404:
        log("!", f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
    return None, None

def gh_put(path, content_str, commit_msg):
    """يكتب أو يُحدّث ملفًا في المستودع"""
    url = _url(path)
    r_chk = requests.get(url, headers=_headers(), timeout=20)
    sha = r_chk.json().get("sha") if r_chk.status_code == 200 else None

    payload = {
        "message": commit_msg,
        "content": base64.b64encode(content_str.encode("utf-8")).decode(),
        "branch": BRANCH_NAME,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=_headers(), json=payload, timeout=30)
    if r.status_code in (200, 201):
        return True
    log("!", f"PUT {path} -> HTTP {r.status_code}: {r.text[:300]}")
    return False

# ---------------------------------------------------------------------------
# Execution engines
# ---------------------------------------------------------------------------
def _find(binary, *fallbacks):
    for b in (binary, *fallbacks):
        p = shutil.which(b)
        if p:
            return p
    return None

def _run(cmd, timeout, use_shell=False):
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=use_shell,
    )
    return p.returncode, p.stdout or "", p.stderr or ""

def _join(out, err):
    out, err = (out or "").strip(), (err or "").strip()
    if out and err:
        return f"{out}\n\n[STDERR]\n{err}"
    return err if err else out or "(no output)"

def run_task(engine, command, timeout):
    """يُنفّذ الأمر بالمحرك المناسب. يُرجع (rc, output_str)"""
    e = (engine or "BASH").strip().upper()
    try:
        if e == "PYTHON":
            py = _find("python3", "python")
            if not py:
                return 1, "Python not available in runner."
            rc, out, err = _run([py, "-c", command], timeout)

        elif e == "BASH":
            bash = _find("bash", "sh")
            if not bash:
                return 1, "Bash not available in runner."
            rc, out, err = _run([bash, "-c", command], timeout)

        elif e == "CMD":
            bash = _find("bash", "sh")
            rc, out, err = _run([bash, "-c", command], timeout)

        elif e == "NODEJS":
            node = _find("node", "nodejs")
            if not node:
                return 1, "Node.js not available in runner."
            rc, out, err = _run([node, "-e", command], timeout)

        elif e == "POWERSHELL":
            pwsh = _find("pwsh", "powershell")
            if not pwsh:
                return 1, "PowerShell Core (pwsh) not available. Add 'Install pwsh' step to workflow."
            rc, out, err = _run(
                [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
                timeout
            )

        else:
            return 1, f"Unsupported engine: '{engine}'. Supported: PYTHON, BASH, CMD, NODEJS, POWERSHELL"

        return rc, _join(out, err)

    except subprocess.TimeoutExpired:
        return 124, f"Timeout expired after {timeout}s"
    except FileNotFoundError:
        return 1, f"Engine '{engine}' binary not found on runner."
    except Exception as exc:
        return 1, f"Execution error: {exc}"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    section("GHA Bridge Runner V3.0 — Cloud Native Mode")
    print(f"  Repo     : {REPO_OWNER}/{REPO_NAME}  [{BRANCH_NAME}]")
    print(f"  Task     : {TASK_FILE}")
    print(f"  Results  : {RESULT_DIR}/")
    print(f"  Timeout  : {COMMAND_TIMEOUT}s")
    print(f"  Engines  : PYTHON | BASH | CMD | NODEJS | POWERSHELL")

    if not GITHUB_TOKEN:
        log("✗", "GH_TOKEN secret is missing! Add it in repo Settings > Secrets.")
        sys.exit(1)
    if not REPO_OWNER or not REPO_NAME:
        log("✗", "REPO_OWNER or REPO_NAME env var is missing.")
        sys.exit(1)

    section("Reading task from inbox")
    raw, task_sha = gh_get(TASK_FILE)

    if not raw or raw.strip().lower() in ("waiting", "null", "", "{}"):
        log("i", "No pending task found in inbox. Exiting cleanly.")
        sys.exit(0)

    try:
        task = json.loads(raw)
    except json.JSONDecodeError as e:
        log("✗", f"Invalid JSON in task file: {e}")
        sys.exit(1)

    task_id     = str(task.get("task_id", "unknown"))
    engine      = task.get("engine", "BASH")
    command     = task.get("command", "")
    description = task.get("description", "No description")
    timeout     = int(task.get("timeout", COMMAND_TIMEOUT))

    section(f"Task [{task_id}]")
    print(f"  Description : {description}")
    print(f"  Engine      : {engine}")
    print(f"  Timeout     : {timeout}s")
    print(f"  Command preview:")
    for line in str(command).splitlines()[:10]:
        print(f"    {line}")
    if len(str(command).splitlines()) > 10:
        print(f"    ... ({len(str(command).splitlines())} lines total)")

    section("Executing")
    log(">>", "Running...")
    rc, output = run_task(engine, command, timeout)
    status = "SUCCESS" if rc == 0 else "FAILED"
    log("<<", f"Result: {status}  (exit code: {rc})")

    print("\n--- Output (first 2000 chars) ---")
    print((output or "")[:2000])
    print("-" * 40)

    section("Uploading result")
    result = {
        "task_id"        : task_id,
        "status"         : status,
        "return_code"    : rc,
        "engine"         : engine,
        "description"    : description,
        "command_preview": str(command)[:800],
        "output"         : (output or "")[:8000],
        "timestamp"      : _now_iso(),
        "executed_on"    : "github_actions_runner",
        "runner_os"      : os.environ.get("RUNNER_OS", "unknown"),
        "workflow_run"   : os.environ.get("GITHUB_RUN_ID", "unknown"),
        "repo"           : f"{REPO_OWNER}/{REPO_NAME}",
        "bridge_version" : "3.0",
    }

    ts_ms = int(time.time() * 1000)
    result_path = f"{RESULT_DIR}/result_{task_id}_{ts_ms}.json"
    uploaded = gh_put(
        result_path,
        json.dumps(result, ensure_ascii=False, indent=2),
        f"[Bridge] Result for task {task_id} — {status}"
    )

    if uploaded:
        log("✓", f"Result uploaded → {result_path}")
    else:
        log("✗", "Failed to upload result!")

    cleared = gh_put(TASK_FILE, "waiting", f"[Bridge] Task {task_id} processed")
    if cleared:
        log("✓", "Inbox cleared — ready for next task.")
    else:
        log("!", "Could not clear inbox. Next run may re-execute same task.")

    sys.exit(0 if rc == 0 else 1)

if __name__ == "__main__":
    main()

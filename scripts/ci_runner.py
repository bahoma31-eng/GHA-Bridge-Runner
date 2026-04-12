#!/usr/bin/env python3
"""
scripts/ci_runner.py
~~~~~~~~~~~~~~~~~~~~
Code Interpreter Loop Runner — GHA-Bridge-Runner V3.1

مزايا جديدة:
  - Code Interpreter (Python sandbox) كمحرك افتراضي
  - تناوب النماذج اللغوية لتوفير التوكنز (round-robin)
  - حد أقصى 5 دورات لكل تشغيل
  - ملف لوج مفصّل يُرفع تلقائيًا إلى logs/ في المستودع

تدفق التنفيذ:
  1. تقرأ المهمة من inbox/ci_task.json
  2. تُنفّذ الكود داخل python -c مع capture كامل
  3. دورة بعد دورة حتى الانتهاء أو بلوغ 5 دورات
  4. ترفع النتيجة إلى outbox/ واللوج إلى logs/
  5. تمسح الـ inbox استعدادًا للمهمة القادمة
"""

import os, sys, json, time, base64, subprocess, shutil, textwrap
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("[ERROR] requests not installed. Run: pip install requests")
    sys.exit(1)

# ─── Config ────────────────────────────────────────────────────────────────
GH_TOKEN        = os.environ.get("GH_TOKEN", "")
REPO_OWNER      = os.environ.get("REPO_OWNER", "")
REPO_NAME       = os.environ.get("REPO_NAME", "")
BRANCH          = os.environ.get("BRANCH_NAME", "main")
TASK_FILE       = os.environ.get("CI_TASK_FILE", "inbox/ci_task.json")
RESULT_DIR      = os.environ.get("RESULT_DIR", "outbox")
LOG_DIR         = os.environ.get("LOG_DIR", "logs")
MAX_CYCLES      = int(os.environ.get("MAX_CYCLES", "5"))
CMD_TIMEOUT     = int(os.environ.get("COMMAND_TIMEOUT", "120"))

# نماذج للتناوب — يُستخدم الاسم في اللوج فقط (لا API calls هنا)
# المقصود: إذا كان لديك خطوات متعددة تستدعي LLM خارجيًا،
# كل دورة تُسجّل أي "نموذج" يُفترض استخدامه لتوزيع الحمل
MODEL_ROTATION = [
    "gpt-4o-mini",       # دورة 1
    "claude-haiku-3-5",  # دورة 2
    "gpt-4o-mini",       # دورة 3
    "claude-haiku-3-5",  # دورة 4
    "gpt-4o-mini",       # دورة 5
]

API_BASE = "https://api.github.com"

# ─── Logging ────────────────────────────────────────────────────────────────
log_lines = []   # يُجمع هنا كل شيء لرفعه لاحقًا

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(icon, msg, cycle=None):
    prefix = f"[CYC{cycle}]" if cycle else "      "
    line   = f"[{ts()}] {prefix} {icon}  {msg}"
    print(line, flush=True)
    log_lines.append(line)

def divider(title=""):
    sep = "=" * 70
    lines = [f"\n{sep}"]
    if title:
        lines.append(f"  {title}")
        lines.append("-" * 70)
    for l in lines:
        print(l, flush=True)
        log_lines.append(l)

# ─── GitHub API helpers ─────────────────────────────────────────────────────
def _hdrs():
    return {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "GHA-CI-Runner-V31",
    }

def _url(path):
    return f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"

def gh_get(path):
    r = requests.get(_url(path), headers=_hdrs(), timeout=20)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d["content"]).decode("utf-8", errors="replace"), d.get("sha")
    if r.status_code != 404:
        log("!", f"GET {path} => HTTP {r.status_code}")
    return None, None

def gh_put(path, text, msg):
    url = _url(path)
    r0  = requests.get(url, headers=_hdrs(), timeout=20)
    sha = r0.json().get("sha") if r0.status_code == 200 else None
    payload = {
        "message": msg,
        "content": base64.b64encode(text.encode()).decode(),
        "branch" : BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=_hdrs(), json=payload, timeout=30)
    return r.status_code in (200, 201)

# ─── Execution engine ───────────────────────────────────────────────────────
def execute_python(code, timeout):
    """ينفّذ كود Python ويُعيد (exit_code, stdout+stderr)"""
    py = shutil.which("python3") or shutil.which("python")
    if not py:
        return 1, "Python not found on runner."
    try:
        p = subprocess.run(
            [py, "-c", code],
            capture_output=True, text=True, timeout=timeout
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        combined = out + ("\n\n[STDERR]\n" + err if err else "")
        return p.returncode, combined.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return 124, f"Timeout after {timeout}s"
    except Exception as e:
        return 1, f"Execution error: {e}"

# ─── Main loop ──────────────────────────────────────────────────────────────
def main():
    divider("GHA Code Interpreter Loop Runner V3.1")
    log("i", f"Repo     : {REPO_OWNER}/{REPO_NAME} [{BRANCH}]")
    log("i", f"Task     : {TASK_FILE}")
    log("i", f"Max Cycles: {MAX_CYCLES}")
    log("i", f"Timeout  : {CMD_TIMEOUT}s per cycle")
    log("i", f"Models   : {' → '.join(MODEL_ROTATION[:MAX_CYCLES])}")

    if not GH_TOKEN:
        log("✗", "GH_TOKEN is missing! Set it in repo Secrets.")
        sys.exit(1)

    # ── قراءة المهمة ─────────────────────────────────────────────────────
    divider("Reading task from inbox")
    raw, _ = gh_get(TASK_FILE)

    if not raw or raw.strip().lower() in ("waiting", "null", "", "{}"):
        log("i", "No pending CI task. Exiting cleanly.")
        sys.exit(0)

    try:
        task = json.loads(raw)
    except json.JSONDecodeError as e:
        log("✗", f"Invalid JSON in {TASK_FILE}: {e}")
        sys.exit(1)

    task_id     = str(task.get("task_id",     f"ci_{int(time.time())}"))
    description = task.get("description",     "No description")
    steps       = task.get("steps",           [])   # قائمة من أكواد Python
    # دعم الأكواد المدرجة مباشرة بدون steps
    if not steps and task.get("code"):
        steps = [task["code"]]

    log("i", f"Task ID  : {task_id}")
    log("i", f"Desc     : {description}")
    log("i", f"Steps    : {len(steps)} (max cycles = {MAX_CYCLES})")

    # ── حلقة التنفيذ ──────────────────────────────────────────────────────
    divider("Execution Loop")
    cycle_results = []
    final_status  = "SUCCESS"
    final_rc      = 0

    for i, code in enumerate(steps[:MAX_CYCLES], start=1):
        model = MODEL_ROTATION[(i - 1) % len(MODEL_ROTATION)]
        divider(f"Cycle {i}/{min(len(steps), MAX_CYCLES)}  |  Model slot: {model}")

        log(">>", f"Executing step {i}...", cycle=i)
        log("🤖", f"LLM model slot: {model}", cycle=i)

        # عرض preview
        preview = textwrap.shorten(code, width=120, placeholder=" ...")
        log("💻", f"Code preview: {preview}", cycle=i)

        t0 = time.time()
        rc, output = execute_python(code, CMD_TIMEOUT)
        elapsed = round(time.time() - t0, 2)

        status_i = "SUCCESS" if rc == 0 else "FAILED"
        log("<<", f"Done in {elapsed}s — {status_i} (rc={rc})", cycle=i)

        # عرض output
        out_lines = output.splitlines()[:20]
        for l in out_lines:
            log("  ", l, cycle=i)
        if len(output.splitlines()) > 20:
            log("  ", f"... ({len(output.splitlines())} lines total, truncated)", cycle=i)

        cycle_results.append({
            "cycle"       : i,
            "model_slot"  : model,
            "status"      : status_i,
            "return_code" : rc,
            "elapsed_s"   : elapsed,
            "output"      : output[:4000],
        })

        if rc != 0:
            final_status = "FAILED"
            final_rc     = rc
            log("!", f"Step {i} failed — continuing to next step (unless last).", cycle=i)

    if len(steps) > MAX_CYCLES:
        log("!", f"⚠️  {len(steps) - MAX_CYCLES} step(s) skipped (MAX_CYCLES={MAX_CYCLES} reached).")

    # ── تجميع النتيجة ─────────────────────────────────────────────────────
    divider("Uploading Result")
    ts_ms = int(time.time() * 1000)
    run_id = os.environ.get("GITHUB_RUN_ID", "unknown")

    result = {
        "task_id"       : task_id,
        "description"   : description,
        "status"        : final_status,
        "cycles_run"    : len(cycle_results),
        "cycles_max"    : MAX_CYCLES,
        "cycle_results" : cycle_results,
        "model_rotation": MODEL_ROTATION[:MAX_CYCLES],
        "timestamp"     : now_iso(),
        "executed_on"   : "github_actions_runner",
        "runner_os"     : os.environ.get("RUNNER_OS", "unknown"),
        "workflow_run"  : run_id,
        "repo"          : f"{REPO_OWNER}/{REPO_NAME}",
        "bridge_version": "3.1-ci",
    }

    result_path = f"{RESULT_DIR}/ci_result_{task_id}_{ts_ms}.json"
    ok = gh_put(
        result_path,
        json.dumps(result, ensure_ascii=False, indent=2),
        f"[CI] Result {task_id} — {final_status}"
    )
    log("✓" if ok else "✗", f"Result → {result_path}")

    # ── رفع ملف اللوج ─────────────────────────────────────────────────────
    divider("Uploading Log")
    log_text    = "\n".join(log_lines)
    log_path    = f"{LOG_DIR}/ci_log_{task_id}_{ts_ms}.txt"
    log_ok = gh_put(
        log_path,
        log_text,
        f"[CI] Log {task_id} — {final_status}"
    )
    log("✓" if log_ok else "✗", f"Log → {log_path}")

    # ── مسح الـ inbox ─────────────────────────────────────────────────────
    divider("Clearing Inbox")
    cleared = gh_put(TASK_FILE, "waiting", f"[CI] Task {task_id} processed")
    log("✓" if cleared else "!", "Inbox cleared." if cleared else "Could not clear inbox.")

    # ── ملخص ──────────────────────────────────────────────────────────────
    divider("Summary")
    log("📊", f"Task       : {task_id}")
    log("📊", f"Status     : {final_status}")
    log("📊", f"Cycles run : {len(cycle_results)} / {MAX_CYCLES}")
    log("📊", f"Result     : {result_path}")
    log("📊", f"Log        : {log_path}")
    for cr in cycle_results:
        icon = "✅" if cr["status"] == "SUCCESS" else "❌"
        log(icon, f"  Cycle {cr['cycle']} [{cr['model_slot']}] — {cr['status']} ({cr['elapsed_s']}s)")

    sys.exit(0 if final_rc == 0 else 1)

if __name__ == "__main__":
    main()

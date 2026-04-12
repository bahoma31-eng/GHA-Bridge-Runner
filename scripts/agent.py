#!/usr/bin/env python3
"""
GHA Smart Agent — ReAct Loop (Observe→Think→Act→Watch)
Models: Groq / Gemini (alternating to save tokens)
Max cycles: 5
Logging: logs/agent_<issue>_<run>.md
"""

import os, sys, json, textwrap, traceback, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path
import requests

# ── env ────────────────────────────────────────────────────
GH_TOKEN        = os.environ["GH_TOKEN"]
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
REPO            = os.environ["REPO_FULL_NAME"]
ISSUE_NUMBER    = int(os.environ["ISSUE_NUMBER"])
ISSUE_TITLE     = os.environ.get("ISSUE_TITLE", "")
ISSUE_BODY      = os.environ.get("ISSUE_BODY", "")
COMMENT_BODY    = os.environ.get("COMMENT_BODY", "")
EVENT_NAME      = os.environ.get("EVENT_NAME", "issues")
RUN_ID          = os.environ.get("RUN_ID", "0")

MAX_CYCLES      = 5
GH_API          = "https://api.github.com"
GH_HEADERS      = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── logging ────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
LOG_FILE = Path(f"logs/agent_issue{ISSUE_NUMBER}_run{RUN_ID}.md")
_log_lines = []

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)

def flush_log():
    header = textwrap.dedent(f"""
    # Agent Log — Issue #{ISSUE_NUMBER}
    - **Run ID:** {RUN_ID}
    - **Repo:** {REPO}
    - **Event:** {EVENT_NAME}
    - **Date:** {datetime.now(timezone.utc).isoformat()}

    ---
    """).strip()
    LOG_FILE.write_text(header + "\n\n" + "\n".join(_log_lines), encoding="utf-8")

# ── GitHub helpers ──────────────────────────────────────────
def gh_comment(body: str):
    url = f"{GH_API}/repos/{REPO}/issues/{ISSUE_NUMBER}/comments"
    r = requests.post(url, headers=GH_HEADERS, json={"body": body}, timeout=30)
    r.raise_for_status()
    log(f"Comment posted: {r.json()['html_url']}")

def gh_set_label(label: str):
    url = f"{GH_API}/repos/{REPO}/issues/{ISSUE_NUMBER}/labels"
    requests.post(f"{GH_API}/repos/{REPO}/labels",
                  headers=GH_HEADERS,
                  json={"name": label, "color": "0075ca"},
                  timeout=10)
    r = requests.post(url, headers=GH_HEADERS, json={"labels": [label]}, timeout=15)
    r.raise_for_status()
    log(f"Label set: {label}")

def gh_remove_label(label: str):
    url = f"{GH_API}/repos/{REPO}/issues/{ISSUE_NUMBER}/labels/{requests.utils.quote(label)}"
    requests.delete(url, headers=GH_HEADERS, timeout=10)

def gh_api_call(method: str, endpoint: str, payload: dict = None) -> dict:
    url = endpoint if endpoint.startswith("http") else f"{GH_API}{endpoint}"
    fn  = getattr(requests, method.lower())
    r   = fn(url, headers=GH_HEADERS, json=payload, timeout=30)
    return {"status": r.status_code, "body": r.json() if r.content else {}}

# ── Code execution ──────────────────────────────────────────
def execute_code(code: str, language: str = "python") -> dict:
    """Execute Python or Bash code and return stdout/stderr/returncode."""
    try:
        if language.lower() in ("python", "py"):
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w",
                                             delete=False, encoding="utf-8") as f:
                f.write(code)
                fname = f.name
            result = subprocess.run(
                [sys.executable, fname],
                capture_output=True, text=True, timeout=60
            )
            os.unlink(fname)
        else:
            result = subprocess.run(
                code, shell=True, capture_output=True, text=True, timeout=60,
                executable="/bin/bash"
            )
        return {
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Timeout (60s)", "returncode": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}

def http_request(method: str, url: str, headers: dict = None, payload=None) -> dict:
    try:
        r = requests.request(method.upper(), url,
                             headers=headers or {}, json=payload, timeout=30)
        try:    body = r.json()
        except: body = r.text[:2000]
        return {"status": r.status_code, "body": body, "success": r.ok}
    except Exception as e:
        return {"status": 0, "body": str(e), "success": False}

# ── LLM layer ───────────────────────────────────────────────
GROQ_MODELS   = ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"]
GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash"]

_model_turn = 0  # 0=groq, 1=gemini, alternating

def call_llm(messages: list) -> str:
    global _model_turn
    provider = "groq" if _model_turn % 2 == 0 else "gemini"
    _model_turn += 1
    log(f"Calling LLM provider: {provider}")

    if provider == "groq" and GROQ_API_KEY:
        return _call_groq(messages)
    elif provider == "gemini" and GEMINI_API_KEY:
        return _call_gemini(messages)
    elif GROQ_API_KEY:
        return _call_groq(messages)
    elif GEMINI_API_KEY:
        return _call_gemini(messages)
    else:
        raise RuntimeError("No LLM API key available.")

def _call_groq(messages: list) -> str:
    import groq as groq_lib
    client = groq_lib.Groq(api_key=GROQ_API_KEY)
    for model in GROQ_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0.2, max_tokens=2048
            )
            return resp.choices[0].message.content
        except Exception as e:
            log(f"Groq model {model} failed: {e}", "WARN")
    raise RuntimeError("All Groq models failed.")

def _call_gemini(messages: list) -> str:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    history = []
    for m in messages:
        if m["role"] == "user":
            history.append({"role": "user", "parts": [m["content"]]})
        elif m["role"] == "assistant":
            history.append({"role": "model", "parts": [m["content"]]})
    system_txt = "\n".join(system_parts)
    for model_name in GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(model_name,
                                          system_instruction=system_txt or None)
            chat  = model.start_chat(history=history[:-1] if history else [])
            last  = history[-1]["parts"][0] if history else "hello"
            resp  = chat.send_message(last)
            return resp.text
        except Exception as e:
            log(f"Gemini model {model_name} failed: {e}", "WARN")
    raise RuntimeError("All Gemini models failed.")

# ── System prompt ────────────────────────────────────────────
SYSTEM_PROMPT = """You are a powerful autonomous agent running inside GitHub Actions.
You operate in a strict ReAct loop: Observe → Think → Act → Watch → repeat.

## Your output format (STRICT JSON only, no markdown fences):
{
  "thought": "your reasoning about the current state",
  "action": "execute_code | gh_comment | gh_set_label | gh_api | http_request | finish | fail",
  "action_input": { ... }
}

## Actions:
- execute_code: {"code": "...", "language": "python|bash"}
- gh_comment: {"body": "markdown comment text"}
- gh_set_label: {"label": "label-name"}
- gh_api: {"method": "GET|POST|PATCH|DELETE", "endpoint": "/repos/...", "payload": {...}}
- http_request: {"method": "GET|POST", "url": "https://...", "headers": {}, "payload": {}}
- finish: {"summary": "what was accomplished"}
- fail: {"reason": "why it failed"}

## Rules:
1. Always start by understanding the full task from context.
2. Write clean, safe code. Never delete repo contents.
3. Post progress comments so the user sees what you are doing.
4. Log every observation. Think before acting.
5. If an action fails, adapt and try a different approach.
6. Max 5 cycles — be efficient.
"""

# ── Main ReAct loop ──────────────────────────────────────────
def build_task_description() -> str:
    if EVENT_NAME == "issue_comment" and COMMENT_BODY.strip():
        return f"Issue Title: {ISSUE_TITLE}\n\nIssue Body:\n{ISSUE_BODY}\n\nNew Comment (trigger):\n{COMMENT_BODY}"
    return f"Issue Title: {ISSUE_TITLE}\n\nIssue Body:\n{ISSUE_BODY}"

def run_agent():
    task = build_task_description()
    log(f"Starting agent for Issue #{ISSUE_NUMBER}")
    log(f"Task:\n{task[:500]}")

    try: gh_set_label("\U0001f916 agent-running")
    except: pass

    gh_comment(f"## \U0001f916 Agent Started\n\n**Task received.** Starting ReAct loop...\n\n```\n{task[:300]}\n```\n\n_Max cycles: {MAX_CYCLES}_")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": f"## Task\n{task}"},
    ]

    cycle = 0
    final_status = "unknown"

    while cycle < MAX_CYCLES:
        cycle += 1
        log(f"── Cycle {cycle}/{MAX_CYCLES} ──────────────────────")

        try:
            raw_response = call_llm(messages)
        except Exception as e:
            log(f"LLM call failed: {e}", "ERROR")
            gh_comment(f"\u274c **LLM Error on cycle {cycle}:** {e}")
            final_status = "fail"
            break

        log(f"LLM response:\n{raw_response[:800]}")

        try:
            clean = raw_response.strip()
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:])
                clean = clean.rsplit("```", 1)[0].strip()
            decision = json.loads(clean)
        except json.JSONDecodeError:
            log(f"JSON parse error. Raw: {raw_response[:300]}", "WARN")
            messages.append({"role": "assistant", "content": raw_response})
            messages.append({"role": "user", "content": "Your response was not valid JSON. Reply with pure JSON only, no markdown."})
            continue

        thought = decision.get("thought", "")
        action  = decision.get("action", "")
        ainput  = decision.get("action_input", {})

        log(f"Thought: {thought[:300]}")
        log(f"Action: {action}")

        observation = {}

        if action == "finish":
            summary = ainput.get("summary", "Task completed.")
            gh_comment(f"## \u2705 Task Completed\n\n{summary}\n\n_Cycles used: {cycle}/{MAX_CYCLES}_")
            try:
                gh_set_label("\u2705 agent-done")
                gh_remove_label("\U0001f916 agent-running")
            except: pass
            final_status = "success"
            log("Agent finished successfully.")
            break

        elif action == "fail":
            reason = ainput.get("reason", "Unknown failure.")
            gh_comment(f"## \u274c Agent Failed\n\n**Reason:** {reason}\n\n_Cycles used: {cycle}/{MAX_CYCLES}_")
            try:
                gh_set_label("\u274c agent-failed")
                gh_remove_label("\U0001f916 agent-running")
            except: pass
            final_status = "fail"
            log(f"Agent failed: {reason}", "ERROR")
            break

        elif action == "execute_code":
            code     = ainput.get("code", "")
            language = ainput.get("language", "python")
            log(f"Executing {language} code ({len(code)} chars)")
            result = execute_code(code, language)
            observation = result
            status_icon = "\u2705" if result["success"] else "\u26a0\ufe0f"
            snippet = (result["stdout"] or result["stderr"] or "no output")[:1000]
            gh_comment(
                f"### {status_icon} Cycle {cycle} \u2014 Code Execution ({language})\n\n"
                f"**Return code:** `{result['returncode']}`\n\n"
                f"```\n{snippet}\n```"
            )

        elif action == "gh_comment":
            body = ainput.get("body", "")
            gh_comment(body)
            observation = {"posted": True}

        elif action == "gh_set_label":
            label = ainput.get("label", "")
            gh_set_label(label)
            observation = {"label_set": label}

        elif action == "gh_api":
            method   = ainput.get("method", "GET")
            endpoint = ainput.get("endpoint", "")
            payload  = ainput.get("payload")
            result   = gh_api_call(method, endpoint, payload)
            observation = result

        elif action == "http_request":
            result = http_request(
                ainput.get("method", "GET"),
                ainput.get("url", ""),
                ainput.get("headers"),
                ainput.get("payload"),
            )
            observation = result

        else:
            observation = {"error": f"Unknown action: {action}"}
            log(f"Unknown action: {action}", "WARN")

        log(f"Observation: {str(observation)[:400]}")

        messages.append({"role": "assistant", "content": raw_response})
        messages.append({
            "role": "user",
            "content": f"## Observation (Cycle {cycle})\n```json\n{json.dumps(observation, ensure_ascii=False, indent=2)[:2000]}\n```\nContinue. Cycles remaining: {MAX_CYCLES - cycle}"
        })

    else:
        gh_comment(
            f"## \u23f1\ufe0f Max Cycles Reached ({MAX_CYCLES})\n\n"
            "The agent reached the maximum number of cycles without finishing. "
            "Please review the logs or re-open with more specific instructions."
        )
        try:
            gh_set_label("\u23f1\ufe0f agent-timeout")
            gh_remove_label("\U0001f916 agent-running")
        except: pass
        final_status = "timeout"
        log("Max cycles reached.", "WARN")

    log(f"Agent finished. Status: {final_status}")
    flush_log()

# ── entry point ──────────────────────────────────────────────
if __name__ == "__main__":
    try:
        run_agent()
    except Exception as e:
        tb = traceback.format_exc()
        log(f"FATAL: {e}\n{tb}", "FATAL")
        flush_log()
        try:
            gh_comment(f"## \U0001f4a5 Agent Crashed\n\n```\n{tb[-1500:]}\n```")
        except: pass
        sys.exit(1)

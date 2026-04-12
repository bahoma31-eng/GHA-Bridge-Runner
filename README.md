# Cloud Bridge Runner V3.0 — GitHub Actions Native

يُنفّذ المهام داخل GitHub Actions Runner مباشرةً، بدون الحاجة لجهاز محلي أو وكيل خارجي.

## البنية

```
.github/
  workflows/
    bridge.yml          ← Workflow الرئيسي
scripts/
  bridge_runner.py      ← منطق تنفيذ المهام
inbox/
  local_task.json       ← ملف المهمة (اكتب هنا → trigger تلقائي)
outbox/
  result_<id>_<ts>.json ← نتائج التنفيذ
```

## الإعداد

1. أضف `GH_TOKEN` في **Settings → Secrets and Variables → Actions**
   - صلاحيات مطلوبة: `contents: write`

2. تأكد أن الـ workflow مُفعَّل في تبويب **Actions**

## طرق التشغيل

### أ) يدوي عبر GitHub UI
1. Actions → "🌉 Bridge Runner" → Run workflow
2. الصق JSON المهمة في حقل `task_json`

### ب) تلقائي عبر push
اكتب المهمة في `inbox/local_task.json` وادفع إلى main:
```bash
echo '{"task_id":"t1","engine":"PYTHON","command":"print(2+2)","description":"Test"}' > inbox/local_task.json
git add inbox/local_task.json && git commit -m "New task" && git push
```

### ج) عبر GitHub API
```bash
curl -X POST \
  -H "Authorization: token YOUR_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/bahoma31-eng/GHA-Bridge-Runner/actions/workflows/bridge.yml/dispatches" \
  -d '{"ref":"main","inputs":{"task_json":"{\"task_id\":\"api1\",\"engine\":\"BASH\",\"command\":\"echo Hello\",\"description\":\"API task\"}"}}'
```

## صيغة المهمة

```json
{
  "task_id": "my_task_001",
  "engine": "PYTHON",
  "command": "import platform; print(platform.version())",
  "description": "اطبع معلومات النظام",
  "timeout": 120
}
```

### المحركات المتاحة

| Engine     | المتطلبات                     |
|------------|-------------------------------|
| PYTHON     | Python 3 (متاح افتراضيًا)     |
| BASH       | Bash (متاح افتراضيًا)         |
| CMD        | يُنفَّذ عبر bash (Linux)       |
| NODEJS     | Node.js (متاح افتراضيًا)      |
| POWERSHELL | pwsh (يحتاج خطوة تثبيت إضافية)|

## قراءة النتائج

النتائج تُرفع تلقائيًا إلى `outbox/` بصيغة:
```json
{
  "task_id": "my_task_001",
  "status": "SUCCESS",
  "return_code": 0,
  "output": "...",
  "timestamp": "2026-04-12T19:00:00Z",
  "executed_on": "github_actions_runner",
  "runner_os": "Linux",
  "workflow_run": "12345678"
}
```

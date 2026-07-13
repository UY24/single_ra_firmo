# Adaptive Slack Cost Precision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render genuine sub-cent Slack notification costs without rounding them to `$0.00`, while retaining compact two-decimal formatting for ordinary USD amounts.

**Architecture:** Add one private formatter to `app.core.notify` and route every Slack cost string through it. Verify behavior through the public `notify_run_complete` block payload so the tests cover the user-visible notification.

**Tech Stack:** Python 3.12, `unittest`, Slack Block Kit payload dictionaries

---

### Task 1: Format Slack costs adaptively

**Files:**
- Modify: `backend/tests/test_serpwow_notify.py`
- Modify: `backend/app/core/notify.py`

- [ ] **Step 1: Write the failing rendering assertions**

Change `NotifyRenderTests.test_serpwow_split_costs_rendered` to require the exact sub-cent values:

```python
serp = next(v for k, v in fields.items() if "SerpWow searches" in k)
self.assertIn("3 searches", serp)
self.assertIn("$0.00105", serp)
cost = next(v for k, v in fields.items() if "Cost" in k)
self.assertIn("LLM $0.0002", cost)
self.assertIn("Total $0.00125", cost)
```

Keep `test_ai_mode_shaped_call_unchanged` asserting `$1.23`, proving normal amounts remain two-decimal values.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd backend && ../.venv/bin/python -m unittest tests.test_serpwow_notify.NotifyRenderTests.test_serpwow_split_costs_rendered -v
```

Expected: FAIL because the current payload contains `$0.00` instead of `$0.00105`, `$0.0002`, and `$0.00125`.

- [ ] **Step 3: Add the minimal adaptive formatter**

Add this private helper near the existing formatting helpers in `backend/app/core/notify.py`:

```python
def _fmt_usd(value: int | float) -> str:
    amount = float(value)
    if 0 < abs(amount) < 0.01:
        return f"${amount:,.6f}".rstrip("0").rstrip(".")
    return f"${amount:,.2f}"
```

Use `_fmt_usd` for `serpwow_cost_usd`, `llm_cost_usd`, and `cost_usd` in both split and legacy rendering paths. Do not change notification parameters or block structure.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
cd backend && ../.venv/bin/python -m unittest tests.test_notify tests.test_serpwow_notify -v
```

Expected: all notification tests pass.

- [ ] **Step 5: Run the complete regression suite**

Run:

```bash
cd backend && ../.venv/bin/python -m unittest discover -s tests -t .
```

Expected: all tests pass.

- [ ] **Step 6: Commit the notification change separately**

```bash
git add backend/app/core/notify.py backend/tests/test_serpwow_notify.py
git commit -m "fix: preserve sub-cent Slack costs"
```

# Adaptive Slack Cost Precision

## Goal

Keep ordinary USD amounts compact while preventing real sub-cent LLM and SerpWow costs from rendering as `$0.00` in Slack notifications.

## Design

Add one private USD formatter in `app.core.notify` and use it for the SerpWow, LLM, total, and legacy cost displays.

- Amounts of at least one cent render with two decimal places, such as `$1.23`.
- Positive amounts below one cent render with up to six decimal places and trailing zeros removed, such as `$0.00105` or `$0.0002`.
- Zero renders as `$0.00`.
- Existing notification parameters, block structure, and AI Mode behavior remain unchanged.

## Verification

Update notification rendering tests to assert exact small-cost values, verify ordinary costs remain two-decimal values, and run both notification test modules.

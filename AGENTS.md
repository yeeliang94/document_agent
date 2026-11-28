# Working on Document Agent

- **No hidden settings.** Every feature switch and limit is a Settings value (`app/app_settings.py`) with a control on the Settings page (`web/pages.js`, `settingsPage`). `tests/test_settings_controls.py` fails when one has no control. `.env` holds only secrets and the endpoint.
- **Screens start as a prototype** that the owner approves before the app code changes.
- **Plans** live in `docs/`; the current one is `docs/PLAN-IMPROVEMENTS.md`. Mark a step done only after its Verify line has been checked.

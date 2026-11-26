"""No hidden settings: every option the server keeps has a control on the Settings page.

The page is plain JavaScript, so the test reads web/pages.js and looks for each option drawn by one of the
page's control helpers inside settingsPage().
"""

import re
from pathlib import Path

from app.app_settings import OPTIONS

PAGES = Path(__file__).resolve().parents[1] / 'web' / 'pages.js'
CONTROL = re.compile(r"\b(?:steps|number|onOff|choice|field)\('([a-z_]+)'")


def settings_page() -> str:
    text = PAGES.read_text(encoding='utf-8')
    start = text.index('export function settingsPage(')
    end = text.index('\n}\n', start)
    return text[start:end]


def drawn(page: str) -> set[str]:
    return set(CONTROL.findall(page))


def test_every_setting_has_a_control():
    missing = sorted(set(OPTIONS) - drawn(settings_page()))
    assert not missing, f'Settings with no control on the Settings page: {", ".join(missing)}. Add one in web/pages.js.'


def test_every_control_is_a_real_setting():
    unknown = sorted(drawn(settings_page()) - set(OPTIONS))
    assert not unknown, f'Controls for settings the server does not keep: {", ".join(unknown)}.'


def test_the_check_notices_a_missing_control():
    page = settings_page().replace("onOff('model_on_create'", "onOff('gone'")
    assert 'model_on_create' in set(OPTIONS) - drawn(page)

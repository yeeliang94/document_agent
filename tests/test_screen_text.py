"""Screen text: facts on a screen are joined in plain English, never with a middle dot ("3 skills · version 4").

The owner reads the dot as a sign of a machine-made interface, so the app uses a comma between facts, a colon where
a label introduces text, and a new line where the facts are different things. The screens are plain files in web/
and the server's messages come from app/, so the test reads both.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOTS = ('·', '&middot;', '&#183;', '\\u00b7', '\\00b7')


def test_no_middle_dot_in_any_screen_or_server_message():
    found = []
    for folder, kinds in (('web', ('*.js', '*.html', '*.css')), ('app', ('*.py',))):
        for kind in kinds:
            for path in sorted((ROOT / folder).rglob(kind)):
                for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                    if any(dot in line for dot in DOTS):
                        found.append(f'{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}')
    assert not found, 'Join facts with a comma, a colon or a new line, not a middle dot:\n' + '\n'.join(found)

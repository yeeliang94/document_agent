"""Playbooks: the named instructions the agent follows on every run of one kind of work.

A playbook's instructions are kept as versions, with an activity log, the way app/versioned.py keeps any named
text. A playbook also has skills, and the first playbooks are made from the built-in instructions.
"""

from __future__ import annotations

from .cleaning import now
from .modules import DEFAULT_CONTEXT, MODULE_LABELS
from .skills import Skills
from .versioned import MAX_NAME, MAX_TEXT, SOURCES, StaleVersion, Versioned  # noqa: F401 - imported from here too


class Playbooks(Versioned):
    kind, label, text = 'playbook', 'playbook', 'instructions'

    def __init__(self, store):
        # Called with (playbook id, version, source) after each saved version, so the tests kept for the playbook
        # can be reviewed and replayed (app/runtime.py).
        super().__init__(store)
        with self.connect() as c:
            # The first playbooks, made once. A database from before playbooks kept its instructions in a
            # `profiles` table; the text edited there is carried over, and that table is left as it was.
            first = dict(DEFAULT_CONTEXT)
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone():
                first.update(c.execute('SELECT module, context FROM profiles ORDER BY rowid').fetchall())
            for module, context in first.items():
                made = c.execute(
                    'INSERT OR IGNORE INTO playbooks VALUES (?,?,?)', (module, MODULE_LABELS.get(module, module), now())
                ).rowcount
                if made:
                    c.execute(
                        'INSERT INTO playbook_versions VALUES (?,?,?,?,?,?,?)',
                        (module, 1, now(), 'created', '', context, '{}'),
                    )
        self.skills = Skills(self, store.root)

    def create(self, name: str, copy_of: str | None = None) -> dict:
        """A blank playbook, or one that starts from another's latest instructions and skills."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            name = self._name(c, name)
            source = self._latest(c, copy_of) if copy_of else None
            detail = {'copied_from': copy_of, 'copied_version': source['version']} if source else {}
            playbook_id = self._insert(c, name, source['text'] if source else '', detail)
        self.event(playbook_id, 'playbook.created', f'Created "{name}"', version=1, **detail)
        if copy_of:
            try:
                self.skills.copy(copy_of, playbook_id)
            except Exception:
                # A copy that cannot bring its skills along is not left behind half made.
                self.skills.discard(playbook_id)
                with self.connect() as c:
                    for table in ('playbook_versions', 'playbook_events'):
                        c.execute(f'DELETE FROM {table} WHERE playbook_id=?', (playbook_id,))
                    c.execute('DELETE FROM playbooks WHERE id=?', (playbook_id,))
                self.event(copy_of, 'playbook.copy_failed', f'A copy named "{name}" could not be made', 'WARN')
                raise
        return self.get(playbook_id)

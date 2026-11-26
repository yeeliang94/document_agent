"""Clients: one set of notes per client, covering all their work, that a run follows on top of its playbook.

The notes are kept as versions with Undo, the way a playbook's instructions are (app/versioned.py). A run copies
the notes when it is created, so a later edit never changes it. Where the notes and the playbook disagree, the
client wins. Client folders follow no fixed pattern, so suggest() looks for a known client's name anywhere in the
folder and file names; the app suggests and never attaches a client on its own.
"""

from __future__ import annotations

import re

from .versioned import Versioned

# Words that say what kind of company a name is, not which one: "ACME Sdn Bhd" is the client "Acme".
LEGAL = frozenset(
    'sdn bhd berhad ltd limited inc incorporated llc llp plc pte pty co corp corporation company group gmbh ag sa '
    'bv nv the and of'.split()
)


def words(text: str) -> list[str]:
    """Lower-case words and numbers, in any script; "acme_oct.xlsx" gives acme, oct, xlsx."""
    return [w for w in re.findall(r'[^\W_]+', (text or '').casefold())]


def _key(name: str) -> list[str]:
    found = words(name.replace('&', ' and '))
    return [w for w in found if w not in LEGAL] or found


def mentions(name: str, text: str) -> bool:
    """True when text names the client: its name's words in a row, or run together ("BlueRiver")."""
    key, said = _key(name), words(text)
    if not key:
        return False
    if any(said[i : i + len(key)] == key for i in range(len(said) - len(key) + 1)):
        return True
    return len(key) > 1 and ''.join(key) in said


class Clients(Versioned):
    kind, label, text = 'client', 'client', 'notes'

    def create(self, name: str, notes: str = '') -> dict:
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            name = self._name(c, name)
            client_id = self._insert(c, name, notes.strip())
        self.event(client_id, 'client.created', f'Created "{name}"', version=1)
        return self.get(client_id)

    def suggest(self, paths: list[str]) -> dict | None:
        """The one known client the chosen folder and file names point to, or None when none or several do. A
        client named in more of the paths wins; a tie is no suggestion, since a wrong one would apply one
        client's rules to another."""
        counts = {}
        for client in self.list():
            hits = sum(mentions(client['name'], path) for path in paths)
            if hits:
                counts[client['id']] = (hits, len(_key(client['name'])), client)
        if not counts:
            return None
        ranked = sorted(counts.values(), key=lambda c: (c[0], c[1]), reverse=True)
        if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
            return None
        return ranked[0][2]


SCOPES = ('playbook', 'client')  # where a remembered comment or a lesson is kept


def where(store, job: dict, scope: str):
    """Where text of a scope is kept for this job: (store.playbooks or store.clients, the id), or None when the job
    has no such place (a run with no client). A job from before playbooks names its playbook by its module."""
    if scope == 'client':
        client_id = (job.get('client') or {}).get('id')
        return (store.clients, client_id) if client_id else None
    playbook_id = (job.get('playbook') or {}).get('id') or job.get('module') or ''
    return (store.playbooks, playbook_id) if playbook_id else None


NO = re.compile(r'^\W*(no|not|nope|neither|wrong)\b', re.IGNORECASE)
YES = re.compile(r'^\W*(yes|yeah|yep|correct|right|confirmed|it is|that is)\b', re.IGNORECASE)


def confirmed(name: str, answer: str) -> bool:
    """True when the user's answer says yes to this client: it starts with a yes, or names the client and does not
    start with a no. Anything else (a no, another client, a question back) is not a yes."""
    answer = answer or ''
    return not NO.match(answer) and bool(YES.match(answer) or mentions(name, answer))


def follow(store, job_id: str, name: str) -> dict:
    """The agent's follow_client tool, for a run started with no client: attach the client the user confirmed.
    Refused unless the user answered yes to a question that names that client, so a client is never attached
    without the person saying so. Returns {"reply": words for the agent, "client": the client attached or None}."""
    job = store.get(job_id)
    refused = lambda reply: {'reply': reply, 'client': None}  # noqa: E731
    if job.get('client'):
        return refused(f'This run already follows the notes for {job["client"]["name"]}.')
    known = store.clients.list()
    client = next((c for c in known if c['name'].casefold() == ' '.join(name.split()).casefold()), None)
    client = client or next((c for c in known if mentions(c['name'], name)), None)
    if not client:
        listed = ', '.join(c['name'] for c in known) or 'none'
        return refused(f'There is no client "{name}". The known clients are: {listed}.')
    asked = [q for q in job.get('engine_questions') or [] if mentions(client['name'], q['question'])]
    if not asked:
        return refused(f'Ask the user with ask_user whether this run is for {client["name"]} first, and wait for the answer.')  # fmt: skip
    if not confirmed(client['name'], asked[-1]['answer']):
        return refused(
            f'The user answered "{asked[-1]["answer"]}", which does not say this run is for {client["name"]}. '
            'Carry on without client notes.'
        )
    client = store.clients.get(client['id'])
    following = {'id': client['id'], 'name': client['name'], 'version': client['version'], 'chosen_by': 'answer'}
    taken = {}

    def attach(j):
        if j.get('client'):  # attached by a call that got here first
            return
        taken['yes'] = True
        # Earlier runs of another client are no longer examples for this run (see service.choose_past_runs).
        runs = [r for r in j.get('past_runs') or [] if r.get('client') in (None, client['name'])]
        j.update(client=following, client_notes=client['text'], past_runs=runs)

    store.mutate(job_id, attach)
    if not taken:
        return refused('This run already follows the notes of a client.')
    store.event(job_id, 'client.followed', f'Following the notes for {client["name"]}, as you answered',
                type='job', client=client['id'], version=client['version'])  # fmt: skip
    reply = (
        f'This run now follows the notes for {client["name"]}. They apply the way the house rules do; where they '
        'and the house rules disagree, follow the client notes and say so in notes.md.\n\n'
        + (client['text'].strip() or '(No notes yet.)')
    )
    return {'reply': reply, 'client': {'name': client['name'], 'notes': client['text']}}

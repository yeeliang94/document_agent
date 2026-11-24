"""Skills: checking an uploaded ZIP, storing it per playbook, and the copy each job takes."""

import io
import shutil
import sqlite3
import stat
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.skills import MAX_FILES, MAX_SKILLS, MAX_ZIP_BYTES, SkillRefused, read_skill_zip
from app.store import Store

from helpers import LOCAL, workbook_bytes

SKILL = """---
name: payroll-proration
description: Work out part-month pay.
---
# Part-month pay

Pay calendar days worked over calendar days in the month.
"""


def skill_zip(files=None, **extra):
    """A ZIP of {path: text or bytes}; the default is one good skill."""
    files = {'SKILL.md': SKILL, **extra} if files is None else files
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return buffer.getvalue()


def named(name, description='Does a thing.'):
    return skill_zip({'SKILL.md': f'---\nname: {name}\ndescription: {description}\n---\nBody\n'})


def link_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('SKILL.md', SKILL)
        link = zipfile.ZipInfo('notes.md')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, '/etc/passwd')
    return buffer.getvalue()


def locked_zip():
    """A ZIP whose one entry is marked as password-protected."""
    data = bytearray(skill_zip())
    for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
        at = data.index(signature)
        data[at + offset] |= 0x1
    return bytes(data)


def damaged_zip():
    """A ZIP whose stored bytes no longer match their checksum."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        archive.writestr('SKILL.md', SKILL)
    data = bytearray(buffer.getvalue())
    at = data.index(b'Part-month')
    data[at : at + 4] = b'XXXX'
    return bytes(data)


def lying_zip():
    """A ZIP that declares a small size for a file that unpacks to more than the limit."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('SKILL.md', SKILL)
        archive.writestr('big.txt', 'x' * (MAX_ZIP_BYTES + 100))
    data = bytearray(buffer.getvalue())
    size = (MAX_ZIP_BYTES + 100).to_bytes(4, 'little')
    while (at := data.find(size)) >= 0:
        data[at : at + 4] = (10).to_bytes(4, 'little')
    return bytes(data)


def test_a_good_zip_is_read_flat_or_from_inside_one_folder():
    flat = read_skill_zip(
        skill_zip(**{'reference/rates.csv': 'grade,rate\nA,1.0\n', '__MACOSX/._SKILL.md': 'x', '.DS_Store': 'x'})
    )
    assert (flat['name'], flat['description']) == ('payroll-proration', 'Work out part-month pay.')
    assert sorted(flat['files']) == ['SKILL.md', 'reference/rates.csv']
    foldered = read_skill_zip(
        skill_zip({'my-skill/skill.md': SKILL, 'my-skill/reference/rates.csv': 'grade,rate\nA,1.0\n'})
    )
    assert sorted(foldered['files']) == ['SKILL.md', 'reference/rates.csv']
    # The fingerprint follows the contents, not how the ZIP was packed.
    assert foldered['sha256'] == flat['sha256']
    assert read_skill_zip(skill_zip(**{'reference/rates.csv': 'changed'}))['sha256'] != flat['sha256']
    folded = read_skill_zip(
        skill_zip({'SKILL.md': '---\nname: "a-b"\ndescription: >\n  First line\n  second line.\n---\n'})
    )
    assert (folded['name'], folded['description']) == ('a-b', 'First line second line.')


@pytest.mark.parametrize(
    ('data', 'reason'),
    [
        (b'not a zip at all', 'not a ZIP'),
        (b'x' * (MAX_ZIP_BYTES + 1), 'larger than'),
        (skill_zip(**{f'notes/{i}.md': 'x' for i in range(MAX_FILES)}), 'more than'),
        (skill_zip(**{'big.txt': 'x' * (MAX_ZIP_BYTES + 1)}), 'unpacks to more than'),
        (link_zip(), 'link'),
        (locked_zip(), 'password'),
        (skill_zip(**{'../escape.md': 'x'}), 'unsafe file path'),
        (skill_zip(**{'/etc/cron.md': 'x'}), 'unsafe file path'),
        (skill_zip({'notes.md': 'No main file'}), 'No SKILL.md inside'),
        (skill_zip({'a/SKILL.md': SKILL, 'b/notes.md': 'x'}), 'No SKILL.md inside'),
        (skill_zip(**{'tool.sh': '#!/bin/sh'}), 'Text and Python only'),
        (skill_zip(**{'run.js': 'process.exit()'}), 'Text and Python only'),
        (skill_zip(**{'run.pyc': b'\x00compiled'}), 'Text and Python only'),
        (skill_zip(**{'inner.zip': skill_zip()}), 'Text and Python only'),
        (skill_zip(**{'SKILL': 'no extension'}), 'Text and Python only'),
        (skill_zip(**{'run.py': b'\x00\x01binary'}), 'not a text file'),
        (skill_zip(**{'notes.md': b'MZ\x90\x00\x03binary'}), 'not a text file'),
        (skill_zip(**{'notes.md': b'\xff\xfe\xfa'}), 'not a text file'),
        (skill_zip(**{'Notes.md': 'a', 'notes.md': 'b'}), 'twice'),
        (skill_zip(**{'caf\u00e9.md': 'a', 'cafe\u0301.md': 'b'}), 'twice'),
        (skill_zip({'top/SKILL.md': SKILL, 'top/skill.md/x.md': 'x'}), 'both a file and a folder'),
        (skill_zip(**{'notes.md/inner.md': 'x', 'notes.md': 'y'}), 'both a file and a folder'),
        (skill_zip(**{'x' * 300 + '.md': 'x'}), 'longer than'),
        (damaged_zip(), 'damaged'),
        (lying_zip(), 'damaged|unpacks to more than'),
        (skill_zip({'SKILL.md': '# No opening block\n'}), 'must open with'),
        (skill_zip({'SKILL.md': '---\nname: never-closed\ndescription: x\n'}), 'must open with'),
        (named('Has Spaces'), 'needs a name'),
        (named('../up'), 'needs a name'),
        (named('x' * 65), 'needs a name'),
        (skill_zip({'SKILL.md': '---\nname: no-description\n---\n'}), 'one-line description'),
        (named('long', 'x' * 1025), 'longer than'),
    ],
)
def test_a_bad_zip_is_refused_with_its_reason(data, reason):
    with pytest.raises(SkillRefused, match=reason):
        read_skill_zip(data)


def names(events):
    return [e['name'] for e in events]


def test_skills_are_stored_replaced_switched_and_removed_with_a_log_entry_each(tmp_path):
    books = Store(tmp_path).playbooks
    skills = books.skills
    added = skills.add('payroll', skill_zip(**{'reference/rates.csv': 'a,b\n'}), 'proration.zip')
    assert (added['name'], added['enabled'], added['files'], added['paths']) == (
        'payroll-proration', True, 2, ['SKILL.md', 'reference/rates.csv'],
    )  # fmt: skip
    assert added['text'] == SKILL
    home = tmp_path / 'playbooks' / 'payroll' / 'skills' / 'payroll-proration'
    assert (home / added['sha256'] / 'reference' / 'rates.csv').read_text() == 'a,b\n'
    # Switched off, then replaced by a new upload of the same name: the files change, the switch stays off.
    assert skills.enable('payroll', 'payroll-proration', False)['enabled'] is False
    skills.enable('payroll', 'payroll-proration', False)  # no change, no log entry
    replaced = skills.add('payroll', skill_zip(), 'proration-v2.zip')
    assert (replaced['enabled'], replaced['paths']) == (False, ['SKILL.md'])
    assert replaced['sha256'] != added['sha256']
    assert [p.name for p in home.iterdir()] == [replaced['sha256']]  # the replaced upload is gone, nothing half made
    with pytest.raises(SkillRefused):
        skills.add('payroll', skill_zip(**{'run.sh': 'x'}), 'bad.zip')
    assert [s['name'] for s in skills.list('payroll')] == ['payroll-proration']
    skills.remove('payroll', 'payroll-proration')
    assert skills.list('payroll') == [] and not home.exists()
    log = books.events('payroll')
    assert names(log) == ['skill.added', 'skill.turned_off', 'skill.replaced', 'skill.refused', 'skill.removed']
    assert (log[2]['previous_sha256'], log[2]['sha256'], log[2]['file']) == (
        added['sha256'],
        replaced['sha256'],
        'proration-v2.zip',
    )
    assert (log[3]['level'], log[3]['file']) == ('WARN', 'bad.zip') and 'Text and Python only' in log[3]['reason']
    for call in (
        lambda: skills.list('nope'),
        lambda: skills.add('nope', skill_zip()),
        lambda: skills.get('payroll', 'payroll-proration'),
        lambda: skills.get('payroll', '../../jobs'),
        lambda: skills.enable('payroll', 'missing', True),
        lambda: skills.remove('payroll', 'missing'),
    ):
        with pytest.raises(KeyError):
            call()


def test_a_playbook_holds_a_limited_number_of_skills(tmp_path):
    skills = Store(tmp_path).playbooks.skills
    for i in range(MAX_SKILLS):
        skills.add('ap', named(f'skill-{i}'))
    with pytest.raises(SkillRefused, match='already has'):
        skills.add('ap', named('one-too-many'))
    skills.add('ap', named('skill-0', 'Replacing one is still allowed.'))
    assert len(skills.list('ap')) == MAX_SKILLS


def test_a_copied_playbook_starts_with_the_same_skills(tmp_path):
    books = Store(tmp_path).playbooks
    books.skills.add('payroll', skill_zip())
    books.skills.add('payroll', named('bank-checks'))
    books.skills.enable('payroll', 'bank-checks', False)
    copy = books.create('Payroll for the UK entity', copy_of='payroll')
    assert [(s['name'], s['enabled']) for s in books.skills.list(copy['id'])] == [
        ('bank-checks', False),
        ('payroll-proration', True),
    ]
    # The copy has its own files: removing the original's skill leaves the copy's in place.
    books.skills.remove('payroll', 'payroll-proration')
    assert books.skills.get(copy['id'], 'payroll-proration')['text'] == SKILL


def upload_job(client, playbook_id):
    return client.post(
        '/api/jobs',
        data={'period': '2026-10', 'playbook_id': playbook_id},
        files=[('template', ('template.xlsx', workbook_bytes())), ('files', ('a.txt', b'EMP001'))],
    )


def test_the_skill_routes_and_the_copy_a_job_takes(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        url = '/api/playbooks/payroll/skills'
        made = client.post(url, files={'file': ('proration.zip', skill_zip(**{'reference/rates.csv': 'a,b\n'}))})
        assert made.status_code == 201 and made.json()['name'] == 'payroll-proration'
        assert client.post(url, files={'file': ('bank.zip', named('bank-checks'))}).status_code == 201
        assert client.patch(url + '/bank-checks', json={'enabled': False}).json()['enabled'] is False
        assert [(s['name'], s['enabled']) for s in client.get(url).json()] == [
            ('bank-checks', False),
            ('payroll-proration', True),
        ]
        assert client.get(url + '/payroll-proration').json()['text'] == SKILL
        # Refusals the screen can show.
        refused = client.post(url, files={'file': ('tool.zip', skill_zip(**{'run.sh': 'x'}))})
        assert refused.status_code == 400 and 'Text and Python only' in refused.json()['detail']
        huge = client.post(url, files={'file': ('huge.zip', b'x' * (MAX_ZIP_BYTES + 50))})
        assert huge.status_code == 400 and 'larger than' in huge.json()['detail']
        assert client.post('/api/playbooks/nope/skills', files={'file': ('a.zip', skill_zip())}).status_code == 404
        assert client.get(url + '/missing').status_code == 404
        assert client.patch(url + '/missing', json={'enabled': True}).status_code == 404
        assert client.delete(url + '/missing').status_code == 404

        # A new job copies the skills that are switched on, and records what it took.
        job = upload_job(client, 'payroll').json()
        taken = made.json()
        assert job['skills'] == [
            {
                'name': 'payroll-proration',
                'description': taken['description'],
                'sha256': taken['sha256'],
                'programs': [],
            }  # fmt: skip
        ]
        copied = tmp_path / 'jobs' / job['id'] / 'skills'
        assert sorted(p.relative_to(copied).as_posix() for p in copied.rglob('*') if p.is_file()) == [
            'payroll-proration/SKILL.md',
            'payroll-proration/reference/rates.csv',
        ]
        assert app.state.store.events(job['id'])[0]['skills'] == job['skills']
        # Changing or removing the playbook's skill afterwards does not touch the job's copy.
        client.post(url, files={'file': ('proration.zip', skill_zip({'SKILL.md': SKILL + 'Changed.\n'}))})
        assert client.delete(url + '/payroll-proration').status_code == 204
        assert (copied / 'payroll-proration' / 'SKILL.md').read_text() == SKILL
        assert client.get(f'/api/jobs/{job["id"]}').json()['skills'] == job['skills']
        # The browser's file routes still serve only uploads, the template and outputs.
        assert (
            client.get(f'/api/jobs/{job["id"]}/file', params={'path': 'skills/payroll-proration/SKILL.md'}).status_code
            == 404
        )
        # A playbook with no skills gives a job with none.
        assert upload_job(client, 'ap').json()['skills'] == []


def test_a_copy_that_cannot_bring_its_skills_leaves_no_half_made_playbook(tmp_path):
    books = Store(tmp_path).playbooks
    added = books.skills.add('payroll', skill_zip())
    books.skills.add('payroll', named('bank-checks'))
    # The files of one skill were removed by hand.
    shutil.rmtree(tmp_path / 'playbooks' / 'payroll' / 'skills' / 'payroll-proration' / added['sha256'])
    with pytest.raises(OSError):
        books.create('Payroll copy', copy_of='payroll')
    assert [p['name'] for p in books.list()] == ['Payroll', 'Accounts payable']
    assert sorted(p.name for p in (tmp_path / 'playbooks').iterdir()) == ['payroll']
    assert books.events('payroll')[-1]['name'] == 'playbook.copy_failed'
    books.create('Payroll copy')  # the name is free again


def test_a_skill_whose_files_are_gone_is_reported_plainly(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        url = '/api/playbooks/payroll/skills'
        added = client.post(url, files={'file': ('proration.zip', skill_zip())}).json()
        shutil.rmtree(tmp_path / 'playbooks' / 'payroll' / 'skills' / 'payroll-proration' / added['sha256'])
        assert client.get(url + '/payroll-proration').status_code == 404
        # A new job says which skill is the problem, without showing where the app keeps its files.
        refused = upload_job(client, 'payroll')
        assert refused.status_code == 400
        assert 'payroll-proration' in refused.json()['detail'] and str(tmp_path) not in refused.text
        # Switching it off still works, and then jobs can be made again.
        assert client.patch(url + '/payroll-proration', json={'enabled': False}).json()['enabled'] is False
        assert upload_job(client, 'payroll').status_code == 201
        # Uploading it again brings it back.
        assert client.post(url, files={'file': ('proration.zip', skill_zip())}).status_code == 201
        assert client.get(url + '/payroll-proration').json()['text'] == SKILL


def test_many_uploads_of_one_skill_at_once_leave_the_record_matching_the_files(tmp_path):
    skills = Store(tmp_path).playbooks.skills
    versions = [skill_zip({'SKILL.md': SKILL + f'Variant {i}.\n'}) for i in range(12)]
    errors = []

    def one(data):
        try:
            skills.add('payroll', data)
        except Exception as error:  # noqa: BLE001 - any failure fails the test
            errors.append(error)

    threads = [threading.Thread(target=one, args=(data,)) for data in versions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    kept = skills.get('payroll', 'payroll-proration')
    home = tmp_path / 'playbooks' / 'payroll' / 'skills' / 'payroll-proration'
    assert [p.name for p in home.iterdir()] == [kept['sha256']]
    assert read_skill_zip(skill_zip({'SKILL.md': kept['text']}))['sha256'] == kept['sha256']
    copied = skills.copy_to_job('payroll', tmp_path / 'job-skills')
    assert copied[0]['sha256'] == kept['sha256']
    assert (tmp_path / 'job-skills' / 'payroll-proration' / 'SKILL.md').read_text() == kept['text']


def test_control_characters_are_kept_out_of_the_description_and_the_log(tmp_path):
    books = Store(tmp_path).playbooks
    added = books.skills.add('ap', named('tidy', 'Check\x1b[31m bank\tdetails.'), 'odd\x07name.zip')
    assert added['description'] == 'Check [31m bank details.'
    assert books.events('ap')[-1]['file'] == 'odd name.zip'


PROGRAM = 'def first_pay(salary, days):\n    return round(salary * days / 26, 2)\n'


def test_a_skill_may_hold_python_programs_and_shows_them(tmp_path):
    books = Store(tmp_path).playbooks
    skills = books.skills
    read = read_skill_zip(skill_zip(**{'scripts/pay.py': PROGRAM, 'Tools/Check.PY': 'print(1)\n', 'notes.md': 'x'}))
    assert read['programs'] == ['Tools/Check.PY', 'scripts/pay.py']
    added = skills.add('payroll', skill_zip(**{'scripts/pay.py': PROGRAM}), 'pay.zip')
    assert (added['programs'], added['code']) == (1, [{'path': 'scripts/pay.py', 'text': PROGRAM}])
    assert skills.list('payroll')[0]['programs'] == 1
    assert books.events('payroll')[-1]['programs'] == ['scripts/pay.py']
    # It arrives switched off, so no run uses code the user has not read; a job takes it once it is switched on.
    assert (added['enabled'], added['review'], books.events('payroll')[-1]['switched_off_for_review']) == (
        False,
        True,
        True,
    )
    assert skills.copy_to_job('payroll', tmp_path / 'early' / 'skills') == []
    skills.enable('payroll', 'payroll-proration', True)
    taken = skills.copy_to_job('payroll', tmp_path / 'job' / 'skills')
    assert taken[0]['programs'] == ['scripts/pay.py']
    assert (tmp_path / 'job' / 'skills' / 'payroll-proration' / 'scripts' / 'pay.py').read_text() == PROGRAM
    # Uploaded again with the same programs it stays on; with a changed or an added program it goes off again.
    same = skills.add('payroll', skill_zip(**{'scripts/pay.py': PROGRAM, 'notes.md': 'new note'}), 'pay.zip')
    assert (same['enabled'], same['review']) == (True, False)
    changed = skills.add('payroll', skill_zip(**{'scripts/pay.py': PROGRAM + '# changed\n'}), 'pay.zip')
    assert (changed['enabled'], changed['review']) == (False, True)
    skills.enable('payroll', 'payroll-proration', True)
    more = skills.add(
        'payroll', skill_zip(**{'scripts/pay.py': PROGRAM + '# changed\n', 'extra.py': 'x = 1\n'}), 'pay.zip'
    )
    assert (more['enabled'], more['review']) == (False, True)
    # A skill of text only is on from the start, as before, and replacing programs with text needs no review.
    plain = skills.add('ap', skill_zip(), 'plain.zip')
    assert (plain['programs'], plain['code'], plain['enabled'], plain['review']) == (0, [], True, False)
    # A copied playbook keeps the count and the setting; replacing with a text-only upload clears the count.
    copy = books.create('Payroll copy', copy_of='payroll')
    assert (skills.list(copy['id'])[0]['programs'], skills.list(copy['id'])[0]['enabled']) == (2, False)
    skills.enable('payroll', 'payroll-proration', True)
    text_only = skills.add('payroll', skill_zip(), 'text.zip')
    assert (text_only['programs'], text_only['enabled'], text_only['review']) == (0, True, False)


def test_a_skills_table_from_before_programs_gains_the_count(tmp_path):
    with sqlite3.connect(tmp_path / 'jobs.db') as c:
        c.execute(
            'CREATE TABLE playbook_skills(playbook_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, '
            'enabled INTEGER NOT NULL, sha256 TEXT NOT NULL, files INTEGER NOT NULL, bytes INTEGER NOT NULL, '
            'added_at TEXT NOT NULL, PRIMARY KEY(playbook_id, name))'
        )
        c.execute("INSERT INTO playbook_skills VALUES ('payroll','old-skill','Old.',1,'abc',1,10,'2026-11-01')")
    skills = Store(tmp_path).playbooks.skills
    assert [(s['name'], s['programs']) for s in skills.list('payroll')] == [('old-skill', 0)]
    assert Store(tmp_path).playbooks.skills.list('payroll')[0]['programs'] == 0  # a second start changes nothing

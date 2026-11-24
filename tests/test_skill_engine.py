"""The agent and the user's skills: the goal card, the read_skill tool, the sandbox upload and the run record."""

import asyncio
import contextlib
import io
import json
import zipfile
from types import SimpleNamespace

from agents.tool_context import ToolContext
from fastapi.testclient import TestClient
from openpyxl import Workbook
from test_skills import SKILL, named, skill_zip

from app.config import Settings
from app.engine import agent as engine
from app.engine import service
from app.engine import skills as engine_skills
from app.engine.endpoint import Settings as Endpoint
from app.engine.goal import goal_card
from app.engine.jobspec import Deliverable, JobSpec
from app.engine.runstore import RunStore
from app.main import create_app
from app.skills import fingerprint

from helpers import LOCAL, workbook_bytes

REMOTE = {'packet.zip': '/mnt/data/packet.zip', 't.xlsx': '/mnt/data/t.xlsx', 'skills.zip': '/mnt/data/skills.zip'}
SKILLS = (
    {'name': 'payroll-proration', 'description': 'Work out part-month pay.', 'sha256': 'a' * 64},
    {'name': 'bank-checks', 'description': 'Check bank details.', 'sha256': 'b' * 64},
)


def spec_with_skills(tmp_path, skills=SKILLS):
    """A job spec whose skills sit in a folder, as a job's copy does."""
    folder = tmp_path / 'skills'
    for skill in skills:
        (folder / skill['name'] / 'reference').mkdir(parents=True, exist_ok=True)
        (folder / skill['name'] / 'SKILL.md').write_text(f'# {skill["name"]}\nFollow this.', encoding='utf-8')
        (folder / skill['name'] / 'reference' / 'rates.csv').write_text('grade,rate\nA,1.0\n', encoding='utf-8')
    template = tmp_path / 't.xlsx'
    Workbook().save(template)
    packet = tmp_path / 'packet.zip'
    with zipfile.ZipFile(packet, 'w') as archive:
        archive.writestr('a.txt', 'EMP001')
    # Each skill carries the fingerprint of its files, as a job's record does.
    skills = [
        {**k, 'sha256': fingerprint({p.relative_to(folder / k['name']).as_posix(): p.read_bytes()
                                     for p in (folder / k['name']).rglob('*') if p.is_file()})}
        for k in skills
    ]  # fmt: skip
    return JobSpec(
        id='job', module='payroll', goal='Prepare payroll.', zip=packet, context='House rules text',
        deliverables=(Deliverable('out.xlsx', template=template),), playbook='Payroll', playbook_id='payroll',
        playbook_version=2, skills=tuple(skills), skills_dir=folder,
    )  # fmt: skip


def events(store):
    lines = (store.dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()
    return [json.loads(line) for line in lines]


def test_the_goal_card_lists_skills_by_name_and_description_only(tmp_path):
    spec = spec_with_skills(tmp_path)
    card = goal_card(spec, REMOTE, 'row')
    assert '# Skills\n' in card and 'read it with read_skill' in card and '/mnt/data/skills.zip' in card
    assert '- payroll-proration: Work out part-month pay.\n- bank-checks: Check bank details.' in card
    assert 'Follow this.' not in card  # the skill's own text is read on demand, never pasted into the brief
    assert card.index('# House rules') < card.index('# Skills') < card.index('# Deliverables')
    # A job with no skills gets the brief it always got.
    plain = goal_card(JobSpec(**{**spec.__dict__, 'skills': ()}), REMOTE, 'row')
    assert 'Skills' not in plain and 'read_skill' not in plain
    assert plain == card.replace(card[card.index('# Skills') : card.index('# Deliverables')], '')


def test_read_skill_returns_the_text_records_the_read_and_corrects_wrong_names(tmp_path):
    spec = spec_with_skills(tmp_path)
    store = RunStore(tmp_path / 'runs')
    job = SimpleNamespace(spec=spec, store=store, skills_read={})
    assert engine.skill_text(job, 'main', 'payroll-proration') == '# payroll-proration\nFollow this.'
    assert engine.skill_text(job, 'main', 'payroll-proration', 'reference/rates.csv') == 'grade,rate\nA,1.0\n'
    assert engine.skill_text(job, 'main', 'payroll-proration', '') == '# payroll-proration\nFollow this.'
    assert engine.skill_text(job, 'review', 'bank-checks') == '# bank-checks\nFollow this.'
    # Wrong names and made-up paths get an answer the agent can act on, and are not counted as reads.
    assert 'The skills are: payroll-proration, bank-checks' in engine.skill_text(job, 'main', 'proration')
    for file in ('missing.md', '../bank-checks/SKILL.md', '/etc/passwd', '../../packet.zip'):
        reply = engine.skill_text(job, 'main', 'payroll-proration', file)
        assert 'has no file' in reply and 'Its files are: SKILL.md, reference/rates.csv' in reply
    assert job.skills_read == {'main': ['payroll-proration'], 'review': ['bank-checks']}
    assert [e['first'] for e in events(store) if e['event'] == 'skill.read'] == [True, False, False, True]
    read = [e for e in events(store) if e['event'] == 'skill.read']
    assert [(e['who'], e['skill'], e['file']) for e in read] == [
        ('main', 'payroll-proration', 'SKILL.md'),
        ('main', 'payroll-proration', 'reference/rates.csv'),
        ('main', 'payroll-proration', 'SKILL.md'),
        ('review', 'bank-checks', 'SKILL.md'),
    ]
    # A very long skill is cut, and says so.
    (spec.skills_dir / 'bank-checks' / 'SKILL.md').write_text('x' * (engine_skills.SKILL_CHARS + 10), encoding='utf-8')
    long = engine.skill_text(job, 'main', 'bank-checks')
    assert (
        long.startswith('x' * engine_skills.SKILL_CHARS)
        and 'Cut at' in long
        and len(long) < engine_skills.SKILL_CHARS + 300
    )


async def call_tool(agent, job, **arguments):
    """Call the agent's read_skill tool the way the SDK does."""
    tool = next(t for t in agent.tools if getattr(t, 'name', '') == 'read_skill')
    raw = json.dumps(arguments)
    return await tool.on_invoke_tool(
        ToolContext(job, tool_name='read_skill', tool_call_id='1', tool_arguments=raw), raw
    )


class FakeBox:
    """Stands in for a sandbox: remembers what was uploaded."""

    def __init__(self):
        self.id, self.uploads = 'box', {}

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, settings, name, memory='4g'):
        yield cls.made.setdefault(name.split('-')[0], cls())

    async def upload_bytes(self, filename, data):
        self.uploads[filename] = data
        return f'/mnt/data/{filename}'

    async def upload_file(self, local, filename=None):
        return await self.upload_bytes(filename or local.name, local.read_bytes())

    async def list_files(self):
        return []

    async def status(self):
        return 'running'


def run_with_fake_sandbox(monkeypatch, spec, tmp_path, **options):
    """Run the real run_job with a pretend sandbox and a pretend lead agent that reads one skill and stops."""
    FakeBox.made = {}
    seen = {}

    async def drive(agent, prompt, job, store, echo, control):
        seen.update(agent=agent, job=job, prompt=prompt)
        if spec.skills:
            seen['reply'] = await call_tool(agent, job, name=spec.skills[0]['name'], file='SKILL.md')
        output = SimpleNamespace(
            status='partial', model_dump=lambda: {'status': 'partial', 'summary': '', 'open_questions': []}
        )
        return SimpleNamespace(final_output=output, to_input_list=lambda: [])

    monkeypatch.setattr(engine, 'Sandbox', FakeBox)
    monkeypatch.setattr(engine, '_drive', drive)
    store = asyncio.run(
        engine.run_job(spec, Endpoint(api_key='unused'), tmp_path / 'runs', echo=lambda _: None, **options)
    )
    return store, seen, FakeBox.made['job']


def test_a_run_uploads_its_skills_offers_the_tool_and_records_what_was_read(tmp_path, monkeypatch):
    spec = spec_with_skills(tmp_path)
    store, seen, box = run_with_fake_sandbox(monkeypatch, spec, tmp_path)
    with zipfile.ZipFile(io.BytesIO(box.uploads['skills.zip'])) as archive:
        assert sorted(archive.namelist()) == [
            'bank-checks/SKILL.md', 'bank-checks/reference/rates.csv',
            'payroll-proration/SKILL.md', 'payroll-proration/reference/rates.csv',
        ]  # fmt: skip
    assert (store.dir / 'input' / 'skills.zip').read_bytes() == box.uploads['skills.zip']
    assert '# Skills' in box.uploads['goal_card.md'].decode() and '# Skills' in seen['agent'].instructions
    assert 'read_skill' in [getattr(t, 'name', '') for t in seen['agent'].tools]
    record = store.record
    assert record['playbook'] == {'id': 'payroll', 'name': 'Payroll', 'version': 2}
    assert record['skills'] == [{'name': k['name'], 'sha256': k['sha256'], 'programs': []} for k in spec.skills]
    assert record['skills_read'] == {'main': ['payroll-proration']}
    assert seen['reply'] == '# payroll-proration\nFollow this.'
    schema = next(t for t in seen['agent'].tools if getattr(t, 'name', '') == 'read_skill').params_json_schema
    assert schema['required'] == ['name', 'file']


def test_a_run_without_skills_uploads_none_and_has_no_skill_tool(tmp_path, monkeypatch):
    store, seen, box = run_with_fake_sandbox(monkeypatch, spec_with_skills(tmp_path, skills=()), tmp_path)
    assert 'skills.zip' not in box.uploads
    assert 'read_skill' not in [getattr(t, 'name', '') for t in seen['agent'].tools]
    assert 'Skills' not in seen['agent'].instructions and 'read_skill' not in seen['agent'].instructions
    assert (store.record['skills'], store.record['skills_read']) == ([], {})


def review_with_fake_sandbox(monkeypatch, spec, tmp_path):
    FakeBox.made = {}
    seen = {}

    async def run(agent, prompt, context=None, max_turns=0, run_config=None):
        seen.update(agent=agent, context=context, max_turns=max_turns, run_config=run_config)
        usage = SimpleNamespace(
            requests=1, input_tokens=1, output_tokens=1, input_tokens_details=None, output_tokens_details=None
        )
        report = SimpleNamespace(model_dump=lambda: {'verdict': 'ok', 'doubts': []})
        return SimpleNamespace(
            final_output=report, new_items=[], raw_responses=[], context_wrapper=SimpleNamespace(usage=usage)
        )

    monkeypatch.setattr(engine, 'Sandbox', FakeBox)
    monkeypatch.setattr(engine.Runner, 'run', run)
    store = RunStore(tmp_path / 'runs')
    job = SimpleNamespace(
        store=store, settings=Endpoint(api_key='unused'), spec=spec, zip_parts=[spec.zip], files=[], effort='low',
        card=goal_card(spec, REMOTE, 'row'), answerer=SimpleNamespace(log=[]), review_usage=[], skills_read={},
        skills_packed=engine_skills.load_skills(spec) if spec.skills else b'',
    )  # fmt: skip
    asyncio.run(engine.review(job, 1, []))
    return seen, FakeBox.made['review'], job


def test_the_reviewer_gets_the_skills_too(tmp_path, monkeypatch):
    seen, box, job = review_with_fake_sandbox(monkeypatch, spec_with_skills(tmp_path), tmp_path)
    assert 'skills.zip' in box.uploads and seen['context'] is job
    assert seen['run_config'] is engine.RUN_CONFIG  # the reviewer's pictures are made to fit too
    assert 'read_skill' in [getattr(t, 'name', '') for t in seen['agent'].tools]
    assert "The user's skills on the goal card apply to the deliverables" in seen['agent'].instructions
    assert 'a skill that was not followed' in seen['agent'].instructions and seen['max_turns'] == 22
    seen, box, _ = review_with_fake_sandbox(monkeypatch, spec_with_skills(tmp_path, skills=()), tmp_path)
    assert 'skills.zip' not in box.uploads
    assert 'read_skill' not in [getattr(t, 'name', '') for t in seen['agent'].tools]
    assert 'skills' not in seen['agent'].instructions.lower()
    assert seen['max_turns'] == 20


def test_a_job_made_in_the_app_carries_its_skills_to_the_engine_and_the_feed(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=''))
    with TestClient(base_url=LOCAL, app=app) as client:
        store = app.state.store
        client.post('/api/playbooks/payroll/skills', files={'file': ('a.zip', skill_zip())})
        client.post('/api/playbooks/payroll/skills', files={'file': ('b.zip', named('bank-checks'))})
        client.patch('/api/playbooks/payroll/skills/bank-checks', json={'enabled': False})
        job = client.post(
            '/api/jobs',
            data={'period': '2026-10', 'playbook_id': 'payroll'},
            files=[('template', ('template.xlsx', workbook_bytes())), ('files', ('a.txt', b'EMP001'))],
        ).json()
        spec = service.job_spec(store, store.get(job['id']))
        assert [s['name'] for s in spec.skills] == ['payroll-proration']
        reader = SimpleNamespace(spec=spec, store=RunStore(tmp_path / 'runs'), skills_read={})
        assert engine.skill_text(reader, 'main', 'payroll-proration') == SKILL
        assert 'bank-checks' in engine.skill_text(reader, 'main', 'bank-checks') and reader.skills_read == {
            'main': ['payroll-proration']
        }
        # A job from before skills has none and still builds its spec.
        store.mutate(job['id'], lambda j: j.pop('skills'))
        assert service.job_spec(store, store.get(job['id'])).skills == ()
    # The feed says a skill is being read once, however many of its files the agent opens.
    say = service.MESSAGES['skill.read']
    assert say({'skill': 'payroll-proration', 'first': True}) == 'Reading skill payroll-proration'
    assert say({'skill': 'payroll-proration', 'first': False}) is None


def test_a_run_does_not_start_when_a_listed_skill_is_missing_or_changed(tmp_path, monkeypatch):
    spec = spec_with_skills(tmp_path)
    (spec.skills_dir / 'bank-checks' / 'SKILL.md').unlink()
    FakeBox.made = {}
    monkeypatch.setattr(engine, 'Sandbox', FakeBox)
    store = asyncio.run(engine.run_job(spec, Endpoint(api_key='unused'), tmp_path / 'runs', echo=lambda _: None))
    assert store.record['status'] == 'failed' and FakeBox.made == {}  # no sandbox was opened
    assert 'The files of skill "bank-checks" are missing from this job' in store.record['error']
    # Files that are there but no longer what the job copied are refused the same way.
    spec = spec_with_skills(tmp_path)
    (spec.skills_dir / 'payroll-proration' / 'SKILL.md').write_text('Edited on disk.', encoding='utf-8')
    store = asyncio.run(engine.run_job(spec, Endpoint(api_key='unused'), tmp_path / 'runs2', echo=lambda _: None))
    assert store.record['status'] == 'failed' and 'changed after this job was created' in store.record['error']


def test_a_link_placed_in_a_skill_folder_is_not_part_of_the_skill(tmp_path):
    spec = spec_with_skills(tmp_path)
    secret = tmp_path / 'secret.txt'
    secret.write_text('outside the skill', encoding='utf-8')
    (spec.skills_dir / 'bank-checks' / 'link.md').symlink_to(secret)
    job = SimpleNamespace(spec=spec, store=RunStore(tmp_path / 'runs'), skills_read={})
    reply = engine_skills.skill_text(job, 'main', 'bank-checks', 'link.md')
    assert 'has no file' in reply and 'outside the skill' not in reply
    with zipfile.ZipFile(io.BytesIO(engine_skills.load_skills(spec))) as archive:
        assert 'bank-checks/link.md' not in archive.namelist()


def test_a_run_that_opens_with_pictures_sends_them_as_one_user_message(tmp_path, monkeypatch):
    spec = spec_with_skills(tmp_path, skills=())
    picture = {'name': 'round-01-picture-01.png', 'data': b'\x89PNG fake'}
    _, seen, _ = run_with_fake_sandbox(
        monkeypatch, spec, tmp_path, resume='Carry on from the comments.', resume_images=(picture,)
    )
    # One conversation item, a user message, whose parts are the text, the picture's name and the picture. A bare
    # list of parts would be read by the model's API as a list of items and refused.
    (message,) = seen['prompt']
    assert message['role'] == 'user'
    assert [part['type'] for part in message['content']] == ['input_text', 'input_text', 'input_image']
    assert message['content'][0]['text'].startswith('Carry on from the comments.')
    assert message['content'][1]['text'] == 'Picture round-01-picture-01.png:'
    # Without pictures the opening stays the plain text it always was.
    _, seen, _ = run_with_fake_sandbox(monkeypatch, spec, tmp_path, resume='Carry on.')
    assert seen['prompt'] == 'Carry on.'


def test_a_skill_with_programs_is_named_on_the_card_and_its_runs_are_recorded(tmp_path):
    spec = spec_with_skills(tmp_path)
    skills = ({**spec.skills[0], 'programs': ['scripts/pay.py', 'check.py']}, spec.skills[1])
    spec = JobSpec(**{**spec.__dict__, 'skills': skills})
    card = goal_card(spec, REMOTE, 'row')
    assert '- payroll-proration: Work out part-month pay. Programs: scripts/pay.py, check.py.' in card
    assert '- bank-checks: Check bank details.\n' in card
    assert 'run the program as its skill describes, rather than writing your own version' in card
    plain = goal_card(spec_with_skills(tmp_path), REMOTE, 'row')
    assert 'Programs' not in plain and 'run the program' not in plain
    store = RunStore(tmp_path / 'runs')
    job = SimpleNamespace(spec=spec, store=store, skills_run=[])
    # The sandbox's note of the programs that started is the evidence; only the job's own programs count.
    log = 'payroll-proration/scripts/pay.py\nsomething/else.py\npayroll-proration/scripts/pay.py\n\n'
    assert engine_skills.record_runs(job, log) == ['payroll-proration/scripts/pay.py']
    assert engine_skills.record_runs(job, log + 'payroll-proration/check.py\n') == [
        'payroll-proration/scripts/pay.py', 'payroll-proration/check.py',
    ]  # fmt: skip
    ran = [(e['skill'], e['program']) for e in events(store) if e['event'] == 'skill.run']
    assert ran == [('payroll-proration', 'scripts/pay.py'), ('payroll-proration', 'check.py')]  # once each
    assert service.MESSAGES['skill.run']({'skill': 'a', 'program': 'b.py'}) == 'Running b.py from skill a'


def run_program(source: bytes, tmp_path, monkeypatch, name='__main__'):
    """Run a program as the sandbox would get it, with the note going to a file under tmp_path."""
    log = tmp_path / 'log'
    log.unlink(missing_ok=True)
    monkeypatch.setattr(engine_skills, 'RUN_LOG', str(log))
    made = engine_skills.instrument('pay', 'scripts/calc.py', source)
    scope = {'__name__': name}
    exec(compile(made, 'calc.py', 'exec'), scope)  # noqa: S102 - the program under test
    return made.decode(), scope, log.read_text() if log.exists() else ''


def test_a_program_notes_that_it_started_without_changing_what_it_does(tmp_path, monkeypatch):
    plain = b'def double(x):\n    return x * 2\n'
    made, scope, log = run_program(plain, tmp_path, monkeypatch, name='calc')
    assert scope['double'](4) == 8 and log == 'pay/scripts/calc.py\n' and made.endswith(plain.decode())
    # The opening docstring and __future__ imports stay first, so the program means what it meant.
    source = b'"""The calculator."""\nfrom __future__ import annotations\n\nimport sys\nRESULT = __doc__\n'
    made, scope, log = run_program(source, tmp_path, monkeypatch)
    assert scope['RESULT'] == 'The calculator.' and log == 'pay/scripts/calc.py\n'
    assert made.index('from __future__') < made.index('Added by the app') < made.index('import sys')
    # No newline at the end of a one-line program, an empty program, and one Python cannot parse.
    made, scope, log = run_program(b'"""Only a docstring."""', tmp_path, monkeypatch)
    assert log == 'pay/scripts/calc.py\n' and scope['__doc__'] == 'Only a docstring.'
    assert run_program(b'', tmp_path, monkeypatch)[2] == 'pay/scripts/calc.py\n'
    broken = b'def broken(:\n'
    assert engine_skills.instrument('pay', 'scripts/calc.py', broken) == broken
    # Reading a program is not running it: only the copy that ran leaves a note.
    assert not (tmp_path / 'never').exists()


def test_the_sandbox_gets_noting_programs_and_the_record_takes_the_note(tmp_path, monkeypatch):
    spec = spec_with_skills(tmp_path)
    folder = spec.skills_dir / 'payroll-proration'
    (folder / 'scripts').mkdir()
    (folder / 'scripts' / 'pay.py').write_text('X = 1\n', encoding='utf-8')
    files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    skills = ({**spec.skills[0], 'sha256': fingerprint(files), 'programs': ['scripts/pay.py']}, spec.skills[1])
    spec = JobSpec(**{**spec.__dict__, 'skills': skills})
    with zipfile.ZipFile(io.BytesIO(engine_skills.load_skills(spec))) as archive:
        sent = archive.read('payroll-proration/scripts/pay.py').decode()
        assert 'Added by the app' in sent and sent.endswith('X = 1\n')
        assert archive.read('payroll-proration/SKILL.md') == (folder / 'SKILL.md').read_bytes()
    assert (folder / 'scripts' / 'pay.py').read_text() == 'X = 1\n'  # the job's own copy is untouched

    class Box(FakeBox):
        async def list_files(self):
            return [{'id': 'f1', 'path': engine_skills.RUN_LOG}, {'id': 'f2', 'path': '/mnt/data/other'}]

        async def download_id(self, file_id):
            return b'payroll-proration/scripts/pay.py\n' if file_id == 'f1' else b''

    store = RunStore(tmp_path / 'runs')
    job = SimpleNamespace(spec=spec, store=store, skills_run=[], main=Box())
    asyncio.run(engine.note_programs(job))
    assert job.skills_run == ['payroll-proration/scripts/pay.py']
    # A sandbox that cannot be read is logged, never raised; a job without programs is not looked at.

    class Broken(FakeBox):
        async def list_files(self):
            raise RuntimeError('gone')

    job.main = Broken()
    asyncio.run(engine.note_programs(job))
    assert [e['event'] for e in events(store)][-1] == 'skill.run_log_failed'
    plain = SimpleNamespace(spec=spec_with_skills(tmp_path / 'plain'), store=store, skills_run=[], main=Broken())
    asyncio.run(engine.note_programs(plain))
    assert [e['event'] for e in events(store)].count('skill.run_log_failed') == 1


def test_once_the_checks_pass_the_agent_holds_its_work_against_the_house_rules_one_time(tmp_path, monkeypatch):
    def run(spec, folder):
        FakeBox.made = {}
        prompts = []

        async def drive(agent, prompt, job, store, echo, control):
            prompts.append(prompt)
            output = SimpleNamespace(
                status='complete', model_dump=lambda: {'status': 'complete', 'summary': '', 'open_questions': []}
            )
            return SimpleNamespace(final_output=output, to_input_list=lambda: [])

        passed = {'passed': True, 'issues': [], 'template_changes': [], 'empty_rows': [], 'summary': {}}
        monkeypatch.setattr(engine, 'Sandbox', FakeBox)
        monkeypatch.setattr(engine, '_drive', drive)
        monkeypatch.setattr(engine, '_check', lambda job: passed)
        store = asyncio.run(engine.run_job(spec, Endpoint(api_key='unused'), folder / 'runs', echo=lambda _: None))
        return store, prompts

    spec = spec_with_skills(tmp_path, skills=())  # it has house rules: context='House rules text'
    store, prompts = run(spec, tmp_path)
    assert len(prompts) == 2 and prompts[1][-1]['content'] == engine.RULES_CHECK
    assert store.record['status'] == 'complete' and [e['event'] for e in events(store)].count('rules.held') == 1
    assert 'match the example' in engine.RULES_CHECK
    # A job with no rules and no skills of the user's own has nothing to be held to.
    (tmp_path / 'plain').mkdir()
    bare = JobSpec(**{**spec_with_skills(tmp_path / 'plain', skills=()).__dict__, 'context': '  '})
    store, prompts = run(bare, tmp_path / 'plain')
    assert len(prompts) == 1 and 'rules.held' not in [e['event'] for e in events(store)]
    assert service.MESSAGES['rules.held']({}) == 'Checks passed; holding the result against your house rules'

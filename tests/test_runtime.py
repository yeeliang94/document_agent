import asyncio

from app.config import Settings
from app.runtime import Runner
from app.store import Store


def test_restart_recovers_interrupted_jobs_without_rerunning_them(tmp_path):
    store = Store(tmp_path)
    jobs = {}
    for status in ('running', 'queued', 'pausing', 'waiting', 'cancelled', 'completed'):
        job = store.create('payroll', '2026-10', 'Persisted task', '')
        store.update(job['id'], status=status)
        jobs[status] = job['id']

    async def recover():
        runner = Runner(store, Settings(data_dir=tmp_path, api_key=''))
        await runner.start()
        await runner.close()

    asyncio.run(recover())
    for original in ('running', 'queued', 'pausing'):
        assert store.get(jobs[original])['status'] == 'paused'
        assert store.events(jobs[original])[-1]['name'] == 'job.recovered'
    for original in ('waiting', 'cancelled', 'completed'):
        assert store.get(jobs[original])['status'] == original


def test_new_jobs_see_only_the_tools_they_can_use(tmp_path):
    import asyncio

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from app.config import Settings
    from app.runtime import Runner
    from app.store import Store

    store = Store(tmp_path)
    job = store.create('payroll', '2026-10', 'Prepare the output', '')
    store.state(job['id']).put('schema', 'accounting', {'version': 2})
    offered = []

    def respond(messages, info):
        offered.append({tool.name for tool in info.function_tools})
        return ModelResponse(
            parts=[ToolCallPart('final_result', {'summary': 'Stop', 'status': 'partial', 'unresolved': ['test']})]
        )

    asyncio.run(Runner(store, Settings(data_dir=tmp_path), FunctionModel(respond)).run(job['id']))
    tools = offered[0]
    assert {'extract_dataset', 'delegate_task', 'get_task_status', 'cancel_task'}.isdisjoint(tools)
    assert {'inspect_source', 'validate_output', 'update_work_plan', 'list_files'} <= tools
    assert {'inspect_workbook', 'validate_job', 'query_inventory', 'save_memory'}.isdisjoint(tools)

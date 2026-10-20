"""Private subprocess protocol. Input is trusted local application data, never model code."""

import asyncio
import pickle
import sys
from types import SimpleNamespace

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from .harness_tools import register_harness_tools
from .store import Store
from .tools import JobDeps, register_tools


def main():
    root, job_id, settings, actor, scope, name, kwargs = pickle.loads(sys.stdin.buffer.read())
    # Build exactly the same registered tool functions; no model request is made here.
    agent = Agent(FunctionModel(lambda messages, info: None), deps_type=JobDeps)
    register_tools(agent)
    register_harness_tools(agent)
    tool = agent._function_toolset.tools[name]
    ctx = SimpleNamespace(deps=JobDeps(Store(root), job_id, settings, actor, scope))
    result = asyncio.run(tool.function(ctx, **kwargs))
    sys.stdout.buffer.write(pickle.dumps(result))


if __name__ == '__main__':
    main()

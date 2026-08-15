"""Shared agent execution helpers."""

from collections.abc import Awaitable, Callable, Sequence

from agent_framework import AgentResponse


async def run_agent_attempts[ResultT](
    prompts: Sequence[str],
    execute: Callable[[str], Awaitable[AgentResponse]],
    extract: Callable[[AgentResponse], ResultT | None],
) -> ResultT | None:
    for prompt in prompts:
        result = extract(await execute(prompt))
        if result is not None:
            return result
    return None

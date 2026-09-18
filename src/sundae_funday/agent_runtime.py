"""Shared agent execution helpers."""

import logging
from collections.abc import Awaitable, Callable, Sequence

from agent_framework import Agent, AgentResponse
from agent_framework.exceptions import ChatClientException
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


async def run_structured[PlanT: BaseModel](
    agent: Agent, prompt: str, schema: type[PlanT]
) -> PlanT | None:
    current_prompt = prompt
    for attempt in range(2):
        try:
            response = await agent.run(
                current_prompt, options={"response_format": schema}
            )
            return schema.model_validate_json(response.text)
        except ValidationError as error:
            detail = error.json(include_input=False, include_url=False)
            logger.warning(
                "%s validation failed on attempt %s/2: %s",
                schema.__name__,
                attempt + 1,
                detail,
            )
            current_prompt = (
                f"{prompt}\n\nValidation errors:\n{detail}\n"
                "Return corrected JSON matching the response schema."
            )
        except ChatClientException as error:
            raise RuntimeError(
                f"{schema.__name__} structured-output request failed"
            ) from error
    return None


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

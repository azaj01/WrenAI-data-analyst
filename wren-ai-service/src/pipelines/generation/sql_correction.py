import asyncio
import logging
import sys
from typing import Any, Dict, List, Optional

from hamilton import base
from hamilton.async_driver import AsyncDriver
from haystack import Document
from haystack.components.builders.prompt_builder import PromptBuilder
from langfuse.decorators import observe

from src.core.engine import Engine
from src.core.pipeline import BasicPipeline
from src.core.provider import LLMProvider
from src.pipelines.generation.utils.sql import (
    SQL_GENERATION_MODEL_KWARGS,
    TEXT_TO_SQL_RULES,
    SQLGenPostProcessor,
)
from src.utils import trace_cost

logger = logging.getLogger("wren-ai-service")


sql_correction_system_prompt = f"""
### TASK ###
You are an ANSI SQL expert with exceptional logical thinking skills and debugging skills.

Now you are given syntactically incorrect ANSI SQL query and related error message, please generate the syntactically correct ANSI SQL query without changing original semantics.

{TEXT_TO_SQL_RULES}

### FINAL ANSWER FORMAT ###
The final answer must be a corrected SQL query in JSON format:

{{
    "sql": <CORRECTED_SQL_QUERY_STRING>
}}
"""

sql_correction_user_prompt_template = """
{% if documents %}
### DATABASE SCHEMA ###
{% for document in documents %}
    {{ document }}
{% endfor %}
{% endif %}

### QUESTION ###
SQL: {{ invalid_generation_result.sql }}
Error Message: {{ invalid_generation_result.error }}

Let's think step by step.
"""


## Start of Pipeline
@observe(capture_input=False)
def prompts(
    documents: List[Document],
    invalid_generation_results: List[Dict],
    prompt_builder: PromptBuilder,
) -> list[dict]:
    return [
        prompt_builder.run(
            documents=documents,
            invalid_generation_result=invalid_generation_result,
        )
        for invalid_generation_result in invalid_generation_results
    ]


@observe(as_type="generation", capture_input=False)
@trace_cost
async def generate_sql_corrections(
    prompts: list[dict], generator: Any, generator_name: str
) -> list[dict]:
    tasks = []
    for prompt in prompts:
        task = asyncio.ensure_future(generator(prompt=prompt.get("prompt")))
        tasks.append(task)

    return await asyncio.gather(*tasks), generator_name


@observe(capture_input=False)
async def post_process(
    generate_sql_corrections: list[dict],
    post_processor: SQLGenPostProcessor,
    engine_timeout: float,
    project_id: str | None = None,
) -> list[dict]:
    return await post_processor.run(
        generate_sql_corrections,
        timeout=engine_timeout,
        project_id=project_id,
    )


## End of Pipeline


class SQLCorrection(BasicPipeline):
    def __init__(
        self,
        llm_provider: LLMProvider,
        engine: Engine,
        engine_timeout: Optional[float] = 30.0,
        **kwargs,
    ):
        self._components = {
            "generator": llm_provider.get_generator(
                system_prompt=sql_correction_system_prompt,
                generation_kwargs=SQL_GENERATION_MODEL_KWARGS,
            ),
            "generator_name": llm_provider.get_model(),
            "prompt_builder": PromptBuilder(
                template=sql_correction_user_prompt_template
            ),
            "post_processor": SQLGenPostProcessor(engine=engine),
        }

        self._configs = {
            "engine_timeout": engine_timeout,
        }

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    @observe(name="SQL Correction")
    async def run(
        self,
        contexts: List[Document],
        invalid_generation_results: List[Dict[str, str]],
        project_id: str | None = None,
    ):
        logger.info("SQLCorrection pipeline is running...")
        return await self._pipe.execute(
            ["post_process"],
            inputs={
                "invalid_generation_results": invalid_generation_results,
                "documents": contexts,
                "project_id": project_id,
                **self._components,
                **self._configs,
            },
        )


if __name__ == "__main__":
    from src.pipelines.common import dry_run_pipeline

    dry_run_pipeline(
        SQLCorrection,
        "sql_correction",
        invalid_generation_results=[],
        contexts=[],
    )

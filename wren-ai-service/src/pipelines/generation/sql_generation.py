import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from litellm import CompletionTokensDetails
import orjson
from hamilton import base
from hamilton.experimental.h_async import AsyncDriver
from haystack.components.builders.prompt_builder import PromptBuilder
from langfuse.decorators import observe
from pydantic import BaseModel

from src.core.engine import Engine
from src.core.pipeline import BasicPipeline
from src.core.provider import LLMProvider
from src.pipelines.common import (
    TEXT_TO_SQL_RULES,
    SQLGenPostProcessor,
    construct_instructions,
    sql_generation_system_prompt,
)
from src.utils import async_timer, timer
from src.web.v1.services.ask import AskConfigurations
import dspy
import dspy.evaluate
import dspy.teleprompt
import os
import json
from eval.dspy_modules.ask_generation import AskGenerationV1

logger = logging.getLogger("wren-ai-service")


sql_generation_user_prompt_template = """
### TASK ###
Given a user query that is ambiguous in nature, your task is to interpret the query in various plausible ways and
generate three SQL statements that could potentially answer each interpreted version of the queries.
Provide three different interpretations and corresponding SQL queries that reflect these interpretations.
Ensure that your SQL queries are diverse, covering a range of possible meanings behind the ambiguous query.

### EXAMPLES ###
Consider the structure of a generic database which includes common tables like users, orders, products, and transactions.
Here are the ambiguous user queries:

1. "Find the records of recent high-value transactions."
2. "Show me popular items that are not selling well."
3. "Retrieve user feedback on products from last month."

For each query, start by explaining the different ways the query can be interpreted. Then, provide SQL queries corresponding to each interpretation.
Your SQL statements should include SELECT statements, appropriate WHERE clauses to filter the results, and JOINs if necessary to combine information from different tables.
Remember to include ordering and limit clauses where relevant to address the 'recent', 'high-value', 'popular', and 'last month' aspects of the queries.

Example for the first query:

Interpretation 1: Recent high-value transactions are defined as transactions that occurred in the last 30 days with a value greater than $10,000.
SQL Query 1: SELECT * FROM "transactions" WHERE "transaction_date" >= NOW() - INTERVAL '30 days' AND "value" > 10000 ORDER BY "transaction_date" DESC;
SUMMARY 1: Recent high-value transactions.

Interpretation 2: High-value transactions are those in the top "10%" of all transactions in terms of value, and 'recent' is defined as the last 3 months.
SQL Query 2: WITH "ranked_transactions" AS (SELECT *, NTILE(10) OVER (ORDER BY "value" DESC) AS "percentile_rank" FROM "transactions" WHERE "transaction_date" >= NOW() - INTERVAL '3 months') SELECT * FROM "ranked_transactions" WHERE "percentile_rank" = 1 ORDER BY "transaction_date" DESC;
SUMMARY 2: Top 10% transactions last 3 months.

Interpretation 3: 'Recent' refers to the last week, and 'high-value' transactions are those above the average transaction value of the past week.
SQL Query 3: SELECT * FROM "transactions" WHERE "transaction_date" >= NOW() - INTERVAL '7 days' AND "value" > (SELECT AVG("value") FROM "transactions" WHERE "transaction_date" >= NOW() - INTERVAL '7 days') ORDER BY "transaction_date" DESC;
SUMMARY 3: Above-average transactions last week.

Proceed in a similar manner for the other queries.

### DATABASE SCHEMA ###
{% for document in documents %}
    {{ document }}
{% endfor %}

{% if exclude %}
### EXCLUDED STATEMETS ###
Ensure that the following excluded statements are not used in the generated queries to maintain variety and avoid repetition.
{% for doc in exclude %}
    {{ doc.statement }}
{% endfor %}
{% endif %}

{{ alert }}
{% if instructions %}
{{ instructions }}
{% endif %}

### FINAL ANSWER FORMAT ###
The final answer must be the JSON format like following:

{
    "results": [
        {"sql": <SQL_QUERY_STRING_1>},
        {"sql": <SQL_QUERY_STRING_2>},
        {"sql": <SQL_QUERY_STRING_3>}
    ]
}

{% if samples %}
### SAMPLES ###
{% for sample in samples %}
Question:
{{sample.question}}
SQL:
{{sample.sql}}
{% endfor %}
{% endif %}

### QUESTION ###
User's Question: {{ query }}
Current Time: {{ current_time }}

Let's think step by step.
"""


## Start of Pipeline
@timer
@observe(capture_input=False)
def prompt(
    query: str,
    documents: List[str],
    exclude: List[Dict],
    alert: str,
    prompt_builder: PromptBuilder,
    configurations: AskConfigurations | None = None,
    samples: List[Dict] | None = None,
) -> dict:
    logger.debug(f"query: {query}")
    logger.debug(f"documents: {documents}")
    logger.debug(
        f"exclude: {orjson.dumps(exclude, option=orjson.OPT_INDENT_2).decode()}"
    )
    logger.debug(f"configurations: {configurations}")
    if samples:
        logger.debug(f"samples: {samples}")
    result_dict = prompt_builder.run(
        query=query,
        documents=documents,
        exclude=exclude,
        alert=alert,
        instructions=construct_instructions(configurations),
        samples=samples,
        current_time=datetime.now(),
    )
    context = []
    for doc in documents:
        context.append(str(doc))
    result_dict['context'] = context
    result_dict['question'] = query
    return result_dict

# Validation logic: check that the predicted answer is correct.
# Also check that the retrieved context does actually contain that answer.
def validate_context_and_answer(example: dspy.Example, pred : dspy.Example) -> bool:
    answer_EM = dspy.evaluate.answer_exact_match(example, pred)
    answer_PM = dspy.evaluate.answer_passage_match(example, pred)
    return answer_EM and answer_PM

def configure_llm_provider(llm: str, api_key: str) -> None:
    dspy.settings.configure(lm=dspy.OpenAI(model=llm, api_key=api_key,max_tokens=4096))


@async_timer
@observe(as_type="generation", capture_input=False)
async def generate_sql(prompt: dict, generator: Any) -> dict:
    optimized_path = os.getenv("DSPY_OPTIMAZED_MODEL", "")
    if not optimized_path:
      logger.debug(f"prompt: {orjson.dumps(prompt, option=orjson.OPT_INDENT_2).decode()}")
      return await generator.run(prompt=prompt.get("prompt"))
    else:
      # use dspy to evaluate
      configure_llm_provider(
          os.getenv("GENERATION_MODEL"), os.getenv("LLM_OPENAI_API_KEY")
      )
      inputset = [dspy.Example(
                  context=str(prompt["context"]),
                  question=str(prompt["question"]),
                  answer=""
              ).with_inputs("question", "context")]
      evaluator = dspy.evaluate.Evaluate(
              devset=inputset,
              metric=validate_context_and_answer,
              display_progress=True,
              display_table=True,
              return_outputs=True,
          )
      module = AskGenerationV1()
      module.load(optimized_path)
      results = evaluator(module, metric=validate_context_and_answer)

    answers = None
    for result in results:
      if isinstance(result, list):
        for item in result:
          if isinstance(item, tuple):
            if len(item) == 3:
              # remove prefix and suffix ```json`
              cleaned_string = "".join(item[1].get('answer').split('\n')[1:-1]).strip()
              answers = cleaned_string # orjson.loads(cleaned_string)
    return {"replies":[answers]}


def default_serializer(obj: Any) -> Any:
    if isinstance(obj, CompletionTokensDetails):
        return f"<special>{str(obj)}"


@async_timer
@observe(capture_input=False)
async def post_process(
    generate_sql: dict,
    post_processor: SQLGenPostProcessor,
    project_id: str | None = None,
) -> dict:
    logger.debug(
        f"generate_sql: {orjson.dumps(generate_sql, option=orjson.OPT_INDENT_2, default=default_serializer).decode()}"
    )
    return await post_processor.run(generate_sql.get("replies"), project_id=project_id)


## End of Pipeline
class SQLResult(BaseModel):
    sql: str


class GenerationResults(BaseModel):
    results: list[SQLResult]


SQL_GENERATION_MODEL_KWARGS = {
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "sql_results",
            "schema": GenerationResults.model_json_schema(),
        },
    }
}


class SQLGeneration(BasicPipeline):
    def __init__(
        self,
        llm_provider: LLMProvider,
        engine: Engine,
        **kwargs,
    ):
        self._components = {
            "generator": llm_provider.get_generator(
                system_prompt=sql_generation_system_prompt,
                generation_kwargs=SQL_GENERATION_MODEL_KWARGS,
            ),
            "prompt_builder": PromptBuilder(
                template=sql_generation_user_prompt_template
            ),
            "post_processor": SQLGenPostProcessor(engine=engine),
        }

        self._configs = {
            "alert": TEXT_TO_SQL_RULES,
        }

        super().__init__(
            AsyncDriver({}, sys.modules[__name__], result_builder=base.DictResult())
        )

    def visualize(
        self,
        query: str,
        contexts: List[str],
        exclude: List[Dict],
        samples: List[Dict] | None = None,
        project_id: str | None = None,
        configurations: AskConfigurations | None = None,
    ) -> None:
        destination = "outputs/pipelines/generation"
        if not Path(destination).exists():
            Path(destination).mkdir(parents=True, exist_ok=True)

        self._pipe.visualize_execution(
            ["post_process"],
            output_file_path=f"{destination}/sql_generation.dot",
            inputs={
                "query": query,
                "documents": contexts,
                "exclude": exclude,
                "samples": samples,
                "project_id": project_id,
                "configurations": configurations,
                **self._components,
                **self._configs,
            },
            show_legend=True,
            orient="LR",
        )

    @async_timer
    @observe(name="SQL Generation")
    async def run(
        self,
        query: str,
        contexts: List[str],
        exclude: List[Dict],
        samples: List[Dict] | None = None,
        project_id: str | None = None,
        configurations: AskConfigurations | None = None,
    ):
        logger.info("SQL Generation pipeline is running...")
        return await self._pipe.execute(
            ["post_process"],
            inputs={
                "query": query,
                "documents": contexts,
                "exclude": exclude,
                "samples": samples,
                "project_id": project_id,
                "configurations": configurations,
                **self._components,
                **self._configs,
            },
        )


if __name__ == "__main__":
    from langfuse.decorators import langfuse_context

    from src.core.engine import EngineConfig
    from src.core.pipeline import async_validate
    from src.providers import init_providers
    from src.utils import init_langfuse, load_env_vars

    load_env_vars()
    init_langfuse()

    llm_provider, _, _, engine = init_providers(engine_config=EngineConfig())
    pipeline = SQLGeneration(
        llm_provider=llm_provider,
        engine=engine,
    )

    pipeline.visualize("this is a test query", [], [])
    async_validate(lambda: pipeline.run("this is a test query", [], []))

    langfuse_context.flush()

# ambiguityLambda.py
# PointyHat — ambiguity check Lambda (text path only)
# Extracted from doJob — single responsibility: decide if query needs clarification.
#
# Input:  { jobId, jobType, query, refinedQuery }
# Output: {
#   jobId, jobType, query, refinedQuery,
#   is_ambiguous: bool,
#   clarification?: { reasoning, question, options },
#   refined_query: str
# }
#
# Logfire traces the full agent run automatically.

import json
import os
import boto3
from typing import Optional
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider
import logfire

# ── AWS clients ───────────────────────────────────────────
bedrock_client = boto3.client('bedrock-runtime', region_name='us-east-1')
ssm            = boto3.client('ssm', region_name='us-east-1')

SONNET_MODEL_ID     = os.environ['SONNET_MODEL_ID']
LOGFIRE_TOKEN_PARAM = os.environ['LOGFIRE_TOKEN_PARAM']

# ── Logfire cold start ────────────────────────────────────
def _configure_logfire():
    token = ssm.get_parameter(
        Name=LOGFIRE_TOKEN_PARAM, WithDecryption=True
    )['Parameter']['Value']
    logfire.configure(token=token, service_name='pointyhat-ambiguity')
    logfire.instrument_pydantic_ai()

_configure_logfire()

# ── Pydantic models ───────────────────────────────────────
class ClarifyingQuestion(BaseModel):
    reasoning: str
    question:  str
    options:   list[str]  # always ends with "Doesn't matter"

class AmbiguityCheck(BaseModel):
    is_ambiguous:  bool
    clarification: Optional[ClarifyingQuestion] = None

# ── Model factory ─────────────────────────────────────────
def make_model() -> BedrockConverseModel:
    return BedrockConverseModel(
        model_name=SONNET_MODEL_ID,
        provider=BedrockProvider(bedrock_client=bedrock_client),
    )

# ── Ambiguity agent ───────────────────────────────────────
AMBIGUITY_PROMPT = (
    'You are a hat search assistant. Decide if a query has enough detail '
    'to find a specific hat product page.\n\n'
    'If ambiguous: set is_ambiguous=true and provide one clarifying question '
    'with 2-5 options. Always include "Doesn\'t matter" last. '
    'Include your reasoning for why the question was needed.\n\n'
    'If specific enough: set is_ambiguous=false, leave clarification null.'
)

def run_ambiguity_check(query: str) -> AmbiguityCheck:
    agent = Agent(
        model=make_model(),
        output_type=AmbiguityCheck,
        system_prompt=AMBIGUITY_PROMPT,
    )
    result = agent.run_sync(f'Evaluate: "{query}"')
    return result.output

# ── Lambda handler ────────────────────────────────────────
def lambda_handler(event, context):
    job_id       = event['jobId']
    job_type     = event.get('jobType', 'text')
    refined      = event.get('refinedQuery') or event.get('query', '')

    print(f"ambiguityLambda: checking query '{refined}' for job {job_id}")

    check = run_ambiguity_check(refined)
    print(f"Ambiguity result: is_ambiguous={check.is_ambiguous}")

    output = {
        'jobId':        job_id,
        'jobType':      job_type,
        'query':        event.get('query', ''),
        'refinedQuery': refined,
        'refined_query': refined,
        'is_ambiguous': check.is_ambiguous,
    }

    if check.is_ambiguous and check.clarification:
        output['clarification'] = check.clarification.model_dump()

    return output

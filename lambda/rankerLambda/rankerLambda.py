# rankerLambda.py
# PointyHat — ranker Lambda (both paths)
# Extracted from doJob — single responsibility: pick top 3 recommendations.
#
# Input:  { jobId, jobType, refinedQuery, hacksearchData }
# Output: {
#   jobId, jobType,
#   results:   [ HatRecommendation x3 ],
#   reasoning: { model, candidates_evaluated, reasoning, intent }
# }
#
# Logfire traces the full agent run automatically.

import json
import os
from decimal import Decimal
from typing import Optional
from enum import Enum
import boto3
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
    logfire.configure(token=token, service_name='pointyhat-ranker')
    logfire.instrument_pydantic_ai()

_configure_logfire()

# ── Pydantic models ───────────────────────────────────────
class RelevanceLevel(str, Enum):
    LOW    = 'low'
    MEDIUM = 'medium'
    HIGH   = 'high'

class HatRecommendation(BaseModel):
    title:     str
    url:       str
    image_url: Optional[str] = None
    retailer:  str
    price:     Optional[str] = None
    reason:    str
    relevance: RelevanceLevel

class RecommendationSet(BaseModel):
    recommendations: list[HatRecommendation]
    reasoning:       str

# ── Helpers ───────────────────────────────────────────────
def convert_floats(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: convert_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_floats(i) for i in obj]
    return obj

def make_model() -> BedrockConverseModel:
    return BedrockConverseModel(
        model_name=SONNET_MODEL_ID,
        provider=BedrockProvider(bedrock_client=bedrock_client),
    )

# ── Ranker agent ──────────────────────────────────────────
RANKER_PROMPT = (
    'You are an expert hat curator. Select the top 3 hats from the candidates '
    'that best match the query.\n\n'
    '- Use all candidate fields to evaluate fit\n'
    '- Prefer candidates with has_image: true\n'
    '- relevance: "high" = exact match, "medium" = close, "low" = partial\n'
    '- Write a short reason per pick (1-2 sentences)\n'
    '- Include overall reasoning explaining your selections\n\n'
    'Return exactly 3 recommendations.'
)

def run_ranker(query: str, hacksearch_data: dict) -> RecommendationSet:
    intent     = hacksearch_data.get('intent', {})
    candidates = hacksearch_data.get('candidates', [])

    candidates_text = json.dumps([{
        'title':       c.get('title', ''),
        'url':         c.get('url', ''),
        'retailer':    c.get('retailer', ''),
        'price':       c.get('price'),
        'description': c.get('description', '')[:150],
        'color_text':  (c.get('color_text') or '')[:80],
        'style':       c.get('style'),
        'has_image':   c.get('image') is not None,
    } for c in candidates], indent=2)

    agent = Agent(
        model=make_model(),
        output_type=RecommendationSet,
        system_prompt=RANKER_PROMPT,
    )

    prompt = (
        f'Query: "{query}"\n'
        f'Intent — colors: {intent.get("colors", [])} '
        f'styles: {intent.get("styles", [])} '
        f'team: {intent.get("team", "none")}\n\n'
        f'Candidates:\n{candidates_text}'
    )

    result = agent.run_sync(prompt)
    response = result.output

    # Backfill image_url from candidates by URL match
    url_to_image = {c['url']: c.get('image') for c in candidates}
    for rec in response.recommendations:
        if rec.image_url is None:
            rec.image_url = url_to_image.get(rec.url)

    return response

# ── Lambda handler ────────────────────────────────────────
def lambda_handler(event, context):
    job_id         = event.get('jobId', event.get('job_id', 'unknown'))
    job_type       = event.get('jobType', 'text')
    refined_query  = event.get('refinedQuery') or event.get('refined_query', '')
    # hacksearch returns candidates + intent at top level or nested
    hacksearch_data = event.get('hacksearchData') or {
        'candidates': event.get('candidates', []),
        'intent':     event.get('intent', {}),
    }

    print(f"rankerLambda: ranking for '{refined_query}', job {job_id}")

    recs = run_ranker(refined_query, hacksearch_data)
    print(f"Got {len(recs.recommendations)} recommendations")

    candidates = hacksearch_data.get('candidates', [])
    intent     = hacksearch_data.get('intent', {})

    results_dicts = convert_floats([r.model_dump() for r in recs.recommendations])
    reasoning     = convert_floats({
        'model':                SONNET_MODEL_ID,
        'candidates_evaluated': len(candidates),
        'reasoning':            recs.reasoning,
        'intent': {
            'team':            intent.get('team', ''),
            'colors':          intent.get('colors', []),
            'styles':          intent.get('styles', []),
            'design_keywords': intent.get('design_keywords', []),
            'search_variants': intent.get('search_variants', [])[:3],
        },
    })

    return {
        'jobId':     job_id,
        'jobType':   job_type,
        'results':   results_dicts,
        'reasoning': reasoning,
    }

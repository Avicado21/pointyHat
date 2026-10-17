# visionLambda.py
# PointyHat — vision agent Lambda (image path only)
# Extracted from doJob — single responsibility: analyze outfit image.
#
# Input:  { jobId, jobType, imageBase64, imageMimeType, fetchImageFromDb? }
# Output: {
#   jobId, jobType,
#   image_analysis: { colors, style_signals, outfit_context,
#                     hat_suggestions, suggested_hat_color,
#                     question, reasoning },
#   clarification: { reasoning, question, options },
#   refined_query: suggested_hat_color  (base for query assembly)
# }
#
# Logfire traces the full agent run automatically via instrument_pydantic_ai().

import json
import os
import base64
import boto3
from typing import Optional
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider
import logfire

# ── AWS clients ───────────────────────────────────────────
dynamodb       = boto3.resource('dynamodb', region_name='us-east-1')
bedrock_client = boto3.client('bedrock-runtime', region_name='us-east-1')
ssm            = boto3.client('ssm', region_name='us-east-1')

SONNET_MODEL_ID    = os.environ['SONNET_MODEL_ID']
LOGFIRE_TOKEN_PARAM = os.environ['LOGFIRE_TOKEN_PARAM']
TABLE_NAME         = os.environ['TABLE_NAME']

# ── Logfire cold start ────────────────────────────────────
def _configure_logfire():
    token = ssm.get_parameter(
        Name=LOGFIRE_TOKEN_PARAM, WithDecryption=True
    )['Parameter']['Value']
    logfire.configure(token=token, service_name='pointyhat-vision')
    logfire.instrument_pydantic_ai()

_configure_logfire()

# ── Pydantic models ───────────────────────────────────────
class OutfitAnalysis(BaseModel):
    colors:               list[str]  # dominant clothing colors, no hat types
    style_signals:        list[str]  # vibe descriptors specific to this outfit
    outfit_context:       str        # one sentence describing what they are wearing
    hat_suggestions:      list[str]  # style names only, no colors. Last = "Something else"
    suggested_hat_color:  str        # one color that aesthetically complements the outfit
    question:             str        # warm sentence noting outfit + asking hat style
    reasoning:            str        # full analysis — collapsible drawer + Logfire

# ── Model factory ─────────────────────────────────────────
def make_model() -> BedrockConverseModel:
    return BedrockConverseModel(
        model_name=SONNET_MODEL_ID,
        provider=BedrockProvider(bedrock_client=bedrock_client),
    )

# ── Image retrieval ───────────────────────────────────────
def get_image_data(event: dict) -> tuple[str, str]:
    """
    Get image base64 + mime type from event or DynamoDB.
    Large images set fetchImageFromDb=True in SF payload.
    """
    if event.get('imageBase64'):
        return event['imageBase64'], event.get('imageMimeType', 'image/jpeg')
    if event.get('fetchImageFromDb'):
        item = dynamodb.Table(TABLE_NAME).get_item(
            Key={ 'jobId': event['jobId'] }
        ).get('Item', {})
        return item.get('imageBase64', ''), item.get('imageMimeType', 'image/jpeg')
    return '', 'image/jpeg'

# ── Vision agent ──────────────────────────────────────────
VISION_PROMPT = (
    'You are a hat stylist. Analyze the outfit in the image and extract:\n'
    '- colors: dominant clothing colors (not skin or background)\n'
    '- style_signals: vibe descriptors specific to this outfit\n'
    '- outfit_context: one sentence describing what the person is wearing\n'
    '- hat_suggestions: 3-5 hat styles that complement this outfit\n'
    '  Style names only, no colors. Last option must be "Something else"\n'
    '- suggested_hat_color: one color that aesthetically complements this outfit\n'
    '  Consider contrast and harmony, not just mirroring outfit colors\n'
    '- question: one warm sentence noting what they are wearing and asking what hat style they want\n'
    '- reasoning: full analysis covering palette, formality, and style choices\n'
)

def run_vision_agent(image_base64: str, mime_type: str) -> OutfitAnalysis:
    image_bytes = base64.b64decode(image_base64)
    agent = Agent(
        model=make_model(),
        output_type=OutfitAnalysis,
        system_prompt=VISION_PROMPT,
    )
    result = agent.run_sync([
        BinaryContent(data=image_bytes, media_type=mime_type),
        'Analyze this outfit and suggest appropriate hat styles.',
    ])
    return result.output

# ── Lambda handler ────────────────────────────────────────
def lambda_handler(event, context):
    job_id   = event['jobId']
    job_type = event.get('jobType', 'image')

    print(f"visionLambda: analyzing outfit for job {job_id}")

    image_b64, mime_type = get_image_data(event)
    if not image_b64:
        raise RuntimeError(f"No image data found for job {job_id}")

    analysis = run_vision_agent(image_b64, mime_type)
    print(f"Vision complete — suggestions: {analysis.hat_suggestions}")

    # Build image_analysis dict for DynamoDB + frontend outfit drawer
    image_analysis = {
        'colors':              analysis.colors,
        'style_signals':       analysis.style_signals,
        'outfit_context':      analysis.outfit_context,
        'hat_suggestions':     analysis.hat_suggestions,
        'suggested_hat_color': analysis.suggested_hat_color,
        'question':            analysis.question,
        'reasoning':           analysis.reasoning,
    }

    # Build ClarifyingQuestion for writerLambda CLARIFICATION write
    # options = hat_suggestions (already includes "Something else")
    clarification = {
        'reasoning': analysis.reasoning,
        'question':  analysis.question,
        'options':   analysis.hat_suggestions,
    }

    # refined_query carries suggested_hat_color only — no hat type yet.
    # Frontend builds final query: suggested_hat_color + user chip choice.
    return {
        'jobId':        job_id,
        'jobType':      job_type,
        'image_analysis': image_analysis,
        'clarification':  clarification,
        'refined_query':  analysis.suggested_hat_color,
        # pass through any other fields from the event
        **{k: v for k, v in event.items()
           if k not in ('jobId', 'jobType', 'imageBase64', 'imageMimeType', 'fetchImageFromDb')},
    }

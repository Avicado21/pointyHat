# resumeJob.py
# PointyHat — POST /resume handler
# Called when user picks a clarification chip or submits custom input.
# Reads taskToken from DynamoDB, calls SendTaskSuccess to resume
# the paused Step Functions execution at HackSearch.
#
# Input:  { jobId, choice, imageAnalysis? }
# Output: { jobId, status: "RESUMING" }
#
# choice + imageAnalysis.suggested_hat_color are used to build
# the refined query: suggested_hat_color + choice.lower()
# e.g. "charcoal" + "Fedora" → "charcoal fedora"
#
# For text path clarification: choice appended to refinedQuery directly.

import json
import os
import boto3

sfn      = boto3.client('stepfunctions', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

TABLE_NAME = os.environ['TABLE_NAME']

SKIP_CHOICES = {'something else', "doesn't matter", 'other'}


def lambda_handler(event, context):
    try:
        body = json.loads(event.get('body', '{}'))
    except Exception:
        body = {}

    job_id         = body.get('jobId')
    choice         = body.get('choice', '').strip()
    image_analysis = body.get('imageAnalysis')  # passed from frontend for image jobs

    if not job_id or not choice:
        return {
            'statusCode': 400,
            'headers': { 'Access-Control-Allow-Origin': '*' },
            'body': json.dumps({ 'error': 'jobId and choice are required' })
        }

    # ── Read taskToken + current state from DynamoDB ──────
    table = dynamodb.Table(TABLE_NAME)
    item  = table.get_item(Key={ 'jobId': job_id }).get('Item')

    if not item:
        return {
            'statusCode': 404,
            'headers': { 'Access-Control-Allow-Origin': '*' },
            'body': json.dumps({ 'error': 'Job not found' })
        }

    task_token = item.get('task_token')
    if not task_token:
        return {
            'statusCode': 409,
            'headers': { 'Access-Control-Allow-Origin': '*' },
            'body': json.dumps({ 'error': 'Job is not waiting for clarification' })
        }

    job_type    = item.get('jobType', 'text')
    refined_base = item.get('refined_query', item.get('query', ''))

    # ── Build refined query ───────────────────────────────
    # Image path: suggested_hat_color + user's chosen style
    # Text path:  existing refined_query + user's choice
    if job_type == 'image' and image_analysis:
        hat_color = (image_analysis.get('suggested_hat_color') or '').lower().strip()
        if choice.lower() in SKIP_CHOICES:
            refined_query = (hat_color + ' hat').strip()
        else:
            refined_query = (hat_color + ' ' + choice.lower()).strip()
    else:
        # Text path
        if choice.lower() in SKIP_CHOICES:
            refined_query = refined_base
        else:
            refined_query = (refined_base + ' ' + choice.lower()).strip()

    print(f"resumeJob: job {job_id} — choice='{choice}' refined='{refined_query}'")

    # ── SendTaskSuccess — resumes SF execution ────────────
    # SF picks up from after writeClarification state
    # and continues to hackSearchTask with refinedQuery in payload
    sfn.send_task_success(
        taskToken=task_token,
        output=json.dumps({
            'jobId':        job_id,
            'jobType':      job_type,
            'refinedQuery': refined_query,
            'refined_query': refined_query,
            'query':        item.get('query', ''),
        })
    )

    # Clear task_token immediately after SendTaskSuccess so duplicate
    # calls from the frontend fail gracefully with 409 instead of erroring
    table.update_item(
        Key={ 'jobId': job_id },
        UpdateExpression='REMOVE task_token SET #s = :s',
        ExpressionAttributeNames={ '#s': 'status' },
        ExpressionAttributeValues={ ':s': 'IN_PROGRESS' },
    )
    print(f"SF execution resumed for job {job_id} with query '{refined_query}'")

    return {
        'statusCode': 200,
        'headers': { 'Access-Control-Allow-Origin': '*' },
        'body': json.dumps({
            'jobId':   job_id,
            'status':  'RESUMING',
            'refined': refined_query,
        })
    }

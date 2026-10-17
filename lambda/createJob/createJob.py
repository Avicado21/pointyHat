# createJob.py
# PointyHat — POST /search handler
# Trigger only — validates input, generates jobId, starts SF execution.
# No DynamoDB write — writerLambda owns all writes via the state machine.
#
# Text path:  { query, refinedQuery, isRefined }
# Image path: { imageBase64, imageMimeType, jobType: "image" }
#
# Large images (>150KB base64) are written to DynamoDB here
# so visionLambda can fetch them — SF payload has a 256KB limit.

import json
import os
import uuid
import boto3

sfn      = boto3.client('stepfunctions', region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

STATE_MACHINE_ARN = os.environ['STATE_MACHINE_ARN']
TABLE_NAME        = os.environ['TABLE_NAME']

# Images larger than this are stored in DynamoDB instead of inlined in SF input
SF_IMAGE_INLINE_LIMIT = 200000  # ~150KB raw


def lambda_handler(event, context):
    try:
        body = json.loads(event.get('body', '{}'))
    except Exception:
        body = {}

    image_base64 = body.get('imageBase64')
    image_mime   = body.get('imageMimeType', 'image/jpeg')
    job_type     = 'image' if image_base64 else 'text'
    query        = body.get('query', '').strip()

    if job_type == 'text' and not query:
        return {
            'statusCode': 400,
            'headers': { 'Access-Control-Allow-Origin': '*' },
            'body': json.dumps({ 'error': 'No query or image provided' })
        }

    refined_query = body.get('refinedQuery', query).strip()
    is_refined    = bool(body.get('isRefined', False))
    job_id        = str(uuid.uuid4())

    # ── Build SF execution input ──────────────────────────
    sf_input = {
        'jobId':        job_id,
        'jobType':      job_type,
        'query':        query,
        'refinedQuery': refined_query,
        'isRefined':    is_refined,
    }

    if job_type == 'image':
        sf_input['imageMimeType'] = image_mime
        if len(image_base64) < SF_IMAGE_INLINE_LIMIT:
            # Small enough to inline in SF execution input
            sf_input['imageBase64'] = image_base64
        else:
            # Too large — write to DynamoDB, visionLambda reads from there
            sf_input['fetchImageFromDb'] = True
            dynamodb.Table(TABLE_NAME).put_item(Item={
                'jobId':        job_id,
                'status':       'PENDING',
                'jobType':      job_type,
                'imageBase64':  image_base64,
                'imageMimeType': image_mime,
            })
            print(f"Large image stored in DynamoDB for job {job_id}")

    # ── Start Step Functions execution ────────────────────
    # Execution name must be unique — use jobId
    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=job_id,
        input=json.dumps(sf_input),
    )
    print(f"Started SF execution for job {job_id} ({job_type})")

    return {
        'statusCode': 202,
        'headers': { 'Access-Control-Allow-Origin': '*' },
        'body': json.dumps({
            'jobId':   job_id,
            'status':  'PENDING',
            'jobType': job_type,
        })
    }

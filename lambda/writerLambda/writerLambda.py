# writerLambda.py
# PointyHat — single source of truth for all DynamoDB writes.
# Called by Step Functions at every state transition.
#
# writeType: PENDING      — initial job record, called by SF first state
# writeType: CLARIFICATION — question + options + taskToken stored
#                            SF pauses here (waitForTaskToken)
#                            resumeJob will call SendTaskSuccess to continue
# writeType: COMPLETE     — results + reasoning written, job done
# writeType: FAILED       — error written, terminal state
#
# Input shape from SF:
#   {
#     writeType: str,
#     jobId:     str,
#     taskToken: str (CLARIFICATION only — injected by SF),
#     data:      { ...full state machine payload }
#   }

import json
import os
import time
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
TABLE_NAME = os.environ['TABLE_NAME']


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def convert_floats(obj):
    """Recursively convert floats to Decimal for DynamoDB storage."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: convert_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_floats(i) for i in obj]
    return obj


def lambda_handler(event, context):
    table      = dynamodb.Table(TABLE_NAME)
    write_type = event.get('writeType')
    job_id     = event.get('jobId')
    data       = event.get('data', {})
    task_token = event.get('taskToken')  # only present on CLARIFICATION

    if not job_id:
        raise ValueError("jobId is required")

    print(f"writerLambda: {write_type} for job {job_id}")

    # ── PENDING ──────────────────────────────────────────────
    # First write — establishes the job record in DynamoDB.
    # image_base64 stored here for large images that exceed SQS limit.
    if write_type == 'PENDING':
        ttl  = int(time.time()) + 86400  # 24hr TTL
        item = {
            'jobId':   job_id,
            'status':  'PENDING',
            'jobType': data.get('jobType', 'text'),
            'query':   data.get('query', ''),
            'ttl':     ttl,
        }
        # Store image in DynamoDB — authoritative source for large images
        if data.get('imageBase64'):
            item['imageBase64']   = data['imageBase64']
            item['imageMimeType'] = data.get('imageMimeType', 'image/jpeg')

        table.put_item(Item=item)
        print(f"Job {job_id} — PENDING written")
        return { 'jobId': job_id, 'jobType': item['jobType'], 'status': 'PENDING', **data }

    # ── CLARIFICATION ─────────────────────────────────────────
    # Writes clarification question + options to DynamoDB.
    # Stores taskToken so resumeJob can call SendTaskSuccess later.
    # Also writes image_analysis for image jobs (drives outfit drawer).
    elif write_type == 'CLARIFICATION':
        if not task_token:
            raise ValueError("taskToken required for CLARIFICATION write")

        update_expr   = 'SET #s = :s, clarification = :c, refined_query = :rq, task_token = :tt'
        expr_names    = { '#s': 'status' }
        expr_values   = {
            ':s':  'CLARIFICATION',
            ':c':  convert_floats(data.get('clarification', {})),
            ':rq': data.get('refined_query', data.get('suggested_hat_color', '')),
            ':tt': task_token,
        }

        # image_analysis — written for image jobs, drives outfit drawer
        image_analysis = data.get('image_analysis')
        if image_analysis:
            update_expr += ', image_analysis = :ia'
            expr_values[':ia'] = convert_floats(image_analysis)

        table.update_item(
            Key={ 'jobId': job_id },
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        print(f"Job {job_id} — CLARIFICATION written, taskToken stored")
        # Return nothing — SF is now paused waiting for SendTaskSuccess
        return { 'jobId': job_id, 'status': 'CLARIFICATION' }

    # ── COMPLETE ──────────────────────────────────────────────
    # Final write — results + reasoning stored, job done.
    # results and reasoning come from rankerLambda output via SF payload.
    elif write_type == 'COMPLETE':
        results   = convert_floats(data.get('results', []))
        reasoning = convert_floats(data.get('reasoning', {}))

        if not results:
            print(f"WARNING: No results in COMPLETE write for job {job_id}")

        table.update_item(
            Key={ 'jobId': job_id },
            UpdateExpression='SET #s = :s, results = :r, reasoning = :rs',
            ExpressionAttributeNames={ '#s': 'status' },
            ExpressionAttributeValues={
                ':s':  'COMPLETE',
                ':r':  results,
                ':rs': reasoning,
            },
        )
        print(f"Job {job_id} — COMPLETE written, {len(results)} results")
        return { 'jobId': job_id, 'status': 'COMPLETE' }

    # ── FAILED ────────────────────────────────────────────────
    # Terminal error state — written via addCatch on any SF task.
    elif write_type == 'FAILED':
        error = data.get('error', data.get('Cause', 'Unknown error'))
        table.update_item(
            Key={ 'jobId': job_id },
            UpdateExpression='SET #s = :s, #e = :e',
            ExpressionAttributeNames={ '#s': 'status', '#e': 'error' },
            ExpressionAttributeValues={ ':s': 'FAILED', ':e': str(error) },
        )
        print(f"Job {job_id} — FAILED written: {error}")
        return { 'jobId': job_id, 'status': 'FAILED' }

    else:
        raise ValueError(f"Unknown writeType: {write_type}")

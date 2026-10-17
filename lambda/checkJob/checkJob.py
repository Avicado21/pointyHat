# checkJob.py
# PointyHat — GET /job/{jobId} handler
# Reads job status from DynamoDB and returns the appropriate payload.
#
# Status responses:
#   PENDING / IN_PROGRESS → { status }
#   CLARIFICATION         → { status, clarification, refined_query, image_analysis? }
#   COMPLETE              → { status, results, reasoning, image_analysis? }
#   FAILED                → { status, error }
#
# image_analysis is only present on image jobs and powers the
# "what we saw in your outfit" drawer on the frontend.

import json
import os
import boto3
from decimal import Decimal


class DecimalEncoder(json.JSONEncoder):
    """Convert DynamoDB Decimal types to float for JSON serialization."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def lambda_handler(event, context):
    table  = boto3.resource('dynamodb', region_name='us-east-1').Table(os.environ['TABLE_NAME'])
    job_id = event['pathParameters']['jobId']

    response = table.get_item(Key={'jobId': job_id})
    item = response.get('Item')

    if not item:
        return {
            'statusCode': 404,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*',
            },
            'body': json.dumps({'error': 'Job not found'})
        }

    status   = item.get('status', 'PENDING')
    job_type = item.get('jobType', 'text')
    result   = {'status': status, 'jobType': job_type}

    if status == 'COMPLETE':
        result['results']   = item.get('results', [])
        result['reasoning'] = item.get('reasoning', {})
        # image_analysis drives the "what we saw" outfit drawer
        if job_type == 'image':
            result['image_analysis'] = item.get('image_analysis', {})

    elif status == 'CLARIFICATION':
        result['clarification'] = item.get('clarification', {})
        result['refined_query'] = item.get('refined_query', '')
        # Return image_analysis during clarification so the frontend
        # can show the outfit drawer before the user answers the question
        if job_type == 'image':
            result['image_analysis'] = item.get('image_analysis', {})

    elif status == 'FAILED':
        result['error'] = item.get('error', 'Unknown error')

    # PENDING / IN_PROGRESS return only { status, jobType }

    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
        },
        'body': json.dumps(result, cls=DecimalEncoder)
    }

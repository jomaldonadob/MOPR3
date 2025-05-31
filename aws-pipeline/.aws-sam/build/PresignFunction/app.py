import os
import json
import boto3

s3 = boto3.client('s3')

def handler(event, context):
    body = json.loads(event['body'])
    key = body['key']            # p. ej. "coordenates.csv"
    bucket = os.environ['BUCKET']  # tu bucket, p.ej. "mopr4-coordinates-bucket"

    # Genero la URL presign con la condición de que Content-Type sea "text/csv"
    presigned = s3.generate_presigned_url(
        ClientMethod='put_object',
        Params={
            'Bucket': bucket,
            'Key': key,
            'ContentType': 'text/csv'
        },
        ExpiresIn=900  # expira en 900 segundos (15 min)
    )

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'url': presigned})
    }

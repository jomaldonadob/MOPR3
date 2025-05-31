import os
import json
import csv
import boto3
from urllib.parse import unquote_plus

s3 = boto3.client('s3')
sqs = boto3.client('sqs')

# Estas variables deben coincidir con las que configuraste en tu SAM template:
QUEUE_URL = os.environ['QUEUE_URL']    # URL de tu SQS (p. ej. https://sqs.us-east-1.amazonaws.com/123445/CoordinatesQueue)
TABLE   = os.environ['TABLE']          # Nombre de tu DynamoDB, aunque acá Ingest solo mandará a SQS

def handler(event, context):
    """
    Cuando S3 sube un objeto “coordenates.csv”, esta función se dispara.
    Debe descargar el CSV, parsearlo y enviar cada fila a SQS.
    """
    # 1) Obtenemos los datos del evento S3
    for record in event.get('Records', []):
        # El nombre de bucket
        bucket_name = record['s3']['bucket']['name']
        # El key (ruta) del objeto en el bucket
        key = unquote_plus(record['s3']['object']['key'])

        print(f"[IngestFunction] Evento S3 - bucket: {bucket_name}, key: {key}")

        # 2) Bajamos el CSV a /tmp (el único lugar con escritura en Lambda)
        tmp_path = f"/tmp/{os.path.basename(key)}"
        try:
            s3.download_file(bucket_name, key, tmp_path)
            print(f"[IngestFunction] Descargado archivo a {tmp_path}")
        except Exception as e:
            print(f"[IngestFunction][Error] Al descargar {key} de {bucket_name}: {e}")
            continue  # Pasamos a la siguiente fila del evento, si hubiera más

        # 3) Parseamos el CSV (asumiré que tiene un header “id,latitude,longitude”)
        #    y enviamos cada línea a SQS como JSON en el body.
        filas_enviadas = 0
        try:
            with open(tmp_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Convierte row (un diccionario) a JSON para enviarlo como MessageBody
                    msg_body = json.dumps({
                        "id": row.get("id", ""),
                        "latitude": row.get("latitude", ""),
                        "longitude": row.get("longitude", "")
                    })
                    # 4) Enviamos a SQS
                    try:
                        response = sqs.send_message(
                            QueueUrl=QUEUE_URL,
                            MessageBody=msg_body
                        )
                        filas_enviadas += 1
                    except Exception as sqs_err:
                        print(f"[IngestFunction][Error] Al enviar a SQS: {sqs_err}")
                        # (Podrías optar por seguir o abortar; aquí seguimos intentando el resto)
            print(f"[IngestFunction] Mensajes enviados a SQS: {filas_enviadas}")
        except Exception as parse_err:
            print(f"[IngestFunction][Error] Al leer/parsear CSV en {tmp_path}: {parse_err}")

    return {
        "statusCode": 200,
        "body": json.dumps({"message": f"Procesé {filas_enviadas} filas (si no hubo errores)."})
    }

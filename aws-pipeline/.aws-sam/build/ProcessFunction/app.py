import os, json, requests, boto3
from requests.adapters import HTTPAdapter, Retry

dynamodb = boto3.resource("dynamodb")
TABLE = dynamodb.Table(os.environ["TABLE"])

# Sesión con back-off
sess = requests.Session()
sess.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))

def handler(event, context):
    for rec in event["Records"]:
        ids = json.loads(rec["body"])
        for id in ids:
            # recupera item
            resp = TABLE.get_item(Key={"id": id})
            if "Item" not in resp:
                continue
            item = resp["Item"]
            lat = item["latitude"]
            lon = item["longitude"]
            try:
                r = sess.get(f"https://api.postcodes.io/postcodes?lon={lon}&lat={lat}")
                r.raise_for_status()
                result = r.json()["result"][0]["postcode"]
                TABLE.update_item(
                    Key={"id": id},
                    UpdateExpression="SET postcode=:p, status=:s",
                    ExpressionAttributeValues={":p": result, ":s": "OK"}
                )
            except Exception as e:
                TABLE.update_item(
                    Key={"id": id},
                    UpdateExpression="SET status=:s, error=:e",
                    ExpressionAttributeValues={":s": "ERROR", ":e": str(e)}
                )
    return {"status": "processed"}

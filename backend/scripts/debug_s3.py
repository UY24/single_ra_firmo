import asyncio
import os
import json
import sys
from dotenv import load_dotenv

# Add ctest folder to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from app.services.serpwow.engine import get_s3_client, read_upload_artifact

async def test():
    upload_id = "fb2884b0-f38c-4776-bf7f-582028f59522"
    print("Initializing boto3 client...")
    client = get_s3_client()
    bucket = os.getenv("S3_BUCKET")
    print(f"Bucket: {bucket}")
    
    key = f"single_ra_isi/{upload_id}/state.json"
    print(f"Attempting to fetch key: {key}")
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        print("Success! Response metadata:")
        print(response.get("ResponseMetadata"))
        print("Reading body...")
        body = response["Body"].read()
        print(f"Body read successfully! Length: {len(body)} bytes")
        data = json.loads(body.decode("utf-8"))
        print(f"JSON parsed successfully! Keys: {list(data.keys())}")
        print(f"Number of rows: {len(data.get('rows', []))}")
    except Exception as e:
        print(f"Error fetching state: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(test())

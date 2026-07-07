import asyncio
import os
import json
import sys
from dotenv import load_dotenv

# Allow importing project modules when executing from scripts/ directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from app.services.serpwow import engine as app

async def main():
    upload_id = "d6584620-0e3d-46e9-8c7a-3ca06db5b087"
    try:
        state = await app.read_upload_artifact(upload_id, "state")
        print("Upload ID:", state.get("upload_id"))
        print("Pipeline:", state.get("pipeline"))
        print("Status:", state.get("status"))
        print("Total Rows:", state.get("total_rows"))
        print("Processed Rows:", state.get("processed_rows"))
        print("Success Rows:", state.get("success_rows"))
        print("Failed Rows:", state.get("failed_rows"))
        
        statuses = {}
        for r in state.get("rows", []):
            st = r.get("status")
            statuses[st] = statuses.get(st, 0) + 1
        print("Row Statuses Breakdown:", statuses)
        
        # Check first 5 rows that are not completed
        not_completed = [r for r in state.get("rows", []) if r.get("status") not in {"completed", "failed"}]
        print("First 5 not completed rows:")
        for r in not_completed[:5]:
            print(f"Row {r.get('row_index')}: status={r.get('status')}, company={r.get('company_name')}")
            
    except Exception as e:
        print("Error reading state:", e)

if __name__ == "__main__":
    asyncio.run(main())

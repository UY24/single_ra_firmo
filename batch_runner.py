import argparse
import asyncio
import os
import sys

# Allow importing the backend package when executing from the repo root.
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.services.serpwow import legacy_app as app


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Gemini batch post-processing for a specific upload_id."
    )
    parser.add_argument("upload_id", help="Upload ID to process")
    args = parser.parse_args()

    await app.run_gemini_batch_for_upload(args.upload_id)
    print(f"Gemini batch processing finished for upload_id={args.upload_id}")


if __name__ == "__main__":
    asyncio.run(main())

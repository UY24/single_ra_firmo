import argparse
import asyncio

import app


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

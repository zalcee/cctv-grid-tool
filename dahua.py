import os
import math
import json
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from io import BytesIO

from dotenv import load_dotenv
import httpx
from PIL import Image

# Load environment variables
load_dotenv()

NVR_IP = os.getenv("NVR_IP")
USERNAME = os.getenv("NVR_USERNAME")
PASSWORD = os.getenv("NVR_PASSWORD")

channels_env = os.getenv("CHANNELS", "")
CHANNELS = [int(ch.strip()) for ch in channels_env.split(",")] if channels_env else []

if not NVR_IP or not USERNAME or not PASSWORD or not CHANNELS:
    print("Error: Missing required environment configuration variables.")
    exit(1)

# Ensure uploads directory exists
uploads_dir = Path("uploads")
uploads_dir.mkdir(parents=True, exist_ok=True)

# Define Philippine Time (PHT) UTC+8
PHT = timezone(timedelta(hours=8))

# --- HELPER FUNCTIONS ---

def get_pht_iso_string(dt: datetime) -> str:
    """Converts a datetime object to a PHT (+08:00) formatted ISO string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PHT).isoformat()

def parse_dahua_response(text: str) -> dict:
    """Parses Dahua's plain text key=value response into a Python dictionary."""
    result = {}
    for line in text.strip().split('\n'):
        if '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip()
    return result

async def get_channel_snapshot(client: httpx.AsyncClient, channel: int):
    """Captures and resizes a JPEG snapshot from a Dahua NVR channel."""
    # Dahua snapshot CGI endpoint
    url = f"http://{NVR_IP}/cgi-bin/snapshot.cgi?channel={channel}"
    try:
        response = await client.get(url)
        if response.status_code != 200:
            print(f"[Channel {channel}] Snapshot fetch failed with status {response.status_code}")
            return None
        
        # Open image from bytes and resize using Pillow
        img = Image.open(BytesIO(response.content))
        return img.resize((640, 360), Image.Resampling.LANCZOS)
    except Exception as e:
        print(f"[Channel {channel}] Snapshot error: {str(e)}")
        return None

async def get_channel_retention(client: httpx.AsyncClient, channel: int):
    """Queries true retention metadata for a specific channel using Dahua's media search session."""
    now = datetime.now(PHT)
    hundred_days_ago = now - timedelta(days=100)

    # Dahua requires date strings in 'YYYY-MM-DD HH:MM:SS' format
    start_str = hundred_days_ago.strftime("%Y-%m-%d %H:%M:%S")
    end_str = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Step 1: Create a search session
        create_url = f"http://{NVR_IP}/cgi-bin/mediaFileFind.cgi?action=factory.create"
        res_create = await client.get(create_url)
        parsed_create = parse_dahua_response(res_create.text)
        
        obj_id = parsed_create.get("result")
        if not obj_id:
            return {"hasRecording": False, "retentionDays": 0, "message": "Failed to create Dahua search session"}

        # Step 2: Set the search conditions for this session (searching for standard .dav video files)
        cond_url = (f"http://{NVR_IP}/cgi-bin/mediaFileFind.cgi?action=findFile"
                    f"&object={obj_id}&condition.Channel={channel}"
                    f"&condition.StartTime={start_str}&condition.EndTime={end_str}"
                    f"&condition.Types[0]=dav")
        await client.get(cond_url)

        # Step 3: Pull the very first (oldest) result from the search session
        next_url = f"http://{NVR_IP}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={obj_id}&count=1"
        res_next = await client.get(next_url)
        parsed_next = parse_dahua_response(res_next.text)

        # Step 4: Always safely close the session to free up NVR memory
        destroy_url = f"http://{NVR_IP}/cgi-bin/mediaFileFind.cgi?action=factory.destroy&object={obj_id}"
        await client.get(destroy_url)

        # Parse the extracted retention data
        found = parsed_next.get("found", "0")
        if found == "0" or "items[0].StartTime" not in parsed_next:
            return {"hasRecording": False, "retentionDays": 0, "message": "No recording found"}

        oldest_str = parsed_next["items[0].StartTime"]
        
        # Convert Dahua time string back to a Python datetime object (assuming NVR matches PHT time)
        oldest_date = datetime.strptime(oldest_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PHT)
        
        diff_time = now - oldest_date
        
        return {
            "hasRecording": True,
            "oldestRecording": get_pht_iso_string(oldest_date),
            "latestRecording": get_pht_iso_string(now),
            "retentionDays": math.ceil(diff_time.total_seconds() / (24 * 3600))
        }
    except Exception as e:
        return {"status": "Failed parsing retention details", "error": str(e)}

# --- MAIN EXECUTION LOGIC ---

async def generate_collage_and_retention():
    # Use HTTPX AsyncClient with Digest Auth (Standard for Dahua)
    auth = httpx.DigestAuth(USERNAME, PASSWORD)
    
    pht_now = datetime.now(PHT)
    pht_date_stamp = pht_now.strftime("%Y-%m-%d")

    print(f"\nProcessing feeds and calculating real retention for {len(CHANNELS)} Dahua channels...")

    async with httpx.AsyncClient(auth=auth, timeout=30.0) as client:
        # Create concurrent tasks for snapshots and retention
        tasks = []
        for channel in CHANNELS:
            task = asyncio.gather(
                get_channel_snapshot(client, channel),
                get_channel_retention(client, channel)
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)

    valid_snapshots = []
    retention_data = {}

    for i, (snapshot, retention) in enumerate(results):
        channel = CHANNELS[i]
        retention_data[f"channel_{channel}"] = retention
        if snapshot is not None:
            valid_snapshots.append(snapshot)

    if not valid_snapshots:
        print("Error: Could not pull valid image feeds from any configured channel.")
        return

    # Dynamic Canvas Grid Math
    tile_width = 640
    tile_height = 360
    cols = math.ceil(math.sqrt(len(valid_snapshots)))
    rows = math.ceil(len(valid_snapshots) / cols)

    # Generate final composite collage using Pillow
    collage = Image.new("RGB", (cols * tile_width, rows * tile_height), (0, 0, 0))

    for index, img in enumerate(valid_snapshots):
        x = (index % cols) * tile_width
        y = (index // cols) * tile_height
        collage.paste(img, (x, y))

    # Save image collage and JSON metadata to disk
    image_path = uploads_dir / f"{pht_date_stamp}-dahua-collage.jpg"
    json_path = uploads_dir / f"{pht_date_stamp}-dahua-retention.txt"

    # Save JPEG with 85% quality
    collage.save(image_path, "JPEG", quality=85)
    
    with open(json_path, "w") as f:
        json.dump(retention_data, f, indent=4)

    print("\n✅ Success! Dahua Collage and retention data generated.")
    print(f"📁 Image Saved: {image_path}")
    print(f"📁 JSON Saved: {json_path}\n")

if __name__ == "__main__":
    # Run the main asynchronous function
    asyncio.run(generate_collage_and_retention())
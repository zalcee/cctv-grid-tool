import os
import math
import json
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from io import BytesIO

from dotenv import load_dotenv
import httpx
import xmltodict
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

async def get_channel_snapshot(client: httpx.AsyncClient, channel: int):
    """Captures and resizes a JPEG snapshot from an NVR channel."""
    url = f"http://{NVR_IP}/ISAPI/Streaming/channels/{channel}/picture"
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
    """Queries true retention metadata for a specific channel over the last 100 days."""
    now = datetime.now(timezone.utc)
    hundred_days_ago = now - timedelta(days=100)

    # Hikvision requires strict UTC format (Z) for searches
    start_time = hundred_days_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    unique_search_id = str(uuid.uuid4())

    xml_payload = f"""
    <CMSearchDescription>
        <searchID>{unique_search_id}</searchID>
        <trackList><trackID>{channel}</trackID></trackList>
        <timeSpanList>
            <timeSpan><startTime>{start_time}</startTime><endTime>{end_time}</endTime></timeSpan>
        </timeSpanList>
        <maxResults>1</maxResults>
    </CMSearchDescription>
    """.strip()

    url = f"http://{NVR_IP}/ISAPI/ContentMgmt/search"
    
    try:
        response = await client.post(url, content=xml_payload, headers={"Content-Type": "application/xml"})
        if response.status_code != 200:
            return {"status": "Error querying recordings"}

        result = xmltodict.parse(response.text)
        
        match_list = result.get("CMSearchResult", {}).get("matchList", {})
        if not match_list:
            return {"hasRecording": False, "retentionDays": 0, "message": "No recording found"}

        match_items = match_list.get("searchMatchItem")
        if not match_items:
            return {"hasRecording": False, "retentionDays": 0, "message": "No recording found"}

        # Handle whether XML parsed a single item (dict) or multiple items (list)
        first_match = match_items[0] if isinstance(match_items, list) else match_items
        start_time_str = first_match.get("timeSpan", {}).get("startTime")
        
        # Parse the oldest date and ensure it is treated as UTC
        oldest_date = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        
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
    # Use HTTPX AsyncClient with Digest Auth natively
    auth = httpx.DigestAuth(USERNAME, PASSWORD)
    
    # Grab the date stamp based on Philippine Time
    pht_now = datetime.now(PHT)
    pht_date_stamp = pht_now.strftime("%Y-%m-%d")

    print(f"\nProcessing feeds and calculating real retention for {len(CHANNELS)} channels...")

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
    image_path = uploads_dir / f"{pht_date_stamp}-collage.jpg"
    json_path = uploads_dir / f"{pht_date_stamp}-retention.txt"

    # Save JPEG with 85% quality
    collage.save(image_path, "JPEG", quality=85)
    
    with open(json_path, "w") as f:
        json.dump(retention_data, f, indent=4)

    print("\n✅ Success! Collage and retention data generated.")
    print(f"📁 Image Saved: {image_path}")
    print(f"📁 JSON Saved: {json_path}\n")

if __name__ == "__main__":
    # Run the main asynchronous function
    asyncio.run(generate_collage_and_retention())
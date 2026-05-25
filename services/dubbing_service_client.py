import requests
from elevenlabs.client import ElevenLabs
from io import BytesIO
import time
import subprocess

audio_url = "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4"

# --- Download video ---
print("🔁 Download video...")
try:
    with requests.get(audio_url, timeout=600, stream=True) as r:
        r.raise_for_status()

        video_data = BytesIO()
        for chunk in r.iter_content(chunk_size=1024*256):
            if chunk:
                video_data.write(chunk)

        video_data.seek(0)
        video_data.name = "video.mp4"

    print("✅ Download OK!")

except Exception as e:
    print("❌ Download error:", e)
    exit()


# --- ElevenLabs ---
client = ElevenLabs(api_key="sk_05f840fbb006599da5fcc82dc9c36a265bd7ddc636a3549e")

dub = client.dubbing.create(
    file=video_data,
    target_lang="es",
    watermark=True
)

print("🔑 Dubbing ID:", dub.dubbing_id)

# ---- WAIT ----
print("🔁 Đang dubbing...")
while True:
    status = client.dubbing.get(dub.dubbing_id).status
    print("👉 Status:", status)

    if status == "dubbed":
        print("🎉 Done!")
        break
    time.sleep(5)

# --- FIX: DUBBING AUDIO RETURN = STREAM GENERATOR ---
print("📥 Đang tải file MP4...")

stream = client.dubbing.audio.get(dub.dubbing_id, "es")  # Đây là generator

mp4_path = f"dub_{dub.dubbing_id}.mp4"

with open(mp4_path, "wb") as f:
    for chunk in stream:
        f.write(chunk)

print(f"✅ Đã lưu file MP4: {mp4_path}")

# --- Extract MP3 từ MP4 ---
mp3_path = f"dub_{dub.dubbing_id}.mp3"

cmd = [
    "ffmpeg",
    "-y",
    "-i", mp4_path,
    "-vn",
    "-acodec", "libmp3lame",
    "-b:a", "192k",
    mp3_path
]

subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
print(f"🎧 Đã extract MP3: {mp3_path}")

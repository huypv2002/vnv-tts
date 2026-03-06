"""
VNV TTS Tool - Database & TTS Configuration
"""

# D1 Configuration - VNV Tool (separate from main app)
D1_WORKER_URL = "https://vnv-tts-api.kh431248.workers.dev"
D1_API_KEY = "vnv-tts-secret-key-2026"

# TTS Proxy Workers — round-robin để phân tải, giảm 429
TTS_WORKER_URLS = [
    "https://vnv-tts-proxy.kh431248.workers.dev",
    "https://vnv-tts-proxy-2.kh431248.workers.dev",
    "https://vnv-tts-proxy-3.kh431248.workers.dev",
]
TTS_WORKER_URL = TTS_WORKER_URLS[0]  # backward compat
TTS_API_KEY = ""  # Set nếu đã config secret trên CF
    
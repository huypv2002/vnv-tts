import boto3
from botocore.client import Config

# Thay các giá trị này bằng của Cloudflare R2
R2_ACCESS_KEY_ID = "8d550c4aeebe5933c090f3542089b1ee"
R2_SECRET_ACCESS_KEY = "92bab19f37c9423e92c8a252fb02edc98bf0f9fd0c4084697949240ec8d1a3e4"
R2_ENDPOINT = "https://42280caf276b281cfc86c52af003920a.r2.cloudflarestorage.com"
# Tên bucket của bạn
R2_BUCKET = "dubbing-11labs"

# PUBLIC DOMAIN (bạn đã cung cấp)
PUBLIC_DOMAIN = "https://pub-3a0e71777aef422986e402ec0e463892.r2.dev"

# =========================
# 📁 FILE LOCAL CẦN UPLOAD
# =========================
file_path = r"C:\Users\84795\Downloads\Audio\Audio\app\uploads\test.mp4"
object_name = "uploads/test.mp4"

# =========================
# 🚀 TẠO CLIENT UPLOAD
# =========================
s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4")
)

# =========================
# 📤 UPLOAD FILE
# =========================
s3.upload_file(file_path, R2_BUCKET, object_name)

print("✅ Upload thành công!")

# =========================
# 🔗 TẠO PUBLIC URL ĐÚNG
# =========================
public_url = f"{PUBLIC_DOMAIN}/{object_name}"
print("🌍 Public URL:", public_url)
🎯 MỤC TIÊU CUỐI

1.000–10.000 key ElevenLabs

Tool Python mở / tắt / restart hành vi luôn đúng

Không test key

Không retry vô nghĩa

Không cần nhớ trạng thái trong app

Supabase là nguồn sự thật duy nhất

🧠 TƯ DUY CỐT LÕI (điều nhiều hệ thống sai)

❌ “Key còn hay không → test khi dùng”
✅ “Key được phép dùng hay không → quyết định trước trong DB”

➡ Tool chỉ dùng – không suy luận

🧱 KIẾN TRÚC TỔNG THỂ
Python Tool
   ↓ (request key)
Supabase (Key State Machine)
   ↓ (READY key)
ElevenLabs API
   ↓ (result)
Supabase (update state)


➡ App stateless
➡ DB stateful

🧩 PHẦN 1 – QUẢN LÝ VÒNG ĐỜI KEY (KEY LIFECYCLE)
Mỗi key chỉ nằm trong 1 trạng thái duy nhất
Trạng thái	Ý nghĩa	Có được pick không
READY	Đủ credit, dùng được	✅
LOW_CREDIT	Không đủ để gen	❌
TEMP_LOCK	Lỗi tạm (rate, net)	❌
DEAD	Revoke / invalid	❌

🔑 Chỉ 1 trạng thái được phép dùng: READY

🧠 PHẦN 2 – LUẬT VĨNH VIỄN (quan trọng nhất)
🔥 LUẬT 1 – ĐÃ BỊ LOẠI → KHÔNG BAO GIỜ QUAY LẠI

Key rơi xuống LOW_CREDIT hoặc DEAD

❌ Không reset

❌ Không retry

❌ Không test lại

➡ Mở app lần sau DB đã nhớ rồi

🔥 LUẬT 2 – LỖI TẠM PHẢI CÓ THỜI GIAN CHỜ

TEMP_LOCK luôn có locked_until

Khi app mở lại:

Nếu chưa hết giờ → key tự bị loại

Không cần app nhớ

⚡ PHẦN 3 – CHIẾN LƯỢC PICK KEY (SIÊU NHANH)
Tool chỉ làm 1 việc:

“Cho tôi 1 key READY hợp lệ”

Supabase:

Lọc sẵn

Trả đúng 1 key

Không trả key từng fail

Không trả key sắp chết

➡ 1 query – 0 retry – 0 delay

🔒 PHẦN 4 – CHỐNG TRÙNG & SCALE
Khi nhiều tool chạy:

Mỗi key chỉ được cấp cho 1 job tại 1 thời điểm

Không trùng key

Không race condition

➡ Scale lên 100–1.000 worker vẫn an toàn

🧠 PHẦN 5 – TOOL PYTHON ĐƯỢC GIẢN NÃO

Tool KHÔNG CẦN BIẾT:

Key còn bao nhiêu credit

Key sắp chết hay chưa

Key có từng fail không

Tool CHỈ CẦN:

Dùng key

Báo kết quả

➡ Tool đơn giản – ít bug – ít state

🧨 PHẦN 6 – XỬ LÝ SAU KHI DÙNG (PERSIST)

Sau mỗi lần gọi ElevenLabs:

Nếu thành công → cập nhật credit

Nếu credit < ngưỡng → chuyển LOW_CREDIT

Nếu lỗi auth → DEAD

Nếu lỗi tạm → TEMP_LOCK

➡ Trạng thái được ghi vĩnh viễn


🚀 PHẦN 7 – TỐI ƯU HIỆU NĂNG (THỰC TẾ)
Vấn đề	Giải pháp
App restart	DB nhớ state
Tool mở lại	Pick đúng key
Key hết credit	Bị loại vĩnh viễn
Key lỗi	Lock có thời gian
Delay	0–1 query
Retry	Gần như 0
Scale	10k key OK
🏆 KẾT LUẬN CUỐI (câu quan trọng nhất)

🔑 Muốn “lần sau mở app không trúng key cũ”
→ BẮT BUỘC phải giết key ở DATABASE, không phải ở APP

Khi:

Key có vòng đời rõ ràng

SQL chỉ trả READY

Mọi quyết định được persist

➡ App mở / đóng / crash / restart
➡ Hành vi vẫn luôn đúng


---

## 📊 CẤU TRÚC DATABASE

### Bảng `user_api_keys` (sau migration)

| Cột | Kiểu | Mô tả |
|-----|------|-------|
| id | SERIAL | Primary key |
| user_id | INTEGER | FK -> users |
| api_key | VARCHAR(255) | ElevenLabs API key |
| credit_remaining | INTEGER | Credit còn lại |
| credit_limit | INTEGER | Credit tối đa |
| is_active | BOOLEAN | Còn active không |
| **key_state** | VARCHAR(20) | `READY`, `LOW_CREDIT`, `TEMP_LOCK`, `DEAD` |
| **locked_until** | TIMESTAMP | Thời điểm hết lock (cho TEMP_LOCK) |
| **last_error** | TEXT | Lý do lỗi cuối |
| **failure_count** | INTEGER | Số lần lỗi liên tiếp |
| **exhausted_at** | TIMESTAMP | Thời điểm bị loại |
| **last_credit_check** | TIMESTAMP | Lần cuối check API |
| last_used | TIMESTAMP | Lần cuối sử dụng |
| created_at | TIMESTAMP | Ngày tạo |
| updated_at | TIMESTAMP | Ngày cập nhật |

### SQL Migration

```sql
-- Chạy file: admin_tools/migrate_key_state_machine.sql
```

### Functions trong DB

1. **pick_ready_key(user_id, required_credits, excluded_keys)**
   - Pick 1 key READY có đủ credit
   - Tự động unlock TEMP_LOCK hết hạn
   - Ưu tiên key credit cao nhất

2. **update_key_after_use(key_id, used_credits, success, error_type, error_message)**
   - Cập nhật state sau khi dùng
   - Tự động chuyển state theo kết quả

3. **get_key_stats(user_id)**
   - Thống kê key theo state

---

## 🔧 CÁCH SỬ DỤNG

### 1. Chạy Migration SQL

```bash
# Trong Supabase SQL Editor, chạy:
admin_tools/migrate_key_state_machine.sql
```

### 2. Sử dụng KeyPoolV2 trong Python

```python
from services.key_pool_v2 import KeyPoolV2

# Khởi tạo
pool = KeyPoolV2(supabase_client, user_id=3)

# Load keys
pool.load()

# Pick key (1 query, 0 retry)
api_key = pool.get_key(required_credits=500)

# Sau khi dùng thành công
pool.report_success(api_key, used_credits=450)

# Nếu lỗi
pool.report_error(api_key, "rate_limit", "429 Too Many Requests")
```

### 3. Error Types

| Error Type | State | Vĩnh viễn? |
|------------|-------|------------|
| `auth_error`, `401`, `invalid_key` | DEAD | ✅ |
| `quota_exceeded`, `insufficient_credits` | LOW_CREDIT | ✅ |
| `voice_limit` | DEAD | ✅ |
| `rate_limit`, `429` | TEMP_LOCK 60s | ❌ |
| `network_error`, `timeout` | TEMP_LOCK 30s | ❌ |

---

## 📈 MONITORING

### View thống kê

```sql
SELECT * FROM v_user_key_summary;
```

### Query key theo state

```sql
-- Key READY
SELECT * FROM user_api_keys 
WHERE user_id = 3 AND key_state = 'READY'
ORDER BY credit_remaining DESC;

-- Key bị lock
SELECT * FROM user_api_keys 
WHERE user_id = 3 AND key_state = 'TEMP_LOCK'
AND locked_until > NOW();

-- Key đã chết
SELECT * FROM user_api_keys 
WHERE user_id = 3 AND key_state IN ('LOW_CREDIT', 'DEAD');
```

---

## ✅ CHECKLIST TRIỂN KHAI

- [x] Chạy `migrate_key_state_machine.sql` trong Supabase
- [x] Thay `key_pool.py` bằng `key_pool_v2.py` trong code (đã refactor)
- [ ] Test với 1 user trước
- [ ] Monitor qua `v_user_key_summary`
- [ ] Scale lên nhiều user

---

## 📝 REFACTOR LOG

### Ngày refactor: 2026-01-12

**Files đã sửa:**
1. `services/key_pool_v2.py` - Tạo mới với State Machine Architecture
2. `11Labs0811.py` - Đổi import từ `key_pool` sang `key_pool_v2`
3. `services/tts_service.py` - Đổi import từ `key_pool` sang `key_pool_v2`
4. `admin_tools/migrate_key_state_machine.sql` - SQL migration script

**Backward Compatibility:**
- `KeyPoolV2` có alias `LocalKeyPool` để code cũ không cần sửa
- Tất cả methods cũ đều được giữ lại: `_keys`, `_by_api`, `_lock`, `mark_exhausted`, `mark_invalid`, etc.
- `KeyEntry.is_active` property được thêm để tương thích với code cũ

**Bước tiếp theo:**
1. ✅ Chạy SQL migration trong Supabase
2. ✅ Integrate KeyPoolV2 vào TTS worker (qua KeyPoolManager.acquire_key())
3. Test app với 1 user
4. Monitor logs để đảm bảo state machine hoạt động đúng

---

## 🔧 INTEGRATION LOG (2026-01-12)

### Vấn đề ban đầu:
- `KeyPoolManager.acquire_key()` dùng round-robin, không query DB
- TTS worker liên tục gặp 401 vì pick key theo thứ tự, không theo credit

### Giải pháp:
Sửa `KeyPoolManager` để delegate sang `KeyPoolV2` khi có DB:

1. **Thêm `_key_pool_db` reference** vào `KeyPoolManager.__init__()`
2. **Thêm `set_key_pool_db()` method** để connect với KeyPoolV2
3. **Sửa `acquire_key()`**:
   - Nếu có `_key_pool_db`: gọi `_acquire_key_from_db()` → query DB theo `key_state='READY'`, `credit_remaining DESC`
   - Fallback về `_acquire_key_local()` nếu không có DB
4. **Sửa `release_key()`**: Report success/error về DB qua `_key_pool_db.report_success()` / `report_error()`
5. **Sửa `mark_401()`, `mark_400()`**: Report error về DB

### Flow mới:
```
TTS Worker
   ↓ acquire_key()
KeyPoolManager
   ↓ _acquire_key_from_db()
KeyPoolV2.get_key()
   ↓ SQL query
Supabase (key_state='READY', credit_remaining DESC)
   ↓ return api_key
TTS Worker (dùng key)
   ↓ release_key(success=True/False)
KeyPoolManager
   ↓ report_success() / report_error()
KeyPoolV2 → Supabase (update key_state)
```

### Files đã sửa:
- `11Labs0811.py`:
  - `KeyPoolManager.__init__()`: Thêm `_key_pool_db = None`
  - `KeyPoolManager.set_key_pool_db()`: Method mới
  - `KeyPoolManager.acquire_key()`: Delegate sang DB
  - `KeyPoolManager.release_key()`: Report về DB
  - `KeyPoolManager.mark_401()`, `mark_400()`: Chỉ cooldown local (DB update qua release_key)
  - `_setup_ui()`: Gọi `self.keys.set_key_pool_db(self.key_pool_db)`
  - `ElevenClient.tts_direct()`: Thêm parameter `api_key` để dùng key đã acquire
  - `ChunkWorker`: Truyền `_acquired_key` vào `tts_direct()`
  - `LineWorker`: Truyền `_acquired_key` vào `tts_direct()`

### Bug đã fix (2026-01-12):
1. **Duplicate DEAD report**: `mark_401()` và `release_key()` đều gọi `report_error()` → Sửa: `mark_401()` chỉ cooldown local
2. **Key mismatch**: `tts_direct()` dùng `cur()` thay vì key đã acquire → Sửa: Thêm parameter `api_key`
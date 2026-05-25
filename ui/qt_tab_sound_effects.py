"""
Sound Effects Tab for Qt MainWindow (11Labs0811.py).
Tạo hiệu ứng âm thanh từ text sử dụng ElevenLabs Sound Generation API.

API: POST https://api.elevenlabs.io/v1/sound-generation
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, QThread, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import QHeaderView, QFileDialog, QMessageBox
from typing import Optional, Dict, List, Callable
import os
import json
import time
import requests
import uuid
import threading
from datetime import datetime


def _get_app_dir() -> str:
    """Get application directory."""
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log(msg: str):
    """Log message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [SoundFX] {msg}")


# ========== SIGNALS FOR PARALLEL WORKER ==========
class SoundEffectWorkerSignals(QObject):
    """Signals for SoundEffectRunnable to communicate with main thread."""
    progress = Signal(str, str, int)  # job_id, status, percent
    finished = Signal(str, bool, str, str)  # job_id, success, output_path, error
    key_rotated = Signal(str, str)  # job_id, new_key_prefix


# ========== SOUND EFFECT RUNNABLE (for QThreadPool) ==========
class SoundEffectRunnable(QRunnable):
    """Runnable for generating sound effects in parallel.
    
    Mỗi worker acquire key riêng từ pool.
    Áp dụng cùng logic key/proxy như TTS và Dubbing:
    - Rotate key khi 401/429
    - Mark key DEAD khi auth error
    - Rotate proxy khi unusual_activity
    """
    
    def __init__(self, job_id: str, text: str, output_path: str,
                 keys_pool, key_lock: threading.Lock,
                 proxy_getter: Callable = None,
                 proxy_rotator: Callable = None,
                 signals: SoundEffectWorkerSignals = None,
                 stop_flag_ref: Callable = None,
                 duration: float = None, prompt_influence: float = 0.3,
                 loop: bool = False):
        super().__init__()
        self.job_id = job_id
        self.text = text
        self.output_path = output_path
        self.keys_pool = keys_pool
        self._key_lock = key_lock  # Shared lock for key operations
        self.proxy_getter = proxy_getter
        self.proxy_rotator = proxy_rotator
        self.signals = signals
        self._stop_flag_ref = stop_flag_ref
        self.duration = duration
        self.prompt_influence = prompt_influence
        self.loop = loop
        
        self._current_key = None
        self._key_rotate_count = 0
        self._max_key_rotates = 30
        
        self.setAutoDelete(False)
    
    def _should_stop(self) -> bool:
        if self._stop_flag_ref and callable(self._stop_flag_ref):
            return self._stop_flag_ref()
        return False
    
    def _get_api_key(self) -> str:
        """Acquire API key from pool (thread-safe)."""
        with self._key_lock:
            if self.keys_pool:
                # Use acquire_key if available
                if hasattr(self.keys_pool, 'acquire_key'):
                    key = self.keys_pool.acquire_key(
                        required_chars=len(self.text),
                        max_attempts=50,
                        timeout=30.0,
                        line_id=f"sfx_{self.job_id}"
                    )
                    if key:
                        _log(f"🔑 [{self.job_id}] Acquired key: {key[:8]}...")
                    return key
                elif hasattr(self.keys_pool, 'cur'):
                    return self.keys_pool.cur()
        return None
    
    def _rotate_key(self) -> str:
        """Rotate to next API key (thread-safe)."""
        with self._key_lock:
            if self.keys_pool:
                if hasattr(self.keys_pool, 'acquire_key'):
                    new_key = self.keys_pool.acquire_key(
                        required_chars=len(self.text),
                        max_attempts=50,
                        timeout=30.0,
                        line_id=f"sfx_{self.job_id}"
                    )
                    if new_key:
                        _log(f"🔄 [{self.job_id}] Rotated to key: {new_key[:8]}...")
                    return new_key
                elif hasattr(self.keys_pool, 'rotate'):
                    self.keys_pool.rotate()
                    return self.keys_pool.cur() if hasattr(self.keys_pool, 'cur') else None
        return None
    
    def _mark_key_error(self, api_key: str, error_type: str):
        """Mark key as having error (thread-safe)."""
        with self._key_lock:
            if self.keys_pool and api_key:
                if hasattr(self.keys_pool, 'release_key'):
                    self.keys_pool.release_key(
                        api_key, 0, success=False, error_code=error_type,
                        line_id=f"sfx_{self.job_id}"
                    )
                elif hasattr(self.keys_pool, 'mark_401') and error_type in ['401', '402', '422']:
                    self.keys_pool.mark_401(api_key)
                _log(f"🔴 [{self.job_id}] Marked key error: {api_key[:8]}...")
    
    def _release_key(self, api_key: str, chars_used: int, success: bool, error_code: str = None):
        """Release key with result (thread-safe)."""
        with self._key_lock:
            if self.keys_pool and hasattr(self.keys_pool, 'release_key'):
                self.keys_pool.release_key(
                    api_key, chars_used, success=success, error_code=error_code,
                    line_id=f"sfx_{self.job_id}"
                )
    
    def _get_proxy(self) -> str:
        if self.proxy_getter and callable(self.proxy_getter):
            try:
                return self.proxy_getter()
            except:
                pass
        return None
    
    def run(self):
        try:
            # Acquire key for this job
            self._current_key = self._get_api_key()
            if not self._current_key:
                _log(f"⏳ [{self.job_id}] No key, waiting 2s and retry...")
                time.sleep(2)
                self._current_key = self._get_api_key()
            
            if not self._current_key:
                if self.signals:
                    self.signals.finished.emit(self.job_id, False, "", "Không có API key!")
                return
            
            if self.signals:
                self.signals.progress.emit(self.job_id, "generating", 30)
            
            # Generate with retry
            result = self._generate_with_retry()
            
            if result.get('success'):
                # Release key with success
                self._release_key(self._current_key, len(self.text), success=True)
                if self.signals:
                    self.signals.progress.emit(self.job_id, "done", 100)
                    self.signals.finished.emit(self.job_id, True, self.output_path, "")
            else:
                # Release key with failure
                self._release_key(self._current_key, 0, success=False, error_code=result.get('error_code'))
                if self.signals:
                    self.signals.finished.emit(self.job_id, False, "", result.get('error', 'Unknown error'))
                
        except Exception as e:
            _log(f"❌ [{self.job_id}] Exception: {e}")
            if self.signals:
                self.signals.finished.emit(self.job_id, False, "", str(e))
    
    def _generate_with_retry(self) -> Dict:
        """Generate sound effect with retry on key/proxy errors."""
        max_retries = self._max_key_rotates
        
        for retry in range(max_retries):
            if self._should_stop():
                return {'success': False, 'error': 'Stopped'}
            
            # Nếu không có key, xoay key
            if not self._current_key:
                _log(f"🔄 [{self.job_id}] No key, rotating...")
                self._current_key = self._rotate_key()
                if not self._current_key:
                    time.sleep(2)
                    continue
            
            result = self._generate()
            
            if result.get('success'):
                return result
            
            error = result.get('error', '')
            error_code = result.get('error_code', '')
            
            # Connection errors - retry with delay
            if error_code == 'exception':
                error_lower = error.lower()
                if any(x in error_lower for x in ['connection', 'timeout', 'reset', 'refused', 'network', 'socket', 'ssl', 'eof']):
                    _log(f"⚠️ [{self.job_id}] Connection error, retrying ({retry + 1}/{max_retries})...")
                    time.sleep(5)
                    continue
            
            # ⚠️ CRITICAL: Check unusual_activity FIRST - it's a PROXY issue, NOT key issue!
            if 'unusual_activity' in error.lower() or 'unusual activity' in error.lower():
                _log(f"⚠️ [{self.job_id}] Unusual activity - PROXY issue, rotating proxy...")
                if self.proxy_rotator and callable(self.proxy_rotator):
                    try:
                        self.proxy_rotator()
                        new_proxy = self._get_proxy()
                        _log(f"✅ [{self.job_id}] [Proxy rotated] Got: {new_proxy[:50] if new_proxy else 'None'}...")
                    except Exception as e:
                        _log(f"⚠️ [{self.job_id}] Proxy rotation error: {e}")
                time.sleep(3)
                continue
            
            # Key errors - rotate (but NOT for unusual_activity which was handled above)
            if error_code in ['401', '402', '422'] or 'invalid_api_key' in error.lower():
                _log(f"🔄 [{self.job_id}] Key error {error_code}, rotating... ({self._key_rotate_count + 1}/{max_retries})")
                self._mark_key_error(self._current_key, error_code or '401')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                if self.signals:
                    self.signals.key_rotated.emit(self.job_id, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(1)
                continue
            
            # Rate limit / concurrent limit - đổi key
            if error_code == '429' or 'rate' in error.lower() or 'too_many_concurrent' in error.lower():
                _log(f"🔄 [{self.job_id}] Rate limit / concurrent limit (429), rotating key...")
                self._mark_key_error(self._current_key, '429')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                if self.signals:
                    self.signals.key_rotated.emit(self.job_id, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(2)
                continue
            
            # Quota exceeded - rotate key
            if 'quota_exceeded' in error.lower():
                _log(f"🔄 [{self.job_id}] Quota exceeded, rotating key...")
                self._mark_key_error(self._current_key, 'quota_exceeded')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                time.sleep(1)
                continue
            
            # Server errors (5xx) - retry
            if error_code.startswith('5'):
                _log(f"⚠️ [{self.job_id}] Server error {error_code}, retrying...")
                time.sleep(5)
                continue
            
            # Other errors - don't retry
            return result
        
        return {'success': False, 'error': f'Đã thử {max_retries} lần, không thành công'}
    
    def _generate(self) -> Dict:
        """Call sound generation API."""
        url = "https://api.elevenlabs.io/v1/sound-generation"
        headers = {
            "xi-api-key": self._current_key,
            "Content-Type": "application/json"
        }
        
        data = {
            "text": self.text,
            "model_id": "eleven_text_to_sound_v2",
            "prompt_influence": self.prompt_influence,
            "loop": self.loop
        }
        
        if self.duration:
            data["duration_seconds"] = self.duration
        
        proxy_url = self._get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        
        if proxy_url:
            _log(f"🌐 [{self.job_id}] Using proxy: {proxy_url[:50]}...")
        else:
            _log(f"⚠️ [{self.job_id}] NO PROXY - may trigger unusual_activity")
        
        _log(f"[{self.job_id}] Generating: {self.text[:50]}...")
        
        try:
            response = requests.post(url, headers=headers, json=data,
                                    proxies=proxies, timeout=120)
            
            if response.status_code == 200:
                if self.signals:
                    self.signals.progress.emit(self.job_id, "saving", 80)
                
                with open(self.output_path, 'wb') as f:
                    f.write(response.content)
                
                _log(f"✅ [{self.job_id}] Saved: {self.output_path}")
                return {'success': True}
            
            # Parse error
            error_code = str(response.status_code)
            error_msg = response.text[:200]
            
            try:
                detail = response.json().get('detail', {})
                if isinstance(detail, dict):
                    error_msg = detail.get('message', error_msg)
                    status = detail.get('status', '')
                    if status:
                        error_msg = f"{status}: {error_msg}"
                elif isinstance(detail, str):
                    error_msg = detail
            except:
                pass
            
            _log(f"❌ [{self.job_id}] Error {error_code}: {error_msg[:100]}")
            return {'success': False, 'error': error_msg, 'error_code': error_code}
            
        except Exception as e:
            _log(f"❌ [{self.job_id}] Exception: {e}")
            return {'success': False, 'error': str(e), 'error_code': 'exception'}


# ========== SOUND EFFECTS TAB UI ==========
class SoundEffectsTab(QtWidgets.QWidget):
    """Tab for Sound Effects Generation."""
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.queue_paths: List[str] = []
        self.jobs: List[Dict] = []
        self.runnables: List[SoundEffectRunnable] = []
        self._stop = False
        self._key_lock = threading.Lock()
        self._results_lock = threading.Lock()
        self._completed_count = 0
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the sound effects tab UI."""
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)
        
        # ========== TOP ROW: Input + Options ==========
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(10)
        
        # === INPUT SECTION ===
        grp_input = QtWidgets.QGroupBox("Input")
        input_layout = QtWidgets.QVBoxLayout(grp_input)
        input_layout.setSpacing(5)
        
        # File/Folder path
        file_row = QtWidgets.QHBoxLayout()
        self.txt_path = QtWidgets.QLineEdit()
        self.txt_path.setPlaceholderText("Chọn file TXT hoặc thư mục...")
        self.txt_path.setReadOnly(True)
        
        self.btn_file = QtWidgets.QPushButton("📄 File")
        self.btn_file.clicked.connect(self._browse_file)
        
        self.btn_folder = QtWidgets.QPushButton("📁 Folder")
        self.btn_folder.clicked.connect(self._browse_folder)
        
        file_row.addWidget(self.txt_path, 1)
        file_row.addWidget(self.btn_file)
        file_row.addWidget(self.btn_folder)
        input_layout.addLayout(file_row)
        
        # File info
        self.lbl_info = QtWidgets.QLabel("")
        self.lbl_info.setStyleSheet("color: #666; font-size: 10px;")
        input_layout.addWidget(self.lbl_info)
        
        input_layout.addStretch()
        grp_input.setMinimumWidth(400)
        top_row.addWidget(grp_input)
        
        # === OPTIONS SECTION ===
        grp_options = QtWidgets.QGroupBox("Tùy chọn")
        opt_layout = QtWidgets.QGridLayout(grp_options)
        opt_layout.setHorizontalSpacing(10)
        opt_layout.setVerticalSpacing(5)
        
        # Duration
        opt_layout.addWidget(QtWidgets.QLabel("Thời lượng (s):"), 0, 0)
        self.spn_duration = QtWidgets.QDoubleSpinBox()
        self.spn_duration.setRange(0, 30)
        self.spn_duration.setValue(15)
        self.spn_duration.setSpecialValueText("Tự động")
        self.spn_duration.setToolTip("0 = tự động, 0.5-30s")
        opt_layout.addWidget(self.spn_duration, 0, 1)
        
        # Prompt influence
        opt_layout.addWidget(QtWidgets.QLabel("Prompt influence:"), 1, 0)
        self.spn_influence = QtWidgets.QDoubleSpinBox()
        self.spn_influence.setRange(0, 1)
        self.spn_influence.setValue(0.3)
        self.spn_influence.setSingleStep(0.1)
        self.spn_influence.setToolTip("0-1, cao hơn = theo prompt chặt hơn")
        opt_layout.addWidget(self.spn_influence, 1, 1)
        
        # Loop checkbox
        self.chk_loop = QtWidgets.QCheckBox("Loop (lặp mượt)")
        self.chk_loop.setToolTip("Tạo âm thanh có thể lặp liên tục")
        opt_layout.addWidget(self.chk_loop, 2, 0, 1, 2)
        
        # Thread count
        opt_layout.addWidget(QtWidgets.QLabel("Số luồng:"), 3, 0)
        self.spn_threads = QtWidgets.QSpinBox()
        self.spn_threads.setRange(1, 10)
        self.spn_threads.setValue(3)
        self.spn_threads.setToolTip("Số job chạy song song (1-10)")
        opt_layout.addWidget(self.spn_threads, 3, 1)
        
        grp_options.setMinimumWidth(200)
        top_row.addWidget(grp_options)
        top_row.addStretch()
        root.addLayout(top_row)

        # ========== JOB TABLE ==========
        grp_jobs = QtWidgets.QGroupBox("Danh sách công việc")
        jobs_layout = QtWidgets.QVBoxLayout(grp_jobs)
        
        self.tbl_jobs = QtWidgets.QTableWidget(0, 5)
        self.tbl_jobs.setHorizontalHeaderLabels([
            "#", "Prompt", "Trạng thái", "Tiến độ", "Hành động"
        ])
        self.tbl_jobs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_jobs.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tbl_jobs.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_jobs.verticalHeader().setVisible(False)
        self.tbl_jobs.setAlternatingRowColors(True)
        
        hdr = self.tbl_jobs.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(2, 100)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(3, 80)
        hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(4, 80)
        
        jobs_layout.addWidget(self.tbl_jobs)
        root.addWidget(grp_jobs, 1)
        
        # ========== BOTTOM BUTTONS ==========
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(10)
        
        self.btn_start = QtWidgets.QPushButton("▶ Bắt đầu")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                font-weight: bold; padding: 8px 20px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2ecc71; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_start.clicked.connect(self._start_generation)
        btn_row.addWidget(self.btn_start)
        
        self.btn_stop = QtWidgets.QPushButton("⏹ Dừng")
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white;
                font-weight: bold; padding: 8px 20px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #c0392b; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_stop.clicked.connect(self._stop_generation)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_stop)
        
        self.btn_clear = QtWidgets.QPushButton("🗑 Xóa hoàn thành")
        self.btn_clear.clicked.connect(self._clear_completed)
        btn_row.addWidget(self.btn_clear)
        
        btn_row.addStretch()
        
        self.lbl_status = QtWidgets.QLabel("Sẵn sàng")
        self.lbl_status.setStyleSheet("color: #666;")
        btn_row.addWidget(self.lbl_status)
        
        root.addLayout(btn_row)

    # ========== FILE HANDLING ==========
    def _browse_file(self):
        """Browse for TXT file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file TXT", "", "Text Files (*.txt);;All Files (*.*)"
        )
        if file_path:
            self.txt_path.setText(file_path)
            self._load_file(file_path)
    
    def _browse_folder(self):
        """Browse for folder containing TXT files."""
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if folder:
            self.txt_path.setText(folder)
            self._load_folder(folder)
    
    def _load_file(self, file_path: str):
        """Load prompts from a single TXT file."""
        self.queue_paths = [file_path]
        self.jobs = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f if l.strip()]
            
            output_dir = os.path.dirname(file_path)
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            
            for i, line in enumerate(lines, 1):
                job_id = str(uuid.uuid4())[:8]
                output_path = os.path.join(output_dir, f"{base_name}_sfx_{i:03d}.mp3")
                self.jobs.append({
                    'id': job_id,
                    'text': line,
                    'output_path': output_path,
                    'status': 'pending',
                    'progress': 0
                })
            
            self.lbl_info.setText(f"📄 {len(self.jobs)} prompt(s) từ {os.path.basename(file_path)}")
            self._refresh_table()
            
        except Exception as e:
            QMessageBox.warning(self, "Lỗi", f"Không đọc được file: {e}")
    
    def _load_folder(self, folder: str):
        """Load prompts from all TXT files in folder."""
        self.queue_paths = []
        self.jobs = []
        
        txt_files = sorted([f for f in os.listdir(folder) if f.endswith('.txt')])
        
        for txt_file in txt_files:
            file_path = os.path.join(folder, txt_file)
            self.queue_paths.append(file_path)
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = [l.strip() for l in f if l.strip()]
                
                base_name = os.path.splitext(txt_file)[0]
                
                for i, line in enumerate(lines, 1):
                    job_id = str(uuid.uuid4())[:8]
                    output_path = os.path.join(folder, f"{base_name}_sfx_{i:03d}.mp3")
                    self.jobs.append({
                        'id': job_id,
                        'text': line,
                        'output_path': output_path,
                        'status': 'pending',
                        'progress': 0,
                        'source_file': txt_file
                    })
            except:
                continue
        
        self.lbl_info.setText(f"📁 {len(self.jobs)} prompt(s) từ {len(txt_files)} file(s)")
        self._refresh_table()

    # ========== GENERATION ==========
    def _start_generation(self):
        """Start generating sound effects."""
        if not self.jobs:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn file hoặc thư mục!")
            return
        
        # ========== CHECK SUBSCRIPTION trước khi bắt đầu ==========
        if hasattr(self.main_window, 'current_user_id') and self.main_window.current_user_id:
            if not self._check_subscription_active():
                info = self._load_subscription_info()
                
                error_msg = "Gói của bạn không hợp lệ!\n\n"
                
                if info.get('status') == 'no_subscription':
                    error_msg += "❌ Bạn chưa có gói nào.\n"
                elif info.get('status') == 'expired':
                    error_msg += f"❌ Gói '{info.get('type', '')}' đã hết hạn.\n"
                    if info.get('end_date'):
                        error_msg += f"📅 Ngày hết hạn: {info['end_date'][:10]}\n"
                
                count_chars = info.get('count_characters')
                if count_chars is not None and count_chars <= 0:
                    error_msg += "❌ Đã hết số ký tự trong gói.\n"
                
                error_msg += "\nVui lòng liên hệ admin để gia hạn."
                
                QMessageBox.critical(self, "⛔ Subscription không hợp lệ", error_msg)
                _log("⛔ Blocked - subscription không hợp lệ")
                return
            
            # Check credits còn đủ không
            if not self._check_subscription_credits():
                return
        
        # Check API key
        keys_pool = self._get_keys_pool()
        if not keys_pool or not keys_pool.cur():
            QMessageBox.warning(self, "Lỗi", "Không có API key!")
            return
        
        # Force refresh proxy before starting - đợi đến khi có IP mới
        main_window = self.main_window
        if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
            self.lbl_status.setText("Đang xoay proxy...")
            QtWidgets.QApplication.processEvents()
            
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    fresh_proxy = main_window.proxy_service_db.force_refresh()
                    if fresh_proxy:
                        _log(f"✅ [Pre-gen] Fresh proxy: {fresh_proxy[:50]}...")
                        break
                    else:
                        _log(f"⚠️ [Pre-gen] Attempt {attempt + 1}/{max_attempts} - waiting for proxy...")
                        time.sleep(3)
                except Exception as e:
                    _log(f"⚠️ Pre-gen proxy refresh failed: {e}")
                    time.sleep(3)
        
        # Reset state
        self._stop = False
        self._completed_count = 0
        self.runnables = []
        
        # Get options
        duration = self.spn_duration.value() if self.spn_duration.value() > 0 else None
        influence = self.spn_influence.value()
        loop = self.chk_loop.isChecked()
        max_workers = self.spn_threads.value()
        
        # Proxy getter function
        def get_proxy():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                return main_window.proxy_service_db.get_current_proxy()
            return None
        
        # Proxy rotator function - force refresh to get new IP
        def proxy_rotator():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                return main_window.proxy_service_db.force_refresh()
            return None
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        
        # Create signals object
        self._signals = SoundEffectWorkerSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.key_rotated.connect(self._on_key_rotated)
        
        # Setup thread pool
        pool = QThreadPool.globalInstance()
        pool.setMaxThreadCount(max_workers)
        
        _log(f"🚀 Starting {len(self.jobs)} jobs with {max_workers} threads")
        
        # Create and start runnables for pending jobs
        pending_jobs = [j for j in self.jobs if j['status'] == 'pending']
        for job in pending_jobs:
            job['status'] = 'queued'
            
            runnable = SoundEffectRunnable(
                job_id=job['id'],
                text=job['text'],
                output_path=job['output_path'],
                keys_pool=keys_pool,
                key_lock=self._key_lock,
                proxy_getter=get_proxy,
                proxy_rotator=proxy_rotator,
                signals=self._signals,
                stop_flag_ref=lambda: self._stop,
                duration=duration,
                prompt_influence=influence,
                loop=loop
            )
            
            self.runnables.append(runnable)
            pool.start(runnable)
        
        self._refresh_table()
        self.lbl_status.setText(f"Đang xử lý 0/{len(pending_jobs)} với {max_workers} luồng...")

    def _on_progress(self, job_id: str, status: str, percent: int):
        """Handle progress update."""
        for job in self.jobs:
            if job['id'] == job_id:
                job['status'] = status
                job['progress'] = percent
                break
        self._refresh_table()
    
    def _on_key_rotated(self, job_id: str, new_key: str):
        """Handle key rotation."""
        _log(f"🔄 Job {job_id} rotated to key: {new_key}")
    
    def _on_finished(self, job_id: str, success: bool, output_path: str, error: str):
        """Handle worker finished."""
        job_text = ""
        job_duration = None
        
        with self._results_lock:
            self._completed_count += 1
        
        for job in self.jobs:
            if job['id'] == job_id:
                if success:
                    job['status'] = 'done'
                    job['progress'] = 100
                    job['output_path'] = output_path
                    job_text = job.get('text', '')
                    # Get duration from spinbox (used for generation)
                    job_duration = self.spn_duration.value() if self.spn_duration.value() > 0 else 15
                else:
                    job['status'] = 'failed'
                    job['error'] = error
                break
        
        self._refresh_table()
        
        # Deduct credits after successful generation (only once!)
        if success and job_text:
            self._deduct_credits_for_sound_effect(job_text, job_duration)
        
        # Update status
        total_jobs = len([j for j in self.jobs if j['status'] != 'pending'])
        self.lbl_status.setText(f"Đang xử lý {self._completed_count}/{total_jobs}...")
        
        # Check if all done
        pending_or_running = sum(1 for j in self.jobs if j['status'] in ('pending', 'queued', 'generating', 'saving'))
        if pending_or_running == 0:
            self._on_all_done()
    
    def _on_all_done(self):
        """All jobs completed."""
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._stop = False
        self.runnables = []
        
        done = sum(1 for j in self.jobs if j['status'] == 'done')
        failed = sum(1 for j in self.jobs if j['status'] == 'failed')
        
        self.lbl_status.setText(f"✅ Hoàn thành: {done} thành công, {failed} lỗi")
        _log(f"All done: {done} success, {failed} failed")
    
    def _stop_generation(self):
        """Stop all workers."""
        self._stop = True
        
        # Wait for thread pool to finish current tasks
        pool = QThreadPool.globalInstance()
        pool.clear()  # Clear pending tasks
        
        # Mark queued jobs as stopped
        for job in self.jobs:
            if job['status'] in ('queued', 'pending'):
                job['status'] = 'pending'  # Reset to pending
        
        self.runnables = []
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("Đã dừng")
    
    def _clear_completed(self):
        """Clear completed/failed jobs."""
        self.jobs = [j for j in self.jobs if j['status'] not in ('done', 'failed')]
        self._refresh_table()

    # ========== TABLE ==========
    def _refresh_table(self):
        """Refresh job table."""
        self.tbl_jobs.setRowCount(len(self.jobs))
        
        for i, job in enumerate(self.jobs):
            # #
            item_idx = QtWidgets.QTableWidgetItem(str(i + 1))
            item_idx.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 0, item_idx)
            
            # Prompt (truncated)
            text = job.get('text', '')[:50] + ('...' if len(job.get('text', '')) > 50 else '')
            self.tbl_jobs.setItem(i, 1, QtWidgets.QTableWidgetItem(text))
            
            # Status
            status = job.get('status', '')
            status_map = {
                'pending': '⏳ Chờ',
                'queued': '📋 Hàng đợi',
                'generating': '🔄 Đang tạo',
                'saving': '💾 Lưu',
                'done': '✅ Xong',
                'failed': '❌ Lỗi'
            }
            item_status = QtWidgets.QTableWidgetItem(status_map.get(status, status))
            item_status.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 2, item_status)
            
            # Progress
            progress = job.get('progress', 0)
            item_progress = QtWidgets.QTableWidgetItem(f"{progress}%")
            item_progress.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 3, item_progress)
            
            # Action
            action_widget = QtWidgets.QWidget()
            action_layout = QtWidgets.QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            action_layout.setSpacing(2)
            
            if job.get('output_path') and os.path.exists(job.get('output_path', '')):
                btn_open = QtWidgets.QPushButton("📂")
                btn_open.setToolTip("Mở thư mục")
                btn_open.setFixedSize(28, 24)
                btn_open.clicked.connect(lambda _, p=job['output_path']: self._open_location(p))
                action_layout.addWidget(btn_open)
            
            action_layout.addStretch()
            self.tbl_jobs.setCellWidget(i, 4, action_widget)
    
    def _open_location(self, file_path: str):
        """Open file location."""
        import subprocess
        import sys
        folder = os.path.dirname(file_path)
        if sys.platform == 'darwin':
            subprocess.run(['open', folder])
        elif sys.platform == 'win32':
            subprocess.run(['explorer', folder])
        else:
            subprocess.run(['xdg-open', folder])
    
    # ========== HELPERS ==========
    def _get_keys_pool(self):
        """Get keys pool from main window."""
        if self.main_window and hasattr(self.main_window, 'keys'):
            return self.main_window.keys
        return None

    # ========== SUBSCRIPTION CHECK ==========
    def _check_subscription_active(self) -> bool:
        """
        Check if user has active subscription.
        Kiểm tra:
        - is_active = True
        - end_date chưa hết hạn  
        - count_characters > 0 (nếu có)
        
        Returns: True nếu subscription còn hợp lệ
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                _log("⚠️ No user logged in")
                return False
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                _log("⚠️ No database connection")
                return False
            
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('is_active, end_date, count_characters, subscription_type')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                _log("❌ No active subscription found")
                return False
            
            sub = result.data[0]
            
            # Check if subscription has expired
            if sub.get('end_date'):
                from datetime import datetime, timezone
                try:
                    end_date_str = sub['end_date']
                    if end_date_str.endswith('Z'):
                        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    elif '+' in end_date_str or '-' in end_date_str[-6:]:
                        end_date = datetime.fromisoformat(end_date_str)
                    else:
                        end_date = datetime.fromisoformat(end_date_str)
                    
                    # Compare với UTC time
                    now = datetime.now(timezone.utc) if end_date.tzinfo else datetime.now()
                    if now > end_date:
                        _log(f"❌ Subscription expired: {end_date}")
                        return False
                except Exception as e:
                    _log(f"⚠️ Error parsing end_date: {e}")
            
            # Check character-based quota
            try:
                count_chars = sub.get('count_characters')
                if count_chars is not None and int(count_chars) <= 0:
                    _log("❌ No remaining characters (count_characters <= 0)")
                    return False
            except Exception:
                pass
            
            _log(f"✅ Subscription active: {sub.get('subscription_type', 'unknown')}")
            return True
            
        except Exception as e:
            _log(f"❌ Subscription check error: {e}")
            return False
    
    def _load_subscription_info(self) -> dict:
        """
        Load subscription info từ database.
        Returns dict với thông tin gói hoặc {} nếu lỗi.
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return {}
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return {}
            
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('*')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                return {'status': 'no_subscription'}
            
            sub = result.data[0]
            
            # Calculate days remaining
            days_remaining = None
            if sub.get('end_date'):
                from datetime import datetime, timezone
                try:
                    end_str = sub['end_date']
                    if end_str.endswith('Z'):
                        end_date = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    else:
                        end_date = datetime.fromisoformat(end_str)
                    
                    now = datetime.now(timezone.utc) if end_date.tzinfo else datetime.now()
                    days_remaining = max(0, (end_date - now).days)
                except:
                    pass
            
            return {
                'type': sub.get('subscription_type', 'unknown'),
                'end_date': sub.get('end_date'),
                'days_remaining': days_remaining,
                'is_active': sub.get('is_active', False),
                'count_characters': sub.get('count_characters'),
                'status': 'active' if sub.get('is_active') and (days_remaining is None or days_remaining > 0) else 'expired'
            }
        except Exception as e:
            _log(f"❌ Load subscription error: {e}")
            return {'error': str(e)}
    
    def _check_subscription_credits(self) -> bool:
        """
        Kiểm tra subscription credits trước khi bắt đầu Sound Effects.
        Returns: True nếu còn credits, False nếu hết.
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return True  # Không có user_id, bỏ qua kiểm tra
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return True  # Không có supabase, bỏ qua kiểm tra
            
            # Get current subscription
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('id, count_characters, plan_name, subscription_type')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                return True  # Không có subscription, bỏ qua kiểm tra
            
            sub = result.data[0]
            current_chars = sub.get('count_characters')
            plan_name = sub.get('plan_name') or sub.get('subscription_type', 'Unknown')
            
            if current_chars is None:
                return True  # Không track characters, bỏ qua kiểm tra
            
            if current_chars <= 0:
                # Hết credits - hiện popup và không cho bắt đầu
                _log(f"❌ Hết credits! Gói {plan_name} còn {current_chars:,} ký tự")
                QMessageBox.warning(
                    self, 
                    "Hết Credits Gói",
                    f"❌ Gói {plan_name} đã hết credits!\n\n"
                    f"Số ký tự còn lại: {current_chars:,}\n\n"
                    "Vui lòng nâng cấp gói hoặc liên hệ admin để nạp thêm."
                )
                return False
            
            # Còn credits - cho phép bắt đầu
            _log(f"✅ Gói {plan_name} còn {current_chars:,} ký tự")
            return True
            
        except Exception as e:
            _log(f"⚠️ Check credits error: {e}")
            return True  # Lỗi thì cho phép tiếp tục

    # ========== CREDIT DEDUCTION ==========
    def _deduct_credits_for_sound_effect(self, text: str, duration: float = None):
        """
        Trừ credits sau khi tạo sound effect thành công.
        
        ElevenLabs Sound Effects tính credits theo:
        - Khoảng 100 credits/giây audio
        - Hoặc tính theo độ dài text nếu không có duration
        
        Công thức: duration_seconds * 100 hoặc len(text) * 2
        
        Thread-safe với retry và random delay để tránh database lock.
        """
        import random
        
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return
            
            # Tính credits
            if duration and duration > 0:
                # ~100 credits/giây + 20% margin
                chars_to_deduct = int(duration * 100 * 1.2)
            else:
                # Fallback: tính theo text length * 2
                chars_to_deduct = len(text) * 2
            
            if chars_to_deduct <= 0:
                return
            
            _log(f"📝 Deducting {chars_to_deduct:,} credits for sound effect")
            
            # Retry với random delay để tránh database lock [Errno 35]
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    # Random delay trước mỗi attempt để tránh collision
                    if attempt > 0:
                        delay = random.uniform(0.5, 1.5)
                        _log(f"⏳ Retry {attempt + 1}/{max_retries} after {delay:.1f}s delay...")
                        time.sleep(delay)
                    
                    # Get current subscription
                    result = self.main_window.supabase.table('user_subscriptions')\
                        .select('id, count_characters')\
                        .eq('user_id', self.main_window.current_user_id)\
                        .eq('is_active', True)\
                        .order('created_at', desc=True)\
                        .limit(1)\
                        .execute()
                    
                    if not result.data:
                        return
                    
                    sub = result.data[0]
                    sub_id = sub['id']
                    current_chars = sub.get('count_characters')
                    
                    if current_chars is None:
                        return  # No character limit tracking
                    
                    # Calculate new value
                    new_chars = max(0, int(current_chars) - chars_to_deduct)
                    
                    # Update với optimistic locking
                    update_result = self.main_window.supabase.table('user_subscriptions')\
                        .update({'count_characters': new_chars})\
                        .eq('id', sub_id)\
                        .eq('count_characters', current_chars)\
                        .execute()
                    
                    if update_result.data:
                        _log(f"✅ Deducted {chars_to_deduct:,} credits → {new_chars:,} remaining")
                        
                        # Update main window subscription label
                        if hasattr(self.main_window, '_update_subscription_label'):
                            from PySide6 import QtCore
                            QtCore.QTimer.singleShot(100, self.main_window._update_subscription_label)
                        
                        # Check if credits exhausted
                        if new_chars <= 0:
                            _log("❌ Credits exhausted after sound effect!")
                            QMessageBox.warning(
                                self,
                                "Hết Credits",
                                "❌ Gói của bạn đã hết credits sau khi tạo sound effect!\n\n"
                                "Vui lòng nâng cấp gói hoặc liên hệ admin."
                            )
                        return
                    else:
                        # Optimistic lock conflict - retry
                        _log(f"⚠️ Optimistic lock conflict, retrying...")
                        continue
                        
                except Exception as inner_e:
                    error_str = str(inner_e).lower()
                    # Handle database lock errors: [Errno 35] Resource temporarily unavailable
                    if 'errno 35' in error_str or 'resource temporarily unavailable' in error_str or 'database is locked' in error_str:
                        _log(f"⚠️ Database lock error, will retry: {inner_e}")
                        continue
                    else:
                        # Other errors - raise to outer handler
                        raise
            
            _log(f"⚠️ Failed to deduct credits after {max_retries} retries")
                        
        except Exception as e:
            _log(f"⚠️ Deduct credits error: {e}")

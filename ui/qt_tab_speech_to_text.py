"""
Speech-to-Text Tab for Qt MainWindow (11Labs0811.py).
Chuyển giọng nói thành văn bản sử dụng ElevenLabs Speech-to-Text API.

API: POST https://api.elevenlabs.io/v1/speech-to-text
Model: scribe_v1 hoặc scribe_v1_experimental

Features:
- Hỗ trợ nhiều định dạng audio/video (mp3, wav, mp4, etc.)
- Tự động nhận diện ngôn ngữ
- Diarization (phân biệt người nói)
- Export ra nhiều định dạng (TXT, SRT, JSON)
- Tag audio events (tiếng cười, tiếng bước chân, etc.)
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
    print(f"[{timestamp}] [STT] {msg}")


# ========== LANGUAGE CODES ==========
LANGUAGE_CODES = {
    "Tự động": None,
    "Tiếng Việt": "vi",
    "English": "en",
    "日本語 (Japanese)": "ja",
    "한국어 (Korean)": "ko",
    "中文 (Chinese)": "zh",
    "Français": "fr",
    "Deutsch": "de",
    "Español": "es",
    "Italiano": "it",
    "Português": "pt",
    "Русский": "ru",
    "العربية (Arabic)": "ar",
    "हिन्दी (Hindi)": "hi",
    "ไทย (Thai)": "th",
    "Indonesia": "id",
}


# ========== SIGNALS FOR WORKER ==========
class STTWorkerSignals(QObject):
    """Signals for STTRunnable to communicate with main thread."""
    progress = Signal(str, str, int)  # job_id, status, percent
    finished = Signal(str, bool, dict, str)  # job_id, success, result_data, error
    key_rotated = Signal(str, str)  # job_id, new_key_prefix


# ========== STT RUNNABLE (for QThreadPool) ==========
class STTRunnable(QRunnable):
    """Runnable for transcribing audio in parallel."""
    
    def __init__(self, job_id: str, file_path: str,
                 keys_pool, key_lock: threading.Lock,
                 proxy_getter: Callable = None,
                 proxy_rotator: Callable = None,
                 signals: STTWorkerSignals = None,
                 stop_flag_ref: Callable = None,
                 language_code: str = None,
                 diarize: bool = False,
                 num_speakers: int = None,
                 tag_audio_events: bool = True,
                 timestamps_granularity: str = "word"):
        super().__init__()
        self.job_id = job_id
        self.file_path = file_path
        self.keys_pool = keys_pool
        self._key_lock = key_lock
        self.proxy_getter = proxy_getter
        self.proxy_rotator = proxy_rotator
        self.signals = signals
        self._stop_flag_ref = stop_flag_ref
        self.language_code = language_code
        self.diarize = diarize
        self.num_speakers = num_speakers
        self.tag_audio_events = tag_audio_events
        self.timestamps_granularity = timestamps_granularity
        
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
                if hasattr(self.keys_pool, 'acquire_key'):
                    key = self.keys_pool.acquire_key(
                        required_chars=1000,  # STT doesn't use chars but need some value
                        max_attempts=50,
                        timeout=30.0,
                        line_id=f"stt_{self.job_id}"
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
                        required_chars=1000,
                        max_attempts=50,
                        timeout=30.0,
                        line_id=f"stt_{self.job_id}"
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
                        line_id=f"stt_{self.job_id}"
                    )
                elif hasattr(self.keys_pool, 'mark_401') and error_type in ['401', '402', '422']:
                    self.keys_pool.mark_401(api_key)
                _log(f"🔴 [{self.job_id}] Marked key error: {api_key[:8]}...")
    
    def _release_key(self, api_key: str, success: bool, error_code: str = None):
        """Release key with result (thread-safe)."""
        with self._key_lock:
            if self.keys_pool and hasattr(self.keys_pool, 'release_key'):
                self.keys_pool.release_key(
                    api_key, 0, success=success, error_code=error_code,
                    line_id=f"stt_{self.job_id}"
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
                    self.signals.finished.emit(self.job_id, False, {}, "Không có API key!")
                return
            
            if self.signals:
                self.signals.progress.emit(self.job_id, "transcribing", 30)
            
            # Transcribe with retry
            result = self._transcribe_with_retry()
            
            if result.get('success'):
                self._release_key(self._current_key, success=True)
                if self.signals:
                    self.signals.progress.emit(self.job_id, "done", 100)
                    self.signals.finished.emit(self.job_id, True, result.get('data', {}), "")
            else:
                self._release_key(self._current_key, success=False, error_code=result.get('error_code'))
                if self.signals:
                    self.signals.finished.emit(self.job_id, False, {}, result.get('error', 'Unknown error'))
                
        except Exception as e:
            _log(f"❌ [{self.job_id}] Exception: {e}")
            if self.signals:
                self.signals.finished.emit(self.job_id, False, {}, str(e))
    
    def _transcribe_with_retry(self) -> Dict:
        """Transcribe with retry on key/proxy errors."""
        max_retries = self._max_key_rotates
        
        for retry in range(max_retries):
            if self._should_stop():
                return {'success': False, 'error': 'Stopped'}
            
            if not self._current_key:
                _log(f"🔄 [{self.job_id}] No key, rotating...")
                self._current_key = self._rotate_key()
                if not self._current_key:
                    time.sleep(2)
                    continue
            
            result = self._transcribe()
            
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
            
            # Key errors - rotate
            if error_code in ['401', '402', '422'] or 'invalid_api_key' in error.lower():
                _log(f"🔄 [{self.job_id}] Key error {error_code}, rotating...")
                self._mark_key_error(self._current_key, error_code or '401')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                if self.signals:
                    self.signals.key_rotated.emit(self.job_id, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(1)
                continue
            
            # Rate limit / concurrent limit - rotate key
            if error_code == '429' or 'rate' in error.lower() or 'too_many_concurrent' in error.lower():
                _log(f"🔄 [{self.job_id}] Rate limit (429), rotating key...")
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
    
    def _transcribe(self) -> Dict:
        """Call speech-to-text API."""
        url = "https://api.elevenlabs.io/v1/speech-to-text"
        headers = {
            "xi-api-key": self._current_key,
        }
        
        # Prepare form data
        data = {
            "model_id": "scribe_v1",
            "tag_audio_events": str(self.tag_audio_events).lower(),
            "timestamps_granularity": self.timestamps_granularity,
            "diarize": str(self.diarize).lower(),
        }
        
        if self.language_code:
            data["language_code"] = self.language_code
        
        if self.num_speakers and self.num_speakers > 0:
            data["num_speakers"] = str(self.num_speakers)
        
        proxy_url = self._get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        
        if proxy_url:
            _log(f"🌐 [{self.job_id}] Using proxy: {proxy_url[:50]}...")
        else:
            _log(f"⚠️ [{self.job_id}] NO PROXY - may trigger unusual_activity")
        
        _log(f"[{self.job_id}] Transcribing: {os.path.basename(self.file_path)}...")
        
        try:
            # Open file for upload
            with open(self.file_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(self.file_path), f, 'audio/mpeg')
                }
                
                response = requests.post(
                    url, 
                    headers=headers, 
                    data=data,
                    files=files,
                    proxies=proxies, 
                    timeout=300  # 5 minutes timeout for large files
                )
            
            if response.status_code == 200:
                result_data = response.json()
                _log(f"✅ [{self.job_id}] Transcription complete")
                return {'success': True, 'data': result_data}
            
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



# ========== SPEECH TO TEXT TAB UI ==========
class SpeechToTextTab(QtWidgets.QWidget):
    """Tab for Speech-to-Text (Chuyển giọng nói thành văn bản)."""
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.queue_paths: List[str] = []
        self.jobs: List[Dict] = []
        self.runnables: List[STTRunnable] = []
        self._stop = False
        self._key_lock = threading.Lock()
        self._results_lock = threading.Lock()
        self._completed_count = 0
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the speech-to-text tab UI."""
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)
        
        # ========== TOP ROW: Input + Options ==========
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(10)
        
        # === INPUT SECTION ===
        grp_input = QtWidgets.QGroupBox("Đầu vào")
        input_layout = QtWidgets.QVBoxLayout(grp_input)
        input_layout.setSpacing(5)
        
        # File/Folder path
        file_row = QtWidgets.QHBoxLayout()
        self.txt_path = QtWidgets.QLineEdit()
        self.txt_path.setPlaceholderText("Chọn file audio/video hoặc thư mục...")
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
        
        # Language
        opt_layout.addWidget(QtWidgets.QLabel("Ngôn ngữ:"), 0, 0)
        self.cmb_language = QtWidgets.QComboBox()
        for lang_name in LANGUAGE_CODES.keys():
            self.cmb_language.addItem(lang_name)
        self.cmb_language.setToolTip("Chọn ngôn ngữ hoặc để tự động nhận diện")
        opt_layout.addWidget(self.cmb_language, 0, 1)
        
        # Diarization (speaker detection)
        self.chk_diarize = QtWidgets.QCheckBox("Phân biệt người nói")
        self.chk_diarize.setToolTip("Nhận diện và đánh dấu từng người nói")
        self.chk_diarize.stateChanged.connect(self._on_diarize_changed)
        opt_layout.addWidget(self.chk_diarize, 1, 0, 1, 2)
        
        # Number of speakers
        opt_layout.addWidget(QtWidgets.QLabel("Số người nói:"), 2, 0)
        self.spn_speakers = QtWidgets.QSpinBox()
        self.spn_speakers.setRange(0, 32)
        self.spn_speakers.setValue(0)
        self.spn_speakers.setSpecialValueText("Tự động")
        self.spn_speakers.setToolTip("0 = tự động, tối đa 32 người")
        self.spn_speakers.setEnabled(False)
        opt_layout.addWidget(self.spn_speakers, 2, 1)
        
        # Tag audio events
        self.chk_tag_events = QtWidgets.QCheckBox("Đánh dấu sự kiện âm thanh")
        self.chk_tag_events.setChecked(True)
        self.chk_tag_events.setToolTip("Đánh dấu tiếng cười, tiếng bước chân, etc.")
        opt_layout.addWidget(self.chk_tag_events, 3, 0, 1, 2)
        
        # Thread count
        opt_layout.addWidget(QtWidgets.QLabel("Số luồng:"), 4, 0)
        self.spn_threads = QtWidgets.QSpinBox()
        self.spn_threads.setRange(1, 10)
        self.spn_threads.setValue(3)
        self.spn_threads.setToolTip("Số job chạy song song (1-10)")
        opt_layout.addWidget(self.spn_threads, 4, 1)
        
        grp_options.setMinimumWidth(250)
        top_row.addWidget(grp_options)
        top_row.addStretch()
        root.addLayout(top_row)

        # ========== JOB TABLE ==========
        grp_jobs = QtWidgets.QGroupBox("Danh sách công việc")
        jobs_layout = QtWidgets.QVBoxLayout(grp_jobs)
        
        self.tbl_jobs = QtWidgets.QTableWidget(0, 5)
        self.tbl_jobs.setHorizontalHeaderLabels([
            "#", "File", "Trạng thái", "Ngôn ngữ", "Hành động"
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
        self.tbl_jobs.setColumnWidth(4, 120)
        
        jobs_layout.addWidget(self.tbl_jobs)
        root.addWidget(grp_jobs, 1)
        
        # ========== RESULT SECTION ==========
        grp_result = QtWidgets.QGroupBox("Kết quả")
        result_layout = QtWidgets.QVBoxLayout(grp_result)
        
        self.txt_result = QtWidgets.QTextEdit()
        self.txt_result.setPlaceholderText("Kết quả chuyển đổi sẽ hiển thị ở đây...")
        self.txt_result.setReadOnly(True)
        self.txt_result.setMinimumHeight(150)
        result_layout.addWidget(self.txt_result)
        
        # Export buttons
        export_row = QtWidgets.QHBoxLayout()
        
        self.btn_copy = QtWidgets.QPushButton("📋 Sao chép")
        self.btn_copy.clicked.connect(self._copy_result)
        export_row.addWidget(self.btn_copy)
        
        self.btn_save_txt = QtWidgets.QPushButton("💾 Lưu TXT")
        self.btn_save_txt.clicked.connect(self._save_txt)
        export_row.addWidget(self.btn_save_txt)
        
        self.btn_save_srt = QtWidgets.QPushButton("📝 Lưu SRT")
        self.btn_save_srt.clicked.connect(self._save_srt)
        export_row.addWidget(self.btn_save_srt)
        
        export_row.addStretch()
        result_layout.addLayout(export_row)
        
        root.addWidget(grp_result)
        
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
        self.btn_start.clicked.connect(self._start_transcription)
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
        self.btn_stop.clicked.connect(self._stop_transcription)
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
    
    def _on_diarize_changed(self, state):
        """Enable/disable speaker count when diarization is toggled."""
        self.spn_speakers.setEnabled(state == Qt.Checked)

    # ========== FILE HANDLING ==========
    def _browse_file(self):
        """Browse for audio/video file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file audio/video", "",
            "Audio/Video Files (*.mp3 *.wav *.m4a *.flac *.ogg *.mp4 *.mkv *.avi *.mov *.webm);;All Files (*.*)"
        )
        if file_path:
            self.txt_path.setText(file_path)
            self._load_file(file_path)
    
    def _browse_folder(self):
        """Browse for folder containing audio/video files."""
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if folder:
            self.txt_path.setText(folder)
            self._load_folder(folder)
    
    def _load_file(self, file_path: str):
        """Load a single audio/video file."""
        self.queue_paths = [file_path]
        self.jobs = []
        
        job_id = str(uuid.uuid4())[:8]
        self.jobs.append({
            'id': job_id,
            'file_path': file_path,
            'file_name': os.path.basename(file_path),
            'status': 'pending',
            'language': '',
            'text': '',
            'words': []
        })
        
        self.lbl_info.setText(f"📄 1 file: {os.path.basename(file_path)}")
        self._refresh_table()
    
    def _load_folder(self, folder: str):
        """Load all audio/video files in folder."""
        self.queue_paths = []
        self.jobs = []
        
        extensions = ('.mp3', '.wav', '.m4a', '.flac', '.ogg', '.mp4', '.mkv', '.avi', '.mov', '.webm')
        files = sorted([f for f in os.listdir(folder) if f.lower().endswith(extensions)])
        
        for file_name in files:
            file_path = os.path.join(folder, file_name)
            self.queue_paths.append(file_path)
            
            job_id = str(uuid.uuid4())[:8]
            self.jobs.append({
                'id': job_id,
                'file_path': file_path,
                'file_name': file_name,
                'status': 'pending',
                'language': '',
                'text': '',
                'words': []
            })
        
        self.lbl_info.setText(f"📁 {len(self.jobs)} file(s) từ thư mục")
        self._refresh_table()


    # ========== TRANSCRIPTION ==========
    def _start_transcription(self):
        """Start transcribing audio files."""
        if not self.jobs:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn file hoặc thư mục!")
            return
        
        # Reset all jobs to pending (cho phép chạy lại)
        for job in self.jobs:
            if job['status'] in ('done', 'failed'):
                job['status'] = 'pending'
                job['text'] = ''
                job['words'] = []
                job['language'] = ''
        
        # Check API key
        keys_pool = self._get_keys_pool()
        if not keys_pool or not keys_pool.cur():
            QMessageBox.warning(self, "Lỗi", "Không có API key!")
            return
        
        # Force refresh proxy before starting
        main_window = self.main_window
        if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
            self.lbl_status.setText("Đang xoay proxy...")
            QtWidgets.QApplication.processEvents()
            
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    fresh_proxy = main_window.proxy_service_db.force_refresh()
                    if fresh_proxy:
                        _log(f"✅ [Pre-STT] Fresh proxy: {fresh_proxy[:50]}...")
                        break
                    else:
                        _log(f"⚠️ [Pre-STT] Attempt {attempt + 1}/{max_attempts} - waiting for proxy...")
                        time.sleep(3)
                except Exception as e:
                    _log(f"⚠️ Pre-STT proxy refresh failed: {e}")
                    time.sleep(3)
        
        # Reset state
        self._stop = False
        self._completed_count = 0
        self.runnables = []
        
        # Get options
        lang_name = self.cmb_language.currentText()
        language_code = LANGUAGE_CODES.get(lang_name)
        diarize = self.chk_diarize.isChecked()
        num_speakers = self.spn_speakers.value() if diarize and self.spn_speakers.value() > 0 else None
        tag_audio_events = self.chk_tag_events.isChecked()
        max_workers = self.spn_threads.value()
        
        # Proxy getter function
        def get_proxy():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                return main_window.proxy_service_db.get_current_proxy()
            return None
        
        # Proxy rotator function
        def proxy_rotator():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                return main_window.proxy_service_db.force_refresh()
            return None
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        
        # Create signals object
        self._signals = STTWorkerSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.key_rotated.connect(self._on_key_rotated)
        
        # Setup thread pool
        pool = QThreadPool.globalInstance()
        pool.setMaxThreadCount(max_workers)
        
        _log(f"🚀 Starting {len(self.jobs)} STT jobs with {max_workers} threads")
        
        # Create and start runnables for pending jobs
        pending_jobs = [j for j in self.jobs if j['status'] == 'pending']
        for job in pending_jobs:
            job['status'] = 'queued'
            
            runnable = STTRunnable(
                job_id=job['id'],
                file_path=job['file_path'],
                keys_pool=keys_pool,
                key_lock=self._key_lock,
                proxy_getter=get_proxy,
                proxy_rotator=proxy_rotator,
                signals=self._signals,
                stop_flag_ref=lambda: self._stop,
                language_code=language_code,
                diarize=diarize,
                num_speakers=num_speakers,
                tag_audio_events=tag_audio_events
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
                break
        self._refresh_table()
    
    def _on_key_rotated(self, job_id: str, new_key: str):
        """Handle key rotation."""
        _log(f"🔄 Job {job_id} rotated to key: {new_key}")
    
    def _on_finished(self, job_id: str, success: bool, result_data: dict, error: str):
        """Handle worker finished."""
        with self._results_lock:
            self._completed_count += 1
        
        for job in self.jobs:
            if job['id'] == job_id:
                if success:
                    job['status'] = 'done'
                    job['text'] = result_data.get('text', '')
                    job['words'] = result_data.get('words', [])
                    job['language'] = result_data.get('language_code', '')
                    
                    # Show result in text area
                    self.txt_result.setPlainText(job['text'])
                else:
                    job['status'] = 'failed'
                    job['error'] = error
                break
        
        self._refresh_table()
        
        # Update status
        total_jobs = len([j for j in self.jobs if j['status'] != 'pending'])
        self.lbl_status.setText(f"Đang xử lý {self._completed_count}/{total_jobs}...")
        
        # Check if all done
        pending_or_running = sum(1 for j in self.jobs if j['status'] in ('pending', 'queued', 'transcribing'))
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
    
    def _stop_transcription(self):
        """Stop all workers."""
        self._stop = True
        
        pool = QThreadPool.globalInstance()
        pool.clear()
        
        for job in self.jobs:
            if job['status'] in ('queued', 'pending'):
                job['status'] = 'pending'
        
        self.runnables = []
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("Đã dừng")
    
    def _clear_completed(self):
        """Clear completed/failed jobs."""
        self.jobs = [j for j in self.jobs if j['status'] not in ('done', 'failed')]
        self._refresh_table()
        self.txt_result.clear()

    # ========== TABLE ==========
    def _refresh_table(self):
        """Refresh job table."""
        self.tbl_jobs.setRowCount(len(self.jobs))
        
        for i, job in enumerate(self.jobs):
            # #
            item_idx = QtWidgets.QTableWidgetItem(str(i + 1))
            item_idx.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 0, item_idx)
            
            # File name
            self.tbl_jobs.setItem(i, 1, QtWidgets.QTableWidgetItem(job.get('file_name', '')))
            
            # Status
            status = job.get('status', '')
            status_map = {
                'pending': '⏳ Chờ',
                'queued': '📋 Hàng đợi',
                'transcribing': '🔄 Đang xử lý',
                'done': '✅ Xong',
                'failed': '❌ Lỗi'
            }
            item_status = QtWidgets.QTableWidgetItem(status_map.get(status, status))
            item_status.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 2, item_status)
            
            # Language
            lang = job.get('language', '')
            item_lang = QtWidgets.QTableWidgetItem(lang)
            item_lang.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 3, item_lang)
            
            # Action
            action_widget = QtWidgets.QWidget()
            action_layout = QtWidgets.QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            action_layout.setSpacing(2)
            
            if job.get('text'):
                btn_view = QtWidgets.QPushButton("👁 Xem")
                btn_view.setToolTip("Xem kết quả")
                btn_view.setFixedSize(50, 24)
                btn_view.clicked.connect(lambda _, j=job: self._view_result(j))
                action_layout.addWidget(btn_view)
                
                btn_save = QtWidgets.QPushButton("💾")
                btn_save.setToolTip("Lưu kết quả")
                btn_save.setFixedSize(28, 24)
                btn_save.clicked.connect(lambda _, j=job: self._save_job_result(j))
                action_layout.addWidget(btn_save)
            
            action_layout.addStretch()
            self.tbl_jobs.setCellWidget(i, 4, action_widget)
    
    def _view_result(self, job: dict):
        """View result of a job."""
        self.txt_result.setPlainText(job.get('text', ''))
    
    def _save_job_result(self, job: dict):
        """Save result of a specific job."""
        if not job.get('text'):
            return
        
        base_name = os.path.splitext(job.get('file_name', 'output'))[0]
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Lưu kết quả", f"{base_name}.txt",
            "Text Files (*.txt);;SRT Files (*.srt);;All Files (*.*)"
        )
        
        if file_path:
            try:
                if file_path.endswith('.srt'):
                    self._write_srt(file_path, job.get('words', []))
                else:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(job.get('text', ''))
                _log(f"✅ Saved: {file_path}")
            except Exception as e:
                QMessageBox.warning(self, "Lỗi", f"Không lưu được file: {e}")

    # ========== EXPORT ==========
    def _copy_result(self):
        """Copy result to clipboard."""
        text = self.txt_result.toPlainText()
        if text:
            QtWidgets.QApplication.clipboard().setText(text)
            self.lbl_status.setText("✅ Đã sao chép!")
    
    def _save_txt(self):
        """Save result as TXT file."""
        text = self.txt_result.toPlainText()
        if not text:
            QMessageBox.warning(self, "Lỗi", "Không có kết quả để lưu!")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Lưu file TXT", "transcript.txt",
            "Text Files (*.txt);;All Files (*.*)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(text)
                _log(f"✅ Saved TXT: {file_path}")
                self.lbl_status.setText(f"✅ Đã lưu: {os.path.basename(file_path)}")
            except Exception as e:
                QMessageBox.warning(self, "Lỗi", f"Không lưu được file: {e}")
    
    def _save_srt(self):
        """Save result as SRT subtitle file."""
        # Get current selected job with words
        selected_job = None
        for job in self.jobs:
            if job.get('words'):
                selected_job = job
                break
        
        if not selected_job or not selected_job.get('words'):
            QMessageBox.warning(self, "Lỗi", "Không có dữ liệu timestamp để tạo SRT!")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Lưu file SRT", "transcript.srt",
            "SRT Files (*.srt);;All Files (*.*)"
        )
        
        if file_path:
            try:
                self._write_srt(file_path, selected_job.get('words', []))
                _log(f"✅ Saved SRT: {file_path}")
                self.lbl_status.setText(f"✅ Đã lưu: {os.path.basename(file_path)}")
            except Exception as e:
                QMessageBox.warning(self, "Lỗi", f"Không lưu được file: {e}")
    
    def _write_srt(self, file_path: str, words: List[dict]):
        """Write words to SRT format."""
        if not words:
            return
        
        # Group words into segments (max 10 words or 4 seconds per segment)
        segments = []
        current_segment = []
        segment_start = None
        
        for word in words:
            if word.get('type') != 'word':
                continue
            
            if segment_start is None:
                segment_start = word.get('start', 0)
            
            current_segment.append(word.get('text', ''))
            
            # Check if segment should end
            segment_duration = (word.get('end', 0) or 0) - (segment_start or 0)
            if len(current_segment) >= 10 or segment_duration >= 4:
                segments.append({
                    'start': segment_start,
                    'end': word.get('end', 0),
                    'text': ' '.join(current_segment)
                })
                current_segment = []
                segment_start = None
        
        # Add remaining words
        if current_segment:
            last_word = words[-1] if words else {}
            segments.append({
                'start': segment_start or 0,
                'end': last_word.get('end', 0) or 0,
                'text': ' '.join(current_segment)
            })
        
        # Write SRT
        with open(file_path, 'w', encoding='utf-8') as f:
            for i, seg in enumerate(segments, 1):
                start_time = self._format_srt_time(seg['start'])
                end_time = self._format_srt_time(seg['end'])
                f.write(f"{i}\n")
                f.write(f"{start_time} --> {end_time}\n")
                f.write(f"{seg['text']}\n\n")
    
    def _format_srt_time(self, seconds: float) -> str:
        """Format seconds to SRT time format (HH:MM:SS,mmm)."""
        if seconds is None:
            seconds = 0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    # ========== HELPERS ==========
    def _get_keys_pool(self):
        """Get keys pool from main window."""
        if self.main_window and hasattr(self.main_window, 'keys'):
            return self.main_window.keys
        return None

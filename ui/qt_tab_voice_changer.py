"""
Voice Changer Tab - Thay đổi giọng nói (Speech-to-Speech)
Chuyển đổi audio từ giọng này sang giọng khác.

Features:
- Upload audio file
- Chọn voice đích (search + save như TTS)
- Hỗ trợ nhiều key (key rotation)
- Remove background noise
- Batch convert nhiều file
- Save/Load settings
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, QThread, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import QHeaderView, QFileDialog, QMessageBox
from typing import Optional, Dict, List, Callable
import os
import json
import time
import uuid
import threading
from datetime import datetime

from services.voice_changer_service import VoiceChangerService, STS_MODELS, OUTPUT_FORMATS


def _get_app_dir() -> str:
    """Get application directory."""
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log(msg: str):
    """Log message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [VoiceChanger] {msg}")


# Config file path
CONFIG_FILE = os.path.join(_get_app_dir(), "voice_changer_config.json")


def load_config() -> Dict:
    """Load saved config."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        _log(f"Error loading config: {e}")
    return {}


def save_config(config: Dict):
    """Save config to file."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        _log(f"Config saved to {CONFIG_FILE}")
    except Exception as e:
        _log(f"Error saving config: {e}")


# ========== SIGNALS FOR WORKER ==========
class VCWorkerSignals(QObject):
    """Signals for VoiceChangerRunnable."""
    progress = Signal(str, str, int)  # job_id, status, percent
    finished = Signal(str, bool, str, str)  # job_id, success, output_path, error
    key_rotated = Signal(str, str)  # job_id, new_key_prefix


# ========== VOICE CHANGER RUNNABLE ==========
class VoiceChangerRunnable(QRunnable):
    """Runnable for converting audio in parallel."""
    
    def __init__(
        self,
        job_id: str,
        file_path: str,
        output_path: str,
        voice_id: str,
        model_id: str,
        keys_pool,
        key_lock: threading.Lock,
        proxy_getter: Callable = None,
        proxy_rotator: Callable = None,
        signals: VCWorkerSignals = None,
        stop_flag_ref: Callable = None,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        use_speaker_boost: bool = False,
        remove_background_noise: bool = False,
        output_format: str = "mp3_44100_128",
    ):
        super().__init__()
        self.job_id = job_id
        self.file_path = file_path
        self.output_path = output_path
        self.voice_id = voice_id
        self.model_id = model_id
        self.keys_pool = keys_pool
        self._key_lock = key_lock
        self.proxy_getter = proxy_getter
        self.proxy_rotator = proxy_rotator
        self.signals = signals
        self._stop_flag_ref = stop_flag_ref
        
        # Voice settings
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.style = style
        self.use_speaker_boost = use_speaker_boost
        self.remove_background_noise = remove_background_noise
        self.output_format = output_format
        
        self._current_key = None
        self._key_rotate_count = 0
        self._max_key_rotates = 30
        
        self.service = VoiceChangerService()
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
                        required_chars=5000,  # Estimate for STS
                        max_attempts=50,
                        timeout=30.0,
                        line_id=f"vc_{self.job_id}"
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
                        required_chars=5000,
                        max_attempts=50,
                        timeout=30.0,
                        line_id=f"vc_{self.job_id}"
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
                        line_id=f"vc_{self.job_id}"
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
                    line_id=f"vc_{self.job_id}"
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
            # Acquire key
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
                self.signals.progress.emit(self.job_id, "converting", 30)
            
            # Convert with retry
            result = self._convert_with_retry()
            
            if result.get('success'):
                self._release_key(self._current_key, success=True)
                if self.signals:
                    self.signals.progress.emit(self.job_id, "done", 100)
                    self.signals.finished.emit(self.job_id, True, result.get('output_path', ''), "")
            else:
                self._release_key(self._current_key, success=False, error_code=result.get('error_type'))
                if self.signals:
                    self.signals.finished.emit(self.job_id, False, "", result.get('error', 'Unknown error'))
                
        except Exception as e:
            _log(f"❌ [{self.job_id}] Exception: {e}")
            if self.signals:
                self.signals.finished.emit(self.job_id, False, "", str(e))
    
    def _convert_with_retry(self) -> Dict:
        """Convert with retry on key/proxy errors."""
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
            
            proxy = self._get_proxy()
            
            success, result = self.service.convert(
                api_key=self._current_key,
                voice_id=self.voice_id,
                audio_path=self.file_path,
                output_path=self.output_path,
                model_id=self.model_id,
                stability=self.stability,
                similarity_boost=self.similarity_boost,
                style=self.style,
                use_speaker_boost=self.use_speaker_boost,
                remove_background_noise=self.remove_background_noise,
                output_format=self.output_format,
                proxy=proxy,
            )
            
            if success:
                return {'success': True, 'output_path': result}
            
            error = result
            error_type = self.service.get_error_type(error)
            
            # Connection errors - retry with delay
            if error_type == 'connection':
                _log(f"⚠️ [{self.job_id}] Connection error, retrying ({retry + 1}/{max_retries})...")
                time.sleep(5)
                continue
            
            # Proxy issue
            if error_type == 'proxy':
                _log(f"⚠️ [{self.job_id}] Proxy issue, rotating proxy...")
                if self.proxy_rotator and callable(self.proxy_rotator):
                    try:
                        self.proxy_rotator()
                    except Exception as e:
                        _log(f"⚠️ [{self.job_id}] Proxy rotation error: {e}")
                time.sleep(3)
                continue
            
            # Key errors - rotate
            if error_type in ['401', '402']:
                _log(f"🔄 [{self.job_id}] Key error {error_type}, rotating...")
                self._mark_key_error(self._current_key, error_type)
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                if self.signals:
                    self.signals.key_rotated.emit(self.job_id, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(1)
                continue
            
            # Rate limit - rotate key
            if error_type == '429':
                _log(f"🔄 [{self.job_id}] Rate limit (429), rotating key...")
                self._mark_key_error(self._current_key, '429')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                if self.signals:
                    self.signals.key_rotated.emit(self.job_id, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(2)
                continue
            
            # Server errors (5xx) - retry
            if error_type == 'server':
                _log(f"⚠️ [{self.job_id}] Server error, retrying...")
                time.sleep(5)
                continue
            
            # Other errors - don't retry
            return {'success': False, 'error': error, 'error_type': error_type}
        
        return {'success': False, 'error': f'Đã thử {max_retries} lần, không thành công'}


# ========== VOICE CHANGER TAB UI ==========
class VoiceChangerTab(QtWidgets.QWidget):
    """Tab for Voice Changer (Thay đổi giọng nói)."""
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.queue_paths: List[str] = []
        self.jobs: List[Dict] = []
        self.runnables: List[VoiceChangerRunnable] = []
        self._stop = False
        self._key_lock = threading.Lock()
        self._completed_count = 0
        
        # Voice search results
        self._voice_results = []
        
        # Load saved config
        self._config = load_config()
        
        self._setup_ui()
        self._load_saved_settings()
    
    def _setup_ui(self):
        """Setup the voice changer tab UI."""
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)
        
        # ========== TOP ROW: Voice + Settings + Options (40-30-30) ==========
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(10)
        
        # === VOICE SECTION (40%) ===
        grp_voice = QtWidgets.QGroupBox("Giọng đọc đích")
        voice_layout = QtWidgets.QVBoxLayout(grp_voice)
        voice_layout.setSpacing(5)
        
        # Voice ID input + Search
        voice_row1 = QtWidgets.QHBoxLayout()
        voice_row1.addWidget(QtWidgets.QLabel("Voice ID:"))
        self.txt_voice_id = QtWidgets.QLineEdit()
        self.txt_voice_id.setPlaceholderText("Nhập Voice ID hoặc tên...")
        voice_row1.addWidget(self.txt_voice_id, 1)
        
        self.btn_search = QtWidgets.QPushButton("🔍 Tìm")
        self.btn_search.setFixedWidth(60)
        self.btn_search.clicked.connect(self._search_voice)
        voice_row1.addWidget(self.btn_search)
        voice_layout.addLayout(voice_row1)
        
        # Voice combo (search results)
        voice_row2 = QtWidgets.QHBoxLayout()
        voice_row2.addWidget(QtWidgets.QLabel("Kết quả:"))
        self.cmb_voice = QtWidgets.QComboBox()
        self.cmb_voice.currentIndexChanged.connect(self._on_voice_selected)
        voice_row2.addWidget(self.cmb_voice, 1)
        
        self.btn_save_voice = QtWidgets.QPushButton("💾 Lưu")
        self.btn_save_voice.setFixedWidth(60)
        self.btn_save_voice.setToolTip("Lưu voice vào danh sách")
        self.btn_save_voice.clicked.connect(self._save_voice)
        voice_row2.addWidget(self.btn_save_voice)
        voice_layout.addLayout(voice_row2)
        
        # Saved voices
        voice_row3 = QtWidgets.QHBoxLayout()
        voice_row3.addWidget(QtWidgets.QLabel("Đã lưu:"))
        self.cmb_saved_voices = QtWidgets.QComboBox()
        self.cmb_saved_voices.currentIndexChanged.connect(self._on_saved_voice_selected)
        voice_row3.addWidget(self.cmb_saved_voices, 1)
        
        self.btn_delete_voice = QtWidgets.QPushButton("🗑")
        self.btn_delete_voice.setToolTip("Xóa voice đã chọn")
        self.btn_delete_voice.setFixedWidth(40)
        self.btn_delete_voice.clicked.connect(self._delete_saved_voice)
        voice_row3.addWidget(self.btn_delete_voice)
        voice_layout.addLayout(voice_row3)
        
        top_row.addWidget(grp_voice, 40)  # 40%
        
        # === SETTINGS SECTION (30%) ===
        grp_settings = QtWidgets.QGroupBox("Cài đặt")
        settings_layout = QtWidgets.QGridLayout(grp_settings)
        settings_layout.setHorizontalSpacing(8)
        settings_layout.setVerticalSpacing(5)
        
        # Model
        settings_layout.addWidget(QtWidgets.QLabel("Model:"), 0, 0)
        self.cmb_model = QtWidgets.QComboBox()
        for model_id, model_name in STS_MODELS:
            self.cmb_model.addItem(f"{model_name}", model_id)
        settings_layout.addWidget(self.cmb_model, 0, 1)
        
        # Output format
        settings_layout.addWidget(QtWidgets.QLabel("Format:"), 1, 0)
        self.cmb_format = QtWidgets.QComboBox()
        for fmt in OUTPUT_FORMATS:
            self.cmb_format.addItem(fmt)
        self.cmb_format.setCurrentText("mp3_44100_128")
        settings_layout.addWidget(self.cmb_format, 1, 1)
        
        # Stability
        settings_layout.addWidget(QtWidgets.QLabel("Ổn định:"), 2, 0)
        self.spn_stability = QtWidgets.QSpinBox()
        self.spn_stability.setRange(0, 100)
        self.spn_stability.setValue(50)
        self.spn_stability.setSuffix("%")
        settings_layout.addWidget(self.spn_stability, 2, 1)
        
        # Similarity
        settings_layout.addWidget(QtWidgets.QLabel("Tương đồng:"), 3, 0)
        self.spn_similarity = QtWidgets.QSpinBox()
        self.spn_similarity.setRange(0, 100)
        self.spn_similarity.setValue(75)
        self.spn_similarity.setSuffix("%")
        settings_layout.addWidget(self.spn_similarity, 3, 1)
        
        top_row.addWidget(grp_settings, 30)  # 30%
        
        # === OPTIONS SECTION (30%) ===
        grp_options = QtWidgets.QGroupBox("Tùy chọn")
        opt_layout = QtWidgets.QVBoxLayout(grp_options)
        opt_layout.setSpacing(5)
        
        # Style
        style_row = QtWidgets.QHBoxLayout()
        style_row.addWidget(QtWidgets.QLabel("Style:"))
        self.spn_style = QtWidgets.QSpinBox()
        self.spn_style.setRange(0, 100)
        self.spn_style.setValue(0)
        self.spn_style.setSuffix("%")
        style_row.addWidget(self.spn_style)
        opt_layout.addLayout(style_row)
        
        # Checkboxes
        self.chk_speaker_boost = QtWidgets.QCheckBox("Tăng cường loa")
        opt_layout.addWidget(self.chk_speaker_boost)
        
        self.chk_remove_noise = QtWidgets.QCheckBox("Loại bỏ noise")
        self.chk_remove_noise.setToolTip("Loại bỏ tiếng ồn nền từ audio nguồn")
        opt_layout.addWidget(self.chk_remove_noise)
        
        # Thread count
        thread_row = QtWidgets.QHBoxLayout()
        thread_row.addWidget(QtWidgets.QLabel("Số luồng:"))
        self.spn_threads = QtWidgets.QSpinBox()
        self.spn_threads.setRange(1, 10)
        self.spn_threads.setValue(3)
        thread_row.addWidget(self.spn_threads)
        opt_layout.addLayout(thread_row)
        
        opt_layout.addStretch()
        top_row.addWidget(grp_options, 30)  # 30%
        
        root.addLayout(top_row)
        
        # ========== INPUT SECTION ==========
        grp_input = QtWidgets.QGroupBox("Đầu vào")
        input_layout = QtWidgets.QVBoxLayout(grp_input)
        
        # File/Folder path
        file_row = QtWidgets.QHBoxLayout()
        self.txt_path = QtWidgets.QLineEdit()
        self.txt_path.setPlaceholderText("Chọn file audio hoặc thư mục...")
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
        
        root.addWidget(grp_input)
        
        # ========== JOB TABLE ==========
        grp_jobs = QtWidgets.QGroupBox("Danh sách công việc")
        jobs_layout = QtWidgets.QVBoxLayout(grp_jobs)
        
        self.tbl_jobs = QtWidgets.QTableWidget(0, 5)
        self.tbl_jobs.setHorizontalHeaderLabels([
            "#", "File nguồn", "Trạng thái", "Output", "Hành động"
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
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(4, 100)
        
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
        self.btn_start.clicked.connect(self._start_conversion)
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
        self.btn_stop.clicked.connect(self._stop_conversion)
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

    def _load_saved_settings(self):
        """Load saved settings from config."""
        try:
            # Load saved voices
            saved_voices = self._config.get('saved_voices', [])
            self.cmb_saved_voices.clear()
            self.cmb_saved_voices.addItem("-- Chọn voice đã lưu --", "")
            for v in saved_voices:
                self.cmb_saved_voices.addItem(f"{v['name']} ({v['voice_id'][:8]}...)", v['voice_id'])
            
            # Load last settings
            if 'last_voice_id' in self._config:
                self.txt_voice_id.setText(self._config['last_voice_id'])
            if 'last_model' in self._config:
                idx = self.cmb_model.findData(self._config['last_model'])
                if idx >= 0:
                    self.cmb_model.setCurrentIndex(idx)
            if 'stability' in self._config:
                self.spn_stability.setValue(self._config['stability'])
            if 'similarity' in self._config:
                self.spn_similarity.setValue(self._config['similarity'])
            if 'style' in self._config:
                self.spn_style.setValue(self._config['style'])
            if 'speaker_boost' in self._config:
                self.chk_speaker_boost.setChecked(self._config['speaker_boost'])
            if 'remove_noise' in self._config:
                self.chk_remove_noise.setChecked(self._config['remove_noise'])
            if 'output_format' in self._config:
                idx = self.cmb_format.findText(self._config['output_format'])
                if idx >= 0:
                    self.cmb_format.setCurrentIndex(idx)
            if 'threads' in self._config:
                self.spn_threads.setValue(self._config['threads'])
            
            _log(f"Loaded {len(saved_voices)} saved voices")
        except Exception as e:
            _log(f"Error loading settings: {e}")
    
    def _save_current_settings(self):
        """Save current settings to config."""
        try:
            self._config['last_voice_id'] = self.txt_voice_id.text().strip()
            self._config['last_model'] = self.cmb_model.currentData()
            self._config['stability'] = self.spn_stability.value()
            self._config['similarity'] = self.spn_similarity.value()
            self._config['style'] = self.spn_style.value()
            self._config['speaker_boost'] = self.chk_speaker_boost.isChecked()
            self._config['remove_noise'] = self.chk_remove_noise.isChecked()
            self._config['output_format'] = self.cmb_format.currentText()
            self._config['threads'] = self.spn_threads.value()
            save_config(self._config)
        except Exception as e:
            _log(f"Error saving settings: {e}")
    
    # ========== VOICE SEARCH ==========
    def _get_api_key(self) -> Optional[str]:
        """Get an API key for search."""
        main_window = self.main_window
        if main_window:
            # Main window uses self.keys (KeyPoolManager)
            if hasattr(main_window, 'keys') and main_window.keys:
                key = main_window.keys.cur()
                if key:
                    return key
        return None
    
    def _search_voice(self):
        """Search for voice by ID or name."""
        query = self.txt_voice_id.text().strip()
        if not query:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Voice ID hoặc tên để tìm kiếm!")
            return
        
        api_key = self._get_api_key()
        if not api_key:
            QMessageBox.warning(self, "Lỗi", "Không có API key!")
            return
        
        try:
            from services.voice_service import VoiceService
            svc = VoiceService()
            results = svc.search_voices_by_id_or_name(api_key, query)
            
            self._voice_results = results
            self.cmb_voice.clear()
            
            if results:
                for r in results:
                    label = r['name']
                    if not r.get("free_users_allowed", True):
                        label += " (Paid)"
                    self.cmb_voice.addItem(label, r['voice_id'])
                
                # Auto select first
                self.cmb_voice.setCurrentIndex(0)
                self._on_voice_selected(0)
                
                _log(f"Found {len(results)} voices for '{query}'")
            else:
                QMessageBox.information(self, "Kết quả", f"Không tìm thấy voice cho '{query}'")
                
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Lỗi tìm kiếm: {e}")
    
    def _on_voice_selected(self, index: int):
        """Handle voice selection from search results."""
        if index < 0 or index >= len(self._voice_results):
            return
        
        voice = self._voice_results[index]
        self.txt_voice_id.setText(voice['voice_id'])
        
        # Load voice settings if available
        if 'settings' in voice and voice['settings']:
            settings = voice['settings']
            if 'stability' in settings:
                self.spn_stability.setValue(int(float(settings['stability']) * 100))
            if 'similarity_boost' in settings:
                self.spn_similarity.setValue(int(float(settings['similarity_boost']) * 100))
            if 'style' in settings:
                self.spn_style.setValue(int(float(settings['style']) * 100))
            if 'use_speaker_boost' in settings:
                self.chk_speaker_boost.setChecked(bool(settings['use_speaker_boost']))
    
    def _save_voice(self):
        """Save current voice to saved list."""
        voice_id = self.txt_voice_id.text().strip()
        if not voice_id:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Voice ID!")
            return
        
        # Get voice name
        voice_name = ""
        if self._voice_results:
            for v in self._voice_results:
                if v['voice_id'] == voice_id:
                    voice_name = v['name']
                    break
        
        if not voice_name:
            voice_name = voice_id[:10] + "..."
        
        # Check if already saved
        saved_voices = self._config.get('saved_voices', [])
        for v in saved_voices:
            if v['voice_id'] == voice_id:
                QMessageBox.information(self, "Thông báo", "Voice này đã được lưu!")
                return
        
        # Save
        saved_voices.append({
            'voice_id': voice_id,
            'name': voice_name,
            'stability': self.spn_stability.value(),
            'similarity': self.spn_similarity.value(),
            'style': self.spn_style.value(),
            'speaker_boost': self.chk_speaker_boost.isChecked(),
        })
        self._config['saved_voices'] = saved_voices
        save_config(self._config)
        
        # Refresh combo
        self.cmb_saved_voices.addItem(f"{voice_name} ({voice_id[:8]}...)", voice_id)
        
        QMessageBox.information(self, "Thành công", f"Đã lưu voice: {voice_name}")
    
    def _on_saved_voice_selected(self, index: int):
        """Handle saved voice selection."""
        voice_id = self.cmb_saved_voices.currentData()
        if not voice_id:
            return
        
        self.txt_voice_id.setText(voice_id)
        
        # Load saved settings
        saved_voices = self._config.get('saved_voices', [])
        for v in saved_voices:
            if v['voice_id'] == voice_id:
                self.spn_stability.setValue(v.get('stability', 50))
                self.spn_similarity.setValue(v.get('similarity', 75))
                self.spn_style.setValue(v.get('style', 0))
                self.chk_speaker_boost.setChecked(v.get('speaker_boost', False))
                break
    
    def _delete_saved_voice(self):
        """Delete selected saved voice."""
        voice_id = self.cmb_saved_voices.currentData()
        if not voice_id:
            return
        
        reply = QMessageBox.question(
            self, "Xác nhận",
            "Xóa voice này khỏi danh sách đã lưu?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            saved_voices = self._config.get('saved_voices', [])
            saved_voices = [v for v in saved_voices if v['voice_id'] != voice_id]
            self._config['saved_voices'] = saved_voices
            save_config(self._config)
            
            # Refresh combo
            idx = self.cmb_saved_voices.currentIndex()
            self.cmb_saved_voices.removeItem(idx)
    
    # ========== FILE HANDLING ==========
    def _browse_file(self):
        """Browse for audio file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file audio", "",
            "Audio Files (*.mp3 *.wav *.m4a *.flac *.ogg *.aac);;All Files (*.*)"
        )
        if file_path:
            self.txt_path.setText(file_path)
            self._load_file(file_path)
    
    def _browse_folder(self):
        """Browse for folder containing audio files."""
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if folder:
            self.txt_path.setText(folder)
            self._load_folder(folder)
    
    def _load_file(self, file_path: str):
        """Load a single audio file."""
        self.queue_paths = [file_path]
        self.jobs = []
        
        job_id = str(uuid.uuid4())[:8]
        self.jobs.append({
            'id': job_id,
            'file_path': file_path,
            'file_name': os.path.basename(file_path),
            'status': 'pending',
            'output_path': '',
        })
        
        self.lbl_info.setText(f"📄 1 file: {os.path.basename(file_path)}")
        self._refresh_table()
    
    def _load_folder(self, folder: str):
        """Load all audio files in folder."""
        self.queue_paths = []
        self.jobs = []
        
        extensions = ('.mp3', '.wav', '.m4a', '.flac', '.ogg', '.aac')
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
                'output_path': '',
            })
        
        self.lbl_info.setText(f"📁 {len(self.jobs)} file(s) từ thư mục")
        self._refresh_table()
    
    def _refresh_table(self):
        """Refresh job table."""
        self.tbl_jobs.setRowCount(len(self.jobs))
        
        for row, job in enumerate(self.jobs):
            # #
            self.tbl_jobs.setItem(row, 0, QtWidgets.QTableWidgetItem(str(row + 1)))
            # File name
            self.tbl_jobs.setItem(row, 1, QtWidgets.QTableWidgetItem(job['file_name']))
            # Status
            status_item = QtWidgets.QTableWidgetItem(self._get_status_text(job['status']))
            self.tbl_jobs.setItem(row, 2, status_item)
            # Output
            self.tbl_jobs.setItem(row, 3, QtWidgets.QTableWidgetItem(job.get('output_path', '')))
            # Action
            if job['status'] == 'done' and job.get('output_path'):
                btn = QtWidgets.QPushButton("▶ Play")
                btn.clicked.connect(lambda checked, p=job['output_path']: self._play_audio(p))
                self.tbl_jobs.setCellWidget(row, 4, btn)
    
    def _get_status_text(self, status: str) -> str:
        """Get display text for status."""
        status_map = {
            'pending': '⏳ Chờ',
            'queued': '📋 Đợi',
            'converting': '🔄 Đang xử lý',
            'done': '✅ Hoàn thành',
            'failed': '❌ Lỗi',
        }
        return status_map.get(status, status)
    
    def _play_audio(self, path: str):
        """Play audio file."""
        import subprocess
        import sys
        
        if sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        elif sys.platform == 'win32':
            os.startfile(path)
        else:
            subprocess.Popen(['xdg-open', path])

    # ========== CONVERSION ==========
    def _get_keys_pool(self):
        """Get keys pool from main window."""
        main_window = self.main_window
        if main_window:
            # Main window uses self.keys (KeyPoolManager)
            if hasattr(main_window, 'keys') and main_window.keys:
                return main_window.keys
        return None
    
    def _start_conversion(self):
        """Start converting audio files."""
        if not self.jobs:
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn file hoặc thư mục!")
            return
        
        voice_id = self.txt_voice_id.text().strip()
        if not voice_id:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Voice ID!")
            return
        
        # Save settings
        self._save_current_settings()
        
        # Reset jobs
        for job in self.jobs:
            if job['status'] in ('done', 'failed'):
                job['status'] = 'pending'
                job['output_path'] = ''
        
        # Check API key
        keys_pool = self._get_keys_pool()
        if not keys_pool:
            QMessageBox.warning(self, "Lỗi", "Không có API key pool!")
            return
        
        # Get settings
        model_id = self.cmb_model.currentData()
        stability = self.spn_stability.value() / 100.0
        similarity = self.spn_similarity.value() / 100.0
        style = self.spn_style.value() / 100.0
        speaker_boost = self.chk_speaker_boost.isChecked()
        remove_noise = self.chk_remove_noise.isChecked()
        output_format = self.cmb_format.currentText()
        max_workers = self.spn_threads.value()
        
        # Proxy getter
        main_window = self.main_window
        
        def get_proxy():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                return main_window.proxy_service_db.get_current_proxy()
            return None
        
        def proxy_rotator():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                return main_window.proxy_service_db.force_refresh()
            return None
        
        # Reset state
        self._stop = False
        self._completed_count = 0
        self.runnables = []
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        
        # Create signals
        self._signals = VCWorkerSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.key_rotated.connect(self._on_key_rotated)
        
        # Setup thread pool
        pool = QThreadPool.globalInstance()
        pool.setMaxThreadCount(max_workers)
        
        _log(f"🚀 Starting {len(self.jobs)} voice changer jobs with {max_workers} threads")
        
        # Create output directory
        output_dir = os.path.join(os.path.dirname(self.jobs[0]['file_path']), "voice_changed")
        os.makedirs(output_dir, exist_ok=True)
        
        # Create and start runnables
        pending_jobs = [j for j in self.jobs if j['status'] == 'pending']
        for job in pending_jobs:
            job['status'] = 'queued'
            
            # Generate output path
            base_name = os.path.splitext(job['file_name'])[0]
            ext = "mp3" if "mp3" in output_format else "wav" if "pcm" in output_format else "mp3"
            output_path = os.path.join(output_dir, f"{base_name}_vc.{ext}")
            
            runnable = VoiceChangerRunnable(
                job_id=job['id'],
                file_path=job['file_path'],
                output_path=output_path,
                voice_id=voice_id,
                model_id=model_id,
                keys_pool=keys_pool,
                key_lock=self._key_lock,
                proxy_getter=get_proxy,
                proxy_rotator=proxy_rotator,
                signals=self._signals,
                stop_flag_ref=lambda: self._stop,
                stability=stability,
                similarity_boost=similarity,
                style=style,
                use_speaker_boost=speaker_boost,
                remove_background_noise=remove_noise,
                output_format=output_format,
            )
            
            self.runnables.append(runnable)
            pool.start(runnable)
        
        self._refresh_table()
        self.lbl_status.setText(f"Đang xử lý 0/{len(pending_jobs)}...")
    
    def _stop_conversion(self):
        """Stop conversion."""
        self._stop = True
        self.lbl_status.setText("Đang dừng...")
        _log("🛑 Stop requested")
    
    def _on_progress(self, job_id: str, status: str, percent: int):
        """Handle progress update."""
        for job in self.jobs:
            if job['id'] == job_id:
                job['status'] = status
                break
        self._refresh_table()
    
    def _on_finished(self, job_id: str, success: bool, output_path: str, error: str):
        """Handle job finished."""
        for job in self.jobs:
            if job['id'] == job_id:
                if success:
                    job['status'] = 'done'
                    job['output_path'] = output_path
                else:
                    job['status'] = 'failed'
                    job['error'] = error
                break
        
        self._completed_count += 1
        total = len([j for j in self.jobs if j['status'] != 'pending'])
        
        self._refresh_table()
        self.lbl_status.setText(f"Hoàn thành {self._completed_count}/{total}")
        
        # Check if all done
        pending = [j for j in self.jobs if j['status'] in ('pending', 'queued', 'converting')]
        if not pending:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            
            done_count = len([j for j in self.jobs if j['status'] == 'done'])
            failed_count = len([j for j in self.jobs if j['status'] == 'failed'])
            
            self.lbl_status.setText(f"✅ Hoàn thành: {done_count} thành công, {failed_count} lỗi")
            _log(f"✅ All jobs completed: {done_count} success, {failed_count} failed")
    
    def _on_key_rotated(self, job_id: str, new_key_prefix: str):
        """Handle key rotation."""
        _log(f"🔄 [{job_id}] Key rotated to: {new_key_prefix}")
    
    def _clear_completed(self):
        """Clear completed jobs from list."""
        self.jobs = [j for j in self.jobs if j['status'] not in ('done', 'failed')]
        self._refresh_table()
        self.lbl_status.setText(f"Đã xóa, còn {len(self.jobs)} job")

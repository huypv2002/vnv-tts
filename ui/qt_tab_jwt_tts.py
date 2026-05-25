"""
JWT TTS Tab - "💎 Giọng Trả Phí" Tab
UI giống hệt tab chính "Chuyển văn bản" nhưng sử dụng JWT accounts

Features:
- Voice search với feedback
- SRT generation từ audio
- Advanced settings (gap, pause chars)
- Folder batch processing
- Smart account loading (lazy load)
"""

from __future__ import annotations

import os
import sys
import threading
import time
import json
import math
import subprocess
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal, QObject, QRunnable, QThreadPool
from PySide6.QtWidgets import QHeaderView

from services.jwt_account_pool import JWTAccountPool, JWTAPIClient, AccountState
from services.jwt_tts_service import JWTTTSService

# Preview mode imports
try:
    import sys as _sys
    _app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _app_dir not in _sys.path:
        _sys.path.insert(0, _app_dir)
    from preview_client import PreviewClient
    PREVIEW_AVAILABLE = True
except ImportError:
    PREVIEW_AVAILABLE = False


def _get_app_dir() -> str:
    """Get application directory - works for both script and compiled exe."""
    if "__compiled__" in globals():
        # Nuitka compiled
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    if getattr(sys, 'frozen', False):
        # PyInstaller or other frozen
        return os.path.dirname(sys.executable)
    # Running as script - go up one level from ui/ folder
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_DIR = _get_app_dir()
print(f"🔧 [JWT_TTS] APP_DIR = {APP_DIR}")

# 🔧 JWT TTS Settings file - sử dụng APP_DIR từ main app nếu có, fallback về local
JWT_TTS_SETTINGS_FILE = os.path.join(APP_DIR, "jwt_tts_settings.json")


def find_ffprobe() -> Optional[str]:
    """Find ffprobe executable"""
    if sys.platform == 'darwin':
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in [os.path.join(app_dir, 'ffprobe'), '/usr/local/bin/ffprobe', '/opt/homebrew/bin/ffprobe']:
            if os.path.exists(path):
                return path
    else:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bundled = os.path.join(app_dir, 'ffprobe.exe')
        if os.path.exists(bundled):
            return bundled
        found = shutil.which('ffprobe')
        if found:
            return found
    return None


class JWTAdvancedSettingsDialog(QtWidgets.QDialog):
    """Advanced Settings Dialog cho JWT TTS Tab"""
    
    def __init__(self, parent=None, settings: Dict = None):
        super().__init__(parent)
        self.setWindowTitle("Cài đặt nâng cao")
        self.resize(400, 350)
        self._settings = settings or {}
        self._setup_ui()
    
    def _setup_ui(self):
        root = QtWidgets.QGridLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setVerticalSpacing(8)
        r = 0
        
        # === Gap segments ===
        self.cb_gap_segments = QtWidgets.QCheckBox("Ngắt âm giữa các đoạn")
        self.cb_gap_segments.setChecked(self._settings.get('gap_enabled', False))
        root.addWidget(self.cb_gap_segments, r, 0)
        
        self.sb_gap = QtWidgets.QDoubleSpinBox()
        self.sb_gap.setRange(0, 10)
        self.sb_gap.setValue(self._settings.get('gap_seconds', 1.3))
        self.sb_gap.setSingleStep(0.1)
        self.sb_gap.setFixedWidth(60)
        root.addWidget(self.sb_gap, r, 1)
        root.addWidget(QtWidgets.QLabel("(s)"), r, 2)
        r += 1
        
        # Gap every N segments
        root.addWidget(QtWidgets.QLabel("    Cách nhau"), r, 0)
        self.sb_every = QtWidgets.QSpinBox()
        self.sb_every.setRange(1, 100)
        self.sb_every.setValue(self._settings.get('gap_every', 5))
        self.sb_every.setFixedWidth(60)
        root.addWidget(self.sb_every, r, 1)
        root.addWidget(QtWidgets.QLabel("đoạn"), r, 2)
        r += 1
        
        # Separator
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        root.addWidget(line, r, 0, 1, 3)
        r += 1
        
        # === Pause char ===
        self.cb_pause_char = QtWidgets.QCheckBox("Ngắt âm theo ký tự")
        self.cb_pause_char.setChecked(self._settings.get('pause_char_enabled', True))
        root.addWidget(self.cb_pause_char, r, 0, 1, 3)
        r += 1
        
        # Char 1
        char_grid = QtWidgets.QGridLayout()
        char_grid.setHorizontalSpacing(6)
        
        char_grid.addWidget(QtWidgets.QLabel("Ký tự:"), 0, 0)
        self.ed_char1 = QtWidgets.QLineEdit(self._settings.get('char1', ','))
        self.ed_char1.setFixedWidth(40)
        char_grid.addWidget(self.ed_char1, 0, 1)
        
        self.sb_char1 = QtWidgets.QDoubleSpinBox()
        self.sb_char1.setRange(0, 10)
        self.sb_char1.setValue(self._settings.get('char1_sec', 0.3))
        self.sb_char1.setSingleStep(0.1)
        self.sb_char1.setFixedWidth(60)
        char_grid.addWidget(self.sb_char1, 0, 2)
        char_grid.addWidget(QtWidgets.QLabel("(s)"), 0, 3)
        
        # Char 2
        char_grid.addWidget(QtWidgets.QLabel("Ký tự:"), 1, 0)
        self.ed_char2 = QtWidgets.QLineEdit(self._settings.get('char2', '.'))
        self.ed_char2.setFixedWidth(40)
        char_grid.addWidget(self.ed_char2, 1, 1)
        
        self.sb_char2 = QtWidgets.QDoubleSpinBox()
        self.sb_char2.setRange(0, 10)
        self.sb_char2.setValue(self._settings.get('char2_sec', 0.5))
        self.sb_char2.setSingleStep(0.1)
        self.sb_char2.setFixedWidth(60)
        char_grid.addWidget(self.sb_char2, 1, 2)
        char_grid.addWidget(QtWidgets.QLabel("(s)"), 1, 3)
        
        root.addLayout(char_grid, r, 0, 1, 3)
        r += 1
        
        # Separator
        line2 = QtWidgets.QFrame()
        line2.setFrameShape(QtWidgets.QFrame.HLine)
        line2.setFrameShadow(QtWidgets.QFrame.Sunken)
        root.addWidget(line2, r, 0, 1, 3)
        r += 1
        
        # === Max chars per line ===
        root.addWidget(QtWidgets.QLabel("Số ký tự tối đa / chunk:"), r, 0)
        self.ed_max_chars = QtWidgets.QLineEdit(str(self._settings.get('max_chars', 800)))
        self.ed_max_chars.setFixedWidth(80)
        validator = QtGui.QIntValidator(100, 1000, self.ed_max_chars)
        self.ed_max_chars.setValidator(validator)
        root.addWidget(self.ed_max_chars, r, 1)
        r += 1
        
        # Spacer
        root.setRowStretch(r, 1)
        r += 1
        
        # Buttons
        btn = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btn.accepted.connect(self.accept)
        btn.rejected.connect(self.reject)
        root.addWidget(btn, r, 0, 1, 3)
    
    def get_settings(self) -> Dict:
        """Get settings from dialog"""
        # Parse max_chars from input field
        try:
            max_chars = int(self.ed_max_chars.text())
            max_chars = max(100, min(1000, max_chars))  # Clamp to valid range
        except:
            max_chars = 800
        
        return {
            'gap_enabled': self.cb_gap_segments.isChecked(),
            'gap_seconds': self.sb_gap.value(),
            'gap_every': self.sb_every.value(),
            'pause_char_enabled': self.cb_pause_char.isChecked(),
            'char1': self.ed_char1.text() or ',',
            'char1_sec': self.sb_char1.value(),
            'char2': self.ed_char2.text() or '.',
            'char2_sec': self.sb_char2.value(),
            'max_chars': max_chars,
        }


class JWTVoiceManagerDialog(QtWidgets.QDialog):
    """Voice Manager Dialog - Quản lý Voice ID với custom label và model"""
    
    voice_selected = Signal(str, str, str)  # voice_id, label, model_id
    
    def __init__(self, parent=None, voice_list: List[Dict] = None, current_voice_id: str = None, 
                 service=None, proxy_fn=None):
        super().__init__(parent)
        self.setWindowTitle("🎤 Quản lý Voice")
        self.resize(650, 500)
        self._voice_list = voice_list or []
        self._current_voice_id = current_voice_id
        self._service = service  # JWTTTSService for searching
        self._proxy_fn = proxy_fn
        self._pending_new_voice = None  # Voice mới từ search, chờ user click Lưu
        self._setup_ui()
        self._load_voices()
        self._clear_form()  # Clear form on open
    
    def _setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # === Table ===
        self.tbl_voices = QtWidgets.QTableWidget(0, 4)
        self.tbl_voices.setHorizontalHeaderLabels(["Voice ID", "Label", "Model", "Tên gốc"])
        self.tbl_voices.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_voices.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tbl_voices.verticalHeader().setVisible(False)
        
        hdr = self.tbl_voices.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_voices.setColumnWidth(0, 180)  # Voice ID
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)  # Label
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_voices.setColumnWidth(2, 150)  # Model
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_voices.setColumnWidth(3, 120)  # Tên gốc
        
        layout.addWidget(self.tbl_voices)
        
        # === Add/Edit Form ===
        form_group = QtWidgets.QGroupBox("Thêm / Sửa Voice")
        form_layout = QtWidgets.QGridLayout(form_group)
        form_layout.setHorizontalSpacing(10)
        form_layout.setVerticalSpacing(8)
        
        # Voice ID
        form_layout.addWidget(QtWidgets.QLabel("Voice ID:"), 0, 0)
        self.ed_voice_id = QtWidgets.QLineEdit()
        self.ed_voice_id.setPlaceholderText("Nhập Voice ID (vd: GxxMAMfQkDlnqjpzjLHH)")
        form_layout.addWidget(self.ed_voice_id, 0, 1, 1, 2)
        
        # Custom Label
        form_layout.addWidget(QtWidgets.QLabel("Label:"), 1, 0)
        self.ed_label = QtWidgets.QLineEdit()
        self.ed_label.setPlaceholderText("Tên hiển thị tùy chỉnh (vd: Giọng nam trầm)")
        form_layout.addWidget(self.ed_label, 1, 1, 1, 2)
        
        # Model
        form_layout.addWidget(QtWidgets.QLabel("Model:"), 2, 0)
        self.cb_model = QtWidgets.QComboBox()
        self.cb_model.addItems([
            "eleven_multilingual_v2",
            "eleven_turbo_v2_5",
            "eleven_turbo_v2",
            "eleven_flash_v2_5",
            "eleven_flash_v2",
            "eleven_v3"
        ])
        form_layout.addWidget(self.cb_model, 2, 1)
        
        # Original name (read-only)
        form_layout.addWidget(QtWidgets.QLabel("Tên gốc:"), 2, 2)
        self.lbl_original_name = QtWidgets.QLabel("-")
        self.lbl_original_name.setStyleSheet("color: #666;")
        form_layout.addWidget(self.lbl_original_name, 2, 3)
        
        layout.addWidget(form_group)
        
        # === Buttons ===
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(8)
        
        self.btn_add = QtWidgets.QPushButton("🔍 Tìm kiếm")
        self.btn_add.setMinimumWidth(100)
        btn_layout.addWidget(self.btn_add)
        
        self.btn_update = QtWidgets.QPushButton("💾 Lưu")
        self.btn_update.setMinimumWidth(100)
        self.btn_update.setEnabled(False)
        btn_layout.addWidget(self.btn_update)
        
        self.btn_delete = QtWidgets.QPushButton("🗑️ Xóa")
        self.btn_delete.setMinimumWidth(80)
        self.btn_delete.setEnabled(False)
        btn_layout.addWidget(self.btn_delete)
        
        btn_layout.addStretch(1)
        
        self.btn_close = QtWidgets.QPushButton("Đóng")
        self.btn_close.setMinimumWidth(80)
        btn_layout.addWidget(self.btn_close)
        
        layout.addLayout(btn_layout)
        
        # === Connect signals ===
        self.btn_add.clicked.connect(self._search_voice)
        self.btn_update.clicked.connect(self._save_voice)
        self.btn_delete.clicked.connect(self._delete_voice)
        self.btn_close.clicked.connect(self.close)
        self.tbl_voices.itemSelectionChanged.connect(self._on_selection_changed)
        self.tbl_voices.cellDoubleClicked.connect(self._on_double_click_select)
    
    def _load_voices(self):
        """Load voices vào table"""
        self.tbl_voices.setRowCount(0)
        
        for voice in self._voice_list:
            row = self.tbl_voices.rowCount()
            self.tbl_voices.insertRow(row)
            
            voice_id = voice.get('id', '')
            label = voice.get('label', voice.get('name', ''))
            model = voice.get('model', 'eleven_multilingual_v2')
            original_name = voice.get('name', '')
            
            # Voice ID
            id_item = QtWidgets.QTableWidgetItem(voice_id)
            id_item.setToolTip(voice_id)
            self.tbl_voices.setItem(row, 0, id_item)
            
            # Label
            label_item = QtWidgets.QTableWidgetItem(label)
            if label != original_name:
                label_item.setForeground(QtGui.QColor("#0066cc"))  # Blue for custom label
            self.tbl_voices.setItem(row, 1, label_item)
            
            # Model
            model_display = model.replace("eleven_", "").replace("_", " ")
            model_item = QtWidgets.QTableWidgetItem(model_display)
            model_item.setData(Qt.UserRole, model)  # Store full model name
            self.tbl_voices.setItem(row, 2, model_item)
            
            # Original name
            name_item = QtWidgets.QTableWidgetItem(original_name)
            name_item.setForeground(QtGui.QColor("#888"))
            self.tbl_voices.setItem(row, 3, name_item)
            
            # Highlight current voice
            if voice_id == self._current_voice_id:
                for col in range(4):
                    item = self.tbl_voices.item(row, col)
                    if item:
                        item.setBackground(QtGui.QColor(200, 230, 255))
    
    def _on_selection_changed(self):
        """Handle table selection change"""
        selected = self.tbl_voices.selectedItems()
        has_selection = len(selected) > 0
        
        self.btn_update.setEnabled(has_selection)
        self.btn_delete.setEnabled(has_selection)
        
        # Clear pending new voice khi user chọn voice từ table
        if has_selection:
            self._pending_new_voice = None
            self.btn_update.setText("💾 Lưu")  # Reset button text
            
            row = self.tbl_voices.currentRow()
            voice_id = self.tbl_voices.item(row, 0).text()
            label = self.tbl_voices.item(row, 1).text()
            model = self.tbl_voices.item(row, 2).data(Qt.UserRole) or "eleven_multilingual_v2"
            original_name = self.tbl_voices.item(row, 3).text()
            
            self.ed_voice_id.setText(voice_id)
            self.ed_label.setText(label)
            self.cb_model.setCurrentText(model)
            self.lbl_original_name.setText(original_name or "-")
    
    def _on_double_click_select(self, row: int, col: int):
        """Handle double-click on table row - select voice and close dialog"""
        if row < 0:
            return
        
        voice_id = self.tbl_voices.item(row, 0).text()
        label = self.tbl_voices.item(row, 1).text()
        model = self.tbl_voices.item(row, 2).data(Qt.UserRole) or "eleven_multilingual_v2"
        
        print(f"🎤 [VoiceManager] Double-click selected: {label} ({voice_id[:8]}...) - {model}")
        
        # Emit signal to sync with main UI
        self.voice_selected.emit(voice_id, label, model)
        
        # Close dialog
        self.accept()
    
    def _search_voice(self):
        """Search voice by ID - verify and add to list
        
        Features:
        - Progress dialog loading
        - Retry với nhiều account/proxy khi lỗi
        - Hiển thị đúng lỗi (400, 401, etc.)
        """
        print("[DEBUG] _search_voice() called in JWTVoiceManagerDialog")
        
        voice_id = self.ed_voice_id.text().strip()
        label = self.ed_label.text().strip()
        model = self.cb_model.currentText()
        
        print(f"[DEBUG] voice_id={voice_id}, label={label}, model={model}")
        
        if not voice_id:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Voice ID")
            return
        
        # Check duplicate
        for voice in self._voice_list:
            if voice.get('id') == voice_id:
                QtWidgets.QMessageBox.warning(self, "Lỗi", f"Voice ID '{voice_id}' đã tồn tại trong danh sách")
                return
        
        # Check service
        if not self._service:
            print("[DEBUG] No service available!")
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Không có service để search voice.\nVui lòng load accounts trước.")
            return
        
        print(f"[DEBUG] Service available: {self._service}")
        print(f"[DEBUG] Account pool: {self._service.account_pool}")
        
        # Show progress dialog
        progress = QtWidgets.QProgressDialog("🔍 Đang tìm kiếm voice...", "Hủy", 0, 0, self)
        progress.setWindowTitle("Tìm kiếm Voice")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QtWidgets.QApplication.processEvents()
        
        # Disable button
        self.btn_add.setEnabled(False)
        
        max_retries = 3
        last_error = None
        original_name = None
        
        for attempt in range(max_retries):
            if progress.wasCanceled():
                break
            
            # Update progress message
            progress.setLabelText(f"🔍 Đang tìm kiếm voice... (lần {attempt + 1}/{max_retries})")
            QtWidgets.QApplication.processEvents()
            
            account = None
            try:
                account = self._service.account_pool.get_account()
                print(f"[DEBUG] Got account: {account.email if account else 'None'}")
                
                if not account:
                    last_error = "Không có account khả dụng"
                    continue
                
                # Get proxy
                proxies = self._proxy_fn() if self._proxy_fn else None
                print(f"[DEBUG] Proxies: {proxies}")
                
                try:
                    # Method 1: Try get_voice_info directly (fastest)
                    progress.setLabelText(f"🔍 Kiểm tra voice ID... (lần {attempt + 1})")
                    QtWidgets.QApplication.processEvents()
                    
                    print(f"[DEBUG] Calling JWTAPIClient.get_voice_info...")
                    voice_info = JWTAPIClient.get_voice_info(account.jwt_token, voice_id, proxies)
                    print(f"[DEBUG] voice_info result: {voice_info}")
                    
                    if voice_info and voice_info.get('name'):
                        original_name = voice_info.get('name')
                        print(f"[DEBUG] Found voice name: {original_name}")
                        self._service.account_pool.release_account(account.email, success=True)
                        break
                    
                    # Method 2: Search in shared voices
                    progress.setLabelText(f"🔍 Tìm trong shared voices... (lần {attempt + 1})")
                    QtWidgets.QApplication.processEvents()
                    
                    print(f"[DEBUG] Searching in shared voices...")
                    voices = JWTAPIClient.search_all_shared_voices(
                        account.jwt_token,
                        search=voice_id,
                        proxies=proxies
                    )
                    
                    for v in voices:
                        vid = v.get('voice_id') or v.get('public_owner_id')
                        if vid == voice_id:
                            original_name = v.get('name', 'Unknown')
                            break
                    
                    if original_name:
                        print(f"[DEBUG] Found in shared voices: {original_name}")
                        self._service.account_pool.release_account(account.email, success=True)
                        break
                    
                    # Method 3: Search in user voices
                    progress.setLabelText(f"🔍 Tìm trong user voices... (lần {attempt + 1})")
                    QtWidgets.QApplication.processEvents()
                    
                    print(f"[DEBUG] Searching in user voices...")
                    user_voices = JWTAPIClient.list_voices(account.jwt_token, proxies)
                    for v in user_voices:
                        if v.get('voice_id') == voice_id:
                            original_name = v.get('name', 'Unknown')
                            break
                    
                    if original_name:
                        print(f"[DEBUG] Found in user voices: {original_name}")
                        self._service.account_pool.release_account(account.email, success=True)
                        break
                    
                    # Not found after all methods - this is actual "not found"
                    self._service.account_pool.release_account(account.email, success=True)
                    last_error = "not_found"
                    print(f"[DEBUG] Voice not found after all methods")
                    break  # Don't retry if voice genuinely not found
                    
                except Exception as api_error:
                    error_str = str(api_error).lower()
                    print(f"[DEBUG] API error: {api_error}")
                    
                    # Check error type
                    if "401" in error_str or "unauthorized" in error_str:
                        last_error = f"Account lỗi 401 (unauthorized): {account.email}"
                        self._service.account_pool.release_account(account.email, success=False)
                    elif "400" in error_str:
                        last_error = f"Lỗi 400 (Bad Request): {api_error}"
                        self._service.account_pool.release_account(account.email, success=False)
                    elif "429" in error_str or "rate" in error_str:
                        last_error = f"Rate limit (429): {account.email}"
                        self._service.account_pool.release_account(account.email, success=False)
                    elif "timeout" in error_str or "connection" in error_str:
                        last_error = f"Connection error: {api_error}"
                        self._service.account_pool.release_account(account.email, success=False)
                    else:
                        last_error = f"API error: {api_error}"
                        self._service.account_pool.release_account(account.email, success=False)
                    
                    # Continue to retry with different account/proxy
                    continue
                    
            except Exception as e:
                last_error = str(e)
                print(f"[DEBUG] Exception: {e}")
                if account:
                    self._service.account_pool.release_account(account.email, success=False)
                continue
        
        # Lưu trạng thái canceled trước khi close
        was_canceled = progress.wasCanceled()
        
        # Close progress dialog
        progress.close()
        
        # Re-enable button
        self.btn_add.setEnabled(True)
        
        # Handle result
        if was_canceled:
            print("[DEBUG] Progress was canceled")
            return
        
        # 🔧 DEBUG
        print(f"[DEBUG] Search result: original_name={original_name}, last_error={last_error}")
        
        try:
            if original_name:
                # Found - hiển thị tên vào Label input để user có thể sửa trước khi lưu
                print(f"[DEBUG] Setting label to: {original_name}")
                
                # Block signals để tránh _on_selection_changed ghi đè
                self.tbl_voices.blockSignals(True)
                self.tbl_voices.clearSelection()
                self.tbl_voices.blockSignals(False)
                
                # Set form values AFTER blocking signals
                self.lbl_original_name.setText(original_name)
                self.ed_label.setText(original_name)  # Hiển thị tên gốc vào Label input
                
                print(f"[DEBUG] ed_label.text() after set = {self.ed_label.text()}")
                
                # Force UI update
                self.ed_label.repaint()
                QtWidgets.QApplication.processEvents()
                
                # Enable Lưu button để user có thể thêm voice
                self.btn_update.setEnabled(True)
                self.btn_update.setText("➕ Thêm")  # Đổi text vì đây là voice mới
                self.btn_delete.setEnabled(False)
                
                # Lưu trạng thái để biết đây là voice mới (chưa có trong list)
                self._pending_new_voice = {
                    'id': voice_id,
                    'name': original_name,
                    'model': model
                }
                
                QtWidgets.QMessageBox.information(
                    self, "Tìm thấy",
                    f"✅ Tìm thấy voice:\n{original_name}\n\nNhấn 'Thêm' để lưu vào danh sách."
                )
            elif last_error == "not_found":
                # Voice genuinely not found
                reply = QtWidgets.QMessageBox.question(
                    self, "Voice không tìm thấy",
                    f"Không tìm thấy voice với ID:\n{voice_id}\n\n"
                    "Voice có thể không tồn tại hoặc là voice private.\n"
                    "Bạn vẫn muốn thêm voice này?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
                )
                if reply == QtWidgets.QMessageBox.Yes:
                    new_voice = {
                        'id': voice_id,
                        'name': label or voice_id[:12],
                        'label': label or voice_id[:12],
                        'model': model
                    }
                    self._voice_list.insert(0, new_voice)
                    self._load_voices()
                    
                    # Clear form
                    self.ed_voice_id.clear()
                    self.ed_label.clear()
                    self.lbl_original_name.setText("-")
                    
                    # Select new row without triggering selection change
                    self.tbl_voices.blockSignals(True)
                    self.tbl_voices.selectRow(0)
                    self.tbl_voices.blockSignals(False)
                    
                    self.btn_update.setEnabled(False)
                    self.btn_delete.setEnabled(False)
            else:
                # Error occurred
                QtWidgets.QMessageBox.critical(
                    self, "Lỗi tìm kiếm",
                    f"❌ Không thể tìm kiếm voice sau {max_retries} lần thử.\n\n"
                    f"Lỗi cuối: {last_error}\n\n"
                    "Vui lòng kiểm tra:\n"
                    "- Kết nối mạng\n"
                    "- Proxy settings\n"
                    "- JWT accounts"
                )
        except Exception as e:
            print(f"[DEBUG] Exception in search result handling: {e}")
            import traceback
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(self, "Lỗi", f"Lỗi xử lý kết quả: {e}")
    
    def _save_voice(self):
        """Save voice - thêm mới hoặc cập nhật voice đã có"""
        voice_id = self.ed_voice_id.text().strip()
        label = self.ed_label.text().strip()
        model = self.cb_model.currentText()
        
        if not voice_id:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Voice ID")
            return
        
        # Case 1: Thêm voice mới (từ search)
        if hasattr(self, '_pending_new_voice') and self._pending_new_voice:
            if self._pending_new_voice.get('id') == voice_id:
                # Check duplicate
                for voice in self._voice_list:
                    if voice.get('id') == voice_id:
                        QtWidgets.QMessageBox.warning(self, "Lỗi", f"Voice ID '{voice_id}' đã tồn tại trong danh sách")
                        return
                
                # Add new voice
                new_voice = {
                    'id': voice_id,
                    'name': self._pending_new_voice.get('name', label),
                    'label': label or self._pending_new_voice.get('name', voice_id[:12]),
                    'model': model
                }
                self._voice_list.insert(0, new_voice)
                
                # Clear pending
                self._pending_new_voice = None
                
                # Reload table and clear form
                self._load_voices()
                self._clear_form()
                
                # Reset button text
                self.btn_update.setText("💾 Lưu")
                
                QtWidgets.QMessageBox.information(self, "Thành công", f"✅ Đã thêm voice: {label or new_voice['name']}")
                return
        
        # Case 2: Cập nhật voice đã có trong table
        row = self.tbl_voices.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng chọn voice cần lưu hoặc tìm kiếm voice mới")
            return
        
        # Find and update in list
        old_voice_id = self.tbl_voices.item(row, 0).text()
        updated = False
        for voice in self._voice_list:
            if voice.get('id') == old_voice_id:
                voice['id'] = voice_id
                voice['label'] = label or voice.get('name', voice_id[:8])
                voice['model'] = model
                updated = True
                break
        
        if updated:
            # Reload table
            self._load_voices()
            self.tbl_voices.selectRow(row)
            QtWidgets.QMessageBox.information(self, "Thành công", f"✅ Đã lưu voice: {label or voice_id[:12]}")
    
    def _delete_voice(self):
        """Delete selected voice"""
        row = self.tbl_voices.currentRow()
        if row < 0:
            return
        
        voice_id = self.tbl_voices.item(row, 0).text()
        label = self.tbl_voices.item(row, 1).text()
        
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận xóa",
            f"Bạn có chắc muốn xóa voice:\n{label}\n({voice_id})?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        
        if reply == QtWidgets.QMessageBox.Yes:
            # Remove from list
            self._voice_list = [v for v in self._voice_list if v.get('id') != voice_id]
            
            # Reload table
            self._load_voices()
            
            # Clear form
            self._clear_form()
    
    def _clear_form(self):
        """Clear all input fields"""
        self.ed_voice_id.clear()
        self.ed_label.clear()
        self.lbl_original_name.setText("-")
        self.cb_model.setCurrentIndex(0)
        self.btn_update.setEnabled(False)
        self.btn_update.setText("💾 Lưu")  # Reset button text
        self.btn_delete.setEnabled(False)
        self._pending_new_voice = None  # Clear pending voice
        # Deselect table
        self.tbl_voices.clearSelection()
    
    def get_voice_list(self) -> List[Dict]:
        """Get updated voice list"""
        return self._voice_list


class JWTTTSWorkerSignals(QObject):
    progress = Signal(int, int, str)  # current, total, message
    line_done = Signal(int, bool, str)  # line_idx, success, error
    finished = Signal(bool, str, str)  # success, message, output_dir
    account_changed = Signal(str)  # email


class JWTBatchTTSWorkerSignals(QObject):
    """Signals for JWTBatchTTSWorker"""
    progress = Signal(int, int, str)  # current, total, status
    file_progress = Signal(int, int, int)  # file_idx, current_in_file, total_in_file
    update_sub_table = Signal(str, int, str, str)  # file_path, row, column_name, value
    log = Signal(str)  # log message
    finished = Signal(int, int)  # success_count, fail_count


class JWTBatchTTSWorker(QRunnable):
    """Worker để chạy batch TTS trong background thread với đa luồng
    
    Xử lý giống tab "Chuyển văn bản":
    - Mỗi chunk là 1 task riêng
    - Output: tts_mini/_chunks/{para_idx}.{chunk_idx}.mp3
    - Sau khi tất cả chunks của 1 paragraph hoàn thành, merge thành doan_{para_idx}.mp3
    
    🔧 NEW: Batch mode - mỗi file có output folder riêng
    """
    
    def __init__(self, service, chunks, voice_id, model_id, voice_settings, 
                 max_workers: int = 5, output_dir: str = "", tts_mini_dir: str = "",
                 user_id: int = None, supabase = None, file_path: str = "",
                 file_output_dirs: Dict = None, preview_client=None):
        super().__init__()
        self.service = service
        self.preview_client = preview_client  # PreviewClient for preview mode
        # chunks: [(row, para_idx, chunk_idx, total_chunks, text, chunk_output_path, file_idx), ...]
        # 🔧 NEW: Thêm file_idx vào tuple
        self.chunks = chunks
        self.voice_id = voice_id
        self.model_id = model_id
        self.voice_settings = voice_settings
        self.output_dir = output_dir
        self.tts_mini_dir = tts_mini_dir
        self.user_id = user_id
        self.supabase = supabase
        self.file_path = file_path  # 🔧 FIX: Track which file this worker is processing
        self.file_output_dirs = file_output_dirs or {}  # 🔧 NEW: {file_idx: {'output_dir': ..., 'tts_mini_dir': ...}}
        self.signals = JWTBatchTTSWorkerSignals()
        self._stop = False
        self._lock = threading.Lock()
        self._completed = 0
        self._success = 0
        self._failed = 0
        self._total_chars_used = 0  # Track total chars for deduct
        
        # Track paragraph completion for merging - per file
        # 🔧 NEW: Key là (file_idx, para_idx) thay vì chỉ para_idx
        self._paragraph_chunks = {}  # {(file_idx, para_idx): {'total': N, 'completed': [], 'files': {}}}
        for chunk in chunks:
            # 🔧 NEW: Unpack với file_idx
            if len(chunk) >= 7:
                row, para_idx, chunk_idx, total_chunks, text, output_path, file_idx = chunk
            else:
                row, para_idx, chunk_idx, total_chunks, text, output_path = chunk
                file_idx = 0
            
            key = (file_idx, para_idx)
            if key not in self._paragraph_chunks:
                self._paragraph_chunks[key] = {
                    'total': total_chunks,
                    'completed': set(),
                    'files': {},
                    'file_idx': file_idx
                }
        
        # Limit max_workers based on number of accounts (skip in preview mode)
        if self.preview_client:
            self.max_workers = max_workers
        else:
            num_accounts = 1
            if hasattr(service, 'account_pool') and service.account_pool:
                stats = service.account_pool.get_stats()
                num_accounts = max(1, stats.get('total', 1))
            
            max_safe_workers = num_accounts * 2
            self.max_workers = min(max_workers, max_safe_workers)
    
    def stop(self):
        self._stop = True
    
    def _deduct_subscription_characters(self, chars_used: int):
        """Trừ số ký tự đã dùng từ subscription (count_characters) - dùng RPC"""
        if chars_used <= 0 or not self.user_id or not self.supabase:
            return
        
        try:
            # Tính billed_chars với margin 20%
            billed_chars = max(1, int(math.ceil(chars_used * 1.2)))
            
            # Dùng RPC để deduct (atomic operation)
            result = self.supabase.rpc('deduct_subscription_characters', {
                'p_user_id': self.user_id,
                'p_chars_used': billed_chars
            })
            
            if result.data:
                new_chars = result.data.get('count_characters', 0)
                self.signals.log.emit(f"Deducted {billed_chars:,} chars -> {new_chars:,} remaining")
            
        except Exception as e:
            self.signals.log.emit(f"Deduct error: {e}")
    
    def _pre_warm_accounts(self):
        """
        🔧 OPTIMIZED: Pre-warm và pre-cleanup accounts
        Skip entirely in preview mode.
        """
        if self.preview_client:
            self.signals.log.emit("✅ [Preview Mode] Skip pre-warm (dùng TokenPool)")
            return
        
        if not hasattr(self.service, 'account_pool') or not self.service.account_pool:
            return
        
        # 🔧 Chỉ pre-warm số accounts cần thiết
        accounts_needed = min(self.max_workers * 2, 20)  # Max 20 accounts
        
        self.signals.log.emit(f"⚡ Quick pre-warm: {accounts_needed} accounts (lazy refresh for others)")
        
        # 🔧 v2: Pre-cleanup voices CHỈ KHI voice không phải premade
        # Premade voices không cần add vào library nên không cần cleanup
        if not self._is_premade_voice(self.voice_id):
            self._pre_cleanup_voices(accounts_needed)
        else:
            self.signals.log.emit(f"✅ Using premade voice - skip pre-cleanup")
        
        self.signals.log.emit(f"✅ Ready to start (JWT refresh on-demand)")
    
    def _is_premade_voice(self, voice_id: str) -> bool:
        """Check if voice_id is a premade voice (không cần add vào library)"""
        # Danh sách premade voice IDs
        PREMADE_VOICE_IDS = {
            "CwhRBWXzGAHq8TQ4Fs17",  # Roger
            "EXAVITQu4vr4xnSDxMaL",  # Sarah
            "FGY2WhTYpPnrIDTdsKH5",  # Laura
            "IKne3meq5aSn9XLyUdCD",  # Charlie
            "JBFqnCBsd6RMkjVDRZzb",  # George
            "N2lVS1w4EtoT3dr4eOWO",  # Callum
            "SAz9YHcvj6GT2YYXdXww",  # River
            "SOYHLrjzK2X1ezoPC6cr",  # Harry
            "TX3LPaxmHKxFdv7VOQHJ",  # Liam
            "Xb7hH8MSUJpSbSDYk0k2",  # Alice
            "XrExE9yKIg1WjnnlVkGX",  # Matilda
            "bIHbv24MWmeRgasZH58o",  # Will
            "cgSgspJ2msm6clMCkdW9",  # Jessica
            "cjVigY5qzO86Huf0OWal",  # Eric
            "hpp4J3VqNfWAUOO0d1Us",  # Bella
            "iP95p4xoKVk53GoZ742B",  # Chris
            "nPczCjzI2devNBz1zQrb",  # Brian
            "onwK4e9ZLuTAKqWW03F9",  # Daniel
            "pFZP5JQG7iQjIQuC4Bku",  # Lily
            "pNInz6obpgDQGcFmaJgB",  # Adam
            "pqHfZKP75CvOlQylNhV4",  # Bill
        }
        return voice_id in PREMADE_VOICE_IDS
    
    def _pre_cleanup_voices(self, num_accounts: int):
        """
        🔧 v2 NEW: Pre-cleanup custom voices cho các accounts sẽ dùng.
        Xóa tất cả custom voices (non-premade) để có slot trống cho voice mới.
        
        Args:
            num_accounts: Số accounts cần cleanup
        """
        if not hasattr(self.service, 'account_pool') or not self.service.account_pool:
            return
        
        try:
            from services.jwt_account_pool import JWTAPIClient
            
            # Get proxy
            proxies = None
            if hasattr(self.service, '_get_proxy'):
                proxies = self.service._get_proxy()
            
            # Cleanup thông qua account_pool method (đã có sẵn)
            pool = self.service.account_pool
            account_list = pool.get_account_list()
            if not account_list:
                return
            
            # Cleanup top N accounts (những account sẽ được pick đầu tiên)
            cleanup_count = min(num_accounts, len(account_list))
            total_deleted = 0
            
            self.signals.log.emit(f"🧹 Pre-cleanup voices for {cleanup_count} accounts...")
            
            skipped_exhausted = 0
            for i, acc_info in enumerate(account_list[:cleanup_count]):
                if self._stop:
                    break
                
                email = acc_info.get('email', '')
                
                # Sử dụng method cleanup_account_voices của pool (đã handle JWT refresh)
                try:
                    deleted = pool.cleanup_account_voices(email, proxies)
                    if deleted == -1:
                        # Account đã hết 54 edits/month - skip
                        skipped_exhausted += 1
                        self.signals.log.emit(f"   ⏭️ {email[:20]}...: edits exhausted (54/month)")
                    elif deleted > 0:
                        total_deleted += deleted
                        self.signals.log.emit(f"   🧹 {email[:20]}...: deleted {deleted} voices")
                    else:
                        self.signals.log.emit(f"   ✅ {email[:20]}...: clean")
                except Exception as e:
                    # Log error để debug
                    self.signals.log.emit(f"   ⚠️ {email[:20]}...: error - {str(e)[:40]}")
            
            if total_deleted > 0:
                msg = f"🧹 Pre-cleanup done: {total_deleted} voices deleted"
                if skipped_exhausted > 0:
                    msg += f" ({skipped_exhausted} accounts skipped - edits exhausted)"
                self.signals.log.emit(msg)
            elif skipped_exhausted > 0:
                self.signals.log.emit(f"🧹 Pre-cleanup: {skipped_exhausted} accounts skipped (edits exhausted)")
            else:
                self.signals.log.emit(f"🧹 Pre-cleanup: all accounts clean")
                
        except Exception as e:
            self.signals.log.emit(f"⚠️ Pre-cleanup error: {e}")
    
    def _get_mp3_duration(self, filepath: str) -> str:
        """Get MP3 duration as string (e.g. "12.34s")"""
        if not filepath or not os.path.exists(filepath):
            return ""
        
        try:
            import subprocess
            import shutil
            
            ffprobe_path = None
            if sys.platform == 'darwin':
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for path in [os.path.join(app_dir, 'ffprobe'), '/usr/local/bin/ffprobe', '/opt/homebrew/bin/ffprobe']:
                    if os.path.exists(path):
                        ffprobe_path = path
                        break
            else:
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundled = os.path.join(app_dir, 'ffprobe.exe')
                if os.path.exists(bundled):
                    ffprobe_path = bundled
                else:
                    ffprobe_path = shutil.which('ffprobe')
            
            if not ffprobe_path:
                return ""
            
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=5, startupinfo=si
            )
            
            if result.returncode == 0 and result.stdout.strip():
                duration = float(result.stdout.strip())
                return f"{duration:.2f}s"
        except Exception:
            pass
        
        return ""
    
    def _process_single_chunk(self, chunk_data) -> Tuple[int, int, int, bool, str, int]:
        """Process 1 chunk TTS
        
        Returns: (row, para_idx, chunk_idx, success, output_path, file_idx)
        """
        # 🔧 NEW: Unpack với file_idx
        if len(chunk_data) >= 7:
            row, para_idx, chunk_idx, total_chunks, text, output_path, file_idx = chunk_data
        else:
            row, para_idx, chunk_idx, total_chunks, text, output_path = chunk_data
            file_idx = 0
        
        text = text.strip()
        
        if self._stop:
            return (row, para_idx, chunk_idx, False, "", file_idx)
        
        if not text:
            return (row, para_idx, chunk_idx, True, output_path, file_idx)
        
        # Update status
        self.signals.update_sub_table.emit(self.file_path, row, "Status", "Processing...")
        
        # ========== PREVIEW MODE ==========
        if self.preview_client:
            max_retries = 999  # Retry cho đến khi thành công hoặc user stop
            consecutive_errors = 0
            max_consecutive_fatal = 10  # Dừng nếu 10 lỗi liên tiếp không phải network
            
            for attempt in range(1, max_retries + 1):
                if self._stop:
                    return (row, para_idx, chunk_idx, False, "", file_idx)
                try:
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Preview ({attempt})...")
                    self.preview_client.tts_direct(
                        voice_id=self.voice_id,
                        text=text,
                        model_id=self.model_id,
                        settings=self.voice_settings,
                        outpath=output_path,
                    )
                    # Verify file
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                        output_name = os.path.basename(output_path)
                        file_size = os.path.getsize(output_path) / 1024
                        self.signals.update_sub_table.emit(self.file_path, row, "Output", f"{output_name} ({file_size:.1f}KB)")
                        timing = self._get_mp3_duration(output_path)
                        if timing:
                            self.signals.update_sub_table.emit(self.file_path, row, "Timing", timing)
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", "Done")
                        return (row, para_idx, chunk_idx, True, output_path, file_idx)
                    else:
                        if os.path.exists(output_path):
                            os.remove(output_path)
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} file invalid, retry {attempt}")
                        time.sleep(2)
                        
                except Exception as e:
                    err_msg = str(e).lower()
                    
                    # 401 = IP rate limited / token hết hạn → lấy token mới (pool tự xử lý)
                    if "401" in err_msg or "unauthorized" in err_msg:
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} 401 - token/IP expired, retry...")
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Token expired ({attempt})...")
                        time.sleep(3)
                        consecutive_errors = 0  # Network issue, not fatal
                        continue
                    
                    # 429 = rate limit → đợi lâu hơn
                    if "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                        wait = min(10 + attempt * 2, 60)
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} 429 rate limit, wait {wait}s...")
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Rate limit ({wait}s)...")
                        time.sleep(wait)
                        consecutive_errors = 0
                        continue
                    
                    # Connection/proxy errors → retry nhanh (pool sẽ lấy proxy mới)
                    if any(x in err_msg for x in ["connection", "reset", "disconnect", "proxy", "remotedisconnected", "timeout", "network"]):
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} connection error, retry...")
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Reconnect ({attempt})...")
                        time.sleep(2)
                        consecutive_errors = 0
                        continue
                    
                    # Token pool trống → đợi solver
                    if "token pool" in err_msg or "timeout" in err_msg:
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} waiting for token pool...")
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Wait token ({attempt})...")
                        time.sleep(5)
                        consecutive_errors = 0
                        continue
                    
                    # 400 bad request = text có vấn đề → fatal
                    if "400" in err_msg and "bad request" in err_msg:
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} 400 Bad Request - text invalid")
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", "Error: Bad text")
                        return (row, para_idx, chunk_idx, False, "", file_idx)
                    
                    # Lỗi khác → đếm consecutive, dừng nếu quá nhiều
                    consecutive_errors += 1
                    self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} error ({consecutive_errors}/{max_consecutive_fatal}): {str(e)[:100]}")
                    if consecutive_errors >= max_consecutive_fatal:
                        self.signals.log.emit(f"[Preview] Chunk {para_idx}.{chunk_idx} ❌ Quá nhiều lỗi liên tiếp, dừng")
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", "Error")
                        return (row, para_idx, chunk_idx, False, "", file_idx)
                    time.sleep(3)
            
            self.signals.update_sub_table.emit(self.file_path, row, "Status", "Error")
            return (row, para_idx, chunk_idx, False, "", file_idx)
        
        # ========== JWT MODE (original) ==========
        attempt = 0
        no_account_count = 0
        max_no_account_retries = 15
        
        while not self._stop:
            attempt += 1
            
            try:
                result = self.service.generate_tts(
                    text=text,
                    voice_id=self.voice_id,
                    output_path=output_path,
                    model_id=self.model_id,
                    **self.voice_settings
                )
                
                if result.success:
                    # Update Output column với tên file chunk
                    output_name = os.path.basename(output_path)
                    try:
                        if os.path.exists(output_path):
                            file_size = os.path.getsize(output_path) / 1024
                            output_display = f"{output_name} ({file_size:.1f}KB)"
                        else:
                            output_display = output_name
                    except:
                        output_display = output_name
                    
                    self.signals.update_sub_table.emit(self.file_path, row, "Output", output_display)
                    
                    # Update Timing
                    timing = self._get_mp3_duration(output_path)
                    if timing:
                        self.signals.update_sub_table.emit(self.file_path, row, "Timing", timing)
                    
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", "Done")
                    return (row, para_idx, chunk_idx, True, output_path, file_idx)
                
                # Handle error
                error = result.error or "Unknown"
                error_lower = error.lower()
                
                if 'no available account' in error_lower or 'no usable account' in error_lower:
                    no_account_count += 1
                    if no_account_count >= max_no_account_retries:
                        self.signals.update_sub_table.emit(self.file_path, row, "Status", "Error: No account")
                        return (row, para_idx, chunk_idx, False, "", file_idx)
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Wait ({no_account_count})...")
                    time.sleep(3)
                    continue
                
                no_account_count = 0
                
                if any(x in error_lower for x in ['401', 'unusual activity', 'unauthorized']):
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Switch ({attempt})...")
                    time.sleep(1)
                    continue
                
                if any(x in error_lower for x in ['quota', 'insufficient', '402']):
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Switch ({attempt})...")
                    time.sleep(1)
                    continue
                
                if any(x in error_lower for x in ['network', 'timeout', 'proxy', 'connection', 'remote', 'reset']):
                    wait_time = min(2 * attempt, 10)
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Retry ({attempt})...")
                    time.sleep(wait_time)
                    continue
                
                if any(x in error_lower for x in ['429', 'rate_limit', 'too_many', 'concurrent']):
                    wait_time = 3 if 'concurrent' in error_lower else min(5 * attempt, 30)
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Wait ({wait_time}s)...")
                    time.sleep(wait_time)
                    continue
                
                if 'voice_not_found' in error_lower:
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", "Error: Voice not found")
                    return (row, para_idx, chunk_idx, False, "", file_idx)
                
                self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Retry ({attempt})...")
                time.sleep(2)
                
            except Exception as e:
                self.signals.update_sub_table.emit(self.file_path, row, "Status", f"Retry ({attempt})...")
                time.sleep(2)
        
        self.signals.update_sub_table.emit(self.file_path, row, "Status", "Stopped")
        return (row, para_idx, chunk_idx, False, "", file_idx)
    
    def _merge_paragraph_chunks(self, key: Tuple[int, int]):
        """Merge all chunks of a paragraph into doan_{para_idx}.mp3
        
        🔧 NEW: key là (file_idx, para_idx) để hỗ trợ batch mode
        """
        para_info = self._paragraph_chunks.get(key)
        if not para_info:
            return
        
        file_idx, para_idx = key
        total = para_info['total']
        files = para_info['files']
        
        # Check if all chunks completed
        if len(files) != total:
            return
        
        # Sort chunk files by chunk_idx
        sorted_files = [files[i] for i in sorted(files.keys())]
        
        # 🔧 NEW: Get output dir for this file from file_output_dirs
        if self.file_output_dirs and file_idx in self.file_output_dirs:
            parent_dir = self.file_output_dirs[file_idx]['output_dir']
        else:
            # Fallback to default
            parent_dir = os.path.dirname(self.tts_mini_dir)
        
        output_path = os.path.join(parent_dir, f"doan_{para_idx}.mp3")
        
        if total == 1:
            # Only 1 chunk - just copy
            import shutil
            try:
                shutil.copy2(sorted_files[0], output_path)
                self.signals.log.emit(f"Paragraph {para_idx}: copied to {os.path.basename(output_path)}")
            except Exception as e:
                self.signals.log.emit(f"Paragraph {para_idx}: copy failed - {e}")
        else:
            # Multiple chunks - merge with ffmpeg
            if self._merge_chunks_ffmpeg(sorted_files, output_path):
                self.signals.log.emit(f"Paragraph {para_idx}: merged {total} chunks → {os.path.basename(output_path)}")
            else:
                self.signals.log.emit(f"Paragraph {para_idx}: merge failed")
    
    def _merge_chunks_ffmpeg(self, chunk_files: List[str], output_path: str) -> bool:
        """Merge chunks using ffmpeg concat"""
        if not chunk_files:
            return False
        
        try:
            import subprocess
            import shutil
            
            ffmpeg_path = None
            if sys.platform == 'darwin':
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for path in [os.path.join(app_dir, 'ffmpeg'), '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg']:
                    if os.path.exists(path):
                        ffmpeg_path = path
                        break
            else:
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundled = os.path.join(app_dir, 'ffmpeg.exe')
                if os.path.exists(bundled):
                    ffmpeg_path = bundled
                else:
                    ffmpeg_path = shutil.which('ffmpeg')
            
            if not ffmpeg_path:
                return False
            
            list_file = output_path + ".txt"
            with open(list_file, 'w', encoding='utf-8') as f:
                for chunk_file in chunk_files:
                    safe_path = chunk_file.replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
            
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', output_path],
                capture_output=True, text=True, timeout=60, startupinfo=si
            )
            
            try:
                os.remove(list_file)
            except:
                pass
            
            return result.returncode == 0 and os.path.exists(output_path)
            
        except Exception as e:
            self.signals.log.emit(f"FFmpeg merge error: {e}")
            return False
    
    def run(self):
        """Run batch TTS với ThreadPoolExecutor
        
        🔧 NEW: Hỗ trợ batch mode với nhiều files, mỗi file có output folder riêng
        🔧 v2: Xử lý tuần tự theo file - hoàn thành file 1 → merge → file 2 → merge
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        total = len(self.chunks)
        self.signals.log.emit(f"Starting JWT TTS: {total} chunks, {self.max_workers} threads")
        
        # Pre-warm accounts
        self._pre_warm_accounts()
        
        # 🔧 v2: Group chunks by file_idx
        chunks_by_file = {}
        for chunk in self.chunks:
            if len(chunk) >= 7:
                row, para_idx, chunk_idx, total_chunks, text, output_path, file_idx = chunk
            else:
                row, para_idx, chunk_idx, total_chunks, text, output_path = chunk
                file_idx = 0
            
            if file_idx not in chunks_by_file:
                chunks_by_file[file_idx] = []
            chunks_by_file[file_idx].append(chunk)
        
        # 🔧 v2: Process files sequentially
        sorted_file_indices = sorted(chunks_by_file.keys())
        
        for file_idx in sorted_file_indices:
            if self._stop:
                break
            
            file_chunks = chunks_by_file[file_idx]
            file_name = self.file_output_dirs.get(file_idx, {}).get('base_name', f'file_{file_idx + 1}')
            self.signals.log.emit(f"📁 Processing file {file_idx + 1}: {file_name} ({len(file_chunks)} chunks)")
            
            # Filter empty chunks for this file
            non_empty_chunks = []
            for chunk in file_chunks:
                if len(chunk) >= 7:
                    row, para_idx, chunk_idx, total_chunks, text, output_path, f_idx = chunk
                else:
                    row, para_idx, chunk_idx, total_chunks, text, output_path = chunk
                    f_idx = 0
                
                if not text.strip():
                    with self._lock:
                        self._completed += 1
                        self._success += 1
                    self.signals.update_sub_table.emit(self.file_path, row, "Status", "Empty")
                    self.signals.progress.emit(self._completed, total, f"{self._completed}/{total}")
                else:
                    non_empty_chunks.append(chunk)
            
            if not non_empty_chunks:
                # File này không có chunk nào cần xử lý, emit 100% và merge luôn
                self.signals.file_progress.emit(file_idx, len(file_chunks), len(file_chunks))
                self._merge_file_paragraphs(file_idx)
                continue
            
            # 🔧 v2: Track progress per file
            file_completed = 0
            file_total = len(file_chunks)
            
            # Process chunks of this file with thread pool
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_chunk = {}
                for i, chunk in enumerate(non_empty_chunks):
                    if self._stop:
                        break
                    future = executor.submit(self._process_single_chunk, chunk)
                    future_to_chunk[future] = chunk
                    if i < self.max_workers:
                        time.sleep(0.3)
                
                for future in as_completed(future_to_chunk):
                    if self._stop:
                        for f in future_to_chunk:
                            f.cancel()
                        break
                    
                    try:
                        result = future.result()
                        if len(result) >= 6:
                            row, para_idx, chunk_idx, success, output_path, f_idx = result
                        else:
                            row, para_idx, chunk_idx, success, output_path = result
                            f_idx = 0
                        
                        # Get text length from original chunk data
                        original_chunk = future_to_chunk[future]
                        chunk_text = original_chunk[4]  # text is at index 4
                        
                        with self._lock:
                            self._completed += 1
                            file_completed += 1
                            
                            if success:
                                self._success += 1
                                # Deduct ngay sau mỗi chunk thành công
                                if chunk_text.strip():
                                    chars_used = len(chunk_text.strip())
                                    self._total_chars_used += chars_used
                                    # Deduct immediately to D1
                                    self._deduct_subscription_characters(chars_used)
                                
                                # Track chunk completion với key (file_idx, para_idx)
                                key = (f_idx, para_idx)
                                if key in self._paragraph_chunks:
                                    self._paragraph_chunks[key]['completed'].add(chunk_idx)
                                    if output_path:
                                        self._paragraph_chunks[key]['files'][chunk_idx] = output_path
                                    
                                    # Check if all chunks of this paragraph are done
                                    para_info = self._paragraph_chunks[key]
                                    if len(para_info['completed']) == para_info['total']:
                                        self._merge_paragraph_chunks(key)
                            else:
                                self._failed += 1
                            
                            self.signals.progress.emit(self._completed, total, f"{self._completed}/{total}")
                            # 🔧 v2: Emit file progress
                            self.signals.file_progress.emit(file_idx, file_completed, file_total)
                            
                    except Exception as e:
                        with self._lock:
                            self._completed += 1
                            file_completed += 1
                            self._failed += 1
                            self.signals.progress.emit(self._completed, total, f"{self._completed}/{total}")
                            self.signals.file_progress.emit(file_idx, file_completed, file_total)
                        self.signals.log.emit(f"Thread error: {e}")
            
            # 🔧 v2: Emit 100% cho file này và merge
            self.signals.file_progress.emit(file_idx, file_total, file_total)
            
            # 🔧 v2: Merge file này ngay sau khi hoàn thành tất cả chunks
            if not self._stop:
                self._merge_file_paragraphs(file_idx)
        
        self.signals.log.emit(f"Finished: {self._success}/{total} success, {self._failed} failed")
        self.signals.log.emit(f"Total chars used: {self._total_chars_used:,}")
        
        self.signals.finished.emit(self._success, self._failed)
    
    def _merge_file_paragraphs(self, file_idx: int):
        """🔧 v2 NEW: Merge tất cả doan_X.mp3 của 1 file thành file tổng hợp"""
        if not self.file_output_dirs or file_idx not in self.file_output_dirs:
            # Fallback to single file mode
            if file_idx == 0:
                self._merge_all_paragraphs()
            return
        
        try:
            from pathlib import Path
            import subprocess
            import shutil
            
            file_info = self.file_output_dirs[file_idx]
            output_dir = file_info['output_dir']
            base_name = file_info.get('base_name', f'file_{file_idx}')
            
            if not os.path.exists(output_dir):
                return
            
            # Tìm ffmpeg
            ffmpeg_path = None
            if sys.platform == 'darwin':
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for path in [os.path.join(app_dir, 'ffmpeg'), '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg']:
                    if os.path.exists(path):
                        ffmpeg_path = path
                        break
            else:
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundled = os.path.join(app_dir, 'ffmpeg.exe')
                if os.path.exists(bundled):
                    ffmpeg_path = bundled
                else:
                    ffmpeg_path = shutil.which('ffmpeg')
            
            if not ffmpeg_path:
                self.signals.log.emit(f"📁 File {file_idx + 1}: FFmpeg not found - skip merge")
                return
            
            # Tìm tất cả file doan_*.mp3 trong output_dir
            mp3_files = []
            for f in Path(output_dir).glob("doan_*.mp3"):
                try:
                    para_num = int(f.stem.split('_')[1])
                    mp3_files.append((para_num, str(f)))
                except (ValueError, IndexError):
                    continue
            
            if not mp3_files:
                self.signals.log.emit(f"📁 File {file_idx + 1}: No doan_*.mp3 files found")
                return
            
            # Sắp xếp theo số thứ tự
            mp3_files.sort(key=lambda x: x[0])
            mp3_files = [f for _, f in mp3_files]
            
            output_filename = f"{base_name}.mp3"
            output_path = os.path.join(output_dir, output_filename)
            
            self.signals.log.emit(f"📁 File {file_idx + 1}: Merging {len(mp3_files)} paragraphs → {output_filename}")
            
            # Tạo file list cho ffmpeg concat
            list_file = output_path + ".txt"
            with open(list_file, 'w', encoding='utf-8') as f:
                for mp3_file in mp3_files:
                    safe_path = mp3_file.replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
            
            # Chạy ffmpeg concat
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', output_path],
                capture_output=True, text=True, timeout=120, startupinfo=si
            )
            
            # Cleanup list file
            try:
                os.remove(list_file)
            except:
                pass
            
            if result.returncode == 0 and os.path.exists(output_path):
                file_size = os.path.getsize(output_path) / 1024 / 1024  # MB
                self.signals.log.emit(f"📁 File {file_idx + 1}: ✅ Created {output_filename} ({file_size:.2f}MB)")
                
                # Move file ra folder cha (cùng cấp với file TXT gốc)
                txt_dir = os.path.dirname(output_dir)
                final_mp3_path = os.path.join(txt_dir, output_filename)
                
                try:
                    if os.path.exists(final_mp3_path):
                        os.remove(final_mp3_path)
                    shutil.move(output_path, final_mp3_path)
                    self.signals.log.emit(f"📁 File {file_idx + 1}: Moved to {txt_dir}")
                except Exception as e:
                    self.signals.log.emit(f"📁 File {file_idx + 1}: Move failed - {e}")
            else:
                self.signals.log.emit(f"📁 File {file_idx + 1}: ❌ Merge failed")
                
        except Exception as e:
            self.signals.log.emit(f"📁 File {file_idx + 1}: Merge error - {e}")
    
    def _merge_all_paragraphs(self):
        """Merge tất cả doan_X.mp3 thành file tổng hợp {filename}.mp3"""
        if not self.tts_mini_dir or not os.path.exists(self.tts_mini_dir):
            return
        
        try:
            from pathlib import Path
            import subprocess
            import shutil
            
            # Tìm ffmpeg
            ffmpeg_path = None
            if sys.platform == 'darwin':
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for path in [os.path.join(app_dir, 'ffmpeg'), '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg']:
                    if os.path.exists(path):
                        ffmpeg_path = path
                        break
            else:
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundled = os.path.join(app_dir, 'ffmpeg.exe')
                if os.path.exists(bundled):
                    ffmpeg_path = bundled
                else:
                    ffmpeg_path = shutil.which('ffmpeg')
            
            if not ffmpeg_path:
                self.signals.log.emit("FFmpeg not found - skip final merge")
                return
            
            # Tìm tất cả file doan_*.mp3 cùng cấp với tts_mini (parent folder)
            parent_dir = os.path.dirname(self.tts_mini_dir)
            mp3_files = []
            for f in Path(parent_dir).glob("doan_*.mp3"):
                try:
                    para_num = int(f.stem.split('_')[1])
                    mp3_files.append((para_num, str(f)))
                except (ValueError, IndexError):
                    continue
            
            if not mp3_files:
                self.signals.log.emit("No doan_*.mp3 files found for final merge")
                return
            
            # Sắp xếp theo số thứ tự
            mp3_files.sort(key=lambda x: x[0])
            mp3_files = [f for _, f in mp3_files]
            
            # Output path: cùng folder với doan_*.mp3
            folder_name = os.path.basename(parent_dir)  # {filename}_jwt_tts
            # Bỏ suffix _jwt_tts để lấy tên file gốc
            if folder_name.endswith('_jwt_tts'):
                base_name = folder_name[:-8]
            else:
                base_name = folder_name
            
            output_filename = f"{base_name}.mp3"
            output_path = os.path.join(parent_dir, output_filename)
            
            self.signals.log.emit(f"Merging {len(mp3_files)} paragraphs → {output_filename}")
            
            # Tạo file list cho ffmpeg concat
            list_file = output_path + ".txt"
            with open(list_file, 'w', encoding='utf-8') as f:
                for mp3_file in mp3_files:
                    safe_path = mp3_file.replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
            
            # Chạy ffmpeg concat
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', output_path],
                capture_output=True, text=True, timeout=120, startupinfo=si
            )
            
            # Cleanup list file
            try:
                os.remove(list_file)
            except:
                pass
            
            if result.returncode == 0 and os.path.exists(output_path):
                file_size = os.path.getsize(output_path) / 1024 / 1024  # MB
                self.signals.log.emit(f"Created: {output_filename} ({file_size:.2f}MB)")
                
                # Move file ra folder cha (cùng cấp với file TXT gốc)
                # parent_dir = {filename}_jwt_tts/ → txt_dir = folder chứa file TXT
                txt_dir = os.path.dirname(parent_dir)
                final_mp3_path = os.path.join(txt_dir, output_filename)
                
                try:
                    if os.path.exists(final_mp3_path):
                        os.remove(final_mp3_path)
                    shutil.move(output_path, final_mp3_path)
                    self.signals.log.emit(f"Moved to: {final_mp3_path}")
                except Exception as e:
                    self.signals.log.emit(f"Move failed: {e}, kept at {output_path}")
                
                # 🔧 KEEP _chunks folder for resume functionality
                # chunks_dir = os.path.join(self.tts_mini_dir, "_chunks")
                # if os.path.exists(chunks_dir):
                #     try:
                #         shutil.rmtree(chunks_dir, ignore_errors=True)
                #         self.signals.log.emit("Cleaned up _chunks folder")
                #     except:
                #         pass
            else:
                self.signals.log.emit(f"Final merge failed: {result.stderr[:200] if result.stderr else 'Unknown error'}")
                
        except Exception as e:
            self.signals.log.emit(f"Final merge error: {e}")
    
    def _merge_all_paragraphs_batch(self):
        """🔧 NEW: Merge tất cả doan_X.mp3 thành file tổng hợp cho MỖI file trong batch mode"""
        if not self.file_output_dirs:
            # Fallback to single file mode
            self._merge_all_paragraphs()
            return
        
        try:
            from pathlib import Path
            import subprocess
            import shutil
            
            # Tìm ffmpeg
            ffmpeg_path = None
            if sys.platform == 'darwin':
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for path in [os.path.join(app_dir, 'ffmpeg'), '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg']:
                    if os.path.exists(path):
                        ffmpeg_path = path
                        break
            else:
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundled = os.path.join(app_dir, 'ffmpeg.exe')
                if os.path.exists(bundled):
                    ffmpeg_path = bundled
                else:
                    ffmpeg_path = shutil.which('ffmpeg')
            
            if not ffmpeg_path:
                self.signals.log.emit("FFmpeg not found - skip final merge")
                return
            
            # Merge cho từng file
            for file_idx, file_info in self.file_output_dirs.items():
                output_dir = file_info['output_dir']
                base_name = file_info.get('base_name', f'file_{file_idx}')
                
                if not os.path.exists(output_dir):
                    continue
                
                # Tìm tất cả file doan_*.mp3 trong output_dir
                mp3_files = []
                for f in Path(output_dir).glob("doan_*.mp3"):
                    try:
                        para_num = int(f.stem.split('_')[1])
                        mp3_files.append((para_num, str(f)))
                    except (ValueError, IndexError):
                        continue
                
                if not mp3_files:
                    self.signals.log.emit(f"📁 File {file_idx + 1}: No doan_*.mp3 files found")
                    continue
                
                # Sắp xếp theo số thứ tự
                mp3_files.sort(key=lambda x: x[0])
                mp3_files = [f for _, f in mp3_files]
                
                output_filename = f"{base_name}.mp3"
                output_path = os.path.join(output_dir, output_filename)
                
                self.signals.log.emit(f"📁 File {file_idx + 1}: Merging {len(mp3_files)} paragraphs → {output_filename}")
                
                # Tạo file list cho ffmpeg concat
                list_file = output_path + ".txt"
                with open(list_file, 'w', encoding='utf-8') as f:
                    for mp3_file in mp3_files:
                        safe_path = mp3_file.replace("'", "'\\''")
                        f.write(f"file '{safe_path}'\n")
                
                # Chạy ffmpeg concat
                si = None
                if os.name == 'nt':
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = subprocess.SW_HIDE
                
                result = subprocess.run(
                    [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', output_path],
                    capture_output=True, text=True, timeout=120, startupinfo=si
                )
                
                # Cleanup list file
                try:
                    os.remove(list_file)
                except:
                    pass
                
                if result.returncode == 0 and os.path.exists(output_path):
                    file_size = os.path.getsize(output_path) / 1024 / 1024  # MB
                    self.signals.log.emit(f"✅ File {file_idx + 1}: Created {output_filename} ({file_size:.2f}MB)")
                    
                    # Move file ra folder cha (cùng cấp với file TXT gốc)
                    txt_dir = os.path.dirname(output_dir)
                    final_mp3_path = os.path.join(txt_dir, output_filename)
                    
                    try:
                        if os.path.exists(final_mp3_path):
                            os.remove(final_mp3_path)
                        shutil.move(output_path, final_mp3_path)
                        self.signals.log.emit(f"📁 File {file_idx + 1}: Moved to {final_mp3_path}")
                    except Exception as e:
                        self.signals.log.emit(f"⚠️ File {file_idx + 1}: Move failed - {e}")
                else:
                    self.signals.log.emit(f"❌ File {file_idx + 1}: Merge failed")
            
            self.signals.log.emit(f"🎉 Batch merge complete: {len(self.file_output_dirs)} files")
                
        except Exception as e:
            self.signals.log.emit(f"❌ Batch merge error: {e}")


class JWTTTSWorker(QRunnable):
    """Worker để chạy TTS trong background thread"""
    
    def __init__(self, service, lines, voice_id, model_id, voice_settings):
        super().__init__()
        self.service = service
        self.lines = lines  # [{"idx": 1, "text": "...", "output_path": "..."}]
        self.voice_id = voice_id
        self.model_id = model_id
        self.voice_settings = voice_settings
        self.signals = JWTTTSWorkerSignals()
        self._stop = False
        self._lock = threading.Lock()
        self._completed = 0
        self._success = 0
        self._failed = 0
    
    def stop(self):
        self._stop = True
    
    def run(self):
        total = len(self.lines)
        output_dir = ""
        
        for item in self.lines:
            if self._stop:
                break
            
            idx = item["idx"]
            text = item["text"].strip()
            output_path = item["output_path"]
            output_dir = os.path.dirname(output_path)
            
            if not text:
                with self._lock:
                    self._completed += 1
                    self._success += 1
                self.signals.line_done.emit(idx, True, "")
                continue
            
            try:
                result = self.service.generate_tts(
                    text=text,
                    voice_id=self.voice_id,
                    output_path=output_path,
                    model_id=self.model_id,
                    **self.voice_settings
                )
                
                with self._lock:
                    self._completed += 1
                    if result.success:
                        self._success += 1
                    else:
                        self._failed += 1
                
                if result.account_email:
                    self.signals.account_changed.emit(result.account_email)
                
                self.signals.line_done.emit(idx, result.success, result.error or "")
                
            except Exception as e:
                with self._lock:
                    self._completed += 1
                    self._failed += 1
                self.signals.line_done.emit(idx, False, str(e))
            
            with self._lock:
                self.signals.progress.emit(self._completed, total, f"{self._completed}/{total}")
            
            time.sleep(0.3)
        
        with self._lock:
            success = self._failed == 0
            msg = f"✅ {self._success}/{total} thành công" if success else f"⚠️ {self._success}/{total} thành công, {self._failed} lỗi"
        
        self.signals.finished.emit(success, msg, output_dir)


class JWTTTSTab(QtWidgets.QWidget):
    """Tab 💎 Giọng Trả Phí - UI giống hệt tab Chuyển văn bản"""
    
    def __init__(self, parent=None, log_fn: Callable = None, proxy_fn: Callable = None, user_id: int = None, proxy_service=None):
        super().__init__(parent)
        self._parent = parent
        self._log_fn = log_fn or print
        self._proxy_fn = proxy_fn
        self._proxy_service = proxy_service  # 🔧 NEW: ProxyService instance for rotation
        
        # Get user_id from parameter or parent
        self._user_id = user_id
        if not self._user_id and parent and hasattr(parent, 'current_user_id'):
            self._user_id = parent.current_user_id
        
        self._service: Optional[JWTTTSService] = None
        self._worker: Optional[JWTBatchTTSWorker] = None
        self._pool = QThreadPool.globalInstance()
        self._files: List[Tuple[str, str]] = []
        self._current_file_lines: List[str] = []
        self._current_file_index: int = 0  # Track current file index in batch mode
        self._output_dir: str = ""
        self._settings = self._load_settings()
        self._voices_cache: List[Dict] = []
        
        # 🔧 Track file completion for batch mode
        self._files_completed: int = 0
        self._files_total: int = 0
        
        # 🔧 FIX: Ensure advanced settings are saved to file if missing
        self._ensure_advanced_settings_saved()
        
        # 🔧 FIX: Track status per file in folder batch mode
        # Format: {filepath: {row: {'Output': val, 'Timing': val, 'Status': val}}}
        self._file_status: Dict[str, Dict[int, Dict[str, str]]] = {}
        
        self._setup_ui()
        
        # Auto load accounts từ D1 sau khi UI ready
        QtCore.QTimer.singleShot(500, self._auto_load_d1_accounts)
    
    def _log(self, msg: str):
        try:
            self._log_fn(msg)
        except:
            print(msg)
    
    def _get_service(self) -> JWTTTSService:
        if self._service is None:
            self._service = JWTTTSService(log_fn=self._log, proxy_fn=self._proxy_fn, proxy_service=self._proxy_service)
        return self._service
    
    def set_proxy_service(self, proxy_service):
        """
        🔧 NEW: Set ProxyService instance để có thể rotate proxy khi cần.
        Gọi method này sau khi tab được tạo nếu proxy_service chưa có sẵn.
        """
        self._proxy_service = proxy_service
        if self._service:
            self._service.set_proxy_service(proxy_service)
            self._log("✅ ProxyService linked to JWT TTS service")
    
    def set_user_id(self, user_id: int):
        """Set user_id và auto load accounts từ D1"""
        self._user_id = user_id
        if user_id:
            self._auto_load_d1_accounts()
    
    def _auto_load_d1_accounts(self):
        """Auto load JWT accounts từ D1 database"""
        # Try to get user_id from parent if not set
        if not self._user_id and self._parent and hasattr(self._parent, 'current_user_id'):
            self._user_id = self._parent.current_user_id
        
        if not self._user_id:
            self._log("No user_id available for D1 auto-load")
            return
        
        try:
            service = self._get_service()
            count = service.load_accounts_from_d1(self._user_id)
            
            if count > 0:
                self.lbl_accounts.setText(f"D1: {count} accounts")
                self.lbl_accounts.setStyleSheet("color: #28a745; font-weight: bold;")
                self._log(f"Auto-loaded {count} JWT accounts from D1 for user {self._user_id}")
            else:
                self.lbl_accounts.setText("D1: 0 accounts")
                self.lbl_accounts.setStyleSheet("color: #888; font-weight: bold;")
                self._log(f"No JWT accounts found in D1 for user {self._user_id}")
        except Exception as e:
            self._log(f"Auto-load D1 accounts error: {e}")
            self.lbl_accounts.setText("D1: Load failed")
            self.lbl_accounts.setStyleSheet("color: #dc3545; font-weight: bold;")
    
    def _load_settings(self) -> Dict:
        settings_file = JWT_TTS_SETTINGS_FILE
        print(f"🔧 [JWT_TTS] _load_settings() called, settings_file = {settings_file}")
        # Default advanced settings
        default_advanced = {
            'gap_enabled': False,
            'gap_seconds': 1.3,
            'gap_every': 5,
            'pause_char_enabled': True,
            'char1': ',',
            'char1_sec': 0.3,
            'char2': '.',
            'char2_sec': 0.5,
            'max_chars': 300,
        }
        defaults = {
            'voice_id': '21m00Tcm4TlvDq8ikWAM', 'voice_name': 'Rachel',
            'model_id': 'eleven_multilingual_v2', 'stability': 50, 'similarity': 75,
            'speed': 1.0, 'style': 0, 'speaker_boost': False, 'change_settings': True,
            'thread_count': 5, 'accounts_file': '', 'last_folder': '', 'last_search_text': '',
            'voice_list': [],  # Danh sách voices đã lưu: [{id, name}, ...]
            'advanced': default_advanced,  # Advanced settings with defaults
        }
        
        file_exists = os.path.exists(settings_file)
        print(f"🔧 [JWT_TTS] File exists: {file_exists}")
        
        if file_exists:
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    # Merge advanced settings properly (keep defaults for missing keys)
                    if 'advanced' in loaded:
                        default_advanced.update(loaded['advanced'])
                        loaded['advanced'] = default_advanced
                    defaults.update(loaded)
                print(f"✅ [JWT_TTS] Loaded settings from {settings_file}")
            except Exception as e:
                print(f"⚠️ Error loading jwt_tts_settings.json: {e}")
        else:
            # File không tồn tại - tạo mới với defaults (backup - should already exist from module init)
            print(f"🔧 [JWT_TTS] Creating new settings file...")
            try:
                with open(settings_file, 'w', encoding='utf-8') as f:
                    json.dump(defaults, f, indent=2, ensure_ascii=False)
                print(f"✅ Created jwt_tts_settings.json at {settings_file}")
            except Exception as e:
                print(f"❌ Cannot create jwt_tts_settings.json: {e}")
        
        return defaults
    
    def _save_settings(self):
        try:
            with open(JWT_TTS_SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"❌ Cannot save jwt_tts_settings.json: {e}")
    
    def _ensure_advanced_settings_saved(self):
        """Ensure advanced settings exist in file (for migration from old versions)"""
        try:
            if os.path.exists(JWT_TTS_SETTINGS_FILE):
                with open(JWT_TTS_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                # If 'advanced' key is missing in file, save current settings
                if 'advanced' not in saved:
                    self._save_settings()
        except:
            pass
    
    def _setup_ui(self):
        root = QtWidgets.QGridLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        
        # ========== ROW 0: Voice + Settings + Options ==========
        # --- Voice Group ---
        grp_voice = QtWidgets.QGroupBox("Voice")
        v = QtWidgets.QGridLayout(grp_voice)
        v.setHorizontalSpacing(6)
        
        v.addWidget(QtWidgets.QLabel("Name:"), 0, 0)
        self.ed_name = QtWidgets.QLineEdit()
        self.ed_name.setPlaceholderText("Voice ID hoặc tên...")
        self.ed_name.setText(self._settings.get('last_search_text', ''))
        self.ed_name.setMinimumWidth(120)
        v.addWidget(self.ed_name, 0, 1)
        
        # Search + Save buttons in horizontal layout
        btn_search_save = QtWidgets.QHBoxLayout()
        btn_search_save.setSpacing(2)
        # 🔧 Ẩn nút search bên ngoài - chuyển vào dialog
        # self.bt_search = QtWidgets.QPushButton("🔍")
        # self.bt_search.setToolTip("Tìm voice")
        # self.bt_search.setFixedWidth(30)
        self.bt_save_voice = QtWidgets.QPushButton("📋")
        self.bt_save_voice.setToolTip("Quản lý Voice")
        self.bt_save_voice.setFixedWidth(30)
        # btn_search_save.addWidget(self.bt_search)
        btn_search_save.addWidget(self.bt_save_voice)
        v.addLayout(btn_search_save, 0, 2)
        
        v.addWidget(QtWidgets.QLabel("Voice:"), 1, 0)
        self.cb_voice = QtWidgets.QComboBox()
        self.cb_voice.setMinimumWidth(180)
        if self._settings.get('voice_name') and self._settings.get('voice_id'):
            self.cb_voice.addItem(self._settings['voice_name'], userData=self._settings['voice_id'])
        v.addWidget(self.cb_voice, 1, 1, 1, 2)
        
        v.addWidget(QtWidgets.QLabel("Model:"), 2, 0)
        self.cb_model = QtWidgets.QComboBox()
        self.cb_model.addItems(["eleven_multilingual_v2", "eleven_turbo_v2_5", "eleven_turbo_v2", 
                                "eleven_flash_v2_5", "eleven_flash_v2", "eleven_v3"])
        self.cb_model.setCurrentText(self._settings.get('model_id', 'eleven_multilingual_v2'))
        v.addWidget(self.cb_model, 2, 1, 1, 2)
        
        v.addWidget(QtWidgets.QLabel("Language:"), 3, 0)
        self.cb_language = QtWidgets.QComboBox()
        for display, code in [("Auto-detect (Default)", ""), ("Vietnamese - vi", "vi"), 
                              ("English - en", "en"), ("Japanese - ja", "ja"), ("Korean - ko", "ko")]:
            self.cb_language.addItem(display, userData=code)
        v.addWidget(self.cb_language, 3, 1, 1, 2)
        grp_voice.setMaximumWidth(280)
        
        # --- Voice Settings Group ---
        grp_set = QtWidgets.QGroupBox("")
        s = QtWidgets.QGridLayout(grp_set)
        s.setHorizontalSpacing(8)
        
        self.cb_change = QtWidgets.QCheckBox("Change voice settings")
        self.cb_change.setChecked(self._settings.get('change_settings', True))
        s.addWidget(self.cb_change, 0, 0, 1, 3)
        
        self.cb_boost = QtWidgets.QCheckBox("Speaker Boost")
        self.cb_boost.setChecked(self._settings.get('speaker_boost', False))
        s.addWidget(self.cb_boost, 0, 3, 1, 2)
        
        s.addWidget(QtWidgets.QLabel("Speed:"), 1, 0)
        self.sb_speed = QtWidgets.QDoubleSpinBox()
        self.sb_speed.setRange(0.5, 2.0)
        self.sb_speed.setValue(self._settings.get('speed', 1.0))
        self.sb_speed.setSingleStep(0.05)
        s.addWidget(self.sb_speed, 1, 1)
        
        s.addWidget(QtWidgets.QLabel("Style:"), 1, 2)
        self.sb_style = QtWidgets.QSpinBox()
        self.sb_style.setRange(0, 100)
        self.sb_style.setValue(self._settings.get('style', 0))
        self.sb_style.setSuffix(" %")
        s.addWidget(self.sb_style, 1, 3)
        
        self.bt_reset = QtWidgets.QPushButton("Reset")
        s.addWidget(self.bt_reset, 1, 4)
        
        s.addWidget(QtWidgets.QLabel("Stability:"), 2, 0)
        self.cb_stab = QtWidgets.QComboBox()
        self.cb_stab.addItems(["0%", "50%", "100%"])
        stab_val = self._settings.get('stability', 50)
        self.cb_stab.setCurrentIndex(0 if stab_val <= 25 else (1 if stab_val <= 75 else 2))
        s.addWidget(self.cb_stab, 2, 1)
        
        s.addWidget(QtWidgets.QLabel("Similarity:"), 2, 2)
        self.sb_sim = QtWidgets.QSpinBox()
        self.sb_sim.setRange(0, 100)
        self.sb_sim.setValue(self._settings.get('similarity', 75))
        self.sb_sim.setSuffix(" %")
        s.addWidget(self.sb_sim, 2, 3)
        
        self.bt_load = QtWidgets.QPushButton("Load")
        self.bt_load.setToolTip("Load voice đã lưu")
        s.addWidget(self.bt_load, 2, 4)
        grp_set.setMaximumWidth(320)
        
        # --- Options Group ---
        grp_opt = QtWidgets.QGroupBox("Options")
        o = QtWidgets.QGridLayout(grp_opt)
        
        th = QtWidgets.QHBoxLayout()
        th.addWidget(QtWidgets.QLabel("Thread:"))
        self.sb_thread = QtWidgets.QSpinBox()
        self.sb_thread.setRange(1, 10)
        self.sb_thread.setValue(self._settings.get('thread_count', 5))
        th.addWidget(self.sb_thread)
        th.addStretch(1)
        o.addLayout(th, 0, 0, 1, 3)
        
        self.bt_adv = QtWidgets.QPushButton("Cài đặt nâng cao")
        o.addWidget(self.bt_adv, 1, 0, 1, 3)
        grp_opt.setMaximumWidth(200)
        
        # Top row (row 0)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(grp_voice, 4)
        top.addWidget(grp_set, 4)
        top.addWidget(grp_opt, 2)
        root.addLayout(top, 0, 0, 1, 2)
        
        # ========== ROW 1: JWT Accounts (HIDDEN - preview mode) ==========
        grp_acc = QtWidgets.QGroupBox("💎 JWT Accounts")
        acc_layout = QtWidgets.QHBoxLayout(grp_acc)
        acc_layout.setContentsMargins(10, 8, 10, 8)
        acc_layout.setSpacing(10)
        
        # 📂 Load Accounts button (hidden - accounts auto-load from D1)
        self.btn_load_accounts = QtWidgets.QPushButton("📂 Load Accounts...")
        self.btn_load_accounts.setMinimumWidth(130)
        self.btn_load_accounts.setVisible(False)  # Ẩn - accounts tự động load từ D1
        acc_layout.addWidget(self.btn_load_accounts)
        
        self.lbl_accounts = QtWidgets.QLabel("Preview Mode")
        self.lbl_accounts.setStyleSheet("color: #888; font-weight: bold;")
        acc_layout.addWidget(self.lbl_accounts)
        
        acc_layout.addStretch(1)
        
        # 🔧 HIDDEN: Check Credits và Cleanup Voices - không cần thiết cho user
        self.btn_check_credits = QtWidgets.QPushButton("💰 Check Credits")
        self.btn_check_credits.setVisible(False)
        acc_layout.addWidget(self.btn_check_credits)
        
        self.btn_cleanup = QtWidgets.QPushButton("🧹 Cleanup Voices")
        self.btn_cleanup.setVisible(False)
        acc_layout.addWidget(self.btn_cleanup)
        
        grp_acc.setFixedHeight(55)
        grp_acc.setVisible(False)  # 🔧 ẨN panel D1 accounts (preview mode)
        root.addWidget(grp_acc, 1, 0, 1, 2)

        # ========== ROW 2: Batch Job ==========
        grp_b = QtWidgets.QGroupBox("Batch Job")
        b_layout = QtWidgets.QHBoxLayout(grp_b)
        
        # Left side
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 5, 0)
        
        # Path row
        path_row = QtWidgets.QHBoxLayout()
        self.lbl_path = QtWidgets.QLabel("Đường Dẫn:")
        self.lbl_path.setFixedWidth(60)
        path_row.addWidget(self.lbl_path)
        self.ed_folder = QtWidgets.QLineEdit()
        self.ed_folder.setPlaceholderText("Chọn file .txt hoặc thư mục...")
        path_row.addWidget(self.ed_folder)
        left_layout.addLayout(path_row)
        
        # Buttons row
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(4)
        btn_row.addSpacing(60)
        
        self.bt_browse_file = QtWidgets.QPushButton("📄 File")
        self.bt_browse_folder = QtWidgets.QPushButton("📁 Folder")
        self.bt_browse_srt = QtWidgets.QPushButton("📜 SRT")
        self.bt_browse_file.setToolTip("Chọn 1 file .txt")
        self.bt_browse_folder.setToolTip("Chọn thư mục chứa file .txt")
        self.bt_browse_srt.setToolTip("Chọn file .srt để gen voice")
        btn_row.addWidget(self.bt_browse_file, 1)
        btn_row.addWidget(self.bt_browse_folder, 1)
        btn_row.addWidget(self.bt_browse_srt, 1)
        left_layout.addLayout(btn_row)
        
        # Result row
        result_row = QtWidgets.QHBoxLayout()
        result_row.addSpacing(60)
        
        self.cb_autosrt = QtWidgets.QCheckBox("Tự động tạo Srt")
        result_row.addWidget(self.cb_autosrt, 1)
        
        self.lbl_result = QtWidgets.QLabel("Kết Quả: 0/0")
        self.lbl_result.setStyleSheet("""
            QLabel {
                border: 1px solid #999;
                border-radius: 3px;
                padding: 2px 6px;
                background-color: #f5f5f5;
            }
        """)
        self.lbl_result.setAlignment(Qt.AlignCenter)
        result_row.addWidget(self.lbl_result, 1)
        result_row.addStretch(1)
        left_layout.addLayout(result_row)
        left_layout.addStretch(1)
        
        # Right side - queue table
        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 0, 0, 0)
        
        self.tbl_queue = QtWidgets.QTableWidget(0, 4)
        self.tbl_queue.setHorizontalHeaderLabels(["ID", "FileName", "Status", "Tiến độ"])
        # Disable selection - không cho user click vào bảng
        self.tbl_queue.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.tbl_queue.setFocusPolicy(Qt.NoFocus)
        hdr = self.tbl_queue.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(2, 100)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(3, 100)
        self.tbl_queue.verticalHeader().setVisible(False)
        small_font = QtGui.QFont()
        small_font.setPointSize(8)
        self.tbl_queue.setFont(small_font)
        self.tbl_queue.verticalHeader().setDefaultSectionSize(20)
        right_layout.addWidget(self.tbl_queue)
        
        b_layout.addWidget(left_widget, 35)
        b_layout.addWidget(right_widget, 65)
        
        grp_b.setFixedHeight(130)
        root.addWidget(grp_b, 2, 0, 1, 2)
        
        # ========== ROW 3: Subtitles ==========
        grp_s = QtWidgets.QGroupBox("Subtitles")
        vs = QtWidgets.QVBoxLayout(grp_s)
        
        # Control buttons
        tb = QtWidgets.QHBoxLayout()
        self.bt_start = QtWidgets.QPushButton("Start")
        self.bt_stop = QtWidgets.QPushButton("Stop")
        self.bt_more = QtWidgets.QPushButton("📁 Output")
        self.bt_stop.setEnabled(False)
        for w in [self.bt_start, self.bt_stop, self.bt_more]:
            tb.addWidget(w)
        tb.addStretch(1)
        vs.addLayout(tb)
        
        # Subtitles table - ID, Output, Timing, Chars, Content, Status (bỏ Voice#)
        self.tbl_sub = QtWidgets.QTableWidget(0, 6)
        self.tbl_sub.setHorizontalHeaderLabels(["ID", "Output", "Timing", "Chars", "Content", "Status"])
        self.tbl_sub.verticalHeader().setVisible(False)
        self.tbl_sub.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_sub.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        
        hdr_sub = self.tbl_sub.horizontalHeader()
        hdr_sub.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(0, 40)   # ID
        hdr_sub.setSectionResizeMode(1, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(1, 80)   # Output
        hdr_sub.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(2, 80)   # Timing
        hdr_sub.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(3, 50)   # Chars
        self.tbl_sub.setColumnHidden(3, True)  # Ẩn cột Chars
        hdr_sub.setSectionResizeMode(4, QHeaderView.Stretch)  # Content
        hdr_sub.setSectionResizeMode(5, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(5, 100)   # Status
        
        vs.addWidget(self.tbl_sub)
        root.addWidget(grp_s, 3, 0, 1, 2)
        root.setRowStretch(3, 1)
        
        # ========== CONNECT SIGNALS ==========
        # 🔧 bt_search đã được ẩn - chuyển vào dialog
        # self.bt_search.clicked.connect(self._search_voice)
        self.bt_browse_file.clicked.connect(self._pick_file)
        self.bt_browse_folder.clicked.connect(self._pick_folder)
        self.bt_browse_srt.clicked.connect(self._pick_srt)
        self.bt_start.clicked.connect(self._start_tts)
        self.bt_stop.clicked.connect(self._stop_tts)
        self.bt_more.clicked.connect(self._open_output_folder)
        self.bt_reset.clicked.connect(self._reset_settings)
        self.bt_save_voice.clicked.connect(self._open_voice_manager)
        self.bt_load.clicked.connect(self._load_voice_settings)
        self.bt_adv.clicked.connect(self._open_advanced_settings)
        
        self.btn_load_accounts.clicked.connect(self._load_accounts)
        self.btn_check_credits.clicked.connect(self._check_all_credits)
        self.btn_cleanup.clicked.connect(self._cleanup_voices)
        
        self.cb_voice.currentIndexChanged.connect(self._on_voice_changed)
        self.cb_model.currentIndexChanged.connect(self._save_settings_delayed)
        self.sb_speed.valueChanged.connect(self._save_settings_delayed)
        self.sb_sim.valueChanged.connect(self._save_settings_delayed)
        self.sb_thread.valueChanged.connect(self._save_settings_delayed)
        
        # 🔧 NEW: Auto-load khi paste path vào ed_folder (với debounce)
        self._path_load_timer = QtCore.QTimer()
        self._path_load_timer.setSingleShot(True)
        self._path_load_timer.timeout.connect(self._load_from_path_input)
        self.ed_folder.textChanged.connect(self._on_path_text_changed)
        
        # 🔧 Disabled: Queue table click - không cho user click vào bảng batch job
        # self.tbl_queue.cellClicked.connect(self._on_queue_item_clicked)
        
        # 🔧 Auto-load saved voices khi khởi động
        QtCore.QTimer.singleShot(100, self._load_voice_settings)
    
    # ------------------------------------------------------------------
    # SETTINGS
    # ------------------------------------------------------------------
    def _reset_settings(self):
        """Reset voice settings to default"""
        self.sb_speed.setValue(1.0)
        self.sb_style.setValue(0)
        self.cb_stab.setCurrentIndex(1)  # 50%
        self.sb_sim.setValue(75)
        self.cb_boost.setChecked(False)
    
    def _open_voice_manager(self):
        """Open Voice Manager Dialog"""
        voice_list = self._settings.get('voice_list', [])
        current_voice_id = self._settings.get('voice_id')
        
        dialog = JWTVoiceManagerDialog(
            self, voice_list, current_voice_id,
            service=self._get_service(),
            proxy_fn=self._proxy_fn
        )
        dialog.voice_selected.connect(self._on_voice_selected_from_manager)
        
        # Show dialog (blocking)
        dialog.exec()
        
        # Always save updated voice list when dialog closes
        self._settings['voice_list'] = dialog.get_voice_list()
        self._save_settings()
        
        # Reload combobox
        self._reload_voice_combobox()
    
    def _on_voice_selected_from_manager(self, voice_id: str, label: str, model_id: str):
        """Handle voice selection from Voice Manager"""
        self._log(f"🎤 Selected voice: {label} ({voice_id[:8]}...) - {model_id}")
        
        # Update settings
        self._settings['voice_id'] = voice_id
        self._settings['voice_name'] = label
        self._settings['model_id'] = model_id
        self._save_settings()
        
        # Update UI
        self.ed_name.setText(voice_id)
        self.cb_model.setCurrentText(model_id)
        
        # Update combobox
        self._reload_voice_combobox()
        
        # Select the voice in combobox
        for i in range(self.cb_voice.count()):
            if self.cb_voice.itemData(i) == voice_id:
                self.cb_voice.setCurrentIndex(i)
                break
    
    def _reload_voice_combobox(self):
        """Reload voice combobox from settings"""
        # Block signals to prevent triggering _on_voice_changed
        self.cb_voice.blockSignals(True)
        
        current_voice_id = self._settings.get('voice_id')
        self.cb_voice.clear()
        
        voice_list = self._settings.get('voice_list', [])
        for voice in voice_list:
            vid = voice.get('id')
            # Use label if available, otherwise use name
            display_name = voice.get('label') or voice.get('name', vid[:8] if vid else 'Unknown')
            model = voice.get('model', '')
            if model:
                model_short = model.replace('eleven_', '').replace('_', ' ')
                display_name = f"{display_name} [{model_short}]"
            
            if vid:
                self.cb_voice.addItem(display_name, userData=vid)
        
        # Select current voice
        if current_voice_id:
            for i in range(self.cb_voice.count()):
                if self.cb_voice.itemData(i) == current_voice_id:
                    self.cb_voice.setCurrentIndex(i)
                    break
        
        self.cb_voice.blockSignals(False)
    
    def _save_voice_to_list(self):
        """Save current voice selection to voice_list (internal use)"""
        idx = self.cb_voice.currentIndex()
        
        if idx >= 0:
            voice_id = self.cb_voice.itemData(idx)
            voice_name = self.cb_voice.currentText()
            if voice_id:
                # Lưu voice hiện tại
                self._settings['voice_id'] = voice_id
                self._settings['voice_name'] = voice_name
                
                # Lưu vào voice_list (tránh trùng)
                voice_list = self._settings.get('voice_list', [])
                # Xóa nếu đã tồn tại
                voice_list = [v for v in voice_list if v.get('id') != voice_id]
                # Thêm vào đầu
                voice_list.insert(0, {
                    'id': voice_id, 
                    'name': voice_name,
                    'label': voice_name,
                    'model': self.cb_model.currentText()
                })
                # Giới hạn 50 voices
                self._settings['voice_list'] = voice_list[:50]
                
                self._save_settings()
                self._log(f"✅ Đã lưu voice: {voice_name}")
    
    def _load_voice_settings(self):
        """Load saved voices from settings"""
        voice_list = self._settings.get('voice_list', [])
        voice_id = self._settings.get('voice_id')
        model_id = self._settings.get('model_id', 'eleven_multilingual_v2')
        
        # Reload combobox với format mới
        self._reload_voice_combobox()
        
        # Set model
        if model_id:
            self.cb_model.setCurrentText(model_id)
        
        # Set current voice in text field
        if voice_id:
            self.ed_name.setText(voice_id)
        
        if voice_list:
            self._log(f"📥 Đã load {len(voice_list)} voices từ settings")
        else:
            self._log("⚠️ Chưa có voice đã lưu - nhấn 📋 để quản lý")
    
    def _open_advanced_settings(self):
        """Open advanced settings dialog"""
        dialog = JWTAdvancedSettingsDialog(self, self._settings.get('advanced', {}))
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            adv_settings = dialog.get_settings()
            self._settings['advanced'] = adv_settings
            self._save_settings()
            self._log(f"✅ Đã lưu cài đặt nâng cao: gap={adv_settings['gap_enabled']}, max_chars={adv_settings['max_chars']}")
    
    def _save_settings_delayed(self):
        """Save settings"""
        self._settings['model_id'] = self.cb_model.currentText()
        self._settings['speed'] = self.sb_speed.value()
        self._settings['similarity'] = self.sb_sim.value()
        self._settings['thread_count'] = self.sb_thread.value()
        self._settings['last_search_text'] = self.ed_name.text()
        self._save_settings()
    
    def _on_voice_changed(self, index: int):
        """Handle voice selection change"""
        if index < 0:
            return
        voice_id = self.cb_voice.itemData(index)
        voice_name = self.cb_voice.itemText(index)
        if voice_id:
            self._settings['voice_id'] = voice_id
            self._settings['voice_name'] = voice_name
            
            # Tìm model từ voice_list
            voice_list = self._settings.get('voice_list', [])
            for v in voice_list:
                if v.get('id') == voice_id:
                    model = v.get('model')
                    if model:
                        self._settings['model_id'] = model
                        self.cb_model.setCurrentText(model)
                    break
            
            # Cập nhật voice_list (đưa voice được chọn lên đầu)
            voice_list = [v for v in voice_list if v.get('id') != voice_id]
            # Tìm voice info để giữ label và model
            voice_info = None
            for v in self._settings.get('voice_list', []):
                if v.get('id') == voice_id:
                    voice_info = v
                    break
            
            if voice_info:
                voice_list.insert(0, voice_info)
            else:
                voice_list.insert(0, {
                    'id': voice_id, 
                    'name': voice_name,
                    'label': voice_name,
                    'model': self.cb_model.currentText()
                })
            self._settings['voice_list'] = voice_list[:50]
            
            self._save_settings()
            # Hiển thị voice_id trong textbox Name
            self.ed_name.setText(voice_id)
    
    # ------------------------------------------------------------------
    # ACCOUNTS
    # ------------------------------------------------------------------
    def _load_accounts(self):
        """Load accounts - hiển thị dialog chọn nguồn"""
        # Tạo dialog chọn nguồn
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Load JWT Accounts")
        dialog.setMinimumWidth(350)
        
        layout = QtWidgets.QVBoxLayout(dialog)
        
        # Info label
        info = QtWidgets.QLabel("Chọn nguồn để load JWT accounts:")
        layout.addWidget(info)
        
        # Option 1: Load từ D1
        btn_d1 = QtWidgets.QPushButton(f"📡 Load từ D1 Database (user_id={self._user_id or 'N/A'})")
        btn_d1.setEnabled(bool(self._user_id))
        layout.addWidget(btn_d1)
        
        # Option 2: Load từ file
        btn_file = QtWidgets.QPushButton("📂 Load từ File TXT (email|password)")
        layout.addWidget(btn_file)
        
        # Cancel
        btn_cancel = QtWidgets.QPushButton("Hủy")
        layout.addWidget(btn_cancel)
        
        def load_from_d1():
            dialog.accept()
            self._auto_load_d1_accounts()
        
        def load_from_file():
            dialog.accept()
            self._load_accounts_from_file()
        
        btn_d1.clicked.connect(load_from_d1)
        btn_file.clicked.connect(load_from_file)
        btn_cancel.clicked.connect(dialog.reject)
        
        dialog.exec()
    
    def _load_accounts_from_file(self):
        """Load accounts từ file TXT"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file accounts (email|password)",
            self._settings.get('accounts_file', ''),
            "Text Files (*.txt);;All Files (*)"
        )
        
        if not path:
            return
        
        service = self._get_service()
        count = service.load_accounts_from_file(path)
        
        if count > 0:
            self._settings['accounts_file'] = path
            self._save_settings()
            self.lbl_accounts.setText(f"File: {count} accounts")
            self.lbl_accounts.setStyleSheet("color: #28a745; font-weight: bold;")
            self._log(f"Loaded {count} JWT accounts from {os.path.basename(path)}")
        else:
            self.lbl_accounts.setText("Load failed")
            self.lbl_accounts.setStyleSheet("color: #dc3545; font-weight: bold;")
            self._log(f"Failed to load accounts from {path}")
    
    def _check_all_credits(self):
        """Check credits của tất cả accounts"""
        service = self._get_service()
        if service.account_pool.get_stats()['total'] == 0:
            QtWidgets.QMessageBox.warning(self, "Warning", "Chưa load accounts!")
            return
        
        self._log("💰 Checking credits...")
        self.btn_check_credits.setEnabled(False)
        self.btn_check_credits.setText("...")
        QtWidgets.QApplication.processEvents()
        
        try:
            stats = service.get_stats()
            self.lbl_accounts.setText(f"✅ {stats['ready']}/{stats['total']} ready | 💰 {stats['total_chars_generated']:,} chars")
            self.lbl_accounts.setStyleSheet("color: #28a745; font-weight: bold;")
            self._log(f"💰 Stats: {stats['ready']} ready, {stats['total_chars_generated']:,} chars generated")
        except Exception as e:
            self._log(f"❌ Check error: {e}")
        finally:
            self.btn_check_credits.setEnabled(True)
            self.btn_check_credits.setText("💰 Check Credits")
    
    def _cleanup_voices(self):
        """Cleanup custom voices từ tất cả accounts"""
        service = self._get_service()
        if service.account_pool.get_stats()['total'] == 0:
            QtWidgets.QMessageBox.warning(self, "Warning", "Chưa load accounts!")
            return
        
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận",
            "Xóa tất cả custom voices từ các accounts?\n\n"
            "Chỉ xóa voices do user tạo, giữ lại premade voices.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        
        if reply != QtWidgets.QMessageBox.Yes:
            return
        
        self._log("🧹 Cleaning up voices...")
        self.btn_cleanup.setEnabled(False)
        QtWidgets.QApplication.processEvents()
        
        try:
            proxies = self._proxy_fn() if self._proxy_fn else None
            total_deleted = 0
            
            for acc_info in service.account_pool.get_account_list():
                email = acc_info['email']
                deleted = service.account_pool.cleanup_account_voices(email, proxies)
                total_deleted += deleted
                if deleted > 0:
                    self._log(f"🧹 {email}: deleted {deleted} voices")
            
            self._log(f"🧹 Total deleted: {total_deleted} voices")
            QtWidgets.QMessageBox.information(self, "Hoàn thành", f"Đã xóa {total_deleted} custom voices!")
        except Exception as e:
            self._log(f"❌ Cleanup error: {e}")
        finally:
            self.btn_cleanup.setEnabled(True)

    # ================================================================
    # VOICE SEARCH
    # ================================================================
    def _search_voice(self):
        """Search voices từ ElevenLabs với JWT account"""
        search_text = self.ed_name.text().strip()
        
        service = self._get_service()
        stats = service.account_pool.get_stats()
        
        if stats['total'] == 0:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Chưa load accounts!\n\n"
                "Vui lòng bấm nút 📂 Load Accounts để load accounts trước."
            )
            return
        
        self._log(f"🔍 Searching voices: '{search_text}'...")
        self.bt_search.setEnabled(False)
        self.bt_search.setText("...")
        QtWidgets.QApplication.processEvents()
        
        try:
            account = service.account_pool.get_account()
            if not account:
                self._log("❌ No available account for search")
                QtWidgets.QMessageBox.warning(self, "Lỗi", "Không có account khả dụng để search!")
                return
            
            try:
                proxies = self._proxy_fn() if self._proxy_fn else None
                
                # Search user voices + shared voices
                voices = JWTAPIClient.search_all_voices(
                    account.jwt_token,
                    search=search_text,
                    proxies=proxies
                )
                
                # Search shared voices if search text provided
                if search_text:
                    try:
                        shared_voices = JWTAPIClient.search_all_shared_voices(
                            account.jwt_token,
                            search=search_text,
                            proxies=proxies
                        )
                        for v in shared_voices[:20]:
                            v['_is_shared'] = True
                        voices.extend(shared_voices[:20])
                    except Exception as e:
                        self._log(f"⚠️ Search shared voices error: {e}")
                
                self._voices_cache = voices
                
                # Lấy danh sách voice_id hiện có trong combobox
                existing_ids = set()
                for i in range(self.cb_voice.count()):
                    vid = self.cb_voice.itemData(i)
                    if vid:
                        existing_ids.add(vid)
                
                # Thêm voices mới vào đầu combobox (không clear)
                added_count = 0
                for voice in voices:
                    name = voice.get('name', 'Unknown')
                    voice_id = voice.get('voice_id') or voice.get('public_owner_id')
                    category = voice.get('category', '')
                    is_shared = voice.get('_is_shared', False)
                    
                    # Chỉ thêm nếu chưa có trong combobox
                    if voice_id and voice_id not in existing_ids:
                        if is_shared:
                            display = f"🌐 {name}"
                        elif category == 'premade':
                            display = f"⭐ {name}"
                        else:
                            display = name
                        
                        self.cb_voice.insertItem(added_count, display, userData=voice_id)
                        added_count += 1
                        existing_ids.add(voice_id)
                
                # Select voice đầu tiên trong kết quả search
                if voices and added_count > 0:
                    self.cb_voice.setCurrentIndex(0)
                elif voices:
                    # Voice đã có trong combobox, select nó
                    first_voice_id = voices[0].get('voice_id') or voices[0].get('public_owner_id')
                    for i in range(self.cb_voice.count()):
                        if self.cb_voice.itemData(i) == first_voice_id:
                            self.cb_voice.setCurrentIndex(i)
                            break
                
                # Show search result feedback
                result_msg = f"✅ Tìm thấy {len(voices)} voices (thêm {added_count} mới)"
                self._log(result_msg)
                self._settings['last_search_text'] = search_text
                self._save_settings()
                
                # Show notification
                if len(voices) == 0:
                    QtWidgets.QMessageBox.information(
                        self, "Kết quả tìm kiếm",
                        f"Không tìm thấy voice nào với từ khóa: '{search_text}'\n\n"
                        "Thử tìm với từ khóa khác hoặc để trống để xem tất cả."
                    )
                else:
                    # Brief status update instead of popup for successful search
                    self.lbl_result.setText(f"🔍 {len(voices)} voices")
                    QtCore.QTimer.singleShot(3000, lambda: self.lbl_result.setText(f"Kết Quả: 0/{len(self._chunks_data) if hasattr(self, '_chunks_data') else 0}"))
                    
                    # 🔧 NEW: Auto-save first voice to voice_list with original name
                    if voices:
                        first_voice = voices[0]
                        voice_id = first_voice.get('voice_id') or first_voice.get('public_owner_id')
                        voice_name = first_voice.get('name', 'Unknown')
                        
                        if voice_id:
                            # Check if already in voice_list
                            voice_list = self._settings.get('voice_list', [])
                            exists = any(v.get('id') == voice_id for v in voice_list)
                            
                            if not exists:
                                # Add to voice_list with original name
                                voice_list.insert(0, {
                                    'id': voice_id,
                                    'name': voice_name,
                                    'label': voice_name,  # Default label = original name
                                    'model': self.cb_model.currentText()
                                })
                                self._settings['voice_list'] = voice_list[:50]
                                self._settings['voice_id'] = voice_id
                                self._settings['voice_name'] = voice_name
                                self._save_settings()
                                self._log(f"💾 Auto-saved voice: {voice_name} ({voice_id[:8]}...)")
                
            finally:
                service.account_pool.release_account(account.email, success=True)
                
        except Exception as e:
            self._log(f"❌ Search error: {e}")
            QtWidgets.QMessageBox.critical(self, "Lỗi", f"Lỗi khi tìm kiếm: {e}")
        finally:
            self.bt_search.setEnabled(True)
            self.bt_search.setText("🔍")
    
    # ------------------------------------------------------------------
    # FILE LOADING
    # ------------------------------------------------------------------
    def _on_path_text_changed(self, text: str):
        """
        🔧 NEW: Debounce handler khi text thay đổi trong ed_folder.
        Đợi 500ms sau khi user ngừng gõ/paste rồi mới load.
        """
        # Reset timer mỗi khi text thay đổi
        self._path_load_timer.stop()
        
        # Chỉ trigger nếu có text và có vẻ là path hợp lệ
        text = text.strip()
        if text:
            # Remove quotes để check
            clean_path = text
            if (text.startswith('"') and text.endswith('"')) or \
               (text.startswith("'") and text.endswith("'")):
                clean_path = text[1:-1]
            
            # Chỉ auto-load nếu path tồn tại (tránh load khi đang gõ)
            if os.path.exists(clean_path):
                self._path_load_timer.start(300)  # 300ms debounce
    
    def _load_from_path_input(self):
        """
        🔧 NEW: Load file/folder từ path được paste vào ed_folder.
        Auto-detect loại: .txt file, .srt file, hoặc folder.
        Hỗ trợ path có quotes ("path" hoặc 'path').
        """
        path = self.ed_folder.text().strip()
        if not path:
            return
        
        # Remove quotes nếu có
        if (path.startswith('"') and path.endswith('"')) or \
           (path.startswith("'") and path.endswith("'")):
            path = path[1:-1]
        
        # Update text field với path đã clean
        self.ed_folder.setText(path)
        
        # Check if path exists
        if not os.path.exists(path):
            self._log(f"❌ Path không tồn tại: {path}")
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"Path không tồn tại:\n{path}")
            return
        
        # Auto-detect type and load
        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext == '.txt':
                self._log(f"📄 Loading TXT file: {os.path.basename(path)}")
                self._settings['last_folder'] = os.path.dirname(path)
                self._save_settings()
                self._load_single_file(path)
            elif ext == '.srt':
                self._log(f"📜 Loading SRT file: {os.path.basename(path)}")
                self._settings['last_folder'] = os.path.dirname(path)
                self._save_settings()
                self._load_srt_file(path)
            else:
                self._log(f"⚠️ Unsupported file type: {ext}")
                QtWidgets.QMessageBox.warning(self, "Lỗi", f"Loại file không hỗ trợ: {ext}\nChỉ hỗ trợ .txt và .srt")
        elif os.path.isdir(path):
            self._log(f"📁 Loading folder: {os.path.basename(path)}")
            self._settings['last_folder'] = path
            self._save_settings()
            self._load_folder(path)
        else:
            self._log(f"❌ Invalid path: {path}")
    
    def _pick_file(self):
        """Pick single TXT file"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file text",
            self._settings.get('last_folder', ''),
            "Text Files (*.txt);;All Files (*)"
        )
        
        if not path:
            return
        
        self._settings['last_folder'] = os.path.dirname(path)
        self._save_settings()
        
        self.ed_folder.setText(path)
        self._load_single_file(path)
    
    def _pick_folder(self):
        """Pick folder containing TXT files"""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Chọn thư mục chứa file .txt",
            self._settings.get('last_folder', '')
        )
        
        if not folder:
            return
        
        self._settings['last_folder'] = folder
        self._save_settings()
        
        self.ed_folder.setText(folder)
        self._load_folder(folder)
    
    def _pick_srt(self):
        """Pick SRT file"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file SRT",
            self._settings.get('last_folder', ''),
            "SRT Files (*.srt);;All Files (*)"
        )
        
        if not path:
            return
        
        self._settings['last_folder'] = os.path.dirname(path)
        self._save_settings()
        
        self.ed_folder.setText(path)
        self._load_srt_file(path)
    
    # ================================================================
    # FILE STATUS TRACKING (for folder batch mode)
    # ================================================================
    def _get_current_file_path(self) -> Optional[str]:
        """Get current file path in batch mode"""
        if hasattr(self, '_files') and self._files and hasattr(self, '_current_file_index'):
            if 0 <= self._current_file_index < len(self._files):
                return self._files[self._current_file_index][0]
        return None
    
    def _save_current_file_status(self):
        """Save status of current file before switching to another file
        
        🔧 FIX: Merge với status đã có trong _file_status (từ worker signal)
        để không mất status khi worker đang chạy
        """
        file_path = self._get_current_file_path()
        if not file_path:
            return
        
        # Get existing status (from worker signals)
        existing_status = self._file_status.get(file_path, {})
        
        # Save status for each row in subtitles table
        status_data = {}
        for row in range(self.tbl_sub.rowCount()):
            row_data = {}
            # Save Output (col 1), Timing (col 2), Status (col 5)
            for col, col_name in [(1, 'Output'), (2, 'Timing'), (5, 'Status')]:
                item = self.tbl_sub.item(row, col)
                if item:
                    row_data[col_name] = item.text()
            if row_data:
                status_data[row] = row_data
        
        # 🔧 FIX: Merge - existing status takes priority (from worker)
        # because worker may have updated status after UI was last refreshed
        for row, row_data in status_data.items():
            if row not in existing_status:
                existing_status[row] = row_data
            else:
                # Merge columns, existing takes priority
                for col_name, value in row_data.items():
                    if col_name not in existing_status[row]:
                        existing_status[row][col_name] = value
        
        if existing_status:
            self._file_status[file_path] = existing_status
            self._log(f"💾 Saved status for {os.path.basename(file_path)}: {len(existing_status)} rows")
    
    def _restore_file_status(self, file_path: str):
        """Restore status for a file after switching back to it"""
        if file_path not in self._file_status:
            return
        
        status_data = self._file_status[file_path]
        restored_count = 0
        done_count = 0  # 🔧 FIX: Track done count for result label
        
        for row, row_data in status_data.items():
            if row >= self.tbl_sub.rowCount():
                continue
            
            for col_name, value in row_data.items():
                col_map = {'Output': 1, 'Timing': 2, 'Status': 5}
                col = col_map.get(col_name)
                if col is not None:
                    item = self.tbl_sub.item(row, col)
                    if item:
                        item.setText(value)
                    else:
                        self.tbl_sub.setItem(row, col, QtWidgets.QTableWidgetItem(value))
                    
                    # Restore background color for Status column
                    if col_name == 'Status':
                        item = self.tbl_sub.item(row, col)
                        if item:
                            value_lower = value.lower()
                            if "done" in value_lower or "empty" in value_lower:
                                item.setBackground(QtGui.QColor(200, 255, 200))  # Green
                                done_count += 1  # 🔧 FIX: Count done items
                            elif "error" in value_lower or "fail" in value_lower or "stopped" in value_lower:
                                item.setBackground(QtGui.QColor(255, 200, 200))  # Red
                            elif any(x in value_lower for x in ["processing", "retry", "switch", "wait"]):
                                item.setBackground(QtGui.QColor(255, 255, 200))  # Yellow
            
            restored_count += 1
        
        # 🔧 FIX: Update result label with done count
        if restored_count > 0:
            total = self.tbl_sub.rowCount()
            self.lbl_result.setText(f"Kết Quả: {done_count}/{total}")
            self._log(f"🔄 Restored status for {os.path.basename(file_path)}: {done_count}/{total} done")
    
    def _update_file_status(self, row: int, col_name: str, value: str):
        """Update status in _file_status dict when status changes during TTS"""
        file_path = self._get_current_file_path()
        if not file_path:
            return
        
        if file_path not in self._file_status:
            self._file_status[file_path] = {}
        
        if row not in self._file_status[file_path]:
            self._file_status[file_path][row] = {}
        
        self._file_status[file_path][row][col_name] = value
    
    def _load_single_file(self, path: str):
        """Load single TXT file"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            lines = [l.strip() for l in content.split('\n') if l.strip()]
            self._current_file_lines = lines
            
            base_name = os.path.splitext(os.path.basename(path))[0]
            self._output_dir = os.path.join(os.path.dirname(path), f"{base_name}_jwt_tts")
            
            # Reset file completion counter
            self._files_completed = 0
            self._files_total = 1
            self._is_folder_batch = False  # Single file mode
            self._files = [(path, os.path.basename(path))]
            self._current_file_index = 0
            self.lbl_result.setText(f"Kết Quả: 0/1")
            
            self.tbl_queue.setRowCount(1)
            self.tbl_queue.setItem(0, 0, QtWidgets.QTableWidgetItem("1"))
            self.tbl_queue.setItem(0, 1, QtWidgets.QTableWidgetItem(os.path.basename(path)))
            self.tbl_queue.setItem(0, 2, QtWidgets.QTableWidgetItem("Ready"))
            self.tbl_queue.setItem(0, 3, QtWidgets.QTableWidgetItem(f"{len(lines)} lines"))
            
            self._populate_subtitles_table(lines)
            self._log(f"📄 Loaded {len(lines)} lines from {os.path.basename(path)}")
            
        except Exception as e:
            self._log(f"❌ Error loading file: {e}")
    
    def _load_folder(self, folder: str):
        """Load all TXT files from folder for batch processing
        
        🔧 NEW: Hiển thị TẤT CẢ lines từ tất cả files trong bảng subtitle
        Nhưng output ra từng folder riêng cho mỗi file
        """
        try:
            txt_files = sorted([
                f for f in os.listdir(folder)
                if f.endswith('.txt') and os.path.isfile(os.path.join(folder, f))
            ])
            
            if not txt_files:
                self._log(f"⚠️ No .txt files found in {folder}")
                QtWidgets.QMessageBox.warning(
                    self, "Không tìm thấy file",
                    f"Không có file .txt nào trong thư mục:\n{folder}"
                )
                return
            
            # 🔧 FIX: Clear file status when loading new folder
            self._file_status = {}
            
            # Reset file completion counter
            self._files_completed = 0
            self._files_total = len(txt_files)
            self._is_folder_batch = len(txt_files) > 1  # Flag for folder batch mode
            self.lbl_result.setText(f"Kết Quả: 0/{len(txt_files)}")
            
            self._files = [(os.path.join(folder, f), f) for f in txt_files]
            self._current_folder = folder
            self._current_file_index = 0
            
            # Populate queue table
            self.tbl_queue.setRowCount(len(txt_files))
            for i, filename in enumerate(txt_files):
                self.tbl_queue.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i + 1)))
                self.tbl_queue.setItem(i, 1, QtWidgets.QTableWidgetItem(filename))
                self.tbl_queue.setItem(i, 2, QtWidgets.QTableWidgetItem("Ready"))
                self.tbl_queue.setItem(i, 3, QtWidgets.QTableWidgetItem("-"))
            
            # 🔧 NEW: Load TẤT CẢ files vào bảng subtitle
            all_lines_with_file_info = []  # [(file_idx, line_text), ...]
            
            for file_idx, filename in enumerate(txt_files):
                file_path = os.path.join(folder, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        lines = [l.strip() for l in f.read().split('\n') if l.strip()]
                    
                    for line in lines:
                        all_lines_with_file_info.append((file_idx, line))
                    
                    # Update queue table với số lines
                    self.tbl_queue.setItem(file_idx, 3, QtWidgets.QTableWidgetItem(f"{len(lines)} lines"))
                    
                except Exception as e:
                    self._log(f"⚠️ Error reading {filename}: {e}")
            
            # Set output dir for first file (sẽ được update khi gen TTS)
            if txt_files:
                base_name = os.path.splitext(txt_files[0])[0]
                self._output_dir = os.path.join(folder, f"{base_name}_jwt_tts")
            
            # Populate subtitles table với tất cả lines
            self._populate_subtitles_table_batch(all_lines_with_file_info)
            
            # Highlight first row in queue
            self.tbl_queue.selectRow(0)
            
            total_lines = len(all_lines_with_file_info)
            self._log(f"📁 Found {len(txt_files)} .txt files in folder")
            self._log(f"📄 Loaded {total_lines} lines total from all files")
            
        except Exception as e:
            self._log(f"❌ Error loading folder: {e}")
            QtWidgets.QMessageBox.critical(self, "Lỗi", f"Lỗi khi load folder: {e}")
    
    def _load_srt_file(self, path: str):
        """Load SRT file and extract text"""
        try:
            import re
            
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            pattern = r'(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.+?)(?=\n\n|\Z)'
            matches = re.findall(pattern, content, re.DOTALL)
            
            lines = []
            timings = []
            for match in matches:
                text = match[3].strip().replace('\n', ' ')
                if text:
                    lines.append(text)
                    timings.append(match[1][:8])  # HH:MM:SS
            
            self._current_file_lines = lines
            
            base_name = os.path.splitext(os.path.basename(path))[0]
            self._output_dir = os.path.join(os.path.dirname(path), f"{base_name}_jwt_tts")
            
            self.tbl_queue.setRowCount(1)
            self.tbl_queue.setItem(0, 0, QtWidgets.QTableWidgetItem("1"))
            self.tbl_queue.setItem(0, 1, QtWidgets.QTableWidgetItem(os.path.basename(path)))
            self.tbl_queue.setItem(0, 2, QtWidgets.QTableWidgetItem("Ready"))
            self.tbl_queue.setItem(0, 3, QtWidgets.QTableWidgetItem(f"{len(lines)} subs"))
            
            self._populate_subtitles_table(lines, timings)
            self._log(f"📜 Loaded {len(lines)} subtitles from SRT")
            
        except Exception as e:
            self._log(f"❌ Error loading SRT: {e}")
    
    def _on_queue_item_clicked(self, row: int, column: int):
        """Handle click on queue table to switch files in batch mode"""
        if not hasattr(self, '_files') or not self._files:
            return
        
        if row < 0 or row >= len(self._files):
            return
        
        # 🔧 FIX: Skip if clicking on same file
        if hasattr(self, '_current_file_index') and row == self._current_file_index:
            return
        
        # 🔧 FIX: Save status of current file before switching
        self._save_current_file_status()
        
        file_path, filename = self._files[row]
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f.read().split('\n') if l.strip()]
            
            self._current_file_lines = lines
            self._current_file_index = row
            
            # Update output dir for this file
            base_name = os.path.splitext(filename)[0]
            folder = os.path.dirname(file_path)
            self._output_dir = os.path.join(folder, f"{base_name}_jwt_tts")
            
            # Update subtitles table
            self._populate_subtitles_table(lines)
            
            # 🔧 FIX: Restore status if this file was processed before
            self._restore_file_status(file_path)
            
            # Update path display
            self.ed_folder.setText(file_path)
            
            self._log(f"📄 Switched to: {filename} ({len(lines)} lines)")
            
        except Exception as e:
            self._log(f"❌ Error loading file: {e}")
    
    def _populate_subtitles_table(self, lines: List[str], timings: List[str] = None):
        """Populate subtitles table - giống tab Chuyển văn bản
        
        Split mỗi line thành chunks (max chars from settings) và hiển thị:
        - ID dạng "1.1", "1.2" nếu có nhiều chunks
        - ID dạng "1" nếu chỉ có 1 chunk
        """
        # Get max_chars from advanced settings
        adv = self._settings.get('advanced', {})
        MAX_CHARS = adv.get('max_chars', 300)
        
        # Split tất cả lines thành chunks
        self._chunks_data = []  # [(para_idx, chunk_idx, total_chunks, text, row), ...]
        
        self.tbl_sub.setRowCount(0)
        row = 0
        
        for para_idx, text in enumerate(lines, start=1):
            text = text.strip()
            if not text:
                continue
            
            # Split text thành chunks
            chunks = self._split_text_into_chunks(text, MAX_CHARS)
            num_chunks = len(chunks)
            
            for chunk_idx, chunk_text in enumerate(chunks, start=1):
                self.tbl_sub.insertRow(row)
                
                # ID dạng "1.1", "1.2" nếu nhiều chunks, hoặc "1" nếu 1 chunk
                if num_chunks > 1:
                    display_id = f"{para_idx}.{chunk_idx}"
                else:
                    display_id = str(para_idx)
                
                # Timing từ SRT (nếu có)
                timing = "-"
                if timings and para_idx <= len(timings):
                    timing = timings[para_idx - 1]
                
                # Content hiển thị (cắt ngắn)
                display_text = chunk_text[:80] + "..." if len(chunk_text) > 80 else chunk_text
                
                self.tbl_sub.setItem(row, 0, QtWidgets.QTableWidgetItem(display_id))  # ID
                self.tbl_sub.setItem(row, 1, QtWidgets.QTableWidgetItem("-"))  # Output
                self.tbl_sub.setItem(row, 2, QtWidgets.QTableWidgetItem(timing if chunk_idx == 1 else "-"))  # Timing
                self.tbl_sub.setItem(row, 3, QtWidgets.QTableWidgetItem(str(len(chunk_text))))  # Chars
                self.tbl_sub.setItem(row, 4, QtWidgets.QTableWidgetItem(display_text))  # Content
                self.tbl_sub.setItem(row, 5, QtWidgets.QTableWidgetItem("Pending"))  # Status
                
                # Lưu chunk data
                self._chunks_data.append({
                    'para_idx': para_idx,
                    'chunk_idx': chunk_idx,
                    'total_chunks': num_chunks,
                    'text': chunk_text,
                    'row': row
                })
                
                row += 1
        
        total_chunks = len(self._chunks_data)
        # Không ghi đè lbl_result ở đây - nó được set trong _load_single_file hoặc _load_folder
        # self.lbl_result.setText(f"Kết Quả: 0/{total_chunks}")
        self._log(f"📄 Loaded {len(lines)} paragraphs → {total_chunks} chunks")
    
    def _populate_subtitles_table_batch(self, lines_with_file_info: List[Tuple[int, str]]):
        """Populate subtitles table cho batch mode - hiển thị TẤT CẢ lines từ tất cả files
        
        🔧 NEW: Mỗi chunk lưu thêm file_idx để biết thuộc file nào
        Output sẽ được tạo ra từng folder riêng cho mỗi file
        
        Args:
            lines_with_file_info: List of (file_idx, line_text)
        """
        # Get max_chars from advanced settings
        adv = self._settings.get('advanced', {})
        MAX_CHARS = adv.get('max_chars', 300)
        
        # Split tất cả lines thành chunks
        self._chunks_data = []  # Thêm file_idx vào mỗi chunk
        
        self.tbl_sub.setRowCount(0)
        row = 0
        
        # Track para_idx per file (mỗi file bắt đầu từ 1)
        current_file_idx = -1
        para_idx_in_file = 0
        
        for file_idx, text in lines_with_file_info:
            text = text.strip()
            if not text:
                continue
            
            # Reset para_idx khi chuyển sang file mới
            if file_idx != current_file_idx:
                current_file_idx = file_idx
                para_idx_in_file = 0
            
            para_idx_in_file += 1
            
            # Split text thành chunks
            chunks = self._split_text_into_chunks(text, MAX_CHARS)
            num_chunks = len(chunks)
            
            for chunk_idx, chunk_text in enumerate(chunks, start=1):
                self.tbl_sub.insertRow(row)
                
                # ID dạng "F1.1.1" (file.para.chunk) hoặc "F1.1" nếu 1 chunk
                # Hoặc đơn giản hơn: "1.1", "1.2" với file_idx ẩn
                if num_chunks > 1:
                    display_id = f"{para_idx_in_file}.{chunk_idx}"
                else:
                    display_id = str(para_idx_in_file)
                
                # Thêm prefix file number nếu có nhiều files
                if len(self._files) > 1:
                    display_id = f"F{file_idx + 1}.{display_id}"
                
                # Content hiển thị (cắt ngắn)
                display_text = chunk_text[:80] + "..." if len(chunk_text) > 80 else chunk_text
                
                self.tbl_sub.setItem(row, 0, QtWidgets.QTableWidgetItem(display_id))  # ID
                self.tbl_sub.setItem(row, 1, QtWidgets.QTableWidgetItem("-"))  # Output
                self.tbl_sub.setItem(row, 2, QtWidgets.QTableWidgetItem("-"))  # Timing
                self.tbl_sub.setItem(row, 3, QtWidgets.QTableWidgetItem(str(len(chunk_text))))  # Chars
                self.tbl_sub.setItem(row, 4, QtWidgets.QTableWidgetItem(display_text))  # Content
                self.tbl_sub.setItem(row, 5, QtWidgets.QTableWidgetItem("Pending"))  # Status
                
                # Lưu chunk data với file_idx
                self._chunks_data.append({
                    'file_idx': file_idx,  # 🔧 NEW: Thêm file_idx
                    'para_idx': para_idx_in_file,
                    'chunk_idx': chunk_idx,
                    'total_chunks': num_chunks,
                    'text': chunk_text,
                    'row': row
                })
                
                row += 1
        
        total_chunks = len(self._chunks_data)
        self._log(f"📄 Batch loaded {len(lines_with_file_info)} paragraphs → {total_chunks} chunks")
    
    def _split_text_into_chunks(self, text: str, max_chars: int) -> List[str]:
        """Split text thành chunks - CHỈ cắt tại dấu chấm (. ! ?)
        
        Logic GREEDY:
        - Tích lũy các câu cho đến khi thêm câu tiếp theo sẽ vượt max_chars
        - Khi đó mới cắt tại dấu chấm hiện tại
        - KHÔNG cắt theo dấu phẩy
        
        Ví dụ với max_chars=300:
        - Câu 1 (100 ký tự) + Câu 2 (70 ký tự) + Câu 3 (100 ký tự) = 270 < 300 → tiếp tục
        - Nếu thêm Câu 4 (150 ký tự) → 270 + 150 = 420 > 300 → cắt tại Câu 3
        """
        if len(text) <= max_chars:
            return [text]
        
        import re
        # Tách thành các câu dựa trên dấu chấm câu
        # Pattern: tìm các câu kết thúc bằng . ! ? (và các biến thể tiếng Việt/Trung)
        sentence_pattern = r'[^.!?。！？]*[.!?。！？]+'
        sentences = re.findall(sentence_pattern, text)
        
        # Phần còn lại không có dấu chấm cuối
        remaining = text
        for s in sentences:
            remaining = remaining.replace(s, '', 1)
        remaining = remaining.strip()
        
        if not sentences:
            # Không có dấu chấm -> fallback cắt tại khoảng trắng
            chunks = []
            start = 0
            while start < len(text):
                if len(text) - start <= max_chars:
                    chunk = text[start:].strip()
                    if chunk:
                        chunks.append(chunk)
                    break
                end = start + max_chars
                space_pos = text.rfind(' ', start, end)
                if space_pos > start:
                    chunks.append(text[start:space_pos].strip())
                    start = space_pos + 1
                else:
                    chunks.append(text[start:end].strip())
                    start = end
            return chunks if chunks else [text]
        
        # Logic GREEDY: tích lũy câu cho đến khi vượt max_chars
        chunks = []
        current_chunk = ""
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # Nếu thêm câu này vào sẽ vượt max_chars
            test_chunk = (current_chunk + " " + sentence).strip() if current_chunk else sentence
            
            if len(test_chunk) > max_chars:
                # Lưu chunk hiện tại (nếu có)
                if current_chunk:
                    chunks.append(current_chunk)
                # Bắt đầu chunk mới với câu này
                current_chunk = sentence
            else:
                # Thêm câu vào chunk hiện tại
                current_chunk = test_chunk
        
        # Thêm chunk cuối cùng
        if current_chunk:
            # Nếu còn phần remaining, thêm vào chunk cuối
            if remaining:
                test_final = (current_chunk + " " + remaining).strip()
                if len(test_final) <= max_chars:
                    chunks.append(test_final)
                else:
                    chunks.append(current_chunk)
                    chunks.append(remaining)
            else:
                chunks.append(current_chunk)
        elif remaining:
            chunks.append(remaining)
        
        return chunks if chunks else [text]

    # ================================================================
    # TTS PROCESSING
    # ================================================================
    def _start_tts(self):
        """Start TTS processing - giống tab Chuyển văn bản
        
        🔧 NEW: Batch mode xử lý tất cả files cùng lúc, mỗi file có output folder riêng
        🔧 NEW: Preview mode - dùng PreviewClient từ parent nếu có
        """
        # Check preview mode from parent
        preview_client = None
        if self._parent and hasattr(self._parent, 'client') and isinstance(getattr(self._parent, 'client', None), PreviewClient if PREVIEW_AVAILABLE else type(None)):
            preview_client = self._parent.client
        
        if not preview_client:
            # JWT mode - cần accounts
            service = self._get_service()
            stats = service.account_pool.get_stats()
            
            if stats['total'] == 0:
                QtWidgets.QMessageBox.warning(
                    self, "Warning",
                    "Chưa load accounts!\n\nVui lòng bấm nút 📂 Load Accounts để load file accounts."
                )
                return
        else:
            self._log("✅ [Preview Mode] Dùng PreviewClient (HSW + TokenPool)")
            service = self._get_service() if self._service else None
        
        # 🔧 NEW: Auto-load từ path input nếu chưa có chunks
        if not hasattr(self, '_chunks_data') or not self._chunks_data:
            path = self.ed_folder.text().strip()
            if path:
                # Remove quotes nếu có
                if (path.startswith('"') and path.endswith('"')) or \
                   (path.startswith("'") and path.endswith("'")):
                    path = path[1:-1]
                
                if os.path.exists(path):
                    self._log(f"🔄 Auto-loading from path: {path}")
                    if os.path.isfile(path):
                        ext = os.path.splitext(path)[1].lower()
                        if ext == '.txt':
                            self._load_single_file(path)
                        elif ext == '.srt':
                            self._load_srt_file(path)
                    elif os.path.isdir(path):
                        self._load_folder(path)
        
        # Check lại sau khi auto-load
        if not hasattr(self, '_chunks_data') or not self._chunks_data:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Chưa có text!\n\nVui lòng load file TXT hoặc SRT."
            )
            return
        
        voice_id = self.cb_voice.currentData()
        if not voice_id:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Chưa chọn voice!\n\nVui lòng Search và chọn voice."
            )
            return
        
        # ================================================================
        # 🔧 NEW: Batch mode - tạo output folders cho tất cả files
        # ================================================================
        is_batch_mode = getattr(self, '_is_folder_batch', False) and hasattr(self, '_files') and len(self._files) > 1
        
        # Map file_idx -> output_dir và tts_mini_dir
        self._file_output_dirs = {}  # {file_idx: {'output_dir': ..., 'tts_mini_dir': ...}}
        
        if is_batch_mode:
            # Batch mode: tạo folder riêng cho mỗi file
            for file_idx, (file_path, filename) in enumerate(self._files):
                base_name = os.path.splitext(filename)[0]
                folder = os.path.dirname(file_path)
                output_dir = os.path.join(folder, f"{base_name}_jwt_tts")
                tts_mini_dir = os.path.join(output_dir, "tts_mini")
                
                os.makedirs(output_dir, exist_ok=True)
                os.makedirs(tts_mini_dir, exist_ok=True)
                
                self._file_output_dirs[file_idx] = {
                    'output_dir': output_dir,
                    'tts_mini_dir': tts_mini_dir,
                    'base_name': base_name
                }
            
            self._log(f"📁 Created {len(self._file_output_dirs)} output folders for batch mode")
            
            # Set _output_dir to first file (for compatibility)
            if 0 in self._file_output_dirs:
                self._output_dir = self._file_output_dirs[0]['output_dir']
        else:
            # Single file mode
            if not self._output_dir:
                self._output_dir = os.path.join(APP_DIR, "outputs", "jwt_tts")
            os.makedirs(self._output_dir, exist_ok=True)
            
            tts_mini_dir = os.path.join(self._output_dir, "tts_mini")
            os.makedirs(tts_mini_dir, exist_ok=True)
            
            self._file_output_dirs[0] = {
                'output_dir': self._output_dir,
                'tts_mini_dir': tts_mini_dir,
                'base_name': os.path.basename(self._output_dir).replace('_jwt_tts', '')
            }
        
        # ================================================================
        # Prepare chunks data cho worker
        # ================================================================
        chunks_for_worker = []
        
        for chunk in self._chunks_data:
            file_idx = chunk.get('file_idx', 0)  # Default to 0 for single file mode
            para_idx = chunk['para_idx']
            chunk_idx = chunk['chunk_idx']
            total_chunks = chunk['total_chunks']
            text = chunk['text']
            row = chunk['row']
            
            # Get output dir for this file
            file_info = self._file_output_dirs.get(file_idx, self._file_output_dirs.get(0))
            tts_mini_dir = file_info['tts_mini_dir']
            
            # Output path cho chunk: tts_mini/{para_idx}.{chunk_idx}.mp3
            chunk_filename = f"{para_idx}.{chunk_idx}.mp3"
            chunk_output_path = os.path.join(tts_mini_dir, chunk_filename)
            
            # 🔧 NEW: Thêm file_idx vào tuple để worker biết
            chunks_for_worker.append((row, para_idx, chunk_idx, total_chunks, text, chunk_output_path, file_idx))
        
        if not chunks_for_worker:
            self._log(f"⚠️ Không có chunks để xử lý")
            return
        
        self._log(f"📄 Chuẩn bị xử lý {len(chunks_for_worker)} chunks")
        
        # Voice settings
        stab_idx = self.cb_stab.currentIndex()
        stability = [0.0, 0.5, 1.0][stab_idx]
        
        voice_settings = {
            "stability": stability,
            "similarity_boost": self.sb_sim.value() / 100.0,
            "style": self.sb_style.value() / 100.0,
            "use_speaker_boost": self.cb_boost.isChecked(),
        }
        if self.sb_speed.value() != 1.0:
            voice_settings["speed"] = self.sb_speed.value()
        
        # Get thread count from UI
        max_workers = self.sb_thread.value()
        
        # Get user_id and supabase from parent (main window)
        user_id = None
        supabase = None
        if self._parent:
            user_id = getattr(self._parent, 'current_user_id', None)
            supabase = getattr(self._parent, 'supabase', None)
        
        # 🔧 FIX: Get current file path for status tracking
        current_file_path = self._get_current_file_path() or ""
        
        # Create worker với file_output_dirs
        self._worker = JWTBatchTTSWorker(
            service=service,
            chunks=chunks_for_worker,
            voice_id=voice_id,
            model_id=self.cb_model.currentText(),
            voice_settings=voice_settings,
            max_workers=max_workers,
            output_dir=self._output_dir,
            tts_mini_dir=self._file_output_dirs.get(0, {}).get('tts_mini_dir', ''),
            user_id=user_id,
            supabase=supabase,
            file_path=current_file_path,
            file_output_dirs=self._file_output_dirs,  # 🔧 NEW: Pass all output dirs
            preview_client=preview_client  # 🔧 NEW: Preview mode
        )
        
        # Connect signals
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.file_progress.connect(self._on_file_progress)
        self._worker.signals.update_sub_table.connect(self._on_update_sub_table)
        self._worker.signals.log.connect(self._log)
        self._worker.signals.finished.connect(self._on_finished)
        
        self.bt_start.setEnabled(False)
        self.bt_stop.setEnabled(True)
        
        QThreadPool.globalInstance().start(self._worker)
        self._log(f"▶️ Started JWT TTS: {len(chunks_for_worker)} chunks, {max_workers} threads")
    
    def _merge_existing_paragraphs(self, tts_mini_dir: str):
        """Merge các file doan_*.mp3 đã có thành file tổng hợp"""
        if not os.path.exists(tts_mini_dir):
            self._log("⚠️ tts_mini folder not found")
            return
        
        try:
            # Tìm ffmpeg
            ffmpeg_path = None
            if sys.platform == 'darwin':
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for path in [os.path.join(app_dir, 'ffmpeg'), '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg']:
                    if os.path.exists(path):
                        ffmpeg_path = path
                        break
            else:
                app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                bundled = os.path.join(app_dir, 'ffmpeg.exe')
                if os.path.exists(bundled):
                    ffmpeg_path = bundled
                else:
                    ffmpeg_path = shutil.which('ffmpeg')
            
            if not ffmpeg_path:
                self._log("⚠️ FFmpeg not found - cannot merge")
                return
            
            # Tìm tất cả file doan_*.mp3
            mp3_files = []
            for f in Path(tts_mini_dir).glob("doan_*.mp3"):
                try:
                    para_num = int(f.stem.split('_')[1])
                    mp3_files.append((para_num, str(f)))
                except:
                    continue
            
            if not mp3_files:
                self._log("⚠️ No doan_*.mp3 files found")
                return
            
            mp3_files.sort(key=lambda x: x[0])
            mp3_files = [f for _, f in mp3_files]
            
            # Output path
            folder_name = os.path.basename(self._output_dir)
            if folder_name.endswith('_jwt_tts'):
                base_name = folder_name[:-8]
            else:
                base_name = folder_name
            
            output_filename = f"{base_name}.mp3"
            output_path = os.path.join(self._output_dir, output_filename)
            
            self._log(f"🔗 Merging {len(mp3_files)} paragraphs → {output_filename}")
            
            # Tạo file list
            list_file = output_path + ".txt"
            with open(list_file, 'w', encoding='utf-8') as f:
                for mp3_file in mp3_files:
                    safe_path = mp3_file.replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
            
            # Chạy ffmpeg
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffmpeg_path, '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', output_path],
                capture_output=True, text=True, timeout=120, startupinfo=si
            )
            
            try:
                os.remove(list_file)
            except:
                pass
            
            if result.returncode == 0 and os.path.exists(output_path):
                file_size = os.path.getsize(output_path) / 1024 / 1024
                self._log(f"✅ Created: {output_filename} ({file_size:.2f}MB)")
                
                # Move to parent folder
                txt_dir = os.path.dirname(self._output_dir)
                final_path = os.path.join(txt_dir, output_filename)
                try:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    shutil.move(output_path, final_path)
                    self._log(f"📁 Moved to: {final_path}")
                except Exception as e:
                    self._log(f"⚠️ Move failed: {e}")
                
                QtWidgets.QMessageBox.information(
                    self, "Hoàn thành",
                    f"Đã nối file thành công!\n\n{output_filename}"
                )
            else:
                self._log(f"❌ Merge failed: {result.stderr[:200] if result.stderr else 'Unknown'}")
                
        except Exception as e:
            self._log(f"❌ Merge error: {e}")
    
    def _stop_tts(self):
        """Stop TTS processing"""
        if self._worker:
            self._worker.stop()
        self.bt_stop.setEnabled(False)
        self._log("⏹️ Stop requested")
    
    def _open_output_folder(self):
        """Open output folder"""
        if self._output_dir and os.path.exists(self._output_dir):
            import subprocess
            if sys.platform == 'darwin':
                subprocess.run(['open', self._output_dir])
            elif sys.platform == 'win32':
                subprocess.run(['explorer', self._output_dir])
            else:
                subprocess.run(['xdg-open', self._output_dir])
        else:
            self._log("⚠️ Output folder not found")
    
    # ================================================================
    # SIGNAL HANDLERS
    # ================================================================
    def _on_progress(self, current: int, total: int, status: str):
        """Handle progress update - chỉ update progress bar, không ghi đè lbl_result"""
        # Không ghi đè lbl_result vì nó hiển thị số files hoàn thành
        # self.lbl_result.setText(f"Kết Quả: {current}/{total}")
        # 🔧 v2: Không update tbl_queue ở đây nữa - dùng _on_file_progress
        pass
    
    def _on_file_progress(self, file_idx: int, current: int, total: int):
        """🔧 v2 NEW: Handle file progress update - cập nhật tiến độ cho từng file trong batch"""
        if file_idx < 0 or file_idx >= self.tbl_queue.rowCount():
            return
        
        pct = int(current / max(1, total) * 100)
        self.tbl_queue.setItem(file_idx, 3, QtWidgets.QTableWidgetItem(f"{pct}%"))
        
        # Update status column
        if pct >= 100:
            self.tbl_queue.setItem(file_idx, 2, QtWidgets.QTableWidgetItem("Done"))
            # Highlight row
            for col in range(self.tbl_queue.columnCount()):
                item = self.tbl_queue.item(file_idx, col)
                if item:
                    item.setBackground(QtGui.QColor(200, 255, 200))
        else:
            self.tbl_queue.setItem(file_idx, 2, QtWidgets.QTableWidgetItem("Processing"))
    
    def _on_line_done(self, line_idx: int, success: bool, error: str):
        """Handle line completion"""
        row = line_idx - 1
        if row < 0 or row >= self.tbl_sub.rowCount():
            return
        
        if success:
            self.tbl_sub.setItem(row, 1, QtWidgets.QTableWidgetItem(f"doan_{line_idx}.mp3"))
            self.tbl_sub.setItem(row, 5, QtWidgets.QTableWidgetItem("Done"))  # Status cột 5
            item = self.tbl_sub.item(row, 5)
            if item:
                item.setBackground(QtGui.QColor(200, 255, 200))
        else:
            error_short = error[:15] + "..." if len(error) > 15 else error
            self.tbl_sub.setItem(row, 5, QtWidgets.QTableWidgetItem(f"Error: {error_short}"))  # Status cột 5
            item = self.tbl_sub.item(row, 5)
            if item:
                item.setBackground(QtGui.QColor(255, 200, 200))
    
    def _on_account_changed(self, email: str):
        """Handle account change notification - không dùng nữa vì bỏ cột Voice#"""
        pass  # Bỏ cột Voice# nên không cần update
    
    def _on_finished(self, success_count: int, fail_count: int):
        """Handle TTS completion
        
        🔧 NEW: Batch mode xử lý tất cả files cùng lúc, không cần auto-continue
        """
        self.bt_start.setEnabled(True)
        self.bt_stop.setEnabled(False)
        
        total = success_count + fail_count
        self._log(f"✅ Finished: {success_count}/{total} success")
        
        # 🔧 Check if this is folder batch mode
        is_folder_batch = getattr(self, '_is_folder_batch', False)
        files_total = getattr(self, '_files_total', 1)
        
        # 🔧 NEW: Batch mode xử lý tất cả files cùng lúc
        # Nên files_completed = files_total khi hoàn thành
        if is_folder_batch:
            self._files_completed = files_total
        else:
            self._files_completed = 1 if success_count > 0 else 0
        
        # Update lbl_result
        files_completed = getattr(self, '_files_completed', 0)
        self.lbl_result.setText(f"Kết Quả: {files_completed}/{files_total}")
        
        # Update queue table status for ALL files in batch mode
        if is_folder_batch:
            for i in range(self.tbl_queue.rowCount()):
                status = "Done" if success_count > 0 else "Failed"
                self.tbl_queue.setItem(i, 2, QtWidgets.QTableWidgetItem(status))
        elif self.tbl_queue.rowCount() > 0:
            status = "Done" if success_count > 0 else "Failed"
            self.tbl_queue.setItem(0, 2, QtWidgets.QTableWidgetItem(status))
        
        # Generate SRT if checkbox is checked (cho từng file trong batch mode)
        if self.cb_autosrt.isChecked() and success_count > 0:
            try:
                if is_folder_batch and hasattr(self, '_file_output_dirs'):
                    # Generate SRT cho từng file
                    for file_idx, file_info in self._file_output_dirs.items():
                        self._generate_srt_for_file(file_idx, file_info)
                else:
                    self._generate_srt()
            except Exception as e:
                self._log(f"❌ SRT generation error: {e}")
        
        # 🔧 NEW: Hiện MessageBox phù hợp
        if is_folder_batch:
            self._log(f"🎉 Batch complete: {files_total} files processed")
            QtWidgets.QMessageBox.information(
                self, "Batch Hoàn thành",
                f"Đã xử lý xong tất cả {files_total} files!\n\n"
                f"Thành công: {success_count} chunks\n"
                f"Thất bại: {fail_count} chunks\n\n"
                f"Output folder: {getattr(self, '_current_folder', self._output_dir)}"
            )
        else:
            # Single file mode
            QtWidgets.QMessageBox.information(
                self, "Hoàn thành",
                f"Đã xử lý xong!\n\n"
                f"Thành công: {success_count}\n"
                f"Thất bại: {fail_count}\n\n"
                f"Output: {self._output_dir}"
            )
    
    def _switch_to_file(self, file_index: int):
        """Switch to a specific file in batch mode (internal use)"""
        if not hasattr(self, '_files') or file_index >= len(self._files):
            return
        
        # Save current file status
        self._save_current_file_status()
        
        file_path, filename = self._files[file_index]
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f.read().split('\n') if l.strip()]
            
            self._current_file_lines = lines
            self._current_file_index = file_index
            
            # Update output dir for this file
            base_name = os.path.splitext(filename)[0]
            folder = os.path.dirname(file_path)
            self._output_dir = os.path.join(folder, f"{base_name}_jwt_tts")
            
            # Update subtitles table
            self._populate_subtitles_table(lines)
            
            # Update queue selection
            self.tbl_queue.selectRow(file_index)
            
            # Update status to Processing
            self.tbl_queue.setItem(file_index, 2, QtWidgets.QTableWidgetItem("Processing..."))
            
            # Update path display
            self.ed_folder.setText(file_path)
            
            self._log(f"📄 Switched to: {filename} ({len(lines)} lines)")
            
        except Exception as e:
            self._log(f"❌ Error switching to file: {e}")
    
    # ================================================================
    # SRT GENERATION
    # ================================================================
    def _generate_srt(self):
        """Generate SRT file from generated audio"""
        if not self._output_dir:
            self._log("⚠️ No output directory for SRT generation")
            return
        
        # doan_*.mp3 files are in output_dir (parent of tts_mini)
        parent_dir = self._output_dir
        
        ffprobe = find_ffprobe()
        if not ffprobe:
            self._log("⚠️ ffprobe not found, skipping SRT generation")
            return
        
        # Find doan_*.mp3 files in output_dir
        mp3_files = sorted(
            [f for f in Path(parent_dir).glob("doan_*.mp3")],
            key=lambda x: int(x.stem.replace('doan_', ''))
        )
        
        if not mp3_files:
            self._log("⚠️ No doan_*.mp3 files found for SRT generation")
            return
        
        # Get original text content
        parts = []
        if hasattr(self, '_chunks_data') and self._chunks_data:
            # Group chunks by para_idx to get original paragraphs
            para_texts = {}
            for chunk in self._chunks_data:
                para_idx = chunk['para_idx']
                if para_idx not in para_texts:
                    para_texts[para_idx] = []
                para_texts[para_idx].append(chunk['text'])
            
            # Merge chunks back to paragraphs
            for para_idx in sorted(para_texts.keys()):
                parts.append(' '.join(para_texts[para_idx]))
        
        # 🔧 FIX: Fallback - đọc từ file gốc nếu không có _chunks_data
        if not parts and hasattr(self, '_current_file_lines') and self._current_file_lines:
            parts = [line.strip() for line in self._current_file_lines if line.strip()]
        
        # 🔧 FIX: Fallback 2 - đọc từ file TXT gốc
        if not parts and self._files:
            try:
                current_file = self._files[self._current_file_index][0] if self._current_file_index < len(self._files) else None
                if current_file and os.path.exists(current_file):
                    with open(current_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                    parts = [line.strip() for line in content.split('\n') if line.strip()]
                    self._log(f"📄 SRT: Read {len(parts)} lines from original file")
            except Exception as e:
                self._log(f"⚠️ SRT: Could not read original file: {e}")
        
        if not parts:
            self._log("⚠️ SRT: No text content available")
            return
        
        if len(parts) != len(mp3_files):
            self._log(f"⚠️ SRT: {len(parts)} parts but {len(mp3_files)} mp3 files")
        
        # Get duration for each segment
        durations = []
        for mp3 in mp3_files:
            dur = self._get_mp3_duration_seconds(ffprobe, str(mp3))
            durations.append(dur)
        
        # Get advanced settings
        adv = self._settings.get('advanced', {})
        gap_enabled = adv.get('gap_enabled', False)
        gap_seconds = adv.get('gap_seconds', 1.3)
        gap_every = adv.get('gap_every', 5)
        
        # Output SRT path
        folder_name = os.path.basename(self._output_dir)
        if folder_name.endswith('_jwt_tts'):
            base_name = folder_name[:-8]
        else:
            base_name = folder_name
        
        srt_path = os.path.join(os.path.dirname(self._output_dir), f"{base_name}.srt")
        
        # Write SRT
        self._write_srt(srt_path, parts, durations, gap_enabled, gap_seconds, gap_every)
        self._log(f"📝 SRT created: {os.path.basename(srt_path)}")
    
    def _generate_srt_for_file(self, file_idx: int, file_info: Dict):
        """🔧 NEW: Generate SRT for a specific file in batch mode"""
        output_dir = file_info.get('output_dir')
        base_name = file_info.get('base_name', f'file_{file_idx}')
        
        if not output_dir or not os.path.exists(output_dir):
            return
        
        ffprobe = find_ffprobe()
        if not ffprobe:
            return
        
        # Find doan_*.mp3 files in output_dir
        mp3_files = sorted(
            [f for f in Path(output_dir).glob("doan_*.mp3")],
            key=lambda x: int(x.stem.replace('doan_', ''))
        )
        
        if not mp3_files:
            return
        
        # Get text content for this file from _chunks_data
        parts = []
        if hasattr(self, '_chunks_data') and self._chunks_data:
            # Group chunks by para_idx for this file
            para_texts = {}
            for chunk in self._chunks_data:
                if chunk.get('file_idx', 0) == file_idx:
                    para_idx = chunk['para_idx']
                    if para_idx not in para_texts:
                        para_texts[para_idx] = []
                    para_texts[para_idx].append(chunk['text'])
            
            # Merge chunks back to paragraphs
            for para_idx in sorted(para_texts.keys()):
                parts.append(' '.join(para_texts[para_idx]))
        
        # Fallback: read from original file
        if not parts and hasattr(self, '_files') and file_idx < len(self._files):
            try:
                file_path = self._files[file_idx][0]
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    parts = [line.strip() for line in content.split('\n') if line.strip()]
            except Exception as e:
                self._log(f"⚠️ SRT: Could not read file {file_idx}: {e}")
        
        if not parts:
            return
        
        # Get duration for each segment
        durations = []
        for mp3 in mp3_files:
            dur = self._get_mp3_duration_seconds(ffprobe, str(mp3))
            durations.append(dur)
        
        # Get advanced settings
        adv = self._settings.get('advanced', {})
        gap_enabled = adv.get('gap_enabled', False)
        gap_seconds = adv.get('gap_seconds', 1.3)
        gap_every = adv.get('gap_every', 5)
        
        # Output SRT path (cùng cấp với file TXT gốc)
        txt_dir = os.path.dirname(output_dir)
        srt_path = os.path.join(txt_dir, f"{base_name}.srt")
        
        # Write SRT
        self._write_srt(srt_path, parts, durations, gap_enabled, gap_seconds, gap_every)
        self._log(f"📝 SRT created for file {file_idx + 1}: {os.path.basename(srt_path)}")
    
    def _get_mp3_duration_seconds(self, ffprobe: str, filepath: str) -> float:
        """Get MP3 duration in seconds"""
        try:
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=10, startupinfo=si
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            self._log(f"⚠️ Duration error for {filepath}: {e}")
        
        return 0.0
    
    def _write_srt(self, srt_path: str, parts: List[str], durations: List[float],
                   gap_enabled: bool = False, gap_seconds: float = 0, gap_every: int = 5):
        """Ghi file SRT từ danh sách nội dung và thời lượng
        
        Logic: 
        - Chia text thành subtitle 50-60 ký tự (dễ đọc trên video)
        - Duration mỗi subtitle = tỉ lệ theo ký tự
        - THÊM gaps vào timeline sau mỗi gap_every segments
        
        Args:
            gap_enabled: Có bật gaps không
            gap_seconds: Số giây gap
            gap_every: Chèn gap sau mỗi N segments
        """
        def format_time(seconds: float) -> str:
            """Convert seconds to SRT time format: HH:MM:SS,mmm"""
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            millis = int((seconds - int(seconds)) * 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        
        def split_subtitle(text: str, max_chars: int = 60) -> List[str]:
            """Chia text thành subtitle 50-60 ký tự"""
            if len(text) <= max_chars:
                return [text]
            
            subtitles = []
            current = ""
            sentence_ends = '.!?。！？'
            
            words = text.split()
            for word in words:
                test = (current + " " + word).strip() if current else word
                if len(test) > max_chars:
                    if current:
                        subtitles.append(current)
                    current = word
                else:
                    current = test
                    # Cắt ở dấu câu nếu đủ dài (>40 chars)
                    if current and current[-1] in sentence_ends and len(current) >= 40:
                        subtitles.append(current)
                        current = ""
            
            if current:
                subtitles.append(current)
            
            return subtitles if subtitles else [text]
        
        current_time = 0.0
        lines = []
        subtitle_idx = 1
        
        for segment_idx, (part_text, part_duration) in enumerate(zip(parts, durations), start=1):
            # Chia part thành subtitle 50-60 ký tự
            sub_texts = split_subtitle(part_text, max_chars=60)
            
            if not sub_texts:
                continue
            
            # Lưu start time của segment này
            segment_start_time = current_time
            
            # Chia duration cho các subtitles theo tỉ lệ ký tự
            total_chars = sum(len(s) for s in sub_texts)
            
            for idx, sub_text in enumerate(sub_texts):
                # Duration tỉ lệ với số ký tự
                if total_chars > 0:
                    sub_duration = part_duration * (len(sub_text) / total_chars)
                else:
                    sub_duration = part_duration / len(sub_texts)
                
                start = current_time
                end = current_time + sub_duration
                
                # ✅ Subtitle CUỐI của segment phải kết thúc đúng tại segment boundary
                if idx == len(sub_texts) - 1:
                    end = segment_start_time + part_duration
                
                lines.append(str(subtitle_idx))
                lines.append(f"{format_time(start)} --> {format_time(end)}")
                lines.append(sub_text)
                lines.append("")  # Blank line
                
                current_time = end
                subtitle_idx += 1
            
            # ✅ THÊM GAP sau segment (nếu enabled)
            if gap_enabled and gap_seconds > 0 and gap_every > 0 and len(parts) > gap_every:
                # Thêm gap sau mỗi gap_every segments
                if segment_idx % gap_every == 0 and segment_idx < len(parts):
                    current_time += gap_seconds
        
        # Ghi ra file
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    
    def _on_update_sub_table(self, file_path: str, row: int, column_name: str, value: str):
        """Handle update_sub_table signal from worker
        
        file_path: Path của file đang được xử lý bởi worker
        row: 0-indexed row trong bảng (worker gửi trực tiếp row từ _chunks_data)
        
        🔧 FIX: Chỉ update UI nếu file_path khớp với file đang hiển thị
        Luôn update _file_status cho đúng file
        """
        # 🔧 FIX: Luôn update _file_status cho đúng file (dù có đang hiển thị hay không)
        if file_path:
            if file_path not in self._file_status:
                self._file_status[file_path] = {}
            if row not in self._file_status[file_path]:
                self._file_status[file_path][row] = {}
            self._file_status[file_path][row][column_name] = value
        
        # 🔧 FIX: Chỉ update UI nếu đang hiển thị đúng file
        current_file = self._get_current_file_path()
        if file_path and current_file and file_path != current_file:
            # Đang hiển thị file khác, không update UI
            return
        
        if row < 0 or row >= self.tbl_sub.rowCount():
            return
        
        col_map = {"Output": 1, "Timing": 2, "Status": 5}
        col = col_map.get(column_name)
        if col is not None:
            item = self.tbl_sub.item(row, col)
            if item:
                item.setText(value)
            else:
                self.tbl_sub.setItem(row, col, QtWidgets.QTableWidgetItem(value))
            
            # Set background color for status
            if column_name == "Status":
                item = self.tbl_sub.item(row, col)
                if item:
                    value_lower = value.lower()
                    if "done" in value_lower or "empty" in value_lower:
                        item.setBackground(QtGui.QColor(200, 255, 200))  # Green
                    elif "error" in value_lower or "fail" in value_lower or "stopped" in value_lower:
                        item.setBackground(QtGui.QColor(255, 200, 200))  # Red
                    elif any(x in value_lower for x in ["processing", "retry", "switch", "wait"]):
                        item.setBackground(QtGui.QColor(255, 255, 200))  # Yellow
                    else:
                        item.setBackground(QtGui.QColor(255, 255, 255))  # White


    # ================================================================
    # CACHE/RESUME FUNCTIONS
    # ================================================================
    def _check_existing_mp3_files(self, tts_mini_dir: str, total_paragraphs: int) -> dict:
        """
        Kiểm tra các file doan_X.mp3 và chunk X.Y.mp3 đã tồn tại
        Returns: {
            'existing_ids': [1, 2, 3, ...],  # Các paragraph ID đã có file doan_X.mp3 hợp lệ
            'missing_ids': [4, 5, ...],       # Các paragraph ID còn thiếu
            'existing_chunks': {1: [1,2,3], 2: [1,2]},  # Các chunk đã tồn tại theo paragraph
            'total': total_paragraphs
        }
        """
        existing_ids = []
        existing_chunks = {}  # {para_idx: [chunk_idx, ...]}
        
        self._log(f"[Cache] Scanning tts_mini_dir: {tts_mini_dir}")
        
        # 1. Scan file doan_X.mp3 cùng cấp với tts_mini (parent folder)
        parent_dir = os.path.dirname(tts_mini_dir)
        if os.path.exists(parent_dir):
            for f in Path(parent_dir).glob("doan_*.mp3"):
                try:
                    para_num = int(f.stem.split('_')[1])
                    if f.stat().st_size > 0:
                        existing_ids.append(para_num)
                except:
                    continue
            self._log(f"[Cache] Found {len(existing_ids)} completed paragraphs (doan_X.mp3)")
        
        # 2. Scan file X.Y.mp3 và X.mp3 trong tts_mini folder (không có _chunks)
        # Format: 2.mp3 (1 chunk), 1.1.mp3, 1.2.mp3 (nhiều chunks)
        if os.path.exists(tts_mini_dir):
            chunk_files_multi = list(Path(tts_mini_dir).glob("[0-9]*.[0-9]*.mp3"))  # 1.1.mp3, 1.2.mp3
            chunk_files_single = [f for f in Path(tts_mini_dir).glob("[0-9]*.mp3") if '.' not in f.stem or f.stem.count('.') == 0]  # 2.mp3
            chunk_files = chunk_files_multi + chunk_files_single
            self._log(f"[Cache] Found {len(chunk_files)} chunk files in {tts_mini_dir}")
            
            for f in chunk_files:
                try:
                    stem = f.stem
                    # Multi-chunk format: 1.1 -> para=1, chunk=1
                    if '.' in stem:
                        parts = stem.split('.')
                        if len(parts) >= 2:
                            para_num = int(parts[0])
                            chunk_num = int(parts[1])
                            if f.stat().st_size > 0:
                                if para_num not in existing_chunks:
                                    existing_chunks[para_num] = []
                                if chunk_num not in existing_chunks[para_num]:
                                    existing_chunks[para_num].append(chunk_num)
                    # Single-chunk format: 2.mp3 -> para=2, chunk=1
                    elif stem.isdigit():
                        para_num = int(stem)
                        if f.stat().st_size > 0:
                            if para_num not in existing_chunks:
                                existing_chunks[para_num] = []
                            if 1 not in existing_chunks[para_num]:
                                existing_chunks[para_num].append(1)
                except:
                    continue
            
            for para, chunks in existing_chunks.items():
                self._log(f"[Cache] Para {para}: {len(chunks)} chunks cached")
        else:
            self._log(f"[Cache] tts_mini dir not found: {tts_mini_dir}")
        
        existing_ids.sort()
        all_ids = set(range(1, total_paragraphs + 1))
        missing_ids = sorted(list(all_ids - set(existing_ids)))
        
        return {
            'existing_ids': existing_ids,
            'missing_ids': missing_ids,
            'existing_chunks': existing_chunks,
            'total': total_paragraphs
        }
    
    def _show_resume_dialog(self, base: str, existing_count: int, total: int, missing_count: int, cached_chunks: int = 0) -> str:
        """
        Hiển thị dialog hỏi user khi phát hiện file đã tồn tại
        Returns: 'continue' | 'restart' | 'merge_only' | 'cancel'
        """
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Phát hiện file đã xử lý")
        dlg.setModal(True)
        dlg.setMinimumWidth(400)
        
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(12)
        
        if missing_count == 0:
            # Tất cả đã hoàn thành
            msg = f"📁 File: {base}\n\n✅ Tất cả {total} đoạn đã được tạo trước đó."
            lbl = QtWidgets.QLabel(msg)
            lbl.setStyleSheet("font-size: 11pt;")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            
            btn_merge = QtWidgets.QPushButton("🔗 Nối file MP3")
            btn_merge.setToolTip("Chỉ nối các file đã có thành MP3 hoàn chỉnh")
            btn_restart = QtWidgets.QPushButton("🔄 Làm lại từ đầu")
            btn_restart.setToolTip("Xóa hết và tạo lại tất cả")
            btn_cancel = QtWidgets.QPushButton("❌ Hủy")
            
            btn_merge.clicked.connect(lambda: dlg.done(1))
            btn_restart.clicked.connect(lambda: dlg.done(2))
            btn_cancel.clicked.connect(lambda: dlg.done(0))
            
            btn_layout = QtWidgets.QHBoxLayout()
            btn_layout.addWidget(btn_merge)
            btn_layout.addWidget(btn_restart)
            btn_layout.addWidget(btn_cancel)
            layout.addLayout(btn_layout)
            
            result = dlg.exec()
            if result == 1:
                return 'merge_only'
            elif result == 2:
                return 'restart'
            else:
                return 'cancel'
        else:
            # Còn một số đoạn chưa hoàn thành
            if cached_chunks > 0 and existing_count == 0:
                msg = f"📁 File: {base}\n\n⏸️ Phát hiện {cached_chunks} chunks đã được tạo.\n📝 Còn cần xử lý các chunks còn lại."
            else:
                msg = f"📁 File: {base}\n\n⏸️ Phát hiện {existing_count}/{total} đoạn đã được tạo."
                if cached_chunks > 0:
                    msg += f"\n🧩 Thêm {cached_chunks} chunks đang xử lý dở."
                msg += f"\n📝 Còn {missing_count} đoạn cần xử lý."
            lbl = QtWidgets.QLabel(msg)
            lbl.setStyleSheet("font-size: 11pt;")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            
            btn_continue = QtWidgets.QPushButton(f"▶️ Tiếp tục ({missing_count} đoạn)")
            btn_continue.setToolTip("Chỉ tạo các đoạn còn thiếu")
            btn_continue.setStyleSheet("background-color: #28a745; color: white; font-weight: bold;")
            btn_restart = QtWidgets.QPushButton("🔄 Làm lại từ đầu")
            btn_restart.setToolTip("Xóa hết và tạo lại tất cả")
            btn_cancel = QtWidgets.QPushButton("❌ Hủy")
            
            btn_continue.clicked.connect(lambda: dlg.done(1))
            btn_restart.clicked.connect(lambda: dlg.done(2))
            btn_cancel.clicked.connect(lambda: dlg.done(0))
            
            btn_layout = QtWidgets.QHBoxLayout()
            btn_layout.addWidget(btn_continue)
            btn_layout.addWidget(btn_restart)
            btn_layout.addWidget(btn_cancel)
            layout.addLayout(btn_layout)
            
            result = dlg.exec()
            if result == 1:
                return 'continue'
            elif result == 2:
                return 'restart'
            else:
                return 'cancel'
    
    def _delete_existing_mp3_files(self, tts_mini_dir: str):
        """Xóa tất cả file doan_*.mp3 và chunks trong folder tts_mini"""
        if not os.path.exists(tts_mini_dir):
            return
        
        # Xóa doan_*.mp3
        for f in Path(tts_mini_dir).glob("doan_*.mp3"):
            try:
                f.unlink()
                self._log(f"[Resume] Đã xóa: {f.name}")
            except Exception as e:
                self._log(f"[Resume] Không thể xóa {f.name}: {e}")
        
        # Xóa chunks trong tts_mini folder (không có _chunks folder nữa)
        if os.path.exists(tts_mini_dir):
            for f in Path(tts_mini_dir).glob("*.mp3"):
                try:
                    f.unlink()
                except:
                    pass
            self._log(f"[Resume] Đã xóa chunks trong {tts_mini_dir}")


# ============================================================
# SETUP FUNCTION
# ============================================================
def setup_jwt_tts_tab(main_window, tab_widget: QtWidgets.QTabWidget, log_fn: Callable = None, proxy_fn: Callable = None, user_id: int = None, proxy_service=None):
    """
    Setup JWT TTS tab và thêm vào tab widget
    
    Args:
        main_window: Main window instance
        tab_widget: QTabWidget để thêm tab
        log_fn: Log function
        proxy_fn: Proxy function
        user_id: User ID để load accounts từ D1
        proxy_service: ProxyService instance để rotate proxy khi cần
    
    Returns:
        JWTTTSTab instance
    """
    tab = JWTTTSTab(parent=main_window, log_fn=log_fn, proxy_fn=proxy_fn, user_id=user_id, proxy_service=proxy_service)
    tab_widget.insertTab(1, tab, "💎 Giọng Trả Phí")  # Insert at position 1 (after first tab)
    return tab


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    tab = JWTTTSTab()
    tab.setWindowTitle("💎 Giọng Trả Phí - Demo")
    tab.resize(900, 700)
    tab.show()
    sys.exit(app.exec())

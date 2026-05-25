"""
Voice Manager Dialog for Multiple Voice Tab.
Allows users to add, edit, delete voice IDs.
Auto-tests voice when adding (with progress popup).
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import QHeaderView, QProgressDialog
import json
import os
import requests


class SingleVoiceTestWorker(QThread):
    """Worker thread for testing a single voice."""
    
    finished = Signal(bool, str, str)  # success, message, voice_name
    
    def __init__(self, voice_id: str, api_key: str, proxy_url: str = None, main_window=None):
        super().__init__()
        self.voice_id = voice_id
        self.api_key = api_key
        self.proxy_url = proxy_url
        self.main_window = main_window
        self._stop = False
        self.voice_name = ""
    
    def stop(self):
        self._stop = True
    
    def _refresh_proxy(self):
        """Xoay proxy mới khi gặp unusual_activity."""
        if self.main_window and hasattr(self.main_window, 'proxy_service_db') and self.main_window.proxy_service_db:
            try:
                fresh_proxy = self.main_window.proxy_service_db.force_refresh()
                if fresh_proxy:
                    self.proxy_url = fresh_proxy
                    print(f"[VoiceManager] 🔄 Rotated proxy: {fresh_proxy[:50]}...")
                    return True
            except Exception as e:
                print(f"[VoiceManager] ⚠️ Proxy refresh error: {e}")
        return False
    
    def run(self):
        # Step 1: Fetch voice name
        name = self._fetch_voice_name()
        if self._stop:
            return
        
        self.voice_name = name or f"Voice {self.voice_id[:8]}..."
        
        # Step 2: Test with v3 API (with retry on unusual_activity)
        max_retries = 3
        for attempt in range(max_retries):
            if self._stop:
                return
            
            success, msg = self._test_voice()
            
            # Check for unusual_activity error
            if not success and "unusual" in msg.lower():
                print(f"[VoiceManager] unusual_activity detected, attempt {attempt+1}/{max_retries}")
                if attempt < max_retries - 1:
                    # Xoay proxy và retry
                    self._refresh_proxy()
                    import time
                    time.sleep(2)
                    continue
            
            # Success or other error - stop retrying
            break
        
        self.finished.emit(success, msg, self.voice_name)
    
    def _fetch_voice_name(self) -> str:
        """Fetch voice name from API."""
        try:
            url = f"https://api.elevenlabs.io/v1/voices/{self.voice_id}"
            headers = {"xi-api-key": self.api_key}
            
            proxies = None
            if self.proxy_url:
                proxies = {"http": self.proxy_url, "https": self.proxy_url}
            
            response = requests.get(url, headers=headers, proxies=proxies, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                return data.get("name", "")
        except:
            pass
        return ""
    
    def _test_voice(self) -> tuple:
        """Test voice with text-to-dialogue v3 API."""
        url = "https://api.elevenlabs.io/v1/text-to-dialogue"
        
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "inputs": [{"text": "Hello", "voice_id": self.voice_id}],
            "model_id": "eleven_v3",
            "settings": {"stability": 0.5},
            "apply_text_normalization": "auto"
        }
        
        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        
        try:
            response = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=60)
            
            if response.status_code == 200:
                return True, "OK"
            else:
                error_msg = f"Lỗi {response.status_code}"
                try:
                    detail = response.json().get("detail", {})
                    if isinstance(detail, dict):
                        error_msg = detail.get("message", error_msg)[:80]
                    elif isinstance(detail, str):
                        error_msg = detail[:80]
                except:
                    pass
                return False, error_msg
        except Exception as e:
            return False, str(e)[:50]


class VoiceManagerDialog(QtWidgets.QDialog):
    """Dialog for managing multiple voice IDs."""
    
    voices_changed = Signal(list)
    
    def __init__(self, parent=None, main_window=None):
        super().__init__(parent)
        self.main_window = main_window
        self.voices = []
        self.test_worker = None
        self._progress_dialog = None
        self._pending_voice_data = None
        self._user_cancelled = False  # Flag to track if user actually cancelled
        self._setup_ui()
        self._load_voices()
    
    def _setup_ui(self):
        self.setWindowTitle("Quản lý Voice ID")
        self.setMinimumSize(600, 400)
        self.setModal(True)
        
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Table
        self.tbl_voices = QtWidgets.QTableWidget(0, 4)
        self.tbl_voices.setHorizontalHeaderLabels(["#", "Voice ID", "Tên", "Trạng thái"])
        self.tbl_voices.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_voices.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tbl_voices.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_voices.verticalHeader().setVisible(False)
        self.tbl_voices.setAlternatingRowColors(True)
        
        hdr = self.tbl_voices.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_voices.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_voices.setColumnWidth(3, 100)
        layout.addWidget(self.tbl_voices)
        
        # Buttons row
        btn_row1 = QtWidgets.QHBoxLayout()
        btn_row1.setSpacing(5)
        
        self.btn_add = QtWidgets.QPushButton("Thêm Voice")
        self.btn_edit = QtWidgets.QPushButton("Sửa")
        self.btn_delete = QtWidgets.QPushButton("Xóa")
        
        self.btn_add.clicked.connect(self._add_voice)
        self.btn_edit.clicked.connect(self._edit_voice)
        self.btn_delete.clicked.connect(self._delete_voice)
        
        btn_row1.addWidget(self.btn_add)
        btn_row1.addWidget(self.btn_edit)
        btn_row1.addWidget(self.btn_delete)
        btn_row1.addStretch()
        layout.addLayout(btn_row1)
        
        # Status label
        self.lbl_status = QtWidgets.QLabel("Sẵn sàng")
        layout.addWidget(self.lbl_status)
        
        # Dialog buttons
        btn_row2 = QtWidgets.QHBoxLayout()
        btn_row2.addStretch()
        
        self.btn_save = QtWidgets.QPushButton("Lưu && Đóng")
        self.btn_cancel = QtWidgets.QPushButton("Hủy")
        
        self.btn_save.clicked.connect(self._save_and_close)
        self.btn_cancel.clicked.connect(self.reject)
        
        btn_row2.addWidget(self.btn_save)
        btn_row2.addWidget(self.btn_cancel)
        layout.addLayout(btn_row2)

    def _get_config_path(self) -> str:
        import sys
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(app_dir, "dialogue_voices.json")
        print(f"[VoiceManager] Config path: {config_path}")
        return config_path
    
    def _load_voices(self):
        config_path = self._get_config_path()
        print(f"[VoiceManager] Loading from: {config_path}")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.voices = data.get("voices", [])
                    print(f"[VoiceManager] Loaded {len(self.voices)} voices: {[v.get('name') for v in self.voices]}")
            except Exception as e:
                print(f"[VoiceManager] Lỗi load voices: {e}")
                self.voices = []
        else:
            print(f"[VoiceManager] Config file not found, starting with empty list")
            self.voices = []
        self._refresh_table()
    
    def _save_voices(self):
        config_path = self._get_config_path()
        print(f"[VoiceManager] Saving {len(self.voices)} voices to: {config_path}")
        print(f"[VoiceManager] Voices: {[v.get('name') for v in self.voices]}")
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({"voices": self.voices}, f, ensure_ascii=False, indent=2)
            print(f"[VoiceManager] ✅ Đã lưu {len(self.voices)} voices")
        except Exception as e:
            print(f"[VoiceManager] ❌ Lỗi lưu voices: {e}")
    
    def _refresh_table(self):
        self.tbl_voices.setRowCount(len(self.voices))
        for i, voice in enumerate(self.voices):
            item_idx = QtWidgets.QTableWidgetItem(str(i + 1))
            item_idx.setTextAlignment(Qt.AlignCenter)
            self.tbl_voices.setItem(i, 0, item_idx)
            
            self.tbl_voices.setItem(i, 1, QtWidgets.QTableWidgetItem(voice.get("voice_id", "")))
            self.tbl_voices.setItem(i, 2, QtWidgets.QTableWidgetItem(voice.get("name", "")))
            
            status = "✓" if voice.get("tested", False) else "—"
            item_status = QtWidgets.QTableWidgetItem(status)
            item_status.setTextAlignment(Qt.AlignCenter)
            self.tbl_voices.setItem(i, 3, item_status)
        
        self.lbl_status.setText(f"Tổng: {len(self.voices)} voice(s)")

    def _get_api_key(self) -> str:
        api_key = None
        if self.main_window and hasattr(self.main_window, 'keys'):
            api_key = self.main_window.keys.cur()
        
        if not api_key:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Chưa có API key! Load key trước.")
            return None
        return api_key
    
    def _get_proxy_url(self) -> str:
        """Get proxy URL from main window's proxy_service_db."""
        proxy_url = None
        if self.main_window and hasattr(self.main_window, 'proxy_service_db') and self.main_window.proxy_service_db:
            proxy_url = self.main_window.proxy_service_db.get_current_proxy()
        return proxy_url

    def _add_voice(self):
        """Add voice with auto-test."""
        print("[VoiceManager] _add_voice() called")
        
        # Get voice ID from user
        voice_id, ok = QtWidgets.QInputDialog.getText(
            self, "Thêm Voice", 
            "Nhập Voice ID:",
            QtWidgets.QLineEdit.Normal
        )
        
        if not ok or not voice_id.strip():
            print("[VoiceManager] User cancelled or empty voice_id")
            return
        
        voice_id = voice_id.strip()
        print(f"[VoiceManager] Adding voice_id: {voice_id}")
        
        # Check duplicate
        existing_ids = [v["voice_id"] for v in self.voices]
        if voice_id in existing_ids:
            print(f"[VoiceManager] Duplicate voice_id: {voice_id}")
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Voice ID đã tồn tại!")
            return
        
        # Get API key
        api_key = self._get_api_key()
        if not api_key:
            print("[VoiceManager] No API key available")
            return
        
        proxy_url = self._get_proxy_url()
        print(f"[VoiceManager] Using proxy: {proxy_url[:50] if proxy_url else 'None'}...")
        
        # Store pending data
        self._pending_voice_data = {"voice_id": voice_id}
        self._user_cancelled = False  # Reset cancel flag
        
        # Show progress dialog
        self._progress_dialog = QProgressDialog(
            "Đang kiểm tra voice...\n(Lấy tên + Test v3 API)", 
            "Hủy", 0, 0, self
        )
        self._progress_dialog.setWindowTitle("Đang xử lý")
        self._progress_dialog.setWindowModality(Qt.WindowModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.canceled.connect(self._on_progress_canceled)
        self._progress_dialog.show()
        
        # Start test worker
        print(f"[VoiceManager] Starting test worker for {voice_id}")
        self.test_worker = SingleVoiceTestWorker(voice_id, api_key, proxy_url, main_window=self.main_window)
        self.test_worker.finished.connect(self._on_add_test_finished, Qt.QueuedConnection)
        self.test_worker.start()
    
    def _on_progress_canceled(self):
        """Called when user clicks Cancel button on progress dialog."""
        print("[VoiceManager] User clicked Cancel")
        self._user_cancelled = True
        self._cancel_test()
    
    def _cancel_test(self):
        """Cancel ongoing test - only clear data if user actually cancelled."""
        print(f"[VoiceManager] _cancel_test called, user_cancelled={self._user_cancelled}")
        if self.test_worker:
            self.test_worker.stop()
            self.test_worker.wait()
            self.test_worker = None
        # Only clear pending data if user actually cancelled
        if self._user_cancelled:
            self._pending_voice_data = None
            self._user_cancelled = False
    
    @QtCore.Slot(bool, str, str)
    def _on_add_test_finished(self, success: bool, message: str, voice_name: str):
        """Handle test result when adding voice."""
        print(f"[VoiceManager] _on_add_test_finished: success={success}, message={message}, voice_name={voice_name}")
        
        # Close progress dialog - disconnect signal first to prevent _on_progress_canceled being called
        if self._progress_dialog:
            try:
                self._progress_dialog.canceled.disconnect(self._on_progress_canceled)
            except:
                pass
            self._progress_dialog.close()
            self._progress_dialog = None
        
        self.test_worker = None
        
        if not self._pending_voice_data:
            print("[VoiceManager] No pending voice data!")
            return
        
        voice_id = self._pending_voice_data["voice_id"]
        self._pending_voice_data = None
        
        if success:
            # Add voice to list
            voice_data = {
                "voice_id": voice_id,
                "name": voice_name,
                "tested": True
            }
            self.voices.append(voice_data)
            print(f"[VoiceManager] Added voice: {voice_name} ({voice_id}), total: {len(self.voices)}")
            
            # Force UI update on main thread
            QtCore.QTimer.singleShot(0, self._do_refresh_after_add)
            self._last_added_name = voice_name
        else:
            # Show error
            print(f"[VoiceManager] Test failed: {message}")
            QtWidgets.QMessageBox.warning(
                self, "Không thể thêm Voice",
                f"Voice ID: {voice_id}\n\n"
                f"Lỗi: {message}\n\n"
                "Voice này không hoạt động với v3 API."
            )
            self.lbl_status.setText(f"❌ Thêm thất bại: {message}")
    
    def _do_refresh_after_add(self):
        """Refresh table after adding voice - called via QTimer to ensure main thread."""
        print(f"[VoiceManager] _do_refresh_after_add: {len(self.voices)} voices")
        self._refresh_table()
        self._save_voices()  # Auto-save sau khi thêm voice
        name = getattr(self, '_last_added_name', '')
        self.lbl_status.setText(f"✅ Đã thêm: {name}")
        # Force process pending events to update UI immediately
        QtWidgets.QApplication.processEvents()
    
    def _edit_voice(self):
        """Edit voice name only (no re-test)."""
        row = self.tbl_voices.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Chọn voice để sửa!")
            return
        
        voice = self.voices[row]
        current_name = voice.get("name", "")
        
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Sửa tên Voice",
            f"Voice ID: {voice['voice_id']}\n\nNhập tên mới:",
            QtWidgets.QLineEdit.Normal,
            current_name
        )
        
        if ok and new_name.strip():
            self.voices[row]["name"] = new_name.strip()
            self._refresh_table()
            self._save_voices()  # Auto-save sau khi sửa voice
            self.lbl_status.setText(f"Đã cập nhật: {new_name.strip()}")
    
    def _delete_voice(self):
        row = self.tbl_voices.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Chọn voice để xóa!")
            return
        
        voice = self.voices[row]
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận",
            f"Xóa voice '{voice.get('name', voice['voice_id'])}'?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        
        if reply == QtWidgets.QMessageBox.Yes:
            del self.voices[row]
            self._refresh_table()
            self._save_voices()  # Auto-save sau khi xóa voice
            self.lbl_status.setText("Đã xóa voice")

    def _save_and_close(self):
        self._save_voices()
        self.voices_changed.emit(self.voices)
        self.accept()
    
    def get_voices(self) -> list:
        return self.voices
    
    def closeEvent(self, event):
        if self.test_worker and self.test_worker.isRunning():
            self.test_worker.stop()
            self.test_worker.wait()
        super().closeEvent(event)

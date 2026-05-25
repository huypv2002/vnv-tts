"""
Multiple Voice Integration for Qt MainWindow (11Labs0811.py).
This is a NEW file - does not modify any existing code.

Contains methods to integrate Multiple Voice Panel into Qt MainWindow.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt
from typing import TYPE_CHECKING, List, Optional

from models.voice_entry import VoiceEntry
from services.multiple_voice_config import MultipleVoiceConfig


def setup_qt_multiple_voice_integration(main_window) -> None:
    """
    Setup multiple voice integration for Qt MainWindow.
    
    This function adds:
    - Multiple voice checkbox in Batch Job section
    - Multiple voice panel (collapsible)
    - New batch processing method for multiple voices
    
    Args:
        main_window: Qt MainWindow instance to integrate with.
    """
    print("🎭 [MULTI_VOICE] Starting Qt integration setup...")
    
    try:
        # Store config reference
        main_window._multi_voice_config = MultipleVoiceConfig()
        
        # Add toggle checkbox to Batch Job section
        _add_qt_multiple_voice_toggle(main_window)
        
        # Create multiple voice panel (initially hidden)
        _create_qt_multiple_voice_panel(main_window)
        
        # Store original start method reference
        main_window._original_start_queue = main_window.start_queue_sequential
        
        # Patch start method
        _patch_qt_start_method(main_window)
        
        print("✅ [MULTI_VOICE] Qt integration setup complete")
        
    except Exception as e:
        print(f"❌ [MULTI_VOICE] Qt integration failed: {e}")
        import traceback
        traceback.print_exc()


def _add_qt_multiple_voice_toggle(main_window) -> None:
    """Add toggle checkbox for multiple voice mode to Batch Job section."""
    try:
        # Create checkbox
        main_window.cb_multi_voice = QtWidgets.QCheckBox("🎭 Multiple Voice")
        main_window.cb_multi_voice.setChecked(main_window._multi_voice_config.enabled)
        main_window.cb_multi_voice.setToolTip("Sử dụng nhiều giọng đọc xen kẽ")
        main_window.cb_multi_voice.stateChanged.connect(
            lambda state: _on_qt_multi_voice_toggle(main_window, state)
        )
        
        # Find the result_row layout in Batch Job section
        # We need to add checkbox next to "Tự động tạo Srt"
        # The checkbox cb_autosrt is in result_row
        
        # Get parent of cb_autosrt
        if hasattr(main_window, 'cb_autosrt'):
            parent_layout = main_window.cb_autosrt.parent().layout()
            if parent_layout:
                # Insert after cb_autosrt
                idx = parent_layout.indexOf(main_window.cb_autosrt)
                if idx >= 0:
                    parent_layout.insertWidget(idx + 1, main_window.cb_multi_voice)
                    print("✅ [MULTI_VOICE] Added checkbox after 'Tự động tạo Srt'")
                    return
        
        # Fallback: Try to find grp_b (Batch Job group)
        # and add to left_widget layout
        for child in main_window.centralWidget().findChildren(QtWidgets.QGroupBox):
            if child.title() == "Batch Job":
                # Find left_widget inside
                for widget in child.findChildren(QtWidgets.QWidget):
                    layout = widget.layout()
                    if layout and isinstance(layout, QtWidgets.QVBoxLayout):
                        # Add checkbox to this layout
                        layout.insertWidget(2, main_window.cb_multi_voice)
                        print("✅ [MULTI_VOICE] Added checkbox to Batch Job (fallback)")
                        return
        
        print("⚠️ [MULTI_VOICE] Could not find suitable location for checkbox")
        
    except Exception as e:
        print(f"❌ [MULTI_VOICE] Error adding toggle: {e}")
        import traceback
        traceback.print_exc()


def _create_qt_multiple_voice_panel(main_window) -> None:
    """Create the multiple voice panel UI (Qt version)."""
    try:
        # Create panel widget
        main_window._multi_voice_panel = QtMultipleVoicePanel(main_window)
        
        # Find main layout and add panel
        central = main_window.centralWidget()
        if central and central.layout():
            root_layout = central.layout()
            
            # Create group box for the panel
            grp_multi = QtWidgets.QGroupBox("🎭 Multiple Voice (Nhiều giọng đọc)")
            grp_layout = QtWidgets.QVBoxLayout(grp_multi)
            grp_layout.addWidget(main_window._multi_voice_panel)
            
            # Store reference
            main_window._multi_voice_group = grp_multi
            
            # Insert after row 1 (Batch Job)
            root_layout.addWidget(grp_multi, 3, 0, 1, 2)
            
            # Initially hide if not enabled
            if not main_window._multi_voice_config.enabled:
                grp_multi.setVisible(False)
            
            print("✅ [MULTI_VOICE] Created panel widget")
        else:
            print("⚠️ [MULTI_VOICE] Could not find central layout")
            
    except Exception as e:
        print(f"❌ [MULTI_VOICE] Error creating panel: {e}")
        import traceback
        traceback.print_exc()


def _on_qt_multi_voice_toggle(main_window, state: int) -> None:
    """Handle multiple voice toggle change."""
    enabled = state == Qt.Checked.value if hasattr(Qt.Checked, 'value') else state == 2
    
    # Update config
    main_window._multi_voice_config.enabled = enabled
    main_window._multi_voice_config.save()
    
    # Show/hide panel
    if hasattr(main_window, '_multi_voice_group'):
        main_window._multi_voice_group.setVisible(enabled)
    
    if enabled:
        print("✅ [MULTI_VOICE] Mode ENABLED")
    else:
        print("❌ [MULTI_VOICE] Mode DISABLED")


def _patch_qt_start_method(main_window) -> None:
    """Patch the start method to support multiple voice."""
    original_start = main_window.start_queue_sequential
    
    def patched_start():
        # Check if multiple voice is enabled
        if hasattr(main_window, 'cb_multi_voice') and main_window.cb_multi_voice.isChecked():
            # Validate voice list
            if hasattr(main_window, '_multi_voice_panel'):
                voice_list = main_window._multi_voice_panel.get_voice_list()
                if len(voice_list) >= 2:
                    print("🎭 [MULTI_VOICE] Starting with multiple voices...")
                    _start_multi_voice_generation(main_window)
                    return
                else:
                    QtWidgets.QMessageBox.warning(
                        main_window,
                        "Multiple Voice",
                        "Cần ít nhất 2 voice trong danh sách!\nVui lòng thêm voice hoặc tắt Multiple Voice."
                    )
                    return
        
        # Use original method
        original_start()
    
    main_window.start_queue_sequential = patched_start
    print("✅ [MULTI_VOICE] Patched start_queue_sequential")


def _start_multi_voice_generation(main_window) -> None:
    """Start TTS generation with multiple voices."""
    # TODO: Implement multi-voice generation logic
    # For now, show info message
    voice_list = main_window._multi_voice_panel.get_voice_list()
    mode = main_window._multi_voice_panel.get_assignment_mode()
    
    QtWidgets.QMessageBox.information(
        main_window,
        "Multiple Voice",
        f"Multiple Voice mode activated!\n\n"
        f"Voices: {len(voice_list)}\n"
        f"Mode: {mode}\n\n"
        f"(Full implementation coming soon)"
    )


class QtMultipleVoicePanel(QtWidgets.QWidget):
    """
    Qt Panel for managing multiple voice entries.
    
    Layout:
    - Left: Voice list with add/remove buttons
    - Right: Voice editor
    - Bottom: Assignment mode selector
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._config = MultipleVoiceConfig()
        self._selected_index = -1
        self._voice_results = []
        
        self._setup_ui()
        self._refresh_voice_list()
    
    def _setup_ui(self):
        """Setup the panel UI."""
        main_layout = QtWidgets.QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        # === LEFT: Voice List ===
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 5, 0)
        
        # Voice list
        self.voice_list = QtWidgets.QListWidget()
        self.voice_list.setMaximumWidth(200)
        self.voice_list.currentRowChanged.connect(self._on_voice_selected)
        left_layout.addWidget(self.voice_list)
        
        # Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("+ Thêm")
        self.btn_add.clicked.connect(self.add_voice_entry)
        self.btn_remove = QtWidgets.QPushButton("- Xóa")
        self.btn_remove.clicked.connect(self.remove_voice_entry)
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        left_layout.addLayout(btn_layout)
        
        main_layout.addWidget(left_widget)
        
        # === RIGHT: Voice Editor ===
        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QFormLayout(right_widget)
        
        # Voice ID
        self.ed_voice_id = QtWidgets.QLineEdit()
        self.ed_voice_id.setPlaceholderText("Nhập Voice ID...")
        right_layout.addRow("Voice ID:", self.ed_voice_id)
        
        # Voice Name
        self.ed_voice_name = QtWidgets.QLineEdit()
        self.ed_voice_name.setPlaceholderText("Tên voice...")
        right_layout.addRow("Tên:", self.ed_voice_name)
        
        # Model
        self.cb_model = QtWidgets.QComboBox()
        self.cb_model.addItems([
            "eleven_turbo_v2_5",
            "eleven_multilingual_v2",
            "eleven_monolingual_v1",
            "eleven_v3"
        ])
        right_layout.addRow("Model:", self.cb_model)
        
        # Speed
        self.sb_speed = QtWidgets.QDoubleSpinBox()
        self.sb_speed.setRange(0.7, 1.2)
        self.sb_speed.setValue(1.0)
        self.sb_speed.setSingleStep(0.05)
        right_layout.addRow("Tốc độ:", self.sb_speed)
        
        # Stability
        self.sb_stability = QtWidgets.QSpinBox()
        self.sb_stability.setRange(0, 100)
        self.sb_stability.setValue(50)
        self.sb_stability.setSuffix(" %")
        right_layout.addRow("Ổn định:", self.sb_stability)
        
        # Similarity
        self.sb_similarity = QtWidgets.QSpinBox()
        self.sb_similarity.setRange(0, 100)
        self.sb_similarity.setValue(75)
        self.sb_similarity.setSuffix(" %")
        right_layout.addRow("Tương đồng:", self.sb_similarity)
        
        # Apply button
        self.btn_apply = QtWidgets.QPushButton("Áp dụng")
        self.btn_apply.clicked.connect(self._apply_settings)
        right_layout.addRow("", self.btn_apply)
        
        main_layout.addWidget(right_widget)
        
        # === BOTTOM: Assignment Mode ===
        bottom_widget = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QHBoxLayout(bottom_widget)
        
        bottom_layout.addWidget(QtWidgets.QLabel("Chế độ:"))
        
        self.rb_alternating = QtWidgets.QRadioButton("Xen kẽ")
        self.rb_alternating.setChecked(True)
        self.rb_sequential = QtWidgets.QRadioButton("Tuần tự")
        self.rb_per_file = QtWidgets.QRadioButton("Theo file")
        
        bottom_layout.addWidget(self.rb_alternating)
        bottom_layout.addWidget(self.rb_sequential)
        bottom_layout.addWidget(self.rb_per_file)
        bottom_layout.addStretch()
        
        # Save/Clear buttons
        self.btn_save = QtWidgets.QPushButton("Lưu")
        self.btn_save.clicked.connect(self._save_config)
        self.btn_clear = QtWidgets.QPushButton("Xóa tất cả")
        self.btn_clear.clicked.connect(self._clear_config)
        bottom_layout.addWidget(self.btn_save)
        bottom_layout.addWidget(self.btn_clear)
        
        # Add bottom to main layout
        main_layout.addWidget(bottom_widget)
    
    def _refresh_voice_list(self):
        """Refresh the voice list from config."""
        self.voice_list.clear()
        for entry in self._config.voice_entries:
            display = f"{entry.voice_name} ({entry.voice_id[:8]}...)"
            self.voice_list.addItem(display)
    
    def _on_voice_selected(self, index: int):
        """Handle voice selection."""
        self._selected_index = index
        if index >= 0:
            entry = self._config.get_entry(index)
            if entry:
                self._load_entry_to_editor(entry)
    
    def _load_entry_to_editor(self, entry: VoiceEntry):
        """Load entry into editor fields."""
        self.ed_voice_id.setText(entry.voice_id)
        self.ed_voice_name.setText(entry.voice_name)
        
        # Set model
        idx = self.cb_model.findText(entry.model)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)
        
        self.sb_speed.setValue(entry.speed)
        self.sb_stability.setValue(int(entry.stability * 100))
        self.sb_similarity.setValue(int(entry.similarity_boost * 100))
    
    def _apply_settings(self):
        """Apply editor settings to selected entry."""
        if self._selected_index < 0:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng chọn voice để cập nhật!")
            return
        
        voice_id = self.ed_voice_id.text().strip()
        if not voice_id:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Voice ID không được để trống!")
            return
        
        entry = VoiceEntry(
            voice_id=voice_id,
            voice_name=self.ed_voice_name.text().strip() or "Unnamed",
            model=self.cb_model.currentText(),
            stability=self.sb_stability.value() / 100.0,
            similarity_boost=self.sb_similarity.value() / 100.0,
            style=0,
            speed=self.sb_speed.value(),
            use_speaker_boost=False
        )
        
        if self._config.update_entry(self._selected_index, entry):
            self._config.save()
            self._refresh_voice_list()
            self.voice_list.setCurrentRow(self._selected_index)
            print(f"✅ Updated voice: {entry.voice_name}")
    
    def add_voice_entry(self):
        """Add a new voice entry."""
        if self._config.add_entry():
            self._config.save()
            self._refresh_voice_list()
            # Select new entry
            self.voice_list.setCurrentRow(self._config.get_entry_count() - 1)
        else:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Đã đạt tối đa 10 voice!")
    
    def remove_voice_entry(self):
        """Remove selected voice entry."""
        if self._selected_index < 0:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng chọn voice để xóa!")
            return
        
        if self._config.remove_entry(self._selected_index):
            self._config.save()
            self._refresh_voice_list()
            # Select previous or first
            new_idx = min(self._selected_index, self._config.get_entry_count() - 1)
            if new_idx >= 0:
                self.voice_list.setCurrentRow(new_idx)
        else:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Cần tối thiểu 2 voice!")
    
    def _save_config(self):
        """Save configuration."""
        # Update mode
        if self.rb_alternating.isChecked():
            self._config.assignment_mode = "alternating"
        elif self.rb_sequential.isChecked():
            self._config.assignment_mode = "sequential"
        else:
            self._config.assignment_mode = "per_file"
        
        if self._config.save():
            QtWidgets.QMessageBox.information(self, "Thành công", "Đã lưu cấu hình!")
    
    def _clear_config(self):
        """Clear configuration."""
        reply = QtWidgets.QMessageBox.question(
            self, "Xác nhận",
            "Xóa toàn bộ cấu hình multiple voice?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self._config.clear()
            self._refresh_voice_list()
            self._selected_index = -1
    
    def get_voice_list(self) -> List[VoiceEntry]:
        """Get current voice list."""
        return self._config.voice_entries
    
    def get_assignment_mode(self) -> str:
        """Get current assignment mode."""
        if self.rb_alternating.isChecked():
            return "alternating"
        elif self.rb_sequential.isChecked():
            return "sequential"
        return "per_file"

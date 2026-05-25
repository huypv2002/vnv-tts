"""
Multiple Voice Panel UI Component.
This is a NEW file - does not modify any existing code.

Provides UI for managing multiple voices in TTS processing.
Cloned structure from VoicePanel but with list management capabilities.
"""
from __future__ import annotations

import ttkbootstrap as tb
from tkinter import ttk
from typing import List, Optional, Callable

from models.voice_entry import VoiceEntry
from services.multiple_voice_config import MultipleVoiceConfig


class MultipleVoicePanel:
    """
    Panel for managing multiple voice entries.
    
    Layout:
    - Left: Voice list (Treeview) with add/remove buttons
    - Right: Voice editor (cloned from VoicePanel settings)
    - Bottom: Assignment mode selector and action buttons
    """
    
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent, style='Gray.TFrame')
        
        # Initialize config service
        self._config = MultipleVoiceConfig()
        
        # Track selected voice index
        self._selected_index: int = -1
        
        # Voice search results cache
        self._voice_results: List[dict] = []
        
        # Main container
        main_container = tb.Frame(self.frame, style='Gray.TFrame')
        main_container.pack(fill="both", expand=True)
        
        # =====================================================
        # LEFT PANEL: Voice List
        # =====================================================
        left_frame = tb.Labelframe(
            main_container, 
            text="Danh sách Voice", 
            padding=10, 
            style="Bordered.TLabelframe",
            width=250
        )
        left_frame.pack(side="left", fill="y", padx=(0, 10))
        left_frame.pack_propagate(False)
        
        # Voice list (Treeview)
        list_frame = tb.Frame(left_frame, style='Gray.TFrame')
        list_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        # Treeview columns
        columns = ("name", "voice_id")
        self.voice_tree = ttk.Treeview(
            list_frame, 
            columns=columns, 
            show="headings",
            selectmode="browse",
            height=6
        )
        self.voice_tree.heading("name", text="Tên Voice")
        self.voice_tree.heading("voice_id", text="Voice ID")
        self.voice_tree.column("name", width=120)
        self.voice_tree.column("voice_id", width=100)
        self.voice_tree.pack(side="left", fill="both", expand=True)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.voice_tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.voice_tree.configure(yscrollcommand=scrollbar.set)
        
        # Bind selection event
        self.voice_tree.bind("<<TreeviewSelect>>", self._on_voice_selected)
        
        # List action buttons
        btn_frame = tb.Frame(left_frame, style='Gray.TFrame')
        btn_frame.pack(fill="x")
        
        self.add_btn = tb.Button(
            btn_frame, 
            text="+ Thêm", 
            width=8, 
            bootstyle="success-outline",
            command=self.add_voice_entry
        )
        self.add_btn.pack(side="left", padx=(0, 5))
        
        self.remove_btn = tb.Button(
            btn_frame, 
            text="- Xóa", 
            width=8, 
            bootstyle="danger-outline",
            command=self.remove_voice_entry
        )
        self.remove_btn.pack(side="left")
        
        # =====================================================
        # RIGHT PANEL: Voice Editor (cloned from VoicePanel)
        # =====================================================
        right_frame = tb.Labelframe(
            main_container, 
            text="Cấu hình Voice", 
            padding=10, 
            style="Bordered.TLabelframe"
        )
        right_frame.pack(side="right", fill="both", expand=True)
        
        # Search row
        search_row = tb.Frame(right_frame, style='Gray.TFrame')
        search_row.pack(fill="x", pady=(0, 10))
        
        tb.Label(search_row, text="Tìm kiếm:").pack(side="left")
        self.search_entry = tb.Entry(search_row)
        self.search_entry.pack(side="left", padx=(5, 5), fill="x", expand=True)
        
        self.search_btn = tb.Button(
            search_row, 
            text="Tìm", 
            width=6, 
            bootstyle="secondary-outline",
            command=self._on_search
        )
        self.search_btn.pack(side="left")
        
        # Voice selection row
        voice_row = tb.Frame(right_frame, style='Gray.TFrame')
        voice_row.pack(fill="x", pady=(0, 5))
        
        tb.Label(voice_row, text="Giọng:").pack(side="left")
        self.voice_combo = ttk.Combobox(voice_row, state="readonly")
        self.voice_combo.pack(side="left", padx=(5, 0), fill="x", expand=True)
        self.voice_combo.bind("<<ComboboxSelected>>", self._on_voice_combo_selected)
        
        # Model row
        model_row = tb.Frame(right_frame, style='Gray.TFrame')
        model_row.pack(fill="x", pady=(0, 10))
        
        tb.Label(model_row, text="Mô hình:").pack(side="left")
        self.model_combo = ttk.Combobox(model_row, state="readonly")
        self.model_combo.set("eleven_turbo_v2_5")
        self.model_combo["values"] = [
            "eleven_turbo_v2_5",
            "eleven_multilingual_v2",
            "eleven_monolingual_v1"
        ]
        self.model_combo.pack(side="left", padx=(5, 0), fill="x", expand=True)
        
        # Voice settings grid
        settings_frame = tb.Frame(right_frame, style='Gray.TFrame')
        settings_frame.pack(fill="x", pady=(0, 10))
        
        # Row 1: Speed & Style
        row1 = tb.Frame(settings_frame, style='Gray.TFrame')
        row1.pack(fill="x", pady=(0, 5))
        
        tb.Label(row1, text="Tốc độ:").pack(side="left")
        self.speed_var = tb.DoubleVar(value=1.0)
        tb.Spinbox(row1, from_=0.7, to=1.2, increment=0.05, textvariable=self.speed_var, width=5).pack(side="left", padx=(5, 20))
        
        tb.Label(row1, text="Kiểu:").pack(side="left")
        self.style_var = tb.IntVar(value=0)
        style_box = tb.Frame(row1, style='Gray.TFrame')
        style_box.pack(side="left")
        tb.Spinbox(style_box, from_=0, to=100, textvariable=self.style_var, width=5).pack(side="left")
        tb.Label(style_box, text="%").pack(side="left", padx=(2, 0))
        
        # Row 2: Stability & Similarity
        row2 = tb.Frame(settings_frame, style='Gray.TFrame')
        row2.pack(fill="x", pady=(0, 5))
        
        tb.Label(row2, text="Ổn định:").pack(side="left")
        self.stability_var = tb.IntVar(value=50)
        stab_box = tb.Frame(row2, style='Gray.TFrame')
        stab_box.pack(side="left", padx=(5, 20))
        tb.Spinbox(stab_box, from_=0, to=100, textvariable=self.stability_var, width=5).pack(side="left")
        tb.Label(stab_box, text="%").pack(side="left", padx=(2, 0))
        
        tb.Label(row2, text="Tương đồng:").pack(side="left")
        self.similarity_var = tb.IntVar(value=75)
        sim_box = tb.Frame(row2, style='Gray.TFrame')
        sim_box.pack(side="left")
        tb.Spinbox(sim_box, from_=0, to=100, textvariable=self.similarity_var, width=5).pack(side="left")
        tb.Label(sim_box, text="%").pack(side="left", padx=(2, 0))
        
        # Row 3: Speaker boost
        row3 = tb.Frame(settings_frame, style='Gray.TFrame')
        row3.pack(fill="x")
        
        self.speaker_boost_var = tb.BooleanVar(value=False)
        tb.Checkbutton(
            row3, 
            text="Tăng cường loa", 
            variable=self.speaker_boost_var,
            style='TCheckbutton'
        ).pack(side="left")
        
        # Apply button
        apply_btn = tb.Button(
            row3,
            text="Áp dụng",
            width=8,
            bootstyle="primary-outline",
            command=self._apply_settings_to_entry
        )
        apply_btn.pack(side="right")
        
        # =====================================================
        # BOTTOM: Assignment Mode & Actions
        # =====================================================
        bottom_frame = tb.Frame(self.frame, style='Gray.TFrame')
        bottom_frame.pack(fill="x", pady=(10, 0))
        
        # Assignment mode
        mode_frame = tb.Frame(bottom_frame, style='Gray.TFrame')
        mode_frame.pack(side="left")
        
        tb.Label(mode_frame, text="Chế độ gán:").pack(side="left")
        self.mode_var = tb.StringVar(value="alternating")
        
        modes = [
            ("Xen kẽ", "alternating"),
            ("Tuần tự", "sequential"),
            ("Theo file", "per_file")
        ]
        
        for text, value in modes:
            tb.Radiobutton(
                mode_frame,
                text=text,
                variable=self.mode_var,
                value=value,
                style='TRadiobutton'
            ).pack(side="left", padx=(10, 0))
        
        # Action buttons
        action_frame = tb.Frame(bottom_frame, style='Gray.TFrame')
        action_frame.pack(side="right")
        
        self.save_config_btn = tb.Button(
            action_frame,
            text="Lưu cấu hình",
            width=12,
            bootstyle="success-outline",
            command=self.save_config
        )
        self.save_config_btn.pack(side="left", padx=(0, 5))
        
        self.clear_config_btn = tb.Button(
            action_frame,
            text="Xóa cấu hình",
            width=12,
            bootstyle="danger-outline",
            command=self.clear_config
        )
        self.clear_config_btn.pack(side="left")
        
        # Load initial data
        self._refresh_voice_list()
        self._load_mode_from_config()
    
    # =====================================================
    # Voice List Management
    # =====================================================
    
    def add_voice_entry(self) -> bool:
        """Add a new voice entry with default settings."""
        if self._config.add_entry():
            self._config.save()
            self._refresh_voice_list()
            # Select the new entry
            count = self._config.get_entry_count()
            if count > 0:
                self._select_entry_by_index(count - 1)
            return True
        else:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning(f"Đã đạt tối đa 10 voice entries!")
            return False
    
    def remove_voice_entry(self) -> bool:
        """Remove the selected voice entry."""
        if self._selected_index < 0:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Vui lòng chọn voice để xóa!")
            return False
        
        if self._config.remove_entry(self._selected_index):
            self._config.save()
            self._refresh_voice_list()
            # Select previous entry or first
            new_count = self._config.get_entry_count()
            if new_count > 0:
                new_idx = min(self._selected_index, new_count - 1)
                self._select_entry_by_index(new_idx)
            else:
                self._selected_index = -1
                self._clear_editor()
            return True
        else:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning(f"Cần tối thiểu 2 voice entries!")
            return False
    
    def _refresh_voice_list(self) -> None:
        """Refresh the voice list treeview from config."""
        # Clear existing items
        for item in self.voice_tree.get_children():
            self.voice_tree.delete(item)
        
        # Add entries from config
        for entry in self._config.voice_entries:
            voice_id_short = entry.voice_id[:12] + "..." if len(entry.voice_id) > 12 else entry.voice_id
            self.voice_tree.insert("", "end", values=(entry.voice_name, voice_id_short))
    
    def _select_entry_by_index(self, index: int) -> None:
        """Select an entry in the treeview by index."""
        children = self.voice_tree.get_children()
        if 0 <= index < len(children):
            item_id = children[index]
            self.voice_tree.selection_set(item_id)
            self.voice_tree.focus(item_id)
            self._on_voice_selected(None)
    
    def _on_voice_selected(self, event) -> None:
        """Handle voice selection in treeview."""
        selection = self.voice_tree.selection()
        if not selection:
            self._selected_index = -1
            return
        
        # Get index of selected item
        item_id = selection[0]
        children = self.voice_tree.get_children()
        self._selected_index = children.index(item_id)
        
        # Load entry into editor
        entry = self._config.get_entry(self._selected_index)
        if entry:
            self._load_entry_to_editor(entry)
    
    def _load_entry_to_editor(self, entry: VoiceEntry) -> None:
        """Load a VoiceEntry into the editor controls."""
        # Set voice combo (just display name)
        self.voice_combo.config(state="normal")
        self.voice_combo.set(entry.voice_name)
        self.voice_combo.config(state="readonly")
        
        # Set model
        self.model_combo.config(state="normal")
        self.model_combo.set(entry.model)
        self.model_combo.config(state="readonly")
        
        # Set parameters
        self.speed_var.set(entry.speed)
        self.style_var.set(int(entry.style * 100))
        self.stability_var.set(int(entry.stability * 100))
        self.similarity_var.set(int(entry.similarity_boost * 100))
        self.speaker_boost_var.set(entry.use_speaker_boost)
        
        # Store voice_id in search entry for reference
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, entry.voice_id)
    
    def _clear_editor(self) -> None:
        """Clear the editor controls."""
        self.voice_combo.config(state="normal")
        self.voice_combo.set("")
        self.voice_combo.config(state="readonly")
        
        self.search_entry.delete(0, "end")
        
        self.speed_var.set(1.0)
        self.style_var.set(0)
        self.stability_var.set(50)
        self.similarity_var.set(75)
        self.speaker_boost_var.set(False)
    
    def _apply_settings_to_entry(self) -> None:
        """Apply current editor settings to selected entry."""
        if self._selected_index < 0:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Vui lòng chọn voice để cập nhật!")
            return
        
        # Get current values from editor
        voice_id = self.search_entry.get().strip()
        voice_name = self.voice_combo.get().strip()
        model = self.model_combo.get().strip()
        
        if not voice_id:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Voice ID không được để trống!")
            return
        
        # Create updated entry
        entry = VoiceEntry(
            voice_id=voice_id,
            voice_name=voice_name or "Unnamed Voice",
            model=model or "eleven_turbo_v2_5",
            stability=self.stability_var.get() / 100.0,
            similarity_boost=self.similarity_var.get() / 100.0,
            style=self.style_var.get() / 100.0,
            speed=self.speed_var.get(),
            use_speaker_boost=self.speaker_boost_var.get()
        )
        
        # Update config
        if self._config.update_entry(self._selected_index, entry):
            self._config.save()
            self._refresh_voice_list()
            self._select_entry_by_index(self._selected_index)
            print(f"✅ Updated voice entry: {entry.voice_name}")
        else:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error("Không thể cập nhật voice entry!")
    
    # =====================================================
    # Voice Search
    # =====================================================
    
    def _on_search(self) -> None:
        """Handle voice search."""
        query = self.search_entry.get().strip()
        if not query:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Vui lòng nhập Voice ID hoặc tên để tìm kiếm!")
            return
        
        # Get API key from main window
        root = self.frame.winfo_toplevel()
        main_win = getattr(root, "_main_window_ref", None)
        if not main_win:
            return
        
        api_key = main_win.get_next_api_key() if hasattr(main_win, 'get_next_api_key') else None
        if not api_key:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error("Không có API key khả dụng!")
            return
        
        try:
            from services.voice_service import VoiceService
            svc = VoiceService()
            results = svc.search_voices_by_id_or_name(api_key, query)
            
            if not results:
                from ttkbootstrap.dialogs import Messagebox
                Messagebox.show_info("Không tìm thấy voice phù hợp!")
                return
            
            # Store results
            self._voice_results = results
            
            # Populate combo
            items = []
            for r in results:
                label = r.get('name', 'Unknown')
                if not r.get("free_users_allowed", True):
                    label += " (Paid)"
                items.append(label)
            
            self.voice_combo.config(state="normal")
            self.voice_combo["values"] = items
            self.voice_combo.set(items[0])
            self.voice_combo.config(state="readonly")
            
            # Auto-select first result
            self._on_voice_combo_selected(None)
            
            print(f"🔍 Found {len(results)} voices for '{query}'")
            
        except Exception as e:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error(f"Lỗi tìm kiếm: {e}")
    
    def _on_voice_combo_selected(self, event) -> None:
        """Handle voice selection from search results."""
        selection = self.voice_combo.get().strip()
        if not selection or not self._voice_results:
            return
        
        # Find matching result
        sel_name = selection.replace(" (Paid)", "").strip()
        match = next((v for v in self._voice_results if v.get("name") == sel_name), None)
        
        if not match:
            return
        
        # Check if paid
        if not match.get("free_users_allowed", True):
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Voice này yêu cầu trả phí!")
            return
        
        # Update search entry with voice_id
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, match.get("voice_id", ""))
        
        # Load settings if available
        if "settings" in match and match["settings"]:
            settings = match["settings"]
            if "stability" in settings:
                self.stability_var.set(int(float(settings["stability"]) * 100))
            if "similarity_boost" in settings:
                self.similarity_var.set(int(float(settings["similarity_boost"]) * 100))
            if "style" in settings:
                self.style_var.set(int(float(settings["style"]) * 100))
            if "use_speaker_boost" in settings:
                self.speaker_boost_var.set(bool(settings["use_speaker_boost"]))
    
    # =====================================================
    # Configuration
    # =====================================================
    
    def save_config(self) -> None:
        """Save current configuration."""
        # Update assignment mode
        self._config.assignment_mode = self.mode_var.get()
        
        if self._config.save():
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_info("Đã lưu cấu hình multiple voice!")
        else:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error("Không thể lưu cấu hình!")
    
    def clear_config(self) -> None:
        """Clear configuration and reset to defaults."""
        from ttkbootstrap.dialogs import Messagebox
        if Messagebox.okcancel("Xóa toàn bộ cấu hình multiple voice?", "Xác nhận"):
            self._config.clear()
            self._refresh_voice_list()
            self._clear_editor()
            self._load_mode_from_config()
            Messagebox.show_info("Đã xóa cấu hình!")
    
    def _load_mode_from_config(self) -> None:
        """Load assignment mode from config."""
        self.mode_var.set(self._config.assignment_mode)
    
    # =====================================================
    # Public API
    # =====================================================
    
    def get_voice_list(self) -> List[VoiceEntry]:
        """Get the current list of voice entries."""
        return self._config.voice_entries
    
    def get_assignment_mode(self) -> str:
        """Get the current assignment mode."""
        return self.mode_var.get()
    
    def is_enabled(self) -> bool:
        """Check if multiple voice mode is enabled in config."""
        return self._config.enabled
    
    def set_enabled(self, enabled: bool) -> None:
        """Set multiple voice mode enabled state."""
        self._config.enabled = enabled
        self._config.save()

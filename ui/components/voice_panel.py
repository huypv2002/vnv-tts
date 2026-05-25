from __future__ import annotations

import ttkbootstrap as tb
from tkinter import ttk


class VoicePanel:
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent, style='Gray.TFrame')
        
        # Main container: 2 separate bordered panels
        main_container = tb.Frame(self.frame, style='Gray.TFrame')
        main_container.pack(fill="both", expand=True)
        
        # -------------------------------------------------------------
        # PANEL 1: Voice Selection (Left) - Fixed Width
        # -------------------------------------------------------------
        selection_frame = tb.Labelframe(main_container, text="Giọng đọc", padding=10, style="Bordered.TLabelframe", width=300)
        selection_frame.pack(side="left", fill="y", padx=(0, 10))
        selection_frame.pack_propagate(False) # Force fixed width
        
        # Row 1: Name + Search
        row1 = tb.Frame(selection_frame, style='Gray.TFrame')
        row1.pack(fill="x", pady=(0, 5))
        tb.Label(row1, text="Tên:").pack(side="left")
        self.name_entry = tb.Entry(row1)
        self.name_entry.pack(side="left", padx=(5, 5), fill="x", expand=True)
        search_btn = tb.Button(row1, text="Tìm kiếm", width=8, bootstyle="secondary-outline", command=self.on_search)
        search_btn.pack(side="left")
        
        # Row 2: Voice
        row2 = tb.Frame(selection_frame, style='Gray.TFrame')
        row2.pack(fill="x", pady=(0, 5))
        tb.Label(row2, text="Giọng:").pack(side="left")
        self.voice_combo = ttk.Combobox(row2, state="normal")
        self.voice_combo.set("")
        self.voice_combo.pack(side="left", padx=(5, 0), fill="x", expand=True)
        self.voice_combo.bind("<<ComboboxSelected>>", self.on_voice_selected)
        self.voice_combo.bind("<<ComboboxSelected>>", self.on_load_saved_voice_settings, add='+')
        
        # Row 3: Model
        row3 = tb.Frame(selection_frame, style='Gray.TFrame')
        row3.pack(fill="x")
        tb.Label(row3, text="Mô hình:").pack(side="left")
        self.model_combo = ttk.Combobox(row3, state="readonly")
        self.model_combo.set("eleven_turbo_v2_5")
        self.model_combo.pack(side="left", padx=(5, 0), fill="x", expand=True)
        
        # -------------------------------------------------------------
        # PANEL 2: Voice Settings (Right)
        # -------------------------------------------------------------
        settings_frame = tb.Labelframe(main_container, text="Cấu hình giọng đọc", padding=10, style="Bordered.TLabelframe")
        settings_frame.pack(side="right", fill="both", expand=True, padx=(0, 0))
        
        # Checkboxes 
        checkbox_row = tb.Frame(settings_frame, style='Gray.TFrame')
        checkbox_row.pack(fill="x", pady=(0, 5))
        self.change_settings_var = tb.BooleanVar(value=True)
        tb.Checkbutton(checkbox_row, text="Thay đổi cấu hình", variable=self.change_settings_var, style='TCheckbutton').pack(side="left")
        self.speaker_boost_var = tb.BooleanVar(value=False)
        tb.Checkbutton(checkbox_row, text="Tăng cường loa", variable=self.speaker_boost_var, style='TCheckbutton').pack(side="left", padx=(10, 0))
        
        # Grid Layout for Settings
        # Configure Grid columns
        # Labels take natural width. Inputs take remaining space evenly.
        settings_frame.columnconfigure(0, weight=0) # Label 1 (Fixed)
        settings_frame.columnconfigure(1, weight=1) # Input 1 (Expand)
        settings_frame.columnconfigure(2, weight=0) # Label 2 (Fixed)
        settings_frame.columnconfigure(3, weight=1) # Input 2 (Expand)
        
        # Grid Container
        grid_frame = tb.Frame(settings_frame, style='Gray.TFrame')
        grid_frame.pack(fill="x", pady=(0, 5))
        
        # Row 1: Speed & Style
        # Speed
        tb.Label(grid_frame, text="Tốc độ:").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=5)
        self.speed_var = tb.DoubleVar(value=1.00)
        tb.Spinbox(grid_frame, from_=0.7, to=1.2, increment=0.05, textvariable=self.speed_var, width=5).grid(row=0, column=1, sticky="w", padx=(0, 20), pady=5)
        
        # Style
        tb.Label(grid_frame, text="Kiểu:").grid(row=0, column=2, sticky="w", padx=(0, 5), pady=5)
        self.style_var = tb.IntVar(value=0)
        style_box = tb.Frame(grid_frame, style='Gray.TFrame')
        style_box.grid(row=0, column=3, sticky="w", pady=5) # sticky w to keep it tight? No, let's keep it 'w' as spinbox is fixed width.
        tb.Spinbox(style_box, from_=0, to=100, textvariable=self.style_var, width=5).pack(side="left")
        tb.Label(style_box, text="%").pack(side="left", padx=(2, 0))

        # Row 2: Stability & Similarity
        # Stability
        tb.Label(grid_frame, text="Ổn định:").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=5)
        self.stability_var = tb.IntVar(value=50)
        stab_box = tb.Frame(grid_frame, style='Gray.TFrame')
        stab_box.grid(row=1, column=1, sticky="w", padx=(0, 20), pady=5)
        tb.Spinbox(stab_box, from_=0, to=100, textvariable=self.stability_var, width=5).pack(side="left")
        tb.Label(stab_box, text="%").pack(side="left", padx=(2, 0))
        
        # Similarity
        tb.Label(grid_frame, text="Độ tương đồng:").grid(row=1, column=2, sticky="w", padx=(0, 5), pady=5)
        self.similarity_var = tb.IntVar(value=75)
        sim_box = tb.Frame(grid_frame, style='Gray.TFrame')
        sim_box.grid(row=1, column=3, sticky="w", pady=5)
        tb.Spinbox(sim_box, from_=0, to=100, textvariable=self.similarity_var, width=5).pack(side="left")
        tb.Label(sim_box, text="%").pack(side="left", padx=(2, 0))
        
        # Row 3: Buttons (Reset & Download)
        btn_row = tb.Frame(settings_frame, style='Gray.TFrame')
        btn_row.pack(fill="x", pady=(5, 0))
        
        reset_btn = tb.Button(btn_row, text="Mặc định", width=8, bootstyle="secondary-outline", command=self.on_reset_style)
        reset_btn.pack(side="left", padx=(0, 10))
        
        download_btn = tb.Button(btn_row, text="Tải", width=6, bootstyle="secondary-outline", command=self.on_download_model)
        download_btn.pack(side="left")
    
    def on_download_model(self):
        """Handle download model (placeholder)"""
        pass

        # Internal state for selected voice search results
        self._voice_results = []

        # Auto-load models on app start (best-effort, silent on failure)
        self.frame.after(200, self._load_models_startup)
        
        # Setup auto-save callbacks
        self.frame.after(500, self._setup_auto_save_callbacks)

    # UI Callbacks
    def on_search(self):
        from ui.main_window import MainWindow  # avoid circular on import time
        # Find root MainWindow via master chain
        root = self.frame.winfo_toplevel()
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win is None:
            return
        # Get an API key only when needed
        api_key = main_win.get_next_api_key()
        if not api_key:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error("Không có API key khả dụng để tìm voice.")
            return

        # Query value: prefer Name field content; fallback to current Voice text
        query = self.name_entry.get().strip() or self.voice_combo.get().strip()
        if not query:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Vui lòng nhập Voice Id/Name vào ô Name để tìm kiếm.")
            return

        # Perform search (synchronously for now)
        try:
            from services.voice_service import VoiceService
            svc = VoiceService()
            # Use enhanced search that tries both shared voices and direct lookup
            results = svc.search_voices_by_id_or_name(api_key, query)
            
            # Debug: Print results
            print(f"🔍 Search results for '{query}': {len(results)} found")
            for i, r in enumerate(results):
                print(f"   {i+1}. {r.get('name', 'No name')} (ID: {r.get('voice_id', 'No ID')}, Free: {r.get('free_users_allowed', 'Unknown')})")
            
            # Populate Voice combobox with name only
            items = []
            for r in results:
                label = r['name']
                if not r.get("free_users_allowed", True):
                    label += "  (Paid)"
                items.append(label)
            self._voice_results = results
            if items:
                # Temporarily make combobox writable to set values
                self.voice_combo.config(state="normal")
                self.voice_combo["values"] = items
                self.voice_combo.set(items[0])
                self.voice_combo.config(state="readonly")  # Make it readonly again
                first = results[0]
                # If first result is paid, warn and do not load into fields
                if not first.get("free_users_allowed", True):
                    from ttkbootstrap.dialogs import Messagebox
                    Messagebox.show_warning(
                        "Voice này yêu cầu trả phí (free_users_allowed = false). Không thể nạp vào Voice Id/Model hoặc lưu.")
                    # Clear any previous selection display
                    self.voice_combo.set("")
                    return
                # Auto load global models list and fill by name
                names = svc.list_models(api_key)
                if names:
                    # Temporarily make model combobox writable to set values
                    self.model_combo.config(state="normal")
                    self.model_combo["values"] = names
                    if not self.model_combo.get().strip():
                        self.model_combo.set(names[0])
                    self.model_combo.config(state="readonly")
                # Set Name field to the selected voice_id
                self.name_entry.delete(0, "end")
                self.name_entry.insert(0, first["voice_id"])
                
                # Load voice settings if available
                if "settings" in first and first["settings"]:
                    settings = first["settings"]
                    print(f"🎛️ Loading voice settings from API: {settings}")
                    
                    # Update voice parameter controls with settings from API
                    if "stability" in settings:
                        self.stability_var.set(int(float(settings["stability"]) * 100))
                    if "similarity_boost" in settings:
                        self.similarity_var.set(int(float(settings["similarity_boost"]) * 100))
                    if "style" in settings:
                        self.style_var.set(int(float(settings["style"]) * 100))
                    if "use_speaker_boost" in settings:
                        self.speaker_boost_var.set(bool(settings["use_speaker_boost"]))
                    if "speed" in settings:
                        try:
                            val = float(settings.get("speed", 1.0))
                            self.speed_var.set(max(0.7, min(1.2, val)))
                        except Exception:
                            self.speed_var.set(1.0)
                
                # Store the first result for auto-save purposes
                if not hasattr(self, '_voice_results') or not self._voice_results:
                    self._voice_results = [first]
                
                # Trigger auto-save after successful search
                self._trigger_auto_save()
        except Exception as e:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error(f"Lỗi tìm voice: {e}")

    def on_voice_selected(self, event=None):
        from ui.main_window import MainWindow
        root = self.frame.winfo_toplevel()
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win is None:
            return
        # When user selects a voice, update models and Name (voice_id)
        selection = self.voice_combo.get().strip()
        if not selection or not self._voice_results:
            return
        # Match by name (remove " (Paid)" if present)
        sel_name = selection.replace("  (Paid)", "").strip()
        match = next((v for v in self._voice_results if v.get("name") == sel_name), None)
        if not match:
            return
        # Warn and block assignment if voice requires payment
        if not match.get("free_users_allowed", True):
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning(
                "Voice này yêu cầu trả phí (free_users_allowed = false). Không thể nạp vào Voice Id/Model hoặc lưu.")
            # Do not change fields for paid voices
            return

        # Auto load global models list and fill by name
        try:
            from services.voice_service import VoiceService
            api_key = main_win.get_next_api_key()
            if api_key:
                names = VoiceService().list_models(api_key)
                if names:
                    # Temporarily make model combobox writable to set values
                    self.model_combo.config(state="normal")
                    self.model_combo["values"] = names
                    if not self.model_combo.get().strip():
                        self.model_combo.set(names[0])
                    self.model_combo.config(state="readonly")
        except Exception:
            pass
        # Set Name to voice_id
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, match.get("voice_id", ""))

    def on_save_selection(self) -> None:
        from ui.main_window import MainWindow
        root = self.frame.winfo_toplevel()
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win is None:
            return
        voice_display = self.voice_combo.get().strip()
        model = self.model_combo.get().strip()
        if not voice_display or not model:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Vui lòng chọn Voice và Model trước khi lưu.")
            return
        # Extract voice_id before " - "
        voice_id = voice_display.split(" - ", 1)[0]
        # Prevent saving paid voices
        current_id = self.name_entry.get().strip()
        match = next((v for v in (self._voice_results or []) if v.get("voice_id") == current_id), None)
        if match and not match.get("free_users_allowed", True):
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning(
                "Voice này yêu cầu trả phí (free_users_allowed = false). Không thể lưu vào thư viện.")
            return
        # Gather advanced settings from controls (convert % to 0-1 range)
        try:
            stability = float(self.stability_var.get()) / 100.0
        except Exception:
            stability = 0.0
        try:
            similarity = float(self.similarity_var.get()) / 100.0
        except Exception:
            similarity = 1.0
        try:
            style = float(self.style_var.get()) / 100.0
        except Exception:
            style = 0.0
        try:
            speed = float(self.speed_var.get())
            speed = max(0.7, min(1.2, speed))
        except Exception:
            speed = 1.0
        
        # Get speaker boost value directly
        boost = self.speaker_boost_var.get()
        
        print(f"💾 Saving voice settings - Stability: {stability}, Similarity: {similarity}, Style: {style}, Boost: {boost} (type: {type(boost)})")
        
        # Get voice name from search results
        voice_name = ""
        if match:
            voice_name = match.get("name", "")
        
        try:
            # Force ensure boost is the correct value
            print(f"🔍 About to save: boost={boost}, type={type(boost)}")
            
            main_win._settings.add_voice(
                voice_id, model,
                name=voice_name,
                stability=stability,
                similarity_boost=similarity,
                style=style,
                use_speaker_boost=boost,
                speed=speed
            )
            
            # Verify what was saved
            for v in main_win._settings._settings.voices:
                if v.voice_id == voice_id and v.model == model:
                    print(f"✅ Verified in memory: boost={v.use_speaker_boost}, type={type(v.use_speaker_boost)}")
                    break
            
            # Refresh library in main window
            main_win.refresh_voice_library()
            
            # DON'T restore - let it load from saved file to verify
            # This will show if the save worked correctly
            
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_info(f"Đã lưu voice vào thư viện.\nSpeaker Boost: {boost}")
        except Exception as e:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error(f"Lỗi khi lưu voice: {e}")

    def on_clear_library(self) -> None:
        from ui.main_window import MainWindow
        root = self.frame.winfo_toplevel()
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win is None:
            return
        from ttkbootstrap.dialogs import Messagebox
        if Messagebox.okcancel("Xóa toàn bộ thư viện voice đã lưu?", "Xác nhận"):
            try:
                main_win._settings.clear_voices()
                # refresh UI list
                main_win.refresh_voice_library()
                Messagebox.show_info("Đã xóa thư viện voice.")
            except Exception as e:
                Messagebox.show_error(f"Lỗi khi xóa thư viện: {e}")

    def on_reset_style(self) -> None:
        """Reset Style to 0%"""
        self.style_var.set(0)
        print("🔄 Style reset to 0%")
    
    def on_load_settings(self) -> None:
        """Load voice settings from saved library"""
        self.on_load_saved_voice_settings()
    
    def on_load_saved_voice_settings(self, event=None) -> None:
        """Load voice settings when selecting from saved library"""
        from ui.main_window import MainWindow
        root = self.frame.winfo_toplevel()
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if not main_win:
            return
        
        voice_display = self.voice_combo.get().strip()
        if not voice_display:
            return
        
        # Find matching saved voice
        for v in getattr(main_win, '_saved_voices', []):
            if v.name == voice_display or v.voice_id == voice_display:
                # Load all settings
                try:
                    self.stability_var.set(int(v.stability * 100))
                    self.similarity_var.set(int(v.similarity_boost * 100))
                    self.style_var.set(int(v.style * 100))
                    try:
                        self.speed_var.set(max(0.7, min(1.2, float(getattr(v, "speed", 1.0)))))
                    except Exception:
                        self.speed_var.set(1.0)
                    # Set speaker boost explicitly
                    self.speaker_boost_var.set(bool(v.use_speaker_boost))
                    print(f"📥 Loaded settings for {voice_display}: stability={v.stability}, similarity={v.similarity_boost}, style={v.style}, speed={getattr(v, 'speed', 1.0)}, boost={v.use_speaker_boost} (type: {type(v.use_speaker_boost)})")
                except Exception as e:
                    print(f"❌ Error loading settings: {e}")
                    import traceback
                    traceback.print_exc()
                break
    
    def _load_models_startup(self) -> None:
        # Load models once when app starts; do not show errors to avoid noisy UI
        try:
            from ui.main_window import MainWindow
            root = self.frame.winfo_toplevel()
            main_win: MainWindow = getattr(root, "_main_window_ref", None)
            if main_win is None:
                return
            api_key = main_win.get_next_api_key()
            if not api_key:
                return
            from services.voice_service import VoiceService
            names = VoiceService().list_models(api_key)
            if names:
                # Temporarily make model combobox writable to set values
                self.model_combo.config(state="normal")
                self.model_combo["values"] = names
                if not self.model_combo.get().strip():
                    self.model_combo.set(names[0])
                self.model_combo.config(state="readonly")
        except Exception:
            # Silent on startup
            pass

    def open_advanced_settings(self) -> None:
        import ttkbootstrap as tb
        from ui.main_window import MainWindow
        root = self.frame.winfo_toplevel()
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        popup = tb.Toplevel(self.frame)
        popup.title("Advance Settings")
        popup.resizable(False, False)
        popup.geometry("560x300")

        container = tb.Frame(popup, padding=10)
        container.pack(fill="both", expand=True)

        # Row1
        row1 = tb.Frame(container)
        row1.pack(fill="x", pady=(0, 5))
        # Load current settings
        adv = main_win._settings.get_advanced_settings() if main_win else {}
        # Mặc định: tất cả checkbox trong popup = False nếu chưa có cấu hình
        var_seg = tb.BooleanVar(value=adv.get("pause_between_segments_enabled", False))
        tb.Checkbutton(row1, text="Ngắt âm giữa các đoạn (", variable=var_seg).pack(side="left")
        sp_gap = tb.Spinbox(row1, from_=0.0, to=5.0, increment=0.1, width=5)
        sp_gap.set(adv.get("segment_gap_seconds", 1.3))
        sp_gap.pack(side="left")
        tb.Label(row1, text=") (s)  Cách nhau ").pack(side="left")
        sp_count = tb.Spinbox(row1, from_=1, to=20, width=5)
        sp_count.set(adv.get("segment_count", 5))
        sp_count.pack(side="left")
        tb.Label(row1, text=" đoạn").pack(side="left")

        # Row2
        row2 = tb.Frame(container)
        row2.pack(fill="x", pady=(0, 5))
        var_srt = tb.BooleanVar(value=adv.get("srt_split_enabled", False))
        tb.Checkbutton(row2, text="Ngắt âm theo file srt", variable=var_srt).pack(side="left")

        # Row3
        row3 = tb.Frame(container)
        row3.pack(fill="x", pady=(0, 5))
        var_char = tb.BooleanVar(value=adv.get("per_char_enabled", False))
        tb.Checkbutton(row3, text="Ngắt âm theo ký tự", variable=var_char).pack(side="left")
        tb.Label(row3, text="Ký tự ,").pack(side="left", padx=(10, 4))
        sp_char1 = tb.Spinbox(row3, from_=0.0, to=2.0, increment=0.1, width=5)
        sp_char1.set(adv.get("comma_pause", 0.3))
        sp_char1.pack(side="left")
        tb.Label(row3, text="(s)").pack(side="left", padx=(6, 12))
        tb.Label(row3, text="Ký tự .").pack(side="left", padx=(4, 4))
        sp_char2 = tb.Spinbox(row3, from_=0.0, to=2.0, increment=0.1, width=5)
        sp_char2.set(adv.get("dot_pause", 0.5))
        sp_char2.pack(side="left")
        tb.Label(row3, text="(s)").pack(side="left")

        # Row4: download type
        row4 = tb.Frame(container)
        row4.pack(fill="x", pady=(0, 5))
        tb.Label(row4, text="Download Type : ").pack(side="left")
        from tkinter import ttk
        cb_dl = ttk.Combobox(row4, state="readonly", width=18)
        cb_dl["values"] = ["1 <ORIGINAL>", "2 <LOW>", "3 <MID>", "4 <HIGH>"]
        cb_dl.set(adv.get("download_type", "1 <ORIGINAL>"))
        cb_dl.pack(side="left")

        # Row5: max chars per line
        row5 = tb.Frame(container)
        row5.pack(fill="x", pady=(0, 10))
        tb.Label(row5, text="Số ký tự tối đa / dòng").pack(side="left")
        sp_max = tb.Spinbox(row5, from_=10, to=5000, width=8)
        sp_max.set(adv.get("max_chars_per_line", 1000))
        sp_max.pack(side="left", padx=(6, 0))

        # Save button
        btn_row = tb.Frame(container)
        btn_row.pack(fill="x")
        def save_and_close():
            try:
                data = {
                    "pause_between_segments_enabled": bool(var_seg.get()),
                    "segment_gap_seconds": float(sp_gap.get()),
                    "segment_count": int(sp_count.get()),
                    "srt_split_enabled": bool(var_srt.get()),
                    "per_char_enabled": bool(var_char.get()),
                    "comma_pause": float(sp_char1.get()),
                    "dot_pause": float(sp_char2.get()),
                    "download_type": cb_dl.get(),
                    "max_chars_per_line": int(sp_max.get()),
                }
                if main_win:
                    main_win._settings.save_advanced_settings(data)
            finally:
                popup.destroy()

        tb.Button(btn_row, text="Lưu", bootstyle="success", width=10, command=save_and_close).pack(side="right")
    
    def _setup_auto_save_callbacks(self) -> None:
        """Setup auto-save callbacks for all controls"""
        try:
            # Bind change events to trigger auto-save
            self.name_entry.bind('<KeyRelease>', self._trigger_auto_save)
            self.name_entry.bind('<FocusOut>', self._trigger_auto_save)
            
            self.voice_combo.bind('<<ComboboxSelected>>', self._trigger_auto_save, add='+')
            self.model_combo.bind('<<ComboboxSelected>>', self._trigger_auto_save, add='+')
            
            # Voice parameters - use trace for StringVar/IntVar changes
            self.stability_var.trace('w', lambda *args: self._trigger_auto_save())
            self.similarity_var.trace('w', lambda *args: self._trigger_auto_save())
            self.style_var.trace('w', lambda *args: self._trigger_auto_save())
            self.speaker_boost_var.trace('w', lambda *args: self._trigger_auto_save())
            
            print("🔄 Voice panel auto-save callbacks setup completed")
            
        except Exception as e:
            print(f"❌ Error setting up auto-save callbacks: {e}")
    
    def _trigger_auto_save(self, event=None) -> None:
        """Trigger auto-save with debounce"""
        try:
            from ui.main_window import MainWindow
            root = self.frame.winfo_toplevel()
            main_win: MainWindow = getattr(root, "_main_window_ref", None)
            
            if main_win and hasattr(main_win, 'save_current_config'):
                # Cancel previous save timer if exists
                if hasattr(self, '_save_timer_id'):
                    try:
                        root.after_cancel(self._save_timer_id)
                    except:
                        pass
                
                # Schedule new save with 2 second delay (debounce)
                self._save_timer_id = root.after(2000, lambda: main_win.save_current_config())
                
        except Exception as e:
            print(f"❌ Error triggering auto-save: {e}")


from __future__ import annotations

import ttkbootstrap as tb
from tkinter import ttk
from tkinter import filedialog
import os
from typing import Optional, Dict


class DubbingPanel:
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent)
        self.selected_file = ""
        self.dubbing_projects = []  # Store dubbing projects for monitoring
        
        # Main container with padding
        container = tb.Frame(self.frame, padding=10)
        container.pack(fill="both", expand=True)
        
        # Title
        title_label = tb.Label(
            container, 
            text="ElevenLabs Dubbing - Lồng tiếng video/audio tự động",
            font=("Arial", 14, "bold"),
            bootstyle="primary"
        )
        title_label.pack(pady=(0, 20))
        
        # File Upload Section
        file_frame = tb.Labelframe(container, text="File Upload", padding=10)
        file_frame.pack(fill="x", pady=(0, 15))
        
        # File selection row
        file_row = tb.Frame(file_frame)
        file_row.pack(fill="x", pady=(0, 10))
        
        tb.Label(file_row, text="File video/audio:").pack(side="left")
        self.file_entry = tb.Entry(file_row, width=50)
        self.file_entry.pack(side="left", padx=(5, 5), fill="x", expand=True)
        
        browse_btn = tb.Button(
            file_row, 
            text="Chọn file", 
            command=self.browse_file,
            bootstyle="secondary"
        )
        browse_btn.pack(side="left")
        
        # URL input (alternative to file)
        url_row = tb.Frame(file_frame)
        url_row.pack(fill="x")
        
        tb.Label(url_row, text="Hoặc URL:").pack(side="left")
        self.url_entry = tb.Entry(url_row, width=60)
        self.url_entry.pack(side="left", padx=(5, 0), fill="x", expand=True)
        
        # Bind URL entry to update watermark display
        self.url_entry.bind('<KeyRelease>', lambda e: self._update_watermark_display())
        self.url_entry.bind('<FocusOut>', lambda e: self._update_watermark_display())
        
        # Project Settings Section
        settings_frame = tb.Labelframe(container, text="Cài đặt dự án", padding=10)
        settings_frame.pack(fill="x", pady=(0, 15))
        
        # Row 1: Name and Languages
        row1 = tb.Frame(settings_frame)
        row1.pack(fill="x", pady=(0, 10))
        
        tb.Label(row1, text="Tên dự án:").pack(side="left")
        self.name_entry = tb.Entry(row1, width=25)
        self.name_entry.pack(side="left", padx=(5, 15))
        
        tb.Label(row1, text="Ngôn ngữ nguồn:").pack(side="left")
        self.source_lang_combo = ttk.Combobox(row1, width=15, state="readonly")
        self.source_lang_combo['values'] = [
            "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", 
            "nl", "cs", "ar", "zh", "ja", "hu", "ko", "hi", "vi"
        ]
        self.source_lang_combo.set("en")
        self.source_lang_combo.pack(side="left", padx=(5, 15))
        
        tb.Label(row1, text="Ngôn ngữ đích:").pack(side="left")
        self.target_lang_combo = ttk.Combobox(row1, width=15, state="readonly")
        self.target_lang_combo['values'] = [
            "vi", "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", 
            "nl", "cs", "ar", "zh", "ja", "hu", "ko", "hi"
        ]
        self.target_lang_combo.set("vi")
        self.target_lang_combo.pack(side="left", padx=(5, 0))
        
        # Row 2: Speakers and Accent
        row2 = tb.Frame(settings_frame)
        row2.pack(fill="x", pady=(0, 10))
        
        tb.Label(row2, text="Số người nói:").pack(side="left")
        self.speakers_var = tb.IntVar(value=1)
        speakers_spin = tb.Spinbox(row2, from_=1, to=10, textvariable=self.speakers_var, width=8)
        speakers_spin.pack(side="left", padx=(5, 15))
        
        tb.Label(row2, text="Giọng đích:").pack(side="left")
        self.target_accent_entry = tb.Entry(row2, width=20)
        self.target_accent_entry.pack(side="left", padx=(5, 15))
        
        tb.Label(row2, text="Mode:").pack(side="left")
        self.mode_combo = ttk.Combobox(row2, width=12, state="readonly")
        self.mode_combo['values'] = ["automatic", "manual"]
        self.mode_combo.set("automatic")
        self.mode_combo.pack(side="left", padx=(5, 0))
        
        # Advanced Options Section
        advanced_frame = tb.Labelframe(container, text="Tùy chọn nâng cao", padding=10)
        advanced_frame.pack(fill="x", pady=(0, 15))
        
        # Checkboxes row 1
        check_row1 = tb.Frame(advanced_frame)
        check_row1.pack(fill="x", pady=(0, 5))
        
        self.watermark_var = tb.BooleanVar(value=False)  # Default False
        self.watermark_check = tb.Checkbutton(
            check_row1, 
            text="Watermark (auto-detect)", 
            variable=self.watermark_var,
            state="disabled"  # Will be auto-managed
        )
        self.watermark_check.pack(side="left", padx=(0, 20))
        
        self.highest_resolution_var = tb.BooleanVar(value=True)
        tb.Checkbutton(
            check_row1, 
            text="Độ phân giải cao", 
            variable=self.highest_resolution_var
        ).pack(side="left", padx=(0, 20))
        
        self.drop_background_var = tb.BooleanVar(value=False)
        tb.Checkbutton(
            check_row1, 
            text="Bỏ âm nền", 
            variable=self.drop_background_var
        ).pack(side="left", padx=(0, 20))
        
        # Checkboxes row 2
        check_row2 = tb.Frame(advanced_frame)
        check_row2.pack(fill="x", pady=(0, 10))
        
        self.profanity_filter_var = tb.BooleanVar(value=False)
        tb.Checkbutton(
            check_row2, 
            text="Lọc từ tục tĩu", 
            variable=self.profanity_filter_var
        ).pack(side="left", padx=(0, 20))
        
        self.dubbing_studio_var = tb.BooleanVar(value=False)
        tb.Checkbutton(
            check_row2, 
            text="Dubbing Studio", 
            variable=self.dubbing_studio_var
        ).pack(side="left", padx=(0, 20))
        
        self.disable_voice_cloning_var = tb.BooleanVar(value=False)
        tb.Checkbutton(
            check_row2, 
            text="Tắt voice cloning", 
            variable=self.disable_voice_cloning_var
        ).pack(side="left", padx=(0, 20))
        
        # Time Range Section
        time_row = tb.Frame(advanced_frame)
        time_row.pack(fill="x")
        
        tb.Label(time_row, text="Thời gian bắt đầu (s):").pack(side="left")
        self.start_time_var = tb.IntVar()
        tb.Spinbox(
            time_row, 
            from_=0, 
            to=36000, 
            textvariable=self.start_time_var, 
            width=8
        ).pack(side="left", padx=(5, 15))
        
        tb.Label(time_row, text="Thời gian kết thúc (s):").pack(side="left")
        self.end_time_var = tb.IntVar()
        tb.Spinbox(
            time_row, 
            from_=0, 
            to=36000, 
            textvariable=self.end_time_var, 
            width=8
        ).pack(side="left", padx=(5, 15))
        
        tb.Label(time_row, text="CSV FPS:").pack(side="left")
        self.csv_fps_var = tb.DoubleVar(value=25.0)
        tb.Spinbox(
            time_row, 
            from_=1.0, 
            to=60.0, 
            increment=0.1,
            textvariable=self.csv_fps_var, 
            width=8
        ).pack(side="left", padx=(5, 0))
        
        # Control Buttons Section
        control_frame = tb.Frame(container)
        control_frame.pack(fill="x", pady=(15, 0))
        
        # Left side - main action button
        self.start_btn = tb.Button(
            control_frame,
            text="🎬 Bắt đầu Dubbing",
            command=self.start_dubbing,
            bootstyle="success",
            width=20
        )
        self.start_btn.pack(side="left")
        
        # Right side - utility buttons  
        btn_right = tb.Frame(control_frame)
        btn_right.pack(side="right")
        
        refresh_btn = tb.Button(
            btn_right,
            text="🔄 Refresh",
            command=self.refresh_projects,
            bootstyle="info-outline",
            width=12
        )
        refresh_btn.pack(side="right", padx=(5, 0))
        
        clear_btn = tb.Button(
            btn_right,
            text="🗑 Clear",
            command=self.clear_form,
            bootstyle="secondary-outline", 
            width=12
        )
        clear_btn.pack(side="right", padx=(5, 0))
        
        # Status and Progress Section
        status_frame = tb.Labelframe(container, text="Trạng thái và kết quả", padding=10)
        status_frame.pack(fill="both", expand=True, pady=(15, 0))
        
        # Status display
        self.status_label = tb.Label(
            status_frame, 
            text="Sẵn sàng tạo dự án dubbing mới",
            font=("Arial", 10)
        )
        self.status_label.pack(pady=(0, 10))
        
        # Progress bar
        self.progress = tb.Progressbar(
            status_frame, 
            mode='indeterminate',
            bootstyle="success-striped"
        )
        self.progress.pack(fill="x", pady=(0, 10))
        
        # Results text area
        self.result_text = tb.ScrolledText(
            status_frame,
            height=8,
            wrap="word"
        )
        self.result_text.pack(fill="both", expand=True)
        
        # Add some initial help text
        help_text = """
📖 HƯỚNG DẪN SỬ DỤNG DUBBING:

1. Chọn file video/audio hoặc nhập URL
2. Đặt tên cho dự án
3. Chọn ngôn ngữ nguồn và ngôn ngữ đích
4. Cấu hình các tùy chọn nâng cao (nếu cần)
5. Nhấn "Bắt đầu Dubbing" để bắt đầu

✨ Lưu ý: 
   - Quá trình dubbing có thể mất vài phút tùy vào độ dài video/audio
   - 🇨🇦 Dubbing sử dụng Canada proxy (tránh phát hiện unusual activity)
   - 📁 Hỗ trợ: MP4, AVI, MOV, MKV, MP3, WAV, FLAC, AAC
   - 🏷️ Watermark tự động: Video=ON, Audio=OFF (để tránh lỗi API)
   - 💡 File audio không hỗ trợ watermark (hạn chế của ElevenLabs API)
   - 🛡️ Canada proxy giúp tránh lỗi "detected_unusual_activity"
        """
        self.result_text.insert("1.0", help_text)
    
    def browse_file(self) -> None:
        """Browse and select video/audio file"""
        try:
            filetypes = [
                ("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv *.flv"),
                ("Audio files", "*.mp3 *.wav *.flac *.aac *.ogg"),
                ("All files", "*.*")
            ]
            
            file_path = filedialog.askopenfilename(
                title="Chọn file video hoặc audio",
                filetypes=filetypes
            )
            
            if file_path:
                self.selected_file = file_path
                self.file_entry.delete(0, "end")
                self.file_entry.insert(0, file_path)
                
                # Auto-generate project name from filename
                filename = os.path.splitext(os.path.basename(file_path))[0]
                if not self.name_entry.get().strip():
                    self.name_entry.delete(0, "end")
                    self.name_entry.insert(0, f"Dubbing_{filename}")
                
                self.log_message(f"✅ Đã chọn file: {os.path.basename(file_path)}")
                
                # Update watermark display based on file type
                self._update_watermark_display()
                
        except Exception as e:
            self.log_message(f"❌ Lỗi chọn file: {e}")
    
    def start_dubbing(self) -> None:
        """Start dubbing process"""
        try:
            # Validate inputs
            if not self._validate_inputs():
                return
            
            self.start_btn.config(state="disabled", text="Đang xử lý...")
            self.progress.start()
            self.log_message("🚀 Bắt đầu quá trình dubbing...")
            
            # Get dubbing parameters
            params = self._get_dubbing_params()
            self.log_message(f"📋 Tham số: {params}")
            
            # This method will be replaced by MainWindow integration
            # If not replaced, show error
            self.log_message("❌ Service chưa được kết nối! Vui lòng khởi động từ MainWindow.")
            self.start_btn.config(state="normal", text="🎬 Bắt đầu Dubbing")
            self.progress.stop()
            
        except Exception as e:
            self.log_message(f"❌ Lỗi khởi tạo dubbing: {e}")
            self.start_btn.config(state="normal", text="🎬 Bắt đầu Dubbing")
            self.progress.stop()
    
    def _validate_inputs(self) -> bool:
        """Validate user inputs"""
        file_path = self.file_entry.get().strip()
        url = self.url_entry.get().strip()
        
        if not file_path and not url:
            self.log_message("❌ Vui lòng chọn file hoặc nhập URL")
            return False
        
        if file_path and not os.path.exists(file_path):
            self.log_message("❌ File không tồn tại")
            return False
        
        if not self.name_entry.get().strip():
            self.log_message("❌ Vui lòng nhập tên dự án")
            return False
        
        if not self.source_lang_combo.get():
            self.log_message("❌ Vui lòng chọn ngôn ngữ nguồn")
            return False
        
        if not self.target_lang_combo.get():
            self.log_message("❌ Vui lòng chọn ngôn ngữ đích")
            return False
        
        return True
    
    def _get_dubbing_params(self) -> Dict:
        """Get dubbing parameters from form"""
        # Auto-detect watermark based on file type
        is_video = self._is_video_file()
        auto_watermark = is_video  # Only allow watermark for video files
        
        self.log_message(f"🎬 File type: {'Video' if is_video else 'Audio'}")
        self.log_message(f"🏷️ Watermark: {'Enabled' if auto_watermark else 'Disabled'} (auto-detected)")
        
        params = {
            "name": self.name_entry.get().strip(),
            "source_lang": self.source_lang_combo.get(),
            "target_lang": self.target_lang_combo.get(),
            "num_speakers": self.speakers_var.get(),
            "watermark": auto_watermark,  # Auto-detected based on file type
            "highest_resolution": self.highest_resolution_var.get(),
            "drop_background_audio": self.drop_background_var.get(),
            "use_profanity_filter": self.profanity_filter_var.get(),
            "dubbing_studio": self.dubbing_studio_var.get(),
            "disable_voice_cloning": self.disable_voice_cloning_var.get(),
            "mode": self.mode_combo.get(),
        }
        
        # Optional parameters
        if self.url_entry.get().strip():
            params["source_url"] = self.url_entry.get().strip()
        
        if self.target_accent_entry.get().strip():
            params["target_accent"] = self.target_accent_entry.get().strip()
        
        if self.start_time_var.get() > 0:
            params["start_time"] = self.start_time_var.get()
        
        if self.end_time_var.get() > 0:
            params["end_time"] = self.end_time_var.get()
        
        if self.csv_fps_var.get() != 25.0:
            params["csv_fps"] = self.csv_fps_var.get()
        
        return params
    
    def _is_video_file(self) -> bool:
        """Detect if selected file is video or audio"""
        try:
            # Check URL first
            url = self.url_entry.get().strip()
            if url:
                # For URLs, assume video (most common case)
                return True
            
            # Check file path
            file_path = self.file_entry.get().strip()
            if not file_path:
                return False
            
            # Check file extension
            ext = os.path.splitext(file_path)[1].lower()
            
            video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v'}
            audio_extensions = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma'}
            
            if ext in video_extensions:
                return True
            elif ext in audio_extensions:
                return False
            else:
                # Unknown extension, default to video
                return True
                
        except Exception as e:
            print(f"Error detecting file type: {e}")
            # Default to video on error
            return True
    
    def _update_watermark_display(self) -> None:
        """Update watermark checkbox based on detected file type"""
        try:
            is_video = self._is_video_file()
            
            if is_video:
                self.watermark_var.set(True)
                self.watermark_check.config(text="Watermark (Video - Enabled)")
            else:
                self.watermark_var.set(False)
                self.watermark_check.config(text="Watermark (Audio - Disabled)")
                
        except Exception as e:
            print(f"Error updating watermark display: {e}")
    
    def _simulate_dubbing_process(self, params: Dict) -> None:
        """
        DEPRECATED: Simulation method - replaced by real API integration
        This method should not be called when integrated with MainWindow
        """
        self.log_message("⚠️ SIMULATION METHOD CALLED - This should not happen!")
        self.log_message("❌ Vui lòng sử dụng ứng dụng từ MainWindow để có API thật")
        self._reset_ui()
    
    def _reset_ui(self) -> None:
        """Reset UI after process completion"""
        self.start_btn.config(state="normal", text="🎬 Bắt đầu Dubbing")
        self.progress.stop()
    
    @property
    def master(self):
        """Get master window for after() calls"""
        return self.frame.winfo_toplevel()
    
    def refresh_projects(self) -> None:
        """Refresh dubbing projects status"""
        self.log_message("🔄 Đang làm mới danh sách dự án...")
        # TODO: Implement actual refresh logic
        self.log_message("✅ Đã làm mới")
    
    def clear_form(self) -> None:
        """Clear all form fields"""
        self.file_entry.delete(0, "end")
        self.url_entry.delete(0, "end")
        self.name_entry.delete(0, "end")
        self.target_accent_entry.delete(0, "end")
        
        # Reset to defaults
        self.source_lang_combo.set("en")
        self.target_lang_combo.set("vi")
        self.speakers_var.set(1)
        self.mode_combo.set("automatic")
        self.start_time_var.set(0)
        self.end_time_var.set(0)
        self.csv_fps_var.set(25.0)
        
        # Reset checkboxes
        self.watermark_var.set(False)  # Default False
        self.highest_resolution_var.set(True)
        self.drop_background_var.set(False)
        self.profanity_filter_var.set(False)
        self.dubbing_studio_var.set(False)
        self.disable_voice_cloning_var.set(False)
        
        # Reset watermark display
        self.watermark_check.config(text="Watermark (auto-detect)")
        
        self.selected_file = ""
        self.log_message("🗑 Đã xóa toàn bộ form")
    
    def log_message(self, message: str) -> None:
        """Add message to result text area"""
        try:
            from datetime import datetime
            timestamp = datetime.now().strftime("%H:%M:%S")
            full_message = f"[{timestamp}] {message}\n"
            
            self.result_text.insert("end", full_message)
            self.result_text.see("end")  # Scroll to bottom
            
            # Update status label with latest message
            self.status_label.config(text=message)
            
        except Exception as e:
            print(f"Error logging message: {e}")

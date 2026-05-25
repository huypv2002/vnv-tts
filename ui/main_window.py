from __future__ import annotations

import asyncio
import threading
import logging
import os
import sys
import re
from datetime import datetime
import ttkbootstrap as tb
from tkinter import ttk
from ui.components.voice_panel import VoicePanel
from ui.components.batch_panel import BatchPanel
from ui.components.subtitles_panel import SubtitlesPanel
from ui.components.controls_panel import ControlsPanel
from ui.components.status_bar import StatusBar
# from ui.components.key_upload_panel import KeyUploadPanel  # REMOVED - not needed
from models.ui_models import AppState
from services.user_service import UserService
from services.optimized_credit_checker import OptimizedCreditChecker
from services.tts_service import TTSTaskRunner
from services.srt_generator import generate_srt_from_folder
from services.proxy_service import ProxyService
from services.accurate_credit_tracker import AccurateCreditTracker
from services.user_config_service import UserConfigService
from services.update_service import UpdateService


class MainWindow:
    def __init__(self, master, settings_service, auth_service=None, user_id=None) -> None:
        self.master = master
        self._settings = settings_service
        self._auth = auth_service
        self.user_id = user_id
        self.app_state = AppState()
        
        # Setup logging
        self.setup_ui_logger()
        
        # Initialize services
        if auth_service and hasattr(auth_service, 'supabase'):
            self.user_service = UserService(auth_service.supabase)
            self.credit_checker = OptimizedCreditChecker(auth_service.supabase)
            self._key_supabase = auth_service.supabase
            self.proxy_service = ProxyService(supabase=auth_service.supabase, user_id=user_id) if user_id else None
            if self.proxy_service:
                self.proxy_service.start_auto_rotate(rotate_every=60)
            self.credit_tracker = AccurateCreditTracker(auth_service.supabase, user_id) if user_id else None
            # Initialize user config service (local JSON file, không cần supabase)
            self.user_config_service = UserConfigService() if user_id else None
            # Initialize update service
            print(f"🔄 [UPDATE] Initializing UpdateService...")
            self.update_service = UpdateService(auth_service.supabase)
            print(f"✅ [UPDATE] UpdateService initialized")
        else:
            self.user_service = None
            self.credit_checker = None
            self.proxy_service = None
            self.credit_tracker = None
            self.user_config_service = None
            self.update_service = None
        
        # Configure main window
        master.title("mvh30 Auto TTS Subtitles (elevenlabs) 3.24")
        # Configure main window
        master.title("mvh30 Auto TTS Subtitles (elevenlabs) 3.24")
        master.geometry("1100x800")
        
        # Gray Theme Configuration
        style = tb.Style()
        bg_color = '#e0e0e0'  # Uniform Gray
        
        # Base styles for gray background
        style.configure('Gray.TFrame', background=bg_color)
        style.configure('TFrame', background=bg_color)
        style.configure('TLabelframe', background=bg_color, bordercolor='black')
        style.configure('TLabelframe.Label', background=bg_color, foreground='black')
        style.configure('TLabel', background=bg_color, foreground='black')
        style.configure('TCheckbutton', background=bg_color, foreground='black')
        style.configure('TRadiobutton', background=bg_color, foreground='black')
        
        # Bordered Styles (Soft Gray Border)
        style.configure('Bordered.TFrame', background=bg_color, borderwidth=1, relief="solid", bordercolor="#b0b0b0")
        style.configure('Bordered.TLabelframe', background=bg_color, borderwidth=1, relief="solid", bordercolor="#b0b0b0")
        style.configure('Bordered.TLabelframe.Label', background=bg_color, foreground="black")
        
        master.configure(background=bg_color)

        # Expose back-reference
        setattr(master, "_main_window_ref", self)
        
        # Main container
        main_frame = tb.Frame(master, style='Gray.TFrame')
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # ===== TOP SECTION: Voice + Settings + Options (3 cols) =====
        # Wrapper Panel "Cấu hình chung"
        top_wrapper = tb.Labelframe(main_frame, text="Cấu hình chung", padding=10, style='Bordered.TLabelframe')
        top_wrapper.pack(fill="x", pady=(0, 10))
        
        # Left container - Will hold VoicePanel (which internally has 2 panels)
        # VoicePanel needs to return/manage 2 columns basically, or we pass it a container to split.
        voice_container = tb.Frame(top_wrapper, style='Gray.TFrame')
        voice_container.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        self.voice_panel = VoicePanel(voice_container)
        self.voice_panel.frame.pack(fill="both", expand=True)
        
        # Right side - Options Panel
        options_container = tb.Frame(top_wrapper, width=260, style='Gray.TFrame')
        options_container.pack(side="right", fill="y")
        options_container.pack_propagate(False)
        
        # Options section - Bordered
        options_frame = tb.Labelframe(options_container, text="Tùy chọn", padding=10, style='Bordered.TLabelframe')
        options_frame.pack(fill="both", expand=True)
        
        opt_row1 = tb.Frame(options_frame, style='Gray.TFrame')
        opt_row1.pack(fill="x", pady=(0, 5))
        tb.Label(opt_row1, text="Luồng:").pack(side="left", padx=(0, 5))
        self.thread_var = tb.IntVar(value=5)
        tb.Spinbox(opt_row1, from_=1, to=20, textvariable=self.thread_var, width=5).pack(side="left")

        opt_row3 = tb.Frame(options_frame, style='Gray.TFrame')
        opt_row3.pack(fill="x", pady=(0, 5))
        self.auto_split_var = tb.BooleanVar(value=False)
        tb.Checkbutton(opt_row3, text="Tự tách câu", variable=self.auto_split_var, style='TCheckbutton').pack(side="left")
        self.auto_split_entry = tb.Entry(opt_row3, width=10)
        self.auto_split_entry.insert(0, ".,;?!")
        self.auto_split_entry.pack(side="left", padx=(5, 5))
        auto_split_help_btn = tb.Button(opt_row3, text="?", width=3, bootstyle="secondary-outline", command=self.show_auto_split_help)
        auto_split_help_btn.pack(side="left")
        
        opt_row4 = tb.Frame(options_frame, style='Gray.TFrame')
        opt_row4.pack(fill="x", pady=(5, 0))
        advanced_btn = tb.Button(opt_row4, text="Cài đặt nâng cao", bootstyle="secondary-outline", command=self.open_advanced_settings)
        advanced_btn.pack(fill="x")
        
        # PROXY SECTION REMOVED (Logic remains in __init__ but UI is gone)
        
        # ===== MIDDLE SECTION: Batch Job (full width, fixed height) =====
        middle_frame = tb.Frame(main_frame)
        middle_frame.pack(fill="x", pady=(0, 3))  # fill="x" thay vì "both", không expand
        
        self.batch_panel = BatchPanel(middle_frame)
        self.batch_panel.frame.pack(fill="x")  # fill="x" thay vì "both"
        
        # Track stats
        self.completed_count = 0
        self.error_count = 0
        
        # Track running state
        self.is_running = False
        self.should_stop = False
        
        # ===== BOTTOM SECTION: Subtitles - chiếm phần còn lại =====
        subtitles_container = tb.Frame(main_frame)
        subtitles_container.pack(fill="both", expand=True, pady=(0, 3))
        
        # Subtitles (controls đã chuyển sang Batch Job panel)
        self.subtitles_panel = SubtitlesPanel(subtitles_container)
        self.subtitles_panel.frame.pack(fill="both", expand=True)
        
        # Expose controls_panel from batch_panel for backward compatibility
        # Controls panel is now inside batch_panel
        self.controls_panel = self.batch_panel.controls_panel
        
        # Status bar
        self.status_bar = StatusBar(main_frame)
        self.status_bar.frame.pack(fill="x", side="bottom")
        
        # Initialize with saved voices
        self.refresh_voice_library()
        
        # Load user account information
        self.load_account_info()
        
        # Load user config from database
        self.load_user_config()
        
        # Setup auto-save on window close
        self.setup_auto_save()
        
        # Setup auto-save for other controls
        self.setup_control_auto_save()
        
        # Clear subtitles panel
        self.subtitles_panel.clear()
        
        # Setup callback for folder selection
        self.batch_panel.set_on_folder_selected_callback(self.on_folder_selected)
        
        # Check for updates in background (non-blocking)
        print(f"🔄 [UPDATE] Checking if update_service exists: {self.update_service is not None}")
        if self.update_service:
            print(f"✅ [UPDATE] Calling check_for_updates_async()...")
            self.check_for_updates_async()
        else:
            print(f"⚠️ [UPDATE] Update service is None, skipping update check")

    def open_advanced_settings(self) -> None:
        """Open advanced settings dialog"""
        if hasattr(self.voice_panel, 'open_advanced_settings'):
            self.voice_panel.open_advanced_settings()
    
    def show_auto_split_help(self) -> None:
        """Show help for Auto Split feature"""
        from ttkbootstrap.dialogs import Messagebox
        Messagebox.show_info(
            "Auto Split giúp tự động tách văn bản theo các ký tự phân cách.\n"
            "Mặc định: .,;?!\n"
            "Bạn có thể thay đổi các ký tự phân cách trong ô input.",
            "Auto Split Help"
        )

    def on_folder_selected(self, txt_files: list, file_type: str = "txt") -> None:
        """
        Callback when folder is selected - load subtitles preview into subtitles panel.
        This provides immediate visual feedback without needing to click Start.
        Runs in background thread to avoid blocking UI.
        """
        def load_preview():
            try:
                print(f"📁 Folder selected with {len(txt_files)} files - Loading preview...")
                self.ui_logger.info(f"FOLDER_SELECTED - {len(txt_files)} files, loading preview")
                
                # Clear existing subtitles in main thread
                self.master.after(0, lambda: self.subtitles_panel.clear())
                
                # Reset stats in main thread
                self.master.after(0, lambda: setattr(self, 'completed_count', 0))
                self.master.after(0, lambda: setattr(self, 'error_count', 0))
                self.master.after(0, lambda: self.update_result_stats(0, 0))
                
                # Load paragraphs from all files and display in subtitles panel
                total_paragraphs = 0
                
                # Use TextChunker directly to avoid creating full TTSTaskRunner
                from services.tts_service import TextChunker
                
                for file_idx, path in enumerate(txt_files, start=1):
                    try:
                        # Read file content
                        import pathlib
                        import os
                        
                        # Check if file is empty
                        file_size = os.path.getsize(path)
                        if file_size == 0:
                            print(f"⚠️ File is empty (0KB): {path} - Skipping preview...")
                            continue
                        
                        content = pathlib.Path(path).read_text(encoding='utf-8', errors='ignore')
                        
                        # Check if content is empty after reading
                        if not content or len(content.strip()) == 0:
                            print(f"⚠️ File has no readable content: {path} - Skipping preview...")
                            continue
                        
                        # Detect file type and process accordingly
                        file_ext = os.path.splitext(path)[1].lower()
                        
                        if file_ext == '.srt':
                            # For SRT files: extract only text content
                            paragraphs = self.extract_srt_text_only(content)
                            print(f"📄 File {file_idx}: {os.path.basename(path)} - {len(paragraphs)} text lines from SRT")
                        else:
                            # For TXT files: split paragraphs normally
                            paragraphs = TextChunker.split_paragraphs(content)
                            print(f"📄 File {file_idx}: {os.path.basename(path)} - {len(paragraphs)} paragraphs")
                        
                        # Add paragraphs to subtitle table in main thread
                        for p_idx, para in enumerate(paragraphs, start=1):
                            preview = para[:80] + "..." if len(para) > 80 else para
                            row_id = f"{file_idx}-{p_idx}"
                            
                            # Add row to subtitles panel in main thread
                            def add_row(rid=row_id, out="", time="", txt=preview, vnum="", stat="⏳ Queued"):
                                self.subtitles_panel.add_subtitle_row(rid, out, time, txt, vnum, stat)
                            
                            self.master.after(0, add_row)
                            total_paragraphs += 1
                        
                    except Exception as e:
                        print(f"❌ Error loading preview for {path}: {e}")
                        self.ui_logger.error(f"FOLDER_PREVIEW_ERROR - File: {path}, Error: {str(e)}")
                
                # Update stats in main thread
                self.master.after(0, lambda: self.subtitles_panel.update_stats(0, 0, total_paragraphs, 0))
                
                print(f"✅ Loaded preview: {total_paragraphs} paragraphs from {len(txt_files)} files")
                self.ui_logger.info(f"FOLDER_PREVIEW_SUCCESS - {total_paragraphs} paragraphs loaded")
                
                # Update button states based on file type
                self.master.after(0, lambda: self.update_button_states(file_type))
                
            except Exception as e:
                print(f"❌ Error in on_folder_selected: {e}")
                self.ui_logger.error(f"FOLDER_SELECTED_ERROR - {str(e)}")
                import traceback
                traceback.print_exc()
        
        # Run in background thread to avoid blocking UI
        import threading
        thread = threading.Thread(target=load_preview, daemon=True)
        thread.start()
    
    def update_button_states(self, file_type: str) -> None:
        """Update button states based on current file type"""
        try:
            # Get the "Tạo srt" menu item from controls panel
            if hasattr(self.controls_panel, '_set_create_srt_enabled'):
                if file_type == "srt":
                    # Disable "Tạo srt" menu item when using SRT files
                    self.controls_panel._set_create_srt_enabled(False)
                    print("🔒 Disabled 'Tạo srt' menu item - using SRT files")
                else:
                    # Enable "Tạo srt" menu item when using TXT files
                    self.controls_panel._set_create_srt_enabled(True)
                    print("🔓 Enabled 'Tạo srt' menu item - using TXT files")
            elif hasattr(self.controls_panel, 'create_srt_btn'):
                # Fallback for old button-based approach
                if file_type == "srt":
                    self.controls_panel.create_srt_btn.config(state="disabled")
                    print("🔒 Disabled 'Tạo srt' button - using SRT files")
                else:
                    self.controls_panel.create_srt_btn.config(state="normal")
                    print("🔓 Enabled 'Tạo srt' button - using TXT files")
        except Exception as e:
            print(f"❌ Error updating button states: {e}")
    
    def setup_ui_logger(self):
        """Setup logger for UI operations"""
        self.ui_logger = logging.getLogger('ui_operations')
        if self.ui_logger.handlers:
            return
        
        self.ui_logger.setLevel(logging.DEBUG)
        
        # Create logs directory - handle both development and exe environments
        try:
            # Try to get the directory where the exe is located
            if hasattr(sys, 'frozen') and hasattr(sys, '_MEIPASS'):
                # Running from PyInstaller exe
                exe_dir = os.path.dirname(sys.executable)
                log_dir = os.path.join(exe_dir, 'logs')
            else:
                # Running from source code
                log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
        except:
            # Fallback to current working directory
            log_dir = os.path.join(os.getcwd(), 'logs')
        
        # Ensure log directory exists
        try:
            os.makedirs(log_dir, exist_ok=True)
            print(f"📁 Log directory created/verified: {log_dir}")
        except Exception as e:
            print(f"❌ Error creating log directory {log_dir}: {e}")
            # Fallback to temp directory
            import tempfile
            log_dir = os.path.join(tempfile.gettempdir(), 'tts_logs')
            os.makedirs(log_dir, exist_ok=True)
            print(f"📁 Using fallback log directory: {log_dir}")
        
        # Create file handler with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = os.path.join(log_dir, f'ui_operations_{timestamp}.log')
        
        try:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            
            # Create formatter
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            
            self.ui_logger.addHandler(file_handler)
            
            # Log successful setup
            self.ui_logger.info(f"LOGGER_SETUP_SUCCESS - Log file: {log_file}")
            print(f"✅ UI Logger setup successfully: {log_file}")
            
        except Exception as e:
            print(f"❌ Error setting up file handler: {e}")
            # Add console handler as fallback
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            console_handler.setFormatter(formatter)
            self.ui_logger.addHandler(console_handler)
            self.ui_logger.info("LOGGER_SETUP_FALLBACK - Using console logging")
            print("⚠️ Using console logging as fallback")

    # ===== Batch generate from TXT =====
    def stop_generation(self) -> None:
        """Stop the running generation immediately"""
        if self.is_running:
            self.should_stop = True
            print("🛑 Stop requested by user - stopping immediately")
            self.ui_logger.info("STOP_REQUESTED - User requested immediate stop")
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_info("Đang dừng quá trình ngay lập tức...")
        else:
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Không có quá trình nào đang chạy")
    
    def start_generate_txt_batch(self) -> None:
        print("🚀 start_generate_txt_batch called")
        self.ui_logger.info("BATCH_GENERATE_START - Starting batch generation process")
        
        # Check if user has active subscription
        if not self._check_subscription_active():
            self.ui_logger.warning("BATCH_GENERATE_SUBSCRIPTION_ERROR - User subscription inactive")
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error("Gói đăng ký của bạn đã hết hạn hoặc không hoạt động!\nVui lòng gia hạn để tiếp tục sử dụng.")
            return
        
        if self.is_running:
            self.ui_logger.warning("BATCH_GENERATE_ALREADY_RUNNING - Another process is already running")
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_warning("Đang có quá trình chạy, vui lòng đợi hoặc bấm Stop")
            return
        
        try:
            # Need selected files from batch panel
            files = self.batch_panel.get_all_files() if hasattr(self.batch_panel, 'get_all_files') else []
            print(f"📁 Found {len(files)} files: {files}")
            self.ui_logger.info(f"BATCH_FILES_SELECTED - Count: {len(files)}, Files: {files}")
            if not files:
                self.ui_logger.warning("BATCH_NO_FILES_SELECTED - No files selected for processing")
                print("❌ No files selected")
                return
            # Use current voice_id and model from UI
            voice_display = self.voice_panel.voice_combo.get().strip()
            print(f"🎤 Voice: {voice_display}")
            self.ui_logger.info(f"VOICE_SELECTION - Display: {voice_display}")
            
            # Get voice_id using improved logic
            voice_id = self.get_current_voice_id()
            
            if not voice_id:
                self.ui_logger.error("VOICE_ID_NOT_AVAILABLE - No voice ID available")
                print("❌ No voice ID available")
                from ttkbootstrap.dialogs import Messagebox
                Messagebox.show_error("Không tìm thấy Voice ID hợp lệ!\nVui lòng chọn voice hoặc nhập Voice ID.")
                return
            model_name = self.voice_panel.model_combo.get().strip() or "eleven_turbo_v2_5"
            print(f"🎵 Voice ID: {voice_id}, Model: {model_name}")
            self.ui_logger.info(f"TTS_SETTINGS - Voice ID: {voice_id}, Model: {model_name}")
            # Get max_workers from thread spinbox (default 5, max 5)
            max_workers = getattr(self, 'thread_var', None).get() if hasattr(self, 'thread_var') else 5
            max_workers = max(1, min(5, int(max_workers)))  # Validate: min=1, max=5
            self.ui_logger.info(f"WORKER_THREADS - Using {max_workers} worker threads (multi-threading enabled)")

            # Get advanced settings
            advanced_settings = self._settings.get_advanced_settings()
            self.ui_logger.info(f"ADVANCED_SETTINGS - Settings: {advanced_settings}")
            
            # Prepare runner with local key pool, proxy and accurate credit tracker
            runner = TTSTaskRunner(self._key_supabase, self.user_id, output_dir="outputs", 
                                 max_workers=max_workers, advanced_settings=advanced_settings,
                                 proxy_service=self.proxy_service, credit_tracker=self.credit_tracker)
            self.ui_logger.info("TTS_RUNNER_CREATED - TTS Task Runner initialized successfully")

            # Only reset stats, don't clear subtitles (already loaded from folder selection)
            # Reset all subtitle statuses to Queued if already loaded
            if len(self.subtitles_panel._subtitle_rows) > 0:
                print(f"🔄 Resetting {len(self.subtitles_panel._subtitle_rows)} subtitle rows to Queued status...")
                for row in self.subtitles_panel._subtitle_rows:
                    row_id = row[0]
                    self.master.after(0, lambda rid=row_id: self.subtitles_panel.update_subtitle_status(rid, "Queued", ""))
            else:
                # Clear subtitles only if not already loaded
                self.master.after(0, lambda: self.subtitles_panel.clear())
            self.master.after(0, lambda: self.reset_result_stats())
            
            def work():
                self.is_running = True
                self.should_stop = False
                print("🔄 Starting batch work...")
                self.ui_logger.info("BATCH_WORK_START - Starting batch processing work")
                import pathlib
                import os
                import time
                start_time = time.time()
                total_paragraphs = 0
                done_paragraphs = 0
                
                # Check if subtitles are already loaded (from folder selection)
                subtitles_already_loaded = len(self.subtitles_panel._subtitle_rows) > 0
                print(f"ℹ️ Subtitles already loaded: {subtitles_already_loaded} ({len(self.subtitles_panel._subtitle_rows)} rows)")
                
                try:
                    for file_idx, path in enumerate(files):
                        # Check stop signal
                        if self.should_stop:
                            print("🛑 Stopped by user")
                            self.ui_logger.info("BATCH_WORK_STOPPED - Stopped by user request")
                            break
                        
                        print(f"📄 Processing file {file_idx + 1}/{len(files)}: {path}")
                        self.ui_logger.info(f"FILE_PROCESSING_START - Index: {file_idx + 1}, Path: {path}")
                        
                        try:
                            # Update batch table status to Running
                            self.master.after(0, lambda i=file_idx: self.batch_panel.update_status(i, 'Processing'))
                            
                            self.ui_logger.info(f"FILE_READ_START - Attempting to read file: {path}")
                            
                            # Check if file is empty (0KB) before reading
                            file_size = os.path.getsize(path)
                            if file_size == 0:
                                self.ui_logger.warning(f"FILE_EMPTY - File is empty (0KB): {path}")
                                print(f"⚠️ File is empty (0KB): {path} - Skipping...")
                                self.master.after(0, lambda i=file_idx: self.batch_panel.update_status(i, 'Skipped'))
                                continue
                            
                            content = pathlib.Path(path).read_text(encoding='utf-8', errors='ignore')
                            
                            # Check if content is empty after reading
                            if not content or len(content.strip()) == 0:
                                self.ui_logger.warning(f"FILE_CONTENT_EMPTY - File has no readable content: {path}")
                                print(f"⚠️ File has no readable content: {path} - Skipping...")
                                self.master.after(0, lambda i=file_idx: self.batch_panel.update_status(i, 'Skipped'))
                                continue
                            
                            self.ui_logger.info(f"FILE_READ_SUCCESS - Path: {path}, Length: {len(content)}")
                            print(f"📝 Read {len(content)} characters from {path}")
                            print(f"📄 File content preview: {content[:200]}...")
                            print(f"📁 Full file path: {path}")
                        except Exception as e:
                            error_msg = f"FILE_READ_ERROR - Path: {path}, Error: {str(e)}"
                            self.ui_logger.error(error_msg)
                            print(f"❌ Error reading file {path}: {e}")
                            self.master.after(0, lambda i=file_idx: self.batch_panel.update_status(i, 'Error'))
                            continue
                        
                        # Detect file type and process accordingly
                        file_ext = os.path.splitext(path)[1].lower()
                        
                        if file_ext == '.srt':
                            # For SRT files: extract only text content (ignore STT and timing)
                            self.ui_logger.info(f"SRT_TEXT_EXTRACT_START - Extracting text from SRT file")
                            paragraphs = self.extract_srt_text_only(content)
                            self.ui_logger.info(f"SRT_TEXT_EXTRACT_SUCCESS - Extracted {len(paragraphs)} text lines")
                            print(f"📋 Extracted {len(paragraphs)} text lines from SRT")
                        else:
                            # For TXT files: split paragraphs normally
                            self.ui_logger.info(f"PARAGRAPH_SPLIT_START - Splitting content into paragraphs")
                            paragraphs = runner.split_paragraphs(content)
                            self.ui_logger.info(f"PARAGRAPH_SPLIT_SUCCESS - Split into {len(paragraphs)} paragraphs")
                            print(f"📋 Split into {len(paragraphs)} paragraphs")
                        
                        total_paragraphs += len(paragraphs)
                        
                        # Add all paragraphs to subtitle table (only if not already loaded)
                        if not subtitles_already_loaded:
                            for p_idx, para in enumerate(paragraphs, start=1):
                                preview = para[:80] + "..." if len(para) > 80 else para
                                row_id = f"{file_idx + 1}-{p_idx}"
                                print(f"➕ Adding subtitle row: {row_id} - {preview[:30]}...")
                                # Use proper closure to capture variables
                                def add_row(rid=row_id, txt=preview):
                                    self.subtitles_panel.add_subtitle_row(rid, "", "", txt, "", "Queued")
                                self.master.after(0, add_row)
                        else:
                            print(f"ℹ️ Subtitles already loaded from folder selection - skipping duplicate add")
                        
                        # Update stats
                        self.master.after(0, lambda d=done_paragraphs, t=total_paragraphs, e=int(time.time()-start_time): 
                            self.subtitles_panel.update_stats(d, 0, t, e))
                        
                        base = os.path.splitext(os.path.basename(path))[0]
                        parent = os.path.dirname(path)
                        mini_dir = os.path.join(parent, 'tts_mini')
                        self.ui_logger.info(f"MINI_DIR_CREATE_START - Creating mini directory: {mini_dir}")
                        os.makedirs(mini_dir, exist_ok=True)
                        self.ui_logger.info(f"MINI_DIR_CREATE_SUCCESS - Mini directory created: {mini_dir}")
                        print(f"📁 Created mini dir: {mini_dir}")
                        
                        # Collect voice settings from UI
                        self.ui_logger.info(f"VOICE_SETTINGS_START - Collecting voice settings from UI")
                        vs = {
                            'stability': float(self.voice_panel.stability_var.get()) / 100.0,
                            'similarity_boost': float(self.voice_panel.similarity_var.get()) / 100.0,
                            'style': float(self.voice_panel.style_var.get()) / 100.0,
                            'use_speaker_boost': bool(self.voice_panel.speaker_boost_var.get()),
                            'speed': max(0.7, min(1.2, float(self.voice_panel.speed_var.get()))),
                        }
                        self.ui_logger.info(f"VOICE_SETTINGS_SUCCESS - Voice settings: {vs}")
                        print(f"🎛️ Voice settings: {vs}")
                        
                        # Define callbacks for subtitle table updates
                        def on_para_start(p_idx, para_text):
                            row_id = f"{file_idx + 1}-{p_idx}"
                            self.master.after(0, lambda: self.subtitles_panel.update_subtitle_status(row_id, "Processing"))
                            self.master.after(0, lambda: self.subtitles_panel.update_stats(done_paragraphs, 1, total_paragraphs, int(time.time()-start_time)))
                        
                        def on_status_update(status_type, message):
                            """Callback for detailed status updates"""
                            # Map status types to display status
                            status_map = {
                                "verify": "Verifying",
                                "check_key": "Check Key",
                                "synthesizing": "Synthesizing",
                                "uploading": "Uploading",
                            }
                            display_status = status_map.get(status_type, "Processing")
                            # Find the current processing paragraph (last one with Processing status)
                            # This is a simple implementation - can be improved
                            try:
                                for row in self.subtitles_panel._subtitle_rows:
                                    if row[5] == "Processing":
                                        row_id = row[0]
                                        self.master.after(0, lambda rid=row_id, st=display_status: 
                                            self.subtitles_panel.update_subtitle_status(rid, st))
                                        break
                            except Exception as e:
                                self.ui_logger.warning(f"STATUS_UPDATE_ERROR - {e}")
                        
                        def on_para_done(p_idx, output_path):
                            nonlocal done_paragraphs
                            done_paragraphs += 1
                            row_id = f"{file_idx + 1}-{p_idx}"
                            out_file = os.path.basename(output_path)
                            
                            self.ui_logger.info(f"CALLBACK_PARA_DONE - Index: {p_idx}, RowID: {row_id}, Path: {output_path}")
                            print(f"🎯 on_para_done called: p_idx={p_idx}, row_id={row_id}, output_path={output_path}")
                            print(f"📁 Output file: {out_file}")
                            
                            # Verify file exists
                            if not os.path.exists(output_path):
                                self.ui_logger.error(f"CALLBACK_FILE_MISSING - Path: {output_path}")
                                print(f"❌ Output file does not exist: {output_path}")
                                return
                            
                            # Get audio duration for timing
                            from services.srt_generator import get_audio_duration
                            duration = get_audio_duration(output_path)
                            timing = f"{duration:.2f}s" if duration > 0 else ""
                            print(f"⏱️ Audio duration: {timing}")
                            
                            # Update subtitle row with timing
                            def update_row():
                                try:
                                    self.ui_logger.info(f"UI_UPDATE_START - RowID: {row_id}, Status: DONE, Output: {out_file}, Timing: {timing}")
                                    print(f"🔄 Updating UI for row {row_id} to DONE with timing: {timing}")
                                    # Pass timing as parameter to update_subtitle_status
                                    self.subtitles_panel.update_subtitle_status(row_id, "DONE", out_file, timing)
                                    self.ui_logger.info(f"UI_UPDATE_SUCCESS - RowID: {row_id}")
                                    print(f"✅ UI update completed for row {row_id}")
                                except Exception as e:
                                    self.ui_logger.error(f"UI_UPDATE_ERROR - RowID: {row_id}, Error: {str(e)}")
                                    print(f"❌ Error updating UI for row {row_id}: {e}")
                                    import traceback
                                    traceback.print_exc()
                            
                            self.master.after(0, update_row)
                            self.master.after(0, lambda: self.subtitles_panel.update_stats(done_paragraphs, 0, total_paragraphs, int(time.time()-start_time)))
                            
                            # Refresh credits after each paragraph
                            if self.credit_tracker:
                                # Update remaining characters based on subscription quota
                                remaining_chars = self.credit_tracker.get_remaining_characters()
                            
                            # Nếu ký tự còn lại <= 0 thì dừng quá trình và thông báo
                            if remaining_chars <= 0:
                                print("❌ No remaining characters in subscription - stopping batch")
                                self.should_stop = True
                            
                            def _update_chars_display(rem=remaining_chars):
                                try:
                                    self.status_bar.update_credits_only(rem)
                                    if rem <= 0:
                                        from ttkbootstrap.dialogs import Messagebox
                                        Messagebox.show_error(
                                            "Bạn đã hết ký tự trong gói!\n"
                                            "Quá trình sẽ dừng lại. Vui lòng nâng cấp hoặc nạp thêm gói."
                                        )
                                except Exception as e:
                                    print(f"❌ Error updating characters display: {e}")
                            
                            self.master.after(0, _update_chars_display)
                            
                        # Generate pieces
                        self.ui_logger.info(f"TTS_GENERATION_START - Starting TTS generation for {len(paragraphs)} paragraphs")
                        print(f"🎵 Starting TTS generation...")
                        generation_error = None
                        try:
                            runner.run_for_texts(
                                voice_id,
                                model_name,
                                paragraphs,
                                mini_dir=mini_dir,
                                basename=base,
                                voice_settings=vs,
                                on_paragraph_start=on_para_start,
                                on_paragraph_done=on_para_done,
                                stop_callback=lambda: self.should_stop,
                                on_status_update=on_status_update,
                            )
                            self.ui_logger.info(
                                f"TTS_GENERATION_COMPLETE - TTS generation completed for {len(paragraphs)} paragraphs"
                            )
                        except Exception as gen_exc:
                            # Lưu lỗi nhưng vẫn cố gắng ghép các đoạn đã tạo thành công
                            generation_error = gen_exc
                            self.ui_logger.error(
                                f"TTS_GENERATION_ERROR - Error during generation, will attempt partial concat. Error: {gen_exc}"
                            )
                            print(f"❌ Error during TTS generation, attempting partial concat: {gen_exc}")

                        # Concat to parent/<base>.mp3 using ffmpeg
                        from services.ffmpeg_utils import get_ffmpeg_path
                        ffmpeg_bin = get_ffmpeg_path()
                        out_total = os.path.join(parent, f'{base}.mp3')
                        self.ui_logger.info(f"CONCAT_START - Starting concatenation to: {out_total}")
                        print(f"🔗 Concatenating to: {out_total}")
                        
                        # Check if the paragraph folder exists and has mp3 files
                        paragraph_folder = os.path.join(mini_dir, base)
                        self.ui_logger.info(f"MP3_CHECK_START - Checking for MP3 files in: {paragraph_folder}")
                        if os.path.exists(paragraph_folder):
                            mp3_files = [f for f in os.listdir(paragraph_folder) if f.lower().endswith('.mp3')]
                            self.ui_logger.info(f"MP3_CHECK_SUCCESS - Found {len(mp3_files)} mp3 files: {mp3_files}")
                            print(f"📁 Found {len(mp3_files)} mp3 files in {paragraph_folder}: {mp3_files}")
                            if mp3_files:
                                # Get advanced settings for pause configuration
                                advanced_settings = self._settings.get_advanced_settings()
                                self.ui_logger.info(f"CONCAT_EXECUTE - Executing ffmpeg concat for {len(mp3_files)} files with advanced settings: {advanced_settings}")
                                runner.concat_pieces(ffmpeg_bin, paragraph_folder, out_total, advanced_settings)
                                try:
                                    ok_concat = os.path.exists(out_total) and os.path.getsize(out_total) > 0
                                except Exception:
                                    ok_concat = False

                                if ok_concat:
                                    size = os.path.getsize(out_total)
                                    self.ui_logger.info(
                                        f"CONCAT_SUCCESS - Successfully concatenated to: {out_total} (size={size})"
                                    )
                                    # update table status to Completed (kể cả khi chỉ ghép được một phần)
                                    self.master.after(
                                        0, lambda i=file_idx: self.batch_panel.update_status(i, 'Completed')
                                    )
                                else:
                                    # treat as error if final file missing/empty
                                    self.ui_logger.error(f"CONCAT_VERIFY_FAIL - Output not valid: {out_total}")
                                    self.error_count += 1
                                    self.master.after(0, lambda: self.update_result_stats())
                                    self.master.after(
                                        0, lambda i=file_idx: self.batch_panel.update_status(i, 'Error')
                                    )
                                    continue
                                self.ui_logger.info(f"CONCAT_SUCCESS - Successfully concatenated to: {out_total}")
                                
                                # Manual callback for successful paragraphs that weren't called during TTS
                                print(f"🔄 Manually updating UI for successful paragraphs...")
                                for p_idx in range(1, len(paragraphs) + 1):
                                    row_id = f"{file_idx + 1}-{p_idx}"
                                    paragraph_file = os.path.join(paragraph_folder, f"{p_idx}.mp3")
                                    
                                    if os.path.exists(paragraph_file):
                                        print(f"✅ Found paragraph file: {paragraph_file}")
                                        # Call the callback manually
                                        try:
                                            on_para_done(p_idx, paragraph_file)
                                            print(f"📞 Manual callback completed for paragraph {p_idx}")
                                        except Exception as e:
                                            print(f"❌ Error in manual callback for paragraph {p_idx}: {e}")
                                    else:
                                        print(f"❌ Paragraph file not found: {paragraph_file}")
                            else:
                                error_msg = f"MP3_FILES_NOT_FOUND - No mp3 files found in {paragraph_folder}"
                                self.ui_logger.error(error_msg)
                                print(f"❌ No mp3 files found in {paragraph_folder}")
                                raise RuntimeError(f"No mp3 files found in {paragraph_folder}")
                        else:
                            error_msg = f"PARAGRAPH_FOLDER_NOT_EXIST - Paragraph folder does not exist: {paragraph_folder}"
                            self.ui_logger.error(error_msg)
                            print(f"❌ Paragraph folder does not exist: {paragraph_folder}")
                            raise RuntimeError(f"Paragraph folder does not exist: {paragraph_folder}")
                        
                        # Generate SRT if auto_srt is enabled
                        if self.batch_panel.auto_srt_var.get():
                            srt_path = os.path.join(parent, f'{base}.srt')
                            self.ui_logger.info(f"SRT_GENERATION_START - Generating SRT file: {srt_path}")
                            print(f"📝 Generating SRT: {srt_path}")
                            generate_srt_from_folder(os.path.join(mini_dir, base), paragraphs, srt_path)
                            self.ui_logger.info(f"SRT_GENERATION_SUCCESS - SRT file generated: {srt_path}")
                        
                        # Update result stats
                        self.completed_count += 1
                        print(f"✅ File completed: {base} (Total completed: {self.completed_count})")
                        self.master.after(0, lambda: self.update_result_stats())
                        
                        # update table status
                        self.ui_logger.info(f"FILE_COMPLETED - Successfully completed file: {base}")
                        print(f"✅ Completed file: {base}")
                        self.master.after(0, lambda i=file_idx: self.batch_panel.update_status(i, 'Completed'))
                        
                except Exception as e:
                        error_msg = str(e)
                        print(f"❌ Error processing file {path}: {e}")
                        self.ui_logger.error(f"FILE_PROCESSING_ERROR - File: {path}, Error: {error_msg}")
                        
                        # Analyze error and provide specific guidance
                        if "401" in error_msg or "unauthorized" in error_msg or "authentication failed" in error_msg:
                            self.ui_logger.error(f"CRITICAL_ERROR - All API keys are invalid/expired")
                            print(f"🔑 CRITICAL ERROR: All API keys are invalid/expired!")
                            print(f"   This means your ElevenLabs API keys are no longer working.")
                            print(f"   SOLUTIONS:")
                            print(f"   1. Check if your ElevenLabs account is still active")
                            print(f"   2. Log into ElevenLabs dashboard and generate new API keys")
                            print(f"   3. Update your key database with new valid keys")
                            print(f"   4. Check if your subscription is still active")
                            print(f"   5. Contact ElevenLabs support if account issues persist")
                        elif "voice" in error_msg.lower() and "invalid" in error_msg.lower():
                            print(f"🎤 SOLUTION: Voice ID issue. Please:")
                            print(f"   1. Use a different voice ID")
                            print(f"   2. Check if you have access to this voice")
                            print(f"   3. Try with a free/default voice")
                        elif "no working keys" in error_msg.lower():
                            print(f"💳 SOLUTION: No working API keys available. Please:")
                            print(f"   1. Add more API keys to your account")
                            print(f"   2. Check credit balance on existing keys")
                            print(f"   3. Wait for quota reset if rate limited")
                        elif "proxy" in error_msg.lower():
                            print(f"🌐 SOLUTION: Proxy connection issue. Please:")
                            print(f"   1. Check proxy configuration")
                            print(f"   2. Try disabling proxy temporarily")
                            print(f"   3. Contact proxy provider")
                        else:
                            print(f"❓ SOLUTION: General error. Please:")
                            print(f"   1. Check internet connection")
                            print(f"   2. Verify file permissions")
                            print(f"   3. Try with a smaller text file")
                        
                        self.error_count += 1
                        print(f"❌ File error: {base} (Total errors: {self.error_count})")
                        self.master.after(0, lambda: self.update_result_stats())
                        self.master.after(0, lambda i=file_idx: self.batch_panel.update_status(i, 'Error'))
                        # Continue to next file
                
                finally:
                    print("🏁 Batch work completed, flushing to DB...")
                    self.ui_logger.info(f"BATCH_WORK_COMPLETED - Total paragraphs: {total_paragraphs}, Done: {done_paragraphs}, Stopped: {self.should_stop}")
                    runner.finalize()
                    self.is_running = False
                    print("✅ All done!")
                    
                    # Final credit refresh
                    if self.credit_tracker:
                        final_chars = self.credit_tracker.get_remaining_characters()
                        self.master.after(0, lambda: self.status_bar.update_credits_only(final_chars))
                    
                    # Show success popup
                    from ttkbootstrap.dialogs import Messagebox
                    status_text = "Đã dừng" if self.should_stop else "Hoàn thành"
                    self.master.after(0, lambda: Messagebox.show_info(
                        f"{status_text} tạo file MP3!\n\n"
                        f"Tổng đoạn: {total_paragraphs}\n"
                        f"Hoàn thành: {done_paragraphs}\n"
                        f"Thời gian: {int(time.time()-start_time)}s",
                        status_text
                    ))
            import threading
            threading.Thread(target=work, daemon=True).start()
        except Exception as e:
            print(f"Error starting batch generate: {e}")

    def start_generate_selected(self) -> None:
        print("🎯 start_generate_selected called")
        try:
            if not hasattr(self.batch_panel, 'get_selected_file'):
                print("❌ batch_panel has no get_selected_file method")
                return
            path = self.batch_panel.get_selected_file()
            print(f"📄 Selected file: {path}")
            if not path:
                print("❌ No file selected")
                return
            # Get voice_id using improved logic
            voice_id = self.get_current_voice_id()
            
            if not voice_id:
                print("❌ No voice ID available")
                from ttkbootstrap.dialogs import Messagebox
                Messagebox.show_error("Không tìm thấy Voice ID hợp lệ!\nVui lòng chọn voice hoặc nhập Voice ID.")
                return
            model_name = self.voice_panel.model_combo.get().strip() or "eleven_turbo_v2_5"
            max_workers = getattr(self, 'thread_var', None).get() if hasattr(self, 'thread_var') else 5
            max_workers = max(1, min(5, int(max_workers)))  # Max: 5 workers
            
            runner = TTSTaskRunner(self._key_supabase, self.user_id, output_dir="outputs", max_workers=max_workers)

            def work_one():
                import pathlib, os
                try:
                    content = pathlib.Path(path).read_text(encoding='utf-8', errors='ignore')
                except Exception:
                    return
                paragraphs = runner.split_paragraphs(content)
                base = os.path.splitext(os.path.basename(path))[0]
                parent = os.path.dirname(path)
                mini_dir = os.path.join(parent, 'tts_mini')
                os.makedirs(mini_dir, exist_ok=True)
                try:
                    vs = {
                        'stability': float(self.voice_panel.stability_var.get() if hasattr(self.voice_panel, 'stability_var') else 0.0),
                        'similarity_boost': float(self.voice_panel.similarity_var.get() if hasattr(self.voice_panel, 'similarity_var') else 1.0),
                        'style': float(self.voice_panel.style_var.get() if hasattr(self.voice_panel, 'style_var') else 0.0),
                        'use_speaker_boost': bool(self.voice_panel.speaker_boost_var.get() if hasattr(self.voice_panel, 'speaker_boost_var') else True),
                        'speed': max(0.7, min(1.2, float(self.voice_panel.speed_var.get() if hasattr(self.voice_panel, 'speed_var') else 1.0))),
                    }
                    runner.run_for_texts(voice_id, model_name, paragraphs, mini_dir=mini_dir, basename=base, voice_settings=vs)
                    ffmpeg_bin = 'ffmpeg.exe'
                    out_total = os.path.join(parent, f'{base}.mp3')
                    
                    # Check if the paragraph folder exists and has mp3 files
                    paragraph_folder = os.path.join(mini_dir, base)
                    if os.path.exists(paragraph_folder):
                        mp3_files = [f for f in os.listdir(paragraph_folder) if f.lower().endswith('.mp3')]
                        print(f"📁 Found {len(mp3_files)} mp3 files in {paragraph_folder}: {mp3_files}")
                        if mp3_files:
                            runner.concat_pieces(ffmpeg_bin, paragraph_folder, out_total)
                            ok_concat = os.path.exists(out_total) and os.path.getsize(out_total) > 0
                            if ok_concat:
                                try:
                                    idx = self.batch_panel.txt_files.index(path)
                                    self.master.after(0, lambda i=idx: self.batch_panel.update_status(i, 'Completed'))
                                except Exception:
                                    pass
                            else:
                                try:
                                    idx = self.batch_panel.txt_files.index(path)
                                    self.master.after(0, lambda i=idx: self.batch_panel.update_status(i, 'Error'))
                                except Exception:
                                    pass
                        else:
                            print(f"❌ No mp3 files found in {paragraph_folder}")
                            raise RuntimeError(f"No mp3 files found in {paragraph_folder}")
                    else:
                        print(f"❌ Paragraph folder does not exist: {paragraph_folder}")
                        raise RuntimeError(f"Paragraph folder does not exist: {paragraph_folder}")
                    try:
                        idx = self.batch_panel.txt_files.index(path)
                        self.master.after(0, lambda i=idx: self.batch_panel.update_status(i, 'Completed'))
                    except Exception:
                        pass
                except Exception:
                    try:
                        idx = self.batch_panel.txt_files.index(path)
                        self.master.after(0, lambda i=idx: self.batch_panel.update_status(i, 'Error'))
                    except Exception:
                        pass
                finally:
                    runner.finalize()

            import threading
            threading.Thread(target=work_one, daemon=True).start()
        except Exception as e:
            print(f"Error starting selected generate: {e}")
    
    def refresh_voice_library(self) -> None:
        """Refresh the voice library from settings"""
        voices = self._settings.list_voices()
        # Store voices for later lookup
        self._saved_voices = voices
        # Update voice combo with voice display names
        voice_names = [v.name if v.name else v.voice_id for v in voices]
        # Always update, even when empty (after clear)
        # Temporarily make combobox writable to set values
        self.voice_panel.voice_combo.config(state="normal")
        self.voice_panel.voice_combo['values'] = voice_names
        if not voice_names:
            try:
                # Clear selection text if nothing remains
                self.voice_panel.voice_combo.set("")
            except Exception:
                pass
        self.voice_panel.voice_combo.config(state="readonly")
        
        # Store voice results in panel for voice_id lookup
        self.voice_panel._voice_results = [
            {"voice_id": v.voice_id, "name": v.name if v.name else v.voice_id, "free_users_allowed": True}
            for v in voices
        ]
        
        # If there are saved voices, load the first one's settings
        if voices and len(voices) > 0:
            first_voice = voices[0]
            try:
                # Load settings for first voice
                self.voice_panel.stability_var.set(int(first_voice.stability * 100))
                self.voice_panel.similarity_var.set(int(first_voice.similarity_boost * 100))
                self.voice_panel.style_var.set(int(first_voice.style * 100))
                try:
                    self.voice_panel.speed_var.set(float(getattr(first_voice, "speed", 1.0)))
                except Exception:
                    self.voice_panel.speed_var.set(1.0)
                self.voice_panel.speaker_boost_var.set(first_voice.use_speaker_boost)
                print(f"📥 Loaded voice settings: stability={first_voice.stability}, similarity={first_voice.similarity_boost}, style={first_voice.style}, speed={getattr(first_voice, 'speed', 1.0)}, boost={first_voice.use_speaker_boost}")
            except Exception as e:
                print(f"❌ Error loading voice settings: {e}")
    
    def update_result_stats(self, completed: int = 0, errors: int = 0) -> None:
        """Update the result statistics display"""
        # Use current values if not provided
        if completed == 0 and errors == 0:
            completed = self.completed_count
            errors = self.error_count
        
        # Update result label in batch panel
        if hasattr(self.batch_panel, 'result_label'):
            total = len(self.batch_panel.txt_files) if hasattr(self.batch_panel, 'txt_files') else 0
            self.batch_panel.result_label.config(text=f"Kết Quả: {completed}/{total}")
        print(f"📊 Result stats updated: {completed} completed, {errors} errors")
    
    def reset_result_stats(self) -> None:
        """Reset result statistics before starting new batch"""
        self.completed_count = 0
        self.error_count = 0
        # Update result label in batch panel
        if hasattr(self.batch_panel, 'result_label'):
            total = len(self.batch_panel.txt_files) if hasattr(self.batch_panel, 'txt_files') else 0
            self.batch_panel.result_label.config(text=f"Kết Quả: 0/{total}")
        print(f"🔄 Result stats reset: {self.completed_count} completed, {self.error_count} errors")
    
    def _check_subscription_active(self) -> bool:
        """Check if user has active subscription"""
        try:
            if not self.user_id or not self._key_supabase:
                return False
            
            result = self._key_supabase.table('user_subscriptions')\
                .select('is_active, end_date, count_characters')\
                .eq('user_id', self.user_id)\
                .eq('is_active', True)\
                .execute()
            
            if not result.data:
                print("❌ No active subscription found")
                return False
            
            sub = result.data[0]
            
            # Check if subscription has expired
            if sub.get('end_date'):
                from datetime import datetime
                try:
                    end_date = datetime.fromisoformat(sub['end_date'].replace('Z', '+00:00'))
                    if datetime.now() > end_date:
                        print(f"❌ Subscription expired: {end_date}")
                        return False
                except:
                    pass

            # Check character-based quota (count_characters <= 0 means hết ký tự)
            try:
                count_chars = sub.get('count_characters')
                if count_chars is not None and int(count_chars) <= 0:
                    print("❌ Subscription has no remaining characters (count_characters <= 0)")
                    return False
            except Exception:
                # If parsing fails, fall back to allowing; better to allow than block by bug
                pass
            
            print("✅ Subscription is active")
            return True
            
        except Exception as e:
            print(f"❌ Error checking subscription: {e}")
            return False
    
    def create_srt_for_selected(self) -> None:
        """Create SRT files for all selected txt files in batch panel"""
        try:
            files = self.batch_panel.get_all_files()
            if not files:
                from ttkbootstrap.dialogs import Messagebox
                Messagebox.show_warning("Chưa chọn thư mục TXT")
                return
            
            def work():
                import pathlib, os
                from services.srt_generator import generate_srt_from_folder
                from services.tts_service import TextChunker
                
                success_count = 0
                error_count = 0
                
                for path in files:
                    try:
                        print(f"🔍 Processing SRT for: {path}")
                        
                        # Validate file path
                        if not path or not os.path.exists(path):
                            print(f"❌ File not found: {path}")
                            error_count += 1
                            continue
                        
                        # Read content
                        content = pathlib.Path(path).read_text(encoding='utf-8', errors='ignore')
                        if not content.strip():
                            print(f"❌ File is empty: {path}")
                            error_count += 1
                            continue
                        
                        # Split paragraphs using TextChunker
                        paragraphs = TextChunker.split_paragraphs(content)
                        if not paragraphs:
                            print(f"❌ No paragraphs found in: {path}")
                            error_count += 1
                            continue
                        
                        # Build paths
                        base = os.path.splitext(os.path.basename(path))[0]
                        parent = os.path.dirname(path)
                        mini_folder = os.path.join(parent, 'tts_mini', base)
                        srt_path = os.path.join(parent, f'{base}.srt')
                        
                        print(f"📁 Looking for audio files in: {mini_folder}")
                        
                        # Validate mini_folder exists
                        if not mini_folder or not os.path.exists(mini_folder):
                            print(f"❌ Audio folder not found: {mini_folder}")
                            print(f"ℹ️ Hint: Run TTS generation first to create audio files")
                            error_count += 1
                            continue
                        
                        # Check if folder has audio files
                        audio_files = [f for f in os.listdir(mini_folder) if f.lower().endswith('.mp3')]
                        if not audio_files:
                            print(f"❌ No MP3 files found in: {mini_folder}")
                            error_count += 1
                            continue
                        
                        print(f"🎵 Found {len(audio_files)} audio files")
                        
                        # Generate SRT
                        if generate_srt_from_folder(mini_folder, paragraphs, srt_path):
                            success_count += 1
                            print(f"✅ Created SRT: {srt_path}")
                        else:
                            print(f"❌ Failed to create SRT for: {base}")
                            error_count += 1
                            
                    except Exception as e:
                        print(f"❌ Error creating SRT for {path}: {e}")
                        error_count += 1
                        import traceback
                        traceback.print_exc()
                
                # Show result
                from ttkbootstrap.dialogs import Messagebox
                if success_count > 0:
                    message = f"✅ Đã tạo {success_count} file SRT thành công"
                    if error_count > 0:
                        message += f"\n❌ {error_count} file lỗi"
                    self.master.after(0, lambda: Messagebox.show_info(message))
                else:
                    self.master.after(0, lambda: Messagebox.show_error(f"❌ Không tạo được file SRT nào!\n{error_count} file lỗi\n\nHãy chạy TTS generation trước để tạo audio files."))
            
            import threading
            threading.Thread(target=work, daemon=True).start()
            
        except Exception as e:
            print(f"Error creating SRT: {e}")
    
    def import_srt_folder(self) -> None:
        """Import SRT folder - similar to TXT folder import but for SRT files"""
        try:
            from tkinter import filedialog
            from ttkbootstrap.dialogs import Messagebox
            
            # Ask user to select SRT folder
            folder = filedialog.askdirectory(title="Chọn thư mục chứa file .srt")
            if not folder:
                return
            
            print(f"📁 Selected SRT folder: {folder}")
            
            # Scan for SRT files
            import os
            srt_files = []
            for name in os.listdir(folder):
                if name.lower().endswith(".srt"):
                    srt_files.append(os.path.join(folder, name))
            
            # Sort files using natural sort (file1.srt, file2.srt, file10.srt instead of file1.srt, file10.srt, file2.srt)
            srt_files.sort(key=lambda path: self._natural_sort_key(os.path.basename(path)))
            
            if not srt_files:
                Messagebox.show_warning("Không tìm thấy file .srt nào trong thư mục đã chọn!")
                return
            
            print(f"🎯 Found {len(srt_files)} SRT files")
            
            # Update batch panel with SRT files (reuse existing logic)
            self.batch_panel.selected_folder = folder
            self.batch_panel.folder_entry.delete(0, "end")
            self.batch_panel.folder_entry.insert(0, folder)
            self.batch_panel.txt_files = srt_files  # Reuse txt_files list for SRT files
            self.batch_panel.current_file_type = "srt"  # Set file type to SRT
            
            # Update batch table
            rows = []
            for idx, path in enumerate(srt_files, start=1):
                fname = os.path.basename(path)
                rows.append([str(idx), fname, "Queued"])
            
            self.batch_panel.file_table.build_table_data(
                coldata=self.batch_panel._coldata, 
                rowdata=rows
            )
            
            # Load SRT preview into subtitles panel
            self.load_srt_preview(srt_files)
            
            # Update button states - disable "Tạo srt" button
            self.update_button_states("srt")
            
            Messagebox.show_info(f"✅ Đã import {len(srt_files)} file SRT")
            
        except Exception as e:
            print(f"❌ Error importing SRT folder: {e}")
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error(f"Lỗi import SRT folder: {e}")
    
    def load_srt_preview(self, srt_files: list) -> None:
        """Load SRT files preview into subtitles panel"""
        def load_preview():
            try:
                print(f"📄 Loading SRT preview for {len(srt_files)} files...")
                
                # Clear existing subtitles
                self.master.after(0, lambda: self.subtitles_panel.clear())
                
                # Reset stats
                self.master.after(0, lambda: setattr(self, 'completed_count', 0))
                self.master.after(0, lambda: setattr(self, 'error_count', 0))
                self.master.after(0, lambda: self.update_result_stats(0, 0))
                
                total_entries = 0
                
                for file_idx, path in enumerate(srt_files, start=1):
                    try:
                        import os
                        
                        # Check if file exists and not empty
                        if not os.path.exists(path) or os.path.getsize(path) == 0:
                            print(f"⚠️ SRT file empty or not found: {path}")
                            continue
                        
                        # Read SRT content
                        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read().strip()
                        
                        if not content:
                            print(f"⚠️ SRT file has no content: {path}")
                            continue
                        
                        # Extract only text content from SRT (ignore STT numbers and timing)
                        text_lines = self.extract_srt_text_only(content)
                        print(f"📄 File {file_idx}: {os.path.basename(path)} - {len(text_lines)} text lines extracted")
                        
                        # Add text lines to subtitle table (like TXT paragraphs)
                        for entry_idx, text_line in enumerate(text_lines, start=1):
                            row_id = f"{file_idx}-{entry_idx}"
                            
                            # Preview text (truncate if too long)
                            preview_text = text_line[:80] + "..." if len(text_line) > 80 else text_line
                            
                            # Add row to subtitles panel in main thread
                            def add_row(rid=row_id, out="", time="", txt=preview_text, vnum="", stat="📄 SRT→TTS"):
                                self.subtitles_panel.add_subtitle_row(rid, out, time, txt, vnum, stat)
                            
                            self.master.after(0, add_row)
                            total_entries += 1
                        
                    except Exception as e:
                        print(f"❌ Error loading SRT preview for {path}: {e}")
                
                # Update stats in main thread
                self.master.after(0, lambda: self.subtitles_panel.update_stats(0, 0, total_entries, 0))
                
                print(f"✅ Loaded SRT preview: {total_entries} entries from {len(srt_files)} files")
                
            except Exception as e:
                print(f"❌ Error in load_srt_preview: {e}")
                import traceback
                traceback.print_exc()
        
        # Run in background thread
        import threading
        thread = threading.Thread(target=load_preview, daemon=True)
        thread.start()
    
    def extract_srt_text_only(self, content: str) -> list:
        """Extract only text content from SRT file, ignore STT numbers and timing"""
        try:
            text_lines = []
            
            # Normalize line endings
            content = content.replace('\r\n', '\n').replace('\r', '\n')
            
            # Split by double newlines (SRT block separator)
            blocks = content.strip().split('\n\n')
            
            for block in blocks:
                block = block.strip()
                if not block:
                    continue
                
                lines = [line.strip() for line in block.split('\n') if line.strip()]
                
                if len(lines) < 2:
                    # Invalid block (need at least index + timing)
                    continue
                
                # Line 0: index (number)
                # Line 1: timing (00:00:00,000 --> 00:00:00,000)
                # Lines 2+: text content
                
                # Check if line 0 is a number (index)
                try:
                    int(lines[0])
                except ValueError:
                    # Not a valid index, might be malformed SRT - skip
                    continue
                
                # Check if line 1 contains timing pattern (-->)
                if '-->' not in lines[1]:
                    # Not a valid timing line - skip
                    continue
                
                # Extract text (lines 2+)
                if len(lines) >= 3:
                    text_content = '\n'.join(lines[2:]).strip()
                    
                    if text_content:  # Only add non-empty text
                        text_lines.append(text_content)
            
            print(f"📝 Extracted {len(text_lines)} text lines from SRT")
            return text_lines
            
        except Exception as e:
            print(f"❌ Error extracting SRT text: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def load_proxy(self) -> None:
        """Load and test proxy configuration"""
        from ttkbootstrap.dialogs import Messagebox
        
        if not self.proxy_service:
            Messagebox.show_error("Proxy service not available")
            return
        
        def work():
            try:
                # Load proxy config from DB
                config = self.proxy_service.get_proxy_config()
                if not config:
                    def show_err():
                        Messagebox.show_error("Không tìm thấy cấu hình proxy cho user này")
                    self.master.after(0, show_err)
                    return
                
                # Reset IP to get new one
                new_ip = self.proxy_service.reset_proxy_ip()
                if new_ip:
                    def show_success():
                        Messagebox.show_info(f"✅ Proxy loaded!\nNew IP: {new_ip}")
                    self.master.after(0, show_success)
                    self.master.after(0, lambda: self.proxy_combo.set("ACTIVE"))
                else:
                    def show_warn():
                        Messagebox.show_warning("Không thể reset proxy IP")
                    self.master.after(0, show_warn)
                    
            except Exception as e:
                print(f"❌ Error loading proxy: {e}")
                def show_error():
                    Messagebox.show_error(f"Lỗi: {e}")
                self.master.after(0, show_error)
        
        import threading
        threading.Thread(target=work, daemon=True).start()
    
    def load_account_info(self) -> None:
        """Load user account information from Supabase with retry logic"""
        if not self.user_service or not self.user_id:
            self.status_bar.show_error("User service not available")
            return
        
        # Show loading state
        self.status_bar.show_loading()
        
        # Load account info in background thread with retry
        def load_info():
            import time
            max_retries = 3
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    # Small delay before first attempt to let DB connection stabilize
                    if attempt > 0:
                        wait_time = 1.0 * attempt  # 1s, 2s
                        print(f"🔄 Retrying load account info (attempt {attempt + 1}/{max_retries}) after {wait_time}s...")
                        time.sleep(wait_time)
                    elif attempt == 0:
                        # First attempt: small delay to let login complete
                        time.sleep(0.3)
                    
                    account_info = self.user_service.get_user_account_info(self.user_id)
                    
                    # Check if successful
                    if account_info and 'error' not in account_info:
                        # Update UI in main thread
                        self.master.after(0, lambda: self.status_bar.update_account_info(account_info))
                        
                        # Start credit refresh if needed
                        self.start_credit_refresh()
                        
                        print(f"✅ Account info loaded successfully (attempt {attempt + 1})")
                        return  # Success, exit retry loop
                    else:
                        error_msg = account_info.get('error', 'Unknown error') if account_info else 'No data returned'
                        last_error = error_msg
                        print(f"⚠️ Load account info failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                        
                        # If it's "User not found" and we still have retries, continue
                        if attempt < max_retries - 1:
                            continue
                        else:
                            # Last attempt failed, show error
                            self.master.after(0, lambda msg=error_msg: self.status_bar.show_error(f"Không thể tải thông tin tài khoản: {msg}"))
                            return
                    
                except Exception as e:
                    last_error = str(e)
                    print(f"❌ Error loading account info (attempt {attempt + 1}/{max_retries}): {e}")
                    
                    if attempt < max_retries - 1:
                        continue  # Retry
                    else:
                        # Last attempt failed
                        self.master.after(0, lambda msg=last_error: self.status_bar.show_error(f"Lỗi: {msg}"))
                        return
            
            # All retries exhausted
            if last_error:
                self.master.after(0, lambda msg=last_error: self.status_bar.show_error(f"Không thể tải thông tin sau {max_retries} lần thử: {msg}"))
        
        thread = threading.Thread(target=load_info, daemon=True)
        thread.start()
    
    def start_credit_refresh(self) -> None:
        """Start periodic credit refresh with background updates"""
        def refresh_credits():
            try:
                if self.credit_checker and self.user_id:
                    # Use background refresh with parallel processing (100 keys per batch, 20 workers)
                    credit_info = asyncio.run(self.credit_checker.background_credit_refresh(
                        self.user_id, 
                        max_keys_per_batch=100, 
                        max_workers=20
                    ))
                    
                    if 'error' not in credit_info:
                        updated = credit_info.get('updated', 0)
                        batches = credit_info.get('batches_processed', 0)
                        print(f"🔄 Background refresh: Updated {updated} keys in {batches} batches")

                        # After keys are refreshed, update UI using character-based quota
                        if self.credit_tracker:
                            remaining_chars = self.credit_tracker.get_remaining_characters()
                            self.master.after(0, lambda: self.status_bar.update_credits_only(remaining_chars))
                
                # Schedule next refresh in 5 minutes (give time for large batches)
                self.master.after(300000, self.start_credit_refresh)  # 5 minutes = 300000 ms
                
            except Exception as e:
                print(f"Error refreshing credits: {e}")
                # Retry in 2 minutes on error
                self.master.after(120000, self.start_credit_refresh)
        
        thread = threading.Thread(target=refresh_credits, daemon=True)
        thread.start()
    
    def get_next_api_key(self) -> str:
        """Get next available API key for TTS generation"""
        if not self.credit_checker or not self.user_id:
            return None
        
        try:
            return asyncio.run(self.credit_checker.get_next_available_key(self.user_id))
        except Exception as e:
            print(f"Error getting API key: {e}")
            return None
    
    # def upload_api_key_file(self, file_path: str) -> bool:
    #     """Disabled - keys are already in database - REMOVED"""
    #     pass
    
    # def update_key_upload_panel(self, account_info: dict) -> None:
    #     """Update key upload panel with account information - REMOVED"""
    #     pass
    
    def force_refresh_account_info(self) -> None:
        """Force immediate refresh of account information"""
        print("🔄 Force refreshing account info...")
        
        def refresh_now():
            try:
                if not self.user_service or not self.user_id:
                    return
                
                # Show loading
                self.master.after(0, lambda: self.status_bar.show_loading())
                
                # Get fresh account info
                account_info = self.user_service.get_user_account_info(self.user_id)
                print(f"📊 Account info refreshed: {account_info}")
                
                # Update UI immediately
                self.master.after(0, lambda: self.status_bar.update_account_info(account_info))
                
                # Update key upload panel - REMOVED
                # if account_info and 'error' not in account_info:
                #     self.master.after(0, lambda: self.update_key_upload_panel(account_info))
                
            except Exception as e:
                print(f"❌ Error force refreshing account info: {e}")
                self.master.after(0, lambda: self.status_bar.show_error(str(e)))
        
        # Run in background thread
        thread = threading.Thread(target=refresh_now, daemon=True)
        thread.start()
    
    def trigger_credit_check(self) -> None:
        """Trigger fast credit check (optimized)"""
        print("💳 Triggering fast credit check...")
        
        def check_credits():
            try:
                if not self.credit_tracker or not self.user_id:
                    return

                # Use accurate tracker to read remaining characters in current subscription
                remaining_chars = self.credit_tracker.get_remaining_characters()
                print(f"💰 Remaining characters: {remaining_chars:,}")

                # Update status bar with remaining characters
                self.master.after(0, lambda: self.status_bar.update_credits_only(remaining_chars))
                
            except Exception as e:
                print(f"❌ Error checking credits: {e}")
        
        # Run in background thread
        thread = threading.Thread(target=check_credits, daemon=True)
        thread.start()
    
    # def add_refresh_button(self) -> None:
    #     """Add manual refresh button for testing - REMOVED"""
    #     pass
    
    # def manual_refresh(self) -> None:
    #     """Manual refresh for testing purposes - REMOVED"""
    #     pass
    
    # def trigger_full_batch_check(self, batch_size: int = 100) -> None:
    #     """Trigger full batch credit check (for manual use) - REMOVED"""
    #     pass
    
    def load_user_config(self) -> None:
        """Load user configuration from local JSON file"""
        if not self.user_config_service:
            print("💾 No user config service available")
            return
        
        try:
            print(f"💾 Loading user config from local file")
            config = self.user_config_service.load_user_config()
            
            if not config:
                print("💾 No saved config found, using defaults")
                return
            
            print(f"💾 Loaded config: {config}")
            
            # Load voice settings
            if config.get('voice_id'):
                self.voice_panel.name_entry.delete(0, "end")
                self.voice_panel.name_entry.insert(0, config['voice_id'])
            
            if config.get('voice_name'):
                # Try to set voice combo if name exists in saved voices
                voice_names = self.voice_panel.voice_combo['values']
                if config['voice_name'] in voice_names:
                    # Temporarily make combobox writable to set value
                    self.voice_panel.voice_combo.config(state="normal")
                    self.voice_panel.voice_combo.set(config['voice_name'])
                    self.voice_panel.voice_combo.config(state="readonly")
                else:
                    # If voice name not in current values, add it and set it
                    current_values = list(voice_names) if voice_names else []
                    if config['voice_name'] not in current_values:
                        current_values.append(config['voice_name'])
                        self.voice_panel.voice_combo.config(state="normal")
                        self.voice_panel.voice_combo['values'] = current_values
                        self.voice_panel.voice_combo.set(config['voice_name'])
                        self.voice_panel.voice_combo.config(state="readonly")
            
            if config.get('model_name'):
                # Temporarily make model combobox writable to set value
                self.voice_panel.model_combo.config(state="normal")
                self.voice_panel.model_combo.set(config['model_name'])
                self.voice_panel.model_combo.config(state="readonly")
            
            # Load voice parameters
            self.voice_panel.stability_var.set(int(config.get('stability', 0.5) * 100))
            self.voice_panel.similarity_var.set(int(config.get('similarity_boost', 0.75) * 100))
            self.voice_panel.style_var.set(int(config.get('style', 0.0) * 100))
            self.voice_panel.speaker_boost_var.set(bool(config.get('use_speaker_boost', False)))
            
            # Load UI settings
            # self.loop_var removed - skipping loop_enabled setting
            # self.loop_var.set(bool(config.get('loop_enabled', True)))
            self.auto_split_var.set(bool(config.get('auto_split_enabled', True)))
            if hasattr(self.batch_panel, 'auto_srt_var'):
                self.batch_panel.auto_srt_var.set(bool(config.get('auto_srt_enabled', False)))
            
            # Load proxy settings
            if hasattr(self, 'proxy_combo'):
                self.proxy_combo.set(config.get('proxy_mode', 'FREE'))
            # Clamp saved thread count to [1,5] to respect app limits
            try:
                saved_threads = int(config.get('thread_count', 3))
            except Exception:
                saved_threads = 3
            clamped_threads = max(1, min(5, saved_threads))
            if clamped_threads != saved_threads:
                print(f"ℹ️ Adjusted saved thread_count from {saved_threads} to {clamped_threads} (limits 1..5)")
            self.thread_var.set(clamped_threads)  # Default: 5 workers
            
            # Last folder loading DISABLED - không tự động load folder nữa
            # Người dùng sẽ chọn folder mới mỗi lần sử dụng
            
            print("✅ User config loaded successfully")
            
            # Trigger auto-save after loading config to ensure it's saved properly
            if hasattr(self, 'save_current_config'):
                self.master.after(1000, self.save_current_config)  # Save after 1 second delay
            
        except Exception as e:
            print(f"❌ Error loading user config: {e}")
            import traceback
            traceback.print_exc()
    
    def _natural_sort_key(self, text: str) -> list:
        """
        Natural sort key function for sorting filenames.
        Converts text to a list of strings and numbers for proper natural sorting.
        Example: "file10.txt" -> ["file", 10, ".txt"]
        """
        def convert(text_part):
            return int(text_part) if text_part.isdigit() else text_part.lower()
        
        return [convert(c) for c in re.split(r'(\d+)', text)]

    def auto_load_folder_files(self, folder_path: str) -> None:
        """Auto load files from saved folder path"""
        try:
            import os
            if not os.path.exists(folder_path):
                print(f"❌ Last folder no longer exists: {folder_path}")
                return
            
            # Scan txt and srt files
            files = []
            txt_count = 0
            srt_count = 0
            for name in os.listdir(folder_path):
                if name.lower().endswith(".txt"):
                    files.append(os.path.join(folder_path, name))
                    txt_count += 1
                elif name.lower().endswith(".srt"):
                    files.append(os.path.join(folder_path, name))
                    srt_count += 1
            
            # Sort files using natural sort (file1.txt, file2.txt, file10.txt instead of file1.txt, file10.txt, file2.txt)
            files.sort(key=lambda path: self._natural_sort_key(os.path.basename(path)))
            
            if files:
                self.batch_panel.txt_files = files
                
                # Determine file type
                if srt_count > 0 and txt_count == 0:
                    self.batch_panel.current_file_type = "srt"
                elif txt_count > 0 and srt_count == 0:
                    self.batch_panel.current_file_type = "txt"
                elif txt_count > 0 and srt_count > 0:
                    # Mixed files - default to txt
                    self.batch_panel.current_file_type = "txt"
                else:
                    self.batch_panel.current_file_type = "txt"
                
                # Reload table
                rows = []
                for idx, path in enumerate(files, start=1):
                    fname = os.path.basename(path)
                    rows.append([str(idx), fname, "Queued"])
                self.batch_panel.file_table.build_table_data(
                    coldata=self.batch_panel._coldata, 
                    rowdata=rows
                )
                print(f"📁 Auto-loaded {len(files)} files from last folder ({self.batch_panel.current_file_type})")
                
                # Trigger callback to load subtitles preview
                if hasattr(self, 'on_folder_selected'):
                    try:
                        self.on_folder_selected(files, self.batch_panel.current_file_type)
                    except Exception as e:
                        print(f"❌ Error loading preview from auto-load: {e}")
        except Exception as e:
            print(f"❌ Error auto-loading folder files: {e}")
    
    def setup_auto_save(self) -> None:
        """Setup auto-save when window is closing"""
        if not self.user_config_service or not self.user_id:
            print("💾 No user config service or user ID available for auto-save")
            return
        
        def on_closing():
            """Save config and cleanup threads before closing"""
            try:
                print("💾 Auto-saving user config before closing...")
                self.save_current_config()
                
                # Stop proxy service auto-rotation thread
                if hasattr(self, 'proxy_service') and self.proxy_service:
                    try:
                        print("🛑 Stopping proxy service auto-rotation...")
                        self.proxy_service.stop_auto_rotate()
                    except Exception as e:
                        print(f"⚠️ Error stopping proxy service: {e}")
                
                # Stop credit checker background refresh if running
                if hasattr(self, 'credit_checker') and self.credit_checker:
                    try:
                        print("🛑 Stopping credit checker background refresh...")
                        if hasattr(self.credit_checker, 'stop_background_refresh'):
                            self.credit_checker.stop_background_refresh()
                    except Exception as e:
                        print(f"⚠️ Error stopping credit checker: {e}")
                
                # Stop TTS runner if running
                if hasattr(self, 'tts_runner') and self.tts_runner:
                    try:
                        print("🛑 Stopping TTS runner...")
                        if hasattr(self.tts_runner, 'stop'):
                            self.tts_runner.stop()
                    except Exception as e:
                        print(f"⚠️ Error stopping TTS runner: {e}")
                
                print("✅ Cleanup completed, closing window...")
                # Call original destroy
                self.master.destroy()
                # Force exit to ensure all threads are terminated
                import os
                os._exit(0)
            except Exception as e:
                print(f"❌ Error during cleanup: {e}")
                # Still close the window even if cleanup fails
                try:
                    self.master.destroy()
                except:
                    pass
                import os
                os._exit(0)
        
        # Bind the close event
        self.master.protocol("WM_DELETE_WINDOW", on_closing)
        print("💾 Auto-save setup completed")
    
    def save_current_config(self) -> bool:
        """Save current UI configuration to local JSON file"""
        if not self.user_config_service:
            return False
        
        try:
            # Collect current configuration from UI
            config = {
                # Voice settings
                'voice_id': self.voice_panel.name_entry.get().strip(),
                'voice_name': self.voice_panel.voice_combo.get().strip(),
                'model_name': self.voice_panel.model_combo.get().strip(),
                
                # Voice parameters (convert from 0-100 to 0.0-1.0)
                'stability': float(self.voice_panel.stability_var.get()) / 100.0,
                'similarity_boost': float(self.voice_panel.similarity_var.get()) / 100.0,
                'style': float(self.voice_panel.style_var.get()) / 100.0,
                'use_speaker_boost': bool(self.voice_panel.speaker_boost_var.get()),
                
                # UI settings
                # 'loop_enabled': bool(self.loop_var.get()), # Removed
                'auto_split_enabled': bool(self.auto_split_var.get()),
                'auto_srt_enabled': bool(self.batch_panel.auto_srt_var.get()) if hasattr(self.batch_panel, 'auto_srt_var') else False,
                
                # Proxy settings
                'proxy_mode': self.proxy_combo.get() if hasattr(self, 'proxy_combo') else 'FREE',
                'thread_count': int(self.thread_var.get()),
                
                # Last used folder - DISABLED (không lưu đường dẫn nữa)
                'last_folder_path': '',
            }
            
            # Save advanced settings if available
            if hasattr(self, '_settings'):
                advanced = self._settings.get_advanced_settings()
                config.update({
                    'pause_between_segments_enabled': advanced.get('pause_between_segments_enabled', True),
                    'segment_gap_seconds': advanced.get('segment_gap_seconds', 1.3),
                    'segment_count': advanced.get('segment_count', 5),
                    'srt_split_enabled': advanced.get('srt_split_enabled', False),
                    'per_char_enabled': advanced.get('per_char_enabled', True),
                    'comma_pause': advanced.get('comma_pause', 0.3),
                    'dot_pause': advanced.get('dot_pause', 0.5),
                    'download_type': advanced.get('download_type', '1 <ORIGINAL>'),
                    'max_chars_per_line': advanced.get('max_chars_per_line', 1000),
                })
            
            print(f"💾 Saving config: {config}")
            success = self.user_config_service.save_user_config(config=config)
            
            if success:
                print("✅ User config saved successfully")
            else:
                print("❌ Failed to save user config")
            
            return success
            
        except Exception as e:
            print(f"❌ Error saving user config: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def setup_control_auto_save(self) -> None:
        """Setup auto-save callbacks for all main window controls"""
        try:
            # Bind controls to auto-save
            self.loop_var.trace('w', lambda *args: self._schedule_auto_save())
            self.auto_split_var.trace('w', lambda *args: self._schedule_auto_save())
            self.thread_var.trace('w', lambda *args: self._schedule_auto_save())
            
            # Batch panel auto-save
            if hasattr(self.batch_panel, 'auto_srt_var'):
                self.batch_panel.auto_srt_var.trace('w', lambda *args: self._schedule_auto_save())
            
            # Bind folder selection to auto-save
            if hasattr(self.batch_panel, 'folder_entry'):
                self.batch_panel.folder_entry.bind('<KeyRelease>', lambda e: self._schedule_auto_save())
                self.batch_panel.folder_entry.bind('<FocusOut>', lambda e: self._schedule_auto_save())
            
            print("🔄 Main window auto-save callbacks setup completed")
            
        except Exception as e:
            print(f"❌ Error setting up control auto-save: {e}")
    
    def _schedule_auto_save(self) -> None:
        """Schedule auto-save with debounce"""
        try:
            # Cancel previous save timer if exists
            if hasattr(self, '_main_save_timer_id'):
                try:
                    self.master.after_cancel(self._main_save_timer_id)
                except:
                    pass
            
            # Schedule new save with 2 second delay (debounce)
            self._main_save_timer_id = self.master.after(2000, self.save_current_config)
            
        except Exception as e:
            print(f"❌ Error scheduling auto-save: {e}")
    
    def get_current_voice_id(self) -> str:
        """Get current voice ID with improved fallback logic"""
        try:
            # Priority 1: Voice ID field (most reliable)
            voice_id = self.voice_panel.name_entry.get().strip()
            if voice_id and len(voice_id) >= 10:  # Valid ElevenLabs voice ID is usually 20+ chars
                print(f"🎯 Using Voice ID from field: {voice_id}")
                return voice_id
            
            # Priority 2: Get from selected voice in combo
            voice_display = self.voice_panel.voice_combo.get().strip()
            
            # Try to match from saved voices first
            if voice_display and hasattr(self, '_saved_voices'):
                for v in self._saved_voices:
                    if v.name == voice_display:
                        print(f"🔍 Found voice_id from saved voices: {v.voice_id}")
                        return v.voice_id
            
            # Try to match from search results
            if voice_display and hasattr(self.voice_panel, '_voice_results'):
                for r in self.voice_panel._voice_results:
                    if r.get('name') == voice_display:
                        voice_id = r.get('voice_id', '')
                        if voice_id:
                            print(f"🔍 Found voice_id from search results: {voice_id}")
                            return voice_id
            
            # Priority 3: Extract ID from combo text if it contains " - "
            if voice_display and " - " in voice_display:
                potential_id = voice_display.split(" - ")[0].strip()
                if len(potential_id) >= 10:
                    print(f"🔍 Extracted voice_id from combo: {potential_id}")
                    return potential_id
            
            # Priority 4: Use default voice if nothing else works
            default_voice = "pNInz6obpgDQGcFmaJgB"  # Adam voice
            print(f"🔄 Using default voice ID: {default_voice}")
            return default_voice
            
        except Exception as e:
            print(f"❌ Error getting voice ID: {e}")
            # Return default on error
            return "pNInz6obpgDQGcFmaJgB"
    
    def validate_voice_settings(self) -> bool:
        """Validate current voice settings"""
        try:
            voice_id = self.get_current_voice_id()
            model = self.voice_panel.model_combo.get().strip()
            
            if not voice_id:
                print("❌ No voice ID selected")
                return False
            
            if not model:
                print("❌ No model selected")
                return False
            
            # Validate voice parameters
            try:
                stability = float(self.voice_panel.stability_var.get())
                similarity = float(self.voice_panel.similarity_var.get())
                style = float(self.voice_panel.style_var.get())
                
                if not (0 <= stability <= 100):
                    print("❌ Stability must be 0-100")
                    return False
                if not (0 <= similarity <= 100):
                    print("❌ Similarity must be 0-100")
                    return False
                if not (0 <= style <= 100):
                    print("❌ Style must be 0-100")
                    return False
                    
            except ValueError as e:
                print(f"❌ Invalid voice parameter values: {e}")
                return False
            
            print("✅ Voice settings validated successfully")
            return True
            
        except Exception as e:
            print(f"❌ Error validating voice settings: {e}")
            return False
    
    def check_for_updates_async(self):
        """Check for updates in background thread (non-blocking)"""
        print(f"🔄 [UPDATE] check_for_updates_async called")
        
        if not self.update_service:
            print(f"⚠️ [UPDATE] Update service not initialized")
            return
        
        print(f"✅ [UPDATE] Update service initialized, starting check in background...")
        
        def check_update():
            try:
                import threading
                import time
                print(f"🔄 [UPDATE] Background thread started, waiting 2 seconds...")
                # Đợi một chút để UI load xong
                time.sleep(2)
                
                print(f"🔄 [UPDATE] Calling check_for_updates()...")
                update_info = self.update_service.check_for_updates()
                
                if update_info:
                    print(f"🆕 [UPDATE] Update found! Showing dialog...")
                    # Có update mới - hiển thị dialog trong main thread
                    self.master.after(0, lambda: self.show_update_dialog(update_info))
                else:
                    print(f"✅ [UPDATE] No update available")
            except Exception as e:
                print(f"❌ [UPDATE] Error in check_update thread: {e}")
                import traceback
                traceback.print_exc()
                logging.error(f"Error checking for updates: {e}")
        
        # Chạy trong thread riêng
        import threading
        thread = threading.Thread(target=check_update, daemon=True)
        thread.start()
        print(f"✅ [UPDATE] Background thread started")
    
    def show_update_dialog(self, update_info):
        """Hiển thị dialog update"""
        try:
            from ui.components.update_dialog import UpdateDialog
            dialog = UpdateDialog(self.master, self.update_service, update_info)
            dialog.show_modal()
        except Exception as e:
            logging.error(f"Error showing update dialog: {e}")

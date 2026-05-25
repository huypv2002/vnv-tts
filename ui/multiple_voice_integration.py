"""
Multiple Voice Integration for MainWindow.
This is a NEW file - does not modify any existing code.

Contains methods to integrate Multiple Voice Panel into MainWindow.
These methods should be called from MainWindow.__init__ or added via monkey patching.

USAGE:
    In MainWindow.__init__, add at the end:
    
    from ui.multiple_voice_integration import setup_multiple_voice_integration
    setup_multiple_voice_integration(self)
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import ttkbootstrap as tb

if TYPE_CHECKING:
    from ui.main_window import MainWindow


def setup_multiple_voice_integration(main_window: 'MainWindow') -> None:
    """
    Setup multiple voice integration for MainWindow.
    
    This function adds:
    - Multiple voice panel UI
    - Toggle checkbox for enabling/disabling multiple voice mode
    - New batch processing method for multiple voices
    
    Args:
        main_window: MainWindow instance to integrate with.
    """
    # Import here to avoid circular imports
    from ui.components.multiple_voice_panel import MultipleVoicePanel
    from services.multiple_voice_config import MultipleVoiceConfig
    
    # Store config reference
    main_window._multi_voice_config = MultipleVoiceConfig()
    
    # Create multiple voice panel (initially hidden)
    _create_multiple_voice_panel(main_window)
    
    # Add toggle checkbox to options
    _add_multiple_voice_toggle(main_window)
    
    # Add new methods to main_window
    main_window.start_generate_txt_batch_multi_voice = lambda: _start_generate_txt_batch_multi_voice(main_window)
    main_window.on_multiple_voice_toggle = lambda: _on_multiple_voice_toggle(main_window)
    
    print("✅ Multiple Voice integration setup complete")


def _create_multiple_voice_panel(main_window: 'MainWindow') -> None:
    """Create the multiple voice panel UI."""
    from ui.components.multiple_voice_panel import MultipleVoicePanel
    
    try:
        # Get main_frame - navigate up from voice_panel
        # voice_panel.frame -> voice_container -> top_wrapper -> main_frame
        voice_container = main_window.voice_panel.frame.master  # voice_container
        top_wrapper = voice_container.master  # top_wrapper (Labelframe "Cấu hình chung")
        main_frame = top_wrapper.master  # main_frame
        
        # Create a collapsible frame for multiple voice
        multi_voice_wrapper = tb.Labelframe(
            main_frame, 
            text="🎭 Multiple Voice (Nhiều giọng đọc)", 
            padding=10, 
            style='Bordered.TLabelframe'
        )
        
        # Create the panel
        main_window.multiple_voice_panel = MultipleVoicePanel(multi_voice_wrapper)
        main_window.multiple_voice_panel.frame.pack(fill="both", expand=True)
        
        # Store wrapper reference
        main_window._multi_voice_wrapper = multi_voice_wrapper
        
        # Initially hide if not enabled
        if main_window._multi_voice_config.enabled:
            # Pack after top_wrapper
            multi_voice_wrapper.pack(fill="x", pady=(0, 10), after=top_wrapper)
            print("✅ Multiple Voice panel shown (enabled in config)")
        else:
            # Don't pack - keep hidden
            print("ℹ️ Multiple Voice panel hidden (disabled in config)")
            
    except Exception as e:
        print(f"❌ Error creating multiple voice panel: {e}")
        import traceback
        traceback.print_exc()


def _add_multiple_voice_toggle(main_window: 'MainWindow') -> None:
    """Add toggle checkbox for multiple voice mode."""
    # Create toggle variable
    main_window.multi_voice_enabled_var = tb.BooleanVar(value=main_window._multi_voice_config.enabled)
    
    # Find the left_col in batch_panel (first child of main_container)
    # Structure: batch_panel.frame -> main_container -> left_col
    try:
        main_container = main_window.batch_panel.frame.winfo_children()[0]  # main_container
        left_col = main_container.winfo_children()[0]  # left_col (Bordered.TFrame)
        
        # Create a new frame for the toggle, add it after middle_row (before controls_container)
        toggle_frame = tb.Frame(left_col, style='Gray.TFrame')
        toggle_frame.pack(fill="x", pady=(5, 5))
        
        toggle_cb = tb.Checkbutton(
            toggle_frame,
            text="🎭 Multiple Voice",
            variable=main_window.multi_voice_enabled_var,
            style='TCheckbutton',
            command=lambda: _on_multiple_voice_toggle(main_window)
        )
        toggle_cb.pack(side="left")
        
        main_window._multi_voice_toggle = toggle_cb
        print("✅ Added Multiple Voice toggle checkbox to batch panel")
        
    except Exception as e:
        print(f"⚠️ Could not add toggle to batch panel: {e}")
        # Fallback: try to add to the main frame directly
        try:
            # Get main_frame from voice_panel hierarchy
            main_frame = main_window.voice_panel.frame.master.master
            
            toggle_frame = tb.Frame(main_frame, style='Gray.TFrame')
            toggle_frame.pack(fill="x", pady=(5, 0))
            
            toggle_cb = tb.Checkbutton(
                toggle_frame,
                text="🎭 Sử dụng Multiple Voice",
                variable=main_window.multi_voice_enabled_var,
                style='TCheckbutton',
                command=lambda: _on_multiple_voice_toggle(main_window)
            )
            toggle_cb.pack(side="left", padx=(10, 0))
            
            main_window._multi_voice_toggle = toggle_cb
            print("✅ Added Multiple Voice toggle checkbox (fallback location)")
        except Exception as e2:
            print(f"❌ Failed to add toggle checkbox: {e2}")


def _on_multiple_voice_toggle(main_window: 'MainWindow') -> None:
    """Handle multiple voice toggle change."""
    enabled = main_window.multi_voice_enabled_var.get()
    
    # Update config
    main_window._multi_voice_config.enabled = enabled
    main_window._multi_voice_config.save()
    
    # Show/hide multiple voice panel
    if enabled:
        # Show panel
        if hasattr(main_window, '_multi_voice_wrapper'):
            try:
                # Get top_wrapper to pack after it
                voice_container = main_window.voice_panel.frame.master
                top_wrapper = voice_container.master
                main_window._multi_voice_wrapper.pack(fill="x", pady=(0, 10), after=top_wrapper)
                print("✅ Multiple Voice mode ENABLED - panel shown")
            except Exception as e:
                print(f"⚠️ Error showing panel: {e}")
    else:
        # Hide panel
        if hasattr(main_window, '_multi_voice_wrapper'):
            main_window._multi_voice_wrapper.pack_forget()
            print("❌ Multiple Voice mode DISABLED - panel hidden")


def _start_generate_txt_batch_multi_voice(main_window: 'MainWindow') -> None:
    """
    Start batch TTS generation with multiple voices.
    
    This is a NEW method that does NOT modify the original start_generate_txt_batch.
    """
    print("🚀 start_generate_txt_batch_multi_voice called")
    
    # Import required modules
    from services.multi_voice_tts_runner import MultiVoiceTTSRunner
    from services.voice_assigner import VoiceAssigner
    from services.srt_generator import generate_srt_from_folder
    from services.ffmpeg_utils import get_ffmpeg_path
    import pathlib
    import time
    
    # Check subscription (reuse existing method)
    if hasattr(main_window, '_check_subscription_active') and not main_window._check_subscription_active():
        from ttkbootstrap.dialogs import Messagebox
        Messagebox.show_error("Gói đăng ký của bạn đã hết hạn!")
        return
    
    if main_window.is_running:
        from ttkbootstrap.dialogs import Messagebox
        Messagebox.show_warning("Đang có quá trình chạy!")
        return
    
    # Get files
    files = main_window.batch_panel.get_all_files() if hasattr(main_window.batch_panel, 'get_all_files') else []
    if not files:
        print("❌ No files selected")
        return
    
    # Get voice list from multiple voice panel
    voice_list = main_window.multiple_voice_panel.get_voice_list()
    if len(voice_list) < 2:
        from ttkbootstrap.dialogs import Messagebox
        Messagebox.show_error("Cần ít nhất 2 voice trong danh sách!")
        return
    
    # Get assignment mode
    assignment_mode = main_window.multiple_voice_panel.get_assignment_mode()
    
    print(f"📁 Files: {len(files)}")
    print(f"🎤 Voices: {len(voice_list)}")
    print(f"📋 Mode: {assignment_mode}")
    
    # Get settings
    max_workers = main_window.thread_var.get() if hasattr(main_window, 'thread_var') else 5
    max_workers = max(1, min(5, int(max_workers)))
    advanced_settings = main_window._settings.get_advanced_settings()
    
    # Create runner
    runner = MultiVoiceTTSRunner(
        main_window._key_supabase,
        main_window.user_id,
        output_dir="outputs",
        max_workers=max_workers,
        advanced_settings=advanced_settings,
        proxy_service=main_window.proxy_service if hasattr(main_window, 'proxy_service') else None,
        credit_tracker=main_window.credit_tracker if hasattr(main_window, 'credit_tracker') else None
    )
    
    # Create voice assigner
    voice_assigner = VoiceAssigner(voice_list, assignment_mode)
    
    # Reset UI
    main_window.master.after(0, lambda: main_window.subtitles_panel.clear())
    main_window.master.after(0, lambda: main_window.reset_result_stats())
    
    def work():
        main_window.is_running = True
        main_window.should_stop = False
        
        start_time = time.time()
        total_paragraphs = 0
        done_paragraphs = 0
        
        try:
            for file_idx, path in enumerate(files):
                if main_window.should_stop:
                    print("🛑 Stopped by user")
                    break
                
                print(f"📄 Processing file {file_idx + 1}/{len(files)}: {path}")
                
                # Update batch status
                main_window.master.after(0, lambda i=file_idx: main_window.batch_panel.update_status(i, 'Processing'))
                
                try:
                    # Read file
                    file_size = os.path.getsize(path)
                    if file_size == 0:
                        print(f"⚠️ Empty file: {path}")
                        main_window.master.after(0, lambda i=file_idx: main_window.batch_panel.update_status(i, 'Skipped'))
                        continue
                    
                    content = pathlib.Path(path).read_text(encoding='utf-8', errors='ignore')
                    if not content.strip():
                        main_window.master.after(0, lambda i=file_idx: main_window.batch_panel.update_status(i, 'Skipped'))
                        continue
                    
                    # Split paragraphs
                    paragraphs = runner.split_paragraphs(content)
                    total_paragraphs += len(paragraphs)
                    
                    # Add to subtitle panel
                    for p_idx, para in enumerate(paragraphs, start=1):
                        preview = para[:80] + "..." if len(para) > 80 else para
                        row_id = f"{file_idx + 1}-{p_idx}"
                        
                        # Get voice for this paragraph
                        voice = voice_assigner.get_voice_for_paragraph(p_idx - 1, file_idx)
                        voice_info = f"[{voice.voice_name}]"
                        
                        def add_row(rid=row_id, txt=preview, vinfo=voice_info):
                            main_window.subtitles_panel.add_subtitle_row(rid, "", "", txt, vinfo, "Queued")
                        main_window.master.after(0, add_row)
                    
                    # Setup output paths
                    base = os.path.splitext(os.path.basename(path))[0]
                    parent = os.path.dirname(path)
                    mini_dir = os.path.join(parent, 'tts_mini')
                    os.makedirs(mini_dir, exist_ok=True)
                    
                    # Callbacks
                    def on_para_start(p_idx, para_text):
                        row_id = f"{file_idx + 1}-{p_idx}"
                        main_window.master.after(0, lambda: main_window.subtitles_panel.update_subtitle_status(row_id, "Processing"))
                    
                    def on_para_done(p_idx, output_path):
                        nonlocal done_paragraphs
                        done_paragraphs += 1
                        row_id = f"{file_idx + 1}-{p_idx}"
                        out_file = os.path.basename(output_path) if output_path else ""
                        main_window.master.after(0, lambda: main_window.subtitles_panel.update_subtitle_status(row_id, "DONE", out_file))
                    
                    def on_voice_change(idx, voice_name, voice_id):
                        print(f"🎤 Voice change at paragraph {idx}: {voice_name}")
                    
                    # Process with multiple voices
                    runner.run_for_texts_multi_voice(
                        voice_assigner=voice_assigner,
                        paragraphs=paragraphs,
                        mini_dir=mini_dir,
                        basename=base,
                        file_index=file_idx,
                        on_paragraph_start=on_para_start,
                        on_paragraph_done=on_para_done,
                        on_voice_change=on_voice_change,
                        stop_callback=lambda: main_window.should_stop,
                    )
                    
                    # Concat pieces
                    ffmpeg_bin = get_ffmpeg_path()
                    out_total = os.path.join(parent, f'{base}.mp3')
                    paragraph_folder = os.path.join(mini_dir, base)
                    
                    if os.path.exists(paragraph_folder):
                        mp3_files = [f for f in os.listdir(paragraph_folder) if f.lower().endswith('.mp3')]
                        if mp3_files:
                            runner.concat_pieces(ffmpeg_bin, paragraph_folder, out_total, advanced_settings)
                    
                    # Generate SRT if enabled
                    if main_window.batch_panel.auto_srt_var.get():
                        srt_path = os.path.join(parent, f'{base}.srt')
                        generate_srt_from_folder(os.path.join(mini_dir, base), paragraphs, srt_path)
                    
                    # Update status
                    main_window.completed_count += 1
                    main_window.master.after(0, lambda: main_window.update_result_stats())
                    main_window.master.after(0, lambda i=file_idx: main_window.batch_panel.update_status(i, 'Completed'))
                    
                except Exception as e:
                    print(f"❌ Error processing {path}: {e}")
                    main_window.error_count += 1
                    main_window.master.after(0, lambda: main_window.update_result_stats())
                    main_window.master.after(0, lambda i=file_idx: main_window.batch_panel.update_status(i, 'Error'))
        
        finally:
            runner.finalize()
            main_window.is_running = False
            
            elapsed = int(time.time() - start_time)
            status = "Đã dừng" if main_window.should_stop else "Hoàn thành"
            
            from ttkbootstrap.dialogs import Messagebox
            main_window.master.after(0, lambda: Messagebox.show_info(
                f"{status} tạo file MP3 (Multiple Voice)!\n\n"
                f"Tổng đoạn: {total_paragraphs}\n"
                f"Hoàn thành: {done_paragraphs}\n"
                f"Thời gian: {elapsed}s",
                status
            ))
    
    # Run in background thread
    threading.Thread(target=work, daemon=True).start()


def patch_start_generate_for_multi_voice(main_window: 'MainWindow') -> None:
    """
    Patch the controls panel to use multi-voice when enabled.
    
    This creates a wrapper that checks the toggle and calls the appropriate method.
    """
    # Store original method reference
    original_on_generate = main_window.controls_panel.on_generate
    
    def patched_on_generate():
        # Check if multiple voice is enabled
        if hasattr(main_window, 'multi_voice_enabled_var') and main_window.multi_voice_enabled_var.get():
            # Use multiple voice processing
            if hasattr(main_window, 'start_generate_txt_batch_multi_voice'):
                main_window.start_generate_txt_batch_multi_voice()
            else:
                print("❌ Multi-voice method not available, using single voice")
                original_on_generate()
        else:
            # Use original single voice processing
            original_on_generate()
    
    # Replace the method
    main_window.controls_panel.on_generate = patched_on_generate
    print("✅ Patched on_generate to support multiple voice toggle")

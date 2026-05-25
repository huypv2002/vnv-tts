"""
Multi Voice TTS Runner for Multiple Voice Tab feature.
This is a NEW file - does not modify any existing code.

Wraps TTSTaskRunner to support multiple voices without modifying the original class.
"""
from __future__ import annotations

import os
import shutil
from typing import List, Tuple, Callable, Optional, Dict, Any

from models.voice_entry import VoiceEntry
from services.voice_assigner import VoiceAssigner


class MultiVoiceTTSRunner:
    """
    Wrapper class to process TTS with multiple voices.
    
    IMPORTANT: This class does NOT modify TTSTaskRunner.
    It wraps and delegates to the original runner while adding multi-voice logic.
    """
    
    def __init__(
        self,
        supabase_client,
        user_id: int,
        output_dir: str = "outputs",
        max_workers: int = 5,
        advanced_settings: Optional[Dict[str, Any]] = None,
        proxy_service=None,
        credit_tracker=None
    ) -> None:
        """
        Initialize the multi-voice TTS runner.
        
        Args:
            supabase_client: Supabase client for database operations.
            user_id: User ID for key pool.
            output_dir: Output directory for generated files.
            max_workers: Maximum number of concurrent workers.
            advanced_settings: Advanced TTS settings.
            proxy_service: Proxy service for API calls.
            credit_tracker: Credit tracker for usage monitoring.
        """
        # Import here to avoid circular imports
        from services.tts_service import TTSTaskRunner
        
        # Create the original runner instance (NOT modifying it)
        self._runner = TTSTaskRunner(
            supabase_client,
            user_id,
            output_dir,
            max_workers,
            advanced_settings,
            proxy_service,
            credit_tracker
        )
        
        self.output_dir = output_dir
        self.max_workers = max_workers
        self.advanced_settings = advanced_settings or {}
        
        # Track current voice for status updates
        self._current_voice_name: str = ""
        self._current_voice_id: str = ""
        
        print(f"✅ MultiVoiceTTSRunner initialized - max_workers={max_workers}")
    
    def run_for_texts_multi_voice(
        self,
        voice_assigner: VoiceAssigner,
        paragraphs: List[str],
        mini_dir: str,
        basename: str,
        file_index: int = 0,
        on_paragraph_start: Optional[Callable[[int, str], None]] = None,
        on_paragraph_done: Optional[Callable[[int, str], None]] = None,
        on_paragraph_error: Optional[Callable[[int, str], None]] = None,
        on_voice_change: Optional[Callable[[int, str, str], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
        on_status_update: Optional[Callable[[str, str], None]] = None,
    ) -> List[Tuple[str, str]]:
        """
        Process paragraphs with multiple voices based on VoiceAssigner.
        
        Args:
            voice_assigner: VoiceAssigner instance to determine voice for each paragraph.
            paragraphs: List of text paragraphs to process.
            mini_dir: Directory for intermediate files.
            basename: Base name for output files.
            file_index: Index of current file (for per-file mode).
            on_paragraph_start: Callback when paragraph processing starts.
            on_paragraph_done: Callback when paragraph processing completes.
            on_paragraph_error: Callback when paragraph processing fails.
            on_voice_change: Callback when voice changes (idx, voice_name, voice_id).
            stop_callback: Callback to check if processing should stop.
            on_status_update: Callback for status updates.
            
        Returns:
            List of (paragraph_id, output_path) tuples.
        """
        if not paragraphs:
            return []
        
        # Set total paragraphs for sequential mode
        voice_assigner.set_total_paragraphs(len(paragraphs))
        
        outputs: List[Tuple[str, str]] = []
        dst_folder = os.path.join(mini_dir, basename)
        os.makedirs(dst_folder, exist_ok=True)
        
        # Group paragraphs by voice for more efficient processing
        voice_groups = self._group_paragraphs_by_voice(
            paragraphs, voice_assigner, file_index
        )
        
        print(f"📊 Multi-voice processing: {len(paragraphs)} paragraphs, {len(voice_groups)} voice groups")
        
        # Process each voice group
        global_para_idx = 0
        for voice_entry, para_indices in voice_groups:
            # Check stop signal
            if stop_callback and stop_callback():
                print("🛑 Stop signal received")
                break
            
            # Notify voice change
            if on_voice_change:
                on_voice_change(global_para_idx, voice_entry.voice_name, voice_entry.voice_id)
            
            self._current_voice_name = voice_entry.voice_name
            self._current_voice_id = voice_entry.voice_id
            
            print(f"🎤 Processing with voice: {voice_entry.voice_name} ({len(para_indices)} paragraphs)")
            
            # Get paragraphs for this voice
            group_paragraphs = [paragraphs[i] for i in para_indices]
            
            # Create voice settings dict
            voice_settings = voice_entry.get_voice_settings_dict()
            
            # Create wrapped callbacks that include voice info
            def wrapped_start(p_idx, para_text, original_indices=para_indices, orig_callback=on_paragraph_start):
                actual_idx = original_indices[p_idx - 1] + 1  # Convert to 1-based
                if orig_callback:
                    orig_callback(actual_idx, para_text)
            
            def wrapped_done(p_idx, output_path, original_indices=para_indices, orig_callback=on_paragraph_done):
                actual_idx = original_indices[p_idx - 1] + 1  # Convert to 1-based
                if orig_callback:
                    orig_callback(actual_idx, output_path)
            
            def wrapped_error(p_idx, error_msg, original_indices=para_indices, orig_callback=on_paragraph_error):
                actual_idx = original_indices[p_idx - 1] + 1  # Convert to 1-based
                if orig_callback:
                    orig_callback(actual_idx, error_msg)
            
            # Process this group using original runner
            try:
                group_results = self._runner.run_for_texts(
                    voice_id=voice_entry.voice_id,
                    model_name=voice_entry.model,
                    paragraphs=group_paragraphs,
                    mini_dir=mini_dir,
                    basename=f"{basename}_voice_{voice_entry.voice_id[:8]}",
                    voice_settings=voice_settings,
                    on_paragraph_start=wrapped_start,
                    on_paragraph_done=wrapped_done,
                    on_paragraph_error=wrapped_error,
                    stop_callback=stop_callback,
                    on_status_update=on_status_update,
                )
                
                # Copy results to correct positions in dst_folder
                for result_idx, (para_id, temp_path) in enumerate(group_results):
                    if temp_path and os.path.exists(temp_path):
                        actual_idx = para_indices[result_idx] + 1  # 1-based
                        final_path = os.path.join(dst_folder, f"{actual_idx}.mp3")
                        shutil.copy2(temp_path, final_path)
                        outputs.append((f"p{actual_idx}", final_path))
                        print(f"✅ Copied {temp_path} -> {final_path}")
                
            except Exception as e:
                error_msg = str(e)
                print(f"❌ Error processing voice group {voice_entry.voice_name}: {e}")
                
                # Check if this is a voice unavailability error
                if self._is_voice_unavailable_error(error_msg):
                    print(f"⚠️ Voice {voice_entry.voice_name} is unavailable, skipping to next voice")
                    # Log warning but continue with next voice group
                    if on_paragraph_error:
                        for idx in para_indices:
                            on_paragraph_error(idx + 1, f"Voice unavailable: {voice_entry.voice_name}")
                    continue
                else:
                    # For other errors, also continue but log more details
                    print(f"⚠️ Non-voice error, continuing with next group: {error_msg}")
                    continue
            
            global_para_idx += len(para_indices)
        
        return outputs
    
    def _group_paragraphs_by_voice(
        self,
        paragraphs: List[str],
        voice_assigner: VoiceAssigner,
        file_index: int
    ) -> List[Tuple[VoiceEntry, List[int]]]:
        """
        Group paragraph indices by their assigned voice.
        
        This optimizes processing by batching paragraphs that use the same voice.
        
        Args:
            paragraphs: List of paragraphs.
            voice_assigner: VoiceAssigner to determine voice for each paragraph.
            file_index: Current file index.
            
        Returns:
            List of (VoiceEntry, [paragraph_indices]) tuples.
        """
        # For per-file mode, all paragraphs use the same voice
        if voice_assigner.mode == "per_file":
            voice = voice_assigner.get_voice_for_file(file_index)
            return [(voice, list(range(len(paragraphs))))]
        
        # For sequential mode, group consecutive paragraphs by voice
        if voice_assigner.mode == "sequential":
            groups: List[Tuple[VoiceEntry, List[int]]] = []
            current_voice = None
            current_indices: List[int] = []
            
            for idx in range(len(paragraphs)):
                voice = voice_assigner.get_voice_for_paragraph(idx, file_index)
                
                if current_voice is None:
                    current_voice = voice
                    current_indices = [idx]
                elif voice.voice_id == current_voice.voice_id:
                    current_indices.append(idx)
                else:
                    groups.append((current_voice, current_indices))
                    current_voice = voice
                    current_indices = [idx]
            
            if current_indices:
                groups.append((current_voice, current_indices))
            
            return groups
        
        # For alternating mode, we need to process in order but track voice changes
        # Group by voice but maintain order
        voice_to_indices: Dict[str, Tuple[VoiceEntry, List[int]]] = {}
        
        for idx in range(len(paragraphs)):
            voice = voice_assigner.get_voice_for_paragraph(idx, file_index)
            
            if voice.voice_id not in voice_to_indices:
                voice_to_indices[voice.voice_id] = (voice, [])
            
            voice_to_indices[voice.voice_id][1].append(idx)
        
        return list(voice_to_indices.values())
    
    # ========== Delegate methods to original runner ==========
    
    def split_paragraphs(self, content: str) -> List[str]:
        """Delegate to original runner's split_paragraphs method."""
        return self._runner.split_paragraphs(content)
    
    def concat_pieces(
        self,
        ffmpeg_bin: str,
        folder: str,
        output_path: str,
        advanced_settings: Optional[Dict[str, Any]] = None
    ) -> None:
        """Delegate to original runner's concat_pieces method."""
        return self._runner.concat_pieces(ffmpeg_bin, folder, output_path, advanced_settings)
    
    def finalize(self) -> None:
        """Delegate to original runner's finalize method."""
        return self._runner.finalize()
    
    @property
    def should_stop(self) -> bool:
        """Get stop flag from original runner."""
        return self._runner.should_stop
    
    @should_stop.setter
    def should_stop(self, value: bool) -> None:
        """Set stop flag on original runner."""
        self._runner.should_stop = value
    
    def get_current_voice_info(self) -> Tuple[str, str]:
        """
        Get current voice information for status display.
        
        Returns:
            Tuple of (voice_name, voice_id).
        """
        return (self._current_voice_name, self._current_voice_id)
    
    def _is_voice_unavailable_error(self, error_msg: str) -> bool:
        """
        Check if an error indicates voice unavailability.
        
        Args:
            error_msg: Error message string.
            
        Returns:
            True if the error indicates the voice is unavailable.
        """
        unavailable_indicators = [
            "voice not found",
            "voice_not_found",
            "invalid voice",
            "voice does not exist",
            "voice unavailable",
            "voice is not available",
            "no access to voice",
            "voice_id",
            "404",
        ]
        
        error_lower = error_msg.lower()
        return any(indicator in error_lower for indicator in unavailable_indicators)

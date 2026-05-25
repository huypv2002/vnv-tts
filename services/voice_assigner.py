"""
Voice Assigner Service for Multiple Voice Tab feature.
This is a NEW file - does not modify any existing code.

Handles logic for assigning voices to paragraphs based on assignment mode.
"""
from __future__ import annotations

from typing import List, Optional
from enum import Enum

from models.voice_entry import VoiceEntry


class AssignmentMode(Enum):
    """Voice assignment modes."""
    SEQUENTIAL = "sequential"      # Use voices in order, one voice per batch of paragraphs
    ALTERNATING = "alternating"    # Alternate between voices for each paragraph
    PER_FILE = "per_file"          # One voice per input file


class VoiceAssigner:
    """
    Assigns voices to paragraphs based on the selected assignment mode.
    
    Modes:
    - Sequential: Divides paragraphs evenly among voices. Voice 1 handles first N/M paragraphs,
                  Voice 2 handles next N/M paragraphs, etc.
    - Alternating: Cycles through voices for each paragraph. Paragraph 0 uses Voice 0,
                   Paragraph 1 uses Voice 1, etc., wrapping around.
    - Per File: Each file uses a different voice. All paragraphs in File 0 use Voice 0,
                all paragraphs in File 1 use Voice 1, etc., wrapping around.
    """
    
    def __init__(self, voice_list: List[VoiceEntry], mode: str = "alternating") -> None:
        """
        Initialize the voice assigner.
        
        Args:
            voice_list: List of VoiceEntry objects to assign from.
            mode: Assignment mode - "sequential", "alternating", or "per_file".
            
        Raises:
            ValueError: If voice_list is empty or mode is invalid.
        """
        if not voice_list:
            raise ValueError("Voice list cannot be empty")
        
        self._voice_list = voice_list.copy()
        self._mode = self._validate_mode(mode)
        self._current_index = 0
        
        # For sequential mode, we need to know total paragraphs
        self._total_paragraphs: Optional[int] = None
    
    @staticmethod
    def _validate_mode(mode: str) -> AssignmentMode:
        """Validate and convert mode string to AssignmentMode enum."""
        try:
            return AssignmentMode(mode.lower())
        except ValueError:
            valid_modes = [m.value for m in AssignmentMode]
            raise ValueError(f"Invalid mode: {mode}. Must be one of {valid_modes}")
    
    @property
    def mode(self) -> str:
        """Current assignment mode as string."""
        return self._mode.value
    
    @property
    def voice_count(self) -> int:
        """Number of voices in the list."""
        return len(self._voice_list)
    
    def set_total_paragraphs(self, total: int) -> None:
        """
        Set total number of paragraphs (needed for sequential mode).
        
        Args:
            total: Total number of paragraphs to process.
        """
        self._total_paragraphs = total
    
    def get_voice_for_paragraph(self, paragraph_index: int, file_index: int = 0) -> VoiceEntry:
        """
        Get the appropriate voice for a paragraph based on assignment mode.
        
        Args:
            paragraph_index: Index of the paragraph (0-based, global across all files).
            file_index: Index of the file containing this paragraph (0-based).
            
        Returns:
            VoiceEntry to use for this paragraph.
        """
        if self._mode == AssignmentMode.SEQUENTIAL:
            return self._get_voice_sequential(paragraph_index)
        elif self._mode == AssignmentMode.ALTERNATING:
            return self._get_voice_alternating(paragraph_index)
        elif self._mode == AssignmentMode.PER_FILE:
            return self._get_voice_per_file(file_index)
        else:
            # Fallback to alternating
            return self._get_voice_alternating(paragraph_index)
    
    def _get_voice_sequential(self, paragraph_index: int) -> VoiceEntry:
        """
        Get voice for sequential mode.
        
        Divides paragraphs evenly among voices.
        If total_paragraphs is not set, falls back to alternating.
        """
        if self._total_paragraphs is None or self._total_paragraphs == 0:
            # Fallback to alternating if total not known
            return self._get_voice_alternating(paragraph_index)
        
        # Calculate which voice should handle this paragraph
        paragraphs_per_voice = self._total_paragraphs / len(self._voice_list)
        voice_index = int(paragraph_index / paragraphs_per_voice)
        
        # Clamp to valid range
        voice_index = min(voice_index, len(self._voice_list) - 1)
        
        return self._voice_list[voice_index]
    
    def _get_voice_alternating(self, paragraph_index: int) -> VoiceEntry:
        """
        Get voice for alternating mode.
        
        Cycles through voices: paragraph i uses voice (i mod M).
        """
        voice_index = paragraph_index % len(self._voice_list)
        return self._voice_list[voice_index]
    
    def _get_voice_per_file(self, file_index: int) -> VoiceEntry:
        """
        Get voice for per-file mode.
        
        Each file uses a different voice: file j uses voice (j mod M).
        """
        voice_index = file_index % len(self._voice_list)
        return self._voice_list[voice_index]
    
    def get_voice_for_file(self, file_index: int) -> VoiceEntry:
        """
        Get the voice assigned to a specific file.
        
        This is a convenience method for per-file mode, but works for all modes
        by returning the voice that would be used for the first paragraph of the file.
        
        Args:
            file_index: Index of the file (0-based).
            
        Returns:
            VoiceEntry assigned to this file.
        """
        if self._mode == AssignmentMode.PER_FILE:
            return self._get_voice_per_file(file_index)
        else:
            # For other modes, return the voice for first paragraph
            # This is just informational
            return self._voice_list[file_index % len(self._voice_list)]
    
    def get_voice_at_index(self, index: int) -> Optional[VoiceEntry]:
        """
        Get voice at a specific index in the list.
        
        Args:
            index: Index in the voice list (0-based).
            
        Returns:
            VoiceEntry if index is valid, None otherwise.
        """
        if 0 <= index < len(self._voice_list):
            return self._voice_list[index]
        return None
    
    def reset(self) -> None:
        """Reset internal state for a new processing session."""
        self._current_index = 0
        self._total_paragraphs = None
    
    def get_assignment_preview(self, num_paragraphs: int, num_files: int = 1) -> List[str]:
        """
        Get a preview of voice assignments for debugging/display.
        
        Args:
            num_paragraphs: Number of paragraphs to preview.
            num_files: Number of files (for per-file mode).
            
        Returns:
            List of voice names showing assignment for each paragraph.
        """
        self.set_total_paragraphs(num_paragraphs)
        
        preview = []
        paragraphs_per_file = num_paragraphs // max(num_files, 1)
        
        for i in range(num_paragraphs):
            file_idx = i // paragraphs_per_file if paragraphs_per_file > 0 else 0
            file_idx = min(file_idx, num_files - 1)
            
            voice = self.get_voice_for_paragraph(i, file_idx)
            preview.append(voice.voice_name)
        
        return preview

"""
Multiple Voice Configuration Service.
This is a NEW file - does not modify any existing code.

Handles persistence of multiple voice configuration to local JSON file.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional
from dataclasses import dataclass, field, asdict

from models.voice_entry import VoiceEntry


# Constants
MIN_VOICE_ENTRIES = 2
MAX_VOICE_ENTRIES = 10
CONFIG_FILENAME = "multiple_voice_config.json"


@dataclass
class MultipleVoiceConfigData:
    """Data structure for multiple voice configuration."""
    enabled: bool = False
    assignment_mode: str = "alternating"  # "sequential", "alternating", "per_file"
    voice_entries: List[VoiceEntry] = field(default_factory=list)


class MultipleVoiceConfig:
    """
    Service to manage persistence of multiple voice configuration.
    
    Configuration is saved to a local JSON file and loaded on startup.
    Enforces boundary constraints: minimum 2, maximum 10 voice entries.
    """
    
    def __init__(self, config_dir: Optional[str] = None) -> None:
        """
        Initialize the configuration service.
        
        Args:
            config_dir: Directory to store config file. If None, uses current directory.
        """
        self._config_dir = config_dir or os.getcwd()
        self._config_path = os.path.join(self._config_dir, CONFIG_FILENAME)
        
        # Initialize with default data
        self._data = MultipleVoiceConfigData()
        
        # Try to load existing config
        self.load()
    
    @property
    def enabled(self) -> bool:
        """Whether multiple voice mode is enabled."""
        return self._data.enabled
    
    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Set multiple voice mode enabled state."""
        self._data.enabled = bool(value)
    
    @property
    def assignment_mode(self) -> str:
        """Current voice assignment mode."""
        return self._data.assignment_mode
    
    @assignment_mode.setter
    def assignment_mode(self, value: str) -> None:
        """Set voice assignment mode."""
        valid_modes = ["sequential", "alternating", "per_file"]
        if value not in valid_modes:
            raise ValueError(f"Invalid assignment mode: {value}. Must be one of {valid_modes}")
        self._data.assignment_mode = value
    
    @property
    def voice_entries(self) -> List[VoiceEntry]:
        """List of voice entries."""
        return self._data.voice_entries.copy()
    
    def get_entry_count(self) -> int:
        """Get the number of voice entries."""
        return len(self._data.voice_entries)
    
    def add_entry(self, entry: Optional[VoiceEntry] = None) -> bool:
        """
        Add a new voice entry to the list.
        
        Args:
            entry: VoiceEntry to add. If None, creates a default entry.
            
        Returns:
            True if entry was added, False if at maximum capacity.
        """
        if len(self._data.voice_entries) >= MAX_VOICE_ENTRIES:
            print(f"❌ Cannot add voice entry: maximum {MAX_VOICE_ENTRIES} entries reached")
            return False
        
        if entry is None:
            entry = VoiceEntry.default()
        
        self._data.voice_entries.append(entry)
        print(f"✅ Added voice entry: {entry.voice_name} (total: {len(self._data.voice_entries)})")
        return True
    
    def remove_entry(self, index: int) -> bool:
        """
        Remove a voice entry from the list.
        
        Args:
            index: Index of entry to remove (0-based).
            
        Returns:
            True if entry was removed, False if at minimum capacity or invalid index.
        """
        if len(self._data.voice_entries) <= MIN_VOICE_ENTRIES:
            print(f"❌ Cannot remove voice entry: minimum {MIN_VOICE_ENTRIES} entries required")
            return False
        
        if index < 0 or index >= len(self._data.voice_entries):
            print(f"❌ Cannot remove voice entry: invalid index {index}")
            return False
        
        removed = self._data.voice_entries.pop(index)
        print(f"✅ Removed voice entry: {removed.voice_name} (total: {len(self._data.voice_entries)})")
        return True
    
    def update_entry(self, index: int, entry: VoiceEntry) -> bool:
        """
        Update a voice entry at the specified index.
        
        Args:
            index: Index of entry to update (0-based).
            entry: New VoiceEntry data.
            
        Returns:
            True if entry was updated, False if invalid index.
        """
        if index < 0 or index >= len(self._data.voice_entries):
            print(f"❌ Cannot update voice entry: invalid index {index}")
            return False
        
        self._data.voice_entries[index] = entry
        print(f"✅ Updated voice entry at index {index}: {entry.voice_name}")
        return True
    
    def get_entry(self, index: int) -> Optional[VoiceEntry]:
        """
        Get a voice entry at the specified index.
        
        Args:
            index: Index of entry to get (0-based).
            
        Returns:
            VoiceEntry if found, None if invalid index.
        """
        if index < 0 or index >= len(self._data.voice_entries):
            return None
        return self._data.voice_entries[index]
    
    def save(self) -> bool:
        """
        Save configuration to JSON file.
        
        Returns:
            True if saved successfully, False on error.
        """
        try:
            data = {
                'enabled': self._data.enabled,
                'assignment_mode': self._data.assignment_mode,
                'voice_entries': [entry.to_dict() for entry in self._data.voice_entries]
            }
            
            with open(self._config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            print(f"✅ Saved multiple voice config to: {self._config_path}")
            return True
            
        except Exception as e:
            print(f"❌ Error saving multiple voice config: {e}")
            return False
    
    def load(self) -> bool:
        """
        Load configuration from JSON file.
        
        Returns:
            True if loaded successfully, False on error (uses defaults).
        """
        try:
            if not os.path.exists(self._config_path):
                print(f"ℹ️ No config file found at {self._config_path}, using defaults")
                self._initialize_defaults()
                return False
            
            with open(self._config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self._data.enabled = bool(data.get('enabled', False))
            self._data.assignment_mode = data.get('assignment_mode', 'alternating')
            
            # Validate assignment mode
            if self._data.assignment_mode not in ["sequential", "alternating", "per_file"]:
                self._data.assignment_mode = "alternating"
            
            # Load voice entries
            entries_data = data.get('voice_entries', [])
            self._data.voice_entries = [
                VoiceEntry.from_dict(entry_data) 
                for entry_data in entries_data
            ]
            
            # Ensure minimum entries
            self._ensure_valid_entry_count()
            
            print(f"✅ Loaded multiple voice config: {len(self._data.voice_entries)} entries")
            return True
            
        except json.JSONDecodeError as e:
            print(f"❌ Corrupt config file, resetting to defaults: {e}")
            self._initialize_defaults()
            return False
            
        except Exception as e:
            print(f"❌ Error loading multiple voice config: {e}")
            self._initialize_defaults()
            return False
    
    def clear(self) -> None:
        """
        Clear configuration and reset to defaults.
        Also removes the config file.
        """
        self._initialize_defaults()
        
        try:
            if os.path.exists(self._config_path):
                os.remove(self._config_path)
                print(f"✅ Removed config file: {self._config_path}")
        except Exception as e:
            print(f"❌ Error removing config file: {e}")
        
        print("✅ Multiple voice config cleared and reset to defaults")
    
    def _initialize_defaults(self) -> None:
        """Initialize with default configuration."""
        self._data = MultipleVoiceConfigData(
            enabled=False,
            assignment_mode="alternating",
            voice_entries=[
                VoiceEntry.default(),
                VoiceEntry.default()
            ]
        )
        # Give default entries different names
        self._data.voice_entries[0].voice_name = "Voice 1"
        self._data.voice_entries[1].voice_name = "Voice 2"
    
    def _ensure_valid_entry_count(self) -> None:
        """Ensure voice entries count is within valid range."""
        # Add entries if below minimum
        while len(self._data.voice_entries) < MIN_VOICE_ENTRIES:
            entry = VoiceEntry.default()
            entry.voice_name = f"Voice {len(self._data.voice_entries) + 1}"
            self._data.voice_entries.append(entry)
            print(f"⚠️ Added default entry to meet minimum: {entry.voice_name}")
        
        # Remove entries if above maximum
        removed_count = 0
        while len(self._data.voice_entries) > MAX_VOICE_ENTRIES:
            removed = self._data.voice_entries.pop()
            removed_count += 1
        
        if removed_count > 0:
            print(f"⚠️ Removed {removed_count} entries to meet maximum limit")
    
    def validate_entries(self) -> List[str]:
        """
        Validate all voice entries and return list of issues.
        
        Returns:
            List of validation error messages (empty if all valid).
        """
        issues = []
        
        for idx, entry in enumerate(self._data.voice_entries):
            if not entry.voice_id:
                issues.append(f"Entry {idx + 1}: Missing voice_id")
            if not entry.model:
                issues.append(f"Entry {idx + 1}: Missing model")
            if entry.stability < 0 or entry.stability > 1:
                issues.append(f"Entry {idx + 1}: Invalid stability ({entry.stability})")
            if entry.similarity_boost < 0 or entry.similarity_boost > 1:
                issues.append(f"Entry {idx + 1}: Invalid similarity_boost ({entry.similarity_boost})")
            if entry.style < 0 or entry.style > 1:
                issues.append(f"Entry {idx + 1}: Invalid style ({entry.style})")
            if entry.speed < 0.7 or entry.speed > 1.2:
                issues.append(f"Entry {idx + 1}: Invalid speed ({entry.speed})")
        
        return issues
    
    def get_valid_entries(self) -> List[VoiceEntry]:
        """
        Get only valid voice entries (with non-empty voice_id).
        
        Returns:
            List of valid VoiceEntry objects.
        """
        return [entry for entry in self._data.voice_entries if entry.voice_id]

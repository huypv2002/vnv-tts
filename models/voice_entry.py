"""
VoiceEntry dataclass for Multiple Voice Tab feature.
This is a NEW file - does not modify any existing code.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Any


@dataclass
class VoiceEntry:
    """
    Data model for a single voice entry in the Multiple Voice list.
    
    Attributes:
        voice_id: ElevenLabs voice ID
        voice_name: Display name of the voice
        model: ElevenLabs model name (e.g., "eleven_turbo_v2_5")
        stability: Voice stability (0.0 - 1.0)
        similarity_boost: Voice similarity boost (0.0 - 1.0)
        style: Voice style exaggeration (0.0 - 1.0)
        speed: Voice speed (0.7 - 1.2)
        use_speaker_boost: Whether to use speaker boost
    """
    voice_id: str
    voice_name: str
    model: str
    stability: float
    similarity_boost: float
    style: float
    speed: float
    use_speaker_boost: bool
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert VoiceEntry to dictionary for JSON serialization.
        
        Returns:
            Dictionary representation of the VoiceEntry
        """
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VoiceEntry':
        """
        Create VoiceEntry from dictionary.
        
        Args:
            data: Dictionary with voice entry data
            
        Returns:
            VoiceEntry instance
            
        Raises:
            KeyError: If required fields are missing
            TypeError: If field types are incorrect
        """
        return cls(
            voice_id=str(data.get('voice_id', '')),
            voice_name=str(data.get('voice_name', '')),
            model=str(data.get('model', 'eleven_turbo_v2_5')),
            stability=float(data.get('stability', 0.5)),
            similarity_boost=float(data.get('similarity_boost', 0.75)),
            style=float(data.get('style', 0.0)),
            speed=float(data.get('speed', 1.0)),
            use_speaker_boost=bool(data.get('use_speaker_boost', False))
        )
    
    @classmethod
    def default(cls) -> 'VoiceEntry':
        """
        Create a VoiceEntry with default settings.
        
        Returns:
            VoiceEntry with default values
        """
        return cls(
            voice_id='',
            voice_name='New Voice',
            model='eleven_turbo_v2_5',
            stability=0.5,
            similarity_boost=0.75,
            style=0.0,
            speed=1.0,
            use_speaker_boost=False
        )
    
    def get_voice_settings_dict(self) -> Dict[str, Any]:
        """
        Get voice settings in format expected by ElevenLabs API.
        
        Returns:
            Dictionary with voice settings for TTS API call
        """
        return {
            'stability': self.stability,
            'similarity_boost': self.similarity_boost,
            'style': self.style,
            'use_speaker_boost': self.use_speaker_boost,
            'speed': self.speed,
        }
    
    def __eq__(self, other: object) -> bool:
        """Check equality with another VoiceEntry."""
        if not isinstance(other, VoiceEntry):
            return False
        return (
            self.voice_id == other.voice_id and
            self.voice_name == other.voice_name and
            self.model == other.model and
            abs(self.stability - other.stability) < 0.001 and
            abs(self.similarity_boost - other.similarity_boost) < 0.001 and
            abs(self.style - other.style) < 0.001 and
            abs(self.speed - other.speed) < 0.001 and
            self.use_speaker_boost == other.use_speaker_boost
        )
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"VoiceEntry(voice_id='{self.voice_id}', "
            f"voice_name='{self.voice_name}', "
            f"model='{self.model}')"
        )

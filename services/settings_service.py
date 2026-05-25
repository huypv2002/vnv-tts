from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from appdirs import user_config_dir


@dataclass
class VoiceEntry:
    voice_id: str
    model: str
    name: str = ""  # Display name
    stability: float = 0.0
    similarity_boost: float = 1.0
    style: float = 0.0
    use_speaker_boost: bool = False
    speed: float = 1.0  # ElevenLabs voice speed (clamped 0.7-1.2)


@dataclass
class AppSettings:
    voices: List[VoiceEntry] = field(default_factory=list)
    # Global advanced settings for processing
    pause_between_segments_enabled: bool = False
    segment_gap_seconds: float = 1.3
    segment_count: int = 5
    srt_split_enabled: bool = False
    per_char_enabled: bool = False
    comma_pause: float = 0.3
    dot_pause: float = 0.5
    download_type: str = "1 <ORIGINAL>"
    max_chars_per_line: int = 1000


class SettingsService:
    def __init__(self) -> None:
        self._config_dir = Path(user_config_dir("audio_app", "audio"))
        self._config_file = self._config_dir / "settings.json"
        self._settings = AppSettings()

    def ensure_initialized(self) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        if self._config_file.exists():
            try:
                raw = json.loads(self._config_file.read_text(encoding="utf-8"))
                # Load voices with explicit boolean conversion
                voices = []
                for v_data in raw.get("voices", []):
                    # Ensure use_speaker_boost is boolean and speed is float with default 1.0
                    v_data["use_speaker_boost"] = bool(v_data.get("use_speaker_boost", False))
                    # Clamp speed to ElevenLabs allowed range [0.7, 1.2]
                    try:
                        speed_val = float(v_data.get("speed", 1.0))
                    except Exception:
                        speed_val = 1.0
                    v_data["speed"] = max(0.7, min(1.2, speed_val))
                    voices.append(VoiceEntry(**v_data))
                self._settings = AppSettings(
                    voices=voices,
                    pause_between_segments_enabled=raw.get("pause_between_segments_enabled", False),
                    segment_gap_seconds=raw.get("segment_gap_seconds", 1.3),
                    segment_count=raw.get("segment_count", 5),
                    srt_split_enabled=raw.get("srt_split_enabled", False),
                    per_char_enabled=raw.get("per_char_enabled", False),
                    comma_pause=raw.get("comma_pause", 0.3),
                    dot_pause=raw.get("dot_pause", 0.5),
                    download_type=raw.get("download_type", "1 <ORIGINAL>"),
                    max_chars_per_line=raw.get("max_chars_per_line", 1000),
                )
            except Exception:
                self._settings = AppSettings()
                self.save()
        else:
            self.save()

    def save(self) -> None:
        # Convert voices to dict with explicit boolean conversion
        voices_data = []
        for v in self._settings.voices:
            voice_dict = asdict(v)
            # Ensure use_speaker_boost is boolean and speed stays numeric
            voice_dict["use_speaker_boost"] = bool(voice_dict.get("use_speaker_boost", False))
            try:
                voice_dict["speed"] = max(0.7, min(1.2, float(voice_dict.get("speed", 1.0))))
            except Exception:
                voice_dict["speed"] = 1.0
            voices_data.append(voice_dict)
        
        payload = {
            "voices": voices_data,
            "pause_between_segments_enabled": self._settings.pause_between_segments_enabled,
            "segment_gap_seconds": self._settings.segment_gap_seconds,
            "segment_count": self._settings.segment_count,
            "srt_split_enabled": self._settings.srt_split_enabled,
            "per_char_enabled": self._settings.per_char_enabled,
            "comma_pause": self._settings.comma_pause,
            "dot_pause": self._settings.dot_pause,
            "download_type": self._settings.download_type,
            "max_chars_per_line": self._settings.max_chars_per_line,
        }
        self._config_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Voice management
    def list_voices(self) -> List[VoiceEntry]:
        return list(self._settings.voices)

    def add_voice(self, voice_id: str, model: str,
                  name: str = "",
                  stability: float = 0.0,
                  similarity_boost: float = 1.0,
                  style: float = 0.0,
                  use_speaker_boost: bool = False,
                  speed: float = 1.0) -> None:
        try:
            speed = max(0.7, min(1.2, float(speed)))
        except Exception:
            speed = 1.0
        print(f"💾 add_voice called with: voice_id={voice_id}, model={model}, boost={use_speaker_boost} (type: {type(use_speaker_boost)}), speed={speed}")
        
        # Prevent duplicates (same voice_id + model)
        for v in self._settings.voices:
            if v.voice_id == voice_id and v.model == model:
                # Update settings values on duplicate instead of adding again
                v.name = name
                v.stability = stability
                v.similarity_boost = similarity_boost
                v.style = style
                v.use_speaker_boost = use_speaker_boost
                v.speed = speed
                print(f"🔄 Updated existing voice entry, boost={v.use_speaker_boost}, speed={v.speed}")
                self.save()
                print(f"✅ Saved to file")
                return
        self._settings.voices.append(VoiceEntry(
            voice_id=voice_id,
            model=model,
            name=name,
            stability=stability,
            similarity_boost=similarity_boost,
            style=style,
            use_speaker_boost=use_speaker_boost,
            speed=speed
        ))
        self.save()

    def clear_voices(self) -> None:
        """Remove all saved voices from the library."""
        if self._settings.voices:
            self._settings.voices.clear()
            self.save()

    # Advanced settings accessors
    def get_advanced_settings(self) -> dict:
        s = self._settings
        return {
            "pause_between_segments_enabled": s.pause_between_segments_enabled,
            "segment_gap_seconds": s.segment_gap_seconds,
            "segment_count": s.segment_count,
            "srt_split_enabled": s.srt_split_enabled,
            "per_char_enabled": s.per_char_enabled,
            "comma_pause": s.comma_pause,
            "dot_pause": s.dot_pause,
            "download_type": s.download_type,
            "max_chars_per_line": s.max_chars_per_line,
        }

    def save_advanced_settings(self, data: dict) -> None:
        s = self._settings
        s.pause_between_segments_enabled = bool(data.get("pause_between_segments_enabled", s.pause_between_segments_enabled))
        s.segment_gap_seconds = float(data.get("segment_gap_seconds", s.segment_gap_seconds))
        s.segment_count = int(data.get("segment_count", s.segment_count))
        s.srt_split_enabled = bool(data.get("srt_split_enabled", s.srt_split_enabled))
        s.per_char_enabled = bool(data.get("per_char_enabled", s.per_char_enabled))
        s.comma_pause = float(data.get("comma_pause", s.comma_pause))
        s.dot_pause = float(data.get("dot_pause", s.dot_pause))
        s.download_type = str(data.get("download_type", s.download_type))
        s.max_chars_per_line = int(data.get("max_chars_per_line", s.max_chars_per_line))
        self.save()

    def remove_voice(self, index: int) -> None:
        if 0 <= index < len(self._settings.voices):
            del self._settings.voices[index]
            self.save()



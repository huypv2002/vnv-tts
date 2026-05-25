from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class FileStatus(Enum):
    PENDING = "Pending"
    PROCESSING = "Processing"
    DONE = "DONE"
    ERROR = "Error"
    DOWNLOADING = "Downloading ..."


class SubtitleStatus(Enum):
    PENDING = "Pending"
    PROCESSING = "Processing"
    DONE = "DONE"
    ERROR = "Error"
    DOWNLOADING = "Downloading ..."


@dataclass
class VoiceSettings:
    name: str = ""
    voice_id: str = "Leon Stern - Fiction & Fa..."
    model: str = "eleven_turbo_v2_5"
    change_settings: bool = True
    speed: float = 1.00
    stability: int = 50
    similarity: int = 75
    style: int = 0
    speaker_boost: bool = False


@dataclass
class BatchFile:
    id: int
    filename: str
    status: FileStatus
    path: Optional[str] = None


@dataclass
class SubtitleLine:
    id: int
    output: str = ""
    timing: str = ""
    content: str = ""
    voice_number: str = ""
    status: SubtitleStatus = SubtitleStatus.PENDING


@dataclass
class BatchJobSettings:
    folder_path: str = "F:\\Prompt CT\\Audio CT\\Audio"
    auto_create_srt: bool = False
    files: List[BatchFile] = field(default_factory=list)


@dataclass
class ProcessingStats:
    done: int = 0
    processing: int = 0
    total: int = 0
    elapsed_seconds: int = 0


@dataclass
class AppOptions:
    loop: bool = True
    auto_split: bool = True
    proxy: str = "FREE"
    threads: int = 10
    die_count: int = 0
    error_count: int = 0
    total_count: int = 31


@dataclass
class CreditsInfo:
    credits: int = 11720
    email: str = "VanViet99@11labs.vn"
    expired_date: str = "23/10/2025 14:52"


@dataclass
class AppState:
    voice_settings: VoiceSettings = field(default_factory=VoiceSettings)
    batch_settings: BatchJobSettings = field(default_factory=BatchJobSettings)
    subtitle_lines: List[SubtitleLine] = field(default_factory=list)
    processing_stats: ProcessingStats = field(default_factory=ProcessingStats)
    options: AppOptions = field(default_factory=AppOptions)
    credits: CreditsInfo = field(default_factory=CreditsInfo)
    current_file_path: str = "F:\\Prompt CT\\Audio CT\\Audio\\d49.txt"
    is_processing: bool = False

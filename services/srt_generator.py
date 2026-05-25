from __future__ import annotations

import os
import subprocess
from typing import List, Tuple


def get_audio_duration(audio_path: str) -> float:
    """Get duration of audio file using ffprobe"""
    from services.ffmpeg_utils import get_ffprobe_path
    cmd = [
        get_ffprobe_path(),
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        audio_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception as e:
        print(f"❌ Error getting duration: {e}")
    return 0.0


def format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT time format: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_srt_from_paragraphs(paragraphs: List[str], audio_files: List[str], output_path: str) -> bool:
    """
    Generate SRT file from paragraphs and their corresponding audio files
    
    Args:
        paragraphs: List of text paragraphs
        audio_files: List of audio file paths (must match paragraphs in order)
        output_path: Output SRT file path
    
    Returns:
        bool: True if successful
    """
    try:
        if len(paragraphs) != len(audio_files):
            print(f"❌ Mismatch: {len(paragraphs)} paragraphs vs {len(audio_files)} audio files")
            return False
        
        srt_content = []
        current_time = 0.0
        
        for idx, (para, audio_file) in enumerate(zip(paragraphs, audio_files), start=1):
            # Get duration of this audio segment
            duration = get_audio_duration(audio_file)
            if duration <= 0:
                print(f"⚠️ Warning: Could not get duration for {audio_file}, using 5s default")
                duration = 5.0
            
            # Calculate timing
            start_time = current_time
            end_time = current_time + duration
            
            # Format SRT entry
            srt_content.append(f"{idx}")
            srt_content.append(f"{format_srt_time(start_time)} --> {format_srt_time(end_time)}")
            srt_content.append(para)
            srt_content.append("")  # Empty line between entries
            
            current_time = end_time
        
        # Write SRT file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(srt_content))
        
        print(f"✅ SRT file created: {output_path}")
        return True
        
    except Exception as e:
        print(f"❌ Error generating SRT: {e}")
        return False


def generate_srt_from_folder(mini_folder: str, paragraphs: List[str], output_srt: str) -> bool:
    """
    Generate SRT from numbered mp3 files in a folder
    
    Args:
        mini_folder: Folder containing 1.mp3, 2.mp3, etc.
        paragraphs: Original text paragraphs
        output_srt: Output SRT file path
    """
    try:
        # Get audio files in order
        files = sorted([f for f in os.listdir(mini_folder) if f.lower().endswith('.mp3')],
                      key=lambda x: int(os.path.splitext(x)[0]))
        
        if not files:
            print(f"❌ No audio files found in {mini_folder}")
            return False
        
        audio_paths = [os.path.join(mini_folder, f) for f in files]
        
        return generate_srt_from_paragraphs(paragraphs, audio_paths, output_srt)
        
    except Exception as e:
        print(f"❌ Error generating SRT from folder: {e}")
        return False


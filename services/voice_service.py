from __future__ import annotations

import json
import subprocess
import os
from typing import List, Dict, Optional


class VoiceService:
    """Service to search ElevenLabs shared voices and extract free models."""

    SHARED_VOICES_URL = "https://api.elevenlabs.io/v1/shared-voices"
    VOICES_URL = "https://api.elevenlabs.io/v1/voices"
    MODELS_URL = "https://api.elevenlabs.io/v1/models"

    def search_shared_voices(self, api_key: str, search: str) -> List[Dict]:
        """
        Search shared voices by id or name substring.

        Returns a list of dicts: {
            'voice_id': str,
            'name': str,
            'free_users_allowed': bool,
            'models': List[str],  # distinct model_id values for free usage
            'preview_url': Optional[str]
        }
        """
        cmd = [
            "curl",
            "-sG",
            self.SHARED_VOICES_URL,
            "-H",
            f"xi-api-key: {api_key}",
            "-d",
            f"search={search}",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, encoding='utf-8', errors='replace',
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0:
            raise RuntimeError(f"Voice search failed: {result.stderr}")

        try:
            data = json.loads(result.stdout)
            print(f"🔍 Raw shared voices response: {json.dumps(data, indent=2)[:500]}...")
        except Exception as exc:
            print(f"❌ Raw response: {result.stdout[:500]}")
            raise RuntimeError(f"Invalid JSON from ElevenLabs: {exc}")

        voices = data.get("voices", []) or []
        output: List[Dict] = []
        for v in voices:
            free_allowed = bool(v.get("free_users_allowed", False))
            verified = v.get("verified_languages", []) or []
            model_ids = []
            seen = set()
            for lang in verified:
                mid = lang.get("model_id")
                if mid and mid not in seen:
                    seen.add(mid)
                    model_ids.append(mid)
            output.append({
                "voice_id": v.get("voice_id"),
                "name": (v.get("name") or "").strip(),
                "free_users_allowed": free_allowed,
                "models": model_ids,
                "preview_url": v.get("preview_url")
            })

        return output

    def get_voice_details(self, api_key: str, voice_id: str) -> Optional[Dict]:
        """
        Get detailed information about a specific voice by ID.
        
        Returns a dict with voice details or None if not found.
        """
        cmd = [
            "curl",
            "-s",
            f"{self.VOICES_URL}/{voice_id}",
            "-H",
            f"xi-api-key: {api_key}",
        ]

        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=20, 
            encoding='utf-8', 
            errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        
        if result.returncode != 0:
            print(f"❌ Voice details API failed: {result.stderr}")
            return None

        try:
            data = json.loads(result.stdout)
            print(f"🔍 Raw voice details response: {json.dumps(data, indent=2)[:500]}...")
        except Exception as exc:
            print(f"❌ Raw response: {result.stdout[:500]}")
            print(f"❌ Invalid JSON from voice details API: {exc}")
            return None

        # Extract relevant information from the response
        # Based on the response structure you provided
        available_for_tiers = data.get("available_for_tiers", [])
        verified_languages = data.get("verified_languages", [])
        
        # Determine if voice is free based on available_for_tiers
        # If available_for_tiers is empty, it's likely free for all users
        free_users_allowed = len(available_for_tiers) == 0
        
        # Extract model IDs from verified_languages
        model_ids = []
        if verified_languages:
            for lang in verified_languages:
                if isinstance(lang, dict) and lang.get("model_id"):
                    model_ids.append(lang["model_id"])
        
        voice_info = {
            "voice_id": data.get("voice_id"),
            "name": data.get("name", "").strip(),
            "category": data.get("category", ""),
            "description": data.get("description", ""),
            "preview_url": data.get("preview_url"),
            "labels": data.get("labels", {}),
            "settings": data.get("settings", {}),
            "verified_languages": verified_languages,
            "available_for_tiers": available_for_tiers,
            "free_users_allowed": free_users_allowed,
            "models": model_ids,  # Add models list for compatibility
        }
        
        return voice_info

    def search_voices_by_id_or_name(self, api_key: str, search: str) -> List[Dict]:
        """
        Enhanced search that tries both shared voices and direct voice lookup.
        First tries shared voices search, then falls back to direct voice lookup if search looks like a voice ID.
        """
        results = []
        
        # First try shared voices search
        try:
            shared_results = self.search_shared_voices(api_key, search)
            results.extend(shared_results)
            print(f"🔍 Found {len(shared_results)} results from shared voices search")
        except Exception as e:
            print(f"⚠️ Shared voices search failed: {e}")
        
        # If search looks like a voice ID (starts with letters and contains alphanumeric), try direct lookup
        if len(search) >= 10 and search.replace('-', '').replace('_', '').isalnum():
            try:
                voice_details = self.get_voice_details(api_key, search)
                if voice_details:
                    # Convert to same format as shared voices
                    voice_result = {
                        "voice_id": voice_details["voice_id"],
                        "name": voice_details["name"],
                        "free_users_allowed": voice_details["free_users_allowed"],
                        "models": voice_details.get("models", []),
                        "preview_url": voice_details["preview_url"],
                        "settings": voice_details.get("settings", {}),  # Include voice settings
                        "labels": voice_details.get("labels", {}),  # Include voice labels
                    }
                    
                    # Check if this voice is already in results (avoid duplicates)
                    if not any(r["voice_id"] == voice_result["voice_id"] for r in results):
                        results.append(voice_result)
                        print(f"🎯 Found voice by direct lookup: {voice_details['name']}")
                    else:
                        print(f"🔄 Voice already found in shared search results")
                        
            except Exception as e:
                print(f"⚠️ Direct voice lookup failed: {e}")
        
        return results

    def list_models(self, api_key: str) -> List[str]:
        """Fetch all models and return their model IDs (field: model_id)."""
        cmd = [
            "curl",
            "-s",
            self.MODELS_URL,
            "-H",
            f"xi-api-key: {api_key}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, encoding='utf-8', errors='replace',
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0:
            raise RuntimeError(f"Fetch models failed: {result.stderr}")
        try:
            data = json.loads(result.stdout)
        except Exception as exc:
            raise RuntimeError(f"Invalid JSON from models API: {exc}")
        # API returns a list of objects; pick the 'model_id' field instead of 'name'
        if not isinstance(data, list):
            return []
        model_ids: List[str] = []
        for item in data:
            model_id = item.get("model_id")
            if isinstance(model_id, str) and model_id.strip():
                model_ids.append(model_id.strip())
        return model_ids



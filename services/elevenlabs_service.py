from __future__ import annotations

import base64
from typing import Optional

import requests


class ElevenLabsService:
    def __init__(self, api_key_provider) -> None:
        self._get_key = api_key_provider

    def list_voices(self) -> Optional[dict]:
        key = self._get_key()
        if not key:
            return None
        try:
            resp = requests.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": key},
                timeout=20,
            )
            if resp.ok:
                return resp.json()
        except Exception:
            return None
        return None



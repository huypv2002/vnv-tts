from __future__ import annotations

from typing import Optional

import keyring


SERVICE_NAME = "audio_app"
ELEVENLABS_SECRET_KEY = "elevenlabs_api_key"


class SecureStore:
    @staticmethod
    def get_elevenlabs_key() -> Optional[str]:
        return keyring.get_password(SERVICE_NAME, ELEVENLABS_SECRET_KEY)

    @staticmethod
    def set_elevenlabs_key(value: str) -> None:
        keyring.set_password(SERVICE_NAME, ELEVENLABS_SECRET_KEY, value)



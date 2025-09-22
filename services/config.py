"""Centralized configuration management for the iMessage AI agent.

This module consolidates configuration handling across environment variables,
on-disk settings, and runtime defaults. The goal is to make it easy for both
command-line and future desktop entry points to operate on a single source of
truth while keeping sensitive data out of the repository root.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from cryptography.fernet import Fernet

# Base application support directory used for persisted state/configuration.
APP_SUPPORT_DIR = Path("~/Library/Application Support/imessage-ai").expanduser()
CONFIG_FILE = APP_SUPPORT_DIR / "config.json"
STATE_FILE = APP_SUPPORT_DIR / "state.json"
TMP_IMAGES_DIR = APP_SUPPORT_DIR / "tmp_images"
LEGACY_SETTINGS_FILE = Path("settings.json")
KEY_FILE = APP_SUPPORT_DIR / "secret.key"


def _ensure_support_dir() -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


DEFAULT_AI_SETTINGS: Dict[str, Any] = {
    "ai_trigger_tag": "@ai",
    "allowed_users": [],
    "openai_model": "gpt-4o-mini",
    "system_prompt": "You are a concise, helpful assistant. Keep answers brief.",
    "context_window": 25,
    "enable_search": False,
    "google_cse_id": "",
    "search_max_results": 5,
    "search_cache_ttl": 120,
    "image_chunk_size": 5,
}


DEFAULT_CONFIG: Dict[str, Any] = {
    "ai_settings": DEFAULT_AI_SETTINGS,
    "openai_api_key": None,
    "auth_token": None,
    "session_secret": None,
}


def _default_config() -> Dict[str, Any]:
    """Return a deep copy of the default configuration skeleton."""

    return {
        "ai_settings": deepcopy(DEFAULT_AI_SETTINGS),
        "openai_api_key": None,
        "auth_token": None,
        "session_secret": None,
    }


def _get_or_create_key() -> bytes:
    _ensure_support_dir()
    if KEY_FILE.exists():
        try:
            return KEY_FILE.read_bytes()
        except Exception:
            pass
    key = Fernet.generate_key()
    try:
        KEY_FILE.write_bytes(key)
    except Exception:
        # As a last resort let callers operate without a persisted key.
        return key
    return key


def _fernet() -> Fernet:
    return Fernet(_get_or_create_key())


def encrypt_list(items: Iterable[str]) -> List[str]:
    f = _fernet()
    out: List[str] = []
    for item in items or []:
        if not item:
            continue
        out.append(f.encrypt(item.encode("utf-8")).decode("utf-8"))
    return out


def decrypt_list(tokens: Iterable[str]) -> List[str]:
    f = _fernet()
    out: List[str] = []
    for token in tokens or []:
        if not token:
            continue
        try:
            out.append(f.decrypt(token.encode("utf-8")).decode("utf-8"))
        except Exception:
            continue
    return out


class Config:
    """Runtime configuration wrapper.

    Provides a dictionary-like interface for AI-level settings while handling
    persistence, environment overrides, and legacy migrations. Mutations are not
    automatically persisted—call :meth:`save` after updating.
    """

    def __init__(self) -> None:
        _ensure_support_dir()
        self._data: Dict[str, Any] = _default_config()
        self._env_overrides: Dict[str, Any] = {}
        self._load_from_disk()
        self._load_legacy_settings()
        self._apply_env_overrides()

    # ------------------------------------------------------------------
    # Properties and convenience accessors
    # ------------------------------------------------------------------
    @property
    def ai_settings(self) -> Dict[str, Any]:
        return self._data.setdefault("ai_settings", deepcopy(DEFAULT_AI_SETTINGS))

    @property
    def allowed_users(self) -> List[str]:
        return list(self.ai_settings.get("allowed_users", []))

    @property
    def openai_api_key(self) -> Optional[str]:
        return self._env_overrides.get("openai_api_key") or self._data.get("openai_api_key")

    @openai_api_key.setter
    def openai_api_key(self, value: Optional[str]) -> None:
        self._data["openai_api_key"] = value or None

    @property
    def auth_token(self) -> Optional[str]:
        return self._env_overrides.get("auth_token") or self._data.get("auth_token")

    @auth_token.setter
    def auth_token(self, value: Optional[str]) -> None:
        self._data["auth_token"] = value or None

    @property
    def session_secret(self) -> Optional[str]:
        return self._env_overrides.get("session_secret") or self._data.get("session_secret")

    @session_secret.setter
    def session_secret(self, value: Optional[str]) -> None:
        self._data["session_secret"] = value or None

    @property
    def support_dir(self) -> Path:
        return APP_SUPPORT_DIR

    @property
    def state_path(self) -> Path:
        return STATE_FILE

    @property
    def tmp_images_dir(self) -> Path:
        return TMP_IMAGES_DIR

    # ------------------------------------------------------------------
    # Loading / persistence
    # ------------------------------------------------------------------
    def _load_legacy_settings(self) -> None:
        if not LEGACY_SETTINGS_FILE.exists():
            return
        try:
            payload = json.loads(LEGACY_SETTINGS_FILE.read_text())
        except Exception:
            return

        ai = self.ai_settings
        for key in DEFAULT_AI_SETTINGS:
            if key in payload:
                ai[key] = payload[key]

        if payload.get("allowed_users_encrypted") and not payload.get("allowed_users"):
            ai["allowed_users"] = sorted({
                self._normalize_handle(x)
                for x in decrypt_list(payload.get("allowed_users_encrypted", []))
                if x
            })
        elif isinstance(payload.get("allowed_users"), list):
            ai["allowed_users"] = [self._normalize_handle(x) for x in payload.get("allowed_users", [])]

    def _load_from_disk(self) -> None:
        if not CONFIG_FILE.exists():
            return
        try:
            raw = json.loads(CONFIG_FILE.read_text())
        except Exception:
            return

        ai_payload = raw.get("ai_settings") or {}
        ai_target = self.ai_settings
        for key, default_val in DEFAULT_AI_SETTINGS.items():
            if key in ai_payload:
                ai_target[key] = ai_payload[key]
        if ai_payload.get("allowed_users_encrypted"):
            ai_target["allowed_users"] = sorted({
                self._normalize_handle(x)
                for x in decrypt_list(ai_payload.get("allowed_users_encrypted", []))
                if x
            })
        elif isinstance(ai_payload.get("allowed_users"), list):
            ai_target["allowed_users"] = [self._normalize_handle(x) for x in ai_payload.get("allowed_users", [])]

        self._data["openai_api_key"] = raw.get("openai_api_key")
        self._data["auth_token"] = raw.get("auth_token")
        self._data["session_secret"] = raw.get("session_secret")

    def _apply_env_overrides(self) -> None:
        env = os.environ
        openai_key = env.get("OPENAI_KEY") or env.get("OPENAI_API_KEY")
        if openai_key:
            self._env_overrides["openai_api_key"] = openai_key
        auth_token = env.get("IMSG_AI_TOKEN")
        if auth_token:
            self._env_overrides["auth_token"] = auth_token
        session_secret = env.get("IMSG_AI_SECRET")
        if session_secret:
            self._env_overrides["session_secret"] = session_secret

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------
    def update_ai_settings(self, updates: Dict[str, Any]) -> None:
        ai = self.ai_settings
        for key, value in updates.items():
            if key not in DEFAULT_AI_SETTINGS:
                continue
            if key == "allowed_users":
                if isinstance(value, list):
                    ai[key] = [self._normalize_handle(v) for v in value if v]
            else:
                ai[key] = value

    def set_allowed_users(self, handles: Iterable[str]) -> None:
        self.ai_settings["allowed_users"] = [self._normalize_handle(h) for h in handles if h]

    def save(self) -> None:
        payload = json.loads(json.dumps(self._data))  # shallow clone for serialization
        ai_payload = payload.setdefault("ai_settings", {})
        allowed = [self._normalize_handle(x) for x in self.ai_settings.get("allowed_users", []) if x]
        ai_payload["allowed_users"] = allowed
        ai_payload["allowed_users_encrypted"] = encrypt_list(allowed)
        CONFIG_FILE.write_text(json.dumps(payload, indent=2))

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_phone(value: str) -> str:
        if not value:
            return ""
        s = str(value).strip()
        if s.lower().startswith("tel:"):
            s = s[4:]
        lead_plus = s.startswith("+")
        digits = "".join(ch for ch in s if ch.isdigit())
        if not digits:
            return ""
        return ("+" if lead_plus else "") + digits

    @classmethod
    def _normalize_handle(cls, value: str) -> str:
        if not value:
            return ""
        s = str(value).strip()
        if "@" in s:
            return s.lower()
        return cls._normalize_phone(s)


def load_config() -> Config:
    return Config()


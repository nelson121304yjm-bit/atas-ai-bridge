"""Session keys and optional macOS Keychain persistence by provider address."""

import sys

from .common import AppError
from .provider import _validate_key, normalize_base_url


class CredentialVault:
    def __init__(self, keychain=None):
        self._keychain = keychain
        self._session = {}

    def _persistent(self):
        if self._keychain is None:
            from .keychain import Keychain
            self._keychain = Keychain()
        return self._keychain

    def set(self, base_url: str, key: str, persist: bool = False) -> None:
        address = normalize_base_url(base_url)
        _validate_key(key)
        if type(persist) is not bool:
            raise AppError("invalid_persistence", "persist must be a boolean.")
        if persist:
            try:
                self._persistent().set(address, key)
            except Exception as exc:
                raise AppError("keychain_error", "Keychain could not save the provider key.", 503) from None
            self._session.pop(address, None)
        else:
            if self._keychain is not None or sys.platform == "darwin":
                try:
                    self._persistent().delete(address)
                except Exception:
                    raise AppError("keychain_error", "Keychain could not clear the provider key.", 503) from None
            self._session[address] = key

    def get(self, base_url: str) -> str | None:
        address = normalize_base_url(base_url)
        if address in self._session:
            return self._session[address]
        try:
            return self._persistent().get(address)
        except Exception:
            raise AppError("keychain_error", "Keychain could not read the provider key.", 503) from None

    def delete(self, base_url: str) -> None:
        address = normalize_base_url(base_url)
        try:
            self._persistent().delete(address)
        except Exception:
            raise AppError("keychain_error", "Keychain could not delete the provider key.", 503) from None
        self._session.pop(address, None)

    def has(self, base_url: str) -> bool:
        return self.get(base_url) is not None

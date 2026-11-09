"""Security.framework generic-password adapter; secrets never enter argv."""

import ctypes
import sys

from .common import AppError


_UTF8 = 0x08000100
_NOT_FOUND = -25300


class Keychain:
    def __init__(self):
        if sys.platform != "darwin":
            raise AppError("keychain_unavailable", "macOS Keychain is unavailable.", 503)
        try:
            self._security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security")
            self._core = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self._configure()
        except (OSError, AttributeError, ValueError):
            raise AppError("keychain_unavailable", "macOS Keychain is unavailable.", 503) from None

    def _configure(self):
        core, security = self._core, self._security
        core.CFStringCreateWithCString.argtypes = (ctypes.c_void_p, ctypes.c_char_p,
                                                    ctypes.c_uint32)
        core.CFStringCreateWithCString.restype = ctypes.c_void_p
        core.CFDataCreate.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8),
                                      ctypes.c_long)
        core.CFDataCreate.restype = ctypes.c_void_p
        core.CFDataGetLength.argtypes = (ctypes.c_void_p,)
        core.CFDataGetLength.restype = ctypes.c_long
        core.CFDataGetBytePtr.argtypes = (ctypes.c_void_p,)
        core.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
        core.CFDictionaryCreateMutable.argtypes = (ctypes.c_void_p, ctypes.c_long,
                                                    ctypes.c_void_p, ctypes.c_void_p)
        core.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        core.CFDictionarySetValue.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p)
        core.CFRelease.argtypes = (ctypes.c_void_p,)
        security.SecItemCopyMatching.argtypes = (ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_void_p))
        security.SecItemCopyMatching.restype = ctypes.c_int32
        security.SecItemAdd.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        security.SecItemAdd.restype = ctypes.c_int32
        security.SecItemUpdate.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        security.SecItemUpdate.restype = ctypes.c_int32
        security.SecItemDelete.argtypes = (ctypes.c_void_p,)
        security.SecItemDelete.restype = ctypes.c_int32
        self._constants = {name: ctypes.c_void_p.in_dll(security, name).value for name in (
            "kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
            "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne")}
        self._constants["kCFBooleanTrue"] = ctypes.c_void_p.in_dll(core, "kCFBooleanTrue").value
        self._key_callbacks = ctypes.addressof(ctypes.c_byte.in_dll(
            core, "kCFTypeDictionaryKeyCallBacks"))
        self._value_callbacks = ctypes.addressof(ctypes.c_byte.in_dll(
            core, "kCFTypeDictionaryValueCallBacks"))

    def _string(self, value: str):
        result = self._core.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)
        if not result:
            raise AppError("keychain_error", "Keychain string allocation failed.", 503)
        return result

    def _data(self, value: str):
        raw = value.encode("utf-8")
        array = (ctypes.c_uint8 * len(raw)).from_buffer_copy(raw)
        result = self._core.CFDataCreate(None, array, len(raw))
        if not result:
            raise AppError("keychain_error", "Keychain data allocation failed.", 503)
        return result

    def _dictionary(self, entries):
        result = self._core.CFDictionaryCreateMutable(None, 0, self._key_callbacks,
                                                       self._value_callbacks)
        if not result:
            raise AppError("keychain_error", "Keychain query allocation failed.", 503)
        for name, value in entries:
            self._core.CFDictionarySetValue(result, self._constants[name], value)
        return result

    def _query(self, address: str):
        service = self._string("ATAS AI Review")
        account = self._string(address)
        try:
            query = self._dictionary((
                ("kSecClass", self._constants["kSecClassGenericPassword"]),
                ("kSecAttrService", service), ("kSecAttrAccount", account)))
        finally:
            self._core.CFRelease(service)
            self._core.CFRelease(account)
        return query

    @staticmethod
    def _check(status):
        if status != 0:
            raise AppError("keychain_error", f"Keychain operation failed (OSStatus {status}).", 503)

    def get(self, address: str) -> str | None:
        query = self._query(address)
        try:
            self._core.CFDictionarySetValue(query, self._constants["kSecReturnData"],
                                             self._constants["kCFBooleanTrue"])
            self._core.CFDictionarySetValue(query, self._constants["kSecMatchLimit"],
                                             self._constants["kSecMatchLimitOne"])
            result = ctypes.c_void_p()
            status = self._security.SecItemCopyMatching(query, ctypes.byref(result))
            if status == _NOT_FOUND:
                return None
            self._check(status)
            try:
                length = self._core.CFDataGetLength(result)
                pointer = self._core.CFDataGetBytePtr(result)
                return ctypes.string_at(pointer, length).decode("utf-8")
            finally:
                self._core.CFRelease(result)
        finally:
            self._core.CFRelease(query)

    def set(self, address: str, key: str) -> None:
        query = self._query(address)
        data = self._data(key)
        attributes = self._dictionary((("kSecValueData", data),))
        try:
            status = self._security.SecItemUpdate(query, attributes)
            if status == _NOT_FOUND:
                self._core.CFDictionarySetValue(query, self._constants["kSecValueData"], data)
                status = self._security.SecItemAdd(query, None)
            self._check(status)
        finally:
            self._core.CFRelease(attributes)
            self._core.CFRelease(data)
            self._core.CFRelease(query)

    def delete(self, address: str) -> None:
        query = self._query(address)
        try:
            status = self._security.SecItemDelete(query)
            if status != _NOT_FOUND:
                self._check(status)
        finally:
            self._core.CFRelease(query)

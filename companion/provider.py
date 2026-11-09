"""Bounded text Chat Completions client for configured providers."""

import ipaddress
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .common import AppError, encode_json


MAX_RESPONSE = 2 * 1024 * 1024
TIMEOUT_SECONDS = 60
MAX_ERROR_RESPONSE = 4096
QUOTA_CODES = {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"}


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        raise AppError("invalid_base_url", "Provider base URL is invalid.")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise AppError("invalid_base_url", "Provider base URL is invalid.") from exc
    if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None or
            parsed.password is not None or "?" in value or "#" in value or
            any(char.isspace() for char in value) or port == 0):
        raise AppError("invalid_base_url", "Provider base URL is invalid.")
    if parsed.scheme == "http":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError()
        except ValueError as exc:
            raise AppError("invalid_base_url", "HTTP providers must use a numeric loopback address.") from exc
    authority = f"[{host}]" if ":" in host else host.lower()
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path.rstrip("/"), "", ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request: Request, timeout: int):
    handlers = [ProxyHandler({})] if urlsplit(request.full_url).scheme == "http" else []
    return build_opener(*handlers, _NoRedirect()).open(request, timeout=timeout)


def _reject_nonfinite(_value):
    raise ValueError("Non-finite JSON number")


def _quota_error(response: HTTPError) -> bool:
    try:
        raw = response.read(MAX_ERROR_RESPONSE + 1)
        if len(raw) > MAX_ERROR_RESPONSE:
            return False
        data = json.loads(raw, parse_constant=_reject_nonfinite)
        error = data.get("error") if isinstance(data, dict) else None
        return isinstance(error, dict) and error.get("code") in QUOTA_CODES
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _validate_key(key):
    if (not isinstance(key, str) or not 1 <= len(key) <= 8192 or
            any(not 33 <= ord(char) <= 126 for char in key)):
        raise AppError("invalid_key", "Provider key is invalid.")


def model_identity(value):
    """Keep only a complete, bounded provider identity; never infer an alias."""
    if (isinstance(value, str) and 1 <= len(value) <= 256 and value == value.strip()
            and value.isprintable()):
        return value
    return None


class ProviderClient:
    def complete(self, base_url: str, model: str, key: str, messages: list) -> dict:
        base_url = normalize_base_url(base_url)
        if (not isinstance(model, str) or not 1 <= len(model) <= 128 or
                any(char in model for char in "\r\n\x00")):
            raise AppError("invalid_model", "Model must contain 1 to 128 safe characters.")
        _validate_key(key)
        if (not isinstance(messages, list) or
                any(not isinstance(message, dict) for message in messages)):
            raise AppError("invalid_messages", "Messages must be a list of objects.")
        try:
            body = encode_json({"model": model, "messages": messages})
        except (TypeError, ValueError, OverflowError) as exc:
            raise AppError("invalid_messages", "Messages must contain finite JSON data.") from exc
        request = Request(base_url + "/chat/completions", data=body, method="POST",
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + key})
        try:
            with _open_no_redirect(request, TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise AppError("provider_status", "Provider returned an unexpected status.", 502)
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise AppError("provider_response_too_large", "Provider response exceeds 2 MiB.", 502)
                request_header = response.headers.get("x-request-id")
        except HTTPError as exc:
            try:
                if exc.code == 401:
                    raise AppError("provider_auth", "Provider rejected credentials.", 401) from None
                if exc.code in (402, 403, 429) and _quota_error(exc):
                    raise AppError("provider_quota", "Provider quota is exhausted.", 402) from None
                if exc.code == 429:
                    raise AppError("provider_rate_limited", "Provider rate limit reached.", 429) from None
                if 300 <= exc.code < 400:
                    raise AppError("provider_redirect", "Provider redirect was rejected.", 502) from None
                raise AppError("provider_http_error", "Provider request failed.", 502) from None
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            raise AppError("provider_unavailable", "Provider connection failed.", 502) from None
        try:
            parsed = json.loads(raw, parse_constant=_reject_nonfinite)
            choice = parsed["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError()
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError,
                ValueError) as exc:
            raise AppError("invalid_provider_response", "Provider response is invalid.", 502) from None
        result = {"text": content}
        actual_model = model_identity(parsed.get("model"))
        if actual_model is not None:
            result["model"] = actual_model
        if "usage" in parsed:
            if not isinstance(parsed["usage"], dict):
                raise AppError("invalid_provider_response", "Provider response is invalid.", 502)
            result["usage"] = parsed["usage"]
        request_id = parsed.get("id") or request_header
        if isinstance(request_id, str):
            result["request_id"] = request_id
        return result

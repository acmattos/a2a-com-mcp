"""Server configuration. The requestState secret comes from the environment and nowhere else."""

from __future__ import annotations

from collections.abc import Mapping

SECRET_ENV_VAR = "REQUEST_STATE_SECRET"
MIN_SECRET_BYTES = 32
# The spec asks for 5 to 30 minutes; 10 minutes is the SDK default and the documented choice.
REQUEST_STATE_TTL_SECONDS = 600


class ConfigError(Exception):
    """Invalid or missing configuration. Messages never contain the secret."""


def load_request_state_secret(environ: Mapping[str, str]) -> str:
    """Return the secret, or raise ``ConfigError``. No generated or built-in fallback exists.

    A hex value (what ``secrets.token_hex(32)`` prints) counts by the bytes it encodes, so 32 hex
    characters are only 16 bytes of randomness and are refused. Any other value counts by its UTF-8 size.
    """
    value = environ.get(SECRET_ENV_VAR, "").strip()
    if not value:
        raise ConfigError(f"{SECRET_ENV_VAR} is not set. Generate one with: "
                          'python -c "import secrets; print(secrets.token_hex(32))"')
    if _size_in_bytes(value) < MIN_SECRET_BYTES:
        raise ConfigError(f"{SECRET_ENV_VAR} must carry at least {MIN_SECRET_BYTES} bytes "
                          "(use secrets.token_hex(32), which prints 64 hex characters)")
    return value


def _size_in_bytes(value: str) -> int:
    try:
        return len(bytes.fromhex(value))
    except ValueError:
        return len(value.encode("utf-8"))

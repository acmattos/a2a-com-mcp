import os
import subprocess
import sys

import pytest

from config import (
    MIN_SECRET_BYTES,
    REQUEST_STATE_TTL_SECONDS,
    ConfigError,
    load_request_state_secret,
)
from conftest import MCP_MAIN


def env(value=None) -> dict:
    return {} if value is None else {"REQUEST_STATE_SECRET": value}


def test_ttl_is_ten_minutes_inside_the_required_five_to_thirty_minute_range():
    assert REQUEST_STATE_TTL_SECONDS == 600
    assert 5 * 60 <= REQUEST_STATE_TTL_SECONDS <= 30 * 60


def test_the_recommended_generator_output_is_accepted():
    secret = "ab" * 32  # what `secrets.token_hex(32)` produces: 64 hex chars = 32 random bytes
    assert load_request_state_secret(env(secret)) == secret
    assert MIN_SECRET_BYTES == 32


def test_surrounding_whitespace_is_ignored():
    secret = "cd" * 32
    assert load_request_state_secret(env(f"  {secret}\r\n")) == secret


@pytest.mark.parametrize("value", [None, "", "   ", "\n"])
def test_missing_or_blank_secret_is_rejected(value):
    with pytest.raises(ConfigError, match="REQUEST_STATE_SECRET"):
        load_request_state_secret(env(value))


@pytest.mark.parametrize("value", [
    "ab" * 31,  # hex of 31 bytes
    "ab" * 16,  # hex of 16 bytes: long as text, short as entropy
    "short",
    "x" * 31,  # not hex, 31 bytes of text
])
def test_secret_with_less_than_32_bytes_is_rejected(value):
    with pytest.raises(ConfigError, match="32"):
        load_request_state_secret(env(value))


def test_non_hex_secret_of_at_least_32_bytes_is_accepted():
    secret = "correct-horse-battery-staple-0123456789"
    assert load_request_state_secret(env(secret)) == secret


def test_error_messages_never_echo_the_secret():
    for value in ("tooshort-SECRET-VALUE", "ab" * 16):
        with pytest.raises(ConfigError) as error:
            load_request_state_secret(env(value))
        assert value not in str(error.value)


@pytest.mark.parametrize("secret", [None, "tooshort"])
def test_server_process_refuses_to_start_without_a_valid_secret(tmp_path, secret):
    environment = {k: v for k, v in os.environ.items() if k != "REQUEST_STATE_SECRET"}
    environment.update(MCP_HOST="127.0.0.1", MCP_PORT="0")
    if secret:
        environment["REQUEST_STATE_SECRET"] = secret
    completed = subprocess.run([sys.executable, str(MCP_MAIN)], cwd=tmp_path, env=environment,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode != 0
    assert "REQUEST_STATE_SECRET" in completed.stderr
    if secret:
        assert secret not in completed.stderr

"""Tests for local provider stream read timeout auto-detection.

When a local LLM provider is detected (Ollama, llama.cpp, vLLM, etc.),
the httpx stream read timeout should be automatically increased from the
default 60s to HERMES_API_TIMEOUT (1800s) to avoid premature connection
kills during long prefill phases.
"""

import os
import pytest
from unittest.mock import patch

from agent.model_metadata import is_local_endpoint


class TestLocalStreamReadTimeout:
    """Verify stream read timeout auto-detection logic."""

    @pytest.mark.parametrize("base_url", [
        "http://localhost:11434",
        "http://127.0.0.1:8080",
        "http://0.0.0.0:5000",
        "http://192.168.1.100:8000",
        "http://10.0.0.5:1234",
        "http://host.docker.internal:11434",
        "http://host.containers.internal:11434",
        "http://host.lima.internal:11434",
    ])
    def test_local_endpoint_bumps_read_timeout(self, base_url):
        """Local endpoint + default timeout -> bumps to base_timeout."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_STREAM_READ_TIMEOUT", None)
            _base_timeout = float(os.getenv("HERMES_API_TIMEOUT", 1800.0))
            _stream_read_timeout = float(os.getenv("HERMES_STREAM_READ_TIMEOUT", 120.0))
            if _stream_read_timeout == 120.0 and base_url and is_local_endpoint(base_url):
                _stream_read_timeout = _base_timeout
            assert _stream_read_timeout == 1800.0

    def test_user_override_respected_for_local(self):
        """User sets HERMES_STREAM_READ_TIMEOUT -> keep their value even for local."""
        with patch.dict(os.environ, {"HERMES_STREAM_READ_TIMEOUT": "300"}, clear=False):
            _base_timeout = float(os.getenv("HERMES_API_TIMEOUT", 1800.0))
            _stream_read_timeout = float(os.getenv("HERMES_STREAM_READ_TIMEOUT", 120.0))
            base_url = "http://localhost:11434"
            if _stream_read_timeout == 120.0 and base_url and is_local_endpoint(base_url):
                _stream_read_timeout = _base_timeout
            assert _stream_read_timeout == 300.0

    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com",
        "https://openrouter.ai/api",
        "https://api.anthropic.com",
    ])
    def test_remote_endpoint_keeps_default(self, base_url):
        """Remote endpoint -> keep 120s default."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_STREAM_READ_TIMEOUT", None)
            _base_timeout = float(os.getenv("HERMES_API_TIMEOUT", 1800.0))
            _stream_read_timeout = float(os.getenv("HERMES_STREAM_READ_TIMEOUT", 120.0))
            if _stream_read_timeout == 120.0 and base_url and is_local_endpoint(base_url):
                _stream_read_timeout = _base_timeout
            assert _stream_read_timeout == 120.0

    def test_empty_base_url_keeps_default(self):
        """No base_url set -> keep 120s default."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_STREAM_READ_TIMEOUT", None)
            _base_timeout = float(os.getenv("HERMES_API_TIMEOUT", 1800.0))
            _stream_read_timeout = float(os.getenv("HERMES_STREAM_READ_TIMEOUT", 120.0))
            base_url = ""
            if _stream_read_timeout == 120.0 and base_url and is_local_endpoint(base_url):
                _stream_read_timeout = _base_timeout
            assert _stream_read_timeout == 120.0


class TestIsLocalEndpoint:
    """Direct unit tests for is_local_endpoint."""

    @pytest.mark.parametrize("url", [
        "http://localhost:11434",
        "http://127.0.0.1:8080",
        "http://0.0.0.0:5000",
        "http://[::1]:11434",
        "http://192.168.1.100:8000",
        "http://10.0.0.5:1234",
        "http://172.17.0.1:11434",
    ])
    def test_classic_local_addresses(self, url):
        assert is_local_endpoint(url) is True

    @pytest.mark.parametrize("url", [
        "http://host.docker.internal:11434",
        "http://host.docker.internal:8080/v1",
        "http://gateway.docker.internal:11434",
        "http://host.containers.internal:11434",
        "http://host.lima.internal:11434",
    ])
    def test_container_dns_names(self, url):
        assert is_local_endpoint(url) is True

    @pytest.mark.parametrize("url", [
        "https://api.openai.com",
        "https://openrouter.ai/api",
        "https://api.anthropic.com",
        "https://evil.docker.internal.example.com",
    ])
    def test_remote_endpoints(self, url):
        assert is_local_endpoint(url) is False

    @pytest.mark.parametrize("url", [
        "http://100.64.0.0:11434",            # lower bound of CGNAT block
        "http://100.64.0.1:11434/v1",         # lower bound +1
        "http://100.77.243.5:11434",          # representative Tailscale host
        "https://100.100.100.100:443",        # Tailscale MagicDNS anchor
        "https://100.127.255.254:443",        # upper bound -1
        "http://100.127.255.255:11434",       # upper bound of CGNAT block
    ])
    def test_tailscale_cgnat_is_local(self, url):
        """Tailscale 100.64.0.0/10 should be treated as local for timeout bumps."""
        assert is_local_endpoint(url) is True

    @pytest.mark.parametrize("url", [
        "http://100.63.255.255:11434",        # just below CGNAT block
        "http://100.128.0.1:11434",           # just above CGNAT block
        "http://100.200.0.1:11434",           # well outside CGNAT
        "http://99.64.0.1:11434",             # first octet wrong
    ])
    def test_near_but_not_cgnat_is_remote(self, url):
        """Hosts adjacent to but outside 100.64.0.0/10 must not match."""
        assert is_local_endpoint(url) is False


class TestStreamingStaleTimeoutLocalBypass:
    """Verify streaming stale timeout bypass for local endpoints.

    Reproduces the bug where HERMES_STREAM_STALE_TIMEOUT=300 (env var)
    prevented the local-endpoint bypass from activating, causing false
    kills on localhost proxies (e.g. Claude Code proxy at localhost:8317).
    """

    def _resolve_streaming_stale_timeout(
        self,
        base_url: str,
        env_stale: str | None = None,
        provider_config_stale: float | None = None,
        est_tokens: int = 0,
    ) -> float:
        """Reproduce the streaming stale timeout resolution logic."""
        from agent.model_metadata import is_local_endpoint

        _stale_from_provider_config = provider_config_stale is not None
        if provider_config_stale is not None:
            _stream_stale_timeout_base = provider_config_stale
        elif env_stale is not None:
            _stream_stale_timeout_base = float(env_stale)
        else:
            _stream_stale_timeout_base = 180.0

        if not _stale_from_provider_config and base_url and is_local_endpoint(base_url):
            return float("inf")

        if est_tokens > 100_000:
            return max(_stream_stale_timeout_base, 300.0)
        elif est_tokens > 50_000:
            return max(_stream_stale_timeout_base, 240.0)
        return _stream_stale_timeout_base

    def test_implicit_default_local_bypass(self):
        """Default (180s) + local endpoint -> inf (disabled)."""
        timeout = self._resolve_streaming_stale_timeout("http://localhost:11434")
        assert timeout == float("inf")

    def test_env_var_local_bypass(self):
        """HERMES_STREAM_STALE_TIMEOUT=300 + localhost -> inf.

        This was the bug: the old code checked == 180.0, so env var
        values prevented the bypass from activating.
        """
        timeout = self._resolve_streaming_stale_timeout(
            "http://localhost:8317/v1",
            env_stale="300",
        )
        assert timeout == float("inf")

    def test_env_var_remote_no_bypass(self):
        """HERMES_STREAM_STALE_TIMEOUT=300 + remote -> 300 (env respected)."""
        timeout = self._resolve_streaming_stale_timeout(
            "https://api.openai.com",
            env_stale="300",
        )
        assert timeout == 300.0

    def test_provider_config_local_no_bypass(self):
        """Provider config stale_timeout_seconds=600 + localhost -> 600.

        Explicit per-provider config must always be respected.
        """
        timeout = self._resolve_streaming_stale_timeout(
            "http://localhost:8317/v1",
            provider_config_stale=600.0,
        )
        assert timeout == 600.0

    def test_provider_config_overrides_env_for_local(self):
        """Provider config takes priority over env var for local endpoints."""
        timeout = self._resolve_streaming_stale_timeout(
            "http://localhost:8317/v1",
            env_stale="300",
            provider_config_stale=120.0,
        )
        assert timeout == 120.0

    def test_env_var_remote_with_context_scaling(self):
        """Remote + env var + 68k tokens -> max(300, 240) = 300."""
        timeout = self._resolve_streaming_stale_timeout(
            "https://api.openai.com",
            env_stale="300",
            est_tokens=68_000,
        )
        assert timeout == 300.0

    def test_no_env_no_config_remote_default(self):
        """Remote + no config + no env -> 180s default."""
        timeout = self._resolve_streaming_stale_timeout(
            "https://api.openai.com",
        )
        assert timeout == 180.0

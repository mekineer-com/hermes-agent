"""Tests for WhatsApp connect() error handling.

Regression tests for two bugs in WhatsAppAdapter.connect():

1. Uninitialized ``data`` variable: when ``resp.json()`` raised after the
   health endpoint returned HTTP 200, ``http_ready`` was set to True but
   ``data`` was never assigned.  The subsequent ``data.get("status")``
   check raised ``NameError``.

2. Bridge log file handle leaked on error paths: the file was opened before
   the health-check loop but never closed when ``connect()`` returned False.
   Repeated connection failures accumulated open file descriptors.
"""

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.whatsapp_wal import WhatsAppGatewayWal
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _make_adapter():
    """Create a WhatsAppAdapter with test attributes (bypass __init__)."""
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter._bridge_port = 19876
    adapter._bridge_script = "/tmp/test-bridge.js"
    adapter._session_path = Path("/tmp/test-wa-session")
    adapter._bridge_log_fh = None
    adapter._bridge_log = None
    adapter._bridge_process = None
    adapter._reply_prefix = None
    adapter._running = False
    adapter._message_handler = None
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._background_tasks = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._message_queue = asyncio.Queue()
    adapter._http_session = None
    adapter._max_message_age_seconds = 300
    adapter._bridge_health = {}
    adapter._web_source_enabled = False
    adapter._web_source_process = None
    adapter._web_source_pid_path = Path("/tmp/test-wa-web-source.pid")
    adapter._web_source_log_fh = None
    adapter._web_source_error = None
    adapter._web_source_intentionally_stopped = False
    adapter._last_runtime_status_refresh = 0.0
    adapter._contact_store = MagicMock()
    adapter._gateway_wal = MagicMock()
    adapter._gateway_wal.append.side_effect = (
        lambda event: {"wal_seq": int(event.get("seq") or 1), "bridge_seq": int(event.get("seq") or 1)}
    )
    return adapter


def _live_event():
    return SimpleNamespace(raw_message={"deliveryMode": "live"}, message_type=None)


@pytest.mark.asyncio
async def test_build_message_event_carries_bridge_message_id_to_source():
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={"session_name": "test"}))

    event = await adapter._build_message_event(
        {
            "deliveryMode": "live",
            "chatId": "chat123",
            "messageId": "wamid.123",
            "senderId": "chat123",
            "senderName": "Test Contact",
            "body": "hi",
        }
    )

    assert event is not None
    assert event.message_id == "wamid.123"
    assert event.source.message_id == "wamid.123"


def _mock_aiohttp(status=200, json_data=None, json_side_effect=None):
    """Build a mock ``aiohttp.ClientSession`` returning a fixed response."""
    mock_resp = MagicMock()
    mock_resp.status = status
    if json_side_effect:
        mock_resp.json = AsyncMock(side_effect=json_side_effect)
    else:
        mock_resp.json = AsyncMock(return_value=json_data or {})

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_AsyncCM(mock_resp))

    return MagicMock(return_value=_AsyncCM(mock_session))


def _connect_patches(mock_proc, mock_fh, mock_client_cls=None):
    """Return a dict of common patches needed to reach the health-check loop."""
    patches = {
        "gateway.platforms.whatsapp.check_whatsapp_requirements": True,
        "gateway.platforms.whatsapp.asyncio.create_task": MagicMock(),
    }
    base = [
        patch("gateway.platforms.whatsapp.check_whatsapp_requirements", return_value=True),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "mkdir", return_value=None),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("builtins.open", return_value=mock_fh),
        patch("gateway.platforms.whatsapp.asyncio.sleep", new_callable=AsyncMock),
        patch("gateway.platforms.whatsapp.asyncio.create_task"),
    ]
    if mock_client_cls is not None:
        base.append(patch("aiohttp.ClientSession", mock_client_cls))
    return base


# ---------------------------------------------------------------------------
# _close_bridge_log() unit tests
# ---------------------------------------------------------------------------

class TestCloseBridgeLog:
    """Direct tests for the _close_bridge_log() helper method."""

    @staticmethod
    def _bare_adapter():
        from gateway.platforms.whatsapp import WhatsAppAdapter
        a = WhatsAppAdapter.__new__(WhatsAppAdapter)
        a._bridge_log_fh = None
        return a

    def test_closes_open_handle(self):
        adapter = self._bare_adapter()
        mock_fh = MagicMock()
        adapter._bridge_log_fh = mock_fh

        adapter._close_bridge_log()

        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None

    def test_noop_when_no_handle(self):
        adapter = self._bare_adapter()

        adapter._close_bridge_log()  # must not raise

        assert adapter._bridge_log_fh is None

    def test_suppresses_close_exception(self):
        adapter = self._bare_adapter()
        mock_fh = MagicMock()
        mock_fh.close.side_effect = OSError("already closed")
        adapter._bridge_log_fh = mock_fh

        adapter._close_bridge_log()  # must not raise

        assert adapter._bridge_log_fh is None


# ---------------------------------------------------------------------------
# data variable initialization
# ---------------------------------------------------------------------------

class TestDataInitialized:
    """Verify ``data = {}`` prevents NameError when resp.json() fails."""

    @pytest.mark.asyncio
    async def test_no_name_error_when_json_always_fails(self):
        """HTTP 200 sets http_ready but json() always raises.

        Without the fix, ``data`` was never assigned and the Phase 2 check
        ``data.get("status")`` raised NameError.  With ``data = {}``, the
        check evaluates to ``None != "connected"`` and Phase 2 runs normally.
        """
        adapter = _make_adapter()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # bridge stays alive

        mock_client_cls = _mock_aiohttp(
            status=200, json_side_effect=ValueError("bad json"),
        )
        mock_fh = MagicMock()

        patches = _connect_patches(mock_proc, mock_fh, mock_client_cls)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], \
             patch.object(type(adapter), "_poll_messages", return_value=MagicMock()):
            # Must NOT raise NameError
            result = await adapter.connect()

        # connect() returns True (warn-and-proceed path)
        assert result is True
        assert adapter._running is True


# ---------------------------------------------------------------------------
# File handle cleanup on error paths
# ---------------------------------------------------------------------------

class TestFileHandleClosedOnError:
    """Verify the bridge log file handle is closed on every failure path."""

    @pytest.mark.asyncio
    async def test_closed_when_bridge_dies_phase1(self):
        """Bridge process exits during Phase 1 health-check loop."""
        adapter = _make_adapter()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # dead immediately
        mock_proc.returncode = 1

        mock_fh = MagicMock()
        patches = _connect_patches(mock_proc, mock_fh)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7]:
            result = await adapter.connect()

        assert result is False
        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None


class TestConnectCleanup:
    """Verify failure paths release the scoped session lock."""

    @pytest.mark.asyncio
    async def test_releases_lock_when_npm_install_fails(self):
        adapter = _make_adapter()

        def _path_exists(path_obj):
            return not str(path_obj).endswith("node_modules")

        install_result = MagicMock(returncode=1, stderr="install failed")

        with patch("gateway.platforms.whatsapp.check_whatsapp_requirements", return_value=True), \
             patch.object(Path, "exists", autospec=True, side_effect=_path_exists), \
             patch("subprocess.run", return_value=install_result), \
             patch("gateway.status.acquire_scoped_lock", return_value=(True, None)), \
             patch("gateway.status.release_scoped_lock") as mock_release:
            result = await adapter.connect()

        assert result is False
        mock_release.assert_called_once_with("whatsapp-session", str(adapter._session_path))
        assert adapter._platform_lock_identity is None


class TestBridgeRuntimeFailure:
    """Verify runtime bridge death is surfaced as a fatal adapter error."""

    @pytest.mark.asyncio
    async def test_send_marks_retryable_fatal_when_managed_bridge_exits(self):
        adapter = _make_adapter()
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._running = True
        adapter._http_session = MagicMock()  # Persistent session active
        mock_fh = MagicMock()
        adapter._bridge_log_fh = mock_fh

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 7
        adapter._bridge_process = mock_proc

        result = await adapter.send("chat-123", "hello")

        assert result.success is False
        assert "exited unexpectedly" in result.error
        assert adapter.fatal_error_code == "whatsapp_bridge_exited"
        assert adapter.fatal_error_retryable is True
        fatal_handler.assert_awaited_once()
        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None

    @pytest.mark.asyncio
    async def test_poll_messages_marks_retryable_fatal_when_managed_bridge_exits(self):
        adapter = _make_adapter()
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._running = True
        adapter._http_session = MagicMock()  # Persistent session active
        mock_fh = MagicMock()
        adapter._bridge_log_fh = mock_fh

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 23
        adapter._bridge_process = mock_proc

        await adapter._poll_messages()

        assert adapter.fatal_error_code == "whatsapp_bridge_exited"
        assert adapter.fatal_error_retryable is True
        fatal_handler.assert_awaited_once()
        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("returncode", [0, -2, -15])
    async def test_shutdown_suppresses_fatal_on_planned_bridge_exit(self, returncode):
        """During graceful disconnect(), SIGTERM/SIGINT/clean-exit are NOT fatal.

        Regression guard for the bug where every gateway shutdown/restart
        logged "Fatal whatsapp adapter error (whatsapp_bridge_exited)" and
        dispatched a fatal-error notification just before the normal
        "✓ whatsapp disconnected" — because _check_managed_bridge_exit()
        saw the bridge's returncode of -15 (our own SIGTERM) and classified
        it as an unexpected crash.
        """
        adapter = _make_adapter()
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._running = True
        adapter._http_session = MagicMock()
        adapter._bridge_log_fh = MagicMock()
        adapter._shutting_down = True  # disconnect() sets this before SIGTERM

        mock_proc = MagicMock()
        mock_proc.poll.return_value = returncode
        adapter._bridge_process = mock_proc

        result = await adapter._check_managed_bridge_exit()

        assert result is None, (
            f"returncode={returncode} during shutdown should be suppressed, "
            f"got fatal message: {result!r}"
        )
        assert adapter.fatal_error_code is None
        fatal_handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_still_surfaces_nonzero_crash(self):
        """Even during shutdown, a truly crashed bridge (e.g. returncode 9) is fatal.

        The suppression list is deliberately narrow (0, -2, -15) so that
        OOM-kill (137), assertion failures, or custom error exits still
        reach the fatal-error handler and user notification path.
        """
        adapter = _make_adapter()
        fatal_handler = AsyncMock()
        adapter.set_fatal_error_handler(fatal_handler)
        adapter._running = True
        adapter._http_session = MagicMock()
        adapter._bridge_log_fh = MagicMock()
        adapter._shutting_down = True

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 137  # SIGKILL / OOM-kill
        adapter._bridge_process = mock_proc

        result = await adapter._check_managed_bridge_exit()

        assert result is not None
        assert "exited unexpectedly" in result
        assert adapter.fatal_error_code == "whatsapp_bridge_exited"
        fatal_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_when_http_not_ready(self):
        """Health endpoint never returns 200 within 15 attempts."""
        adapter = _make_adapter()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # bridge alive

        mock_client_cls = _mock_aiohttp(status=503)
        mock_fh = MagicMock()
        patches = _connect_patches(mock_proc, mock_fh, mock_client_cls)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8]:
            result = await adapter.connect()

        assert result is False
        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None

    @pytest.mark.asyncio
    async def test_closed_when_bridge_dies_phase2(self):
        """Bridge alive during Phase 1 but dies during Phase 2."""
        adapter = _make_adapter()

        # Phase 1 (15 iterations): alive.  Phase 2 (iteration 16): dead.
        call_count = [0]

        def poll_side_effect():
            call_count[0] += 1
            return None if call_count[0] <= 15 else 1

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = poll_side_effect
        mock_proc.returncode = 1

        # Health returns 200 with status != "connected" -> triggers Phase 2
        mock_client_cls = _mock_aiohttp(
            status=200, json_data={"status": "disconnected"},
        )
        mock_fh = MagicMock()
        patches = _connect_patches(mock_proc, mock_fh, mock_client_cls)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8]:
            result = await adapter.connect()

        assert result is False
        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None

    @pytest.mark.asyncio
    async def test_closed_on_unexpected_exception(self):
        """Popen raises, outer except block must still close the handle."""
        adapter = _make_adapter()

        mock_fh = MagicMock()

        with patch("gateway.platforms.whatsapp.check_whatsapp_requirements", return_value=True), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "mkdir", return_value=None), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("subprocess.Popen", side_effect=OSError("spawn failed")), \
             patch("builtins.open", return_value=mock_fh):
            result = await adapter.connect()

        assert result is False
        mock_fh.close.assert_called_once()
        assert adapter._bridge_log_fh is None


# ---------------------------------------------------------------------------
# _kill_port_process() cross-platform tests
# ---------------------------------------------------------------------------

class TestKillPortProcess:
    """Verify _kill_port_process uses platform-appropriate commands."""

    def test_uses_netstat_and_taskkill_on_windows(self):
        from gateway.platforms.whatsapp import _kill_port_process

        netstat_output = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    0.0.0.0:3000           0.0.0.0:0              LISTENING       12345\n"
            "  TCP    0.0.0.0:3001           0.0.0.0:0              LISTENING       99999\n"
        )
        mock_netstat = MagicMock(stdout=netstat_output)
        mock_taskkill = MagicMock()

        def run_side_effect(cmd, **kwargs):
            if cmd[0] == "netstat":
                return mock_netstat
            if cmd[0] == "taskkill":
                return mock_taskkill
            return MagicMock()

        with patch("gateway.platforms.whatsapp._IS_WINDOWS", True), \
             patch("gateway.platforms.whatsapp.subprocess.run", side_effect=run_side_effect) as mock_run:
            _kill_port_process(3000)

        # netstat called
        assert any(
            call.args[0][0] == "netstat" for call in mock_run.call_args_list
        )
        # taskkill called with correct PID
        assert any(
            call.args[0] == ["taskkill", "/PID", "12345", "/F"]
            for call in mock_run.call_args_list
        )

    def test_does_not_kill_wrong_port_on_windows(self):
        from gateway.platforms.whatsapp import _kill_port_process

        netstat_output = (
            "  TCP    0.0.0.0:30000          0.0.0.0:0              LISTENING       55555\n"
        )
        mock_netstat = MagicMock(stdout=netstat_output)

        with patch("gateway.platforms.whatsapp._IS_WINDOWS", True), \
             patch("gateway.platforms.whatsapp.subprocess.run", return_value=mock_netstat) as mock_run:
            _kill_port_process(3000)

        # Should NOT call taskkill because port 30000 != 3000
        assert not any(
            call.args[0][0] == "taskkill"
            for call in mock_run.call_args_list
        )

    def test_uses_fuser_on_linux(self):
        from gateway.platforms.whatsapp import _kill_port_process

        mock_check = MagicMock(returncode=0)

        with patch("gateway.platforms.whatsapp._IS_WINDOWS", False), \
             patch("gateway.platforms.whatsapp.subprocess.run", return_value=mock_check) as mock_run:
            _kill_port_process(3000)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["fuser", "3000/tcp"] in calls
        assert ["fuser", "-k", "3000/tcp"] in calls

    def test_skips_fuser_kill_when_port_free(self):
        from gateway.platforms.whatsapp import _kill_port_process

        mock_check = MagicMock(returncode=1)  # port not in use

        with patch("gateway.platforms.whatsapp._IS_WINDOWS", False), \
             patch("gateway.platforms.whatsapp.subprocess.run", return_value=mock_check) as mock_run:
            _kill_port_process(3000)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["fuser", "3000/tcp"] in calls
        assert ["fuser", "-k", "3000/tcp"] not in calls

    def test_suppresses_exceptions(self):
        from gateway.platforms.whatsapp import _kill_port_process

        with patch("gateway.platforms.whatsapp._IS_WINDOWS", True), \
             patch("gateway.platforms.whatsapp.subprocess.run", side_effect=OSError("no netstat")):
            _kill_port_process(3000)  # must not raise


class TestStaleBridgePidfile:
    def test_kills_only_matching_bridge_process(self, tmp_path):
        from gateway.platforms.whatsapp import _kill_stale_bridge_by_pidfile

        session = tmp_path / "session"
        session.mkdir()
        bridge = tmp_path / "bridge.js"
        bridge.write_text("// stub")
        (session / "bridge.pid").write_text("123")

        with patch("gateway.status._pid_exists", side_effect=[True, False]), \
             patch("gateway.platforms.whatsapp._pid_cmdline", return_value=["node", str(bridge), "--session", str(session)]), \
             patch("gateway.platforms.whatsapp._terminate_pid_tree") as terminate:
            _kill_stale_bridge_by_pidfile(session, bridge)

        terminate.assert_called_once_with(123, force=False)
        assert not (session / "bridge.pid").exists()

    def test_ignores_pidfile_when_command_does_not_match(self, tmp_path):
        from gateway.platforms.whatsapp import _kill_stale_bridge_by_pidfile

        session = tmp_path / "session"
        session.mkdir()
        bridge = tmp_path / "bridge.js"
        bridge.write_text("// stub")
        (session / "bridge.pid").write_text("123")

        with patch("gateway.status._pid_exists", return_value=True), \
             patch("gateway.platforms.whatsapp._pid_cmdline", return_value=["node", "/other/bridge.js"]), \
             patch("gateway.platforms.whatsapp._terminate_pid_tree") as terminate:
            _kill_stale_bridge_by_pidfile(session, bridge)

        terminate.assert_not_called()
        assert not (session / "bridge.pid").exists()


# ---------------------------------------------------------------------------
# Persistent HTTP session lifecycle
# ---------------------------------------------------------------------------

class TestHttpSessionLifecycle:
    """Verify persistent aiohttp.ClientSession is created and cleaned up."""

    @pytest.mark.asyncio
    async def test_disconnect_uses_taskkill_tree_on_windows(self):
        """Windows disconnect should target the bridge process tree, not just the parent PID."""
        adapter = _make_adapter()
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.side_effect = [0]
        adapter._bridge_process = mock_proc
        adapter._poll_task = None
        adapter._http_session = None
        adapter._running = True
        adapter._session_lock_identity = None

        with patch("gateway.platforms.whatsapp._IS_WINDOWS", True), \
             patch("gateway.platforms.whatsapp.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run, \
             patch("gateway.platforms.whatsapp.asyncio.sleep", new_callable=AsyncMock):
            await adapter.disconnect()

        mock_run.assert_called_once_with(
            ["taskkill", "/PID", "12345", "/T"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        mock_proc.terminate.assert_not_called()
        mock_proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_closed_on_disconnect(self):
        """disconnect() should close self._http_session."""
        adapter = _make_adapter()
        mock_session = AsyncMock()
        mock_session.closed = False
        adapter._http_session = mock_session
        adapter._poll_task = None
        adapter._bridge_process = None
        adapter._running = True
        adapter._session_lock_identity = None

        await adapter.disconnect()

        mock_session.close.assert_called_once()
        assert adapter._http_session is None

    @pytest.mark.asyncio
    async def test_session_not_closed_when_already_closed(self):
        """disconnect() should skip close() when session is already closed."""
        adapter = _make_adapter()
        mock_session = AsyncMock()
        mock_session.closed = True
        adapter._http_session = mock_session
        adapter._poll_task = None
        adapter._bridge_process = None
        adapter._running = True
        adapter._session_lock_identity = None

        await adapter.disconnect()

        mock_session.close.assert_not_called()
        assert adapter._http_session is None

    @pytest.mark.asyncio
    async def test_poll_task_cancelled_on_disconnect(self):
        """disconnect() should cancel the poll task."""
        adapter = _make_adapter()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        mock_future = asyncio.Future()
        mock_future.set_exception(asyncio.CancelledError())
        mock_task.__await__ = mock_future.__await__
        adapter._poll_task = mock_task
        adapter._http_session = None
        adapter._bridge_process = None
        adapter._running = True
        adapter._session_lock_identity = None

        await adapter.disconnect()

        mock_task.cancel.assert_called_once()
        assert adapter._poll_task is None

    @pytest.mark.asyncio
    async def test_disconnect_skips_done_poll_task(self):
        """disconnect() should not cancel an already-done poll task."""
        adapter = _make_adapter()
        mock_task = MagicMock()
        mock_task.done.return_value = True
        adapter._poll_task = mock_task
        adapter._http_session = None
        adapter._bridge_process = None
        adapter._running = True
        adapter._session_lock_identity = None

        await adapter.disconnect()

        mock_task.cancel.assert_not_called()
        assert adapter._poll_task is None


class TestDurableBridgeAck:
    @pytest.mark.asyncio
    async def test_poll_messages_acks_each_wal_row(self):
        adapter = _make_adapter()
        adapter._running = True
        adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
        adapter.handle_message = AsyncMock()
        adapter._build_message_event = AsyncMock(side_effect=[_live_event(), _live_event()])

        first_resp = MagicMock(status=200)
        first_resp.json = AsyncMock(return_value=[
            {"seq": 11, "chatId": "a@lid", "body": "one", "deliveryMode": "live"},
            {"seq": 12, "chatId": "a@lid", "body": "two", "deliveryMode": "live"},
        ])
        second_resp = MagicMock(status=200)
        second_resp.json = AsyncMock(return_value=[])
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=[_AsyncCM(first_resp), _AsyncCM(second_resp)])
        mock_session.post = MagicMock(return_value=_AsyncCM(MagicMock(status=200)))
        adapter._http_session = mock_session

        async def _stop_after_sleep(*_args, **_kwargs):
            adapter._running = False

        with patch("gateway.platforms.whatsapp.asyncio.sleep", new=AsyncMock(side_effect=_stop_after_sleep)):
            await adapter._poll_messages()

        assert adapter.handle_message.await_count == 2
        assert mock_session.post.call_args_list[0].kwargs["json"] == {"up_to_seq": 11}
        assert mock_session.post.call_args_list[1].kwargs["json"] == {"up_to_seq": 12}

    @pytest.mark.asyncio
    async def test_poll_messages_marks_filtered_wal_row_processed(self):
        adapter = _make_adapter()
        adapter._running = True
        adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
        adapter._build_message_event = AsyncMock(return_value=None)
        wal = MagicMock()
        wal.append.return_value = {"wal_seq": 41, "bridge_seq": 77}
        adapter._gateway_wal = wal

        first_resp = MagicMock(status=200)
        first_resp.json = AsyncMock(return_value=[{"seq": 77, "chatId": "a@lid", "body": "ignored"}])
        second_resp = MagicMock(status=200)
        second_resp.json = AsyncMock(return_value=[])
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=[_AsyncCM(first_resp), _AsyncCM(second_resp)])
        mock_session.post = MagicMock(return_value=_AsyncCM(MagicMock(status=200)))
        adapter._http_session = mock_session

        async def _sleep_once(*_args, **_kwargs):
            adapter._running = False

        with patch("gateway.platforms.whatsapp.asyncio.sleep", new=AsyncMock(side_effect=_sleep_once)):
            await adapter._poll_messages()

        wal.mark_processed.assert_called_once_with(41)

    @pytest.mark.asyncio
    async def test_poll_messages_skips_row_without_seq(self):
        adapter = _make_adapter()
        adapter._running = True
        adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
        adapter._build_message_event = AsyncMock()
        adapter._gateway_wal = MagicMock()
        adapter._gateway_wal.append.return_value = None

        first_resp = MagicMock(status=200)
        first_resp.json = AsyncMock(return_value=[{"chatId": "a@lid", "body": "bad"}])
        second_resp = MagicMock(status=200)
        second_resp.json = AsyncMock(return_value=[])
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=[_AsyncCM(first_resp), _AsyncCM(second_resp)])
        mock_session.post = MagicMock(return_value=_AsyncCM(MagicMock(status=200)))
        adapter._http_session = mock_session

        async def _sleep_once(*_args, **_kwargs):
            adapter._running = False

        with patch("gateway.platforms.whatsapp.asyncio.sleep", new=AsyncMock(side_effect=_sleep_once)):
            await adapter._poll_messages()

        adapter._build_message_event.assert_not_called()
        mock_session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_gateway_wal_dispatches_pending_rows(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        event = _live_event()
        event.raw_message = {"chatId": "x", "deliveryMode": "live"}
        adapter._build_message_event = AsyncMock(return_value=event)
        wal = MagicMock()
        wal.pending.return_value = [
            {"wal_seq": 3, "bridge_seq": 17, "event": {"seq": 17, "chatId": "a@lid", "body": "x", "deliveryMode": "live"}},
        ]
        adapter._gateway_wal = wal

        await adapter._replay_gateway_wal()

        adapter.handle_message.assert_awaited_once()
        assert event.raw_message["wal_seq"] == 3
        wal.mark_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_processing_complete_advances_wal_offset(self):
        adapter = _make_adapter()
        processed = []
        adapter._session_store = SimpleNamespace(
            _db=SimpleNamespace(
                mark_message_source_key_processed=lambda **kwargs: processed.append(kwargs)
            )
        )
        event = SimpleNamespace(
            raw_message={
                "wal_seq": 12,
                "chatId": "12025550199@lid",
                "messageId": "wamid.12",
            }
        )

        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        adapter._gateway_wal.mark_processed.assert_called_once_with(12)
        assert processed == [
            {
                "source_chat_id": "12025550199@lid",
                "source_message_id": "wamid.12",
            }
        ]

    @pytest.mark.asyncio
    async def test_failed_processing_advances_wal_offset_without_source_key(self):
        adapter = _make_adapter()
        db = SimpleNamespace(mark_message_source_key_processed=MagicMock())
        adapter._session_store = SimpleNamespace(_db=db)
        event = SimpleNamespace(
            raw_message={
                "wal_seq": 12,
                "chatId": "12025550199@lid",
                "messageId": "wamid.12",
            }
        )

        await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

        adapter._gateway_wal.mark_processed.assert_called_once_with(12)
        db.mark_message_source_key_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_gateway_wal_raises_on_invalid_row_payload(self):
        adapter = _make_adapter()
        wal = MagicMock()
        wal.pending.return_value = [{"wal_seq": 5, "bridge_seq": 9, "event": "not-a-dict"}]
        adapter._gateway_wal = wal

        with pytest.raises(ValueError, match="Invalid WhatsApp WAL row payload"):
            await adapter._replay_gateway_wal()


class TestGatewayWalCrashWindows:
    @pytest.mark.asyncio
    async def test_ack_then_dispatch_crash_replays_after_restart(self, tmp_path):
        wal_path = tmp_path / "gateway_wal.jsonl"
        offset_path = tmp_path / "gateway_wal.offset"
        wal1 = WhatsAppGatewayWal(wal_path=wal_path, offset_path=offset_path, compact_every=100)

        adapter1 = _make_adapter()
        adapter1._gateway_wal = wal1
        adapter1._running = True
        adapter1._check_managed_bridge_exit = AsyncMock(return_value=None)
        async def _boom_before_dispatch(_event):
            raise RuntimeError("boom-before-dispatch")

        adapter1._build_message_event = _boom_before_dispatch
        adapter1._http_session = MagicMock()
        first_resp = MagicMock(status=200)
        first_resp.json = AsyncMock(return_value=[{"seq": 501, "chatId": "a@lid", "body": "one", "deliveryMode": "live"}])
        adapter1._http_session.get = MagicMock(return_value=_AsyncCM(first_resp))
        adapter1._http_session.post = MagicMock(return_value=_AsyncCM(MagicMock(status=200)))

        async def _sleep_once(*_args, **_kwargs):
            adapter1._running = False

        with patch("gateway.platforms.whatsapp.asyncio.sleep", new=AsyncMock(side_effect=_sleep_once)):
            await adapter1._poll_messages()

        assert [row["bridge_seq"] for row in wal1.pending()] == [501]

        wal2 = WhatsAppGatewayWal(wal_path=wal_path, offset_path=offset_path, compact_every=100)
        adapter2 = _make_adapter()
        adapter2._gateway_wal = wal2
        adapter2.handle_message = AsyncMock()
        replay_event = _live_event()
        adapter2._build_message_event = AsyncMock(return_value=replay_event)

        await adapter2._replay_gateway_wal()

        adapter2.handle_message.assert_awaited_once()
        assert replay_event.raw_message["wal_seq"] == 1

        await adapter2.on_processing_complete(replay_event, ProcessingOutcome.SUCCESS)
        assert wal2.pending() == []


class TestPollLoopDispatch:
    @pytest.mark.asyncio
    async def test_poll_loop_batch_completion_leaves_no_wal_replay(self, tmp_path):
        from gateway.platforms.whatsapp import WhatsAppAdapter

        wal_path = tmp_path / "gateway_wal.jsonl"
        offset_path = tmp_path / "gateway_wal.offset"
        config = PlatformConfig(
            enabled=True,
            extra={
                "session_name": "test",
                "session_path": str(tmp_path / "session"),
                "web_source_enabled": False,
                "text_batch_delay_seconds": 0.01,
                "text_batch_split_delay_seconds": 0.01,
                "max_message_age_seconds": 0,
            },
        )
        adapter = WhatsAppAdapter(config)
        adapter._gateway_wal = WhatsAppGatewayWal(wal_path=wal_path, offset_path=offset_path, compact_every=100)
        adapter._running = True
        adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
        adapter._check_web_source_exit = MagicMock()
        adapter._write_whatsapp_runtime_status = MagicMock()
        delivered = []

        async def _handle(event):
            delivered.append(event)
            await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        adapter.handle_message = AsyncMock(side_effect=_handle)
        first_resp = MagicMock(status=200)
        first_resp.json = AsyncMock(return_value=[
            {"seq": 1, "chatId": "chat123", "messageId": "msg-1", "senderId": "chat123", "body": "one", "deliveryMode": "live"},
            {"seq": 2, "chatId": "chat123", "messageId": "msg-2", "senderId": "chat123", "body": "two", "deliveryMode": "live"},
            {"seq": 3, "chatId": "chat123", "messageId": "msg-3", "senderId": "chat123", "body": "three", "deliveryMode": "live"},
        ])
        second_resp = MagicMock(status=200)
        second_resp.json = AsyncMock(return_value=[])
        adapter._http_session = MagicMock()
        adapter._http_session.get = MagicMock(side_effect=[_AsyncCM(first_resp), _AsyncCM(second_resp)])
        adapter._http_session.post = MagicMock(return_value=_AsyncCM(MagicMock(status=200)))

        real_sleep = asyncio.sleep

        async def _sleep(delay, *args, **kwargs):
            if delay >= 1:
                adapter._running = False
                return None
            return await real_sleep(delay, *args, **kwargs)

        with patch("gateway.platforms.whatsapp.asyncio.sleep", new=AsyncMock(side_effect=_sleep)):
            await adapter._poll_messages()
        await real_sleep(0.05)

        assert [event.text for event in delivered] == ["one\ntwo\nthree"]
        assert WhatsAppGatewayWal(wal_path=wal_path, offset_path=offset_path, compact_every=100).pending() == []

    @pytest.mark.asyncio
    async def test_dispatch_non_live_modes_mark_wal_and_warn_only_for_missing_mode(self, caplog):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        src = SessionSource(platform=Platform.WHATSAPP, chat_id="chat123", chat_type="dm", user_id="user1")

        async def _dispatch(raw):
            await adapter._dispatch_built_message_event(
                MessageEvent(
                    text="body",
                    message_type=MessageType.TEXT,
                    source=src,
                    raw_message=raw,
                )
            )

        with caplog.at_level(logging.WARNING):
            await _dispatch({"deliveryMode": "persist_only", "chatId": "chat123", "messageId": "m7", "wal_seq": 7})
            await _dispatch({"eventType": "revoke", "chatId": "chat123", "messageId": "m8", "wal_seq": 8})
            await _dispatch({"chatId": "chat123", "messageId": "m9", "wal_seq": 9})

        adapter.handle_message.assert_not_called()
        assert adapter._message_handler.await_count == 3
        assert [call.args[0] for call in adapter._gateway_wal.mark_processed.call_args_list] == [7, 8, 9]
        warnings = [
            record.message
            for record in caplog.records
            if "Bridge event missing/invalid deliveryMode" in record.message
        ]
        assert len(warnings) == 1
        assert "m9" in warnings[0]

    @pytest.mark.asyncio
    async def test_stale_live_marks_wal_but_missing_timestamp_batches(self):
        from gateway.platforms.whatsapp import WhatsAppAdapter

        adapter = WhatsAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={"session_name": "test", "max_message_age_seconds": 60},
            )
        )
        adapter._gateway_wal = MagicMock()
        adapter.handle_message = AsyncMock()
        src = SessionSource(platform=Platform.WHATSAPP, chat_id="chat123", chat_type="dm", user_id="user1")

        await adapter._dispatch_built_message_event(
            MessageEvent(
                text="old",
                message_type=MessageType.TEXT,
                source=src,
                raw_message={
                    "deliveryMode": "live",
                    "chatId": "chat123",
                    "messageId": "old",
                    "wal_seq": 5,
                    "timestamp": time.time() - 120,
                },
            )
        )
        await adapter._dispatch_built_message_event(
            MessageEvent(
                text="current",
                message_type=MessageType.TEXT,
                source=src,
                raw_message={"deliveryMode": "live", "chatId": "chat123", "messageId": "current", "wal_seq": 6},
            )
        )

        adapter.handle_message.assert_not_called()
        adapter._gateway_wal.mark_processed.assert_called_once_with(5)
        assert len(adapter._pending_text_batches) == 1
        for task in adapter._pending_text_batch_tasks.values():
            task.cancel()
        await asyncio.gather(*adapter._pending_text_batch_tasks.values(), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_contact_store_updates_from_poll_and_replay(self):
        adapter = _make_adapter()
        adapter._running = True
        adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
        adapter._build_message_event = AsyncMock(return_value=None)
        msg = {"seq": 31, "chatId": "chat123", "messageId": "m31", "senderName": "Test Contact", "body": "hi"}
        first_resp = MagicMock(status=200)
        first_resp.json = AsyncMock(return_value=[msg])
        second_resp = MagicMock(status=200)
        second_resp.json = AsyncMock(return_value=[])
        adapter._http_session = MagicMock()
        adapter._http_session.get = MagicMock(side_effect=[_AsyncCM(first_resp), _AsyncCM(second_resp)])
        calls = []
        adapter._http_session.post = MagicMock(
            side_effect=lambda *args, **kwargs: calls.append("ack") or _AsyncCM(MagicMock(status=200))
        )
        adapter._contact_store.update_from_event.side_effect = lambda *args, **kwargs: calls.append("contact")

        async def _sleep_once(*_args, **_kwargs):
            adapter._running = False

        with patch("gateway.platforms.whatsapp.asyncio.sleep", new=AsyncMock(side_effect=_sleep_once)):
            await adapter._poll_messages()

        adapter._contact_store.update_from_event.assert_called_with(msg, source="gateway_wal")
        assert calls[:2] == ["ack", "contact"]
        replay = _make_adapter()
        replay_msg = {"seq": 32, "chatId": "chat123", "messageId": "m32", "senderName": "Replay Contact", "body": "replay"}
        replay._gateway_wal.pending.return_value = [{"wal_seq": 4, "bridge_seq": 32, "event": replay_msg}]
        replay._build_message_event = AsyncMock(return_value=None)

        await replay._replay_gateway_wal()

        replay._contact_store.update_from_event.assert_called_once_with(replay_msg, source="gateway_wal_replay")


# ---------------------------------------------------------------------------
# Pre-flight: keep setup/status alive when creds.json is missing
# ---------------------------------------------------------------------------


class TestNoCredsPreflight:
    """Missing reply-bridge creds should enter setup mode, not bootstrap Baileys."""

    @pytest.mark.asyncio
    async def test_connect_enters_setup_mode_when_no_creds(self, tmp_path):
        from gateway.platforms.whatsapp import WhatsAppAdapter

        bridge = tmp_path / "bridge.js"
        bridge.write_text("// stub")
        session_path = tmp_path / "session"
        session_path.mkdir()
        adapter = WhatsAppAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "bridge_script": str(bridge),
                    "session_path": str(session_path),
                    "web_source_enabled": False,
                    "web_source_status": str(tmp_path / "status.json"),
                },
            )
        )
        adapter._acquire_platform_lock = MagicMock(return_value=True)
        adapter._release_platform_lock = MagicMock()

        with patch(
            "gateway.platforms.whatsapp.check_whatsapp_requirements",
            return_value=True,
        ):
            result = await adapter.connect()

        assert result is True
        assert adapter._fatal_error_code == "whatsapp_not_paired"
        assert adapter._fatal_error_retryable is False
        assert adapter._bridge_health["status"] == "not_paired"
        adapter._acquire_platform_lock.assert_called_once_with(
            "whatsapp-session", str(session_path), "WhatsApp session"
        )
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_setup_monitor_reconnects_when_creds_appear(self, tmp_path):
        adapter = _make_adapter()
        adapter._running = True
        adapter._http_session = None
        adapter._session_path = tmp_path / "session"
        adapter._session_path.mkdir()
        (adapter._session_path / "creds.json").write_text("{}", encoding="utf-8")
        adapter._release_platform_lock = MagicMock()
        adapter.connect = AsyncMock(return_value=True)

        await adapter._monitor_web_source_setup()

        adapter._release_platform_lock.assert_called_once()
        adapter.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_proceeds_when_creds_present(self, tmp_path):
        """When creds.json exists, the preflight check is bypassed and
        connect() proceeds to the bridge bootstrap path. We don't fully
        simulate the bridge here — we just verify no fast-fail occurs.
        """
        from gateway.platforms.whatsapp import WhatsAppAdapter

        adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
        adapter.platform = Platform.WHATSAPP
        adapter.config = MagicMock()
        adapter._bridge_port = 19877
        bridge = tmp_path / "bridge.js"
        bridge.write_text("// stub")
        adapter._bridge_script = str(bridge)
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "creds.json").write_text("{}")
        adapter._session_path = session_dir
        adapter._bridge_log_fh = None
        adapter._fatal_error_code = None
        adapter._fatal_error_message = None
        adapter._fatal_error_retryable = True
        # Stub _acquire_platform_lock to return False so connect() exits
        # cleanly *after* the preflight, without spawning subprocesses.
        adapter._acquire_platform_lock = MagicMock(return_value=False)

        with patch(
            "gateway.platforms.whatsapp.check_whatsapp_requirements",
            return_value=True,
        ):
            result = await adapter.connect()

        # Preflight passed — exits because we faked lock acquisition,
        # but the fatal-error code is NOT the "not paired" one.
        assert result is False
        assert adapter._fatal_error_code != "whatsapp_not_paired"

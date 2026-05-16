"""Unit tests for amifuse.startup_runner module."""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class TestProcessStackBase:
    """Verify pr_StackBase initialization in _create_process().

    Source inspection tests: fragile by design, to be replaced with
    functional tests when integration test infrastructure is available (Phase 5).
    """

    def test_pr_stackbase_is_stack_upper(self):
        """Verify pr_StackBase is set to the stack upper bound (top of stack)."""
        from amifuse.startup_runner import HandlerLauncher
        source = inspect.getsource(HandlerLauncher._create_process)
        assert "proc.stack_base.bptr = stack.get_upper() >> 2" in source
        assert "proc.stack_base.bptr = stack.get_lower() >> 2" not in source


def test_build_resume_frame_wait_uses_all_pending_signals():
    from amifuse.startup_runner import _build_resume_frame

    cleared = []

    block_state = {
        "waitport_blocked_sp": None,
        "waitport_blocked_port": None,
        "waitport_blocked_ret": None,
        "wait_blocked_mask": 0x12,
        "wait_blocked_sp": 0x2000,
        "wait_blocked_ret": 0x3456,
        "waitpkt_blocked": False,
    }

    class FakeMem:
        def r32(self, _addr):
            raise AssertionError("explicit wait_blocked_ret should be used")

    class FakePortMgr:
        def has_msg(self, _addr):
            return False

        def get_msg(self, _addr):
            raise AssertionError("Wait() resume must not dequeue a message")

    def fake_pending(mask):
        if mask == 0x12:
            return 0x10
        if mask == 0xFFFFFFFF:
            return 0x34
        raise AssertionError(f"unexpected mask {mask:#x}")

    resume = _build_resume_frame(
        block_state,
        default_port_addr=0x9999,
        mem=FakeMem(),
        port_mgr=FakePortMgr(),
        compute_pending_signals=fake_pending,
        clear_signals_from_task=cleared.append,
    )

    assert resume == {"pc": 0x3456, "sp": 0x2004, "d0": 0x34}
    assert cleared == [0x34]


def test_build_resume_frame_waitport_dequeues_message():
    from amifuse.startup_runner import _build_resume_frame

    block_state = {
        "waitport_blocked_sp": 0x1800,
        "waitport_blocked_port": 0x2222,
        "waitport_blocked_ret": 0x4567,
        "wait_blocked_mask": None,
        "wait_blocked_sp": None,
        "wait_blocked_ret": None,
        "waitpkt_blocked": False,
    }

    class FakeMem:
        def __init__(self):
            self.values = {
                0xABC0 + 0: 0x3000,
                0xABC0 + 4: 0x4000,
            }
            self.writes = []

        def r32(self, addr):
            return self.values[addr]

        def w32(self, addr, value):
            self.writes.append((addr, value))

    class FakePortMgr:
        def __init__(self):
            self.got = []

        def has_msg(self, addr):
            return addr == 0x2222

        def get_msg(self, addr):
            self.got.append(addr)
            return 0xABC0

    mem = FakeMem()
    pmgr = FakePortMgr()
    resume = _build_resume_frame(
        block_state,
        default_port_addr=0x9999,
        mem=mem,
        port_mgr=pmgr,
        compute_pending_signals=lambda _mask: 0,
        clear_signals_from_task=lambda _signals: None,
    )

    assert resume == {"pc": 0x4567, "sp": 0x1804, "d0": 0xABC0}
    assert pmgr.got == [0x2222]
    assert mem.writes == [(0x4000, 0x3000), (0x3000 + 4, 0x4000)]


def test_run_burst_reseeds_execbase_when_restarting_at_main_loop_with_null_a6():
    from amifuse.startup_runner import HandlerLauncher

    source = inspect.getsource(HandlerLauncher.run_burst)

    assert "state.pc == state.main_loop_pc" in source
    assert "state.regs[REG_A6] == 0" in source


class TestStdpktRingBuffer:
    """Verify stdpkt ring buffer frees old allocation on growth."""

    def _make_launcher(self):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher.alloc = MagicMock()
        launcher.mem = MagicMock()
        launcher._stdpkt_ring = []
        launcher._stdpkt_sizes = []
        launcher._stdpkt_ring_size = 8
        launcher._stdpkt_index = 0
        # Minimal field offsets (actual values don't matter for allocation tests)
        launcher._msg_size = 20
        launcher._pkt_size = 48
        launcher._msg_ln_type_offset = 0
        launcher._msg_ln_succ_offset = 4
        launcher._msg_ln_pred_offset = 8
        launcher._mn_replyport_offset = 12
        launcher._mn_length_offset = 16
        launcher._msg_ln_name_offset = 18
        launcher._dp_link_offset = 0
        launcher._dp_port_offset = 4
        launcher._dp_type_offset = 8
        launcher._dp_arg1_offset = 12
        launcher._debug = False
        launcher.exec_impl = MagicMock()
        # Stub out _link_msg_to_port — we're only testing allocation behavior
        launcher._link_msg_to_port = lambda *args, **kwargs: None
        return launcher

    def test_stdpkt_first_alloc(self):
        launcher = self._make_launcher()
        new_mem = SimpleNamespace(addr=0x5000, size=68)
        launcher.alloc.alloc_memory.return_value = new_mem

        launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.alloc.alloc_memory.assert_called_once()
        launcher.alloc.free_memory.assert_not_called()
        assert launcher._stdpkt_ring[0] is new_mem

    def test_stdpkt_reuses_existing_slot(self):
        launcher = self._make_launcher()
        existing = SimpleNamespace(addr=0x5000, size=68)
        launcher._stdpkt_ring = [existing] + [None] * 7
        launcher._stdpkt_sizes = [68] + [0] * 7

        launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.alloc.alloc_memory.assert_not_called()
        launcher.alloc.free_memory.assert_not_called()

    def test_stdpkt_grows_and_frees_old_slot(self):
        launcher = self._make_launcher()
        old_mem = SimpleNamespace(addr=0x5000, size=32)
        new_mem = SimpleNamespace(addr=0x6000, size=68)
        launcher._stdpkt_ring = [old_mem] + [None] * 7
        launcher._stdpkt_sizes = [32] + [0] * 7
        launcher.alloc.alloc_memory.return_value = new_mem

        launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.alloc.alloc_memory.assert_called_once()
        launcher.alloc.free_memory.assert_called_once_with(old_mem)
        assert launcher._stdpkt_ring[0] is new_mem

    def test_stdpkt_failed_growth_keeps_old_slot(self):
        launcher = self._make_launcher()
        old_mem = SimpleNamespace(addr=0x5000, size=32)
        launcher._stdpkt_ring = [old_mem] + [None] * 7
        launcher._stdpkt_sizes = [32] + [0] * 7
        launcher.alloc.alloc_memory.side_effect = RuntimeError("oom")

        with pytest.raises(RuntimeError, match="oom"):
            launcher._build_std_packet(0x1000, 0x2000, 1, [])

        assert launcher._stdpkt_ring[0] is old_mem
        assert launcher._stdpkt_sizes[0] == 32
        launcher.alloc.free_memory.assert_not_called()


class TestMetaPacketSenders:
    """Verify ACTION_SET_PROTECT/COMMENT/DATE packet senders marshal correctly."""

    def _make_launcher(self):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher.alloc = MagicMock()
        launcher.mem = MagicMock()
        launcher.send_packet = MagicMock(return_value=(0xAABB, 0xCCDD))
        return launcher

    def _state(self):
        return SimpleNamespace(port_addr=0x1000, reply_port_addr=0x2000)

    def test_send_set_protect_uses_correct_action_and_arg_layout(self):
        from amifuse.startup_runner import ACTION_SET_PROTECT

        launcher = self._make_launcher()
        state = self._state()

        launcher.send_set_protect(state, 0xDEAD0000, 0xBEEF0000, 0x12345678)

        launcher.send_packet.assert_called_once_with(
            state,
            ACTION_SET_PROTECT,
            [0, 0xDEAD0000, 0xBEEF0000, 0x12345678],
        )

    def test_send_set_comment_uses_correct_action_and_arg_layout(self):
        from amifuse.startup_runner import ACTION_SET_COMMENT

        launcher = self._make_launcher()
        state = self._state()

        launcher.send_set_comment(state, 0xDEAD0000, 0xBEEF0000, 0xCAFE0000)

        launcher.send_packet.assert_called_once_with(
            state,
            ACTION_SET_COMMENT,
            [0, 0xDEAD0000, 0xBEEF0000, 0xCAFE0000],
        )

    def test_send_set_date_marshals_datestamp_struct(self, monkeypatch):
        from amifuse import startup_runner
        from amifuse.startup_runner import ACTION_SET_DATE

        launcher = self._make_launcher()
        state = self._state()

        ds_addr = 0x4000
        launcher.alloc.alloc_struct.return_value = SimpleNamespace(addr=ds_addr)

        # Patch DateStampStruct to a recording stub.
        class _Field:
            def __init__(self):
                self.val = None

        class _RecordingDateStamp:
            def __init__(self, mem, addr):
                self.addr = addr
                self.ds_Days = _Field()
                self.ds_Minute = _Field()
                self.ds_Tick = _Field()

        monkeypatch.setattr(startup_runner, "DateStampStruct", _RecordingDateStamp)

        # Capture the instance the sender constructs.
        captured = {}
        orig = _RecordingDateStamp.__init__

        def capturing_init(self, mem, addr):
            orig(self, mem, addr)
            captured["instance"] = self

        monkeypatch.setattr(_RecordingDateStamp, "__init__", capturing_init)

        launcher.send_set_date(
            state, 0xDEAD0000, 0xBEEF0000, days=5471, mins=720, ticks=1500
        )

        launcher.alloc.alloc_struct.assert_called_once()
        ds = captured["instance"]
        assert ds.ds_Days.val == 5471
        assert ds.ds_Minute.val == 720
        assert ds.ds_Tick.val == 1500
        launcher.send_packet.assert_called_once_with(
            state,
            ACTION_SET_DATE,
            [0, 0xDEAD0000, 0xBEEF0000, ds_addr],
        )

    def test_action_constants_have_canonical_values(self):
        """Sanity-check the constants match the AmigaOS DOS packet numbers."""
        from amifuse.startup_runner import (
            ACTION_SET_PROTECT,
            ACTION_SET_COMMENT,
            ACTION_SET_DATE,
        )

        assert ACTION_SET_PROTECT == 21
        assert ACTION_SET_COMMENT == 28
        assert ACTION_SET_DATE == 34

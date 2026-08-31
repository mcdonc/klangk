"""Tests for klangk.interval (IntervalWorker)."""

import asyncio
import types

import pytest


class TestSweepAbstractHook2910:
    def test_missing_sweep_override_raises(self):
        from klangk.interval import IntervalWorker

        class Bare(IntervalWorker):
            pass

        worker = Bare(app=types.SimpleNamespace())
        with pytest.raises(NotImplementedError):
            asyncio.run(worker.sweep())

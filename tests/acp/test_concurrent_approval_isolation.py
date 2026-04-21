"""Regression tests for GHSA-qg5c-hvr5-hjgr.

Before the fix, ``tools.terminal_tool._approval_callback`` was a module-global.
When two ACP sessions overlapped in the same process, session B's
``set_approval_callback`` overwrote session A's — so session A's
dangerous-command approval could be routed through session B's callback
(and vice versa).

The fix stores the callback in a ``ContextVar`` that each asyncio task
gets its own copy of, and ACP's ``prompt`` handler wraps the executor call
with ``contextvars.copy_context().run(...)`` so the per-session callback
survives the hop into the worker thread.

These tests exercise the primitive directly without spinning up a full
``HermesACPAgent`` — they verify that:

1. Two concurrent asyncio tasks can each set ``_approval_callback_var`` to
   a distinct session-specific callback and each see their own value.
2. The value is still visible from inside a ``run_in_executor`` worker
   thread when the caller uses ``copy_context().run``.
3. The raw ``run_in_executor`` path without ``copy_context`` does NOT
   propagate contextvars — this is the asyncio contract we rely on the
   ACP adapter to bridge.
"""

import asyncio
import contextvars

import pytest

from tools import terminal_tool as tt


async def _session(session_id: str, overlap_delay: float, observed: dict):
    """Simulate an ACP session.

    1. Registers a session-specific approval callback via the public
       ``set_approval_callback`` API.
    2. Yields control so sibling tasks can install their own callbacks
       and create a realistic overlap window.
    3. Runs a synchronous worker in a thread executor using
       ``copy_context().run`` (mirrors the ACP adapter's pattern) and
       records which callback identity the worker observes.
    """
    def approval_cb(command, description, **_):
        return f"approval-from-{session_id}"

    tt.set_approval_callback(approval_cb)
    await asyncio.sleep(overlap_delay)

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()

    def _in_worker():
        cb = tt._approval_callback_var.get()
        return cb("rm -rf /", "dangerous") if cb else None

    observed[session_id] = await loop.run_in_executor(
        None, lambda: ctx.run(_in_worker)
    )


class TestConcurrentACPApprovalIsolation:
    """Regression guard for cross-session approval callback confusion."""

    def test_concurrent_sessions_see_their_own_callback(self):
        """Two overlapping ACP sessions each observe their own callback.

        Session A starts first but sleeps longer, so by the time it reads
        its callback, session B has already registered its own. Before
        the ContextVar fix, both sessions would observe whichever callback
        was set most recently in the module-global slot.
        """
        observed: dict = {}

        async def main():
            await asyncio.gather(
                _session("A-cd0fa01e", 0.05, observed),
                _session("B-cc2f5ce8", 0.02, observed),
            )

        asyncio.run(main())

        assert observed["A-cd0fa01e"] == "approval-from-A-cd0fa01e"
        assert observed["B-cc2f5ce8"] == "approval-from-B-cc2f5ce8"

    def test_callback_visible_through_run_in_executor_with_copy_context(self):
        """``copy_context().run`` propagates the callback into the worker thread."""
        async def runner():
            def cb(cmd, desc, **_):
                return "approved"

            tt.set_approval_callback(cb)

            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()

            def _worker():
                got = tt._approval_callback_var.get()
                return got("x", "y") if got else None

            return await loop.run_in_executor(None, lambda: ctx.run(_worker))

        assert asyncio.run(runner()) == "approved"

    def test_set_approval_callback_is_context_scoped(self):
        """A direct ``set_approval_callback`` call does not leak into the caller's context.

        This is the asyncio-level guarantee the ACP fix relies on: a child
        task's ``ContextVar.set`` mutates only the child's context copy.
        """
        observed: dict = {}

        async def child():
            def cb(cmd, desc, **_):
                return "child"
            tt.set_approval_callback(cb)
            observed["child"] = tt._approval_callback_var.get()("x", "y")

        async def main():
            # Parent sees no callback
            observed["parent_before"] = tt._approval_callback_var.get()
            await asyncio.create_task(child())
            # Parent still sees no callback after child completes
            observed["parent_after"] = tt._approval_callback_var.get()

        asyncio.run(main())

        assert observed["parent_before"] is None
        assert observed["child"] == "child"
        assert observed["parent_after"] is None


class TestRunInExecutorContextContract:
    """Document the asyncio contract the ACP adapter relies on."""

    def test_run_in_executor_without_copy_context_does_not_propagate(self):
        """Without ``copy_context().run``, contextvars do NOT cross into the worker.

        This is the asyncio standard-library behavior. If the ACP adapter
        ever drops the ``copy_context().run`` wrapper around ``_run_agent``,
        this test will pass (contextvars will appear empty in the worker)
        while the isolation test above will fail — a clear signal that the
        bridging wrapper is missing.
        """
        probe: contextvars.ContextVar = contextvars.ContextVar("probe", default="unset")

        async def runner():
            probe.set("set-in-task")
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, probe.get)

        # Worker thread does not inherit the task's context
        assert asyncio.run(runner()) == "unset"

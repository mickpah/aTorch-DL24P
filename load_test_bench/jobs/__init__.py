"""Durable job engine for test orchestration.

This package is deliberately Qt-free: it is both the testability boundary and
the Prefect seam. Phases take JSON-serializable params plus an injected
PhaseContext, return a JSON-serializable PhaseResult, and report progress only
through PhaseReporter - so a later orchestrator can wrap a phase as a task
without rewriting it.

Prefect adoption criteria (evaluated 2026-07-24, decision: not now): adopt
only when (a) the rig goes headless and the Qt UI stops being the operator
interface, (b) more than one rig needs central scheduling/observability, or
(c) cross-machine retry/caching/artifact semantics are needed. The
SafetySupervisor stays app-side under every future architecture - an
orchestrator must never be in the emergency-stop path.
"""

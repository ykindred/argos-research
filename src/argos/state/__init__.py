"""Persistent research memory; only trusted orchestration code receives a store."""

from argos.state.store import StateError, StateSnapshot, StateStore

__all__ = ["StateError", "StateSnapshot", "StateStore"]

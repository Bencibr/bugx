#!/usr/bin/env python3
"""core/supervisor/exceptions.py - Supervisor 错误模型与异常层级 (DOC-070)."""

from __future__ import annotations


class SupervisorError(RuntimeError):
    pass


class NoRunError(SupervisorError):
    pass


class RunAlreadyStarted(SupervisorError):
    pass


class InvalidTransition(SupervisorError):
    pass


class BudgetExhausted(SupervisorError):
    pass


class ClaimRejected(SupervisorError):
    pass


class LeaseExpired(SupervisorError):
    pass


class RevisionConflict(SupervisorError):
    pass


class InvalidCheckpoint(SupervisorError):
    pass


class PersistenceError(SupervisorError):
    pass


class WorkspacePathError(SupervisorError):
    pass


class ConcurrencyNotSatisfied(SupervisorError):
    pass

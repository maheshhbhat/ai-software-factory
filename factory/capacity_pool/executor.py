#!/usr/bin/env python3
"""One bounded execution boundary for every model-backed factory workload."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .providers import AttemptResult, ProviderAdapter
from .router import ModelCapacity, RouteRequest, remaining_envelope, route
from .state import CapacityState, CapacityUnavailable, DuplicateTask


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str
    output: str
    attempts: tuple[dict, ...]
    consumed_budget_units: float
    terminal_outcome: str


class CapacityExecutor:
    def __init__(self, adapters: dict[str, ProviderAdapter], state: CapacityState,
                 *, telemetry=lambda **fields: None, monotonic=time.monotonic):
        self.adapters, self.state = adapters, state
        self.emit, self.monotonic = telemetry, monotonic

    def execute(self, *, task_key: str, request: RouteRequest,
                registry: tuple[ModelCapacity, ...], payload,
                validate=lambda output: None, reservation_id: str | None = None,
                on_started=lambda lease: None) -> ExecutionResult:
        try:
            plan = route(request, registry)
        except LookupError:
            return self._finish("no-eligible-capacity", "", [], 0.0,
                                terminal_outcome="not-admitted")
        reserved = None
        if reservation_id is not None:
            try:
                reserved = self.state.reservation(
                    reservation_id, task_key=task_key)
            except CapacityUnavailable as exc:
                return self._finish("no-eligible-capacity", str(exc)[:500], [], 0.0,
                                    terminal_outcome="not-admitted")
            matching = [step for step in plan.steps
                        if (step.provider, step.model) ==
                        (reserved.provider, reserved.model)]
            if not matching:
                return self._finish(
                    "no-eligible-capacity", "reserved route no longer satisfies policy", [], 0.0,
                    terminal_outcome="not-admitted")
            plan = type(plan)(plan.task_type,
                              tuple(matching + [step for step in plan.steps
                                                if step not in matching]),
                              plan.total_timeout_seconds, plan.total_budget_units,
                              plan.fallback_on, plan.stop_on)
        started, consumed, records = self.monotonic(), 0.0, []
        for index, step in enumerate(plan.steps):
            invocation_id = uuid.uuid4().hex
            attempt = None
            lease = None
            observed_failure = True
            elapsed = max(0, int(self.monotonic() - started))
            remaining_time, remaining_budget = remaining_envelope(
                plan, elapsed_seconds=elapsed, consumed_budget_units=consumed)
            if remaining_time <= 0 or remaining_budget <= 0:
                return self._finish("budget-exhausted", "", records, consumed,
                                    terminal_outcome="limit-stopped")
            attempts_left = len(plan.steps) - index
            attempt_time = (remaining_time if attempts_left == 1 else
                            max(1, remaining_time // attempts_left))
            attempt_budget = (remaining_budget if attempts_left == 1 else
                              remaining_budget / attempts_left)
            adapter = self.adapters.get(step.provider)
            if adapter is None:
                if index == 0 and reserved is not None:
                    self.state.release(reserved.lease_id)
                    return self._finish(
                        "start-unavailable", "reserved provider adapter missing",
                        records, consumed, terminal_outcome="launch-failed")
                attempt = AttemptResult("unavailable", diagnostic="provider adapter missing",
                                        process_started=False)
            else:
                try:
                    if index == 0 and reserved is not None:
                        lease = self.state.consume(
                            reserved.lease_id, task_key=task_key)
                        attempt_budget = min(attempt_budget,
                                             lease.reserved_budget)
                        attempt_time = min(
                            attempt_time,
                            max(1, int(lease.expires_at - self.state.clock())))
                        try:
                            on_started(lease)
                        except Exception as exc:
                            self.state.abort_start(lease.lease_id)
                            return self._finish(
                                "start-evidence-failed", str(exc)[:500], records,
                                consumed, terminal_outcome="launch-failed")
                    else:
                        fallback_reservation = self.state.reserve(
                            task_key, step.provider, step.model, attempt_budget,
                            ttl_seconds=attempt_time)
                        lease = self.state.consume(
                            fallback_reservation.lease_id, task_key=task_key)
                        if index > 0:
                            try:
                                on_started(lease)
                            except Exception as exc:
                                self.state.abort_start(lease.lease_id)
                                return self._finish(
                                    "start-evidence-failed", str(exc)[:500],
                                    records, consumed,
                                    terminal_outcome="launch-failed")
                except DuplicateTask:
                    return self._finish("duplicate-execution", "", records, consumed,
                                        terminal_outcome="not-admitted")
                except CapacityUnavailable as exc:
                    if index == 0 and reserved is not None:
                        self.state.release(reserved.lease_id)
                        return self._finish(
                            "start-unavailable", str(exc)[:500], records, consumed,
                            terminal_outcome="launch-failed")
                    attempt = AttemptResult("unavailable", diagnostic=str(exc)[:500],
                                            process_started=False)
                    lease = None
                    observed_failure = False
                try:
                    if lease is not None:
                        attempt = adapter.run(
                            model=step.model, effort=step.effort,
                            timeout_seconds=attempt_time, budget_units=attempt_budget,
                            payload=payload)
                except Exception as exc:  # adapter bugs and unknown exits fail closed
                    attempt = AttemptResult("unknown-failure", diagnostic=str(exc)[:500],
                                            process_started=False)
                finally:
                    # Unreported usage is conservatively charged at reservation.
                    reported = (attempt.consumed_budget_units
                                if attempt is not None and
                                attempt.consumed_budget_units is not None
                                else remaining_budget)
                    if lease is not None and self.state.lease_status(
                            lease.lease_id) in {"active", "consumed"}:
                        consumed += self.state.reconcile(
                            lease.lease_id, consumed_budget_units=reported)
            terminal = self._terminal_attempt_outcome(attempt)
            validation_error = None
            if attempt.succeeded:
                try:
                    validate(attempt.output)
                except Exception as exc:
                    validation_error = exc
                    terminal = "validation-failed"
            usage_receipt = self._usage_receipt(
                attempt, charged_capacity_units=(
                    min(attempt_budget, reported) if lease is not None else 0.0))
            record = {"attempt": index + 1, "invocation_id": invocation_id,
                      "worker_start_invocation_id": (task_key if lease else None),
                      "reservation_id": (lease.lease_id if lease else None),
                      "provider": step.provider,
                      "model": step.model, "tier": step.tier.name.lower(),
                      "effort": step.effort, "outcome": attempt.outcome,
                      "elapsed_seconds": max(0, int(self.monotonic() - started)),
                      "consumed_budget_units": consumed,
                      "mutation_state": attempt.mutation_state,
                      "terminal_outcome": terminal,
                      "usage_receipt": usage_receipt}
            records.append(record)
            self.emit(metric="capacity.route.attempt", **record)
            if attempt.succeeded:
                if validation_error is not None:
                    self.state.mark_quality_failure(
                        step.provider, step.model, "schema-invalid")
                    return self._finish(
                        "schema-invalid", str(validation_error)[:500], records, consumed,
                        terminal_outcome="validation-failed")
                self.state.mark_healthy(step.provider, step.model, "validated-success")
                return self._finish("success", attempt.output, records, consumed,
                                    terminal_outcome="completed")
            if attempt.outcome in plan.fallback_on and observed_failure:
                reason = ("quota-exhausted" if attempt.outcome == "quota"
                          else "rate-limited" if attempt.outcome == "rate-limit"
                          else attempt.outcome)
                affected_model = "*" if attempt.failure_scope == "provider" else step.model
                self.state.mark_failure(step.provider, affected_model, reason)
            if attempt.mutation_state not in {"none", "pre-mutation"}:
                return self._finish("ambiguous-mutation", "", records, consumed,
                                    terminal_outcome=terminal)
            if attempt.outcome in plan.stop_on or attempt.outcome not in plan.fallback_on:
                if attempt.outcome in {
                    "malformed-output", "schema-invalid", "unsafe-output",
                    "scope-violation", "failed-tests",
                }:
                    self.state.mark_quality_failure(
                        step.provider, step.model, attempt.outcome)
                return self._finish(attempt.outcome, attempt.diagnostic[:500],
                                    records, consumed,
                                    terminal_outcome=terminal)
        diagnostic = attempt.diagnostic[:500] if attempt is not None else ""
        return self._finish("no-eligible-capacity", diagnostic, records, consumed,
                            terminal_outcome=(records[-1]["terminal_outcome"]
                                              if records else "not-admitted"))

    @staticmethod
    def _terminal_attempt_outcome(attempt):
        if not attempt.process_started:
            return "launch-failed"
        if attempt.outcome in {"timeout", "quota", "rate-limit", "budget-exhausted"}:
            return "limit-stopped"
        if attempt.outcome in {"malformed-output", "schema-invalid", "unsafe-output",
                               "scope-violation", "failed-tests"}:
            return "validation-failed"
        if attempt.succeeded:
            return "completed"
        return "started-mid-work-failed"

    @staticmethod
    def _usage_receipt(attempt, *, charged_capacity_units):
        if attempt.exact_cost_usd is None and charged_capacity_units is None:
            raise RuntimeError(
                "measurement-integrity failure: invocation has no usage receipt")
        receipt = {
            "reported_usage": dict(attempt.usage) if attempt.usage is not None else None,
            "normalized_capacity_units": (float(charged_capacity_units)
                                           if charged_capacity_units is not None else None),
            "capacity_unit_basis": "reconciled-reservation",
            "exact_cost_usd": attempt.exact_cost_usd,
            "dollar_cost_unavailable_reason": (
                None if attempt.exact_cost_usd is not None else
                attempt.dollar_cost_unavailable_reason or
                "provider-did-not-report-exact-cost"),
        }
        return receipt

    def _finish(self, outcome, output, records, consumed, *, terminal_outcome):
        self.emit(metric="capacity.route.final", outcome=outcome,
                  terminal_outcome=terminal_outcome, attempts=len(records),
                  consumed_budget_units=consumed)
        return ExecutionResult(outcome, output, tuple(records), consumed,
                               terminal_outcome)

#!/usr/bin/env python3
import subprocess
import unittest
from pathlib import Path

from factory.capacity_pool.providers.cli import (
    claude_command, cli_adapter, codex_command,
)


class CapacityProviderTests(unittest.TestCase):
    def test_command_syntax_is_confined_and_model_effort_are_explicit(self):
        self.assertEqual("claude", claude_command(
            model="fable", effort="medium", payload="p", budget_units=2)[0])
        codex = codex_command(model="terra", effort="medium", payload="p")
        self.assertEqual("codex", codex[0])
        self.assertIn("terra", codex)
        self.assertIn('model_reasoning_effort="medium"', codex)

    def test_provider_failure_is_normalized_without_prompt_in_diagnostic(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "rate limit reached")
        adapter = cli_adapter("openai", cwd=Path("."), environment={}, runner=runner)
        result = adapter.run(model="terra", effort="medium", timeout_seconds=5,
                             budget_units=1, payload="secret prompt")
        self.assertEqual("rate-limit", result.outcome)
        self.assertNotIn("secret prompt", result.diagnostic)

    def test_timeout_is_retryable_but_usage_remains_unreported(self):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        adapter = cli_adapter("anthropic", cwd=Path("."), environment={}, runner=runner)
        result = adapter.run(model="balanced", effort="medium", timeout_seconds=5,
                             budget_units=1, payload="p")
        self.assertEqual("timeout", result.outcome)
        self.assertIsNone(result.consumed_budget_units)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import subprocess
import unittest
from pathlib import Path

from factory.capacity_pool.providers.cli import (
    InvocationPayload, claude_command, cli_adapter, codex_command, provider_environment,
)


class CapacityProviderTests(unittest.TestCase):
    def test_workspace_write_maps_to_each_provider_sandbox(self):
        payload = InvocationPayload("deliver", access="workspace-write")
        claude = claude_command(
            model="a", effort="medium", payload=payload, budget_units=1)
        codex = codex_command(model="o", effort="medium", payload=payload)
        self.assertEqual("acceptEdits", claude[claude.index("--permission-mode") + 1])
        self.assertEqual("workspace-write", codex[codex.index("--sandbox") + 1])

    def test_command_syntax_is_confined_and_model_effort_are_explicit(self):
        self.assertEqual("claude", claude_command(
            model="fable", effort="medium", payload=InvocationPayload("p"),
            budget_units=2)[0])
        codex = codex_command(model="terra", effort="medium",
                              payload=InvocationPayload("p"))
        self.assertEqual("codex", codex[0])
        self.assertIn("terra", codex)
        self.assertIn('model_reasoning_effort="medium"', codex)

    def test_capability_specific_tool_and_network_bounds_are_adapter_data(self):
        payload = InvocationPayload(
            "ack", access="workspace-write", network_access=True,
            skip_git_repo_check=True,
            allowed_tools=("Bash(gh issue comment:*)",),
            disallowed_tools=("Write", "Edit"))
        claude = claude_command(
            model="economy", effort="low", payload=payload, budget_units=1)
        codex = codex_command(model="economy", effort="low", payload=payload)
        self.assertEqual("Bash(gh issue comment:*)",
                         claude[claude.index("--allowedTools") + 1])
        self.assertEqual("Write,Edit",
                         claude[claude.index("--disallowedTools") + 1])
        self.assertIn("sandbox_workspace_write.network_access=true", codex)
        self.assertIn("--skip-git-repo-check", codex)

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

    def test_provider_environments_do_not_share_credentials(self):
        source = {"PATH": "/bin", "HOME": "/operator", "OPENAI_API_KEY": "openai",
                  "ANTHROPIC_API_KEY": "anthropic", "GITHUB_TOKEN": "github"}
        openai = provider_environment("openai", source)
        anthropic = provider_environment("anthropic", source)
        self.assertEqual({"PATH", "OPENAI_API_KEY"}, set(openai))
        self.assertEqual({"PATH", "HOME", "ANTHROPIC_API_KEY"}, set(anthropic))
        self.assertNotIn("GITHUB_TOKEN", openai | anthropic)


if __name__ == "__main__":
    unittest.main()

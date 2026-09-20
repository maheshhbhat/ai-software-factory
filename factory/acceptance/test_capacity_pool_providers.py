#!/usr/bin/env python3
import os
import subprocess
import unittest
from pathlib import Path

from factory.capacity_pool.providers.cli import (
    InvocationPayload, claude_command, cli_adapter, codex_command, muse_command,
    provider_environment, reported_usage,
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
        self.assertTrue(result.process_started)

    def test_provider_usage_and_exact_cost_are_read_without_pricing(self):
        stream = ('{"usage":{"input_tokens":12,"output_tokens":3}}\n'
                  '{"total_cost_usd":0.42}\n')
        usage, cost = reported_usage(stream)
        self.assertEqual({"input_tokens": 12, "output_tokens": 3}, usage)
        self.assertEqual(.42, cost)

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stream, "")
        result = cli_adapter(
            "anthropic", cwd=Path("."), environment={}, runner=runner).run(
                model="balanced", effort="medium", timeout_seconds=5,
                budget_units=1, payload="p")
        self.assertEqual(.42, result.exact_cost_usd)
        self.assertIsNone(result.dollar_cost_unavailable_reason)
        self.assertEqual({"input_tokens": 12, "output_tokens": 3}, result.usage)

    def test_missing_executable_is_never_claimed_as_started(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError("missing")
        result = cli_adapter(
            "openai", cwd=Path("."), environment={}, runner=runner).run(
                model="terra", effort="medium", timeout_seconds=5,
                budget_units=1, payload="p")
        self.assertFalse(result.process_started)
        self.assertEqual("provider-did-not-report-exact-cost",
                         result.dollar_cost_unavailable_reason)

    def test_provider_environments_do_not_share_credentials(self):
        source = {"PATH": "/bin", "HOME": "/operator", "OPENAI_API_KEY": "openai",
                  "ANTHROPIC_API_KEY": "anthropic", "GITHUB_TOKEN": "github"}
        openai = provider_environment("openai", source)
        anthropic = provider_environment("anthropic", source)
        self.assertEqual({"PATH", "HOME", "OPENAI_API_KEY"}, set(openai))
        self.assertEqual({"PATH", "HOME", "ANTHROPIC_API_KEY"}, set(anthropic))
        self.assertNotIn("GITHUB_TOKEN", openai | anthropic)

    def test_prompt_text_is_never_a_command_line_argument(self):
        """Story #674: a large prompt as an argv element can exceed the OS
        argument-list limit. No constructed command may contain it."""
        secret = "UNIQUE_LARGE_PROMPT_MARKER_" + ("x" * 5000)
        payload = InvocationPayload(secret)
        claude = claude_command(model="fable", effort="medium", payload=payload,
                                budget_units=1)
        codex = codex_command(model="terra", effort="medium", payload=payload)
        muse = muse_command(model="spark", effort="medium", payload=payload,
                            prompt_path=Path("/tmp/whatever.prompt.txt"))
        self.assertNotIn(secret, claude)
        self.assertNotIn(secret, codex)
        self.assertNotIn(secret, muse)
        self.assertIn("-", codex)
        self.assertIn("--prompt-file", muse)

    def test_claude_and_codex_receive_the_prompt_via_stdin(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(command, 0, "", "")

        for provider in ("anthropic", "openai"):
            with self.subTest(provider=provider):
                cli_adapter(provider, cwd=Path("."), environment={}, runner=runner).run(
                    model="m", effort="medium", timeout_seconds=5,
                    budget_units=1, payload="the actual prompt text")
                self.assertEqual("the actual prompt text", captured["input"])
                self.assertNotIn("the actual prompt text", captured["command"])

    def test_muse_receives_the_prompt_via_a_cleaned_up_temp_file(self):
        seen = {}

        def runner(command, **kwargs):
            path = Path(command[command.index("--prompt-file") + 1])
            seen["path"] = path
            seen["content"] = path.read_text(encoding="utf-8")
            self.assertIsNone(kwargs.get("input"))
            return subprocess.CompletedProcess(command, 0, "", "")

        cli_adapter("meta", cwd=Path("."), environment={}, runner=runner).run(
            model="spark", effort="medium", timeout_seconds=5,
            budget_units=1, payload="meta prompt text")
        self.assertEqual("meta prompt text", seen["content"])
        self.assertFalse(seen["path"].exists(),
                         "prompt temp file must be removed after the call returns")

    def test_muse_temp_file_is_cleaned_up_after_a_timeout(self):
        seen = {}

        def runner(command, **kwargs):
            seen["path"] = Path(command[command.index("--prompt-file") + 1])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = cli_adapter("meta", cwd=Path("."), environment={}, runner=runner).run(
            model="spark", effort="medium", timeout_seconds=5,
            budget_units=1, payload="meta prompt text")
        self.assertEqual("timeout", result.outcome)
        self.assertFalse(seen["path"].exists(),
                         "prompt temp file must be removed even when the runner times out")

    def test_large_payload_fails_as_argv_but_succeeds_via_stdin_on_a_real_process(self):
        """Story #674's actual defect, reproduced and fixed against a real OS
        process — not a mock. Sized from the live ARG_MAX so this is
        correct on whatever machine runs it, not a hardcoded guess."""
        arg_max = os.sysconf("SC_ARG_MAX")
        big = "x" * (arg_max + 200_000)
        with self.assertRaises(OSError):
            subprocess.run(["/bin/echo", big], capture_output=True, timeout=30)
        result = subprocess.run(["/bin/cat"], input=big, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(0, result.returncode)
        self.assertEqual(big, result.stdout)


if __name__ == "__main__":
    unittest.main()

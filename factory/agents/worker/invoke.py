#!/usr/bin/env python3
"""Headless bounded delivery: claimed Story identity in, one durable PR out.

Capacity Pool owns model/provider choice, credentials, command construction,
health, leases, and fallback. This wrapper owns the product mutation boundary:
it supplies the Story prompt, validates changed paths, runs product tests, and
writes the linked pull request only after those checks pass.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "factory" / "gates"))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import merge_gate  # noqa: E402
import observability as obs  # noqa: E402
import runlog  # noqa: E402
import correction_context  # noqa: E402
from factory.agents.planning import contract as planning_contract  # noqa: E402
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool import admission as capacity_admission  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import (  # noqa: E402
    InvocationPayload, cli_adapter, provider_environment,
)
from factory.capacity_pool.state import CapacityState, default_state_path  # noqa: E402
from factory.runtime import operating_envelope  # noqa: E402

DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_USD = 40.0
MARKER = "worker-artifact"
SECTION = r"^### {name}\s*$\n(.*?)(?=^### |\Z)"
STORY_LINK = re.compile(r"(?m)^Story: #(\d+)$")
START_MARKER = "<!-- factory-worker-start:v1 -->"
FAILURE_MARKER = "FACTORY_WORKER_FAILURE_V1="
RECOVERY_SCHEMA_VERSION = 2
RECOVERY_TRUST = "untrusted-partial-work-from-failed-worker"
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def valid_commit(value) -> bool:
    return isinstance(value, str) and FULL_COMMIT.fullmatch(value) is not None


def valid_utc_timestamp(value) -> bool:
    """Return whether value is a full ISO datetime with canonical UTC suffix."""
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() == timezone.utc.utcoffset(None)


def valid_recovery_worker(value, *, repo: str, story_number: int) -> bool:
    task_prefix = f"delivery:{repo.lower()}:{story_number}:"
    return (isinstance(value, dict) and
            isinstance(value.get("task"), str) and
            value["task"].startswith(task_prefix) and
            len(value["task"]) > len(task_prefix) and
            not any(key in value and
                    (not isinstance(value[key], str) or not value[key])
                    for key in ("invocation_id", "reservation_id",
                                "provider", "model")))


class DeliveryError(RuntimeError):
    """A bounded delivery could not be completed.

    `output` carries whatever the failed subprocess printed. An engine that
    crashed or ran out of time still spent what it spent before it did, and
    that report is the only place those tokens are ever counted.
    """

    def __init__(self, message: str, output: str = "", *,
                 mutation_state: str = "none", terminal_outcome: str = ""):
        super().__init__(message)
        self.output = output
        self.mutation_state = mutation_state
        self.terminal_outcome = terminal_outcome
        self.recovery_ref = ""


class RecoveryError(DeliveryError):
    """Recovery evidence is invalid and must remain untouched for diagnosis."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message, output, mutation_state="post-mutation",
                         terminal_outcome="recovery-invalid")


def platform_diagnostics(checkout: pathlib.Path) -> dict:
    """Return non-sensitive facts that explain local filesystem failures.

    ``os.access`` reports permission-bit access.  When it says writable but the
    real Git operation returns ``EPERM``, the useful conclusion is that an
    external policy (for example a process sandbox) denied the operation.
    """
    checkout = checkout.absolute()
    git_dir = checkout / ".git"
    resolution_error = None
    try:
        if git_dir.is_file():
            marker = git_dir.read_text(errors="replace").strip()
            if marker.startswith("gitdir:"):
                candidate = pathlib.Path(marker.removeprefix("gitdir:").strip())
                git_dir = candidate if candidate.is_absolute() else checkout / candidate
        git_dir = git_dir.resolve()
    except OSError as exc:
        resolution_error = f"{type(exc).__name__}: {exc}"

    def describe(path: pathlib.Path) -> dict:
        try:
            metadata = path.stat()
        except OSError as exc:
            return {"path": str(path), "stat_error": f"{type(exc).__name__}: {exc}"}
        return {
            "path": str(path),
            "mode": oct(stat.S_IMODE(metadata.st_mode)),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "writable_by_access_check": os.access(path, os.W_OK),
        }

    result = {
        "platform": sys.platform,
        "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "effective_gid": os.getegid() if hasattr(os, "getegid") else None,
        "git_dir": describe(git_dir),
        "git_worktrees_dir": describe(git_dir / "worktrees"),
        "sandbox_marker_names": sorted(
            name for name in os.environ
            if "SANDBOX" in name.upper() or name.upper().startswith("CODEX_")),
    }
    if resolution_error:
        result["git_dir_resolution_error"] = resolution_error
    return result


# Not credentials: tools, locale, and scratch space for product tests.
BASE_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL")


@dataclass(frozen=True)
class Bounds:
    max_usd: float
    timeout: int


@dataclass(frozen=True)
class Delivery:
    story: int
    project: int
    branch: str
    pull_request: int
    head: str
    replay: bool


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def api(self, path: str, *, method: str = "GET", value=None):
        data = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "factory-delivery-worker"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise DeliveryError(
                    f"repository access constraint failed: GitHub returned {exc.code}") from exc
            raise

    def issue(self, number: int):
        return self.api(f"/issues/{number}")

    def pages(self, path: str):
        result, page = [], 1
        while True:
            join = "&" if "?" in path else "?"
            batch = self.api(f"{path}{join}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise DeliveryError(f"GitHub returned malformed collection for {path}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def pull_requests(self):
        return self.pages("/pulls?state=all")

    def create_pr(self, title: str, head: str, base: str, body: str):
        return self.api("/pulls", method="POST",
                        value={"title": title, "head": head, "base": base,
                               "body": body, "draft": False})

    def update_pr(self, number: int, body: str):
        return self.api(f"/pulls/{number}", method="PATCH", value={"body": body})


def section(body: str, name: str) -> str:
    match = re.search(SECTION.format(name=re.escape(name)), body or "",
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise DeliveryError(f"Story is missing ### {name}")
    return match.group(1).strip()


def acceptance_verification_commands(body: str) -> list[tuple[str, list[str]]]:
    """Read automated acceptance commands from the canonical Story section."""
    commands = []
    match = re.search(SECTION.format(name=re.escape("Acceptance notes")), body or "",
                      re.MULTILINE | re.DOTALL)
    if not match:
        return commands  # pre-2.4 Story fixture or stored historical Story
    for line in match.group(1).strip().splitlines():
        if not line.startswith("- "):
            raise DeliveryError("Acceptance notes must contain one criterion per bullet")
        criterion = line[2:]
        if criterion.count(planning_contract.VERIFICATION_MARKER) != 1:
            # Pre-2.4 Stories are deliberately readable and have no records.
            continue
        _, encoded = criterion.split(planning_contract.VERIFICATION_MARKER, 1)
        try:
            record = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise DeliveryError("Story acceptance verification is not valid JSON") from error
        if not isinstance(record, dict):
            raise DeliveryError("Story acceptance verification must be an object")
        if record.get("type") == "human-bell":
            continue
        if (record.get("type") != "automated"
                or set(record) != planning_contract.AUTOMATED_VERIFICATION_FIELDS
                or not all(isinstance(record.get(field), str) and record[field].strip()
                           for field in planning_contract.AUTOMATED_VERIFICATION_FIELDS)):
            raise DeliveryError("Story automated acceptance verification is malformed")
        try:
            command = planning_contract.automated_verification_command(record)
        except planning_contract.ContractError as error:
            raise DeliveryError(str(error)) from error
        commands.append((record["executor"], command))
    return commands


def run_acceptance_verifications(body: str, *, cwd: pathlib.Path, timeout: int,
                                 trace_id: str, repo: str, story: int, project: int,
                                 runner=subprocess.run) -> None:
    """Execute every declared automated acceptance check in the trusted wrapper."""
    for executor, command in acceptance_verification_commands(body):
        with obs.Activity("delivery-worker", "acceptance-verification", "running",
                          trace_id=trace_id, repo=repo, story=story, project=project,
                          executor=executor):
            run(command, cwd=cwd, timeout=timeout,
                env=tool_environment(), runner=runner)


def labels(issue: dict) -> set[str]:
    return {item.get("name", "") if isinstance(item, dict) else str(item)
            for item in issue.get("labels", [])}


def parse_bounds(body: str) -> Bounds:
    raw = section(body, "Spend cap")
    money = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", raw)
    minutes = re.search(r"([0-9]+)\s*min", raw, re.I)
    if not money or not minutes:
        raise DeliveryError("Spend cap must contain `$N / N min`")
    value = Bounds(float(money.group(1)), int(minutes.group(1)) * 60)
    if value.max_usd <= 0 or value.timeout <= 0:
        raise DeliveryError("Spend cap bounds must be positive")
    return value


def capacity_state_path(environ=None) -> pathlib.Path:
    return default_state_path(ROOT, environ)


def state_version(events: list[dict]) -> str:
    claims = [item for item in events if item.get("event") == "labeled" and
              (item.get("label") or {}).get("name") == "story:claimed"]
    if not claims:
        raise DeliveryError("Story has no durable claimed state version")
    latest = claims[-1]
    return str(latest.get("id") or latest.get("created_at"))


def marker(story: int, version: str) -> str:
    return f"<!-- {MARKER}:{story}:{version} -->"


def worker_start_body(*, repo: str, story: int, version: str,
                      reservation: str, invocation: str) -> str:
    value = {"schema_version": 1, "repo": repo.lower(), "story": story,
             "state_version": version, "reservation_id": reservation,
             "invocation_id": invocation,
             "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return START_MARKER + "\n\n```json\n" + json.dumps(
        value, sort_keys=True) + "\n```"


def publish_worker_start(client: GitHub, *, repo: str, story: int, version: str,
                         reservation: str, invocation: str) -> None:
    body = worker_start_body(repo=repo, story=story, version=version,
                             reservation=reservation, invocation=invocation)
    created = client.api(
        f"/issues/{story}/comments", method="POST", value={"body": body})
    if not isinstance(created, dict) or created.get("body") != body:
        raise DeliveryError("durable worker-start write was not acknowledged")


def linked_prs(story: int, pulls: list[dict]) -> list[dict]:
    found = []
    for pull in pulls:
        matches = STORY_LINK.findall((pull.get("body") or "").replace("\r\n", "\n"))
        if matches.count(str(story)) > 1:
            raise DeliveryError(f"PR #{pull.get('number')} duplicates Story: #{story}")
        if str(story) in matches:
            found.append(pull)
    if len(found) > 1:
        raise DeliveryError(f"multiple pull requests link Story: #{story}")
    return found


def tool_environment(environ=None) -> dict[str, str]:
    """Tools, locale and scratch space — what a non-engine subprocess gets.

    No credential, on any platform, for any engine declared later. The
    delivered change's own test command runs under this.
    """
    environ = os.environ if environ is None else environ
    return {key: value for key, value in environ.items()
            if key in BASE_ENVIRONMENT}


def printed(value) -> str:
    """Whatever a subprocess wrote, decoded for us or not.

    `TimeoutExpired` hands back bytes when the process died mid-stream; a
    completed process hands back text.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def run(cmd: list[str], *, cwd: pathlib.Path, timeout: int, env=None,
        runner=subprocess.run) -> subprocess.CompletedProcess:
    try:
        result = runner(cmd, cwd=str(cwd), capture_output=True, text=True,
                        timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        # Keep both streams. A timeout's stderr was dropped entirely, and for
        # an engine killed mid-explanation that was the half worth reading.
        detail = f"bounded execution exhausted after {timeout}s"
        stderr_excerpt = runlog.diagnostic_excerpt(printed(exc.stderr))
        if stderr_excerpt:
            detail += f"; stderr diagnostic: {stderr_excerpt}"
        raise DeliveryError(detail, printed(exc.stdout)) from exc
    if result.returncode:
        # The engine's own explanation lives in these streams. A 300-character
        # slice of stderr lost it twice: the 2026-08-22 authentication refusal
        # ("Please run /login") was printed to stdout and discarded, and the
        # 2026-08-23 failures of #332 recorded `command failed (1):` with
        # nothing after the colon. runlog.tail keeps 2000 characters and
        # redacts credentials — both properties wanted here.
        raise DeliveryError(
            f"command failed ({result.returncode})"
            f"; stderr diagnostic: {runlog.diagnostic_excerpt(result.stderr)}"
            f"; stdout diagnostic: {runlog.diagnostic_excerpt(result.stdout)}",
            printed(result.stdout))
    return result


def git(args: list[str], cwd: pathlib.Path, *, timeout=300, runner=subprocess.run):
    return run(["git", *args], cwd=cwd, timeout=timeout, runner=runner)


def _remote_repo(url: str) -> str:
    value = url.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    marker = "github.com/"
    return value.split(marker, 1)[1] if marker in value else ""


@contextlib.contextmanager
def checkout_for_repo(repo: str, configured: pathlib.Path,
                      runner=subprocess.run):
    """Use the configured checkout only when its origin is the requested repo."""
    try:
        origin = git(["remote", "get-url", "origin"], configured,
                     runner=runner).stdout.strip()
    except DeliveryError:
        origin = ""
    if _remote_repo(origin).lower() == repo.lower():
        yield configured
        return
    with tempfile.TemporaryDirectory(prefix="factory-product-checkout-") as temp:
        checkout = pathlib.Path(temp) / "repository"
        run(["gh", "repo", "clone", repo, str(checkout), "--", "--quiet"],
            cwd=pathlib.Path(temp), timeout=300, runner=runner)
        yield checkout


def changed_paths(worktree: pathlib.Path, base: str,
                  runner=subprocess.run) -> list[str]:
    tracked_items = [item for item in git(
        ["diff", "--name-status", "-z", f"{base}...HEAD"], worktree,
        runner=runner).stdout.split("\0") if item]
    tracked = []
    index = 0
    while index < len(tracked_items):
        status = tracked_items[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(tracked_items):
            raise DeliveryError("Git returned incomplete committed path status")
        tracked.extend(tracked_items[index:index + path_count])
        index += path_count
    # --untracked-files=all: plain --porcelain collapses a brand-new directory
    # to one `dir/` line, which no `dir/sub/**` scope can match — the worker
    # then refuses its own in-scope work (#358, found live by fixture #357).
    pending = [item for item in git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], worktree,
        runner=runner).stdout.split("\0") if item]
    paths = list(tracked)
    index = 0
    while index < len(pending):
        entry = pending[index]
        if len(entry) <= 3:
            raise DeliveryError("Git returned malformed machine-readable status")
        status = entry[:2]
        paths.append(entry[3:])
        if "R" in status or "C" in status:
            index += 1
            if index >= len(pending) or not pending[index]:
                raise DeliveryError("Git returned incomplete rename status")
            paths.append(pending[index])
        index += 1
    return sorted(set(path for path in paths if path))


def repository_test_command(worktree: pathlib.Path) -> list[str]:
    """Resolve validation from the product checkout, never from the factory."""
    override = os.environ.get("FACTORY_DELIVERY_TEST_CMD", "").strip()
    if override:
        return shlex.split(override)
    package = worktree / "package.json"
    if package.is_file():
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryError(f"invalid product package.json: {exc}") from exc
        if ((manifest.get("scripts") or {}).get("test") or "").strip():
            return ["npm", "test"]
    factory_tests = worktree / "factory" / "agents" / "worker" / "test_repo.sh"
    if factory_tests.is_file():
        return [str(factory_tests)]
    raise DeliveryError("repository declares no supported test command")


def build_input(client: GitHub, story: dict, project: dict, *, repo: str,
                pull_request: dict | None = None) -> dict:
    story_comments = client.pages(f"/issues/{story['number']}/comments")
    pull_comments = (client.pages(f"/issues/{pull_request['number']}/comments")
                     if pull_request is not None else [])
    decisions = [item for item in client.pages("/issues?state=all")
                 if "type:adr" in labels(item)]
    obligations = operating_envelope.obligations(
        story.get("body") or "", project.get("body") or "")
    packet = correction_context.assemble(
        repository=repo, project=project["number"], story=story,
        pull_request=pull_request,
        story_comments=story_comments, pull_comments=pull_comments)
    return {"story": story, "project": project,
            "operating_envelope_obligations": obligations, "adrs": decisions,
            "correction_context": packet}


def read_back_pr(client: GitHub, story_number: int, durable: str,
                 *, attempts: int = 3) -> dict:
    """Confirm the canonical artifact through GitHub, tolerating brief lag."""
    for attempt in range(attempts):
        fresh = linked_prs(story_number, client.pull_requests())
        if len(fresh) == 1 and durable in (fresh[0].get("body") or ""):
            return fresh[0]
        if attempt + 1 < attempts:
            time.sleep(1)
    raise DeliveryError("durable PR read-back failed")


def protected_control_scope(repo: str, scope: list[str]) -> list[str]:
    """Return protected paths only when delivery targets the factory itself."""
    if repo.rsplit("/", 1)[-1] != "ai-software-factory":
        return []
    return merge_gate.protected_factory_scope(scope)


def recovery_patch(repo: str, story_number: int, environ=None) -> pathlib.Path:
    """Stable host-local checkpoint used only after a worker changed files."""
    environ = os.environ if environ is None else environ
    root = pathlib.Path(environ.get(
        "FACTORY_RECOVERY_DIR", str(ROOT / ".runtime" / "worker-recovery")))
    return root / repo.replace("/", "--") / f"story-{story_number}.patch"


def recovery_manifest(patch: pathlib.Path) -> pathlib.Path:
    return patch.with_suffix(".json")


def recovery_patch_paths(patch: pathlib.Path, *, runner=subprocess.run) -> list[str]:
    """Read the path set Git records in a checkpoint without applying it."""
    result = runner(
        ["git", "apply", "--numstat", "-z", str(patch)], cwd=patch.parent,
        capture_output=True, text=True)
    if result.returncode:
        raise RecoveryError("recovery checkpoint patch paths are invalid")
    paths = []
    for record in result.stdout.split("\0"):
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise RecoveryError("recovery checkpoint patch paths are invalid")
        paths.append(fields[2])
    patch_text = patch.read_text(encoding="utf-8")
    for line in patch_text.splitlines():
        if line.startswith(("rename from ", "rename to ",
                            "copy from ", "copy to ")):
            encoded = line.split(" ", 2)[2]
            try:
                if encoded.startswith('"'):
                    # Git's quoted paths are byte strings: octal escapes such
                    # as \303\251 are the UTF-8 bytes for "é", not Latin-1
                    # characters. Parse the C-style quoting into bytes first.
                    quoted = ast.literal_eval(encoded)
                    if not isinstance(quoted, str):
                        raise ValueError("quoted Git path is not text")
                    decoded = quoted.encode("latin-1").decode("utf-8")
                else:
                    decoded = encoded
            except (SyntaxError, ValueError, UnicodeDecodeError,
                    UnicodeEncodeError) as exc:
                raise RecoveryError(
                    "recovery checkpoint patch paths are invalid") from exc
            if not isinstance(decoded, str) or not decoded:
                raise RecoveryError("recovery checkpoint patch paths are invalid")
            paths.append(decoded)
    if not paths:
        raise RecoveryError("recovery checkpoint patch paths are invalid")
    return sorted(set(paths))


def validate_pushed_recovery(value: dict, patch_text: str | None, *,
                             repo: str | None, story_number: int | None,
                             scope: list[str] | None,
                             patch_paths: list[str] | None = None) -> str:
    """Validate all provenance needed before durable checkpoint deletion."""
    expected_repo = repo if repo is not None else value.get("repository")
    expected_story = (story_number if story_number is not None
                      else value.get("story"))
    expected = {"schema_version": RECOVERY_SCHEMA_VERSION,
                "trust": RECOVERY_TRUST, "repository": expected_repo,
                "story": expected_story}
    if (not isinstance(expected_repo, str) or not expected_repo or
            not isinstance(expected_story, int) or expected_story <= 0 or
            any(value.get(key) != item for key, item in expected.items())):
        raise RecoveryError("pushed recovery checkpoint identity is invalid")
    head = value.get("delivered_head")
    paths = value.get("recovered_paths")
    if (not valid_commit(value.get("base_commit")) or
            not valid_commit(head) or
            not isinstance(paths, list) or not paths or
            any(not isinstance(path, str) or not path for path in paths) or
            (patch_paths is not None and sorted(set(paths)) != patch_paths) or
            (scope is not None and merge_gate.paths_out_of_scope(paths, scope)) or
            (patch_text is not None and
             value.get("patch_sha256") != hashlib.sha256(
                 patch_text.encode("utf-8")).hexdigest()) or
            any(not isinstance(value.get(key), str) or not value[key]
                for key in ("previous_mutation_state",
                            "previous_terminal_outcome"))):
        raise RecoveryError("pushed recovery checkpoint provenance is invalid")
    if not valid_recovery_worker(
            value.get("originating_worker"), repo=expected_repo,
            story_number=expected_story):
        raise RecoveryError("pushed recovery worker identity is invalid")
    for key in ("recovered_at", "delivery_verified_at"):
        if not valid_utc_timestamp(value.get(key)):
            raise RecoveryError(f"pushed recovery {key} is invalid")
    return head


def recovery_available(patch: pathlib.Path, *, repo: str | None = None,
                       story_number: int | None = None,
                       scope: list[str] | None = None,
                       runner=subprocess.run) -> bool:
    manifest = recovery_manifest(patch)
    patch_tombstone = patch.with_name(patch.name + ".deleting")
    manifest_tombstone = manifest.with_name(manifest.name + ".deleting")
    if patch_tombstone.exists() or manifest_tombstone.exists():
        # Cleanup begins only after the recovered delivery is durable. Finish
        # any interrupted transaction instead of treating its remainder as a
        # new invalid recovery checkpoint.
        patch_source = patch if patch.is_file() else patch_tombstone
        manifest_source = manifest if manifest.is_file() else manifest_tombstone
        try:
            value = json.loads(manifest_source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError("recovery cleanup transaction is invalid") from exc
        if not isinstance(value, dict):
            raise RecoveryError("recovery cleanup transaction is invalid")
        if not patch_source.is_file():
            if manifest_source == manifest_tombstone:
                validate_pushed_recovery(
                    value, None, repo=repo, story_number=story_number,
                    scope=scope)
                manifest_tombstone.unlink(missing_ok=True)
                return False
            raise RecoveryError("recovery cleanup transaction is invalid")
        patch_text = patch_source.read_text(encoding="utf-8")
        validate_pushed_recovery(
            value, patch_text, repo=repo, story_number=story_number, scope=scope,
            patch_paths=recovery_patch_paths(patch_source, runner=runner))
        for item in (patch, manifest, patch_tombstone, manifest_tombstone):
            item.unlink(missing_ok=True)
        return False
    if patch.is_file() != manifest.is_file():
        raise RecoveryError("recovery checkpoint patch/manifest pair is incomplete")
    if not patch.is_file():
        return False
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        patch_text = patch.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        raise RecoveryError("recovery checkpoint metadata is unavailable")
    if not isinstance(value, dict):
        raise RecoveryError("recovery checkpoint metadata is not an object")
    if value.get("patch_sha256") != hashlib.sha256(
            patch_text.encode("utf-8")).hexdigest():
        raise RecoveryError("recovery checkpoint digest does not match its manifest")
    return True


def _write_recovery_file(target: pathlib.Path, value: str) -> None:
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(target)


def remove_recovery(patch: pathlib.Path) -> None:
    """Remove the complete checkpoint pair after successful delivery only."""
    manifest = recovery_manifest(patch)
    patch_tombstone = patch.with_name(patch.name + ".deleting")
    manifest_tombstone = manifest.with_name(manifest.name + ".deleting")
    patch.replace(patch_tombstone)
    try:
        manifest.replace(manifest_tombstone)
    except Exception:
        patch_tombstone.replace(patch)
        raise
    patch_tombstone.unlink(missing_ok=True)
    manifest_tombstone.unlink(missing_ok=True)


def mark_recovery_pushed(patch: pathlib.Path, head: str) -> None:
    """Record that verified recovered work is durable on an exact branch head."""
    if not valid_commit(head):
        raise RecoveryError("pushed recovery head is invalid")
    try:
        manifest = recovery_manifest(patch)
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery checkpoint metadata is unavailable") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery checkpoint metadata is not an object")
    value.pop("pending_head", None)
    value.pop("push_prepared_at", None)
    value["delivered_head"] = head
    value["delivery_verified_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    _write_recovery_file(
        manifest, json.dumps(value, indent=2, sort_keys=True) + "\n")


def mark_recovery_push_pending(patch: pathlib.Path, head: str) -> None:
    """Durably bind the checkpoint to the commit before remote mutation."""
    if not valid_commit(head):
        raise RecoveryError("pending recovery head is invalid")
    try:
        manifest = recovery_manifest(patch)
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery checkpoint metadata is unavailable") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery checkpoint metadata is not an object")
    value["pending_head"] = head
    value["push_prepared_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    _write_recovery_file(
        manifest, json.dumps(value, indent=2, sort_keys=True) + "\n")


def reconcile_recovery_push(patch: pathlib.Path, observed_head: str, *,
                            repo: str, story_number: int) -> None:
    """Promote a pre-push marker when the remote proves the push landed."""
    try:
        value = json.loads(
            recovery_manifest(patch).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery checkpoint metadata is unavailable") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery checkpoint metadata is not an object")
    pending = value.get("pending_head")
    if pending is None:
        return
    expected = {"schema_version": RECOVERY_SCHEMA_VERSION,
                "trust": RECOVERY_TRUST, "repository": repo,
                "story": story_number}
    prepared = value.get("push_prepared_at")
    try:
        if (any(value.get(key) != item for key, item in expected.items()) or
                not valid_commit(pending) or
                not valid_utc_timestamp(prepared)):
            raise ValueError("invalid pending recovery provenance")
    except (AttributeError, TypeError, ValueError) as exc:
        raise RecoveryError("pending recovery checkpoint is invalid") from exc
    if pending != observed_head:
        return
    mark_recovery_pushed(patch, pending)


def pushed_recovery_head(patch: pathlib.Path, *, repo: str, story_number: int,
                         scope: list[str] | None = None,
                         runner=subprocess.run) -> str | None:
    """Return a validated durable head, if this checkpoint was already pushed."""
    try:
        value = json.loads(recovery_manifest(patch).read_text(encoding="utf-8"))
        patch_text = patch.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery checkpoint metadata is unavailable") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery checkpoint metadata is not an object")
    head = value.get("delivered_head")
    if head is None:
        return None
    return validate_pushed_recovery(
        value, patch_text, repo=repo, story_number=story_number, scope=scope,
        patch_paths=recovery_patch_paths(patch, runner=runner))


def recovered_work_state(worktree: pathlib.Path, paths: list[str], *,
                         base_commit: str, runner=subprocess.run) -> str:
    """Digest the exact recovered file state without trusting Git metadata."""
    digest = hashlib.sha256()
    staged = git(["diff", "--binary", "--cached", base_commit, "--", *paths],
                 worktree, runner=runner).stdout
    digest.update(staged.encode("utf-8") + b"\0")
    for relative in sorted(paths):
        path = worktree / relative
        digest.update(relative.encode("utf-8") + b"\0")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(b"missing\0")
            continue
        digest.update(f"{stat.S_IFMT(metadata.st_mode)}:{stat.S_IMODE(metadata.st_mode)}"
                      .encode("ascii") + b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8") + b"\0")
        elif path.is_file():
            digest.update(path.read_bytes() + b"\0")
        else:
            # Git submodules are directories in the worktree but gitlinks in
            # the index. The staged patch above is their authoritative state.
            digest.update(b"directory-or-gitlink\0")
    return digest.hexdigest()


def _gitlinks(output: str) -> set[str]:
    result = set()
    for line in output.splitlines():
        metadata, separator, relative = line.partition("\t")
        fields = metadata.split()
        if separator and fields and fields[0] == "160000":
            result.add(relative)
    return result


def checkout_recovered_gitlinks(worktree: pathlib.Path, paths: list[str], *,
                                base_commit: str,
                                runner=subprocess.run) -> None:
    """Make each staged gitlink's checked-out directory match the index."""
    prior = _gitlinks(git(
        ["ls-tree", base_commit, "--", *paths], worktree,
        runner=runner).stdout)
    staged = git(
        ["ls-files", "--stage", "--", *paths], worktree,
        runner=runner).stdout
    gitlinks = _gitlinks(staged)
    staged_paths = {
        line.partition("\t")[2] for line in staged.splitlines()
        if line.partition("\t")[1]
    }
    root = worktree.resolve()
    for relative in sorted(prior - gitlinks):
        if any(path == relative or path.startswith(relative + "/")
               for path in staged_paths):
            # Removing the old checkout now would also remove regular files
            # created by the patch. Preserve them and the checkpoint instead.
            raise RecoveryError(
                "recovered gitlink replacement requires explicit cleanup")
        target = worktree / relative
        if not target.resolve().is_relative_to(root):
            raise RecoveryError("deleted recovery gitlink escapes worktree")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    if gitlinks:
        # Tests and the worker must inspect the dependency revision that will
        # actually be committed, not an older submodule checkout.
        git(["submodule", "update", "--init", "--checkout", "--",
             *sorted(gitlinks)],
            worktree, runner=runner)


def mark_finalization_failure(exc: Exception, patch: pathlib.Path) -> None:
    """Keep a pushed recovery retry charged until its PR is durable."""
    setattr(exc, "mutation_state", "post-mutation")
    if not getattr(exc, "terminal_outcome", ""):
        setattr(exc, "terminal_outcome", "delivery-finalization-failed")
    setattr(exc, "recovery_ref", str(patch))


def checkpoint_failed_work(worktree: pathlib.Path, checkout: pathlib.Path, *,
                           repo: str, story_number: int, default: str,
                           base_ref: str, base_commit: str,
                           scope: list[str], mutation_state: str,
                           terminal_outcome: str,
                           originating_worker: dict | None = None,
                           runner=subprocess.run) -> str:
    """Preserve authorized in-scope edits before removing the temp worktree.

    The checkpoint is host-local and unreviewed. It is never pushed or merged.
    A later launch for the same Story applies it, then runs the normal scope,
    test, commit, push, and review path.
    """
    paths = changed_paths(worktree, base_ref, runner=runner)
    if not paths or merge_gate.paths_out_of_scope(paths, scope):
        return ""
    if not valid_commit(base_commit):
        raise DeliveryError("recovery checkpoint base commit is invalid")
    git(["add", "--", *paths], worktree, runner=runner)
    patch = git(["diff", "--binary", "--cached", base_commit],
                worktree, runner=runner).stdout
    if not patch:
        return ""
    target = recovery_patch(repo, story_number)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "trust": RECOVERY_TRUST,
        "repository": repo,
        "story": story_number,
        "base_commit": base_commit,
        "recovered_paths": paths,
        "previous_mutation_state": mutation_state,
        "previous_terminal_outcome": terminal_outcome,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "originating_worker": dict(originating_worker or {}),
        "recovered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _write_recovery_file(target, patch)
    _write_recovery_file(
        recovery_manifest(target),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return str(target)


def restore_failed_work(patch: pathlib.Path, worktree: pathlib.Path, *,
                        repo: str, story_number: int, base_commit: str,
                        scope: list[str],
                        runner=subprocess.run) -> dict:
    """Validate, apply, and describe a failed worker's untrusted checkpoint."""
    manifest_path = recovery_manifest(patch)
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        patch_text = patch.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"recovery checkpoint metadata is unavailable: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError("recovery checkpoint metadata is not an object")
    paths = value.get("recovered_paths")
    expected = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "trust": RECOVERY_TRUST,
        "repository": repo,
        "story": story_number,
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise RecoveryError("recovery checkpoint identity is invalid")
    recorded_base = value.get("base_commit")
    if not valid_commit(recorded_base):
        raise RecoveryError("recovery checkpoint base commit is invalid")
    if (not isinstance(paths, list) or not paths or
            any(not isinstance(path, str) or not path for path in paths)):
        raise RecoveryError("recovery checkpoint paths are invalid")
    if merge_gate.paths_out_of_scope(paths, scope):
        raise RecoveryError("recovery checkpoint paths exceed Story scope")
    digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    if value.get("patch_sha256") != digest:
        raise RecoveryError("recovery checkpoint digest does not match its manifest")
    for key in ("previous_mutation_state", "previous_terminal_outcome"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RecoveryError(f"recovery checkpoint {key} is invalid")
    worker = value.get("originating_worker")
    if not valid_recovery_worker(
            worker, repo=repo, story_number=story_number):
        raise RecoveryError("recovery checkpoint worker identity is invalid")
    recovered_at = value.get("recovered_at")
    if not valid_utc_timestamp(recovered_at):
        raise RecoveryError("recovery checkpoint timestamp is invalid")
    try:
        apply_args = ["apply", "--index", str(patch)]
        if recorded_base != base_commit:
            git(["merge-base", "--is-ancestor", recorded_base, base_commit],
                worktree, runner=runner)
            apply_args = ["apply", "--3way", "--index", str(patch)]
        git(apply_args, worktree, runner=runner)
        checkout_recovered_gitlinks(
            worktree, paths, base_commit=base_commit, runner=runner)
        applied_paths = changed_paths(worktree, base_commit, runner=runner)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError(
            f"recovery checkpoint application failed: {exc}") from exc
    if sorted(applied_paths) != sorted(paths):
        raise RecoveryError("recovery checkpoint patch paths do not match its manifest")
    if merge_gate.paths_out_of_scope(applied_paths, scope):
        raise RecoveryError("recovery checkpoint patch exceeds Story scope")
    return {
        "present": True,
        "trust": RECOVERY_TRUST,
        "recovered_paths": paths,
        "previous_mutation_state": value["previous_mutation_state"],
        "previous_terminal_outcome": value["previous_terminal_outcome"],
        "base_commit": value["base_commit"],
        "originating_worker": worker,
        "recovered_at": recovered_at,
    }


def worker_prompt(value: dict) -> str:
    recovery = value.get("recovery_context", {})
    disclosure = ""
    if recovery.get("present"):
        disclosure = (
            "\n\n## Recovered work warning\n\n"
            "The files named in `recovery_context.recovered_paths` contain "
            "`untrusted partial changes` recovered from a failed previous worker. "
            "Inspect them before continuing. Keep, revise, or discard them according "
            "to the Story and tests; do not assume they are correct or complete. "
            "The previous terminal outcome explains why the checkpoint exists, not "
            "whether any recovered change is valid.")
    return (HERE.joinpath("prompt.md").read_text() + disclosure
            + "\n\n## Invocation input\n\n" + json.dumps(value, indent=2))


def execute(repo: str, story_number: int, token: str, checkout: pathlib.Path,
            *, timeout: int | None = None, max_usd: float | None = None,
            runner=subprocess.run, client: GitHub | None = None,
            state: CapacityState | None = None, registry=None,
            reservation: str | None = None) -> Delivery:
    client = client or GitHub(repo, token)
    metadata = client.api("")  # read preflight before any write
    default = metadata.get("default_branch")
    if not default:
        raise DeliveryError("repository access constraint failed: default branch unavailable")
    story = client.issue(story_number)
    project_number = int(section(story.get("body") or "", "Project").removeprefix("#"))
    project = client.issue(project_number)
    bounds = parse_bounds(story.get("body") or "")
    if timeout is not None:
        bounds = Bounds(bounds.max_usd, min(bounds.timeout, timeout))
    if max_usd is not None:
        bounds = Bounds(min(bounds.max_usd, max_usd), bounds.timeout)
    if bounds.timeout <= 0 or bounds.max_usd <= 0:
        raise DeliveryError("effective timeout and spend bounds must be positive")
    events = client.pages(f"/issues/{story_number}/timeline")
    version = state_version(events)
    task_key = (capacity_admission.delivery_task_key(
                    repo, story, next_attempt=False)
                if reservation else f"delivery:{repo}:{story_number}:{version}")
    delivery_trace = obs.story_trace_id(repo, story_number, events)
    durable = marker(story_number, version)
    scope, error = merge_gate.parse_scope(story.get("body") or "")
    if error:
        raise DeliveryError(f"invalid Story scope: {error}")
    pulls = linked_prs(story_number, client.pull_requests())
    saved_recovery = recovery_patch(repo, story_number)
    try:
        has_recovery = recovery_available(
            saved_recovery, repo=repo, story_number=story_number, scope=scope,
            runner=runner)
    except RecoveryError as exc:
        exc.recovery_ref = str(saved_recovery)
        raise
    if pulls and durable in (pulls[0].get("body") or ""):
        pull = pulls[0]
        if has_recovery:
            try:
                reconcile_recovery_push(
                    saved_recovery, pull["head"]["sha"], repo=repo,
                    story_number=story_number)
                delivered_head = pushed_recovery_head(
                    saved_recovery, repo=repo, story_number=story_number,
                    scope=scope, runner=runner)
            except RecoveryError as exc:
                exc.recovery_ref = str(saved_recovery)
                raise
            if delivered_head is not None and delivered_head != pull["head"]["sha"]:
                exc = RecoveryError(
                    "durable recovery head does not match pull request head")
                exc.recovery_ref = str(saved_recovery)
                raise exc
            if delivered_head == pull["head"]["sha"]:
                remove_recovery(saved_recovery)
        return Delivery(story_number, project_number, pull["head"]["ref"],
                        pull["number"], pull["head"]["sha"], True)

    protected = protected_control_scope(repo, scope)
    if protected:
        raise DeliveryError(
            "FACTORY_SELF_MODIFICATION_FORBIDDEN: protected Story scope: "
            + ", ".join(protected))
    branch = pulls[0]["head"]["ref"] if pulls else f"story/{story_number}-delivery"
    remote = git(["ls-remote", "--heads", "origin", branch], checkout,
                 runner=runner).stdout.strip()
    if remote:
        git(["fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
            checkout, runner=runner)
    base_ref = f"origin/{branch}" if remote else f"origin/{default}"
    base_commit = git(["rev-parse", base_ref], checkout, runner=runner).stdout.strip()
    try:
        value = build_input(client, story, project, repo=repo,
                            pull_request=pulls[0] if pulls else None)
    except Exception as exc:
        if has_recovery:
            setattr(exc, "mutation_state", "post-mutation")
            setattr(exc, "terminal_outcome", "recovery-input-preflight-failed")
            setattr(exc, "recovery_ref", str(saved_recovery))
        raise
    packet = value["correction_context"]
    obs.process_event(
        "delivery.correction-context", trace_id=delivery_trace,
        repo=repo, story=story_number, project=project_number,
        retry=packet["retry"], pull_request=packet["pull_request"],
        head=packet["head"], digest=packet["digest"],
        source_ids=[item["comment_id"] for item in packet["records"]])
    if has_recovery:
        try:
            reconcile_recovery_push(
                saved_recovery, base_commit, repo=repo,
                story_number=story_number)
            delivered_head = pushed_recovery_head(
                saved_recovery, repo=repo, story_number=story_number, scope=scope,
                runner=runner)
        except RecoveryError as exc:
            exc.recovery_ref = str(saved_recovery)
            raise
        if delivered_head:
            if delivered_head != base_commit:
                exc = RecoveryError(
                    "pushed recovery head does not match branch head")
                exc.recovery_ref = str(saved_recovery)
                raise exc
            try:
                body = f"Story: #{story_number}\n\n{durable}\n"
                pull = (client.update_pr(pulls[0]["number"], body) if pulls else
                        client.create_pr(f"Story #{story_number}: bounded delivery",
                                         branch, default, body))
                fresh = read_back_pr(client, story_number, durable)
            except Exception as exc:
                mark_finalization_failure(exc, saved_recovery)
                raise
            obs.process_event(
                "delivery.pull-request.written", trace_id=delivery_trace,
                repo=repo, story=story_number, project=project_number,
                pull_request=fresh["number"], head=delivered_head)
            remove_recovery(saved_recovery)
            return Delivery(story_number, project_number, branch, fresh["number"],
                            delivered_head, False)
    originating_worker = {"task": task_key}
    with tempfile.TemporaryDirectory(prefix=f"factory-story-{story_number}-") as temp:
        worktree = pathlib.Path(temp)
        recovery_context = {"present": False}
        restored_state = None
        git(["worktree", "add", "--detach", str(worktree), base_ref], checkout,
            runner=runner)
        try:
            if has_recovery:
                recovery_context = restore_failed_work(
                    saved_recovery, worktree, repo=repo,
                    story_number=story_number, base_commit=base_commit,
                    scope=scope, runner=runner)
                restored_state = recovered_work_state(
                    worktree, recovery_context["recovered_paths"],
                    base_commit=base_commit, runner=runner)
            value["recovery_context"] = recovery_context
            prompt = worker_prompt(value)
            owns_state = state is None
            if state is None:
                state_path = capacity_state_path()
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state = CapacityState(state_path, uri=False)
            try:
                available = tuple(registry or resolved_registry(health=state.health))
                labels_set = set(labels(story))
                triggers = frozenset(
                    name for name, candidates in {
                        "hazard": {"hazard", "risk:hazard"},
                        "high-complexity": {"high-complexity", "complexity:high"},
                    }.items() if labels_set & candidates)
                prior_models = state.models_for_task_prefix(
                    capacity_admission.delivery_task_prefix(repo, story),
                    exclude_task_key=task_key)
                request = POLICIES["delivery"].request(
                    triggers=triggers, total_timeout_seconds=bounds.timeout,
                    total_budget_units=bounds.max_usd,
                    prior_models=prior_models)

                def mutation_state():
                    return ("post-mutation" if changed_paths(
                        worktree, f"origin/{default}", runner=runner) else "none")

                adapters = {provider: cli_adapter(
                    provider, cwd=worktree,
                    environment=provider_environment(provider), runner=runner,
                    mutation_state=mutation_state)
                    for provider in {item.provider for item in available}}
                capacity = CapacityExecutor(
                    adapters, state,
                    telemetry=lambda **fields: obs.telemetry(
                        component="delivery-worker", operation="capacity-route",
                        story=story_number, project=project_number, **fields))

                def validate(_output):
                    produced = changed_paths(
                        worktree, f"origin/{default}", runner=runner)
                    if not produced:
                        raise DeliveryError("worker produced no repository changes")
                    outside = merge_gate.paths_out_of_scope(produced, scope)
                    if outside:
                        raise DeliveryError("worker changed paths outside Story scope: "
                                            + ", ".join(outside))

                with obs.Activity("delivery-worker", "engine", "executing",
                                  trace_id=delivery_trace, repo=repo,
                                  story=story_number, project=project_number):
                    result = capacity.execute(
                        task_key=task_key,
                        request=request, registry=available,
                        payload=InvocationPayload(prompt, access="workspace-write"),
                        validate=validate, reservation_id=reservation,
                        on_started=(lambda lease: publish_worker_start(
                            client, repo=repo, story=story_number, version=version,
                            reservation=lease.lease_id, invocation=task_key)))
                if result.attempts:
                    final_attempt = result.attempts[-1]
                    originating_worker.update({
                        key: final_attempt[key] for key in (
                            "invocation_id", "reservation_id", "provider", "model")
                        if final_attempt.get(key) is not None
                    })
                    runlog.engine_usage(
                        story=story_number, project=project_number,
                        engine=final_attempt["model"], phase="worker",
                        launch=("completed" if result.outcome == "success" else "failed"),
                        output=result.output)
                if result.outcome != "success":
                    detail = f": {runlog.tail(result.output)}" if result.output else ""
                    raise DeliveryError(
                        f"delivery capacity failed: {result.outcome}{detail}",
                        result.output, mutation_state=(
                            result.attempts[-1]["mutation_state"]
                            if result.attempts else "none"),
                        terminal_outcome=result.terminal_outcome)
            finally:
                if owns_state:
                    state.close()
            paths = changed_paths(worktree, f"origin/{default}", runner=runner)
            if not paths:
                raise DeliveryError("worker produced no repository changes")
            outside = merge_gate.paths_out_of_scope(paths, scope)
            if outside:
                raise DeliveryError("worker changed paths outside Story scope: " +
                                    ", ".join(outside))
            tests = repository_test_command(worktree)
            # Not an engine: the delivered change's own tests have nothing to
            # log in to, so they run without any credential.
            with obs.Activity("delivery-worker", "tests", "running",
                              trace_id=delivery_trace, repo=repo,
                              story=story_number, project=project_number):
                run(tests, cwd=worktree, timeout=bounds.timeout,
                    env=tool_environment(), runner=runner)
            run_acceptance_verifications(
                story.get("body") or "", cwd=worktree, timeout=bounds.timeout,
                trace_id=delivery_trace, repo=repo, story=story_number,
                project=project_number, runner=runner)
            git(["add", "--", *paths], worktree, runner=runner)
            git(["commit", "-m", f"Deliver Story #{story_number}"], worktree,
                runner=runner)
            head = git(["rev-parse", "HEAD"], worktree,
                       runner=runner).stdout.strip()
            if not has_recovery:
                ref = checkpoint_failed_work(
                    worktree, checkout, repo=repo, story_number=story_number,
                    default=default, base_ref=base_ref,
                    base_commit=base_commit, scope=scope,
                    mutation_state="post-mutation",
                    terminal_outcome="completed-awaiting-push",
                    originating_worker=originating_worker, runner=runner)
                if not ref:
                    raise RecoveryError(
                        "pre-push recovery checkpoint could not be created")
                saved_recovery = pathlib.Path(ref)
                has_recovery = True
            mark_recovery_push_pending(saved_recovery, head)
            try:
                git(["push", "origin", f"HEAD:refs/heads/{branch}"], worktree,
                    runner=runner)
            except Exception as exc:
                if has_recovery:
                    setattr(exc, "mutation_state", "ambiguous")
                    setattr(exc, "terminal_outcome", "push-outcome-ambiguous")
                    setattr(exc, "recovery_ref", str(saved_recovery))
                raise
            if has_recovery:
                try:
                    mark_recovery_pushed(saved_recovery, head)
                except Exception as exc:
                    # Git reported success, but without the durable promotion
                    # marker a retry must reconcile the remote before deciding
                    # whether another worker may run.
                    setattr(exc, "mutation_state", "ambiguous")
                    setattr(exc, "terminal_outcome", "push-outcome-ambiguous")
                    setattr(exc, "recovery_ref", str(saved_recovery))
                    raise
        except Exception as exc:
            try:
                if (isinstance(exc, RecoveryError) or
                        (has_recovery and getattr(exc, "recovery_ref", "") ==
                         str(saved_recovery))):
                    ref = str(saved_recovery)
                else:
                    recovered_paths = recovery_context.get("recovered_paths", [])
                    recovery_is_unchanged = (
                        has_recovery and restored_state is not None and
                        recovered_work_state(
                            worktree, recovered_paths, base_commit=base_commit,
                            runner=runner) ==
                        restored_state and
                        sorted(changed_paths(worktree, base_ref, runner=runner)) ==
                        sorted(recovered_paths))
                    if recovery_is_unchanged:
                        ref = str(saved_recovery)
                    else:
                        ref = checkpoint_failed_work(
                            worktree, checkout, repo=repo,
                            story_number=story_number, default=default,
                            base_ref=base_ref, base_commit=base_commit, scope=scope,
                            mutation_state="post-mutation",
                            terminal_outcome=(
                                getattr(exc, "terminal_outcome", "") or
                                "worker-wrapper-failed"),
                            originating_worker=originating_worker, runner=runner)
                        if has_recovery and not ref:
                            ref = str(saved_recovery)
            except Exception as checkpoint_error:
                # A checkpoint failure must never turn known work into a
                # definite no-start. Keep the original failure, block fallback,
                # and include the preservation error in its diagnostic.
                setattr(exc, "mutation_state", "ambiguous")
                prior = getattr(exc, "output", "")
                setattr(exc, "output", (prior + "\ncheckpoint failed: " +
                                         str(checkpoint_error)).strip())
                ref = ""
            if ref:
                if getattr(exc, "mutation_state", "") != "ambiguous":
                    setattr(exc, "mutation_state", "post-mutation")
                setattr(exc, "recovery_ref", ref)
            raise
        finally:
            git(["worktree", "remove", "--force", str(worktree)], checkout,
                runner=runner)

    try:
        body = f"Story: #{story_number}\n\n{durable}\n"
        if pulls:
            pull = client.update_pr(pulls[0]["number"], body)
        else:
            pull = client.create_pr(f"Story #{story_number}: bounded delivery",
                                    branch, default, body)
        fresh = read_back_pr(client, story_number, durable)
    except Exception as exc:
        if has_recovery:
            mark_finalization_failure(exc, saved_recovery)
        raise
    obs.process_event("delivery.pull-request.written", trace_id=delivery_trace,
                      repo=repo, story=story_number, project=project_number,
                      pull_request=fresh["number"], head=head)
    if has_recovery:
        remove_recovery(saved_recovery)
    return Delivery(story_number, project_number, branch, fresh["number"], head, False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--checkout", default=str(ROOT))
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--reservation")
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("delivery failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    if not args.reservation or not re.fullmatch(r"[0-9a-f]{32}", args.reservation):
        print("delivery failed: a valid admission reservation is required",
              file=sys.stderr)
        return 2
    try:
        with checkout_for_repo(args.repo, pathlib.Path(args.checkout)) as checkout:
            result = execute(args.repo, args.story, token, checkout,
                             timeout=args.timeout, max_usd=args.max_usd,
                             reservation=args.reservation)
    except Exception as exc:
        obs.operational_log("ERROR", "delivery worker failed", exc=exc,
                            component="delivery-worker", operation="delivery",
                            repo=args.repo, story=args.story,
                            platform_diagnostics=platform_diagnostics(
                                pathlib.Path(args.checkout)))
        print(f"delivery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        # The engine's own account, when one exists, travels with the failure:
        # the launcher records this stream on worker.launch.end, which is where
        # a poison report goes looking for an explanation (#330).
        output = getattr(exc, "output", "")
        if output:
            print(f"engine output tail:\n{runlog.tail(output)}", file=sys.stderr)
        print(FAILURE_MARKER + json.dumps({
            "mutation_state": getattr(exc, "mutation_state", "none"),
            "terminal_outcome": getattr(exc, "terminal_outcome", ""),
            "recovery_ref": getattr(exc, "recovery_ref", ""),
        }, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

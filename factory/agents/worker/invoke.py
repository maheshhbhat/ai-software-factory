#!/usr/bin/env python3
"""Headless bounded delivery: claimed Story identity in, one durable PR out.

The engine runs in a built environment, never an inherited one. Inheriting the
operator's shell would hand the engine `GITHUB_TOKEN`/`GH_TOKEN` — which can
write to the repository outside the wrapper's control — and the whole
`FACTORY_WORKER_*` block, which is how the operator declares *which* engine to
launch. A prompt-injected engine that could edit either could change what the
factory does next. So the environment is an allowlist, and this module states
what is on it and why:

* `PATH` — find the engine binary and the tools it shells out to.
* `LANG`, `LC_ALL` — decoding of the engine's own output.
* `TMPDIR` — scratch space; the engine writes nothing durable outside the worktree.
* `SHELL` — the interpreter the engine's Bash tool uses.

That is the whole of `tool_environment()`, and it is all a subprocess gets
unless it is an engine that has to log in. The delivered change's own test
command is such a subprocess: it needs the tools, the locale and scratch
space, and nothing that could authenticate as the operator — so it is launched
with `tool_environment()` and never grows a credential when a new engine is
declared.

An engine additionally gets its own login, and *which* credential that is is
both engine-specific and platform-specific — so it is declared per engine in
`ENGINE_CREDENTIALS` rather than accumulated into one shared list. The Claude
CLI resolves its login from the macOS keychain, which is looked up for the
calling user and so needs `USER`; on Linux the same credential is a file under
`HOME` and `USER` is irrelevant. Either way an explicit `ANTHROPIC_API_KEY` or
`CLAUDE_CODE_OAUTH_TOKEN` also works. Appending today's variable name to one
list would pass the platform it was found on and silently fail the other, so
`clean_environment()` forwards exactly the sources that exist on the platform
it is running on, and `unreachable_credentials()` lets a test say "this engine
can log in" without naming a variable. `clean_environment()` takes the engine
as a required argument for the same reason: a call site has to say whether it
is launching an engine, and only an engine launch can reach a credential.

Write permission is granted deliberately: Claude receives `acceptEdits` and
Codex receives automatic approval inside its workspace-write sandbox, because
the Story asks the engine to change files. That does not widen what may be delivered — `execute()`
compares every changed path against the Story's declared `### Scope` after the
engine exits and refuses the delivery if anything falls outside it.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "factory" / "gates"))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import merge_gate  # noqa: E402
import observability as obs  # noqa: E402
import runlog  # noqa: E402

DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_USD = 40.0
MARKER = "worker-artifact"
SECTION = r"^### {name}\s*$\n(.*?)(?=^### |\Z)"
STORY_LINK = re.compile(r"(?m)^Story: #(\d+)$")


class DeliveryError(RuntimeError):
    """A bounded delivery could not be completed.

    `output` carries whatever the failed subprocess printed. An engine that
    crashed or ran out of time still spent what it spent before it did, and
    that report is the only place those tokens are ever counted.
    """

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


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


@dataclass(frozen=True)
class Credential:
    """One way an engine resolves its own login, and what that costs us."""

    source: str
    variables: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()  # empty means every platform

    def on(self, platform: str) -> bool:
        return not self.platforms or platform.startswith(self.platforms)


# Not credentials: the tools, locale and scratch space any engine needs.
BASE_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL")

# How each engine logs in. Platform-specific by nature — see the module
# docstring for why this is a table and not one shared list of names.
ENGINE_CREDENTIALS: dict[str, tuple[Credential, ...]] = {
    "claude": (
        Credential("macOS login keychain, resolved for the calling user",
                   ("USER",), ("darwin",)),
        Credential("credential file under the operator's home directory",
                   ("HOME",), ("linux",)),
        Credential("explicit API key", ("ANTHROPIC_API_KEY",)),
        Credential("explicit OAuth token", ("CLAUDE_CODE_OAUTH_TOKEN",)),
    ),
    # Verified 2026-08-22: `codex exec` authenticates under an environment
    # holding nothing but the base set, on either platform.
    "codex": (
        Credential("engine-managed session, needing nothing from this environment"),
    ),
}


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


def state_version(events: list[dict]) -> str:
    claims = [item for item in events if item.get("event") == "labeled" and
              (item.get("label") or {}).get("name") == "story:claimed"]
    if not claims:
        raise DeliveryError("Story has no durable claimed state version")
    latest = claims[-1]
    return str(latest.get("id") or latest.get("created_at"))


def marker(story: int, version: str) -> str:
    return f"<!-- {MARKER}:{story}:{version} -->"


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


def credential_sources(engine: str | None,
                       platform: str | None = None) -> tuple[Credential, ...]:
    """The logins reachable on this platform, for the engine being launched.

    `None` means the subprocess is not an engine at all and has nothing to log
    in to, so it gets no credential. A *named* engine we do not recognise —
    anything launched through `FACTORY_DELIVERY_MODEL_CMD` — gets every
    declared engine's sources, since we cannot tell which one it will look for.
    """
    if engine is None:
        return ()
    platform = sys.platform if platform is None else platform
    declared = ((ENGINE_CREDENTIALS[engine],) if engine in ENGINE_CREDENTIALS
                else tuple(ENGINE_CREDENTIALS.values()))
    return tuple(source for group in declared for source in group
                 if source.on(platform))


def tool_environment(environ=None) -> dict[str, str]:
    """Tools, locale and scratch space — what a non-engine subprocess gets.

    No credential, on any platform, for any engine declared later. The
    delivered change's own test command runs under this.
    """
    environ = os.environ if environ is None else environ
    return {key: value for key, value in environ.items()
            if key in BASE_ENVIRONMENT}


def clean_environment(engine: str | None, platform: str | None = None,
                      environ=None) -> dict[str, str]:
    """Allowlist an engine's environment: the tool set, plus its own logins."""
    environ = os.environ if environ is None else environ
    keep = {name for source in credential_sources(engine, platform)
            for name in source.variables}
    return tool_environment(environ) | {key: value for key, value
                                        in environ.items() if key in keep}


def unreachable_credentials(engine: str | None, env: dict[str, str],
                            platform: str | None = None) -> list[Credential]:
    """Declared logins this environment has cut the engine off from.

    Empty means every way the engine knows how to authenticate on this platform
    survived the allowlist. This is the question a test should ask — an engine
    that cannot log in is the failure, not the absence of a particular name.
    """
    return [source for source in credential_sources(engine, platform)
            if not all(name in env for name in source.variables)]


def engine_name(command: list[str]) -> str | None:
    """The engine a model command launches, recognised or not.

    Every model command launches *some* engine, so the name is returned even
    when it is not in `ENGINE_CREDENTIALS` — an operator's own
    `FACTORY_DELIVERY_MODEL_CMD` still has to log in. `None` is reserved for
    "there is no command", which is not an engine launch.
    """
    if not command:
        return None
    return pathlib.PurePath(command[0]).name.lower()


def model_command(input_file: str, bounds: Bounds,
                  engine: str = "claude") -> list[str]:
    template = os.environ.get("FACTORY_DELIVERY_MODEL_CMD", "").strip()
    if template:
        return [part.replace("{input_file}", input_file)
                    .replace("{max_usd}", str(bounds.max_usd))
                    .replace("{timeout}", str(bounds.timeout))
                for part in shlex.split(template)]
    prompt = HERE.joinpath("prompt.md").read_text()
    payload = prompt + "\n\n## Invocation input\n\n" + pathlib.Path(input_file).read_text()
    if engine == "codex":
        # Codex has no CLI dollar-cap option. The enclosing subprocess timeout
        # still enforces the Story's time bound, and its JSONL events preserve
        # the usage it actually reports. Do not invent a spend guarantee.
        # --approve-for-me supplies the workspace-write sandbox itself. Codex
        # rejects combining it with an explicit --sandbox option.
        return ["codex", "exec", "--approve-for-me", "--ephemeral",
                "--ignore-user-config", "--json", payload]
    if engine != "claude":
        raise DeliveryError(f"unsupported delivery engine: {engine}")
    # acceptEdits grants Edit/Write; dontAsk denies them. The Story asks the
    # engine to change files, and `execute()` enforces the Story's Scope on
    # what it changed afterwards. See the module docstring.
    #
    # stream-json exposes the engine lifecycle while preserving its usage.
    return ["claude", "-p", payload, "--max-budget-usd", str(bounds.max_usd),
            "--permission-mode", "acceptEdits", "--output-format", "stream-json",
            "--verbose",
            "--no-session-persistence"]


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
        stderr_tail = runlog.tail(printed(exc.stderr))
        if stderr_tail:
            detail += f"; stderr tail: {stderr_tail}"
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
            f"; stderr tail: {runlog.tail(result.stderr)}"
            f"; stdout tail: {runlog.tail(result.stdout)}",
            printed(result.stdout))
    return result


def run_engine_streamed(cmd: list[str], *, cwd: pathlib.Path, timeout: int,
                        env, story: int, project: int | None) -> subprocess.CompletedProcess:
    """Stream bounded, redacted engine output while retaining usage evidence."""
    process = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True, bufsize=1)
    captured = {"stdout": collections.deque(maxlen=2000),
                "stderr": collections.deque(maxlen=2000)}

    def consume(name: str, stream) -> None:
        for line in iter(stream.readline, ""):
            captured[name].append(line)
            obs.operational_log(
                "INFO", "engine output", component="delivery-worker",
                operation="engine-stream", stage="running", story=story,
                project=project, engine=engine_name(cmd), stream=name,
                engine_output_tail=runlog.tail(line.rstrip()))
        stream.close()

    threads = [threading.Thread(target=consume, args=(name, stream), daemon=True)
               for name, stream in (("stdout", process.stdout),
                                    ("stderr", process.stderr))]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=1)
        stdout = "".join(captured["stdout"])
        stderr = "".join(captured["stderr"])
        detail = f"bounded execution exhausted after {timeout}s"
        if stderr:
            detail += f"; stderr tail: {runlog.tail(stderr)}"
        raise DeliveryError(detail, stdout) from exc
    for thread in threads:
        thread.join(timeout=1)
    return subprocess.CompletedProcess(
        cmd, returncode, "".join(captured["stdout"]),
        "".join(captured["stderr"]))


def launch_engine(cmd: list[str], *, cwd: pathlib.Path, timeout: int, env,
                  story: int, project: int | None = None,
                  runner=subprocess.run) -> subprocess.CompletedProcess:
    """Run the delivery engine, recording what it reported about its usage.

    One runtime-log record per invocation, whether the launch completed or
    failed. Nothing is inferred: if the engine said nothing about its usage the
    record says exactly that, and no number is invented to stand in for it.
    """
    engine = engine_name(cmd)
    try:
        if runner is subprocess.run:
            result = run_engine_streamed(
                cmd, cwd=cwd, timeout=timeout, env=env,
                story=story, project=project)
            if result.returncode:
                raise DeliveryError(
                    f"command failed ({result.returncode})"
                    f"; stderr tail: {runlog.tail(result.stderr)}"
                    f"; stdout tail: {runlog.tail(result.stdout)}",
                    printed(result.stdout))
        else:
            result = run(cmd, cwd=cwd, timeout=timeout, env=env, runner=runner)
    except DeliveryError as error:
        runlog.engine_usage(story=story, project=project, engine=engine,
                            phase="worker", launch="failed", output=error.output)
        raise
    runlog.engine_usage(story=story, project=project, engine=engine,
                        phase="worker", launch="completed",
                        output=printed(getattr(result, "stdout", None)))
    # Usage accounting alone cannot explain a successful engine process that
    # leaves the worktree unchanged. Preserve only the bounded, credential-
    # redacted tail so a later wrapper failure has the engine's own final
    # explanation without turning the operation log into a prompt transcript.
    obs.operational_log(
        "INFO", "delivery engine completed",
        component="delivery-worker", operation="engine-output",
        story=story, project=project, engine=engine,
        engine_output_tail=runlog.tail(
            printed(getattr(result, "stdout", None))),
    )
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
    tracked = git(["diff", "--name-only", f"{base}...HEAD"], worktree,
                  runner=runner).stdout.splitlines()
    # --untracked-files=all: plain --porcelain collapses a brand-new directory
    # to one `dir/` line, which no `dir/sub/**` scope can match — the worker
    # then refuses its own in-scope work (#358, found live by fixture #357).
    pending = git(["status", "--porcelain", "--untracked-files=all"], worktree,
                  runner=runner).stdout.splitlines()
    paths = tracked + [line[3:] for line in pending if len(line) > 3]
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


def build_input(client: GitHub, story: dict, project: dict) -> dict:
    comments = client.pages(f"/issues/{story['number']}/comments")
    decisions = [item for item in client.pages("/issues?state=all")
                 if "type:adr" in labels(item)]
    return {"story": story, "project": project, "adrs": decisions,
            "review_findings": [item for item in comments
                                if "## Review findings" in (item.get("body") or "")]}


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


def execute(repo: str, story_number: int, token: str, checkout: pathlib.Path,
            *, timeout: int | None = None, max_usd: float | None = None,
            engine: str = "claude", runner=subprocess.run,
            client: GitHub | None = None) -> Delivery:
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
    delivery_trace = obs.story_trace_id(repo, story_number, events)
    durable = marker(story_number, version)
    pulls = linked_prs(story_number, client.pull_requests())
    if pulls and durable in (pulls[0].get("body") or ""):
        pull = pulls[0]
        return Delivery(story_number, project_number, pull["head"]["ref"],
                        pull["number"], pull["head"]["sha"], True)

    scope, error = merge_gate.parse_scope(story.get("body") or "")
    if error:
        raise DeliveryError(f"invalid Story scope: {error}")
    protected = merge_gate.protected_factory_scope(scope)
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
    value = build_input(client, story, project)
    with tempfile.TemporaryDirectory(prefix=f"factory-story-{story_number}-") as temp:
        worktree = pathlib.Path(temp)
        git(["worktree", "add", "--detach", str(worktree), base_ref], checkout,
            runner=runner)
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(value, handle)
                input_file = handle.name
            try:
                command = model_command(input_file, bounds, engine)
                with obs.Activity("delivery-worker", "engine", "executing",
                                  trace_id=delivery_trace, repo=repo,
                                  story=story_number, project=project_number,
                                  engine=engine_name(command)):
                    launch_engine(command, cwd=worktree, timeout=bounds.timeout,
                                  env=clean_environment(engine_name(command)),
                                  story=story_number, project=project_number,
                                  runner=runner)
            finally:
                pathlib.Path(input_file).unlink(missing_ok=True)
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
            git(["add", "--", *paths], worktree, runner=runner)
            git(["commit", "-m", f"Deliver Story #{story_number}"], worktree,
                runner=runner)
            git(["push", "origin", f"HEAD:refs/heads/{branch}"], worktree,
                runner=runner)
            head = git(["rev-parse", "HEAD"], worktree, runner=runner).stdout.strip()
        finally:
            git(["worktree", "remove", "--force", str(worktree)], checkout,
                runner=runner)

    body = f"Story: #{story_number}\n\n{durable}\n"
    if pulls:
        pull = client.update_pr(pulls[0]["number"], body)
    else:
        pull = client.create_pr(f"Story #{story_number}: bounded delivery",
                                branch, default, body)
    fresh = read_back_pr(client, story_number, durable)
    obs.process_event("delivery.pull-request.written", trace_id=delivery_trace,
                      repo=repo, story=story_number, project=project_number,
                      pull_request=fresh["number"], head=head)
    return Delivery(story_number, project_number, branch, fresh["number"], head, False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--checkout", default=str(ROOT))
    parser.add_argument("--engine", choices=sorted(ENGINE_CREDENTIALS),
                        default="claude")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-usd", type=float)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("delivery failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        with checkout_for_repo(args.repo, pathlib.Path(args.checkout)) as checkout:
            result = execute(args.repo, args.story, token, checkout,
                             timeout=args.timeout, max_usd=args.max_usd,
                             engine=args.engine)
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
        return 1
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

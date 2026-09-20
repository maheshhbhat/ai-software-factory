#!/usr/bin/env python3
"""Headless planning invocation: artifact identity in, durable GitHub plan out."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "runtime"))
import artifacts  # noqa: E402
import contract  # noqa: E402
import observability as obs  # noqa: E402
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import (  # noqa: E402
    InvocationPayload, cli_adapter, provider_environment,
)
from factory.capacity_pool.state import CapacityState, default_state_path  # noqa: E402

DEFAULT_TIMEOUT = 900
DEFAULT_MAX_USD = 5.0
DEFAULT_MAX_REPOSITORY_BYTES = 500_000


class InvocationError(RuntimeError):
    pass


class RepositoryTooLargeError(InvocationError):
    """`read_repository` hit the configured byte cap.

    A distinct type, not just a message, so `execute` can tell "genuinely
    too large — try the repository-native fallback" apart from every other
    reason `read_repository` fails closed, which must keep failing closed.
    """


def labels_of(issue: dict) -> list[str]:
    return [label["name"] if isinstance(label, dict) else label
            for label in issue.get("labels", [])]


def state_version(client: artifacts.GitHubStore, issue: dict) -> str:
    """Stable across the invocation's own comments/body writes."""
    altitude = contract.select_altitude(set(labels_of(issue)))
    if altitude is contract.Altitude.CAMPAIGN:
        stable = json.dumps({"body": issue.get("body") or "", "labels": sorted(labels_of(issue))},
                            sort_keys=True).encode()
        return hashlib.sha256(stable).hexdigest()[:20]
    timeline = client._pages(f"/issues/{issue['number']}/timeline")
    lifecycle = [event for event in timeline if event.get("event") == "labeled"
                 and (event.get("label") or {}).get("name", "").startswith("project:")]
    if not lifecycle:
        raise InvocationError("project trigger has no durable lifecycle state version")
    latest = lifecycle[-1]
    return str(latest.get("id") or latest.get("created_at"))


def read_repository(client: artifacts.GitHubStore,
                     max_repository_bytes: int = DEFAULT_MAX_REPOSITORY_BYTES,
                     *, repo: str | None = None, artifact: int | None = None,
                     ) -> tuple[str, list[dict], dict]:
    """Private-repository read preflight. No writer is called before this returns.

    `max_repository_bytes` bounds the total grounded source content read for
    Planning's prompt. The default (`DEFAULT_MAX_REPOSITORY_BYTES`) is
    unchanged from the prior hardcoded 500,000; a caller may explicitly pass
    a different positive value instead. There is no automatic sizing from
    repository size, and no truncation — exceeding this bound, whatever its
    value, still fails closed before any planning artifact is written.

    `repo`/`artifact` identify the invocation in the audit log below; they
    are not otherwise used. Planning runs in its own subprocess and does not
    inherit the poller's ambient tracing context, so without them a log
    entry cannot show which invocation it belongs to — and two invocations
    sharing the same limit would otherwise hash to the same log event_id.
    """
    if max_repository_bytes <= 0:
        raise InvocationError(
            "repository read constraint failed: max_repository_bytes must be positive")
    # Recorded as the first thing this function does that isn't input
    # validation, so the configured limit is attributable in the log
    # whatever later fails — a byte-limit breach, but just as much a
    # product.md/ADR fetch or decode failure that happens first.
    obs.process_event("planning.repository.max_bytes_configured",
                      max_repository_bytes=max_repository_bytes, repo=repo, artifact=artifact)
    metadata = client._api("")
    branch = metadata.get("default_branch")
    if not branch:
        raise InvocationError("repository read constraint failed: default branch unavailable")
    tree = client._api(f"/git/trees/{branch}?recursive=1")
    files = sorted(item["path"] for item in tree.get("tree", [])
                   if item.get("type") == "blob")
    product_paths = [path for path in files if path.lower() == "product.md"]
    if len(product_paths) != 1:
        raise InvocationError("repository read constraint failed: product.md missing or ambiguous")

    def content(path):
        item = client._api(f"/contents/{path}")
        import base64
        return base64.b64decode(item["content"]).decode("utf-8")

    product = content(product_paths[0])
    adr_paths = [path for path in files
                 if path.lower().endswith(".md") and
                 ("/adr" in f"/{path.lower()}" or "/decisions/" in f"/{path.lower()}/")]
    adrs = [{"path": path, "content": content(path)} for path in adr_paths]
    source_paths = [path for path in files if path != product_paths[0] and
                    path.lower().endswith((".js", ".mjs", ".cjs", ".ts", ".tsx",
                                           ".jsx", ".py", ".json", ".toml", ".md",
                                           ".yml", ".yaml", ".html", ".htm", ".css"))]
    sources, total = {}, 0
    for path in source_paths:
        text = content(path)
        total += len(text.encode())
        if total > max_repository_bytes:
            raise RepositoryTooLargeError(
                "repository read constraint failed: grounded source context "
                f"exceeds configured limit of {max_repository_bytes} bytes")
        sources[path] = text
    evidence = repository_evidence(files, sources)
    return product, adrs, {"default_branch": branch, "files": files,
                           "sources": sources, **evidence}


def clone_and_ground_repository(client: artifacts.GitHubStore, repo: str, token: str,
                                workspace_root: pathlib.Path, *,
                                artifact: int | None = None,
                                max_repository_bytes: int = DEFAULT_MAX_REPOSITORY_BYTES,
                                ) -> tuple[str, list[dict], dict, pathlib.Path]:
    """Fallback grounding for a repository `read_repository` cannot inline
    (see `RepositoryTooLargeError`). Clones the repository at its exact
    current commit instead of reading and inlining every file's content —
    the same clone-and-explore pattern `factory/agents/review/invoke.py`
    already uses safely in production.

    Returns `(product, adrs, repository, workspace)`. `workspace` is a
    *neutral* directory containing the checkout at `workspace/repo` — never
    the checkout itself. An engine launched with its working directory
    inside a real checkout loads that repository's own `CLAUDE.md`/
    `AGENTS.md` as its own operating instructions (a real, previously
    observed incident: see the comment in
    `factory/capacity_pool/providers/cli.py`'s `probe()`), which would
    defeat this contract's own rule that repository content is context,
    never instructions. `run_model` tells the model where the checkout
    actually is via the prompt instead.

    `repository["sources"]` is empty and `repository["commit_sha"]`
    records exactly what was checked out. Every grounding read below —
    the file index, `product.md`, ADRs, and the evidence scan — is pinned
    to that exact commit, not the branch name, so a push landing between
    the commit lookup and these reads can never produce a checkout and a
    file index that disagree. `repository_evidence` runs unchanged,
    reading the same file categories from the clone instead of the GitHub
    API, so its forbidden-dependency/production-owner facts are identical
    either way — bounded by the same `max_repository_bytes` ceiling the
    non-fallback path uses, so a repository with huge fixture/data files
    under a test directory still fails closed rather than exhausting
    memory, exactly the failure mode this Story exists to avoid elsewhere.
    """
    metadata = client._api("")
    branch = metadata.get("default_branch")
    if not branch:
        raise InvocationError("repository read constraint failed: default branch unavailable")
    ref = client._api(f"/git/ref/heads/{branch}")
    commit_sha = (ref.get("object") or {}).get("sha")
    if not commit_sha:
        raise InvocationError("repository read constraint failed: exact commit unavailable")
    # Every read below names commit_sha explicitly (a tree SHA, or ?ref=)
    # rather than the branch — never the branch again, which can advance
    # between this lookup and any later call.
    tree = client._api(f"/git/trees/{commit_sha}?recursive=1")
    files = sorted(item["path"] for item in tree.get("tree", [])
                   if item.get("type") == "blob")
    product_paths = [path for path in files if path.lower() == "product.md"]
    if len(product_paths) != 1:
        raise InvocationError("repository read constraint failed: product.md missing or ambiguous")

    def api_content(path):
        item = client._api(f"/contents/{path}?ref={commit_sha}")
        return base64.b64decode(item["content"]).decode("utf-8")

    product = api_content(product_paths[0])
    adr_paths = [path for path in files
                 if path.lower().endswith(".md") and
                 ("/adr" in f"/{path.lower()}" or "/decisions/" in f"/{path.lower()}/")]
    adrs = [{"path": path, "content": api_content(path)} for path in adr_paths]

    repo_dir = workspace_root / "repo"
    auth_header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    clone_env = dict(os.environ)
    # Scoped to the github.com URL prefix, not a blanket http.extraHeader:
    # an unscoped header is sent with every HTTP request git makes for this
    # process, including a Git LFS smudge filter's request to whatever
    # lfs.url a checked-out .lfsconfig names. A repository-controlled LFS
    # server would then receive this factory token. Scoping the config key
    # to https://github.com/ means git (and git-lfs, which reads the same
    # config) only attaches it to requests against that URL.
    clone_env.update({"GIT_CONFIG_COUNT": "1",
                      "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                      "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth_header}",
                      # Planning reads text (product.md, ADRs, JSON/test
                      # evidence) from the checkout; it never needs the
                      # binary content an LFS pointer resolves to. Without
                      # this, checkout runs the LFS smudge filter, which
                      # downloads every tracked object the commit
                      # references — an unbounded, repository-controlled
                      # amount of outbound traffic and worker disk use.
                      "GIT_LFS_SKIP_SMUDGE": "1"})
    obs.process_event("planning.repository.cloned", repo=repo, artifact=artifact,
                      commit_sha=commit_sha)
    # --filter=blob:none + --no-checkout: fetch the full commit/tree graph
    # (needed so the later checkout can resolve commit_sha even if it is
    # not the branch tip by the time this runs) but no blob content up
    # front. The explicit checkout below then fetches only the blobs the
    # one target commit's tree actually references, not the repository's
    # full history of file content.
    subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
                    f"https://github.com/{repo}.git", "repo"],
                   cwd=workspace_root, env=clone_env, check=True,
                   capture_output=True, text=True, timeout=120)
    subprocess.run(["git", "checkout", "--quiet", commit_sha], cwd=repo_dir,
                   env={"PATH": clone_env.get("PATH", "")}, check=True,
                   capture_output=True, text=True, timeout=60)

    # Only the categories repository_evidence() actually inspects — the
    # model itself reads whatever else it needs directly from the clone.
    # Bounded exactly like the non-fallback path's grounded-content cap:
    # this is the one case where the fallback still reads file content
    # into memory up front, so it needs the same fail-closed protection.
    evidence_paths = [path for path in files
                      if path.lower().endswith(".json") or executable_test_path(path)]
    local_sources, evidence_total = {}, 0
    for path in evidence_paths:
        local_path = repo_dir / path
        # is_symlink() checked without following: a symlink to a special
        # file (e.g. a procfs path) can report stat() size 0 while still
        # streaming unbounded content on read, defeating the size check
        # below entirely. Rejected outright rather than resolved and
        # validated, since evidence never needs to be a symlink.
        if local_path.is_symlink() or not local_path.is_file():
            continue
        # Checked against the file's size on disk before read_text() runs,
        # so a single file already over the limit is never materialized in
        # memory to find that out.
        evidence_total += local_path.stat().st_size
        if evidence_total > max_repository_bytes:
            raise InvocationError(
                "repository read constraint failed: policy/test evidence content "
                f"exceeds {max_repository_bytes} bytes")
        local_sources[path] = local_path.read_text(encoding="utf-8", errors="replace")
    evidence = repository_evidence(files, local_sources)

    return product, adrs, {"default_branch": branch, "files": files, "sources": {},
                           "commit_sha": commit_sha, **evidence}, workspace_root


def repository_evidence(files: list[str], sources: dict[str, str]) -> dict:
    """Extract explicit structured ownership and mechanically provable policy facts."""
    known = set(files)
    owners, forbidden, assertions = [], set(), []
    for source_path, text in sources.items():
        if source_path.lower().endswith(".json"):
            try:
                manifest = json.loads(text)
            except json.JSONDecodeError:
                manifest = None
            if isinstance(manifest, dict):
                containers = (manifest.get("factoryPlanning"),
                              manifest.get("factoryPolicy"), manifest.get("policy"))
                for container in containers:
                    if isinstance(container, dict):
                        values = container.get("forbiddenDependencies") or []
                        if isinstance(values, list):
                            forbidden.update(item for item in values
                                             if isinstance(item, str) and item)
                        ownership = container.get("productionOwners") or []
                        if isinstance(ownership, dict):
                            ownership = [{"behavior": behavior, "path": path}
                                         for behavior, path in ownership.items()]
                        if isinstance(ownership, list):
                            for fact in ownership:
                                if not isinstance(fact, dict):
                                    continue
                                path = fact.get("path")
                                behavior = fact.get("behavior")
                                story_key = fact.get("storyKey", fact.get("story_key"))
                                if (not isinstance(path, str) or path not in known
                                        or not ((isinstance(behavior, str) and behavior)
                                                or (isinstance(story_key, str)
                                                    and story_key))):
                                    continue
                                owners.append({
                                    "path": path, "behavior": behavior or "",
                                    "story_key": story_key or "",
                                    "evidence": (f"{source_path}:"
                                                 "productionOwners"),
                                })
        if executable_test_path(source_path):
            for line_number, line in enumerate(text.splitlines(), 1):
                names = forbidden_dependency_assertions(line)
                for name in names:
                    forbidden.add(name)
                    assertions.append({
                        "kind": "forbidden-dependency", "name": name,
                        "evidence": f"{source_path}:{line_number}",
                    })
    return {"production_owners": owners,
            "forbidden_dependencies": sorted(forbidden),
            "policy_assertions": assertions}


DEPENDENCY_ACCESS = (r"(?:devDependencies|dependencies)(?:\?\.)?"
                     r"(?:\.([@A-Za-z0-9_/-]+)|\[['\"]([@A-Za-z0-9_./-]+)['\"]\])")


def executable_test_path(path: str) -> bool:
    """True for conventional executable test files, never prose or product source."""
    lowered = path.lower()
    name = pathlib.PurePosixPath(lowered).name
    return (lowered.startswith(("test/", "tests/", "spec/", "specs/"))
            or "/test/" in lowered or "/tests/" in lowered
            or name.startswith("test_") or ".test." in name or ".spec." in name)


def forbidden_dependency_assertions(line: str) -> set[str]:
    """Names denied by explicit assertion shapes; positive assertions yield none."""
    names = set()
    patterns = (
        rf"assert\.(?:equal|strictEqual)\s*\([^,]*{DEPENDENCY_ACCESS}\s*,\s*"
        r"(?:undefined|null|false)\b",
        rf"expect\s*\([^)]*{DEPENDENCY_ACCESS}[^)]*\)\."
        r"(?:toBeUndefined|toBeFalsy|toBeNull)\s*\(",
        rf"assert(?:\.ok)?\s*\(\s*!\s*[^)]*{DEPENDENCY_ACCESS}",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, line, re.I):
            names.add(next(value for value in match.groups() if value))
    empty_map_patterns = (
        r"assert\.(?:deepEqual|deepStrictEqual)\s*\([^,]*"
        r"(?:devDependencies|dependencies)\s*,\s*\{\s*\}",
        r"expect\s*\([^)]*(?:devDependencies|dependencies)[^)]*\)\."
        r"(?:toEqual|toStrictEqual)\s*\(\s*\{\s*\}\s*\)",
    )
    if any(re.search(pattern, line, re.I) for pattern in empty_map_patterns):
        names.add("*")
    return names


def prompt_version() -> str:
    return hashlib.sha256(HERE.joinpath("prompt.md").read_bytes()).hexdigest()[:12]


def review_comments(client: artifacts.GitHubStore, number: int) -> list[dict]:
    """Ground revisions in human comments, excluding the agent's own artifacts."""
    return [{"id": item.get("id"), "author": (item.get("user") or {}).get("login"),
             "created_at": item.get("created_at"), "body": item.get("body") or ""}
            for item in client.list_comments(number)
            if f"<!-- {artifacts.MARKER}:" not in (item.get("body") or "")]


def feedback_version(comments: list[dict]) -> str:
    stable = json.dumps(comments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(stable).hexdigest()[:12]


def existing_plan(client: artifacts.GitHubStore, artifact: int) -> dict:
    prefix = f"<!-- {artifacts.MARKER}:{artifact}:"
    issues = [{"number": item["number"], "title": item.get("title"),
               "labels": labels_of(item), "body": item.get("body") or ""}
              for item in client.list_issues("all")
              if prefix in (item.get("body") or "") and
              (":adr -->" in item["body"] or ":story:" in item["body"])]
    digests = [{"id": item.get("id"), "body": item.get("body") or ""}
               for item in client.list_comments(artifact)
               if prefix in (item.get("body") or "") and ":digest -->" in item["body"]]
    return {"issues": issues, "digests": digests}


def verify_with_retry(client, trigger, key, altitude, attempts=5, sleeper=time.sleep):
    """Retry GitHub's eventually-consistent issue listing for a bounded window."""
    error = None
    for attempt in range(attempts):
        try:
            return artifacts.verify(client, trigger, key, altitude)
        except artifacts.ArtifactError as exc:
            error = exc
            if attempt + 1 < attempts:
                sleeper(attempt + 1)
    raise error


def _parse_output(raw: str) -> dict:
    try:
        try:
            parsed_stdout = json.loads(raw)
        except json.JSONDecodeError:
            parsed_stdout = None
        if isinstance(parsed_stdout, dict) and "type" not in parsed_stdout and "structured_output" not in parsed_stdout:
            envelope = parsed_stdout
        else:
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            envelope = None
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                structured = event.get("structured_output")
                if isinstance(structured, dict):
                    envelope = structured
                    break
                message = event.get("message")
                content = message.get("content", []) if isinstance(message, dict) else []
                for block in reversed(content):
                    if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "StructuredOutput":
                        continue
                    candidate = block.get("input")
                    if isinstance(candidate, str):
                        try:
                            candidate = json.loads(candidate)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(candidate, dict):
                        envelope = candidate
                        break
                if envelope is not None:
                    break
            if envelope is None:
                for event in reversed(events):
                    raw_result = event.get("result") if isinstance(event, dict) else None
                    if isinstance(raw_result, str) and raw_result.strip():
                        try:
                            candidate = json.loads(raw_result)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict):
                            envelope = candidate
                            break
        if isinstance(envelope, dict) and isinstance(envelope.get("structured_output"), dict):
            envelope = envelope["structured_output"]
        elif isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            envelope = json.loads(envelope["result"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvocationError("planning model returned malformed JSON") from exc
    if not isinstance(envelope, dict):
        raise InvocationError("planning model returned a non-object")
    return envelope


def _capacity_state() -> CapacityState:
    path = default_state_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    return CapacityState(path, uri=False)


def _planning_triggers(value: dict) -> frozenset[str]:
    labels = set((value.get("trigger") or {}).get("labels", []))
    triggers = set()
    if labels & {"architecture", "type:architecture", "risk:architecture"}:
        triggers.add("architecture")
    if labels & {"high-complexity", "complexity:high"}:
        triggers.add("high-complexity")
    return frozenset(triggers)


def run_model(value: dict, timeout: int, max_usd: float,
              runner=subprocess.run, clock=time.monotonic, *,
              state: CapacityState | None = None, registry=None,
              workspace: pathlib.Path | None = None) -> dict:
    """`workspace`, when given, is a *neutral* directory containing a real
    repository checkout at `workspace/repo` (see
    `clone_and_ground_repository`) — the engine's own working directory
    stays outside the checkout itself, so it never auto-loads the target
    repository's own `CLAUDE.md`/`AGENTS.md` as its own instructions (a
    real, previously observed incident; see the comment in
    `factory/capacity_pool/providers/cli.py`'s `probe()`). `prompt.md`
    tells the model the checkout is at `./repo` relative to its working
    directory. `access="read-only"` is already `InvocationPayload`'s
    default — unchanged either way, so the model can read files there but
    never write into the product repo. `skip_git_repo_check` is set for
    this case because the neutral working directory itself is not a git
    repository, only the `repo` subdirectory beneath it is.
    """
    altitude = contract.select_altitude(set((value.get("trigger") or {}).get("labels", [])))
    schema_value = contract.json_schema(altitude)
    prompt = (HERE.joinpath("prompt.md").read_text()
              + "\n\n## Invocation input\n\n" + json.dumps(value, indent=2)
              + "\n\nReturn only the contract JSON object; do not write GitHub directly.")
    with tempfile.NamedTemporaryFile("w", suffix=".schema.json", encoding="utf-8") as schema, \
            tempfile.NamedTemporaryFile("w", suffix=".result.json", encoding="utf-8") as output:
        json.dump(schema_value, schema)
        schema.flush()
        owns_state = state is None
        state = state or _capacity_state()
        try:
            available = tuple(registry or resolved_registry(health=state.health))
            request = POLICIES["planning"].request(
                triggers=_planning_triggers(value), total_timeout_seconds=timeout,
                total_budget_units=max_usd)
            payload = InvocationPayload(prompt, schema_value, pathlib.Path(schema.name),
                                        pathlib.Path(output.name),
                                        skip_git_repo_check=(workspace is not None))
            adapters = {provider: cli_adapter(
                provider, cwd=(workspace or ROOT),
                environment=provider_environment(provider), runner=runner)
                for provider in {item.provider for item in available}}
            executor = CapacityExecutor(
                adapters, state,
                telemetry=lambda **fields: obs.telemetry(
                    component="planning-agent", operation="capacity-route", **fields),
                monotonic=clock)
            parsed = None
            def validate(raw):
                nonlocal parsed
                parsed = _parse_output(raw)
                contract.validate_output(altitude, parsed, value.get("repository"))
            material = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            result = executor.execute(
                task_key="planning:" + hashlib.sha256(material).hexdigest(),
                request=request, registry=available, payload=payload, validate=validate)
            if result.outcome != "success":
                raise InvocationError(f"planning capacity failed: {result.outcome}")
            return parsed
        finally:
            if owns_state:
                state.close()

def execute(repo: str, artifact: int, token: str, timeout: int, max_usd: float,
            runner=subprocess.run, *, state=None, registry=None,
            max_repository_bytes: int = DEFAULT_MAX_REPOSITORY_BYTES) -> artifacts.WrittenPlan:
    client = artifacts.GitHubStore(repo, token)
    # The ExitStack only ever holds anything when the repository-native
    # fallback below actually triggers; for every repository that already
    # fits, this is an empty stack and changes nothing. It wraps the rest
    # of this function (not just the read) so the clone survives through
    # run_model and is removed on every exit path once execute returns.
    with contextlib.ExitStack() as stack:
        try:
            issue = client.get_issue(artifact)
            workspace = None
            try:
                product, adrs, repository = read_repository(
                    client, max_repository_bytes=max_repository_bytes,
                    repo=repo, artifact=artifact)
            except RepositoryTooLargeError:
                temp = stack.enter_context(
                    tempfile.TemporaryDirectory(prefix=f"factory-planning-{artifact}-"))
                product, adrs, repository, workspace = clone_and_ground_repository(
                    client, repo, token, pathlib.Path(temp), artifact=artifact,
                    max_repository_bytes=max_repository_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                raise InvocationError(
                    f"repository read constraint failed: GitHub returned {exc.code}; "
                    "no planning artifacts were written") from exc
            raise
        feedback = review_comments(client, artifact)
        prior_plan = existing_plan(client, artifact)
        value = {"trigger": {**issue, "labels": labels_of(issue)}, "product": product,
                 "adrs": adrs, "repository": repository, "review_comments": feedback,
                 "existing_plan": prior_plan}
        validated = contract.validate_input(value)
        altitude = contract.select_altitude(set(validated.trigger["labels"]))
        key = (f"{artifact}:{state_version(client, issue)}:{altitude.value}:"
               f"prompt-{prompt_version()}:feedback-{feedback_version(feedback)}")
        output = run_model(value, timeout, max_usd, runner=runner,
                           state=state, registry=registry, workspace=workspace)
        contract.validate_output(altitude, output, repository)
        artifacts.write(client, value["trigger"], key, output)
        verified = verify_with_retry(client, value["trigger"], key, altitude)
        if altitude is contract.Altitude.PROJECT:
            fresh = client.get_issue(artifact)
            labels = set(labels_of(fresh))
            if "project:planning" not in labels:
                raise InvocationError(
                    "verified project output cannot finish: trigger is not project:planning")
            labels.remove("project:planning")
            labels.add("project:awaiting-ready")
            client.update_labels(artifact, sorted(labels))
        return verified


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Headless planning agent")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--artifact", required=True, type=int)
    parser.add_argument("--timeout", type=int,
                        default=int(os.environ.get("FACTORY_PLANNING_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--max-usd", type=float,
                        default=float(os.environ.get("FACTORY_PLANNING_MAX_USD", DEFAULT_MAX_USD)))
    parser.add_argument("--max-repository-bytes", type=int,
                        default=int(os.environ.get("FACTORY_PLANNING_MAX_REPOSITORY_BYTES",
                                                    DEFAULT_MAX_REPOSITORY_BYTES)))
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("planning failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.max_usd <= 0 or args.max_repository_bytes <= 0:
        print("planning failed: timeout, max-usd, and max-repository-bytes must be positive",
              file=sys.stderr)
        return 2
    try:
        result = execute(args.repo, args.artifact, token, args.timeout, args.max_usd,
                         max_repository_bytes=args.max_repository_bytes)
    except Exception as exc:  # fail loudly at the headless boundary
        print(f"planning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"altitude": result.altitude.value, "project": result.project,
                      "adr": result.adr, "stories": result.stories}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

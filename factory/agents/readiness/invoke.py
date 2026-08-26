#!/usr/bin/env python3
"""Independent exact-revision production-readiness evaluator."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))

from factory.agents.review.invoke import (  # noqa: E402
    GitHub, bounded_review_adapter, clean_environment, git_auth_header,
    reviewer_provider_environment, store_outcome, finalize_outcome,
)
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import InvocationPayload  # noqa: E402
from factory.capacity_pool.state import CapacityState  # noqa: E402
from factory.runtime import operating_envelope, production_readiness  # noqa: E402

DEFAULT_TIMEOUT = 300


class EvaluationError(RuntimeError):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def labels(issue: dict) -> set[str]:
    return {item.get("name") if isinstance(item, dict) else item
            for item in issue.get("labels", [])}


def clone_integrated(repo: str, token: str, revision: str,
                     workspace: pathlib.Path, timeout: int) -> pathlib.Path:
    env = clean_environment()
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": git_auth_header(token)})
    subprocess.run(["git", "clone", "--quiet", f"https://github.com/{repo}.git", "repo"],
                   cwd=workspace, env=env, check=True, capture_output=True,
                   text=True, timeout=timeout)
    checkout = workspace / "repo"
    subprocess.run(["git", "checkout", "--quiet", revision], cwd=checkout,
                   env=clean_environment(), check=True, capture_output=True,
                   text=True, timeout=timeout)
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                            env=clean_environment(), check=True,
                            capture_output=True, text=True, timeout=30).stdout.strip()
    if actual != revision:
        raise EvaluationError("integrated checkout revision does not match")
    return checkout


def parse_model_output(path: pathlib.Path, revision: str,
                       envelope: list[dict]) -> tuple[list[dict], list[dict]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError("malformed production-readiness output") from exc
    if not isinstance(value, dict) or set(value) != {
            "revision", "results", "observations"} or value.get("revision") != revision:
        raise EvaluationError("malformed or stale production-readiness output")
    results, observations = value.get("results"), value.get("observations")
    if not isinstance(results, list) or not isinstance(observations, list):
        raise EvaluationError("production-readiness results are malformed")
    if [item.get("id") for item in results if isinstance(item, dict)] != [
            item["id"] for item in envelope]:
        raise EvaluationError("production-readiness output omitted an envelope ID")
    return results, observations


def assert_checkout_unchanged(checkout: pathlib.Path,
                              staging: pathlib.Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=checkout, env=clean_environment(), capture_output=True, text=True,
        timeout=30, check=True)
    allowed = f"?? {staging.relative_to(checkout)}"
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines != [allowed]:
        raise EvaluationError(
            "production-readiness evaluator modified the integrated checkout")


def payload(project: dict, envelope: list[dict], revision: str,
            output: pathlib.Path) -> InvocationPayload:
    fields = {"project": project.get("number"), "revision": revision,
              "project_body": project.get("body") or "",
              "operating_envelope": envelope}
    prompt = (HERE.joinpath("prompt.md").read_text(encoding="utf-8")
              + f"\n\nWrite the JSON outcome to: {output}\n\nInput: "
              + json.dumps(fields, sort_keys=True))
    return InvocationPayload(prompt, access="workspace-write",
                             allowed_tools=("Read", "Bash", "Write"),
                             disallowed_tools=("Agent",))


def execute(repo: str, project_number: int, token: str, *, client=None,
            timeout: int = DEFAULT_TIMEOUT, state: CapacityState | None = None,
            registry=None, runner=subprocess.run) -> dict:
    client = client or GitHub(repo, token)
    project = client.api(f"/issues/{project_number}")
    if not {"type:project", "project:active"} <= labels(project):
        raise EvaluationError("production readiness requires an active Project")
    envelope = operating_envelope.parse_project(project.get("body") or "")
    metadata = client.api("")
    branch = metadata.get("default_branch")
    integrated = client.api(f"/commits/{branch}") if branch else {}
    revision = integrated.get("sha")
    if not isinstance(revision, str) or len(revision) != 40:
        raise EvaluationError("integrated revision is unavailable")
    comments = client.pages(f"/issues/{project_number}/comments")
    existing = production_readiness.latest(
        comments, repo=repo, project=project_number, revision=revision,
        envelope=envelope)
    if existing is not None:
        return {"status": "replay", "overall": existing["overall"],
                "revision": revision}

    started = timestamp()
    with tempfile.TemporaryDirectory(
            prefix=f"factory-readiness-{project_number}-{revision[:8]}-") as temporary:
        workspace = pathlib.Path(temporary)
        checkout = clone_integrated(repo, token, revision, workspace, timeout)
        staging = checkout / ".factory-readiness-out.json"
        durable = checkout / ".git" / "factory-readiness-out.json"
        store_outcome(staging, durable)
        owns_state = state is None
        if state is None:
            configured = os.environ.get("FACTORY_CAPACITY_STATE", "").strip()
            state_path = pathlib.Path(configured) if configured else \
                ROOT / "runs" / "capacity-pool.sqlite"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = CapacityState(state_path, uri=False)
        try:
            available = tuple(registry or resolved_registry(health=state.health))
            providers = {item.provider for item in available}
            adapters = {provider: bounded_review_adapter(
                provider, cwd=checkout,
                environment=reviewer_provider_environment(
                    provider, workspace / "evaluator-home" / provider),
                staging=staging, output=durable, runner=runner)
                for provider in providers}
            executor = CapacityExecutor(adapters, state)
            parsed = {}

            def validate(_output):
                assert_checkout_unchanged(checkout, staging)
                finalize_outcome(staging, durable)
                results, observations = parse_model_output(
                    durable, revision, envelope)
                parsed.update({"results": results, "observations": observations})

            outcome = executor.execute(
                task_key=f"production-readiness:{repo}:{project_number}:{revision}",
                request=POLICIES["production-readiness"].request(
                    total_timeout_seconds=timeout),
                registry=available, payload=payload(project, envelope, revision, staging),
                validate=validate)
            if outcome.outcome != "success":
                raise EvaluationError(
                    f"production-readiness capacity failed: {outcome.outcome}")
        finally:
            if owns_state:
                state.close()
    artifact = production_readiness.build(
        repo=repo, project=project_number, revision=revision, envelope=envelope,
        results=parsed["results"], observations=parsed["observations"],
        started_at=started, completed_at=timestamp())
    fresh = client.api(f"/commits/{branch}")
    if fresh.get("sha") != revision:
        raise EvaluationError("integrated revision changed before publication")
    client.api(f"/issues/{project_number}/comments", method="POST",
               value={"body": production_readiness.render(artifact)})
    durable_artifact = production_readiness.latest(
        client.pages(f"/issues/{project_number}/comments"), repo=repo,
        project=project_number, revision=revision, envelope=envelope)
    if durable_artifact != artifact:
        raise EvaluationError("durable production-readiness read-back failed")
    return {"status": "published", "overall": artifact["overall"],
            "revision": revision}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--project", required=True, type=int)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("production readiness failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        print(json.dumps(execute(args.repo, args.project, token,
                                 timeout=args.timeout), sort_keys=True))
        return 0
    except Exception as exc:
        print(f"production readiness failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

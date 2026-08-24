"""Bounded, credential-redacted line streaming for long engine subprocesses."""

from __future__ import annotations

import collections
import contextvars
import pathlib
import subprocess
import threading

import observability as obs
import runlog


def run(command: list[str], *, cwd, env, timeout: int, component: str,
        operation: str, **fields) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        command, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True, bufsize=1)
    captured = {"stdout": collections.deque(maxlen=2000),
                "stderr": collections.deque(maxlen=2000)}

    def consume(name, stream):
        for line in iter(stream.readline, ""):
            captured[name].append(line)
            obs.operational_log(
                "INFO", "engine output", component=component,
                operation=operation, stage="running", stream=name,
                engine_output_tail=runlog.tail(line.rstrip()), **fields)
        stream.close()

    threads = []
    for item in (("stdout", process.stdout), ("stderr", process.stderr)):
        context = contextvars.copy_context()
        threads.append(threading.Thread(
            target=lambda values=item, current=context:
                current.run(consume, *values), daemon=True))
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=1)
        error = subprocess.TimeoutExpired(command, timeout,
                                          output="".join(captured["stdout"]),
                                          stderr="".join(captured["stderr"]))
        raise error
    for thread in threads:
        thread.join(timeout=1)
    return subprocess.CompletedProcess(
        command, returncode, "".join(captured["stdout"]),
        "".join(captured["stderr"]))

#!/usr/bin/env python3
# This file is also an executable Python program, because the tests herein
# call it as a subprocess.

import logging
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import pytest

from workqueue import OverviewViewParams, Progress, WorkQueue


def build_params(document_set_id: str, api_token: str) -> OverviewViewParams:
    return OverviewViewParams(
        "http://test-server/what/ever", document_set_id, api_token
    )


@pytest.fixture
def work():
    with tempfile.TemporaryDirectory() as td:
        with ThreadPoolExecutor(2, thread_name_prefix="test-executor-") as ex:
            yield WorkQueue(__file__, ex, Path(td))


def test_job_succeed(work):
    params = build_params("succeed", "y")
    job = work.ensure_run(params)
    assert job.params == params
    for progress in work.report_job_progress_until_completed(job):
        pass
    assert (progress.returncode, progress.error) == (0, None)


def test_job_fail(work):
    params = build_params("fail", "an errmsg")
    job = work.ensure_run(params)
    for progress in work.report_job_progress_until_completed(job):
        pass
    assert (progress.returncode, progress.error) == (
        1,
        "Exited with code 1\nstderr:\nan errmsg",
    )


def test_job_warn_if_forgot_to_write_file(work, caplog):
    params = build_params("succeedbutforgettowritefile", "a")
    job = work.ensure_run(params)
    for progress in work.report_job_progress_until_completed(job):
        pass
    assert (progress.returncode, progress.error) == (
        -999,
        "Exited with code -999\nstderr:\n"
        "invalid program: it should have written to its output file",
    )
    assert caplog.record_tuples == [
        (
            "workqueue",
            logging.WARNING,
            "invalid program: it should have written to its output file",
        )
    ]


def test_job_warn_if_invalid_stdout(work, caplog):
    params = build_params("succeedbutwritejunktostdout", "a")
    job = work.ensure_run(params)
    for progress in work.report_job_progress_until_completed(job):
        pass
    assert (progress.returncode, progress.error) == (0, None)
    assert caplog.record_tuples[0] == (
        "workqueue",
        logging.WARNING,
        "invalid program: stdout must look like '0.25\\tmessage'; got 'not progress'",
    )


def test_concurrent_jobs(work):
    params = [build_params("succeed", str(i)) for i in range(10)]
    jobs = [work.ensure_run(p) for p in params]
    for job in jobs:
        for progress in work.report_job_progress_until_completed(job):
            pass
        assert (progress.returncode, progress.error) == (0, None)


def test_n_ahead_in_queue(work):
    # 2 slow jobs. They'll finish eventually.
    work.ensure_run(build_params("sleephalfsecond", "1")),
    work.ensure_run(build_params("sleephalfsecond", "2")),
    # 2 fast jobs, then _our_ job, then 1 fast job
    work.ensure_run(build_params("succeed", "3")),
    work.ensure_run(build_params("succeed", "4")),
    job = work.ensure_run(build_params("succeed", "us"))
    work.ensure_run(build_params("succeed", "6")),
    # ... looking at the list, you can see we're #5 -- meaning 4 ahead

    progresses = list(work.report_job_progress_until_completed(job))
    assert progresses[0] == Progress(4)
    assert progresses[-1] == Progress(fraction=1.0, returncode=0, error=None)


def test_progress_message(work):
    job = work.ensure_run(build_params("writeprogress", "0.2N0.3ThiN0.9Tbye"))
    progresses = list(work.report_job_progress_until_completed(job))
    assert Progress(fraction=0.2, message=None) in progresses
    assert Progress(fraction=0.3, message="hi") in progresses
    assert Progress(fraction=0.9, message="bye") in progresses


def simulate_a_program(args: List[str]) -> None:
    """
    The "helper-program" aspect of this Python file.

    pytest imports this file for its tests. The _tests_ actually run this
    program (through __main__) to test things.
    """
    _, server, document_set_id, api_token, output_path = args

    if document_set_id == "succeed":
        Path(output_path).write_bytes(b"data")
        sys.exit(0)
    elif document_set_id == "fail":
        sys.stderr.write(api_token)
        sys.exit(1)
    elif document_set_id == "succeedbutforgettowritefile":
        sys.exit(0)
    elif document_set_id == "succeedbutwritejunktostdout":
        sys.stdout.write("not progress\n")
        sys.stdout.flush()
        time.sleep(0.5)  # make sure the progress poll sees this line
        Path(output_path).write_bytes(b"data")
        sys.exit(0)
    elif document_set_id == "sleephalfsecond":
        time.sleep(0.5)
        Path(output_path).write_bytes(b"data")
        sys.exit(0)
    elif document_set_id == "writeprogress":
        for line in api_token.split("N"):
            sys.stdout.write(line.replace("T", "\t") + "\n")
            sys.stdout.flush()
            time.sleep(1)
        Path(output_path).write_bytes(b"data")
        sys.exit(0)

    sys.stderr.write("invalid document_set_id: %r\n" % document_set_id)
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise RuntimeError(
            "You probably mean to be running this with pytest. "
            "The '__main__' functionality is a helper script that simulates "
            "a slow-running program."
        )
    simulate_a_program(sys.argv)

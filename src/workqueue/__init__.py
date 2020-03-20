import logging
import os
import selectors
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, NamedTuple, Optional

logger = logging.getLogger(__name__)
POLL_INTERVAL = 0.25  # seconds


class OverviewViewParams(NamedTuple):
    server: str
    """URL for Overview API server. e.g.: `https://www.overviewdocs.com`"""

    document_set_id: str
    """DocumentSet ID on Overview API server."""

    api_token: str
    """Token granting access to document set on Overview API server."""


class Progress(NamedTuple):
    """
    A snapshot in time of a Job's status.
    """

    n_ahead_in_queue: int = 0
    """Number of jobs ahead of the requested one (0 means 'this one is running')."""

    fraction: float = 0.0
    """Value from 0.0 to 1.0 indicating how far along this job is."""

    message: Optional[str] = None
    """Message that program provided alongside fraction."""

    returncode: Optional[int] = None
    """0 if job completed successfully; non-0 if it completed unsuccessfully."""

    error: Optional[str] = None
    """Error message, if `returncode` is set."""


def _run_and_log_exceptions(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Exception in workqueue module")
        raise


@dataclass
class Job:
    params: OverviewViewParams

    was_started: threading.Event = field(default_factory=threading.Event)

    last_stdout_line: Optional[bytes] = None
    """
    Last line of progress from subprocess.

    e.g., "0.23" or "0.23\tworking...".

    Do not read this until .was_started.is_set().
    """

    was_completed: threading.Event = field(default_factory=threading.Event)
    """When set, job is completed."""

    returncode: Optional[int] = None
    """
    Process returncode. 0 means success.

    Do not read this until .was_completed.is_set().
    """

    stderr: Optional[bytes] = None
    """
    Process stderr (in binary).

    Do not read this until .was_completed.is_set().
    """

    @property
    def current_progress(self) -> Progress:
        """
        Progress, if the job is running or completed. Otherwise 0.0.
        """
        if self.returncode is not None:
            if self.returncode == 0:
                error = None
            else:
                stderr = self.stderr.decode("utf-8", errors="replace")
                error = f"Exited with code {self.returncode}\nstderr:\n{stderr}"
            return Progress(fraction=1.0, returncode=self.returncode, error=error)

        if self.last_stdout_line is not None:
            parts = self.last_stdout_line.decode("utf-8", errors="replace").split(
                "\t", 1
            )
            try:
                fraction = float(parts[0])
                if fraction < 0.0 or fraction > 1.0:
                    raise ValueError(
                        "Fraction must be between 0.0 and 1.0; got %f" % fraction
                    )
            except ValueError:
                logger.warning(
                    "invalid program: stdout must look like '0.25\\tmessage'; got %r",
                    self.last_stdout_line.decode("utf-8", errors="replace"),
                )
                return Progress()

            if len(parts) == 2:
                message = parts[1]
            else:
                message = None

            return Progress(fraction=fraction, message=message)

        return Progress()


@dataclass
class State:
    """
    Currently running and pending jobs.

    This is not thread-safe. Callers must coordinate using a lock.
    """

    pending: Dict[OverviewViewParams, Job] = field(default_factory=dict)
    """Unstarted Jobs. Used for querying status."""

    running: Dict[OverviewViewParams, Job] = field(default_factory=dict)
    """Started, not-yet-completed jobs."""


@dataclass(frozen=True)
class WorkQueue:
    """
    Debouncer of huge jobs.

    We never run two jobs with the same `params`.

    To the caller, this monolithic object can be used as follows:

        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from workqueue import WorkQueue, OverviewViewParams

        # once, ever:
        work = WorkQueue(
            program_path=Path(__file__).parent / "do_work.py",
            executor=ThreadPoolExecutor(2, thread_name_prefix="my-work")
        )

        # and once per, say, HTTP request in our hypothetical HTTP framework:
        def serve(req, res):
            params = OverviewViewParams(
                req.query_string["server"],
                req.query_string["document_set_id"],
                req.auth_header["username"],  # api_token
            )
            maybe_job = work.ensure_run(params)
            if job is None:
                # the job was already finished with these params.
                res.send(204)  # No Content
                return
            else:
                res.send(200)  # OK (we presume -- gotta send a header!)
                res.send_header("Content-Type", "text/plain; charset="utf-8")
                for progress in work.report_job_progress_until_completed(job):
                    # Simple logic: send one line of progress info.
                    #
                    # Better logic would be to "debounce": don't send _every_
                    # event, but only send events when the HTTP send buffer is
                    # empty. This will get events to the user sooner.
                    res.send(json.dumps(progress._asdict()).encode("utf-8"))
                # Now the job is completed.
                return

    If a job completes with an error no state will be stored. The next call to
    `.ensure_run()` will re-start from scratch.

    This class can be used as a context manager:

        with WorkQueue(
            program_path=Path(__file__).parent / "do_work.py",
            executor=ThreadPoolExecutor(2, thread_name_prefix="my-work")
        ) as work:
            # ...
            # and when we exit, the ThreadPoolExecutor will be shut down.
    """

    program_path: Path
    """
    Path to a program with 4 positional params. Run as:

        /path/to/program server document_set_id api_token output_path

    The program must:

        * Write only progress events to stdout. A progress event looks
          like "0.24\n" or "0.24\tdoing something...\n".
        * Exit with returncode 0 on success and write to `output_path`.
        * Exit with returncode !=0 on failure, optioally writing to stderr.
    """

    executor: ThreadPoolExecutor

    storage_dir: Path
    """
    Path where we store data.
    """

    state_lock: threading.Lock = field(default_factory=threading.Lock)
    state: State = field(default_factory=State)

    def _run_one_job(self, job) -> None:
        """
        Called by self.executor.

        Execute one job, updating state and job.current_progress.
        """
        destination_path = self.destination_path_for_params(job.params)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        # Set to running
        with self.state_lock:
            del self.state.pending[job.params]
            self.state.running[job.params] = job
            job.was_started.set()

        # Run the subprocess. Sets job.last_stdout_line repeatedly, then
        # job.stderr and job.returncode
        with tempfile.NamedTemporaryFile(
            dir=self.storage_dir, prefix="building-model-", suffix=".tmp"
        ) as tf:
            with subprocess.Popen(
                [
                    self.program_path,
                    job.params.server,
                    job.params.document_set_id,
                    job.params.api_token,
                    tf.name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # so progress reports aren't delayed
            ) as popen:
                # Read from both stdout and stderr at the same time. This
                # requires non-blocking reads and selectors. (If we don't do
                # this, we'd need to read with threads ... or we'd get deadlock
                # when a buffer fills.)
                #
                # Primer: to run a process, we must:
                #
                # 1. Start it (subprocess.Popen())
                # 2. Read from its stdout and stderr as they get filled. (If we don't
                #    read, the process' writes will stall forever.)
                # 3. Read until its stdout and stderr are closed.
                # 4. wait() to get its retval. (subprocess.Popen.wait().)
                stdout_buf = b""
                stderr = []
                with selectors.DefaultSelector() as selector:
                    selector.register(popen.stdout.fileno(), selectors.EVENT_READ)
                    selector.register(popen.stderr.fileno(), selectors.EVENT_READ)

                    while selector.get_map():
                        ready = selector.select()
                        for key, events in ready:
                            chunk = os.read(key.fd, 32768)
                            if not chunk:
                                # The subprocess closed this fd (stdout/stderr).
                                #
                                # This typically happens when the subprocess exits.
                                selector.unregister(key.fd)
                            else:
                                # We just read a chunk.
                                if key.fd == popen.stdout.fileno():
                                    # stdout: maintain a buffer of half-finished lines;
                                    # write the last-finished line to
                                    # job.last_stdout_line.
                                    stdout_buf += chunk
                                    while b"\n" in stdout_buf:
                                        (
                                            job.last_stdout_line,
                                            stdout_buf,
                                        ) = stdout_buf.split(b"\n", 1)
                                elif key.fd == popen.stderr.fileno():
                                    # stderr: append to our (infinite) buffer.
                                    stderr.append(chunk)
                        # ... and loop, until we've removed everything from `selector`,
                        # meaning the subprocess closed its stdout+stderr.

                job.returncode = popen.wait()
                job.stderr = b"".join(stderr)

            if job.returncode == 0:
                if os.stat(tf.name).st_size == 0:
                    # Assume the file is empty because the script neglected to
                    # write to it -- erroneously.
                    job.returncode = -999  # clearly not a POSIX returncode
                    job.stderr = (
                        b"invalid program: it should have written to its output file"
                    )
                    logger.warning(
                        "invalid program: it should have written to its output file"
                    )
                else:
                    # Rename tempfile to its final resting place.
                    #
                    # Hard-link so tempfile.NamedTemporaryFile() can unlink its
                    # own handle without error ... and to ensure we're atomic.
                    os.link(tf.name, destination_path)

        # Set to completed
        with self.state_lock:
            del self.state.running[job.params]
            job.was_completed.set()

    def destination_path_for_params(self, params: OverviewViewParams) -> Path:
        return (
            self.storage_dir
            / params.server.split("/")[2]
            / params.document_set_id
            / f"{params.api_token}.out"
        )

    def ensure_run(self, params: OverviewViewParams) -> Optional[Job]:
        """
        Ensure a Job has been queued or completed with params `params`.

        Return `None` if we know the Job has been fully completed.

        Otherwise, return a new or existing `Job`.
        """
        with self.state_lock:
            # Since we're multi-threaded, we can't tell how far along the
            # _actual_ job is. We only have "bounds" on how far along it is.
            # For instance: a job in `state.running` has _started_ executing,
            # but we don't know whether it's completed.

            # running (or completed but still in RAM)?
            try:
                return self.state.running[params]
            except KeyError:
                pass

            # pending (or running/completed but still in RAM)?
            try:
                return self.state.pending[params]
            except KeyError:
                pass

            # completed and deleted from RAM?
            if self.destination_path_for_params(params).exists():
                return None  # completed

            # ... never seen before! Spawn it now
            job = Job(params)
            self.state.pending[params] = job

        self.executor.submit(_run_and_log_exceptions, self._run_one_job, job)
        return job

    def report_job_progress_until_completed(self, job: Job) -> Iterator[Progress]:
        """
        Generate `Progress` events; raise `StopIteration` when the Job is done.
        """
        while not job.was_started.wait(POLL_INTERVAL):
            # Unstarted.
            with self.state_lock:
                # Avoid a race and check that job is still in state.pending. If
                # it isn't, job.was_started.is_set() will be true so our loop
                # will end.
                if job.params in self.state.pending:
                    # dict is ordered by insertion time, so
                    # self.state.pending.values() is ordered.
                    n_pending_before = next(
                        i for i, j in enumerate(self.state.pending.values()) if j == job
                    )
                    progress = Progress(
                        n_ahead_in_queue=len(self.state.running) + n_pending_before
                    )
                else:
                    progress = None
                    assert job.was_started.is_set()  # this is the race
            # release lock, then yield
            if progress:
                yield progress

        yield job.current_progress

        while not job.was_completed.wait(POLL_INTERVAL):
            # A race is no problem. job.current_progress can't fail.
            yield job.current_progress

        # Yield one final progress report -- the "completed" one.
        yield job.current_progress

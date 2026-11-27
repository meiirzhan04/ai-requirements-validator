"""Deliver backend progress to the Streamlit script that owns the status container."""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
from queue import Empty, Queue
from time import monotonic

import streamlit as st

from src import pipeline
from src.contracts import ProgressEvent, ValidationReport

logger = logging.getLogger(__name__)


@dataclass
class AnalysisJob:
    started: float = field(default_factory=monotonic)
    events: Queue = field(default_factory=Queue)
    history: list[tuple[float, ProgressEvent]] = field(default_factory=list)
    result: Future[ValidationReport] | None = None


def pending_job(key):
    return st.session_state.setdefault("_analysis_jobs", {}).get(key)


def run_with_progress(key, document, settings, tracker):
    jobs = st.session_state.setdefault("_analysis_jobs", {})
    job = jobs.get(key)
    if job is None:
        job = AnalysisJob()

        def run():
            try:
                return pipeline.run_pipeline(
                    document, settings, on_progress=lambda event: job.events.put((monotonic(), event))
                )
            except Exception as exc:
                logger.error("Analysis worker failed (%s)", type(exc).__name__)
                raise

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
        job.result = executor.submit(run)
        executor.shutdown(wait=False)
        jobs[key] = job

    tracker.start_at(job.started)
    for timestamp, event in job.history:
        tracker.handle(event, timestamp)
    while True:
        # Access the session through Streamlit so an interrupted script yields on rerun.
        if pending_job(key) is not job:
            st.stop()
        finished = job.result.done()
        try:
            timestamp, event = job.events.get(timeout=0 if finished else 0.1)
        except Empty:
            if finished:
                break
            continue
        job.history.append((timestamp, event))
        tracker.handle(event, timestamp)
    return job.result.result()


def finish_job(key):
    st.session_state.get("_analysis_jobs", {}).pop(key, None)

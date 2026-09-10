"""The orchestrator's own activities.

What: read a runner envelope from the object store; write the session summary.
Why: the workflow must not read S3 itself, and an envelope must not ride
inside an activity that also runs a command. Reading it here keeps payloads
bounded (the runner caps its lists; this refuses anything past 256 KiB) and
keeps the object store out of the workflow sandbox. The summary stands in for
opening a pull request: everything a reviewer or a dashboard needs, in one
object with a predictable key.
Production: the same two activities, plus a presign for the patch.
"""

import asyncio
import json
from dataclasses import asdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sandbox.objectstore import ObjectStore
from sandbox.orchestrator.steps import (
    MAX_ENVELOPE_BYTES,
    PUBLISH_SUMMARY,
    READ_ENVELOPE,
    EnvelopeRequest,
    SessionSummary,
    SessionUris,
)
from sandbox.timeutil import now_iso


class OrchestratorActivities:
    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def all(self) -> list:
        return [self.read_envelope, self.publish_summary]

    @activity.defn(name=READ_ENVELOPE)
    async def read_envelope(self, req: EnvelopeRequest) -> dict:
        data = await asyncio.to_thread(self.store.get_bytes, req.uri)
        if len(data) > MAX_ENVELOPE_BYTES:
            raise ApplicationError(
                f"envelope {req.uri} is {len(data)} bytes; the cap is {MAX_ENVELOPE_BYTES}",
                non_retryable=True,
            )
        envelope = json.loads(data)
        if envelope.get("kind") != req.kind:
            raise ApplicationError(
                f"envelope {req.uri} is a {envelope.get('kind')!r} envelope, expected {req.kind!r}",
                non_retryable=True,
            )
        return envelope

    @activity.defn(name=PUBLISH_SUMMARY)
    async def publish_summary(self, summary: SessionSummary) -> str:
        uri = SessionUris(summary.session_id).summary
        await asyncio.to_thread(
            self.store.put_json, uri, {**asdict(summary), "finished_at": now_iso()}
        )
        return uri

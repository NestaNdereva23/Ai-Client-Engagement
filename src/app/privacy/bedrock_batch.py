"""Bedrock's batch inference: S3 input/output plus CreateModelInvocationJob,
instead of Anthropic's direct Message Batches API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import boto3

BEDROCK_BATCH_MIN_RECORDS = 100

_BEDROCK_BATCH_ENDED_STATUSES = {"Completed", "PartiallyCompleted", "Failed", "Stopped", "Expired"}


class BedrockBatchClient:
    """Same .messages.batches.create/retrieve/results shape as the Anthropic
    SDK, backed by S3 + Bedrock's CreateModelInvocationJob instead.
    """

    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        role_arn: str,
        model_id: str,
        bedrock_client: Any | None = None,
        s3_client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._role_arn = role_arn
        self._model_id = model_id
        self._bedrock = bedrock_client or boto3.client("bedrock", region_name=region)
        self._s3 = s3_client or boto3.client("s3", region_name=region)
        self.messages = SimpleNamespace(batches=self)

    def create(self, *, requests: list[dict[str, Any]], batch_id: str) -> Any:
        prefix = f"ace-batch/{batch_id}"
        body = "\n".join(json.dumps(r) for r in requests).encode("utf-8")
        self._s3.put_object(Bucket=self._bucket, Key=f"{prefix}/input.jsonl", Body=body)
        response = self._bedrock.create_model_invocation_job(
            jobName=f"ace-{batch_id}",
            roleArn=self._role_arn,
            modelId=self._model_id,
            inputDataConfig={
                "s3InputDataConfig": {"s3Uri": f"s3://{self._bucket}/{prefix}/input.jsonl"}
            },
            outputDataConfig={
                "s3OutputDataConfig": {"s3Uri": f"s3://{self._bucket}/{prefix}/output/"}
            },
        )
        return SimpleNamespace(id=response["jobArn"])

    def retrieve(self, provider_batch_id: str) -> Any:
        job = self._bedrock.get_model_invocation_job(jobIdentifier=provider_batch_id)
        status = "ended" if job["status"] in _BEDROCK_BATCH_ENDED_STATUSES else "in_progress"
        counts = SimpleNamespace(
            succeeded=job.get("successRecordCount", 0),
            errored=job.get("errorRecordCount", 0),
            canceled=0,
            expired=0,
        )
        return SimpleNamespace(processing_status=status, request_counts=counts)

    def results(self, provider_batch_id: str) -> Any:
        job = self._bedrock.get_model_invocation_job(jobIdentifier=provider_batch_id)
        output_uri = job["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]
        bucket, prefix = output_uri.removeprefix("s3://").split("/", 1)
        listing = self._s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        out_key = next(o["Key"] for o in listing["Contents"] if o["Key"].endswith(".jsonl.out"))
        body = self._s3.get_object(Bucket=bucket, Key=out_key)["Body"].read().decode("utf-8")
        return (_bedrock_result(json.loads(line)) for line in body.splitlines() if line.strip())


def _bedrock_result(record: dict[str, Any]) -> Any:
    if "error" in record:
        error = record["error"]
        outcome = f"error {error.get('errorCode')}: {error.get('errorMessage')}"
        return SimpleNamespace(custom_id=record["recordId"], result=SimpleNamespace(type=outcome))
    output = record["modelOutput"]
    content = [
        SimpleNamespace(type=block["type"], text=block.get("text", ""))
        for block in output.get("content", [])
    ]
    usage = output.get("usage") or {}
    message = SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")
        ),
        stop_reason=output.get("stop_reason"),
    )
    return SimpleNamespace(
        custom_id=record["recordId"], result=SimpleNamespace(type="succeeded", message=message)
    )


# No Bedrock minimum-record-count restriction applies here -- that number
# (BEDROCK_BATCH_MIN_RECORDS) is specific to AWS's real CreateModelInvocationJob
# batch API, which SyncLoopBatchClient never calls.
_SYNC_BATCH_DIR = Path(os.environ.get("SYNC_BATCH_DIR", "/home/ai/ace/var/sync_batches"))

# Same placeholder as AnthropicLLMClient._generate_kwargs: the Anthropic API
# (direct or via AnthropicBedrock) rejects a blank user message outright, and
# an empty turn is a real, legitimate case -- the privacy boundary can
# allow-list zero facts for a given client/angle while the system prompt
# alone still carries everything needed.
_EMPTY_USER_PLACEHOLDER = "(no additional client facts for this draft)"


class SyncLoopBatchClient:
    """Same .messages.batches.create/retrieve/results shape as
    BedrockBatchClient, but runs every record as an ordinary synchronous
    Bedrock call in a loop instead of AWS's CreateModelInvocationJob --
    no S3 bucket or IAM role needed, at the cost of being slower and
    missing batch-rate pricing. create() does all the work up front and
    writes the finished result to a small local file keyed by batch_id
    (mirroring BedrockBatchClient's S3 object), so a later, possibly
    different worker process's retrieve()/results() call still finds it.
    """

    def __init__(
        self,
        *,
        region: str,
        model_id: str,
        client: Any | None = None,
        result_dir: Path | None = None,
    ) -> None:
        self._model_id = model_id
        self._client = client or anthropic.AnthropicBedrock(aws_region=region)
        self._dir = result_dir or _SYNC_BATCH_DIR
        self.messages = SimpleNamespace(batches=self)

    def _result_path(self, batch_id: str) -> Path:
        return self._dir / f"{batch_id}.jsonl.out"

    def create(self, *, requests: list[dict[str, Any]], batch_id: str) -> Any:
        records: list[dict[str, Any]] = []
        for request in requests:
            record_id = request["recordId"]
            model_input = dict(request["modelInput"])
            model_input.pop("anthropic_version", None)  # only meaningful in the raw JSONL format
            messages = [
                {
                    "role": m["role"],
                    "content": (
                        (m["content"] or _EMPTY_USER_PLACEHOLDER)
                        if isinstance(m["content"], str)
                        else m["content"]
                    ),
                }
                for m in model_input["messages"]
            ]
            try:
                response = self._client.messages.create(
                    model=self._model_id,
                    max_tokens=model_input["max_tokens"],
                    system=model_input["system"],
                    messages=messages,
                    **(
                        {"temperature": model_input["temperature"]}
                        if "temperature" in model_input
                        else {}
                    ),
                )
            except anthropic.APIError as exc:
                records.append(
                    {
                        "recordId": record_id,
                        "error": {"errorCode": "APIError", "errorMessage": str(exc)},
                    }
                )
                continue
            records.append(
                {
                    "recordId": record_id,
                    "modelOutput": {
                        "content": [
                            {"type": block.type, "text": getattr(block, "text", "")}
                            for block in response.content
                        ],
                        "usage": {
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                        },
                        "stop_reason": response.stop_reason,
                    },
                }
            )

        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._result_path(batch_id)
        path.write_text("\n".join(json.dumps(r) for r in records))
        return SimpleNamespace(id=batch_id)

    def retrieve(self, provider_batch_id: str) -> Any:
        records = self._read_records(provider_batch_id)
        succeeded = sum(1 for r in records if "error" not in r)
        errored = sum(1 for r in records if "error" in r)
        counts = SimpleNamespace(succeeded=succeeded, errored=errored, canceled=0, expired=0)
        return SimpleNamespace(processing_status="ended", request_counts=counts)

    def results(self, provider_batch_id: str) -> Any:
        return (_bedrock_result(r) for r in self._read_records(provider_batch_id))

    def _read_records(self, provider_batch_id: str) -> list[dict[str, Any]]:
        path = self._result_path(provider_batch_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

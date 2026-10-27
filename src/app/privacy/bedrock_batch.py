"""Bedrock's batch inference: S3 input/output plus CreateModelInvocationJob,
instead of Anthropic's direct Message Batches API.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

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

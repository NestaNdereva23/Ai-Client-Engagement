"""BedrockBatchClient: S3 input/output and job polling stand in for
Anthropic's direct Message Batches API, behind the same create/retrieve/
results shape -- these run against fake bedrock/s3 clients, no network call.
"""

from __future__ import annotations

import io
import json

from app.privacy.bedrock_batch import BedrockBatchClient


class FakeBedrockControlPlane:
    def __init__(self, status: str = "Completed") -> None:
        self.status = status
        self.create_calls: list[dict] = []

    def create_model_invocation_job(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"jobArn": "arn:aws:bedrock:us-east-1:123456789012:model-invocation-job/abc123"}

    def get_model_invocation_job(self, *, jobIdentifier):
        return {
            "status": self.status,
            "successRecordCount": 1,
            "errorRecordCount": 1,
            "outputDataConfig": {
                "s3OutputDataConfig": {"s3Uri": "s3://ace-batch-bucket/ace-batch/b1/output/"}
            },
        }


class FakeS3:
    def __init__(self, output_body: bytes = b"") -> None:
        self.put_calls: list[dict] = []
        self._output_body = output_body

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)

    def list_objects_v2(self, *, Bucket, Prefix):
        return {
            "Contents": [
                {"Key": f"{Prefix}output.jsonl.out"},
                {"Key": f"{Prefix}manifest.json.out"},
            ]
        }

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self._output_body)}


def test_bedrock_batch_client_round_trip() -> None:
    output_lines = [
        json.dumps(
            {
                "recordId": "run-1",
                "modelOutput": {
                    "content": [{"type": "text", "text": "Hi there"}],
                    "usage": {"input_tokens": 12, "output_tokens": 34},
                    "stop_reason": "end_turn",
                },
            }
        ),
        json.dumps({"recordId": "run-2", "error": {"errorCode": 400, "errorMessage": "bad input"}}),
    ]
    fake_bedrock = FakeBedrockControlPlane()
    fake_s3 = FakeS3("\n".join(output_lines).encode("utf-8"))
    client = BedrockBatchClient(
        region="us-east-1",
        bucket="ace-batch-bucket",
        role_arn="arn:aws:iam::123456789012:role/ace-batch",
        model_id="us.anthropic.claude-sonnet-4-6",
        bedrock_client=fake_bedrock,
        s3_client=fake_s3,
    )

    created = client.create(requests=[{"recordId": "run-1", "modelInput": {}}], batch_id="b1")
    assert created.id.endswith("abc123")
    assert fake_bedrock.create_calls[0]["modelId"] == "us.anthropic.claude-sonnet-4-6"
    assert fake_bedrock.create_calls[0]["roleArn"] == "arn:aws:iam::123456789012:role/ace-batch"
    assert fake_s3.put_calls[0]["Key"] == "ace-batch/b1/input.jsonl"

    status = client.retrieve(created.id)
    assert status.processing_status == "ended"
    assert status.request_counts.succeeded == 1
    assert status.request_counts.errored == 1

    results = list(client.results(created.id))
    assert results[0].custom_id == "run-1"
    assert results[0].result.type == "succeeded"
    assert results[0].result.message.content[0].text == "Hi there"
    assert results[0].result.message.usage.input_tokens == 12
    assert results[0].result.message.stop_reason == "end_turn"
    assert results[1].custom_id == "run-2"
    assert results[1].result.type != "succeeded"


def test_bedrock_batch_client_retrieve_reports_in_progress_before_a_terminal_status() -> None:
    client = BedrockBatchClient(
        region="us-east-1",
        bucket="ace-batch-bucket",
        role_arn="arn:aws:iam::123456789012:role/ace-batch",
        model_id="us.anthropic.claude-sonnet-4-6",
        bedrock_client=FakeBedrockControlPlane(status="InProgress"),
        s3_client=FakeS3(),
    )
    assert client.retrieve("job-1").processing_status == "in_progress"

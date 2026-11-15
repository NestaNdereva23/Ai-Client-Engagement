"""AWS keys kept in .env must reach the real environment for the AWS libraries."""

from __future__ import annotations

from app.config import Settings, export_aws_credentials


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        aws_access_key_id="key-from-dotenv",
        aws_secret_access_key="secret-from-dotenv",
    )


def test_keys_from_settings_reach_the_environment(monkeypatch) -> None:
    settings = _settings()
    env: dict[str, str] = {}
    monkeypatch.setattr("os.environ", env)

    export_aws_credentials(settings)

    assert env == {
        "AWS_ACCESS_KEY_ID": "key-from-dotenv",
        "AWS_SECRET_ACCESS_KEY": "secret-from-dotenv",
    }


def test_a_variable_already_set_is_not_overwritten(monkeypatch) -> None:
    settings = _settings()
    env = {"AWS_ACCESS_KEY_ID": "real-key"}
    monkeypatch.setattr("os.environ", env)

    export_aws_credentials(settings)

    assert env["AWS_ACCESS_KEY_ID"] == "real-key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "secret-from-dotenv"


def test_empty_settings_export_nothing(monkeypatch) -> None:
    settings = Settings(_env_file=None, aws_access_key_id="", aws_secret_access_key="")
    env: dict[str, str] = {}
    monkeypatch.setattr("os.environ", env)

    export_aws_credentials(settings)

    assert env == {}

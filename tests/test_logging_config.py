from __future__ import annotations

import logging

import structlog

from app.logging_config import configure_logging


def test_errors_log_without_local_values(capsys):
    configure_logging("INFO")
    logger = structlog.get_logger("secret_check")
    api_key = "sk-" + "never-printed"

    try:
        raise RuntimeError(f"boom {len(api_key)}")
    except RuntimeError:
        logger.exception("send_failed")

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "RuntimeError: boom" in output
    assert "never-printed" not in output
    logging.getLogger().handlers.clear()

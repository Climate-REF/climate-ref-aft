"""Shared test fixtures for the AFT integration tests."""

from collections.abc import Callable

import pytest
from click.testing import Result
from climate_ref import cli
from climate_ref.config import DiagnosticProviderConfig
from climate_ref_core.logging import remove_log_handler
from loguru import logger
from typer.testing import CliRunner

AFT_PROVIDERS = ["climate_ref_esmvaltool", "climate_ref_pmp", "climate_ref_ilamb"]


@pytest.fixture
def invoke_cli(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Result]:
    """
    Override upstream invoke_cli to drop mix_stderr (removed in typer 0.21+).

    This can be removed once climate-ref fixes the upstream conftest_plugin.
    """
    runner = CliRunner()

    def _invoke_cli(args: list[str], expected_exit_code: int = 0, always_log: bool = False) -> Result:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("COLUMNS", "200")

        result = runner.invoke(app=cli.app, args=args)

        if hasattr(logger, "default_handler_id"):
            remove_log_handler()

        if always_log or result.exit_code != expected_exit_code:
            print("## Command: ", " ".join(args))
            print("Exit code: ", result.exit_code)
            print("Command stdout")
            print(result.stdout)
            print("Command stderr")
            print(getattr(result, "stderr", ""))
            print("## Command end")

        if result.exit_code != expected_exit_code:
            if result.exception:
                raise result.exception
            raise ValueError(f"Expected exit code {expected_exit_code}, got {result.exit_code}")
        return result

    return _invoke_cli


@pytest.fixture
def config_cmip7_aft(config):
    """Configure the test environment to use the CMIP7 Assessment Fast Track providers."""
    config.diagnostic_providers = [DiagnosticProviderConfig(provider=provider) for provider in AFT_PROVIDERS]
    config.executor.executor = "climate_ref.executor.LocalExecutor"

    # Write the config to disk so it is used by the CLI
    config.save()

    return config

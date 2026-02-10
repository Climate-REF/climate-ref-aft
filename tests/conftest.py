"""
Shared test fixtures for the AFT integration tests.

These fixtures configure the test environment with all AFT diagnostic providers
and set up the CLI helpers needed for end-to-end testing.
"""

from __future__ import annotations

import pytest

from climate_ref.config import DiagnosticProviderConfig


@pytest.fixture
def config_cmip7_aft(config):
    """
    Configure the test environment to use the CMIP7 Assessment Fast Track providers.

    This overrides the default test config to include all three AFT providers
    (ESMValTool, PMP, ILAMB) and uses the local executor for parallelised execution.
    """
    config.diagnostic_providers = [
        DiagnosticProviderConfig(provider=provider)
        for provider in ["climate_ref_esmvaltool", "climate_ref_pmp", "climate_ref_ilamb"]
    ]
    config.executor.executor = "climate_ref.executor.LocalExecutor"

    # Write the config to disk so it is used by the CLI
    config.save()

    return config

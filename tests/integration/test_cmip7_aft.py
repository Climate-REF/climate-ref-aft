"""
CMIP7 Assessment Fast Track integration tests.

These tests verify that the full AFT stack works end-to-end:
all three diagnostic providers (ESMValTool, PMP, ILAMB) are discovered,
data is ingested, and diagnostics execute successfully.
"""

from __future__ import annotations

import platform
from collections.abc import Iterable

import pandas as pd
import pytest
from climate_ref.database import Database
from climate_ref.models import ExecutionGroup


def create_execution_dataframe(execution_groups: Iterable[ExecutionGroup]) -> pd.DataFrame:
    """
    Build a summary DataFrame from execution groups for test assertions.

    Parameters
    ----------
    execution_groups
        The execution groups to summarise

    Returns
    -------
        DataFrame with columns: diagnostic, provider, execution_id, execution_key,
        result_id, successful
    """
    data = []

    for group in execution_groups:
        metadata = {
            "diagnostic": group.diagnostic.slug,
            "provider": group.diagnostic.provider.slug,
            "execution_id": group.id,
            "execution_key": group.key,
        }

        if group.executions:
            result = group.executions[-1]
            metadata["result_id"] = result.id
            metadata["successful"] = result.successful

        data.append(metadata)

        print(metadata)

    return pd.DataFrame(data)


@pytest.mark.slow
def test_solve_cmip7_aft(
    sample_data_dir,
    config_cmip7_aft,
    invoke_cli,
    monkeypatch,
):
    """
    End-to-end test: ingest sample data and solve with all AFT providers.

    This test exercises the complete pipeline:
    1. Fetch PMP climatology data
    2. Ingest CMIP6 and obs4MIPs sample data
    3. Run the solver with --one-per-diagnostic
    4. Verify all three providers produced results
    """
    # Arm-based MacOS users will need to set the environment variable `MAMBA_PLATFORM=osx-64`
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        monkeypatch.setenv("MAMBA_PLATFORM", "osx-64")

    # The conda environments should already be created in the default location
    # See github CI integration test

    assert len(config_cmip7_aft.diagnostic_providers) == 3

    db = Database.from_config(config_cmip7_aft)

    invoke_cli(
        [
            "datasets",
            "fetch-data",
            "--registry",
            "pmp-climatology",
            "--output-directory",
            str(sample_data_dir / "pmp-climatology"),
        ]
    )

    # Ingest the sample data
    invoke_cli(["datasets", "ingest", "--source-type", "cmip6", str(sample_data_dir / "CMIP6")])
    invoke_cli(["datasets", "ingest", "--source-type", "obs4mips", str(sample_data_dir / "obs4REF")])
    invoke_cli(
        ["datasets", "ingest", "--source-type", "pmp-climatology", str(sample_data_dir / "pmp-climatology")]
    )

    # Solve
    # This will also create conda environments for the diagnostic providers
    # We always log the std out and stderr from the command as it is useful for debugging
    invoke_cli(["--verbose", "solve", "--one-per-diagnostic", "--timeout", f"{60 * 60}"], always_log=True)

    execution_groups = db.session.query(ExecutionGroup).all()
    df = create_execution_dataframe(execution_groups)

    print(df)

    # Check that all 3 diagnostic providers have been used
    assert set(df["provider"].unique()) == {"esmvaltool", "ilamb", "pmp"}

    # Check that some of the diagnostics have been marked successful
    assert df["successful"].any()


@pytest.mark.slow
def test_provider_discovery(config_cmip7_aft):
    """
    Verify that all AFT providers are discoverable via entry points.

    This is a critical test for the repo split: it confirms that providers
    installed from separate repositories are correctly discovered by
    the entry point mechanism.
    """
    import importlib.metadata

    entry_points = importlib.metadata.entry_points(group="climate-ref.providers")
    provider_names = {ep.name for ep in entry_points}

    # All three AFT providers must be discoverable
    assert "esmvaltool" in provider_names, f"ESMValTool provider not found. Available: {provider_names}"
    assert "pmp" in provider_names, f"PMP provider not found. Available: {provider_names}"
    assert "ilamb" in provider_names, f"ILAMB provider not found. Available: {provider_names}"

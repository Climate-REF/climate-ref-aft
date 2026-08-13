"""
CMIP7 Assessment Fast Track integration tests.

These tests verify that the full AFT stack works end-to-end:
all three diagnostic providers (ESMValTool, PMP, ILAMB) are discovered,
data is ingested, and diagnostics execute successfully.
"""

import importlib.metadata
import platform
from collections.abc import Iterable

import pandas as pd
import pytest
from climate_ref.database import Database
from climate_ref.models import ExecutionGroup

EXPECTED_PROVIDERS = {"esmvaltool", "pmp", "ilamb"}


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

    return pd.DataFrame(data)


@pytest.mark.slow
def test_solve_cmip7_aft(
    sample_data_dir,
    config_cmip7_aft,
    invoke_cli,
    monkeypatch,
):
    """Ingest sample data and solve with all AFT providers."""
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

    invoke_cli(["datasets", "ingest", "--source-type", "cmip6", str(sample_data_dir / "CMIP6")])
    invoke_cli(["datasets", "ingest", "--source-type", "obs4mips", str(sample_data_dir / "obs4REF")])
    invoke_cli(
        ["datasets", "ingest", "--source-type", "pmp-climatology", str(sample_data_dir / "pmp-climatology")]
    )

    # Solving also creates the conda environments for the diagnostic providers.
    # Always log stdout and stderr, because it is what makes a failure here debuggable.
    invoke_cli(["--verbose", "solve", "--one-per-diagnostic", "--timeout", f"{60 * 60}"], always_log=True)

    df = create_execution_dataframe(db.session.query(ExecutionGroup).all())
    print(df)

    assert set(df["provider"].unique()) == EXPECTED_PROVIDERS
    assert df["successful"].any()


def test_provider_discovery(config_cmip7_aft):
    """
    Verify that all AFT providers are discoverable via entry points.

    This confirms that providers installed from separate repositories are
    correctly discovered by the entry point mechanism.
    """
    entry_points = importlib.metadata.entry_points(group="climate-ref.providers")
    provider_names = {ep.name for ep in entry_points}

    missing = EXPECTED_PROVIDERS - provider_names
    assert not missing, f"providers not found: {missing}. Available: {provider_names}"

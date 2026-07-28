"""Render the Helm chart and assert on the resulting Kubernetes objects.

`helm lint` only checks that one values permutation parses.
These tests render the chart the way an operator would install it
and assert on the objects that come out,
so that template regressions surface in seconds rather than in a minikube job.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART = REPO_ROOT / "helm"

# helm/values.yaml defaults to ENVIRONMENT=production with an empty SECRET_KEY,
# which the ref.validateApiSecret guard rejects.
# Every render that is not testing the guard itself supplies a placeholder.
PLACEHOLDER_SECRET = "render-test-not-a-real-secret"  # noqa: S105

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


@pytest.fixture(scope="session", autouse=True)
def chart_dependencies():
    """Vendor the dragonfly subchart into helm/charts once per session."""
    subprocess.run(  # noqa: S603
        [shutil.which("helm"), "dependency", "build", str(CHART)],  # noqa: S607
        check=True,
        capture_output=True,
    )


def _render(*set_args: str, values: str | None = None) -> subprocess.CompletedProcess:
    """Run `helm template` and return the completed process without raising."""
    cmd = [shutil.which("helm"), "template", "test", str(CHART)]
    if values is not None:
        cmd += ["-f", str(REPO_ROOT / values)]
    for arg in set_args:
        cmd += ["--set", arg]
    return subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603


def render(*set_args: str, values: str | None = None) -> list[dict]:
    """Render the chart and return the Kubernetes objects it produced."""
    result = _render(*set_args, values=values)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def find(docs: list[dict], kind: str, name: str) -> dict:
    """Return the single object of `kind` whose metadata.name ends with `name`."""
    matches = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"].endswith(name)]
    assert len(matches) == 1, f"expected exactly one {kind} ending in {name!r}, got {len(matches)}"
    return matches[0]


VALUES_FILES = [
    None,
    "helm/ci/minimal-values.yaml",
    "helm/ci/gh-actions-values.yaml",
    "helm/local-test-values.yaml",
]


@pytest.mark.parametrize("values", VALUES_FILES)
def test_shipped_values_files_render(values):
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", values=values)
    assert docs, f"{values or 'helm/values.yaml'} rendered no objects"


PROVIDERS = ["orchestrator", "esmvaltool", "pmp", "ilamb"]


def _worker_args(docs: list[dict], provider: str) -> list[str]:
    deployment = find(docs, "Deployment", f"-{provider}")
    return deployment["spec"]["template"]["spec"]["containers"][0]["args"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_gets_a_worker_deployment(provider):
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    args = _worker_args(docs, provider)
    assert args[:4] == ["celery", "start-worker", "--loglevel", "DEBUG"]


def test_orchestrator_worker_is_not_scoped_to_a_provider():
    args = _worker_args(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), "orchestrator")
    assert "--provider" not in args


@pytest.mark.parametrize("provider", ["esmvaltool", "pmp", "ilamb"])
def test_diagnostic_workers_are_scoped_to_their_provider(provider):
    args = _worker_args(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), provider)
    assert args[args.index("--provider") + 1] == provider


def test_esmvaltool_worker_is_pinned_to_one_celery_child():
    # Each esmvaltool execution fans out via its own Dask cluster.
    # Extra Celery children multiply that footprint and OOM the node.
    args = _worker_args(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), "esmvaltool")
    assert "--concurrency=1" in args


def test_production_render_fails_without_a_secret_key():
    # helm/values.yaml ships ENVIRONMENT=production and an empty SECRET_KEY on purpose,
    # so a bare render must fail rather than deploy a guessable key.
    result = _render()
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_non_production_render_does_not_require_a_secret_key():
    docs = render("api.env.ENVIRONMENT=local")
    assert docs


def _provider_env(docs: list[dict], provider: str) -> dict:
    return find(docs, "Secret", f"-{provider}")["stringData"]


def test_provider_specific_env_does_not_leak_between_providers():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    assert "ESMVALTOOL_CONFIG_DIR" in _provider_env(docs, "esmvaltool")
    assert "ESMVALTOOL_CONFIG_DIR" not in _provider_env(docs, "pmp")
    assert _provider_env(docs, "pmp")["DASK_SCHEDULER"] == "synchronous"
    assert _provider_env(docs, "ilamb")["DASK_SCHEDULER"] == "synchronous"
    assert "DASK_SCHEDULER" not in _provider_env(docs, "orchestrator")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_provider_inherits_the_shared_defaults(provider):
    env = _provider_env(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), provider)
    assert env["HOME"] == "/tmp"  # noqa: S108
    assert env["REF_CONFIGURATION"] == "/ref"
    assert env["REF_EXECUTOR"] == "climate_ref_celery.executor.CeleryExecutor"


THREAD_CAP_VARS = [
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_provider_caps_the_numerical_backend_threads(provider):
    # Unbounded numpy/scipy thread pools scale with the host core count and
    # oversubscribe the CPUs on large nodes. See the upstream memory use guide.
    env = _provider_env(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), provider)
    for var in THREAD_CAP_VARS:
        assert env[var] == "4"


def test_threadpool_limit_can_be_tuned_per_provider():
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "defaults.threadpoolLimit=2",
        "providers.pmp.threadpoolLimit=8",
    )
    for var in THREAD_CAP_VARS:
        assert _provider_env(docs, "ilamb")[var] == "2"
        assert _provider_env(docs, "pmp")[var] == "8"


def test_an_explicit_env_var_wins_over_the_threadpool_limit():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "providers.pmp.env.OMP_NUM_THREADS=16")
    env = _provider_env(docs, "pmp")
    # --set parses the override as an int, hence the str() normalisation.
    assert str(env["OMP_NUM_THREADS"]) == "16"
    assert env["MKL_NUM_THREADS"] == "4"


def test_nulling_the_threadpool_limit_leaves_the_backends_unbounded():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "defaults.threadpoolLimit=null")
    for var in THREAD_CAP_VARS:
        assert var not in _provider_env(docs, "pmp")


def test_a_nulled_env_override_still_renders_the_provider_secret():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "providers.pmp.env=null")
    assert _provider_env(docs, "pmp")["OMP_NUM_THREADS"] == "4"


def test_esmvaltool_config_is_rendered_and_mounted():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    configmap = find(docs, "ConfigMap", "-esmvaltool-config")
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    assert config["max_parallel_tasks"] == 2

    pod = find(docs, "Deployment", "-esmvaltool")["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["esmvaltool-config"] == "/etc/esmvaltool"
    assert _provider_env(docs, "esmvaltool")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_esmvaltool_config_can_be_opted_out():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "providers.esmvaltool.config=null")
    assert not [d for d in docs if d["metadata"]["name"].endswith("-esmvaltool-config")]


def test_per_provider_values_override_the_shared_defaults():
    # helm/README.md promises that any default can be overridden per provider.
    # Sprig `merge` gives precedence to its first argument,
    # so the defaults used to win on every populated key.
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "providers.pmp.replicaCount=7",
        "providers.pmp.image.tag=override-tag",
        "providers.pmp.env.HOME=/override-home",
    )
    deployment = find(docs, "Deployment", "-pmp")
    assert deployment["spec"]["replicas"] == 7
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":override-tag")
    assert _provider_env(docs, "pmp")["HOME"] == "/override-home"


def test_overriding_one_provider_does_not_affect_the_others():
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "providers.pmp.replicaCount=7",
    )
    assert find(docs, "Deployment", "-ilamb")["spec"]["replicas"] == 1
    assert find(docs, "Deployment", "-orchestrator")["spec"]["replicas"] == 1


def test_external_broker_is_used_when_dragonfly_is_disabled():
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "dragonfly.enabled=false",
        "externalBroker.url=redis://broker.example:6379",
    )
    assert not [d for d in docs if d["metadata"]["name"].endswith("-dragonfly")]
    for provider in PROVIDERS:
        env = _provider_env(docs, provider)
        assert env["CELERY_BROKER_URL"] == "redis://broker.example:6379"
        assert env["CELERY_RESULT_BACKEND"] == "redis://broker.example:6379"


def test_disabling_dragonfly_without_a_broker_url_fails_with_a_clear_message():
    result = _render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "dragonfly.enabled=false")
    assert result.returncode != 0
    assert "externalBroker.url" in result.stderr


def test_flower_only_waits_for_dragonfly_when_dragonfly_is_deployed():
    with_dragonfly = find(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), "Deployment", "-flower")
    assert with_dragonfly["spec"]["template"]["spec"]["initContainers"]

    without = find(
        render(
            f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
            "dragonfly.enabled=false",
            "externalBroker.url=redis://broker.example:6379",
        ),
        "Deployment",
        "-flower",
    )
    assert "initContainers" not in without["spec"]["template"]["spec"]


def test_dragonfly_is_deployed_by_default():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    assert [d for d in docs if d["metadata"]["name"].endswith("-dragonfly")]
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"].startswith("redis://test-dragonfly")


def test_absent_dragonfly_keys_still_render_with_the_bundled_broker(tmp_path):
    # `helm upgrade --reuse-values` from a release predating externalBroker supplies
    # neither dragonfly.enabled nor externalBroker, because Helm replaces the chart
    # defaults with the old release's values. Strip both keys to reproduce that.
    chart = tmp_path / "helm"
    shutil.copytree(CHART, chart)
    values = chart / "values.yaml"
    text = values.read_text()
    assert "\ndragonfly:\n  enabled: true\n" in text
    text = text.replace("\ndragonfly:\n  enabled: true\n", "\ndragonfly:\n")
    assert '\nexternalBroker:\n  url: ""\n' in text
    text = text.replace('\nexternalBroker:\n  url: ""\n', "\n")
    values.write_text(text)

    result = subprocess.run(  # noqa: S603
        [
            shutil.which("helm"),
            "template",
            "test",
            str(chart),
            "--set",
            f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_explicitly_nulling_dragonfly_enabled_fails_with_a_clear_message():
    # An explicit null reads as disabled. That is defensible, but it must produce
    # the actionable message rather than a nil pointer dereference.
    result = _render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "dragonfly.enabled=null")
    assert result.returncode != 0
    assert "externalBroker.url" in result.stderr
    assert "nil pointer" not in result.stderr


def test_broker_url_containing_an_apostrophe_survives_yaml_serialisation():
    # toYaml quotes the template expression, then tpl injects the URL inside those quotes,
    # so an unescaped apostrophe in a broker password would break out of the scalar.
    url = "redis://:pa'ss@broker.example:6379/0"
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "dragonfly.enabled=false",
        f"externalBroker.url={url}",
    )
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == url


def test_local_test_values_do_not_hardcode_the_broker_service():
    # A hardcoded broker URL silently defeats externalBroker and breaks any
    # release whose name is not the one baked into the string.
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", values="helm/local-test-values.yaml")
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_no_deployment_references_a_service_account_that_is_not_created():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "providers.pmp.serviceAccount.create=null")
    created = {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}
    for deployment in [d for d in docs if d.get("kind") == "Deployment"]:
        wanted = deployment["spec"]["template"]["spec"].get("serviceAccountName")
        assert wanted is None or wanted in created, (
            f"{deployment['metadata']['name']} wants ServiceAccount {wanted!r}, which is not created"
        )


def test_every_deployment_uses_its_own_service_account_by_default():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    created = {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}
    for deployment in [d for d in docs if d.get("kind") == "Deployment"]:
        wanted = deployment["spec"]["template"]["spec"].get("serviceAccountName")
        assert wanted in created, f"{deployment['metadata']['name']} wants missing SA {wanted!r}"


def test_flower_waits_for_dragonfly_when_the_enabled_key_is_absent(tmp_path):
    # `ref.brokerUrl` treats an absent dragonfly.enabled as enabled, so the flower
    # init container must agree. Otherwise flower starts before its broker is ready.
    chart = tmp_path / "helm"
    shutil.copytree(CHART, chart)
    values = chart / "values.yaml"
    text = values.read_text()
    assert "\ndragonfly:\n  enabled: true\n" in text
    text = text.replace("\ndragonfly:\n  enabled: true\n", "\ndragonfly:\n")
    assert '\nexternalBroker:\n  url: ""\n' in text
    text = text.replace('\nexternalBroker:\n  url: ""\n', "\n")
    values.write_text(text)

    result = subprocess.run(  # noqa: S603
        [
            shutil.which("helm"),
            "template",
            "test",
            str(chart),
            "--set",
            f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    flower = find(docs, "Deployment", "-flower")
    assert flower["spec"]["template"]["spec"].get("initContainers"), (
        "flower must still wait for the bundled broker when dragonfly.enabled is absent"
    )
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_flower_skips_the_broker_wait_when_dragonfly_is_explicitly_disabled():
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "dragonfly.enabled=false",
        "externalBroker.url=redis://ext:6379",
    )
    flower = find(docs, "Deployment", "-flower")
    assert "initContainers" not in flower["spec"]["template"]["spec"]


def test_a_custom_service_account_name_is_the_one_that_gets_created():
    # The deployment templates prefer serviceAccount.name, so the ServiceAccount
    # templates must create it under that same name or the pod cannot be admitted.
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "providers.pmp.serviceAccount.name=my-sa",
        "api.serviceAccount.name=api-sa",
        "flower.serviceAccount.name=flower-sa",
    )
    created = {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}
    assert {"my-sa", "api-sa", "flower-sa"} <= created
    for deployment in [d for d in docs if d.get("kind") == "Deployment"]:
        wanted = deployment["spec"]["template"]["spec"].get("serviceAccountName")
        assert wanted is None or wanted in created, (
            f"{deployment['metadata']['name']} wants ServiceAccount {wanted!r}, which is not created"
        )

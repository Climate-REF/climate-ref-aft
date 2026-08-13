"""Render the Helm chart and assert on the resulting Kubernetes objects.

`helm lint` only checks that one values permutation parses.
These tests render the chart the way an operator would install it
and assert on the objects that come out,
so that template regressions surface in seconds rather than in a minikube job.
"""

import shutil
import subprocess
import tempfile
import tomllib
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


def _render(
    *set_args: str, values: str | None = None, values_data: dict | None = None
) -> subprocess.CompletedProcess:
    """Run `helm template` and return the completed process without raising."""
    cmd = [shutil.which("helm"), "template", "test", str(CHART)]
    if values is not None:
        cmd += ["-f", str(REPO_ROOT / values)]
    tmp: Path | None = None
    if values_data is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(values_data, f)
            tmp = Path(f.name)
        cmd += ["-f", str(tmp)]
    for arg in set_args:
        cmd += ["--set", arg]
    try:
        return subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    finally:
        if tmp is not None:
            tmp.unlink()


def render(*set_args: str, values: str | None = None, values_data: dict | None = None) -> list[dict]:
    """Render the chart and return the Kubernetes objects it produced."""
    result = _render(*set_args, values=values, values_data=values_data)
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


def _container(docs: list[dict], provider: str) -> dict:
    """Return the worker container of a provider's Deployment."""
    return find(docs, "Deployment", f"-{provider}")["spec"]["template"]["spec"]["containers"][0]


def _worker_args(docs: list[dict], provider: str) -> list[str]:
    return _container(docs, provider)["args"]


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


@pytest.mark.parametrize("provider", PROVIDERS)
def test_workers_have_no_liveness_probe_by_default(provider):
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    assert "livenessProbe" not in _container(docs, provider)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_liveness_probe_pings_the_workers_own_celery_node(provider):
    # A wedged worker still passes a process check,
    # so the probe has to exercise the consumer loop of this pod specifically.
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "defaults.livenessProbe.enabled=true",
        "defaults.livenessProbe.pingTimeoutSeconds=17",
    )
    command = _container(docs, provider)["livenessProbe"]["exec"]["command"][-1]
    assert "inspect ping" in command
    assert "-d celery@$(hostname)" in command
    assert "-t 17" in command


def test_liveness_probe_timings_are_configurable():
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "defaults.livenessProbe.enabled=true",
        "defaults.livenessProbe.initialDelaySeconds=11",
        "defaults.livenessProbe.periodSeconds=22",
        "defaults.livenessProbe.timeoutSeconds=33",
        "defaults.livenessProbe.failureThreshold=44",
    )
    probe = _container(docs, "pmp")["livenessProbe"]
    assert probe["initialDelaySeconds"] == 11
    assert probe["periodSeconds"] == 22
    assert probe["timeoutSeconds"] == 33
    assert probe["failureThreshold"] == 44


def test_liveness_probe_can_be_enabled_for_one_provider_only():
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "providers.ilamb.livenessProbe.enabled=true",
        "providers.ilamb.livenessProbe.periodSeconds=60",
    )
    assert _container(docs, "ilamb")["livenessProbe"]["periodSeconds"] == 60
    assert "livenessProbe" not in _container(docs, "pmp")


def test_liveness_probe_is_rejected_with_the_solo_pool():
    # The solo pool runs tasks in the main thread, so a busy worker cannot answer the ping
    # and every busy worker would be restarted.
    result = _render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "defaults.livenessProbe.enabled=true",
        "providers.pmp.extraArgs={--pool=solo}",
    )
    assert result.returncode != 0
    assert "providers.pmp: livenessProbe cannot be used with the solo pool" in result.stderr


def test_liveness_probe_guard_only_matches_the_pool_argument():
    # An extra argument that merely contains "solo" is not the solo pool.
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "defaults.livenessProbe.enabled=true",
        "providers.pmp.extraArgs={--queues=solo-tests}",
    )
    assert "livenessProbe" in _container(docs, "pmp")


def test_liveness_probe_can_be_disabled_for_one_provider_only():
    # mergeOverwrite must not swallow a per-provider `false`.
    docs = render(
        f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}",
        "defaults.livenessProbe.enabled=true",
        "providers.ilamb.livenessProbe.enabled=false",
    )
    assert "livenessProbe" not in _container(docs, "ilamb")
    assert "livenessProbe" in _container(docs, "pmp")


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


def _container_env(docs: list[dict], provider: str) -> dict:
    container = find(docs, "Deployment", f"-{provider}")["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_provider_specific_env_does_not_leak_between_providers():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    assert "ESMVALTOOL_CONFIG_DIR" in _container_env(docs, "esmvaltool")
    assert "ESMVALTOOL_CONFIG_DIR" not in _container_env(docs, "pmp")
    # The env var is template-managed container env, so no provider Secret carries it.
    for provider in PROVIDERS:
        assert "ESMVALTOOL_CONFIG_DIR" not in _provider_env(docs, provider)
    assert _provider_env(docs, "pmp")["DASK_SCHEDULER"] == "synchronous"
    assert _provider_env(docs, "ilamb")["DASK_SCHEDULER"] == "synchronous"
    assert "DASK_SCHEDULER" not in _provider_env(docs, "orchestrator")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_provider_inherits_the_shared_defaults(provider):
    env = _provider_env(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"), provider)
    assert env["HOME"] == "/tmp"  # noqa: S108
    assert env["REF_CONFIGURATION"] == "/ref"
    assert env["REF_EXECUTOR"] == "climate_ref_celery.executor.CeleryExecutor"


def test_esmvaltool_config_is_rendered_and_mounted():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    configmap = find(docs, "ConfigMap", "-esmvaltool-config")
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    assert config["max_parallel_tasks"] == 2

    pod = find(docs, "Deployment", "-esmvaltool")["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["esmvaltool-config"] == "/etc/esmvaltool"
    assert _container_env(docs, "esmvaltool")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_esmvaltool_config_dir_survives_an_env_override():
    # esmvalcore silently ignores a config dir that ESMVALTOOL_CONFIG_DIR does not point at,
    # so the env var must ride with the mount in the deployment template
    # rather than sit in values.yaml where an override can drop it. See issue #28.
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "providers.esmvaltool.env=null")
    assert _container_env(docs, "esmvaltool")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_esmvaltool_config_can_be_opted_out():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", "providers.esmvaltool.config=null")
    assert not [d for d in docs if d["metadata"]["name"].endswith("-esmvaltool-config")]
    pod = find(docs, "Deployment", "-esmvaltool")["spec"]["template"]["spec"]
    mounts = [m["name"] for m in pod["containers"][0].get("volumeMounts", [])]
    assert "esmvaltool-config" not in mounts
    assert "ESMVALTOOL_CONFIG_DIR" not in _container_env(docs, "esmvaltool")


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


SIZE_ROUTES = """
default = "{provider}"

[esmvaltool]
default = "esmvaltool-medium"
rules = [
  { match = "portrait-*", queue = "esmvaltool-large" },
]
"""

SIZE_VALUES = {
    "api": {"env": {"SECRET_KEY": PLACEHOLDER_SECRET}},
    "celeryRoutes": SIZE_ROUTES,
    "providers": {
        "esmvaltool": {"queues": ["esmvaltool", "esmvaltool-medium"]},
        "esmvaltool-large": {
            "provider": "esmvaltool",
            "queues": ["esmvaltool-large", "esmvaltool-medium"],
            "concurrency": 1,
            "config": {"max_parallel_tasks": 4},
        },
    },
}


def _worker_deployments(docs: list[dict]) -> list[dict]:
    workers = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        if container.get("args", [])[:2] == ["celery", "start-worker"]:
            workers.append(doc)
    return workers


def _consumed_queues(docs: list[dict]) -> set[str]:
    """The queues the rendered workers consume, mirroring start-worker's defaults."""
    consumed: set[str] = set()
    for worker in _worker_deployments(docs):
        args = worker["spec"]["template"]["spec"]["containers"][0]["args"]
        queues = None
        # A repeated --queues last-wins in celery's CLI, so keep the final occurrence.
        for arg in args:
            if arg.startswith("--queues="):
                queues = arg.removeprefix("--queues=").split(",")
        if queues is None:
            queues = [args[args.index("--provider") + 1]] if "--provider" in args else ["celery"]
        consumed.update(queues)
    return consumed


def _routed_queues(docs: list[dict]) -> set[str]:
    """Every queue the rendered routing table can produce."""
    tables = [
        d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-celery-routes")
    ]
    if not tables:
        return set()
    table = tomllib.loads(tables[0]["data"]["routes.toml"])
    top_default = table.get("default")

    routed: set[str] = set()
    for provider, entry in table.items():
        if provider == "default":
            continue
        assert isinstance(entry, dict), f"unexpected top-level routing table key {provider!r}"
        templates = [rule["queue"] for rule in entry.get("rules", [])]
        templates.append(entry.get("default") or top_default or "{provider}")
        routed.update(template.format(provider=provider) for template in templates)

    # The top-level default also applies to providers with no table entry.
    if top_default:
        for worker in _worker_deployments(docs):
            args = worker["spec"]["template"]["spec"]["containers"][0]["args"]
            if "--provider" in args:
                routed.add(top_default.format(provider=args[args.index("--provider") + 1]))
    return routed


def assert_no_orphan_queues(docs: list[dict]) -> None:
    """A task sent to a queue with no consumer waits forever and the solve blocks."""
    orphans = _routed_queues(docs) - _consumed_queues(docs)
    assert not orphans, f"routing table targets queues no worker consumes: {sorted(orphans)}"


def test_celery_routes_are_off_by_default():
    docs = render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}")
    assert not [d for d in docs if d["metadata"]["name"].endswith("-celery-routes")]
    assert "REF_CELERY_ROUTES" not in _container_env(docs, "esmvaltool")
    api = find(docs, "Deployment", "-api")["spec"]["template"]["spec"]["containers"][0]
    assert "REF_CELERY_ROUTES" not in {e["name"] for e in api.get("env", [])}


def test_celery_routes_render_as_valid_toml():
    docs = render(values_data=SIZE_VALUES)
    configmap = find(docs, "ConfigMap", "-celery-routes")
    table = tomllib.loads(configmap["data"]["routes.toml"])
    assert table["esmvaltool"]["rules"][0]["queue"] == "esmvaltool-large"


@pytest.mark.parametrize("component", ["-api", "-esmvaltool", "-orchestrator"])
def test_celery_routes_are_mounted_where_the_executor_can_run(component):
    # The API triggers solves, and an operator may exec `ref solve` in a worker pod,
    # so the table rides along everywhere.
    docs = render(values_data=SIZE_VALUES)
    container = find(docs, "Deployment", component)["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    path = env["REF_CELERY_ROUTES"]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert path == f"{mounts['celery-routes']}/routes.toml"


def test_worker_instance_can_run_a_provider_under_a_different_name():
    docs = render(values_data=SIZE_VALUES)
    args = _worker_args(docs, "esmvaltool-large")
    assert args[args.index("--provider") + 1] == "esmvaltool"
    assert "--queues=esmvaltool-large,esmvaltool-medium" in args
    # The queue override must follow the `--` separator to reach the celery worker.
    assert args.index("--") < args.index("--queues=esmvaltool-large,esmvaltool-medium")


def test_split_esmvaltool_instances_get_their_own_config():
    docs = render(values_data=SIZE_VALUES)
    large = yaml.safe_load(find(docs, "ConfigMap", "-esmvaltool-large-config")["data"]["config.yaml"])
    assert large["max_parallel_tasks"] == 4
    default = yaml.safe_load(find(docs, "ConfigMap", "-esmvaltool-config")["data"]["config.yaml"])
    assert default["max_parallel_tasks"] == 2
    assert _container_env(docs, "esmvaltool-large")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_size_values_route_to_no_orphan_queue():
    assert_no_orphan_queues(render(values_data=SIZE_VALUES))


@pytest.mark.parametrize("values", VALUES_FILES)
def test_shipped_values_route_to_no_orphan_queue(values):
    assert_no_orphan_queues(render(f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}", values=values))


def test_orphan_queue_guard_detects_a_queue_with_no_consumer():
    values = {
        "api": {"env": {"SECRET_KEY": PLACEHOLDER_SECRET}},
        "celeryRoutes": '[esmvaltool]\nrules = [ { match = "*", queue = "esmvaltool-huge" } ]\n',
    }
    with pytest.raises(AssertionError, match="esmvaltool-huge"):
        assert_no_orphan_queues(render(values_data=values))

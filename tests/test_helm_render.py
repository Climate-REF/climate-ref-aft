"""Render the Helm chart and assert on the resulting Kubernetes objects.

`helm lint` only checks that one values permutation parses.
These tests render the chart the way an operator would install it
and assert on the objects that come out,
so that template regressions surface in seconds rather than in a minikube job.
"""

import functools
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest
import yaml
from climate_ref_celery.routing import RoutingTable

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART = REPO_ROOT / "helm"

# helm/values.yaml defaults to ENVIRONMENT=production with an empty SECRET_KEY,
# which the ref.validateApiSecret guard rejects.
# Every render that is not testing the guard itself supplies a placeholder.
PLACEHOLDER_SECRET = "render-test-not-a-real-secret"  # noqa: S105
SECRET_ARG = f"api.env.SECRET_KEY={PLACEHOLDER_SECRET}"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


@pytest.fixture(scope="session", autouse=True)
def chart_dependencies():
    """Vendor the dragonfly subchart into helm/charts once per session."""
    subprocess.run(  # noqa: S603
        [shutil.which("helm"), "dependency", "build", str(CHART)],  # noqa: S607
        check=True,
        capture_output=True,
    )


@functools.cache
def _run_helm(cmd: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess:
    """Run helm once per distinct invocation, because rendering is deterministic for fixed inputs."""
    return subprocess.run(list(cmd), input=stdin, capture_output=True, text=True)  # noqa: S603


def _render(
    *set_args: str, values: str | dict | None = None, chart: Path = CHART
) -> subprocess.CompletedProcess:
    """Run `helm template` and return the completed process without raising.

    `values` is either a repo-relative path to a values file, or a dict piped in on stdin.
    """
    cmd = [shutil.which("helm"), "template", "test", str(chart)]
    stdin = None
    if isinstance(values, dict):
        cmd += ["-f", "-"]
        stdin = yaml.safe_dump(values)
    elif values is not None:
        cmd += ["-f", str(REPO_ROOT / values)]
    for arg in set_args:
        cmd += ["--set", arg]
    return _run_helm(tuple(cmd), stdin)


def render(*set_args: str, values: str | dict | None = None, chart: Path = CHART) -> list[dict]:
    """Render the chart and return the Kubernetes objects it produced."""
    result = _render(*set_args, values=values, chart=chart)
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
    docs = render(SECRET_ARG, values=values)
    assert docs, f"{values or 'helm/values.yaml'} rendered no objects"


PROVIDERS = ["orchestrator", "esmvaltool", "pmp", "ilamb"]


def _container(docs: list[dict], provider: str) -> dict:
    """Return the worker container of a provider's Deployment."""
    return find(docs, "Deployment", f"-{provider}")["spec"]["template"]["spec"]["containers"][0]


def _worker_args(docs: list[dict], provider: str) -> list[str]:
    return _container(docs, provider)["args"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_gets_a_worker_deployment(provider):
    docs = render(SECRET_ARG)
    args = _worker_args(docs, provider)
    assert args[:4] == ["celery", "start-worker", "--loglevel", "DEBUG"]


def test_orchestrator_worker_is_not_scoped_to_a_provider():
    args = _worker_args(render(SECRET_ARG), "orchestrator")
    assert "--provider" not in args


@pytest.mark.parametrize("provider", ["esmvaltool", "pmp", "ilamb"])
def test_diagnostic_workers_are_scoped_to_their_provider(provider):
    args = _worker_args(render(SECRET_ARG), provider)
    assert args[args.index("--provider") + 1] == provider


def test_esmvaltool_worker_is_pinned_to_one_celery_child():
    # Each esmvaltool execution fans out via its own Dask cluster.
    # Extra Celery children multiply that footprint and OOM the node.
    args = _worker_args(render(SECRET_ARG), "esmvaltool")
    assert "--concurrency=1" in args


@pytest.mark.parametrize("provider", PROVIDERS)
def test_workers_have_no_liveness_probe_by_default(provider):
    docs = render(SECRET_ARG)
    assert "livenessProbe" not in _container(docs, provider)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_liveness_probe_pings_the_workers_own_celery_node(provider):
    # A wedged worker still passes a process check,
    # so the probe has to exercise the consumer loop of this pod specifically.
    docs = render(
        SECRET_ARG,
        "defaults.livenessProbe.enabled=true",
        "defaults.livenessProbe.pingTimeoutSeconds=17",
    )
    command = _container(docs, provider)["livenessProbe"]["exec"]["command"][-1]
    assert "inspect ping" in command
    assert "-d celery@$(hostname)" in command
    assert "-t 17" in command


def test_liveness_probe_timings_are_configurable():
    docs = render(
        SECRET_ARG,
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
        SECRET_ARG,
        "providers.ilamb.livenessProbe.enabled=true",
        "providers.ilamb.livenessProbe.periodSeconds=60",
    )
    assert _container(docs, "ilamb")["livenessProbe"]["periodSeconds"] == 60
    assert "livenessProbe" not in _container(docs, "pmp")


def test_liveness_probe_is_rejected_with_the_solo_pool():
    # The solo pool runs tasks in the main thread, so a busy worker cannot answer the ping
    # and every busy worker would be restarted.
    result = _render(
        SECRET_ARG,
        "defaults.livenessProbe.enabled=true",
        "providers.pmp.extraArgs={--pool=solo}",
    )
    assert result.returncode != 0
    assert "providers.pmp: livenessProbe cannot be used with the solo pool" in result.stderr


def test_liveness_probe_guard_only_matches_the_pool_argument():
    # An extra argument that merely contains "solo" is not the solo pool.
    docs = render(
        SECRET_ARG,
        "defaults.livenessProbe.enabled=true",
        "providers.pmp.extraArgs={--queues=solo-tests}",
    )
    assert "livenessProbe" in _container(docs, "pmp")


def test_liveness_probe_can_be_disabled_for_one_provider_only():
    # mergeOverwrite must not swallow a per-provider `false`.
    docs = render(
        SECRET_ARG,
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
    docs = render(SECRET_ARG)
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
    env = _provider_env(render(SECRET_ARG), provider)
    assert env["HOME"] == "/tmp"  # noqa: S108
    assert env["REF_CONFIGURATION"] == "/ref"
    assert env["REF_EXECUTOR"] == "climate_ref_celery.executor.CeleryExecutor"


def test_esmvaltool_config_is_rendered_and_mounted():
    docs = render(SECRET_ARG)
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
    docs = render(SECRET_ARG, "providers.esmvaltool.env=null")
    assert _container_env(docs, "esmvaltool")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_esmvaltool_config_can_be_opted_out():
    docs = render(SECRET_ARG, "providers.esmvaltool.config=null")
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
        SECRET_ARG,
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
        SECRET_ARG,
        "providers.pmp.replicaCount=7",
    )
    assert find(docs, "Deployment", "-ilamb")["spec"]["replicas"] == 1
    assert find(docs, "Deployment", "-orchestrator")["spec"]["replicas"] == 1


def test_external_broker_is_used_when_dragonfly_is_disabled():
    docs = render(
        SECRET_ARG,
        "dragonfly.enabled=false",
        "externalBroker.url=redis://broker.example:6379",
    )
    assert not [d for d in docs if d["metadata"]["name"].endswith("-dragonfly")]
    for provider in PROVIDERS:
        env = _provider_env(docs, provider)
        assert env["CELERY_BROKER_URL"] == "redis://broker.example:6379"
        assert env["CELERY_RESULT_BACKEND"] == "redis://broker.example:6379"


def test_disabling_dragonfly_without_a_broker_url_fails_with_a_clear_message():
    result = _render(SECRET_ARG, "dragonfly.enabled=false")
    assert result.returncode != 0
    assert "externalBroker.url" in result.stderr


def test_flower_only_waits_for_dragonfly_when_dragonfly_is_deployed():
    with_dragonfly = find(render(SECRET_ARG), "Deployment", "-flower")
    assert with_dragonfly["spec"]["template"]["spec"]["initContainers"]

    without = find(
        render(
            SECRET_ARG,
            "dragonfly.enabled=false",
            "externalBroker.url=redis://broker.example:6379",
        ),
        "Deployment",
        "-flower",
    )
    assert "initContainers" not in without["spec"]["template"]["spec"]


def test_dragonfly_is_deployed_by_default():
    docs = render(SECRET_ARG)
    assert [d for d in docs if d["metadata"]["name"].endswith("-dragonfly")]
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"].startswith("redis://test-dragonfly")


@pytest.fixture
def chart_without_broker_keys(tmp_path) -> Path:
    """A copy of the chart with dragonfly.enabled and externalBroker stripped from values.yaml.

    `helm upgrade --reuse-values` from a release predating externalBroker supplies neither key,
    because Helm replaces the chart defaults with the old release's values.
    """
    chart = tmp_path / "helm"
    shutil.copytree(CHART, chart)
    values = chart / "values.yaml"
    text = values.read_text()
    for old, new in (
        ("\ndragonfly:\n  enabled: true\n", "\ndragonfly:\n"),
        ('\nexternalBroker:\n  url: ""\n', "\n"),
    ):
        assert old in text, f"values.yaml no longer contains {old!r}"
        text = text.replace(old, new)
    values.write_text(text)
    return chart


def test_absent_dragonfly_keys_still_render_with_the_bundled_broker(chart_without_broker_keys):
    docs = render(SECRET_ARG, chart=chart_without_broker_keys)
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_explicitly_nulling_dragonfly_enabled_fails_with_a_clear_message():
    # An explicit null reads as disabled. That is defensible, but it must produce
    # the actionable message rather than a nil pointer dereference.
    result = _render(SECRET_ARG, "dragonfly.enabled=null")
    assert result.returncode != 0
    assert "externalBroker.url" in result.stderr
    assert "nil pointer" not in result.stderr


def test_broker_url_containing_an_apostrophe_survives_yaml_serialisation():
    # toYaml quotes the template expression, then tpl injects the URL inside those quotes,
    # so an unescaped apostrophe in a broker password would break out of the scalar.
    url = "redis://:pa'ss@broker.example:6379/0"
    docs = render(
        SECRET_ARG,
        "dragonfly.enabled=false",
        f"externalBroker.url={url}",
    )
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == url


def test_local_test_values_do_not_hardcode_the_broker_service():
    # A hardcoded broker URL silently defeats externalBroker and breaks any
    # release whose name is not the one baked into the string.
    docs = render(SECRET_ARG, values="helm/local-test-values.yaml")
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_flower_waits_for_dragonfly_when_the_enabled_key_is_absent(chart_without_broker_keys):
    # `ref.brokerUrl` treats an absent dragonfly.enabled as enabled, so the flower
    # init container must agree. Otherwise flower starts before its broker is ready.
    docs = render(SECRET_ARG, chart=chart_without_broker_keys)
    flower = find(docs, "Deployment", "-flower")
    assert flower["spec"]["template"]["spec"].get("initContainers"), (
        "flower must still wait for the bundled broker when dragonfly.enabled is absent"
    )


def _pod_annotations(docs: list[dict], provider: str) -> dict:
    return find(docs, "Deployment", f"-{provider}")["spec"]["template"]["metadata"]["annotations"]


def test_each_provider_is_keyed_to_its_own_secret():
    # Hashing the whole rendered secret.yaml gave every worker the same checksum,
    # so a change to one provider's env restarted all of them
    # and re-ran whatever long executions were in flight.
    docs = render(SECRET_ARG)
    checksums = {p: _pod_annotations(docs, p)["checksum/config"] for p in PROVIDERS}
    assert len(set(checksums.values())) == len(PROVIDERS), f"providers share a checksum: {checksums}"


def test_changing_one_provider_env_does_not_restart_the_others():
    before = render(SECRET_ARG)
    after = render(SECRET_ARG, "providers.ilamb.env.DASK_SCHEDULER=threads")
    assert _pod_annotations(after, "ilamb") != _pod_annotations(before, "ilamb")
    for provider in ("esmvaltool", "pmp", "orchestrator"):
        assert _pod_annotations(after, provider) == _pod_annotations(before, provider)


def test_changing_a_provider_config_restarts_that_worker():
    # The ConfigMap is remounted on upgrade, but the running process only rereads it on restart,
    # so the config has to take part in the pod annotations.
    before = render(SECRET_ARG)
    after = render(SECRET_ARG, "providers.esmvaltool.config.max_parallel_tasks=9")
    assert (
        _pod_annotations(after, "esmvaltool")["checksum/configmap"]
        != _pod_annotations(before, "esmvaltool")["checksum/configmap"]
    )


def test_pod_annotations_cannot_clobber_the_rollout_checksums():
    # A duplicate key leaves the last one standing, so the chart-managed keys are rendered last.
    docs = render(SECRET_ARG, "providers.ilamb.podAnnotations.checksum/config=stale")
    assert _pod_annotations(docs, "ilamb")["checksum/config"] != "stale"


def test_only_providers_with_a_config_get_a_configmap_checksum():
    docs = render(SECRET_ARG)
    assert "checksum/configmap" in _pod_annotations(docs, "esmvaltool")
    for provider in ("pmp", "ilamb", "orchestrator"):
        assert "checksum/configmap" not in _pod_annotations(docs, provider)


def test_any_provider_can_carry_a_chart_managed_config():
    # The config mount is driven by the provider's own values, not by a template
    # that knows esmvaltool by name.
    docs = render(
        SECRET_ARG,
        "providers.pmp.config.foo=bar",
        "providers.pmp.configMountPath=/etc/pmp",
        "providers.pmp.configEnvVar=PMP_CONFIG_DIR",
    )
    configmap = find(docs, "ConfigMap", "-pmp-config")
    assert yaml.safe_load(configmap["data"]["config.yaml"]) == {"foo": "bar"}

    pod = find(docs, "Deployment", "-pmp")["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["pmp-config"] == "/etc/pmp"
    assert _container_env(docs, "pmp")["PMP_CONFIG_DIR"] == "/etc/pmp"


def test_a_config_without_a_mount_path_fails_with_a_clear_message():
    result = _render(SECRET_ARG, "providers.pmp.config.foo=bar")
    assert result.returncode != 0
    assert "providers.pmp: config is set, so configMountPath must be set too" in result.stderr


def _service_account_names(docs: list[dict]) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}


def _assert_wanted_service_accounts_exist(docs: list[dict], required: bool = False) -> None:
    """Assert every Deployment's serviceAccountName is one the chart also creates."""
    created = _service_account_names(docs)
    for deployment in [d for d in docs if d.get("kind") == "Deployment"]:
        wanted = deployment["spec"]["template"]["spec"].get("serviceAccountName")
        if wanted is None and not required:
            continue
        assert wanted in created, (
            f"{deployment['metadata']['name']} wants ServiceAccount {wanted!r}, which is not created"
        )


def test_no_deployment_references_a_service_account_that_is_not_created():
    docs = render(SECRET_ARG, "providers.pmp.serviceAccount.create=null")
    _assert_wanted_service_accounts_exist(docs)


def test_every_deployment_uses_its_own_service_account_by_default():
    docs = render(SECRET_ARG)
    _assert_wanted_service_accounts_exist(docs, required=True)


def test_declining_to_create_a_service_account_omits_the_field():
    # Naming an account the chart does not create means the pod is never admitted,
    # so the field has to disappear rather than fall back to a default name.
    docs = render(
        SECRET_ARG,
        "api.serviceAccount.create=false",
        "flower.serviceAccount.create=false",
        "providers.pmp.serviceAccount.create=false",
    )
    for component in ("api", "flower", "pmp"):
        pod = find(docs, "Deployment", f"-{component}")["spec"]["template"]["spec"]
        assert "serviceAccountName" not in pod
    _assert_wanted_service_accounts_exist(docs)


def test_a_custom_service_account_name_is_the_one_that_gets_created():
    # The deployment templates prefer serviceAccount.name, so the ServiceAccount
    # templates must create it under that same name or the pod cannot be admitted.
    docs = render(
        SECRET_ARG,
        "providers.pmp.serviceAccount.name=my-sa",
        "api.serviceAccount.name=api-sa",
        "flower.serviceAccount.name=flower-sa",
    )
    assert {"my-sa", "api-sa", "flower-sa"} <= _service_account_names(docs)
    _assert_wanted_service_accounts_exist(docs)


SIZE_ROUTES = """
default = "{provider}"

[esmvaltool]
default = "esmvaltool-medium"
rules = [
  { match = "portrait-*", queue = "esmvaltool-large" },
]
"""

SIZE_VALUES = {
    "celeryRoutes": SIZE_ROUTES,
    "providers": {
        "esmvaltool": {"queues": ["esmvaltool", "esmvaltool-medium"]},
        "esmvaltool-large": {
            "provider": "esmvaltool",
            "queues": ["esmvaltool-large", "esmvaltool-medium"],
            "concurrency": 1,
            "config": {"max_parallel_tasks": 4},
            "configMountPath": "/etc/esmvaltool",
            "configEnvVar": "ESMVALTOOL_CONFIG_DIR",
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
    """Every queue the rendered routing table can produce.

    The queue precedence lives upstream in `RoutingTable`, so the table is parsed with it
    rather than with a second copy of those rules that can drift.
    """
    tables = [
        d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-celery-routes")
    ]
    if not tables:
        return set()
    with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
        f.write(tables[0]["data"]["routes.toml"])
        f.flush()
        table = RoutingTable.from_file(Path(f.name))

    providers = set(table.providers)
    for worker in _worker_deployments(docs):
        args = worker["spec"]["template"]["spec"]["containers"][0]["args"]
        if "--provider" in args:
            providers.add(args[args.index("--provider") + 1])

    routed: set[str] = set()
    for provider in providers:
        entry = table.providers.get(provider)
        if entry is not None:
            routed.update(rule.queue.format(provider=provider) for rule in entry.rules)
        # A diagnostic no rule matches lands on the applicable default, or the bare provider queue.
        routed.add(table.queue_for(provider, "no-rule-matches-this-slug"))
    return routed


def assert_no_orphan_queues(docs: list[dict]) -> None:
    """A task sent to a queue with no consumer waits forever and the solve blocks."""
    orphans = _routed_queues(docs) - _consumed_queues(docs)
    assert not orphans, f"routing table targets queues no worker consumes: {sorted(orphans)}"


def test_celery_routes_are_off_by_default():
    docs = render(SECRET_ARG)
    assert not [d for d in docs if d["metadata"]["name"].endswith("-celery-routes")]
    assert "REF_CELERY_ROUTES" not in _container_env(docs, "esmvaltool")
    api = find(docs, "Deployment", "-api")["spec"]["template"]["spec"]["containers"][0]
    assert "REF_CELERY_ROUTES" not in {e["name"] for e in api.get("env", [])}


def test_celery_routes_render_as_valid_toml():
    docs = render(SECRET_ARG, values=SIZE_VALUES)
    configmap = find(docs, "ConfigMap", "-celery-routes")
    table = tomllib.loads(configmap["data"]["routes.toml"])
    assert table["esmvaltool"]["rules"][0]["queue"] == "esmvaltool-large"


@pytest.mark.parametrize("component", ["api", "esmvaltool", "orchestrator"])
def test_celery_routes_are_mounted_where_the_executor_can_run(component):
    # The API triggers solves, and an operator may exec `ref solve` in a worker pod,
    # so the table rides along everywhere.
    docs = render(SECRET_ARG, values=SIZE_VALUES)
    path = _container_env(docs, component)["REF_CELERY_ROUTES"]
    mounts = {m["name"]: m["mountPath"] for m in _container(docs, component)["volumeMounts"]}
    assert path == f"{mounts['celery-routes']}/routes.toml"


def test_worker_instance_can_run_a_provider_under_a_different_name():
    docs = render(SECRET_ARG, values=SIZE_VALUES)
    args = _worker_args(docs, "esmvaltool-large")
    assert args[args.index("--provider") + 1] == "esmvaltool"
    assert "--queues=esmvaltool-large,esmvaltool-medium" in args
    # The queue override must follow the `--` separator to reach the celery worker.
    assert args.index("--") < args.index("--queues=esmvaltool-large,esmvaltool-medium")


def test_split_esmvaltool_instances_get_their_own_config():
    docs = render(SECRET_ARG, values=SIZE_VALUES)
    large = yaml.safe_load(find(docs, "ConfigMap", "-esmvaltool-large-config")["data"]["config.yaml"])
    assert large["max_parallel_tasks"] == 4
    default = yaml.safe_load(find(docs, "ConfigMap", "-esmvaltool-config")["data"]["config.yaml"])
    # The exact default is owned by test_esmvaltool_config_is_rendered_and_mounted.
    assert default["max_parallel_tasks"] != large["max_parallel_tasks"]
    assert _container_env(docs, "esmvaltool-large")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_size_values_route_to_no_orphan_queue():
    assert_no_orphan_queues(render(SECRET_ARG, values=SIZE_VALUES))


@pytest.mark.parametrize("values", VALUES_FILES)
def test_orphan_queue_guard_runs_against_shipped_values(values):
    # Trivially green today because no shipped values file sets celeryRoutes.
    # The guard bites when one gains a routing table.
    assert_no_orphan_queues(render(SECRET_ARG, values=values))


def test_orphan_queue_guard_detects_a_queue_with_no_consumer():
    values = {
        "celeryRoutes": '[esmvaltool]\nrules = [ { match = "*", queue = "esmvaltool-huge" } ]\n',
    }
    with pytest.raises(AssertionError, match="esmvaltool-huge"):
        assert_no_orphan_queues(render(SECRET_ARG, values=values))

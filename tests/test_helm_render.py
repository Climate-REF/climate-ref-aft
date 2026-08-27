"""Render the Helm chart and assert on the resulting Kubernetes objects.

`helm lint` only checks that one values permutation parses.
These tests render the chart the way an operator would install it
and assert on the objects that come out,
so that template regressions surface in seconds rather than in a minikube job.
"""

import functools
import re
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
    docs = render(values=values)
    assert docs, f"{values or 'helm/values.yaml'} rendered no objects"


PROVIDERS = ["orchestrator", "esmvaltool", "pmp", "ilamb"]


def _container(docs: list[dict], provider: str) -> dict:
    """Return the worker container of a provider's Deployment."""
    return find(docs, "Deployment", f"-{provider}")["spec"]["template"]["spec"]["containers"][0]


def _worker_args(docs: list[dict], provider: str) -> list[str]:
    return _container(docs, provider)["args"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_gets_a_worker_deployment(provider):
    docs = render()
    args = _worker_args(docs, provider)
    assert args[:4] == ["celery", "start-worker", "--loglevel", "DEBUG"]


def test_orchestrator_worker_is_not_scoped_to_a_provider():
    args = _worker_args(render(), "orchestrator")
    assert "--provider" not in args


@pytest.mark.parametrize("provider", ["esmvaltool", "pmp", "ilamb"])
def test_diagnostic_workers_are_scoped_to_their_provider(provider):
    args = _worker_args(render(), provider)
    assert args[args.index("--provider") + 1] == provider


def test_esmvaltool_worker_is_pinned_to_one_celery_child():
    # Each esmvaltool execution fans out via its own Dask cluster.
    # Extra Celery children multiply that footprint and OOM the node.
    args = _worker_args(render(), "esmvaltool")
    assert "--concurrency=1" in args


@pytest.mark.parametrize("provider", PROVIDERS)
def test_workers_have_no_liveness_probe_by_default(provider):
    docs = render()
    assert "livenessProbe" not in _container(docs, provider)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_liveness_probe_pings_the_workers_own_celery_node(provider):
    # A wedged worker still passes a process check,
    # so the probe has to exercise the consumer loop of this pod specifically.
    docs = render(
        "defaults.livenessProbe.enabled=true",
        "defaults.livenessProbe.pingTimeoutSeconds=17",
    )
    command = _container(docs, provider)["livenessProbe"]["exec"]["command"][-1]
    assert "inspect ping" in command
    assert "-d celery@$(hostname)" in command
    assert "-t 17" in command


def test_liveness_probe_timings_are_configurable():
    docs = render(
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
        "providers.ilamb.livenessProbe.enabled=true",
        "providers.ilamb.livenessProbe.periodSeconds=60",
    )
    assert _container(docs, "ilamb")["livenessProbe"]["periodSeconds"] == 60
    assert "livenessProbe" not in _container(docs, "pmp")


def test_liveness_probe_is_rejected_with_the_solo_pool():
    # The solo pool runs tasks in the main thread, so a busy worker cannot answer the ping
    # and every busy worker would be restarted.
    result = _render(
        "defaults.livenessProbe.enabled=true",
        "providers.pmp.extraArgs={--pool=solo}",
    )
    assert result.returncode != 0
    assert "providers.pmp: livenessProbe cannot be used with the solo pool" in result.stderr


def test_liveness_probe_guard_only_matches_the_pool_argument():
    # An extra argument that merely contains "solo" is not the solo pool.
    docs = render(
        "defaults.livenessProbe.enabled=true",
        "providers.pmp.extraArgs={--queues=solo-tests}",
    )
    assert "livenessProbe" in _container(docs, "pmp")


def test_liveness_probe_can_be_disabled_for_one_provider_only():
    # mergeOverwrite must not swallow a per-provider `false`.
    docs = render(
        "defaults.livenessProbe.enabled=true",
        "providers.ilamb.livenessProbe.enabled=false",
    )
    assert "livenessProbe" not in _container(docs, "ilamb")
    assert "livenessProbe" in _container(docs, "pmp")


def _provider_env(docs: list[dict], provider: str) -> dict:
    return find(docs, "Secret", f"-{provider}")["stringData"]


def _container_env(docs: list[dict], provider: str) -> dict:
    container = find(docs, "Deployment", f"-{provider}")["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_provider_specific_env_does_not_leak_between_providers():
    docs = render()
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
    env = _provider_env(render(), provider)
    assert env["HOME"] == "/tmp"  # noqa: S108
    assert env["REF_CONFIGURATION"] == "/ref"
    assert env["REF_EXECUTOR"] == "climate_ref_celery.executor.CeleryExecutor"


AFT_PROVIDERS = "climate_ref_esmvaltool:provider,climate_ref_ilamb:provider,climate_ref_pmp:provider"


@pytest.mark.parametrize("component", [*PROVIDERS, "api", "migrate"])
def test_every_component_names_the_same_diagnostic_providers(component):
    env = _provider_env(render(), component)
    assert env["REF_DIAGNOSTIC_PROVIDERS"] == AFT_PROVIDERS


def test_the_named_providers_are_the_ones_with_workers():
    docs = render()
    deployed = set()
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        args = doc["spec"]["template"]["spec"]["containers"][0].get("args") or []
        if "--provider" in args:
            deployed.add(args[args.index("--provider") + 1])
    named = _provider_env(docs, "orchestrator")["REF_DIAGNOSTIC_PROVIDERS"].split(",")
    assert sorted(named) == sorted(f"climate_ref_{p}:provider" for p in deployed)


def test_esmvaltool_config_is_rendered_and_mounted():
    docs = render()
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
    docs = render("providers.esmvaltool.env=null")
    assert _container_env(docs, "esmvaltool")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_esmvaltool_config_can_be_opted_out():
    docs = render("providers.esmvaltool.config=null")
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
        "providers.pmp.replicaCount=7",
        "providers.pmp.image.tag=override-tag",
        "providers.pmp.env.HOME=/override-home",
    )
    deployment = find(docs, "Deployment", "-pmp")
    assert deployment["spec"]["replicas"] == 7
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":override-tag")
    assert _provider_env(docs, "pmp")["HOME"] == "/override-home"


def test_overriding_one_provider_does_not_affect_the_others():
    docs = render("providers.pmp.replicaCount=7")
    assert find(docs, "Deployment", "-ilamb")["spec"]["replicas"] == 1
    assert find(docs, "Deployment", "-orchestrator")["spec"]["replicas"] == 1


def test_external_broker_is_used_when_dragonfly_is_disabled():
    docs = render(
        "dragonfly.enabled=false",
        "externalBroker.url=redis://broker.example:6379",
    )
    assert not [d for d in docs if d["metadata"]["name"].endswith("-dragonfly")]
    for provider in PROVIDERS:
        env = _provider_env(docs, provider)
        assert env["CELERY_BROKER_URL"] == "redis://broker.example:6379"
        assert env["CELERY_RESULT_BACKEND"] == "redis://broker.example:6379"


def test_disabling_dragonfly_without_a_broker_url_fails_with_a_clear_message():
    result = _render("dragonfly.enabled=false")
    assert result.returncode != 0
    assert "externalBroker.url" in result.stderr


def test_flower_only_waits_for_dragonfly_when_dragonfly_is_deployed():
    with_dragonfly = find(render(), "Deployment", "-flower")
    assert with_dragonfly["spec"]["template"]["spec"]["initContainers"]

    without = find(
        render(
            "dragonfly.enabled=false",
            "externalBroker.url=redis://broker.example:6379",
        ),
        "Deployment",
        "-flower",
    )
    assert "initContainers" not in without["spec"]["template"]["spec"]


def test_workers_only_wait_for_dragonfly_when_dragonfly_is_deployed():
    docs = render()
    without_broker_docs = render(
        "dragonfly.enabled=false",
        "externalBroker.url=redis://broker.example:6379",
    )
    for provider in PROVIDERS:
        with_dragonfly = find(docs, "Deployment", f"-{provider}")
        init_containers = with_dragonfly["spec"]["template"]["spec"]["initContainers"]
        assert [c for c in init_containers if c["name"] == "wait-for-dragonfly"]

        without = find(without_broker_docs, "Deployment", f"-{provider}")
        assert "initContainers" not in without["spec"]["template"]["spec"]


def test_dragonfly_is_deployed_by_default():
    docs = render()
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
    docs = render(chart=chart_without_broker_keys)
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_explicitly_nulling_dragonfly_enabled_fails_with_a_clear_message():
    # An explicit null reads as disabled. That is defensible, but it must produce
    # the actionable message rather than a nil pointer dereference.
    result = _render("dragonfly.enabled=null")
    assert result.returncode != 0
    assert "externalBroker.url" in result.stderr
    assert "nil pointer" not in result.stderr


def test_broker_url_containing_an_apostrophe_survives_yaml_serialisation():
    # toYaml quotes the template expression, then tpl injects the URL inside those quotes,
    # so an unescaped apostrophe in a broker password would break out of the scalar.
    url = "redis://:pa'ss@broker.example:6379/0"
    docs = render(
        "dragonfly.enabled=false",
        f"externalBroker.url={url}",
    )
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == url


def test_local_test_values_do_not_hardcode_the_broker_service():
    # A hardcoded broker URL silently defeats externalBroker and breaks any
    # release whose name is not the one baked into the string.
    docs = render(values="helm/local-test-values.yaml")
    assert _provider_env(docs, "pmp")["CELERY_BROKER_URL"] == "redis://test-dragonfly:6379"


def test_flower_waits_for_dragonfly_when_the_enabled_key_is_absent(chart_without_broker_keys):
    # `ref.brokerUrl` treats an absent dragonfly.enabled as enabled, so the flower
    # init container must agree. Otherwise flower starts before its broker is ready.
    docs = render(chart=chart_without_broker_keys)
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
    docs = render()
    checksums = {p: _pod_annotations(docs, p)["checksum/config"] for p in PROVIDERS}
    assert len(set(checksums.values())) == len(PROVIDERS), f"providers share a checksum: {checksums}"


def test_changing_one_provider_env_does_not_restart_the_others():
    before = render()
    after = render("providers.ilamb.env.DASK_SCHEDULER=threads")
    assert _pod_annotations(after, "ilamb") != _pod_annotations(before, "ilamb")
    for provider in ("esmvaltool", "pmp", "orchestrator"):
        assert _pod_annotations(after, provider) == _pod_annotations(before, provider)


def test_changing_a_provider_config_restarts_that_worker():
    # The ConfigMap is remounted on upgrade, but the running process only rereads it on restart,
    # so the config has to take part in the pod annotations.
    before = render()
    after = render("providers.esmvaltool.config.max_parallel_tasks=9")
    assert (
        _pod_annotations(after, "esmvaltool")["checksum/configmap"]
        != _pod_annotations(before, "esmvaltool")["checksum/configmap"]
    )


def test_pod_annotations_cannot_clobber_the_rollout_checksums():
    # A duplicate key leaves the last one standing, so the chart-managed keys are rendered last.
    docs = render("providers.ilamb.podAnnotations.checksum/config=stale")
    assert _pod_annotations(docs, "ilamb")["checksum/config"] != "stale"


def test_only_providers_with_a_config_get_a_configmap_checksum():
    docs = render()
    assert "checksum/configmap" in _pod_annotations(docs, "esmvaltool")
    for provider in ("pmp", "ilamb", "orchestrator"):
        assert "checksum/configmap" not in _pod_annotations(docs, provider)


def test_any_provider_can_carry_a_chart_managed_config():
    # The config mount is driven by the provider's own values, not by a template
    # that knows esmvaltool by name.
    docs = render(
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
    result = _render("providers.pmp.config.foo=bar")
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
    docs = render("providers.pmp.serviceAccount.create=null")
    _assert_wanted_service_accounts_exist(docs)


def test_every_deployment_uses_its_own_service_account_by_default():
    docs = render()
    _assert_wanted_service_accounts_exist(docs, required=True)


def test_declining_to_create_a_service_account_omits_the_field():
    # Naming an account the chart does not create means the pod is never admitted,
    # so the field has to disappear rather than fall back to a default name.
    docs = render(
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
    docs = render()
    assert not [d for d in docs if d["metadata"]["name"].endswith("-celery-routes")]
    assert "REF_CELERY_ROUTES" not in _container_env(docs, "esmvaltool")
    api = find(docs, "Deployment", "-api")["spec"]["template"]["spec"]["containers"][0]
    assert "REF_CELERY_ROUTES" not in {e["name"] for e in api.get("env", [])}


def test_celery_routes_render_as_valid_toml():
    docs = render(values=SIZE_VALUES)
    configmap = find(docs, "ConfigMap", "-celery-routes")
    table = tomllib.loads(configmap["data"]["routes.toml"])
    assert table["esmvaltool"]["rules"][0]["queue"] == "esmvaltool-large"


@pytest.mark.parametrize("component", ["api", "esmvaltool", "orchestrator"])
def test_celery_routes_are_mounted_where_the_executor_can_run(component):
    # The API triggers solves, and an operator may exec `ref solve` in a worker pod,
    # so the table rides along everywhere.
    docs = render(values=SIZE_VALUES)
    path = _container_env(docs, component)["REF_CELERY_ROUTES"]
    mounts = {m["name"]: m["mountPath"] for m in _container(docs, component)["volumeMounts"]}
    assert path == f"{mounts['celery-routes']}/routes.toml"


def test_worker_instance_can_run_a_provider_under_a_different_name():
    docs = render(values=SIZE_VALUES)
    args = _worker_args(docs, "esmvaltool-large")
    assert args[args.index("--provider") + 1] == "esmvaltool"
    assert "--queues=esmvaltool-large,esmvaltool-medium" in args
    # The queue override must follow the `--` separator to reach the celery worker.
    assert args.index("--") < args.index("--queues=esmvaltool-large,esmvaltool-medium")


def test_split_esmvaltool_instances_get_their_own_config():
    docs = render(values=SIZE_VALUES)
    large = yaml.safe_load(find(docs, "ConfigMap", "-esmvaltool-large-config")["data"]["config.yaml"])
    assert large["max_parallel_tasks"] == 4
    default = yaml.safe_load(find(docs, "ConfigMap", "-esmvaltool-config")["data"]["config.yaml"])
    # The exact default is owned by test_esmvaltool_config_is_rendered_and_mounted.
    assert default["max_parallel_tasks"] != large["max_parallel_tasks"]
    assert _container_env(docs, "esmvaltool-large")["ESMVALTOOL_CONFIG_DIR"] == "/etc/esmvaltool"


def test_size_values_route_to_no_orphan_queue():
    assert_no_orphan_queues(render(values=SIZE_VALUES))


@pytest.mark.parametrize("values", VALUES_FILES)
def test_orphan_queue_guard_runs_against_shipped_values(values):
    # Trivially green today because no shipped values file sets celeryRoutes.
    # The guard bites when one gains a routing table.
    assert_no_orphan_queues(render(values=values))


def test_orphan_queue_guard_detects_a_queue_with_no_consumer():
    values = {
        "celeryRoutes": '[esmvaltool]\nrules = [ { match = "*", queue = "esmvaltool-huge" } ]\n',
    }
    with pytest.raises(AssertionError, match="esmvaltool-huge"):
        assert_no_orphan_queues(render(values=values))


def _mount(docs: list[dict], component: str, path: str) -> dict:
    """Return the volumeMount at `path` for a component's Deployment, or an empty dict."""
    mounts = _container(docs, component).get("volumeMounts", [])
    return next((m for m in mounts if m["mountPath"] == path), {})


def test_orchestrator_is_configured_from_its_own_top_level_block():
    docs = render("orchestrator.replicaCount=3")
    assert find(docs, "Deployment", "-orchestrator")["spec"]["replicas"] == 3


def test_orchestrator_can_be_disabled():
    docs = render("orchestrator.enabled=false")
    assert not [d for d in docs if d["metadata"]["name"].endswith("-orchestrator")]


def test_orchestrator_left_under_providers_fails_with_a_migration_message():
    # The stale key would otherwise render a second worker on the same `celery` queue.
    result = _render("providers.orchestrator.replicaCount=1")
    assert result.returncode != 0
    assert "providers.orchestrator moved to the top-level `orchestrator` block" in result.stderr


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_worker_can_write_its_log_directory(provider):
    # climate_ref_celery.app opens a loguru file sink under config.paths.log as the worker starts,
    # so a read-only /ref/log stops the worker before it consumes a single task.
    docs = render(values="helm/ci/gh-actions-values.yaml")
    mounts = _container(docs, provider).get("volumeMounts", [])
    writable = {m["mountPath"] for m in mounts if not m.get("readOnly")}
    assert writable & {"/ref", "/ref/log"}, f"{provider} has no writable /ref/log"


def test_diagnostic_workers_get_read_only_ref_and_shared_scratch():
    # Provider workers read the conda environments and write only their execution outputs.
    # Scratch stays on the shared volume because the orchestrator copies results out of it.
    docs = render(values="helm/ci/gh-actions-values.yaml")
    for provider in ("esmvaltool", "pmp", "ilamb"):
        assert _mount(docs, provider, "/ref")["readOnly"] is True
        assert _mount(docs, provider, "/ref/scratch")["subPath"] == "scratch"


def test_orchestrator_and_migrate_job_can_write_ref():
    # The orchestrator runs `providers setup` and copies scratch into results,
    # and migrations write the database, which defaults to SQLite under /ref.
    docs = render(values="helm/ci/gh-actions-values.yaml")
    assert _mount(docs, "orchestrator", "/ref").get("readOnly") is not True
    job = find(docs, "Job", "-migrate")["spec"]["template"]["spec"]["containers"][0]
    ref = next(m for m in job["volumeMounts"] if m["mountPath"] == "/ref")
    assert ref.get("readOnly") is not True


@pytest.mark.parametrize("component", ["api", "flower"])
def test_http_route_renders_the_configured_filters(component):
    # A filter that silently vanishes leaves a route the operator believes is gated behind auth.
    middleware = {
        "type": "ExtensionRef",
        "extensionRef": {"group": "traefik.io", "kind": "Middleware", "name": "forwardauth"},
    }
    values = {component: {"httpRoute": {"enabled": True, "filters": [middleware]}}}
    docs = render(values=values)
    route = find(docs, "HTTPRoute", f"-{component}")
    assert route["spec"]["rules"][0]["filters"] == [middleware]


@pytest.mark.parametrize("component", ["api", "flower"])
def test_http_route_omits_filters_when_none_are_set(component):
    values = {component: {"httpRoute": {"enabled": True}}}
    docs = render(values=values)
    assert "filters" not in find(docs, "HTTPRoute", f"-{component}")["spec"]["rules"][0]


def _mount_paths(workload: dict) -> set[str]:
    mounts = workload["spec"]["template"]["spec"]["containers"][0].get("volumeMounts", [])
    return {m["mountPath"] for m in mounts}


def test_migrate_job_follows_an_orchestrator_only_env_override():
    # The Job migrates the database the orchestrator then talks to.
    # Taking env from `defaults` while taking volumes from the orchestrator would let a
    # migration run against a different database than the app uses.
    docs = render("orchestrator.env.REF_DATABASE_URL=sqlite:////ref/db/other.db")
    secret = find(docs, "Secret", "-migrate")
    assert secret["stringData"]["REF_DATABASE_URL"] == "sqlite:////ref/db/other.db"


@pytest.mark.parametrize("values", ["helm/ci/minimal-values.yaml", "helm/local-test-values.yaml"])
def test_orchestrator_and_migrate_job_inherit_the_default_ref_mount(values):
    # These files mount /ref through `defaults` alone and never name the orchestrator's volumes.
    # An empty `volumes` list in the orchestrator block replaces that inherited list rather than
    # falling back to it, which left the pod with no /ref and a read-only root filesystem.
    docs = render(values=values)
    assert "/ref" in _mount_paths(find(docs, "Deployment", "-orchestrator"))
    assert "/ref" in _mount_paths(find(docs, "Job", "-migrate"))


def _scaled_components(docs: list[dict]) -> set[str]:
    """Return the component label of every ScaledObject rendered."""
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]
        for d in docs
        if d.get("kind") == "ScaledObject"
    }


def test_no_scaled_objects_without_keda():
    assert not _scaled_components(render())


def test_keda_scales_a_worker_on_its_own_queue():
    docs = render("providers.pmp.keda.enabled=true")
    assert _scaled_components(docs) == {"pmp"}
    spec = find(docs, "ScaledObject", "-pmp")["spec"]
    assert spec["scaleTargetRef"]["name"] == find(docs, "Deployment", "-pmp")["metadata"]["name"]
    assert spec["minReplicaCount"] == 0
    assert [t["metadata"]["listName"] for t in spec["triggers"]] == ["pmp"]


def test_keda_worker_leaves_replicas_to_the_autoscaler():
    # A chart-set replicas fights KEDA back to the static count on every upgrade.
    docs = render("providers.pmp.keda.enabled=true")
    assert "replicas" not in find(docs, "Deployment", "-pmp")["spec"]


def test_keda_watches_every_queue_a_split_instance_consumes():
    docs = render(
        "providers.esmvaltool.keda.enabled=true",
        "providers.esmvaltool.queues={esmvaltool,esmvaltool-large}",
    )
    triggers = find(docs, "ScaledObject", "-esmvaltool")["spec"]["triggers"]
    assert [t["metadata"]["listName"] for t in triggers] == ["esmvaltool", "esmvaltool-large"]


def test_keda_scales_the_orchestrator_on_the_default_queue():
    docs = render("orchestrator.keda.enabled=true")
    triggers = find(docs, "ScaledObject", "-orchestrator")["spec"]["triggers"]
    assert [t["metadata"]["listName"] for t in triggers] == ["celery"]


def test_keda_points_at_the_bundled_broker_by_default():
    """The scaler dials from the KEDA namespace, so its address has to be fully qualified."""
    docs = render("providers.pmp.keda.enabled=true")
    address = find(docs, "ScaledObject", "-pmp")["spec"]["triggers"][0]["metadata"]["address"]
    assert address == "test-dragonfly.default.svc.cluster.local:6379"


def test_keda_without_the_bundled_broker_needs_an_explicit_address():
    result = _render(
        "dragonfly.enabled=false",
        "externalBroker.url=redis://elsewhere:6379",
        "providers.pmp.keda.enabled=true",
    )
    assert result.returncode != 0
    assert "keda.redisAddress" in result.stderr


def test_keda_and_hpa_together_fail_with_a_clear_message():
    result = _render(
        "providers.pmp.keda.enabled=true",
        "providers.pmp.autoscaling.enabled=true",
    )
    assert result.returncode != 0
    assert "autoscaling.enabled and keda.enabled both set" in result.stderr


def _prometheus_query(docs: list[dict], instance: str) -> str:
    triggers = find(docs, "ScaledObject", f"-{instance}")["spec"]["triggers"]
    prometheus = [t for t in triggers if t["type"] == "prometheus"]
    assert len(prometheus) == 1
    return prometheus[0]["metadata"]["query"]


RUNNING_TASKS_ARGS = (
    "providers.pmp.keda.enabled=true",
    "providers.pmp.keda.runningTasks.enabled=true",
    "providers.pmp.keda.runningTasks.serverAddress=http://prometheus.monitoring.svc:9090",
)


def test_keda_running_tasks_trigger_holds_a_busy_worker_up():
    docs = render(*RUNNING_TASKS_ARGS)
    assert "flower_worker_number_of_currently_executing_tasks" in _prometheus_query(docs, "pmp")


def test_keda_running_tasks_query_matches_this_instance_only():
    # Flower labels the metric `celery@<pod>`, so the selector must not let a size-split
    # instance hold up the plain one, which is what a bare provider prefix would do.
    docs = render(
        "providers.esmvaltool.keda.enabled=true",
        "providers.esmvaltool.keda.runningTasks.enabled=true",
        "providers.esmvaltool.keda.runningTasks.serverAddress=http://prom:9090",
    )
    selector = re.search(r'worker=~"([^"]+)"', _prometheus_query(docs, "esmvaltool")).group(1)
    # PromQL anchors `=~` at both ends.
    assert re.fullmatch(selector, "celery@test-climate-ref-aft-esmvaltool-6d4b9c7f8x-2kfjd")
    assert not re.fullmatch(selector, "celery@test-climate-ref-aft-esmvaltool-large-6d4b9c7f8x-2kfjd")


def test_keda_running_tasks_query_is_overridable():
    docs = render(
        *RUNNING_TASKS_ARGS,
        "providers.pmp.keda.runningTasks.query=sum(something_else)",
    )
    assert _prometheus_query(docs, "pmp") == "sum(something_else)"


def test_keda_running_tasks_trigger_needs_a_prometheus_address():
    result = _render(
        "providers.pmp.keda.enabled=true",
        "providers.pmp.keda.runningTasks.enabled=true",
    )
    assert result.returncode != 0
    assert "keda.runningTasks.serverAddress" in result.stderr


@pytest.mark.parametrize("provider", ["esmvaltool", "pmp", "ilamb"])
def test_scaled_object_watches_the_queue_the_worker_actually_consumes(provider):
    # The ScaledObject names the queue itself, while the worker derives it from the
    # provider's own `slug`. A provider whose slug left its name behind would leave the
    # trigger watching a queue nothing publishes to, so the worker would never leave zero.
    slug = __import__(f"climate_ref_{provider}", fromlist=["provider"]).provider.slug
    docs = render(f"providers.{provider}.keda.enabled=true")
    triggers = find(docs, "ScaledObject", f"-{provider}")["spec"]["triggers"]
    assert [t["metadata"]["listName"] for t in triggers] == [slug]


def test_keda_redis_metadata_carries_broker_options():
    docs = render(
        "providers.pmp.keda.enabled=true",
        "providers.pmp.keda.redisMetadata.enableTLS=true",
    )
    metadata = find(docs, "ScaledObject", "-pmp")["spec"]["triggers"][0]["metadata"]
    assert metadata["enableTLS"] == "true"
    assert metadata["listName"] == "pmp"


def test_keda_redis_metadata_cannot_take_over_a_chart_owned_key():
    # Overriding listName would collapse a multi-queue instance into identical triggers.
    docs = render(
        "providers.esmvaltool.keda.enabled=true",
        "providers.esmvaltool.queues={esmvaltool,esmvaltool-large}",
        "providers.esmvaltool.keda.redisMetadata.listName=override",
        "providers.esmvaltool.keda.redisMetadata.listLength=5",
    )
    triggers = find(docs, "ScaledObject", "-esmvaltool")["spec"]["triggers"]
    assert [t["metadata"]["listName"] for t in triggers] == ["esmvaltool", "esmvaltool-large"]
    assert {t["metadata"]["listLength"] for t in triggers} == {"1"}


def test_keda_trigger_metadata_values_are_strings():
    # The KEDA scalers parse their metadata as strings and reject a bare int.
    docs = render(
        "providers.pmp.keda.enabled=true",
        "providers.pmp.keda.listLength=3",
    )
    metadata = find(docs, "ScaledObject", "-pmp")["spec"]["triggers"][0]["metadata"]
    assert all(isinstance(v, str) for v in metadata.values()), metadata


def test_keda_advanced_block_passes_through():
    docs = render(
        "providers.pmp.keda.enabled=true",
        "providers.pmp.keda.advanced.restoreToOriginalReplicaCount=true",
    )
    advanced = find(docs, "ScaledObject", "-pmp")["spec"]["advanced"]
    assert advanced == {"restoreToOriginalReplicaCount": True}


def test_keda_refuses_to_scale_a_long_diagnostic_to_zero_unguarded():
    # The redis trigger goes inactive when the queue empties, not when the work finishes.
    # esmvaltool runs for up to six hours, so a one minute cooldown discards work in flight.
    result = _render(
        "providers.esmvaltool.keda.enabled=true",
        "providers.esmvaltool.keda.cooldownPeriod=60",
    )
    assert result.returncode != 0
    assert "while a diagnostic is still running" in result.stderr


@pytest.mark.parametrize(
    "remedy",
    [
        "providers.esmvaltool.keda.cooldownPeriod=21600",
        "providers.esmvaltool.keda.runningTasks.enabled=true",
        "providers.esmvaltool.keda.extraTriggers[0].type=cron",
    ],
)
def test_keda_scale_down_guard_accepts_each_documented_remedy(remedy):
    # Each remedy alone must clear the guard, so the base render is one that fails without it.
    docs = render(
        "providers.esmvaltool.keda.enabled=true",
        "providers.esmvaltool.keda.cooldownPeriod=60",
        "providers.esmvaltool.keda.runningTasks.serverAddress=http://prom:9090",
        remedy,
    )
    assert find(docs, "ScaledObject", "-esmvaltool")


@pytest.mark.parametrize(("provider", "expected"), [("esmvaltool", 21600), ("pmp", 7200)])
def test_keda_cooldown_defaults_to_the_workers_own_task_limit(provider, expected):
    # The safe cooldown is one the chart can work out, so enabling keda alone must not need it.
    docs = render(f"providers.{provider}.keda.enabled=true")
    assert find(docs, "ScaledObject", f"-{provider}")["spec"]["cooldownPeriod"] == expected


def test_keda_cooldown_stays_short_when_a_trigger_holds_busy_workers_up():
    # runningTasks reports work in flight, so the cooldown no longer has to outlast it.
    docs = render(
        "providers.esmvaltool.keda.enabled=true",
        "providers.esmvaltool.keda.runningTasks.enabled=true",
        "providers.esmvaltool.keda.runningTasks.serverAddress=http://prom:9090",
    )
    assert find(docs, "ScaledObject", "-esmvaltool")["spec"]["cooldownPeriod"] == 1800


def test_keda_cooldown_guard_reads_a_templated_task_limit():
    # env values may be templates, so a guard reading them raw would miss this entirely.
    values = {
        "taskLimit": "7200",
        "providers": {
            "pmp": {
                "env": {"CELERY_TASK_TIME_LIMIT": "{{ .Values.taskLimit }}"},
                "keda": {"enabled": True, "cooldownPeriod": 60},
            }
        },
    }
    result = _render(values=values)
    assert result.returncode != 0
    assert "CELERY_TASK_TIME_LIMIT of 7200" in result.stderr


def _pod_spec(docs: list[dict], component: str, kind: str = "Deployment") -> dict:
    return find(docs, kind, f"-{component}")["spec"]["template"]["spec"]


def test_no_priority_class_is_set_by_default():
    docs = render()
    for component in ("api", "orchestrator", "pmp"):
        assert "priorityClassName" not in _pod_spec(docs, component)
    assert "priorityClassName" not in _pod_spec(docs, "migrate", kind="Job")


def test_api_and_workers_take_separate_priority_classes():
    docs = render(
        "api.priorityClassName=ref-api",
        "defaults.priorityClassName=ref-worker",
    )
    assert _pod_spec(docs, "api")["priorityClassName"] == "ref-api"
    assert _pod_spec(docs, "orchestrator")["priorityClassName"] == "ref-worker"
    assert _pod_spec(docs, "pmp")["priorityClassName"] == "ref-worker"


def test_a_worker_priority_class_can_be_overridden_per_instance():
    docs = render(
        "defaults.priorityClassName=ref-worker",
        "providers.pmp.priorityClassName=ref-worker-low",
    )
    assert _pod_spec(docs, "pmp")["priorityClassName"] == "ref-worker-low"
    assert _pod_spec(docs, "ilamb")["priorityClassName"] == "ref-worker"


def test_the_migrate_job_takes_the_worker_priority_class():
    # The hook must schedule before the release proceeds, so leaving it on the cluster
    # default would let it outrank the workers it migrates for.
    docs = render("defaults.priorityClassName=ref-worker")
    assert _pod_spec(docs, "migrate", kind="Job")["priorityClassName"] == "ref-worker"


def test_the_migrate_job_follows_an_orchestrator_priority_class_override():
    docs = render(
        "defaults.priorityClassName=ref-worker",
        "orchestrator.priorityClassName=ref-orchestrator",
    )
    assert _pod_spec(docs, "migrate", kind="Job")["priorityClassName"] == "ref-orchestrator"

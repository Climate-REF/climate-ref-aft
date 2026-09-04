# Run and triage a solve

How to run a solve on a bootstrapped deployment, watch it, and get it unstuck.
[Bootstrap a deployment](bootstrap-a-deployment.md) comes first.

```bash
export NS=climate-ref
export RELEASE=ref
alias reforch="kubectl -n $NS exec deploy/$RELEASE-climate-ref-aft-orchestrator -c orchestrator --"
```

## How a solve moves

`ref solve` compares the ingested datasets against every diagnostic's data requirements
and works out which execution groups are new or dirty.
Each one becomes a Celery task on its provider's queue.
A worker for that provider runs it in `/ref/scratch`.
The orchestrator consumes the result off the default queue and copies the outputs into `/ref/results`.

So a solve has three places to look when it is not moving:
the queue in Dragonfly, the worker pods, and the orchestrator.

## Start a solve

`solve` blocks until every execution finishes or the timeout passes.
Detach it so the exec dying does not stop it, and log it under `/ref/log`:

```bash
reforch sh -c 'setsid nohup ref solve --timeout 0 > /ref/log/solve.log 2>&1 < /dev/null &'
```

`--timeout 0` waits without limit.
`--no-wait` queues everything and returns at once.
The work carries on either way, because the workers hold the tasks, not the `solve` process.

Narrow it while learning what a deployment does:

- `--provider pmp` or `--diagnostic annual-cycle` filter on a substring of the slug.
- `--one-per-diagnostic` runs one execution of each diagnostic.
- `--dataset-filter source_id=ACCESS-ESM1-5` limits the models.
- `--dry-run` prints what would be queued.

## Watch it

Counts by provider and status:

```bash
reforch ref executions stats
```

The pods, and the autoscaler if KEDA is on:

```bash
kubectl -n $NS get pods
kubectl -n $NS get scaledobjects
```

Flower shows each queue, which worker holds which task, and how long it has run:

```bash
kubectl -n $NS port-forward svc/$RELEASE-climate-ref-aft-flower 5555:5555
```

Then open <http://localhost:5555>.
Do not expose Flower on a route.
Its API has no authentication and can revoke and restart tasks.

The queue lengths and each worker's consumer state, from inside a worker:

```bash
kubectl -n $NS exec -i deploy/$RELEASE-climate-ref-aft-orchestrator -c orchestrator -- \
  uv run python - < scripts/lib/broker_state.py
```

A queue that stays non-zero while its worker shows nothing active is the picture to recognise.
The worker is not consuming.

## Read a failure

```bash
reforch ref executions list-groups --not-successful
reforch ref executions inspect <execution id>
```

`inspect` prints the datasets, the outputs and the tail of the execution log.
The full log sits with the outputs under `/ref/results`.
The worker's own log says what happened around the execution:

```bash
kubectl -n $NS logs deploy/$RELEASE-climate-ref-aft-esmvaltool --tail=200
```

`ref executions list-groups` sorts newest first.
Add `--provider` or `--diagnostic` to narrow it, and `--format json` to script over it.

## Stuck executions

An execution shows `running` with no worker holding it when the worker died mid task.
A time limit, an OOM kill, a pod eviction or a broker restart all do this.
The execution group stays blocked until the record is closed.

Confirm nothing is holding it, in Flower or with the broker state script.
Then close the record and re-queue:

```bash
reforch ref executions fail-running --older-than 12
reforch ref solve --rerun-failed --timeout 0
```

`--older-than` is in hours.
Pick a value beyond the longest time limit a provider carries, so a live execution is not failed under a worker.
Without it every running execution is failed, which is right only when no worker is busy.

## A worker that stops consuming

A Celery worker can wedge: it stays Running and Ready, but the queue backs up and it takes nothing.
Flower shows it online with nothing active.
Restart it:

```bash
kubectl -n $NS rollout restart deploy/$RELEASE-climate-ref-aft-esmvaltool
```

The Deployment uses `Recreate`, and the pod gets its provider's `terminationGracePeriodSeconds`
to finish the task it holds.
A task cut off anyway is redelivered, because the workers acknowledge late.

`defaults.livenessProbe.enabled: true` has the kubelet do this itself.
It is off by default, because a probe that fails during a long diagnostic restarts the pod and loses the execution.
The [chart README](../../helm/README.md#worker-liveness-probe) explains the timings.

## Out of memory

```bash
kubectl -n $NS describe pod -l app.kubernetes.io/component=ilamb | grep -A3 'Last State'
```

`OOMKilled` with exit code 137 means the pod limit is too low for that diagnostic.
Nothing in the three providers bounds its own memory, so the pod limit is the only ceiling.
The REF records what each execution used, so size from measurement rather than guesswork:

```bash
reforch ref executions resources --by provider
reforch ref executions resources --provider ilamb
```

Raise the limit for that provider in the values file and `helm upgrade`.
The chart README's sizing table is the floor for the full AFT set.

## Nothing is being picked up

Work the list from the broker outwards:

1. Dragonfly is Running and the workers can reach it.
   `kubectl -n $NS logs deploy/$RELEASE-climate-ref-aft-pmp | head` shows the broker connection.
   A worker that started before the broker can wedge silently. Restart it.
2. The task is on a queue some worker consumes.
   The broker state script lists every queue.
   A queue with a task and no consumer is an orphan.
   That happens when `celeryRoutes` names a queue no instance lists under `queues`.
3. The worker exists.
   With KEDA the pool scales from zero, so the first pod appears within a polling interval.
   A `ScaledObject` with `READY False` names the problem in its status.
4. The worker's `REF_DIAGNOSTIC_PROVIDERS` matches the API's.
   A provider named in one and not the other queues executions nobody runs.

## The solve finished, the API shows nothing

The API reads the same database, so a result that exists on disk and not in the API is a path problem.
The API mounts `/ref` read-only, and looks for the results tree the orchestrator wrote.
Check both point at the same claim, and that `REF_CONFIGURATION` agrees.

The API caches its provider list at startup.
After a change to the environments or the providers list, roll it:

```bash
kubectl -n $NS rollout restart deploy/$RELEASE-climate-ref-aft-api
```

## Upgrade the chart

```bash
helm upgrade $RELEASE oci://ghcr.io/climate-ref/charts/climate-ref-aft --version <new> -n $NS -f values.yaml
```

The migrate hook runs first.
Every worker then restarts under `Recreate`, each waiting its grace period for the task it holds.
A long esmvaltool execution can hold the rollout for hours, which is the intended trade.
Upgrade between solves where possible, or accept that in-flight executions re-run.

Upgrade the workers before anything that submits tasks.
An old worker cannot decode a message from a newer client.

## Report a problem

```bash
reforch ref doctor --format markdown
```

That prints the findings and a description of the deployment: versions, configuration,
what is ingested, and the relevant environment variables.
Paste it into the issue.

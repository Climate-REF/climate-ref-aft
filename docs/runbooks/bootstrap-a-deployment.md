# Bootstrap a deployment

How to take the chart from `helm install` to a deployment that can solve.
This is the generic procedure.
What is specific to one cluster, such as paths, sizing and the database, belongs in that cluster's own notes.

Every `ref` command below runs inside the orchestrator pod.
It is the one pod with the whole of `/ref` writable, so it is the only place setup can run.

```bash
export NS=climate-ref
export RELEASE=ref
alias reforch="kubectl -n $NS exec deploy/$RELEASE-climate-ref-aft-orchestrator -c orchestrator --"
```

The chart names its resources `<release>-climate-ref-aft-<component>`.
Setting `nameOverride` shortens that to `<release>-<component>`, so adjust the alias to match.

## Before you start

- Helm 3 and `kubectl` pointed at the cluster.
- A volume for `/ref`, writable by uid 1000.
  It holds the conda environments, the reference data, scratch and results.
  Provider setup alone writes close to 50GB, so budget for 200Gi before any model output.
  It must be ReadWriteMany unless every pod can be pinned to one node.
- Model data mounted read-only into every worker at the same path.
- Internet access from the orchestrator pod.
  `providers setup` and `fetch-data` both download.
- Optionally a Postgres database.
  Left unset the REF uses SQLite under `/ref/db`, which is fine for a small deployment.

## 1. Write the values file

Start from [`helm/examples/small-values.yaml`](../../helm/examples/small-values.yaml).
It wires one claim into every pod with the access each needs,
and shrinks the workers to something a single node can hold.

Things to change for your cluster:

- `claimName`, to the claim you created.
- A `volumes` and `volumeMounts` entry per model data directory, read-only, on `defaults`.
- `nodeSelector` and `tolerations`, if the workers must land on particular nodes.
- `REF_DATABASE_URL` in a Secret named by `extraEnvFrom`, if using Postgres.
  Set it on `api` and on `defaults` together, so the API and the workers share one database.
- `resources`, once you know what the diagnostics you care about need.
  The chart defaults in [`helm/README.md`](../../helm/README.md#sizing) are the floor for the full AFT set.

Render it before installing:

```bash
helm template $RELEASE oci://ghcr.io/climate-ref/charts/climate-ref-aft --version 0.6.4 \
  -n $NS -f values.yaml > /dev/null
```

## 2. Install

```bash
kubectl create namespace $NS
helm install $RELEASE oci://ghcr.io/climate-ref/charts/climate-ref-aft --version 0.6.4 \
  -n $NS -f values.yaml
kubectl -n $NS get pods -w
```

A pre-install hook runs `ref db migrate` before anything else starts.
Helm waits for it, so a failing migration fails the install.
Its log is the first thing to read if the install does not return:

```bash
kubectl -n $NS logs job/$RELEASE-climate-ref-aft-migrate
```

After the install every pod should be Running, and the API answers its health check:

```bash
kubectl -n $NS exec deploy/$RELEASE-climate-ref-aft-orchestrator -c orchestrator -- \
  python3 -c "import urllib.request as u; print(u.urlopen('http://$RELEASE-climate-ref-aft-api/api/v1/utils/health-check/').read())"
```

## 3. Set up the providers

```bash
reforch ref providers setup
```

This does three things per provider, and each is idempotent:

- builds its conda environment under `/ref/software`,
- fetches its own reference data into `/ref/cache`, several GB for ILAMB and PMP,
- ingests that data and validates the result.

The environments take a few minutes on a fast disk and the data depends on the link.
A `kubectl exec` dies with your terminal, so run anything this long detached,
with its output under `/ref/log`, and follow it from a second exec:

```bash
reforch sh -c 'setsid nohup ref providers setup > /ref/log/providers-setup.log 2>&1 < /dev/null &'
reforch tail -f /ref/log/providers-setup.log
```

The log reports `Finished setting up provider <slug>` for each provider as it passes,
and ends with `Setup failed for providers` naming any that did not.
ESMValTool's reference data alone runs to tens of GB, so expect this to take a while.
`--skip-data --skip-validate` builds only the environments,
which is enough to test the deployment against the sample data below,
but a full solve needs the data.

Confirm every provider passes before going on:

```bash
reforch ref providers setup --validate-only
```

The API reads the provider environments at startup, so restart it once they exist:

```bash
kubectl -n $NS rollout restart deploy/$RELEASE-climate-ref-aft-api
kubectl -n $NS rollout status deploy/$RELEASE-climate-ref-aft-api
```

## 4. Fetch and ingest the obs4REF data

The obs4REF registry holds the obs4MIPs datasets the AFT diagnostics select, around 10GB.
Fetch it to a directory under `/ref`, then ingest that directory as `obs4mips`:

```bash
reforch sh -c 'setsid nohup ref datasets fetch-data --registry obs4ref --output-directory /ref/data/obs4ref > /ref/log/fetch-obs4ref.log 2>&1 < /dev/null &'
reforch ref datasets ingest --source-type obs4mips /ref/data/obs4ref
```

The log ends with a progress bar at 100% once every file is copied into place.
A download that stalls shows no error, the log simply stops moving.
Kill the process and run the command again, because files already in `/ref/cache` are not fetched twice.
The image has no `ps` or `pgrep`, so find it through `/proc`:

```bash
reforch sh -c 'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline 2>/dev/null | grep -q "bin/ref datasets fetch-data" && kill ${p#/proc/}; done'
```

The source type is `obs4mips` and not `obs4ref`.
The solver matches a requirement against its own source type only,
and the diagnostics ask for `obs4mips`, so data ingested as `obs4ref` is never selected.
`ref doctor` reports it as unreachable.

If you also have your own obs4MIPs archive, ingest only the parts the registry does not carry.
A dataset ingested twice from two paths leaves a diagnostic reading the same period twice.
`ref doctor` reports both problems.

## 5. Ingest the model data

```bash
reforch ref datasets ingest --source-type cmip6 --chunk-size 500 /data/cmip6
```

Ingest walks the tree and records each dataset in the database.
The default parser reads metadata from the DRS path rather than opening files,
so tens of thousands of files ingest in minutes.
`--chunk-size` bounds memory on a large archive.

Ingest a subtree instead to bootstrap on a few models first.
A path like `/data/cmip6/CMIP/CSIRO/ACCESS-ESM1-5` is a valid argument.

Check what landed:

```bash
reforch ref datasets stats
```

## 6. Check the deployment

```bash
reforch ref doctor
```

`doctor` finds what a solve would silently plan around:
reference data that is missing, data ingested under a type nothing selects,
duplicated periods, and diagnostics the ingested data cannot satisfy.
Fix the errors.
The warnings say which diagnostics will not run, which may be what you intended.

Then confirm the API can see the same database:

```bash
kubectl -n $NS port-forward svc/$RELEASE-climate-ref-aft-api 8000:80 &
curl -s http://localhost:8000/api/v1/utils/health-check/
curl -s http://localhost:8000/api/v1/cmip7-aft-diagnostics/ | head -c 300
```

## 7. Run a smoke solve

One quick diagnostic per provider proves the whole loop, from the queue to a result on disk:

```bash
reforch ref solve --timeout 900 --one-per-provider \
  --diagnostic global-mean-timeseries \
  --diagnostic annual-cycle \
  --diagnostic gpp-wecann
reforch ref executions stats
```

Every provider should show one successful execution.
A provider stuck at `running` for the whole timeout means its worker never picked the task up.
See [Run and triage a solve](run-and-triage-a-solve.md).

The deployment is bootstrapped.
A full solve is the same command with no filters.

## Without model data

To exercise the deployment before any archive is mounted, fetch the sample data:

```bash
reforch ref datasets fetch-data --registry sample-data --output-directory /ref/data/sample
reforch ref datasets ingest --source-type cmip6 /ref/data/sample/CMIP6
reforch ref datasets ingest --source-type obs4mips /ref/data/sample/obs4REF
```

The esmvaltool and pmp legs of the smoke solve run against it.
The ilamb leg needs the obs4REF registry from step 4,
because the sample carries an older version of its reference dataset than the diagnostic asks for.

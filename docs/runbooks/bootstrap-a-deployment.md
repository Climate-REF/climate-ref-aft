# Bootstrap a deployment

Before we can run the executions, we must run some initial commands to bootstrap a new deployment.

Every `ref` command below runs inside the orchestrator pod.
It is the one pod with a writable `/ref`  directory.

```bash
export NS=climate-ref
export RELEASE=ref
alias ref-orch="kubectl -n $NS exec deploy/$RELEASE-climate-ref-aft-orchestrator -c orchestrator --"
```

The chart names its resources `<release>-climate-ref-aft-<component>`.
Setting `nameOverride` shortens that to `<release>-<component>`, so adjust the alias to match.

## Before you start

- Helm 3 and `kubectl` pointed at the cluster.
- A volume for `/ref`, writable by uid 1000.
  It holds the conda environments, the reference data, scratch and results.
  Provider setup alone writes close to 50GB, so budget for 200Gi before any model output.
  It must be ReadWriteMany unless every pod can be pinned to one node.
  A full CMIP6 run requires a few TB.
- Model data mounted read-only into every worker at the same path.
- Internet access from the orchestrator pod.
  `providers setup` and `fetch-data` both download data and input files.
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
Look at the log output if anything goes wrong:

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
ref-orch ref providers setup
```

This does three things per provider:

- builds its conda environment under `/ref/software`,
- fetches its own reference data into `/ref/cache`, 10s of GB,
- ingests that data and validates the result

This is idempontent and is safe to rerun.
It should be run after an update.

Building the conda environments can take a few minutes depending on the file system.

The output should report `Finished setting up provider <slug>` for each provider as it passes,
and ends with `Setup failed for providers` naming any that did not.
Confirm every provider passes before going on:

```bash
ref-orch ref providers setup --validate-only
```

The API reads the provider environments at startup, so restart it once they exist:

```bash
kubectl -n $NS rollout restart deploy/$RELEASE-climate-ref-aft-api
kubectl -n $NS rollout status deploy/$RELEASE-climate-ref-aft-api
```

## 4. Fetch and ingest observation data

The obs4REF registry holds the obs4MIPs datasets the AFT diagnostics select.
Fetch it to a directory under `/ref`, then ingest that directory as `obs4REF`:

```bash
ref-orch sh -c 'setsid nohup ref datasets fetch-data --registry obs4ref --output-directory /ref/data/obs4ref > /ref/log/fetch-obs4ref.log 2>&1 < /dev/null &'
ref-orch ref datasets ingest --source-type obs4ref /ref/data/obs4ref
```

The log ends with a progress bar at 100% once every file is copied into place.

Additional obs4MIPs data will be required () to be downloaded from ESGF and ingested.

```bash
ref-orch ref datasets ingest --source-type obs4mips /ref/data/obs4mips
```

## 5. Ingest the model data

```bash
ref-orch ref datasets ingest --source-type cmip6 --chunk-size 500 /data/cmip6
```

Ingest walks the tree and records each dataset in the database.
The default parser reads metadata from the DRS path rather than opening files,
so tens of thousands of files ingest in minutes.
`--chunk-size` bounds memory on a large archive.

Ingest a subtree instead to bootstrap on a few models first.
A path like `/data/cmip6/CMIP/CSIRO/ACCESS-ESM1-5` is a valid argument.
The REF only requires monthly files and a glob pattern can be provided to minimise the number of files that are read in.

Check what landed:

```bash
ref-orch ref datasets stats
```

## 6. Check the deployment

```bash
ref-orch ref doctor
```

`doctor` helps identify potential issues before a solve, such as:
reference data that is missing, data ingested under a type nothing selects,
duplicated periods, and diagnostics the ingested data cannot satisfy.

The warnings say which diagnostics will not run, which may be what you intended.
Generally additional datasets will need to be downloaded and ingested.

Then confirm the API can see the same database:

```bash
kubectl -n $NS port-forward svc/$RELEASE-climate-ref-aft-api 8000:80 &
curl -s http://localhost:8000/api/v1/utils/health-check/
curl -s http://localhost:8000/api/v1/cmip7-aft-diagnostics/ | head -c 300
```

## 7. Run a smoke solve

One quick diagnostic per provider proves the whole loop, from the queue to a result on disk:

```bash
ref-orch ref solve --timeout 900 --one-per-provider \
  --diagnostic global-mean-timeseries \
  --diagnostic annual-cycle \
  --diagnostic gpp-wecann
ref-orch ref executions stats
```

Every provider should show one successful execution.
A provider stuck at `running` for the whole timeout means its worker never picked the task up.
See [Run and triage a solve](run-and-triage-a-solve.md).

The deployment is bootstrapped.
A full solve is the same command with no filters.

## Without model data

To exercise the deployment before any archive is mounted, fetch the sample data:

```bash
ref-orch ref datasets fetch-data --registry sample-data --output-directory /ref/data/sample
ref-orch ref datasets ingest --source-type cmip6 /ref/data/sample/CMIP6
ref-orch ref datasets ingest --source-type obs4mips /ref/data/sample/obs4REF
```

The sample data does not contain the datasets required by ilamb.
It requires also ingesting the obs4REF registry above.

# Google Cloud single-VM research deployment

This folder turns the local integration into a short-lived cloud demonstration
without pretending that a one-node system is production infrastructure.

The main purpose is to answer a dissertation question with evidence:

> Can a confirmed smart-contract event travel through a cloud-hosted data
> pipeline, become a verified and versioned model input, and produce an
> explainable fraud-risk assessment?

The deployment keeps all application parts on one Compute Engine VM to make the
experiment affordable and understandable. It uses Docker Compose for process
isolation and the Google Cloud Ops Agent for logs and monitoring.

## What runs on the VM

```text
Public browser
     │ port 80 only
     ▼
Nginx + React ──► FastAPI ──► Pinata/IPFS + Sepolia
                                  │
                                  ▼
                          blockchain listener
                                  │ verified event
                                  ▼
                               Kafka
                                  │
                                  ▼
                feature processing + XGBoost + SHAP
                                  │
                            PostgreSQL audit
                                  │
                                  ▼
                        Sepolia assessment write-back

Private metrics ──► Google Ops Agent ──► Cloud Monitoring
Docker logs ──────► Google Ops Agent ──► Cloud Logging
```

Sepolia and Pinata remain external public test services. Kafka, PostgreSQL,
FastAPI and every metrics endpoint remain private to Docker or VM loopback.
Only Nginx port 80 is opened by the Terraform firewall.

## Honest scope

This is a **cloud-based research pipeline**, but it is not a production
insurance deployment:

- Kafka has one broker and replication factor one.
- PostgreSQL has one instance on one VM disk.
- HTTP is used for the short-lived fictional-data demonstration.
- The VM is one failure domain and is manually sized.
- Separate Sepolia-only submission and assessment wallets are supplied to their
  respective application containers.
- Synthetic insurers authenticate through one API key each; the server stores
  only their SHA-256 digests.
- Model training data is suitable for research, not a real fraud decision.

The value of this setup is reproducible integration and measurable behaviour,
not high availability.

## Cost-safety rules

The default `e2-standard-2` VM is intentionally large enough for Kafka,
PostgreSQL, XGBoost and SHAP, but it is not an always-free machine.

Use it only with a Google Cloud account that visibly says **Free Trial** in the
Billing Overview. Google currently states that a non-upgraded trial is not
billed and stops when its credit or time expires. Do not press **Activate** or
upgrade to a paid account if personal spending must remain impossible.

Also follow these practical rules:

1. Use a separate project created only for this experiment.
2. Create ordinary budget alerts, while remembering that alerts are not a
   universal hard spending cap.
3. Run the VM only while deploying, testing or collecting evidence.
4. Export reviewed evidence before destroying the VM.
5. Run `terraform destroy` as soon as the cloud experiment is finished.
6. Confirm in the Google Cloud console that the VM and disk are gone.

Official references:

- [Google Cloud Free Trial](https://cloud.google.com/free/docs/free-cloud-features)
- [Why ordinary budgets do not automatically cap all spending](https://cloud.google.com/billing/docs/how-to/budgets)
- [Disable billing for a project](https://cloud.google.com/billing/docs/how-to/modify-project)

## Files in this deployment

| File | Human explanation |
| --- | --- |
| `compose.yml` | Runs the complete application on one VM |
| `Dockerfile.app` | Builds one shared Python runtime for API, listener and worker |
| `Dockerfile.frontend` | Builds React and serves it through Nginx |
| `.env.gcp.example` | Documents every required value without containing secrets |
| `terraform/` | Creates the VM, narrow firewall rules and monitoring identity |
| `monitoring/ops-agent.yaml` | Sends private metrics and Docker logs to Google |
| `monitoring/dashboard.json` | Creates the initial dissertation dashboard |
| `scripts/train-model.sh` | Reproduces the reviewed model artifact |
| `scripts/deploy.sh` | Validates, builds and starts the pipeline |
| `scripts/verify-deployment.sh` | Performs safe checks without printing secrets |
| `scripts/collect-evidence.sh` | Captures one local experimental evidence bundle |
| `scripts/stop.sh` | Stops containers without deleting their data |

## 1. Prepare the Google Cloud account

Install the Google Cloud CLI and Terraform on your own computer. Then log in:

```bash
gcloud auth login
gcloud auth application-default login
```

Create or choose a dedicated Free Trial project and store its ID:

```bash
export CLAIMS_GCP_PROJECT="your-free-trial-project-id"
gcloud config set project "${CLAIMS_GCP_PROJECT}"
```

Terraform uses Application Default Credentials from the second login command.
The project owner also needs permission to connect through Identity-Aware Proxy.
If that role is not already available, grant it to the Google account that will
open the SSH session. OS Admin Login also gives that account the `sudo` access
needed to install the monitoring configuration:

```bash
gcloud projects add-iam-policy-binding "${CLAIMS_GCP_PROJECT}" \
  --member="user:YOUR_GOOGLE_ACCOUNT_EMAIL" \
  --role="roles/iap.tunnelResourceAccessor"

gcloud projects add-iam-policy-binding "${CLAIMS_GCP_PROJECT}" \
  --member="user:YOUR_GOOGLE_ACCOUNT_EMAIL" \
  --role="roles/compute.osAdminLogin"
```

## 2. Create the VM

From this repository:

```bash
cd deploy/gcp/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and replace the project placeholder. Then review before
creating anything:

```bash
terraform init
terraform fmt -check
terraform validate
terraform plan
```

Only after the plan shows one expected research VM, two firewall rules, one
service account and its narrow log/metric permissions:

```bash
terraform apply
```

Terraform prints the temporary application URL and an IAP SSH command. The
ephemeral IP can change after the VM is stopped and started; this avoids paying
for a reserved unused address.

## 3. Put the reviewed code on the VM

Connect using the `iap_ssh_command` Terraform output. On the VM, clone the
reviewed branch or commit that contains this deployment:

```bash
cd /opt
sudo git clone \
  https://github.com/ganesh1997oli/Decentralized-Claims-Registry.git \
  decentralized-claims-registry
sudo chown -R "${USER}:${USER}" decentralized-claims-registry
cd decentralized-claims-registry
```

Do not copy `.env.local`, Terraform state, unreviewed evidence or unrelated
working files to the VM.

## 4. Configure secrets locally on the VM

```bash
cp deploy/gcp/.env.gcp.example deploy/gcp/.env.gcp
chmod 600 deploy/gcp/.env.gcp
```

Edit the copied file and replace every `CHANGE_ME` value. Useful secret
generators are:

```bash
openssl rand -hex 24
openssl rand -hex 32
```

Generate one independent insurer credential at a time from the repository root:

```bash
python backend/scripts/generate_insurer_credential.py \
  northstar-mutual northstar-cloud-v1 --daily-quota 25
```

Give the printed raw key only to that synthetic insurer operator. Put only the
printed digest entry in the `INSURER_CREDENTIALS_JSON` list and repeat for each
insurer. Generate `CLAIM_AUTHORIZATION_KEY` with `openssl rand -hex 32`; it is
shared only by the backend and scoring worker so the worker can reject an IPFS
document that did not pass through the authenticated gateway.

Keep `CLAIMS_DEPLOYMENT_ID="sepolia-security-audit-v1"` for the reviewed
hardened contract bundled into the image. The API, listener, and worker all use
this same selector and refuse a legacy or incompatible artifact.

Use only:

- fictional claim data;
- separate Sepolia-only submitter and assessor wallets with only their intended
  contract roles and enough test ETH for their writes;
- a Pinata token intended for this research project;
- a unique PostgreSQL password;
- unique digest-only insurer credential entries and a claim-authorization key;
- a unique duplicate-fingerprint HMAC key.

Do not put the deployment/admin key on the VM. FastAPI receives only
`SEPOLIA_SUBMITTER_PRIVATE_KEY`, while the worker receives only
`SEPOLIA_ASSESSOR_PRIVATE_KEY`.

The default per-IP, per-insurer, and daily quota counters are process-local and
fit this deployment's single FastAPI process. They reset on restart. A
multi-process or multi-VM deployment must replace them with a shared atomic
store. Nginx and FastAPI both enforce the default 16 KiB claim request limit;
keep `client_max_body_size` and `MAX_CLAIM_BODY_BYTES` aligned if it changes.

If the VM's external IP changes, update `FRONTEND_ORIGINS` before rebuilding or
restarting the API.

## 5. Reproduce the model artifact

The trained artifact is intentionally ignored by Git. Build it inside the same
Python image that serves predictions:

```bash
deploy/gcp/scripts/train-model.sh
```

This downloads the pinned research dataset, trains the existing leakage-aware
pipeline and writes `model.joblib` plus checksum metadata into the configured
host directory. The scoring worker later mounts that directory read-only.

Review [the model results](../../model/RESULTS.md) before using the artifact.

## 6. Deploy and verify

Start everything:

```bash
deploy/gcp/scripts/deploy.sh
```

The first build downloads Python, Node, Kafka and PostgreSQL images, so it takes
longer than later deployments. Check the result:

```bash
deploy/gcp/scripts/verify-deployment.sh
```

Useful read-only operational commands are:

```bash
docker compose \
  --env-file deploy/gcp/.env.gcp \
  -f deploy/gcp/compose.yml \
  ps

docker compose \
  --env-file deploy/gcp/.env.gcp \
  -f deploy/gcp/compose.yml \
  logs --follow listener scoring-worker
```

Open the `application_url` Terraform output in a browser and submit fictional
test claims. Never submit a name, address, real policy, photograph or real
insurance evidence.

## 7. Send metrics and logs to Google Cloud

The VM startup script installs the Ops Agent. Once the containers are running,
install the repository configuration:

```bash
deploy/gcp/scripts/install-ops-agent-config.sh
```

Confirm the agent:

```bash
sudo systemctl status google-cloud-ops-agent --no-pager
```

Create the prepared dashboard from any machine with `gcloud` authenticated:

```bash
gcloud monitoring dashboards create \
  --project "${CLAIMS_GCP_PROJECT}" \
  --config-from-file deploy/gcp/monitoring/dashboard.json
```

The Ops Agent scrapes once per minute and keeps only the small set of metrics
needed for the research questions.

## What each metric means

| Metric | Plain-language meaning |
| --- | --- |
| `claims_listener_block_lag` | Confirmed Sepolia blocks not yet processed |
| `claims_listener_poll_errors_total` | Recoverable RPC, IPFS, Kafka or checkpoint errors |
| `claims_listener_kafka_publications_total` | Verified events acknowledged by Kafka |
| `claims_scoring_events_total{outcome}` | Successfully handled or failed Kafka events |
| `claims_scoring_model_inference_seconds` | XGBoost prediction plus SHAP explanation time |
| `claims_scoring_processing_seconds` | Whole handler, including IPFS, database and Sepolia |
| `kafka_consumergroup_lag` | Kafka messages waiting for the scoring consumer |

The 500 ms research target should be evaluated against
`claims_scoring_model_inference_seconds`, preferably using p95. It should not be
applied to `claims_scoring_processing_seconds`, because that second measurement
includes public IPFS calls and waiting for an Ethereum transaction receipt.

Useful PromQL checks in Metrics Explorer are:

```promql
claims_listener_block_lag
```

```promql
sum(kafka_consumergroup_lag{consumergroup="claims-registry-scorer-v1"})
```

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(claims_scoring_model_inference_seconds_bucket[10m])
  )
)
```

```promql
sum by (outcome) (increase(claims_scoring_events_total[10m]))
```

Recommended dissertation alerts are:

- listener block lag remains above 3 for 10 minutes;
- Kafka consumer lag remains above 5 for 10 minutes;
- any failed scoring event appears;
- model inference p95 exceeds 0.5 seconds;
- a listener, worker or Kafka `up` metric becomes zero.

Alerts demonstrate operational thinking, but a short research run can also
record these conditions through screenshots and saved queries.

## 8. Collect evidence

After a controlled test run:

```bash
deploy/gcp/scripts/collect-evidence.sh
```

This creates an ignored timestamped folder containing:

- container state;
- the last 500 listener and worker log lines;
- listener metrics;
- scoring metrics;
- Kafka metrics;
- the UTC collection time.

Review the files before moving selected, redacted evidence into the
dissertation. Transaction hashes are public, but wallet keys, Pinata tokens,
database passwords and HMAC keys must never appear.

## 9. Stop or remove the experiment

To stop only the application containers while keeping their named volumes:

```bash
deploy/gcp/scripts/stop.sh
```

Stopping containers does not stop Compute Engine billing. Stop the VM from the
Google Cloud console or with `gcloud compute instances stop` when taking a short
break. Remember that the ephemeral public IP can change.

When the experiment is complete, first copy the reviewed evidence elsewhere,
then remove the entire Terraform deployment:

```bash
cd deploy/gcp/terraform
terraform destroy
```

Finally verify in the console that the Compute Engine VM and its boot disk no
longer exist. Terraform intentionally sets the boot disk to auto-delete with the
VM. The local Terraform state and `.env.gcp` remain private files and should
also be removed when no longer needed.

## Troubleshooting

### The scoring worker keeps restarting

Check:

```bash
docker compose \
  --env-file deploy/gcp/.env.gcp \
  -f deploy/gcp/compose.yml \
  logs scoring-worker
```

The usual causes are:

- model artifacts have not been trained;
- the checksum in `metadata.json` does not match `model.joblib`;
- the PostgreSQL password contains URL-special characters;
- the Sepolia wallet lacks assessor permission;
- the configured RPC endpoint is temporarily unavailable.

### The listener does not publish old claims

On its first start, the listener begins from the newest safely confirmed block
unless `LISTENER_START_BLOCK` is intentionally set. To backfill a known range,
stop the listener, set the desired start block, and use a fresh checkpoint only
after recording why that change was made. Do not casually delete the checkpoint.

### Metrics exist locally but not in Cloud Monitoring

Check all three private endpoints:

```bash
curl http://127.0.0.1:9101/metrics
curl http://127.0.0.1:9102/metrics
curl http://127.0.0.1:9308/metrics
```

Then inspect the agent:

```bash
sudo systemctl status google-cloud-ops-agent --no-pager
sudo journalctl -u google-cloud-ops-agent -n 100 --no-pager
```

Prometheus samples arrive in Cloud Monitoring under
`prometheus.googleapis.com` and are queried most easily with PromQL.

### The browser cannot reach the site

Confirm:

- the Terraform HTTP firewall exists;
- the frontend container is healthy;
- the VM external IP matches the URL;
- port 80 is not occupied by another host process.

Use `verify-deployment.sh` on the VM before debugging the public network. Its
read-only output includes the selected deployment ID, chain ID, and contract
address so configuration drift is visible without printing secrets.

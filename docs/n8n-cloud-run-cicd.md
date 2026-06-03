# n8n + Processing API on Cloud Run

This guide is for a first-time setup. It deploys one Cloud Run service with two containers:

- `n8n`: public ingress container. The backend calls this service.
- `processing-api`: internal FastAPI sidecar. It is not exposed publicly; n8n calls it at `http://127.0.0.1:8000`.

The GitHub Actions pipeline builds both images, deploys the multi-container Cloud Run service, imports the workflow JSON files from `n8n/workflows/`, and publishes `LegalFam Message Flow`.

## Files Added To The Repo

- `Dockerfile.n8n`: n8n image with bundled workflow JSON files and sync script.
- `api/Dockerfile`: FastAPI processing image.
- `.dockerignore`: prevents `.env`, `secrets/`, `work/`, and unrelated files from entering the n8n build context.
- `scripts/sync-n8n-workflows.sh`: imports workflows and publishes configured workflow IDs.
- `.github/workflows/deploy-n8n.yml`: CI/CD workflow for Cloud Run.

## 1. Confirm GCP Values

In the GCP web console, confirm these values before running commands:

- Project ID.
- Cloud SQL PostgreSQL instance ID.
- Cloud SQL region.
- GitHub repo full name, for example `your-user/agentic-flow`.
- Whether you will use a custom domain for n8n or the generated Cloud Run URL.

Use the same region as your Cloud SQL instance when possible.

## 2. Set CLI Variables

Run this in Google Cloud Shell or a local terminal authenticated with `gcloud auth login`.

```sh
export PROJECT_ID="legalfam-497502"
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
export REGION="us-central1"
export ARTIFACT_REPOSITORY="legalfam"
export SQL_INSTANCE="legalfam"
export CLOUD_SQL_CONNECTION_NAME="$PROJECT_ID:$REGION:$SQL_INSTANCE"
export GITHUB_REPO="LegalFam/agentic-flow"

export N8N_DB_NAME="n8n"
export N8N_DB_USER="n8n"
export N8N_SERVICE="legalfam-n8n"
export N8N_SYNC_JOB="legalfam-n8n-sync"

export RUNTIME_SA_NAME="legalfam-n8n-runtime"
export DEPLOY_SA_NAME="legalfam-github-deployer"
export RUNTIME_SA="$RUNTIME_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
export DEPLOY_SA="$DEPLOY_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
```

## 3. Create Artifact Registry

```sh
gcloud artifacts repositories create "$ARTIFACT_REPOSITORY" \
  --repository-format=docker \
  --location="$REGION"
```

## 4. Create The n8n Cloud SQL Database

Use your existing Cloud SQL PostgreSQL instance.

```sh
gcloud sql databases create "$N8N_DB_NAME" --instance="$SQL_INSTANCE"

gcloud sql users create "$N8N_DB_USER" \
  --instance="$SQL_INSTANCE" \
  --password="replace-with-a-strong-password"
```

Keep the password. You need the exact same value in the next step.

## 5. Create Secret Manager Secrets

```sh
printf 'replace-with-the-n8n-db-password' | gcloud secrets create n8n-db-password --data-file=-
printf 'replace-with-a-stable-32-plus-char-random-key' | gcloud secrets create n8n-encryption-key --data-file=-
printf 'replace-with-your-gemini-api-key' | gcloud secrets create gemini-api-key --data-file=-
```

Important: do not rotate or replace `n8n-encryption-key` after n8n credentials exist. n8n uses it to decrypt stored credentials.

## 6. Create Service Accounts And IAM

```sh
gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
  --display-name="LegalFam n8n Runtime"

gcloud iam service-accounts create "$DEPLOY_SA_NAME" \
  --display-name="LegalFam GitHub Deployer"
```

Runtime service account permissions:

```sh
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/cloudsql.client"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUNTIME_SA" \
  --role="roles/documentai.apiUser"
```

Deployment service account permissions:

```sh
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA" \
  --role="roles/artifactregistry.writer"

gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:$DEPLOY_SA" \
  --role="roles/iam.serviceAccountUser"
```

## 7. Configure GitHub Actions Authentication

This lets GitHub Actions deploy to GCP without storing a service account key.

```sh
gcloud iam workload-identity-pools create github-pool \
  --location=global \
  --display-name="GitHub Actions Pool"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global \
  --workload-identity-pool=github-pool \
  --display-name="GitHub Provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='$GITHUB_REPO' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/$GITHUB_REPO"
```

Get the provider name. You will paste this into GitHub secrets.

```sh
gcloud iam workload-identity-pools providers describe github-provider \
  --location=global \
  --workload-identity-pool=github-pool \
  --format="value(name)"
```

## 8. Add GitHub Actions Repository Variables

In GitHub:

```text
Repository > Settings > Secrets and variables > Actions > Variables
```

Use **Repository variables**.

Do **not** create a GitHub Environment for this setup. The workflow does not use `environment: ...`, so GitHub Environment variables will not be read by the pipeline.

Add:

```text
GCP_PROJECT_ID=your-gcp-project-id
GCP_REGION=us-central1
GCP_ARTIFACT_REPOSITORY=legalfam
N8N_CLOUD_RUN_SERVICE=legalfam-n8n
N8N_SYNC_JOB=legalfam-n8n-sync
N8N_PUBLIC_URL=https://your-n8n-domain-or-temporary-placeholder
N8N_PUBLISH_WORKFLOW_IDS=zrMqCWRq1V6lsI7i
CLOUD_SQL_CONNECTION_NAME=your-gcp-project-id:us-central1:your-cloud-sql-instance-id
N8N_DB_NAME=n8n
N8N_DB_USER=n8n
N8N_RUNTIME_SERVICE_ACCOUNT=legalfam-n8n-runtime@your-gcp-project-id.iam.gserviceaccount.com
```

Add these Python sidecar variables:

```text
GEMINI_MODEL=gemini-2.5-flash
ENABLE_GEMINI_FILE_SEARCH_UPLOAD=false
MAX_UPLOAD_MB=75
MIN_TEXT_CHARS_FOR_NATIVE_PDF=800
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
DOCUMENT_AI_LOCATION=us
DOCUMENT_AI_PROCESSOR_ID=your-document-ai-processor-id
```

`GEMINI_FILE_SEARCH_STORE` can be left empty unless you want a default store at the API level.

If you do not know the final Cloud Run URL yet, set `N8N_PUBLIC_URL` to a temporary value for the first run, then update it after Cloud Run creates the service URL and rerun the workflow.

## 9. Add GitHub Actions Repository Secrets

In GitHub:

```text
Repository > Settings > Secrets and variables > Actions > Secrets
```

Use **Repository secrets**.

Do **not** use Environment secrets unless you later modify `.github/workflows/deploy-n8n.yml` to include an explicit environment.

Add:

```text
GCP_WIF_PROVIDER=projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider
GCP_DEPLOY_SERVICE_ACCOUNT=legalfam-github-deployer@your-gcp-project-id.iam.gserviceaccount.com
```

Use the exact `GCP_WIF_PROVIDER` value returned by the command in step 8.

If you later want protected deployments or separate staging/production config, create a GitHub Environment and add this to the `deploy` job:

```yaml
environment: production
```

Until then, use only repository-level variables and secrets.

## 10. Commit And Push

From `agentic-flow`:

```sh
git add .dockerignore Dockerfile.n8n .github/workflows/deploy-n8n.yml scripts/sync-n8n-workflows.sh docs/n8n-cloud-run-cicd.md n8n/workflows
git commit -m "ci: deploy n8n with processing sidecar"
git push origin main
```

GitHub Actions will run `Deploy n8n to Cloud Run`.

## 11. Fix The Public URL After First Deploy

If you are using the generated Cloud Run URL:

1. Open GCP web console.
2. Go to `Cloud Run`.
3. Open `legalfam-n8n`.
4. Copy the service URL.
5. In GitHub Actions variables, set `N8N_PUBLIC_URL` to that URL.
6. In GitHub Actions, manually run `Deploy n8n to Cloud Run` again.

This second run makes n8n generate production webhook URLs with the correct public base URL.

## 12. Verify Cloud Run Containers

In GCP web console:

```text
Cloud Run > legalfam-n8n > Revisions > Containers
```

You should see:

```text
n8n
processing-api
```

Only `n8n` should be the ingress container. The Python API does not get its own Cloud Run service URL.

## 13. Bootstrap n8n Credentials Once

Open the deployed n8n instance and create or import these credentials:

```text
Google Gemini(PaLM) Api account
Header Auth account
Google Drive account
```

The workflow JSON references credentials by ID/name. Once credentials exist in the n8n database, later pushes to `main` can update and republish workflows without using the n8n UI.

## 14. Backend Webhook

Configure the backend to call:

```text
POST https://your-n8n-url/webhook/chat-process
```

Expected body:

```json
{
  "session_id": "uuid",
  "message": "consulta del usuario"
}
```

Use the header required by the n8n `Header Auth account` credential.

## 15. Internal Python API Address

Inside n8n workflows, the processing API is reached through:

```text
http://127.0.0.1:8000
```

The CI/CD workflow sets:

```text
PROCESSING_API_BASE_URL=http://127.0.0.1:8000
```

Local Docker Compose still works because the workflow JSON falls back to:

```text
http://processing-api:8000
```

## 16. Future Updates

Pushes to `main` trigger deployment when these paths change:

```text
Dockerfile.n8n
api/**
n8n/workflows/**
scripts/sync-n8n-workflows.sh
.github/workflows/deploy-n8n.yml
```

Python changes rebuild the processing sidecar. Workflow changes are imported and the configured workflow IDs are published again.

## 17. Troubleshooting

### `Dependent container 'processing-api' must have startup probe specified`

This means Cloud Run accepted the multi-container shape, but rejected the deployment because `n8n` depends on `processing-api` and the dependent container needs a startup probe.

The GitHub Actions workflow must include this flag under the `processing-api` container:

```sh
--startup-probe "httpGet.path=/health,httpGet.port=8000,initialDelaySeconds=0,periodSeconds=10,timeoutSeconds=5,failureThreshold=12"
```

The FastAPI service already exposes:

```text
GET /health
```

If this error appears, commit the workflow fix and rerun GitHub Actions. You do not need to manually create the Cloud Run service.

### `Skipped validating Cloud SQL API...`

This warning can appear when `gcloud` cannot contact the Service Usage API during validation. It is not the failure shown above. Still confirm these APIs are enabled in GCP:

```text
Cloud SQL Admin API
Cloud Run Admin API
Service Usage API
```

### `Database connection timed out`

This means n8n reached the Cloud SQL connector but PostgreSQL did not answer fast enough or the connection was interrupted.

The deployment sets:

```text
DB_POSTGRESDB_CONNECTION_TIMEOUT=60000
DB_PING_INTERVAL_SECONDS=10
```

If the error continues after redeploying, check:

```sh
gcloud sql databases list --instance=legalfam --project=legalfam-497502
gcloud sql users list --instance=legalfam --project=legalfam-497502
```

Also confirm the runtime service account has:

```text
roles/cloudsql.client
roles/secretmanager.secretAccessor
```

If credentials and IAM are correct but timeouts continue, the Cloud SQL tier may be too small for n8n startup/migrations. Upgrade the Cloud SQL instance tier before debugging application code.

### `Cannot GET /`

If `/` returns `Cannot GET /` after a deploy:

1. Confirm GitHub variable `N8N_PUBLIC_URL` is the real Cloud Run URL, not `https://temporary-placeholder`.
2. Rerun the GitHub Actions workflow after changing `N8N_PUBLIC_URL`.
3. Try an incognito browser session or clear cookies for the Cloud Run domain.
4. Try direct n8n routes:

```text
https://your-n8n-url/signin
https://your-n8n-url/setup
https://your-n8n-url/home/workflows
```

If those routes also return `Cannot GET`, read Cloud Run logs and verify the request is reaching the `n8n` container, not the `processing-api` sidecar.

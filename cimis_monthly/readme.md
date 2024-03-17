### Deploying the cloud function

Before deploying or calling the cloud functions, the "project" can be set once with the following call, or passed to each gcloud call.

```
gcloud config set project openet
```

The following are the parameters that were set when deploying the function for the first time.  Subsequent deployments only need the project if not set above.

```
gcloud functions deploy cimis-reference-et-monthly-v1 --project openet --runtime python311 --region us-central1 --entry-point cron_scheduler --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --service-account="openet-assets-queue@openet.iam.gserviceaccount.com" --max-instances 1 --set-env-vars FUNCTION_REGION=us-central1
```

### Calling the cloud function

```
gcloud functions call cimis-reference-et-monthly-v1 --project openet
```

```
gcloud functions call cimis-reference-et-monthly-v1 --project openet --data '{"start":"2023-09-01", "end":"2023-09-30"}'
```

### Scheduling the job

```
gcloud scheduler jobs update http cimis-reference-et-monthly-v1 --schedule "15 6 2-6 * *" --uri "https://us-central1-openet.cloudfunctions.net/cimis-reference-et-monthly-v1" --description "Update Monthly CIMIS Reference ET" --http-method POST --time-zone "UTC" --project openet --location us-central1 --max-retry-attempts 3 --attempt-deadline 300s --min-backoff=20s
```

# GRIDMET Monthly Bias Corrected Reference ET Assets

### Set the project ID

Before deploying or calling the cloud functions, the "project" can be set once with the following call, or passed to each gcloud call.

```
gcloud config set project openet
```

### Deploying the cloud function

```
gcloud functions deploy gridmet-reference-et-monthly-v1 --project openet --runtime python311 --region us-central1 --entry-point cron_scheduler --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --service-account="openet-assets-queue@openet.iam.gserviceaccount.com" --max-instances 1 --set-env-vars FUNCTION_REGION=us-central1
```

### Calling the cloud function

```
gcloud functions call gridmet-reference-et-monthly-v1 --project openet -data '{"start":"2021-01-01", "end":"2021-05-31"}'
```

```
gcloud functions call gridmet-reference-et-monthly-v1 --project openet
```

### Scheduling the job

```
gcloud scheduler jobs create http gridmet-reference-et-monthly-v1 --schedule "4 6 5,15,25 * *" --uri "https://us-central1-openet.cloudfunctions.net/gridmet-reference-et-monthly-v1" --description "Update Monthly Bias Corrected GRIDMET Reference ET" --http-method POST --time-zone "UTC" --project openet --location us-central1 --max-retry-attempts 3 --attempt-deadline 300s --min-backoff=20s
```

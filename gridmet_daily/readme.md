# GRIDMET Daily Bias Corrected Reference ET

### Set the project ID

Before deploying or calling the cloud functions, the "project" can be set once with the following call, or passed to each gcloud call.

```
gcloud config set project openet
```

### Deploying the cloud function

```
gcloud functions deploy gridmet-reference-et-daily-v1 --project openet --runtime python37 --region us-central1 --entry-point cron_scheduler --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --service-account="openet-assets-queue@openet.iam.gserviceaccount.com" --max-instances 1
```

### Calling the cloud function
```
gcloud functions call gridmet-reference-et-daily-v1 --project openet
```

### Scheduling the job

```
gcloud scheduler jobs update http gridmet-reference-et-daily-v1 --schedule "2 12 * * *" --uri "https://us-central1-openet.cloudfunctions.net/gridmet-reference-et-daily-v1" --description "Update Daily Bias Corrected GRIDMET Reference ET" --http-method POST --time-zone "UTC" --project openet --location us-central1 --max-retry-attempts 3 --attempt-deadline 300s --min-backoff=20s
```

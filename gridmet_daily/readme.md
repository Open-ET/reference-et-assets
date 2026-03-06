# GRIDMET Daily Bias Corrected Reference ET

### Deploying the cloud function

```
gcloud functions deploy gridmet-reference-et-daily-v1 --project openet --no-gen2 --runtime python311 --region us-central1 --entry-point update --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --service-account="openet-assets-queue@openet.iam.gserviceaccount.com" --max-instances 1 --set-env-vars FUNCTION_REGION=us-central1
```

### Calling the cloud function

```
gcloud functions call gridmet-reference-et-daily-v1 --project openet --data '{"start":"2021-01-01", "end":"2021-05-31"}'
```

```
gcloud functions call gridmet-reference-et-daily-v1 --project openet
```

### Scheduling the job

```
gcloud scheduler jobs update http gridmet-reference-et-daily-v1 --schedule "5 12 * * *" --uri "https://us-central1-openet.cloudfunctions.net/gridmet-reference-et-daily-v1" --description "Update Daily Bias Corrected GRIDMET Reference ET" --http-method POST --time-zone "UTC" --project openet --location us-central1 --max-retry-attempts 3 --attempt-deadline 300s --min-backoff=20s
```

# NLDAS-2 Daily Bias Corrected Reference ET

### Deploying the cloud function

```
gcloud functions deploy nldas-reference-et-daily-v0 --project openet --no-gen2 --runtime python311 --region us-central1 --entry-point update --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --service-account="openet-assets-queue@openet.iam.gserviceaccount.com" --max-instances 1 --set-env-vars FUNCTION_REGION=us-central1
```

### Calling the cloud function

```
gcloud functions call nldas-reference-et-daily-v1 --project openet
```

### Scheduling the job

```
gcloud scheduler jobs update http nldas-reference-et-daily-v0 --schedule "5 12 * * *" --uri "https://us-central1-openet.cloudfunctions.net/nldas-reference-et-daily-v1" --description "Update Daily Bias Corrected NLDAS Reference ET" --http-method POST --time-zone "UTC" --project openet --location us-central1 --max-retry-attempts 3 --attempt-deadline 300s --min-backoff=20s
```

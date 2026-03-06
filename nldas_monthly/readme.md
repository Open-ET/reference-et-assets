# NLDAS-2 Monthly Bias Corrected Reference ET Assets

### Deploying the cloud function

```
gcloud functions deploy nldas-reference-et-monthly-v0 --project openet --no-gen2 --runtime python311 --region us-central1 --entry-point update --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --service-account="openet-assets-queue@openet.iam.gserviceaccount.com" --max-instances 1 --set-env-vars FUNCTION_REGION=us-central1
```

### Calling the cloud function

```
gcloud functions call nldas-reference-et-monthly-v0 --project openet -data '{"start":"2021-01-01", "end":"2021-05-31"}'
```

```
gcloud functions call nldas-reference-et-monthly-v0 --project openet
```

### Scheduling the job

```
gcloud scheduler jobs create http nldas-reference-et-monthly-v0 --schedule "5 6 5,15,25 * *" --uri "https://us-central1-openet.cloudfunctions.net/nldas-reference-et-monthly-v0" --description "Update Monthly Bias Corrected NLDAS Reference ET" --http-method POST --time-zone "UTC" --project openet --location us-central1 --max-retry-attempts 3 --attempt-deadline 300s --min-backoff=20s
```

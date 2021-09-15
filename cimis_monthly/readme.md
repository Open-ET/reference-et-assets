### Deploying the cloud function

Before deploying or calling the cloud functions, the "project" can be set once with the following call, or passed to each gcloud call.
```
gcloud config set project openet
```

The following are the parameters that were set when deploying the function for the first time.  Subsequent deployments only need the project if not set above.
```
gcloud functions deploy cimis-reference-et-monthly --project openet --runtime python37 --entry-point cron_scheduler --trigger-http --allow-unauthenticated --memory 512 --timeout 240 --max-instances 1
```

### Calling the cloud function
```
gcloud functions call cimis-reference-et-monthly --project openet
```

### Scheduling the job

```
gcloud scheduler jobs update http cimis-reference-et-monthly --schedule "10 6 5,15,25 * *" --uri "https://us-central1-openet.cloudfunctions.net/cimis-reference-et-monthly" --description "Update Monthly CIMIS Reference ET" --http-method POST --time-zone "UTC" --project openet --max-retry-attempts 3
```

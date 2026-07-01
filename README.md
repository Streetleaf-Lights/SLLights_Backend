# Azure Functions Project (Python)

A Python Azure Functions project using the v2 programming model, with a starter HTTP-triggered function.

## Project structure

```
azure-functions-project/
├── function_app.py       # Function definitions (v2 model - all functions live here)
├── host.json              # Runtime configuration
├── local.settings.json    # Local dev settings (not committed to git)
├── requirements.txt       # Python dependencies
└── .gitignore
```

## Prerequisites

- [Python 3.9–3.11](https://www.python.org/downloads/) (check current supported versions in Azure docs)
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (for deployment)
- An Azure account with an active subscription (for deployment)

## Local setup

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # on Windows: .venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Start the Functions host locally:
   ```bash
   func start
   ```

4. Test the HTTP trigger:
   ```bash
   curl "http://localhost:7071/api/hello?name=World"
   ```
   You should get back a JSON response.

## Adding more functions

With the Python v2 model, add new functions directly in `function_app.py` using decorators, e.g.:

```python
@app.route(route="another-endpoint")
def another_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("Hello from another function!")
```

Other trigger types you can add similarly:
- `@app.timer_trigger(schedule="0 */5 * * * *", arg_name="myTimer")` — Timer trigger
- `@app.blob_trigger(arg_name="myblob", path="mycontainer/{name}", connection="AzureWebJobsStorage")` — Blob trigger
- `@app.queue_trigger(arg_name="msg", queue_name="myqueue", connection="AzureWebJobsStorage")` — Queue trigger

## Deploying to Azure

1. Log in:
   ```bash
   az login
   ```

2. Create the required Azure resources (resource group, storage account, and function app):
   ```bash
   az group create --name my-functions-rg --location eastus

   az storage account create \
     --name mystorageacct$RANDOM \
     --location eastus \
     --resource-group my-functions-rg \
     --sku Standard_LRS

   az functionapp create \
     --resource-group my-functions-rg \
     --consumption-plan-location eastus \
     --runtime python \
     --runtime-version 3.11 \
     --functions-version 4 \
     --name my-unique-function-app-name \
     --storage-account mystorageacctXXXX \
     --os-type Linux
   ```

3. Deploy your code:
   ```bash
   func azure functionapp publish my-unique-function-app-name
   ```

## Notes

- `local.settings.json` holds secrets/connection strings for local development only — it's excluded from git via `.gitignore` and is never deployed.
- `host.json` controls the Functions runtime behavior; app-level settings (env vars) belong in `local.settings.json` locally and in the Function App's Configuration blade in Azure once deployed.
- The default auth level on the HTTP trigger is `FUNCTION`, meaning a function key is required when calling the deployed endpoint. Change to `func.AuthLevel.ANONYMOUS` in `function_app.py` if you want a public endpoint (only do this if that's intended).

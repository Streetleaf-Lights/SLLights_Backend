import azure.functions as func
import logging
import json

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="hello")
def hello(req: func.HttpRequest) -> func.HttpResponse:
    """
    Simple HTTP-triggered function.
    Call it with: GET/POST /api/hello?name=World
    or POST a JSON body: {"name": "World"}
    """
    logging.info("Python HTTP trigger function 'hello' processed a request.")

    name = req.params.get("name")

    if not name:
        try:
            req_body = req.get_json()
        except ValueError:
            req_body = None
        if req_body:
            name = req_body.get("name")

    if name:
        response_body = {"message": f"Hello, {name}! This HTTP triggered function executed successfully."}
        return func.HttpResponse(
            json.dumps(response_body),
            mimetype="application/json",
            status_code=200,
        )
    else:
        response_body = {
            "message": "This HTTP triggered function executed successfully. "
                        "Pass a name in the query string or in the request body for a personalized response."
        }
        return func.HttpResponse(
            json.dumps(response_body),
            mimetype="application/json",
            status_code=200,
        )

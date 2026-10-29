"""Deploy two Foundry prompt agents and one Logic Apps Automation workflow."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
ARM = "https://management.azure.com"
ARM_SCOPE = ARM + "/.default"
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
AUTOMATION_SCOPE = "api://de712aed-bfe7-4949-9833-aacfebd5fd5b/.default"
AUTOMATION_API = "2026-04-01-preview"
FOUNDRY_USER_ROLE = "53ca6127-db72-4b80-b1b0-d745d6d5456d"


def cli(tool, *args):
    executable = shutil.which(tool)
    if not executable:
        raise RuntimeError(f"Install {tool} and make it available on PATH.")
    result = subprocess.run(
        [executable, *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode:
        # Arguments can contain the signed callback URL; never print them.
        raise RuntimeError(f"{tool} {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class AzureRequestError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def request_json(url, method="GET", body=None, headers=None, timeout=120):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
            return (json.loads(content) if content else {}), dict(response.headers)
    except HTTPError as error:
        content = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(content)
            message = detail.get("error", detail)
            if isinstance(message, dict):
                message = message.get("message", str(message))
        except json.JSONDecodeError:
            message = content[:1000]
        raise AzureRequestError(
            error.code, f"{method} {urlsplit(url).path}: HTTP {error.code}: {message}"
        ) from None


class AzureApi:
    def __init__(self, subscription):
        self.subscription = subscription
        self.tokens = {}

    def request(self, url, method="GET", body=None, scope=ARM_SCOPE):
        token = self.tokens.get(scope)
        if token is None or int(token["expires_on"]) < time.time() + 120:
            token = json.loads(cli(
                "az", "account", "get-access-token", "--scope", scope,
                "--subscription", self.subscription, "--output", "json",
            ))
            self.tokens[scope] = token
        return request_json(
            url, method, body, {"Authorization": f"Bearer {token['accessToken']}"}
        )

    def wait(self, url, scope=ARM_SCOPE, timeout=900):
        deadline = time.monotonic() + timeout
        previous_state = None
        while time.monotonic() < deadline:
            result, _ = self.request(url, scope=scope)
            state = result.get("status", result.get("properties", {}).get("provisioningState"))
            if state and state.lower() == "succeeded":
                return result
            if state and state.lower() in {"failed", "canceled", "cancelled"}:
                raise RuntimeError(f"Azure operation failed: {json.dumps(result)}")
            if state != previous_state:
                print(f"Waiting for Azure operation: {state or 'pending'}...", flush=True)
                previous_state = state
            time.sleep(10)
        raise TimeoutError("Azure operation did not succeed within the deployment timeout.")


def save_outputs(values):
    environment = ROOT / ".azure" / os.environ["AZURE_ENV_NAME"] / ".env"
    if not environment.is_file():
        raise FileNotFoundError("The selected azd environment file does not exist.")
    cli("git", "check-ignore", "--quiet", str(environment))
    for name, value in values.items():
        cli("azd", "env", "set", name, str(value))
        os.environ[name] = str(value)


def deploy_agents(api):
    endpoint = os.environ["PROJECT_ENDPOINT"].rstrip("/")
    versions = {}
    for name in ("player", "gameMaster"):
        definition = json.loads((ROOT / "agents" / f"{name}.json").read_text())
        definition["model"] = os.environ["MODEL_DEPLOYMENT"]
        url = f"{endpoint}/agents/{name}/versions?api-version=v1"
        deadline = time.monotonic() + 300
        while True:
            try:
                agent, _ = api.request(
                    url, "POST", {"definition": definition}, FOUNDRY_SCOPE
                )
                break
            except AzureRequestError as error:
                if error.status != 403 or time.monotonic() >= deadline:
                    raise
                print("Waiting for Foundry role assignment propagation...", flush=True)
                time.sleep(10)
        versions[name] = str(agent["version"])
        print(f"Created {name} version {versions[name]}.", flush=True)
    save_outputs({
        "PLAYER_AGENT_VERSION": versions["player"],
        "GAME_MASTER_AGENT_VERSION": versions["gameMaster"],
    })
    return versions


def ensure_automation(api):
    print("Creating or reusing the Logic Apps Automation project...", flush=True)
    project_url = (
        f"{ARM}{os.environ['AUTOMATION_PROJECT_RESOURCE_ID']}"
        f"?api-version={AUTOMATION_API}"
    )
    try:
        project, _ = api.request(project_url)
    except AzureRequestError as error:
        if error.status != 404:
            raise
        project, headers = api.request(project_url, "PUT", {
            "location": os.environ["AUTOMATION_LOCATION"],
            "identity": {"type": "SystemAssigned"},
            "tags": {"azd-env-name": os.environ["AZURE_ENV_NAME"]},
            "properties": {"description": "Two Foundry agents playing guess my number."},
        })
        operation = next(
            (v for k, v in headers.items() if k.lower() == "azure-asyncoperation"), None
        )
        if operation:
            api.wait(operation)
    if project.get("properties", {}).get("provisioningState", "").lower() != "succeeded":
        api.wait(project_url)

    project_name = quote(os.environ["AUTOMATION_PROJECT_NAME"], safe="")
    print("Creating or reusing the Automation application...", flush=True)
    app_url = (
        f"https://auto.{os.environ['AUTOMATION_LOCATION']}.logic.azure.com"
        f"/automationProjects/{project_name}/applications/guessing-game"
    )
    query = f"?api-version={AUTOMATION_API}"
    try:
        app, _ = api.request(app_url + query, scope=AUTOMATION_SCOPE)
    except AzureRequestError as error:
        if error.status != 404:
            raise
        app, headers = api.request(app_url + query, "PUT", {}, AUTOMATION_SCOPE)
        operation = next(
            (v for k, v in headers.items() if k.lower() == "azure-asyncoperation"), None
        )
        if operation:
            api.wait(operation, AUTOMATION_SCOPE)
    if app.get("properties", {}).get("provisioningState", "").lower() != "succeeded":
        api.wait(app_url + query, AUTOMATION_SCOPE)
    app, _ = api.request(app_url + query, scope=AUTOMATION_SCOPE)
    principal = app.get("identity", {}).get("principalId")
    if not principal:
        raise RuntimeError("The Automation application has no managed identity.")
    scope = os.environ["FOUNDRY_PROJECT_RESOURCE_ID"]
    assignment = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}/{principal}/{FOUNDRY_USER_ROLE}")
    api.request(
        f"{ARM}{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment}"
        "?api-version=2022-04-01",
        "PUT", {"properties": {
            "roleDefinitionId": (
                f"/subscriptions/{api.subscription}/providers/"
                f"Microsoft.Authorization/roleDefinitions/{FOUNDRY_USER_ROLE}"
            ),
            "principalId": principal,
            "principalType": "ServicePrincipal",
        }},
    )
    return app_url


def publish_workflow(api, app_url, versions):
    print("Validating and publishing the Automation workflow...", flush=True)
    workflow = json.loads((ROOT / "automation" / "workflow.json").read_text())
    parameters = {
        "foundryProjectEndpoint": {
            "type": "String", "value": os.environ["PROJECT_ENDPOINT"].rstrip("/")
        },
        "playerAgentVersion": {"type": "String", "value": versions["player"]},
        "gameMasterAgentVersion": {"type": "String", "value": versions["gameMaster"]},
    }
    query = f"?api-version={AUTOMATION_API}"
    api.request(
        f"{app_url}/workflows/guessing-game/validate{query}", "POST",
        {"properties": {
            "definition": workflow["definition"],
            "kind": workflow["kind"],
            "parameters": parameters,
        }}, AUTOMATION_SCOPE,
    )
    deadline = time.monotonic() + 300
    while True:
        try:
            api.request(f"{app_url}/deployWorkflowArtifacts{query}", "POST", {
                "files": {"guessing-game/workflow.json": workflow, "parameters.json": parameters}
            }, AUTOMATION_SCOPE)
            break
        except AzureRequestError as error:
            transient = error.status == 409 or (
                error.status == 502 and "status 'Conflict'" in str(error)
            ) or (error.status == 503 and "The request timed out." in str(error))
            if not transient or time.monotonic() >= deadline:
                raise
            print("Automation host is updating; retrying artifact publication...", flush=True)
            time.sleep(10)

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            deployed, _ = api.request(
                f"{app_url}/workflows/guessing-game{query}", scope=AUTOMATION_SCOPE
            )
            properties = deployed.get("properties", {})
            if (properties.get("health", {}).get("state") == "Healthy"
                    and properties.get("files", {}).get("workflow.json") == workflow):
                callback, _ = api.request(
                    f"{app_url}/workflows/guessing-game/triggers/"
                    f"When_an_HTTP_request_is_received/listCallbackUrl{query}",
                    "POST", {}, AUTOMATION_SCOPE,
                )
                if urlsplit(callback["value"]).scheme != "https":
                    raise ValueError("Automation returned a non-HTTPS callback URL.")
                save_outputs({"LOGIC_APPS_WORKFLOW_URL": callback["value"]})
                return
        except AzureRequestError as error:
            if error.status != 404:
                raise
        time.sleep(5)
    raise TimeoutError("The published workflow and HTTP trigger did not become ready.")


def main():
    outputs = json.loads(cli("azd", "env", "get-values", "--output", "json"))
    os.environ.update({name: str(value) for name, value in outputs.items()})
    required = (
        "AZURE_ENV_NAME", "AZURE_SUBSCRIPTION_ID", "PROJECT_ENDPOINT",
        "FOUNDRY_PROJECT_RESOURCE_ID", "MODEL_DEPLOYMENT", "AUTOMATION_PROJECT_NAME",
        "AUTOMATION_PROJECT_RESOURCE_ID", "AUTOMATION_LOCATION",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError(f"Missing deployment outputs: {', '.join(missing)}")
    api = AzureApi(os.environ["AZURE_SUBSCRIPTION_ID"])
    versions = deploy_agents(api)
    app_url = ensure_automation(api)
    publish_workflow(api, app_url, versions)
    print("Verifying a complete game through Automation...", flush=True)
    subprocess.run(
        [sys.executable, str(ROOT / "automation" / "run.py")], cwd=ROOT, check=True
    )
    print("Two prompt agents and the Automation workflow are deployed.", flush=True)
    print(f"Open https://auto.azure.com/ and select {os.environ['AUTOMATION_PROJECT_NAME']}.")
    print("The signed callback URL is saved only in ignored azd state.")


if __name__ == "__main__":
    try:
        main()
    except (AzureRequestError, RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        sys.exit(1)

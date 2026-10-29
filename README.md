# Guessing game

Two [Microsoft Foundry prompt agents](https://ai.azure.com/) play guess my number,
orchestrated by **[Azure Logic Apps Automation](https://auto.azure.com/)** (preview).
Adapted from [microsoft-foundry-demo](https://github.com/arnaud-tincelin/microsoft-foundry-demo).

- **player** guesses an integer from 1 to 100 using binary search.
- **gameMaster** chooses a number and responds with higher, lower, or correct.
- One stateful **Automation workflow** alternates between the agents, stopping
  when the game ends or after ten guesses. A correct tenth guess wins.

No website, API backend, database, gateway, containers, or extra agents.

## Deploy

Install [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
[Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), and Python 3.10+.
On Windows, the hook also uses PowerShell 7. No Python packages are needed.
Authenticate once with `azd auth login` and `az login`, using the same account/tenant.

```powershell
azd up
```

Choose a subscription and **West US** (`westus`), which supports
both Foundry and Automation. Both use the same region. Other regions require
support for both services and the model. The subscription needs Automation preview
access, GPT-4.1 GlobalStandard quota (30 capacity units), and permission to create
resources and role assignments.

`azd up` creates a resource group, Foundry account/project, and shared GPT-4.1
deployment. Its post-deployment hook creates the two prompt agents, creates the
Automation project/application if absent, assigns its managed identity Foundry
User access on this project, and publishes the workflow with pinned agent versions.
It then runs a real game and checks the full transcript. Deployment fails if the
game cannot complete correctly. No portal import or manual agent creation is required.

Repeat `azd up` to update: existing Automation projects/applications are reused;
Foundry creates new agent versions, and the published workflow is pinned to them.
No unrelated resources or portal drafts are deleted.

## Play and read the conversation

1. Open **https://auto.azure.com/**.
2. Select the project named by `azd env get-value AUTOMATION_PROJECT_NAME`.
3. Open the **guessing-game** application, then the **guessing-game** workflow.
4. Choose **Test**, supply `{}`, and run. After an azd update, reload the designer;
   an older portal draft can differ from the published workflow.
5. In **Monitoring**, expand **Play up to ten turns**. Each **Player** and
   **Game master** action exposes its full request and response for that turn.
6. Open **Conversation** to see every guess and game-master reply together,
   ordered by turn. The HTTP response also returns this transcript, the secret,
   the game status, and the Foundry conversation/Automation run IDs.

To run the **published** workflow and print the dialogue locally:

```powershell
python .\automation\run.py
```

Both agents use the same Foundry conversation. The transcript variable is only
for display, not a second conversation or history replay. Raw prompts, structured
replies, and failure details remain visible on the workflow action cards.

## Files

| Path | Purpose |
| --- | --- |
| `agents\` | Two prompt-agent definitions and strict JSON output formats |
| `automation\workflow.json` | The stateful Automation workflow |
| `automation\run.py` | Optional caller and complete-transcript checks |
| `infra\` | Minimal Bicep infrastructure |
| `hooks\deploy.py` | Foundry agent creation and Automation publication |
| `azure.yaml` | One-command deployment |
| `tests\` | Dependency-free local tests |

## Notes

This is **Logic Apps Automation**, not a Consumption/Standard Logic App,
Azure Automation runbook, or Foundry workflow agent. It uses
`Microsoft.Logic/automationProjects` and the regional Automation application API.
The [Automation preview](https://learn.microsoft.com/azure/logic-apps/automation/quickstart-create-dynamic-automation-workflows)
can change its deployment API or regional availability.

Initial deployment runs returned `WorkflowAppOAuthTokenFailure` while obtaining
managed-identity tokens. The published workflow subsequently authenticated and
ran without changing credentials or role assignments. The exact cause was not
established; startup or identity propagation is a possibility, not a confirmed
diagnosis. Inspect run history before recreating resources or changing authentication.

The workflow calls the real versioned Foundry agents through the Responses API,
using the **application's managed identity** rather than API keys. HTTP actions
are used so both agents can share a conversation inside the Until loop.
Agent calls are not automatically retried, to avoid duplicate moves.

Game messages are deliberately **not secured/hidden** in run history. Anyone with
access to those inputs/outputs can read the conversation. Never put sensitive
data in these demo prompts. The signed HTTP callback is a bearer secret; it is
stored only in ignored `.azure` state. Do not share it or commit it.

Like the original demo, the master maintains its secret through prompt behavior
and conversation history, not a separate private state store. This cannot prove
that a hidden number was fixed before play. The workflow validates the final
result; the command-line caller additionally checks every hint against the
revealed number and rejects inconsistent games.

If execution fails, inspect the failed action in **Monitoring**. Allow time for
new role assignments to propagate. If a caller times out, inspect the original
run before retrying: it may still be running. Execution errors fail the workflow;
they are not reported as game losses. Published run history and designer draft
test history are separate.

## Local checks and cleanup

```powershell
python -m unittest discover -s tests
az bicep build --file .\infra\main.bicep --stdout > $null
```

When finished, `azd down` deletes the environment's resource group, including the
Automation project and its applications. Review the deletion prompt carefully.
Foundry soft-deleted account records can remain according to Azure's retention policy.

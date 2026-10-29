import copy
import importlib.util
import json
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("deploy", "hooks/deploy.py")
runner = load("runner", "automation/run.py")


def game_for(number, guesses=None):
    low, high = 1, 100
    turns = []
    for index in range(1, 11):
        guess = guesses[index - 1] if guesses else (low + high) // 2
        hint = "correct" if guess == number else "higher" if guess < number else "lower"
        status = "won" if hint == "correct" else "lost" if index == 10 else "playing"
        turns.append({
            "turn": index,
            "player": {"guess": guess},
            "gameMaster": {
                "guess": guess, "hint": hint, "status": status,
                "number_picked": None if status == "playing" else number,
                "message": hint,
            },
        })
        if status != "playing":
            break
        if hint == "higher":
            low = guess + 1
        else:
            high = guess - 1
    return {
        "status": status, "number_picked": number, "turns": len(turns),
        "conversation": turns, "conversation_id": "conv_test", "game_run_id": "run_test",
    }


class GameTests(unittest.TestCase):
    def test_binary_search_wins_for_every_secret(self):
        for number in range(1, 101):
            with self.subTest(number=number):
                game = runner.validate_game(game_for(number))
                self.assertEqual(game["status"], "won")
                self.assertLessEqual(game["turns"], 7)

    def test_tenth_correct_guess_wins(self):
        game = game_for(100, list(range(1, 10)) + [100])
        self.assertEqual(runner.validate_game(game)["status"], "won")

    def test_tenth_incorrect_guess_loses(self):
        game = game_for(100, list(range(1, 11)))
        self.assertEqual(runner.validate_game(game)["status"], "lost")

    def test_inconsistent_hint_is_rejected(self):
        game = game_for(42)
        game["conversation"][0]["gameMaster"]["hint"] = "higher"
        with self.assertRaises(ValueError):
            runner.validate_game(game)

    def test_early_reveal_is_rejected(self):
        game = game_for(42)
        game["conversation"][0]["gameMaster"]["number_picked"] = 42
        with self.assertRaises(ValueError):
            runner.validate_game(game)

    def test_excluded_guess_is_rejected(self):
        game = game_for(42)
        game["conversation"][1]["player"]["guess"] = 80
        with self.assertRaises(ValueError):
            runner.validate_game(game)

    def test_continuing_after_win_is_rejected(self):
        game = game_for(50)
        game["conversation"].append(copy.deepcopy(game["conversation"][0]))
        game["turns"] = 2
        with self.assertRaises(ValueError):
            runner.validate_game(game)

    def test_non_https_callback_is_rejected(self):
        with self.assertRaises(ValueError):
            runner.run_game("http://example.test/secret")

    def test_only_unaccepted_startup_404_is_retried(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(game_for(42)).encode()
        error = HTTPError("https://example.test/?sig=secret", 404, "Not Found", {}, io.BytesIO())
        with patch.object(runner, "urlopen", side_effect=[error, response]) as request:
            with patch.object(runner.time, "sleep"):
                runner.run_game("https://example.test/?sig=secret")
        self.assertEqual(request.call_count, 2)

    def test_workflow_failures_are_not_retried_or_logged_with_secret_urls(self):
        url = "https://example.test/?sig=test-secret"
        error = HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO())
        with patch.object(runner, "urlopen", side_effect=error) as request:
            with self.assertRaises(RuntimeError) as raised:
                runner.run_game(url)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("test-secret", str(raised.exception))

    def test_network_failure_does_not_duplicate_a_game(self):
        with patch.object(runner, "urlopen", side_effect=URLError("timeout")) as request:
            with self.assertRaises(RuntimeError):
                runner.run_game("https://example.test/")
        self.assertEqual(request.call_count, 1)


class DeploymentTests(unittest.TestCase):
    def test_agent_definitions_are_prompt_only(self):
        for name in ("player", "gameMaster"):
            definition = json.loads((ROOT / "agents" / f"{name}.json").read_text())
            self.assertEqual(definition["kind"], "prompt")
            self.assertNotIn("tools", definition)
            self.assertTrue(definition["text"]["format"]["strict"])

    def test_agent_calls_share_one_conversation_and_pin_versions(self):
        workflow = json.loads((ROOT / "automation" / "workflow.json").read_text())
        self.assertEqual(workflow["kind"], "Stateful")
        actions = workflow["definition"]["actions"]
        loop = actions["Play_up_to_ten_turns"]
        self.assertEqual(loop["limit"], {"count": 10, "timeout": "PT5M"})
        for action, name in (("Player", "player"), ("Game_master", "gameMaster")):
            card = loop["actions"][action]
            self.assertNotIn("runtimeConfiguration", card)
            self.assertEqual(card["inputs"]["body"]["conversation"], "@body('Create_conversation')['id']")
            self.assertEqual(card["inputs"]["body"]["agent_reference"]["name"], name)
            self.assertIn("parameters(", card["inputs"]["body"]["agent_reference"]["version"])
            self.assertEqual(card["inputs"]["authentication"]["type"], "ManagedServiceIdentity")
            self.assertEqual(card["inputs"]["retryPolicy"]["type"], "none")
        self.assertEqual(actions["Conversation"]["inputs"], "@variables('conversation')")
        self.assertEqual(
            actions["Respond_with_result"]["inputs"]["body"]["conversation"],
            "@outputs('Conversation')",
        )
        self.assertNotIn("gameTrackerApiUrl", workflow["definition"]["parameters"])

    def test_tokens_are_subscription_scoped_and_cached(self):
        api = deploy.AzureApi("subscription-test")
        token = json.dumps({"accessToken": "test-token", "expires_on": 9999999999})
        with patch.object(deploy, "cli", return_value=token) as cli:
            with patch.object(deploy, "request_json", return_value=({}, {})):
                api.request("https://management.azure.com/test")
                api.request("https://management.azure.com/test")
        self.assertEqual(cli.call_count, 1)
        self.assertIn("subscription-test", cli.call_args.args)

    def test_agent_deployment_has_no_unrelated_agents(self):
        api = unittest.mock.Mock()
        api.request.side_effect = [
            ({"version": "2"}, {}), ({"version": "3"}, {})
        ]
        with patch.dict(deploy.os.environ, {
            "PROJECT_ENDPOINT": "https://example.test/api/projects/game/",
            "MODEL_DEPLOYMENT": "gpt-4.1",
        }):
            with patch.object(deploy, "save_outputs"):
                versions = deploy.deploy_agents(api)
        self.assertEqual(versions, {"player": "2", "gameMaster": "3"})
        self.assertEqual(api.request.call_count, 2)
        self.assertIn("/agents/player/versions?api-version=v1", api.request.call_args_list[0].args[0])

    def test_existing_automation_project_and_application_are_not_recreated(self):
        api = unittest.mock.Mock()
        api.subscription = "subscription-test"
        app = {
            "properties": {"provisioningState": "Succeeded"},
            "identity": {"principalId": "application-identity"},
        }
        api.request.side_effect = [
            ({"properties": {"provisioningState": "Succeeded"}}, {}),
            (app, {}), (app, {}), ({}, {}),
        ]
        with patch.dict(deploy.os.environ, {
            "AUTOMATION_PROJECT_RESOURCE_ID": "/subscriptions/test/projects/automation",
            "AUTOMATION_PROJECT_NAME": "automation-test",
            "AUTOMATION_LOCATION": "northcentralus",
            "FOUNDRY_PROJECT_RESOURCE_ID": "/subscriptions/test/accounts/foundry/projects/game",
        }):
            app_url = deploy.ensure_automation(api)
        self.assertIn("auto.northcentralus.logic.azure.com/automationProjects/automation-test", app_url)
        api.wait.assert_not_called()
        mutations = [call for call in api.request.call_args_list if len(call.args) > 1]
        self.assertEqual(len(mutations), 1)
        self.assertIn("/roleAssignments/", mutations[0].args[0])
        self.assertEqual(mutations[0].args[2]["properties"]["principalId"], "application-identity")

    def test_workflow_publication_checks_health_and_saves_callback_privately(self):
        api = unittest.mock.Mock()
        workflow = json.loads((ROOT / "automation" / "workflow.json").read_text())
        api.request.side_effect = [
            ({}, {}), ({}, {}),
            ({"properties": {
                "health": {"state": "Healthy"}, "files": {"workflow.json": workflow},
            }}, {}),
            ({"value": "https://example.test/workflow?sig=test-secret"}, {}),
        ]
        with patch.dict(deploy.os.environ, {"PROJECT_ENDPOINT": "https://example.test/project/"}):
            with patch.object(deploy, "save_outputs") as save:
                deploy.publish_workflow(api, "https://example.test/app", {"player": "2", "gameMaster": "3"})
        save.assert_called_once_with({
            "LOGIC_APPS_WORKFLOW_URL": "https://example.test/workflow?sig=test-secret",
        })
        publication = api.request.call_args_list[1].args[2]["files"]
        self.assertEqual(publication["parameters.json"]["playerAgentVersion"]["value"], "2")
        self.assertEqual(publication["parameters.json"]["gameMasterAgentVersion"]["value"], "3")


if __name__ == "__main__":
    unittest.main()

"""Run the published Automation workflow and check its complete game transcript."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def validate_game(game):
    turns = game["conversation"]
    number = game["number_picked"]
    if type(number) is not int or not 1 <= number <= 100:
        raise ValueError("The workflow did not reveal a valid secret number.")
    if not game.get("conversation_id") or not game.get("game_run_id"):
        raise ValueError("The workflow did not return its conversation and run IDs.")
    if not 1 <= len(turns) <= 10 or game["turns"] != len(turns):
        raise ValueError("The workflow returned an invalid turn count.")
    low, high = 1, 100
    for index, turn in enumerate(turns, 1):
        guess = turn["player"]["guess"]
        reply = turn["gameMaster"]
        if (turn["turn"] != index or type(guess) is not int
                or not low <= guess <= high or reply["guess"] != guess):
            raise ValueError(f"Turn {index} contains an invalid or excluded guess.")
        expected_hint = "correct" if guess == number else "higher" if guess < number else "lower"
        expected_status = "won" if guess == number else "lost" if index == 10 else "playing"
        if reply["hint"] != expected_hint or reply["status"] != expected_status:
            raise ValueError(f"Turn {index} contradicts the revealed secret or game rules.")
        if reply["number_picked"] != (None if expected_status == "playing" else number):
            raise ValueError(f"Turn {index} reveals an inconsistent or premature secret.")
        if index < len(turns) and expected_status != "playing":
            raise ValueError("The workflow continued after the game ended.")
        if expected_hint == "higher":
            low = guess + 1
        elif expected_hint == "lower":
            high = guess - 1
    if game["status"] not in {"won", "lost"} or game["status"] != turns[-1]["gameMaster"]["status"]:
        raise ValueError("The workflow did not return a completed game.")
    return game


def run_game(url):
    if urlsplit(url).scheme != "https":
        raise ValueError("The workflow callback must use HTTPS.")
    request = Request(
        url, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            with urlopen(request, timeout=360) as response:
                if response.status != 200:
                    raise ValueError(f"Unexpected workflow HTTP status: {response.status}")
                return validate_game(json.load(response))
        except HTTPError as error:
            # A new trigger can return 404 before routing is ready; no run was accepted.
            if error.code == 404 and time.monotonic() < deadline:
                print("Waiting for the published trigger route...", flush=True)
                time.sleep(5)
                continue
            # Do not include the signed callback URL in exception output.
            raise RuntimeError(
                f"Workflow returned HTTP {error.code}. Inspect Monitoring at auto.azure.com."
            ) from None
        except (URLError, TimeoutError):
            raise RuntimeError(
                "Workflow connection failed or timed out. Inspect Monitoring before retrying; "
                "the original game may still be running."
            ) from None


def main():
    url = os.environ.get("LOGIC_APPS_WORKFLOW_URL")
    if not url:
        executable = shutil.which("azd")
        if not executable:
            raise RuntimeError("Install azd or set LOGIC_APPS_WORKFLOW_URL.")
        result = subprocess.run(
            [executable, "env", "get-value", "LOGIC_APPS_WORKFLOW_URL"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, check=True,
        )
        url = result.stdout.strip()
    game = run_game(url)
    for turn in game["conversation"]:
        print(f"Turn {turn['turn']}: player guessed {turn['player']['guess']}")
        print(f"  Game master: {turn['gameMaster']['message']}")
    print(f"\n{game['status'].upper()}: number {game['number_picked']}, {game['turns']} turns")
    print(f"Conversation: {game['conversation_id']}")
    print(f"Automation run: {game['game_run_id']}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Game failed: {error}", file=sys.stderr)
        sys.exit(1)

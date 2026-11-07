
import os
import requests
import json
from pathlib import Path
from dotenv import load_dotenv

# Load API key
project_folder = Path(__file__).resolve().parent.parent
load_dotenv(project_folder / ".env", override=True)

API_KEY = os.getenv("SWARMS_API_KEY")
BASE_URL = "https://api.swarms.world"

def analyze_weather(alert):

    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "agent_config": {
            "agent_name": "StormGuard Weather Agent",
            "description": "Analyzes emergency weather alerts.",
            "system_prompt": """
            You are StormGuard's Weather Analysis Agent.

            Analyze the supplied weather alert.

            Extract:
            1. Weather event
            2. Affected location
            3. Severity
            4. Expected conditions
            5. Recommended action explicitly stated in the alert

            Rules:
            - Never invent missing information.
            - Never pretend a simulated alert is real.
            - Do not create forecasts.
            - Use simple, accessible language.
            - Return your answer as valid JSON with these keys:
              weather_type, location, severity, conditions,
              recommended_action, source.
            """,
            "model_name": "gpt-4o-mini",
            "max_loops": 1,
            "max_tokens": 1000,
            "temperature": 0.2
        },
        "task": f"Analyze this weather alert:\n\n{alert}"
    }

    response = requests.post(
        f"{BASE_URL}/v1/agent/completions",
        headers=headers,
        json=payload,
        timeout=120
    )

    if response.status_code != 200:
        print("API Error:", response.status_code)
        print(response.text)
        return None

    result = response.json()

    print("\n--- WEATHER AGENT RESULT ---")
    print(json.dumps(result["outputs"], indent=2))
    print("\n--- API USAGE ---")
    print(result.get("usage"))

    return result


if __name__ == "__main__":

    sample_alert = """
    SIMULATED FLASH FLOOD WARNING

    Location: Miami-Dade County, Florida.

    Heavy rainfall may cause dangerous flooding
    in low-lying areas.

    Residents should avoid flooded roads
    and seek higher ground.
    """

    analyze_weather(sample_alert)

import os
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load the .env file from the project folder
project_folder = Path(__file__).resolve().parent
load_dotenv(project_folder / ".env", override=True)

# Get Swarms API key
api_key = os.getenv("SWARMS_API_KEY")

if not api_key:
    print("ERROR: SWARMS_API_KEY was not found.")
    exit()

# Test connection
url = "https://api.swarms.world/health"

headers = {
    "x-api-key": api_key,
    "Content-Type": "application/json"
}

try:
    response = requests.get(url, headers=headers, timeout=15)

    print("Status code:", response.status_code)
    print("Response:", response.text[:1000])

    if response.status_code == 200:
        print("SUCCESS: Connected to Swarms API!")

except requests.exceptions.RequestException as error:
    print("Connection error:", error)
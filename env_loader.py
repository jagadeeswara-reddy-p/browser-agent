"""Loads TYPESAFE_API_KEY / GATEWAY_API_KEY / GATEWAY_BASE_URL from the parent
project's .env (and this dir's own .env, if any) without needing python-dotenv
or a manual `source` step before running."""
import os


def load_dotenv(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def load_all():
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(here, "..", ".env"))
    load_dotenv(os.path.join(here, ".env"))

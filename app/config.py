import os

from dotenv import load_dotenv

load_dotenv()

DB = {
    'host': os.environ.get('DB_HOST', ''),
    'port': int(os.environ.get('DB_PORT') or 3306),
    'user': os.environ.get('DB_USER', ''),
    'password': os.environ.get('DB_PASSWORD', ''),
}
OLLAMA_URL = os.environ.get('OLLAMA_URL') or 'http://localhost:11434'
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', '')
MAX_APPS = int(os.environ.get('MAX_APPS') or 500)

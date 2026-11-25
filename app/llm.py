"""Turns free text into structured search fields with a local model served by Ollama.
The model only interprets the text - every value is checked against VCdb / PCdb afterwards (resolve.py)."""
import json

import requests

from app.config import OLLAMA_MODEL, OLLAMA_URL

SCHEMA = {
    'type': 'object',
    'properties': {
        'year': {'type': 'integer'},
        'make': {'type': 'string'},
        'model': {'type': 'string'},
        'engine': {'type': 'string'},
        'part_type': {'type': 'string'},
        'position': {'type': 'string'},
        'brand': {'type': 'string'},
        'qualifiers': {'type': 'string'},
        'attributes': {'type': 'string'},
    },
    'required': ['year', 'make', 'model', 'engine', 'part_type', 'position', 'brand', 'qualifiers', 'attributes'],
}

SYSTEM_PROMPT = '''You turn an auto parts search, typed by a parts counter employee, into structured fields.
- Fix obvious typos (altenator -> Alternator, silverdo -> Silverado).
- year: 4-digit model year ("21" or "'21" -> 2021). Use 0 if no year is given.
- make and model: official Auto Care VCdb names. Chevy -> Chevrolet, VW -> Volkswagen, F150 -> F-150.
  From 2011 on, Ram trucks are make "Ram" (a 2015 "Dodge Ram 1500" is make Ram, model 1500).
  Use an empty string for anything not given. Do not invent a model the user did not type.
- engine: displacement in liters as typed, e.g. "5.7" for "5.7L" or "5.7 hemi". Empty if not given.
  Never change the engine to one you think fits the vehicle better - it is validated separately.
- part_type: the part in standard catalog terms, singular, e.g. "Alternator", "Disc Brake Pad Set", "Oil Filter",
  "Water Pump", "Spark Plug". Empty if not given.
- position: Front, Rear, Left, Right, Upper, Lower or a combination such as "Front Left". Empty if not given.
- brand: the PART brand if one is typed, e.g. Bosch, Denso, ACDelco, Motorcraft, Mopar, Moog. Empty if none.
  Don't confuse it with the vehicle make: "ford alternator" has make Ford and no brand; "motorcraft alternator" has brand Motorcraft.
- qualifiers: other VEHICLE details typed, comma-separated: trim / submodel (Laramie, LT, Limited, Sport),
  drive (4WD, AWD, FWD, RWD - write 4x4 as 4WD), fuel (Gas, Diesel), body, cab or bed. Empty if none.
  The engine goes in engine, not here.
- attributes: PART features or specs typed, comma-separated, e.g. "160 amp", "ceramic", "with sensor". Empty if none.'''


def parse_query(text: str) -> dict:
    if not OLLAMA_MODEL:
        raise RuntimeError('OLLAMA_MODEL is not set - put the exact name from `ollama list` in .env')
    response = requests.post(f'{OLLAMA_URL}/api/chat', timeout=180, json={
        'model': OLLAMA_MODEL,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': text}],
        'format': SCHEMA,
        'stream': False,
        'keep_alive': '30m',
        'options': {'temperature': 0},
    })
    response.raise_for_status()
    parsed = json.loads(response.json()['message']['content'])
    return {key: parsed.get(key) or (0 if key == 'year' else '') for key in SCHEMA['properties']}

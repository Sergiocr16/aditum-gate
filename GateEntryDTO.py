import json
import requests

def send_to_node_api(url, gate_entry_dto):
    headers = {
        'Content-type': 'application/json',
    }
    response = requests.post(url, headers=headers, data=json.dumps(gate_entry_dto))
    return response.json()

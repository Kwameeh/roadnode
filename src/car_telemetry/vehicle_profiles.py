from __future__ import annotations
import json, re
from pathlib import Path

def safe_id(value): return re.sub(r'[^A-Za-z0-9_.-]+','_',value or 'unknown')[:80]

class ProfileStore:
    def __init__(self, root): self.root=Path(root).expanduser(); self.root.mkdir(parents=True,exist_ok=True)
    def path(self, vehicle_key): return self.root/f'{safe_id(vehicle_key)}.json'
    def load(self, vehicle_key):
        p=self.path(vehicle_key)
        if not p.exists(): return {"vehicleKey":vehicle_key,"selectedSignals":[]}
        try: return json.loads(p.read_text(encoding='utf-8'))
        except Exception: return {"vehicleKey":vehicle_key,"selectedSignals":[]}
    def save(self, vehicle_key, profile):
        p=self.path(vehicle_key); p.write_text(json.dumps(profile,indent=2),encoding='utf-8')

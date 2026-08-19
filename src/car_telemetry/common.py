from __future__ import annotations
import json, subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence

@dataclass
class Check:
    name: str
    state: str
    detail: str
    @property
    def failed(self) -> bool:
        return self.state == "FAIL"

def run(command: Sequence[str], timeout: float = 10) -> tuple[int,str,str]:
    try:
        c=subprocess.run(list(command), capture_output=True, text=True, timeout=timeout, check=False)
        return c.returncode,c.stdout.strip(),c.stderr.strip()
    except FileNotFoundError:
        return 127,"",f"{command[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124,"","command timed out"

def write_json_atomic(path: str, data: dict) -> None:
    p=Path(path).expanduser(); p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix('.tmp'); t.write_text(json.dumps(data,indent=2,default=str),encoding='utf-8'); t.replace(p)

def read_json(path: str):
    p=Path(path).expanduser()
    if not p.exists(): return None
    try: return json.loads(p.read_text(encoding='utf-8'))
    except Exception: return None

def print_check(c: Check):
    prefix={"OK":"[ OK ]","WARN":"[WARN]","FAIL":"[FAIL]","SKIP":"[SKIP]"}.get(c.state,"[????]")
    print(f"{prefix} {c.name}: {c.detail}")

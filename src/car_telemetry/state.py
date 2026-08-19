from __future__ import annotations
import copy, threading
from datetime import datetime, timezone

def now(): return datetime.now(timezone.utc).isoformat()

class DeviceState:
    def __init__(self, device_id, vehicle_id, stage):
        self.lock=threading.RLock()
        self.data={"agent":"starting","deviceId":device_id,"vehicleId":vehicle_id,"prototypeStage":stage,"startedAt":now(),
                   "gps":{},"imu":{},"obd":{"signals":{},"supportedSignals":[],"selectedSignals":[]},"mqtt":{},"events":{},"system":{}}
    def merge(self, section, values):
        with self.lock:
            current=self.data.setdefault(section,{})
            if isinstance(current,dict): current.update(values)
            else: self.data[section]=dict(values)
            self.data['updatedAt']=now()
    def merge_nested(self, section, key, values):
        with self.lock:
            target=self.data.setdefault(section,{}).setdefault(key,{})
            target.update(values); self.data['updatedAt']=now()
    def set(self,key,value):
        with self.lock: self.data[key]=value; self.data['updatedAt']=now()
    def snapshot(self):
        with self.lock: return copy.deepcopy(self.data)

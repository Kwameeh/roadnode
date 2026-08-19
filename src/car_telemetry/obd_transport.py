from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import glob
from .config import Settings

@dataclass(frozen=True)
class Transport:
    kind:str
    port:str

def usb_candidates():
    result=[]
    for p in sorted(glob.glob('/dev/serial/by-id/*'))+sorted(glob.glob('/dev/ttyUSB*'))+sorted(glob.glob('/dev/ttyACM*')):
        if p not in result: result.append(p)
    return result

def resolve(s:Settings, override_kind=None):
    mode=(override_kind or s.obd_transport).lower()
    if mode not in {'auto','usb','bluetooth'}: raise ValueError('OBD_TRANSPORT must be auto, usb, or bluetooth')
    usb=[]
    if s.obd_usb_port!='auto':
        if Path(s.obd_usb_port).exists(): usb=[s.obd_usb_port]
    else: usb=usb_candidates()
    bt=Path(s.obd_bluetooth_port).exists()
    if mode in {'auto','usb'} and usb: return Transport('usb',usb[0])
    if mode in {'auto','bluetooth'} and bt: return Transport('bluetooth',s.obd_bluetooth_port)
    if mode=='usb': raise FileNotFoundError('No USB ELM327 serial port found')
    if mode=='bluetooth': raise FileNotFoundError(f'{s.obd_bluetooth_port} not found')
    raise FileNotFoundError('No USB or Bluetooth ELM327 transport found')

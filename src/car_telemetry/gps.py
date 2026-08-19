from __future__ import annotations
import threading,time,serial,pynmea2
from .config import Settings
from .state import DeviceState

def parse(line):
    try: m=pynmea2.parse(line)
    except Exception: return {}
    out={}
    if isinstance(m,pynmea2.types.talker.RMC):
        valid=getattr(m,'status','')=='A'; out['validFix']=valid
        if valid:
            out['latitude']=float(m.latitude); out['longitude']=float(m.longitude)
            if getattr(m,'spd_over_grnd',None) not in (None,''): out['speedKph']=round(float(m.spd_over_grnd)*1.852,2)
            if getattr(m,'true_course',None) not in (None,''): out['headingDegrees']=round(float(m.true_course),2)
    elif isinstance(m,pynmea2.types.talker.GGA):
        q=int(getattr(m,'gps_qual',0) or 0); out['validFix']=q>0
        if q>0: out['latitude']=float(m.latitude); out['longitude']=float(m.longitude)
        if getattr(m,'num_sats',None) not in (None,''): out['satellites']=int(m.num_sats)
        if getattr(m,'altitude',None) not in (None,''): out['altitudeMeters']=round(float(m.altitude),2)
        if getattr(m,'horizontal_dil',None) not in (None,''): out['hdop']=round(float(m.horizontal_dil),2)
    return out

def worker(s:Settings,state:DeviceState,stop:threading.Event):
    state.merge('gps',{'enabled':s.gps_enabled,'port':s.gps_port,'baud':s.gps_baud})
    if not s.gps_enabled:return
    while not stop.is_set():
        try:
            with serial.Serial(s.gps_port,s.gps_baud,timeout=1) as port:
                state.merge('gps',{'serialOpen':True,'error':None})
                while not stop.is_set():
                    raw=port.readline()
                    if not raw:continue
                    line=raw.decode('ascii',errors='ignore').strip()
                    if not line.startswith('$'):continue
                    state.merge('gps',{'received':True,'lastDataUnix':time.time(),**parse(line)})
        except Exception as e:
            state.merge('gps',{'serialOpen':False,'error':str(e)}); stop.wait(3)

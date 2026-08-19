from __future__ import annotations
import threading,time
from PIL import Image,ImageDraw,ImageFont
from .config import Settings
from .state import DeviceState

class OLED:
    def __init__(self,s): self.s=s; self.display=None; self.lock=threading.RLock(); self.font=ImageFont.load_default()
    def open(self):
        if self.display:return self.display
        import board,adafruit_ssd1306
        self.display=adafruit_ssd1306.SSD1306_I2C(self.s.oled_width,self.s.oled_height,board.I2C(),addr=self.s.oled_address);return self.display
    def show(self,*lines):
        if not self.s.oled_enabled:return
        with self.lock:
            d=self.open(); img=Image.new('1',(self.s.oled_width,self.s.oled_height)); draw=ImageDraw.Draw(img); y=0
            for line in lines[:6]: draw.text((0,y),str(line)[:21],font=self.font,fill=255);y+=10
            d.image(img);d.show()

def v(signal):
    x=signal.get('value') if isinstance(signal,dict) else signal
    if isinstance(x,dict): return x.get('value','--')
    return '--' if x is None else x

def worker(s:Settings,state:DeviceState,stop:threading.Event):
    if not s.oled_enabled:return
    oled=OLED(s)
    while not stop.is_set():
        try:
            snap=state.snapshot();o=snap.get('obd',{});g=snap.get('gps',{});sig=o.get('signals',{})
            oled.show('CAR TELEMETRY',f"SPD:{v(sig.get('SPEED',{}))}",f"RPM:{v(sig.get('RPM',{}))}",
                      f"GPS:{'FIX' if g.get('validFix') else 'DATA' if g.get('received') else 'WAIT'}",f"OBD:{'OK' if o.get('connected') else 'WAIT'} {str(o.get('transport','')).upper()}")
        except Exception: pass
        stop.wait(1)

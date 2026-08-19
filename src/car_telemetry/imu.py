from __future__ import annotations
import math,threading,time
from .config import Settings
from .state import DeviceState

def open_sensor(s):
    import board,adafruit_mpu6050
    return adafruit_mpu6050.MPU6050(board.I2C(),address=s.imu_address)

def worker(s:Settings,state:DeviceState,stop:threading.Event):
    state.merge('imu',{'enabled':s.imu_enabled,'address':f'0x{s.imu_address:02X}'})
    if not s.imu_enabled:return
    try:
        sensor=open_sensor(s); sums=[0.0]*6
        state.merge('imu',{'calibrating':True,'calibrationPercent':0})
        for i in range(s.imu_calibration_samples):
            a=sensor.acceleration; g=sensor.gyro; vals=(*a,*g)
            sums=[x+y for x,y in zip(sums,vals)]
            if i%10==0 or i==s.imu_calibration_samples-1: state.merge('imu',{'calibrationPercent':int((i+1)*100/s.imu_calibration_samples)})
            time.sleep(.02)
        off=[x/s.imu_calibration_samples for x in sums]
        state.merge('imu',{'calibrating':False,'calibrated':True,'error':None})
    except Exception as e:
        state.merge('imu',{'calibrating':False,'calibrated':False,'error':str(e)}); return
    interval=1/max(s.imu_rate_hz,1)
    while not stop.is_set():
        try:
            ax,ay,az=sensor.acceleration; gx,gy,gz=sensor.gyro
            lx,ly,lz=ax-off[0],ay-off[1],az-off[2]; rg=math.sqrt(ax*ax+ay*ay+az*az)/9.80665
            state.merge('imu',{'linearAccelerationMps2':{'x':round(lx,3),'y':round(ly,3),'z':round(lz,3)},
              'gyroRadPerSec':{'x':round(gx-off[3],3),'y':round(gy-off[4],3),'z':round(gz-off[5],3)},'resultantG':round(rg,3),'temperatureC':round(float(sensor.temperature),2),'error':None})
            state.merge('events',{'harshAcceleration':lx>=s.harsh_accel_mps2,'harshBraking':lx<=s.harsh_brake_mps2,
              'harshCornering':abs(ly)>=s.harsh_corner_mps2,'possibleImpact':rg>=s.impact_g})
        except Exception as e: state.merge('imu',{'error':str(e)})
        stop.wait(interval)

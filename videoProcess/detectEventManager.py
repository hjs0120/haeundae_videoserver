
from threading import Thread
from av.video.frame import VideoFrame
from av import open
from cv2 import imwrite
from fractions import Fraction
from collections import deque
import datetime
import json
import requests
import os
from store.cctvStore import DetectCCTV, PtzCCTV
from store.configStore import ServerConfig
from store.smsStore import SmsConfig
from module.broadcast import Broadcast
from videoProcess.sharedData import SharedDetectData
from module.mipoSMS import Sms
from store.configSettingStore import ConfigSetting

class DetectEventManager():
    def __init__(self, detectCCTV: DetectCCTV, config: ServerConfig, backendHost: str, broadcast: Broadcast, 
                 sharedDetectData: SharedDetectData, phoneList:list[str], smsConfig:SmsConfig | None, linkedPtzCCTV: PtzCCTV, 
                 configSetting: ConfigSetting):
        self.frameCounter = 0
        self.numberOfObject = 0
        
        self.detectCCTV = detectCCTV
        self.linkedPtzCCTV = linkedPtzCCTV
        self.continuousThreshold = configSetting.continuousThreshold
        self.config = config
        self.backendHost = backendHost
        self.phoneList = phoneList
        self.broadcast = broadcast
        self.ptzQueue = sharedDetectData.ptzCoordsQueue
        self.sharedDetectFlag = sharedDetectData.sharedDetectFlag
        self.savingEventLogFlag = False
        self.timestamp = None
        self.outputVideo = None
        self.outputImage  = None        
        self.sharedDetectFlag.value = False
        
        if smsConfig is not None:
            self.sms = Sms(smsConfig, detectCCTV)
          
    def getEvent(self, coords):
        if len(coords) == 0 : # 감지된 객체가 없다면
            self.frameCounter = 0
            self.numberOfObject = 0
            self.sharedDetectFlag.value = False
            return
        
        self.frameCounter += 1
        
        if self.frameCounter < self.continuousThreshold: # 3회이상 연속으로 하지 못했다면.
            self.numberOfObject = 0 
            return
        
        ### alarm and ptz ###
        print(f"{self.detectCCTV.index}번 지능형 CCTV 알람 및 PTZ제어 플레그 On, {len(coords)}개 이벤트", flush=True)
        self.sharedDetectFlag.value = True
        self.putPtzCoord(coords)
        
        if self.numberOfObject >= len(coords): # 이전 프레임의 객체보다 감지 수가 작거나 같다면
            self.numberOfObject = len(coords)
            return
        
        ### SetLog, saveFile and broadcast ###
        print(f"{self.detectCCTV.index}번 지능형 CCTV 로그, 파일 저장, 방송 제어, {len(coords)}개 이벤트", flush=True)
        self.numberOfObject = len(coords)
        self.savingEventLogFlag = True
        
    def setEventLog(self, coords, frame):
        self.timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        cctvName = self.detectCCTV.cctvName.replace(' ', '_').replace('#', '')
        ptzCctvName = self.linkedPtzCCTV.cctvName.replace(' ', '_').replace('#', '')
        fileName = f"{cctvName}_{self.timestamp}"
        ptzFileName = f"{ptzCctvName}_{self.timestamp}"
        for coord in coords:
            fileName += f'_{int(coord[0])}_{int(coord[1])}'
        self.outputVideo = f'public/eventVideo/{self.detectCCTV.index}/{fileName}.mp4'
        self.ptzOutputVideo = f'public/eventPtzVideo/{self.linkedPtzCCTV.index}/{ptzFileName}.mp4'
        self.outputImage = f'public/eventImage/{self.detectCCTV.index}/{fileName}.jpg'
        
        if self.phoneList and self.sms:
            print("sendSmsThread run", flush=True)
            sendSmsThread = Thread(target=self.sms.sendSms, args=[self.phoneList, len(coords)], daemon=True)
            sendSmsThread.start()
        
        print("setEventLogThread run", flush=True)
        setEventLogThread = Thread(target=self.setEventLogThread, args=(coords, frame), daemon=True)
        setEventLogThread.start()
        
        return self.outputVideo, self.ptzOutputVideo
            
    def setEventLogThread(self, coords, frame):
        try:
            os.makedirs(os.path.dirname(self.outputImage), exist_ok=True)
            os.makedirs(os.path.dirname(self.outputVideo), exist_ok=True)
            os.makedirs(os.path.dirname(self.ptzOutputVideo), exist_ok=True)
            success = imwrite(self.outputImage, frame)
        except :
            success = False
            
        url = f'http://{self.backendHost}/forVideoServer/setEventLog'
        params = {
            'cctvIndex': self.detectCCTV.index,
            'objectCoord': json.dumps([[int(coord[0]), int(coord[1])] for coord in coords]), 
            'savedImageDir': f'http://{self.detectCCTV.wsUrl["ip"]}:{self.detectCCTV.wsUrl["port"]}/{self.outputImage}' if success else None,
            'savedVideoDir': f'http://{self.detectCCTV.wsUrl["ip"]}:{self.detectCCTV.wsUrl["port"]}/{self.outputVideo}',
            'savedPtzVideoDir': f'http://{self.detectCCTV.wsUrl["ip"]}:{self.detectCCTV.wsUrl["port"]}/{self.ptzOutputVideo}'
        }
        if self.broadcast is not None:
            self.broadcast.startBroadCast()
        try:
            print(f'이벤트 로그 저장 시도', flush=True)
            response = requests.get(url, params=params)
            if response.status_code == 200 : 
                print(f'이벤트 로그 저장 성공 : {response.text}', flush=True)
            else : 
                print(f'이벤트 로그 저장 실패 : {response.text}', flush=True)
        except:
            print('backend 연결 실패', flush=True)            
        
    def putPtzCoord(self, coords):
        putPtzCoordTread = Thread(target=self.putPtzCoordTread, args=[coords], daemon=True)
        putPtzCoordTread.start()
    
    def putPtzCoordTread(self, coords):
        for coord in coords:
            self.ptzQueue.put(coord)
        self.ptzQueue.put(None)
    
    def updateSmsPhoneList(self, phoneList:list[str]): # 추가
        print(self.detectCCTV.index, phoneList)
        self.phoneList = phoneList
        
    def updateContinuousThreshold(self, continuousThreshold):
        self.continuousThreshold = continuousThreshold


    
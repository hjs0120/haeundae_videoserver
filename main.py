from torch.multiprocessing import Process, Queue
import os 
from store.broadcastStore import BroadcastStore
from store.cctvStore import DetectCCTVStore, PtzCCTVStore, DetectCCTV, PtzCCTV
from store.configStore import ConfigStore, ServerConfig
from store.groupConfigStore import GroupStore
from store.configSettingStore import ConfigSettingStore
from store.smsStore import SmsDestinationStore, SmsConfigStore

from mqtt_client import publisher_loop

#from videoProcess.sharedData import SharedDetectData, SharedPtzData
from videoProcess.sharedData import SharedDetectData
from videoProcess.detectVideoProcess import detectedVideo
#from videoProcess.videoProcess import video
from videoProcess.saveVideo import SaveVideo

from module.broadcast import Broadcast
from api.detectServer import DetectVideoServer
#from api.ptzServer import PtzVideoServer
from module.ptz import Ptz
import requests
import websockets
import asyncio
import gc
import time

import logging
import logging.config
import json

from config import CONFIG

# from config import BACKEND_HOST

#from dotenv import load_dotenv
#load_dotenv()

def setup_logging(
    default_path="logger.json",
    default_level=logging.INFO,
    env_key="LOG_CFG"
):
    """logger.json 설정을 불러와 logging 초기화"""
    path = default_path
    value = os.getenv(env_key, None)
    if value:
        path = value

    if os.path.exists(path):
        with open(path, "rt", encoding="utf-8") as f:
            config = json.load(f)
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=default_level)


class VideoServer():
    def __init__(self, BACKEND_HOST = "192.168.0.31:7000"):
        self.BACKEND_HOST = BACKEND_HOST
        self.ONVIF_PORT = 80
        self.broadcastStore = BroadcastStore(self.BACKEND_HOST)
        self.detectCCTVStore = DetectCCTVStore(self.BACKEND_HOST)
        #self.ptzCCTVStore = PtzCCTVStore(self.BACKEND_HOST)
        self.configStore = ConfigStore(self.BACKEND_HOST)
        self.groupStore = GroupStore(self.BACKEND_HOST)
        self.smsDestinationStore = SmsDestinationStore(self.BACKEND_HOST)
        self.smsConfigStore = SmsConfigStore(self.BACKEND_HOST)
        self.configSettingStore = ConfigSettingStore(self.BACKEND_HOST)
        
        self.getDataLoad()

        self.mqtt_queue: Queue | None = None
        self.mqtt_proc: Process | None = None
        
    def getDataLoad(self):
        self.broadcastStore.getData()
        self.detectCCTVStore.getData()
       # self.ptzCCTVStore.getData()
        self.configStore.getData()
        self.groupStore.getData()
        self.smsDestinationStore.getData()
        self.smsConfigStore.getData()
        self.configSettingStore.getData()
        
        self.broadcastConfig = self.broadcastStore.broadcastConfig
        self.detectCCTV = self.detectCCTVStore.detectCCTV
        #self.ptzCCTV = self.ptzCCTVStore.ptzCCTV
        self.config = self.configStore.config
        self.group = self.groupStore.group
        self.smsDestination = self.smsDestinationStore.smsDestination
        self.smsConfig = self.smsConfigStore.smsConfig
        self.configSetting = self.configSettingStore.configSettings
        
    def selectServerConfig(self) -> ServerConfig:
        #print("서버 설정을 선택해 주세요")
        logger.info("서버 설정을 선택해 주세요")
        for serverConfig in self.config:
            index = serverConfig.index
            detectPortList = serverConfig.detectPortList
            #ptzPortList = serverConfig.ptzPortList
            wsIndex = serverConfig.wsIndex
            #print(f"{index}번 서버 : \n - 지능형 영상 포트 : {detectPortList} \n - PTZ 영상 포트 : {ptzPortList} \n - 포트별 영상 갯수 : {wsIndex}")
            #print(f"{index}번 서버 : \n - 지능형 영상 포트 : {detectPortList} \n - 포트별 영상 갯수 : {wsIndex}")
            logger.info(f"{index}번 서버 : \n - 지능형 영상 포트 : {detectPortList} \n - 포트별 영상 갯수 : {wsIndex}")
            

            userInput = CONFIG["SERVER_INDEX"]
            try:
                inputServerIndex = int(userInput) - 1
            except :
                #print("잘못된 입력 입니다, 다시입력해 주세요")
                logger.error("잘못된 입력 입니다, 다시입력해 주세요")
                continue
            if inputServerIndex in range(len(self.config)):
                return self.config[inputServerIndex]
            else: 
                #print("존재하지 않는 서버 인덱스 입니다, 다시입력해 주세요")
                logger.error("존재하지 않는 서버 인덱스 입니다, 다시입력해 주세요")
                
    #def matchingApiAndProcess(self) -> dict[ServerConfig, dict[str, dict[int, list[DetectCCTV | PtzCCTV]]]]:
    def matchingApiAndProcess(self) -> dict[ServerConfig, dict[str, dict[int, list[DetectCCTV]]]]:
            detectCnt = 0
            #ptzCnt = 0 
            #matchedServer:dict[ServerConfig, dict[str, dict[int, list[DetectCCTV | PtzCCTV]]]] = {}
            matchedServer:dict[ServerConfig, dict[str, dict[int, list[DetectCCTV ]]]] = {}
            
            for serverConfig in self.config:
                matchedDetectPort:dict[int, list[DetectCCTV]] = {}
                #matchedPtzPort:dict[int, list[PtzCCTV]] = {}
                matchedServer[serverConfig] = {}
                
                for detectPort in serverConfig.detectPortList:
                    detectCCTVList: list[DetectCCTV] = []
                    for _ in range(serverConfig.wsIndex):
                        detectCCTVList.append(self.detectCCTV[detectCnt] if len(self.detectCCTV) > detectCnt else DetectCCTV())
                        detectCnt += 1
                    matchedDetectPort[detectPort] = detectCCTVList
                    
                #for ptzPort in serverConfig.ptzPortList:
                #    ptzCCTVList: list[PtzCCTV] = []
                #    for _ in range(serverConfig.wsIndex):
                #        ptzCCTVList.append(self.ptzCCTV[ptzCnt] if len(self.ptzCCTV) > ptzCnt else PtzCCTV())
                #        ptzCnt += 1
                #    matchedPtzPort[ptzPort] = ptzCCTVList
                    
                matchedServer[serverConfig]["detect"] = matchedDetectPort
                #matchedServer[serverConfig]["ptz"] = matchedPtzPort
                
            return matchedServer
        
    def updateWsIndex(self):
        for cctvType, cctvData in self.compareWsIndex.items():
            if cctvType == "detect":
                for detectCCTV, wsUrl in cctvData.items():
                    if detectCCTV.wsUrl != wsUrl:
                        try:
                            response = requests.get(f"http://{self.BACKEND_HOST}/forVideoServer/setDetectWsIndex?cctvIndex={detectCCTV.index}&ip={wsUrl['ip']}&port={wsUrl['port']}&index={wsUrl['index']}")
                            if response.status_code == 200 :
                                #print(f"setDetectWsIndex Success, cctvIndex : {detectCCTV.index}", flush=True)
                                logger.info(f"setDetectWsIndex Success, cctvIndex : {detectCCTV.index}")
                            else :
                                #print("setDetectWsIndex Fail")
                                logger.error("setDetectWsIndex Fail")
                        except Exception as e :
                            #print("setDetectWsIndex Fail : ", e)
                            logger.error(f"setDetectWsIndex Fail : {e}")
                        
                    
            #elif cctvType == "ptz":
            #    for ptzCCTV, wsUrl in cctvData.items():
            #        if ptzCCTV.wsUrl != wsUrl:
            #            try:
            #                response = requests.get(f"http://{self.BACKEND_HOST}/forVideoServer/setPtzWsIndex?cctvIndex={ptzCCTV.index}&ip={wsUrl['ip']}&port={wsUrl['port']}&index={wsUrl['index']}")
            #                if response.status_code == 200 :
            #                    print("setPtzWsIndex Success")
            #                else :
            #                    print("setPtzWsIndex Fail")
            #            except Exception as e :
            #                print("setPtzWsIndex Fail : ", e)

            else:
                pass
              
    def main(self):
        matchedServer = self.matchingApiAndProcess()
        selectedConfig = self.selectServerConfig()
        self.setProcess(matchedServer, selectedConfig)
        self.updateWsIndex()
        self.runProcess()
        
        while True:
            # userinput = input()    
            # if userinput == "q":
            #     break
            time.sleep(1)
            
        # self.killProcess()
        
    def setProcess(self, matchedServer:dict[ServerConfig, dict[str, dict[int, list[DetectCCTV]]]], selectedConfig:ServerConfig):
        selectedMatchedServer = matchedServer[selectedConfig]
        selectedSetting = next((configSetting for configSetting in self.configSetting if configSetting.index == selectedConfig.index), None)
        broadcasts: dict[Broadcast, list[int]] = {}
        self.compareWsIndex:dict[str, dict[DetectCCTV, dict]] = {}
        
        for broadcastData in self.broadcastConfig:
            broadcasts[Broadcast(broadcastData, self.BACKEND_HOST, selectedConfig.index)] = broadcastData.targetDetectCCTV

        maxIndex = 0
        for typeFlag, MatchedServerData in selectedMatchedServer.items():
            if typeFlag == 'detect':
                for detectCCTVs in MatchedServerData.values():
                    for detecCCTV in detectCCTVs:
                        if maxIndex < detecCCTV.index:
                            maxIndex = detecCCTV.index
        maxIndex += 1    
        
        self.detectVideoServers: list[DetectVideoServer] = []
        self.detectVideoProcess: list[Process] = []
        self.matchedSharedData: dict[DetectCCTV, SharedDetectData] = {}
        self.saveVideoDict: dict[int, SaveVideo] = {}
        #self.ptzVideoServers:list[PtzVideoServer] = []
        #self.ptzVideoProcess:list[Process] = []
        #self.ptzAutoControlProcs: list[Process] = []

        if self.mqtt_queue is None:
            self.mqtt_queue = Queue(maxsize=1000)
        if self.mqtt_proc is None:
            self.mqtt_proc = Process(target=publisher_loop, args=(self.mqtt_queue,), daemon=True)
        
        for typeFlag, MatchedServerData in selectedMatchedServer.items():
            if typeFlag == 'detect':
                self.compareWsIndex["detect"] = {}
                
                for port, detectCCTVs in MatchedServerData.items():
                    sharedDetectDataList: list[SharedDetectData] = []
                    saveVideoList: list[SaveVideo] = []
                    
                    for i, detectCCTV in enumerate(detectCCTVs):
                        smsPhoneList:list[str] = []
                        
                        index = detectCCTV.index
                        sharedDetectData = SharedDetectData()
                        self.matchedSharedData[detectCCTV] = sharedDetectData
                        sharedDetectDataList.append(sharedDetectData)
                        targetBroadcast = None
                        for broadcast, targetDetectCCTV in broadcasts.items():
                            if index in targetDetectCCTV:
                                targetBroadcast = broadcast
                                
                        isRunDetectFlag = False    
                        for group in self.group:
                            if index in group.targetDetectCCTV:
                                isRunDetectFlag = True
                                isRunDetect = True if group.isRunDetect == 'Y' else False if group.isRunDetect == 'N' else None
                                for smsDestination in self.smsDestination:  
                                    if group.group in smsDestination.group:
                                        smsPhoneList.append(smsDestination.phone)
                        if not isRunDetectFlag:
                            isRunDetect = True
                        
                        wsUrl = {'ip': selectedConfig.host, 'port': port, 'index': i}
                        self.compareWsIndex["detect"][detectCCTV] = wsUrl
                        saveVideo = SaveVideo()
                        saveVideoList.append(saveVideo)
                        self.saveVideoDict[index] = saveVideo
                        
                        #for type, MatchedServerData in selectedMatchedServer.items():
                        #    if type == 'ptz':
                        #        for ptzCCTVs in MatchedServerData.values():
                        #            for ptzCCTV in ptzCCTVs: 
                        #                if index in ptzCCTV.linkedCCTV:
                        #                    linkedPtzCCTV = ptzCCTV
                        linkedPtzCCTV = None

                        self.detectVideoProcess.append(Process(target=detectedVideo, 
                                                        args=(detectCCTV, sharedDetectData, isRunDetect, targetBroadcast, selectedConfig, self.BACKEND_HOST, 
                                                              smsPhoneList, self.smsConfig[0] if len(self.smsConfig) > 0 else None, saveVideo, linkedPtzCCTV,
                                                              selectedSetting,self.mqtt_queue), 
                                                        daemon=True))
                        
                    self.detectVideoServers.append(DetectVideoServer(port, self.BACKEND_HOST, selectedConfig, sharedDetectDataList, saveVideoList))

            '''      
            if typeFlag == 'ptz':
                self.ptzs: dict[Ptz, bool] = {}
                self.compareWsIndex["ptz"] = {}
                
                for port, ptzCCTVs in MatchedServerData.items():
                    sharedPtzDataList: list[SharedPtzData] = []
                    saveVideoList: list[SaveVideo] = []
                    
                    for i, ptzCCTV in enumerate(ptzCCTVs):
                        sharedPtzData = SharedPtzData()
                        sharedPtzDataList.append(sharedPtzData)
                        sharedDetectDataListForPtz:list[SharedDetectData] = []
                        detectCCTVListForPtz:list[DetectCCTV] = []
                        
                        for detectCCTV, sharedDetectData in self.matchedSharedData.items():
                           if detectCCTV.index in ptzCCTV.linkedCCTV :
                               sharedDetectDataListForPtz.append(sharedDetectData)
                               detectCCTVListForPtz.append(detectCCTV)
                               
                        for index, saveVideo in self.saveVideoDict.items():
                            if index in ptzCCTV.linkedCCTV:
                                saveVideoList.append(saveVideo)
                                
                        ptz = Ptz(ptzCCTV, self.ONVIF_PORT, sharedDetectDataListForPtz, detectCCTVListForPtz)
                        ptzAvailable = ptz.connect()
                        self.ptzs[ptz] = ptzAvailable
                        self.ptzAutoControlProcs.append(Process(target=ptz.AutoControlProc, args=[ptzAvailable], daemon=True))
                        
                        wsUrl = {'ip': selectedConfig.host, 'port': port, 'index': i}
                        self.compareWsIndex["ptz"][ptzCCTV] = wsUrl
                        
                        self.ptzVideoProcess.append(Process(target=video, 
                                                            args=(ptzCCTV, sharedPtzData, self.BACKEND_HOST, selectedConfig, saveVideoList), 
                                                            daemon=True))
                    
                    self.ptzVideoServers.append(PtzVideoServer(port, sharedPtzDataList, selectedConfig, self.ptzs))
            

        self.serverProcs = [Process(target=videoServer.run, args=(), daemon=True) for videoServer in self.detectVideoServers + self.ptzVideoServers]
        '''
        self.serverProcs = [Process(target=videoServer.run, args=(), daemon=True) for videoServer in self.detectVideoServers]

        
    
    def runProcess(self):
        try:
            # MQTT 퍼블리셔 먼저 시작 (단 1개)
            if self.mqtt_proc is not None and not self.mqtt_proc.is_alive():
                self.mqtt_proc.start()
            #for proc in self.serverProcs + self.ptzVideoProcess + self.detectVideoProcess + self.ptzAutoControlProcs:
            for proc in self.serverProcs + self.detectVideoProcess :
                proc.start()
            asyncio.run(self.sendMessage(f"ws://{self.BACKEND_HOST}", 'reload'))
        except Exception as e:
            #print("runProcess error:", e)
            logger.error(f"runProcess error: {e}")

    def killProcess(self):
        try:
            #for proc in self.serverProcs + self.ptzVideoProcess + self.detectVideoProcess:
            for proc in self.serverProcs + self.detectVideoProcess:
                if proc.is_alive():
                    proc.kill()
                    proc.join()
        except:
            pass
        gc.collect()
        # === MQTT 퍼블리셔 종료 ===
        try:
            if self.mqtt_queue is not None:
                self.mqtt_queue.put_nowait(None)  # sentinel
        except Exception:
            pass
        try:
            if self.mqtt_proc is not None and self.mqtt_proc.is_alive():
                self.mqtt_proc.join(timeout=3)
        except Exception:
            pass


    async def sendMessage(self, uri, message):
        async with websockets.connect(uri) as websocket:
            await websocket.send(message)
            #print(f"Message sent: {message}")
            logger.info(f"Message sent: {message}")
        
if __name__ == "__main__":

    setup_logging()   # logger.json 적용
    logger = logging.getLogger(__name__)

    logger.info("서비스 시작")
    logger.debug("디버그 모드 활성화")
    logger.error("에러 발생 예시")

    videoserver = VideoServer(CONFIG["BACKEND_HOST"])
    videoserver.main()

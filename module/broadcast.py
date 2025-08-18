import requests
from urllib.parse import quote_plus
from store.broadcastStore import BroadcastConfig

class Broadcast():
    def __init__(self, broadcastConfig: BroadcastConfig, backendHost=None, selectedServeNumber=None):
        self.poleNumber = broadcastConfig.poleNumber
        self.turnOnUrl = f"{broadcastConfig.broadcastUrl}?DOA{broadcastConfig.poleNumber}={int(broadcastConfig.playTime) * 100}"
        self.getStatusUrl = f"{broadcastConfig.broadcastUrl}?DOSTATE"
        self.backendHost = backendHost
        self.selectedServeNumber = selectedServeNumber
        
        try:
            response = requests.get(self.getStatusUrl, timeout=2)
            if response.status_code == 200:
                self.broadcastIsAvailable = True
            else:
                self.broadcastIsAvailable = False
        except:
            self.broadcastIsAvailable = False
        
        
        
    def startBroadCast(self):
        if self.broadcastIsAvailable:
            isGetStatus = False
            while True: 
                try: 
                    response = requests.get(self.getStatusUrl)
                    isGetStatus = True
                    break
                except Exception as e:
                    print(f'방송 상태 요청 실패 : {e}')
                    break
                
            while isGetStatus : 
                if not int(response.text.replace("relays", "")[int(self.poleNumber)]): # 방송 꺼져있으면
                    try :
                        response = requests.get(self.turnOnUrl, timeout=5)
                    except Exception as e:
                        print(f'방송 시작 요청 실패 : {e}')
                        continue
                else : break
                
                logMessage = f'{self.poleNumber}번 폴 방송 송출'
                encodedLogMessage = quote_plus(logMessage)
                url = f"http://{self.backendHost}/forVideoServer/setVideoServerLog?videoServerIndex={self.selectedServeNumber}&logMessage={encodedLogMessage}"
                try:
                    res = requests.get(url, timeout=5)
                except Exception as e:
                    print(f'방송 로그 저장 실패 : {e}')
                    
                if res.status_code == 200:
                    print(f'방송 로그 저장 성공 : {res.text}')
                else : 
                    print(f'방송 로그 저장 실패 : {res.text}')
                    
                break         
                
                
                    
import requests

class BroadcastConfig():
    def __init__(self):
        self.index = None
        self.poleNumber = None
        self.broadcastUrl = None
        self.targetDetectCCTV = None
        self.playTime = None
        
    def getData(self, row: dict):
        keys = row.keys() if row else []
        for key in keys:
            self.setData(data=row, key=key)
            
    def setData(self, data:dict, key: str):
        try :
            setattr(self, key, data[key])
        except Exception as e :
            setattr(self, key, None)
            print('setData err : ', e)
            
class BroadcastStore():
    def __init__(self, backendHost = "192.168.0.31:7000"):
        self.broadcastConfig:list[BroadcastConfig] = []
        self.backendHost = backendHost
        
    def getData(self):
        try:
            #response = requests.get(f"http://{self.backendHost}/forVideoServer/getBroadcastConfig")
            #broadcastData:list[dict] = response.json()    
            return
        except Exception as e :
            broadcastData = []
            print('DetectCCTVStore.getData err : ', e)
        
        for row in broadcastData:
            broadcast = BroadcastConfig()
            broadcast.getData(row)
            self.broadcastConfig.append(broadcast)
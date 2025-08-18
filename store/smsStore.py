import requests

class SmsDestination():
    def __init__(self):
        self.phone = None
        self.group = None
        
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
            
class SmsConfig():
    def __init__(self):
        self.index = None
        self.server = None
        self.port = None
        self.database = None
        self.username = None
        self.password = None
        self.callback = None

        
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
            
class SmsDestinationStore():
    def __init__(self, backendHost = "192.168.0.31:7000"):
        self.smsDestination:list[SmsDestination] = []
        self.backendHost = backendHost
        
    def getData(self):
        try:
            #response = requests.get(f"http://{self.backendHost}/sms/getSmsDestination")
            #smsDestinationData:list[dict] = response.json()    
            return
        except Exception as e :
            smsDestinationData = []
            print('GroupStore.getData err : ', e)
        
        for row in smsDestinationData:
            smsDestination = SmsDestination()
            smsDestination.getData(row)
            self.smsDestination.append(smsDestination)
            
class SmsConfigStore():
    def __init__(self, backendHost = "192.168.0.31:7000"):
        self.smsConfig:list[SmsConfig] = []
        self.backendHost = backendHost
        
    def getData(self):
        try:
            #response = requests.get(f"http://{self.backendHost}/forVideoServer/getSmsConfig")
            #smsConfigData:list[dict] = response.json()    
            return
        except Exception as e :
            smsConfigData = []
            print('GroupStore.getData err : ', e)
        
        for row in smsConfigData:
            smsConfig = SmsConfig()
            smsConfig.getData(row)
            self.smsConfig.append(smsConfig)
            

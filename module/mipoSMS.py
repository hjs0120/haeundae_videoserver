from store.cctvStore import DetectCCTV
from store.smsStore import SmsConfig
import pyodbc
import datetime

class Sms:
    def __init__(self, smsConfig: SmsConfig, detectCCTV: DetectCCTV):
        self.callback = smsConfig.callback
        self.detectCCTV = detectCCTV
        self.connectionString = ('DRIVER={freeTDS};'
                            f'SERVER={smsConfig.server};'
                            f'PORT={smsConfig.port};'
                            f'DATABASE={smsConfig.database};'
                            f'UID={smsConfig.username};PWD={smsConfig.password};'
                            f'TDS_Version=7.0')

    
    def sendSms(self, phoneList:list[str], numberOfObject: int):
        text = f"[침입자 알림]\n{datetime.datetime.now().replace(microsecond = 0)}\n\n{self.detectCCTV.cctvName} : {numberOfObject}명의 침입자 감지."
        print(self.connectionString)
        conn = None
        try:
            conn = pyodbc.connect(self.connectionString)
            cursor = conn.cursor()

            for phone in phoneList:
                query = """
                INSERT INTO "MipoSMS"."dbo"."msg_queue" (msg_type, dstaddr, callback, stat, TEXT, request_time)
                VALUES ('1', ?, ?, '0', ?, getdate())
                """
                cursor.execute(query, phone, self.callback, text)
                
            # 트랜잭션 커밋
            conn.commit()
            print(f"{cursor.rowcount}개 행 변경")

        except pyodbc.Error as e:
            print(f"Database error: {e}")

        finally:
            # 연결 닫기
            if conn:
                conn.close()
        
if __name__ == "__main__":
    smsConfig = SmsConfig('119.198.112.80', '3700', 'MipoSMS', 'miposms', 'mipo@3700')
    sms = Sms(smsConfig)
    sms.sendSms(['01067666873', '01067666872'], 2)

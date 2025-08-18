import os
import shutil

from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.exception_handlers import HTTPException
from hypercorn.config import Config
from hypercorn.asyncio import serve

import requests
import aiofiles
from zipfile import ZipFile
import re
import json
import asyncio
from videoProcess.detectVideoProcess import SharedDetectData
from videoProcess.saveVideo import SaveVideo
from store.configStore import ServerConfig

import time

class DetectVideoServer():
    def __init__(self, port, backendHost, selectedConfig:ServerConfig, sharedDetectDataList:list[SharedDetectData], saveVideoList: list[SaveVideo]):
        super(DetectVideoServer, self).__init__()    
        self.serverPort = int(port)
        
        self.app = FastAPI()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"], 
            allow_credentials=True,
            allow_methods=["*"], 
            allow_headers=["*"], 
        )
        
        self.app.mount("/public", StaticFiles(directory="public"), name="public")
                
        @self.app.delete("/deleteFile/{file:path}")
        async def delete_file(file: str):
            print(file)
            try: 
                filePath = os.path.join("public/", file)
                if os.path.exists(filePath):
                    try:
                        if os.path.isfile(filePath):
                            os.remove(filePath)
                        elif os.path.isdir(filePath):
                            shutil.rmtree(filePath)
                        else:
                            raise HTTPException(status_code=400, detail="Invalid file path")
                        return {"message": "File deleted successfully"}
                    except Exception as e:
                        raise HTTPException(status_code=500, detail=f"Error deleting file: {str(e)}")
                else:
                    _, ext = os.path.splitext(filePath)
                    index = file.split('/')[1]
                    if ext.lower() == '.mp4':
                        saveVideoList[int(index)].wrongDetectionQueue.put(filePath)
                    raise HTTPException(status_code=404, detail="File not found")
            except Exception as e:
                return {"error": f"파일이 존재하지 않습니다. {e}"},
            
        async def get_file_size(fp):
            try:
                async with aiofiles.open(fp, 'rb') as f:
                    return await f.seek(0, os.SEEK_END)
            except Exception as e:
                print(f"Error reading file {fp}: {e}")
                return 0
            
        @self.app.get("/checkStorage")
        async def checkStorage():
            path = "public/eventVideo"
            if not os.path.exists(path):
                return {"error": "Path does not exist"}
            totalSize = 0
            arr = []
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        arr.append(get_file_size(fp))
            sizes = await asyncio.gather(*arr) 
            totalSize = sum(sizes)
            sizeMb = totalSize / (1024 * 1024)
            print(sizeMb)
            return sizeMb
            
        @self.app.get("/download/")
        async def download_zip(files: str, backgroundTasks: BackgroundTasks):
            fileList:list[str] = json.loads(files)
            folderName = fileList[0].split('/')[0]
            zipFilename = f"{folderName}.zip"
            
            with ZipFile(zipFilename, 'w') as zipf:
                for filePath in fileList:
                    fileFullPath = os.path.join("public", filePath)
                    if os.path.exists(fileFullPath):
                        zipf.write(fileFullPath, arcname=os.path.basename(fileFullPath))
                    else:
                        raise HTTPException(status_code=404, detail=f"File not found: {filePath}")
            
            backgroundTasks.add_task(os.remove, zipFilename)

            return FileResponse(zipFilename, media_type='application/zip', filename=zipFilename)
             
        @self.app.get("/")
        async def main():
            return {"message": "Welcome to DetectVideoServer!"}
    
        @self.app.get("/updateConfigSetting")
        async def updateSmsDestination(configSetting:str = Query(...)):
            sensitivity = json.dumps(json.loads(configSetting)["detectionSensitivity"])

            for sharedDetectData in sharedDetectDataList:
                sharedDetectData.settingQueue.put(configSetting)
                sharedDetectData.sensitivityQueue.put(sensitivity)
            return {"message": "roi 적용 성공"}
    
        @self.app.get("/updateSmsDestination/{index}")
        async def updateSmsDestination(index, phone:str = Query(...)):
            index = int(index)
            phone = json.loads(phone)
            for phoneNumber in phone:
                sharedDetectDataList[index].smsDestinationQueue.put(phoneNumber)
            sharedDetectDataList[index].smsDestinationQueue.put(None)
            return {"message": "roi 적용 성공"}
    
        @self.app.get("/updateRoi/{index}")
        async def updateRoi(index):
            index = int(index)
            roi = requests.get(f"http://{backendHost}/forVideoServer/getRoi?port={port}&index={index}")
            roi = json.loads(roi.text)[0]
            print('roi', roi, flush=True)
            sharedDetectDataList[index].eventRegionQueue.put(['roi'])
            for polygon in roi:
                for coord in polygon:
                    sharedDetectDataList[index].eventRegionQueue.put([coord['x'], coord['y']])
                sharedDetectDataList[index].eventRegionQueue.put(['endSign'])
            sharedDetectDataList[index].eventRegionQueue.put(None)
            return {"message": "roi 적용 성공"}
                
        @self.app.get("/updateRoe/{index}")
        async def updateRoe(index):
            index = int(index)
            roe = requests.get(f"http://{backendHost}/forVideoServer/getRoe?port={port}&index={index}")
            roe = json.loads(roe.text)[0]
            print('roe', roe, flush=True)
            sharedDetectDataList[index].eventRegionQueue.put(['roe'])
            for polygon in roe:
                for coord in polygon:
                    sharedDetectDataList[index].eventRegionQueue.put([coord['x'], coord['y']])
                sharedDetectDataList[index].eventRegionQueue.put(['endSign'])
            sharedDetectDataList[index].eventRegionQueue.put(None)
            return {"message": "roe 적용 성공"}
                
        @self.app.get("/stopDetect/{index}")
        async def stopDetect(index):
            try:
                index = int(index)
                eventRegionQueue = sharedDetectDataList[index].eventRegionQueue
                eventRegionQueue.put(['roi'])
                eventRegionQueue.put([])
                eventRegionQueue.put(['endSign'])
                eventRegionQueue. put(None)
                print(f"{port}/{index}: stopDetect 성공")
            except Exception as e:
                print(f"stopDetect 오류: {e}")
                  
        @self.app.get("/runDetect/{index}")
        async def runDetect(index):
            try:
                index = int(index)
                roi = requests.get(f"http://{backendHost}/forVideoServer/getRoi?port={port}&index={index}")
                roi = json.loads(roi.text)[0]
                eventRegionQueue = sharedDetectDataList[index].eventRegionQueue
                eventRegionQueue.put(['roi'])
                for polygon in roi:
                    for coord in polygon:
                        eventRegionQueue.put([coord['x'], coord['y']])
                    eventRegionQueue.put(['endSign'])
                eventRegionQueue.put(None)
                print(f"{port}/{index}: runDetect 성공")
                
            except Exception as e:
                print(f"runDetect 오류: {e}")
            
        streamClient = [[] for index in range(selectedConfig.wsIndex)]
        
        async def _send_full_once(websocket, sharedDetectDataList, index: int):
            sd = sharedDetectDataList[index]
            full_len = getattr(sd, "sharedFullLen", None)
            if isinstance(full_len, int) and full_len > 0:
                await websocket.send_bytes(bytes(sd.sharedFullFrame[:full_len]))
            else:
                await websocket.send_bytes(bytes(sd.sharedFullFrame[:]))  # 길이 메타 없으면 전체
        
        def _is_ready(sd) -> bool:
            full_len = getattr(sd, "sharedFullLen", None)
            if isinstance(full_len, int):
                return full_len > 0
            try:
                buf = getattr(sd, "sharedFullFrame", None)
                return buf is not None and len(bytes(buf[:])) > 0
            except Exception:
                return False

        async def _wait_first_frame(sharedDetectDataList, index, timeout=3.0):
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                try:
                    sd = sharedDetectDataList[index]
                except Exception:
                    await asyncio.sleep(0.05); continue
                if _is_ready(sd):
                    return True
                await asyncio.sleep(0.05)
            return False
        
        
        @self.app.websocket("/ws/stream/{index}")
        async def websocketStream(websocket: WebSocket, index):
            index = int(index)
            await websocket.accept()
            streamClient[index].append(websocket)
            print(f'{port}/{index}: accept, clients={len(streamClient[index])}')

            # 0) 기본 FPS로 즉시 시작 (환경변수 DEFAULT_STREAM_FPS 허용; 없으면 10)
            
            fps = 2.0
            fps = max(1.0, min(60.0, fps))
            interval = 1.0 / fps
            # 첫 루프에서 바로 전송되도록 last_sent를 interval만큼 과거로 설정
            last_sent = time.monotonic() - interval

            # 시작 즉시 ACK
            #await websocket.send_text(f"fps={int(fps)}")
            print(f'{port}/{index}: start fps={fps}')

            # 1) 전송/수신 단일 루프
            while websocket.client_state == WebSocketState.CONNECTED:
                try:
                    # 다음 프레임 전송까지 남은 시간
                    now = time.monotonic()
                    remaining = max(0.0, (last_sent + interval) - now)

                    # 남은 시간 동안만 명령 수신 대기
                    #  - 명령이 오면 즉시 처리하고 다음 루프로
                    #  - 안 오면 Timeout -> 프레임 전송
                    try:
                        cmd = await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
                    except asyncio.TimeoutError:
                        cmd = None

                    if cmd is not None:
                        t = (cmd or "").strip().lower()
                        if t == "stop":
                            await websocket.close()
                            if websocket in streamClient[index]:
                                streamClient[index].remove(websocket)
                            print(f'{port}/{index}: stop by client')
                            break
                        else:
                            # 숫자면 FPS 변경
                            try:
                                new_fps = float(t)
                                new_fps = max(1.0, min(60.0, new_fps))
                                if abs(new_fps - fps) > 1e-6:
                                    fps = new_fps
                                    interval = 1.0 / fps
                                    await websocket.send_text(f"fps={int(fps)}")  # ACK
                                    print(f'{port}/{index}: fps -> {fps}')
                                    # 새 FPS 즉시 반영: 다음 전송 시점을 지금으로 리셋
                                    last_sent = time.monotonic() - interval
                            except ValueError:
                                # 기타 텍스트 명령은 무시
                                pass
                        # 명령 처리 후 다음 반복으로 (전송 타이밍은 위에서 리셋/유지됨)
                        continue

                    # 여기까지 왔다는 건 remaining 만료(Timeout) → 전송 시점 도래
                    try:
                        await _send_full_once(websocket, sharedDetectDataList, index)
                    except WebSocketDisconnect:
                        break
                    except Exception as e:
                        print(f'{port}/{index}: send err -> {e!r}')
                        break
                    last_sent = time.monotonic()

                except WebSocketDisconnect:
                    try: await websocket.close()
                    finally:
                        if websocket in streamClient[index]: streamClient[index].remove(websocket)
                    print(f'{port}/{index}: close, clients={len(streamClient[index])}')
                except Exception as e:
                    try: await websocket.close()
                    finally:
                        if websocket in streamClient[index]: streamClient[index].remove(websocket)
                    print(f'{port}/{index}: stream err -> {e!r}, clients={len(streamClient[index])}')
        
        alarmClient = [[] for index in range(selectedConfig.wsIndex)]
        @self.app.websocket("/ws/alarm/{index}")
        async def websocketIsDetect(websocket: WebSocket, index):
            index = int(index)
            await websocket.accept()
            alarmClient[index].append(websocket)
            print(f'{port}/{index}: alarm accept, Current Client: {len(streamClient[index])}', flush=True)
            while websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.receive()
                    await websocket.send_bytes(sharedDetectDataList[index].sharedDetectFlag.value)
                    await asyncio.sleep(1/5)
                    
                except WebSocketDisconnect:
                    await websocket.close()
                    streamClient[index].remove(websocket)
                    print(f'{port}/{index}: alarm close, Current Client: {len(streamClient[index])}', flush=True)
                    break
                
                except Exception as e:
                    print(f'err : {e}')
                    await websocket.close()
                    break
        
    def run(self):
        config = Config()
        config.bind = f'0.0.0.0:{self.serverPort}'
        try:
            asyncio.run(serve(self.app, config))
        except Exception as e:
            print(f'{self.serverPort}serve 에러 : {e}')
        
def split_path_filename(s):
    match = re.match(r'(.+)/(.+)', s)
    if match:
        return match.groups()
    return None
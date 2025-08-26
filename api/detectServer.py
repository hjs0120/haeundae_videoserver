import os
import shutil


from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, BackgroundTasks
import contextlib
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState
#from fastapi.responses import HTMLResponse, FileResponse
from fastapi.responses import FileResponse, StreamingResponse
from fastapi import Request
import re
from fastapi.exception_handlers import HTTPException
from hypercorn.config import Config
from hypercorn.asyncio import serve

import requests
import aiofiles
from zipfile import ZipFile
#import refCORSMiddleware
import json
import asyncio
#from videoProcess.detectVideoProcess import SharedDetectData
from videoProcess.sharedData import SharedDetectData
from videoProcess.saveVideo import SaveVideo
from store.configStore import ServerConfig

from types import SimpleNamespace

import time

import logging
logger = logging.getLogger(__name__)

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
                #print(f"Error reading file {fp}: {e}")
                logger.error(f"Error reading file {fp}: {e}")
                return 0
            
        @self.app.get("/checkStorage")
        async def checkStorage():
            path = "public/"
            if not os.path.exists(path):
                logger.error("Path does not exist")
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
            #print(sizeMb)
            logger.info(sizeMb)
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
            #logger.info(f"updateConfigSetting called: {configSetting}")
            try:
                cfg_raw = json.loads(configSetting) if isinstance(configSetting, str) else configSetting
                ns = float(cfg_raw.get("detectionSensitivity", 0.5)); ns = max(0.01, min(0.99, ns))
                dp = int(cfg_raw.get("detectionPeriod", 3)); dp = max(1, dp)
                ct = int(cfg_raw.get("continuousThreshold", 3)); ct = max(1, ct)
                payload = SimpleNamespace(detectionSensitivity=ns, detectionPeriod=dp, continuousThreshold=ct)
                for s in sharedDetectDataList:
                    s.settingQueue.put(payload)  # 문자열 대신 객체로 전달
                logger.info(f"updateConfigSetting OK (sens={ns}, period={dp}, cont={ct})")
                return {"ok": True}
            except Exception as e:
                logger.error(f"updateConfigSetting error: {e}")
                return {"ok": False, "error": str(e)}


    
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
            try:
                index = int(index)
                s = sharedDetectDataList[index]
                # 1) 백엔드에서 ROI 좌표 조회
                resp = requests.get(
                    f"http://{backendHost}/forVideoServer/getRoi",
                    params={"port": port, "index": index},
                    timeout=5
                )
                resp.raise_for_status()
                data = resp.json()
                # data 예: [ [ {"x":..,"y":..}, ... ], [..], ... ]
                raw = data[0] if isinstance(data, list) and data else []
                # 2) 정규화(정수 튜플 + 최소 3점)
                polys = []
                for poly in raw:
                    pts = []
                    for p in poly:
                        if "x" in p and "y" in p:
                            pts.append((int(p["x"]), int(p["y"])))
                    if len(pts) >= 3:
                        polys.append(tuple(pts))
                # 3) 공유변수 교체 + 버전 증가
                with s.region_lock:
                    s.roi_coords = tuple(polys)       # 불변 구조 통째 교체
                    s.region_ver.value += 1           # 변경 통지
                logger.info(f"{port}/{index}: updateRoi 적용 (n={len(polys)})")
                return {"ok": True, "count": len(polys)}
            except Exception as e:
                logger.error(f"{port}/{index}: updateRoi 오류: {e}")
                return {"ok": False, "error": str(e)}

        @self.app.get("/updateRoe/{index}")
        async def updateRoe(index):
            try:
                index = int(index)
                s = sharedDetectDataList[index]
                # 1) 백엔드에서 ROE 좌표 조회
                resp = requests.get(
                    f"http://{backendHost}/forVideoServer/getRoe",
                    params={"port": port, "index": index},
                    timeout=5
                )
                resp.raise_for_status()
                data = resp.json()
                raw = data[0] if isinstance(data, list) and data else []
                # 2) 정규화
                polys = []
                for poly in raw:
                    pts = []
                    for p in poly:
                        if "x" in p and "y" in p:
                            pts.append((int(p["x"]), int(p["y"])))
                    if len(pts) >= 3:
                        polys.append(tuple(pts))
                # 3) 공유변수 교체 + 버전 증가
                with s.region_lock:
                    s.roe_coords = tuple(polys)
                    s.region_ver.value += 1
                logger.info(f"{port}/{index}: updateRoe 적용 (n={len(polys)})")
                return {"ok": True, "count": len(polys)}
            except Exception as e:
                logger.error(f"{port}/{index}: updateRoe 오류: {e}")
                return {"ok": False, "error": str(e)}

        @self.app.get("/runDetect/{index}")
        async def runDetect(index):
            try:
                index = int(index)
                s = sharedDetectDataList[index]
                # 분석은 항상 수행됨. 전송/액션만 허용.
                s.runDetectFlag.value = True
                logger.info(f"{port}/{index}: runDetect 성공 (전송/액션 ON)")
                return {"ok": True}
            except Exception as e:
                logger.error(f"{port}/{index}: runDetect 오류: {e}")
                return {"ok": False, "error": str(e)}

        @self.app.get("/stopDetect/{index}")
        async def stopDetect(index):
            try:
                index = int(index)
                s = sharedDetectDataList[index]
                # 분석은 계속. 전송/액션만 차단.
                s.runDetectFlag.value = False
                logger.info(f"{port}/{index}: stopDetect 성공 (전송/액션 OFF)")
                return {"ok": True}
            except Exception as e:
                logger.error(f"{port}/{index}: stopDetect 오류: {e}")
                return {"ok": False, "error": str(e)}

        streamClient = [[] for index in range(selectedConfig.wsIndex)]


        async def _send_full_once(websocket, sharedDetectDataList, index: int):
            sd = sharedDetectDataList[index]
            full_len = getattr(sd, "sharedFullLen", None)
            if isinstance(full_len, int) and full_len > 0:
                await websocket.send_bytes(bytes(sd.sharedFullFrame[:full_len]))
            else:
                await websocket.send_bytes(bytes(sd.sharedFullFrame[:]))  # 길이 메타 없으면 전체
        
        @self.app.websocket("/ws/stream/{index}")
        async def websocketStream(websocket: WebSocket, index):
            index = int(index)
            await websocket.accept()
            streamClient[index].append(websocket)
            #print(f'{port}/{index}: accept, clients={len(streamClient[index])}')
            logger.info(f'{port}/{index}: accept, clients={len(streamClient[index])}')

            # 0) 기본 FPS로 즉시 시작 (환경변수 DEFAULT_STREAM_FPS 허용; 없으면 10)
            
            fps = 2.0
            fps = max(1.0, min(60.0, fps))
            interval = 1.0 / fps
            # 첫 루프에서 바로 전송되도록 last_sent를 interval만큼 과거로 설정
            last_sent = time.monotonic()

            # 시작 즉시 ACK
            #await websocket.send_text(f"fps={int(fps)}")
            #print(f'{port}/{index}: start fps={fps}')

            paused  = False

            # 1) 전송/수신 단일 루프

            try:
                while websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        if paused:
                            # ⏸ 일시정지 상태: 전송 스케줄링 없음, 다음 명령만 대기
                            try:
                                cmd = await websocket.receive_text()  # timeout 없음
                            except WebSocketDisconnect:
                                break

                            t = (cmd or "").strip().lower()
                            if t == "stop":
                                await websocket.close()
                                if websocket in streamClient[index]:
                                    streamClient[index].remove(websocket)
                                #print(f'{port}/{index}: stop by client')
                                logger.info(f'{port}/{index}: stop by client')
                                break

                            # 숫자면 FPS 변경 / 재개
                            try:
                                new_fps = float(t)
                                if new_fps <= 0.0:
                                    # 이미 paused이므로 그대로 유지 (ACK만 반환)
                                    await websocket.send_text("fps=0 (paused)")
                                    continue
                                # 재개
                                fps = min(60.0, max(1.0, new_fps))
                                interval = 1.0 / fps
                                paused = False
                                await websocket.send_text(f"fps={int(fps)}")
                                # 즉시 새 FPS 반영: 다음 전송을 바로 하도록 last_sent 조정
                                last_sent = time.monotonic() - interval
                                #print(f'{port}/{index}: resume, fps -> {fps}')
                                logger.info(f'{port}/{index}: resume, fps -> {fps}')
                                continue
                            except ValueError:
                                # 기타 텍스트 명령 무시
                                continue

                        else:
                            # ▶ 송신 중 상태: 타임아웃 동안만 명령 대기, 만료되면 프레임 전송
                            now = time.monotonic()
                            remaining = max(0.0, (last_sent + interval) - now)

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
                                    #print(f'{port}/{index}: stop by client')
                                    logger.info(f'{port}/{index}: stop by client')
                                    break
                                else:
                                    # 숫자면 FPS 변경 또는 일시정지
                                    try:
                                        new_fps = float(t)
                                        if new_fps <= 0.0:
                                            paused = True
                                            await websocket.send_text("fps=0 (paused)")
                                            #print(f'{port}/{index}: paused')
                                            logger.info(f'{port}/{index}: paused')
                                            # 일시정지 들어가면 다음 루프에서 paused 블록으로
                                            continue
                                        new_fps = min(60.0, max(1.0, new_fps))
                                        if abs(new_fps - fps) > 1e-6:
                                            fps = new_fps
                                            interval = 1.0 / fps
                                            await websocket.send_text(f"fps={int(fps)}")
                                            #print(f'{port}/{index}: fps -> {fps}')
                                            logger.info(f'{port}/{index}: fps -> {fps}')
                                            # 즉시 반영: 다음 전송 타이밍을 당겨서 곧바로 전송 가능
                                            last_sent = time.monotonic() - interval
                                    except ValueError:
                                        pass
                                continue  # 명령 처리 끝, 다음 루프로

                            # 여기까지 왔으면 remaining 만료 → 전송 시점
                            try:
                                await _send_full_once(websocket, sharedDetectDataList, index)
                                last_sent = time.monotonic()
                            except WebSocketDisconnect:
                                break
                            except Exception as e:
                                # 필요 시 로깅/에러 카운팅 후 재시도/연결정책
                                #print(f'{port}/{index}: send error: {e}')
                                logger.error(f'{port}/{index}: send error: {e}')
                                # 에러 정책에 따라 continue/break
                                continue

                    except WebSocketDisconnect:
                        # 연결 종료 → 루프 종료 (정리는 finally에서 일괄 수행)
                        break
                    except Exception as e:
                        logger.error(f'{port}/{index}: stream err -> {e!r}, clients={len(streamClient[index])}')
                        break
            finally:
                # 모든 종료 경로에서 정리 보장
                try:
                    await websocket.close()
                except Exception:
                    pass
                if websocket in streamClient[index]:
                    streamClient[index].remove(websocket)
                logger.info(f'{port}/{index}: close, clients={len(streamClient[index])}')

        
    def run(self):
        config = Config()
        config.bind = f'0.0.0.0:{self.serverPort}'
        try:
            asyncio.run(serve(self.app, config))
        except Exception as e:
            #print(f'{self.serverPort}serve 에러 : {e}')
            logger.error(f'{self.serverPort}serve 에러 : {e}')
        

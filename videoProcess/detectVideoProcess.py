from torch.multiprocessing import Queue
from concurrent.futures import ThreadPoolExecutor

import os
import contextlib

from av.container import InputContainer
from av.video.frame import VideoFrame
from av import open

from cv2 import imencode, resize, INTER_AREA, LINE_AA, rectangle, IMWRITE_JPEG_QUALITY
from cv2.typing import MatLike


from ultralytics import YOLO

import math
import json
from urllib.parse import quote_plus
import requests
from shapely.geometry import Point
from shapely.geometry.polygon import Polygon
import time
from collections import deque

from store.cctvStore import DetectCCTV
from store.configStore import ServerConfig
from store.smsStore import SmsConfig
from store.configSettingStore import ConfigSetting

from module.broadcast import Broadcast
from videoProcess.detectEventManager import DetectEventManager
from videoProcess.saveVideo import SaveVideo
from videoProcess.sharedData import SharedDetectData

import cv2
import numpy as np

def _poly_to_int_coords(poly):
    """shapely Polygon/MultiPolygon -> [np.ndarray(N,1,2,int32), ...]"""
    polys = []
    if getattr(poly, "geoms", None):  # MultiPolygon
        for g in poly.geoms:
            polys.extend(_poly_to_int_coords(g))
        return polys
    ext = np.array(poly.exterior.coords, dtype=np.int32).reshape(-1,1,2)
    polys.append(ext)
    for hole in poly.interiors:  # 필요 없으면 이 블록 제거 가능(구멍 윤곽선도 그림)
        hole_arr = np.array(hole.coords, dtype=np.int32).reshape(-1,1,2)
        polys.append(hole_arr)
    return polys

def draw_polygon_outline_only(image, polygon, color=(0,255,0), thickness=2, label=None):
    """채우기 없이 외곽선만 그리기"""
    if polygon is None:
        return
    polys = _poly_to_int_coords(polygon)
    for p in polys:
        cv2.polylines(image, [p], isClosed=True, color=color, thickness=thickness, lineType=cv2.LINE_AA)
    if label:
        # 대충 좌상단 근처에 라벨
        xs = [pt[0][0] for p in polys for pt in p]
        ys = [pt[0][1] for p in polys for pt in p]
        tx = max(0, min(xs)); ty = max(0, min(ys) - 6)
        cv2.putText(image, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255,255,255), 1, lineType=cv2.LINE_AA)

def draw_roe_roi_outlines(image, roePolygons, roiPolygons,
                          roe_color=(0,0,255), roi_color=(0,255,0), thickness=2):
    """ROE 먼저, ROI 나중 (겹치면 ROI 라인이 위로 오게)"""
    for i, roe in enumerate(roePolygons or []):
        draw_polygon_outline_only(image, roe, color=roe_color, thickness=thickness, label=f"ROE-{i+1}")
    for i, roi in enumerate(roiPolygons or []):
        draw_polygon_outline_only(image, roi, color=roi_color, thickness=thickness, label=f"ROI-{i+1}")

def detectedVideo(detectCCTV: DetectCCTV, sharedDetectData: SharedDetectData, isRunDetect:bool, broadcast: Broadcast, 
                  serverConfig: ServerConfig, backendHost:str, phoneList:list[str], smsConfig: SmsConfig | None, saveVideo:SaveVideo, 
                  linkedPtzCCTV, configSetting: ConfigSetting):
    # sourcery skip: de-morgan
    
    executor = ThreadPoolExecutor(max_workers=3)
    
    url = detectCCTV.rtsp
    cctvIndex = detectCCTV.index
    imgsz = 1280
    miniWidth = json.loads(os.environ["MINI_SIZE"])[0]
    miniHeight = json.loads(os.environ["MINI_SIZE"])[1]
    mediumWidth = json.loads(os.environ["MEDIUM_SIZE"])[0]
    mediumHeight = json.loads(os.environ["MEDIUM_SIZE"])[1]
    thirtySplitQuality = int(os.environ["thirtySplitQuality"])
    fourthSplitQuality = int(os.environ["fourthSplitQuality"])
    fullFrameQuality = int(os.environ["fullFrameQuality"])
    maxObjectHeight = int(os.environ["maxObjectHeight"])

    rois = detectCCTV.roi
    roiCoords = [[[item['x'], item['y']] for item in roi] for roi in rois]
    roes = detectCCTV.roe
    roeCoords = [[[item['x'], item['y']] for item in roe] for roe in roes]
    
    
    
    fps = 15
    maxDuration = 20  
    saveBufferSize = int(maxDuration * fps)
    frameBuffer: deque[VideoFrame] = deque(maxlen=saveBufferSize)
    
    detectEventManager = DetectEventManager(detectCCTV, serverConfig, backendHost, broadcast, 
                                            sharedDetectData, phoneList, smsConfig, linkedPtzCCTV, configSetting)
    # YOLO INIT
    try:
        #model = YOLO('yolov8s.pt').cuda()
        model = YOLO('yolov8s.pt').cuda()
        print("지능형 Data Load 성공", flush=True)
    except Exception as e:
        print("지능형 Data Load 실패", flush=True)
        
    options={'rtsp_transport': 'tcp',
             'max_delay': '1000',
             'stimeout' : '1000',
             'c:v': 'h264',
             'hwaccel': 'cuda'
             }
    
    while url is not None:
        try:
            # URL 형식에 따라 AV open 분기
            if url.lower().startswith("rtsp://"):
                # RTSP 스트림
                container: InputContainer = open(
                    url,
                    'r',
                    format='rtsp',
                    options=options,
                    buffer_size=102400 * 12,
                    timeout=10
                )
            else:
                # 로컬 파일 또는 HTTP 비디오
                container: InputContainer = open(url, 'r')  # format 지정 안 함

            # 비디오 스트림 찾기
            videoStream = next(s for s in container.streams if s.type == 'video')
            videoStream.thread_type = 'AUTO'
            height, width = 1080, 1920
            
            # 첫 프레임 해상도 확인
            for packet in container.demux(videoStream):
                getFrameFlag = False
                for frame in packet.decode():
                    height, width, _ = frame.to_ndarray(format='bgr24').shape
                    print(url, 'origin Video Size', height, width)
                    getFrameFlag = True
                    break
                if getFrameFlag:
                    break
                
            # REGION INIT
            roiPolygons = [Polygon(mapFromTo(item, width, height) for item in roiCoord) if len(roiCoord) >= 3 else None  for roiCoord in roiCoords]
            roePolygons = [Polygon(mapFromTo(item, width, height) for item in roeCoord) if len(roeCoord) >= 3 else None  for roeCoord in roeCoords]
            if not isRunDetect or isRunDetect is None:
                roiPolygons = [None]
                    
            # FLAG INIT
            cnt = 0
            xyxys = []
            saveVideoRequests:dict[int, str] = {}
            saveVideoFrameCnt = 0
            
            ### VIDEO PROCESS ###
            for packet in container.demux(videoStream) :
                for frame in packet.decode(): 
                    cnt += 1
                    frame:VideoFrame
                    image = frame.to_ndarray(format='bgr24')
                    # UPDATE BY API SIGNAL
                    
                    newRegion = updateRegions(sharedDetectData.eventRegionQueue)
                    if newRegion is not None:
                        if newRegion[0] == 'roi':
                            roiCoords = newRegion[1]
                            roiPolygons = [Polygon(mapFromTo(item, width, height) for item in roiCoord) if len(roiCoord) >= 3 else None  for roiCoord in roiCoords]
                        else:
                            roeCoords = newRegion[1]
                            roePolygons = [Polygon(mapFromTo(item, width, height) for item in roeCoord) if len(roeCoord) >= 3 else None  for roeCoord in roeCoords]
                            
                    newPhoneList = updateSmsDestination(sharedDetectData.smsDestinationQueue)
                    #if newPhoneList is not None:
                        #detectEventManager.updateSmsPhoneList(newPhoneList)
                
                    newDetectionSensitivity = getJsonQueue(sharedDetectData.sensitivityQueue)
                    if newDetectionSensitivity is not None:
                        detectCCTV.detectionSensitivity = newDetectionSensitivity
                        
                    newConfigSetting = getJsonQueue(sharedDetectData.settingQueue)
                    if newConfigSetting is not None:
                        configSetting.getData(newConfigSetting) 
                        detectEventManager.updateContinuousThreshold(configSetting.continuousThreshold)
                    
                    # YOLO DETECT
                    if cnt > configSetting.detectionPeriod:
                        cnt = 0
                        xyxys = []
                        results = model.predict(image, verbose=False, conf=detectCCTV.detectionSensitivity, iou=0.7, imgsz=imgsz, half=True, device='0', classes=0)
                        eventObjectCoords = []
                        for result in results:
                            for xyxy in result.boxes.xyxy:
                                x = int((xyxy[0]+xyxy[2])/2)
                                y = int(xyxy[1] ) if int(xyxy[1]>xyxy[3]) else int(xyxy[3])
                                objectHeight = abs(int(xyxy[1]) - int(xyxy[3]))
                                pos = Point(x,y)            
                                if (not any(pos.within(roePolygon) for roePolygon in roePolygons)) & (objectHeight < maxObjectHeight):
                                    if any(pos.within(roiPolygon) for roiPolygon in roiPolygons) :  
                                        color = (0, 0, 255)
                                        eventObjectCoords.append([x, y])
                                    else :
                                        color = (0, 255, 0)
                                    xyxys.append([xyxy, color]) 
                    
                        detectEventManager.getEvent(eventObjectCoords)
                        if detectEventManager.savingEventLogFlag:
                            detectEventManager.savingEventLogFlag = False
                            outputVideo, ptzOutputVideo = detectEventManager.setEventLog(eventObjectCoords, image)
                            #saveVideo.saveEventQueue.put(ptzOutputVideo)
                            #saveVideoRequests[outputVideo] = saveVideoFrameCnt
                    
                    # IMAGE PROCESS
                    for xyxy, color in xyxys:
                        plotBox(xyxy, image, color)
                        
                    #print("[DBG] image W,H:", image.shape[1], image.shape[0])
                    #print("[DBG] ROI count:", len(roiPolygons), "ROE count:", len(roePolygons))
                    #if roiPolygons: print("[DBG] ROI0 bounds:", roiPolygons[0].bounds)
                    #if roePolygons: print("[DBG] ROE0 bounds:", roePolygons[0].bounds)
                        
                    #draw_roe_roi_outlines(image, roePolygons, roiPolygons, thickness=3)
                        
                    # BUFFER TO SAVE
                    #saveFrame = VideoFrame.from_ndarray(image.copy(), format='bgr24')
                    #frameBuffer.append(saveFrame)
                    #if len(frameBuffer) > saveBufferSize:   
                    #    frameBuffer.popleft()
                    
                    # SAVE BUFFER PROCESS
                    #if saveVideoRequests:
                    #    saveVideoRequest = 0
                    #else:
                    #    saveVideoFrameCnt += 1
                    #    KeyToDelete = []
                    #    for outputVideo_,  saveVideoRequest in saveVideoRequests.items():
                    #        if saveVideoFrameCnt >= saveVideoRequest + saveBufferSize - (fps * 5):
                    #            saveVideo.saveVideo(frameBuffer, fps, outputVideo_)
                    #            KeyToDelete.append(outputVideo_)
                    #    for item in KeyToDelete:
                    #        del saveVideoRequests[item]
                            
                    #    with contextlib.suppress(Exception):
                    #        wrongDetectionDir = saveVideo.wrongDetectionQueue.get_nowait()
                    #        del saveVideoRequests[wrongDetectionDir]
                        
                    # IMAGE RESIZING
                    #miniSizeImage = resizeImage(image, miniWidth, miniHeight)
                    #mediumSizeImage = resizeImage(image, mediumWidth, mediumHeight)
                    
                    #mini_future = executor.submit(encode_webp_pillow, miniSizeImage, thirtySplitQuality)
                    #medium_future = executor.submit(encode_webp_pillow, mediumSizeImage, fourthSplitQuality)
                    full_future = executor.submit(encode_webp_pillow, image, fullFrameQuality)

                    # 결과를 기다림
                    #miniSizeBuffer = mini_future.result()
                    #mediumSizeBuffer = medium_future.result()
                    buffer = full_future.result()

                    # 인코딩된 데이터를 shared memory에 저장
                    #if len(miniSizeBuffer) > len(sharedDetectData.sharedMiniFrame):
                    #    miniSizeBuffer = miniSizeBuffer[:len(sharedDetectData.sharedMiniFrame)]
                    #if len(mediumSizeBuffer) > len(sharedDetectData.sharedMediumFrame):
                    #    mediumSizeBuffer = mediumSizeBuffer[:len(sharedDetectData.sharedMediumFrame)]
                    if len(buffer) > len(sharedDetectData.sharedFullFrame):
                        buffer = buffer[:len(sharedDetectData.sharedFullFrame)]

                    # UPDATE SHARED FRAME VARIABLES
                    #sharedDetectData.sharedMiniFrame[:len(miniSizeBuffer)] = miniSizeBuffer.tobytes()
                    #sharedDetectData.sharedMediumFrame[:len(mediumSizeBuffer)] = mediumSizeBuffer.tobytes()
                    sharedDetectData.sharedFullFrame[:len(buffer)] = buffer.tobytes()
                    
                    
        except Exception as e :
            try:
                print(f'{cctvIndex}번 영상 재생 에러 : {e}', flush=True)
                if hasattr(container, 'close'):
                    container.close()
            except Exception as e:
                print(f'container 조회 실패 : {e}', flush=True)
                
            try:
                logMessage = f"{cctvIndex}번 지능형 영상 재생 에러"
                encodedLogMessage = quote_plus(logMessage)
                setLogUrl = f"http://{backendHost}/forVideoServer/setVideoServerLog?videoServerIndex={serverConfig.index}&logMessage={encodedLogMessage}"
                res = requests.get(setLogUrl)
                if res.status_code == 200:
                    print(res.text)
            except Exception as e : 
                print(f"에러로그 입력 실패: {e}", flush=True)
                
                
        finally:
            time.sleep(3)
            try: 
                if hasattr(container, 'close'):
                    container.close()
                else:
                    print("컨테이너 클로즈 실패: 컨테이너 객체가 존재하지 않음.", flush=True)
            except Exception as e:
                print(f'container 조회 실패 : {e}')
                
def encode_webp_pillow(image, quality=75):
    return imencode('.jpg', image, [IMWRITE_JPEG_QUALITY, quality])[1]

def putEventObjectCoordinates(eventObjectQueue:Queue, eventObjectCoordinates):
    for coordinate in eventObjectCoordinates:
        eventObjectQueue.put(coordinate)
    eventObjectQueue.put(None)

def plotBox(x, img, color=None, line_thickness=2):
    c1, c2 = (int(x[0]), int(x[1])), (int(x[2]), int(x[3]))
    rectangle(img, c1, c2, color, thickness=line_thickness, lineType=LINE_AA)
    
def getJsonQueue(jsonQueue: Queue):
    try:
        data = jsonQueue.get_nowait()
    except Exception: 
        return None
    
    return json.loads(data)
        
def updateSmsDestination(smsDestinationQueue: Queue):
    try:
        data = smsDestinationQueue.get_nowait()
    except Exception:
        return None
    phoneList = []
    while True: 
        if data is None :
            return phoneList
        phoneList.append(data)
        with contextlib.suppress(Exception):
            data = smsDestinationQueue.get_nowait()   
    
def updateRegions(iQueue:Queue):
    try:
        data = iQueue.get_nowait()
    except Exception:
        return None

    coord = []
    if data == ['roi']:
        return updateRegion(iQueue, coord, 'roi')
    elif data == ['roe']:
        return updateRegion(iQueue, coord, 'roe')


# TODO Rename this here and in `updateRegion`
def updateRegion(iQueue, coord, arg2):
    roi = []
    while True:
        try:
            data = iQueue.get_nowait()
        except Exception as e:
            continue
        if data is None:
            break
        if data == ['endSign']:
            roi.append(coord)
            coord = []
            continue
        coord.append(data)
    return [arg2, roi]
           
def resizeImage(image:MatLike, width = 320, height = 206):
    dimension = (width, height)
    return resize(image, dimension, interpolation=INTER_AREA)

def calculatDistance(point1, point2):
    x1, y1 = point1
    x2, y2 = point2
    return math.sqrt((x2 - x1)**2 + (y2 - y1)**2)

def mapFromTo(xy, width, height):
    x = (xy[0]) / (1920) * (width)
    y = (xy[1]) / (1080) * (height)
    
    return [round(x, 4), round(y, 4)]

    

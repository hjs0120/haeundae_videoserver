from torch.multiprocessing import Queue
#from concurrent.futures import ThreadPoolExecutor

import os, contextlib, asyncio

from av.container import InputContainer
#from av.video.frame import VideoFrame
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
#from videoProcess.detectEventManager import DetectEventManager
from videoProcess.saveVideo import SaveVideo
from videoProcess.sharedData import SharedDetectData

import threading

import cv2
import numpy as np
from config import CONFIG

import gc
import ctypes
import psutil
import threading
import time as _time

import logging
logger = logging.getLogger(__name__)

# --- AV 리소스 정리 헬퍼 ---
def _release_av(container=None, video_stream=None):
    """
    남은 디코드 프레임 flush → container close → 참조 해제 → GC → (glibc) 힙 트림.
    파일 소스 재오픈 시 메모리 우상향을 억제하기 위해 반드시 호출.
    """
    try:
        if container is not None:
            # 1) 디코더에 잔여 프레임이 남아있을 수 있으므로 비워줌
            try:
                if video_stream is not None:
                    vs_idx = getattr(video_stream, "index", 0)
                    for _ in container.decode(video=vs_idx):
                        pass
                else:
                    # 스트림 인덱스 모르면 영상 스트림 0만 시도
                    for _ in container.decode(video=0):
                        pass
            except Exception:
                pass
            # 2) 컨테이너 닫기
            with contextlib.suppress(Exception):
                container.close()
    finally:
        # 3) 큰 참조들 정리는 호출부에서 None으로 끊고, 여기서는 힙 회수까지
        gc.collect()
        with contextlib.suppress(Exception):
            ctypes.CDLL("libc.so.6").malloc_trim(0)

# --- 메모리 프로브 ---
def _read_smaps_rollup():
    """ /proc/self/smaps_rollup에서 Pss/Rss/Swap를 읽어 바이트 단위로 반환 """
    pss = rss = swap = 0
    try:
        with open("/proc/self/smaps_rollup", "r") as f:
            for line in f:
                if line.startswith("Pss:"):
                    # e.g. "Pss:                123456 kB"
                    pss = int(line.split()[1]) * 1024
                elif line.startswith("Rss:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith("Swap:"):
                    swap = int(line.split()[1]) * 1024
    except Exception:
        pass
    return rss, pss, swap

def _log_mem(tag: str, extra: dict | None = None):
    """RSS/PSS/스레드/버퍼 길이를 일괄 로깅"""
    p = psutil.Process(os.getpid())
    rss = p.memory_info().rss
    rss2, pss, swap = _read_smaps_rollup()
    ths = p.num_threads()
    info = {
        "RSS_MB(psutil)": f"{rss/1024/1024:.1f}",
        "RSS_MB(smaps)": f"{rss2/1024/1024:.1f}" if rss2 else "n/a",
        "PSS_MB": f"{pss/1024/1024:.1f}" if pss else "n/a",
        "SWAP_MB": f"{swap/1024/1024:.1f}" if swap else "0.0",
        "threads": ths,
    }
    if "frameBuffer" in globals():
        try:
            info["buf_len"] = len(frameBuffer)
            info["buf_item_kb"] = f"{(len(frameBuffer[0])/1024):.0f}" if frameBuffer else "0"
        except Exception:
            pass
    if extra:
        info.update(extra)
    logger.info(f"[MEM] {tag} | " + " ".join(f"{k}={v}" for k,v in info.items()))

def start_mem_watch(interval_sec=60):
    """주기적 메모리 로그(옵션)"""
    def _loop():
        while True:
            _log_mem("periodic")
            _time.sleep(interval_sec)
    t = threading.Thread(target=_loop, daemon=True); t.start()


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
                  linkedPtzCCTV, configSetting: ConfigSetting, mqtt_queue: Queue):
    # sourcery skip: de-morgan
    
    #executor = ThreadPoolExecutor(max_workers=3)
    
    url = detectCCTV.rtsp
    cctvIndex = detectCCTV.index
    imgsz = 640

    fullFrameQuality = CONFIG["fullFrameQuality"]
    maxObjectHeight = CONFIG["maxObjectHeight"]

    #rois = detectCCTV.roi
    #roiCoords = [[[item['x'], item['y']] for item in roi] for roi in rois]
    #roes = detectCCTV.roe
    #roeCoords = [[[item['x'], item['y']] for item in roe] for roe in roes]

    # === 초기 RUN/STOP 상태 반영 ===
    s = sharedDetectData
    # 분석은 항상 수행, 전송/액션만 제어됨
    s.runDetectFlag.value = bool(isRunDetect)

    # === ROI/ROE 초기 로드(FHD 기준 좌표) → 공유변수 기본 세팅 ===
    base_roi = tuple(
        tuple((int(item['x']), int(item['y'])) for item in roi)
        for roi in (detectCCTV.roi or [])
        if isinstance(roi, list) and len(roi) >= 3
    )
    base_roe = tuple(
        tuple((int(item['x']), int(item['y'])) for item in roe)
        for roe in (detectCCTV.roe or [])
        if isinstance(roe, list) and len(roe) >= 3
    )
    if sharedDetectData.roi_coords is None or sharedDetectData.roe_coords is None:
        with sharedDetectData.region_lock:
            if sharedDetectData.roi_coords is None:
                sharedDetectData.roi_coords = base_roi
            if sharedDetectData.roe_coords is None:
                sharedDetectData.roe_coords = base_roe
            # 최초 세팅은 버전 증가 없이도 OK (원하면 +=1)


    
    # 연속 실패 카운트(프레임 변환/유효성 실패)
    fail_count = 0
    fail_threshold = 10  # 연속 10프레임 실패 시 재연결

    fps = 15
    maxDuration = 20  
    saveBufferSize = int(maxDuration * fps)
    #frameBuffer: deque[VideoFrame] = deque(maxlen=saveBufferSize)
    #frameBuffer: deque[np.ndarray] = deque(maxlen=saveBufferSize)
    frameBuffer: deque[bytes] = deque(maxlen=saveBufferSize)
    

    maxStorageSize = int(configSetting.maxStorageSize)

    #logger.info("TP-1")
    #logger.info(maxStorageSize)
    
    #detectEventManager = DetectEventManager(detectCCTV, serverConfig, backendHost, broadcast, 
    #                                        sharedDetectData, phoneList, smsConfig, linkedPtzCCTV, configSetting)
    # YOLO INIT
    try:
        #model = YOLO('yolov8s.pt').cuda()
        model = YOLO('yolov8s.pt').cuda()
        #print("지능형 AI 모델 Load 성공", flush=True)
        logger.info("능형 AI 모델 Load 성공")
    except Exception as e:
        #print("지능형 AI 모델 Load 실패", flush=True)
        logger.error("능형 AI 모델 Load 실패")
        #print("지능형 Data Load 실패", flush=True)
        
    options={'rtsp_transport': 'tcp',
             'max_delay': '1000',
             'stimeout' : '1000',
             'c:v': 'h264',
             'hwaccel': 'cuda'
             }

    src_type = "rtsp"
    prev_time = None
    
    #start_mem_watch(60)
    while url is not None:
        try:
            # 재오픈 루프에서 참조 초기화 (finally에서 항상 정리)
            container = None
            videoStream = None
            packet = None
            frame = None
            image = None
            try:
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
                    src_type = "rtsp"
                else:
                    # 로컬 파일 또는 HTTP 비디오
                    # 분석 버퍼 최소화 옵션으로 내부 버퍼 확장 억제
                    #_log_mem("before_open", {"url": url})
                    container: InputContainer = open(
                        url, 'r',
                        options={
                            "probesize": "32768",          # 32KB
                            "analyzeduration": "0"         # 분석 시간 최소화
                        }
                    )
                    src_type = "video"
                    #_log_mem("after_open", {"src": src_type})
            except Exception as e:
                #print(f"{cctvIndex}번 영상 open 실패: {e}", flush=True)
                logger.error(f"{cctvIndex}번 영상 open 실패: {e}")
                time.sleep(3)   # 잠깐 대기 후 재시도
                continue

            # 비디오 스트림 찾기
            videoStream = next(s for s in container.streams if s.type == 'video')
            videoStream.thread_type = 'AUTO'
            height, width = 1080, 1920

            # 파일일 때만 FPS 추출
            frame_interval = None
            if src_type == "video":
                try:
                    if videoStream.average_rate:
                        video_fps = float(videoStream.average_rate)
                    elif videoStream.guessed_rate:
                        video_fps = float(videoStream.guessed_rate)
                    else:
                        video_fps = None
                    if video_fps and video_fps > 0:
                        frame_interval = 1.0 / video_fps
                except Exception:
                    frame_interval = None
            
            # 첫 프레임 해상도 확인
            for packet in container.demux(videoStream):
                getFrameFlag = False
                for frame in packet.decode():
                    height, width, _ = frame.to_ndarray(format='bgr24').shape
                    #print(url, 'origin Video Size', height, width)
                    logger.info("origin Video Size %dx%d | url=%s", height, width, url)
                    #_log_mem("after_first_frame")
                    getFrameFlag = True
                    break
                if getFrameFlag:
                    break

            # === REGION INIT (FHD 고정: 스케일링 없음) ===
            with sharedDetectData.region_lock:
                _roi_coords = sharedDetectData.roi_coords or tuple()
                _roe_coords = sharedDetectData.roe_coords or tuple()
                local_region_ver = sharedDetectData.region_ver.value

            roiPolygons = [Polygon(poly) for poly in _roi_coords]
            roePolygons = [Polygon(poly) for poly in _roe_coords]

                    
            # FLAG INIT
            cnt = 0
            xyxys = []
            # 요청: { output_path(str): 등록시 saveVideoFrameCnt(int) }
            saveVideoRequests: dict[str, int] = {}
            saveVideoFrameCnt = 0
            intrusion_streak = 0
            saving_lock = threading.Event()
            saving_thread: threading.Thread | None = None
            cooldown_frames = fps * 15              # 저장 후 최소 간격(예: 15초)
            cooldown_until = -1                     # 이 프레임 전까지 트리거 금지            
            
            prev_time = None
            
            ### VIDEO PROCESS ###
           #for packet in container.demux(videoStream) :
           #     for frame in packet.decode(): 

            # --- demux loop with rewind for file inputs ---
            demux_iter = container.demux(videoStream)
            while True:
                packet = next(demux_iter, None)
                if packet is None:
                    if src_type == "video":
                        # EOF -> try rewind instead of reopen
                        with contextlib.suppress(Exception):
                            videoStream.codec_context.flush()
                        try:
                            container.seek(0, stream=videoStream)
                            demux_iter = container.demux(videoStream)
                            # reset per-cycle state (옵션)
                            try:
                                frameBuffer.clear()
                            except Exception:
                                pass
                            cnt = 0; fail_count = 0; saveVideoFrameCnt = 0
                            continue
                        except Exception as _rew_e:
                            logger.warning(f"seek(0) failed; will reopen: {_rew_e}")
                            break  # finally에서 close → 재오픈
                    else:
                        break
                for frame in packet.decode():


                    t0 = time.time()  # 프레임 처리 시작 시각

                    cnt += 1
                    frame:VideoFrame

                    try:
                        image = frame.to_ndarray(format='bgr24')
                        # UPDATE BY API SIGNAL

                        # === 프레임 정상 여부 확인 추가 ===
                        if image is None or image.size == 0:
                            fail_count += 1
                            if fail_count >= fail_threshold:
                                raise RuntimeError(f"{fail_threshold}프레임 연속 실패(빈 프레임)")
                            continue

                        h, w, c = image.shape
                        if h == 0 or w == 0:
                            #print(f"{cctvIndex}번 CCTV: 잘못된 프레임 사이즈 {h}x{w}", flush=True)
                            logger.warning(f"{cctvIndex}번 CCTV: 잘못된 프레임 사이즈 {h}x{w}")
                            fail_count += 1
                            if fail_count >= fail_threshold:
                                raise RuntimeError(f"{fail_threshold}프레임 연속 실패(사이즈 0)")
                            continue
                        
                        # ===== 정상 프레임이므로 카운트 리셋 =====
                        fail_count = 0

                        # === REGION UPDATE (버전 비교 → 변경시에만 재생성) ===
                        shared_ver = sharedDetectData.region_ver.value
                        if shared_ver != local_region_ver:
                            with sharedDetectData.region_lock:
                                _roi_coords = sharedDetectData.roi_coords or tuple()
                                _roe_coords = sharedDetectData.roe_coords or tuple()
                                local_region_ver = sharedDetectData.region_ver.value
                            roiPolygons = [Polygon(poly) for poly in _roi_coords]
                            roePolygons = [Polygon(poly) for poly in _roe_coords]
                            logger.info(
                                f"{cctvIndex}: ROI/ROE 업데이트 반영 "
                                f"(roi={len(_roi_coords)}개, roe={len(_roe_coords)}개, ver={local_region_ver})"
                            )



                        # DRAW REGION
                        newDetectionSensitivity = getJsonQueue(sharedDetectData.sensitivityQueue)
                        if newDetectionSensitivity is not None:
                            detectCCTV.detectionSensitivity = newDetectionSensitivity
                            
                        newConfigSetting = getJsonQueue(sharedDetectData.settingQueue)
                        if newConfigSetting is not None:
                            configSetting.getData(newConfigSetting) 
                        #    detectEventManager.updateContinuousThreshold(configSetting.continuousThreshold)
                        
                        # YOLO DETECT
                        if cnt > configSetting.detectionPeriod:
                            cnt = 0
                            xyxys = []
                            results = model.predict(image, verbose=False, conf=detectCCTV.detectionSensitivity, iou=0.7, imgsz=imgsz, half=True, device='0', classes=0)
                            eventObjectCoords = []
                            roi_intrusion_this_frame = False # 이번 프레임에서 ROI 진입 여부

                            for result in results:
                                for xyxy in result.boxes.xyxy:
                                    x1, y1, x2, y2 = map(int, xyxy)
                                    x = (x1 + x2) // 2
                                    y = max(y1, y2)
                                    objectHeight = abs(y2 - y1)
                                    pos = Point(x, y)

                                    in_roe = any(pos.within(p) for p in roePolygons) if roePolygons else False
                                    in_roi = any(pos.within(p) for p in roiPolygons) if roiPolygons else False

                                    if (not in_roe) and (objectHeight < maxObjectHeight):
                                        if in_roi:
                                            color = (0, 0, 255)
                                            eventObjectCoords.append([x, y])
                                            roi_intrusion_this_frame = True
                                        else:
                                            color = (0, 255, 0)
                                        xyxys.append([(x1, y1, x2, y2), color])


                            # // ADD: 3프레임 연속 진입 감지 → print 1회
                            if roi_intrusion_this_frame:
                                intrusion_streak += 1
                            else:
                                intrusion_streak = 0

                            if intrusion_streak == 3:
                                ts = time.strftime("%Y%m%d_%H%M%S")
                                safe_name = detectCCTV.cctvName.replace(" ", "_").replace("#", "")
                                outdir = f"public/eventVideo/{detectCCTV.index}"
                                outdir_img = f"public/eventImage/{detectCCTV.index}"
                                os.makedirs(outdir, exist_ok=True)
                                os.makedirs(outdir_img, exist_ok=True)

                                outputVideo = f"{outdir}/{safe_name}_{ts}.mp4"
                                outputImg = f"{outdir_img}/{safe_name}_{ts}.jpg"

                                # ① 진행/대기 중이면 새 요청 금지
                                if saving_lock.is_set() or len(saveVideoRequests) > 0:
                                    # 이미 진행 중이거나 대기 요청이 있으면 스킵
                                    pass
                                # ② 쿨다운 중이면 금지
                                elif saveVideoFrameCnt < cooldown_until:
                                    pass
                                else:
                                    # 대기 1건만 등록 (N0 저장)
                                    saveVideoRequests[outputVideo] = saveVideoFrameCnt
                                    #print(f"{cctvIndex}: ROI intrusion detected (3 consecutive frames)", flush=True)
                                    logger.info(f"{cctvIndex}: ROI intrusion detected (3 consecutive frames)")

                                    # 스냅샷 저장
                                    snap_ok = False
                                    try:
                                        if image is None or image.size == 0:
                                            raise ValueError("empty frame")
                                        # 스냅샷용 복사본에 박스 오버레이
                                        _snap = image.copy()
                                        for xyxy, color in xyxys:
                                            plotBox(xyxy, _snap, color)
                                        # JPEG 품질: fullFrameQuality 사용
                                        snap_ok = cv2.imwrite(
                                            outputImg, _snap,
                                            [cv2.IMWRITE_JPEG_QUALITY, int(fullFrameQuality)]
                                        )
                                    except Exception as _se:
                                        logger.warning(f"snapshot save failed: {_se}")
                       
                                    # === MQTT: 중앙 퍼블리셔 큐에 전송 ===
                                    try:
                                        if sharedDetectData.runDetectFlag.value:
                                            #topic_base = CONFIG.get("MQTT_TOPIC", "detect/events").rstrip("/")
                                            #topic = f"{topic_base}/roi/{cctvIndex}"
                                            topic = CONFIG.get("MQTT_TOPIC", "detect/events").rstrip("/")
                                            payload = {
                                                "cctvIndex": int(detectCCTV.index),
                                                "objectCoord": [[int(c[0]), int(c[1])] for c in eventObjectCoords],
                                                "savedImageDir": (
                                                    f'http://{detectCCTV.wsUrl["ip"]}:{detectCCTV.wsUrl["port"]}/{outputImg}'
                                                    if snap_ok else None
                                                ),
                                                "savedVideoDir": f'http://{detectCCTV.wsUrl["ip"]}:{detectCCTV.wsUrl["port"]}/{outputVideo}',
                                                "dateTime": ts,
                                                #"savedPtzVideoDir": f'http://{detectCCTV.wsUrl["ip"]}:{detectCCTV.wsUrl["port"]}/{self.ptzOutputVideo}',
                                            }
                                            if mqtt_queue is not None:
                                                mqtt_queue.put_nowait({"topic": topic, "payload": payload, "qos": 0, "retain": False})
                                    except Exception as _mqe:
                                        logger.warning(f"MQTT queue push failed: {_mqe}")

                        keys_to_delete = []
                        for out_path, req_cnt in list(saveVideoRequests.items()):
                            # N ≥ N₀ + (20초 − 5초) = N₀ + 15초 프레임
                            if saveVideoFrameCnt >= req_cnt + (saveBufferSize - fps*5):
                                if not saving_lock.is_set():
                                    saving_lock.set()
                                    # numpy 스냅샷을 넘겨 저장(완료되면 lock 해제)
                                    snapshot = list(frameBuffer)   # list[bytes]
                                    def _job(frames_bytes, fps_val, path):
                                        try:
                                            saveVideo.save_jpeg_bytes(frames_bytes, fps_val, path)
                                        finally:
                                            saving_lock.clear()
                                            nonlocal cooldown_until
                                            cooldown_until = saveVideoFrameCnt + cooldown_frames
                                    t = threading.Thread(target=_job, args=(snapshot, fps, out_path), daemon=True)
                                    t.start()
                                # 어떤 경우든 같은 요청은 제거(중복 저장 방지)
                                keys_to_delete.append(out_path)
                                
                        for k in keys_to_delete:
                            del saveVideoRequests[k]


                        # IMAGE PROCESS
                        for xyxy, color in xyxys:
                            plotBox(xyxy, image, color)
                            
                        # === BUFFER TO SAVE: numpy(BGR)로만 보관 ===
                        try:
                            if image is None or image.size == 0:
                                raise ValueError("empty frame")
                            if not np.isfinite(image).all():
                                raise ValueError("NaN/Inf in frame")
                            h, w = image.shape[:2]
                            if w <= 0 or h <= 0:
                                raise ValueError(f"bad size {w}x{h}")
                            # 짝수 해상도로 보정(인코딩 호환)
                            tw, th = w - (w % 2), h - (h % 2)
                            if (tw != w) or (th != h):
                                image = cv2.resize(image, (tw, th), interpolation=cv2.INTER_LINEAR)

                            ok, enc = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 65])
                            if ok:
                                frameBuffer.append(enc.tobytes())
                            saveVideoFrameCnt += 1
                        except Exception as _buf_e:
                            #print(f"[CH{cctvIndex}] drop bad frame before save: {_buf_e}", flush=True)
                            logger.error(f"[CH{cctvIndex}] drop bad frame before save: {_buf_e}")

                        # ENCODE & SHARE
                        buffer = encode_webp_pillow(image, fullFrameQuality)

                        if len(buffer) > len(sharedDetectData.sharedFullFrame):
                            buffer = buffer[:len(sharedDetectData.sharedFullFrame)]

                        sharedDetectData.sharedFullFrame[:len(buffer)] = buffer.tobytes()

                    except Exception as e:
                        # 변환/유효성 실패 → 카운트 증가 후 임계치 검사
                        #print(f"{cctvIndex}번 CCTV: 프레임 변환 실패 → {e}", flush=True)
                        logger.error(f"{cctvIndex}번 CCTV: 프레임 변환 실패 → {e}")
                        fail_count += 1
                        if fail_count >= fail_threshold:
                            # 상위 try/except에서 container.close() 및 재연결 처리
                            raise
                        continue


                    # === 슬립 (단순화 버전) ===
                    t1 = time.time()
                    proc_dt = t1 - t0

                    if src_type == "video":
                        if frame_interval:
                            # 목표 간격 - 처리시간 만큼만 쉼 (1배속)
                            sleep_s = frame_interval - proc_dt
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                    else:  # RTSP
                        now = t1
                        if prev_time is not None:
                            dt = now - prev_time  # 이전 프레임부터 현재 프레임까지 실제 간격
                            # 이미 dt만큼 시간이 지나 있음 → 처리시간을 빼고 남은 만큼만 살짝 쉼
                            # (RTSP는 원래 실시간이므로 과도한 슬립은 피하고 상한을 둠)
                            remain = dt - proc_dt
                            if remain > 0:
                                time.sleep(min(remain, 0.05))
                        prev_time = now
                                
                    
        except Exception as e :
            try:
                #print(f'{cctvIndex}번 영상 재생 에러 : {e}', flush=True)
                logger.error(f'{cctvIndex}번 영상 재생 에러 : {e}')
                if hasattr(container, 'close'):
                    container.close()
                    _release_av(container, videoStream)
                    #_log_mem("after_release(except)")
            except Exception as e:
                #print(f'container 조회 실패 : {e}', flush=True)
                logger.error(f'container 조회 실패 : {e}')
                
            try:
                logMessage = f"{cctvIndex}번 지능형 영상 재생 에러"
                encodedLogMessage = quote_plus(logMessage)
                setLogUrl = f"http://{backendHost}/forVideoServer/setVideoServerLog?videoServerIndex={serverConfig.index}&logMessage={encodedLogMessage}"
                res = requests.get(setLogUrl, timeout=3)
                if res.status_code == 200:
                    #print(res.text)
                    logger.error(res.text)
            except Exception as e : 
                #print(f"에러로그 입력 실패: {e}", flush=True)
                logger.error(f"에러로그 입력 실패: {e}")
                
        finally:
            # === 중요: 매 종료 시점에 항상 정리 ===
            time.sleep(3)
            try:
                _release_av(container, videoStream)
            except Exception as e:
                logger.warning(f'AV release warn: {e}')
            #_log_mem("after_release(finally)")
            # 저장 버퍼/스냅샷 등 자료구조도 파일 재오픈 직전에 초기화(중복 보관 방지)
            try:
                if "frameBuffer" in locals() and hasattr(frameBuffer, "clear"):
                    frameBuffer.clear()
            except Exception:
                pass
            # 큰 참조 명시 해제 (GC 대상화)
            try:
                packet = None
                frame = None
                image = None
            except Exception:
                pass
                
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

    

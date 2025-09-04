from torch.multiprocessing import Queue
import os, contextlib, asyncio

from av.container import InputContainer
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
from store.configSettingStore import ConfigSetting

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

from queue import Empty
from types import SimpleNamespace

from time import monotonic

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



def draw_roe_roi_outlines(image, roePolygons, roiPolygons,
                          roe_color=(0,0,255), roi_color=(0,255,0), thickness=2):
    """ROE 먼저, ROI 나중 (겹치면 ROI 라인이 위로 오게)"""
    for i, roe in enumerate(roePolygons or []):
        draw_polygon_outline_only(image, roe, color=roe_color, thickness=thickness, label=f"ROE-{i+1}")
    for i, roi in enumerate(roiPolygons or []):
        draw_polygon_outline_only(image, roi, color=roi_color, thickness=thickness, label=f"ROI-{i+1}")


def detectedVideo(detectCCTV: DetectCCTV, sharedDetectData: SharedDetectData, isRunDetect:bool, 
                  serverConfig: ServerConfig, backendHost:str, saveVideo:SaveVideo, 
                  linkedPtzCCTV, configSetting: ConfigSetting, mqtt_queue: Queue):
    # sourcery skip: de-morgan
    
    #executor = ThreadPoolExecutor(max_workers=3)
    
    url = detectCCTV.rtsp
    cctvIndex = detectCCTV.index
    imgsz = 640

    fullFrameQuality = CONFIG["fullFrameQuality"]
    maxObjectHeight = CONFIG["maxObjectHeight"]

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


    # ★ 최초 1회만 ROI/ROE 요약 로그 출력
    logger.info(
        "[CH%02d] ROI/ROE 초기 세팅 완료 → ROI=%s, ROE=%s",
        int(detectCCTV.index),
        sharedDetectData.roi_coords,
        sharedDetectData.roe_coords
    )            

    # === 초기 CONFIG 반영 (서버 기동 직후에도 즉시 일관 상태 확보) ===
    # - 우선순위: configSetting.* 가 있으면 그 값을 사용, 없으면 detectCCTV/디폴트
    init_sens = float(getattr(configSetting, "detectionSensitivity", 0.5))
    init_sens = max(0.01, min(0.99, init_sens))
    init_period = int(getattr(configSetting, "detectionPeriod", 3)); init_period = max(1, init_period)
    init_cont   = int(getattr(configSetting, "continuousThreshold", 3)); init_cont = max(1, init_cont)
    # 실제 사용 객체들에 적용
    configSetting.detectionSensitivity = init_sens
    configSetting.detectionPeriod = init_period
    configSetting.continuousThreshold = init_cont
    # 마지막 적용 설정 스냅샷(로그/디버깅용)
    cfg = SimpleNamespace(detectionSensitivity=init_sens, detectionPeriod=init_period, continuousThreshold=init_cont)
    logger.info(f"{cctvIndex}: initial cfg applied -> sens={cfg.detectionSensitivity}, period={cfg.detectionPeriod}, cont={cfg.continuousThreshold}")
    
    # 연속 실패 카운트(프레임 변환/유효성 실패)
    fail_count = 0
    fail_threshold = 10  # 연속 10프레임 실패 시 재연결

    fps = 15
    maxDuration = 20  
    saveBufferSize = int(maxDuration * fps)
    frameBuffer: deque[bytes] = deque(maxlen=saveBufferSize)
    
    # YOLO INIT
    try:
        model = YOLO(CONFIG["model_name"]).cuda()
        logger.info("능형 AI 모델 Load 성공")
    except Exception as e:
        logger.error("능형 AI 모델 Load 실패")
        
    options={'rtsp_transport': 'tcp',
             'max_delay': '1000',
             'stimeout' : '1000',
             'c:v': 'h264',
             'hwaccel': 'cuda'
             }

    src_type = "rtsp"
    prev_time = None
    
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
                    container: InputContainer = open(
                        url, 'r',
                        options={
                            "probesize": "32768",          # 32KB
                            "analyzeduration": "0"         # 분석 시간 최소화
                        }
                    )
                    src_type = "video"
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

            last_heartbeat = monotonic()
            frames = 0
            
            ### VIDEO PROCESS ###

            if src_type == "video": prev_frame_idx = -1 # 파일 소스에서 런타임 리와인드 감지용

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
                            # ★ 루프 영상: 이전 요청/쿨다운이 과거 프레임 기준으로 고여 있으면 영원히 시작 못 함
                            saveVideoRequests.clear()
                            cooldown_until = -1
                            intrusion_streak = 0
                            prev_frame_idx = -1
                            logger.info(f"{cctvIndex}: rewind handled after EOF → reset pending requests/cooldown")
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

                        # === REGION UPDATE by QUEUE (non-blocking, 즉시 반영) ===
                        _rq = updateRegions(sharedDetectData.regionQueue)
                        if _rq is not None:
                            try:
                                tag, coords_list = _rq  # ['roi' or 'roe', [ [ [x,y],... ], ... ] ]
                                if tag == 'roi':
                                    roiPolygons = [Polygon(poly) for poly in coords_list]
                                    # 하위 호환/초기 상태 공유를 위해 스냅샷 갱신(선택)
                                    with sharedDetectData.region_lock:
                                        sharedDetectData.roi_coords = tuple(tuple(tuple(p) for p in poly) for poly in coords_list)
                                        local_region_ver = sharedDetectData.region_ver.value + 1
                                        sharedDetectData.region_ver.value = local_region_ver
                                    logger.info(f"{cctvIndex}: ROI 업데이트(QUEUE) 반영 → {len(coords_list)}개")
                                elif tag == 'roe':
                                    roePolygons = [Polygon(poly) for poly in coords_list]
                                    with sharedDetectData.region_lock:
                                        sharedDetectData.roe_coords = tuple(tuple(tuple(p) for p in poly) for poly in coords_list)
                                        local_region_ver = sharedDetectData.region_ver.value + 1
                                        sharedDetectData.region_ver.value = local_region_ver
                                    logger.info(f"{cctvIndex}: ROE 업데이트(QUEUE) 반영 → {len(coords_list)}개")
                            except Exception as _qe:
                                logger.warning(f"{cctvIndex}: regionQueue parse/apply 실패: {_qe}")

                        # (백업 경로) === REGION UPDATE by VERSION (기존 공용버전 비교) ===
                        shared_ver = sharedDetectData.region_ver.value
                        if shared_ver != local_region_ver:
                            logger.info(f"{cctvIndex}: prev region {roiPolygons} / {roePolygons}")
                            with sharedDetectData.region_lock:
                                _roi_coords = sharedDetectData.roi_coords or tuple()
                                _roe_coords = sharedDetectData.roe_coords or tuple()
                                local_region_ver = sharedDetectData.region_ver.value
                            roiPolygons = [Polygon(poly) for poly in _roi_coords]
                            roePolygons = [Polygon(poly) for poly in _roe_coords]
                            logger.info(f"{cctvIndex}: ROI/ROE 업데이트(VER) 반영 (roi={len(_roi_coords)}개, roe={len(_roe_coords)}개, ver={local_region_ver})")


                        # === SETTINGS UPDATE (non-blocking, 서버에서 파싱된 객체 수신) ===
                        _cfg = getJsonQueue(sharedDetectData.settingQueue)
                        if _cfg is not None:
                            try:
                                # 서버가 SimpleNamespace로 넣어줌. 혹시 dict/str 오더라도 호환.
                                if isinstance(_cfg, dict):
                                    _cfg = SimpleNamespace(**_cfg)
                                elif isinstance(_cfg, str):
                                    _cfg = SimpleNamespace(**json.loads(_cfg))

                                if hasattr(_cfg, "detectionSensitivity"):
                                    ns = float(_cfg.detectionSensitivity)
                                    ns = max(0.01, min(0.99, ns))
                                    if ns != configSetting.detectionSensitivity:
                                        logger.info(f"{cctvIndex}: sensitivity {configSetting.detectionSensitivity} -> {ns}")
                                    configSetting.detectionSensitivity = ns
                                if hasattr(_cfg, "detectionPeriod"):
                                    dp = int(_cfg.detectionPeriod)
                                    if dp != configSetting.detectionPeriod:
                                        logger.info(f"{cctvIndex}: detectionPeriod {configSetting.detectionPeriod} -> {dp}")
                                    configSetting.detectionPeriod = dp
                                if hasattr(_cfg, "continuousThreshold"):
                                    ct = int(_cfg.continuousThreshold)
                                    if not hasattr(configSetting, "continuousThreshold") or ct != configSetting.continuousThreshold:
                                        logger.info(f"{cctvIndex}: continuousThreshold {getattr(configSetting,'continuousThreshold',None)} -> {ct}")
                                    configSetting.continuousThreshold = ct

                                # 마지막 적용 설정 스냅샷 갱신
                                cfg = SimpleNamespace(detectionSensitivity=configSetting.detectionSensitivity,
                                                      detectionPeriod=configSetting.detectionPeriod,
                                                      continuousThreshold=configSetting.continuousThreshold)
                                logger.info(
                                   f"{cctvIndex}: cfg applied -> sens={cfg.detectionSensitivity}, "
                                   f"period={cfg.detectionPeriod}, cont={cfg.continuousThreshold}"
                                )
                            except Exception as _ce:
                                logger.warning(f"{cctvIndex}: bad configSetting payload: {_cfg!r} ({_ce})")

                        # YOLO DETECT
                        if cnt > configSetting.detectionPeriod:
                            cnt = 0
                            xyxys = []
                            results = model.predict(image, verbose=False, conf=configSetting.detectionSensitivity, iou=0.7, imgsz=imgsz, half=True, device='0', classes=0)
                            eventObjectCoords = []
                            roi_intrusion_this_frame = False # 이번 프레임에서 ROI 진입 여부

                            for result in results:
                                for xyxy in result.boxes.xyxy:
                                    x1, y1, x2, y2 = map(int, xyxy)
                                    x = (x1 + x2) // 2
                                    y = max(y1, y2)
                                    objectHeight = abs(y2 - y1)
                                    pos = Point(x, y)

                                    in_roe = any(p.covers(pos) for p in roePolygons) if roePolygons else False
                                    in_roi = any(p.covers(pos) for p in roiPolygons) if roiPolygons else False
                                    

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

                            # 디버그: 이번 사이클 개괄(박스/ROI/ROE 히트/연속 카운트)
                            try:
                                box_count = int(sum(len(r.boxes) for r in results))
                            except Exception:
                                box_count = 0
                            logger.debug(
                                f"{cctvIndex}: det_cycle cnt={cnt} boxes={box_count} streak={intrusion_streak} "
                                f"cooldown={cooldown_until} runFlag={int(sharedDetectData.runDetectFlag.value)}"
                            )

                            if intrusion_streak == configSetting.continuousThreshold and sharedDetectData.runDetectFlag.value:
                                ptz_pos = eventObjectCoords[-1] if eventObjectCoords else None
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
                                    #logger.info(f"{cctvIndex}: saving_lock.is_set={saving_lock.is_set()} saveVideoRequests={len(saveVideoRequests)}")
                                    logger.info(
                                        f"{cctvIndex}: saving_lock={saving_lock.is_set()} "
                                        f"saveVideoRequests={len(saveVideoRequests)} "
                                        f"frame={saveVideoFrameCnt} cooldown_until={cooldown_until}"
                                    )                                    
                                    pass
                                # ② 쿨다운 중이면 금지
                                elif saveVideoFrameCnt < cooldown_until:
                                    #logger.info(f"{cctvIndex}: saveVideoFrameCnt ={saveVideoFrameCnt}, cooldown_until={cooldown_until}")
                                    logger.info(f"{cctvIndex}: in cooldown: frame={saveVideoFrameCnt}, until={cooldown_until}")
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
                                        #topic_base = CONFIG.get("MQTT_TOPIC", "detect/events").rstrip("/")
                                        #topic = f"{topic_base}/roi/{cctvIndex}"
                                        topic = CONFIG.get("MQTT_TOPIC", "detect/events").rstrip("/")
                                        payload = {
                                            "cctvIndex": int(detectCCTV.index),
                                            "objectCoord": [[int(c[0]), int(c[1])] for c in eventObjectCoords],
                                            "pos": (
                                                {"x": int(ptz_pos[0]), "y": int(ptz_pos[1])}
                                                if ptz_pos else None
                                            ),
                                            # 좌표계 기준(PTZ 서버에서 필요시 정규화에 사용)
                                            "frameSize": {"w": int(width), "h": int(height)},
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
                                    #logger.info(f"{cctvIndex}: spawning save thread for {out_path} ({len(frameBuffer)} frames)")
                                    saving_lock.set()
                                    snapshot = list(frameBuffer)   # list[bytes]
                                    # 스레드 시작 직전 상태 로그 (길이/fps/경로)
                                    logger.info(
                                        f"[SaveSpawn] frames={len(snapshot)} fps={fps} → {out_path}"
                                    )
                                    def _job(frames_bytes, fps_val, path):
                                        try:
                                            logger.info(f"[SaveJob] start frames={len(frames_bytes)} fps={fps_val} → {path}")
                                            if not frames_bytes:
                                                logger.warning(f"[SaveJob] skip(empty) → {path}")
                                                return
                                            if not isinstance(fps_val, (int, float)) or fps_val <= 0:
                                                logger.error(f"[SaveJob] bad fps={fps_val} → {path}")
                                                return
                                            saveVideo.save_jpegpipe(frames_bytes, fps_val, path)
                                            logger.info(f"[SaveJob] done → {path}")
                                        except Exception:
                                            # 스레드 예외 삼킴 방지
                                            logger.exception(f"[SaveJob] exception → {path}")
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
                                # enc: np.ndarray (uint8 1-D). tobytes()는 한 번만 호출해 재사용
                                jpg_bytes = enc.tobytes()

                                # 1) 이벤트 저장용 순환버퍼에 JPEG 바이트 push
                                frameBuffer.append(jpg_bytes)
                                saveVideoFrameCnt += 1

                                # 2) 실시간 공유메모리에도 같은 JPEG 바이트 복사 (초과 방지)
                                dst = sharedDetectData.sharedFullFrame
                                maxlen = len(dst)
                                n = len(jpg_bytes)
                                if n > maxlen:
                                    n = maxlen
                                # 불필요한 복사 줄이기 위해 memoryview 사용
                                dst[:n] = memoryview(jpg_bytes)[:n]
                                # 길이 메타가 있다면 갱신 (없으면 무시)
                                try:
                                    sharedDetectData.sharedFullLen.value = n
                                except Exception:
                                    pass

                            # ★ 파일 소스 런타임 리와인드 감지(프레임 인덱스 감소)
                            if src_type == "video" and prev_frame_idx != -1 and saveVideoFrameCnt < prev_frame_idx:
                                logger.warning(
                                    f"{cctvIndex}: rewind detected (frame {prev_frame_idx}->{saveVideoFrameCnt}) → "
                                    f"clear pending & reset cooldown"
                                )
                                saveVideoRequests.clear()
                                cooldown_until = -1
                                intrusion_streak = 0
                            prev_frame_idx = saveVideoFrameCnt

                        except Exception as _buf_e:
                            #print(f"[CH{cctvIndex}] drop bad frame before save: {_buf_e}", flush=True)
                            logger.error(f"[CH{cctvIndex}] drop bad frame before save: {_buf_e}")

                        

                        # ENCODE & SHARE
                        #buffer = encode_webp_pillow(image, fullFrameQuality)

                        #if len(buffer) > len(sharedDetectData.sharedFullFrame):
                        #    buffer = buffer[:len(sharedDetectData.sharedFullFrame)]

                        #sharedDetectData.sharedFullFrame[:len(buffer)] = buffer.tobytes()

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
    '''
    try:
        data = jsonQueue.get_nowait()
    except Exception: 
        return None
    
    return json.loads(data)
    '''

    """
    설정 큐에서 최신값만 꺼내 반환.
    - 큐 비어있으면 None
    - 문자열(JSON)이면 파싱 후 dict 반환
    - dict 또는 SimpleNamespace면 그대로 반환
    - bytes/bytearray면 UTF-8로 디코드 후 JSON 파싱 시도
    """
    last = None
    while True:
        try:
            v = jsonQueue.get_nowait()
        except Empty:
            break
        except Exception:
            # 드문 cross-process 오류 안전장치
            break
        else:
            last = v
    if last is None:
        return None
    if isinstance(last, (dict, SimpleNamespace)):
        return last
    if isinstance(last, (bytes, bytearray)):
        try:
            last = last.decode("utf-8")
        except Exception:
            return None
    if isinstance(last, str):
        try:
            return json.loads(last)
        except Exception:
            return None
    return last    
        
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

    

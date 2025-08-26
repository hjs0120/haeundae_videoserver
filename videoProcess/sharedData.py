from torch.multiprocessing import Queue, Array, Value
from multiprocessing.sharedctypes import SynchronizedArray, Synchronized
from multiprocessing import Lock
from typing import Optional, Tuple

from config import CONFIG

class SharedDetectData:
    def __init__(self):
        fullFrameSize = CONFIG["fullFrameSize"]

        self.sharedFullFrame: SynchronizedArray = Array(typecode_or_type='c', size_or_initializer=fullFrameSize)

        # RUN/STOP 제어
        self.runDetectFlag: Synchronized = Value('b', False)
        self.sharedDetectFlag: Synchronized = Value('b', False)

        # === ROI/ROE 공유 좌표(FHD 기준) + 변경 버전/락 ===
        # 불변(튜플) 구조 권장: 갱신 시 통째 교체 → 읽기 안전
        self.roi_coords: Optional[Tuple[Tuple[Tuple[int,int], ...], ...]] = None
        self.roe_coords: Optional[Tuple[Tuple[Tuple[int,int], ...], ...]] = None
        self.region_ver = Value('q', 0)   # 좌표 갱신 시 +1
        self.region_lock = Lock()

        # 기타 큐(필요한 것만 유지)
        self.smsDestinationQueue: Queue = Queue()
        self.ptzCoordsQueue: Queue = Queue()
        self.settingQueue : Queue = Queue()


        
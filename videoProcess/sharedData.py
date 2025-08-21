import os
from torch.multiprocessing import Queue, Array, Value
from multiprocessing.sharedctypes import SynchronizedArray, Synchronized

from config import CONFIG

class SharedDetectData:
    def __init__(self):
        fullFrameSize = CONFIG["fullFrameSize"]
        #fourthSplitSize = CONFIG["fourthSplitSize"]
        #thirtySplitSize = CONFIG["thirtySplitSize"]

        self.sharedFullFrame: SynchronizedArray = Array(typecode_or_type='c', size_or_initializer=fullFrameSize)
        
        self.sharedDetectFlag: Synchronized = Value('b', False)
        self.smsDestinationQueue: Queue = Queue()
        self.eventRegionQueue: Queue = Queue()
        self.ptzCoordsQueue: Queue = Queue()
        self.sensitivityQueue: Queue = Queue()
        self.settingQueue : Queue = Queue()


        
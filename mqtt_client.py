# mqtt_client.py

from __future__ import annotations
import json, logging, time, signal, sys
from typing import Any
from multiprocessing import Queue
from config import CONFIG

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    raise RuntimeError("pip install paho-mqtt 필요") from e

log = logging.getLogger(__name__)

def _to_str(payload: Any) -> str | bytes:
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, (str, bytes)):
        return payload
    return str(payload)

def publisher_loop(q: Queue):
    host = CONFIG.get("MQTT_BROKER", "127.0.0.1")
    port = int(CONFIG.get("MQTT_PORT", 1883))
    client_id = CONFIG.get("CLIENT_ID_detect_server", "detect-server")
    username = CONFIG.get("MQTT_USERNAME") or None
    password = CONFIG.get("MQTT_PASSWORD") or None

    cli = mqtt.Client(client_id=client_id, clean_session=True)
    if username:
        cli.username_pw_set(username, password)

    cli.on_connect = lambda c, u, f, rc: log.info(f"MQTT connected rc={rc} {host}:{port}")
    cli.on_disconnect = lambda c, u, rc: log.info(f"MQTT disconnected rc={rc}")

    cli.connect(host, port, keepalive=60)
    cli.loop_start()

    # 종료 처리: None(센티넬) 수신 시 루프 종료
    running = True
    def _shutdown(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while running:
        try:
            item = q.get(timeout=0.5)
        except Exception:
            continue
        if item is None:  # sentinel
            break
        try:
            topic = item.get("topic")
            payload = _to_str(item.get("payload"))
            qos = int(item.get("qos", 0))
            retain = bool(item.get("retain", False))
            if not topic:
                continue
            ret = cli.publish(topic, payload=payload, qos=qos, retain=retain)
            if ret.rc != 0:
                log.warning(f"MQTT publish rc={ret.rc} topic={topic}")
        except Exception as e:
            log.warning(f"MQTT publish error: {e}")

    try:
        cli.loop_stop()
    finally:
        cli.disconnect()

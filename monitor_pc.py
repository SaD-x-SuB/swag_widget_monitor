# ==================== Импорты ====================
import os
import sys
import time
import re
import datetime
import traceback
import logging
import atexit
import threading

import serial
import serial.tools.list_ports
import requests
import wmi
import pythoncom


# ==================== Логирование ====================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

_start_time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = os.path.join(LOG_DIR, f"monitor_{_start_time}.log")

logger = logging.getLogger("monitor")
logger.setLevel(logging.DEBUG)
logger.propagate = False

_fmt = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)
_console_handler.setLevel(logging.INFO)
logger.addHandler(_console_handler)


def log(msg, level="info"):
    getattr(logger, level.lower(), logger.info)(msg)


def _excepthook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        logger.info("Остановлено пользователем (Ctrl+C)")
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical(
        "Необработанное исключение:\n" + "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)
        )
    )
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _excepthook


def _on_exit():
    logger.info("Скрипт завершён")


atexit.register(_on_exit)

log(f"Запуск monitor_pc.py. Лог-файл: {LOG_FILE}")


# ==================== Настройки ====================
BAUD_RATE = 115200
SEND_INTERVAL = 1.0
OHM_URL = "http://127.0.0.1:8085/data.json"

# ESP32-C3 от Espressif имеет VID 303A. Можно сузить по PID.
ESP_VID = "303A"

RECONNECT_AFTER_FAILURES = 3
RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 2.0


# ==================== Утилита: поиск COM-порта ESP32 ====================
def find_esp_port():
    """
    Ищет COM-порт, принадлежащий ESP32 (по VID 303A).
    Возвращает имя порта ('COM6') или None.
    """
    try:
        for p in serial.tools.list_ports.comports():
            hwid = (p.hwid or "").upper()
            if f"VID:PID={ESP_VID}" in hwid or f"VID_{ESP_VID}" in hwid:
                return p.device
    except Exception as e:
        log(f"Ошибка перебора портов: {e}", "warning")
    return None


# ==================== LibreHardwareMonitor ====================
def walk(node, path=""):
    results = []
    if isinstance(node, dict):
        text = node.get("Text", "")
        value = node.get("Value", "")
        new_path = f"{path}/{text}" if path else text
        if value:
            results.append({"path": new_path, "text": text, "value": value})
        for child in node.get("Children", []):
            results.extend(walk(child, new_path))
    return results


def fetch_sensors():
    try:
        resp = requests.get(OHM_URL, timeout=2)
        resp.raise_for_status()
        data = resp.json()
        root = data.get("Children", [data])
        sensors = []
        for child in root:
            sensors.extend(walk(child))
        return sensors
    except Exception as e:
        log(f"[OHM] Ошибка: {e}", "warning")
        return None


def to_float(value_str):
    try:
        s = str(value_str).replace(",", ".")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if m:
            return float(m.group())
    except Exception:
        pass
    return None


def find(sensors, keywords, unit=None, exclude=None):
    for s in sensors:
        p = s["path"].lower()
        if any(k.lower() in p for k in keywords):
            if exclude and any(e.lower() in p for e in exclude):
                continue
            if unit and unit not in s["value"]:
                continue
            v = to_float(s["value"])
            if v is not None:
                return v
    return None


def get_all_values(sensors):
    v = {k: None for k in
         ["CPU", "RAM", "TEMP", "GPU", "GPUTEMP", "VRAM", "FAN", "CLK", "PWR"]}
    if sensors is None:
        return v

    v["CPU"] = find(sensors, ["CPU/Total", "CPU Package", "CPU Total"], unit="%")
    if v["CPU"] is None:
        v["CPU"] = find(sensors, ["CPU"], unit="%", exclude=["core", "thread"])

    v["RAM"] = find(sensors, ["Memory/Memory Used", "Memory/Used Memory",
                              "RAM/Memory Used"], unit="%")
    if v["RAM"] is None:
        v["RAM"] = find(sensors, ["Memory"], unit="%")

    for name in ["CPU Package", "CPU (Tctl/Tdie)", "CPU (Tctl)", "CPU (Tdie)",
                 "Core Average", "CPU Core", "CPU"]:
        v["TEMP"] = find(sensors, [name], unit="°C")
        if v["TEMP"] is not None:
            break

    v["GPU"] = find(sensors, ["GPU Core/GPU Core Load", "GPU Core/Load",
                              "GPU/Load", "D3D 3D"], unit="%")
    if v["GPU"] is None:
        v["GPU"] = find(sensors, ["GPU"], unit="%")

    for name in ["GPU Core", "GPU Hot Spot", "GPU"]:
        v["GPUTEMP"] = find(sensors, [name], unit="°C")
        if v["GPUTEMP"] is not None:
            break

    v["VRAM"] = find(sensors, ["GPU Memory/GPU Memory Used",
                               "GPU Memory/Used", "D3D Dedicated Memory Used"],
                     unit="%")
    if v["VRAM"] is None:
        v["VRAM"] = find(sensors, ["GPU Memory"], unit="%")

    v["FAN"] = find(sensors, ["Fan", "FAN"], unit="RPM")

    v["CLK"] = find(sensors, ["CPU Core #1/Clock", "CPU Cores/Clock",
                              "CPU/Clock", "Core #1"], unit="MHz")
    if v["CLK"] is None:
        v["CLK"] = find(sensors, ["Clock"], unit="MHz")

    v["PWR"] = find(sensors, ["CPU Package/Power", "CPU/Package",
                              "CPU Cores/Power", "Package"], unit="W")
    if v["PWR"] is None:
        v["PWR"] = find(sensors, ["Package"], unit="W")

    return v


def fmt(val):
    return f"{val:.1f}" if val is not None else "-1"


# ==================== Отправка данных на ESP32 ====================
def run_serial_loop(stop_event):
    """
    Основной цикл: открывает порт, шлёт данные, следит за stop_event.
    При ошибке записи — переподключается.
    """
    port = find_esp_port()
    if port is None:
        log("ESP32 не найдена при старте цикла", "warning")
        return

    log(f"Начинаю работу с {port}")
    ser = None
    failures = 0

    def open_port():
        for attempt in range(1, RECONNECT_ATTEMPTS + 1):
            if stop_event.is_set():
                return None
            try:
                s = serial.Serial(port, BAUD_RATE, timeout=1, write_timeout=1)
                time.sleep(2)
                log(f"Порт {port} открыт (попытка {attempt})")
                return s
            except serial.SerialException as e:
                log(f"Не удалось открыть {port}: {e}", "warning")
                time.sleep(RECONNECT_DELAY)
        log(f"Порт {port} недоступен после {RECONNECT_ATTEMPTS} попыток", "error")
        return None

    ser = open_port()
    if ser is None:
        return

    sensors = fetch_sensors()
    if sensors is None:
        log("LibreHardwareMonitor недоступен. Проверьте http://localhost:8085", "warning")
    else:
        log(f"Найдено сенсоров: {len(sensors)}")

    while not stop_event.is_set():
        try:
            # Перед записью проверяем, что устройство всё ещё подключено
            if find_esp_port() != port:
                log("ESP32 отключена, останавливаю цикл отправки")
                break

            sensors = fetch_sensors()
            v = get_all_values(sensors)

            line = (
                f"CPU:{fmt(v['CPU'])},"
                f"RAM:{fmt(v['RAM'])},"
                f"TEMP:{fmt(v['TEMP'])},"
                f"GPU:{fmt(v['GPU'])},"
                f"GPUTEMP:{fmt(v['GPUTEMP'])},"
                f"VRAM:{fmt(v['VRAM'])},"
                f"FAN:{fmt(v['FAN'])},"
                f"CLK:{fmt(v['CLK'])},"
                f"PWR:{fmt(v['PWR'])}\n"
            )

            try:
                ser.write(line.encode("utf-8"))
                failures = 0
            except (serial.SerialException, OSError) as e:
                failures += 1
                log(f"Ошибка записи ({failures}/{RECONNECT_AFTER_FAILURES}): {e}", "error")
                if failures >= RECONNECT_AFTER_FAILURES:
                    log("Переподключение к порту...", "warning")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    if stop_event.is_set():
                        break
                    ser = open_port()
                    if ser is None:
                        break
                    failures = 0

            time.sleep(SEND_INTERVAL)

        except Exception as e:
            log(f"Ошибка в цикле отправки: {e}", "error")
            time.sleep(2)

    try:
        if ser:
            ser.close()
    except Exception:
        pass
    log("Цикл отправки завершён")


# ==================== WMI-наблюдатель ====================
def wmi_watcher(worker_thread_holder, stop_event):
    """
    Основной цикл: ждёт события подключения/отключения USB.
    При появлении ESP32 — запускает run_serial_loop в отдельном потоке.
    При отключении — просит поток завершиться.
    """
    pythoncom.CoInitialize()
    log("WMI-наблюдатель запущен")

    try:
        c = wmi.WMI()
        watcher = c.Win32_DeviceChangeEvent.watch_for()

        while not stop_event.is_set():
            try:
                event = watcher()
            except Exception as e:
                log(f"WMI watcher error: {e}", "warning")
                time.sleep(1)
                continue

            # EventType: 2 = Device Arrival, 3 = Device Removal
            if event.EventType == 2:
                log(f"USB device arrival (EventType={event.EventType})")
            elif event.EventType == 3:
                log(f"USB device removal (EventType={event.EventType})")
            else:
                continue

            time.sleep(0.5)  # даём Windows досоздать COM-порт

            port = find_esp_port()
            has_esp = port is not None

            worker = worker_thread_holder.get("thread")

            if has_esp and (worker is None or not worker.is_alive()):
                # ESP подключилась и поток не работает — запускаем
                log(f"ESP32 обнаружена на {port}, запускаю поток отправки")
                worker_stop = threading.Event()
                worker = threading.Thread(
                    target=run_serial_loop,
                    args=(worker_stop,),
                    daemon=True,
                )
                worker_thread_holder["thread"] = worker
                worker_thread_holder["stop"] = worker_stop
                worker.start()

            elif not has_esp and worker is not None and worker.is_alive():
                # ESP отключилась — просим поток завершиться
                log("ESP32 отключена, останавливаю поток отправки")
                worker_thread_holder["stop"].set()

    finally:
        pythoncom.CoUninitialize()
        log("WMI-наблюдатель завершён")


# ==================== main ====================
def main():
    stop_event = threading.Event()
    worker_thread_holder = {"thread": None, "stop": None}

    # Проверим, не подключена ли ESP32 уже сейчас — тогда не надо ждать события
    port = find_esp_port()
    if port is not None:
        log(f"ESP32 уже подключена на {port}, запускаю сразу")
        worker_stop = threading.Event()
        worker = threading.Thread(
            target=run_serial_loop, args=(worker_stop,), daemon=True
        )
        worker_thread_holder["thread"] = worker
        worker_thread_holder["stop"] = worker_stop
        worker.start()

    try:
        wmi_watcher(worker_thread_holder, stop_event)
    except KeyboardInterrupt:
        log("Остановлено пользователем")
    finally:
        stop_event.set()
        w = worker_thread_holder.get("stop")
        if w is not None:
            w.set()


if __name__ == "__main__":
    main()
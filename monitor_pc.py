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
import math

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

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
_ch.setLevel(logging.INFO)
logger.addHandler(_ch)


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
atexit.register(lambda: logger.info("Скрипт завершён"))
log(f"Запуск monitor_pc.py. Лог-файл: {LOG_FILE}")


# ==================== Настройки ====================
BAUD_RATE = 460800
SEND_INTERVAL = 1.0
OHM_URL = "http://127.0.0.1:8085/data.json"
ESP_VID = "303A"

CHUNK_SIZE = 64
CHUNK_TIMEOUT = 10.0
PORT_TIMEOUT = 0.05

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GIFS_DIR = os.path.join(SCRIPT_DIR, "GIFs")
THEMES_DIR = os.path.join(SCRIPT_DIR, "Themes")
os.makedirs(GIFS_DIR, exist_ok=True)
os.makedirs(THEMES_DIR, exist_ok=True)

THEME_EXT = ".thme"


# ==================== Поиск COM-порта ====================
def find_esp_port():
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


def discover_all_categories(sensors):
    if sensors is None:
        return {}
    categories = {}

    cpu_load = find(sensors, ["CPU/Total", "CPU Package", "CPU Total"], unit="%")
    if cpu_load is None:
        cpu_load = find(sensors, ["CPU"], unit="%", exclude=["core", "thread"])
    cpu_temp = None
    for name in ["CPU Package", "CPU (Tctl/Tdie)", "CPU (Tctl)", "CPU (Tdie)",
                 "Core Average", "CPU Core"]:
        cpu_temp = find(sensors, [name], unit="°C")
        if cpu_temp is not None:
            break
    cpu_clk = find(sensors, ["CPU Core #1/Clock", "CPU Cores/Clock",
                              "CPU/Clock", "Core #1"], unit="MHz")
    if cpu_clk is None:
        cpu_clk = find(sensors, ["Clock"], unit="MHz")
    cpu_pwr = find(sensors, ["CPU Package/Power", "CPU/Package",
                              "CPU Cores/Power", "Package"], unit="W")
    if cpu_pwr is None:
        cpu_pwr = find(sensors, ["Package"], unit="W")

    if any(v is not None for v in [cpu_load, cpu_temp, cpu_clk, cpu_pwr]):
        categories["CPU"] = {
            "CPU": cpu_load, "TEMP": cpu_temp,
            "CLK": cpu_clk, "PWR": cpu_pwr,
        }

    ram_load = find(sensors, ["Memory/Memory Used", "Memory/Used Memory",
                               "RAM/Memory Used"], unit="%")
    if ram_load is None:
        ram_load = find(sensors, ["Memory"], unit="%")
    if ram_load is not None:
        categories["RAM"] = {"RAM": ram_load}

    gpu_load = find(sensors, ["GPU Core/GPU Core Load", "GPU Core/Load",
                              "GPU/Load", "D3D 3D"], unit="%")
    if gpu_load is None:
        gpu_load = find(sensors, ["GPU"], unit="%")
    gpu_temp = None
    for name in ["GPU Core", "GPU Hot Spot", "GPU"]:
        gpu_temp = find(sensors, [name], unit="°C")
        if gpu_temp is not None:
            break
    vram_load = find(sensors, ["GPU Memory/GPU Memory Used",
                                "GPU Memory/Used", "D3D Dedicated Memory Used"],
                      unit="%")
    if vram_load is None:
        vram_load = find(sensors, ["GPU Memory"], unit="%")

    if any(v is not None for v in [gpu_load, gpu_temp, vram_load]):
        categories["GPU"] = {
            "GPU": gpu_load, "GPUTEMP": gpu_temp, "VRAM": vram_load,
        }

    fan_rpm = find(sensors, ["Fan", "FAN"], unit="RPM")
    if fan_rpm is not None:
        categories["FAN"] = {"FAN": fan_rpm}

    mobo_temp = find(sensors, ["Motherboard", "Chipset", "System"], unit="°C")
    if mobo_temp is not None:
        categories["MOBO"] = {"MTEMP": mobo_temp}

    storage_temp = find(sensors, ["Temperature", "Drive"], unit="°C",
                        exclude=["CPU", "GPU", "Core"])
    storage_used = find(sensors, ["Used Space"], unit="%")
    if storage_temp is not None or storage_used is not None:
        categories["STORAGE"] = {
            "STEMP": storage_temp, "SUSED": storage_used,
        }

    return categories


def fmt(val):
    return f"{val:.1f}" if val is not None else "-1"


def serialize_categories(categories):
    parts = []
    for cat_name, cat_data in categories.items():
        items = []
        for key, val in cat_data.items():
            items.append(f"{key}={fmt(val)}")
        parts.append(f"{cat_name}:" + ",".join(items))
    return ";".join(parts)


# ==================== Общее состояние ====================
serial_lock = threading.Lock()
pause_telemetry = threading.Event()
stop_event = threading.Event()
ser_holder = {"ser": None}

transfer_in_progress = threading.Event()


# ==================== Сканирование содержимого одной директории ====================
def list_dir(rel_path):
    rel_path = rel_path.strip().strip("/")
    target = os.path.join(GIFS_DIR, rel_path.replace("/", os.sep)) if rel_path else GIFS_DIR

    if not os.path.isdir(target):
        log(f"list_dir: не найдена директория {target}", "warning")
        return []

    entries = []
    try:
        for entry in sorted(os.listdir(target)):
            full_path = os.path.join(target, entry)
            if os.path.isdir(full_path):
                entries.append(f"DIR:{entry}")     # ← basename
            elif entry.lower().endswith(".gif") and os.path.isfile(full_path):
                entries.append(entry)              # ← basename
    except Exception as e:
        log(f"Ошибка сканирования {target}: {e}", "error")
    return entries


# ==================== Сканирование тем (.thme) ====================
def scan_themes_recursive(base_dir, prefix=""):
    entries = []
    try:
        for entry in sorted(os.listdir(base_dir)):
            full_path = os.path.join(base_dir, entry)
            rel_path = os.path.join(prefix, entry).replace("\\", "/")
            if os.path.isdir(full_path):
                entries.extend(scan_themes_recursive(full_path, rel_path))
            elif entry.lower().endswith(THEME_EXT) and os.path.isfile(full_path):
                entries.append(rel_path)
    except Exception as e:
        log(f"Ошибка сканирования тем {base_dir}: {e}", "error")
    return entries


def list_themes():
    return scan_themes_recursive(THEMES_DIR)


def read_theme_file(rel_path):
    path = os.path.join(THEMES_DIR, rel_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = []
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#") or stripped.startswith(";"):
                    continue
                lines.append(stripped)
        return "\n".join(lines)
    except Exception as e:
        log(f"Ошибка чтения темы {rel_path}: {e}", "error")
        return None


# ==================== Низкоуровневый обмен ====================
def send_raw(data: bytes):
    ser = ser_holder["ser"]
    if ser is None:
        return
    with serial_lock:
        try:
            ser.write(data)
        except Exception as e:
            log(f"Ошибка записи в Serial: {e}", "error")
            try:
                ser_holder["ser"].close()
            except Exception:
                pass
            ser_holder["ser"] = None


def read_line_raw(timeout=1.0):
    """
    Блокирующее чтение строки до \n.
    ser.read(1) с ser.timeout = PORT_TIMEOUT (0.05 с) — возвращается сразу при байте.
    """
    ser = ser_holder["ser"]
    if ser is None:
        return None

    buf = bytearray()
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            b = ser.read(1)
        except Exception as e:
            log(f"read_line_raw ошибка: {e}", "error")
            return None

        if not b:
            continue

        buf.extend(b)
        if b == b'\n':
            line = bytes(buf).decode("utf-8", errors="replace").strip()
            return line

    return None


def transfer_chunks(data_bytes: bytes, header_magic: str, chunk_magic: str) -> bool:
    size = len(data_bytes)
    if size == 0:
        return False
    chunks = math.ceil(size / CHUNK_SIZE)

    send_raw(f"{header_magic}:{size}:{chunks}\n".encode("utf-8"))

    line = read_line_raw(timeout=15.0)
    if line is None or not line.startswith("@READY"):
        log(f"{header_magic}: не @READY, получили: {line}", "error")
        return False

    for n in range(chunks):
        chunk = data_bytes[n * CHUNK_SIZE:(n + 1) * CHUNK_SIZE]
        cs = len(chunk)

        send_raw(f"{chunk_magic}:{n}:{cs}\n".encode("utf-8"))

        line = read_line_raw(timeout=CHUNK_TIMEOUT)
        if line is None:
            log(f"{header_magic}: таймаут @GO:{n}", "error")
            return False
        if not line.startswith(f"@GO:{n}"):
            log(f"{header_magic}: ожидали @GO:{n}, получили {line}", "error")
            return False

        send_raw(chunk)

        line = read_line_raw(timeout=CHUNK_TIMEOUT)
        if line is None:
            log(f"{header_magic}: таймаут @OK:{n}", "error")
            return False
        if not line.startswith(f"@OK:{n}"):
            log(f"{header_magic}: ожидали @OK:{n}, получили {line}", "error")
            return False

    send_raw(b"@END\n")

    line = read_line_raw(timeout=15.0)
    if line is None:
        log(f"{header_magic}: нет @DONE", "warning")
        return False
    if line.startswith("@DONE"):
        log(f"{header_magic}: OK (@DONE)")
        return True
    log(f"{header_magic}: ожидали @DONE, получили {line}", "warning")
    return False


# ==================== Обработка команд ====================
def handle_ls(path=""):
    path = path.strip().strip("/")
    entries = list_dir(path)
    payload = ",".join(entries)
    log(f"@LS:{path} -> {len(entries)} записей")
    send_raw(f"@LIST:{payload}\n".encode("utf-8"))


def handle_themes_list():
    entries = list_themes()
    payload = ",".join(entries)
    log(f"@LS_THEMES -> {len(entries)} тем")
    send_raw(f"@LIST_THEMES:{payload}\n".encode("utf-8"))


def handle_theme_get(rel_path: str):
    rel_path = rel_path.strip().replace("\\", "/")
    content = read_theme_file(rel_path)
    if content is None:
        log(f"@THEMEGET {rel_path}: не найден", "warning")
        send_raw(b"@ERR:theme_not_found\n")
        return

    data_bytes = content.encode("utf-8")
    log(f"@THEMEGET {rel_path}: {len(data_bytes)} байт")

    try:
        try:
            if ser_holder["ser"] is not None:
                ser_holder["ser"].reset_input_buffer()
        except Exception:
            pass

        ok = transfer_chunks(data_bytes, "@THEMEFILE", "@THEMECHUNK")
        if not ok:
            send_raw(b"@ERR:theme_transfer\n")
    except Exception as e:
        log(f"@THEMEGET {rel_path}: ошибка: {e}", "error")
        send_raw(b"@ERR:theme_read\n")


def handle_get(filename: str):
    filename = filename.strip().replace("\\", "/")
    path = os.path.join(GIFS_DIR, filename.replace("/", os.sep))

    if not os.path.isfile(path):
        log(f"@GET {filename}: не найден", "warning")
        send_raw(b"@ERR:not_found\n")
        return

    size = os.path.getsize(path)
    if size == 0:
        log(f"@GET {filename}: пустой", "warning")
        send_raw(b"@ERR:empty\n")
        return

    chunks = math.ceil(size / CHUNK_SIZE)
    log(f"@GET {filename}: {size} байт, {chunks} чанков по {CHUNK_SIZE}")

    try:
        try:
            if ser_holder["ser"] is not None:
                ser_holder["ser"].reset_input_buffer()
        except Exception:
            pass

        send_raw(f"@FILE:{filename}:{size}:{chunks}\n".encode("utf-8"))

        line = read_line_raw(timeout=15.0)
        if line is None or not line.startswith("@READY"):
            log(f"@GET {filename}: не @READY, получили: {line}", "error")
            return

        with open(path, "rb") as f:
            for n in range(chunks):
                chunk = f.read(CHUNK_SIZE)
                cs = len(chunk)

                send_raw(f"@CHUNK:{n}:{cs}\n".encode("utf-8"))

                line = read_line_raw(timeout=CHUNK_TIMEOUT)
                if line is None:
                    log(f"@GET {filename}: таймаут @GO:{n}", "error")
                    return
                if not line.startswith(f"@GO:{n}"):
                    log(f"@GET {filename}: ожидали @GO:{n}, получили {line}", "error")
                    return

                send_raw(chunk)

                line = read_line_raw(timeout=CHUNK_TIMEOUT)
                if line is None:
                    log(f"@GET {filename}: таймаут @OK:{n}", "error")
                    return
                if not line.startswith(f"@OK:{n}"):
                    log(f"@GET {filename}: ожидали @OK:{n}, получили {line}", "error")
                    return

        send_raw(b"@END\n")

        line = read_line_raw(timeout=15.0)
        if line is None:
            log(f"@GET {filename}: нет @DONE", "warning")
        elif line.startswith("@DONE"):
            log(f"@GET {filename}: OK (@DONE)")
        else:
            log(f"@GET {filename}: ожидали @DONE, получили {line}", "warning")

    except Exception as e:
        log(f"@GET {filename}: ошибка: {e}", "error")
        send_raw(b"@ERR:read\n")


# ==================== Отправка времени ====================
def send_time_once():
    ser = ser_holder["ser"]
    if ser is None:
        return
    t = datetime.datetime.now()
    line = f"@TIME:{t.hour:02d}:{t.minute:02d}:{t.second:02d}\n"
    with serial_lock:
        try:
            ser.write(line.encode("utf-8"))
            log(f"@TIME отправлено: {t.hour:02d}:{t.minute:02d}:{t.second:02d}")
        except Exception as e:
            log(f"Ошибка отправки @TIME: {e}", "error")


# ==================== Чтение строк от ESP32 ====================
def serial_reader():
    while not stop_event.is_set():
        if transfer_in_progress.is_set():
            time.sleep(0.02)
            continue

        ser = ser_holder["ser"]
        if ser is None:
            time.sleep(0.5)
            continue

        try:
            raw = ser.readline()
        except Exception as e:
            log(f"Ошибка чтения Serial: {e}", "error")
            try:
                ser.close()
            except Exception:
                pass
            ser_holder["ser"] = None
            time.sleep(0.5)
            continue

        if not raw:
            continue

        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            continue

        if not line:
            continue

        log(f"ESP32: {line}", "debug")

        if line.startswith("Ready"):
            log("ESP32 готова, синхронизирую время")
            send_time_once()
        elif line.startswith("@LS_THEMES"):
            handle_themes_list()
        elif line.startswith("@THEMEGET:"):
            tname = line[10:]
            transfer_in_progress.set()
            pause_telemetry.set()
            try:
                handle_theme_get(tname)
            finally:
                transfer_in_progress.clear()
                pause_telemetry.clear()
        elif line.startswith("@LS:"):
            path = line[4:]
            handle_ls(path)
        elif line == "@LS":
            handle_ls("")
        elif line.startswith("@LS"):
            # на случай старого формата
            handle_ls("")
        elif line.startswith("@GET:"):
            fname = line[5:]
            transfer_in_progress.set()
            pause_telemetry.set()
            try:
                handle_get(fname)
            finally:
                transfer_in_progress.clear()
                pause_telemetry.clear()
        elif line.startswith("@PAUSE"):
            log("Телеметрия приостановлена")
            pause_telemetry.set()
        elif line.startswith("@RESUME"):
            log("Телеметрия возобновлена")
            pause_telemetry.clear()
        elif line.startswith("@LISTOK:"):
            log(f"ESP32 приняла список GIF ({line})")
        elif line.startswith("@THEMELISTOK:"):
            log(f"ESP32 приняла список тем ({line})")


# ==================== Отправка телеметрии ====================
def telemetry_loop():
    while not stop_event.is_set():
        if pause_telemetry.is_set():
            time.sleep(0.2)
            continue

        if transfer_in_progress.is_set():
            time.sleep(0.2)
            continue

        ser = ser_holder["ser"]
        if ser is None:
            time.sleep(0.5)
            continue

        try:
            sensors = fetch_sensors()
            categories = discover_all_categories(sensors)
            payload = serialize_categories(categories)

            line = f"@DATA:{payload}\n"

            if not pause_telemetry.is_set() and not transfer_in_progress.is_set():
                with serial_lock:
                    try:
                        ser.write(line.encode("utf-8"))
                    except Exception as e:
                        log(f"Ошибка телеметрии: {e}", "error")
                        try:
                            ser.close()
                        except Exception:
                            pass
                        ser_holder["ser"] = None
                        continue

        except Exception as e:
            log(f"Ошибка телеметрии: {e}", "error")

        time.sleep(SEND_INTERVAL)


# ==================== WMI-наблюдатель ====================
def wmi_watcher():
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

            if event.EventType not in (2, 3):
                continue

            time.sleep(0.5)

            port = find_esp_port()
            has_esp = port is not None

            if has_esp and ser_holder["ser"] is None:
                log(f"ESP32 обнаружена на {port}, открываю порт")
                try:
                    s = serial.Serial(port, BAUD_RATE,
                                      timeout=PORT_TIMEOUT,
                                      write_timeout=1)
                    time.sleep(2)
                    ser_holder["ser"] = s
                except Exception as e:
                    log(f"Не удалось открыть {port}: {e}", "warning")

            elif not has_esp and ser_holder["ser"] is not None:
                log("ESP32 отключена, закрываю порт")
                try:
                    ser_holder["ser"].close()
                except Exception:
                    pass
                ser_holder["ser"] = None

    finally:
        pythoncom.CoUninitialize()
        log("WMI-наблюдатель завершён")


# ==================== USB watchdog ====================
def usb_watchdog():
    while not stop_event.is_set():
        time.sleep(2.0)
        if transfer_in_progress.is_set():
            continue
        ser = ser_holder["ser"]
        if ser is None:
            port = find_esp_port()
            if port is not None:
                log(f"[watchdog] ESP32 найдена на {port}, открываю порт")
                try:
                    s = serial.Serial(port, BAUD_RATE,
                                      timeout=PORT_TIMEOUT,
                                      write_timeout=1)
                    time.sleep(2)
                    ser_holder["ser"] = s
                except Exception as e:
                    log(f"[watchdog] Не удалось открыть {port}: {e}", "warning")
            continue

        try:
            _ = ser.in_waiting
        except Exception as e:
            log(f"[watchdog] Порт мёртв ({e}), сбрасываю")
            try:
                ser.close()
            except Exception:
                pass
            ser_holder["ser"] = None


# ==================== main ====================
def main():
    port = find_esp_port()
    if port is not None:
        log(f"ESP32 уже подключена на {port}")
        try:
            s = serial.Serial(port, BAUD_RATE,
                              timeout=PORT_TIMEOUT,
                              write_timeout=1)
            time.sleep(2)
            ser_holder["ser"] = s
        except Exception as e:
            log(f"Не удалось открыть {port}: {e}", "warning")

    threads = [
        threading.Thread(target=wmi_watcher, daemon=True, name="wmi"),
        threading.Thread(target=serial_reader, daemon=True, name="serial"),
        threading.Thread(target=telemetry_loop, daemon=True, name="telemetry"),
        threading.Thread(target=usb_watchdog, daemon=True, name="watchdog"),
    ]
    for t in threads:
        t.start()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log("Остановлено пользователем")
    finally:
        stop_event.set()
        s = ser_holder.get("ser")
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
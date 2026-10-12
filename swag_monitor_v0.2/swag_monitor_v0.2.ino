#include <AnimatedGIF.h>
#include <TFT_eSPI.h>
#include <SPI.h>

#include "swag_dance_3_cut.h"

AnimatedGIF gif;
TFT_eSPI tft = TFT_eSPI();

// ============================================================
// ПИН КНОПКИ
// ============================================================
#define BUTTON_PIN 0

// ============================================================
// ПАРАМЕТРЫ ВВОДА
// ============================================================
const unsigned long LONG_PRESS_MS = 700;
const unsigned long DOUBLE_GAP_MS = 350;
const unsigned long DEBOUNCE_MS   = 30;

// ============================================================
// ПАРАМЕТРЫ ОТРИСОВКИ (неблокирующие таймеры)
// ============================================================
const unsigned long MONITOR_DRAW_MS  = 500;
const unsigned long SETTINGS_DRAW_MS = 500;
const unsigned long BLANK_TICK_MS    = 200;

// ============================================================
// РЕЖИМЫ ЭКРАНОВ
// ============================================================
enum ScreenMode {
  MODE_GIF,
  MODE_GIF_SETTINGS,
  MODE_MONITOR,
  MODE_MONITOR_SETTINGS,
  MODE_BLANK
};

ScreenMode currentMode = MODE_GIF;

// ============================================================
// ОПИСАНИЕ ПАРАМЕТРОВ МОНИТОРИНГА
// ============================================================
#define MAX_PARAMS 9
#define MAX_VISIBLE 5

struct ParamDef {
  const char* key;    // ключ в строке от ПК
  const char* label;  // короткая подпись (<= 5 символов)
  const char* unit;   // "%%", "C", "MHz", "rpm", "W"
};

// Описания (константы)
const ParamDef paramDefs[MAX_PARAMS] = {
  {"CPU",     "CPU",   "%"  },
  {"RAM",     "RAM",   "%"  },
  {"TEMP",    "TEMP",  "C"  },
  {"GPU",     "GPU",   "%"  },
  {"GPUTEMP", "GPU T", "C"  },
  {"VRAM",    "VRAM",  "%"  },
  {"FAN",     "FAN",   "rpm"},
  {"CLK",     "CLK",   "MHz"},
  {"PWR",     "PWR",   "W"  },
};

// Состояния (изменяемые)
struct ParamState {
  float value;
  bool  enabled;
};

ParamState paramState[MAX_PARAMS] = {
  { -1, true  },  // CPU
  { -1, true  },  // RAM
  { -1, true  },  // TEMP
  { -1, false },  // GPU
  { -1, false },  // GPUTEMP
  { -1, false },  // VRAM
  { -1, false },  // FAN
  { -1, false },  // CLK
  { -1, false },  // PWR
};

int visibleIdx[MAX_VISIBLE];
int visibleCount = 0;

int settingsCursor = 0;

// ============================================================
// КНОПКА
// ============================================================
bool          btnStable       = HIGH;
bool          btnLastRead     = HIGH;
unsigned long btnLastChange   = 0;
unsigned long btnPressStart   = 0;
bool          btnWasLong      = false;
bool          waitSecondClick = false;
unsigned long lastClickTime   = 0;

enum ButtonEvent {
  EV_NONE,
  EV_CLICK,
  EV_DOUBLE,
  EV_LONG
};

ButtonEvent pollButton() {
  bool reading = digitalRead(BUTTON_PIN);
  unsigned long now = millis();

  if (reading != btnLastRead) {
    btnLastChange = now;
    btnLastRead = reading;
  }

  if ((now - btnLastChange) >= DEBOUNCE_MS && reading != btnStable) {
    btnStable = reading;
    if (btnStable == LOW) {
      btnPressStart = now;
      btnWasLong = false;
    } else {
      unsigned long dur = now - btnPressStart;
      if (btnWasLong) {
        // EV_LONG уже отдан, ничего не делаем
      } else if (dur >= LONG_PRESS_MS) {
        // на всякий случай — если не успели отдать по таймеру
        btnWasLong = true;
        waitSecondClick = false;
        return EV_LONG;
      } else {
        if (waitSecondClick) {
          waitSecondClick = false;
          return EV_DOUBLE;
        } else {
          waitSecondClick = true;
          lastClickTime = now;
        }
      }
    }
  }

  // Долгое нажатие фиксируем по времени удержания, не дожидаясь отпускания
  if (btnStable == LOW && !btnWasLong && (now - btnPressStart) >= LONG_PRESS_MS) {
    btnWasLong = true;
    waitSecondClick = false;
    return EV_LONG;
  }

  // Если ждём второй клик и пауза истекла — это одиночный
  if (waitSecondClick && (now - lastClickTime) > DOUBLE_GAP_MS) {
    waitSecondClick = false;
    return EV_CLICK;
  }

  return EV_NONE;
}

// ============================================================
// ПРИЁМ ДАННЫХ С ПК
// ============================================================
String serialBuffer = "";

void rebuildVisible() {
  visibleCount = 0;
  for (int i = 0; i < MAX_PARAMS && visibleCount < MAX_VISIBLE; i++) {
    if (paramState[i].enabled) visibleIdx[visibleCount++] = i;
  }
}

void parseSerialData(const String& data) {
  // Сброс значений на "нет данных"
  for (int i = 0; i < MAX_PARAMS; i++) paramState[i].value = -1;

  int pos = 0;
  while (pos < (int)data.length()) {
    int colon = data.indexOf(':', pos);
    if (colon < 0) break;
    int comma = data.indexOf(',', colon);
    if (comma < 0) comma = data.length();

    String key = data.substring(pos, colon);
    String val = data.substring(colon + 1, comma);
    key.trim();
    val.trim();

    for (int i = 0; i < MAX_PARAMS; i++) {
      if (key == paramDefs[i].key) {
        paramState[i].value = val.toFloat();
        break;
      }
    }
    pos = comma + 1;
  }
}

// ============================================================
// ОТРИСОВКА: MONITOR (минимализм, точечная перерисовка)
// ============================================================
// Кэш предыдущих значений, чтобы перерисовывать только изменившиеся блоки
struct MonitorCache {
  bool  drawn;         // блок уже был нарисован
  int   paramIdx;      // индекс параметра
  float value;         // предыдущее значение
  bool  enabled;       // был ли включён при отрисовке
};
MonitorCache monitorCache[MAX_VISIBLE];

int monitorTop = 6;
int monitorLineH = 0;

// Цвет по уровню загрузки (для %)
uint16_t barColor(float v) {
  if (v < 0)      return TFT_DARKGREY;
  if (v >= 80.0f) return TFT_RED;
  if (v >= 50.0f) return TFT_YELLOW;
  return TFT_CYAN;
}

// Рисует один блок параметра в позиции slot (0..visibleCount-1)
void drawMonitorBlock(int slot, bool forceRedraw) {
  if (slot < 0 || slot >= visibleCount) return;
  int i = visibleIdx[slot];
  const ParamDef& d = paramDefs[i];
  const ParamState& s = paramState[i];

  MonitorCache& c = monitorCache[slot];

  // Если ничего не изменилось — не трогаем экран
  if (!forceRedraw && c.drawn && c.paramIdx == i &&
      c.value == s.value && c.enabled == s.enabled) {
    return;
  }

  int y = monitorTop + slot * monitorLineH;
  int yText = y + 2;
  int yBar = y + 12;

  // --- Подпись ---
  tft.setTextSize(1);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);

  // Обрезаем до 5 символов
  char label[8];
  strncpy(label, d.label, 5);
  label[5] = 0;
  tft.setCursor(2, yText);
  tft.print(label);

  // --- Значение ---
  char valBuf[16];
  if (s.value < 0) {
    snprintf(valBuf, sizeof(valBuf), "--");
    tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  } else {
    snprintf(valBuf, sizeof(valBuf), "%.0f%s", s.value, d.unit);
    tft.setTextColor(TFT_CYAN, TFT_BLACK);
  }
  // Очищаем область значения перед печатью
  tft.fillRect(42, yText, 128 - 42, 8, TFT_BLACK);
  tft.setCursor(42, yText);
  tft.print(valBuf);

  // --- Полоска или линия ---
  int barX = 2;
  int barW = 124;
  int barH = 6;

  if (strcmp(d.unit, "%") == 0) {
    uint16_t col = barColor(s.value);
    tft.drawRect(barX, yBar, barW, barH, col);
    // Стираем внутренность перед заливкой
    tft.fillRect(barX + 1, yBar + 1, barW - 2, barH - 2, TFT_BLACK);
    if (s.value > 0) {
      float vv = s.value > 100 ? 100 : s.value;
      int fill = (int)((barW - 2) * vv / 100.0f);
      if (fill > 0) tft.fillRect(barX + 1, yBar + 1, fill, barH - 2, col);
    }
  } else {
    // Не-%: тонкая линия-разделитель
    tft.drawFastHLine(barX, yBar + 2, barW, TFT_DARKGREY);
  }

  c.drawn    = true;
  c.paramIdx = i;
  c.value    = s.value;
  c.enabled  = s.enabled;
}

void drawMonitorFull() {
  tft.fillScreen(TFT_BLACK);

  // Очистка кэша
  for (int k = 0; k < MAX_VISIBLE; k++) {
    monitorCache[k].drawn = false;
    monitorCache[k].paramIdx = -1;
    monitorCache[k].value = -2;
    monitorCache[k].enabled = false;
  }

  if (visibleCount == 0) {
    tft.setTextSize(1);
    tft.setTextColor(TFT_YELLOW, TFT_BLACK);
    tft.setCursor(8, 70);
    tft.print("No params enabled");
    tft.setCursor(8, 84);
    tft.print("Hold for setup");
    return;
  }

  monitorTop = 6;
  monitorLineH = 120 / visibleCount;
  if (monitorLineH > 28) monitorLineH = 28;

  for (int slot = 0; slot < visibleCount; slot++) {
    drawMonitorBlock(slot, true);
  }

  // Подсказка снизу
  tft.setTextSize(1);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.setCursor(2, 152);
  tft.print("Hold: setup");
}

void refreshMonitorValues() {
  for (int slot = 0; slot < visibleCount; slot++) {
    drawMonitorBlock(slot, false);
  }
}

// ============================================================
// ОТРИСОВКА: MONITOR SETTINGS
// ============================================================
void drawSettingsCheckbox(int x, int y, bool checked, uint16_t bg) {
  // Рамка
  tft.drawRect(x, y, 10, 10, TFT_WHITE);
  if (checked) {
    // Толстая галочка — две пары параллельных линий
    tft.drawLine(x + 2, y + 5, x + 4, y + 7, TFT_GREEN);
    tft.drawLine(x + 3, y + 5, x + 5, y + 7, TFT_GREEN);
    tft.drawLine(x + 4, y + 7, x + 8, y + 2, TFT_GREEN);
    tft.drawLine(x + 5, y + 7, x + 9, y + 2, TFT_GREEN);
  }
}

void drawSettingsRow(int i, int y, bool selected) {
  uint16_t bg = selected ? TFT_NAVY : TFT_BLACK;
  tft.fillRect(0, y, 128, 16, bg);

  // Чекбокс
  drawSettingsCheckbox(3, y + 3, paramState[i].enabled, bg);

  // Имя
  tft.setTextSize(1);
  tft.setTextColor(selected ? TFT_WHITE : TFT_LIGHTGREY, bg);
  tft.setCursor(18, y + 4);
  tft.print(paramDefs[i].label);

  // Значение справа
  tft.setTextColor(TFT_DARKGREY, bg);
  tft.setCursor(94, y + 4);
  if (paramState[i].value < 0) tft.print("--");
  else tft.printf("%.0f%s", paramState[i].value, paramDefs[i].unit);
}

void drawMonitorSettings() {
  // Заголовок
  tft.fillRect(0, 0, 128, 14, TFT_NAVY);
  tft.setTextSize(1);
  tft.setTextColor(TFT_WHITE, TFT_NAVY);
  tft.setCursor(4, 4);
  tft.print("MONITOR SETUP");

  // Список параметров
  int top = 16;
  int lineH = 16;
  for (int i = 0; i < MAX_PARAMS; i++) {
    int y = top + i * lineH;
    if (y + lineH > 148) break;
    drawSettingsRow(i, y, i == settingsCursor);
  }

  // Подсказка снизу
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.setTextSize(1);
  tft.setCursor(2, 152);
  tft.print("1:next 2:on hold:exit");
}

// Перерисовать только изменившуюся строку списка
void refreshSettingsCursor(int prevCursor) {
  int top = 16;
  int lineH = 16;

  if (prevCursor >= 0 && (top + prevCursor * lineH + lineH) <= 148) {
    drawSettingsRow(prevCursor, top + prevCursor * lineH, false);
  }
  if (settingsCursor >= 0 && (top + settingsCursor * lineH + lineH) <= 148) {
    drawSettingsRow(settingsCursor, top + settingsCursor * lineH, true);
  }
}

// ============================================================
// ОТРИСОВКА: GIF SETTINGS
// ============================================================
void drawGifSettings() {
  tft.fillScreen(TFT_BLACK);
  tft.fillRect(0, 0, 128, 14, TFT_MAROON);
  tft.setTextSize(1);
  tft.setTextColor(TFT_WHITE, TFT_MAROON);
  tft.setCursor(4, 4);
  tft.print("GIF SETTINGS");

  tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  tft.setCursor(4, 40);
  tft.print("(empty)");

  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.setCursor(4, 60);
  tft.print("Coming soon...");

  tft.setCursor(2, 152);
  tft.print("Hold: exit");
}

// ============================================================
// ОТРИСОВКА: BLANK
// ============================================================
void drawBlankScreen() {
  tft.fillScreen(TFT_BLACK);
}

// ============================================================
// GIF КОЛБЭК
// ============================================================
void GIFDraw(GIFDRAW *pDraw) {
  uint8_t *s;
  uint16_t *d, *usPalette, usTemp[320];
  int x, y, iWidth;

  iWidth = pDraw->iWidth;
  if (iWidth > tft.width()) iWidth = tft.width();

  usPalette = pDraw->pPalette;
  y = pDraw->iY + pDraw->y;

  s = pDraw->pPixels;
  if (pDraw->ucDisposalMethod == 2) {
    for (x = 0; x < iWidth; x++)
      if (s[x] == pDraw->ucTransparent) s[x] = pDraw->ucBackground;
    pDraw->ucHasTransparency = 0;
  }

  if (pDraw->ucHasTransparency) {
    uint8_t *pEnd = s + iWidth, c, ucTransparent = pDraw->ucTransparent;
    int iCount = 0;
    x = 0;
    while (x < iWidth) {
      c = ucTransparent - 1;
      d = usTemp;
      while (c != ucTransparent && s < pEnd) {
        c = *s++;
        if (c == ucTransparent) { s--; break; }
        else { *d++ = usPalette[c]; iCount++; }
      }
      if (iCount) {
        tft.setAddrWindow(pDraw->iX + x, y, iCount, 1);
        tft.pushPixels(usTemp, iCount);
        x += iCount; iCount = 0;
      }
      x++; s++;
    }
  } else {
    for (x = 0; x < iWidth; x++)
      usTemp[x] = usPalette[*s++];
    tft.setAddrWindow(pDraw->iX, y, iWidth, 1);
    tft.pushPixels(usTemp, iWidth);
  }
}

// ============================================================
// ПЕРЕХОДЫ
// ============================================================
void enterMode(ScreenMode m) {
  currentMode = m;
  switch (m) {
    case MODE_GIF:
      tft.fillScreen(TFT_BLACK);
      break;
    case MODE_GIF_SETTINGS:
      drawGifSettings();
      break;
    case MODE_MONITOR:
      drawMonitorFull();
      break;
    case MODE_MONITOR_SETTINGS:
      settingsCursor = 0;
      drawMonitorSettings();
      break;
    case MODE_BLANK:
      drawBlankScreen();
      break;
  }
}

void cycleMainMode() {
  switch (currentMode) {
    case MODE_GIF:      enterMode(MODE_MONITOR); break;
    case MODE_MONITOR:  enterMode(MODE_BLANK);   break;
    case MODE_BLANK:    enterMode(MODE_GIF);     break;
    default: break;
  }
}

// ============================================================
// ОБРАБОТКА СОБЫТИЙ
// ============================================================
void handleButtonEvent(ButtonEvent ev) {
  if (ev == EV_NONE) return;

  if (ev == EV_LONG) {
    if (currentMode == MODE_GIF) {
      enterMode(MODE_GIF_SETTINGS);
    } else if (currentMode == MODE_MONITOR) {
      enterMode(MODE_MONITOR_SETTINGS);
    } else if (currentMode == MODE_GIF_SETTINGS) {
      enterMode(MODE_GIF);
    } else if (currentMode == MODE_MONITOR_SETTINGS) {
      enterMode(MODE_MONITOR);
    }
    return;
  }

  if (ev == EV_CLICK) {
    switch (currentMode) {
      case MODE_GIF:
      case MODE_MONITOR:
      case MODE_BLANK:
        cycleMainMode();
        break;

      case MODE_MONITOR_SETTINGS: {
        int prev = settingsCursor;
        settingsCursor = (settingsCursor + 1) % MAX_PARAMS;
        refreshSettingsCursor(prev);
        break;
      }

      case MODE_GIF_SETTINGS:
        break;
    }
    return;
  }

  if (ev == EV_DOUBLE) {
    if (currentMode == MODE_MONITOR_SETTINGS) {
      bool cur = paramState[settingsCursor].enabled;
      if (!cur) {
        int enabledCount = 0;
        for (int i = 0; i < MAX_PARAMS; i++) if (paramState[i].enabled) enabledCount++;
        if (enabledCount >= MAX_VISIBLE) {
          // Подсказка о лимите
          tft.fillRect(0, 132, 128, 14, TFT_MAROON);
          tft.setTextSize(1);
          tft.setTextColor(TFT_WHITE, TFT_MAROON);
          tft.setCursor(4, 136);
          tft.print("Max 5 params!");
          delay(800);
          drawMonitorSettings();
          return;
        }
      }
      paramState[settingsCursor].enabled = !cur;
      rebuildVisible();
      drawSettingsRow(settingsCursor, 16 + settingsCursor * 16, true);
    }
    return;
  }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  #ifdef RGB_BUILTIN
    neopixelWrite(RGB_BUILTIN, 0, 0, 0);
  #endif

  tft.init();
  tft.setRotation(0);
  tft.fillScreen(TFT_RED);
  delay(100);
  tft.fillScreen(TFT_GREEN);
  delay(100);
  tft.fillScreen(TFT_BLUE);
  delay(100);
  tft.fillScreen(TFT_BLACK);

  rebuildVisible();

  gif.begin(BIG_ENDIAN_PIXELS);
  Serial.println("Ready");
}

// ============================================================
// LOOP (без delay, кроме случая лимита)
// ============================================================
unsigned long lastMonitorDraw  = 0;
unsigned long lastSettingsDraw = 0;
unsigned long lastBlankTick    = 0;

void loop() {
  // ---- Кнопка (всегда, в начале итерации) ----
  ButtonEvent ev = pollButton();
  if (ev != EV_NONE) handleButtonEvent(ev);

  // ---- Приём данных с ПК ----
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      parseSerialData(serialBuffer);
      serialBuffer = "";
    } else {
      serialBuffer += c;
      if (serialBuffer.length() > 200) serialBuffer = "";
    }
  }

  unsigned long now = millis();

  switch (currentMode) {
    case MODE_GIF: {
      if (gif.open((uint8_t *)swag_dance_3_cut, sizeof(swag_dance_3_cut), GIFDraw)) {
        tft.startWrite();
        while (gif.playFrame(true, NULL)) {
          yield();
          ButtonEvent ge = pollButton();
          if (ge != EV_NONE) {
            handleButtonEvent(ge);
            if (currentMode != MODE_GIF) break;
          }
        }
        tft.endWrite();
        gif.close();
      } else {
        // небольшая пауза, чтобы не долбить CPU
        if (now - lastBlankTick >= 200) {
          lastBlankTick = now;
        }
      }
      break;
    }

    case MODE_MONITOR:
      if (now - lastMonitorDraw >= MONITOR_DRAW_MS) {
        lastMonitorDraw = now;
        refreshMonitorValues();
      }
      break;

    case MODE_MONITOR_SETTINGS:
      if (now - lastSettingsDraw >= SETTINGS_DRAW_MS) {
        lastSettingsDraw = now;
        // Обновляем значения в строках, не сбрасывая курсор
        int top = 16, lineH = 16;
        for (int i = 0; i < MAX_PARAMS; i++) {
          int y = top + i * lineH;
          if (y + lineH > 148) break;
          drawSettingsRow(i, y, i == settingsCursor);
        }
      }
      break;

    case MODE_GIF_SETTINGS:
    case MODE_BLANK:
      // "дышим" без блокировки
      if (now - lastBlankTick >= BLANK_TICK_MS) {
        lastBlankTick = now;
      }
      break;
  }
}
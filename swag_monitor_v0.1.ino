#include <AnimatedGIF.h>
#include <TFT_eSPI.h>
#include <SPI.h>

#include "swag_dance_3_cut.h"

AnimatedGIF gif;
TFT_eSPI tft = TFT_eSPI();

// Пин кнопки
#define BUTTON_PIN 0

// Три режима
enum ScreenMode {
  MODE_GIF,
  MODE_MONITOR,
  MODE_BLANK
};

ScreenMode currentMode = MODE_GIF;

// Антидребезг кнопки
unsigned long lastDebounceTime = 0;
const unsigned long debounceDelay = 200;
bool lastButtonState = HIGH;
bool buttonState = HIGH;

// Данные мониторинга
float cpuUsage = 0.0;
float ramUsage = 0.0;
float cpuTemp = 0.0;

// Буфер приёма с порта
String serialBuffer = "";

// ========== Колбэк отрисовки GIF ==========
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

// ========== Отрисовка экрана мониторинга ==========
void drawMonitorScreen() {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(1);

  tft.setCursor(5, 5);
  tft.println("МОНИТОР ПК");

  // Загрузка CPU
  tft.setCursor(5, 30);
  tft.setTextColor(TFT_CYAN, TFT_BLACK);
  tft.printf("CPU: %.0f%%", cpuUsage);
  tft.drawRect(5, 45, 118, 10, TFT_CYAN);
  tft.fillRect(7, 47, (int)(cpuUsage * 1.14), 6, TFT_CYAN);

  // Загрузка RAM
  tft.setCursor(5, 70);
  tft.setTextColor(TFT_GREEN, TFT_BLACK);
  tft.printf("RAM: %.0f%%", ramUsage);
  tft.drawRect(5, 85, 118, 10, TFT_GREEN);
  tft.fillRect(7, 87, (int)(ramUsage * 1.14), 6, TFT_GREEN);

  // Температура CPU
  tft.setCursor(5, 110);
  tft.setTextColor(TFT_YELLOW, TFT_BLACK);
  tft.printf("TEMP: %.0f C", cpuTemp);

  tft.setCursor(5, 140);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.println("Нажмите кнопку");
}

// ========== Тёмный экран ==========
void drawBlankScreen() {
  tft.fillScreen(TFT_BLACK);
}

// ========== Разбор строки с порта ==========
void parseSerialData(String data) {
  // Формат: CPU:xx,RAM:xx,TEMP:xx
  data.trim();
  int cpuIdx = data.indexOf("CPU:");
  int ramIdx = data.indexOf("RAM:");
  int tempIdx = data.indexOf("TEMP:");

  if (cpuIdx >= 0) {
    cpuUsage = data.substring(cpuIdx + 4, data.indexOf(',', cpuIdx)).toFloat();
  }
  if (ramIdx >= 0) {
    ramUsage = data.substring(ramIdx + 4, data.indexOf(',', ramIdx)).toFloat();
  }
  if (tempIdx >= 0) {
    cpuTemp = data.substring(tempIdx + 5).toFloat();
  }
}

// ========== Смена режима ==========
void switchMode() {
  currentMode = (ScreenMode)((currentMode + 1) % 3);

  switch (currentMode) {
    case MODE_GIF:
      tft.fillScreen(TFT_BLACK);
      break;
    case MODE_MONITOR:
      drawMonitorScreen();
      break;
    case MODE_BLANK:
      drawBlankScreen();
      break;
  }
}

// ========== Setup ==========
void setup() {
  Serial.begin(115200);

  // Инициализация кнопки
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  // Гасим встроенный RGB-светодиод
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

  gif.begin(BIG_ENDIAN_PIXELS);
  Serial.println("Multi-mode GIF/Monitor ready");
}

// ========== Loop ==========
void loop() {
  // ===== Проверка кнопки =====
  bool reading = digitalRead(BUTTON_PIN);
  if (reading != lastButtonState) {
    lastDebounceTime = millis();
  }
  if ((millis() - lastDebounceTime) > debounceDelay) {
    if (reading != buttonState) {
      buttonState = reading;
      if (buttonState == LOW) {
        switchMode();
      }
    }
  }
  lastButtonState = reading;

  // ===== Приём данных с порта (всегда, чтобы при переходе в режим монитора данные были свежими) =====
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      parseSerialData(serialBuffer);
      serialBuffer = "";
    } else {
      serialBuffer += c;
      if (serialBuffer.length() > 100) serialBuffer = "";
    }
  }

  // ===== Работа по режиму =====
  switch (currentMode) {
    case MODE_GIF:
      if (gif.open((uint8_t *)swag_dance_3_cut, sizeof(swag_dance_3_cut), GIFDraw)) {
        tft.startWrite();
        while (gif.playFrame(true, NULL)) {
          yield();
          // Во время проигрывания тоже реагируем на кнопку
          bool r = digitalRead(BUTTON_PIN);
          if (r == LOW && r != lastButtonState) {
            delay(debounceDelay);
            if (digitalRead(BUTTON_PIN) == LOW) {
              switchMode();
            }
            lastButtonState = r;
            if (currentMode != MODE_GIF) break;
          }
        }
        tft.endWrite();
        gif.close();
      } else {
        delay(500);
      }
      break;

    case MODE_MONITOR:
      drawMonitorScreen();
      delay(500);  // Обновление раз в 500 мс
      break;

    case MODE_BLANK:
      delay(200);
      break;
  }
}
#ifndef LCD_CONFIG_H
#define LCD_CONFIG_H

#define EXAMPLE_LCD_H_RES              280
#define EXAMPLE_LCD_V_RES              456
#define LCD_BIT_PER_PIXEL              16

// Пины дисплея
#define EXAMPLE_PIN_NUM_LCD_CS          10
#define EXAMPLE_PIN_NUM_LCD_PCLK        11 
#define EXAMPLE_PIN_NUM_LCD_DATA0       4
#define EXAMPLE_PIN_NUM_LCD_DATA1       5
#define EXAMPLE_PIN_NUM_LCD_DATA2       7
#define EXAMPLE_PIN_NUM_LCD_DATA3       19
#define EXAMPLE_PIN_NUM_LCD_RST         21
#define EXAMPLE_PIN_NUM_BK_LIGHT        (-1)

// Параметры LVGL - СБАЛАНСИРОВАННЫЕ
#define EXAMPLE_LVGL_BUF_HEIGHT         30    // Умеренный буфер
#define EXAMPLE_LVGL_TICK_PERIOD_MS     2
#define EXAMPLE_LVGL_TASK_MAX_DELAY_MS  500
#define EXAMPLE_LVGL_TASK_MIN_DELAY_MS  1
#define EXAMPLE_LVGL_TASK_STACK_SIZE    (4096) // Восстанавливаем нормальный стек
#define EXAMPLE_LVGL_TASK_PRIORITY      2

// Точка
#define I2C_ADDR_FT3168                 0x38
#define EXAMPLE_PIN_NUM_TOUCH_SCL       GPIO_NUM_8
#define EXAMPLE_PIN_NUM_TOUCH_SDA       GPIO_NUM_18

#endif
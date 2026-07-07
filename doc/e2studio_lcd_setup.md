# e2studio FSP 配置指南 — RA8D1 EIT 项目 LCD 显示适配

## 概述

本文档指导在 e2studio 中为 RA8D1 EIT 项目配置 LCD 显示所需的 FSP 外设栈和引脚。

配置完成后，点击 "Generate Project Content" 重新生成 `ra_gen/` 文件，然后将生成的文件复制到 EIT 项目的对应目录。

## 一、Stacks 页配置

### 1.1 添加 r_glcdc — Graphics LCD Controller

| 属性 | 设置 |
|------|------|
| Name | `g_display0` |
| **Input → Graphics 1** | |
| Format | RGB565 |
| Horizontal size | 480 |
| Vertical size | 360 |
| **Input → Graphics 2** | Disabled (取消勾选) |
| **Layer → Graphics 1** | Enabled |
| **Layer → Graphics 2** | Disabled |
| **CLUT** | Disabled |
| **Color Correction** | |
| Brightness | Enabled (默认值: R=512, G=512, B=512) |
| Contrast | Enabled (默认值: R=128, G=128, B=128) |
| Gamma → R | Disabled |
| Gamma → G | Disabled |
| Gamma → B | Disabled |
| Dithering | Truncate, Pattern 11 |
| **Output** | |
| Format | 16-bit RGB565 |
| Endian | Little endian |
| Color Order | RGB |
| DE polarity | High active |
| HSYNC polarity | Low active |
| VSYNC polarity | Low active |
| **Horizontal Timing** | |
| Total cycles | 514 |
| Display cycles | 480 |
| Back porch | 20 |
| Sync width | 4 |
| **Vertical Timing** | |
| Total cycles | 382 |
| Display cycles | 360 |
| Back porch | 10 |
| Sync width | 4 |
| **Clock** | |
| Clock source | Internal |
| Clock divisor | 8 |
| Sync edge | Rising |
| **TCON** | |
| HSYNC | TCON Pin 1 |
| VSYNC | TCON Pin 0 |
| DE | TCON Pin 2 |
| Correction order | Brightness → Contrast → Gamma |
| **PHY Layer** | `g_mipi_dsi0` (关联 MIPI DSI 实例) |
| **Interrupts** | |
| Line detect | Enable, IPL = 12 |
| Underflow 1 | Enable, IPL = 0 (disabled) |
| Underflow 2 | Enable, IPL = 0 (disabled) |
| **Callback** | `DisplayVsyncCallback` |

### 1.2 添加 r_mipi_dsi — MIPI DSI Host Controller

| 属性 | 设置 |
|------|------|
| Name | `g_mipi_dsi0` |
| **MIPI PHY** | `g_mipi_phy0` (关联 PHY 实例) |
| Number of lanes | 2 |
| Video mode | Non-burst with sync pulses |
| Pixel format | 16-bit RGB565 (`MIPI_DSI_VIDEO_DATA_16RGB_PIXEL_STREAM`) |
| Continuous clock | Enabled |
| EOTP | Enabled |
| **Video Timing** | |
| Vertical active | 360 |
| Vertical sync | 4 |
| Vertical back porch | 6 (10 - 4) |
| Vertical front porch | 8 (382 - 360 - 10 - 4) |
| VSYNC polarity | Low active |
| Horizontal active | 480 |
| Horizontal sync | 4 |
| Horizontal back porch | 16 (20 - 4) |
| Horizontal front porch | 10 (514 - 480 - 20 - 4) |
| HSYNC polarity | Low active |
| **PHY PLL** | Divider=1, Multiplier=50 → 1 GHz |
| **Interrupts** | 全部启用, IPL = 12 |
| **Callback** | `mipi_dsi0_callback` |

### 1.3 添加 r_mipi_phy — MIPI D-PHY

| 属性 | 设置 |
|------|------|
| Name | `g_mipi_phy0` |
| 保持默认设置 | — |

### 1.4 添加 r_drw — D/AVE 2D GPU

| 属性 | 设置 |
|------|------|
| Name | `g_drw0` |
| Interrupt callback | `drw_callback` (可留空) |
| Interrupt priority | IPL = 2 |

### 1.5 添加 r_gpt — Timer for Backlight PWM

| 属性 | 设置 |
|------|------|
| Name | `g_timer6` (或 `g_backlight_pwm`) |
| Channel | 6 (对应 P10_11 的 GTIOC6A) |
| Mode | Periodic, PWM output |
| Period | 10000 ns (= 100 kHz) |
| Duty cycle | 70% (初始默认值) |
| GTIOCA output | Enabled, PWM mode |
| Pin | P10_11 (GTIOC6A) |

### 1.6（可选） 添加 r_sci_b_i2c — Touch I2C

| 属性 | 设置 |
|------|------|
| Name | `g_i2c_touch` |
| Channel | 3 (SCI3) |
| Mode | I2C (Simple I2C on SCI) |
| Speed | 400 kHz (Fast mode) |
| SCL pin | P5_11 |
| SDA pin | P5_12 |

## 二、Pins 页配置

### 2.1 GLCDC / LCD Graphics 引脚

以下所有引脚设置为 `IOPORT_PERIPHERAL_LCD_GRAPHICS`，驱动能力选择 **Medium**（除了 LCD_CLK 选择 **High**）：

| 引脚 | 信号 | 备注 |
|------|------|------|
| P02_07 | LCD_TCON3 | |
| P05_15 | LCD_TCON4 | |
| P07_11 | LCD_DATA00 | |
| P07_12 | LCD_DATA01 | |
| P07_13 | LCD_DATA02 | |
| P07_14 | LCD_DATA03 | |
| P07_15 | LCD_DATA04 | |
| P08_05 | LCD_TCON0 (VSYNC) | |
| P08_06 | LCD_CLK | **Drive: High** |
| P08_07 | LCD_TCON1 (HSYNC) | |
| P09_02 | LCD_DATA08 | |
| P09_03 | LCD_DATA09 | |
| P09_04 | LCD_DATA10 | |
| P09_10 | LCD_DATA11 | |
| P09_11 | LCD_DATA12 | |
| P09_12 | LCD_DATA13 | |
| P09_13 | LCD_DATA14 | |
| P09_14 | LCD_DATA15 | |
| P09_15 | LCD_DATA16 | |
| P11_05 | LCD_DATA17 | |
| P11_06 | LCD_DATA18 | |
| P11_07 | LCD_TCON2 (DE) | |

### 2.2 MIPI DSI 引脚

| 引脚 | 外设功能 |
|------|----------|
| P02_06 | MIPI DSI |

### 2.3 SDRAM BUS 引脚

所有设置为 BUS 外设功能：

| 引脚 | 外设功能 |
|------|----------|
| P01_12, P01_13, P01_14, P01_15 | BUS |
| P03_00 ~ P03_12 | BUS |
| P06_01 ~ P06_15 | BUS |
| P09_05, P09_06, P09_08, P09_09 | BUS |
| P10_00, P10_08, P10_09, P10_10 | BUS |

### 2.4 Backlight + Reset

| 引脚 | 功能 | 驱动能力 |
|------|------|----------|
| P10_11 | GPT6 GTIOCA (PWM) | — |
| P11_04 | GPIO Output, Initial High | Low |

### 2.5 触摸 I2C（可选）

| 引脚 | 功能 |
|------|------|
| P5_11 | SCI3 SCL |
| P5_12 | SCI3 SDA |

## 三、BSP 页配置

| 属性 | 设置 |
|------|------|
| **SDRAM** | Enable (`BSP_CFG_SDRAM_ENABLED = 1`) |
| Main Stack Size | 16384 (0x4000, 16KB) — 从 8KB 增加到 16KB |
| Heap Size | 0 (保持) |

## 四、Clock 页配置

| 属性 | 设置 |
|------|------|
| **LCDCLK** | |
| Source | PLL1P |
| Divisor | /2 或合适的值 (确保 GLCDC 输出合适的像素时钟) |

确保 SDCLK (SDRAM 时钟) = 120 MHz (从 PCLKB 分频)。

## 五、验证步骤

1. 完成上述配置后，在 e2studio 中点击 **"Generate Project Content"** (Ctrl+B)
2. 确认 `ra_gen/` 目录中生成的文件包含：
   - `common_data.c` 有 `g_display0`, `g_mipi_dsi0`, `g_mipi_phy0`, `fb_background` 声明
   - `hal_data.c` 有 `g_timer6` 声明
   - `pin_data.c` 包含所有 LCD/SDRAM/MIPI 引脚
   - `vector_data.c` 包含新的中断向量
3. 将生成的 `ra_gen/` 文件复制到 EIT 项目的 `ra_gen/`
4. 在 EIT 项目中构建：`cmake --build build/debug`
5. 如果没有编译错误，烧录测试：串口发送 `lcd` 命令观察屏幕颜色循环

## 六、配置确认清单

- [ ] GLCDC 添加，Layer1 启用，480x360 RGB565
- [ ] MIPI DSI 添加，关联 PHY，2-lane 1GHz
- [ ] MIPI PHY 添加
- [ ] D/AVE 2D 添加 (DRW)
- [ ] GPT6 添加为背光 PWM (P10_11)
- [ ] SDRAM 在 BSP 页启用
- [ ] 所有 LCD/SDRAM/MIPI 引脚在 Pins 页配置
- [ ] LCD_RST (P11_04) 配置为 GPIO Output High
- [ ] LCDCLK 在 Clock 页启用
- [ ] 生成后无红色错误标记
- [ ] 生成的文件复制到 EIT 项目

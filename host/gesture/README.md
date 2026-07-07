# EIT Gesture Recognition

基于 EIT (电阻抗断层成像) 手环的实时手势识别模块。

## 架构

```
采集 → 特征提取 → 分类 → 可视化
collect.py  →  features.py  →  model.py  →  recognize_live.py
                                                    ↓
                                        web-eit (浏览器控制台)
```

## 快速开始

### 1. 采集训练数据

```bash
cd host/
python -m gesture.collect --port /dev/ttyACM0 \
    --gestures rest,fist,open,flex,ext \
    --reps 5 --frames-per-rep 10 \
    --out-dir gestures/session_001
```

按提示依次做手势，每轮自动采集 10 帧。

### 2. 训练模型

```bash
python -m gesture.model --data gestures/session_001/features.csv \
    --out gestures/model.joblib
```

输出：混淆矩阵、per-class F1、特征重要性排序。

### 3. 实时识别（Python）

```bash
python -m gesture.recognize_live --port /dev/ttyACM0 \
    --model gestures/model.joblib
```

双面板显示：左为 EIT 重建图，右为手势概率条形图。

### 4. 浏览器控制台

```bash
python web-eit/serial_bridge.py --serial-port /dev/ttyACM0 \
    --solver mcu --mcu-fast bin \
    --gesture-model gestures/model.joblib
```

打开 http://127.0.0.1:8765/?bridge=1，手势结果自动显示在重建图下方。

## 手势定义

| 标签 | 手势 | 说明 |
|------|------|------|
| rest | 放松 | 手腕自然放松 |
| fist | 握拳 | 五指紧握 |
| open | 张开 | 五指伸展张开 |
| flex | 屈腕 | 掌心向内屈腕 |
| ext | 伸腕 | 手背向外伸腕 |

## 依赖

```bash
pip install scikit-learn joblib
# 已有: pyserial, numpy, matplotlib
```

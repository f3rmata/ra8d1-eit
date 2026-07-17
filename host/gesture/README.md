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
    --samples 256 --settle-ms 20 \
    --out-dir gestures/session_001
```

按提示依次做手势，每轮自动采集 10 帧。

### 2. 训练模型

```bash
python -m gesture.model --data gestures/session_001/features.csv \
    --out gestures/model.joblib
```

输出：混淆矩阵、per-class F1、特征重要性排序。

### 2.1 PyTorch/RUHMI 迁移模型

PyTorch 版本使用 repetition 分组交叉验证，自动删除零方差特征，并将鲁棒标准化参数单独保存：

```bash
python host/gesture/train_mlp_torch.py \
    --data host/gestures/session_256_20_a/features.csv \
    --data host/gestures/session_256_20_b/features.csv \
    --out-dir host/gestures/torch_mlp_model
```

输出目录包含：

- `gesture_mlp_state.pt`：PyTorch 权重和网络元数据
- `gesture_mlp.ts`：TorchScript 模型
- `gesture_mlp.onnx`：静态 `[1,45]` 输入、opset 12 的 ONNX 模型，可继续交给 RUHMI
- `preprocess.json`：特征顺序、保留索引、中心值、尺度和裁剪范围
- `evaluation.json`：分组 OOF 和独立测试结果

采集和训练默认要求 `samples=256`、`settle_ms=20`。训练脚本会检查每个目录的 `metadata.json` 并拒绝参数不匹配的数据；`--allow-acquisition-mismatch` 仅用于显式的跨条件实验，不能用于报告同条件模型精度。

### 3. 实时识别（Python）

```bash
python -m gesture.recognize_live --port /dev/ttyACM0
```

双面板显示：左为 EIT 重建图，右为手势概率条形图。

### 4. 浏览器控制台

```bash
python web-eit/serial_bridge.py --serial-port /dev/ttyACM0 \
    --solver mcu --mcu-fast bin
```

Web bridge 默认不加载 AI 模型。需要手势识别时显式添加 `--gesture-model host/gestures/torch_mlp_model_256_20`；旧的 `model.joblib` 也仍可显式指定。

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
# PyTorch/RUHMI training path
pip install torch onnx
# 已有: pyserial, numpy, matplotlib
```

import numpy as np
import tensorflow as tf
import csv
from sklearn.metrics import classification_report, confusion_matrix

interpreter = tf.lite.Interpreter(
    model_path="gestures/ruhmi_model/gesture_classifier.tflite"
)
interpreter.allocate_tensors()
inp = interpreter.get_input_details()[0]
out = interpreter.get_output_details()[0]

with open("gestures/session_20260707_220027/features.csv") as f:
    rows = list(csv.DictReader(f))
meta = {"frame_id", "gesture", "rep"}
fnames = [c for c in rows[0].keys() if c not in meta]
X = np.array([[float(r[f]) for f in fnames] for r in rows], dtype=np.float32)

labels = sorted(set(r["gesture"] for r in rows))
l2i = {l: i for i, l in enumerate(labels)}
y_true = np.array([l2i[r["gesture"]] for r in rows])

# 逐帧推理（模型输入固定 batch=1）
y_pred = []
for i in range(len(X)):
    interpreter.set_tensor(inp["index"], X[i:i+1])  # shape (1, 49)
    interpreter.invoke()
    logits = interpreter.get_tensor(out["index"])[0]
    y_pred.append(int(np.argmax(logits)))

y_pred = np.array(y_pred)
print(f"Accuracy: {np.mean(y_pred == y_true):.4f}")
print(classification_report(y_true, y_pred, target_names=labels))
print("Confusion Matrix:")
print(confusion_matrix(y_true, y_pred))

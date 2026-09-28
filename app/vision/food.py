"""食物识别与热量估算。

四部分：
  1. 55 种常见中餐热量库（纯 Python，无第三方依赖）
  2. 训练脚本（惰性导入 torch/torchvision，仅 train() 调用时需要）
  3. ONNX 推理（惰性导入 onnxruntime；模型文件可选，未部署时明确报错）
  4. 热量估算主接口 estimate_total_calorie() + GLM-4V 识别（预留 API 接口）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .. import config

logger = logging.getLogger(__name__)

DB_PATH = config.PATHS.data_dir / "food_calorie_db.json"
IMG_SIZE = 224
SCALE_FACTOR = 0.75


@dataclass
class FoodCalorieItem:
    food_name: str
    calorie_per_100g: int   # kcal / 100g
    avg_density: float      # 平均密度，用于面积→重量估算


FOOD_DB_RAW = [
    FoodCalorieItem("白米饭", 116, 0.85),
    FoodCalorieItem("馒头", 286, 0.72),
    FoodCalorieItem("面条", 130, 0.80),
    FoodCalorieItem("小米粥", 46, 0.92),
    FoodCalorieItem("蒸红薯", 90, 0.78),
    FoodCalorieItem("水煮鸡蛋", 143, 1.03),
    FoodCalorieItem("煎鸡蛋", 200, 0.95),
    FoodCalorieItem("红烧肉", 395, 0.98),
    FoodCalorieItem("清蒸鱼", 120, 1.02),
    FoodCalorieItem("糖醋排骨", 280, 0.90),
    FoodCalorieItem("清炒青菜", 35, 0.95),
    FoodCalorieItem("油麦菜", 20, 0.96),
    FoodCalorieItem("西兰花", 34, 0.93),
    FoodCalorieItem("土豆丝", 110, 0.88),
    FoodCalorieItem("麻婆豆腐", 150, 0.92),
    FoodCalorieItem("番茄炒蛋", 155, 0.90),
    FoodCalorieItem("宫保鸡丁", 180, 0.89),
    FoodCalorieItem("鱼香肉丝", 172, 0.91),
    FoodCalorieItem("炒牛肉", 125, 0.97),
    FoodCalorieItem("水煮虾", 93, 1.03),
    FoodCalorieItem("炸鸡块", 290, 0.85),
    FoodCalorieItem("饺子猪肉白菜", 230, 0.76),
    FoodCalorieItem("小笼包", 240, 0.74),
    FoodCalorieItem("葱油饼", 310, 0.70),
    FoodCalorieItem("包子韭菜鸡蛋", 200, 0.78),
    FoodCalorieItem("南瓜粥", 50, 0.93),
    FoodCalorieItem("豆腐脑咸", 60, 0.96),
    FoodCalorieItem("豆浆无糖", 40, 1.00),
    FoodCalorieItem("油条", 380, 0.65),
    FoodCalorieItem("春卷", 260, 0.72),
    FoodCalorieItem("凉拌黄瓜", 22, 0.97),
    FoodCalorieItem("凉拌木耳", 27, 0.94),
    FoodCalorieItem("冬瓜汤", 12, 0.98),
    FoodCalorieItem("排骨汤", 160, 0.95),
    FoodCalorieItem("羊肉汤", 190, 0.96),
    FoodCalorieItem("炒豆角", 45, 0.94),
    FoodCalorieItem("红烧茄子", 130, 0.90),
    FoodCalorieItem("炒西葫芦", 23, 0.96),
    FoodCalorieItem("酱牛肉", 125, 1.01),
    FoodCalorieItem("卤鸡腿", 180, 0.92),
    FoodCalorieItem("烤鸭肉", 240, 0.88),
    FoodCalorieItem("炒花菜", 36, 0.93),
    FoodCalorieItem("土豆炖牛肉", 145, 0.91),
    FoodCalorieItem("西红柿炖牛腩", 138, 0.92),
    FoodCalorieItem("干煸豆角", 152, 0.89),
    FoodCalorieItem("水煮肉片", 210, 0.90),
    FoodCalorieItem("毛血旺", 185, 0.93),
    FoodCalorieItem("炒西葫芦鸡蛋", 70, 0.94),
    FoodCalorieItem("蒸南瓜", 75, 0.82),
    FoodCalorieItem("玉米棒", 106, 0.79),
    FoodCalorieItem("山药清炒", 57, 0.88),
    FoodCalorieItem("藕片清炒", 70, 0.87),
    FoodCalorieItem("海带丝凉拌", 18, 0.97),
    FoodCalorieItem("炒金针菇", 32, 0.95),
    FoodCalorieItem("紫菜蛋花汤", 25, 0.99),
]


def init_food_db(db_path: Optional[Path] = None) -> list:
    db_path = db_path or DB_PATH
    data = [{"food_name": i.food_name, "calorie_per_100g": i.calorie_per_100g,
             "avg_density": i.avg_density} for i in FOOD_DB_RAW]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def query_food_calorie(food_name: str, db_path: Optional[Path] = None):
    db_path = db_path or DB_PATH
    if not db_path.exists():
        init_food_db(db_path)
    db = json.loads(db_path.read_text(encoding="utf-8"))
    for item in db:
        if item["food_name"] == food_name:
            return item
    return None


# ===================== 视觉识别结果 → 卡路里/能量/营养 =====================
KCAL_TO_KJ = 4.184          # 1 kcal = 4.184 kJ（"能量"栏用千焦，国内包装多按 kJ 标） 
# 模型报的菜名常带量词/修饰："一碗白米饭"、"一份红烧肉" —— 比对前先去掉
_PORTION_WORDS = ("一碗", "一份", "一盘", "一碟", "一块", "一杯", "一些", "半碗", "几个",
                  "两块", "三块", "少许", "适量")


def _normalize_food_name(name: str) -> str:
    raw = str(name or "").strip().replace(" ", "")
    for w in _PORTION_WORDS:
        if raw.startswith(w):
            raw = raw[len(w):]
    return raw


def match_food_calorie(name: str, db_path: Optional[Path] = None):
    """在 55 种中餐热量库里找这条食物（先精确、再互相包含）。

    视觉模型给出的名字很活（"米饭" / "一碗白米饭" / "红烧肉"），
    而库里是规整名（"白米饭"）—— 用包含关系兜一层，命中就用库里的
    kcal/100g 去算，比模型自己报的数字稳（模型的克数估计仍然采用）。
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    hit = query_food_calorie(raw, db_path)
    if hit:
        return hit
    normalized = _normalize_food_name(raw)
    if normalized and normalized != raw:
        hit = query_food_calorie(normalized, db_path)
        if hit:
            return hit
    db_path = db_path or DB_PATH
    if not db_path.exists():
        init_food_db(db_path)
    db = json.loads(db_path.read_text(encoding="utf-8"))
    for item in db:
        key = str(item["food_name"])
        if key and (key in normalized or normalized in key):
            return item
    return None


def enrich_food_items(items: object) -> list[dict]:
    """把视觉模型给的每条食物补成"卡路里 + 能量 + 三大营养素"。

    输入形如 `[{"name": "白米饭", "portion_g": 150, "kcal": 180, "carb_g": 40, ...}]`；
    名字能在热量库里对上时用库里的 kcal/100g × 克数重算（`source="db"`），
    对不上就用模型自己的估算（`source="model"`）。缺克数时按 100g 记。
    """
    out: list[dict] = []
    if not isinstance(items, (list, tuple)):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("food") or it.get("食物") or "").strip()
        if not name:
            continue
        try:
            portion = float(it.get("portion_g") or it.get("weight_g") or 0)
        except (TypeError, ValueError):
            portion = 0.0
        if portion <= 0:
            portion = 100.0

        def _num(*keys: str) -> Optional[float]:
            for k in keys:
                v = it.get(k)
                if v in (None, ""):
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        kcal = _num("kcal", "calorie_kcal", "calories")
        per100 = None
        db_item = match_food_calorie(name)
        if db_item:
            per100 = float(db_item["calorie_per_100g"])
            kcal = per100 * portion / 100.0
        if kcal is None:
            continue
        row = {
            "name": name,
            "portion_g": round(portion, 1),
            "kcal": int(round(kcal)),
            "energy_kj": int(round(kcal * KCAL_TO_KJ)),
            "source": "db" if per100 is not None else "model",
        }
        if per100 is not None:
            row["kcal_per_100g"] = int(per100)
        for key, out_key in (("carb_g", "carb_g"), ("protein_g", "protein_g"), ("fat_g", "fat_g")):
            v = _num(key)
            if v is not None:
                row[out_key] = round(v, 1)
        out.append(row)
    return out


def food_totals(items: list[dict]) -> dict:
    """合计：卡路里 / 能量 / 碳水 / 蛋白 / 脂肪。"""
    total = {"kcal": 0, "energy_kj": 0, "carb_g": 0.0, "protein_g": 0.0, "fat_g": 0.0}
    for it in items or []:
        total["kcal"] += int(it.get("kcal") or 0)
        total["energy_kj"] += int(it.get("energy_kj") or 0)
        for k in ("carb_g", "protein_g", "fat_g"):
            total[k] += float(it.get(k) or 0)
    total["carb_g"] = round(total["carb_g"], 1)
    total["protein_g"] = round(total["protein_g"], 1)
    total["fat_g"] = round(total["fat_g"], 1)
    return total


# ===================== 训练脚本（可选，需 torch/torchvision） =====================
TRAIN_DATA_ROOT = Path("./ChineseFoodNet")
MODEL_SAVE_PATH = config.PATHS.models_dir / "food_mobilenetv3_075.onnx"


def train(batch_size: int = 16, epochs: int = 25, data_root: Path = TRAIN_DATA_ROOT) -> None:
    """训练 MobileNetV3 食物分类模型并导出 ONNX（需 torch/torchvision）。"""
    import torch
    import torch.nn as nn
    import torchvision.models as models
    from torchvision import datasets, transforms
    from torchvision.models.mobilenetv3 import MobileNet_V3_Large_Weights

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    train_tf = transforms.Compose([
        transforms.Resize((IMG_SIZE, IMG_SIZE)), transforms.RandomHorizontalFlip(),
        transforms.ToTensor(), normalize])
    val_tf = transforms.Compose([
        transforms.Resize((IMG_SIZE, IMG_SIZE)), transforms.ToTensor(), normalize])

    train_ds = datasets.ImageFolder(data_root / "train", transform=train_tf)
    val_ds = datasets.ImageFolder(data_root / "val", transform=val_tf)
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = torch.utils.data.DataLoader(val_ds, batch_size=batch_size)
    num_classes = len(train_ds.classes)

    model = models.mobilenet_v3_large(
        weights=MobileNet_V3_Large_Weights.IMAGENET1K_V1, width_mult=SCALE_FACTOR)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, num_classes)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    loss_fn = nn.CrossEntropyLoss()
    opt = torch.optim.Adam(model.parameters(), lr=1e-4)

    for epoch in range(epochs):
        model.train()
        total_loss = 0.0
        for imgs, labels in train_loader:
            imgs, labels = imgs.to(device), labels.to(device)
            opt.zero_grad()
            loss = loss_fn(model(imgs), labels)
            loss.backward()
            opt.step()
            total_loss += loss.item()
        print(f"Epoch {epoch}, Train Loss: {total_loss / len(train_loader):.4f}")

    model.eval()
    dummy_input = torch.randn(1, 3, IMG_SIZE, IMG_SIZE).to(device)
    MODEL_SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(model, dummy_input, str(MODEL_SAVE_PATH),
                      input_names=["input"], output_names=["logits"], opset_version=13)
    (config.PATHS.models_dir / "food_classes.json").write_text(
        json.dumps(list(train_ds.classes), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Model saved to {MODEL_SAVE_PATH}, classes: {len(train_ds.classes)}")


# ===================== ONNX 推理（可选） =====================
class FoodRecognizer:
    """ONNX 食物识别推理器（需 models/food_mobilenetv3_075.onnx + food_classes.json）。"""

    def __init__(self, model_path: Union[str, Path, None] = None):
        import onnxruntime as ort
        model_path = Path(model_path) if model_path else MODEL_SAVE_PATH
        if not model_path.exists():
            raise FileNotFoundError(
                f"食物识别模型不存在: {model_path}（先运行 train() 或部署模型文件）")
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(model_path), opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        class_json = config.PATHS.models_dir / "food_classes.json"
        if not class_json.exists():
            raise FileNotFoundError("请导出训练时的 food_classes.json")
        self.classes = json.loads(class_json.read_text(encoding="utf-8"))

    def preprocess(self, img_path: Union[str, Path]):
        import cv2
        import numpy as np
        img = cv2.imread(str(img_path))
        if img is None:
            raise FileNotFoundError(f"无法读取图片: {img_path}")
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (IMG_SIZE, IMG_SIZE))
        img = img.astype(np.float32) / 255.0
        img = (img - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32)
        img = np.transpose(img, (2, 0, 1))
        return np.expand_dims(img, axis=0)

    def predict(self, img_path: Union[str, Path]) -> dict:
        import numpy as np
        logits = self.session.run([self.output_name],
                                  {self.input_name: self.preprocess(img_path)})[0][0]
        exp = np.exp(logits - np.max(logits))
        scores = exp / np.sum(exp)
        top_idx = int(np.argmax(scores))
        return {"food_name": self.classes[top_idx], "confidence": round(float(scores[top_idx]), 4),
                "all_scores": scores.tolist()}


_recognizer_instance: FoodRecognizer | None = None


def get_food_recognizer() -> FoodRecognizer:
    global _recognizer_instance
    if _recognizer_instance is None:
        _recognizer_instance = FoodRecognizer()
    return _recognizer_instance


# ===================== 热量估算主接口 =====================
def estimate_total_calorie(img_path: Union[str, Path], plate_total_pixel: float,
                           food_pixel: float, plate_avg_weight: float = 350.0) -> dict:
    """像素占比 → 食物重量 → 总热量。"""
    recog = get_food_recognizer().predict(img_path)
    food_name, conf = recog["food_name"], recog["confidence"]

    db_item = query_food_calorie(food_name)
    if not db_item:
        return {"code": 404, "msg": "未识别到该食物热量数据",
                "food_name": food_name, "confidence": conf}

    residual_ratio = food_pixel / plate_total_pixel if plate_total_pixel > 0 else 0
    food_weight_g = plate_avg_weight * residual_ratio / db_item["avg_density"]
    total_kcal = (food_weight_g / 100) * db_item["calorie_per_100g"]
    return {
        "code": 200,
        "food_name": food_name,
        "recognize_confidence": conf,
        "residual_ratio": round(residual_ratio, 3),
        "food_weight_g": round(food_weight_g, 1),
        "calorie_per_100g": db_item["calorie_per_100g"],
        "total_calorie": round(total_kcal, 1),
    }


# ===================== GLM-4V 食物识别（预留 API 接口） =====================
def identify_food_glm4v(img_path: Union[str, Path]) -> dict:
    """云端视觉识别接口：拍一张餐盘照片 → GLM-4V 识别食物 + 估算热量。

    优先于本地 ONNX 模型使用（无需训练）；未配置 AI 时返回明确提示。
    返回: {food_items: [{name, portion, calorie_kcal}], total_calorie, source}
    """
    from ..core.ai_client import get_vision_client

    client = get_vision_client()
    if client is None:
        return {"code": 503, "msg": "AI 视觉未配置：请在设置页配置 API Key，"
                                    "或部署本地 ONNX 模型后使用 /api/food/estimate"}
    prompt = (
        "你是营养分析助手。识别照片中的所有食物，估算份量并给出营养，只返回 JSON："
        '{"food_items":[{"name":"食物中文名","portion_g":估计份量克数,"kcal":这一份千卡,'
        '"protein_g":蛋白质克数,"carb_g":碳水克数,"fat_g":脂肪克数}],'
        '"total_calorie":整盘合计千卡,"summary":"一句话营养点评"}'
    )
    reply = client.chat(prompt, image_path=Path(img_path))
    try:
        import re
        m = re.search(r"\{.*\}", reply, re.S)
        data = json.loads(m.group(0) if m else reply)
        # 用内置热量库校一遍（命中就用库里的 kcal/100g × 克数），并补能量/合计
        items = enrich_food_items(data.get("food_items") or data.get("items"))
        if items:
            totals = food_totals(items)
            data["food_items"] = items
            data["total_calorie"] = totals["kcal"]
            data["energy_kj"] = totals["energy_kj"]
            data["macros"] = {k: totals[k] for k in ("carb_g", "protein_g", "fat_g")}
        return {"code": 200, "source": "glm-4v", **data}
    except Exception:
        return {"code": 500, "msg": f"识别结果解析失败: {reply[:200]}"}

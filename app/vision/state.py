"""
state.py —— 状态词层：把「8类情绪 × 效价 × 唤醒度」组合成更丰富的可读状态

为什么需要它：
  离散情绪识别模型最多只有 8 类标签（数据集天花板：FER2013=7、FER+/AffectNet=8），
  但加上连续维度（效价 valence / 唤醒度 arousal）之后，
  可以组合出 16 种更细腻的状态词，例如：
    欣喜 / 愉悦 / 舒畅 / 平静 / 专注 / 倦怠 / 紧张 /
    烦躁 / 愤怒 / 不安 / 恐惧 / 低落 / 悲痛 / 惊讶 / 疑惑 / 厌恶
  （"轻蔑"已按要求屏蔽：该类别对中性脸误判率高，引擎默认把它并入中性处理）

怎么用（配合 micro_expression.MicroExpressionEngine）：
    from perception.emotion.state import StateMapper
    mapper = StateMapper()            # 会话级：自动记住你的“中性基线”
    for info in face_list:            # 每帧每个人脸
        mapper.update(info)           # 先更新基线（只在中性帧上滑动）
        st = mapper.map(info)         # -> {'zh':'欣喜','en':'delighted','key':'delighted'}

基线说明：模型输出的效价/唤醒度绝对值因人而异（有人中性脸 V=0.2，有人 V=0.4），
所以用你前几秒的中性帧建立“个人基线”，状态词按相对基线的偏移来判定——
这也正好让轻微表情变化（相对偏移）更容易被呈现出来。
"""

from __future__ import annotations

from typing import Dict, Optional

# ---------------- 相对基线的判定阈值 ----------------
V_HI = 0.08    # 效价比基线高这么多 → 积极
V_LO = -0.08   # 效价比基线低这么多 → 消极
A_HI = 0.10    # 唤醒度比基线高这么多 → 高唤醒
V_STRONG = -0.10  # 效价比基线低 0.15 以上 → 低落；只低 0.08~0.15 → 倦怠
A_LO = -0.08   # 唤醒度明显低于基线（保留，供后续细分用）


def map_state(emotion_label: str, valence: float, arousal: float,
              base_v: Optional[float] = None,
              base_a: Optional[float] = None) -> Dict[str, str]:
    """
    把“8类情绪 × 效价 × 唤醒度”映射成可读状态词。

    参数:
      emotion_label: 小写英文情绪标签，如 'happiness'、'neutral'
      valence/arousal: 0~1（钳位后的显示值）
      base_v/base_a : 个人中性基线（None 时按绝对值判定）
    返回:
      {'zh': 中文状态词, 'en': 英文状态词, 'key': 稳定英文键}
    """
    emo = (emotion_label or "neutral").lower()
    v = float(valence)
    a = float(arousal)
    # 判定：有个人基线 → 用相对偏移（微表情敏感）；
    #       无基线（如单张照片）→ 用绝对值阈值（0.45/0.55，中性=0.5 的 0~1 空间）
    if base_v is None:
        pos = v > 0.55
        neg = v < 0.45
        excited = a > 0.55
        strong_neg = v < 0.38          # 绝对空间：明显走低（低落）
    else:
        bv = v - float(base_v)
        ba = a - float(base_a)
        pos = bv >= V_HI
        neg = bv <= V_LO
        excited = ba >= A_HI
        strong_neg = bv <= V_STRONG    # 相对空间：明显走低（低落）

    if emo == "happiness":
        return {"zh": "欣喜", "en": "delighted", "key": "delighted"} if excited \
            else {"zh": "愉悦", "en": "pleased", "key": "pleased"}
    if emo == "anger":
        return {"zh": "愤怒", "en": "angry", "key": "angry"} if excited \
            else {"zh": "烦躁", "en": "irritated", "key": "irritated"}
    if emo == "fear":
        return {"zh": "恐惧", "en": "fearful", "key": "fearful"} if excited \
            else {"zh": "不安", "en": "uneasy", "key": "uneasy"}
    if emo == "sadness":
        return {"zh": "悲痛", "en": "distressed", "key": "distressed"} if excited \
            else {"zh": "低落", "en": "down", "key": "down"}
    if emo == "surprise":
        return {"zh": "惊讶", "en": "surprised", "key": "surprised"} if excited \
            else {"zh": "疑惑", "en": "puzzled", "key": "puzzled"}
    if emo == "disgust":
        return {"zh": "厌恶", "en": "disgusted", "key": "disgusted"}
    # 轻蔑已按要求屏蔽：按中性处理（由效价/唤醒度决定状态词）
    # 中性：主要靠效价/唤醒度区分
    if pos:
        return {"zh": "舒畅", "en": "content", "key": "content"}
    if neg and excited:
        return {"zh": "紧张", "en": "tense", "key": "tense"}
    if neg and strong_neg:
        # 中性刻板脸 + 效价明显走低 → 低落（不依赖模型判成 sadness，
        # 因为普通摄像头下轻度悲伤大概率还是被判成 neutral）
        return {"zh": "低落", "en": "down", "key": "down"}
    if neg:
        return {"zh": "倦怠", "en": "tired", "key": "tired"}
    if excited:
        return {"zh": "专注", "en": "focused", "key": "focused"}
    return {"zh": "平静", "en": "calm", "key": "calm"}


class StateMapper:
    """
    会话级状态器：用“中性”帧滚动更新个人基线（效价/唤醒度），
    让状态词对每个人的脸自动校准（解决模型输出绝对值因人而异的问题）。
    """

    def __init__(self, alpha: float = 0.04):
        # alpha 越小基线漂得越慢：短暂的低落/疲倦（几秒）不会被基线立刻“吃掉”，
        # 从而能显示成 低落/倦怠；alpha 太大基线会追着情绪跑。
        self.alpha = alpha
        self.base_v: Optional[float] = None
        self.base_a: Optional[float] = None
        self._seen_neutral = 0

    def update(self, info: Dict) -> None:
        """每帧人脸结果丢进来；只有中性帧参与基线滑动。"""
        if info.get("emotion_label") != "neutral":
            return
        v, a = info.get("valence"), info.get("arousal")
        if v is None or a is None:
            return
        v, a = float(v), float(a)
        if self.base_v is None:
            self.base_v, self.base_a = v, a
        else:
            self.base_v += self.alpha * (v - self.base_v)
            self.base_a += self.alpha * (a - self.base_a)
        self._seen_neutral += 1

    def map(self, info: Dict) -> Dict[str, str]:
        return map_state(info.get("emotion_label", "neutral"),
                         info.get("valence", 0.5),
                         info.get("arousal", 0.5),
                         self.base_v, self.base_a)

    def describe(self) -> str:
        if self.base_v is None:
            return "(基线未建立：保持中性约 1 秒)"
        return f"基线 V={self.base_v:.2f} A={self.base_a:.2f} (看过 {self._seen_neutral} 帧中性)"
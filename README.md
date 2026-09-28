# 正念饭崽 · mindful_meal

面向老年慢病人群（高血压、糖尿病等）的 **AI 正念饮食干预机器人**。
摄像头捕捉咀嚼与情绪 → 语音陪伴引导正念进食 → 记录并生成个性化健康文档。

---

## 一、功能总览

| 模块 | 能力 |
|---|---|
| **摄像头感知** | 咀嚼次数/频率（面部肌肉特征法，医学标准 15–30 次/分为正常）、头部移动抑制误计、手口送食与进食速度、分心物、食物残留占比、运动检测 |
| **情绪识别** | HSEmotion 8 类情绪 + 效价/唤醒度 + 16 种状态词 + 微表情事件（本地 ONNX，无需联网）；FER+ 备用；GLM-4V 云端视觉兜底 |
| **机器人表情** | 设备屏表情页用 [agent-robot-avatar](https://github.com/CX-ArtLab/agent-robot-avatar) 组件（DOM/SVG 矢量脸，14 种状态、眨眼/视线/果冻物理），根据 AI 回复自动切换，也可手动点选。（原先那套 11 张卡通 PNG 表情与"叠加到视频帧右上角"的实现已随本地音色一起删除。） |
| **AI 交流** | 一餐由**真实信号**驱动：餐前（安顿/分辨饥与馋/今天吃什么）→ 餐中（一次一句：慢下来、觉察饱足）→ 餐后（肯定 + 一句感受 + 收束），结束时**统计一次**；流式回复（首句约 1–3 秒可听）；结合情绪/饥饿/饱腹/速度给出提醒，慢病场景提示遵医嘱。「说我要吃饭」或「摄像头连续看到在吃」都会开餐，不会按聊天轮数偷偷推进 |
| **语音** | **唤醒/识别本地离线 + 发声走云端**：唤醒 = sherpa-onnx KWS 中文关键词模型（说「你好饭崽」即唤醒，完全本地、不联网）；识别 = Paraformer 中文（完全本地，15.9 秒语音解码 0.26 秒）；**发声 = 阿里云百炼 Qwen3-TTS-Flash（云端服务，必须联网且必须配置 API Key）**，单句约 1~2 秒（还要再下载一次音频）；没配 Key 或接口不可用时降级成一段本地 880Hz 提示音。**说完自动发送**（浏览器端音量检测，静音 0.85 秒即断句）也可手动点 ⏹；**唤醒词在任何自主互动模式下都有效**，机器人说话时喊它可直接打断（应答固定是「我在」；生成等待期**不出声**，只出表情动画） |
| **饮食日志** | SQLite 记录对话/用餐/情绪/送食/咀嚼/体重等事件；4 周周报（用餐数、平均间隔、情绪性进食占比、语速、体重趋势、依从性评分 0–100） |
| **风险预警** | 连续 3 天语速 >140 字/分（焦虑倾向）或 <100（低落倾向）；90 天体重波动 >5%；进食窗口 >12h 连续 3 天；21 点后宵夜 |
| **复查计划** | 三阶段：强化期（0–3 月，每 14 天）→ 巩固期（4–6 月，每 30 天）→ 维持期（7–12 月，每 90 天） |
| **个性文档** | `/api/personal-doc` 生成 Markdown 专属报告（总览 + 风险 + 复查 + 个性化总结），在设备屏的「📊 健康报告 → 我的个性文档」里直接阅读 |
| **食物热量** | 内置 55 种中餐热量库（kcal/100g + 平均密度）；**视觉识别出的菜品会逐条给出份量 / 卡路里 / 能量(kJ) / 碳水·蛋白·脂肪 与整盘合计**，热量优先用热量库校准（`source=db`），库里没有才用模型估算（`source=model`）；另有像素占比→重量→热量的本地估算与 `/api/food/identify` 拍照识别接口 |
| **硬件** | 自动探测：树莓派真机（picamera2 / arecord / aplay / GPIO）↔ Mock 回退；GC9A01 圆屏状态显示；事件可选云端上传 |
| **设备屏**（2.8 寸横屏，也是默认前端） | **整屏即设备屏**：表情页用 [agent-robot-avatar](https://github.com/CX-ArtLab/agent-robot-avatar) 组件（SVG 矢量、14 种 Agent 状态、眨眼/视线/果冻物理）与语音流程同步；摄像头/健康数据页为服务端渲染彩色卡片页（MJPEG ~29fps 实况，可上下滑动） |

## 二、快速开始（Windows 本地）

> **必须用 Python 3.10 ~ 3.12**（推荐 3.12）：依赖里的 mediapipe 0.10.21
> 只发布了 3.9~3.12 的轮子，3.13 装不上。`run.bat` 会自动优先找 3.12/3.11/3.10，
> 并在检测到 3.13+ 时直接提示。

```bash
# 1) 依赖（首次）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 2) 自检（11 步：模块/数据库/状态机/配置存储/分析/视觉/ASR/唤醒/发声/API/设备屏）
.venv/Scripts/python tests/selfcheck.py

# 3) 启动服务
.venv/Scripts/python -m app.main serve --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

> Windows 上也可以直接双击 **`run.bat`**：它会自动建虚拟环境、装依赖、**轮询 `/api/health`
> 等服务真正就绪后**再打开浏览器（冷启动要加载 ASR/KWS/TTS 模型，固定延时不可靠），
> 并在启动前提示"端口是否已被上次的服务占用"。

**页面一直卡在「正在连接设备屏…」怎么办**（这个坑踩过，原因有两层）：

1. **服务没起来** → 浏览器却用缓存的页面渲染，于是只看到加载遮罩、看不到真正的连接错误。
   现在 `/` 与自研 JS/CSS 一律 `Cache-Control: no-store`，不会再出现"缓存旧页面"。
   同时 `index.html` 加了兜底：脚本 6 秒没跑起来就直接提示 **按 Ctrl+F5** 并说明去看启动窗口的报错。
2. **浏览器拿着旧的 `device_ui.js`** → 改了代码不生效、行为诡异。现在 JS/CSS 已禁缓存，
   并且 `init()` 一开始就会置 `window.__dshBooted = true`（兜底提示据此判断是否真的启动了）。

排查顺序：先看启动窗口有没有 `Uvicorn running on ...`；再用
`tools/check_cache_headers.py`（缓存头）、`tools/diag_stuck_loading.py`（页面状态与 JS 报错）
确认，二者都不需要看代码。

**不配置任何 API Key 也能把服务跑起来**：对话走本地规则回复，感知全部本地模型，
唤醒与识别（sherpa-onnx）完全离线可用。但**发声是云端 Qwen3-TTS-Flash，没有 Key 就没有语音** ——
此时每次播报只会放一段本地 880Hz 提示音；手改 `data_local/ai_config.json` 里的
`tts_api_key`（百炼 DashScope Key）后才有正常语音（详见下文"发声"一节）。

### 语音对话怎么用

1. 点输入框左边的 🎤（浏览器会询问麦克风权限，需允许）
2. **直接说话，说完自动发送** —— 不需要再点一次；也可以点 ⏹ 立即结束
3. 说话时状态栏会显示实时音量条「🎙 已听到你说话 ▮▮▮▯▯▯▯▯（停下后自动发送）」，
   确认麦克风确实在收音；停下约 0.9 秒后自动发送
4. 状态栏随后显示「识别中…」→ 识别文本（带耗时与引擎名）
5. 回复文本立刻显示，语音随即播放

**静音检测（VAD）说明**：录音开始会先用 400ms 标定环境底噪（取 25 分位并夹到 5~25），
再用「连续 3 帧超过底噪」确认开始说话、连续 **0.85 秒**低于底噪确认说完。
这个阈值不能小：中文自然语流里 600~800ms 的换气停顿很常见，阈值太小会把一句话
从中间切断（前半句先发出去、后半句又成了一轮），表现就是"前一句没说完后一句插进来"。
录音上限 8 秒是兜底（正常情况下不会触发）。若一直没自动发送，说明环境噪声较大，
可直接点 ⏹ 手动发送。

**噪声与插话防护**（应对嘈杂环境、"它乱接话"）：
| 机制 | 作用 |
|---|---|
| 唤醒端自适应噪声门 | 只上传"明显高于环境底噪"的片段，静音不发请求；底噪 = 最近约 8 秒**所有帧**电平的 20 分位 ×1.8，随环境自动升降（说话只占少数时间，低分位仍稳定落在环境噪声上；曾经只统计"安静帧"，结果环境噪声一高于初始底噪就被判成"一直有人在说话"，门再也关不上） |
| KWS 专用唤醒模型 | 只认配置的唤醒词（拼音级匹配），无关语音/噪声不会命中；不再需要"短词严格判定"那种补丁 |
| 每分钟唤醒上限 | 60 秒内最多唤醒 5 次，噪声环境下兜住"被反复刷醒" |
| 说话时长校验 | 真正"有人在说话"不足 450ms（咳嗽/器皿碰撞）判为噪声，不送识别 |
| 一轮互斥 + 可打断 | 两路对话流互斥（`_dlgBusy`），回复不会互相插入；用户喊唤醒词可**明确打断**（中断 SSE、清空播放队列），且用轮次编号保证被打断那轮的收尾不会污染新一轮 |
| 听到人声就置忙 | 唤醒监听一检测到人声就上报 `reason=voice`（`POST /api/device/listen`），AI 的主动开口让路；服务端在语音合成完、发布前会**再确认一次**用户还没开口，避免"合成那 1~2 秒里用户开始说话"被插嘴 |
| 连续模式不盲录 | 连续模式在一轮回复后**不立刻盲录 8 秒**，而是把唤醒监听挂回去（等你说完再看是否续听）；播报结束后留 350ms 余量再收音，避免把上一句尾音当成新的一句话 |

**响应速度**（本机实测）：从"用户说完"到"听见机器人出声"，
由 VAD 等待 + 识别 + 大模型首字 + 首句 TTS 组成。现在**首句 TTS 是一次云端往返**：
合成请求本身约 1~2 秒，服务端还要再 GET 一次音频文件（接口不直接回音频字节），
所以"说 → 听见第一声"比当年的本地合成明显变长，网络抖动或限流时更慢。
生成等待期间**已经没有任何填充语**（旧版那句缓存的即时回应已随填充语一起删除），
所以"说完"到"听见第一声"中间那段空档，只能靠表情动画和状态栏文案撑着。

| 环节 | 现在（云端 Qwen3-TTS-Flash） |
|---|---|
| 单句 TTS 合成（含下载音频） | **约 1~2 秒**（一次网络往返 + 一次音频下载；无网络则直接走提示音） |
| 大模型首字 | 1.4~3.4 秒 |
| 静音断句等待 | 0.85 秒 |
| 整轮说完 | 没有稳定数字：比本地合成时代（曾实测 5.5~6.4 秒）明显更长，且随网络波动 |

> 上表刻意不给"整轮"一个精确值 —— 云端 TTS 的耗时随网络变化，写死数字反而是不诚实的。
> 要复现请自己跑 `python tools/bench_dialogue_latency.py`（生成等待期的填充语已删除，
> 所以脚本里那个 `[即时回应 ack]` 分支再也不会被触发）。
> 唤醒应答「我在」仍然**预合成 + 缓存**（启动时预热的就是这一句），
> 走的是另一条链路 `GET /api/agent/wake/ack`，所以"喊醒它到它应一声"仍然是秒回。
> 连续两次合成失败会**熔断 120 秒**（`app/voice/qwen_tts_engine.py`），这期间 `available()`
> 直接返回 False，调用方立刻走提示音，不会每句话都白等一次网络超时。

做法：
1. **等待期不出声，改用动画**：生成期间的填充语（旧版随机念一句 `ACK_TEXTS`，如「嗯，我在听。」
   「让我想想。」）**已整体删除** —— `app/config.py` 里的 `ACK_AUDIO` / `ACK_TEXT` / `ACK_TEXTS`
   以及 `app/api/dialogue.py` 里播这段音频的代码都不在了，启动时的预热也随之只剩唤醒应答一条。
   现在"它在想"纯粹由**表情动画**表达：前端调用 `FaceStates.thinking()`，即
   `agent-robot-avatar` 组件的 `startWaiting()`（眼睛绕圈转的加载动画），全程没有人声。
2. **TTS 并行度 2 → 4**、**逗号处提前断句阈值 10 → 6 字**：让首句更早发出、
   后面的句子边播边合成，部分掩盖云端合成的等待。
3. **静音断句保持 0.85 秒** —— 曾试过收到 0.6 秒，结果中文换气停顿被切断成两轮，
   详见下文"噪声与插话防护"。

**"有文字但没声音"怎么查**（两类原因都踩过）：

1. **容器格式标错**：本地合成一律出 WAV（16k/22.05k/24k 都是 RIFF 头）、旧缓存里可能还有 MP3，
   后端必须按**实际字节**报 `format`
   （`voice/tts.py::detect_format`），前端据此选 MIME。曾有一处 ACK 事件硬编码 `format="mp3"`
   而内容是 WAV —— 前端用错 MIME，播放静默失败。
2. **浏览器自动播放策略**：页面没有用户交互时 Chromium 会拒绝 `play()`（`NotAllowedError`）。
   处理方式：
   * **唤醒期间不释放麦克风**：Chromium 对"正在采集麦克风的页面"会放行自动播放，
     所以应答「我在」是在唤醒监听仍持有麦克风时播出的，不需要用户点屏幕；
   * **兜底**：真被拦下时把这段音频放回队列（不丢内容），状态栏显示
     `🔇 点一下屏幕开启声音`，用户任意点击后自动补播；
   * **不再静默**：解码/播放失败会显示 `⚠ 音频播放失败(code N)` 并打 console 警告
     （这个 bug 当初就是被 `play().catch(done)` 静默吞掉、查了很久）。

排查音频是否真的在播，可用 `tools/diag_audio_playback_flow.py`（钩住 `HTMLMediaElement.play`
逐次报告播放/拒绝/出错）与 `tools/check_audio_unlock.py`（确定性模拟"被拦 → 提示 → 补播"）。

### 语音唤醒（说「你好饭崽」直接喊醒）

**不用点屏幕**：设备屏页常开监听麦克风，说「你好饭崽」即自动进入聆听，
说完自动发送；默认「单句」模式，也可在设置面板切成「连续」模式多轮对话。
开关与模式都在**第一页下滑设置面板**里（设备屏默认开启，可关）。

- **唤醒词**：默认 `你好饭崽` / `你好正念`（存在 `data_local/agent_settings.json`）。
  刻意**不含两字词**（如「饭崽」）：两字词谐音太多，嘈杂环境下容易被误唤醒；
  确实想用可以自行加进词表，判定会自动走更严格的短词规则（最终结果 + 更高置信度 + 基本是整句）
- **唤醒应答**：命中后先回应一声**「我在」**（`app/core/interaction_config.py` 的 `WAKE["ack_text"]`，
  启动时已预合成，缓存秒回，走 `GET /api/agent/wake/ack`），
  **播完再静默 0.26 秒才打开麦克风收音** —— 顺序不能反（否则应答会被自己的麦克风录进去），
  也不能立刻切麦克风（实测会把应答尾音掐掉，听起来像"我在"后面卡了半个字）。
  这一句**不受**"等待期填充语已删除"的影响：删掉的是生成等待期那几句，唤醒应答保持不变。
  预合成时还做了一次**质量挑选**（见下），避免把"赶着收尾"的残句缓存下来反复播
- **单句模式**：唤醒 → 应一声「我在」→ 听一句 → 回到待唤醒（再说话要重新喊唤醒词）
- **连续模式**：唤醒后**一直听**，你直接接着说就行（不用再喊唤醒词）；
  静默 `WAKE["continuous_idle_sec"]`（默认 **30 秒**）或说「再见/拜拜/不聊了/结束对话/不用了」才回到待唤醒；
  状态栏会显示**剩余秒数**（`🎙 连续模式：接着说就行（23s 后回待唤醒）`），不用猜还在不在连续里。
  **麦克风从"你说话结束"就一直开着**（生成 + 播报期间也是），所以：
  ① 你在这期间接着说话不会被吞掉；② 播报期间喊唤醒词能真的打断；
  ③ 静默计时从"最后一次听到人声"起算，而不是从播报结束 —— 这三条以前都不成立，
  也正是"连续模式体感像单句"的来源（`tools/check_wake_single_vs_continuous.py` 钉住行为差别）
  ④ **播报结束后再等 1 秒**（`WAKE["continuous_resume_sec"]`）才开始接下一句：
   留这一秒给扬声器尾音散掉，否则机器人自己的尾音会被当成用户开口。
- **语音播报关掉时**（下滑面板「语音播报」= 关）：没有声音就没有"对话"，
  所以「唤醒后：单句/连续」这两项会**灰掉不可选**，说明文字改成"语音播报关闭时不可选"。
- **命中反馈**：头像切到「说话/聆听」+ 底部状态栏「🔔 已唤醒 · 我在听」+ 应答语音
- **有人在说话时 AI 不插嘴**：唤醒监听听到人声的那一刻就上报"设备忙"
  （`POST /api/device/listen` 的 `reason=voice`），自主互动会一直让路到安静下来为止

判定在服务端（`app/voice/sherpa_kws.py`），浏览器只负责把 16k 单声道 PCM 每 450ms 推一片
到 `/api/agent/wake/feed`。麦克风**只由浏览器一个持有者使用**：待唤醒时占用一次
`getUserMedia`，命中后先释放再录音，说完自动恢复监听，不会两个流抢麦；
AI 播报与录音期间不判唤醒，避免听到自己的声音自问自答。

**唤醒与识别**用 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（k2-fsa），**完全本地离线**；
**发声**走云端服务，只有一个引擎（Qwen3-TTS-Flash，阿里云百炼），详见下一节。

### 发声：唯一引擎 Qwen3-TTS-Flash（阿里云百炼，云端）

> ⚠️ **发声不是离线能力**。唤醒与识别（sherpa-onnx）完全本地离线，
> 但**发声必须联网、且必须配置 API Key**；没有 Key 只会播一段本地 880Hz 提示音。

**为什么只剩一个引擎**：本项目原先同时维护 6 套本地 sherpa-onnx 音色（Kokoro ×3、Matcha
+ 声码器、`vits-zh-hf-fanchen-C`、`sherpa-onnx-vits-zh-ll`、Piper 华研）外加 edge-tts 云端晓晓，
以及 `/audio/speech`、`glm-4-voice` 两条兜底。这些音色的**许可普遍不干净**（Matcha 的训练数据
明确限非商用、VITS-LL 与 Piper 华研的模型包没有 LICENSE、edge-tts 走的是 Edge「大声朗读」的
消费级接口），每个音色还要额外维护一份模型、采样率与元数据。按需求把它们**全部删除**，
统一换成阿里云百炼的商用 TTS 服务。

| 环节 | 模型 / 服务 | 说明 |
|---|---|---|
| **唤醒**（KWS） | `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01` | 中文关键词唤醒专用，建模单元=拼音，int8 三件套合计约 4.8MB，流式低延迟，**本地离线** |
| **文字识别** | `sherpa-onnx-paraformer-zh-small`（int8） | 15.9 秒语音解码 0.26 秒，**本地离线** |
| **文字转语音** | `qwen3-tts-flash`（阿里云百炼 / DashScope） | **云端服务**，输出 WAV，本引擎只用接口配置的那一个音色 |

#### 两个"听感"上的坑（都已修，别再踩）

* **念不出来的字符必须先清掉**：接口对纯表情符号（`😋`、`☆`、`*` 之类的片段）
  返回 `HTTP 400 InvalidParameter`。AI 回复里常带 😊😋，切句后可能剩下一个纯表情片段，
  连续两次这种 400 会**触发熔断** —— 表现是"回复念到一半没声了，而且之后两分钟整机都不出声"。
  现在 `qwen_tts_engine.sanitize_for_tts()` 会先把 emoji/符号删掉（`😋` → 空 → 根本不打接口），
  并且 **400 这类"输入问题"不再计入熔断**（熔断只留给网络/限流这类真的服务不可用）。
* **唤醒应答要挑一条"收干净了"的**：`qwen3-tts-flash` 对两三个字的输入会赶着收尾，
  同一句「我在」实测时长在 320ms~960ms 之间乱跳，偶尔直接把尾音掐断
  （听感就是"我在……后面卡了半个字"）。唤醒应答每次唤醒都要播，所以
  `tts_cache.prewarm_ack()` 会最多合成 3 次、挑"时长在 380~1800ms 且句尾 60ms 基本静音"
  的那条缓存下来（都不达标就取最好的一条）；启动日志会打印
  `[tts] 唤醒应答「我在。」合成 OK（640ms，尾音 0.000，第 2 次）`。

#### 接口形态与配置

```text
POST {base}/services/aigc/multimodal-generation/generation
Authorization: Bearer $DASHSCOPE_API_KEY
{"model": "qwen3-tts-flash",
 "input": {"text": "...", "voice": "Cherry", "language_type": "Chinese"}}
```

默认端点是北京地域 `https://dashscope.aliyuncs.com/api/v1`（`app/config.py::QWEN_TTS_API_BASE`）。
**非流式响应里 `output.audio.data` 是空串**，音频只在 `output.audio.url`（有效期 24 小时），
所以实现上会再发一次 GET 把字节下载回来 —— 这也是单句耗时比"纯合成"更长（约 1~2 秒）的原因。
（流式模式 `X-DashScope-SSE: enable` 才是中间 chunk 给 base64 `data`；本项目按句合成、句子都短，
走的是非流式 + 下载。）

配置全部落在 `data_local/ai_config.json`，**没有配置界面**：这些值手写在文件里，
存盘即生效、**不需要重启**（`app/voice/qwen_tts_engine.py::_settings()` 每次合成都重读配置；
字段逐项说明见 `data_local/AI_CONFIG.md`）。设备屏那块下滑设置面板只保留「AI 语速」，
**不能**改音色、端点或 Key，也没有试听（旧的多引擎切换与「🔊 试听一句」已删除）：

| 配置项 | 含义 |
|---|---|
| `tts_api_url` | 合成端点，留空即用北京地域默认端点（换成新加坡地域要同步换 Key） |
| `tts_model` | 模型名，默认 `qwen3-tts-flash` |
| `tts_voice` | 音色名，默认 `Cherry`（其余可选音色见百炼「Qwen-TTS 音色列表」文档） |
| `tts_language` | 语种，默认 `Chinese`（官方文档说明指定具体语种比 `Auto` 合成质量更好） |
| `tts_format` | 输出容器，默认 `wav` |
| `tts_api_key` | 百炼（DashScope）的 Key，**明文**写在同一份 json 里（现在对话与发声**都是百炼**，可以填同一把 Key；视觉仍可选智谱 GLM-4V，见 `vision_api_key`） |

> 环境变量 `DASHSCOPE_API_KEY` 可作为配置缺失时的兜底。
> 改了端点或 Key，服务端会顺手清掉熔断状态，不用干等 120 秒。

**语速**：接口本身没有语速参数，所以设置面板里的「AI 语速」（-50~50）由本地做
**时间轴重采样**（变快=丢样本、变慢=插值，音高不变，`audio_time_stretch()`）。
语义沿用旧口径：`+15%` 就是快 15%。

**失败降级**：合成失败（没配 Key、断网、限流、返回音频为空）时返回 False，
上层写入一段 0.4 秒 880Hz 的**本地提示音**（`app/core/ai_client.py::write_beep_tone`），
保证"有事件就有反馈"，但那**不是人声**。"有字就有人声"只在接口可用时成立。

#### 树莓派注意

发声是纯 HTTP 调用，**树莓派上不占 CPU、也不需要下载任何 TTS 模型**，
所以不再有"Pi 4 上合成跟不上"的问题；代价是**必须联网**，离线时没有语音（只剩提示音）。
唤醒与识别仍然完全本地。

**为什么唤醒从 Vosk 换成 sherpa KWS**（旧实现踩过的坑）：`vosk-model-small-cn` 的词表里
**没有「崽」**，导致「你好饭崽」这个唤醒词根本用不了：

| 方式 | 「你好饭崽」的结果 | 结论 |
|---|---|---|
| Vosk 语法限制（更早的 WakeWordListener） | `你好 [unk]`，final 为空 | ❌ 永远无法命中 |
| Vosk 自由识别 + 拼音容错（上一版） | `你好 贩 灾` | ⚠️ 靠拼音兜住，噪声里易误命中，两字词（饭崽）几乎不可用 |
| **sherpa KWS（现在）** | `你好饭崽` ✅ | 专用模型，两字词也能用，无关语音零误触发 |

自定义唤醒词的做法：把中文转成**拼音 token 行**写进会话即可（`app/voice/sherpa_kws.py`，
用 `pypinyin` 自动转换并校验 token 是否在模型词表内）：

```
你好饭崽  →  n ǐ h ǎo f àn z ǎi @你好饭崽
你好正念  →  n ǐ h ǎo zh èng n iàn @你好正念
饭崽      →  f àn z ǎi @饭崽
```

模型缺失时唤醒不可用（`/api/agent/wake` 的 `engine`/`detector.error` 会说明原因），
启动日志也会提示去跑 `python tools/download_sherpa_models.py`；不再有旧的 Vosk 回退。

> 想离线验证唤醒词：`python -m app.main wake-test 音频.wav`（16k 单声道），
> 命中会打印对应的唤醒词。日常使用走上面的浏览器唤醒（设备屏页常开监听）。

**唤醒不灵时怎么查**：设备屏底部状态栏会实时显示唤醒状态与麦克风电平：

| 状态栏显示 | 含义 | 处理 |
|---|---|---|
| `🎙 待唤醒（说「你好饭崽」）· 🎤 有声音` | 监听正常，且服务端确实收到了你的声音 | 若仍不命中 → 语速/距离，或到设置里换唤醒词 |
| `🎙 待唤醒（说「你好饭崽」）· 🎤 很安静` | 收到了音频但电平很低 | 离麦克风近一点 / 检查系统输入音量 |
| 一直不出现 `🎤` 字样 | 音频没上传（噪声门挡住或麦克风没采到） | 检查麦克风权限；用 `MINDFUL_WAKE_DEBUG=1` 落盘排查 |
| `点一下屏幕以开启语音唤醒` | 浏览器要求先有用户交互才给麦克风 | 点一下屏幕（并在地址栏允许麦克风权限） |
| 什么都没有 / 一直「未开启」 | 唤醒被设置面板关掉了，或麦克风不可用 | 下拉设置面板把「语音唤醒」打开（对应 `data_local/agent_settings.json` 的 `wake_enabled`）—— **验证脚本遇到这种情况会直接提示，不会含糊地报失败** |
| 日志出现 `mode=mock/error` | 启动瞬间相机尚未就绪 | 属正常降级：先用模拟画面，**每 10 秒自动重试**真实摄像头；长期如此再查相机占用/权限 |

**点屏幕也能结束/打断对话**：机器人正在说话或生成时，点一下屏幕＝打断（结束这一轮并退出连续对话）；
空闲时点屏幕＝开始说话（若设置是连续模式，这一轮结束后会继续听）。打断后要再点一次或喊唤醒词，
才会重新进入连续对话 —— 避免被打断后它自己接着说个不停。

**绝不要用 PowerShell 改 UTF-8 文本文件**（本轮的血泪）：`Get-Content -Raw` 按系统 ANSI（cp936）
读文件、`Set-Content -Encoding UTF8` 再写回，中文全成乱码；更糟的是**汉字最后一个字节会和紧跟的
ASCII（`<`、空格、引号）被当成一个 GBK 双字节对一起丢掉** —— `</title>` 变成 `/title>`、
index.html 里 JS 字符串少了收尾引号，页面直接白屏。改文本请用编辑器或本项目的 Python 工具
（一律 UTF-8 读写）。`tests/test_device_ui_static.py` 现在守着这条：静态资源必须是无 BOM 的
合法 UTF-8、无 U+FFFD、闭合标签完整。

**后台终端中文乱码**：由 `run.bat` 统一设 `chcp 65001` + `PYTHONUTF8=1` 解决。
**不要**在代码里把 stdout 重设成 `cp936`：Windows 控制台底层缓冲要求 UTF-8 字节，
改成 GBK 后写出的字节会被按 UTF-8 解释，中文全花（这个坑在 `main.py` 与
`chewing_engine.py` 里各写过一遍，现已移除）。

状态栏数据来自 `/api/agent/wake` 的 `detector.last_level`（服务端收到的音频 RMS），
因此"麦克风没收到声音"和"收到了但没命中"能直接分开。控制台还会打印每次命中的词。

**唤醒词在任何自主互动模式下都能用**（包括机器人正在说话时）：播报/生成期间**不再屏蔽**唤醒，
喊唤醒词即视为**明确要打断** —— 前端会中断当前的 SSE 流、清空播放队列、立刻转入收音
（`abortDialogue()`，用轮次编号保证被打断那轮的收尾不会清掉新一轮的状态）。
只有"正在收音"（麦克风被占用）、"设置面板打开"和"停在健康报告页（第 3 页）"时才会忽略。

**实测注意**：唤醒效果取决于麦克风质量。若你把设备屏跑在**带扬声器的笔记本**上，
浏览器默认开启的回声消除会与系统音频处理叠加，近距离大音量外放时识别率会明显下降；
真机（树莓派 + 独立麦克风）与正常说话距离下不受此影响。

### 语音链路的自动化验证

改唤醒/语音相关代码后，建议按下面顺序跑一遍（都在 `tools/`）：

| 脚本 | 验证什么 |
|---|---|
| `check_wake_browser.py` | 正面：唤醒词 → 取到应答语音 → 自动收音 |
| `check_wake_negative.py` | 反面：无关普通话语音 / 宽带噪声 **不得误唤醒**（并打印服务端听到了什么） |
| `check_wake_acoustic.py` | 真实音频文件离线跑唤醒，不依赖浏览器 |
| `check_wake_mode_switch.py` | 单句 / 连续：点面板按钮 → 设置是否真的存到服务端 |
| `check_wake_single_vs_continuous.py` | 单句 / 连续：**行为**差别（单句说完回待唤醒；连续说完接着听）。假麦克风 + 假识别/对话，不受云端影响 |
| `check_report_page.py` | 设备屏三页切换：表情 / 摄像头（MJPEG）/ **健康报告（第 3 页本人，不是底部按钮）** |
| `bench_runtime_cost.py` | **运行期性能体检**：空闲 / 表情页 / 摄像头页 / 报告页各档 CPU，外加一轮真实对话的首句与首段语音延迟 |
| `build_package.py` | **打发行包**：只带生产代码 + 按需裁剪的模型 + 部署脚本（不含 tests/tools/日志/截图/你的 Key），可 `--zip` |
| `build_exe.py` | **打成单文件桌面程序**（PyInstaller onefile，`dist/exe/`：exe + 模型 + 配置）；`--with-config` 顺带带上本机 Key，`--onedir` 换成启动更快的文件夹版 |
| `desktop_app.py` | 桌面版入口（自动选端口 → 起服务 → 打开设备屏；`--kiosk` 用浏览器应用模式） |
| `check_voice_turn.py` | 全链路：唤醒 → 应答 → 收音 → 识别 → 对话（会临时把自主互动调成 off 再恢复） |
| `check_kws_module.py` | KWS 引擎本身：唤醒词命中 / 无关语音不命中 |
| `check_kws_model.py` | 直接用模型自带音频验证 KWS 与关键词行生成 |
| `bench_dialogue_latency.py` | 端到端延迟拆解：首字 / 首句语音 / 整轮（含云端 TTS 往返） |
| `diag_audio_output.py` | 合成音频本身是否正常（联网调用 Qwen3-TTS-Flash，打印耗时与音频参数） |
| `diag_audio_playback_flow.py` | 钩住 `HTMLMediaElement.play`：每次播放是成功/被自动播放策略拒绝/解码出错 |
| `check_audio_unlock.py` | 自动播放被拦 → 保住音频并提示 → 点击后补播 |
| `diag_wake_upload.py` | 页面侧插桩：上传分片大小、底噪值、页面状态 |
| `check_cache_headers.py` | 前端 HTML/JS/CSS 是否已禁缓存（防止拿旧脚本） |
| `check_frontend_cache_and_boot.py` | 前端能否正常启动（缓存头 + 启动标记） |
| `diag_stuck_loading.py` | 复现"卡在 正在连接设备屏…"：页面状态 + JS 报错 |
| `check_js.py` | 静态检查全部前端 JS：括号配平 + 关键函数存在 + 已删符号不残留 |
| `model_status.py` | 汇总当前 ASR / LLM / TTS 的实际配置与模型文件 |

> ⚠️ **写这类浏览器用例时最容易踩的坑**：只加 `--use-fake-ui-for-media-stream`
> 和 `--use-file-for-fake-audio-capture=xxx.wav` **是不够的** —— 必须同时加
> `--use-fake-device-for-media-stream`，否则 Chromium 仍然使用**真实麦克风**，
> 注入的音频根本没被采集，测试会"通过"得毫无意义（本项目真的踩过：
> 一份抗误唤醒报告因此是无效的，重新做才确认结论）。
> 判断依据：若服务端 `detector.last_heard` 始终为空、却有大量上传分片，
> 多半就是采到了真实环境噪声。

排查"传了却识别不出"可开调试钩子（默认关闭）：`MINDFUL_WAKE_DEBUG=1` 启动服务，
会把浏览器上传的原始 PCM 追加写到 `data_local/wake_debug.pcm`（16k 单声道 S16_LE，
套个 WAV 头就能直接播放/识别）。

实测耗时（本机，摄像头与感知线程全速运行）：

| 环节 | 耗时 |
|---|---|
| 语音识别（6.5 秒语音，本地 Paraformer） | **0.13 秒** |
| 语音识别（15.9 秒语音，本地 Paraformer） | **0.26 秒** |
| 唤醒应答「我在」（预合成 + 缓存，走 `GET /api/agent/wake/ack`） | **0.03~0.45 秒** |
| 大模型首字 | 1.4~1.7 秒 |
| 单句语音合成（云端 Qwen3-TTS-Flash，含下载音频） | **约 1~2 秒**（网络往返；抖动/限流时更慢） |

> 识别与唤醒都是本地模型，数字稳定；**合成是云端调用，耗时随网络变化**，所以这里只给量级。
> 整轮耗时 = 上面几项相加，会比本地合成时代明显变长。
> 生成等待期的填充语已删除，所以除了唤醒应答之外，**没有"说完立刻出声"的缓存兜底了**。

### 识别引擎说明

默认用 **sherpa-onnx + Paraformer 中文模型**（int8，82MB），**完全本地、不联网**：
比原来的 Vosk-small 快 60~100 倍、准确率明显更高（Vosk-small 会把"粥"听成"中"）。
旧的 Vosk 回退已从工程中移除：模型缺失时直接报错提示下载，不再静默降级。
模型文件在 `models/sherpa-onnx-paraformer-zh-small/`（`model.int8.onnx` + `tokens.txt`）。

切换引擎（`app/config.py`）：
```python
SHERPA_NUM_THREADS = 4     # 树莓派建议改 2；用于 ASR 与 KWS
YIELD_VISION_DURING_ASR = True   # 识别期间暂停感知线程，避免 CPU/GIL 争抢

# 语音唤醒与识别（sherpa-onnx，本地离线）
SHERPA_KWS_DIRNAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KWS_THRESHOLD = 0.25       # KWS 命中阈值：调大更严、更少误触发
KWS_SCORE = 1.0            # 关键词加成分：调大更容易命中

# 发声：Qwen3-TTS-Flash（阿里云百炼 / DashScope）
# 端点 / 模型 / 音色 / 语种 / API Key 都存在 data_local/ai_config.json，
# 手改那个文件、存盘即生效（没有配置界面）；下面只是**读不到配置时**的兜底默认值。
QWEN_TTS_API_BASE = "https://dashscope.aliyuncs.com/api/v1"   # 北京地域
QWEN_TTS_MODEL = "qwen3-tts-flash"
QWEN_TTS_VOICE = "Cherry"
QWEN_TTS_LANGUAGE = "Chinese"      # 指定语种比 Auto 合成质量更好
QWEN_TTS_TIMEOUT = 30.0            # 单句合成 + 下载音频的总超时（秒）
```

> 旧的 `DEFAULT_TTS_VOICE` / `EDGE_TTS_TIMEOUT` 以及所有 `SHERPA_TTS_*` 常量，
> 已随本地音色与 edge-tts 一起移除。熔断参数在 `app/voice/qwen_tts_engine.py`
> （`FAILS_BEFORE_DOWN = 2`、`DOWN_SECONDS = 120`）。

**模型下载**（一次即可，脚本支持 GitHub 代理，国内网络可用）：
```bash
python -m tools.download_models          # Paraformer 识别模型（82MB）
python tools/download_sherpa_models.py   # KWS 唤醒模型（约 31MB）
```

> **发声不需要本地模型**：`tools/download_sherpa_models.py` 现在**只下载 KWS 唤醒模型**，
> 不再下载任何 TTS 音色或声码器（`--vocoders-only` 这个选项也已删除）。

脚本会依次尝试 `gh-proxy.com / ghfast.top / ghproxy.net` 代理（GitHub Release 直连在国内常超时；
实测 gh-proxy.com ≈650KB/s，而 ghfast.top 只有 ≈24KB/s，差 27 倍，所以放在第一个），
解压到 `models/` 下：

| 目录 | 用途 |
|---|---|
| `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` | 语音唤醒（KWS），本地离线 |
| `models/sherpa-onnx-paraformer-zh-small/` | 语音转文字（ASR），本地离线 |

`models/` 下其余文件都是**感知模型**，与语音合成无关：
`emotion-ferplus-8.onnx`、`enet_b0_8_va_mtl.onnx`、`face_detection_yunet_2023mar.onnx`、
`face_landmarker.task`、`hand_landmarker.task`，以及运行中生成的
`chewing_model.json` 与 `eating_thresholds.json`。
（旧的 TTS 模型目录与 `models/emojis/` 卡通表情 PNG 已全部删除。）

**为什么识别原来会慢到 15 秒**：视觉管线（帧循环 + 感知 + 手口检测）会大量占用 CPU 与 GIL，
而识别又跑在事件循环上被饿死，实测同一段语音从 2.5 秒劣化到 11.5 秒。现在识别在工作线程里执行，
并在识别期间暂停感知线程，双管齐下。

**注意**：浏览器麦克风只在 `http://localhost` / `http://127.0.0.1` / `https://` 下可用。
如果用 `http://<树莓派IP>:8765` 从别的电脑访问，浏览器会禁用麦克风 —— 需要在树莓派本机访问，
或为服务配置 HTTPS。

**关于音频格式**：本项目不依赖 pydub（Python 3.13 已移除其依赖的 `audioop`）。
浏览器端已把录音转成 16k 单声道 WAV，后端用 numpy 自行重采样，**零外部依赖**。
如需识别 webm/opus/mp3 等压缩音频，装 ffmpeg 即可（`sudo apt install ffmpeg`）。

### 配置 AI（可选，用于真实对话与云端视觉）

**只有一处配置：`data_local/ai_config.json`，用手改，没有配置界面。** 全部字段说明见
**`data_local/AI_CONFIG.md`**（每个字段一行，附最小可用示例与常见问题）。保存即生效 ——
配置是**每次请求前重新读取**的（`app/core/ai_config.py::load()`），**不需要重启服务**。

| 分组 | 字段 |
|---|---|
| 对话大脑 | `api_url`、`model`、`api_key`、`system_prompt`（AI 人格设定）、`enable_thinking` |
| 视觉（可选另配） | `vision_model`、`vision_api_url`、`vision_api_key` —— 留空则沿用对话那组 |
| 发声（TTS） | `tts_api_url`、`tts_api_key`、`tts_model`、`tts_voice`、`tts_language`、`tts_format` |

最小可用示例：

```json
{
  "api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "model": "qwen3.7-flash",
  "api_key": "sk-你的百炼Key",
  "enable_thinking": false,
  "system_prompt": "你是「正念饭崽」……",
  "vision_model": "glm-4v-flash",
  "vision_api_url": "https://open.bigmodel.cn/api/paas/v4",
  "vision_api_key": "你的智谱Key",
  "tts_api_url": "https://dashscope.aliyuncs.com/api/v1",
  "tts_api_key": "sk-你的百炼Key",
  "tts_model": "qwen3-tts-flash",
  "tts_voice": "Cherry",
  "tts_language": "Chinese",
  "tts_format": "wav"
}
```

> ⚠️ **Key 是明文的**（`api_key` / `tts_api_key`），这是**有意的取舍**：文件要能直接用编辑器改，
> 加密的 Key 手改不了。保存时会对文件调 `os.chmod(0o600)`（尽力而为，POSIX 上才真正生效），
> 但**明文 Key 落到本机磁盘就是明文** —— 不要把它提交到公开仓库、不要连同整个 `data_local/`
> 一起分享或截图外发。
> 旧版把 Key 用 Fernet 加密后存进 `encrypted_api_key` / `encrypted_tts_api_key`
> （配 `data_local/ai_config.key`）：**这两个旧字段仍然会被读取**（明文优先，旧的加密字段兜底），
> 但保存时**不再写回**，所以迁移过的那份文件里只剩明文。
> 迁移前的文件备份在 `data_local/ai_config.json.bak`。

也可以用 CLI 写这份文件（`--api-key` 传明文，同样以明文落盘）：

```bash
.venv/Scripts/python -m app.main ai-config \
  --api-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model qwen3.7-flash \
  --api-key <你的百炼Key>
```

当前部署的取值（2026-09 起）：**对话 = 阿里云百炼 `qwen3.7-flash`，上下文 128k**
（`app/config.py::MEMORY_TOKEN_LIMIT = 128_000` 是"模型窗口"这个口径）；
**视觉 = 智谱 `glm-4v-flash`**（单独一组 Key，见 `vision_api_url` / `vision_api_key`）；
**发声 = 百炼 `qwen3-tts-flash`**。对话与发声共用同一把百炼 Key。

> **上下文怎么用（两个数别混）**：`MEMORY_TOKEN_LIMIT=128k` 是硬上限；
> 每次请求**实际发出去**的只有 `MEMORY_SEND_TOKENS=2k`（长期摘要 + 最近若干轮）。
> 原因是语音助手的首字延迟基本由输入长度决定：同一句话输入 8.6k tokens 时首字
> 2.5~4.4s，2k 时 0.7s。超过 8k（= 发送预算 × 4）就把旧对话压成摘要，
> 这样"发出去的窗口"永远落在还留着原文的范围内 —— 既不会越用越慢，
> 也不会出现"没发出去又没进摘要"的记忆空洞。想聊得更"记得住"，把
> `MEMORY_SEND_TOKENS` 调大即可（每 1k tokens 大约多 0.4~0.6s 首字）。

**改这些数值**（上下文长度、帧率、唤醒线程数）不用改代码，也不用重新打包 ——
两种外部覆盖，优先级 **环境变量 > `data_local/tuning.json` > `app/config.py` 默认值**：

| 想调什么 | 键（`data_local/tuning.json`） | 默认 | 说明 |
|---|---|---|---|
| **上下文：每次请求发多少** | `memory_send_tokens` | 2000 | 最常调的就是它：调大更"记得住"，但首字变慢（2k≈0.7s、8k≈2.5~4.4s） |
| 上下文：模型窗口上限 | `memory_token_limit` | 128000 | 硬上限，一般不用动 |
| 上下文：预留 / 保留轮数 | `memory_reserve_tokens` / `memory_keep_recent_turns` | 8000 / 8 | 压缩线 = `memory_send_tokens × 4` |
| 摄像头页帧率 | `screen_camera_fps` | 30 | 树莓派上卡就调 15 |
| 没人看时的兜底帧率 | `screen_idle_fps` | 2.5 | |
| 唤醒解码线程数 | `kws_num_threads` | 1 | **保持 1 最省 CPU**（2 反而更费） |

示例文件：`data_local/tuning.json.example`（另存为 `tuning.json` 再改，**重启服务生效**）。
发行版/exe 同样适用 —— 改 exe 旁边的 `data_local/tuning.json` 即可，不用重新打包；
改完在启动日志里会看到 `[config] 已应用 data_local/tuning.json 的覆盖：…`，
`GET /api/health` 的 `tuning` 字段也会列出被覆盖的值，方便确认生效没生效。

> **`enable_thinking: false` 不要关掉**：Qwen3 系模型默认"先思考再回答"，
> 实测同一句话首字 16.7 秒、总 18.9 秒；关掉后首字 0.9 秒、总 3.6 秒。
> 语音助手靠这个参数才可用（只对 qwen3 系模型生效，换回 GLM 时不会带这个参数）。

也可用环境变量：`ZHIPU_API_KEY`（优先级最高，仅在没读配置文件时生效）。

> 设备设置（亮度、音量、AI 语速、运行模式、自主互动级别、唤醒词）**不在这份文件里**，
> 它们在 `data_local/agent_settings.json`，仍然可以在设备屏下滑设置面板里改。

## 三、命令行

```bash
python -m app.main serve            # Web 服务（对话/摄像头/设备屏/健康报告）
python -m app.main self-test        # 硬件自检
python -m app.main init-db          # 初始化数据库
python -m app.main ai-config ...    # 写 AI 配置（Key 明文存进 data_local/ai_config.json）
python -m app.main ai-chat "你好"    # 单条对话
python -m app.main tts "文本" --play # 文本转语音并播放
python -m app.main transcribe a.wav # 本地识别（Paraformer）
python -m app.main weekly-review    # 周报 JSON
python -m app.main analytics        # 完整健康分析
python -m app.main wake-test a.wav  # 离线验证唤醒词是否命中
python -m app.main assistant        # 树莓派语音助手循环
```

## 四、树莓派部署

```bash
git clone <repo> /opt/mindful_meal && cd /opt/mindful_meal
chmod +x deploy/setup_pi.sh && sudo ./deploy/setup_pi.sh
```

脚本会：装系统依赖（含 alsa-utils/ffmpeg/picamera2/lgpio）→ 建 venv → 装 pip 依赖 →
下载 KWS/Paraformer 两个本地语音模型到 `/data/models` → 复制感知模型 → 注册 systemd 服务。
（**不再下载任何 TTS 模型**：发声走云端 Qwen3-TTS-Flash，需要联网并手改
`data_local/ai_config.json` 填好两个 API Key。）

```bash
sudo systemctl start mindful-meal   # 启动
sudo systemctl status mindful-meal  # 状态
journalctl -u mindful-meal -f       # 日志
```

**路径规则**：树莓派数据在 `/data`（媒体 `/data/media`，库 `/data/user_health.db`），模型优先 `/data/models`，
缺失时回退项目内 `models/`；Windows 本地全部落在项目 `data_local/`。

### 硬件接线（与旧版一致）

| 设备 | 说明 |
|---|---|
| 摄像头 | CSI 排线（picamera2）；USB 摄像头也可，自动回退 OpenCV |
| 麦克风/喇叭 | ReSpeaker 2-Mic HAT，设备名 `plughw:seeed2micvoicec,0` |
| 圆屏 | GC9A01 240×240 软件 SPI：MOSI=GPIO5, SCLK=GPIO6, CS=24, DC=25, RST=27 |
| 按键 | 唤醒=GPIO16，确认=GPIO22，跳过=GPIO23（避开 ReSpeaker 占用的 GPIO17） |

## 五、设备小屏（2.8 寸）与显示方案

### 前端结构（默认前端 = 设备屏仿真）

| 地址 | 说明 |
|---|---|
| `/` | **设备屏**：整屏就是这块 2.8 寸屏（240×320，等比放大），与硬件屏**像素级一致**。**左右滑动**（或方向键）切页；在表情页**轻触屏幕**即开始说话；底部提示条上的 **📊 健康报告** 打开报告覆盖层 |
| `/api/docs` | 交互式 API 文档 |

> **管理后台 `/admin` 已整体删除**（请求它现在就是 404）：原来那套后台页面
> （`static/admin.html`、`static/css/style.css`，以及 `static/js/` 下的 `api / dialogue / camera /
> report / settings / device / main` 七个脚本）与设备屏前端有大量重复代码，维护两份 UI 不划算。
> 现在前端只有一份：`index.html` + `css/device_ui.css` + `js/device_ui.js`
> （外加 `vendor/agent-robot-avatar/`）。后台里独有的三个功能（健康周报 / 完整健康分析 /
> 我的个性文档）**已搬进设备屏前端**，变成一个覆盖层，见下文「健康报告覆盖层」。

也就是说：**平时看 `/` 就等同于看设备屏幕**，不需要接硬件。所有操作都在屏内完成——
页面上没有侧栏，唯一的按钮是底部提示条上的 **📊 健康报告**（真机上也只有一个屏 + 一个 GPIO 按键）。

### 为什么小屏不用 HTML

| 方案 | 树莓派 | 单片机 | 说明 |
|---|---|---|---|
| 浏览器 / HTML | 可用（设备屏表情页就是） | **不可用** | 驱动小屏要桌面环境或 kiosk，多占约 300MB 内存、开机慢十几秒 |
| **服务端渲染 + 直写屏幕** | **推荐** | 不可用（Python 跑不了） | 无需浏览器，Lite 系统即可，开机几秒出画面 |
| 树莓派渲染 + MCU 当显示从机 | 可选 | 可用 | 树莓派算好帧，通过串口/SPI 发给 MCU 屏，将来上单片机走这条路 |

**结论**：设备的 2.8 寸屏走**服务端渲染**（详细页），表情页由浏览器直接渲染组件。
同一个渲染结果有三个出口，换硬件只改一行配置，界面代码不动：

```
SCREEN_OUTPUT = "auto"   # auto(有 framebuffer 就用) / fbdev / spi / none
SCREEN_FBDEV  = "/dev/fb1"        # fbtft 驱动的小屏（最省事，推荐）
SCREEN_SPI_CONTROLLER = "st7789"  # 或 ili9341（直接 SPI 时用）
SCREEN_W, SCREEN_H = 640, 480     # 高清横屏
SCREEN_FPS = 30                   # 树莓派可降到 15
SCREEN_BUTTONS = True             # GPIO 跳过键切页
```

### 三个页面（小屏放不下全部内容，改为切换）

| 页 | 内容 |
|---|---|
| ① **表情主页**（黑底极简，占主体） | 插值表情 + **完整换行**的最近两轮对话（不截断，超长可滚动）+ 关键指标 |
| ② 摄像头（彩色详细页，服务端渲染 MJPEG，**30fps**） | **640×360 实时画面** + 紧跟一条**两行食物信息条**（合计 kcal/kJ · 碳水蛋白脂肪 · 逐条菜品份量 kcal）+ 咀嚼/进食/人脸情绪 数据卡，**上下滑动**查看 |
| ③ **健康报告**（前端 DOM 整页） | 健康周报 / 完整健康分析 / 我的个性文档 三个标签，**可滚动**。**就是这一页本身**，不再挂在底部按钮上 |

（设置类内容不在这些页里，统一在设备屏的下滑设置面板调整；AI 配置则只有手改
`data_local/ai_config.json` 一条路，见上文「配置 AI」。）

- **左右滑动** → 切页（触屏/鼠标拖拽/方向键，树莓派按键 GPIO23，或 API）
- **上下拖动 / 滚轮 / ↑↓** → 滚动页面内容（对话超长、详细页均支持，带滚动条）
- **轻触表情页** → 说话

**屏内交互**（真机与仿真一致）：
- 表情页**轻触屏幕** → 开始录音；说完自动发送（静音 0.9 秒断句），也可以再点一下立即发送
- 表情页**向下滑动** → 打开整页设置面板：亮度、音量、AI 语速、运行模式、自主互动级别、**语音唤醒（开关 + 单句/连续）**、**语音播报（开/关）**、时间校准；
  **向上滑动**（或点「完成」）返回
- 表情页顶部一行显示日期时间（浏览器本地时间）；面板里用**月/日/时/分/秒**手动校准，
  偏移存在本机 localStorage，不经过后端接口
- **切到第 3 页** → 健康报告（见下；底部不再有报告按钮）
- 出错提示会直接显示在屏幕底部（`POST /api/device/notice`）

### 健康报告：设备屏第 3 页（原管理后台的三个功能）

`/admin` 里独有的三个视图已搬进设备屏前端，**作为左右滑动的第 3 页整页显示**
（早期版本是底部「📊 健康报告」按钮弹出的覆盖层，现在按需求改成第三页本人；
进这一页时会暂停唤醒监听，避免看报告时被误触发进对话）。

| 标签页 | 数据源 | 内容 |
|---|---|---|
| **健康周报** | `GET /api/report` | 近 N 天概览（对话轮次 / 正念用餐 / 情绪事件）+ 情绪分布条 + 生成时间 |
| **完整健康分析** | `GET /api/analytics` | 依从性评分、用餐次数、平均间隔、情绪性进食占比、平均语速、体重趋势 + 风险预警 + 个性化洞察 + 复查计划 |
| **我的个性文档** | `GET /api/personal-doc` | 服务端生成的 Markdown 专属报告，前端用极简 Markdown 渲染器（`rpMarkdown`，支持标题/列表/加粗）显示 |

- **怎么打开**：**左右滑动到第 3 页**（底部那排按钮已经去掉；不要再找 📊 按钮）
- **怎么回去**：左右滑动切页，或按 **Escape**
- **刷新**：「刷新」按钮会清掉本次缓存重新拉取；每个标签页**首次打开时缓存**，来回切换不重复请求
- **打开期间不监听唤醒词**（`wakeCanListen()` 会因报告面板打开而返回 false），避免看报告时被误唤醒进入对话
- 实现都在 `app/web/static/js/device_ui.js`（`openReport` / `closeReport` / `initReport` / `rpLoad` /
  `rpRenderWeek` / `rpRenderAnalysis` / `rpRenderDoc`），样式在 `app/web/static/css/device_ui.css`
的 `#reportLayer` / `.rpCard` 一组选择器里

### 食物信息是怎么来的：**图片快照识别**，不是逐帧视频理解

两条完全不同的链路，别混：

| | 连续视频流（本地、逐帧） | 食物信息（云端、抽帧快照） |
|---|---|---|
| 输入 | 采集线程 ~30fps 的每一帧（`VISION_FRAME_FPS=32` 上限） | **每 10 秒取"当前最新那一帧"**（`AI_VISION_INTERVAL`） |
| 模型 | 本地 ONNX：手口模型（0.5s 周期）+ 肌肉法咀嚼（4Hz）+ 人脸情绪 | 云端视觉大模型（当前 `glm-4v-flash`，`vision/service._analyze_llm`） |
| 产出 | 咀嚼 次/分 与累计次数、送食 次/分、快慢等级、人脸情绪、运动/分心物 | 菜名、份量(g)、卡路里、能量(kJ)、碳水/蛋白/脂肪、整盘合计、食物残留（多/中/少/无）、场景一句话 |
| 延迟 | 亚秒级 | **10 秒轮询 + 模型 2~8 秒 → 约 10~18 秒更新一次** |
| 联网 | 不需要 | 需要（没配 Key 时这一块为空） |

完整链路（当前实现）：

```
采集线程(30fps) ──最新一帧──▶ 编码线程(960 宽 JPEG ~2ms)
                                   │
                    每 10s 取最新那一帧（**不是每一帧都发**）
                                   ▼
                    GLM-4V：一张图 + 一段要求返回 JSON 的提示词
                    → food.items[{name,portion_g,kcal,蛋白/碳水/脂肪}]
                                   │
                  ②内置 55 种中餐热量库校准（match_food_calorie）
                     命中 → 就按库里的 kcal/100g × 模型给的克数重算（source=db）
                     没命中 → 用模型自己的估算（source=model）
                                   ▼
        display_state.analysis['food'] ──0.5s 轮询──▶ 摄像头页"食物识别"信息条
```

几个要点：

* **不做食物识别的时候也有数据**：咀嚼/送食/情绪走本地视频流，跟云端识别无关；
* **模拟画面不调云端**（没摄像头时画面是程序画的彩色条纹，发过去只会白花额度、
  还会把"彩色条纹"当场景写进卡片）；
* 另有两条**独立**的"单张图片"入口：`POST /api/food/identify`（上传照片，同一套 JSON）
  与本地 `estimate_total_calorie()`（ONNX MobileNetV3 + 餐盘像素占比 → 重量 → 热量；
  需要 `models/food_mobilenetv3_075.onnx`，**当前没有这个模型文件，所以没在用**）；
* 想省额度/更贴合用餐：可以把云端那次调用限定在"这一餐进行中"再打
  （现在是无条件每 10 秒一次，一天约 8600 次），需要的话告诉我，我加个开关。

### 运行模式与自主互动

参数集中在 **`app/core/interaction_config.py`**（可在真机上编辑后重启生效）：

| 配置 | 取值 | 作用 |
|---|---|---|
| `MODES` | 进食检测 / 日常聊天 | 进食检测**开摄像头**并扮演「正念进食教练」；日常聊天**关摄像头**并扮演「日常陪伴伙伴」 |
| `AUTONOMY_LEVELS` | 激进 / 正常 / 关闭 | AI 不等用户开口就主动说话的频率与触发条件（关闭=用户说话才回应） |
| `TRIGGER_PROMPTS` | comfort/celebrate/chew_fast/eat_fast/checkin | 触发原因对应的提示词，决定"主动说什么" |
| `TRIGGER_COOLDOWN` | 各触发原因的冷却秒数 | 同一种互动的最短间隔（celebrate 900s / comfort 600s / 速度类 300s），防复读机 |
| `BRIGHTNESS` / `VOLUME` / `SPEECH_RATE` | 20~100 / 0~100 / -50~50 | 下滑面板的可调范围（语速换算成 TTS 的 speed） |
| `WAKE` / `WAKE_MODES` | 开/关 · 单句/连续 · 唤醒词表 | **语音唤醒**：常开监听开关、唤醒后的对话模式、唤醒词与冷却时间（设置面板可调） |

运行期选择保存在 `data_local/agent_settings.json`。自主互动的判定与发声在
`app/core/autonomy.py`：它以设备屏两页同源的实时指标（情绪/咀嚼/进食）为依据，
经 `/api/agent/proactive` 把文本、语音与表情推给设备页。

**模式的自动切换**（2026-09-17 起）：

| 时机 | 动作 |
|---|---|
| 日常聊天模式 + 用户说「我要吃饭/开饭了」 | 自动切到**进食检测**（摄像头开，开始看咀嚼/食物/情绪） |
| 进食检测模式 + 这一餐结束（说吃完了 / 超时收尾 / 10 分钟无动静） | 自动切回**日常聊天**（摄像头关，省电省算力） |

自动切换**不覆盖用户自己选的自主互动级别**（手动在面板切模式才会重置成该模式默认值）。

**语音播报开关**（下滑设置面板，`tts_enabled`）：关掉后对话只出文字、主动互动只出表情+文字、
唤醒也不再应答语音（`/api/agent/wake/ack` 直接返回 muted）——整条云端 TTS 链路都不打。

**什么时候可以主动开口**（这几条都是实测踩出来的）：

* **不在用餐就不谈进食**：没开餐（或已经餐后）时不会说"慢点嚼""几分饱""吃完了吗"，
  也不会拿进食数据去夸你 —— 只做日常关心与情绪回应。
  以前无脑按情绪/咀嚼触发，饭早吃完了 AI 还在"为你开心"，日志里连着 5 条 celebrate。
* **情绪要先稳定两拍、且刚刚变化**才算触发（`EMOTION_STABLE_SAMPLES=2`）：摄像头单帧
  误判很常见（库里大量 confidence 0.3~0.5 的 surprise），同一波情绪只回应一次。
* **有人在说话就让路**：前端录音中/播报中会置忙；唤醒监听一旦听到人声也会置忙
  （`reason=voice`），AI 等到安静下来才开口。合成语音要 1~2 秒，所以发布前会**再确认一次**
  "用户还是没开口吗"，避免"合成完才发现用户在说话"时插进去。
* **不会自说自话**：主动开口时会把这一餐的阶段、摄像头状态、时间段、**最近的对话内容**
  和自己上一句说过的话一起交给模型，并要求换一个角度。

### 对话内容：全部由 AI 生成

回复文案不再来自本地规则或状态机脚本：**`meal_manager` 只负责推进「餐前/餐中/餐后」阶段与落库**，
话术完全由 AI 依据角色预设（`interaction_config.MODES` 的 `role_prompt`）现场组织，
避免脚本与 AI 回复互相打架、听起来生硬。未配置真实 AI 时，接口会明确提示去配置，
而不是用本地规则话术冒充对话。

为了不让 AI 说成"模板腔"，还做了这些事：

- 角色预设（`app/config.py::DEFAULT_SYSTEM_PROMPT`）只写身份、风格与专业边界，不写固定话术；
  旧版把"场景应对原则"写死的提示词会在读取时自动升级（用户自己改过的不动）。
- 只有**真的在一餐里**（说「我要吃饭」起、或摄像头看到在吃起）才注入用餐阶段；
  普通聊天不会被硬拉回"你饿了吗"，也不会被按轮数偷偷带进"餐中/餐后"。
- 每次把上一轮的回答一起给模型，明确要求换一种说法，不复用同样的开头和句式。
- 回复文案完全由模型组织，不复用同一句口头禅。旧版为了"接住等待"会在生成期间随机播一句
  填充语（`ACK_TEXTS`），**现在这段语音已删除**：等待期只出表情动画，不说话

**文字是"边说边出"的**：服务端按句推 `reply` 事件，前端收到第一句就插入一条实时气泡
（`#liveReply`）逐句追加；`loadState()` 的 600ms 轮询只重画**历史记录区**（`#dlgHistory`），
不会把实时气泡冲掉 —— 以前两者共用一块 DOM，表现就是"文字闪一下又没了，等 TTS 合成完
才完整出现"。
  （见"响应速度"一节）。

### 进食模型的配置与重新训练

| 想改什么 | 改哪里 |
|---|---|
| 送食灵敏度（指尖离嘴多近算一次）、快慢阈值、头动抑制 | `models/eating_thresholds.json`（不存在会自动生成） |
| 手口检测采样频率 | `app/config.py` 的 `HAND_MOUTH_INTERVAL` |
| 咀嚼概率模型（下巴/颌角/咬肌/颞肌的逻辑回归权重） | 重新训练 → `models/chewing_model.json` |

重新训练（会打开摄像头；咀嚼时连续点按 `C`，按 `S` 预览训练结果，回车保存，`Q` 退出）：

```bash
.venv\Scripts\python.exe -m app.vision.chewing_engine --calibrate
```

保存位置就是 `models/chewing_model.json`（应用启动时加载，无需改代码）。
调参前先用探针看真实数据，再决定往哪边调：

```bash
.venv\Scripts\python.exe tools\model_probe.py --seconds 60   # 看 mouth_dist_ratio / 送食次数
```

### 健康分析口径

- **饮食依从性（0-100）**：情绪性进食占比 <30% 得 30 分；进食窗口超 12 小时的天数 <3 天得 25 分；
  宵夜天数 <3 得 25 分；28 天里有 ≥24 天有用餐记录得 20 分。四项相加，上限 100。
  用餐记录来自每餐结束时写入的 `food_residual` 事件。
- **平均语速**：每次说话（`/api/asr`）都会记一条 `wakeword` 事件（识别字数 ÷ 语音时长），
  取近 14 天平均——所以它会随着你说话而变化。
- **情绪分布**：摄像头识别的情绪（`emotion_detect` 事件，感知线程按变化落库）
  **加上**语速倾向（≥140 字/分记「焦虑倾向」，≤100 记「低落倾向」，其余记「平静」）合并统计。

- **情绪性进食占比（0~1）**：`app/core/analytics.py::calc_emotional_eat_ratio()`。
  口径是**按时间对齐的"饭"**：每顿饭的时间跨度是 `[started_ts, trigger_ts]`，
  只要这段时间里（前后各放宽 **60 秒**）出现过**至少一条负面情绪采样**，
  这顿饭就算一次"情绪性进食"；占比 = 有负面情绪的用餐次数 ÷ 总用餐次数，取值 0~1，
  没有任何用餐记录时返回 `0.0`。
  旧实现是 `负面情绪采样条数 ÷ 用餐次数` —— 分子是摄像头每隔约 20 秒落一条的情绪采样、
  分母是"一顿饭"，**单位不同、不能相除**，于是出现过 **712.5%**（57 条采样 ÷ 8 顿饭）
  这种荒谬结果；现在这个 bug 已修复（`tests/test_core.py` 里有对应的回归用例）。
  这个值在健康分析里被当成比例用：<30% 才拿满依从性的那 30 分。
- **咀嚼次数**：设备屏第一页指标行显示「咀嚼 24/分 · 正常」和「累计 137 次」，
  摄像头页的「咀嚼频率」卡片也带累计次数。
  每分钟频率来自手口模型（认不出来时回退肌肉法）；累计次数由肌肉法检测器提供，
  从服务启动开始累加。

  说明：`次/分` 是**最近 60 秒内嚼了几下**的滑动窗口值——停止咀嚼后它必然在 1 分钟内
  降到 0，这是定义如此，不是计数器被清零（累计次数只增不减）。前端每 600ms 刷新一次。

### 一餐的生命周期：餐前 / 餐中 / 餐后 / 统计

**唯一的真相来源是 `app/core/meal.py` 的 `meal_manager`**，阶段由真实信号推进，
跟"聊了几轮"毫无关系（旧状态机每收一条消息就前进一步，13 轮后自动判定"结束用餐"并落一次统计，
一句普通的「谢谢，这个菜真好吃」也能把一餐收掉 —— 那套已经不再参与判断）：

```
空闲(IDLE) ──说「我要吃饭/开饭」 或 摄像头连续看到在吃 ≥25s──▶ 餐中(MID)
餐前(PRE)  ──说「开吃/开始吃了」 或 摄像头连续看到在吃 ≥25s──▶ 餐中(MID)
餐中(MID)  ──说「吃完了/不吃了」 / 回答「对·嗯」 / 停止咀嚼满 2 分钟被问一句──▶ 餐后(POST)
餐后(POST) ──把"收尾这一句"说完────────────────────────▶ 统计一次，回到 IDLE
（兜底）── 连续 10 分钟既没在吃也没说话（人走了 / 问了他也不回应）────────▶ 收尾并统计
```

各阶段该怎么说话写在 `PHASE_GUIDE`（给模型的是**行为要求**，不是话术模板）：

| 阶段 | 该做什么 | 不该做什么 |
|---|---|---|
| 餐前 | 先安抚/觉察（一次深呼吸或身体扫描）→ 分辨「是胃在饿，还是心在饿」→ 问今天吃什么 | 催他开吃、谈咀嚼速度与几分饱 |
| 餐中 | 一次给**一句**具体可执行的事：放下筷子、多嚼几下再咽、尝一口说味道、感受七八分饱 | 连问几个问题、重复上一轮的提醒 |
| 餐后 | 先肯定吃完，再邀请说一句感受，最后温和收束 | 再提「继续吃」「慢慢嚼」，也不再核对咀嚼速度 |

**统计只做一次、只有一个时机**：餐后收尾那一次（`meal_manager.finish()` 幂等）。
无论被用户话术、超时、还是接口重复触发，都只落一条 `food_residual`（算用餐次数/间隔/依从性）
与一条 `meal_summary`（轮数/时长/结束原因）；记录里带上用餐时长、累计咀嚼次数/频率、
进食速度、情绪与最近的对话片段。**没有会话时不会有任何统计**，也不会谈进食。

几个关键触发与参数：

| 触发 | 参数（`app/core/meal.py` / `interaction_config.EATING_IDLE`） |
|---|---|
| 摄像头连续看到在吃 → **自动开一餐**（直接餐中，因为人已经在吃了） | `AUTO_EAT_SUSTAIN_SEC = 25s`（单帧看到不算，防"筷子晃一下多出一餐"） |
| 餐中停止咀嚼 → 主动问一句「是不是吃完了」 | `EATING_IDLE`：`chews_per_min=5`、`sustain_sec=120`（2 分钟）、`cooldown_sec=300`（问过就 5 分钟内不再问）；**暂停判定只在 `meal_manager.note_metrics` 里做这一处**；用户回「对/嗯/是的」也算吃完 |
| **没人吃 / 没回应**满 10 分钟 → 强制收尾并统计 | `AUTO_SILENT_SEC = 10min`（`last_activity` 只在"检测到进食"或"有一次对话轮次"时刷新 —— 人离开画面、发呆、不吭声都会到点） |
| 整餐挂过久（一直在吃一直在聊）→ 兜底收尾 | `AUTO_CLOSE_SEC = 45min` |
| 显式收尾（脚本/按钮） | `POST /api/dialogue/meal/finish`（同样幂等） |

自主互动设为「关闭」时不会主动问"吃完没"（各互动级别的 `eating_idle_check` 开关）。
回归用例：`tests/test_core.py` 的 `test_meal_flow_over_dialogue_api`（真实接口跑完整链路 +
14 轮不收尾 + 「谢谢」不误收尾 + 只统计一次）、`test_meal_auto_starts_from_vision_when_nobody_said_start`。

### 进食 / 情绪模型自测

怀疑「咀嚼次数、进食速度不生效」或情绪识别不稳时，跑模型探针（走生产同一套模型，
只关掉云端 GLM-4V，避免调试产生外部请求）：

```bash
.venv\Scripts\python.exe tools\model_probe.py                # 摄像头采样 20 秒
.venv\Scripts\python.exe tools\model_probe.py --seconds 60
.venv\Scripts\python.exe tools\model_probe.py --video demo.mp4
```

它会检查模型文件是否齐备，逐次打印「人脸数 / 情绪 / 咀嚼·分 / 送食·分 / 指尖到嘴距离 / 头动」，
并给出结论与排查建议，完整记录写入 `data_local/model_probe.json`。

送食（进食次数）判定已改为「指尖进入嘴部范围记一次 + 必须离开过 + 冷却时间」，
并把嘴边位置从"脸中心"修正为真实嘴心（上下内唇中点）、阈值放宽到 1.5 倍嘴宽。
若探针里 `mouth_dist_ratio` 始终大于 1.5，说明手离嘴太远或关键点没跟上，可据此继续调参。

### 表情：agent-robot-avatar 组件（与语音同步）

表情层直接采用开源项目 **[CX-ArtLab/agent-robot-avatar](https://github.com/CX-ArtLab/agent-robot-avatar)**
（v0.3.2，MIT；纯 SVG + 原生 JS 的 Web Component，运行时零依赖，
已内置到 `app/web/static/vendor/agent-robot-avatar/`，离线可用）。

组件自带：自动眨眼、视线与头部跟随、果冻拖拽形变、悬浮天线、`auto-sleep` 打盹、
14 种 Agent 状态（idle/bored/waiting/input/send/success/failure/warning/inspect/blocked/error/surprise/sleep/wake）。

与语音流程的映射（`app/web/static/js/device_ui.js` 的 `AvatarDirector` + `FaceStates`）：

| 阶段 | 小机器人动作 | 说明 |
|---|---|---|
| 待机 | `reset()` → 待机动画 | 安静 **25s → `bored`**、**60s → `sleep`**（组件自带 `auto-sleep` 也保留） |
| 命中唤醒词 | `surprise` + **天线闪** | 组件在"本来就没睡"时 `play('wake')` 是空操作（实测无事件），所以用 surprise 当"我在！" |
| 用户说话 | `input(true)` + 天线闪 | 说话结束/开始播报时 `input(false)` + 关闪 |
| 识别/生成中 | `startWaiting()`（眼睛绕圈） | **等待期没有语音，只有这个动画** |
| 播报回复 | `stopWaiting()` → `send` | 开始播报时点头 |
| **每句回复** | 按情绪**在动作池里轮换** | 见下表：同类情绪连着出现也会换动作（250ms 防抖） |
| 出错 | `error` | — |

情绪 → 动作池（同一情绪轮换使用，所以"表情跟着每句话变"）：

| 情绪 | 动作池 |
|---|---|
| 开心 / 温柔 / 厉害 / 安心 | `success → send → surprise` / `success → inspect` 轮换 |
| 难过 / 哭 / 委屈 | `failure → warning` |
| 生气 / 烦躁 / 厌烦 | `blocked → angry` |
| 焦虑 / 警惕 | `warning → inspect` |
| 惊讶 | `surprise → inspect` |
| 调皮 / 害羞 | `inspect → surprise` / `inspect → success` |
| 困 | `sleep → bored` |

> 实测（多句回复逐句触发）：开心#1→`success`、#2→`send`、#3→`surprise`；难过#1→`failure`、#2→`warning`。
> 组件支持 23 个动作（idle/bored/waiting/input/send/success/failure/warning/inspect/angry/
> blocked/error/surprise/sleep/wake…），现在这 14 个都用上了。

**动作是谁在驱动**（三条来源，优先级从高到低）：

1. **对话流程**：说话→`input(true)`、识别/生成→`waiting`、播报→`send`、出错→`error`、
   待机→`reset()`（+ 25s `bored` / 60s `sleep`）；
2. **回复文字**：每句按这一句的情绪在动作池里轮换（见上表）；
3. **相机识别到的情绪**（`loadState` 每 600ms 拿到 `emotion_key`）：
   只在**情绪真的变了**、设备空闲、且距上次反应 ≥4 秒时才动 ——
   实测 惊讶→`surprise`、难过→`failure`、高兴→`success`、害怕→`warning`、厌恶→`blocked`；
   同一个情绪重复报不会重复动（否则吃饭时表情会一直抽）。

**组件动作清单（23 个名字 / 16 个不同动作）**：公开 API 里 23 个名字中有 7 个是别名
（`wait`=`waiting`、`fail/failed`=`failure`、`verify/review`=`inspect`、
`policy-blocked`=`blocked`、`system-error/connection-error`=`error`），
所以实际不同动作是 16 个：idle / bored / waiting / input / send / success / failure /
warning / inspect / angry / blocked / error / surprise / sleep / **wake** / **reaction**。
现在用上 **14 个**，没用的两个是：

* `wake` —— 组件在"本来就没睡"时 `play('wake')` 是**空操作**（实测无任何 action 事件），
  而且它有活动/交互时会自己醒来，不需要显式调；唤醒应答改用 `surprise` 表达"我在！"；
* `reaction` —— 只出现在内部规范里，公开 `play()` 的类型不接受它。

**显示架构说明**：表情页由浏览器直接渲染组件（SVG 矢量，任意分辨率都清晰、动画全程 GPU 合成）；
摄像头/健康数据页仍是服务端渲染 MJPEG。因此真机上的 2.8 寸屏需要以 kiosk 模式跑一个浏览器
（Chromium）来点亮表情页。旧的服务端 PIL 像素表情引擎已删除，表情只由 avatar 组件负责。

组件代码 MIT 许可；机器人形象与视觉身份归 CX ArtLab 所有（README 原文声明，MIT 不转移角色版权）。

渲染帧率 `SCREEN_FPS = 30`（详细页）；浏览器镜像走 **MJPEG 流**
（`/api/device/stream`，JPEG 直推 + 帧序号去重，摄像头页实测 ~28.4fps）。
`/api/vision/stream` 同样为 JPEG 直推 + 帧序号去重（无 base64
往返），编码宽度/质量见 `VISION_JPEG_WIDTH` / `VISION_JPEG_QUALITY`。

**摄像头帧率是相机限制，不是软件限制**：采集线程按相机原生速率抓帧（`read()` 自带
一个帧周期的阻塞），编码线程事件驱动逐帧发布，因此流帧率 = 相机实际输出帧率。
实测服务端采集与"裸 `cap.read()`"完全一致（同一时刻对照：裸相机 20.0fps，
服务 20.0fps），说明管线没有额外损耗。注意**笔记本摄像头的帧率随环境光变化**：
暗光下自动曝光会延长到 1/20s 把 30fps 拉到 20fps，这是硬件行为。
`VISION_FRAME_FPS = 32` 只是上限保护值（相机比它快时才节流），不会限制相机。

**说完话不再有"立刻出声"兜底**：大模型生成 + 首句 TTS 合计要 3~5 秒，
但等待期**不再播任何填充语**（旧的「嗯，我在听。」「让我想想。」那一组已删除），
用户说完到听见第一声之间是安静的。这段空档由**动画**表达：前端 `FaceStates.thinking()`
调用组件的 `startWaiting()`（眼睛绕圈的加载效果），配合状态栏文案，而不是靠语音接住。

状态由语音流程自动驱动，无需手动干预：设备屏上的表情与语音阶段（`listening` / `thinking` /
`speaking` / 回复情感）由 `device_ui.js` 的 `FaceStates` 直接驱动。原管理后台里那个
"点选表情"的按钮已随后台一起删除。（`POST /api/vision/emotion` 仍在，但它只改服务端的
`robot_emotion` 快照字段，**不会**改设备屏上 avatar 组件的表情。）

### 在树莓派上接小屏

```bash
# 方式 A（推荐）：用内核 fbtft 驱动，屏当 /dev/fb1 用，无需写 SPI 代码
#   /boot/firmware/config.txt 加：
#   dtoverlay=fbtft,spi0-0,ili9341,width=240,height=320,rotate=90
#   重启后 ls /dev/fb* 应能看到 fb1；控制台不要占屏：sudo raspi-config → Console Autologin
# 方式 B：直接 SPI（需要 spidev）
sudo apt install python3-spidev
#   配置 SCREEN_OUTPUT="spi"、SCREEN_SPI_CONTROLLER="st7789"/"ili9341"
```

没有硬件也能开发：Windows 上 `SCREEN_OUTPUT="none"`，打开网页「设备屏」页即可看到
与设备**像素级一致**的实时画面（服务端渲染同一份帧）。

## 六、架构

```
app/
├── main.py            CLI 入口
├── config.py          路径平台自适应 + 全部可调阈值（集中管理）
├── server.py          FastAPI 应用工厂 + 静态托管（只有 `/` 一个页面，`/admin` 已删除）
├── api/               vision（快照/MJPEG/表情）· dialogue（SSE 流式）
│                      audio（ASR/TTS）· report（周报/分析/个性文档）
│                      settings（AI 配置 HTTP 接口/食物识别，无 UI 调用）· agent（设置/唤醒/主动互动）· device（设备屏）
├── core/              ai_client（OpenAI 兼容流式对话 + 视觉 + TTS 失败时写提示音）
│                      ai_config（唯一的配置读写：明文 Key 优先，兼容旧的 encrypted_* 字段）
│                      state_machine（16 状态）· memory（128k 上下文 + 超限压缩摘要）· analytics · db · uploader
│                      interaction_config（模式/自主互动/亮度音量语速/唤醒词）· autonomy（主动互动）
├── vision/            hub（感知中枢）· emotion_engine/emotion_micro/emotion_model
│                      chewing_engine/chewing · hand_mouth · food · service（四线程）
│                      emoji_render（情绪→emoji 映射）· eating_config
├── voice/             asr / sherpa_asr（Paraformer 薄封装）· sherpa_kws（KWS 唤醒，本地）
│                      qwen_tts_engine（唯一发声引擎：Qwen3-TTS-Flash 云端 + 熔断 + 语速重采样）
│                      tts（统一入口，只剩这一条链路）· tts_cache（合成缓存，只预热唤醒应答）
│                      features（语速）· convert（转 16k WAV）
├── display/           设备小屏：state（表情/页面/对话状态）
│                      render（详细页排版）· outputs（fbdev/SPI/无硬件）· service（渲染线程）
├── hardware/          __init__（自动探测）· real · mock · display_gc9a01
└── web/static/        index.html + css/device_ui.css + js/device_ui.js（前端只有这一份）
                       + vendor/agent-robot-avatar/（表情组件）
```

**性能设计**：感知模型全进程单例（只加载一份）；视觉四线程互不阻塞（帧 24fps / 手口 1.2s /
感知 4fps / GLM-4V 10s）；ASR 走本地模型无网络往返；对话 SSE 分句推送 + TTS 独立有序队列
（AI 生成不被语音合成阻塞，音频按句序播放）。摄像头帧在服务内即原始 JPEG 字节，
MJPEG 流零 base64 往返、零 analysis 构建，轮询快照只含分析结果（~1KB）。

### 性能：两个"越用越卡"的真凶（2026-09-17 实测）

现象是"说话后半天不回" + "界面卡"。分开量之后是两个独立问题：

**① 回复慢 = 模型在"思考" + 服务没重启。** 同一句话、同一份上下文：

| 情况 | 首句文本 | 首段语音 |
|---|---|---|
| 旧进程（配置已换 qwen3.7，但代码是旧的 → 思考模式开着） | **29.5s** | **32.3s** |
| 新代码（`enable_thinking=false`） | **1.2~1.3s** | **2.6~3.4s** |

判据：`ai_config.json` 里 `enable_thinking` 必须是 `false`，**而且改完代码要重启服务** ——
配置是每次请求重读的，但"要不要带这个参数"是代码逻辑，不重启不生效。

**② 常开唤醒监听在烧 CPU。** 浏览器一打开设备页（唤醒监听就常开、每 450ms 传一片
16k PCM），进程 CPU 会从 0.28 核涨到 **1.67 核**，切到摄像头页到 **2.34 核**；
关掉浏览器立刻回到 0.3 核。定位过程（`tools/bench_runtime_cost.py` 分档采样）：

| 档位 | 修前 | 修后 |
|---|---|---|
| 没有浏览器 | 0.28 核 | 0.28 核 |
| 表情页（唤醒在传音频） | **1.67 核** | **0.32 核** |
| 摄像头页（MJPEG） | **2.34 核** | **0.52 核** |

根因是 **KWS 解码线程数**：`sherpa-onnx` 的关键词识别按 2 线程跑，每片 450ms 音频
墙钟只要 48ms，但**线程池自旋要烧 ~0.8s CPU**（22 核机器上尤其明显）。改成
`config.KWS_NUM_THREADS = 1`（这个模型只有 3.3M 参数，单线程解码质量不变，
`tools/check_kws_module.py` 5/5 仍命中），整机 CPU 直接降一个数量级 —— 这在树莓派
（4 核）上就是"能不能用"的差别。

顺带修掉的三个浪费：

* **渲染按"谁在看"给帧**（`SCREEN_CAMERA_FPS=10` / `SCREEN_IDLE_FPS=2.5`）：
  摄像头页要贴一张 960px 照片并缩放，实测 17ms/帧，30fps 就是 0.5 核；
  没人看（无小屏、也没人拉流）时压到 2.5fps，纯数据页 0.4s 刷一次。
* **摄像头帧只在真要渲染时才解码**：以前每个循环（50 次/秒）都 `latest_jpeg()` +
  `Image.open()`，每次新建图像对象、攒到 GC 才回收。
* **JPEG 只在有人拉流时编码**（`_stream_ts` 以前记了却从没被用上）。

复测：`.venv/Scripts/python tools/bench_runtime_cost.py`（默认量 8765；也可
`--port 8766 --pid <PID>` 量别的实例）。期望值：空闲/表情页 ~0.3 核、摄像头页 ~0.5 核、
首句 <2s。

### 定期清理（缓存 / 日志 / 过期事件）

设备是长期无人值守跑的，三处会自己长胖；`app/core/janitor.py` 起了个后台保洁线程
（每 `CACHE_CLEAN_INTERVAL_H`=6 小时跑一轮，启动 1 分钟后先跑一次）：

| 清什么 | 规则 | 参数 |
|---|---|---|
| `media/` 下的临时音频与图片 | 超过 24 小时就删（TTS 中间文件、诊断产物） | `CACHE_MEDIA_KEEP_HOURS` |
| `*.log`（项目根 + `data_local/`） | 超过 2MB **只保留尾部 400 行**（现场不丢）；7 天没写过的直接删 | `CACHE_LOG_MAX_MB` / `CACHE_LOG_KEEP_DAYS` |
| `events` 表里的高频事件 | `emotion_detect` / `wakeword` 超过 90 天删除（顺带 VACUUM） | `CACHE_EVENT_KEEP_DAYS` |

> **对话、用餐、一餐总结、情绪标签这些用户数据永不自动删** —— 只有上面两类高频采样会被清。

手动跑一轮（会打印清了什么）：

```bash
.venv\Scripts\python.exe -m app.main cleanup            # 或 --no-vacuum 跳过整理数据库
```

## 七、主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/vision/snapshot` | 全部分析卡片（轻量 JSON，不含帧） |
| GET | `/api/vision/stream` | MJPEG 视频流 |
| GET | `/api/vision/frame` | 最新一帧 JPEG（非流式） |
| POST | `/api/vision/start` / `stop` | 启停摄像头 |
| GET/POST | `/api/vision/emotion` | 读取/设置机器人表情 |
| POST | `/api/dialogue/stream` | **SSE 流式对话**（reply/audio/done） |
| POST | `/api/dialogue` | 非流式对话（含 `[EMOTION:x]` 解析） |
| GET | `/api/dialogue/meal` | 当前这一餐的状态（阶段/时长/轮数/是谁开的餐） |
| POST | `/api/dialogue/meal/finish` | 显式收尾这一餐并统计（幂等；超时与脚本都用它） |
| POST | `/api/asr` | 上传音频 → 本地识别文本（Paraformer） |
| POST | `/api/tts` | 文本 → base64 音频 |
| GET | `/api/report` / `/api/analytics` / `/api/personal-doc` | 周报 / 完整分析 / 个性文档（设备屏「📊 健康报告」的三个标签页） |
| GET/POST | `/api/config` | AI 配置读写（明文 Key 优先；`GET` 返回的公开视图**不含密钥**，只有 `key_set` 布尔值）。**已无 UI 调用**，保留给测试与脚本 |
| POST | `/api/config/test` | 测试 AI 接口连通性（同样无 UI 调用） |
| GET | `/api/food/database` | 55 种中餐热量库 |
| POST | `/api/food/identify` | 拍照识别食物热量（GLM-4V） |
| GET | `/api/device/screen.png` | **设备小屏当前画面**（PNG，网页镜像用） |
| GET/POST | `/api/device/page` | 读取 / 切换设备屏页面（`next` 为循环切换） |
| POST | `/api/device/listen` | 通知设备屏进入/退出「倾听」 |
| GET/POST | `/api/agent/settings` | 第一页下滑面板设置（模式/自主互动/亮度/音量/语音唤醒） |
| GET | `/api/agent/proactive` | **SSE**：AI 主动互动事件（文本 + 语音 + 表情） |
| GET | `/api/agent/wake` | 语音唤醒状态（开关/模式/唤醒词/命中统计/最近听到的内容） |
| GET | `/api/agent/wake/ack` | 唤醒应答语音（「我在」，缓存秒回） |
| POST | `/api/agent/wake/feed` | **唤醒判定**：上传 16k 单声道 PCM，返回是否命中唤醒词 |
| POST | `/api/agent/wake/reset` | 丢弃唤醒识别会话（页面卸载/切换模式时） |

交互式文档：`http://<host>:8765/api/docs`

## 八、安全提示

- **Key 现在是明文**：`data_local/ai_config.json` 里的 `api_key`（对话）与 `tts_api_key`（发声），
  以及视觉单独用别家时才会填的 `vision_api_key`
  都是明文，这样文件才能直接手改（详见上文「配置 AI」）。保存时会对文件调
  `os.chmod(0o600)`，但**这只是尽力而为**：它只在 POSIX 上真正生效，Windows 上不等于
  "仅本用户可读"，而且**明文就是明文** —— 别把这个文件提交进 git、别连整个 `data_local/`
  一起打包外发或截图晒出；要分享项目请只分享代码。
- 旧版用 Fernet 加密存的 `encrypted_api_key` / `encrypted_tts_api_key` 与 `ai_config.key`
  **仍会被读取**（明文优先），但已不再写入 —— 迁移后的 `ai_config.json` 里只剩明文，
  备份在 `ai_config.json.bak`（同样含明文 Key，别外发）。
- 旧项目 `mindful_pi` 曾以明文保存过 API Key，**建议到智谱控制台重置该 Key**。
- 本项目不迁移旧项目任何配置文件，需重新配置。

## 九、许可与致谢

- 情绪模型 HSEmotion（enet_b0_8_va_mtl）、FER+、YuNet 人脸检测、MediaPipe FaceMesh/HandLandmarker
- 语音：**[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（k2-fsa）** —— 唤醒用 KWS zipformer 中文关键词模型
  （`sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`）、识别用 `sherpa-onnx-paraformer-zh-small`，
  两者都在设备本地运行；
  **发声使用阿里云百炼（DashScope）的 `qwen3-tts-flash` 云端服务**，按字符计费、音频由接口返回，
  本地不再分发任何 TTS 权重。
  `pypinyin` 用于把唤醒词转成 KWS 的拼音 token。
  （旧的 Vosk、edge-tts，以及 Kokoro / Matcha / VITS / Piper 等本地音色与 `models/emojis/`
  卡通表情均已移除。）
- 商业背景见 `docs/商业计划书要点.md`

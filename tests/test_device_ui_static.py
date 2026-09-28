from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"


def test_default_device_page_has_one_avatar_and_hides_stream_until_needed():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "device_ui.css").read_text(encoding="utf-8")

    assert html.count("<agent-robot-avatar") == 1
    assert '<img id="screen" hidden' in html
    assert "face.hidden = !isFace" in js
    assert "img.hidden = isFace" in js
    assert "#faceLayer[hidden], #screen[hidden]" in css
    assert "img.classList." not in js
    assert "face.classList." not in js


def test_face_page_has_swipe_settings_panel():
    """第一页下滑面板：亮度/音量/模式/自主互动 + 主动互动订阅。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "device_ui.css").read_text(encoding="utf-8")

    for element in ('id="mChew"', 'id="mChewCount"',
                    'id="sheet"', 'id="setBright"', 'id="setVolume"', 'id="setRate"',
                    'id="modeSeg"', 'id="autoSeg"', 'id="clockSeg"',
                    'id="clockRow"', 'id="clkTime"',
                    'id="setMon"', 'id="setDay"', 'id="setHour"', 'id="setMin"',
                    'id="setSec"', 'id="clockApply"', 'id="clockReset"'):
        assert element in html
    assert "月" in html and "日" in html and "秒" in html
    assert "#sheet" in css
    assert "openSheet" in js and "/api/agent/settings" in js
    assert "EventSource('/api/agent/proactive')" in js
    assert "audio.volume = currentVolume()" in js
    # 面板整页铺满 + 黑白配色；上滑返回；时间日期本地生成不走接口
    assert "inset: 0" in css
    assert "startY - yOf(e) > SWIPE_MIN" in js
    assert "Date.now() + _clockOffsetMs" in js
    assert "/api/clock" not in js
    assert "applyClock" in js and "fillClockInputs" in js
    # 累计咀嚼次数也要显示（不只是每分钟频率）
    assert "'累计 ' + (m.chew_count ?? '—') + ' 次'" in js


def test_legacy_pil_face_engine_removed():
    """旧的 PIL 像素表情页已删除：不再有模块与接口。

    （管理后台已整体删除，所以这里不再断言 admin.html 的内容。）
    """
    assert not (ROOT / "app" / "display" / "face.py").exists()
    device_api = (ROOT / "app" / "api" / "device.py").read_text(encoding="utf-8")
    assert "face_states" not in device_api
    assert "display.face" not in device_api


def test_admin_backend_removed():
    """管理后台已删除，配置改为手改 data_local/ai_config.json。"""
    from fastapi.testclient import TestClient

    from app.server import create_app

    assert not (STATIC / "admin.html").exists()
    for stale in ("js/api.js", "js/dialogue.js", "js/camera.js", "js/report.js",
                  "js/settings.js", "js/device.js", "js/main.js", "css/style.css"):
        assert not (STATIC / stale).exists(), f"应已删除: {stale}"
    with TestClient(create_app()) as client:
        assert client.get("/admin").status_code == 404
        # 报告接口仍在（前端覆盖层用）
        assert client.get("/api/report").status_code == 200
        assert client.get("/api/analytics").status_code == 200
        assert client.get("/api/personal-doc").status_code == 200


def test_frontend_assets_are_not_cached():
    """页面 HTML 与自研 JS/CSS 必须 no-store。

    设备屏长期跑在 kiosk 浏览器里：缓存住旧脚本会导致"改了代码不生效"，
    甚至页面卡在"正在连接设备屏…"（服务没起时浏览器还拿缓存页面渲染，看不到真实错误）。
    注意：Windows 上 StaticFiles 传进来的 path 是反斜杠（'js\\device_ui.js'），
    判断前缀前必须归一化 —— 曾因此头没设上，而直接调用 get_response('js/x.js') 又看起来正常。
    """
    from fastapi.testclient import TestClient

    from app.server import create_app

    app = create_app()
    with TestClient(app) as client:
        cc = client.get("/").headers.get("cache-control") or ""
        assert "no-store" in cc, f"/ 未禁用缓存: {cc!r}"
        for path in ("/static/js/device_ui.js", "/static/css/device_ui.css"):
            cc = client.get(path).headers.get("cache-control") or ""
            assert "no-store" in cc, f"{path} 未禁用缓存: {cc!r}"


def test_page_has_boot_failure_fallback():
    """脚本没跑起来时不能把用户永远晾在加载遮罩上，要给出可操作提示。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "__dshBooted" in html and "__dshBooted" in js
    assert "Ctrl+F5" in html, "兜底提示要告诉用户怎么恢复"
    assert "window.__dshBooted = true" in js, "init() 一开始就要标记启动成功"


def test_reply_text_does_not_flicker_and_continuous_keeps_mic_open():
    """两条用户实测反馈的回归：

    ① **回复文字闪一下又变**：`loadState()` 每 600ms 重画对话区，把实时气泡冲掉了。
       现在历史记录放在 `#dlgHistory` 里，实时气泡(`#liveReply`)在旁边，互不干扰。
    ② **连续模式像单句**：以前"生成+播报"那 5~10 秒麦克风是关着的 ——
       这期间用户说的话全丢、静默计时还从播报结束才起算。现在录音一停就把
       待唤醒监听挂回去（用 `!_playing` 保证机器人自己的声音不会触发录音）。
    """
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "dlgHistory" in js and "liveReply" in js
    load = js.split("async function loadState()")[1][:900]
    assert "dlgHistory" in load and "hist.innerHTML" in load, "历史区要单独一层"
    assert "box.innerHTML" not in load, "不能再整体重画对话区（会把实时气泡冲掉）"

    stop = js.split("_recorder.onstop = async () => {")[1][:2600]
    assert "if (wakeEnabled() && wakeContinuousNow()) startWakeListen();" in stop, \
        "连续模式要在录音结束时就挂回监听（别等播报结束）"
    assert "_continuousIdleSec" in js and "s 后回待唤醒" in js, "状态栏要能看到连续窗口"


def test_tts_switch_in_settings_panel():
    """下滑设置面板里的「语音播报」开关（关掉后只出文字不出声）。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert 'id="setTts"' in html and "语音播报" in html
    assert "saveSetting({ tts_enabled:" in js
    assert "ttsBox.checked = s.tts_enabled !== false" in js
    # 关掉播报时，"唤醒后 单句/连续"这两个选项要禁用（用户要求）
    assert "applyWakeModeAvailability" in js
    assert "box.querySelectorAll('button').forEach(b => { b.disabled = !ttsOn; })" in js
    assert ".seg.off button, .seg button:disabled" in (STATIC / "css" / "device_ui.css").read_text(
        encoding="utf-8")


def test_continuous_resumes_one_second_after_speech():
    """连续模式：**语音结束后 1 秒**再接受下一句（用户要求）。"""
    from app.core import interaction_config as ic

    assert ic.WAKE["continuous_resume_sec"] == 1.0
    assert ic.panel_payload()["wake_continuous_resume_sec"] == 1.0
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "_continuousResumeMs" in js
    assert "setTimeout(r, _continuousResumeMs)" in js, "续听要用下发的那 1 秒，而不是写死的 350ms"


def test_face_camera_emotion_also_drives_robot_avatar():
    """相机识别到的情绪也要驱动小机器人动作（以前只驱动指标行文字）。

    限制：只在情绪变化且设备空闲时动、最快 4 秒一次、对话流程优先 ——
    否则吃饭时表情会一直抽动。
    """
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "VISION_EMOTION_POOL" in js and "reactToFaceEmotion" in js
    body = js.split("function reactToFaceEmotion(m)")[1][:700]
    assert "m.emotion_key" in body and "_lastFaceEmotion" in body
    assert "4000" in body, "要有节流"
    assert "_recording || _dlgBusy || _playing" in body, "对话流程优先"
    assert "reactToFaceEmotion(m)" in js.split("async function loadState()")[1][:1600], \
        "loadState 里要是真的调用了"


def test_robot_avatar_uses_rich_and_frequent_expressions():
    """小机器人（表情页那个头像）要"表情更丰富、更频繁"。

    用户说的"表情"指这个头像窗口，不是识别到的情绪标签。这里钉住三件事：
      ① 情绪 → **动作池轮换**（同类情绪连着出现也换脸，不是只变一次）；
      ② 回复是**按句**流式到达的 → 每句都触发一次反应（250ms 防抖）；
      ③ 流程节点用上对应动作，并且待机久了会 bored / sleep。
    """
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "const AvatarDirector = {" in js and "POOLS:" in js
    director = js.split("const AvatarDirector = {")[1][:2200]
    for emotion in ("happy", "sad", "angry", "anxiety", "surprise", "sleepy", "bored"):
        assert f"{emotion}:" in director, f"{emotion} 没有动作池"
    assert "_at[emotion]" in director, "同一情绪要在池子里轮换"

    face = js.split("const FaceStates = {")[1][:2200]
    for action in ("'input'", "'waiting'", "'send'", "'surprise'", "'error'"):
        assert action in face, f"流程动作缺 {action}"
    assert "setAntennaFlash(true)" in face and "input(true)" in face
    # 注意断言的是"真的调用了"这个动作，注释里提到不算
    assert "AvatarDirector.play('wake'" not in face, \
        "组件没睡时 play('wake') 是空操作（实测无事件），别用它做唤醒反应"

    # 按句反应：每收到一句就按这一句的情绪走一次（池内轮换由 react 负责）
    assert "extractEmotion(d.text || '') || extractEmotion(fullText)" in js
    assert "setInterval(() => AvatarDirector.tick(), 800)" in js
    assert "'bored'" in director and "'sleep'" in director


def test_static_assets_are_clean_utf8_without_bom():
    """静态资源必须是「无 BOM 的合法 UTF-8，且没有乱码替换符」。

    这条是真事故换来的：用 PowerShell 5.1 的 Get-Content -Raw / Set-Content 改
    index.html 时，PS 按系统 ANSI（cp936）读 UTF-8 文件，中文全成了"姝ｅ康楗唇"，
    而且汉字最后一个字节会和紧跟的 ASCII（'<'/空格/引号）被当成一个 GBK 双字节对**一起丢掉**，
    于是 `</title>` 变成 `/title>`、JS 字符串少了收尾引号 —— 页面直接白屏。
    在 Windows 上改文本请用 Python/编辑器（本项目工具都用 UTF-8 读写），不要用 PS 读写文本。
    """
    checked = 0
    for path in sorted(STATIC.rglob("*")):
        if path.suffix.lower() not in (".html", ".js", ".css", ".svg", ".json"):
            continue
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 带了 BOM"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"{path.name} 不是合法 UTF-8（疑似被 ANSI 工具改坏）: {exc}")
        assert "\ufffd" not in text, f"{path.name} 里有乱码替换符 U+FFFD"
        checked += 1
    assert checked >= 3, "静态资源没扫到，检查路径"


def test_device_page_has_no_broken_tags():
    """闭合标签必须有 '<'：编码事故会把 '<' 吃掉，页面结构就塌了。"""
    import re

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    broken = re.findall(r"(?<![<\w])/(title|head|body|html|div|span|small|button|i|script|label|b)>",
                        html)
    assert not broken, f"这些闭合标签缺 '<'：{broken}"
    # 关键元素成对
    for tag in ("html", "head", "body", "title", "div", "span", "small", "button", "script"):
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = len(re.findall(rf"</{tag}>", html))
        assert opens == closes, f"<{tag}> 开合不匹配: {opens} vs {closes}"
    assert "</title>" in html and "设备屏</title>" in html

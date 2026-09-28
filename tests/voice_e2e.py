"""用「假麦克风」在真实浏览器里跑完整语音对话，并逐段计时。

Chrome/Edge 的 --use-file-for-fake-audio-capture 会把指定 WAV 当作麦克风输入，
于是可以全自动地：点 🎤 → 页面录音 → 静音检测发送 → 识别 → 流式回复 → 播放语音。

用法: .venv/Scripts/python.exe tests/voice_e2e.py [语音wav路径]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765/"   # 正式前端（设备屏；管理后台已删除）
SPEECH = sys.argv[1] if len(sys.argv) > 1 else "data_local/media/bench_5s.wav"
OUT = Path("gui-test-screenshots")
OUT.mkdir(exist_ok=True)


def main() -> int:
    speech = Path(SPEECH).resolve()
    if not speech.exists():
        print(f"语音文件不存在: {speech}")
        return 1
    print(f"假麦克风输入: {speech.name}")

    marks: list[tuple[str, float]] = []
    t0 = time.time()

    def mark(label: str) -> None:
        marks.append((label, time.time() - t0))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={speech}",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        ctx = browser.new_context(viewport={"width": 1200, "height": 900},
                                  permissions=["microphone"])
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 计时探针：只读取事件时间戳，不改业务逻辑（诊断用）
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.evaluate("""() => {
          window.__marks = [];
          window.__t0 = performance.now();
          const rec = (k) => window.__marks.push([k, performance.now() - window.__t0]);
          const origPlay = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function(...a) {
            rec('audio.play()'); this.addEventListener('playing', () => rec('audio.playing'), {once:true});
            return origPlay.apply(this, a);
          };
          const origFetch = window.fetch;
          window.fetch = function(...a) {
            const url = (typeof a[0] === 'string') ? a[0] : (a[0] && a[0].url) || '';
            if (url.includes('/api/asr')) rec('fetch /api/asr');
            if (url.includes('/api/dialogue/stream')) rec('fetch /api/dialogue/stream');
            return origFetch.apply(this, a);
          };
          const origEnqueue = window.enqueueAudio;
          window.enqueueAudio = function(b64) {
            rec('enqueueAudio(' + (b64 ? b64.length + 'B' : 'null') + ')');
            return origEnqueue.apply(this, arguments);
          };
          const origSend = window.sendDialogue;
          window.sendDialogue = function() { rec('sendDialogue()'); return origSend.apply(this, arguments); };
          const obs = new MutationObserver(() => {
            const nodes = document.querySelectorAll('#chatBox .msg');
            const last = nodes[nodes.length - 1];
            if (last) rec('dom:' + last.className);
          });
          obs.observe(document.getElementById('chatBox'), {childList: true, subtree: true, characterData: true});
        }""")

        page.evaluate("() => window.__marks.push(['点击麦克风', performance.now() - window.__t0])")
        page.click("#btnMic")
        page.evaluate("() => window.__marks.push(['点击后(录音开始)', performance.now() - window.__t0])")
        page.wait_for_timeout(16000)          # 录音 + 静音检测 + 识别 + 回复 + 播放
        page.screenshot(path=str(OUT / "voice_e2e.png"))

        marks_js = page.evaluate("() => window.__marks")
        browser.close()

    print("\n=== 页面内时间线（相对脚本开始）===")
    for k, v in marks_js:
        print(f"  {v/1000:7.2f}s  {k}")
    print("\n=== 关键间隔 ===")
    d = dict(marks_js[::1]) if marks_js else {}
    seq = [k for k, _ in marks_js]
    def first(pref):
        for k, v in marks_js:
            if k.startswith(pref):
                return v
        return None
    t_mic = first("点击麦克风")
    t_asr = first("fetch /api/asr")
    t_dlg = first("fetch /api/dialogue")
    t_play = first("audio.play")
    t_snd = first("audio.playing")
    if t_mic and t_asr:
        print(f"  点击麦克风 → 发起识别: {(t_asr - t_mic)/1000:.2f}s（录音+静音检测）")
    if t_asr and t_dlg:
        print(f"  识别完成 → 发起对话: {(t_dlg - t_asr)/1000:.2f}s（识别耗时）")
    if t_dlg and t_play:
        print(f"  发起对话 → 首次播放调用: {(t_play - t_dlg)/1000:.2f}s（AI生成+首句TTS）")
    if t_play and t_snd:
        print(f"  播放调用 → 真正出声: {(t_snd - t_play)/1000:.2f}s")
    if t_mic and t_snd:
        print(f"\n  >>> 点麦克风 → 听到回复: {(t_snd - t_mic)/1000:.2f}s")
    if errors:
        print("\n页面错误:", errors[:5])
    print("\n聊天区内容:")
    print("  " + page_text_summary(marks))
    return 0


def page_text_summary(_):
    return "（见截图 voice_e2e.png）"


if __name__ == "__main__":
    sys.exit(main())

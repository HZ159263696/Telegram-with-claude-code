#!/usr/bin/env python3
"""把语音文件转成中文文字。bridge 收到 Telegram 语音消息时 subprocess 调用。

用法: python3 voice_transcribe.py <音频文件>
输出: stdout 打印转写文本（失败则 stderr + 退出码 1）

用已缓存的 whisper small 模型（中文识别较好、速度可接受）。独立进程隔离，
不占 bridge 常驻内存、不影响其稳定性。
"""
import sys


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: voice_transcribe.py <audio>\n")
        sys.exit(1)
    path = sys.argv[1]
    model_name = sys.argv[2] if len(sys.argv) > 2 else "small"
    import whisper
    model = whisper.load_model(model_name)
    # fp16=False：CPU 上跑，避免 half-precision 警告/报错
    result = model.transcribe(path, language="zh", fp16=False)
    text = (result.get("text") or "").strip()
    sys.stdout.write(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"transcribe error: {e}\n")
        sys.exit(1)

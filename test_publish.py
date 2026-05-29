"""
独立测试脚本：测试B站动态发布功能（图片上传 + 发布动态）。
用法: venv/bin/python test_publish.py
"""
import asyncio
import json
import sys
import io
from pathlib import Path

# 解决Windows GBK编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from auto_publish import publish_dynamic
from config import AUTO_PUBLISH_TOPIC_ID, AUTO_PUBLISH_TOPIC_NAME, UP_NAME

TEST_IMAGE = Path(__file__).parent / "sent_emails" / "pinned_1199636880383016962_20260529231006.png"
TEST_DYNAMIC_ID = "1199636880383016962"  # 跳转链接用的动态ID

async def main():
    # 加载cookies
    cookies = json.loads(Path("cookies.json").read_text())
    print(f"✅ 加载 {len(cookies)} 条cookies")

    if not TEST_IMAGE.exists():
        print(f"❌ 测试图片不存在: {TEST_IMAGE}")
        return

    print(f"📤 开始发布测试动态...")
    print(f"   话题: #{AUTO_PUBLISH_TOPIC_NAME} (ID:{AUTO_PUBLISH_TOPIC_ID})")
    print(f"   图片: {TEST_IMAGE} ({TEST_IMAGE.stat().st_size} bytes)")
    print(f"   链接: https://t.bilibili.com/{TEST_DYNAMIC_ID}?comment_on=1")

    ok = await publish_dynamic(
        dynamic_id=TEST_DYNAMIC_ID,
        screenshot_path=str(TEST_IMAGE),
        cookies=cookies,
        up_name=UP_NAME,
        topic_id=AUTO_PUBLISH_TOPIC_ID,
        topic_name=AUTO_PUBLISH_TOPIC_NAME,
    )

    if ok:
        print("✅ 测试动态发布成功！去B站个人主页查看")
    else:
        print("❌ 测试动态发布失败，查看上方日志定位原因")

if __name__ == "__main__":
    asyncio.run(main())

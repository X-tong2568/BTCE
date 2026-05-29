# email_renderer.py
import time
from config import UP_NAME
from logger_config import logger


class EmailRenderer:
    """
    用于构建邮件 HTML 内容的渲染器。

    此类负责生成完整的邮件 HTML，包括：
    - 渐变主题色头部
    - 动态信息区
    - 新置顶评论区（含文本 + 图片）
    - 原置顶评论区（含文本 + 图片）
    - 跳转按钮区
    整体结构清晰，并适用于邮件客户端显示。
    """

    def __init__(self, color_config):
        """
        初始化渲染器实例。

        :param color_config: 提供随机渐变色的配置对象，必须包含 get_random_gradient() 方法。
        """
        self.color_config = color_config

    def render_email_content(self, dynamic_id, current_html, current_images,
                             last_html, last_images, current_time=None, screenshot_path=None):
        """
        生成邮件完整 HTML。

        渲染流程说明（逐步骤详细解释）：
        --------------------------------------------------
        1. 自动生成当前时间（若调用方未传入）
        2. 调用 color_config 获取随机渐变主色+副色
        3. 拼装邮件 HTML 字符串
        4. 插入监测动态链接、时间信息
        5. 插入新 / 旧置顶评论 HTML 内容
        6. 补全图片 link 的协议（如 //img → https://img）
        7. 添加跳转按钮
        8. 收尾整体 HTML
        --------------------------------------------------

        :return: 完整 HTML 字符串
        """
        try:
            # 生成当前时间（若函数未传入，则自动生成）
            if current_time is None:
                current_time = time.strftime('%Y-%m-%d %H:%M:%S')

            # 从 color_config 获取渐变主题色
            primary_color, secondary_color = self.color_config.get_random_gradient()

            # -------------------------------------------------------
            # 开始构建 HTML 文本（含 CSS 高密度注释版本）
            # -------------------------------------------------------
            email_body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>{UP_NAME} 动态置顶评论更新通知</title>

                <style>

                    /* ================================
                       全局 body 样式
                       ================================ */

                    body {{
                        /* 设置邮件默认字体为微软雅黑，其次 Arial */
                        font-family: 'Microsoft YaHei', Arial, sans-serif;

                        /* 页面四周留白，使内容不至于贴边 */
                        margin: 0;

                        /* 内部整体增加 padding，提高阅读舒适度 */
                        padding: 20px;

                        /* 背景色为灰白，模拟邮件视觉风格 */
                        background-color: #f5f5f5;
                    }}

                    /* ================================
                       邮件容器主框架 .container
                       ================================ */

                    .container {{
                        /* 邮件区域最大宽度 600px */
                        max-width: 600px;

                        /* 自动左右居中 */
                        margin: 0 auto;

                        /* 背景为白色，便于阅读 */
                        background-color: white;

                        /* 圆角效果，使整个邮件外观更柔和 */
                        border-radius: 10px;

                        /* 阴影效果，让邮件主体浮起来 */
                        box-shadow: 0 2px 10px rgba(0,0,0,0.1);

                        /* 隐藏溢出内容（防止子元素突出去） */
                        overflow: hidden;
                    }}

                    /* ================================
                       顶部 header 区域（渐变背景）
                       ================================ */

                    .header {{
                        /* 使用随机渐变颜色构建背景 */
                        background: linear-gradient(135deg, {primary_color}, {secondary_color});

                        /* 前景文字颜色为白色 */
                        color: white;

                        /* 内边距（上下左右 20px） */
                        padding: 20px;

                        /* 文本居中 */
                        text-align: center;
                    }}

                    .header h1 {{
                        /* 重设 margin 为 0，避免标题占额外高度 */
                        margin: 0;

                        /* 标题字号 */
                        font-size: 24px;

                        /* 增加文字阴影提升可读性 */
                        text-shadow: 1px 1px 3px rgba(0,0,0,0.2);
                    }}

                    /* 渐变色分割横条 */
                    .header-gradient-bar {{
                        /* 高度为 5px 的窄条，用于视觉分割 */
                        height: 5px;

                        /* 横向渐变 */
                        background: linear-gradient(90deg, {primary_color}, {secondary_color});

                        /* 与标题之间留出一点间距 */
                        margin-top: 10px;
                    }}

                    /* 内容整体 padding */
                    .content {{
                        padding: 30px;
                    }}

                    /* ================================
                       信息展示区 info-section
                       ================================ */

                    .info-section {{
                        /* 区域文字居中 */
                        text-align: center;

                        /* 灰白背景色 */
                        background-color: #f9f9f9;

                        /* 内边距，保证元素不贴边 */
                        padding: 20px;

                        /* 圆角视觉表现 */
                        border-radius: 8px;

                        /* 区块底部留空，用于区块分隔 */
                        margin-bottom: 20px;

                        /* 左侧边框为主色 */
                        border-left: 4px solid {primary_color};

                        /* 右侧边框为副色 */
                        border-right: 4px solid {secondary_color};
                    }}

                    /* 每行显示区块独立一行 */
                    .centered-block {{
                        display: block;
                        margin: 10px 0;
                        text-align: center;
                    }}

                    /* ================================
                       评论内容区 comment-content
                       ================================ */

                    .comment-content {{
                        /* 外边框 */
                        border: 1px solid #ddd;

                        /* 内边距 */
                        padding: 15px;

                        /* 圆角风格 */
                        border-radius: 5px;

                        /* 保留换行符 */
                        white-space: pre-wrap;

                        /* 长单词 / 链接自动换行 */
                        word-break: break-all;

                        /* 上方间距 */
                        margin-top: 10px;

                        /* 行距增强阅读性 */
                        line-height: 1.5;

                        /* 文本左对齐（便于阅读） */
                        text-align: left;
                    }}

                    /* 新置顶评论背景色 */
                    .current-comment {{
                        background-color: #f0f8ff;
                        border-left: 4px solid {primary_color};
                        border-right: 4px solid {secondary_color};
                    }}

                    /* 原置顶评论背景色 */
                    .previous-comment {{
                        background-color: #f0f0f0;
                        border-left: 4px solid {primary_color};
                        border-right: 4px solid {secondary_color};
                    }}

                    /* ================================
                       图片区域 images-container
                       ================================ */

                    .images-container {{
                        /* 排列方式设为弹性布局 */
                        display: flex;

                        /* 允许换行 */
                        flex-wrap: wrap;

                        /* 图片之间的间距 */
                        gap: 10px;

                        /* 顶部间距 */
                        margin-top: 10px;
                        /* 使图片在容器内水平居中 */
                        justify-content: center;
                    }}

                    /* 图片缩放规则 */
                    .image-item {{
                        /* 最大宽度 300px */
                        max-width: 300px;

                        /* 最大高度 300px */
                        max-height: 300px;

                        /* 保持内容完整比例 */
                        object-fit: contain;

                        /* 图片圆角 */
                        border-radius: 5px;

                        /* 图片边框 */
                        border: 1px solid #ddd;
                    }}

                    /* ================================
                       跳转按钮 .btn
                       ================================ */

                    .btn {{
                        display: inline-block;           /* 允许设置 padding 宽高 */
                        margin-top: 10px;                /* 上方间距 */
                        background: linear-gradient(135deg, {primary_color}, {secondary_color}); /* 渐变背景 */
                        color: #fff;                     /* 字体颜色 */
                        padding: 12px 24px;              /* 内边距 */
                        border-radius: 5px;              /* 圆角按钮 */
                        text-decoration: none;           /* 去除下划线 */
                        font-weight: bold;               /* 字体加粗 */
                        transition: all 0.3s ease;       /* 鼠标 hover 动画效果 */
                        border: none;                    /* 去掉边框 */
                        cursor: pointer;                 /* 鼠标样式为手形 */
                        box-shadow: 0 2px 5px rgba(0,0,0,0.2); /* 按钮阴影 */
                    }}

                    .btn:hover {{
                        transform: translateY(-2px);         /* 悬停时上移动 */
                        box-shadow: 0 4px 8px rgba(0,0,0,0.2); /* 阴影增强 */
                    }}

                    /* ================================
                       独立按钮区 action-section
                       ================================ */

                    .action-section {{
                        text-align: center;                /* 居中显示内容 */
                        padding: 25px;                    /* 内间距 */
                        background: linear-gradient(135deg, #f9f9f9, #f0f0f0); /* 灰色渐变背景 */
                        border-radius: 8px;               /* 区块圆角 */
                        margin: 20px 0;                   /* 上下分隔间距 */
                        border: 2px solid transparent;    /* 为 border-image 做准备 */
                        border-image: linear-gradient(135deg, {primary_color}, {secondary_color}); /* 边框渐变 */
                        border-image-slice: 1;            /* 让渐变填满整个边框 */
                    }}

                    /* ================================
                       底部 footer 区域
                       ================================ */

                    .footer {{
                        text-align: center;      /* 居中文本 */
                        color: #999;             /* 浅灰字体 */
                        font-size: 12px;         /* 较小字号 */
                        margin-top: 20px;        /* 顶部外边距 */
                        padding: 20px;           /* 内边距 */
                        border-top: 1px solid #eee; /* 顶部分割线 */
                    }}

                    /* ================================
                       小徽章类 time-badge / key-badge
                       ================================ */

                    .time-badge {{
                        display: inline-block;                                 /* 将标签视为小块 */
                        background: linear-gradient(135deg, {primary_color}, {secondary_color}); /* 渐变背景 */
                        color: white;                                         /* 白色字体 */
                        padding: 4px 8px;                                    /* 内边距 */
                        border-radius: 3px;                                   /* 小圆角 */
                        font-size: 12px;                                      /* 小字号 */
                        margin: 5px 0;                                       /* 上下间距 */
                    }}

                    .key-badge {{
                        display: inline-block;                                 /* 成为独立元素 */
                        background: linear-gradient(135deg, {primary_color}, {secondary_color}); /* 渐变背景 */
                        color: white;                                         /* 白色文本 */
                        padding: 4px 8px;                                    /* 内边距 */
                        border-radius: 3px;                                   /* 圆角 */
                        font-size: 16px;                                      /* 大字号用于标题 */
                        margin: 5px 0;                                       /* 上下间距 */
                    }}

                    /* 动态链接样式 */
                    .dynamic-link {{
                        display: block;             /* 独占一行 */
                        margin: 10px 0;             /* 上下间距 */
                        word-break: break-all;      /* 防止链接过长撑破布局 */
                    }}

                </style>
            </head>

            <body>
                <div class="container">
                    <div class="header">
                        <h1>{UP_NAME} 动态置顶评论更新通知</h1>
                        <div class="header-gradient-bar"></div>
                    </div>

                    <div class="content">

                        <!-- 监测动态信息 -->
                        <div class="info-section">
                            <div class="centered-block">
                                <span class="time-badge">📱 监测动态：</span>
                            </div>

                            <div class="centered-block">
                                <a href="https://t.bilibili.com/{dynamic_id}" class="dynamic-link">
                                    https://t.bilibili.com/{dynamic_id}
                                </a>
                            </div>

                            <div class="centered-block">
                                <span class="time-badge">⏰ 检测时间：</span>
                            </div>

                            <div class="centered-block">
                                {current_time}
                            </div>
                        </div>

                        <!-- 新置顶评论 -->
                        <div class="info-section">
                            <div class="centered-block">
                                <span class="key-badge">✨ 新置顶评论： ✨</span>
                            </div>
            """

            # 新置顶评论内容：优先用截图，失败兜底文字+图片
            screenshot_ok = False
            if screenshot_path:
                try:
                    import base64
                    with open(screenshot_path, 'rb') as f:
                        b64 = base64.b64encode(f.read()).decode('utf-8')
                    email_body += '<div class="comment-content current-comment" style="text-align:center;padding:10px;">'
                    email_body += f'<img src="data:image/png;base64,{b64}" style="max-width:100%;border-radius:8px;" alt="置顶评论截图">'
                    email_body += '</div>'
                    screenshot_ok = True
                except Exception as e:
                    logger.warning(f"⚠️ 嵌入置顶评论截图失败: {e}")

            if not screenshot_ok:
                # 兜底：旧版文字+评论区图片
                email_body += f'''
                            <div class="comment-content current-comment">
                                {current_html if current_html else "无置顶评论"}
                            </div>
                '''
                if current_images:
                    email_body += '<div class="images-container">'
                    for img_url in current_images:
                        if img_url.startswith('//'):
                            img_url = 'https:' + img_url
                        elif not img_url.startswith(('http://', 'https://')):
                            img_url = 'https:' + img_url
                        email_body += f'<img class="image-item" src="{img_url}" alt="评论图片">'
                    email_body += '</div>'

            # 继续拼接旧置顶评论区域
            email_body += f"""
                        </div>

                        <!-- 原置顶评论 -->
                        <div class="info-section">
                            <div class="centered-block">
                                <span class="key-badge">📄 原置顶评论： 📄</span>
                            </div>

                            <div class="comment-content previous-comment">
                                {last_html if last_html else "无原置顶评论"}
                            </div>
            """

            # 原置顶图片
            if last_images:
                email_body += '<div class="images-container">'
                for img_url in last_images:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif not img_url.startswith(('http://', 'https://')):
                        img_url = 'https:' + img_url
                    email_body += f'<img class="image-item" src="{img_url}" alt="原评论图片">'
                email_body += '</div>'

            # 按钮区 + 结尾
            email_body += f"""
                        </div>

                        <div class="action-section">
                            <p>点击下方按钮查看最新动态：</p>
                            <a class="btn" href="https://t.bilibili.com/{dynamic_id}?comment_on=1" target="_blank">
                                🔍 前往B站查看动态
                            </a>
                        </div>

                    </div>

                    <div class="footer">
                        <p>此邮件由动态监控系统自动发送，请勿回复</p>
                        <p>检测时间: {current_time}</p>
                        <p>本次随机主题色: {primary_color} → {secondary_color}</p>
                    </div>
                </div>
            </body>
            </html>
            """

            return email_body

        except Exception as e:
            logger.error(f"❌ 渲染邮件内容失败: {e}")
            return f"<html><body><h1>渲染邮件内容出错: {e}</h1></body></html>"

    def render_new_dynamic_email(self, up_name: str, dynamic_id: str, dynamic_url: str, current_time: str, content_text: str = "") -> str:
        """生成新动态通知邮件 HTML"""
        try:
            primary_color, secondary_color = self.color_config.get_random_gradient()

            content_block = ""
            if content_text:
                content_block = f"""
                    <div class="info-section" style="text-align:left;">
                        <span class="time-badge">📝 动态内容</span>
                        <p style="white-space:pre-wrap;word-break:break-word;line-height:1.6;">{content_text}</p>
                    </div>
                """

            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>{up_name} 发布新动态</title>
                <style>
                    body {{ font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
                    .container {{ max-width: 600px; margin: 0 auto; background-color: white; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); overflow: hidden; }}
                    .header {{ background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: white; padding: 20px; text-align: center; }}
                    .header h1 {{ margin: 0; font-size: 24px; text-shadow: 1px 1px 3px rgba(0,0,0,0.2); }}
                    .content {{ padding: 30px; }}
                    .info-section {{ background-color: #f9f9f9; padding: 20px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid {primary_color}; }}
                    .btn {{ display: inline-block; margin-top: 20px; background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: #fff; padding: 12px 24px; border-radius: 5px; text-decoration: none; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.2); }}
                    .dynamic-link {{ display: block; margin: 10px 0; word-break: break-all; color: #2196F3; }}
                    .footer {{ text-align: center; color: #999; font-size: 12px; padding: 20px; border-top: 1px solid #eee; }}
                    .time-badge {{ display: inline-block; background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: white; padding: 4px 8px; border-radius: 3px; font-size: 12px; margin: 5px 0; }}
                    .action-section {{ text-align: center; padding: 25px; background: linear-gradient(135deg, #f9f9f9, #f0f0f0); border-radius: 8px; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>{up_name} 发布了新动态</h1>
                    </div>
                    <div class="content">
                        {content_block}
                        <div class="info-section" style="text-align:center;">
                            <span class="time-badge">📱 新动态链接</span>
                            <a href="{dynamic_url}" class="dynamic-link">{dynamic_url}</a>
                            <br>
                            <span class="time-badge">⏰ 检测时间</span>
                            <p>{current_time}</p>
                        </div>
                        <div class="action-section">
                            <a class="btn" href="{dynamic_url}" target="_blank">🔍 前往B站查看动态</a>
                        </div>
                    </div>
                    <div class="footer">
                        <p>此邮件由动态监控系统自动发送，请勿回复</p>
                        <p>检测时间: {current_time}</p>
                    </div>
                </div>
            </body>
            </html>
            """
        except Exception as e:
            logger.error(f"❌ 渲染新动态邮件失败: {e}")
            return f"<html><body><h1>渲染出错: {e}</h1></body></html>"

    def render_new_dynamics_batch_email(self, up_name: str, new_dynamics: list, current_time: str) -> str:
        """生成批量新动态通知邮件——和置顶评论邮件完全一致的样式，截图替换评论区域"""
        try:
            import base64
            primary_color, secondary_color = self.color_config.get_random_gradient()

            items_html = ""
            for dyn in new_dynamics:
                dyn_id = dyn.get("dynamic_id", "")
                dyn_url = f"https://t.bilibili.com/{dyn_id}"
                screenshot_path = dyn.get("screenshot_path", "")

                img_html = ""
                if screenshot_path:
                    try:
                        with open(screenshot_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                        img_html = f'<img class="image-item" src="data:image/png;base64,{b64}" alt="动态截图">'
                    except Exception:
                        img_html = '<p style="color:#999;">[截图加载失败]</p>'

                items_html += f"""
                <div class="info-section">
                    <div class="centered-block">
                        <span class="key-badge">✨ 新动态： ✨</span>
                    </div>
                    <div style="text-align:center;margin-top:10px;">
                        {img_html}
                    </div>
                    <div class="centered-block" style="margin-top:10px;">
                        <span class="time-badge">🔗 动态链接：</span>
                    </div>
                    <div class="centered-block">
                        <a href="{dyn_url}" class="dynamic-link">{dyn_url}</a>
                    </div>
                </div>
                """

            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <style>
                    body {{ font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
                    .container {{ max-width: 600px; margin: 0 auto; background-color: white; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); overflow: hidden; }}
                    .header {{ background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: white; padding: 20px; text-align: center; }}
                    .header h1 {{ margin: 0; font-size: 24px; text-shadow: 1px 1px 3px rgba(0,0,0,0.2); }}
                    .header-gradient-bar {{ height: 5px; background: linear-gradient(90deg, {primary_color}, {secondary_color}); margin-top: 10px; }}
                    .content {{ padding: 30px; }}
                    .info-section {{ background-color: #f9f9f9; padding: 20px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid {primary_color}; border-right: 4px solid {secondary_color}; text-align: center; }}
                    .centered-block {{ display: block; margin: 10px 0; text-align: center; }}
                    .dynamic-link {{ display: block; margin: 10px 0; word-break: break-all; color: #2196F3; }}
                    .image-item {{ max-width: 100%; border-radius: 5px; border: 1px solid #ddd; }}
                    .btn {{ display: inline-block; margin-top: 10px; background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: #fff; padding: 12px 24px; border-radius: 5px; text-decoration: none; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.2); }}
                    .footer {{ text-align: center; color: #999; font-size: 12px; padding: 20px; border-top: 1px solid #eee; }}
                    .time-badge {{ display: inline-block; background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: white; padding: 4px 8px; border-radius: 3px; font-size: 12px; margin: 5px 0; }}
                    .key-badge {{ display: inline-block; background: linear-gradient(135deg, {primary_color}, {secondary_color}); color: white; padding: 4px 8px; border-radius: 3px; font-size: 16px; margin: 5px 0; }}
                    .action-section {{ text-align: center; padding: 25px; background: linear-gradient(135deg, #f9f9f9, #f0f0f0); border-radius: 8px; margin: 20px 0; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>{up_name} 发布了 {len(new_dynamics)} 条新动态</h1>
                        <div class="header-gradient-bar"></div>
                    </div>
                    <div class="content">
                        <!-- 监测信息 -->
                        <div class="info-section">
                            <div class="centered-block">
                                <span class="time-badge">⏰ 检测时间：</span>
                            </div>
                            <div class="centered-block">
                                {current_time}
                            </div>
                        </div>
                        {items_html}
                        <!-- 跳转按钮 -->
                        <div class="action-section">
                            <p>点击下方按钮查看动态：</p>
                            <a class="btn" href="https://t.bilibili.com/{new_dynamics[0].get('dynamic_id', '')}" target="_blank">
                                🔍 前往B站查看动态
                            </a>
                        </div>
                    </div>
                    <div class="footer">
                        <p>此邮件由动态监控系统自动发送，请勿回复</p>
                        <p>检测时间: {current_time}</p>
                        <p>本次随机主题色: {primary_color} → {secondary_color}</p>
                    </div>
                </div>
            </body>
            </html>
            """
        except Exception as e:
            logger.error(f"❌ 渲染批量新动态邮件失败: {e}")
            return f"<html><body><h1>渲染出错: {e}</h1></body></html>"

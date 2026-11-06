# config_github.example.py
# GitHub 通知归档配置示例
# 使用方法：复制为 config_github.py，填入自己的 token
# config_github.py 已加入 .gitignore，不会被提交或部署覆盖

# 是否启用 GitHub 归档
GITHUB_ARCHIVE_ENABLED = False

# 归档仓库（私有仓库）
GITHUB_ARCHIVE_OWNER = "your-github-username"
GITHUB_ARCHIVE_REPO = "your-archive-repo"
GITHUB_ARCHIVE_BRANCH = "main"

# fine-grained PAT
# 权限：只选你的归档私有仓库的 Contents: Read and write
GITHUB_ARCHIVE_TOKEN = ""

# 请求超时（秒）
GITHUB_ARCHIVE_TIMEOUT = 20

# 本地预览模式：True 时不提交 GitHub，只写到 data/archive_preview/
# 本地调试建议 True；服务器上改成 False
GITHUB_ARCHIVE_DRY_RUN = False

# 图片处理：最长边像素、WebP 质量
GITHUB_ARCHIVE_IMAGE_MAX_SIZE = 1280
GITHUB_ARCHIVE_WEBP_QUALITY = 80

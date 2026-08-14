# 财新周刊抓取、总结与归档

本项目参考 `economist_weekly_archiver_skill` 的增量归档和数据库输出方式，抓取
[财新周刊](https://weekly.caixin.com/) 最新或指定期次的全部分类文章。财新原文为中文，
因此不做翻译，只保存中文原文、中文总结和正文图片。

## 抓取边界

- 首页：按页面顺序发现 `/{year}/cw{number}/` 期次，第一条视为最新一期。
- 期次元数据：从期次页读取总期号、年度刊期、官方出版日期，并下载右上角 `.cover` 大图作为卡片封面。
- 目录：抓取封面报道，以及三栏目录中 `.magIntrotit` 分类后对应的所有文章。
- 分页：抓正文时自动将文章 URL 转为 `?p0` 全页模式；数据库仍保存无 query 的稳定 URL 用于去重。
- 正文：只读取 `#Main_Content_Val` 内的 `<p>`，排除 `.aitt`、付费弹层、推荐、广告和评论。
- 图片：读取 `.article_media_pic`、正文容器中的 `img.caixin.com` 图片，以及“显影”等图集页的
  `datanews.caixin.com` 大图；不读取推荐区和广告图。
- 登录态：先调用财新用户信息接口检查持久化 Cookie；仅在接口明确返回未登录时调用登录接口。
- 完整性：只要页面仍有 `.payreadwarp` 就视为付费预览，不根据预览段落数量或字数猜测全文；
  自动把登录 Cookie 注入 Chromium，等待该 class 被清除后再解析。
- 图片页：登录后的页面若没有有效正文但有正文图片，按图片页归档，不把 `.aitt` 网页提示送给 LLM。

示例文章页面中，`#the_content` 还包含相关报道、周刊封面、音频提示、印刷版推广、广告和评论，
所以不能直接提取整个 `#the_content` 或页面所有 `<p>`。项目采用正文白名单，不依赖正文后的
噪音黑名单。

## 安装

```bash
cd /Users/luzhe/Desktop/code/agent_skills/caixin_weekly_archiver_skill
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中填写 `LLM_API_KEY`。`LLM_BASE_URL` 可配置任何兼容 OpenAI Chat
Completions 的服务。

财新的匿名服务端 HTML 可能只返回首段，项目会检测并拒绝将预览误存为全文。在 `.env` 配置
财新账号和登录请求中使用的加密密码：

```dotenv
CAIXIN_ACCOUNT=your-account@example.com
CAIXIN_PASSWORD=REPLACE_WITH_ONCE_PERCENT_ENCODED_ENCRYPTED_PASSWORD
```

`CAIXIN_PASSWORD` 不是明文密码，而是浏览器登录请求中解密前、只经过一次 URL 编码的字符串，
例如值中的 `/`、`=` 分别写成 `%2F`、`%3D`。程序交给 `requests` 后会再编码一次，最终请求 URL
中的 `%` 会显示为 `%25`，与浏览器请求一致。

程序默认把登录 Cookie 保存到项目内 `.caixin-auth.json`，文件权限为 `0600`，且已加入
`.gitignore`。每次抓取会先调用 `/api/ucenter/userinfo/get`：

- 返回 `code=0` 时复用现有会话，不调用登录接口，避免不必要地挤掉其他设备。
- 明确返回 `code=600` 时，才调用 `loginJsonp` 获取新 token 并更新 Cookie 文件。
- 网络错误或未知状态会直接停止，不会把临时故障误判成退出登录后反复登录。

可选配置：

```dotenv
# 留空时使用 .caixin-auth.json
CAIXIN_COOKIE_PATH=

# 一般无需修改，默认值与财新网页端一致
CAIXIN_DEVICE_TYPE=5
CAIXIN_UNIT=1
CAIXIN_DEVICE=CaixinWebsite

# 也可连接已用 --remote-debugging-port 启动的 Chromium
BROWSER_ADDRESS=127.0.0.1:9222

# 或指定独立的持久化 profile
BROWSER_USER_DATA_PATH=/absolute/path/to/caixin/chrome_profile
BROWSER_CONTENT_WAIT_S=15
```

项目不会读取、导出或写入日常 Chrome 的 Cookie 文件。登录后的财新页面会先显示一段预览，
再异步注入全文；抓取器会把自动登录 Cookie 注入独立 Chromium 并轮询正文，默认最多等待 15 秒。

只有在不配置账号密码、需要人工维护浏览器会话时才使用：

```bash
python sync_weekly.py --login
```

在弹出的 Chrome 中登录财新后回到终端按 Enter。后续抓取会复用这个 profile。

## 使用

```bash
# 查看最新一期目录，不抓正文
python sync_weekly.py --dry-run

# 先抓前三篇，仅验证原文、图片和数据库，不调用 LLM
python sync_weekly.py --limit 3 --no-summary

# 抓取最新一期全部文章并总结
python sync_weekly.py

# 抓指定期次，可用期号或完整 URL
python sync_weekly.py --issue cw1218
python sync_weekly.py --issue https://weekly.caixin.com/2026/cw1218/

# 调试单篇正文清洗结果，不写数据库
python sync_weekly.py \
  --single-url https://weekly.caixin.com/2026-08-08/102472481.html \
  --extract-json

# 强制重抓本期已存在 URL
python sync_weekly.py --issue cw1218 --force
```

每完成一篇就原子写盘，可中断后续跑。已存在 URL 默认跳过。

## 输出

```text
database.js
output_results/
├── database_index.js
└── CX/
    └── 2026-08-10/
        ├── cover.jpg
        ├── database.js
        └── images/
frontend/
├── index.html
└── assets/
```

`output_results/CX/{issue_date}/database.js` 和 `output_results/database_index.js` 的层级及字段
与 Economist 项目一致。主要差异只在字段值：

- `publication_type` 为 `CX`。
- `title` 与 `title_zh` 都保存财新原标题。
- `paragraphs[].zh_text` 保存中文原文，`en_text` 为空。
- `summary_md` 保存中文总结。
- `glossary_*` 和 `term_annotations` 保留兼容字段，当前不做英文术语翻译与解析。

项目根 `database.js` 也沿用 Economist 的 `window.economist_db = [...]` 顶层变量，便于复用
现有工具；paper 数据通过 `publication_type: "CX"` 区分财新。

本地查看：

```bash
python3 -m http.server 8765
# 浏览器打开 http://127.0.0.1:8765/frontend/
```

## 测试

```bash
python -m unittest discover -s tests -v
python -m py_compile sync_weekly.py
```

## 版权与安全

抓取结果含付费原文，只应供个人阅读和研究，不要公开部署或提交 `database.js`、图片、`.env`、
`.caixin-auth.json`、浏览器 profile 等内容。财新内容版权归财新传媒所有。

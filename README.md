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
- 完整性：正文容器缺失或正文过短时拒绝入库，可使用已登录浏览器回退重试。

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

财新的匿名服务端 HTML 可能只返回首段，项目会检测并拒绝将预览误存为全文。要抓付费全文，
请配置以下任一登录浏览器方式：

```dotenv
# 连接已用 --remote-debugging-port 启动的 Chromium
BROWSER_ADDRESS=127.0.0.1:9222

# 或使用独立的持久化 profile；首次运行会弹出 Chrome，请在其中登录一次
BROWSER_USER_DATA_PATH=/absolute/path/to/caixin/chrome_profile
BROWSER_CONTENT_WAIT_S=15
```

项目不会读取、导出或写入你日常 Chrome 的 Cookie 文件。
登录后的财新页面会先显示一段预览，再异步注入全文；抓取器会轮询正文，默认最多等待 15 秒。

独立 profile 首次登录：

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
浏览器 profile 等内容。财新内容版权归财新传媒所有。

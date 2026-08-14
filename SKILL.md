---
name: caixin-weekly-archiver
description: 自动检查并维护财新登录会话，抓取财新周刊最新或指定期次的分类目录、订阅正文和正文图片，生成中文总结，并以 Economist-compatible database.js 与 database_index.js 归档。
---

# Caixin Weekly Archiver

本项目抓取财新周刊。默认从 `https://weekly.caixin.com/` 发现最新一期，也支持 `cw1218`
或完整期次 URL。

## 快速使用

```bash
python sync_weekly.py --dry-run
python sync_weekly.py --limit 3 --no-summary
python sync_weekly.py --issue cw1218
```

## 规则

- 分类来自期次目录 `.magIntrotit`，覆盖三栏文章以及独立的封面报道。
- 从期次页 `.mainMagContent .cover img` 下载右上封面大图作为归档卡片图片。
- 从期次页标题与 `.source` 提取总期号、年度刊期和官方出版日期。
- 抓正文时将文章 URL 统一转换为 `?p0` 全页模式，避免长文只归档第一页。
- 先用 `.caixin-auth.json` Cookie 调用用户信息接口；仅在明确未登录时使用 `.env` 中的账号和加密密码重新登录。
- 将 API 登录得到的 Cookie 自动注入独立 Chromium，等待付费正文由页面 JavaScript 注入。
- 页面仍有 `.payreadwarp` 时一律视为付费预览；不要因预览有多个段落而误判为全文。
- 对没有有效段落但有正文图片的页面按图片页归档，排除 `.aitt` 提示且不生成文本总结。
- 正文仅来自 `#Main_Content_Val` 的段落。
- 图片仅来自 `.article_media_pic` 与正文容器的 `img.caixin.com`。
- 不做翻译；`paragraphs[].zh_text` 保存中文原文，`summary_md` 保存中文总结。
- 每篇文章完成后立即原子落库，URL 全局去重。
- 输出 schema 与 Economist 项目的 paper database 和 database index 一致。
- 不要输出、提交或公开分发 `.env`、`.caixin-auth.json`、浏览器 profile、付费正文或其他登录凭据。

完整配置和字段说明见 `README.md`。

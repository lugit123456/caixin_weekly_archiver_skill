#!/usr/bin/env python3
"""Archive Caixin Weekly issues into the Economist-compatible database schema."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests
from dotenv import load_dotenv
from lxml import html as lxml_html


ROOT = Path(__file__).resolve().parent
HOME_URL = "https://weekly.caixin.com/"
LOGIN_URL = "https://gateway.caixin.com/api/ucenter/user/v1/loginJsonp"
USERINFO_URL = "https://gateway.caixin.com/api/ucenter/userinfo/get"
PUBLICATION_TYPE = "CX"
PUBLICATION_NAME = "Caixin Weekly"
ARTICLE_URL_RE = re.compile(r"^https?://weekly\.caixin\.com/\d{4}-\d{2}-\d{2}/\d+\.html(?:\?.*)?$")
ISSUE_URL_RE = re.compile(r"^https?://weekly\.caixin\.com/(\d{4})/(cw\d+)/?$")
PAYWALL_MARKERS = ("阅读全文", "立即订阅", "订阅后畅读", "登录后阅读")
EXCLUDED_PARAGRAPH_CLASSES = {"aitt", "dialog_desc"}
LOGIN_COOKIE_FIELDS = {
    "SA_USER_auth": "userAuth",
    "UID": "uid",
    "SA_USER_UID": "uid",
    "SA_USER_NICK_NAME": "nickname",
    "SA_USER_USER_NAME": "email",
    "SA_USER_UNIT": "unit",
    "SA_USER_DEVICE_TYPE": "deviceType",
    "USER_LOGIN_CODE": "code",
    "SA_AUTH_TYPE": "authType",
}

log = logging.getLogger("caixin-weekly")


@dataclass(frozen=True)
class IssueMeta:
    url: str
    issue_code: str
    issue_number: str
    year_issue: str
    issue_date: str
    title: str
    source_line: str
    cover_url: str


@dataclass(frozen=True)
class ArticleCandidate:
    url: str
    section: str
    title: str


@dataclass(frozen=True)
class ParsedArticle:
    title: str
    paragraphs: list[str]
    image_urls: list[str]

    @property
    def body(self) -> str:
        return "\n\n".join(self.paragraphs)


def configure_logging() -> None:
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers.append(logging.FileHandler(logs_dir / f"sync_{datetime.now():%Y%m%d}.log", encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    log.setLevel(logging.INFO)
    log.handlers[:] = handlers


def clean_text(value: str) -> str:
    value = html.unescape(str(value or "")).replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def node_text(node: Any) -> str:
    """Join inline Chinese markup without inventing spaces around links/spans."""
    return clean_text("".join(node.itertext()))


def canonical_article_url(url: str) -> str:
    """Return the stable article URL used for identity and database deduplication."""
    parsed = urlparse(clean_text(url))
    return urlunparse(parsed._replace(query="", fragment=""))


def full_article_url(url: str) -> str:
    """Return Caixin's all-pages view, replacing any existing query string."""
    return canonical_article_url(url) + "?p0"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, path)
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    })
    return session


def _cookie_value(value: Any) -> str:
    """Match js-cookie's encoding closely enough for Caixin's cross-domain cookies."""
    return quote(str(value), safe="!#$&()*+-./:<=>?@[]^_`{|}~")


def _auth_cookie_rows(session: requests.Session) -> list[dict[str, Any]]:
    names = set(LOGIN_COOKIE_FIELDS)
    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or ".caixin.com",
            "path": cookie.path or "/",
            "expires": cookie.expires,
        }
        for cookie in session.cookies
        if cookie.name in names
    ]


def load_auth_cookies(session: requests.Session, path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("cookies 字段不存在")
        for row in rows:
            if not isinstance(row, dict) or row.get("name") not in LOGIN_COOKIE_FIELDS:
                continue
            kwargs: dict[str, Any] = {
                "domain": str(row.get("domain") or ".caixin.com"),
                "path": str(row.get("path") or "/"),
            }
            if row.get("expires") is not None:
                kwargs["expires"] = int(row["expires"])
            session.cookies.set(str(row["name"]), str(row.get("value") or ""), **kwargs)
        return bool(_auth_cookie_rows(session))
    except Exception as exc:
        log.warning("登录 Cookie 文件无效，将重新检查登录态：%s", exc)
        return False


def save_auth_cookies(session: requests.Session, path: Path) -> None:
    rows = _auth_cookie_rows(session)
    if not rows:
        raise RuntimeError("登录成功后没有可持久化的 Cookie")
    payload = {
        "version": 1,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cookies": rows,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def parse_login_jsonp(source: str, callback: str) -> dict[str, Any]:
    match = re.fullmatch(
        rf"\s*{re.escape(callback)}\((.*)\);?\s*",
        source,
        flags=re.S,
    )
    if not match:
        raise ValueError("财新登录接口未返回预期的 JSONP")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("财新登录接口返回值不是 JSON object")
    return payload


def caixin_login_status(session: requests.Session, timeout: float = 30) -> bool:
    """Return False only when Caixin explicitly reports that the session is logged out."""
    try:
        response = session.get(
            USERINFO_URL,
            headers={"Accept": "application/json, text/plain, */*", "Referer": "https://u.caixin.com/"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"财新登录态检查失败，不自动重新登录：{exc}") from exc

    code = payload.get("code")
    if code == 0 and str((payload.get("data") or {}).get("uid") or ""):
        return True
    if code == 600:
        return False
    raise RuntimeError(
        f"财新登录态检查返回未知状态，不自动重新登录：code={code}, msg={payload.get('msg', '')}"
    )


def login_caixin(session: requests.Session, config: dict[str, Any]) -> None:
    callback = f"__caixincallback{int(time.time() * 1000)}"
    params = {
        "account": config["caixin_account"],
        # CAIXIN_PASSWORD is the once-percent-encoded encrypted value. requests
        # encodes the percent signs again, matching the browser's request URL.
        "password": config["caixin_password"],
        "deviceType": config["caixin_device_type"],
        "unit": config["caixin_unit"],
        "device": config["caixin_device"],
        "userTag": "undefined",
        "extend": json.dumps({"resource_article": ""}, separators=(",", ":")),
        "callback": callback,
    }
    response = session.get(
        LOGIN_URL,
        params=params,
        headers={"Accept": "*/*", "Referer": "https://u.caixin.com/"},
        timeout=30,
    )
    response.raise_for_status()
    payload = parse_login_jsonp(response.text, callback)
    if payload.get("code") != 0:
        raise RuntimeError(f"财新登录失败：code={payload.get('code')}, msg={payload.get('msg', '')}")

    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data.get("uid") or not data.get("code") or not data.get("userAuth"):
        raise RuntimeError("财新登录成功响应缺少 uid、code 或 userAuth")
    for cookie_name, field_name in LOGIN_COOKIE_FIELDS.items():
        value = data.get(field_name)
        if value is not None:
            session.cookies.set(
                cookie_name,
                _cookie_value(value),
                domain=".caixin.com",
                path="/",
            )


def ensure_caixin_login(session: requests.Session, config: dict[str, Any]) -> bool:
    cookie_path: Path = config["caixin_cookie_path"]
    loaded = load_auth_cookies(session, cookie_path)
    if caixin_login_status(session):
        log.info("财新登录态有效%s", "，已复用持久化 Cookie" if loaded else "")
        return True

    account = config["caixin_account"]
    password = config["caixin_password"]
    if not account or not password:
        if account or password:
            raise RuntimeError("CAIXIN_ACCOUNT 与 CAIXIN_PASSWORD 必须同时配置")
        log.warning("财新当前未登录，且未配置 CAIXIN_ACCOUNT/CAIXIN_PASSWORD")
        return False

    log.info("财新当前未登录，调用登录接口获取新会话")
    login_caixin(session, config)
    if not caixin_login_status(session):
        raise RuntimeError("财新登录接口返回成功，但新会话仍未通过登录态检查")
    save_auth_cookies(session, cookie_path)
    log.info("财新自动登录成功，Cookie 已持久化到 %s", cookie_path)
    return True


def fetch_html(session: requests.Session, url: str, timeout: float = 30) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _document(source: str):
    return lxml_html.fromstring(source)


def discover_issues(source: str, base_url: str = HOME_URL) -> list[dict[str, str]]:
    """Return issue links in homepage order, newest first."""
    doc = _document(source)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in doc.xpath("//a[@href]"):
        url = urljoin(base_url, str(anchor.get("href") or ""))
        match = ISSUE_URL_RE.match(url.rstrip("/") + "/")
        if not match or url in seen:
            continue
        seen.add(url)
        image = anchor.xpath(".//img[1]")
        out.append({
            "url": url.rstrip("/") + "/",
            "issue_code": match.group(2),
            "title": node_text(anchor),
            "cover_url": urljoin(base_url, image[0].get("src")) if image and image[0].get("src") else "",
        })
    return out


def resolve_issue_url(session: requests.Session, issue: str | None) -> str:
    if issue and issue.startswith(("http://", "https://")):
        if not ISSUE_URL_RE.match(issue.rstrip("/") + "/"):
            raise ValueError(f"不是财新周刊期次 URL: {issue}")
        return issue.rstrip("/") + "/"

    home_source = fetch_html(session, HOME_URL)
    issues = discover_issues(home_source)
    if not issues:
        raise RuntimeError("首页未发现财新周刊期次链接")
    if not issue or issue == "latest":
        return issues[0]["url"]

    normalized = issue.lower().strip().rstrip("/")
    for item in issues:
        if item["issue_code"].lower() == normalized:
            return item["url"]
    raise ValueError(f"首页往期列表中未找到 {issue}；可直接传完整期次 URL")


def _first_text(doc: Any, xpath: str) -> str:
    nodes = doc.xpath(xpath)
    if not nodes:
        return ""
    node = nodes[0]
    return node_text(node) if hasattr(node, "itertext") else clean_text(str(node))


def _first_attr(doc: Any, xpath: str, attr: str) -> str:
    nodes = doc.xpath(xpath)
    return clean_text(nodes[0].get(attr) or "") if nodes else ""


def parse_issue_page(source: str, issue_url: str) -> tuple[IssueMeta, list[ArticleCandidate]]:
    """Parse the cover story and all three category columns in DOM order."""
    doc = _document(source)
    source_line = _first_text(doc, "//*[contains(concat(' ', normalize-space(@class), ' '), ' source ')]")
    date_match = re.search(r"出版日期[：:]?\s*(\d{4}-\d{2}-\d{2})", source_line)
    if not date_match:
        raise ValueError("期次页缺少官方出版日期")
    issue_date = date_match.group(1)
    year_issue_match = re.search(r"(\d{4}年第\s*\d+\s*期)", source_line)
    year_issue = re.sub(r"\s+", "", year_issue_match.group(1)) if year_issue_match else ""
    title = _first_text(doc, "//*[contains(concat(' ', normalize-space(@class), ' '), ' report ')]//*[contains(concat(' ', normalize-space(@class), ' '), ' title ')]")
    number_match = re.search(r"总第\s*(\d+)\s*期", title)
    issue_number = number_match.group(1) if number_match else ""
    url_match = ISSUE_URL_RE.match(issue_url.rstrip("/") + "/")
    issue_code = url_match.group(2) if url_match else f"cw{issue_number}"
    cover_url = _first_attr(
        doc,
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' mainMagContent ')]"
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' cover ')]//img[1]",
        "src",
    )
    if not cover_url:
        cover_url = _first_attr(doc, "//img[contains(@alt, '总第') and contains(@alt, '期')]", "src")
    cover_url = urljoin(issue_url, cover_url) if cover_url else ""

    candidates: list[ArticleCandidate] = []
    seen: set[str] = set()

    def append_links(nodes: Iterable[Any], section: str) -> None:
        for anchor in nodes:
            url = canonical_article_url(urljoin(issue_url, str(anchor.get("href") or "")))
            article_title = node_text(anchor).removeprefix("{{").strip()
            if ARTICLE_URL_RE.match(url) and article_title and url not in seen:
                seen.add(url)
                candidates.append(ArticleCandidate(url=url, section=section, title=article_title))

    append_links(
        doc.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' report ')]//dl//a[@href]"),
        "封面报道Cover Story",
    )

    columns = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' magIntro2 ')]"
        "/*[contains(@class, 'magContent')]"
    )
    for column in columns:
        section = "未分类"
        for child in column:
            classes = set(str(child.get("class") or "").split())
            if "magIntrotit" in classes:
                section = node_text(child) or section
                continue
            if child.tag.lower() == "dl":
                append_links(child.xpath(".//a[@href]"), section)

    if not candidates:
        raise ValueError("期次页未解析到文章目录")
    return (
        IssueMeta(
            url=issue_url,
            issue_code=issue_code,
            issue_number=issue_number,
            year_issue=year_issue,
            issue_date=issue_date,
            title=title or f"《财新周刊》{issue_code}",
            source_line=source_line,
            cover_url=cover_url,
        ),
        candidates,
    )


def _has_excluded_ancestor(node: Any) -> bool:
    for ancestor in [node, *node.iterancestors()]:
        classes = set(str(ancestor.get("class") or "").split())
        if classes & EXCLUDED_PARAGRAPH_CLASSES:
            return True
        ancestor_id = str(ancestor.get("id") or "").lower()
        if any(token in ancestor_id for token in ("pay-layer", "comment", "relate")):
            return True
    return False


def _is_caixin_content_image(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() in {"img.caixin.com", "datanews.caixin.com"}
        and parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        and "/common/images/" not in parsed.path.lower()
    )


def _looks_like_datanews_article(source: str) -> bool:
    return "datanews.caixin.com/mobile/article/article_xy/" in source or "<cxread" in source.lower()


def _needs_datanews_browser_scroll(source: str, parsed: ParsedArticle) -> bool:
    if not _looks_like_datanews_article(source):
        return False
    article_image_refs = source.count("datanews.caixin.com/mobile/article/article_xy/")
    return article_image_refs < 5 or len(parsed.paragraphs) < 10


def _chinese_char_count(value: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", value))


def _is_short_correction(title: str, body: str) -> bool:
    return "编辑更正" in title and bool(body.strip()) and _chinese_char_count(body) >= 20


def _has_active_charge_wall(doc: Any, title: str, body: str) -> bool:
    if _is_short_correction(title, body):
        return False
    return bool(doc.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' payreadwarp ')]"))


def parse_article_page(
    source: str,
    article_url: str,
    min_chars: int = 80,
    *,
    reject_truncated: bool = False,
) -> ParsedArticle:
    """Whitelist the article title, lead media and #Main_Content_Val paragraphs only."""
    doc = _document(source)
    title = _first_text(doc, "//h1[1]")
    if not title:
        title = _first_attr(doc, "//meta[@property='og:title']", "content")
    if not title:
        title = _first_text(doc, "//title[1]")

    roots = doc.xpath("//*[@id='Main_Content_Val']")
    datanews_roots = doc.xpath("//*[@id='mainArticle' and .//cxread]") if not roots else []
    if datanews_roots:
        roots = datanews_roots
    if not roots:
        raise ValueError("正文容器 #Main_Content_Val 不存在，可能未登录或页面结构已变化")
    root = roots[0]
    paragraphs: list[str] = []
    if datanews_roots:
        for node in root.xpath(".//cxread"):
            text = node_text(node)
            if text:
                paragraphs.append(text)
    else:
        for paragraph in root.xpath(".//p"):
            if _has_excluded_ancestor(paragraph):
                continue
            text = node_text(paragraph)
            if text:
                paragraphs.append(text)

    body = "".join(paragraphs)
    has_active_charge_wall = _has_active_charge_wall(doc, title, body)
    if reject_truncated:
        if has_active_charge_wall:
            raise ValueError("HTTP 页面仍有付费墙，需要已登录浏览器获取全文")

    image_urls: list[str] = []
    seen: set[str] = set()
    image_nodes = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' article_media_pic ')]//img"
        " | //*[@id='Main_Content_Val']//img"
        " | //*[@id='intro']//img"
        " | //*[contains(concat(' ', normalize-space(@class), ' '), ' imageBoxG ')]//img"
        " | //img[contains(concat(' ', normalize-space(@class), ' '), ' articleImageB ')]"
    )
    for image in image_nodes:
        candidates = [image.get("data-src"), image.get("src"), image.get("data-original")]
        for raw_url in candidates:
            url = urljoin(article_url, str(raw_url or "").strip())
            if _is_caixin_content_image(url) and url not in seen:
                seen.add(url)
                image_urls.append(url)
                break

    if (
        len(body) < min_chars
        and not _is_short_correction(title, body)
        and not (not body and image_urls and not has_active_charge_wall)
    ):
        visible_page_text = clean_text(" ".join(doc.itertext()))
        marker = next((item for item in PAYWALL_MARKERS if item in visible_page_text), "")
        suffix = f"（检测到：{marker}）" if marker else ""
        raise ValueError(f"正文过短，仅 {len(body)} 字{suffix}")

    return ParsedArticle(title=title, paragraphs=paragraphs, image_urls=image_urls)


class AuthenticatedBrowser:
    """Lazily start one authenticated Chromium and reuse it for the whole run."""

    def __init__(
        self,
        profile_path: str,
        address: str = "",
        content_wait_s: float = 15,
        cookies: Iterable[Any] = (),
    ) -> None:
        self.profile_path = profile_path
        self.address = address
        self.content_wait_s = content_wait_s
        self.cookies = list(cookies)
        self.page: Any = None

    def _ensure_page(self) -> Any:
        if self.page is not None:
            return self.page
        try:
            from DrissionPage import ChromiumOptions, ChromiumPage  # type: ignore
        except ImportError as exc:
            raise RuntimeError("浏览器兜底需要安装 drissionpage") from exc

        options = ChromiumOptions()
        if self.address:
            options.set_address(self.address)
        else:
            if self.profile_path:
                options.set_user_data_path(self.profile_path)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            options.set_address(f"127.0.0.1:{port}")
        self.page = ChromiumPage(options)
        if self.cookies:
            self.page.set.cookies(self.cookies)
        return self.page

    def _scroll_lazy_article(self, page: Any) -> str:
        latest_source = str(page.html or "")
        stable_rounds = 0
        previous_signature = ("", "", "")
        for _ in range(14):
            try:
                page.run_js("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            time.sleep(1)
            latest_source = str(page.html or "")
            signature = (
                str(len(latest_source)),
                str(latest_source.lower().count("<cxread")),
                str(latest_source.count("datanews.caixin.com/mobile/article/article_xy/")),
            )
            if signature == previous_signature:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
                previous_signature = signature
        return latest_source

    def fetch_html(self, url: str) -> str:
        page = self._ensure_page()
        page.get(url)
        deadline = time.monotonic() + max(self.content_wait_s, 1)
        latest_source = ""
        while time.monotonic() < deadline:
            latest_source = str(page.html or "")
            if _looks_like_datanews_article(latest_source):
                latest_source = self._scroll_lazy_article(page)
                try:
                    parse_article_page(latest_source, url, reject_truncated=True)
                    return latest_source
                except ValueError:
                    return latest_source
            try:
                parse_article_page(latest_source, url, reject_truncated=True)
                return latest_source
            except ValueError:
                time.sleep(1)
        return latest_source

    def close(self) -> None:
        if self.page is None:
            return
        try:
            self.page.close()
        except Exception:
            pass
        self.page = None


def fetch_and_parse_article(
    session: requests.Session,
    url: str,
    *,
    browser: AuthenticatedBrowser | None,
) -> ParsedArticle:
    fetch_url = full_article_url(url)
    source = fetch_html(session, fetch_url)
    try:
        parsed = parse_article_page(source, fetch_url, reject_truncated=True)
        if browser is not None and _needs_datanews_browser_scroll(source, parsed):
            raise ValueError("专题页需要浏览器滚动加载完整正文和图片")
        return parsed
    except ValueError as first_error:
        if browser is None:
            raise
        log.warning("HTTP 页面不完整，使用已登录浏览器重试：%s", first_error)
        browser_source = browser.fetch_html(fetch_url)
        try:
            return parse_article_page(browser_source, fetch_url, reject_truncated=True)
        except ValueError as browser_error:
            raise RuntimeError(
                "浏览器页面仍未返回全文。请检查 CAIXIN_ACCOUNT/CAIXIN_PASSWORD 对应账号的"
                "订阅权限，或检查 BROWSER_USER_DATA_PATH/BROWSER_ADDRESS"
            ) from browser_error


def make_llm_client(config: dict[str, Any]):
    from openai import OpenAI  # type: ignore

    kwargs: dict[str, Any] = {
        "api_key": config["api_key"],
        "timeout": config["timeout"],
    }
    if config["base_url"]:
        kwargs["base_url"] = config["base_url"].rstrip("/").removesuffix("/chat/completions")
    return OpenAI(**kwargs)


def extract_json_object(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 未返回 JSON object")
    return json.loads(text[start : end + 1])


def summarize_article(client: Any, config: dict[str, Any], title: str, section: str, body: str) -> str:
    body_cn_count = _chinese_char_count(body)
    if body_cn_count and body_cn_count < 120:
        return clean_text(body)
    min_summary_chars = 40 if body_cn_count < 500 else 300
    max_summary_chars = 350 if body_cn_count < 500 else 750
    length_instruction = (
        "总结为 80-260 个汉字；若原文信息很少，可用一段短摘要概括核心事实。"
        if body_cn_count < 500
        else "总结为 350-650 个汉字。"
    )
    prompt = f"""请为下面的财新周刊中文文章撰写一版适合中文读者阅读的中文总结，只返回严格 JSON。

要求：
1. 仅依据原文，不添加原文没有的数据、背景或判断。
2. 用 3-5 个自然段讲清核心结论、关键事实、因果逻辑和可能影响，不使用标题、列表或编号。
3. 行文要符合中文财经报道和中文读者的表达习惯：语句通顺、衔接自然、信息密度高，避免生硬直译、口号式表达和机械罗列。
4. 先交代文章最重要的结论或变化，再展开关键事实、背景原因、分歧争议、约束条件和后续影响；不要按原文段落顺序逐段复述。
5. 保持必要的限定语，不把作者、受访者或机构观点改写成确定事实；涉及预测、判断、争议和风险时，明确其来源或条件。
6. {length_instruction}不要复述任何网页提示、广告、推荐阅读、订阅信息或评论。

分类：{section}
标题：{title}
原文：
{body[:30000]}

返回格式：{{"summary_md": "三到五段中文总结"}}
"""
    last_error: Exception | None = None
    for attempt in range(config["retries"] + 1):
        try:
            response = client.chat.completions.create(
                model=config["model"],
                messages=[
                    {
                        "role": "system",
                        "content": "你是严谨、老练的中文财经编辑，只能基于用户提供的原文总结，并以自然流畅的中文报道语言表达。",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=config["max_tokens"],
                temperature=config["temperature"],
                response_format={"type": "json_object"},
            )
            payload = extract_json_object(response.choices[0].message.content or "")
            summary = str(payload.get("summary_md") or "").strip()
            cn_count = _chinese_char_count(summary)
            if not min_summary_chars <= cn_count <= max_summary_chars:
                raise ValueError(f"总结字数不合格：{cn_count}")
            if re.search(r"^(?:#|[-*+]\s|\d+[.、)])", summary, flags=re.M):
                raise ValueError("总结包含标题或列表")
            return summary
        except Exception as exc:
            last_error = exc
            if attempt < config["retries"]:
                time.sleep(1)
    raise RuntimeError(f"文章总结失败：{last_error}")


def _mime_for_path(path: Path) -> str:
    return {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")


def analyze_images(client: Any, config: dict[str, Any], title: str, issue_dir: Path, images: list[str]) -> list[dict[str, str]]:
    if not images or not config["analyze_images"]:
        return []
    import base64

    insights: list[dict[str, str]] = []
    for relative in images[: config["max_images"]]:
        path = issue_dir / relative
        if not path.exists():
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        response = client.chat.completions.create(
            model=config["vision_model"] or config["model"],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"文章标题：{title}\n用50-80个汉字客观说明图片内容及其与文章的关系。只返回JSON：{{\"image_type\":\"photo|chart|illustration\",\"description\":\"...\"}}"},
                    {"type": "image_url", "image_url": {"url": f"data:{_mime_for_path(path)};base64,{encoded}"}},
                ],
            }],
            max_tokens=500,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        payload = extract_json_object(response.choices[0].message.content or "")
        description = str(payload.get("description") or "").strip()
        image_type = str(payload.get("image_type") or "illustration").lower()
        if description:
            insights.append({"path": relative, "image_type": image_type, "description": description})
    return insights


def download_image(session: requests.Session, url: str, destination: Path) -> bool:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return False
    atomic_write_bytes(destination, response.content)
    return True


def image_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def materialize_article_images(
    session: requests.Session,
    image_urls: list[str],
    issue_dir: Path,
    article_id: str,
) -> list[str]:
    images: list[str] = []
    images_dir = issue_dir / "images"
    for index, url in enumerate(image_urls, start=1):
        relative = f"images/{article_id}_{index:02d}{image_suffix(url)}"
        destination = issue_dir / relative
        try:
            if download_image(session, url, destination):
                images.append(relative)
        except Exception as exc:
            log.warning("图片下载失败 %s: %s", url, exc)
    return images


def article_record(
    *,
    article_id: str,
    issue_date: str,
    candidate: ArticleCandidate,
    parsed: ParsedArticle,
    summary: str,
    images: list[str],
    image_insights: list[dict[str, str]],
) -> dict[str, Any]:
    paragraphs = [
        {
            "para_id": f"{article_id}_p{index}",
            "en_text": "",
            "zh_text": paragraph,
            "role": "body",
        }
        for index, paragraph in enumerate(parsed.paragraphs, start=1)
    ]
    return {
        "id": article_id,
        "issue_date": issue_date,
        "section": candidate.section,
        "title": parsed.title or candidate.title,
        "title_zh": parsed.title or candidate.title,
        "url": candidate.url,
        "summary_md": summary,
        "content_raw": parsed.body,
        "content_markdown": parsed.body,
        "paragraphs": paragraphs,
        "images": images,
        "image_insights": image_insights,
        "compiled_article": bool(summary),
        "compile_status": "complete" if summary else "source_only",
        "glossary_entries": [],
        "term_annotations": [],
        "glossary_analysis_complete": False,
        "glossary_version": 0,
    }


def paper_article(article: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": article["id"],
        "publication_type": PUBLICATION_TYPE,
        "publication_date": article["issue_date"],
        "source_pdf": PUBLICATION_NAME,
        "page": 0,
        "page_article_index": index,
        "category": article["section"],
        "title": article["title"],
        "title_zh": article["title_zh"],
        "markdown_path": f"articles/{article['id']}.md",
        "summary_md": article["summary_md"],
        "compiled_article": article["compiled_article"],
        "compile_status": article["compile_status"],
        "content_markdown": article["content_markdown"],
        "content_raw": article["content_raw"],
        "paragraphs": article["paragraphs"],
        "images": article["images"],
        "image_insights": article["image_insights"],
        "term_annotations": article["term_annotations"],
        "glossary_analysis_complete": article["glossary_analysis_complete"],
        "glossary_version": article["glossary_version"],
    }


def write_issue_database(output_root: Path, meta: IssueMeta, articles: list[dict[str, Any]]) -> Path:
    issue_dir = output_root / PUBLICATION_TYPE / meta.issue_date
    database_path = issue_dir / "database.js"
    paper_id = f"{PUBLICATION_TYPE}_{meta.issue_date}_caixin-weekly"
    payload = {
        "id": paper_id,
        "publication_type": PUBLICATION_TYPE,
        "publication_date": meta.issue_date,
        "original_filename": f"Caixin Weekly - {meta.issue_date}",
        "issue_title": meta.title,
        "issue_number": meta.issue_number,
        "year_issue": meta.year_issue,
        "source_line": meta.source_line,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cover_image": "cover.jpg" if (issue_dir / "cover.jpg").exists() else "",
        "article_count": len(articles),
        "glossary_version": 0,
        "glossary": {},
        "source_urls": {
            str(article["id"]): str(article.get("url") or "")
            for article in articles
            if article.get("url")
        },
        "articles": [paper_article(article, index) for index, article in enumerate(articles, start=1)],
    }
    text = (
        "window.paper_databases = window.paper_databases || {};\n"
        f"window.paper_databases[{json.dumps(paper_id)}] = "
        f"{json.dumps(payload, ensure_ascii=False, indent=2)};\n"
    )
    atomic_write_text(database_path, text)
    return database_path


def _load_paper_payload(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"window\.paper_databases\[[^\]]+\]\s*=\s*(\{[\s\S]*\})\s*;", text)
        return json.loads(match.group(1)) if match else None
    except Exception:
        return None


def _record_from_paper_article(
    article: dict[str, Any],
    source_urls: dict[str, Any],
) -> dict[str, Any]:
    article_id = str(article.get("id") or "")
    return {
        "id": article_id,
        "issue_date": str(article.get("publication_date") or ""),
        "section": str(article.get("category") or "未分类"),
        "title": str(article.get("title") or ""),
        "title_zh": str(article.get("title_zh") or article.get("title") or ""),
        "url": canonical_article_url(str(source_urls.get(article_id) or "")),
        "summary_md": str(article.get("summary_md") or ""),
        "content_raw": str(article.get("content_raw") or ""),
        "content_markdown": str(article.get("content_markdown") or ""),
        "paragraphs": list(article.get("paragraphs") or []),
        "images": list(article.get("images") or []),
        "image_insights": list(article.get("image_insights") or []),
        "compiled_article": bool(article.get("compiled_article")),
        "compile_status": str(article.get("compile_status") or "source_only"),
        "glossary_entries": [],
        "term_annotations": list(article.get("term_annotations") or []),
        "glossary_analysis_complete": bool(article.get("glossary_analysis_complete")),
        "glossary_version": int(article.get("glossary_version") or 0),
    }


def read_archived_articles(output_root: Path) -> list[dict[str, Any]]:
    """Rebuild incremental state from the per-issue databases used by the frontend."""
    records: list[dict[str, Any]] = []
    for database_path in output_root.glob(f"{PUBLICATION_TYPE}/*/database.js"):
        payload = _load_paper_payload(database_path)
        if not payload:
            continue
        source_urls = payload.get("source_urls") or {}
        for article in payload.get("articles") or []:
            records.append(_record_from_paper_article(article, source_urls))
    return records


def write_database_index(output_root: Path) -> Path:
    items: list[dict[str, Any]] = []
    for database_path in output_root.glob(f"{PUBLICATION_TYPE}/*/database.js"):
        payload = _load_paper_payload(database_path)
        if not payload:
            continue
        articles = payload.get("articles") or []
        items.append({
            "id": payload["id"],
            "publication_type": payload["publication_type"],
            "publication_date": payload["publication_date"],
            "original_filename": payload["original_filename"],
            "issue_title": payload.get("issue_title") or "",
            "issue_number": payload.get("issue_number") or "",
            "year_issue": payload.get("year_issue") or "",
            "source_line": payload.get("source_line") or "",
            "database_path": f"{PUBLICATION_TYPE}/{payload['publication_date']}/database.js",
            "cover_image": payload.get("cover_image") or "",
            "article_count": len(articles),
            "sections": sorted({str(article.get("category") or "未分类") for article in articles}),
            "titles": [article.get("title") for article in articles if article.get("title")],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
    items.sort(key=lambda item: str(item["publication_date"]), reverse=True)
    path = output_root / "database_index.js"
    atomic_write_text(path, "window.paper_db_index = " + json.dumps(items, ensure_ascii=False, indent=2) + ";\n")
    return path


def load_config() -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    output_root = os.getenv("OUTPUT_ROOT", "").strip()
    return {
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "base_url": os.getenv("LLM_BASE_URL", "").strip(),
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        "vision_model": os.getenv("OPENAI_VISION_MODEL", "").strip(),
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "1800") or 1800),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.3") or 0.3),
        "timeout": float(os.getenv("LLM_TIMEOUT_S", "60") or 60),
        "retries": int(os.getenv("LLM_MAX_RETRIES", "2") or 2),
        "analyze_images": os.getenv("LLM_ANALYZE_ARTICLE_IMAGES", "true").lower() in {"1", "true", "yes"},
        "max_images": int(os.getenv("LLM_MAX_IMAGES_PER_ARTICLE", "6") or 6),
        "delay_min": float(os.getenv("CRAWL_DELAY_MIN_S", "2") or 2),
        "delay_max": float(os.getenv("CRAWL_DELAY_MAX_S", "4") or 4),
        "caixin_account": os.getenv("CAIXIN_ACCOUNT", "").strip(),
        "caixin_password": os.getenv("CAIXIN_PASSWORD", "").strip(),
        "caixin_device_type": os.getenv("CAIXIN_DEVICE_TYPE", "5").strip() or "5",
        "caixin_unit": os.getenv("CAIXIN_UNIT", "1").strip() or "1",
        "caixin_device": os.getenv("CAIXIN_DEVICE", "CaixinWebsite").strip() or "CaixinWebsite",
        "caixin_cookie_path": Path(
            os.getenv("CAIXIN_COOKIE_PATH", "").strip() or ROOT / ".caixin-auth.json"
        ).expanduser(),
        "browser_profile": os.getenv("BROWSER_USER_DATA_PATH", "").strip(),
        "browser_address": os.getenv("BROWSER_ADDRESS", "").strip(),
        "browser_content_wait_s": float(os.getenv("BROWSER_CONTENT_WAIT_S", "15") or 15),
        "output_root": Path(output_root) if output_root else ROOT / "output_results",
    }


def _next_sequence(articles: list[dict[str, Any]], issue_date: str) -> int:
    values: list[int] = []
    for article in articles:
        match = re.search(rf"^art_{re.escape(issue_date)}_(\d+)$", str(article.get("id") or ""))
        if match:
            values.append(int(match.group(1)))
    return max(values, default=0) + 1


def select_candidates(
    candidates: list[ArticleCandidate],
    sections: str = "",
    per_section: int = 0,
) -> list[ArticleCandidate]:
    requested = [clean_text(item) for item in sections.split(",") if clean_text(item)]
    if not requested:
        return list(candidates)
    counts = {section: 0 for section in requested}
    selected: list[ArticleCandidate] = []
    for candidate in candidates:
        match = next(
            (
                section
                for section in requested
                if candidate.section == section
                or candidate.section.startswith(section)
                or section in candidate.section
            ),
            None,
        )
        if match is None or (per_section > 0 and counts[match] >= per_section):
            continue
        counts[match] += 1
        selected.append(candidate)
    missing = [section for section, count in counts.items() if count == 0]
    if missing:
        raise ValueError("目录中未找到分类：" + "、".join(missing))
    return selected


def process_issue(args: argparse.Namespace, config: dict[str, Any]) -> list[dict[str, Any]]:
    session = request_session()
    issue_url = resolve_issue_url(session, args.issue)
    issue_source = fetch_html(session, issue_url)
    meta, candidates = parse_issue_page(issue_source, issue_url)
    total_candidates = len(candidates)
    candidates = select_candidates(candidates, args.sections, args.per_section)
    if args.limit > 0:
        candidates = candidates[: args.limit]
    log.info(
        "期次 %s（%s），目录共 %d 篇%s",
        meta.title,
        meta.issue_date,
        total_candidates,
        f"，本次处理前 {len(candidates)} 篇" if len(candidates) < total_candidates else "",
    )

    if args.dry_run:
        for candidate in candidates:
            print(f"[DRY] {meta.issue_date}  {candidate.section:28s}  {candidate.title}  {candidate.url}")
        return []

    if not args.no_summary and not config["api_key"]:
        raise RuntimeError("未配置 LLM_API_KEY；可先使用 --no-summary 验证抓取")

    authenticated = ensure_caixin_login(session, config)

    output_root: Path = config["output_root"]
    issue_dir = output_root / PUBLICATION_TYPE / meta.issue_date
    if meta.cover_url:
        try:
            download_image(session, meta.cover_url, issue_dir / "cover.jpg")
        except Exception as exc:
            log.warning("封面下载失败：%s", exc)

    existing = read_archived_articles(output_root)
    by_url = {str(article.get("url") or ""): article for article in existing}
    issue_articles = [article for article in existing if article.get("issue_date") == meta.issue_date]
    sequence = _next_sequence(existing, meta.issue_date)
    client = make_llm_client(config) if not args.no_summary else None
    new_articles: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    browser = None
    if authenticated or config["browser_profile"] or config["browser_address"]:
        browser = AuthenticatedBrowser(
            config["browser_profile"] or str(ROOT / ".caixin-browser"),
            config["browser_address"],
            config["browser_content_wait_s"],
            _auth_cookie_rows(session),
        )

    try:
        for candidate in candidates:
            if candidate.url in by_url and not args.force:
                log.info("已存在，跳过：%s", candidate.title)
                continue
            log.info("抓取：[%s] %s", candidate.section, candidate.title)
            try:
                parsed = fetch_and_parse_article(session, candidate.url, browser=browser)
                article_id = f"art_{meta.issue_date}_{sequence:03d}"
                sequence += 1
                images = materialize_article_images(session, parsed.image_urls, issue_dir, article_id)
                summary = (
                    ""
                    if client is None or not parsed.body
                    else summarize_article(client, config, parsed.title, candidate.section, parsed.body)
                )
                image_insights = [] if client is None else analyze_images(client, config, parsed.title, issue_dir, images)
                record = article_record(
                    article_id=article_id,
                    issue_date=meta.issue_date,
                    candidate=candidate,
                    parsed=parsed,
                    summary=summary,
                    images=images,
                    image_insights=image_insights,
                )
                if candidate.url in by_url:
                    existing = [item for item in existing if item.get("url") != candidate.url]
                    issue_articles = [item for item in issue_articles if item.get("url") != candidate.url]
                existing.append(record)
                issue_articles.append(record)
                by_url[candidate.url] = record
                new_articles.append(record)
                write_issue_database(output_root, meta, sorted(issue_articles, key=lambda item: item["id"]))
                write_database_index(output_root)
                log.info("已落库：%s（正文 %d 字，图片 %d 张）", article_id, len(parsed.body), len(images))
            except RuntimeError as exc:
                failures.append((candidate.url, str(exc)))
                log.error("单篇处理失败，继续下一篇：%s", exc)
            except Exception as exc:
                failures.append((candidate.url, str(exc)))
                log.error("单篇处理失败，继续下一篇：%s", exc)
            if config["delay_max"] > 0:
                time.sleep(random.uniform(config["delay_min"], config["delay_max"]))
    finally:
        if browser is not None:
            browser.close()

    write_issue_database(output_root, meta, sorted(issue_articles, key=lambda item: item["id"]))
    write_database_index(output_root)
    if failures:
        sample = "; ".join(f"{url}: {error}" for url, error in failures[:3])
        raise RuntimeError(f"本期有 {len(failures)} 篇失败；已成功文章均已落盘。{sample}")
    return new_articles


def process_single_url(args: argparse.Namespace, config: dict[str, Any]) -> list[dict[str, Any]]:
    session = request_session()
    authenticated = ensure_caixin_login(session, config)
    browser = None
    if authenticated or config["browser_profile"] or config["browser_address"]:
        browser = AuthenticatedBrowser(
            config["browser_profile"] or str(ROOT / ".caixin-browser"),
            config["browser_address"],
            config["browser_content_wait_s"],
            _auth_cookie_rows(session),
        )
    try:
        parsed = fetch_and_parse_article(session, canonical_article_url(args.single_url), browser=browser)
    finally:
        if browser is not None:
            browser.close()
    if args.extract_json:
        print(json.dumps({
            "title": parsed.title,
            "paragraphs": parsed.paragraphs,
            "image_urls": parsed.image_urls,
        }, ensure_ascii=False, indent=2))
        return []
    raise ValueError("单篇入库需要期次元数据；调试请配合 --extract-json")


def import_extracted(args: argparse.Namespace, config: dict[str, Any]) -> list[dict[str, Any]]:
    source_path = Path(args.import_extracted)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    meta_raw = payload.get("issue") or {}
    rows = payload.get("articles") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("导入文件没有 articles")
    meta = IssueMeta(
        url=str(meta_raw.get("url") or ""),
        issue_code=str(meta_raw.get("issue_code") or ""),
        issue_number=str(meta_raw.get("issue_number") or ""),
        year_issue=str(meta_raw.get("year_issue") or ""),
        issue_date=str(meta_raw.get("issue_date") or ""),
        title=str(meta_raw.get("title") or "财新周刊"),
        source_line=str(meta_raw.get("source_line") or ""),
        cover_url=str(meta_raw.get("cover_url") or ""),
    )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta.issue_date):
        raise ValueError("导入文件缺少合法 issue.issue_date")
    if not args.no_summary and not config["api_key"]:
        raise RuntimeError("未配置 LLM_API_KEY")

    session = request_session()
    output_root: Path = config["output_root"]
    issue_dir = output_root / PUBLICATION_TYPE / meta.issue_date
    if meta.cover_url:
        try:
            download_image(session, meta.cover_url, issue_dir / "cover.jpg")
        except Exception as exc:
            log.warning("封面下载失败：%s", exc)
    existing = read_archived_articles(output_root)
    by_url = {str(article.get("url") or ""): article for article in existing}
    issue_articles = [article for article in existing if article.get("issue_date") == meta.issue_date]
    sequence = _next_sequence(existing, meta.issue_date)
    client = make_llm_client(config) if not args.no_summary else None
    imported: list[dict[str, Any]] = []

    for row in rows:
        candidate = ArticleCandidate(
            url=canonical_article_url(str(row.get("url") or "")),
            section=str(row.get("section") or "未分类"),
            title=str(row.get("title") or ""),
        )
        if candidate.url in by_url and not args.force:
            log.info("已存在，跳过：%s", candidate.title)
            continue
        parsed = ParsedArticle(
            title=candidate.title,
            paragraphs=[clean_text(item) for item in row.get("paragraphs") or [] if clean_text(item)],
            image_urls=[str(item) for item in row.get("image_urls") or [] if str(item).strip()],
        )
        if not parsed.paragraphs:
            log.warning("导入正文为空，跳过：%s", candidate.url)
            continue
        article_id = f"art_{meta.issue_date}_{sequence:03d}"
        sequence += 1
        images = materialize_article_images(session, parsed.image_urls, issue_dir, article_id)
        summary = "" if client is None else summarize_article(client, config, parsed.title, candidate.section, parsed.body)
        image_insights = [] if client is None else analyze_images(client, config, parsed.title, issue_dir, images)
        record = article_record(
            article_id=article_id,
            issue_date=meta.issue_date,
            candidate=candidate,
            parsed=parsed,
            summary=summary,
            images=images,
            image_insights=image_insights,
        )
        if candidate.url in by_url:
            existing = [item for item in existing if item.get("url") != candidate.url]
            issue_articles = [item for item in issue_articles if item.get("url") != candidate.url]
        existing.append(record)
        issue_articles.append(record)
        by_url[candidate.url] = record
        imported.append(record)
        write_issue_database(output_root, meta, sorted(issue_articles, key=lambda item: item["id"]))
        write_database_index(output_root)
        log.info("已导入并落库：%s（正文 %d 字，图片 %d 张）", article_id, len(parsed.body), len(images))
    return imported


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="财新周刊抓取、总结与归档")
    parser.add_argument("--issue", default="latest", help="latest、cw1218 或完整期次 URL")
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理前 N 篇，0 表示不限")
    parser.add_argument("--sections", default="", help="逗号分隔的分类名或中文前缀")
    parser.add_argument("--per-section", type=int, default=0, help="每个指定分类最多处理 N 篇")
    parser.add_argument("--dry-run", action="store_true", help="只解析目录，不抓正文或写盘")
    parser.add_argument("--no-summary", action="store_true", help="只保存中文原文和图片，不调用 LLM")
    parser.add_argument("--force", action="store_true", help="覆盖已存在 URL")
    parser.add_argument("--single-url", help="调试单篇正文 URL")
    parser.add_argument("--extract-json", action="store_true", help="将单篇清洗结果输出为 JSON，不入库")
    parser.add_argument("--login", action="store_true", help="打开独立 Chrome profile 的财新首页，供首次登录")
    parser.add_argument("--import-extracted", help="导入已登录浏览器提取的正文 JSON 并总结落库")
    return parser.parse_args()


def login_browser(config: dict[str, Any]) -> None:
    if not config["browser_profile"] and not config["browser_address"]:
        raise ValueError("请先在 .env 配置 BROWSER_USER_DATA_PATH 或 BROWSER_ADDRESS")
    browser = AuthenticatedBrowser(
        config["browser_profile"],
        config["browser_address"],
        config["browser_content_wait_s"],
    )
    page = browser._ensure_page()
    page.get(HOME_URL)
    print("财新登录页面已打开。请在该 Chrome 中完成登录，然后按 Enter 关闭浏览器。")
    input()
    browser.close()


def main() -> int:
    configure_logging()
    args = parse_args()
    config = load_config()
    try:
        if args.login:
            login_browser(config)
        elif args.import_extracted:
            imported = import_extracted(args, config)
            log.info("完成，本次导入 %d 篇", len(imported))
        elif args.single_url:
            process_single_url(args, config)
        else:
            new_articles = process_issue(args, config)
            log.info("完成，本次新增/重写 %d 篇", len(new_articles))
        return 0
    except KeyboardInterrupt:
        log.warning("用户中断；已完成文章均已落盘")
        return 130
    except Exception as exc:
        log.error("失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

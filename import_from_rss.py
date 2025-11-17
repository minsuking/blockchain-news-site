import os
import re
import textwrap
from datetime import datetime
from urllib.parse import urlparse
import requests
import feedparser
from bs4 import BeautifulSoup

# ===== 설정 =====
RSS_URL = "https://your-blog-domain.com/rss"  # 🔹 네 블로그 RSS 주소로 변경
CONTENT_BASE = "content/news"
IMAGE_BASE = "static/images/news"
DEFAULT_CATEGORY = "rss-import"  # 카테고리 기본값
TIME_SUFFIX = "T09:00:00+09:00"  # 한국 기준 고정 시간
# =================


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9가-힣\- ]", "", text)
    text = text.replace(" ", "-")
    return text[:60]


def ensure_dir(path: str):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def extract_image_url(entry) -> str | None:
    """
    RSS 안에서 이미지 후보를 찾는 함수.
    - <media:content>, <enclosure>, description 안의 <img>, content 안의 <img>
    """
    # 1) media:content / enclosure
    media_thumbnail = entry.get("media_thumbnail") or entry.get("media_content")
    if media_thumbnail:
        try:
            return media_thumbnail[0].get("url")
        except Exception:
            pass

    if "enclosures" in entry and entry.enclosures:
        for enc in entry.enclosures:
            url = enc.get("href") or enc.get("url")
            if url:
                return url

    # 2) description / summary 속 HTML 안의 <img>
    html_candidates = []
    if "description" in entry:
        html_candidates.append(entry.description)
    if "summary" in entry and entry.summary not in html_candidates:
        html_candidates.append(entry.summary)
    if "content" in entry:
        for c in entry.content:
            html_candidates.append(c.value)

    for html in html_candidates:
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]

    return None


def clean_html_to_markdown(html: str) -> str:
    """
    최소한만 HTML 태그 제거해서 Hugo markdown으로 쓸 수 있게 정리.
    (완전한 markdown 변환이 필요한 경우는 나중에 별도 처리)
    """
    soup = BeautifulSoup(html, "html.parser")

    # 줄바꿈 태그 처리
    for br in soup.find_all(["br", "hr"]):
        br.replace_with("\n")

    text = soup.get_text("\n")
    # 연속 공백/줄바꿈 정리
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n\n".join(lines)


def main():
    print(f"[INFO] RSS 가져오는 중: {RSS_URL}")
    feed = feedparser.parse(RSS_URL)

    if feed.bozo:
        print("[ERROR] RSS 파싱에 실패했습니다. RSS URL을 다시 확인하세요.")
        return

    print(f"[INFO] RSS 제목: {feed.feed.get('title', '제목 없음')}")
    print(f"[INFO] 항목 개수: {len(feed.entries)}")

    for entry in feed.entries:
        # 제목
        title = entry.get("title", "제목 없음").strip()
        # 링크(출처)
        link = entry.get("link", "").strip()

        # 날짜
        if "published_parsed" in entry and entry.published_parsed:
            dt = datetime(*entry.published_parsed[:6])
        elif "updated_parsed" in entry and entry.updated_parsed:
            dt = datetime(*entry.updated_parsed[:6])
        else:
            dt = datetime.now()

        date_str = dt.strftime("%Y-%m-%d")
        year = dt.strftime("%Y")
        month = dt.strftime("%m")

        # slug 생성
        slug_base = slugify(title) or "untitled"
        slug = f"{date_str}-{slug_base}"

        # 폴더 경로
        content_dir = os.path.join(CONTENT_BASE, year, month)
        ensure_dir(content_dir)
        md_path = os.path.join(content_dir, f"{slug}.md")

        if os.path.exists(md_path):
            print(f"[SKIP] 이미 존재: {md_path}")
            continue

        # 본문 (content > summary > description 순서로)
        body_html = ""
        if "content" in entry and entry.content:
            body_html = entry.content[0].value
        elif "summary" in entry:
            body_html = entry.summary
        elif "description" in entry:
            body_html = entry.description
        else:
            body_html = ""

        body_text = clean_html_to_markdown(body_html)

        # 대표 이미지 URL
        img_url = extract_image_url(entry)
        featured_image = ""

        if img_url:
            try:
                parsed = urlparse(img_url)
                ext = os.path.splitext(parsed.path)[1]
                if ext.lower() not in [".jpg", ".jpeg", ".png", ".webp"]:
                    ext = ".jpg"
                img_dir = os.path.join(IMAGE_BASE, year, month)
                ensure_dir(img_dir)
                img_filename = slug + ext
                img_path = os.path.join(img_dir, img_filename)

                print(f"[IMG] 다운로드: {img_url} -> {img_path}")
                r = requests.get(img_url, timeout=10)
                if r.status_code == 200:
                    with open(img_path, "wb") as f:
                        f.write(r.content)
                    featured_image = f"/images/news/{year}/{month}/{img_filename}"
                else:
                    print(f"[WARN] 이미지 다운로드 실패 status={r.status_code}")
            except Exception as e:
                print(f"[WARN] 이미지 처리 중 오류: {e}")

        # front matter 작성
        front_matter = f"""---
title: "{title.replace('"', '\\"')}"
date: {date_str}{TIME_SUFFIX}
lastmod: {date_str}{TIME_SUFFIX}
draft: false
categories: ["{DEFAULT_CATEGORY}"]
tags: []
summary: ""
sourceUrl: "{link}"
"""

        if featured_image:
            front_matter += f'featuredImage: "{featured_image}"\n'

        front_matter += "---\n\n"

        full_content = front_matter + body_text + "\n"

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(full_content)

        print(f"[OK] 생성: {md_path}")

    print("[DONE] RSS → Hugo 변환 완료")


if __name__ == "__main__":
    main()

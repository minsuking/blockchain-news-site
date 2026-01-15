import os
import re
import json
from datetime import datetime
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

# ===== 환경 변수(.env) 로드 & OpenAI 클라이언트 =====
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ===== 설정 =====
WP_API_BASE = os.getenv("WP_API_BASE")  # 예: https://도메인/wp-json/wp/v2/posts
CONTENT_BASE = "content/news"
IMAGE_BASE = "static/images/news"
DEFAULT_CATEGORY = "블록체인"           # 🔹 카테고리 고정
TIME_SUFFIX = "T09:00:00+09:00"         # 한국 시간 기준 고정
MAX_POSTS = 100                          # 최대 가져올 포스트 수
PER_PAGE = 50                           # WP API per_page 최대 100
# =================


def slugify(text: str) -> str:
    """제목 기반 slug 생성 (한글+영문+숫자만, 공백은 -)"""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9가-힣\- ]", "", text)
    text = text.replace(" ", "-")
    return text[:60]


def ensure_dir(path: str):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def clean_html_to_markdown(html: str) -> str:
    """본문 HTML 최소 정리"""
    soup = BeautifulSoup(html, "html.parser")

    for br in soup.find_all(["br", "hr"]):
        br.replace_with("\n")

    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n\n".join(lines)


def extract_first_image_from_html(html: str, base_url: str | None = None) -> str | None:
    """content.rendered 안에 <img>가 있을 경우 첫 이미지 반환"""
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img")
    if img and img.get("src"):
        src = img["src"]
        if base_url and (src.startswith("/") or not src.startswith("http")):
            return urljoin(base_url, src)
        return src
    return None


def extract_featured_image_from_post(post: dict, content_html: str, base_url: str | None = None) -> str | None:
    """
    1순위: REST API의 _embedded.wp:featuredmedia.source_url
    2순위: content.rendered 안의 첫 번째 <img>
    """
    try:
        embedded = post.get("_embedded", {})
        media_list = embedded.get("wp:featuredmedia")
        if isinstance(media_list, list) and media_list:
            media = media_list[0]
            url = media.get("source_url")
            if not url:
                url = (
                    media.get("media_details", {})
                    .get("sizes", {})
                    .get("full", {})
                    .get("source_url")
                )
            if url:
                return url
    except Exception:
        pass

    # fallback: content 안에서 <img> 찾기
    return extract_first_image_from_html(content_html, base_url)


def rewrite_with_openai(title: str, content: str) -> tuple[str, str]:
    """
    뉴스 원문을 참고자료로 사용하여
    블로그형 해설 + 개인 의견이 포함된 글로 재구성
    """
    prompt = f"""
아래 뉴스 콘텐츠는 '참고 자료'일 뿐입니다.
원문을 그대로 재작성하거나 요약하지 말고,
블로그 글 작성자의 관점에서 새롭게 해석하고 설명하는 글을 작성해주세요.

[참고 뉴스 제목]
{title}

[참고 뉴스 내용]
{content}

작성 목표:
- 뉴스 복제/요약/의역 ❌
- 개인 블로그 해설 글 ✅
- "이 이슈를 이렇게 본다"라는 관점이 드러나야 함

글 구성 가이드:
1. 도입부
   - 최근 이슈에 대한 개인적인 문제의식 또는 관심 포인트 제시
2. 이슈 정리
   - 뉴스 내용을 직접 베끼지 말고, 핵심 맥락만 '풀이하듯' 설명
3. 개인 해석
   - 이 사안이 왜 중요하다고 보는지
   - 시장/개인/독자에게 어떤 의미가 있는지
4. 마무리
   - 개인적인 전망, 우려, 혹은 독자에게 던지는 질문

스타일 가이드:
- 1인칭 시점 허용 ("개인적으로", "내가 보기엔" 등)
- 기자 문체 ❌
- 블로그 칼럼 톤 ⭕
- 문장 구조는 원문과 전혀 달라야 함
- 뉴스 문장, 수치, 표현 그대로 사용하지 말 것

SEO 가이드:
- 자연스럽게 키워드는 녹이되
- 검색용 문장 느낌 ❌
- 사람 글 느낌 ⭕
- AI 티 나는 표현 금지

반환 형식(JSON):
{{
  "title": "블로그용 새 제목",
  "content": "블로그 형식의 본문"
}}
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )

        msg = resp.choices[0].message

        if isinstance(msg.content, list):
            content_str = "".join(
                getattr(part, "text", str(part)) for part in msg.content
            )
        else:
            content_str = msg.content

        data = json.loads(content_str)

        new_title = data.get("title", title).strip()
        new_content = data.get("content", content).strip()

        return new_title, new_content

    except Exception as e:
        print("[WARN] OpenAI 재작성 실패:", e)
        return title, content



def fetch_wp_posts(max_posts: int = MAX_POSTS, per_page: int = PER_PAGE) -> list[dict]:
    """
    WP REST API에서 posts JSON을 최대 max_posts까지 가져온다.
    _embed=1을 붙여서 대표 이미지 정보까지 가져온다.
    """
    collected: list[dict] = []
    page = 1

    if not WP_API_BASE:
        raise RuntimeError("WP_API_BASE 환경 변수가 설정되어 있지 않습니다.")

    while len(collected) < max_posts:
        params = {"per_page": per_page, "page": page, "_embed": "1"}
        print(f"[INFO] WP posts 요청: page={page}, per_page={per_page}")
        resp = requests.get(WP_API_BASE, params=params, timeout=10)

        if resp.status_code != 200:
            print(f"[WARN] WP API 요청 실패 status={resp.status_code}")
            break

        items = resp.json()
        if not items:
            print("[INFO] 더 이상 가져올 포스트가 없습니다.")
            break

        collected.extend(items)
        if len(items) < per_page:
            break

        page += 1

    return collected[:max_posts]


def main():
    print(f"[INFO] WP JSON에서 포스트 가져오는 중: {WP_API_BASE}")
    posts = fetch_wp_posts(MAX_POSTS, PER_PAGE)
    print(f"[INFO] 총 가져온 포스트 수: {len(posts)}")

    for post in posts:
        # 원 제목
        raw_title = post.get("title", {}).get("rendered", "") or "제목 없음"
        orig_title = BeautifulSoup(raw_title, "html.parser").get_text().strip()

        # 링크 (이미지 절대 경로 계산에만 사용)
        link = post.get("link", "").strip()

        # 날짜
        raw_date = post.get("date") or post.get("date_gmt") or ""
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", ""))
        except Exception:
            dt = datetime.now()

        date_str = dt.strftime("%Y-%m-%d")
        year = dt.strftime("%Y")
        month = dt.strftime("%m")

        # slug (원래 제목 기준으로 만드는 게 안전)
        slug_base = slugify(orig_title) or "untitled"
        slug = f"{date_str}-{slug_base}"

        # 경로
        content_dir = os.path.join(CONTENT_BASE, year, month)
        ensure_dir(content_dir)
        md_path = os.path.join(content_dir, f"{slug}.md")

        if os.path.exists(md_path):
            print(f"[SKIP] 이미 존재: {md_path}")
            continue

        # 본문 HTML
        raw_content_html = (
            post.get("content", {}).get("rendered", "")
            or post.get("excerpt", {}).get("rendered", "")
            or ""
        )

        body_text = clean_html_to_markdown(raw_content_html)

        # 🔹 OpenAI로 제목+본문 재작성
        new_title, new_body = rewrite_with_openai(orig_title, body_text)
        title = new_title
        body_text = new_body
        print(f"[AI] 제목 재작성: '{orig_title}'  →  '{title}'")

        # 🔹 대표 이미지 추출 (REST API + fallback)
        img_url = extract_featured_image_from_post(post, raw_content_html, base_url=link)
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

                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                }
                r = requests.get(img_url, headers=headers, timeout=10)
                print(f"[IMG] status={r.status_code}")

                if r.status_code == 200:
                    with open(img_path, "wb") as f:
                        f.write(r.content)
                    # Newsroom 테마용: image: "news/2025/11/파일명"
                    featured_image = f"news/{year}/{month}/{img_filename}"
                else:
                    print(f"[WARN] 이미지 다운로드 실패 status={r.status_code}")
            except Exception as e:
                print(f"[WARN] 이미지 처리 중 오류: {e}")

        # front matter
        safe_title = title.replace('"', '\\"')

        front_matter = "---\n"
        front_matter += f'title: "{safe_title}"\n'
        front_matter += f"date: {date_str}{TIME_SUFFIX}\n"
        front_matter += f"lastmod: {date_str}{TIME_SUFFIX}\n"
        front_matter += "draft: false\n"
        front_matter += f'categories: ["{DEFAULT_CATEGORY}"]\n'
        front_matter += "tags: []\n"
        front_matter += 'summary: ""\n'
        if featured_image:
            front_matter += f'image: "{featured_image}"\n'
        front_matter += "---\n\n"

        full_content = front_matter + body_text + "\n"

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(full_content)

        print(f"[OK] 생성: {md_path}")

    print("[DONE] WP JSON → Hugo 변환 완료")


if __name__ == "__main__":
    main()

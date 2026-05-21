import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Download, Page, async_playwright

START_URL = "https://etd.xjtlu.edu.cn/index.html#/index"
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "Download"
PROFILE_DIR = BASE_DIR / ".browser_profile"
DEFAULT_CONFIG = BASE_DIR / "downloader.config.json"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

AUTH_HOST_HINTS = (
    "trust.xjtlu.edu.cn",
    "uim.xjtlu.edu.cn",
    "idp.xjtlu.edu.cn",
)

PDF_URL_HINTS = (
    ".pdf",
    "browserfile",
    "download",
    "bitstream",
    "filedownload",
    "file/download",
)


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    if not name:
        name = "unknown_paper"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def pick_filename_from_headers(headers: dict) -> str:
    cd = headers.get("content-disposition", "") or headers.get("Content-Disposition", "")
    if not cd:
        return "unknown_paper.pdf"

    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;\n]+)", cd)
    if m:
        return safe_name(unquote(m.group(1).strip().strip('"')))

    m = re.search(r'filename="?([^";\n]+)"?', cd)
    if m:
        return safe_name(unquote(m.group(1).strip().strip('"')))

    return "unknown_paper.pdf"


def unique_path(filename: str) -> Path:
    path = DOWNLOAD_DIR / safe_name(filename)
    i = 1
    while path.exists():
        path = DOWNLOAD_DIR / f"{path.stem}({i}){path.suffix}"
        i += 1
    return path


async def save_pdf_response(resp, downloaded_urls: set[str]) -> Path | None:
    url = resp.url
    if url in downloaded_urls or resp.status != 200:
        return None

    ct = (resp.headers.get("content-type", "") or "").lower()
    url_l = url.lower()
    if ("pdf" not in ct) and not any(hint in url_l for hint in PDF_URL_HINTS):
        return None

    try:
        body = await resp.body()
    except Exception:
        return None

    if not (body and body.startswith(b"%PDF")):
        return None

    downloaded_urls.add(url)
    path = unique_path(pick_filename_from_headers(resp.headers))
    path.write_bytes(body)
    print(f"[Saved] {path}")
    return path


async def save_playwright_download(download: Download) -> None:
    filename = download.suggested_filename or "unknown_paper.pdf"
    path = unique_path(filename)
    await download.save_as(path)
    print(f"[Saved] {path}")


async def body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


async def settle(page: Page, ms: int = 1200) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


def route_from_redirect(url: str) -> str | None:
    if "#" not in url:
        return None
    hash_part = url.split("#", 1)[1]
    parsed = urlparse(hash_part)
    redirect = parse_qs(parsed.query).get("redirect", [None])[0]
    if not redirect:
        return None
    return unquote(redirect)


async def goto_current_base_route(page: Page, route: str) -> None:
    base_url = page.url.split("#", 1)[0]
    await page.goto(f"{base_url}#{route}", wait_until="domcontentloaded", timeout=60000)
    await settle(page)


async def is_auth_page(page: Page) -> bool:
    urls = [page.url, *(frame.url for frame in page.frames)]
    if any(any(host in url.lower() for host in AUTH_HOST_HINTS) for url in urls):
        return True

    text = (await body_text(page)).lower()
    if "authentication centre" in text or "account sign in" in text or "forgot password" in text:
        return True

    try:
        return await page.locator("input[type=password]").count() > 0
    except Exception:
        return False


async def wait_for_login(page: Page, timeout_ms: int) -> None:
    await settle(page)
    if not await is_auth_page(page):
        return

    print("[Login] Complete XJTLU login in the browser. The script will continue afterwards.")
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        if not await is_auth_page(page):
            await settle(page, 3000)
            return
        await asyncio.sleep(1)

    raise TimeoutError(f"Login timed out. Current page: {page.url}")


async def click_visible_text(page: Page, text: str, exact: bool = True) -> bool:
    locators = [
        page.get_by_role("button", name=re.compile(f"^{re.escape(text)}$" if exact else re.escape(text), re.I)),
        page.get_by_text(text, exact=exact),
    ]

    for locator in locators:
        try:
            count = await locator.count()
        except Exception:
            continue

        for index in range(count):
            item = locator.nth(index)
            try:
                if await item.is_visible(timeout=1000):
                    await item.scroll_into_view_if_needed(timeout=3000)
                    await item.click(timeout=5000)
                    await settle(page)
                    return True
            except Exception:
                continue
    return False


async def click_card_by_text(page: Page, text: str) -> bool:
    card = page.locator(".paper-item").filter(has_text=re.compile(re.escape(text), re.I)).first
    if await card.count() > 0:
        await card.scroll_into_view_if_needed(timeout=3000)
        box = await card.bounding_box()
        if box:
            print(f"[Debug] Card box: x={box['x']:.0f}, y={box['y']:.0f}, w={box['width']:.0f}, h={box['height']:.0f}")
        try:
            await card.click(timeout=5000, force=True)
        except Exception:
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await settle(page)
        if "User Agreement" in await body_text(page) or "Policy/EXAMXJTLU" in page.url:
            return True

    clicked = await page.evaluate(
        """label => {
            const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
            const cards = [...document.querySelectorAll('.paper-item')];
            const card = cards.find(el => normalize(el.innerText || el.textContent).includes(label));
            if (!card) return false;
            card.scrollIntoView({block: 'center', inline: 'center'});
            for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                card.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            }
            return true;
        }""",
        text,
    )
    await settle(page)
    return bool(clicked)


async def ensure_logged_in_from_home(page: Page, login_timeout: int) -> None:
    print("[Step] Opening home page")
    await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
    await wait_for_login(page, login_timeout * 1000)

    text = await body_text(page)
    if "Login Successfully" in text:
        print("[Step] Already logged in")
        return

    if "User Login" in text or re.search(r"\bLogin\b", text):
        print("[Step] Clicking Login")
        if not await click_visible_text(page, "Login", exact=True):
            raise RuntimeError("Login button was not found on the home page.")
        await wait_for_login(page, login_timeout * 1000)

    text = await body_text(page)
    if "Login Successfully" not in text:
        print("[Warn] Home page does not show 'Login Successfully'; continuing anyway.")


async def find_exam_page(page: Page) -> Page | None:
    for candidate in reversed(page.context.pages):
        try:
            text = await body_text(candidate)
        except Exception:
            continue
        if "User Agreement" in text:
            return candidate
        if "Paper Code" in text and await candidate.locator(".paper-search input[type=text], input[type=text]").count() > 0:
            return candidate
    return None


async def close_stale_exam_pages(context, keep_page: Page) -> None:
    for candidate in list(context.pages):
        if candidate == keep_page:
            continue
        try:
            text = await body_text(candidate)
            url = candidate.url
        except Exception:
            continue

        if (
            "User Agreement" in text
            or "Paper Code" in text
            or "/Policy/EXAMXJTLU" in url
            or "/SearchDetail/EXAMXJTLU" in url
        ):
            try:
                await candidate.close()
            except Exception:
                pass


async def wait_for_any_exam_page(page: Page, timeout_ms: int = 12000) -> Page:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        candidate = await find_exam_page(page)
        if candidate is not None:
            if candidate != page:
                print(f"[Step] Switched to exam page: {candidate.url}")
            return candidate
        await page.wait_for_timeout(500)

    text = (await body_text(page)).replace("\n", " ")[:500]
    raise RuntimeError(f"Past Exam Papers click did not open the agreement/search page. Current URL: {page.url}. Text: {text}")


async def open_past_exam_papers(page: Page) -> Page:
    text = await body_text(page)
    if "User Agreement" in text or "Paper Code" in text:
        return page

    await close_stale_exam_pages(page.context, page)
    print("[Step] Opening Past Exam Papers")
    clicked = await click_card_by_text(page, "Past Exam Papers")
    if not clicked:
        clicked = await click_visible_text(page, "Past Exam Papers", exact=True)
    try:
        return await wait_for_any_exam_page(page, timeout_ms=12000)
    except RuntimeError:
        if not clicked:
            raise RuntimeError("Past Exam Papers entry was not found.")
        raise


async def wait_for_exam_page(page: Page, timeout_ms: int = 20000) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        text = await body_text(page)
        if "Paper Code" in text and await page.locator(".paper-search input[type=text], input[type=text]").count() > 0:
            return "search"
        if "User Agreement" in text:
            return "agreement"
        await page.wait_for_timeout(500)

    text = (await body_text(page)).replace("\n", " ")[:500]
    raise RuntimeError(f"Past Exam Papers page did not load agreement/search UI. Current URL: {page.url}. Text: {text}")


async def accept_user_agreement(page: Page) -> None:
    state = await wait_for_exam_page(page)
    if state == "search":
        return

    print("[Step] Accepting User Agreement")

    policy_buttons = page.locator(".policy-btn button")
    agree_button = page.locator(".policy-btn button").filter(has_text=re.compile("^\\s*Agree\\s*$", re.I))
    if await agree_button.count() == 0 and await policy_buttons.count() >= 2:
        agree_button = policy_buttons.last
    elif await agree_button.count() == 0:
        agree_button = page.get_by_role("button", name=re.compile("^Agree$", re.I))
    count = await agree_button.count()
    if count == 0:
        raise RuntimeError("Agree button was not found on the User Agreement page.")

    visible_agree = agree_button.last
    for index in range(count - 1, -1, -1):
        candidate = agree_button.nth(index)
        try:
            if await candidate.is_visible(timeout=1000):
                visible_agree = candidate
                break
        except Exception:
            continue
    await visible_agree.scroll_into_view_if_needed(timeout=5000)
    await page.wait_for_timeout(300)
    box = await visible_agree.bounding_box()
    if box:
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    else:
        await visible_agree.click(timeout=5000, force=True)
    await settle(page, 1500)

    redirect_route = route_from_redirect(page.url)
    if redirect_route:
        print(f"[Step] Following agreement redirect: {redirect_route}")
        await goto_current_base_route(page, redirect_route)

    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        if await page.locator(".paper-search input[type=text], input[type=text]").count() > 0:
            return
        if "/SearchDetail/EXAMXJTLU" in page.url:
            await page.wait_for_timeout(500)
        else:
            await page.wait_for_timeout(500)

    text = (await body_text(page)).replace("\n", " ")[:500]
    raise RuntimeError(f"Agree click did not show the paper search input. Current URL: {page.url}. Text: {text}")


async def search_course(page: Page, course_code: str) -> None:
    print(f"[Step] Searching paper code: {course_code}")
    await page.wait_for_selector(".paper-search input[type=text], input[type=text]", timeout=20000)

    text_inputs = page.locator(".paper-search input[type=text]")
    if await text_inputs.count() == 0:
        text_inputs = page.locator("input[type=text]")

    search_input = text_inputs.first
    await search_input.scroll_into_view_if_needed(timeout=3000)
    await search_input.fill(course_code)

    search_buttons = page.locator(".paper-search button").filter(has_text="Search")
    if await search_buttons.count() == 0:
        search_buttons = page.get_by_role("button", name=re.compile("^Search$", re.I))

    await search_buttons.last.click(timeout=5000)
    await settle(page, 2500)

    row_count = await page.locator("table.paper-search-list tbody tr, table tbody tr").count()
    print(f"[Step] Search result rows: {row_count}")


def result_links(page: Page):
    return page.locator("table.paper-search-list tbody tr td:first-child a, table.paper-search-list tbody tr a")


async def collect_result_infos(page: Page) -> list[dict]:
    return await page.evaluate(
        """() => [...document.querySelectorAll('table.paper-search-list tbody tr')]
            .map(row => {
                const a = row.querySelector('td:first-child a') || row.querySelector('a');
                if (!a) return null;
                return {
                    text: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim(),
                    href: a.href || a.getAttribute('href') || ''
                };
            })
            .filter(Boolean)
            .filter(item => item.text || item.href)"""
    )


async def open_result_in_new_tab(page: Page, course_code: str, index: int, info: dict) -> Page:
    links = result_links(page)
    label = info.get("text") or course_code
    print(f"[Step] Opening result {index + 1}: {label or course_code}")

    href = info.get("href") or ""
    if href and not href.startswith("javascript:"):
        detail_page = await page.context.new_page()
        await detail_page.goto(href, wait_until="domcontentloaded", timeout=60000)
        await settle(detail_page, 1800)
        return detail_page

    link = links.nth(index)
    await link.scroll_into_view_if_needed(timeout=3000)
    before_pages = set(page.context.pages)
    try:
        async with page.context.expect_page(timeout=5000) as page_info:
            await link.click(button="middle", timeout=5000)
        detail_page = await page_info.value
        await settle(detail_page, 1800)
        return detail_page
    except Exception:
        await link.click(timeout=5000, modifiers=["Control"])
        for _ in range(20):
            new_pages = [p for p in page.context.pages if p not in before_pages]
            if new_pages:
                detail_page = new_pages[-1]
                await settle(detail_page, 1800)
                return detail_page
            await page.wait_for_timeout(250)

    raise RuntimeError(f"Result {index + 1} did not open in a new tab.")


async def click_view_online(page: Page) -> None:
    print("[Step] Clicking View Online")
    view_button = page.locator("button.view-btn, .view-btn").filter(has_text="View Online")
    if await view_button.count() == 0:
        view_button = page.get_by_role("button", name=re.compile("View Online", re.I))
    if await view_button.count() == 0:
        raise RuntimeError("View Online button was not found on the detail page.")

    before_pages = set(page.context.pages)
    await view_button.first.click(timeout=5000)
    await page.wait_for_timeout(3500)

    for opened_page in [p for p in page.context.pages if p not in before_pages]:
        try:
            await opened_page.wait_for_load_state("domcontentloaded", timeout=8000)
            await opened_page.wait_for_timeout(1500)
        except Exception:
            pass
        try:
            await opened_page.close()
        except Exception:
            pass


async def download_course_papers(page: Page, course_code: str, max_clicks: int) -> None:
    await search_course(page, course_code)
    result_infos = await collect_result_infos(page)
    displayed_total = len(result_infos)
    total = min(displayed_total, max_clicks) if max_clicks > 0 else displayed_total
    if total == 0:
        print("[Warn] No result links found after search.")
        return

    print(f"[Step] Found {displayed_total} displayed result link(s); will open {total}")
    for index in range(total):
        detail_page = await open_result_in_new_tab(page, course_code, index, result_infos[index])
        try:
            await click_view_online(detail_page)
        finally:
            try:
                await detail_page.close()
            except Exception:
                pass
            await page.bring_to_front()


async def run(course_codes: list[str], login_timeout: int, max_clicks: int, headless: bool) -> None:
    course_codes = [code.strip().upper() for code in course_codes if code.strip()]
    if not course_codes:
        raise RuntimeError("No course codes were provided.")

    downloaded_urls: set[str] = set()
    pending_tasks: set[asyncio.Task] = set()

    async with async_playwright() as p:
        launch_kwargs = {"headless": headless, "accept_downloads": True}
        browser_name = "bundled chromium"
        context = None
        for channel in ("msedge", "chrome"):
            try:
                context = await p.chromium.launch_persistent_context(
                    PROFILE_DIR,
                    channel=channel,
                    **launch_kwargs,
                )
                browser_name = channel
                break
            except Exception:
                context = None
        if context is None:
            context = await p.chromium.launch_persistent_context(PROFILE_DIR, **launch_kwargs)

        print(f"Using browser channel: {browser_name}")
        print(f"[Profile] {PROFILE_DIR}")

        def track(coro):
            task = asyncio.create_task(coro)
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        context.on("response", lambda resp: track(save_pdf_response(resp, downloaded_urls)))

        def attach_page_handlers(opened_page: Page):
            opened_page.on("download", lambda d: track(save_playwright_download(d)))

        context.on("page", attach_page_handlers)
        for existing_page in context.pages:
            attach_page_handlers(existing_page)

        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await ensure_logged_in_from_home(page, login_timeout)
            page = await open_past_exam_papers(page)
            await accept_user_agreement(page)

            for course_code in course_codes:
                print(f"[Course] {course_code}")
                await download_course_papers(page, course_code, max_clicks)

            print("[Wait] Listening for final PDF responses for 20 seconds.")
            await page.wait_for_timeout(20000)
            print(f"[Done] Download directory: {DOWNLOAD_DIR}")
        finally:
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            await context.close()


def read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def normalize_course_codes(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip().upper() for item in re.split(r"[,;\s]+", value) if item.strip()]
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    raise ValueError("course_codes must be a string or a list.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto download XJTLU past papers by course code.")
    parser.add_argument("course_codes", nargs="*", help="Course code(s), e.g. INT102 CPT102")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Config JSON path. Default: downloader.config.json")
    parser.add_argument("--login-timeout", type=int, default=None, help="Seconds to wait for login. Default: config or 300")
    parser.add_argument("--max-clicks", type=int, default=None, help="Max result links per course. Default: config or 0, meaning all displayed links")
    parser.add_argument("--headless", action="store_true", help="Headless mode. Do not use this for first login.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config = read_config(args.config)

    course_codes = [code.upper() for code in args.course_codes]
    if not course_codes:
        course_codes = normalize_course_codes(config.get("course_codes"))
    if not course_codes:
        course_codes = normalize_course_codes(input("Course code(s), e.g. INT102 CPT102: ").strip())
    if not course_codes:
        raise SystemExit("Course code cannot be empty.")

    login_timeout = args.login_timeout if args.login_timeout is not None else int(config.get("login_timeout", 300))
    max_clicks = args.max_clicks if args.max_clicks is not None else int(config.get("max_clicks", 0))
    headless = args.headless or bool(config.get("headless", False))

    await run(course_codes, login_timeout, max_clicks, headless)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (TimeoutError, RuntimeError) as exc:
        raise SystemExit(f"[Error] {exc}") from None

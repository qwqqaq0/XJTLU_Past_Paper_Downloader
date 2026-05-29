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

COURSE_CODE_RE = re.compile(r"([A-Z]{2,4})[-_\s]?(\d{3})", re.I)
CURRENT_DOWNLOAD_COURSE_CODE: str | None = None
DETAIL_WAIT_MS = 800
VIEW_ONLINE_WAIT_MS = 5000
FINAL_WAIT_MS = 3000
PDF_SAVE_EVENT: asyncio.Event | None = None
PDF_SAVE_COUNT = 0


def notify_pdf_saved() -> None:
    global PDF_SAVE_COUNT

    PDF_SAVE_COUNT += 1
    if PDF_SAVE_EVENT is not None:
        PDF_SAVE_EVENT.set()

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


def unique_path(filename: str, course_code: str | None = None) -> Path:
    path = download_path(filename, course_code)
    i = 1
    while path.exists():
        path = path.parent / f"{path.stem}({i}){path.suffix}"
        i += 1
    return path


def normalize_course_code(course_code: str | None) -> str | None:
    if not course_code:
        return None
    match = COURSE_CODE_RE.search(course_code)
    if not match:
        return None
    prefix = match.group(1).upper()
    return f"{prefix}{match.group(2)}"


def classify_download_dir(filename: str, course_code: str | None = None) -> Path:
    normalized = normalize_course_code(course_code) or normalize_course_code(filename)
    if not normalized:
        return DOWNLOAD_DIR / "Unknown"

    prefix = re.match(r"[A-Z]+", normalized).group(0)
    return DOWNLOAD_DIR / prefix / normalized


def download_path(filename: str, course_code: str | None = None) -> Path:
    filename = safe_name(filename)
    folder = classify_download_dir(filename, course_code)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


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
    path = unique_path(pick_filename_from_headers(resp.headers), CURRENT_DOWNLOAD_COURSE_CODE)
    path.write_bytes(body)
    print(f"[Saved] {path}")
    notify_pdf_saved()
    return path


async def save_playwright_download(download: Download) -> None:
    filename = download.suggested_filename or "unknown_paper.pdf"
    path = unique_path(filename, CURRENT_DOWNLOAD_COURSE_CODE)
    await download.save_as(path)
    print(f"[Saved] {path}")
    notify_pdf_saved()


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


async def find_search_page(page: Page) -> Page | None:
    for candidate in reversed(page.context.pages):
        try:
            if await candidate.locator(".paper-search input[type=text], input[type=text]").count() > 0:
                return candidate
        except Exception:
            continue
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


async def accept_user_agreement(page: Page, login_timeout: int) -> Page:
    state = await wait_for_exam_page(page)
    if state == "search":
        return page

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
        search_page = await find_search_page(page)
        if search_page is not None:
            return search_page
        await page.wait_for_timeout(500)

    if "#/index" in page.url:
        text = await body_text(page)
        if "Please log in first" in text or ("User Login" in text and "Login Successfully" not in text):
            print("[Step] Login expired after agreement; logging in again")
            await ensure_logged_in_from_home(page, login_timeout)

        print("[Step] Agreement returned to home; reopening Past Exam Papers")
        reopened_page = await open_past_exam_papers(page)
        search_page = await find_search_page(reopened_page)
        if search_page is not None:
            return search_page
        if "User Agreement" in await body_text(reopened_page):
            return await accept_user_agreement(reopened_page, login_timeout)

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
            .map((row, index) => {
                const a = row.querySelector('td:first-child a') || row.querySelector('a');
                if (!a) return null;
                const cells = [...row.querySelectorAll('td')].map(td => (td.innerText || td.textContent || '').replace(/\\s+/g, ' ').trim());
                const rowText = (row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim();
                return {
                    index,
                    text: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim(),
                    href: a.href || a.getAttribute('href') || '',
                    paperCode: cells[1] || '',
                    rowText
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
        await settle(detail_page, DETAIL_WAIT_MS)
        return detail_page

    link = links.nth(int(info.get("index", index)))
    await link.scroll_into_view_if_needed(timeout=3000)
    before_pages = set(page.context.pages)
    try:
        async with page.context.expect_page(timeout=5000) as page_info:
            await link.click(button="middle", timeout=5000)
        detail_page = await page_info.value
        await settle(detail_page, DETAIL_WAIT_MS)
        return detail_page
    except Exception:
        await link.click(timeout=5000, modifiers=["Control"])
        for _ in range(20):
            new_pages = [p for p in page.context.pages if p not in before_pages]
            if new_pages:
                detail_page = new_pages[-1]
                await settle(detail_page, DETAIL_WAIT_MS)
                return detail_page
            await page.wait_for_timeout(250)

    raise RuntimeError(f"Result {index + 1} did not open in a new tab.")


async def click_view_online(page: Page) -> None:
    global PDF_SAVE_EVENT

    print("[Step] Clicking View Online")
    view_button = page.locator("button.view-btn, .view-btn").filter(has_text="View Online")
    if await view_button.count() == 0:
        view_button = page.get_by_role("button", name=re.compile("View Online", re.I))
    if await view_button.count() == 0:
        raise RuntimeError("View Online button was not found on the detail page.")

    before_pages = set(page.context.pages)
    before_saved = PDF_SAVE_COUNT
    if PDF_SAVE_EVENT is not None:
        PDF_SAVE_EVENT.clear()

    await view_button.first.click(timeout=5000)
    if PDF_SAVE_EVENT is not None:
        try:
            await asyncio.wait_for(PDF_SAVE_EVENT.wait(), timeout=VIEW_ONLINE_WAIT_MS / 1000)
        except asyncio.TimeoutError:
            if PDF_SAVE_COUNT == before_saved:
                print("[Warn] Timed out waiting for PDF save after View Online.")
    else:
        await page.wait_for_timeout(VIEW_ONLINE_WAIT_MS)

    for opened_page in [p for p in page.context.pages if p not in before_pages]:
        try:
            await opened_page.wait_for_load_state("domcontentloaded", timeout=8000)
            await opened_page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            await opened_page.close()
        except Exception:
            pass


async def next_results_page(page: Page) -> bool:
    before_infos = await collect_result_infos(page)
    before_key = result_key(before_infos[0]) if before_infos else ""
    before_page = await active_page_number(page)
    next_page = ""
    if before_page.isdigit():
        next_page = str(int(before_page) + 1)

    clicked = False
    click_targets = []
    if next_page:
        click_targets.append(page.locator(f".ivu-page li.ivu-page-item[title='{next_page}']"))
    click_targets.append(page.locator(".ivu-page li.ivu-page-next:not(.ivu-page-disabled), .ivu-page-next:not(.ivu-page-disabled)"))

    for target in click_targets:
        if clicked:
            break
        if await target.count() == 0:
            continue
        button = target.last
        try:
            await button.scroll_into_view_if_needed(timeout=3000)
            await button.click(timeout=5000)
            clicked = True
        except Exception:
            try:
                box = await button.bounding_box(timeout=2000)
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    clicked = True
            except Exception:
                pass

    if not clicked:
        return False

    for _ in range(40):
        await page.wait_for_timeout(250)
        after_infos = await collect_result_infos(page)
        after_key = result_key(after_infos[0]) if after_infos else ""
        after_page = await active_page_number(page)
        if (after_page and after_page != before_page) or (after_key and after_key != before_key):
            await settle(page, 500)
            print(f"[Step] Moved to result page {after_page or '?'}")
            return True

    print(f"[Warn] Next page click did not change results; active page stayed {before_page or '?'}.")
    return False


async def active_page_number(page: Page) -> str:
    try:
        text = await page.locator(".ivu-page-item-active").last.inner_text(timeout=1000)
        return text.strip()
    except Exception:
        return ""


def result_key(info: dict) -> str:
    return info.get("href") or info.get("rowText") or info.get("text") or ""


async def download_search_results(page: Page, search_term: str, max_results: int, max_pages: int, dry_run: bool) -> None:
    global CURRENT_DOWNLOAD_COURSE_CODE

    seen: set[str] = set()
    downloaded = 0
    page_number = 1

    while True:
        result_infos = await collect_result_infos(page)
        fresh_infos = []
        for info in result_infos:
            key = result_key(info)
            if not key or key in seen:
                continue
            seen.add(key)
            fresh_infos.append(info)

        if not result_infos and page_number == 1:
            print("[Warn] No result links found after search.")
            return

        print(f"[Step] Page {page_number}: {len(result_infos)} displayed result(s), {len(fresh_infos)} new")
        for index, info in enumerate(fresh_infos):
            if max_results > 0 and downloaded >= max_results:
                print(f"[Step] Reached max results for {search_term}: {max_results}")
                return

            if dry_run:
                title = info.get("text") or "Untitled"
                paper_code = info.get("paperCode") or search_term
                print(f"[DryRun] Result {downloaded + 1}: {paper_code} - {title}")
                downloaded += 1
                continue

            detail_page = await open_result_in_new_tab(page, search_term, index, info)
            try:
                CURRENT_DOWNLOAD_COURSE_CODE = info.get("paperCode") or search_term
                print(f"[Step] Classifying download as: {CURRENT_DOWNLOAD_COURSE_CODE}")
                await click_view_online(detail_page)
                downloaded += 1
            finally:
                CURRENT_DOWNLOAD_COURSE_CODE = None
                try:
                    await detail_page.close()
                except Exception:
                    pass
                await page.bring_to_front()

        if max_pages > 0 and page_number >= max_pages:
            print(f"[Step] Reached max pages for {search_term}: {max_pages}")
            return

        if not await next_results_page(page):
            print(f"[Step] Finished {search_term}: downloaded {downloaded} result(s)")
            return
        page_number += 1


async def download_course_papers(page: Page, course_code: str, max_results: int, max_pages: int, dry_run: bool) -> None:
    await search_course(page, course_code)
    await download_search_results(page, course_code, max_results, max_pages, dry_run)


async def run(
    search_terms: list[str],
    login_timeout: int,
    max_results: int,
    max_pages: int,
    headless: bool,
    detail_wait_ms: int,
    view_online_wait_ms: int,
    final_wait_ms: int,
    dry_run: bool,
) -> None:
    global DETAIL_WAIT_MS, VIEW_ONLINE_WAIT_MS, FINAL_WAIT_MS, PDF_SAVE_EVENT

    DETAIL_WAIT_MS = detail_wait_ms
    VIEW_ONLINE_WAIT_MS = view_online_wait_ms
    FINAL_WAIT_MS = final_wait_ms
    PDF_SAVE_EVENT = asyncio.Event()

    search_terms = [code.strip().upper() for code in search_terms if code.strip()]
    if not search_terms:
        raise RuntimeError("No course codes or prefixes were provided.")

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
            page = await accept_user_agreement(page, login_timeout)

            for search_term in search_terms:
                print(f"[SearchTerm] {search_term}")
                await download_course_papers(page, search_term, max_results, max_pages, dry_run)

            if not dry_run:
                print(f"[Wait] Listening for final PDF responses for {FINAL_WAIT_MS} ms.")
                await page.wait_for_timeout(FINAL_WAIT_MS)
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


def normalize_search_terms(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip().upper() for item in re.split(r"[,;\s]+", value) if item.strip()]
    if isinstance(value, list):
        terms = []
        for item in value:
            terms.extend(normalize_search_terms(str(item)))
        return terms
    raise ValueError("course_codes/course_prefixes must be a string or a list.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto download XJTLU past papers by course code.")
    parser.add_argument("course_codes", nargs="*", help="Course code(s), e.g. INT102 CPT102")
    parser.add_argument("--prefix", "--prefixes", dest="prefixes", action="append", default=[], help="Course prefix(es), e.g. SAT,CAN,CPT or --prefix SAT --prefix CPT")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Config JSON path. Default: downloader.config.json")
    parser.add_argument("--login-timeout", type=int, default=None, help="Seconds to wait for login. Default: config or 300")
    parser.add_argument("--max-results", type=int, default=None, help="Max result links per search term across pages. Default: config or 0, meaning all")
    parser.add_argument("--max-clicks", type=int, default=None, help="Deprecated alias for --max-results")
    parser.add_argument("--max-pages", type=int, default=None, help="Max result pages per search term. Default: config or 0, meaning all pages")
    parser.add_argument("--detail-wait-ms", type=int, default=None, help="Wait after opening each detail page. Default: config or 800")
    parser.add_argument("--view-wait-ms", type=int, default=None, help="Max wait after clicking View Online. Default: config or 5000")
    parser.add_argument("--final-wait-ms", type=int, default=None, help="Final PDF response wait. Default: config or 3000")
    parser.add_argument("--dry-run", action="store_true", help="Search and paginate only; do not open detail pages or download PDFs.")
    parser.add_argument("--headless", action="store_true", help="Headless mode. Do not use this for first login.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config = read_config(args.config)

    course_codes = normalize_search_terms(args.course_codes)
    cli_prefixes = normalize_search_terms(args.prefixes)

    search_terms = course_codes + cli_prefixes
    if not search_terms:
        search_terms = normalize_search_terms(config.get("course_codes"))
        search_terms += normalize_search_terms(config.get("course_prefixes"))
    if not search_terms:
        search_terms = normalize_search_terms(input("Course code(s) or prefix(es), e.g. INT102 CPT: ").strip())
    if not search_terms:
        raise SystemExit("Course code/prefix cannot be empty.")

    login_timeout = args.login_timeout if args.login_timeout is not None else int(config.get("login_timeout", 300))
    configured_max_results = config.get("max_results", config.get("max_clicks", 0))
    max_results = args.max_results
    if max_results is None:
        max_results = args.max_clicks if args.max_clicks is not None else int(configured_max_results)
    max_pages = args.max_pages if args.max_pages is not None else int(config.get("max_pages", 0))
    headless = args.headless or bool(config.get("headless", False))
    detail_wait_ms = args.detail_wait_ms if args.detail_wait_ms is not None else int(config.get("detail_wait_ms", DETAIL_WAIT_MS))
    view_online_wait_ms = args.view_wait_ms if args.view_wait_ms is not None else int(config.get("view_online_wait_ms", VIEW_ONLINE_WAIT_MS))
    final_wait_ms = args.final_wait_ms if args.final_wait_ms is not None else int(config.get("final_wait_ms", FINAL_WAIT_MS))
    dry_run = bool(args.dry_run or config.get("dry_run", False))

    await run(
        search_terms,
        login_timeout,
        max_results,
        max_pages,
        headless,
        detail_wait_ms,
        view_online_wait_ms,
        final_wait_ms,
        dry_run,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (TimeoutError, RuntimeError) as exc:
        raise SystemExit(f"[Error] {exc}") from None

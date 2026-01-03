import asyncio
import re
from pathlib import Path
from urllib.parse import unquote
from playwright.async_api import async_playwright

START_URL = "https://etd.xjtlu.edu.cn"

BASE_DIR = Path(__file__).resolve().parent      # current script directory
DOWNLOAD_DIR = BASE_DIR / "Download"            # Download to "Download" folder
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if not name:
        name = "unknown_paper.pdf"
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


async def save_pdf_response(resp, downloaded_urls: set[str]):
    url = resp.url
    if url in downloaded_urls:
        return

    # Quick checks to filter out non-PDF responses
    if resp.status != 200:
        return

    ct = (resp.headers.get("content-type", "") or "").lower()
    url_l = url.lower()

    if ("pdf" not in ct) and ("browserfile" not in url_l) and (not url_l.endswith(".pdf")):
        return

    try:
        body = await resp.body()
    except Exception:
        return

    if not (body and body.startswith(b"%PDF")):
        return

    downloaded_urls.add(url)

    filename = pick_filename_from_headers(resp.headers)
    path = DOWNLOAD_DIR / filename

    # Avoid overwriting: add (1)(2)... for same names
    i = 1
    while path.exists():
        path = DOWNLOAD_DIR / f"{path.stem}({i}){path.suffix}"
        i += 1

    path.write_bytes(body)
    print(f"[Saved] {path}")


async def main():
    downloaded_urls: set[str] = set()

    async with async_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome"):
            try:
                browser = await p.chromium.launch(channel=channel, headless=False)
                print(f"Using browser channel: {channel}")
                break
            except Exception:
                pass
        if browser is None:
            browser = await p.chromium.launch(headless=False)
            print("Using bundled chromium")

        context = await browser.new_context()

        context.on("response", lambda r: asyncio.create_task(save_pdf_response(r, downloaded_urls)))

        page = await context.new_page()
        await page.goto(START_URL, wait_until="domcontentloaded")
        print("Open the website and please log in / navigate to the desired page manually. Complete PDFs will be automatically saved to:", DOWNLOAD_DIR)

        while len(context.pages) > 0:
            await asyncio.sleep(1)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

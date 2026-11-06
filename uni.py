import asyncio
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()

UNIVERSITY_EMAIL = os.getenv("UNIVERSITY_EMAIL")
UNIVERSITY_PASSWORD = os.getenv("UNIVERSITY_PASSWORD")
UNIVERSITY_GROUP = os.getenv("UNIVERSITY_GROUP")  
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

BASE = "https://isimm.rnu.tn/public"
LOGIN_URL = f"{BASE}/login"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)
ERR_PATH = OUT_DIR / "error.png"

_lock = asyncio.Lock() 
_cache: dict[str, tuple[float, tuple[Path, str, str]]] = {}

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")


class GroupNotFound(Exception):
    """The group has no timetable page."""


async def _launch(p, headless: bool):
    browser = await p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="fr-FR",
        device_scale_factor=2,
    )
    page = await context.new_page()
    return browser, context, page


async def _login(page) -> None:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_selector('input[placeholder="E-mail"]', timeout=30000)
    await page.locator('input[placeholder="E-mail"]').fill(UNIVERSITY_EMAIL)
    await page.locator('input[placeholder="Mot de passe"]').fill(UNIVERSITY_PASSWORD)
    await page.get_by_role("button", name="CONNEXION").click()
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(3000)

    if "login" in page.url.lower():
        raise RuntimeError("Login failed - check UNIVERSITY_EMAIL / PASSWORD in .env")


async def _debug_shot(page):
    try:
        await page.screenshot(path=str(ERR_PATH), full_page=True)
        print(f"Saved debug screenshot: {ERR_PATH}")
    except Exception:
        pass


async def _capture(page, group: str) -> tuple[Path, str, str]:
    url = f"{BASE}/afficher_emploi/{quote(group, safe='')}"
    await page.goto(url, wait_until="networkidle", timeout=60000)

    if "login" in page.url.lower():
        raise RuntimeError("Session lost (redirected to login)")

    table = page.locator("table", has_text="Horaire").last
    try:
        await table.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        raise GroupNotFound(group)

    title = ""
    try:
        heading = page.get_by_text(re.compile(r"^\s*Groupe TD\s*:")).first
        title = " ".join((await heading.inner_text(timeout=3000)).split())
    except Exception:
        pass

    table_text = " ".join((await table.inner_text()).split())
    content_hash = hashlib.sha256((title + table_text).encode()).hexdigest()

    safe = re.sub(r"[^\w\-]", "_", group)
    path = OUT_DIR / f"timetable_{safe}.png"
    await table.screenshot(path=str(path))
    return path, content_hash, title


async def fetch_timetables(
    groups: list[str], max_age: float = 0, headless: bool = HEADLESS
) -> dict[str, tuple[Path, str, str] | Exception]:
    """
    Logs in ONCE and captures every requested group.
    Returns {group: (image_path, hash, title)} or {group: Exception}.
    max_age: reuse a result fetched less than this many seconds ago.
    """
    results: dict = {}
    async with _lock:
        now = time.time()
        todo = []
        for g in dict.fromkeys(groups):  
            cached = _cache.get(g)
            if cached and now - cached[0] < max_age and cached[1][0].exists():
                results[g] = cached[1]
            else:
                todo.append(g)
        if not todo:
            return results

        async with async_playwright() as p:
            browser, context, page = await _launch(p, headless)
            try:
                await _login(page)
                for g in todo:
                    try:
                        res = await _capture(page, g)
                        _cache[g] = (time.time(), res)
                        results[g] = res
                    except GroupNotFound as e:
                        results[g] = e
                    except Exception as e:
                        await _debug_shot(page)
                        results[g] = e
            except Exception:
                await _debug_shot(page)
                raise
            finally:
                await browser.close()
    return results



def _group_id(value: str, text: str) -> str | None:
    text = (text or "").strip()
    value = (value or "").strip()
    if not text or text.lower().startswith(("choisir", "select", "choose")):
        return None
    if _ID.fullmatch(text):
        return text
    seg = value.rstrip("/").split("/")[-1]
    if value and _ID.fullmatch(seg):
        return seg
    return None


_BIGGEST_SELECT_JS = """() => {
  const sels = [...document.querySelectorAll('select')];
  if (!sels.length) return [];
  sels.sort((a, b) => b.options.length - a.options.length);
  return [...sels[0].options].map(o => [o.value, o.textContent.trim()]);
}"""


async def fetch_groups(headless: bool = HEADLESS) -> list[str]:
    """Reads the 'Choisir Groupe TD' dropdown of the website."""
    async with _lock:
        async with async_playwright() as p:
            browser, context, page = await _launch(p, headless)
            try:
                await _login(page)
                urls = [f"{BASE}/emploi"]
                if UNIVERSITY_GROUP:
                    urls.append(f"{BASE}/afficher_emploi/{quote(UNIVERSITY_GROUP, safe='')}")
                for url in urls:
                    await page.goto(url, wait_until="networkidle", timeout=60000)
                    options = await page.evaluate(_BIGGEST_SELECT_JS)
                    groups = sorted({g for v, t in options if (g := _group_id(v, t))})
                    if len(groups) >= 2:
                        return groups
                return []
            except Exception:
                await _debug_shot(page)
                raise
            finally:
                await browser.close()



if __name__ == "__main__":
    
    headless = HEADLESS and "--show" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if "--groups" in sys.argv:
        found = asyncio.run(fetch_groups(headless))
        print(f"{len(found)} groups found")
        print("\n".join(found))
    else:
        group = args[0] if args else UNIVERSITY_GROUP
        res = asyncio.run(fetch_timetables([group], headless=headless))[group]
        if isinstance(res, Exception):
            raise res
        print("Saved:", res[0])
        print("Title:", res[2])
        print("Hash :", res[1])
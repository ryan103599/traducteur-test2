from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

GOOGLE_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}&op=images"

def _image_file_input(page):
    inputs = page.locator('input[type="file"]')
    count = inputs.count()
    if count == 0:
        raise RuntimeError("Google Traduction n'affiche aucun champ d'import d'image.")
    return inputs.nth(count - 1)

def translate_image_with_google(source: Path, destination: Path, target: str) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            accept_downloads=True,
            locale="fr-FR",
            viewport={"width": 1440, "height": 1100},
        )
        page = context.new_page()
        try:
            page.goto(GOOGLE_URL.format(target=target), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            _image_file_input(page).set_input_files(str(source))

            button = page.get_by_role("button", name="Télécharger la traduction", exact=False)
            try:
                button.wait_for(state="visible", timeout=120000)
            except PlaywrightTimeoutError:
                button = page.get_by_text("Télécharger la traduction", exact=False).first
                button.wait_for(state="visible", timeout=30000)

            with page.expect_download(timeout=30000) as download_info:
                button.click()
            download_info.value.save_as(str(destination))

            if not destination.exists() or destination.stat().st_size == 0:
                raise RuntimeError("Google n'a pas fourni d'image traduite.")
        finally:
            context.close()
            browser.close()

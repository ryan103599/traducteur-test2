from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

GOOGLE_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}"


def _open_image_mode(page):
    # Google ne charge pas toujours directement le panneau Images avec
    # ?op=images. On ouvre donc la page principale puis le panneau Images.
    page.goto(GOOGLE_URL.format(target="fr"), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    # Plusieurs versions de l'interface existent. On essaie d'abord le
    # bouton/onglet accessible "Images", puis quelques sélecteurs stables.
    candidates = [
        page.get_by_role("tab", name="Images", exact=True),
        page.get_by_role("button", name="Images", exact=True),
        page.get_by_text("Images", exact=True),
    ]
    for locator in candidates:
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.click()
                page.wait_for_timeout(1800)
                return
        except Exception:
            pass

    # Fallback : l'URL dédiée peut fonctionner selon la version de Google.
    page.goto(
        f"https://translate.google.com/?hl=fr&sl=auto&tl=fr&op=images",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(2500)


def _image_file_input(page):
    # Le champ de la traduction d'image accepte explicitement ces formats.
    selectors = [
        'input[type="file"][accept*=".jpg"]',
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ]
    for selector in selectors:
        loc = page.locator(selector)
        try:
            if loc.count():
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    accept = (candidate.get_attribute("accept") or "").lower()
                    if ".jpg" in accept or ".jpeg" in accept or ".png" in accept or ".webp" in accept:
                        return candidate
                if selector != 'input[type="file"]':
                    return loc.first
        except Exception:
            pass

    raise RuntimeError(
        "Google Traduction n'affiche pas le champ d'import d'image. "
        "L'interface Google a probablement changé ou le panneau Images n'est pas chargé."
    )


def _wait_for_download_control(page):
    labels = [
        "Télécharger la traduction",
        "Télécharger",
        "Download translation",
        "Download",
    ]
    for label in labels:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=label, exact=False)
            try:
                if loc.count():
                    for i in range(loc.count()):
                        candidate = loc.nth(i)
                        if candidate.is_visible():
                            return candidate
            except Exception:
                pass

    # Dernier recours : rechercher le texte visible.
    for label in labels:
        loc = page.get_by_text(label, exact=False)
        try:
            if loc.count():
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        return candidate
        except Exception:
            pass

    raise RuntimeError("Google a traité l'image mais le bouton de téléchargement est introuvable.")


def translate_image_with_google(source: Path, destination: Path, target: str) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            accept_downloads=True,
            locale="fr-FR",
            viewport={"width": 1440, "height": 1100},
        )
        page = context.new_page()
        try:
            # On utilise directement la langue cible dans l'URL, puis on
            # ouvre réellement le panneau Images.
            page.goto(
                f"https://translate.google.com/?hl=fr&sl=auto&tl={target}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(2500)

            # Cliquer sur "Images" si le panneau n'est pas déjà actif.
            image_input = None
            try:
                image_input = _image_file_input(page)
            except RuntimeError:
                _open_image_mode(page)
                image_input = _image_file_input(page)

            image_input.set_input_files(str(source))

            page.wait_for_timeout(2500)
            button = _wait_for_download_control(page)

            with page.expect_download(timeout=60000) as download_info:
                button.click()
            download_info.value.save_as(str(destination))

            if not destination.exists() or destination.stat().st_size == 0:
                raise RuntimeError("Google n'a pas fourni d'image traduite.")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Google Traduction a mis trop de temps à charger ou à traduire l'image."
            ) from exc
        finally:
            context.close()
            browser.close()

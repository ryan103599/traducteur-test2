from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Le domaine .fr évite certaines redirections régionales de Google.
BASE_URL = "https://translate.google.fr/?hl=fr&sl=auto&tl={target}"
IMAGES_URL = "https://translate.google.fr/?hl=fr&sl=auto&tl={target}&op=images"


def _dismiss_google_consent(page):
    # Google peut afficher un écran de consentement avant de charger Traduction.
    labels = [
        "Tout accepter",
        "J'accepte",
        "Accepter tout",
        "Accept all",
        "I agree",
    ]
    for frame in [page] + list(page.frames):
        for label in labels:
            try:
                loc = frame.get_by_role("button", name=label, exact=True)
                for i in range(loc.count()):
                    button = loc.nth(i)
                    if button.is_visible():
                        button.click()
                        page.wait_for_timeout(2500)
                        return
            except Exception:
                pass


def _find_file_input(page):
    for frame in [page] + list(page.frames):
        try:
            loc = frame.locator('input[type="file"]')
            for i in range(loc.count()):
                candidate = loc.nth(i)
                accept = (candidate.get_attribute("accept") or "").lower()
                if not accept or any(
                    x in accept for x in (".jpg", ".jpeg", ".png", ".webp", "image/")
                ):
                    return candidate
        except Exception:
            pass
    return None


def _click_images_tab(page):
    for frame in [page] + list(page.frames):
        for role in ("tab", "button", "link"):
            try:
                loc = frame.get_by_role(role, name="Images", exact=True)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        candidate.click()
                        page.wait_for_timeout(3000)
                        return True
            except Exception:
                pass

        try:
            loc = frame.get_by_text("Images", exact=True)
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if candidate.is_visible():
                    candidate.click()
                    page.wait_for_timeout(3000)
                    return True
        except Exception:
            pass
    return False


def _open_image_mode(page, target):
    page.goto(IMAGES_URL.format(target=target), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    _dismiss_google_consent(page)
    page.wait_for_timeout(2500)

    if _find_file_input(page) is not None:
        return

    # Fallback : page principale + onglet Images.
    page.goto(BASE_URL.format(target=target), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    _dismiss_google_consent(page)
    _click_images_tab(page)
    page.wait_for_timeout(5000)


def _choose_image_file(page, source):
    image_input = _find_file_input(page)
    if image_input is not None:
        image_input.set_input_files(str(source))
        return

    labels = [
        "Parcourir vos fichiers",
        "Sélectionnez un fichier",
        "Ou sélectionnez un fichier",
        "Choose a file",
        "Browse your files",
    ]

    # Important : il y a deux zones "Parcourir vos fichiers" sur la page
    # (Documents puis Images). On essaie tous les éléments visibles.
    for frame in [page] + list(page.frames):
        for label in labels:
            try:
                loc = frame.get_by_text(label, exact=True)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if not candidate.is_visible():
                        continue
                    try:
                        with page.expect_file_chooser(timeout=5000) as chooser_info:
                            candidate.click()
                        chooser_info.value.set_files(str(source))
                        return
                    except PlaywrightTimeoutError:
                        # Le clic peut simplement révéler l'input.
                        page.wait_for_timeout(1000)
                        image_input = _find_file_input(page)
                        if image_input is not None:
                            image_input.set_input_files(str(source))
                            return
            except Exception:
                pass

    # Dernier recours : cliquer sur un bouton accessible.
    for frame in [page] + list(page.frames):
        for label in labels:
            try:
                loc = frame.get_by_role("button", name=label, exact=False)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if not candidate.is_visible():
                        continue
                    try:
                        with page.expect_file_chooser(timeout=5000) as chooser_info:
                            candidate.click()
                        chooser_info.value.set_files(str(source))
                        return
                    except PlaywrightTimeoutError:
                        image_input = _find_file_input(page)
                        if image_input is not None:
                            image_input.set_input_files(str(source))
                            return
            except Exception:
                pass

    # Diagnostic exploitable : URL, titre et texte visible de la page.
    try:
        debug_text = page.locator("body").inner_text(timeout=5000)
        debug_text = " ".join(debug_text.split())[:1200]
        page.screenshot(path="/tmp/google_translate_debug.png", full_page=True)
        Path("/tmp/google_translate_debug.html").write_text(
            page.content(), encoding="utf-8"
        )
    except Exception:
        debug_text = "(texte de page indisponible)"

    raise RuntimeError(
        "Impossible d'ouvrir l'import d'image de Google Traduction. "
        f"URL={page.url} | Titre={page.title()} | Page={debug_text}"
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
            try:
                loc = page.get_by_role(role, name=label, exact=False)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                pass

    for label in labels:
        try:
            loc = page.get_by_text(label, exact=False)
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if candidate.is_visible():
                    return candidate
        except Exception:
            pass

    raise RuntimeError(
        "Google a traité l'image mais le bouton de téléchargement est introuvable."
    )


def translate_image_with_google(source: Path, destination: Path, target: str) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            locale="fr-FR",
            viewport={"width": 1440, "height": 1100},
        )
        page = context.new_page()

        try:
            _open_image_mode(page, target)
            _choose_image_file(page, source)
            page.wait_for_timeout(6000)

            button = _wait_for_download_control(page)
            with page.expect_download(timeout=60000) as download_info:
                button.click()

            download_info.value.save_as(str(destination))

            if not destination.exists() or destination.stat().st_size == 0:
                raise RuntimeError("Google n'a pas fourni d'image traduite.")

        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Google Traduction a mis trop de temps à charger, traduire ou télécharger l'image."
            ) from exc
        finally:
            context.close()
            browser.close()

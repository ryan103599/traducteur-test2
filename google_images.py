from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}"
IMAGES_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}&op=images"


def _visible_text_locator(page, text_value):
    loc = page.get_by_text(text_value, exact=True)
    for i in range(loc.count()):
        candidate = loc.nth(i)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            pass
    return None


def _open_image_mode(page, target):
    # On tente d'abord l'URL dédiée au mode Images.
    page.goto(
        IMAGES_URL.format(target=target),
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(3000)

    # Si Google nous a renvoyés vers la page principale, on clique sur Images.
    for role in ("tab", "button", "link"):
        loc = page.get_by_role(role, name="Images", exact=True)
        try:
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if candidate.is_visible():
                    candidate.click()
                    page.wait_for_timeout(2000)
                    return
        except Exception:
            pass

    text_loc = _visible_text_locator(page, "Images")
    if text_loc:
        try:
            text_loc.click()
            page.wait_for_timeout(2000)
        except Exception:
            pass


def _image_file_input(page):
    # Selon la version de Google, l'input peut être masqué mais reste présent
    # dans le DOM. On filtre les inputs d'image quand l'attribut accept existe.
    loc = page.locator('input[type="file"]')
    count = loc.count()

    for i in range(count):
        candidate = loc.nth(i)
        accept = (candidate.get_attribute("accept") or "").lower()
        if any(ext in accept for ext in (".jpg", ".jpeg", ".png", ".webp", "image/")):
            return candidate

    if count:
        return loc.first

    return None


def _choose_image_file(page, source):
    # Méthode 1 : input file directement accessible dans le DOM.
    image_input = _image_file_input(page)
    if image_input is not None:
        image_input.set_input_files(str(source))
        return

    # Méthode 2 : Google affiche parfois uniquement "Parcourir vos fichiers"
    # et crée l'input au moment du clic. Playwright permet alors de récupérer
    # le file chooser directement.
    labels = [
        "Parcourir vos fichiers",
        "Sélectionnez un fichier",
        "Ou sélectionnez un fichier",
        "Choose a file",
        "Browse your files",
    ]

    for label in labels:
        loc = page.get_by_text(label, exact=True)
        try:
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if not candidate.is_visible():
                    continue
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    candidate.click()
                chooser_info.value.set_files(str(source))
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    # Dernier essai avec les boutons accessibles.
    for label in labels:
        loc = page.get_by_role("button", name=label, exact=False)
        try:
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if not candidate.is_visible():
                    continue
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    candidate.click()
                chooser_info.value.set_files(str(source))
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    raise RuntimeError(
        "Impossible d'ouvrir l'import d'image de Google Traduction. "
        "Le panneau Images n'est probablement pas chargé ou Google a changé son interface."
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
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                pass

    for label in labels:
        loc = page.get_by_text(label, exact=False)
        try:
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
            page.wait_for_timeout(1500)

            _choose_image_file(page, source)

            # Google peut prendre quelques secondes pour afficher le résultat.
            page.wait_for_timeout(5000)

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

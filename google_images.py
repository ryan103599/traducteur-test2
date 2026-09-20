from pathlib import Path
import re

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

IMAGES_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}&op=images"


def _accept_google_consent(page):
    # Google peut afficher un écran de consentement avant de charger Translate.
    patterns = [
        r"J.?accepte",
        r"Tout accepter",
        r"Accepter tout",
        r"Accept all",
        r"I agree",
    ]
    for pattern in patterns:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=re.compile(pattern, re.I))
            try:
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        candidate.click()
                        page.wait_for_timeout(1500)
                        return
            except Exception:
                pass


def _open_image_mode(page, target):
    page.goto(
        IMAGES_URL.format(target=target),
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(4000)
    _accept_google_consent(page)

    # Si Google n'a pas ouvert le mode Images, cliquer sur l'onglet Images.
    image_patterns = [
        re.compile(r"^Images$", re.I),
        re.compile(r"Traduction d.?image", re.I),
    ]

    for pattern in image_patterns:
        for role in ("tab", "button", "link"):
            loc = page.get_by_role(role, name=pattern)
            try:
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        candidate.click()
                        page.wait_for_timeout(2500)
                        return
            except Exception:
                pass

    # Fallback DOM : recherche de tout élément visible contenant "Images".
    loc = page.locator("text=Images")
    try:
        for i in range(loc.count()):
            candidate = loc.nth(i)
            if candidate.is_visible():
                candidate.click()
                page.wait_for_timeout(2500)
                return
    except Exception:
        pass


def _image_file_input(page):
    loc = page.locator('input[type="file"]')
    try:
        count = loc.count()
        for i in range(count):
            candidate = loc.nth(i)
            accept = (candidate.get_attribute("accept") or "").lower()
            if not accept or any(
                value in accept
                for value in (".jpg", ".jpeg", ".png", ".webp", "image/")
            ):
                return candidate
    except Exception:
        pass
    return None


def _choose_image_file(page, source):
    # 1. Le cas le plus simple : input file présent dans le DOM.
    image_input = _image_file_input(page)
    if image_input is not None:
        image_input.set_input_files(str(source))
        return

    # 2. Google utilise souvent un bouton "Parcourir vos fichiers".
    # On recherche par texte partiel pour ne pas dépendre de la structure HTML.
    patterns = [
        re.compile(r"Parcourir.*fichier", re.I),
        re.compile(r"S[ée]lectionnez.*fichier", re.I),
        re.compile(r"Ou.*s[ée]lectionnez.*fichier", re.I),
        re.compile(r"Choose.*file", re.I),
        re.compile(r"Browse.*file", re.I),
        re.compile(r"upload", re.I),
    ]

    for pattern in patterns:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=pattern)
            try:
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if not candidate.is_visible():
                        continue
                    with page.expect_file_chooser(timeout=8000) as chooser_info:
                        candidate.click()
                    chooser_info.value.set_files(str(source))
                    return
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue

    # 3. Recherche générique des boutons/éléments cliquables contenant le texte.
    for pattern in patterns:
        loc = page.locator("button").filter(has_text=pattern)
        try:
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if not candidate.is_visible():
                    continue
                with page.expect_file_chooser(timeout=8000) as chooser_info:
                    candidate.click()
                chooser_info.value.set_files(str(source))
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    # 4. Dernier recours : trouver n'importe quel élément visible contenant
    # "fichier" dans la zone de la page et déclencher le file chooser.
    loc = page.locator("text=/fichier|file/i")
    try:
        for i in range(loc.count()):
            candidate = loc.nth(i)
            if not candidate.is_visible():
                continue
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                candidate.click()
            chooser_info.value.set_files(str(source))
            return
    except Exception:
        pass

    # Diagnostic utile au prochain test : les textes réellement visibles.
    try:
        buttons = page.locator("button").all_inner_texts()
        visible_buttons = [x.strip() for x in buttons if x.strip()]
    except Exception:
        visible_buttons = []

    raise RuntimeError(
        "Impossible d'ouvrir l'import d'image de Google Traduction. "
        f"Boutons visibles détectés: {visible_buttons[:20]}"
    )


def _wait_for_download_control(page):
    patterns = [
        re.compile(r"T[ée]l[ée]charger la traduction", re.I),
        re.compile(r"T[ée]l[ée]charger", re.I),
        re.compile(r"Download translation", re.I),
        re.compile(r"Download", re.I),
    ]

    for pattern in patterns:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=pattern)
            try:
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                pass

    for pattern in patterns:
        loc = page.get_by_text(pattern)
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
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            locale="fr-FR",
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            _open_image_mode(page, target)
            page.wait_for_timeout(1500)
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

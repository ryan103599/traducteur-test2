from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}"
IMAGES_URL = "https://translate.google.com/?hl=fr&sl=auto&tl={target}&op=images"


def _find_file_input(page):
    # Cherche dans la page principale puis dans les éventuelles frames.
    pages = [page] + list(page.frames)
    for frame in pages:
        try:
            loc = frame.locator('input[type="file"]')
            count = loc.count()
            for i in range(count):
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
    # L'interface actuelle expose bien un onglet "Images", mais le DOM
    # peut varier entre les versions de Google.
    for role in ("tab", "button", "link"):
        try:
            loc = page.get_by_role(role, name="Images", exact=True)
            for i in range(loc.count()):
                candidate = loc.nth(i)
                if candidate.is_visible():
                    candidate.click()
                    page.wait_for_timeout(3000)
                    return True
        except Exception:
            pass

    try:
        loc = page.get_by_text("Images", exact=True)
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
    # URL officielle du mode Images.
    page.goto(
        IMAGES_URL.format(target=target),
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # Laisser les composants Google et leurs scripts s'initialiser.
    page.wait_for_timeout(7000)

    if _find_file_input(page) is not None:
        return

    # Si Google a ignoré op=images, revenir à la page principale et cliquer
    # réellement sur l'onglet Images.
    page.goto(
        BASE_URL.format(target=target),
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(5000)
    _click_images_tab(page)
    page.wait_for_timeout(5000)


def _choose_image_file(page, source):
    # 1. Input file directement présent.
    image_input = _find_file_input(page)
    if image_input is not None:
        image_input.set_input_files(str(source))
        return

    # 2. Google peut créer le file chooser seulement après le clic.
    labels = [
        "Parcourir vos fichiers",
        "Sélectionnez un fichier",
        "Ou sélectionnez un fichier",
        "Choose a file",
        "Browse your files",
    ]

    # On cherche dans toutes les frames.
    frames = [page] + list(page.frames)
    for frame in frames:
        for label in labels:
            try:
                loc = frame.get_by_text(label, exact=True)
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

    # 3. Même chose avec les boutons accessibles.
    for frame in frames:
        for label in labels:
            try:
                loc = frame.get_by_role("button", name=label, exact=False)
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

    # Diagnostic utile si Google change à nouveau son DOM.
    try:
        page.screenshot(path="/tmp/google_translate_debug.png", full_page=True)
        html = page.content()
        Path("/tmp/google_translate_debug.html").write_text(html, encoding="utf-8")
    except Exception:
        pass

    raise RuntimeError(
        "Impossible d'ouvrir l'import d'image de Google Traduction. "
        "Google a probablement affiché une page différente (consentement, "
        "anti-bot ou interface modifiée)."
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
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            _open_image_mode(page, target)
            _choose_image_file(page, source)

            # Attendre l'apparition du résultat et du téléchargement.
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

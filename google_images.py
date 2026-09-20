from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://translate.google.fr/?hl=fr&sl=auto&tl={target}"
IMAGES_URL = "https://translate.google.fr/?hl=fr&sl=auto&tl={target}&op=images"


def _dismiss_google_consent(page):
    for label in ["Tout accepter", "J'accepte", "Accepter tout", "Accept all", "I agree"]:
        for frame in [page] + list(page.frames):
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
                if not accept or any(x in accept for x in (".jpg", ".jpeg", ".png", ".webp", "image/")):
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


def _assert_target_language(page, target):
    try:
        query = parse_qs(urlparse(page.url).query)
        current = query.get("tl", [None])[0]
        if current and current != target:
            raise RuntimeError(
                f"Google Traduction a chargé la mauvaise langue cible ({current} au lieu de {target})."
            )
    except RuntimeError:
        raise
    except Exception:
        pass


def _open_image_mode(page, target):
    page.goto(IMAGES_URL.format(target=target), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    _dismiss_google_consent(page)
    page.wait_for_timeout(2000)
    _assert_target_language(page, target)

    if _find_file_input(page) is not None:
        return

    page.goto(BASE_URL.format(target=target), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    _dismiss_google_consent(page)
    _click_images_tab(page)
    page.wait_for_timeout(5000)
    _assert_target_language(page, target)


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
                        page.wait_for_timeout(1000)
                        image_input = _find_file_input(page)
                        if image_input is not None:
                            image_input.set_input_files(str(source))
                            return
            except Exception:
                pass

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

    raise RuntimeError("Impossible d'ouvrir l'import d'image de Google Traduction.")


def _find_download_control(page):
    # Prefer the exact translated-image control. Google documents this as
    # "Télécharger la traduction Image".
    exact_labels = [
        "Télécharger la traduction Image",
        "Download translation Image",
    ]

    for frame in [page] + list(page.frames):
        for label in exact_labels:
            try:
                loc = frame.locator(f'[aria-label="{label}"]')
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                pass

    for frame in [page] + list(page.frames):
        for label in exact_labels:
            try:
                loc = frame.get_by_role("button", name=label, exact=True)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                pass

    return None


def _force_translated_view(page):
    # Google can keep "Afficher le texte original" enabled after upload.
    # Turn it off so the translated rendering is the active view before
    # downloading the image.
    labels = [
        "Afficher le texte original",
        "Show original text",
        "Afficher l'original",
        "Show original",
    ]

    for frame in [page] + list(page.frames):
        for label in labels:
            for role in ("checkbox", "button", "switch"):
                try:
                    loc = frame.get_by_role(role, name=label, exact=True)
                    for i in range(loc.count()):
                        candidate = loc.nth(i)
                        if not candidate.is_visible():
                            continue
                        try:
                            checked = candidate.get_attribute("aria-checked")
                            if checked == "true":
                                candidate.click()
                                page.wait_for_timeout(1200)
                                return
                        except Exception:
                            pass
                except Exception:
                    pass

    # Some versions expose the control as text next to a switch.
    for frame in [page] + list(page.frames):
        for label in labels:
            try:
                loc = frame.get_by_text(label, exact=True)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    if candidate.is_visible():
                        parent = candidate.locator("..")
                        switch = parent.get_by_role("checkbox")
                        if switch.count() and switch.first.is_visible():
                            if switch.first.get_attribute("aria-checked") == "true":
                                switch.first.click()
                                page.wait_for_timeout(1200)
                                return
            except Exception:
                pass


def _save_debug(page):
    try:
        page.screenshot(path="/tmp/google_translate_result.png", full_page=True)
        Path("/tmp/google_translate_result.html").write_text(
            page.content(), encoding="utf-8"
        )
    except Exception:
        pass


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

            button = None
            for _ in range(90):
                button = _find_download_control(page)
                if button is not None:
                    break
                page.wait_for_timeout(1000)

            if button is None:
                _save_debug(page)
                raise RuntimeError(
                    "Google a traité l'image mais le bouton « Télécharger la traduction Image » est introuvable."
                )

            _force_translated_view(page)
            page.wait_for_timeout(2000)

            # Re-find the control after changing the view. This avoids keeping
            # a stale DOM handle from before Google's result finished rendering.
            button = _find_download_control(page)
            if button is None:
                _save_debug(page)
                raise RuntimeError(
                    "Le bouton « Télécharger la traduction Image » a disparu après l'affichage traduit."
                )

            with page.expect_download(timeout=60000) as download_info:
                button.click()

            download_info.value.save_as(str(destination))

            if not destination.exists() or destination.stat().st_size == 0:
                raise RuntimeError("Google n'a pas fourni l'image traduite.")

            if source.read_bytes() == destination.read_bytes():
                _save_debug(page)
                raise RuntimeError(
                    "Google a téléchargé une image identique à l'originale. "
                    "La traduction d'image n'a probablement pas été appliquée."
                )

        except PlaywrightTimeoutError as exc:
            _save_debug(page)
            raise RuntimeError(
                "Google Traduction a mis trop de temps à charger, traduire ou télécharger l'image."
            ) from exc
        finally:
            context.close()
            browser.close()

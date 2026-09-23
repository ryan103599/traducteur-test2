import hmac
from html import escape
from html import escape
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from urllib.parse import quote
from urllib.parse import quote
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, send_file, session, url_for
from werkzeug.utils import secure_filename

from lara_images import translate_image_with_lara
from lara_usage import get_usage, record_image


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
app.secret_key = os.getenv("ADMIN_SESSION_SECRET", "").strip() or os.urandom(32)

JOBS = {}
LOCK = threading.Lock()
RETENTION_SECONDS = 60 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60
TEMP_PREFIX = "traducteur_"
STORAGE_PATH = Path(tempfile.gettempdir())
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

LANGUAGES = {
    "fr": "Français",
    "en": "Anglais",
    "es": "Espagnol",
    "de": "Allemand",
    "it": "Italien",
    "vi": "Vietnamien",
    "th": "Thaï",
    "id": "Indonésien",
    "ja": "Japonais",
    "ko": "Coréen",
    "zh-CN": "Chinois simplifié",
    "zh-TW": "Chinois traditionnel",
    "ru": "Russe",
}

SOURCE_LANGUAGES = {"auto": "Détection automatique", **LANGUAGES}


def admin_credentials():
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("ADMIN_USERNAME et ADMIN_PASSWORD doivent être définis dans .env.")
    return username, password


def admin_logged_in():
    return session.get("admin_authenticated") is True

ENV_PATH = Path(__file__).resolve().parent / ".env"
LARA_PROFILES_PATH = Path(__file__).resolve().parent / ".lara_key_profiles.json"
ENV_SECRET_KEYS = {"LARA_ACCESS_KEY_ID", "LARA_ACCESS_KEY_SECRET", "ADMIN_PASSWORD", "ADMIN_SESSION_SECRET"}
ENV_EDITABLE_KEYS = [
    "LARA_ACCESS_KEY_ID",
    "LARA_ACCESS_KEY_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ADMIN_SESSION_SECRET",
    "PORT",
]


def read_env_values():
    values = {}
    if not ENV_PATH.is_file():
        return values
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    except OSError:
        pass
    return values


def env_for_admin():
    values = read_env_values()
    result = {}
    for key in ENV_EDITABLE_KEYS:
        value = values.get(key, "")
        result[key] = "••••••••" if key in ENV_SECRET_KEYS and value else value
    return result


def update_env_values(updates):
    current_lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []
    normalized = {}
    for key, value in updates.items():
        if key in ENV_EDITABLE_KEYS and value is not None:
            normalized[key] = str(value).strip()

    found = set()
    output = []
    for line in current_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in normalized:
                value = normalized[key]
                if value != "":
                    safe_value = value.replace("\\", "\\\\").replace('"', '\\\"')
                    line = f'{key}="{safe_value}"'
                    found.add(key)
                elif key in ENV_SECRET_KEYS:
                    found.add(key)
                    # Champ vide dans l'interface = conserver la valeur secrète actuelle.
                else:
                    line = f'{key}=""'
                    found.add(key)
        output.append(line)

    for key in ENV_EDITABLE_KEYS:
        if key in normalized and key not in found and normalized[key] != "":
            safe_value = normalized[key].replace("\\", "\\\\").replace('"', '\\\"')
            if output and output[-1].strip():
                output.append("")
            output.append(f'{key}="{safe_value}"')

    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def read_lara_profiles():
    if not LARA_PROFILES_PATH.is_file():
        return []
    try:
        data = json.loads(LARA_PROFILES_PATH.read_text(encoding="utf-8"))
        return [item for item in data if isinstance(item, dict) and item.get("name")] if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def save_lara_profiles(profiles):
    # Écriture atomique pour éviter un fichier JSON partiellement écrit.
    LARA_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = LARA_PROFILES_PATH.with_name(LARA_PROFILES_PATH.name + ".tmp")
    try:
        tmp_path.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(LARA_PROFILES_PATH)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def active_lara_profile_name():
    values = read_env_values()
    active_id = values.get("LARA_ACCESS_KEY_ID", "")
    for profile in read_lara_profiles():
        if profile.get("access_key_id", "") == active_id:
            return profile.get("name", "")
    return ""


def lara_profiles_for_admin():
    return [
        {
            "name": p.get("name", ""),
            "access_key_id": p.get("access_key_id", ""),
            "access_key_secret": "••••••••" if p.get("access_key_secret") else "",
            "active": p.get("name", "") == active_lara_profile_name(),
        }
        for p in read_lara_profiles()
    ]


def update_runtime_lara_keys(access_key_id, access_key_secret):
    os.environ["LARA_ACCESS_KEY_ID"] = access_key_id
    os.environ["LARA_ACCESS_KEY_SECRET"] = access_key_secret


def require_admin_page():
    if admin_logged_in():
        return None
    return redirect(url_for("admin_login"))


def require_admin_api():
    if admin_logged_in():
        return None
    return jsonify(error="Authentification administrateur requise."), 401


def format_size(size):
    units = ["o", "Ko", "Mo", "Go", "To"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def storage_roots():
    """Retourne uniquement le stockage temporaire système (/tmp)."""
    return [STORAGE_PATH]


def find_storage_work(work_id):
    if not work_id or "/" in work_id or "\\" in work_id or not work_id.startswith(TEMP_PREFIX):
        return None
    for root in storage_roots():
        work = root / work_id
        try:
            if work.is_dir():
                return work
        except OSError:
            continue
    return None


def list_stored_files():
    now = time.time()
    items = []
    seen = set()
    for root in storage_roots():
        try:
            works = root.glob(f"{TEMP_PREFIX}*")
        except OSError:
            continue
        for work in works:
            try:
                work_key = str(work.resolve())
                if work_key in seen or not work.is_dir():
                    continue
                seen.add(work_key)
                created = work.stat().st_mtime
                expires = created + RETENTION_SECONDS
                metadata = {}
                metadata_path = work / "metadata.json"
                try:
                    if metadata_path.is_file():
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    metadata = {}
                files = []
                total_size = 0
                for path in work.rglob("*"):
                    if path.is_file():
                        size = path.stat().st_size
                        total_size += size
                        files.append({
                            "name": str(path.relative_to(work)),
                            "size": size,
                            "size_human": format_size(size),
                        })
                items.append({
                    "id": work.name,
                    "created": created,
                    "expires": expires,
                    "expires_in": max(0, int(expires - now)),
                    "size": total_size,
                    "size_human": format_size(total_size),
                    "files": files,
                    "metadata": metadata,
                })
            except OSError:
                continue
    items.sort(key=lambda item: item["created"], reverse=True)
    return items


def cleanup_old_files():
    """Supprime les dossiers de traduction vieux de plus de 60 jours."""
    cutoff = time.time() - RETENTION_SECONDS
    for root in storage_roots():
        try:
            works = root.glob(f"{TEMP_PREFIX}*")
        except OSError:
            continue
        for work in works:
            try:
                if work.is_dir() and work.stat().st_mtime < cutoff:
                    shutil.rmtree(work, ignore_errors=True)
            except OSError:
                pass


def cleanup_loop():
    """Exécute périodiquement le nettoyage des anciens traitements."""
    while True:
        try:
            cleanup_old_files()
        except Exception:
            app.logger.exception("Erreur lors du nettoyage automatique du stockage")
        time.sleep(CLEANUP_INTERVAL_SECONDS)


def render_admin_storage_jobs(items, kind="all"):
    parts = []
    for item in items:
        files = item.get("files", [])
        uploaded = [f for f in files if Path(f.get("name", "")).suffix.lower() in ALLOWED and not f.get("name", "").lower().startswith("traduit/")]
        translated = [f for f in files if Path(f.get("name", "")).suffix.lower() in ALLOWED and f.get("name", "").lower().startswith("traduit/")]
        wid = quote(str(item.get("id", "")), safe="")
        meta = item.get("metadata") or {}
        def cards(group):
            out = []
            for f in group:
                name = str(f.get("name", ""))
                p = quote(name, safe="")
                out.append('<div class="file-card"><img src="/api/admin/storage/file?work_id='+wid+'&path='+p+'&preview=1" onclick="window.openImage(this.src);this.focus()" tabindex="0" alt="'+escape(name, quote=True)+'" loading="lazy"><div class="file-name">'+escape(name)+'</div><div class="file-meta">'+escape(str(f.get("size_human", "")))+'</div><div class="file-actions"><a href="/api/admin/storage/file?work_id='+wid+'&path='+p+'">Télécharger</a></div><div class="file-menu"><button type="button" onclick="event.stopPropagation();this.parentElement.classList.toggle(&quot;open&quot;)">⋮</button><div class="file-menu-list"><form method="post" action="/admin/storage/'+wid+'/rename-form"><input type="hidden" name="path" value="'+escape(name, quote=True)+'"><input name="name" value="'+escape(name, quote=True)+'"><button type="submit">Renommer</button></form><form method="post" action="/api/admin/storage/'+wid+'/metadata"><input name="client_ip" placeholder="IP"><input name="created_at" type="datetime-local"><button type="submit">Modifier les données</button></form><form method="post" action="/admin/storage/'+wid+'/delete-form"><button type="submit">Supprimer</button></form></div></div></div>')
            return "".join(out)
        h = '<div class="job"><div class="job-head"><div><b>'+escape(str(item.get("id", "")))+'</b><div class="meta">'+escape(time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(item.get("created", 0))))+' · IP '+escape(str(meta.get("client_ip", "inconnue")))+' · '+escape(str(item.get("size_human", "")))+'</div></div></div>'
        if kind != "translated":
            h += '<div class="section"><h3>Images envoyées ('+str(len(uploaded))+')</h3><div class="file-grid">'+(cards(uploaded) or '<div class="empty">Aucune</div>')+'</div></div>'
        if kind != "uploaded":
            h += '<div class="section"><h3>Images traduites ('+str(len(translated))+')</h3><div class="file-grid">'+(cards(translated) or '<div class="empty">Aucune</div>')+'</div></div>'
        h += '</div>'
        parts.append(h)
    return "".join(parts) or '<div class="empty">Aucune image trouvée avec ces filtres.</div>'


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Traducteur d'images · Lara</title>
<style>
:root{--bg:#f4f7fb;--card:#fff;--text:#172033;--muted:#667085;--line:#e4e7ec;--primary:#635bff;--primary-dark:#5147e5;--success:#12b76a;--danger:#d92d20}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at 10% 0%,#e9e7ff 0,transparent 32%),radial-gradient(circle at 90% 10%,#dff7ef 0,transparent 28%),var(--bg)}
.container{max-width:980px;margin:0 auto;padding:42px 20px 50px}
.header{text-align:center;margin-bottom:26px}
.logo{width:58px;height:58px;margin:0 auto 14px;border-radius:18px;display:grid;place-items:center;background:linear-gradient(135deg,var(--primary),#8b5cf6);color:white;font-size:28px;box-shadow:0 12px 28px #635bff35}
h1{font-size:clamp(2rem,5vw,3rem);letter-spacing:-.04em;margin:0 0 8px}
.subtitle{margin:0;color:var(--muted);font-size:1.05rem}
.card{background:rgba(255,255,255,.94);border:1px solid #ffffffaa;border-radius:24px;padding:30px;box-shadow:0 20px 60px #10182812;backdrop-filter:blur(12px)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.field label{display:block;font-size:.9rem;font-weight:700;margin:0 0 8px}
select,input[type=file]{width:100%;padding:13px 14px;border:1px solid #d0d5dd;border-radius:12px;background:white;color:var(--text);font:inherit;outline:none;transition:.2s}
select:focus,input[type=file]:focus{border-color:var(--primary);box-shadow:0 0 0 4px #635bff18}
.upload{margin-top:20px;border:2px dashed #c7c9d9;border-radius:16px;padding:24px;background:#fafbff;text-align:center;transition:.2s}
.upload:hover{border-color:var(--primary);background:#f8f7ff}
.upload strong{display:block;font-size:1rem;margin-bottom:5px}
.upload small{margin:0;color:var(--muted)}
input[type=file]{margin-top:14px}
small{display:block;margin-top:8px;color:var(--muted);line-height:1.5}
.primary{width:100%;margin-top:20px;padding:14px 18px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--primary),#7c3aed);color:white;cursor:pointer;font:inherit;font-weight:800;font-size:1rem;box-shadow:0 10px 22px #635bff30;transition:.2s}
.primary:hover{transform:translateY(-1px);background:linear-gradient(135deg,var(--primary-dark),#6d28d9)}
.primary:disabled{opacity:.55;cursor:not-allowed;transform:none}
.status{margin-top:22px;padding:15px 16px;border-radius:12px;background:#f2f4f7;color:#475467;white-space:pre-wrap;min-height:50px;display:flex;align-items:center}
.status.ok{background:#ecfdf3!important;color:#067647}
.status.err{background:#fef3f2!important;color:#b42318}
.download{display:block;text-align:center;margin-top:14px;padding:13px 16px;border-radius:12px;background:#111827;color:white;text-decoration:none;font-weight:700}
.usage{margin-top:22px;border:1px solid var(--line);border-radius:16px;padding:20px;background:#fcfcfd}
.usage-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.usage-title strong{font-size:1rem}
.badge{padding:6px 9px;border-radius:999px;background:#eef4ff;color:#3538cd;font-size:.78rem;font-weight:700}
.usage-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.usage-item{padding:13px;border-radius:12px;background:white;border:1px solid var(--line);color:var(--muted);font-size:.82rem}
.usage-value{margin-top:5px;color:var(--text);font-size:1.15rem;font-weight:800}
.usage a{color:#4f46e5;font-weight:600;text-decoration:none}
.footer{display:flex;justify-content:center;gap:18px;margin-top:22px;font-size:.9rem}
.footer a{color:var(--muted);text-decoration:none}.footer a:hover{color:var(--primary)}
@media(max-width:700px){.container{padding-top:25px}.card{padding:20px;border-radius:20px}.grid,.usage-row{grid-template-columns:1fr}.header{margin-bottom:20px}}
</style>
</head>
<body>
<div class="container">
<header class="header">
<div class="logo">文</div>
<h1>Traducteur d'images</h1>
<p class="subtitle">Traduisez vos images simplement avec Lara Translate.</p>
</header>
<div class="card">
<div class="grid">
<div class="field"><label for="source">Langue source</label><select id="source">
<option value="auto">Détection automatique</option>
{% for code,name in source_languages.items() %}<option value="{{code}}">{{name}}</option>{% endfor %}
</select></div>
<div class="field"><label for="lang">Langue cible</label><select id="lang">
{% for code,name in languages.items() %}<option value="{{code}}">{{name}}</option>{% endfor %}
</select></div>
</div>
<div class="upload">
<strong>📁 Choisissez votre dossier d'images</strong>
<small>JPG, JPEG, PNG, WebP et TIFF</small>
<input id="files" type="file" webkitdirectory directory multiple accept=".jpg,.jpeg,.png,.webp,.tif,.tiff">
<small>Les images sont envoyées à Lara et les versions traduites sont regroupées dans un ZIP.</small>
</div>
<button id="start" class="primary">✨ Traduire le dossier</button>
<div id="status" class="status">En attente d'un dossier.</div>
<a id="download" class="download" href="#" download style="display:none">Télécharger le ZIP</a>

<div id="quota" class="usage">
<div class="usage-title"><strong>Utilisation Lara — ce mois</strong><span class="badge">Suivi local</span></div>
<div class="usage-row">
<div class="usage-item">Images traduites<div id="usageImages" class="usage-value">—</div></div>
<div class="usage-item">Coût estimé<div id="usageCost" class="usage-value">—</div></div>
<div class="usage-item">Tarif<div id="usagePrice" class="usage-value">—</div></div>
</div>
<small>Compteur local basé sur les traductions réussies via Lara. L'estimation utilise le tarif configuré dans l'application.</small>
<a href="https://laratranslate.com/account/api" target="_blank" rel="noopener">Voir l'utilisation officielle Lara →</a>
</div>
</div>
<footer class="footer"><a href="/admin">Administration du stockage</a></footer>
</div>
<script>
const start=document.getElementById("start"), files=document.getElementById("files");
const source=document.getElementById("source"), lang=document.getElementById("lang"), status=document.getElementById("status"), download=document.getElementById("download");
let downloadPending=document.getElementById("downloadPending");
if(!downloadPending){download.insertAdjacentHTML("afterend",' <a id="downloadPending" class="download" style="display:none" download>⬇ Télécharger le ZIP des images non traduites</a>');downloadPending=document.getElementById("downloadPending");}
const usageImages=document.getElementById("usageImages"), usageCost=document.getElementById("usageCost"), usagePrice=document.getElementById("usagePrice");
function setStatus(t,c=""){status.textContent=t;status.className="status "+c}
async function refreshUsage(){try{const r=await fetch("/api/lara-usage");const raw=await r.text();let u;try{u=JSON.parse(raw)}catch(e){throw new Error("Réponse invalide du serveur pour l'utilisation Lara (HTTP "+r.status+").")}if(!r.ok)throw new Error(u.error||"Impossible de charger l'utilisation Lara.");usageImages.textContent=u.images+" image"+(u.images>1?"s":"");usageCost.textContent=Number(u.estimated_cost_eur||0).toFixed(2).replace(".",",")+" €";usagePrice.textContent=Number(u.price_eur_per_image||0).toFixed(2).replace(".",",")+" €/image"}catch(e){usageImages.textContent="—";usageCost.textContent="—";usagePrice.textContent="—";}}
files.addEventListener("change",()=>{const n=[...files.files].filter(f=>/\\.(jpe?g|png|webp|tiff?)$/i.test(f.name)).length; if(n)setStatus(n+" image"+(n>1?"s":"")+" sélectionnée"+(n>1?"s":"")+" — prête à être traduite.")});
refreshUsage();
start.onclick=async()=>{
 const selected=[...files.files].filter(f=>/\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
 if(!selected.length){setStatus("Choisis un dossier contenant des images.","err");return}
 start.disabled=true;download.style.display="none";if(downloadPending)downloadPending.style.display="none";setStatus("Envoi des images…");
 const fd=new FormData();fd.append("source",source.value);fd.append("target",lang.value);selected.forEach(f=>fd.append("files",f,f.webkitRelativePath||f.name));
 try{const r=await fetch("/translate",{method:"POST",body:fd});const data=await r.json();if(!r.ok)throw new Error(data.error||"Erreur");
 while(true){await new Promise(x=>setTimeout(x,700));const s=await fetch("/status/"+data.job).then(x=>x.json());setStatus(s.message||"Traitement…");if(s.state==="done"){download.href="/download/"+data.job;download.style.display="block";download.textContent=s.incomplete?"⬇ Télécharger le ZIP traduit":"⬇ Télécharger le ZIP";if(downloadPending){if(s.untranslated_zip){downloadPending.href="/download/"+data.job+"/a-traduire";downloadPending.style.display="inline-block"}else{downloadPending.style.display="none"}}setStatus(s.message,"ok");await refreshUsage();break}if(s.state==="error"){setStatus(s.message||"Erreur","err");await refreshUsage();break}}
 }catch(e){setStatus(e.message||"Erreur","err")}finally{start.disabled=false}
};
</script>
</body>
</html>"""


LOGIN_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connexion administrateur</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:430px;margin:80px auto;padding:0 20px;background:#f5f7fb;color:#18202a}
.card{background:white;border-radius:18px;padding:28px;box-shadow:0 8px 30px #00000012}
label{display:block;font-weight:600;margin:16px 0 8px}input,button{width:100%;box-sizing:border-box;padding:12px;border:1px solid #d0d5dd;border-radius:10px}
button{margin-top:20px;background:#111827;color:white;border:0;cursor:pointer;font-weight:700}.err{margin-top:15px;padding:10px;border-radius:8px;background:#fef3f2;color:#b42318}a{color:#175cd3}
</style></head><body><div class="card">
<h1>Administration</h1><p>Connecte-toi pour gérer les fichiers temporaires.</p>
<form method="post"><label>Identifiant</label><input name="username" autocomplete="username" required>
<label>Mot de passe</label><input name="password" type="password" autocomplete="current-password" required>
<button type="submit">Se connecter</button></form>
{% if error %}<div class="err">{{error}}</div>{% endif %}
<p><a href="/">← Retour au traducteur</a></p></div></body></html>"""

ADMIN_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Administration · Traducteur d'images</title>
<style>
:root{--bg:#f4f7fb;--card:#fff;--text:#172033;--muted:#667085;--line:#e4e7ec;--primary:#635bff;--danger:#b42318}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text);background:var(--bg)}
.layout{display:flex;min-height:100vh}.sidebar{width:250px;background:#111827;color:#fff;padding:24px 16px;position:fixed;inset:0 auto 0 0}.brand{font-size:20px;font-weight:800;padding:8px 10px 26px}.brand span{display:inline-grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#635bff,#8b5cf6);margin-right:9px;vertical-align:middle}
.nav{display:grid;gap:6px}.nav a{color:#d1d5db;text-decoration:none;padding:12px 13px;border-radius:10px;font-weight:650}.nav a:hover,.nav a.active{background:#ffffff14;color:#fff}.nav .back{margin-top:20px;border-top:1px solid #ffffff18;padding-top:20px}
.main{margin-left:250px;width:calc(100% - 250px);padding:32px;max-width:1500px}.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}.top h1{margin:0 0 6px;font-size:30px}.muted{color:var(--muted)}.card{background:white;border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 8px 30px #1018280a;margin-bottom:20px}
.filters{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr 1fr auto;gap:10px;align-items:end}.filters label{font-size:12px;font-weight:750;color:#475467}.filters input,.filters select{width:100%;margin-top:6px;padding:11px 12px;border:1px solid #d0d5dd;border-radius:9px;font:inherit}.btn{border:0;border-radius:9px;padding:11px 14px;font-weight:750;cursor:pointer}.primary{background:var(--primary);color:#fff}.danger{background:var(--danger);color:#fff}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{padding:16px;border:1px solid var(--line);border-radius:12px;background:#fafbff}.stat b{display:block;font-size:23px;margin-top:5px}.job{border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:14px}.job-head{display:flex;justify-content:space-between;gap:15px}.job-title{font-weight:800}.job-meta{font-size:13px;color:#667085;margin-top:5px}.section{margin-top:15px}.section h3{font-size:15px;margin:0 0 10px}.file-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}.file-card{position:relative;border:1px solid var(--line);border-radius:11px;padding:9px;background:#fafafa;overflow:visible}.file-card img{width:100%;height:145px;object-fit:contain;background:#fff;border-radius:8px;cursor:zoom-in}.file-name{font-size:13px;word-break:break-word;margin-top:7px}.file-meta{font-size:12px;color:#667085;margin-top:3px}.file-actions{margin-top:6px}.file-actions a{color:#4f46e5;text-decoration:none;font-size:13px}.zip{margin-top:15px;padding:12px;border:1px dashed #d0d5dd;border-radius:10px}.empty{padding:45px;text-align:center;color:#667085}.modal{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center;padding:20px;z-index:1000}.modal.open{display:flex}.modal img{max-width:95vw;max-height:90vh;background:white;border-radius:10px}.modal-close{position:absolute;top:16px;right:20px;border:0;border-radius:50%;width:42px;height:42px;font-size:24px;cursor:pointer}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.settings-grid label{font-size:13px;font-weight:700}.settings-grid input{width:100%;margin-top:7px;padding:11px;border:1px solid #d0d5dd;border-radius:9px}.actions{grid-column:1/-1;display:flex;gap:16px;align-items:center;flex-wrap:wrap}.restart-option{font-size:13px;font-weight:650;color:#475467;display:flex;align-items:center;gap:8px}.restart-option input{width:auto;margin:0}.msg{display:none;padding:11px;border-radius:9px;margin:14px 0}.msg.ok{display:block;background:#ecfdf3;color:#067647}.msg.err{display:block;background:#fef3f2;color:#b42318}
@media(max-width:900px){.sidebar{width:210px}.main{margin-left:210px;width:calc(100% - 210px);padding:20px}.filters{grid-template-columns:1fr 1fr}.stats{grid-template-columns:1fr 1fr}}
@media(max-width:650px){.layout{display:block}.sidebar{position:static;width:auto;padding:12px}.brand{padding:5px 8px 12px}.nav{display:flex;overflow:auto}.nav .back{margin:0;border:0;padding:12px}.main{margin:0;width:auto}.top{display:block}.filters,.settings-grid{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.actions{grid-column:auto}}
</style></head><body>
<div class="layout"><aside class="sidebar"><div class="brand"><span>文</span> Administration</div><nav class="nav">
<a href="/admin" class="active">📊 Tableau de bord</a><a href="/admin/images">🖼️ Images</a><a href="/admin/config">⚙️ Configuration</a><a class="back" href="/">← Retour au traducteur</a><a href="/admin/logout">↪ Déconnexion</a>
</nav></aside><main class="main"><div class="top"><div><h1>Tableau de bord</h1><div class="muted">Vue globale du stockage temporaire et des traitements.</div></div></div>
<div class="stats" id="stats"></div><div class="card"><h2>Derniers traitements</h2><div id="recent"></div></div></main></div>
<script>
function fmt(ts){return new Date(ts*1000).toLocaleString("fr-FR")}function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
async function load(){const r=await fetch("/api/admin/storage");if(!r.ok)return;const d=await r.json(),items=d.items;document.getElementById("stats").innerHTML=[["Dossiers",items.length],["Images envoyées",items.reduce((n,x)=>n+(x.metadata?.image_count||0),0)],["Stockage",items.reduce((n,x)=>n+x.size,0)/1024],["Dernière activité",items[0]?fmt(items[0].created):"—"]].map((x,i)=>'<div class="stat"><span class="muted">'+x[0]+'</span><b>'+((i===2)?(x[1]/1024>=1024?(x[1]/1024).toFixed(1)+" Mo":x[1].toFixed(1)+" Ko"):x[1])+'</b></div>').join("");
document.getElementById("recent").innerHTML=items.slice(0,8).map(x=>'<div class="job"><div class="job-head"><div><div class="job-title">'+esc(x.id)+'</div><div class="job-meta">'+fmt(x.created)+' · '+esc(x.metadata?.client_ip||"inconnue")+' · '+esc(x.size_human)+'</div></div><a href="/admin/images?work_id='+encodeURIComponent(x.id)+'">Voir les images →</a></div></div>').join("")||'<div class="empty">Aucun traitement.</div>'}load();setInterval(load,10000);
</script></main></div></body></html>"""

IMAGES_ADMIN_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Administration · Images</title>
<style>
body{margin:0;font-family:Inter,system-ui,sans-serif;background:#f4f7fb;color:#172033}
.layout{display:flex;min-height:100vh}.sidebar{width:250px;background:#111827;color:#fff;padding:24px 16px;position:fixed;inset:0 auto 0 0}
.brand{font-size:20px;font-weight:800;padding:8px 10px 26px}.brand span{display:inline-grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#635bff,#8b5cf6);margin-right:9px;vertical-align:middle}
.nav{display:grid;gap:6px}.nav a{color:#d1d5db;text-decoration:none;padding:12px;border-radius:10px;font-weight:650}.nav a:hover,.nav a.active{background:#ffffff14;color:#fff}
.main{margin-left:250px;padding:32px;width:calc(100% - 250px);max-width:1500px}.card,.job{background:#fff;border:1px solid #e4e7ec;border-radius:16px;padding:20px;margin-bottom:18px}
.filters{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr 1fr auto;gap:9px;align-items:end}.filters label{font-size:12px;font-weight:750}.filters input{width:100%;margin-top:6px;padding:10px;border:1px solid #d0d5dd;border-radius:9px;box-sizing:border-box}
.btn{border:0;border-radius:9px;padding:10px 14px;background:#635bff;color:#fff;font-weight:750;cursor:pointer}.subfilters{display:flex;gap:8px;margin-top:12px}.tab{border:1px solid #d0d5dd;background:#fff;padding:8px 12px;border-radius:9px;cursor:pointer}.tab.active{background:#eef4ff;color:#3538cd;border-color:#c7d7fe}
.job-head{display:flex;justify-content:space-between;gap:12px}.meta{color:#667085;font-size:13px;margin-top:5px}.section{margin-top:15px}.section h3{font-size:15px;margin:0 0 10px}
.file-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin-top:10px}.file-card{border:1px solid #e4e7ec;border-radius:11px;padding:9px;background:#fafafa}.file-card{position:relative}.file-card img{width:100%;height:145px;object-fit:contain;background:white;border-radius:8px;cursor:zoom-in}.file-card img:focus{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:auto;height:auto;max-width:92vw;max-height:92vh;z-index:1000;outline:4px solid #fff;box-shadow:0 0 0 100vmax #000b;cursor:zoom-out}.file-menu{position:absolute;right:8px;bottom:6px;z-index:10000;pointer-events:auto}.file-menu>button{position:relative;z-index:51;border:0;background:#fff;width:32px;height:32px;border-radius:50%;box-shadow:0 2px 8px #0003;font-size:20px;cursor:pointer;display:grid;place-items:center}.file-menu-list{display:none;position:absolute;right:0;bottom:38px;background:#fff;border:1px solid #d0d5dd;border-radius:10px;box-shadow:0 8px 24px #0002;min-width:150px;padding:5px;z-index:10001;pointer-events:auto}.file-menu.open .file-menu-list{display:block}.file-menu-list button{display:block;width:100%;border:0;background:#fff;text-align:left;padding:9px;border-radius:7px;cursor:pointer;pointer-events:auto;position:relative;z-index:10002}.file-menu-list button:hover{background:#f2f4f7}.file-name{font-size:13px;word-break:break-word;margin-top:7px}.file-meta{font-size:12px;color:#667085}.file-actions{margin-top:6px}.file-actions a{color:#4f46e5;text-decoration:none}
.danger{border:0;border-radius:8px;padding:9px 12px;background:#b42318;color:#fff;cursor:pointer}.zip{margin-top:14px;padding:11px;border:1px dashed #d0d5dd}.empty{text-align:center;padding:40px;color:#667085}.hint{font-size:12px;color:#667085}
.modal{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center;z-index:20}.modal.open{display:flex}.modal img{max-width:95vw;max-height:90vh;background:#fff}.modal button{position:absolute;right:20px;top:15px;border:0;border-radius:50%;width:42px;height:42px;font-size:24px;cursor:pointer}
@media(max-width:900px){.sidebar{width:210px}.main{margin-left:210px;width:calc(100% - 210px);padding:20px}.filters{grid-template-columns:1fr 1fr}}
@media(max-width:650px){.layout{display:block}.sidebar{position:static;width:auto;padding:12px}.nav{display:flex;overflow:auto}.main{margin:0;width:auto}.filters{grid-template-columns:1fr}.job-head{display:block}}
</style></head><body>
<div class="layout"><aside class="sidebar"><div class="brand"><span>文</span> Administration</div><nav class="nav">
<a href="/admin">📊 Tableau de bord</a><a href="/admin/images" class="active">🖼️ Images</a><a href="/admin/config">⚙️ Configuration</a><a href="/">← Retour au traducteur</a><a href="/admin/logout">↪ Déconnexion</a>
</nav></aside>
<main class="main"><h1>Images</h1><p class="meta">Images réellement stockées dans /tmp, avec aperçu, téléchargement et suppression.</p>
<div class="card"><form class="filters" method="get" action="/admin/images">
<label>Date début<input id="from" name="from" type="date"></label><label>Date fin<input id="to" name="to" type="date"></label><label>IP<input id="ip" name="ip" placeholder="ex. 192.168.1.10"></label><label>Taille min (Ko)<input id="min" name="min" type="number" min="0" step="1"></label><label>Taille max (Ko)<input id="max" name="max" type="number" min="0" step="1"></label><button id="filter" class="btn" type="submit">Filtrer</button>
</form>
<div class="subfilters"><a class="tab" href="/admin/images">Tout</a><a class="tab" href="/admin/images?view=uploaded">Envoyées</a><a class="tab" href="/admin/images?view=translated">Traduites</a></div>
<p class="hint">Les images envoyées sont les fichiers originaux du traitement. Les images traduites sont dans le dossier <code>traduit/</code>.</p></div>
<div id="jobs"></div></main></div>
<div id="modal" class="modal"><button id="closeModal" type="button">×</button><img id="big" alt="Aperçu"></div>
<script>
(function(){
"use strict";
var all=__INITIAL_STORAGE__;
var kind="all";
const fromEl=document.getElementById("from"), toEl=document.getElementById("to"), ipEl=document.getElementById("ip");
const minEl=document.getElementById("min"), maxEl=document.getElementById("max"), jobs=document.getElementById("jobs");
const modal=document.getElementById("modal"), big=document.getElementById("big");

window.openImage=function(src){
  var modal=document.getElementById("modal");
  var big=document.getElementById("big");
  if(!modal||!big)return;
  big.src=src;
  modal.classList.add("open");
  document.body.style.overflow="hidden";
}
window.closeImage=function(){
  var modal=document.getElementById("modal");
  var big=document.getElementById("big");
  if(modal)modal.classList.remove("open");
  if(big)big.src="";
  document.body.style.overflow="";
}
function esc(value){
  return String(value == null ? "" : value).replace(/[&<>"']/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];
  });
}
function fmt(ts){return new Date(ts*1000).toLocaleString("fr-FR");}
function fileUrl(item,file,preview){
  return "/api/admin/storage/file?work_id="+encodeURIComponent(item.id)+"&path="+encodeURIComponent(file.name)+(preview?"&preview=1":"");
}
function isImage(file){return /\.(jpe?g|png|webp|tiff?)$/i.test(file.name);}
function render(){
  const from=fromEl.value?new Date(fromEl.value+"T00:00:00").getTime()/1000:-Infinity;
  const to=toEl.value?new Date(toEl.value+"T23:59:59").getTime()/1000:Infinity;
  const needle=ipEl.value.trim().toLowerCase();
  const mi=minEl.value?Number(minEl.value)*1024:0;
  const ma=maxEl.value?Number(maxEl.value)*1024:Infinity;
  const items=all.filter(function(i){
    return i.created>=from && i.created<=to &&
      (!needle || String((i.metadata||{}).client_ip||"").toLowerCase().includes(needle)) &&
      i.size>=mi && i.size<=ma;
  });
  if(!items.length){jobs.innerHTML='<div class="empty">Aucune image trouvée avec ces filtres.</div>';return;}
  jobs.innerHTML=items.map(function(item){
    const uploaded=item.files.filter(function(f){return isImage(f) && !/^traduit\//i.test(f.name);});
    const translated=item.files.filter(function(f){return isImage(f) && /^traduit\//i.test(f.name);});
    const showUploaded=kind!=="translated", showTranslated=kind!=="uploaded";
    function card(file){
      const preview=fileUrl(item,file,true), download=fileUrl(item,file,false);
      return '<div class="file-card"><img src="'+preview+'" data-preview="'+preview+'" onclick="window.openImage(this.src)" alt="'+esc(file.name)+'"><div class="file-name">'+esc(file.name)+'</div><div class="file-meta">'+esc(file.size_human)+'</div><div class="file-actions"><a href="'+download+'">Télécharger</a></div><div class="file-menu"><button type="button" onclick="event.stopPropagation();this.parentElement.classList.toggle(&quot;open&quot;)">⋮</button><div class="file-menu-list"><form method="post" action="/admin/storage/'+esc(item.id)+'/rename-form"><input type="hidden" name="path" value="'+esc(file.name)+'"><input name="name" value="'+esc(file.name)+'"><button type="submit">Renommer</button></form><form method="post" action="/api/admin/storage/'+esc(item.id)+'/metadata"><input name="client_ip" value="'+esc((item.metadata||{}).client_ip||"")+'"><input name="created_at" type="datetime-local"><button type="submit">Modifier les données</button></form><form method="post" action="/admin/storage/'+esc(item.id)+'/delete-form"><button type="submit">Supprimer</button></form></div></div></div>';
    }
    let html='<div class="job"><div class="job-head"><div><b>'+esc(item.id)+'</b><div class="meta">'+fmt(item.created)+' · IP '+esc((item.metadata||{}).client_ip||"inconnue")+' · '+esc(item.size_human)+'</div></div><button class="danger delete-job" data-id="'+esc(item.id)+'">Supprimer</button></div>';
    if(showUploaded) html+='<div class="section"><h3>Images envoyées ('+uploaded.length+')</h3><div class="file-grid">'+(uploaded.length?uploaded.map(card).join(""):'<div class="empty">Aucune</div>')+'</div></div>';
    if(showTranslated) html+='<div class="section"><h3>Images traduites ('+translated.length+')</h3><div class="file-grid">'+(translated.length?translated.map(card).join(""):'<div class="empty">Aucune</div>')+'</div></div>';
    const zips=item.files.filter(function(f){return /\.zip$/i.test(f.name);});
    html+='<div class="zip">'+(zips.length?zips.map(function(z){return '<a href="'+fileUrl(item,z,false)+'">Télécharger '+esc(z.name)+'</a>';}).join(" · "):"Aucun ZIP")+'</div></div>';
    return html;
  }).join("");
  jobs.querySelectorAll(".file-card img").forEach(function(img){img.addEventListener("click",function(){big.src=img.getAttribute("src");modal.classList.add("open");});});
  jobs.querySelectorAll(".file-menu>button").forEach(function(btn){btn.addEventListener("click",function(e){e.stopPropagation();var menu=btn.parentElement;document.querySelectorAll(".file-menu.open").forEach(function(m){if(m!==menu)m.classList.remove("open");});menu.classList.toggle("open");});});
  jobs.querySelectorAll(".file-menu-list button").forEach(function(btn){btn.addEventListener("click",function(e){e.stopPropagation();var a=btn.dataset.action;if(a==="rename")window.renameFile(btn.dataset.id,btn.dataset.path);if(a==="edit-meta")window.editMeta(btn.dataset.id);if(a==="delete")window.removeItem(btn.dataset.id);});});
  jobs.querySelectorAll(".delete-job").forEach(function(btn){btn.addEventListener("click",function(){window.removeItem(btn.dataset.id);});});
}
function load(){
  var requested=(window.location.search.match(/[?&]work_id=([^&]+)/)||[])[1]||"";
  if(requested){
    try{requested=decodeURIComponent(requested);}catch(e){}
    var found=all.filter(function(item){return item.id===requested;});
    if(!found.length){
      jobs.innerHTML='<div class="empty">Ce traitement n’existe plus dans /tmp.</div>';
      return;
    }
    all=found;
  }
  render();
}
window.renameFile=async function(id,path){
  var old=path.split("/").pop(), name=window.prompt("Nouveau nom du fichier :",old);
  if(!name||name===old)return false;
  var r=await fetch("/api/admin/storage/"+encodeURIComponent(id)+"/rename",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:path,name:name})});
  var d=await r.json(); if(!r.ok){alert(d.error||"Renommage impossible.");return false;} load(); return false;
}
window.editMeta=async function(id){
  var item=all.find(function(x){return x.id===id;}); if(!item)return;
  var ip=window.prompt("Adresse IP :",String((item.metadata||{}).client_ip||"")); if(ip===null)return;
  var d=new Date(item.created*1000), pad=function(n){return String(n).padStart(2,"0");};
  var cur=d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+"T"+pad(d.getHours())+":"+pad(d.getMinutes());
  var date=window.prompt("Date et heure (AAAA-MM-JJTHH:MM) :",cur); if(date===null)return;
  var r=await fetch("/api/admin/storage/"+encodeURIComponent(id)+"/metadata",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({client_ip:ip,created_at:date})});
  var data=await r.json(); if(!r.ok){alert(data.error||"Modification impossible.");return false;} load(); return false;
}
window.removeItem=async function(id){
  if(!window.confirm("Supprimer définitivement ce dossier et toutes ses images ?"))return false;
  try{await fetch("/api/admin/storage/"+encodeURIComponent(id),{method:"DELETE"});}finally{load();} return false;
}
document.querySelector(".filters").addEventListener("submit",function(e){e.preventDefault();render();});
document.querySelectorAll(".tab").forEach(function(btn){btn.addEventListener("click",function(){
  if(btn.dataset.f){kind=btn.dataset.f; document.querySelectorAll(".tab").forEach(function(x){x.classList.toggle("active",x===btn);}); render();}
});});
document.getElementById("closeModal").addEventListener("click",closeImage);
modal.addEventListener("click",function(e){if(e.target===modal)closeImage();});
document.addEventListener("keydown",function(e){if(e.key==="Escape")closeImage();});
render();
window.setInterval(function(){
  fetch("/api/admin/storage",{cache:"no-store"})
    .then(function(response){return response.ok?response.json():null;})
    .then(function(data){
      if(data && data.items){
        var requested=(window.location.search.match(/[?&]work_id=([^&]+)/)||[])[1]||"";
        try{requested=decodeURIComponent(requested);}catch(e){}
        all=requested?data.items.filter(function(item){return item.id===requested;}):data.items;
        render();
      }
    }).catch(function(){});
})();

</script></body></html>"""


CONFIG_ADMIN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Administration · Configuration</title><style>
body{margin:0;font-family:Inter,system-ui,sans-serif;background:#f4f7fb;color:#172033}.layout{display:flex;min-height:100vh}.sidebar{width:250px;background:#111827;color:#fff;padding:24px 16px;position:fixed;inset:0 auto 0 0}.brand{font-size:20px;font-weight:800;padding:8px 10px 26px}.brand span{display:inline-grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#635bff,#8b5cf6);margin-right:9px;vertical-align:middle}.nav{display:grid;gap:6px}.nav a{color:#d1d5db;text-decoration:none;padding:12px;border-radius:10px;font-weight:650}.nav a:hover,.nav a.active{background:#ffffff14;color:#fff}.main{margin-left:250px;padding:32px;width:calc(100% - 250px);max-width:1100px}.card{background:#fff;border:1px solid #e4e7ec;border-radius:16px;padding:24px}.profile-card{margin-bottom:22px;padding:18px;border:1px solid #e4e7ec;border-radius:13px;background:#fafbff}.profile-card h2{margin:0 0 5px;font-size:17px}.profile-card p{margin:0 0 14px;color:#667085;font-size:13px}.profile-row{display:flex;gap:9px;margin-top:9px}.profile-row select,.profile-row input{flex:1;min-width:0;padding:11px;border:1px solid #d0d5dd;border-radius:9px;font:inherit}.profile-btn{border:0;border-radius:9px;padding:10px 13px;background:#635bff;color:#fff;font-weight:750;cursor:pointer}.danger-btn{background:#b42318}.profile-msg{display:none;margin-top:10px;padding:9px;border-radius:8px;font-size:13px}.profile-msg.show{display:block;background:#ecfdf3;color:#067647}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px}.grid label{font-size:13px;font-weight:700}.grid input{width:100%;margin-top:7px;padding:12px;border:1px solid #d0d5dd;border-radius:9px;box-sizing:border-box}.actions{grid-column:1/-1;display:flex;gap:12px;align-items:center}.save{border:0;border-radius:9px;padding:12px 16px;background:#635bff;color:#fff;font-weight:800;cursor:pointer}.msg{display:none;padding:11px;border-radius:9px;margin-top:15px}.msg.ok{display:block;background:#ecfdf3;color:#067647}.msg.err{display:block;background:#fef3f2;color:#b42318}@media(max-width:650px){.layout{display:block}.sidebar{position:static;width:auto;padding:12px}.nav{display:flex;overflow:auto}.main{margin:0;width:auto;padding:20px}.grid{grid-template-columns:1fr}.actions{grid-column:auto}}
</style></head><body><div class="layout"><aside class="sidebar"><div class="brand"><span>文</span> Administration</div><nav class="nav"><a href="/admin">📊 Tableau de bord</a><a href="/admin/images">🖼️ Images</a><a href="/admin/config" class="active">⚙️ Configuration</a><a href="/">← Retour au traducteur</a><a href="/admin/logout">↪ Déconnexion</a></nav></aside><main class="main"><h1>Configuration</h1><p>Modification sécurisée du fichier <code>.env</code>.</p><div class="card"><div id="msg" class="msg"></div><div class="profile-card"><h2>🔑 Configurations Lara</h2><p>Conserve plusieurs comptes/clés Lara et bascule rapidement entre eux.</p><div class="profile-row"><select id="profiles"><option value="">Choisir une configuration enregistrée…</option></select><button type="button" class="profile-btn" id="useProfile">Utiliser</button><button type="button" class="profile-btn danger-btn" id="deleteProfile">Supprimer</button></div><div class="profile-row"><input id="profileName" placeholder="Nom, ex. Compte principal"><button type="button" class="profile-btn" id="saveProfile">💾 Enregistrer les clés actuelles</button></div><p class="hint">Les champs de clé restent vides pour protéger les secrets. Si tu ne les renseignes pas ici, l'application enregistre automatiquement les clés actuellement présentes dans <code>.env</code>.</p><div id="profileMsg" class="profile-msg"></div></div><form id="form" class="grid">
<label>Clé Lara — Access Key ID<input name="LARA_ACCESS_KEY_ID" type="password" placeholder="Laisser vide pour conserver"></label><label>Clé Lara — Access Key Secret<input name="LARA_ACCESS_KEY_SECRET" type="password" placeholder="Laisser vide pour conserver"></label><label>Identifiant administrateur<input name="ADMIN_USERNAME"></label><label>Mot de passe administrateur<input name="ADMIN_PASSWORD" type="password" placeholder="Laisser vide pour conserver"></label><label>Secret de session<input name="ADMIN_SESSION_SECRET" type="password" placeholder="Laisser vide pour conserver"></label><label>Port du serveur<input name="PORT" type="number" min="1" max="65535"></label><div class="actions"><button class="save">💾 Enregistrer</button><label class="restart-option"><input id="restart" type="checkbox"> Redémarrer automatiquement le service après l'enregistrement</label></div></form></div></main></div><script>
const form=document.getElementById("form"),msg=document.getElementById("msg"),restart=document.getElementById("restart"),profiles=document.getElementById("profiles"),profileName=document.getElementById("profileName"),profileMsg=document.getElementById("profileMsg");function show(t,e=false){msg.textContent=t;msg.className="msg "+(e?"err":"ok")}async function load(){const r=await fetch("/api/admin/env");const d=await r.json();if(!r.ok)return show(d.error,true);Object.entries(d.values).forEach(([k,v])=>{const e=form.elements[k];if(e)e.value=v==="••••••••"?"":v})}form.onsubmit=async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(form));const r=await fetch("/api/admin/env",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});const d=await r.json();if(!r.ok){show(d.error||"Erreur d'enregistrement",true);return}if(restart.checked){show("Configuration enregistrée. Redémarrage du service…");await fetch("/api/admin/restart",{method:"POST"});setTimeout(()=>location.href="/",2200)}else{show(d.message||"Configuration enregistrée.")}};async function loadProfiles(){const r=await fetch("/api/admin/lara-profiles");const d=await r.json();if(!r.ok){pmsg(d.error||"Impossible de charger les configurations.");return}profiles.innerHTML='<option value="">Choisir une configuration enregistrée…</option>'+d.profiles.map(p=>'<option value="'+ep(p.name)+'">'+(p.active?"✓ Actif — ":"")+ep(p.name)+' — '+ep(p.access_key_id)+'</option>').join("")}
function ep(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function pmsg(t){profileMsg.textContent=t;profileMsg.className="profile-msg show"}
document.getElementById("saveProfile").onclick=async()=>{const name=profileName.value.trim(),id=form.elements.LARA_ACCESS_KEY_ID.value.trim(),secret=form.elements.LARA_ACCESS_KEY_SECRET.value.trim();if(!name)return pmsg("Indique un nom.");pmsg("Enregistrement en cours…");try{const r=await fetch("/api/admin/lara-profiles",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,access_key_id:id,access_key_secret:secret})});const d=await r.json();if(!r.ok)return pmsg(d.error||"Erreur.");pmsg(d.message+" Fichier local : .lara_key_profiles.json");profileName.value="";await loadProfiles()}catch(e){pmsg("Impossible de contacter le serveur : "+e.message)}}
document.getElementById("useProfile").onclick=async()=>{if(!profiles.value)return pmsg("Choisis une configuration.");const r=await fetch("/api/admin/lara-profiles/use",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:profiles.value})});const d=await r.json();if(!r.ok)return pmsg(d.error||"Erreur.");form.elements.LARA_ACCESS_KEY_ID.value="";form.elements.LARA_ACCESS_KEY_SECRET.value="";pmsg(d.message+" Les clés sont actives immédiatement et enregistrées dans .env.")}
document.getElementById("deleteProfile").onclick=async()=>{if(!profiles.value)return pmsg("Choisis une configuration.");if(!confirm("Supprimer cette configuration enregistrée ?"))return;const r=await fetch("/api/admin/lara-profiles/"+encodeURIComponent(profiles.value),{method:"DELETE"});const d=await r.json();if(!r.ok)return pmsg(d.error||"Erreur.");pmsg("Configuration supprimée.");await loadProfiles()}
load();loadProfiles();
</script></main></div></body></html>"""

def set_job(job_id, **values):
    with LOCK:
        JOBS.setdefault(job_id, {}).update(values)


def worker(job_id, files, source, target, client_ip):
    # Une traduction entière reste rattachée à la clé utilisée au démarrage.
    usage_key_id = os.getenv("LARA_ACCESS_KEY_ID", "").strip()
    work = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(STORAGE_PATH)))
    out = work / "traduit"
    pending = work / "a_traduire"
    out.mkdir()
    pending.mkdir()
    try:
        total = len(files)
        metadata_path = work / "metadata.json"
        try:
            metadata_path.write_text(json.dumps({
                "job_id": job_id,
                "created_at": time.time(),
                "source": source,
                "target": target,
                "client_ip": client_ip,
                "image_count": total,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

        results = []
        billed_images = 0
        interrupted_at = None
        interruption_reason = ""

        for i, item in enumerate(files, 1):
            src = work / f"input_{i}{Path(item['name']).suffix.lower()}"
            src.write_bytes(item["data"])
            name = secure_filename(Path(item["name"]).name) or f"image_{i}.png"
            dest = out / name
            set_job(job_id, message=f"Image {i}/{total} : Lara Translate…")
            no_text = False
            try:
                used_lara = translate_image_with_lara(src, dest, target, source)
            except Exception as exc:
                error_text = str(exc)
                no_text = (
                    "No text found in the image" in error_text
                    or "UnprocessableEntityError" in error_text
                    or "(HTTP 422)" in error_text
                )
                quota_exceeded = (
                    "(HTTP 429)" in error_text
                    or "HTTP 429" in error_text
                    or "exceeded your \"api_translation_chars\" quota" in error_text
                    or ("quota" in error_text.lower() and "429" in error_text)
                )
                if quota_exceeded:
                    interrupted_at = i
                    interruption_reason = f"Quota Lara dépassé après {len(results)} image(s) traitée(s)."
                    set_job(job_id, message=f"Image {i}/{total} : quota Lara dépassé, arrêt du traitement…")
                    break
                if not no_text:
                    interrupted_at = i
                    interruption_reason = f"Traitement interrompu : {type(exc).__name__}: {exc}"
                    raise
                shutil.copy2(src, dest)
                used_lara = False
                set_job(job_id, message=f"Image {i}/{total} : aucun texte détecté, image conservée")

            if used_lara:
                record_image(usage_key_id)
                billed_images += 1
            results.append(dest)

            if used_lara:
                set_job(job_id, message=f"Image {i}/{total} terminée")
            elif no_text:
                set_job(job_id, message=f"Image {i}/{total} terminée (aucun texte détecté)")

        zip_path = work / "images_traduites.zip"
        set_job(job_id, message="Création du ZIP…")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for path in results:
                if Path(path).is_file():
                    z.write(path, path.name)

        incomplete = interrupted_at is not None
        untranslated_zip = None

        if incomplete:
            # Toute image qui n'a pas encore été traitée est conservée dans un
            # second dossier puis dans un second ZIP. Cela inclut l'image sur
            # laquelle l'erreur est survenue.
            for i, item in enumerate(files, 1):
                if i < interrupted_at:
                    continue
                name = secure_filename(Path(item["name"]).name) or f"image_{i}.png"
                destination = pending / name
                if destination.exists():
                    stem = destination.stem
                    suffix = destination.suffix
                    destination = pending / f"{stem}_{i}{suffix}"
                try:
                    destination.write_bytes(item["data"])
                except OSError:
                    pass

            untranslated_zip_path = work / "images_a_traduire.zip"
            with zipfile.ZipFile(untranslated_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for path in pending.iterdir():
                    if path.is_file():
                        z.write(path, path.name)
            untranslated_zip = str(untranslated_zip_path)

            translated_count = len(results)
            pending_count = len(list(pending.iterdir()))
            message = interruption_reason
            if translated_count:
                message += f" ZIP traduit disponible ({translated_count} image(s))."
            else:
                message += " Aucune image traduite disponible."
            if pending_count:
                message += f" ZIP à traduire disponible ({pending_count} image(s) restante(s))."

            set_job(
                job_id,
                state="done",
                message=message,
                zip=str(zip_path),
                untranslated_zip=untranslated_zip,
                incomplete=True,
            )
        else:
            skipped = total - billed_images
            if skipped:
                message = f"{total} image(s) traitée(s), dont {skipped} sans texte détecté."
                if billed_images:
                    message += f" {billed_images} appel(s) Lara."
                set_job(job_id, state="done", message=message, zip=str(zip_path))
            else:
                message = f"{total} image(s) traduite(s)."
                set_job(job_id, state="done", message=message, zip=str(zip_path))

    except Exception as exc:
        # Même en cas d'erreur inattendue, produire les deux ZIP :
        # 1) les images déjà traduites ;
        # 2) l'image en échec + toutes les suivantes.
        try:
            zip_path = work / "images_traduites.zip"
            existing_results = [p for p in results if Path(p).is_file()] if "results" in locals() else []
            if not zip_path.exists():
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                    for path in existing_results:
                        z.write(path, path.name)

            if interrupted_at is None:
                interrupted_at = min(len(existing_results) + 1, total) if total else 1

            for i, item in enumerate(files, 1):
                if i < interrupted_at:
                    continue
                name = secure_filename(Path(item["name"]).name) or f"image_{i}.png"
                destination = pending / name
                if destination.exists():
                    destination = pending / f"{destination.stem}_{i}{destination.suffix}"
                try:
                    destination.write_bytes(item["data"])
                except OSError:
                    pass

            untranslated_zip_path = work / "images_a_traduire.zip"
            with zipfile.ZipFile(untranslated_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for path in pending.iterdir():
                    if path.is_file():
                        z.write(path, path.name)

            count = len(existing_results)
            pending_count = len(list(pending.iterdir()))
            message = f"Traitement interrompu : {type(exc).__name__}: {exc}"
            if count:
                message += f" ZIP traduit disponible ({count} image(s))."
            if pending_count:
                message += f" ZIP à traduire disponible ({pending_count} image(s) restante(s))."

            set_job(
                job_id,
                state="done",
                message=message,
                zip=str(zip_path),
                untranslated_zip=str(untranslated_zip_path),
                incomplete=True,
            )
        except Exception as zip_exc:
            set_job(job_id, state="error", message=f"❌ {type(exc).__name__}: {exc} (création des ZIP impossible : {zip_exc})")
    finally:
        set_job(job_id, work=str(work))


@app.get("/")
def index():
    return render_template_string(PAGE, languages=LANGUAGES, source_languages=SOURCE_LANGUAGES)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_logged_in():
        return redirect(url_for("admin"))
    error = None
    if request.method == "POST":
        try:
            expected_user, expected_password = admin_credentials()
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password):
                session.clear()
                session["admin_authenticated"] = True
                return redirect(url_for("admin"))
            error = "Identifiant ou mot de passe incorrect."
        except RuntimeError as exc:
            error = str(exc)
    return render_template_string(LOGIN_PAGE, error=error)


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/admin")
def admin():
    auth = require_admin_page()
    if auth:
        return auth
    return ADMIN_PAGE


@app.get("/admin/images")
def admin_images():
    auth = require_admin_page()
    if auth:
        return auth
    initial_items = list_stored_files()
    view = request.args.get("view", "all")
    if view not in {"all", "uploaded", "translated"}:
        view = "all"
    ip = request.args.get("ip", "").strip().lower()
    start = request.args.get("from", "")
    end = request.args.get("to", "")
    try:
        min_size = max(0, float(request.args.get("min", "0") or 0)) * 1024
    except (TypeError, ValueError):
        min_size = 0
    try:
        max_size = float(request.args.get("max", "") or "inf") * 1024
    except (TypeError, ValueError):
        max_size = float("inf")
    filtered = []
    for item in initial_items:
        if ip and ip not in str((item.get("metadata") or {}).get("client_ip", "")).lower():
            continue
        if item.get("size", 0) < min_size or item.get("size", 0) > max_size:
            continue
        if start:
            try:
                if item.get("created", 0) < time.mktime(time.strptime(start, "%Y-%m-%d")):
                    continue
            except ValueError:
                pass
        if end:
            try:
                if item.get("created", 0) > time.mktime(time.strptime(end, "%Y-%m-%d")) + 86399:
                    continue
            except ValueError:
                pass
        filtered.append(item)
    initial_json = json.dumps(initial_items, ensure_ascii=False, separators=(",", ":"))
    initial_json = initial_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = IMAGES_ADMIN_PAGE.replace("__INITIAL_STORAGE__", initial_json)
    page = page.replace('<div id="jobs"></div>', '<div id="jobs">'+render_admin_storage_jobs(filtered, view)+'</div>')
    return page


@app.get("/admin/config")
def admin_config():
    auth = require_admin_page()
    if auth:
        return auth
    return CONFIG_ADMIN_PAGE


@app.route("/api/admin/env", methods=["GET", "POST"])
def admin_env():
    auth = require_admin_api()
    if auth:
        return auth
    if request.method == "GET":
        if not ENV_PATH.exists():
            return jsonify(error="Le fichier .env est introuvable."), 404
        return jsonify(values=env_for_admin())
    try:
        data = request.get_json(silent=True) or {}
        updates = {key: data.get(key) for key in ENV_EDITABLE_KEYS if key in data}
        if "PORT" in updates and updates["PORT"]:
            try:
                port = int(str(updates["PORT"]))
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                return jsonify(error="Le port doit être un nombre entre 1 et 65535."), 400
        update_env_values(updates)
        return jsonify(ok=True, message="Configuration .env enregistrée. Redémarre l'application pour appliquer les changements.")
    except OSError as exc:
        return jsonify(error=f"Impossible d'enregistrer le fichier .env : {exc}"), 500


@app.post("/api/admin/restart")
def admin_restart():
    auth = require_admin_api()
    if auth:
        return auth

    def restart_process():
        # Le nouveau processus doit être complètement détaché avant d'arrêter
        # celui qui sert actuellement Flask. stdin/stdout/stderr sont fermés
        # pour que le redémarrage survive à l'arrêt du processus courant.
        time.sleep(0.8)
        base_dir = Path(__file__).resolve().parent
        run_script = base_dir / "run.sh"
        restart_log = base_dir / "restart.log"
        try:
            if run_script.is_file():
                command = ["bash", str(run_script)]
            else:
                command = [
                    "bash",
                    "-lc",
                    f"set -a; source {ENV_PATH!s}; set +a; exec {Path(__file__).resolve()}",
                ]

            with restart_log.open("a", encoding="utf-8") as log:
                subprocess.Popen(
                    command,
                    cwd=str(base_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                )

            # L'ancien serveur libère immédiatement le port. Le nouveau
            # processus a déjà été lancé indépendamment de celui-ci.
            os._exit(0)
        except Exception as exc:
            try:
                restart_log.write_text(
                    f"Échec du redémarrage automatique : {type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
            except OSError:
                pass

    threading.Thread(target=restart_process, daemon=True).start()
    return jsonify(ok=True, message="Redémarrage automatique lancé. Le nouveau service va démarrer.")


@app.get("/api/admin/lara-profiles")
def admin_lara_profiles():
    auth = require_admin_api()
    if auth:
        return auth
    return jsonify(profiles=lara_profiles_for_admin())


@app.post("/api/admin/lara-profiles")
def admin_save_lara_profile():
    auth = require_admin_api()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    access_key_id = str(data.get("access_key_id", "")).strip()
    access_key_secret = str(data.get("access_key_secret", "")).strip()

    # Le formulaire masque volontairement les secrets déjà présents dans .env.
    # Si les champs sont laissés vides, on sauvegarde donc les clés actuellement actives.
    current_env = read_env_values()
    if not access_key_id:
        access_key_id = current_env.get("LARA_ACCESS_KEY_ID", "").strip()
    if not access_key_secret:
        access_key_secret = current_env.get("LARA_ACCESS_KEY_SECRET", "").strip()

    if not name:
        return jsonify(error="Donne un nom à cette configuration."), 400
    if not access_key_id or not access_key_secret:
        return jsonify(error="Aucune clé Lara complète à sauvegarder. Renseigne les deux clés ou configure-les dans .env."), 400

    profiles = read_lara_profiles()
    profile = {"name": name, "access_key_id": access_key_id, "access_key_secret": access_key_secret}
    existing = next((i for i, p in enumerate(profiles) if p.get("name") == name), None)
    if existing is None:
        profiles.append(profile)
    else:
        profiles[existing] = profile

    try:
        save_lara_profiles(profiles)
    except OSError as exc:
        return jsonify(error=f"Impossible d'enregistrer les clés Lara : {exc}"), 500

    return jsonify(ok=True, message=f'Configuration "{name}" enregistrée.', profiles=lara_profiles_for_admin())


@app.post("/api/admin/lara-profiles/use")
def admin_use_lara_profile():
    auth = require_admin_api()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    profile = next((p for p in read_lara_profiles() if p.get("name") == name), None)
    if not profile:
        return jsonify(error="Configuration Lara introuvable."), 404

    try:
        update_env_values({
            "LARA_ACCESS_KEY_ID": profile.get("access_key_id", ""),
            "LARA_ACCESS_KEY_SECRET": profile.get("access_key_secret", ""),
        })
    except OSError as exc:
        return jsonify(error=f"Impossible d'activer cette configuration : {exc}"), 500

    update_runtime_lara_keys(profile.get("access_key_id", ""), profile.get("access_key_secret", ""))
    return jsonify(ok=True, message=f'Configuration "{name}" activée.', profiles=lara_profiles_for_admin())


@app.delete("/api/admin/lara-profiles/<path:name>")
def admin_delete_lara_profile(name):
    auth = require_admin_api()
    if auth:
        return auth
    profiles = read_lara_profiles()
    target = next((p for p in profiles if p.get("name") == name), None)
    if not target:
        return jsonify(error="Configuration Lara introuvable."), 404
    if target.get("name") == active_lara_profile_name():
        return jsonify(error="Impossible de supprimer la configuration Lara actuellement active. Active d'abord une autre configuration."), 400
    try:
        save_lara_profiles([p for p in profiles if p.get("name") != name])
    except OSError as exc:
        return jsonify(error=f"Impossible de supprimer la configuration : {exc}"), 500
    return jsonify(ok=True, profiles=lara_profiles_for_admin())


@app.get("/api/admin/storage")
def admin_storage():
    auth = require_admin_api()
    if auth:
        return auth
    return jsonify(items=list_stored_files())


@app.get("/api/admin/storage/<work_id>")
def admin_get_storage(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    work = find_storage_work(work_id)
    if work is None:
        return jsonify(error="Dossier introuvable.", id=work_id), 404

    now = time.time()
    created = work.stat().st_mtime
    metadata = {}
    metadata_path = work / "metadata.json"
    try:
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        metadata = {}

    files = []
    total_size = 0
    for path in work.rglob("*"):
        try:
            if path.is_file():
                size = path.stat().st_size
                total_size += size
                files.append({
                    "name": str(path.relative_to(work)),
                    "size": size,
                    "size_human": format_size(size),
                })
        except OSError:
            continue

    item = {
        "id": work.name,
        "created": created,
        "expires": created + RETENTION_SECONDS,
        "expires_in": max(0, int(created + RETENTION_SECONDS - now)),
        "size": total_size,
        "size_human": format_size(total_size),
        "files": files,
        "metadata": metadata,
    }
    return jsonify(item=item)


@app.post("/api/admin/storage/<work_id>/metadata")
def admin_update_storage_metadata(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    work = find_storage_work(work_id)
    if work is None:
        return jsonify(error="Dossier introuvable."), 404
    data = request.get_json(silent=True) or {}
    try:
        metadata = {}
        metadata_path = work / "metadata.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if "client_ip" in data:
            metadata["client_ip"] = str(data["client_ip"]).strip() or "inconnue"
        if "created_at" in data:
            value = str(data["created_at"]).strip()
            timestamp = time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M"))
            os.utime(work, (timestamp, timestamp))
            metadata["created_at"] = timestamp
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify(ok=True)
    except (OSError, ValueError, TypeError) as exc:
        return jsonify(error=f"Modification impossible : {exc}"), 400


@app.post("/api/admin/storage/<work_id>/rename")
def admin_rename_storage_file(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    work = find_storage_work(work_id)
    if work is None:
        return jsonify(error="Dossier introuvable."), 404
    data = request.get_json(silent=True) or request.form.to_dict()
    old_name = str(data.get("path", "")).strip()
    new_name = str(data.get("name", "")).strip()
    if not old_name or not new_name or "/" in new_name or "\\" in new_name:
        return jsonify(error="Nom de fichier invalide."), 400
    try:
        old_path = (work / old_name).resolve(strict=True)
        old_path.relative_to(work.resolve(strict=True))
        if not old_path.is_file() or old_path.suffix.lower() not in ALLOWED:
            return jsonify(error="Fichier image introuvable."), 404
        new_path = old_path.with_name(new_name)
        if new_path.suffix.lower() not in ALLOWED:
            return jsonify(error="L'extension doit rester une extension d'image valide."), 400
        if new_path.exists():
            return jsonify(error="Un fichier porte déjà ce nom."), 409
        old_path.rename(new_path)
        return jsonify(ok=True)
    except (OSError, ValueError) as exc:
        return jsonify(error=f"Renommage impossible : {exc}"), 400


@app.post("/admin/storage/<work_id>/rename-form")
def admin_rename_storage_form(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    data = request.form.to_dict()
    old_name = str(data.get("path", "")).strip()
    new_name = str(data.get("name", "")).strip()
    if not old_name or not new_name or "/" in new_name or "\\" in new_name:
        return redirect(url_for("admin_images"))
    work = find_storage_work(work_id)
    try:
        old_path = (work / old_name).resolve(strict=True)
        old_path.relative_to(work.resolve(strict=True))
        new_path = old_path.with_name(new_name)
        if old_path.is_file() and old_path.suffix.lower() in ALLOWED and new_path.suffix.lower() in ALLOWED and not new_path.exists():
            old_path.rename(new_path)
    except (OSError, ValueError):
        pass
    return redirect(url_for("admin_images"))


@app.post("/admin/storage/<work_id>/delete-form")
def admin_delete_storage_form(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    work = find_storage_work(work_id)
    if work is not None and work.is_dir():
        try:
            shutil.rmtree(work)
        except OSError:
            pass
    return redirect(url_for("admin_images"))


@app.delete("/api/admin/storage/<work_id>")
def admin_delete_storage(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    if "/" in work_id or "\\\\" in work_id or not work_id.startswith(TEMP_PREFIX):
        return jsonify(error="Dossier invalide."), 400
    work = find_storage_work(work_id)
    try:
        if work is None or not work.is_dir():
            return jsonify(error="Dossier introuvable."), 404
        shutil.rmtree(work)
    except OSError as exc:
        return jsonify(error=f"Suppression impossible : {exc}"), 500
    return jsonify(ok=True)


@app.get("/api/admin/storage/file")
def admin_download_storage_file():
    auth = require_admin_api()
    if auth:
        return auth

    work_id = request.args.get("work_id", "")
    relative_name = request.args.get("path", "")
    if not work_id or "/" in work_id or "\\" in work_id or not work_id.startswith(TEMP_PREFIX):
        return jsonify(error="Dossier invalide."), 400
    if not relative_name:
        return jsonify(error="Fichier invalide."), 400

    work = find_storage_work(work_id)
    try:
        if work is None:
            raise FileNotFoundError(work_id)
        work_resolved = work.resolve(strict=True)
        file_path = (work / relative_name).resolve(strict=True)
        file_path.relative_to(work_resolved)
    except (OSError, ValueError):
        return jsonify(error="Fichier introuvable."), 404

    if not file_path.is_file():
        return jsonify(error="Fichier introuvable."), 404

    preview = request.args.get("preview") == "1"
    if preview and file_path.suffix.lower() not in ALLOWED:
        return jsonify(error="Aperçu disponible uniquement pour les images."), 400

    return send_file(
        file_path,
        as_attachment=not preview,
        download_name=file_path.name,
        mimetype=None if not preview else None,
    )


@app.get("/api/lara-usage")
def lara_usage():
    try:
        return jsonify(get_usage(os.getenv("LARA_ACCESS_KEY_ID", "").strip()))
    except Exception as exc:
        app.logger.exception("Erreur lors du chargement de l'utilisation Lara")
        return jsonify(error=f"Impossible de charger l'utilisation Lara : {type(exc).__name__}: {exc}"), 500


@app.post("/translate")
def translate():
    files = request.files.getlist("files")
    target = request.form.get("target", "fr")
    if target not in LANGUAGES:
        return jsonify(error="Langue cible invalide."), 400
    items = []
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix in ALLOWED:
            items.append({"name": f.filename, "data": f.read()})
    if not items:
        return jsonify(error="Aucune image JPG, PNG, WebP ou TIFF valide."), 400
    job = uuid.uuid4().hex
    source = request.form.get("source", "auto")
    if source != "auto" and source not in LANGUAGES:
        return jsonify(error="Langue source invalide."), 400
    client_ip = request.remote_addr or "inconnue"
    metadata = {
        "job_id": job,
        "created_at": time.time(),
        "client_ip": client_ip,
        "source": source,
        "target": target,
        "image_count": len(items),
        "files": [str(item["name"]) for item in items],
    }
    # job_id est déjà le premier argument de set_job : ne pas le transmettre
    # une seconde fois via **metadata.
    set_job(job, state="running", message="Démarrage…", **{k: v for k, v in metadata.items() if k != "job_id"})
    threading.Thread(target=worker, args=(job, items, source, target, client_ip), daemon=True).start()
    return jsonify(job=job)


@app.get("/status/<job_id>")
def status(job_id):
    with LOCK:
        data = dict(JOBS.get(job_id, {"state": "error", "message": "Job introuvable."}))
    data.pop("work", None)
    data.pop("zip", None)
    return jsonify(data)


@app.get("/download/<job_id>")
def download(job_id):
    with LOCK:
        data = dict(JOBS.get(job_id, {}))
    path = data.get("zip")
    if not path or not Path(path).exists():
        return "Fichier indisponible", 404
    return send_file(path, as_attachment=True, download_name="images_traduites.zip")


@app.get("/download/<job_id>/a-traduire")
def download_untranslated(job_id):
    with LOCK:
        data = dict(JOBS.get(job_id, {}))
    path = data.get("untranslated_zip")
    if not path or not Path(path).exists():
        return "Fichier indisponible", 404
    return send_file(path, as_attachment=True, download_name="images_a_traduire.zip")


if __name__ == "__main__":
    cleanup_old_files()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    port = int(os.getenv("PORT", "8686"))
    print(f"Serveur local : http://127.0.0.1:{port}")
    print(f"Serveur réseau : http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)

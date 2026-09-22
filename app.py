import hmac
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
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
RETENTION_SECONDS = 2 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60
TEMP_PREFIX = "traducteur_"
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


def list_stored_files():
    now = time.time()
    temp_root = Path(tempfile.gettempdir())
    items = []
    for work in temp_root.glob(f"{TEMP_PREFIX}*"):
        try:
            if not work.is_dir():
                continue
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
                    files.append({"name": str(path.relative_to(work)), "size": size, "size_human": format_size(size)})
            items.append({"id": work.name, "created": created, "expires": expires, "expires_in": max(0, int(expires - now)), "size": total_size, "size_human": format_size(total_size), "files": files, "metadata": metadata})
        except OSError:
            continue
    items.sort(key=lambda item: item["created"], reverse=True)
    return items


def cleanup_old_files():
    """Supprime les dossiers de traduction temporaires vieux de plus de 2 jours."""
    cutoff = time.time() - RETENTION_SECONDS
    temp_root = Path(tempfile.gettempdir())
    for work in temp_root.glob(f"{TEMP_PREFIX}*"):
        try:
            if work.is_dir() and work.stat().st_mtime < cutoff:
                shutil.rmtree(work, ignore_errors=True)
        except OSError:
            pass


def cleanup_loop():
    while True:
        cleanup_old_files()
        time.sleep(CLEANUP_INTERVAL_SECONDS)


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
const usageImages=document.getElementById("usageImages"), usageCost=document.getElementById("usageCost"), usagePrice=document.getElementById("usagePrice");
function setStatus(t,c=""){status.textContent=t;status.className="status "+c}
async function refreshUsage(){try{const u=await fetch("/api/lara-usage").then(r=>r.json());usageImages.textContent=u.images+" image"+(u.images>1?"s":"");usageCost.textContent=u.estimated_cost_eur.toFixed(2).replace(".",",")+" €";usagePrice.textContent=u.price_eur_per_image.toFixed(2).replace(".",",")+" €/image"}catch(e){}}
files.addEventListener("change",()=>{const n=[...files.files].filter(f=>/\.(jpe?g|png|webp|tiff?)$/i.test(f.name)).length; if(n)setStatus(n+" image"+(n>1?"s":"")+" sélectionnée"+(n>1?"s":"")+" — prête à être traduite.")});
refreshUsage();
start.onclick=async()=>{
 const selected=[...files.files].filter(f=>/\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
 if(!selected.length){setStatus("Choisis un dossier contenant des images.","err");return}
 start.disabled=true;download.style.display="none";setStatus("Envoi des images…");
 const fd=new FormData();fd.append("source",source.value);fd.append("target",lang.value);selected.forEach(f=>fd.append("files",f,f.webkitRelativePath||f.name));
 try{const r=await fetch("/translate",{method:"POST",body:fd});const data=await r.json();if(!r.ok)throw new Error(data.error||"Erreur");
 while(true){await new Promise(x=>setTimeout(x,700));const s=await fetch("/status/"+data.job).then(x=>x.json());setStatus(s.message||"Traitement…");if(s.state==="done"){download.href="/download/"+data.job;download.style.display="block";download.textContent="⬇ Télécharger le ZIP";setStatus(s.message,"ok");await refreshUsage();break}if(s.state==="error"){setStatus(s.message||"Erreur","err");await refreshUsage();break}}
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
<title>Administration du stockage</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:1200px;margin:40px auto;padding:0 20px;background:#f5f7fb;color:#18202a}
.card{background:white;border-radius:18px;padding:28px;box-shadow:0 8px 30px #00000012}
.muted{color:#667085}.section{margin-top:24px;padding:18px;border:1px solid #eaecf0;border-radius:14px}
.section h2,.section h3{margin-top:0}.file-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.file-card{border:1px solid #eaecf0;border-radius:12px;padding:10px;background:#fafafa}
.file-card img{display:block;width:100%;height:170px;object-fit:contain;background:white;border-radius:8px;margin-bottom:8px;cursor:zoom-in}.modal{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center;padding:20px;z-index:1000}.modal.open{display:flex}.modal img{max-width:95vw;max-height:90vh;object-fit:contain;background:white;border-radius:10px}.modal-close{position:absolute;top:16px;right:20px;background:white;border:0;border-radius:50%;width:42px;height:42px;font-size:24px;cursor:pointer}
.file-name{font-size:.9rem;word-break:break-word}.file-meta{font-size:.8rem;color:#667085;margin-top:4px}
.file-actions{margin-top:8px}.file-actions a{color:#175cd3;margin-right:12px}
.zip-file{margin-top:16px;padding:12px;border:1px dashed #d0d5dd;border-radius:10px}
.danger{padding:9px 14px;border:0;border-radius:8px;background:#b42318;color:white;cursor:pointer}
.empty{padding:30px;text-align:center;color:#667085}
.error{padding:14px;background:#fef3f2;color:#b42318;border-radius:10px;margin-top:15px}.job-info{margin-top:10px;padding:10px 12px;background:#f8f9fc;border-radius:9px;font-size:.9rem;color:#475467}
</style></head><body><div class="card">
<h1>Administration du stockage</h1>
<p class="muted">Fichiers temporaires conservés pendant 48 heures maximum.</p>
<p><a href="/admin/logout">Se déconnecter</a> · <a href="/">← Retour au traducteur</a></p>
<div id="error"></div><div id="imageModal" class="modal" onclick="closeImage(event)"><button class="modal-close" type="button" onclick="closeImage(event)">×</button><img id="modalImage" src="" alt="Aperçu agrandi"></div><div class="filters"><button type="button" class="filter active" data-filter="all">Toutes</button><button type="button" class="filter" data-filter="translated">Images traduites</button><button type="button" class="filter" data-filter="uploaded">Images envoyées</button></div><div id="jobs"></div>
</div>
<script>
let currentFilter="all"; function fmtDate(ts){return new Date(ts*1000).toLocaleString("fr-FR")}
function fileUrl(item,f){return '/api/admin/storage/file?work_id='+encodeURIComponent(item.id)+'&path='+encodeURIComponent(f.name)}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function card(item,f){
  const url=fileUrl(item,f);
  return '<div class="file-card"><img src="'+url+'&preview=1" alt="" onclick="openImage(\''+url+'&preview=1\')"><div class="file-name">'+esc(f.name)+'</div><div class="file-meta">'+esc(f.size_human)+'</div><div class="file-actions"><a href="'+url+'">Télécharger</a></div></div>';
}
function openImage(url){document.getElementById("modalImage").src=url;document.getElementById("imageModal").classList.add("open")} function closeImage(e){if(e.target.id==="imageModal"||e.target.classList.contains("modal-close")){document.getElementById("imageModal").classList.remove("open");document.getElementById("modalImage").src=""}} async function load(){
  try{
    const r=await fetch('/api/admin/storage',{credentials:'same-origin'});
    if(!r.ok) throw new Error('Session administrateur expirée. Recharge la page et reconnecte-toi.');
    const data=await r.json(); const body=document.getElementById('jobs'); body.innerHTML='';
    if(!data.items.length){body.innerHTML='<div class="empty">Aucun fichier temporaire actuellement stocké.</div>';return}
    for(const item of data.items){
      const inputs=item.files.filter(f=>/^input_\\d+\\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
      const outputs=item.files.filter(f=>/^traduit\\//i.test(f.name)&&/\\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
      const zips=item.files.filter(f=>/\\.zip$/i.test(f.name));
      const showInputs=currentFilter==='all'||currentFilter==='uploaded';
      const showOutputs=currentFilter==='all'||currentFilter==='translated';
      const section=document.createElement('div'); section.className='section';
      const meta=item.metadata||{}; const info='<div class="job-info"><strong>Informations</strong> · IP client : '+esc(meta.client_ip||'inconnue')+' · Date/heure : '+fmtDate(meta.created_at||item.created)+' · Source : '+esc(meta.source||'auto')+' · Cible : '+esc(meta.target||'')+' · Images : '+esc(meta.image_count??'')+'</div>'; section.innerHTML='<h2>'+esc(item.id)+'</h2><div class="muted">Créé le '+fmtDate(item.created)+' · Expire le '+fmtDate(item.expires)+' · '+esc(item.size_human)+'</div>'+info+
      (showInputs?'<div class="section"><h3>Images envoyées</h3><div class="file-grid">'+(inputs.length?inputs.map(f=>card(item,f)).join(''):'<div class="muted">Aucune image envoyée.</div>')+'</div></div>':'')+
      (showOutputs?'<div class="section"><h3>Images traduites</h3><div class="file-grid">'+(outputs.length?outputs.map(f=>card(item,f)).join(''):'<div class="muted">Aucune image traduite.</div>')+'</div></div>':'')+
      '<div class="zip-file"><strong>ZIP :</strong> '+(zips.length?zips.map(f=>'<a href="'+fileUrl(item,f)+'">Télécharger le ZIP</a>').join(' · '):'aucun')+'</div>'+
      '<p><button class="danger" onclick="removeItem(\\''+esc(item.id)+'\\')">Supprimer maintenant</button></p>';
      body.appendChild(section);
    }
  }catch(e){document.getElementById('error').innerHTML='<div class="error">'+esc(e.message)+'</div>'}
}
async function removeItem(id){if(!confirm('Supprimer définitivement ce dossier et toutes ses images ?'))return;const r=await fetch('/api/admin/storage/'+encodeURIComponent(id),{method:'DELETE',credentials:'same-origin'});if(!r.ok){const d=await r.json();alert(d.error||'Erreur');return}load()}
document.querySelectorAll(".filter").forEach(function(b){b.addEventListener("click",function(){currentFilter=b.dataset.filter;document.querySelectorAll(".filter").forEach(function(x){x.classList.toggle("active",x===b)});load();});});load();setInterval(load,10000);
</script></body></html>"""

def set_job(job_id, **values):
    with LOCK:
        JOBS.setdefault(job_id, {}).update(values)


def worker(job_id, files, source, target):
    work = Path(tempfile.mkdtemp(prefix="traducteur_"))
    out = work / "traduit"
    out.mkdir()
    try:
        total = len(files)
        metadata_path = work / "metadata.json"
        try:
            metadata_path.write_text(json.dumps({
                "job_id": job_id,
                "created_at": time.time(),
                "source": source,
                "target": target,
                "image_count": total,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        results = []
        billed_images = 0
        for i, item in enumerate(files, 1):
            src = work / f"input_{i}{Path(item['name']).suffix.lower()}"
            src.write_bytes(item["data"])
            name = secure_filename(Path(item["name"]).name) or f"image_{i}.png"
            dest = out / name
            set_job(job_id, message=f"Image {i}/{total} : Lara Translate…")
            try:
                used_lara = translate_image_with_lara(src, dest, target, source)
            except Exception as exc:
                error_text = str(exc)
                no_text = (
                    "No text found in the image" in error_text
                    or "UnprocessableEntityError" in error_text
                    or "(HTTP 422)" in error_text
                )
                if not no_text:
                    raise
                shutil.copy2(src, dest)
                used_lara = False
                set_job(job_id, message=f"Image {i}/{total} : aucun texte détecté, image conservée")
            if used_lara:
                record_image()
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
                z.write(path, path.name)
        suffix = f" ({billed_images} appel(s) Lara)" if billed_images != total else ""
        skipped = total - billed_images
        if skipped:
            message = f"{total} image(s) traitée(s), dont {skipped} sans texte détecté."
            if billed_images:
                message += f" {billed_images} appel(s) Lara."
        else:
            message = f"{total} image(s) traduite(s)."
        set_job(job_id, state="done", message=message, zip=str(zip_path))
    except Exception as exc:
        set_job(job_id, state="error", message=f"❌ {type(exc).__name__}: {exc}")
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
    return redirect(url_for("admin_login"))


@app.get("/admin")
def admin():
    auth = require_admin_page()
    if auth:
        return auth
    return ADMIN_PAGE


@app.get("/api/admin/storage")
def admin_storage():
    auth = require_admin_api()
    if auth:
        return auth
    return jsonify(items=list_stored_files())


@app.delete("/api/admin/storage/<work_id>")
def admin_delete_storage(work_id):
    auth = require_admin_api()
    if auth:
        return auth
    if "/" in work_id or "\\\\" in work_id or not work_id.startswith(TEMP_PREFIX):
        return jsonify(error="Dossier invalide."), 400
    work = Path(tempfile.gettempdir()) / work_id
    try:
        if not work.is_dir():
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

    work = Path(tempfile.gettempdir()) / work_id
    try:
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
    return jsonify(get_usage())


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
    set_job(job, state="running", message="Démarrage…", **metadata)
    threading.Thread(target=worker, args=(job, items, source, target), daemon=True).start()
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


if __name__ == "__main__":
    cleanup_old_files()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print("Serveur local : http://127.0.0.1:8686")
    print("Serveur réseau : http://0.0.0.0:8686")
    app.run(host="0.0.0.0", port=8686, threaded=True)

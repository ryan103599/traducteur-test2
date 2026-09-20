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
            files = []
            total_size = 0
            for path in work.rglob("*"):
                if path.is_file():
                    size = path.stat().st_size
                    total_size += size
                    files.append({"name": str(path.relative_to(work)), "size": size, "size_human": format_size(size)})
            items.append({"id": work.name, "created": created, "expires": expires, "expires_in": max(0, int(expires - now)), "size": total_size, "size_human": format_size(total_size), "files": files})
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
<title>Traducteur d'images</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;background:#f5f7fb;color:#18202a}
.card{background:white;border-radius:18px;padding:28px;box-shadow:0 8px 30px #00000012}
h1{margin-top:0}.muted{color:#667085}
label{display:block;font-weight:600;margin:18px 0 8px}
select,input[type=file],button{width:100%;box-sizing:border-box;padding:12px;border:1px solid #d0d5dd;border-radius:10px;background:white}
button{margin-top:20px;background:#111827;color:white;border:0;cursor:pointer;font-weight:700}
button:disabled{opacity:.5;cursor:not-allowed}
#status{margin-top:20px;padding:14px;border-radius:10px;background:#f2f4f7;white-space:pre-wrap}
#download{display:none;margin-top:18px}.ok{background:#ecfdf3!important;color:#067647}
.err{background:#fef3f2!important;color:#b42318}
small{display:block;margin-top:8px;color:#667085}
.usage-row{display:flex;gap:20px;flex-wrap:wrap;margin-top:10px}
.usage-item{min-width:190px}.usage-value{font-size:1.35rem;font-weight:700}
</style>
</head>
<body>
<div class="card">
<h1>Traducteur d'images</h1>
<p class="muted">Traduction directe des images avec Lara Translate.</p>
<label>Langue source</label>
<select id="source">
<option value="auto">Détection automatique</option>
{% for code,name in source_languages.items() %}<option value="{{code}}">{{name}}</option>{% endfor %}
</select>
<label>Langue cible</label>
<select id="lang">
{% for code,name in languages.items() %}<option value="{{code}}">{{name}}</option>{% endfor %}
</select>
<label>Images</label>
<input id="files" type="file" webkitdirectory directory multiple accept=".jpg,.jpeg,.png,.webp,.tif,.tiff">
<small>Choisis un dossier. Les images sont envoyées directement à Lara, qui renvoie l’image déjà traduite.</small>
<button id="start">Traduire le dossier</button>

<div id="quota" style="margin-top:18px;padding:14px;border:1px solid #d0d5dd;border-radius:10px;background:#fafafa">
<strong>Utilisation Lara — ce mois</strong>
<div class="usage-row">
  <div class="usage-item">Images traduites<div id="usageImages" class="usage-value">—</div></div>
  <div class="usage-item">Coût estimé<div id="usageCost" class="usage-value">—</div></div>
  <div class="usage-item">Tarif<div id="usagePrice" class="usage-value">—</div></div>
</div>
<small>Compteur local basé sur les traductions d’images réussies via Lara. L’estimation utilise le tarif Inpainting de l’application ; elle ne remplace pas le solde officiel Lara.</small>
<a href="https://laratranslate.com/account/api" target="_blank" rel="noopener">Voir l’utilisation officielle Lara →</a>
</div>

<div id="status">En attente.</div>
<a id="download" href="#" download>Télécharger le ZIP</a>
<p style="margin-top:24px"><a href="/admin">Administration du stockage →</a></p>
</div>
<script>
const start=document.getElementById("start"), files=document.getElementById("files");
const source=document.getElementById("source"), lang=document.getElementById("lang"), status=document.getElementById("status"), download=document.getElementById("download");
const usageImages=document.getElementById("usageImages"), usageCost=document.getElementById("usageCost"), usagePrice=document.getElementById("usagePrice");

function setStatus(t,c=""){status.textContent=t;status.className=c}

async function refreshUsage(){
  try{
    const u=await fetch("/api/lara-usage").then(r=>r.json());
    usageImages.textContent=u.images+" image"+(u.images>1?"s":"");
    usageCost.textContent=u.estimated_cost_eur.toFixed(2).replace(".",",")+" €";
    usagePrice.textContent=u.price_eur_per_image.toFixed(2).replace(".",",")+" €/image";
  }catch(e){}
}
refreshUsage();

start.onclick=async()=>{
 const selected=[...files.files].filter(f=>/\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
 if(!selected.length){setStatus("Choisis un dossier contenant des images.","err");return}
 start.disabled=true; download.style.display="none"; setStatus("Envoi des images…");
 const fd=new FormData(); fd.append("source",source.value); fd.append("target",lang.value);
 selected.forEach(f=>fd.append("files",f,f.webkitRelativePath||f.name));
 try{
   const r=await fetch("/translate",{method:"POST",body:fd});
   const data=await r.json(); if(!r.ok) throw new Error(data.error||"Erreur");
   while(true){
     await new Promise(x=>setTimeout(x,700));
     const s=await fetch("/status/"+data.job).then(x=>x.json());
     setStatus(s.message||"Traitement…");
     if(s.state==="done"){
       download.href="/download/"+data.job; download.style.display="block"; download.textContent="Télécharger le ZIP";
       setStatus(s.message,"ok"); await refreshUsage(); break;
     }
     if(s.state==="error"){setStatus(s.message||"Erreur","err"); await refreshUsage(); break}
   }
 }catch(e){setStatus(e.message||"Erreur","err")}
 finally{start.disabled=false}
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
.file-card img{display:block;width:100%;height:170px;object-fit:contain;background:white;border-radius:8px;margin-bottom:8px}
.file-name{font-size:.9rem;word-break:break-word}.file-meta{font-size:.8rem;color:#667085;margin-top:4px}
.file-actions{margin-top:8px}.file-actions a{color:#175cd3;margin-right:12px}
.zip-file{margin-top:16px;padding:12px;border:1px dashed #d0d5dd;border-radius:10px}
.danger{padding:9px 14px;border:0;border-radius:8px;background:#b42318;color:white;cursor:pointer}
.empty{padding:30px;text-align:center;color:#667085}
.error{padding:14px;background:#fef3f2;color:#b42318;border-radius:10px;margin-top:15px}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.filter{padding:10px 16px;border:1px solid #d0d5dd;border-radius:10px;background:white;color:#344054;cursor:pointer;font-weight:600}.filter.active{background:#111827;color:white;border-color:#111827}
</style></head><body><div class="card">
<h1>Administration du stockage</h1>
<p class="muted">Fichiers temporaires conservés pendant 48 heures maximum.</p>
<p><a href="/admin/logout">Se déconnecter</a> · <a href="/">← Retour au traducteur</a></p>
<div id="error"></div><div class="filters"><button class="filter active" data-filter="all" onclick="setFilter('all')">Toutes</button><button class="filter" data-filter="translated" onclick="setFilter('translated')">Images traduites</button><button class="filter" data-filter="uploaded" onclick="setFilter('uploaded')">Images envoyées</button></div><div id="jobs"></div>
</div>
<script>
let currentFilter="all"; function setFilter(v){currentFilter=v; document.querySelectorAll(".filter").forEach(x=>x.classList.toggle("active",x.dataset.filter===v)); renderJobs()} function fmtDate(ts){return new Date(ts*1000).toLocaleString("fr-FR")}
function fileUrl(item,f){return '/api/admin/storage/file?work_id='+encodeURIComponent(item.id)+'&path='+encodeURIComponent(f.name)}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function card(item,f){
  const url=fileUrl(item,f);
  return '<div class="file-card"><img src="'+url+'&preview=1" alt=""><div class="file-name">'+esc(f.name)+'</div><div class="file-meta">'+esc(f.size_human)+'</div><div class="file-actions"><a href="'+url+'">Télécharger</a></div></div>';
}
async function load(){
  try{
    const r=await fetch('/api/admin/storage',{credentials:'same-origin'});
    if(!r.ok) throw new Error('Session administrateur expirée. Recharge la page et reconnecte-toi.');
    const data=await r.json(); window.storageItems=data.items; renderJobs(); return;
    if(!data.items.length){body.innerHTML='<div class="empty">Aucun fichier temporaire actuellement stocké.</div>';return}
    for(const item of data.items){
      const inputs=item.files.filter(f=>/^input_\\d+\\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
      const outputs=item.files.filter(f=>/^traduit\\//i.test(f.name)&&/\\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
      const zips=item.files.filter(f=>/\\.zip$/i.test(f.name));
      const section=document.createElement('div'); section.className='section';
      section.innerHTML='<h2>'+esc(item.id)+'</h2><div class="muted">Créé le '+fmtDate(item.created)+' · Expire le '+fmtDate(item.expires)+' · '+esc(item.size_human)+'</div>'+
      '<div class="section"><h3>Images envoyées</h3><div class="file-grid">'+(inputs.length?inputs.map(f=>card(item,f)).join(''):'<div class="muted">Aucune image envoyée.</div>')+'</div></div>'+
      '<div class="section"><h3>Images traduites</h3><div class="file-grid">'+(outputs.length?outputs.map(f=>card(item,f)).join(''):'<div class="muted">Aucune image traduite.</div>')+'</div></div>'+
      '<div class="zip-file"><strong>ZIP :</strong> '+(zips.length?zips.map(f=>'<a href="'+fileUrl(item,f)+'">Télécharger le ZIP</a>').join(' · '):'aucun')+'</div>'+
      '<p><button class="danger" onclick="removeItem(\\''+esc(item.id)+'\\')">Supprimer maintenant</button></p>';
      body.appendChild(section);
    }
  }catch(e){document.getElementById('error').innerHTML='<div class="error">'+esc(e.message)+'</div>'}
}
function renderJobs(){
 const body=document.getElementById('jobs'); body.innerHTML='';
 const items=window.storageItems||[];
 if(!items.length){body.innerHTML='<div class="empty">Aucun fichier temporaire actuellement stocké.</div>';return}
 for(const item of items){
  const inputs=item.files.filter(f=>/^input_\d+\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
  const outputs=item.files.filter(f=>/^traduit\//i.test(f.name)&&/\.(jpe?g|png|webp|tiff?)$/i.test(f.name));
  const zips=item.files.filter(f=>/\.zip$/i.test(f.name));
  const showIn=currentFilter==="all"||currentFilter==="uploaded";
  const showOut=currentFilter==="all"||currentFilter==="translated";
  const section=document.createElement('div'); section.className='section';
  let html='<h2>'+esc(item.id)+'</h2><div class="muted">Créé le '+fmtDate(item.created)+' · Expire le '+fmtDate(item.expires)+' · '+esc(item.size_human)+'</div>';
  if(showIn) html+='<div class="section"><h3>Images envoyées</h3><div class="file-grid">'+(inputs.length?inputs.map(f=>card(item,f)).join(''):'<div class="muted">Aucune image envoyée.</div>')+'</div></div>';
  if(showOut) html+='<div class="section"><h3>Images traduites</h3><div class="file-grid">'+(outputs.length?outputs.map(f=>card(item,f)).join(''):'<div class="muted">Aucune image traduite.</div>')+'</div></div>';
  html+='<div class="zip-file"><strong>ZIP :</strong> '+(zips.length?zips.map(f=>'<a href="'+fileUrl(item,f)+'">Télécharger le ZIP</a>').join(' · '):'aucun')+'</div><p><button class="danger" onclick="removeItem(\''+esc(item.id)+'\')">Supprimer maintenant</button></p>';
  section.innerHTML=html; body.appendChild(section);
 }
}
async function removeItem(id){if(!confirm('Supprimer définitivement ce dossier et toutes ses images ?'))return;const r=await fetch('/api/admin/storage/'+encodeURIComponent(id),{method:'DELETE',credentials:'same-origin'});if(!r.ok){const d=await r.json();alert(d.error||'Erreur');return}load()}
load();setInterval(load,60000);
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
        results = []
        billed_images = 0
        for i, item in enumerate(files, 1):
            src = work / f"input_{i}{Path(item['name']).suffix.lower()}"
            src.write_bytes(item["data"])
            name = secure_filename(Path(item["name"]).name) or f"image_{i}.png"
            dest = out / name
            set_job(job_id, message=f"Image {i}/{total} : Lara Translate…")
            used_lara = translate_image_with_lara(src, dest, target, source)
            if used_lara:
                record_image()
                billed_images += 1
            results.append(dest)
            set_job(job_id, message=f"Image {i}/{total} terminée")
        zip_path = work / "images_traduites.zip"
        set_job(job_id, message="Création du ZIP…")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for path in results:
                z.write(path, path.name)
        suffix = f" ({billed_images} appel(s) Lara)" if billed_images != total else ""
        set_job(job_id, state="done", message=f"{total} image(s) traduite(s).{suffix}", zip=str(zip_path))
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
    set_job(job, state="running", message="Démarrage…")
    source = request.form.get("source", "auto")
    if source != "auto" and source not in LANGUAGES:
        return jsonify(error="Langue source invalide."), 400
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

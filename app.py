import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

from papago_images import translate_image_with_papago


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

JOBS = {}
LOCK = threading.Lock()
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

LANGUAGES = {
    "fr": "Français",
    "en": "Anglais",
    "es": "Espagnol",
    "de": "Allemand",
    "it": "Italien",
    "pt": "Portugais",
    "ja": "Japonais",
    "ko": "Coréen",
    "zh-CN": "Chinois simplifié",
    "zh-TW": "Chinois traditionnel",
    "ru": "Russe",
    "ar": "Arabe",
}

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
</style>
</head>
<body>
<div class="card">
<h1>Traducteur d'images</h1>
<p class="muted">Traduction directe des images avec NAVER Papago Image Translation.</p>
<label>Langue source</label>
<select id="source">
<option value="auto">Détection automatique</option>
{% for code,name in languages.items() %}<option value="{{code}}">{{name}}</option>{% endfor %}
</select>
<label>Langue cible</label>
<select id="lang">
{% for code,name in languages.items() %}<option value="{{code}}">{{name}}</option>{% endfor %}
</select>
<label>Images</label>
<input id="files" type="file" webkitdirectory directory multiple accept=".jpg,.jpeg,.png,.webp">
<small>Choisis un dossier. JPG, PNG, WebP et TIFF sont envoyés directement à NAVER Papago, qui renvoie l’image déjà traduite.</small>
<button id="start">Traduire le dossier</button>
<div id="status">En attente.</div>
<a id="download" href="#" download>Télécharger le ZIP</a>
</div>
<script>
const start=document.getElementById("start"), files=document.getElementById("files");
const source=document.getElementById("source"), lang=document.getElementById("lang"), status=document.getElementById("status"), download=document.getElementById("download");
function setStatus(t,c=""){status.textContent=t;status.className=c}
start.onclick=async()=>{
 const selected=[...files.files].filter(f=>/\.(jpe?g|png|webp)$/i.test(f.name));
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
       setStatus(s.message,"ok"); break;
     }
     if(s.state==="error"){setStatus(s.message||"Erreur","err");break}
   }
 }catch(e){setStatus(e.message||"Erreur","err")}
 finally{start.disabled=false}
};
</script>
</body>
</html>"""


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
        for i, item in enumerate(files, 1):
            src = work / f"input_{i}{Path(item['name']).suffix.lower()}"
            src.write_bytes(item["data"])
            name = secure_filename(Path(item["name"]).name) or f"image_{i}.png"
            dest = out / name
            set_job(job_id, message=f"Image {i}/{total} : NAVER Papago…")
            translate_image_with_papago(src, dest, target, source)
            results.append(dest)
            set_job(job_id, message=f"Image {i}/{total} terminée")
        zip_path = work / "images_traduites.zip"
        set_job(job_id, message="Création du ZIP…")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for path in results:
                z.write(path, path.name)
        set_job(job_id, state="done", message=f"{total} image(s) traduite(s).", zip=str(zip_path))
    except Exception as exc:
        set_job(job_id, state="error", message=f"❌ {type(exc).__name__}: {exc}")
    finally:
        set_job(job_id, work=str(work))


@app.get("/")
def index():
    return render_template_string(PAGE, languages=LANGUAGES)


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
    print("Serveur local : http://127.0.0.1:8686")
    print("Serveur réseau : http://0.0.0.0:8686")
    app.run(host="0.0.0.0", port=8686, threaded=True)

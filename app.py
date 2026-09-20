from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
import traduit
import os, shutil, tempfile, threading, uuid, zipfile

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
JOBS = {}
LOCK = threading.Lock()
ALLOWED = traduit.IMAGE_EXTENSIONS

HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Traducteur d'images — Google Traduction</title><style>
*{box-sizing:border-box}body{margin:0;background:#0f172a;color:#f8fafc;font-family:Segoe UI,Arial,sans-serif}.page{min-height:100vh;padding:28px;max-width:1200px;margin:auto}h1{margin:0;font-size:32px}.sub{color:#94a3b8;margin:6px 0 24px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.btn,select{border:1px solid #334155;border-radius:8px;background:#1f2937;color:#fff;padding:11px 16px;font-size:14px;font-weight:700}.btn{cursor:pointer}.primary{background:#6366f1;border-color:#6366f1}.btn:disabled{opacity:.45;cursor:not-allowed}input[type=file]{display:none}.file{color:#94a3b8;font-size:13px;margin-left:auto}.card{margin-top:18px;background:#111827;border:1px solid #334155;border-radius:10px;padding:16px}.drop{border:2px dashed #475569;border-radius:10px;padding:42px;text-align:center;color:#94a3b8}.progress-wrap{height:10px;background:#1f2937;border-radius:8px;overflow:hidden;margin-top:18px}.progress{height:100%;width:0;background:#6366f1;transition:width .2s}.status{color:#cbd5e1;margin-top:10px;font-size:14px}.hint{color:#64748b;font-size:12px;margin-top:8px}.preview{margin-top:18px;display:grid;grid-template-columns:1fr 1fr;gap:14px}.preview div{background:#1f2937;border-radius:8px;min-height:260px;display:flex;align-items:center;justify-content:center;color:#64748b;overflow:hidden}.preview img{max-width:100%;max-height:500px}@media(max-width:800px){.preview{grid-template-columns:1fr}.file{margin-left:0;width:100%}}</style></head><body><main class="page">
<h1>Traducteur d'images</h1><div class="sub">Google Traduction • traitement automatique d'un dossier d'images</div>
<div class="toolbar"><label class="btn"><input id="folder" type="file" webkitdirectory directory multiple>📁 Choisir un dossier</label><select id="lang"><option value="fr">Français</option><option value="en">Anglais</option><option value="es">Espagnol</option><option value="de">Allemand</option><option value="it">Italien</option><option value="pt">Portugais</option><option value="ja">Japonais</option><option value="ko">Coréen</option><option value="zh-CN">Chinois simplifié</option><option value="zh-TW">Chinois traditionnel</option><option value="ru">Russe</option><option value="ar">Arabe</option></select><button id="go" class="btn primary" disabled>▶ Traduire le dossier</button><button id="dl" class="btn" disabled>⬇ Télécharger le ZIP</button><div id="file" class="file">Aucun dossier sélectionné</div></div>
<div class="card"><div id="drop" class="drop">Sélectionne un dossier contenant tes images.<br><span class="hint">PNG, JPG, JPEG, WEBP et BMP</span></div><div class="progress-wrap"><div id="bar" class="progress"></div></div><div id="status" class="status">Prêt.</div></div>
<div class="preview"><div id="before">Aperçu original</div><div id="after">Aperçu traduit</div></div></main>
<script>
let files=[],job=null;const folder=document.getElementById('folder'),go=document.getElementById('go'),dl=document.getElementById('dl'),fileLabel=document.getElementById('file'),status=document.getElementById('status'),bar=document.getElementById('bar'),before=document.getElementById('before'),after=document.getElementById('after');
folder.onchange=()=>{files=[...folder.files].filter(f=>/\.(png|jpe?g|webp|bmp)$/i.test(f.name));go.disabled=!files.length;fileLabel.textContent=files.length?files.length+' image(s) sélectionnée(s)':'Aucune image compatible';if(files[0])before.innerHTML='<img src="'+URL.createObjectURL(files[0])+'">';bar.style.width='0%';status.textContent=files.length?'Dossier prêt. Choisis la langue puis lance la traduction.':'Aucune image compatible.'};
go.onclick=async()=>{go.disabled=true;dl.disabled=true;bar.style.width='0%';status.textContent='Envoi du dossier…';const fd=new FormData();files.forEach(f=>fd.append('files',f,f.webkitRelativePath||f.name));fd.append('target',document.getElementById('lang').value);try{const r=await fetch('/translate',{method:'POST',body:fd}),d=await r.json();if(!r.ok)throw Error(d.error||'Erreur');job=d.job_id;poll()}catch(e){status.textContent='Erreur : '+e.message;go.disabled=false}};
async function poll(){const r=await fetch('/status/'+job),j=await r.json();bar.style.width=(j.percent||0)+'%';status.textContent=j.status||'Traduction…';if(j.preview)after.innerHTML='<img src="'+j.preview+'">';if(j.done){go.disabled=false;if(j.error){status.textContent='Erreur : '+j.error;return}dl.disabled=false;status.textContent='Terminé : '+j.total+' image(s). Clique sur « Télécharger le ZIP ». ';return}setTimeout(poll,500)}dl.onclick=()=>{if(job)location.href='/download/'+job};
</script></body></html>'''

def new_job(target,total):
    jid=uuid.uuid4().hex
    with LOCK:JOBS[jid]={"target":target,"percent":0,"current":0,"total":total,"done":False,"status":"Préparation…","error":None,"download":None,"preview":None}
    return jid

def run_folder(jid,files,target):
    td=tempfile.mkdtemp(prefix="traducteur_web_");outdir=os.path.join(td,"traduction")
    try:
        os.makedirs(outdir,exist_ok=True);total=len(files)
        for i,(filename,data) in enumerate(files,1):
            safe=secure_filename(os.path.basename(filename)) or f"image_{i}.png";src=os.path.join(td,f"{i}_{safe}")
            with open(src,"wb") as f:f.write(data)
            dest=os.path.join(outdir,os.path.splitext(safe)[0]+"_"+target+".png");traduit.translate_image(src,dest,target);p=int(i*100/total)
            with LOCK:
                JOBS[jid].update(percent=p,current=i,status=f"Traduction {i}/{total} • {p}%")
                if i==1:JOBS[jid]["preview"]="/result/"+jid
        zp=os.path.join(td,"traduction_"+target+".zip")
        with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED) as z:
            for root,_,names in os.walk(outdir):
                for name in names:z.write(os.path.join(root,name),arcname=os.path.relpath(os.path.join(root,name),outdir))
        with LOCK:JOBS[jid].update(done=True,percent=100,status="Traduction terminée.",download=zp,temp=td)
    except Exception as e:
        shutil.rmtree(td,ignore_errors=True)
        with LOCK:JOBS[jid].update(done=True,error=str(e),status="Erreur.")

@app.get("/")
def index():return render_template_string(HTML)
@app.post("/translate")
def translate_route():
    files=[(f.filename,f.read()) for f in request.files.getlist("files") if f.filename.lower().endswith(ALLOWED)];target=request.form.get("target","fr").strip()
    if not files:return jsonify(error="Aucune image compatible trouvée."),400
    if target not in traduit.SUPPORTED_LANGUAGES:return jsonify(error="Langue cible non supportée."),400
    jid=new_job(target,len(files));threading.Thread(target=run_folder,args=(jid,files,target),daemon=True).start();return jsonify(job_id=jid)
@app.get("/status/<jid>")
def status(jid):
    with LOCK:j=dict(JOBS.get(jid,{}))
    if not j:return jsonify(error="Tâche introuvable."),404
    return jsonify({k:v for k,v in j.items() if k not in ("download","temp")})
@app.get("/result/<jid>")
def result(jid):
    with LOCK:j=JOBS.get(jid)
    if not j or not j.get("preview") or not j.get("temp"):return "Aperçu introuvable",404
    outdir=os.path.join(j["temp"],"traduction");files=[os.path.join(outdir,x) for x in os.listdir(outdir)] if os.path.isdir(outdir) else []
    if not files:return "Aperçu introuvable",404
    return send_file(files[0],mimetype="image/png")
@app.get("/download/<jid>")
def download(jid):
    with LOCK:j=JOBS.get(jid)
    if not j or not j.get("download"):return "Résultat introuvable",404
    return send_file(j["download"],as_attachment=True,download_name=f"traduction_{j['target']}.zip")

if __name__=="__main__":
    # 0.0.0.0 = toutes les interfaces réseau, donc accessible depuis les autres PC.
    app.run(host="0.0.0.0",port=8686,debug=False)

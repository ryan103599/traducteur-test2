from paddleocr import PaddleOCR
from deep_translator import GoogleTranslator, MyMemoryTranslator
from PIL import Image, ImageDraw, ImageFont
import cv2, numpy as np, os, tempfile

IMAGE_EXTENSIONS=(".png",".jpg",".jpeg",".webp",".bmp")
SUPPORTED_LANGUAGES={"fr","en","es","de","it","pt","ja","ko","zh-CN","zh-TW","ru","ar"}

# OCR léger : le modèle server par défaut peut consommer plusieurs Go de RAM.
# Le modèle mobile est beaucoup plus adapté à un PC classique.
OCR_MAX_SIDE=960
OCR_CPU_THREADS=2

ocr=PaddleOCR(
    text_detection_model_name="PP-OCRv5_mobile_det",
    text_recognition_model_name="PP-OCRv5_mobile_rec",
    lang="en",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
    enable_mkldnn=False,
    cpu_threads=OCR_CPU_THREADS,
    text_det_limit_side_len=OCR_MAX_SIDE,
    text_det_limit_type="max",
)

FONT_PATHS=[r"C:\Windows\Fonts\arial.ttf",r"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",r"/System/Library/Fonts/Supplemental/Arial.ttf"]
font_path=next((p for p in FONT_PATHS if os.path.exists(p)),None)

def get_font(size): return ImageFont.truetype(font_path,size) if font_path else ImageFont.load_default()

def wrap_text(text,max_width,font,draw):
    words=text.split();lines=[];cur=""
    for word in words:
        candidate=word if not cur else cur+" "+word
        box=draw.textbbox((0,0),candidate,font=font)
        if box[2]-box[0]<=max_width:cur=candidate
        else:
            if cur:lines.append(cur)
            cur=word
    if cur:lines.append(cur)
    return "\n".join(lines)

def should_merge(a,b):
    h=(a["height"]+b["height"])/2
    vgap=max(0,max(a["y1"],b["y1"])-min(a["y2"],b["y2"]))
    overlap=min(a["x2"],b["x2"])-max(a["x1"],b["x1"])
    hgap=max(0,max(a["x1"],b["x1"])-min(a["x2"],b["x2"]))
    return (vgap<h*1.8 and overlap>-h*2) or (hgap<h*2 and abs(a["cy"]-b["cy"])<h)

def group_text(items):
    groups=[]
    for item in sorted(items,key=lambda x:(x["y1"],x["x1"])):
        for g in groups:
            if any(should_merge(item,e) for e in g):g.append(item);break
        else:groups.append([item])
    out=[]
    for g in groups:
        g.sort(key=lambda x:(x["y1"],x["x1"]))
        out.append({"text":" ".join(x["text"] for x in g),"x1":min(x["x1"] for x in g),"y1":min(x["y1"] for x in g),"x2":max(x["x2"] for x in g),"y2":max(x["y2"] for x in g),"items":g})
    return out

def remove_text(image,items):
    rgb=np.array(image);bgr=cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR);mask=np.zeros((image.height,image.width),dtype=np.uint8)
    for x in items:
        pad=max(2,min(7,int((x["y2"]-x["y1"])*.2)))
        x1=max(0,int(x["x1"]-pad));y1=max(0,int(x["y1"]-pad));x2=min(image.width-1,int(x["x2"]+pad));y2=min(image.height-1,int(x["y2"]+pad))
        if x2>x1 and y2>y1:cv2.rectangle(mask,(x1,y1),(x2,y2),255,-1)
    mask=cv2.dilate(mask,np.ones((3,3),np.uint8),iterations=1)
    result=cv2.inpaint(bgr,mask,3,cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(result,cv2.COLOR_BGR2RGB))

def translate_texts(texts,target):
    """
    Traduction avec plusieurs moteurs.
    MyMemory est essayé en premier pour éviter le blocage actuel de
    Google Translate via deep-translator. Google reste un secours.
    """
    if not texts:
        return []

    # MyMemory supporte aussi translate_batch() et l'auto-détection.
    # On l'utilise en premier afin de ne plus dépendre de la limite
    # Google qui bloque actuellement l'adresse IP de la machine.
    try:
        translator=MyMemoryTranslator(source="auto",target=target)
        translations=translator.translate_batch(texts)
        if translations and len(translations)==len(texts):
            return translations
    except Exception as exc:
        print(f"MyMemory indisponible, tentative Google : {exc}")

    # Secours Google : une seule requête batch, puis quelques retries.
    translator=GoogleTranslator(source="auto",target=target)
    import time
    last_error=None
    for attempt in range(4):
        try:
            translations=translator.translate_batch(texts)
            if translations and len(translations)==len(texts):
                return translations
        except Exception as exc:
            last_error=exc
            message=str(exc).lower()
            if "toomanyrequests" in message or "too many requests" in message:
                time.sleep(3 * (attempt + 1))
            else:
                raise

    raise RuntimeError(
        "Aucun service de traduction n'est disponible actuellement. "
        f"MyMemory a échoué et Google refuse les requêtes : {last_error}"
    )

def translate_image(input_image,output_image,target="fr"):
    input_image=os.path.abspath(input_image)
    if target not in SUPPORTED_LANGUAGES:raise ValueError(f"Langue non supportée: {target}")
    if not os.path.isfile(input_image):raise FileNotFoundError(input_image)

    original=Image.open(input_image).convert("RGB")
    ow,oh=original.size
    scale=min(1.0,OCR_MAX_SIDE/max(ow,oh))
    ocr_image=original
    temp_path=None
    try:
        if scale<1.0:
            nw=max(1,int(ow*scale));nh=max(1,int(oh*scale))
            ocr_image=original.resize((nw,nh),Image.Resampling.LANCZOS)
            fd,temp_path=tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            ocr_image.save(temp_path,"JPEG",quality=88)
            ocr_source=temp_path
        else:
            ocr_source=input_image

        result=ocr.predict(ocr_source)
    finally:
        if temp_path and os.path.exists(temp_path):os.remove(temp_path)

    if not result:
        original.save(output_image,"PNG")
        return output_image

    data=result[0]
    texts=data.get("rec_texts",[])
    boxes=data.get("rec_boxes",[])
    items=[]
    inv=1.0/scale
    for text,box in zip(texts,boxes):
        text=str(text).strip()
        if not text:continue
        x1,y1,x2,y2=[int(float(v)*inv) for v in box]
        items.append({"text":text,"x1":x1,"y1":y1,"x2":x2,"y2":y2,"cx":(x1+x2)/2,"cy":(y1+y2)/2,"height":max(1,y2-y1)})

    if not items:
        original.save(output_image,"PNG")
        return output_image

    blocks=group_text(items)
    translations=translate_texts([b["text"] for b in blocks],target)
    for b,t in zip(blocks,translations):b["translated"]=t or b["text"]

    image=remove_text(original,[i for b in blocks for i in b["items"]])
    draw=ImageDraw.Draw(image)

    for b in blocks:
        h=int(np.median([i["height"] for i in b["items"]]))
        base=max(8,int(h*.9))
        left=max(0,b["x1"]-max(5,int(h*.45)));top=max(0,b["y1"]-max(5,int(h*.45)))
        right=min(image.width,b["x2"]+max(5,int(h*.45)));bottom=min(image.height,b["y2"]+max(5,int(h*.45)))
        aw=max(80,right-left);ah=max(20,bottom-top);size=base
        while size>7:
            font=get_font(size);wrapped=wrap_text(str(b["translated"]),aw,font,draw)
            bb=draw.multiline_textbbox((0,0),wrapped,font=font,spacing=3,align="center")
            if bb[2]-bb[0]<=aw and bb[3]-bb[1]<=ah:break
            size-=1
        bb=draw.multiline_textbbox((0,0),wrapped,font=font,spacing=3,align="center")
        tx=(left+right-(bb[2]-bb[0]))/2;ty=(top+bottom-(bb[3]-bb[1]))/2
        draw.multiline_text((tx+1,ty+1),wrapped,font=font,fill=(255,255,255),spacing=3,align="center")
        draw.multiline_text((tx,ty),wrapped,font=font,fill=(0,0,0),spacing=3,align="center")

    os.makedirs(os.path.dirname(os.path.abspath(output_image)),exist_ok=True)
    image.save(output_image,"PNG")
    return output_image

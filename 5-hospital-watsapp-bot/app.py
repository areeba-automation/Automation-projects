from flask import Flask, request
import requests, json, numpy as np, faiss, wave, unicodedata, re
from datetime import datetime
import phonenumbers

from sentence_transformers import SentenceTransformer
from vosk import Model, KaldiRecognizer
from pydub import AudioSegment

import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

VERIFY_TOKEN = "12345"
WHATSAPP_TOKEN = "your token"
PHONE_NUMBER_ID = "your id"
MANAGER_PHONE = "manager phone num"

user_sessions = {}

# ---------------- LANGUAGE DETECTION ----------------
def detect_language(text):
    for c in text:
        if '\u0600' <= c <= '\u06FF':
            return "urdu"
    roman_words = ["dil","dard","haan","han","ji"]
    for w in roman_words:
        if w in text.lower():
            return "roman"
    return "english"

# ---------------- NORMALIZE ----------------
def normalize_text(text):
    return unicodedata.normalize("NFKD", text.lower().strip())

# ---------------- URDU → ROMAN ----------------
def urdu_to_roman(text):
    mapping = {
        "دل کا درد":"dil ka dard",
        "دل میں درد":"dil ka dard",
        "سینہ درد":"dil ka dard",
        "ہارٹ کا مسئلہ":"heart problem"
    }
    for u,r in mapping.items():
        text = text.replace(u,r)
    return text

# ---------------- VOICE MISHEARING FIX ----------------
def normalize_voice_text(text):
    mapping = {
        "then call that": "دل کا درد",
        "dill ka dard": "دل کا درد",
        "dil ka darad": "دل کا درد",
        "dill ka darad": "دل کا درد",
    }
    text_lower = text.lower()
    for k,v in mapping.items():
        if k in text_lower:
            text_lower = text_lower.replace(k,v)
    return text_lower

# ---------------- CONFIRM WORDS ----------------
confirm_words = ["yes","y","haan","han","ha","ہاں","ji","ok","okay","theek","thik"]

# ---------------- GOOGLE SHEET ----------------
def save_appointment(name, phone, doctor):
    scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("your credential.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Hospital-Leads").sheet1
    sheet.append_row([name, phone, doctor, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    print("Appointment saved")

# ---------------- LOAD DATA ----------------
with open("hospital_data.json") as f:
    hospital_data = json.load(f)

model_embed = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = np.array(model_embed.encode([json.dumps(i) for i in hospital_data])).astype("float32")

index = faiss.IndexFlatL2(embeddings.shape[1])
index.add(embeddings)

vosk_model = Model("vosk-model-small-en-us-0.15")

# ---------------- SPEECH ----------------
def speech_to_text(file):
    wf = wave.open(file, "rb")
    rec = KaldiRecognizer(vosk_model, wf.getframerate())
    text = ""
    while True:
        data = wf.readframes(4000)
        if len(data)==0: break
        if rec.AcceptWaveform(data):
            text += json.loads(rec.Result()).get("text","")
    text += json.loads(rec.FinalResult()).get("text","")
    return text

def download_audio(media_id):
    url=f"https://graph.facebook.com/v18.0/{media_id}"
    headers={"Authorization":f"Bearer {WHATSAPP_TOKEN}"}
    media_url=requests.get(url,headers=headers).json()["url"]
    audio=requests.get(media_url,headers=headers)
    open("voice.ogg","wb").write(audio.content)
    sound=AudioSegment.from_file("voice.ogg")
    sound=sound.set_frame_rate(16000).set_channels(1).set_sample_width(2)
    sound.export("voice.wav",format="wav")
    return "voice.wav"

# ---------------- SEND MESSAGE ----------------
def send_msg(phone,text):
    url=f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers={"Authorization":f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"}
    payload={"messaging_product":"whatsapp","to":phone,"type":"text","text":{"body":text}}
    requests.post(url,headers=headers,json=payload)

# ---------------- REPLIES ----------------
def reply_doctor(r,lang):
    if lang=="urdu":
        return f"""ڈاکٹر دستیاب ہیں

شعبہ: {r['department']}
ڈاکٹر: {r['doctor']}
اوقات: {r['timing']}
فیس: {r['fee']} روپے

اگر آپ اپوائنٹمنٹ بک کروانا چاہتے ہیں تو YES / ہاں / han لکھیں"""
    elif lang=="roman":
        return f"""Doctor mojood he

Department: {r['department']}
Doctor: {r['doctor']}
Timing: {r['timing']}
Fee: Rs {r['fee']}

Agr appointment book krne he to YES / haan / han likhein"""
    else:
        return f"""Doctor Available

Department: {r['department']}
Doctor: {r['doctor']}
Timing: {r['timing']}
Fee: Rs {r['fee']}

If you want to book an appointment reply YES"""

def ask_name_phone(lang):
    if lang=="urdu": return "براہ کرم اپنا نام اور فون نمبر بھیجیں۔"
    elif lang=="roman": return "Apna naam aur phone number bhejein."
    else: return "Please send your name and phone number."

def invalid_number_msg(lang):
    return {
        "urdu":"براہ کرم درست فون نمبر بھیجیں۔",
        "roman":"Phone number correct bhejein.",
        "english":"Please enter a valid phone number."
    }[lang]

def confirm_msg(name,phone,doc,lang):
    if lang=="urdu":
        return f"""اپائنٹمنٹ کنفرم ہوگئی

نام: {name}
ڈاکٹر: {doc}
فون: {phone}"""
    elif lang=="roman":
        return f"""Appointment confirm ho gayi

Name: {name}
Doctor: {doc}
Phone: {phone}"""
    else:
        return f"""Appointment Confirmed

Name: {name}
Doctor: {doc}
Phone: {phone}"""

def no_info_msg(lang):
    if lang=="urdu": return "معلومات دستیاب نہیں، ہسپتال مینیجر آپ سے رابطہ کرے گا"
    elif lang=="roman": return "Information nahi mili, hospital manager aap se contact karega"
    else: return "Information not available, hospital manager will contact you"

# ---------------- PHONE PARSE ----------------
def extract_valid_phone(text):
    numbers = re.findall(r'\+?\d[\d\s\-]{7,15}', text)
    for num in numbers:
        try:
            num = num.replace(" ", "").replace("-", "")
            if num.startswith("+"):
                parsed = phonenumbers.parse(num, None)
            elif num.startswith("0"):
                parsed = phonenumbers.parse(num, "PK")
            elif num.startswith("92"):
                parsed = phonenumbers.parse("+" + num, None)
            else:
                parsed = phonenumbers.parse(num, None)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except:
            continue
    return None

# ---------------- SEARCH ----------------
def search(q):
    # Exact keyword match first
    for i in hospital_data:
        for k in i.get("keywords",[]):
            if k in q or q in k:
                return i
    # Otherwise semantic search
    emb=np.array(model_embed.encode([q])).astype("float32")
    D,I=index.search(emb,1)
    return None if D[0][0]>1.3 else hospital_data[int(I[0][0])]

# ---------------- WEBHOOK ----------------
@app.route("/webhook",methods=["POST"])
def webhook():
    data=request.get_json()
    try:
        value=data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return "OK",200
        msg=value["messages"][0]
        phone=msg["from"]
        print("User raw:", msg.get("text", {}).get("body","[voice/audio]"))

        if phone not in user_sessions:
            user_sessions[phone]={"stage":"normal","lang":None}

        session=user_sessions[phone]

        # TEXT / VOICE
        if msg["type"]=="text":
            text=msg["text"]["body"]
        else:
            file=download_audio(msg["audio"]["id"])
            text=speech_to_text(file)
            print("🎤 Voice raw:", text)
            text = normalize_voice_text(text)

        if session["stage"]=="normal":
            session["lang"]=detect_language(text)

        lang=session["lang"]
        text=normalize_text(text)
        text=urdu_to_roman(text)

        # -------- FLOW --------
        if session["stage"]=="confirm":
            if text in confirm_words:
                session["stage"]="ask_name_phone"
                send_msg(phone, ask_name_phone(lang))
                return "OK",200

        elif session["stage"]=="ask_name_phone":
            phone_num = extract_valid_phone(text)
            if not phone_num:
                send_msg(phone, invalid_number_msg(lang))
                return "OK",200
            name = re.sub(r'\+?\d[\d\s\-]{7,15}', '', text).strip()
            session["name"] = name if name else "Unknown"
            session["phone"] = phone_num
            save_appointment(session["name"], session["phone"], session["doctor"])
            send_msg(phone, confirm_msg(session["name"], session["phone"], session["doctor"], lang))
            session["stage"]="normal"
            return "OK",200

        # -------- SEARCH --------
        res=search(text)
        if not res:
            send_msg(phone,no_info_msg(lang))
            manager_text=f"User Query Not Found\nMessage: {text}\nPhone: {phone}"
            send_msg(MANAGER_PHONE, manager_text)
            return "OK",200

        session["doctor"]=res["doctor"]
        session["stage"]="confirm"
        send_msg(phone,reply_doctor(res,lang))

    except Exception as e:
        print("Error:",e)
    return "OK",200

if __name__=="__main__":
    app.run(port=5000)
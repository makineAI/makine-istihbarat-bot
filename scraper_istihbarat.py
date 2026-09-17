import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

BASEROW_TOKEN = os.getenv("BASEROW_TOKEN")
TABLE_ID = "1197631"  # mai_istihbarat
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BASE_URL = "https://www.insaatyatirim.com"
# SADECE 1. SAYFA
SOURCE_URL = "https://www.insaatyatirim.com/Haberler/yatirim-haberleri/13"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Python tarafında doğrudan elenecek alakasız kelimeler
NEGATIF_KELIMELER = ["konut", "villa", "daire", "rezidans", "otel", "turizm", "kira", "imar"]

def get_existing_links():
    existing = set()
    url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/?user_field_names=true&size=200"
    headers = {"Authorization": f"Token {BASEROW_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            for row in r.json().get("results", []):
                link = row.get("Kaynak_Haber_Linki")
                if link:
                    existing.add(link.strip())
    except Exception as e:
        print(f"[-] Baserow link okuma hatası: {e}")
    return existing

def archive_old_records():
    print("[*] 30 günden eski fırsatlar kontrol ediliyor...")
    url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/?user_field_names=true&size=200"
    headers = {"Authorization": f"Token {BASEROW_TOKEN}"}
    cutoff = datetime.now() - timedelta(days=30)
    
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            for row in r.json().get("results", []):
                t_str = row.get("Tarih")
                durum = row.get("Durum", {})
                d_val = durum.get("value") if isinstance(durum, dict) else str(durum)
                
                if t_str and d_val != "Arşiv":
                    try:
                        if datetime.strptime(t_str, "%Y-%m-%d") < cutoff:
                            patch_url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/{row['id']}/?user_field_names=true"
                            requests.patch(patch_url, headers=headers, json={"Durum": "Arşiv"})
                            print(f"[!] Fırsat ID {row['id']} arşive alındı.")
                    except ValueError:
                        pass
    except Exception as e:
        print(f"[-] Arşivleme hatası: {e}")

def scrape_page_one(existing_links):
    print(f"[*] Sadece 1. sayfa taranıyor: {SOURCE_URL}")
    r = requests.get(SOURCE_URL, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"[-] Sayfa açılamadı: {r.status_code}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    anchors = soup.find_all("a", href=True)
    items = []
    seen = set()

    for a in anchors:
        href = a["href"]
        if "/Haber/" in href or "/haber/" in href:
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            title = a.get_text(strip=True)
            
            # Başlık filtresi
            if len(title) > 25 and full_url not in existing_links and full_url not in seen:
                # Alakasız konut/villa haberlerini baştan ele
                title_lower = title.lower()
                if any(neg in title_lower for neg in NEGATIF_KELIMELER):
                    continue

                seen.add(full_url)
                items.append({"title": title, "url": full_url})

    print(f"[+] 1. sayfada işlenebilecek {len(items)} adet aday haber tespit edildi.")
    return items

def analyze_with_gemini(title, url):
    full_text = title
    try:
        detay_r = requests.get(url, headers=HEADERS, timeout=10)
        if detay_r.status_code == 200:
            dsoup = BeautifulSoup(detay_r.text, "html.parser")
            paragraphs = [p.get_text(strip=True) for p in dsoup.find_all("p") if len(p.get_text(strip=True)) > 40]
            if paragraphs:
                full_text = " ".join(paragraphs[:3])
    except Exception:
        pass

    prompt = f"""
Sen iş makineleri (ekskavatör, loder, beko loder vb.) ve istif makineleri (forklift, reach truck, akülü transpalet vb.) sektöründe uzman bir satış istihbaratçısısın.

GÖREVİN:
Aşağıdaki haberi incele. Bu haber DOĞRUDAN aşağıdaki iki sektörden birine makine satışı veya kiralaması fırsatı yaratıyor mu?
1. İŞ MAKİNESİ: Ağır sanayi, altyapı, büyük fabrika kaba inşaatı, hafriyat, liman vb.
2. İSTİF MAKİNESİ: Lojistik depo, antrepo, fabrika içi üretim tesisi, soğuk hava deposu, dağıtım merkezi vb.

EĞER bu iki sektörle HİÇBİR İLGİSİ YOKSA (örneğin sadece konut, arsa, daire, mevzuat, bürokratik atama vb. ise):
SADECE "ILGISIZ" yaz.

EĞER UYGUNSA:
SADECE aşağıdaki JSON formatında yanıt ver (Markdown tırnakları ```json KULLANMA):
{{
  "İstihbarat_Basligi": "Net, profesyonel başlık (Örn: X Kimya Gebze Yeni Depo İnşası)",
  "Hedef_Firma": "Yatırım yapan ana firma adı (Bulunamazsa 'Bilinmiyor')",
  "İstihbarat_Turu": "Yeni Yatırım / Tesis",
  "İlgili_Sektor": "İstif makinesi VEYA İş makinesi (İkisinden birini tam bu yazımla seç)",
  "Potansiyel_İhtiyac": "Düşük (1-2 Makine) VEYA Orta (3-10 Makine) VEYA Yüksek (10+ Makine)",
  "Sehir_Bolge": "İl / İlçe veya Bölge",
  "İstihbarat_Detayı": "Tahmini makine modelleri (örn: 4 adet Reach Truck, 2 Transpalet) ve satış ekibi için kısa aksiyon tavsiyesi."
}}

Haber Başlığı: {title}
Haber Detayı: {full_text}
"""
    api_url = f"[https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=](https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=){GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        res = requests.post(api_url, json=payload, headers={"Content-Type": "application/json"}, timeout=25)
        raw = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        if "ILGISIZ" in raw:
            print(f"[-] Sektör dışı haber elendi: {title[:40]}...")
            return None

        if raw.startswith("```"):
            raw = raw.strip("`").replace("json", "").strip()
            
        return json.loads(raw)
    except Exception as e:
        print(f"[-] Analiz atlandı: {e}")
        return None

def save_to_baserow(data, source_url):
    url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/?user_field_names=true"
    headers = {"Authorization": f"Token {BASEROW_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "İstihbarat_Basligi": data.get("İstihbarat_Basligi"),
        "Hedef_Firma": data.get("Hedef_Firma"),
        "İstihbarat_Turu": data.get("İstihbarat_Turu"),
        "İlgili_Sektor": data.get("İlgili_Sektor"),
        "Potansiyel_İhtiyac": data.get("Potansiyel_İhtiyac"),
        "Sehir_Bolge": data.get("Sehir_Bolge"),
        "İstihbarat_Detayı": data.get("İstihbarat_Detayı"),
        "Tarih": datetime.now().strftime("%Y-%m-%d"),
        "Kaynak_Haber_Linki": source_url,
        "Durum": "Aktif Fırsat"
    }
    r = requests.post(url, headers=headers, json=payload)
    if r.status_code in [200, 201]:
        print(f"[✓] İstihbarat Baserow'a eklendi: {data.get('İstihbarat_Basligi')}")
    else:
        print(f"[-] Baserow kayıt hatası: {r.status_code} - {r.text}")

def main():
    archive_old_records()
    existing = get_existing_links()
    news = scrape_page_one(existing)
    
    for n in news:
        intel = analyze_with_gemini(n["title"], n["url"])
        if intel:
            save_to_baserow(intel, n["url"])

if __name__ == "__main__":
    main()

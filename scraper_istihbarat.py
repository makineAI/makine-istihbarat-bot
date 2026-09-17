import os
import json
import requests
from urllib.parse import quote, urlparse
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
from google import genai

BASEROW_TOKEN = os.getenv("BASEROW_TOKEN", "").strip()
TABLE_ID = "1197631"  # mai_istihbarat
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

ai_client = genai.Client(api_key=GEMINI_API_KEY)

BASE_URL = "https://www.insaatyatirim.com"
SOURCE_URL = "https://www.insaatyatirim.com/Haberler/yatirim-haberleri/13"

NEGATIF_KELIMELER = ["konut", "villa", "daire", "rezidans", "otel", "turizm", "kira", "imar"]

def sanitize_url(raw_url):
    """URL içindeki Türkçe karakterleri encode ederek Baserow validasyonunu garantiye alır."""
    try:
        raw_url = raw_url.strip()
        if not raw_url.startswith("http"):
            raw_url = f"{BASE_URL}/{raw_url.lstrip('/')}"
        parsed = urlparse(raw_url)
        safe_path = quote(parsed.path)
        return f"{parsed.scheme}://{parsed.netloc}{safe_path}"
    except Exception:
        return raw_url

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

def scrape_with_browser(existing_links):
    print(f"[*] Gerçek Chromium başlatılıyor: {SOURCE_URL}")
    items = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="tr-TR"
        )
        page = context.new_page()
        
        try:
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            html_content = page.content()
        except Exception as e:
            print(f"[-] Sayfa yükleme hatası: {e}")
            browser.close()
            return []

        soup = BeautifulSoup(html_content, "html.parser")
        cards = soup.find_all("div", class_="trending-news-item")
        seen = set()

        for card in cards:
            title_el = card.find("h3", class_="title")
            if not title_el:
                continue
            
            a_tag = title_el.find("a", href=True)
            if not a_tag:
                continue

            href = a_tag["href"].strip()
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            title = a_tag.get_text(strip=True)

            date_el = card.find("div", class_="meta-date")
            news_date = None
            if date_el and date_el.find("span"):
                raw_date = date_el.find("span").get_text(strip=True)
                try:
                    news_date = datetime.strptime(raw_date, "%d.%m.%Y").strftime("%Y-%m-%d")
                except Exception:
                    news_date = datetime.now().strftime("%Y-%m-%d")

            if not news_date:
                news_date = datetime.now().strftime("%Y-%m-%d")

            # sanitize_url ile Baserow'daki kayıtlı linklerle tam örtüşmesini sağla
            safe_url = sanitize_url(full_url)

            if safe_url not in existing_links and safe_url not in seen:
                title_lower = title.lower()
                if any(neg in title_lower for neg in NEGATIF_KELIMELER):
                    continue

                seen.add(safe_url)
                
                detail_text = title
                try:
                    detail_page = context.new_page()
                    detail_page.goto(full_url, wait_until="domcontentloaded", timeout=25000)
                    detail_soup = BeautifulSoup(detail_page.content(), "html.parser")
                    paragraphs = [p.get_text(strip=True) for p in detail_soup.find_all("p") if len(p.get_text(strip=True)) > 40]
                    if paragraphs:
                        detail_text = " ".join(paragraphs[:3])
                    detail_page.close()
                except Exception:
                    pass

                items.append({
                    "title": title,
                    "url": safe_url,
                    "date": news_date,
                    "detail_text": detail_text
                })

        browser.close()

    print(f"[+] 1. sayfada hedefe uygun {len(items)} adet aday haber çekildi.")
    return items

def analyze_with_gemini(title, full_text):
    prompt = f"""
Sen iş makineleri ve istif makineleri sektöründe uzman bir satış istihbaratçısısın.

GÖREVİN:
Aşağıdaki haberi incele. Bu haber DOĞRUDAN makine satışı/kiralaması fırsatı yaratıyor mu?
- İŞ MAKİNESİ: Ağır sanayi, maden, altyapı, büyük fabrika kaba inşaatı, hafriyat vb.
- İSTİF MAKİNESİ: Depo, antrepo, fabrika içi lojistik, soğuk hava deposu, dağıtım merkezi vb.

İlgisizse SADECE "ILGISIZ" yaz.

Uygunsa SADECE aşağıdaki JSON formatında yanıt ver:
{{
  "İstihbarat_Basligi": "Net profesyonel başlık",
  "Hedef_Firma": "Yatırım yapan ana firma adı (Bulunamazsa 'Bilinmiyor')",
  "İstihbarat_Turu": "Yeni Yatırım / Tesis",
  "Sektor": "İstif Makinesi veya İş Makinesi",
  "Potansiyel_İhtiyac": "Düşük (1-2 Makine) veya Orta (3-10 Makine) veya Yüksek (10+ Makine)",
  "Sehir_Bolge": "İl / İlçe veya Bölge",
  "İstihbarat_Detayı": "Tahmini makine modelleri ve satış ekibi için kısa aksiyon tavsiyesi."
}}

Haber Başlığı: {title}
Haber Detayı: {full_text}
"""
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        raw = response.text.strip()
        
        if "ILGISIZ" in raw:
            print(f"[-] Sektör dışı haber elendi: {title[:40]}...")
            return None

        if raw.startswith("```"):
            raw = raw.strip("`").replace("json", "").strip()
            
        data = json.loads(raw)
        
        # Kesin kural: Sadece bu iki ifadeden biri gidecek
        sektor_metin = str(data.get("Sektor", "")).lower()
        if "istif" in sektor_metin:
            data["İlgili_Sektor"] = "İstif Makinesi"
        else:
            data["İlgili_Sektor"] = "İş Makinesi"

        return data
    except Exception as e:
        print(f"[-] Analiz atlandı: {e}")
        return None

def save_to_baserow(data, source_url, news_date):
    url = f"[https://api.baserow.io/api/database/rows/table/](https://api.baserow.io/api/database/rows/table/){TABLE_ID}/?user_field_names=true"
    headers = {"Authorization": f"Token {BASEROW_TOKEN}", "Content-Type": "application/json"}
    
    payload = {
        "İstihbarat_Basligi": data.get("İstihbarat_Basligi"),
        "Hedef_Firma": data.get("Hedef_Firma"),
        "İstihbarat_Turu": data.get("İstihbarat_Turu", "Yeni Yatırım / Tesis"),
        "İlgili_Sektor": data.get("İlgili_Sektor"),
        "Potansiyel_İhtiyac": data.get("Potansiyel_İhtiyac"),
        "Sehir_Bolge": data.get("Sehir_Bolge"),
        "İstihbarat_Detayı": data.get("İstihbarat_Detayı"),
        "Tarih": news_date,
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
    news = scrape_with_browser(existing)
    
    for n in news:
        intel = analyze_with_gemini(n["title"], n["detail_text"])
        if intel:
            save_to_baserow(intel, n["url"], n["date"])

if __name__ == "__main__":
    main()

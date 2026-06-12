import os
import re
import time
import json
import gc
from datetime import datetime
import urllib.parse

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==========================================
# KONFIGURASI
# ==========================================
SPREADSHEET_ID = "1VzjLPm9BquUlYOpvkmlE1QjswXYCVJ5dYko2e0BTblU"
SERVICE_ACCOUNT_FILE = "sjs-lelang-scraper-614129427aff.json"

# 🔥 KHUSUS UNTUK GITHUB ACTIONS 🔥
# Kode ini membaca credentials dari environment variable (aman)
if "GOOGLE_APPLICATION_CREDENTIALS_JSON" in os.environ:
    creds_json = os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"]
    with open(SERVICE_ACCOUNT_FILE, "w") as f:
        f.write(creds_json)
    print("[*] Credentials loaded from environment variable")

HEADLESS_MODE = True
CHECKPOINT_FILE = "scrape_checkpoint.json"
TIMEOUT_PER_URL = 15
MAX_CONSECUTIVE_FAILURES = 5

BASE_URL = "https://lelang.go.id/lot-lelang/katalog-lot-lelang?provinsi=ef893b4b-98f3-4e90-9bf3-7462a2689f3e&page=1&kategori=Tanah&kategori=Rumah&kategori=Ruko&kategori=Toko&kota=718cb234-9887-4445-ad4b-2fc7923c8763&kota=d26248fa-8925-4b83-a495-5359b39315b7&kota=b888b30c-f6bb-4d7d-92a1-4bb92dbdbf98"

def init_driver(headless=True):
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def save_checkpoint(index, urls):
    checkpoint_data = {"index": index, "total": len(urls), "urls": urls, "timestamp": time.time()}
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint_data, f)
    print(f"[*] Checkpoint disimpan: {index}/{len(urls)}")

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                data = json.load(f)
                if "index" in data and "total" in data and "urls" in data:
                    return data
                else:
                    print("[!] File checkpoint corrupt, mulai dari awal.")
                    return None
        except:
            print("[!] File checkpoint corrupt, mulai dari awal.")
            return None
    return None

def clear_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("[*] Checkpoint dihapus.")

def get_all_detail_urls(driver, start_url):
    detail_urls = []
    page_num = 1
    max_pages = 50
    consecutive_empty = 0
    
    while page_num <= max_pages:
        if page_num == 1:
            url = start_url
        else:
            if '&page=' in start_url:
                url = re.sub(r'&page=\d+', f'&page={page_num}', start_url)
            else:
                url = start_url + f'&page={page_num}'
        
        print(f"[*] Membuka halaman {page_num}: {url}")
        driver.get(url)
        time.sleep(8)
        
        for _ in range(3):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        links_found = 0
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/detail-auction/' in href:
                full_url = urllib.parse.urljoin("https://lelang.go.id", href)
                if full_url not in detail_urls:
                    detail_urls.append(full_url)
                    links_found += 1
        
        print(f"    -> Menemukan {links_found} URL detail baru. Total: {len(detail_urls)}")
        
        if links_found == 0:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print(f"[*] 3 halaman berturut-turut kosong, berhenti.")
                break
        else:
            consecutive_empty = 0
        
        page_num += 1
        time.sleep(3)
    
    print(f"[*] Total URL detail dari {page_num-1} halaman: {len(detail_urls)}")
    return detail_urls

def parse_date_indonesia(date_str):
    months = {
        'Januari': '01', 'Februari': '02', 'Maret': '03', 'April': '04',
        'Mei': '05', 'Juni': '06', 'Juli': '07', 'Agustus': '08',
        'September': '09', 'Oktober': '10', 'November': '11', 'Desember': '12',
        'January': '01', 'February': '02', 'March': '03', 'May': '05',
        'June': '06', 'July': '07', 'August': '08', 'October': '10'
    }
    for month_name, month_num in months.items():
        if month_name in date_str:
            date_str = date_str.replace(month_name, month_num)
            break
    parts = re.findall(r'\d+', date_str)
    if len(parts) >= 3:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return date_str

def clean_currency(amount_str):
    return re.sub(r'[^0-9]', '', amount_str)

def extract_detail_data(driver, url, timeout_minutes=15):
    start_time = time.time()
    attempt = 0
    
    while True:
        attempt += 1
        elapsed = (time.time() - start_time) / 60
        
        if elapsed > timeout_minutes:
            print(f"[!] Gagal setelah {timeout_minutes} menit. Melewati URL: {url}")
            return None, None, True
        
        try:
            print(f"[*] Mengambil data dari: {url} (percobaan {attempt}, sudah {elapsed:.1f} menit)")
            driver.get(url)
            time.sleep(5)
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            
            data_publik = {}
            data_internal = {}
            
            # Nama Objek (bersihkan m2berikut menjadi m² berikut)
            title_elem = soup.find('h3', class_='mb-5 text-2xl')
            if not title_elem:
                title_elem = soup.find('h3', class_='text-2xl')
            nama_objek = title_elem.get_text(strip=True) if title_elem else 'N/A'
            nama_objek = nama_objek.replace('m2berikut', 'm² berikut')
            data_publik['nama_objek'] = nama_objek
            
            # Kode Lelang
            kode_elem = None
            for h6 in soup.find_all('h6'):
                if 'Kode Lot Lelang' in h6.get_text():
                    kode_elem = h6.find_next('h6')
                    break
            if kode_elem:
                data_publik['kode_lelang'] = kode_elem.get_text(strip=True)
            else:
                match = re.search(r'/detail-auction/([a-f0-9-]+)', url)
                data_publik['kode_lelang'] = match.group(1) if match else 'Tidak diketahui'
            data_internal['kode_lelang'] = data_publik.get('kode_lelang', '')
            
            # Limit Harga
            limit_elem = None
            for h4 in soup.find_all('h4'):
                if 'Nilai Limit' in h4.get_text():
                    limit_elem = h4.find_next('h6')
                    break
            if limit_elem:
                data_publik['limit_harga'] = re.sub(r'[^0-9]', '', limit_elem.get_text())
            else:
                data_publik['limit_harga'] = '0'
            
            # Uang Jaminan
            jaminan_elem = None
            for h4 in soup.find_all('h4'):
                if 'Uang Jaminan' in h4.get_text():
                    jaminan_elem = h4.find_next('h6')
                    break
            if jaminan_elem:
                data_publik['uang_jaminan'] = re.sub(r'[^0-9]', '', jaminan_elem.get_text())
            else:
                data_publik['uang_jaminan'] = '0'
            
            # Tanggal Batas Akhir Penawaran
            batas_elem = None
            for h4 in soup.find_all('h4'):
                if 'Batas Akhir Penawaran' in h4.get_text():
                    batas_elem = h4.find_next('h6')
                    break
            data_publik['tanggal_batas_akhir_penawaran'] = batas_elem.get_text(strip=True) if batas_elem else ''
            
            # Tanggal Batas Setor Jaminan
            setor_elem = None
            for h4 in soup.find_all('h4'):
                if 'Batas Akhir Setor Uang Jaminan' in h4.get_text():
                    setor_elem = h4.find_next('h6')
                    break
            data_publik['tanggal_batas_setor_jaminan'] = setor_elem.get_text(strip=True) if setor_elem else ''
            
            # Status
            if data_publik.get('tanggal_batas_akhir_penawaran'):
                try:
                    tgl_str = data_publik['tanggal_batas_akhir_penawaran'].replace(' WIB', '')
                    tgl = datetime.strptime(tgl_str, '%d %B %Y %H:%M')
                    data_publik['status'] = 'Aktif' if tgl.date() >= datetime.now().date() else 'Kadaluarsa'
                except:
                    data_publik['status'] = 'Aktif'
            else:
                data_publik['status'] = 'Aktif'
            
            # ALAMAT & LUAS TANAH (bersihkan "Lihat Lokasi-")
            alamat = ''
            luas = '0'
            
            uraian_section = soup.find('div', id='pr_id_2_content_0')
            if not uraian_section:
                uraian_section = soup.find('div', class_='p-tabview-panel')
            
            if uraian_section:
                for div in uraian_section.find_all(['div', 'p']):
                    text = div.get_text(strip=True)
                    if 'Alamat:' in text:
                        alamat = text.split('Alamat:')[-1].strip()
                        # Bersihkan "Lihat Lokasi-" atau "Lihat Lokasi"
                        alamat = re.sub(r'Lihat Lokasi-?', '', alamat).strip()
                        break
                
                for div in uraian_section.find_all(['div', 'p']):
                    text = div.get_text(strip=True)
                    match = re.search(r'Luas:\s*([\d.,]+)\s*M²', text, re.IGNORECASE)
                    if match:
                        luas = match.group(1).replace('.', '').replace(',', '')
                        break
            
            if not alamat:
                alamat = nama_objek
            
            data_publik['alamat_lengkap'] = alamat
            data_publik['luas_tanah'] = luas if luas != '0' else '0'
            
            # JENIS BARANG & BUKTI KEPEMILIKAN
            jenis_barang = ''
            bukti_kepemilikan = ''
            
            if uraian_section:
                for div in uraian_section.find_all(['div', 'p']):
                    text = div.get_text(strip=True)
                    if 'Jenis Barang' in text:
                        next_div = div.find_next('div')
                        if next_div:
                            jenis_barang = next_div.get_text(strip=True)
                    if 'Bukti Kepemilikan' in text:
                        next_div = div.find_next('div')
                        if next_div:
                            bukti_kepemilikan = next_div.get_text(strip=True)
            
            data_publik['jenis_barang'] = jenis_barang
            data_publik['bukti_kepemilikan'] = bukti_kepemilikan
            
            # SET JENIS_UNIT BERDASARKAN JENIS_BARANG
            jenis_barang_lower = jenis_barang.lower()
            if 'tanah' in jenis_barang_lower:
                data_publik['jenis_unit'] = 'Tanah'
            elif 'rumah' in jenis_barang_lower:
                data_publik['jenis_unit'] = 'Rumah'
            elif 'ruko' in jenis_barang_lower:
                data_publik['jenis_unit'] = 'Ruko'
            elif 'toko' in jenis_barang_lower:
                data_publik['jenis_unit'] = 'Toko'
            else:
                data_publik['jenis_unit'] = ''
            
            # 🔥 TAWARKAN KE INVESTOR OTOMATIS "YA"
            data_publik['tawarkan_ke_investor'] = 'YA'
            
            # FOTO
            foto_urls = []
            for img in soup.select('.scrollbar-hide img'):
                src = img.get('src')
                if src and src.startswith('http') and 'logo' not in src:
                    foto_urls.append(src)
            data_publik['foto_urls'] = ', '.join(foto_urls[:5])
            
            # DATA INTERNAL (termasuk url_sumber)
            penjual_elem = None
            for h4 in soup.find_all('h4'):
                if 'Penjual' in h4.get_text():
                    penjual_elem = h4.find_next('h6')
                    break
            data_internal['penjual'] = penjual_elem.get_text(strip=True) if penjual_elem else ''
            
            penyelenggara_elem = None
            for h4 in soup.find_all('h4'):
                if 'Penyelenggara' in h4.get_text():
                    penyelenggara_elem = h4.find_next('h6')
                    break
            data_internal['penyelenggara'] = penyelenggara_elem.get_text(strip=True) if penyelenggara_elem else ''
            
            # TAMBAHKAN URL SUMBER
            data_internal['url_sumber'] = url
            
            print(f"[*] Berhasil mengambil data untuk {data_publik.get('kode_lelang')}")
            return data_publik, data_internal, False
            
        except Exception as e:
            print(f"[!] Percobaan {attempt} gagal: {e}")
            
            if "timeout" in str(e).lower() or "connection" in str(e).lower():
                print("[*] Masalah koneksi, mereset driver...")
                driver.quit()
                time.sleep(2)
                driver = init_driver(HEADLESS_MODE)
                gc.collect()
            
            wait_time = min(30, attempt * 2)
            print(f"[*] Menunggu {wait_time} detik... (batas waktu {timeout_minutes} menit)")
            time.sleep(wait_time)

def update_sheet(ws, data_dict, key_col, headers):
    existing_data = ws.get_all_values()
    if not existing_data:
        ws.append_row(headers)
        existing_data = [headers]
    
    row_num = None
    for i, row in enumerate(existing_data):
        if row and len(row) > 0 and row[0] == data_dict.get('kode_lelang', ''):
            row_num = i + 1
            break
    
    row_values = [data_dict.get(col, '') for col in headers]
    
    if row_num:
        ws.update(f"A{row_num}:{chr(64+len(headers))}{row_num}", [row_values])
        print(f"    -> Update data untuk {data_dict.get('kode_lelang')}")
    else:
        ws.append_row(row_values)
        print(f"    -> Tambah data baru untuk {data_dict.get('kode_lelang')}")

def save_to_sheets(data_list, spreadsheet_id, service_account_file):
    print("[*] Menghubungkan ke Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(service_account_file, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(spreadsheet_id)

    header_aktif = [
        "kode_lelang", "nama_objek", "alamat_lengkap", "limit_harga", "uang_jaminan",
        "tanggal_batas_setor_jaminan", "tanggal_batas_akhir_penawaran", "status",
        "luas_tanah", "jenis_barang", "bukti_kepemilikan", "foto_urls", 
        "tawarkan_ke_investor", "peta", "jenis_unit"
    ]

    header_internal = ["kode_lelang", "penjual", "penyelenggara", "url_sumber"]

    try:
        ws_aktif = sheet.worksheet("AKTIF")
    except:
        ws_aktif = sheet.add_worksheet(title="AKTIF", rows="1000", cols="20")
        ws_aktif.append_row(header_aktif)

    try:
        ws_internal = sheet.worksheet("INTERNAL")
    except:
        ws_internal = sheet.add_worksheet(title="INTERNAL", rows="1000", cols="20")
        ws_internal.append_row(header_internal)

    for pub, itn in data_list:
        update_sheet(ws_aktif, pub, "kode_lelang", header_aktif)
        update_sheet(ws_internal, itn, "kode_lelang", header_internal)
    
    print(f"[+] {len(data_list)} data diproses (update/tambah)")

def main():
    start = time.time()
    driver = None
    batch_size = 5
    consecutive_failures = 0
    total_failures = 0
    
    try:
        checkpoint = load_checkpoint()
        if checkpoint:
            print(f"[*] Melanjutkan dari checkpoint: {checkpoint['index']}/{checkpoint['total']}")
            urls = checkpoint['urls']
            start_index = checkpoint['index']
        else:
            driver = init_driver(HEADLESS_MODE)
            urls = get_all_detail_urls(driver, BASE_URL)
            if not urls:
                print("[!] Tidak ada URL ditemukan.")
                return
            start_index = 0
            driver.quit()
            driver = None
        
        all_data = []
        for i in range(start_index, len(urls)):
            if driver is None:
                driver = init_driver(HEADLESS_MODE)
                consecutive_failures = 0
            
            print(f"\n--- {i+1}/{len(urls)} ---")
            
            pub, itn, is_fatal = extract_detail_data(driver, urls[i], timeout_minutes=TIMEOUT_PER_URL)
            
            if pub is None or itn is None:
                consecutive_failures += 1
                total_failures += 1
                print(f"[!] Gagal mengambil data untuk {urls[i]}")
                print(f"[!] Total gagal berturut-turut: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}")
                
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"[!] {MAX_CONSECUTIVE_FAILURES} URL gagal berturut-turut. Kemungkinan server down.")
                    print("[*] Hentikan scraper. Jalankan lagi nanti.")
                    save_checkpoint(i + 1, urls)
                    break
                
                save_checkpoint(i + 1, urls)
                continue
            
            consecutive_failures = 0
            all_data.append((pub, itn))
            
            if len(all_data) >= batch_size or i == len(urls) - 1:
                print(f"\n[*] Menyimpan {len(all_data)} data ke Google Sheets...")
                save_to_sheets(all_data, SPREADSHEET_ID, SERVICE_ACCOUNT_FILE)
                all_data = []
                clear_checkpoint()
                save_checkpoint(i + 1, urls)
            
            time.sleep(2)
        
        clear_checkpoint()
        
        elapsed_hours = (time.time() - start) / 3600
        print(f"\n{'='*50}")
        print(f"[*] SCRAPING SELESAI")
        print(f"[*] Total URL: {len(urls)}")
        print(f"[*] Total gagal: {total_failures}")
        print(f"[*] Waktu total: {elapsed_hours:.2f} jam")
        print(f"{'='*50}")
        
    except Exception as e:
        print(f"[!] Error: {e}")
        print("[*] Scraper berhenti. Jalankan lagi untuk melanjutkan.")
    finally:
        if driver:
            driver.quit()
        print(f"\n[*] Selesai dalam {round((time.time() - start) / 60, 2)} menit.")

if __name__ == "__main__":
    main()
# milari-gw
Milari Gawe - Job hunting scrape to linkedin, indeed, etc

Dashboard lokal untuk mencari lowongan LinkedIn, Indeed, JobStreet, Adzuna, dan CareerCross, lalu memantau status lamaran. Data tersimpan di `jobs.db`.

## Install

Requirement: Python 3.10+.

```bash
cd milari-gw
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Akses di http://127.0.0.1:5000.

## Adzuna

Daftar di https://developer.adzuna.com untuk dapat beberapa token (subscribe £8/bulan), lalu buat file `.env` dengan isi berikut:

```
ADZUNA_APP_ID=isi_app_id
ADZUNA_APP_KEY=isi_app_key
ADZUNA_COUNTRY=sg
```

Adzuna tidak ada Asia dan kebanyakan Amerika dan Australia, jadi gunakan negara terdekat (mis. Singapore atau Australia).

## Fitur

- Cari lowongan di beberapa situs sekaligus.
- Skor kecocokan: persentase skill yang muncul di judul/deskripsi lowongan, hasil diurutkan dari yang paling cocok.
- Penanda otomatis: visa / relokasi / English-friendly dan remote, plus filter untuk keduanya.
- Pelacak lamaran: Baru, Disimpan, Dilamar, Interview, Offer, Ditolak, lengkap dengan catatan per lowongan.
- Tautan langsung ke halaman pencarian tiap situs.
- Ekspor semua lowongan ke CSV.

## Catatan

- LinkedIn membatasi permintaan, jadi gunakan hasil jumlah kecil (10-20) dan jangan klik "Cari" berulang-ulang.
- JobStreet dan CareerCross dibuat secara best-effort: JobStreet memakai endpoint internal, CareerCross membaca HTML. Keduanya bisa berubah atau memblokir sewaktu-waktu. Jika gagal, pakai tautan "Buka langsung di situs".
- JobStreet Indonesia hanya relevan untuk lokasi di Indonesia; untuk Jepang pilih LinkedIn, Indeed, dan CareerCross.

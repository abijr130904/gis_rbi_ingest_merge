## Merge Shapefile RBI

```powershell
cd D:\Developer\python_project\gis_rbi_ingest

pyhton --version

winges install pyhton.python.3.12

python -m venv .venv

.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip

pip install -r requirements-merge.txt

# 1) selalu dry-run dulu
python merge_shp.py --input "D:\Developer\python_project\gis_rbi_ingest\data" `
  --output "D:\Developer\python_project\gis_rbi_ingest\merged_output" --dry-run `
  --suggest-similar --report ".\dryrun_report.json"

# 2) merge sungguhan
  python merge_shp.py --input "D:\Developer\python_project\gis_rbi_ingest2\data" --output "D:\Developer\python_project\gis_rbi_ingest2\merged_output"

# Opsi umum merge:
#   --target-crs EPSG:4326
#       Transformasikan semua output ke CRS EPSG:4326. Tanpa opsi ini CRS dipertahankan.
#   --allow-schema-union
#       Izinkan kolom berbeda antar sumber; kolom yang tidak ada diisi NULL.
#   --skip-bad-files
#       Lewati file sumber yang rusak/tidak lengkap; hasil bisa parsial.
#   --include-single
#       Sertakan kategori yang hanya ditemukan pada satu sumber.
#   --overwrite
#       Izinkan output yang sudah ada diganti.
#   --category ADMINISTRASI_LN_50K
#       Proses hanya kategori tersebut; opsi ini bisa diulang untuk kategori lain.

# test
python -m pytest tests -q
```

## Panduan CLI

Program ini menggabungkan file Shapefile berdasarkan nama kategori. Jalankan `python merge_shp.py --help` untuk melihat opsi yang tersedia.

### Ekstrak layer GDB langsung dari ZIP

Gunakan `--extract-gdb` untuk menyimpan setiap layer spasial sebagai file `.gdb` terpisah. GDB yang berada di dalam ZIP dibaca langsung oleh GDAL, tanpa mengekstrak isi ZIP ke disk.

```powershell
# Pindai folder sumber beserta subfoldernya
python .\merge_shp.py --input "G:\INAGEOPORTAL_FULL\Maluku Utara 50k" `
  --output "G:\INAGEOPORTAL_FULL\ekstrak_gdb\Maluku Utara 50k" --extract-gdb --dry-run

# Jalankan ekstraksi
python .\merge_shp.py --input "G:\INAGEOPORTAL_FULL\Maluku Utara 50k" `
  --output "G:\INAGEOPORTAL_FULL\ekstrak_gdb\Maluku Utara 50k" --extract-gdb

# Bisa juga berikan satu ZIP langsung
python .\merge_shp.py --input "G:\INAGEOPORTAL_FULL\data.zip" `
  --output "G:\INAGEOPORTAL_FULL\ekstrak_gdb" --extract-gdb
```

Hasil dari ZIP disimpan di subfolder bernama ZIP, mengikuti struktur folder GDB di dalam arsip. Penulisan output membutuhkan driver GDAL `OpenFileGDB` yang mendukung write.

### Ekstrak arsip ZIP/RAR

`ekstrak_arsip.py` mencari arsip di folder sumber dan subfoldernya. Secara bawaan hasil masuk ke folder `hasil_ekstrak` di dalam folder sumber.

```powershell
python ekstrak_arsip.py "D:\data\arsip"
```

| Argumen/Opsi | Kegunaan |
|---|---|
| `SUMBER` | Wajib. Folder yang berisi arsip ZIP/RAR. |
| `-o PATH`, `--output PATH` | Folder tujuan ekstraksi. |
| `-w ANGKA`, `--worker ANGKA` | Jumlah worker paralel. Bawaan `4`, minimal `1`. |
| `-p PASSWORD`, `--password PASSWORD` | Password arsip jika diperlukan. |
| `--timpa` | Ekstrak ulang meskipun folder tujuan arsip sudah ada. |

Contoh menentukan folder output dan jumlah worker:

```powershell
python ekstrak_arsip.py "D:\data\arsip" `
  --output "D:\data\hasil_ekstrak" --worker 4
```

Untuk bantuan opsi masing-masing perintah, jalankan:

```powershell
python merge_shp.py --help
python ekstrak_arsip.py --help
```\n

# Eksperimen L1 structure-first

Prototipe ini mengubah PDF menjadi teks selectable, posisi/struktur kandidat,
garis tabel kandidat, dan crop visual. Ini bukan kompresor PDF byte-exact dan
bukan konverter yang sudah disetujui untuk menghapus sumber.

## Jalankan

Dari root repo, gunakan environment terisolasi. Tidak perlu mengubah dependency
atau `uv.lock` aplikasi acquisition yang sudah ada.

```powershell
uv run --with-requirements tools/l1/requirements.txt pytest tools/l1/test_l1.py -q
uv run --with-requirements tools/l1/requirements.txt python tools/l1/study.py --input data --output reports/l1-study-new --workers 2
```

`--output` harus folder baru, terpisah dari subtree input. Default dibatasi 20 PDF
atau 256 MB input; tidak ada download, OCR, migrasi database atau penghapusan
sumber. Gunakan `--page-limit 3` untuk smoke test; hasil subset halaman tidak
mendapat klaim persentase penghematan terhadap seluruh PDF.

Jika corpus belum ada dalam `data`, workflow `.github/workflows/l1-study.yml`
memulihkan sepuluh PDF dari artefak `.pdf.gz` studi terdahulu yang sudah berada
di repo, lalu mencocokkan SHA-256 dan ukuran dengan manifest. Ini bukan
acquisition baru dan tidak mengganti sumber dengan PDF hasil optimasi.

## Dua kebijakan region, tiga representation

- `guarded`: bagian raster yang tidak diterangkan kotak teks menjadi kandidat
  crop. Envelope grid tabel dipertahankan sebagai crop. Bentuk tersambung yang
  melintasi teks dan band penutup/tanda tangan kandidat turut dipertahankan.
- `vector_candidate`: garis panjang hanya diganti menjadi vektor di dalam
  envelope grid tabel yang terdeteksi. Ini lebih agresif dan membutuhkan audit.
  Potongan kurva stempel tidak boleh dianggap sebagai garis tabel hanya karena
  lolos operasi morphology.

Untuk setiap kebijakan, runner menulis:

| Nama | Isi |
|---|---|
| `json_entries.ildr` | JSON+zstd; setiap asset entry terpisah |
| `channel_pack.ildr` | Channel metadata MessagePack+zstd; asset digabung ke `assets.bin` |
| `flow_pack.ildr` | Channel pack; geometry baris dibuang dan paragraf digabung pada halaman non-tabel |

Ketiganya menyimpan seluruh karakter hasil ekstraksi sekali pada text pool per
halaman. Teks asli hasil ekstraksi tidak dinormalisasi atau diperbaiki diam-diam.
Label `article`, `chapter`, dan lainnya adalah kandidat dari pola sederhana,
bukan AST hukum terverifikasi. Halaman sumber tetap punya ID walaupun tampilan
reflow berbeda. Table-cell/rowspan inference belum diimplementasikan.

Asset PNG atau TIFF Group 4 dipilih berdasarkan ukuran aktual; tidak ada JPEG
untuk crop bilevel. Setiap asset didekode kembali dan dibandingkan pikselnya
terhadap bitmap crop sebelum encoding. Warna tidak otomatis diubah ke hitam-putih.
Tidak ada penggantian logo/stempel/tanda tangan dengan asset canonical.

## Validasi dan batas penting

1. SHA-256 PDF sebelum dan sesudah eksperimen harus sama.
2. Package dibaca ulang dari disk; struktur dan teks harus round-trip.
3. Semua asset dalam package harus ada, checksum cocok, dan piksel cocok.
4. Input kosong menghasilkan `blocked_no_input_pdfs`, bukan saving 0%.
5. Hasil sebagian atau gagal tidak mempunyai aggregate saving corpus penuh.

**Kesamaan teks hanya membuktikan kesetiaan terhadap layer ekstraksi. Itu tidak
membuktikan bahwa OCR sumber benar.** Dalam sampel nyata layer OCR memang sudah
mengandung salah baca. Region detection juga dapat melewatkan visual yang
tercakup seluruhnya oleh kotak OCR. Karena itu:

```json
{"ocr_accuracy": null, "evidence_recall": null,
 "approved_for_source_deletion": false, "status": "requires_review"}
```

Status tidak berubah menjadi approved hanya karena rasio kompresinya tinggi.
Piksel crop dipertahankan, tetapi seluruh piksel halaman tidak dijamin. Jika
PDF tidak memiliki satu raster halaman yang sesuai, crop berasal dari render
halaman maksimum 200 DPI; `asset_basis` menjelaskan sumbernya. Deteksi region
menggunakan downsample 128 DPI secara default, bukan OCR. Ini dapat kehilangan
komponen kecil atau kontras rendah. Region manual dapat dilindungi dengan:

```json
{"SHA256_PDF": {"1": [[100, 500, 450, 800]]}}
```

Koordinat berupa PDF points pada halaman unrotated. Pass JSON itu melalui
`--protect protected.json`. Untuk final acquisition, pencocokan source URL,
provider, dan metadata registry masih perlu integrasi; tool ini mencatat source
hash, ukuran, nama file, versi konverter dan source page.

## Melihat hasil

`preview.html`/`viewer.html` adalah tampilan membaca offline; `fixed-preview.html`
adalah review posisi. Crop visual dapat dibuka di panelnya. Source-page PNG pada
halaman awal/tengah/akhir dan halaman 131 merupakan bahan audit terpisah, bukan
bagian ukuran canonical L1. Viewer bukan salinan resmi dan tidak memperbaiki OCR.

```powershell
uv run --with-requirements tools/l1/requirements.txt python tools/l1/open_l1.py path/to/flow_pack.ildr --html viewer.html --text extracted.txt
```

Gunakan `channel_pack.ildr --fixed` untuk review geometri.

## Hasil yang dihitung

`summary.json`, `representation_benchmark.csv`, `document_results.json`,
`input_manifest.json`, `provenance.json`, `L1_STUDY_REPORT.md`, dan per-document
`page_metrics.json` melaporkan bytes aktual, worst/mean/P10/P50/P90/best, error,
jumlah karakter, kandidat tabel, fallback halaman, serta validasi.

Total canonical memasukkan metadata terkompresi, asset, index dan header ZIP.
Source PDF, index pencarian, preview, log, fixture, duplikat candidate package,
dan output text-only diagnostik tidak ikut. Ruang workspace eksperimen karenanya
lebih besar daripada ukuran satu representation. Shard teks hanyalah diagnosis,
bukan representation L1 tanpa biaya asset. Jangan menjumlah saving antar teknik.

Belum diimplementasikan: classifier signature/stamp terlatih, OCR validation,
legal AST terverifikasi, semantic table extraction, learned templates,
Re-Pair/rANS, atau ingest/eviction produksi. Jangan membuat klaim fitur tersebut.

## Referensi implementasi

- PyMuPDF, Text Extraction: https://pymupdf.readthedocs.io/en/latest/app1.html
- PyMuPDF, Page: https://pymupdf.readthedocs.io/en/latest/page.html
- Pillow formats: https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html
- MessagePack: https://msgpack.org/

Semua heuristic dan kebijakan L1 dalam tool ini adalah rancangan eksperimen,
bukan jaminan yang diberikan library tersebut.

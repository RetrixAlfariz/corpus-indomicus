# Hasil studi storage PDF Corpus-Indomicus

Cakupan: seluruh **10 PDF lokal**, **728 halaman**, **46,684,304 byte** (46.684 MB).
Ini sensus file lokal pada manifest, bukan sampel representatif seluruh PDF hukum Indonesia. Tidak ada download baru atau OCR ulang.

## Temuan dan jawaban

1. Penyebab storage terbesar: image stream **83.316%**. Content stream menyumbang 13.172%.
   Stream tanpa filter berjumlah 6,651,141 byte (14.247%), sehingga generic compression masih memiliki ruang saving meskipun raster sudah terkompresi. Flate seluruh stream menyumbang 0.782%; image Flate 0% pada snapshot ini.
2. Semua 728 halaman masuk heuristik raster+OCR; 728 memiliki setidaknya 20 karakter invisible text dan 728 memiliki full-page raster. Born-digital/raster tanpa text/mixed masing-masing 0% pada dataset ini.
3. Codec dominan adalah CCITTFaxDecode: 749 image objects; 83.155% dari total byte. Image yang tampil pada halaman adalah DeviceGray 1-bit sekitar 400 DPI. JPEG 0.161%; JPX dan JBIG2 0%.
4. Duplicate encoded-stream byte ratio terhadap seluruh PDF: **0.145%**. Redundansi cross-document 50,989 byte (0.109%). Cross-document duplicate object ratio 1.232% dengan denominator nonempty stream objects; bukan seluruh object PDF.
5. Saving exact terukur: gzip 16.779%, zstd 20.670%, 7z/LZMA2 24.237%. Semua klaim exact mensyaratkan SHA-256 hasil rekonstruksi sama dengan original.
6. Structural derivative: 15.070%. Layered 200-DPI/JPEG-75 derivative: -311.896%. Saving negatif berarti file membesar. Text layer dibandingkan dan semua halaman dirender; kualitas hukum/detail kecil belum disetujui. Derived bytes tidak archival-exact.
7. Container stream-CAS+zstd: **17.195%**, dibanding zstd biasa **20.670%**. Untuk korpus ini belum ada alasan storage untuk membangun custom container: gain dedup kecil, ada overhead chunk/manifest, dan rasio kompresinya lebih buruk. Ini tidak menutup kemungkinan hasil berbeda pada korpus lebih besar/beragam.

## Ukuran halaman

Alokasi estimasi byte/page: mean 64,126.8; P10 44,646.9; median/P50 63,965.8; P90 82,828.3; minimum 18,124.0; maksimum 139,259.2.
Page bukan unit byte mandiri di PDF: shared stream dibagi ke halaman pemakai; residual dibagi merata. Nilai file_bytes/page_count juga tersedia terpisah.

## Benchmark per metode

Persentase per-PDF; kolom agregat memakai bobot original bytes. Min = worst, max = best. Waktu satu pass termasuk overhead yang dijelaskan di docs/pdf-storage-study.md.

| Metode | Output bytes | Agregat saving % | Worst | Mean | P10 | P50 | P90 | Best | Encode s | Decode s | Error/skip |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| original | 46,684,304 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0/0 |
| gzip9 | 38,851,377 | 16.779 | 13.786 | 16.654 | 15.122 | 16.371 | 18.713 | 19.993 | 1.894 | 0.165 | 0/0 |
| zstd9 | 37,034,747 | 20.670 | 15.399 | 19.826 | 17.401 | 19.482 | 22.821 | 24.072 | 0.649 | 0.065 | 0/0 |
| 7z_lzma2 | 35,369,533 | 24.237 | 16.953 | 22.412 | 18.740 | 22.298 | 25.945 | 26.596 | 4.019 | 0.840 | 0/0 |
| structural_pymupdf | 39,648,756 | 15.070 | 11.266 | 18.601 | 12.336 | 17.884 | 23.219 | 38.257 | 0.566 | 0.009 | 0/0 |
| layered_200dpi | 192,290,633 | -311.896 | -350.083 | -290.033 | -340.893 | -290.537 | -257.647 | -202.079 | 109.288 | 0.010 | 0/0 |
| djvu | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | 0.000 | 0.000 | 0/10 |
| lepton | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | 0.000 | 0.000 | 0/10 |
| stream_cas_zstd9 | 38,657,152 | 17.195 | 13.785 | 17.167 | 15.816 | 17.055 | 18.972 | 20.402 | 3.082 | 0.684 | 0/0 |

## Komposisi stream

| Category | Encoded bytes | % dari original |
|---|---:|---:|
| other_pdf_objects | 848,719 | 1.818 |
| fonts | 0 | 0.000 |
| image | 38,895,439 | 83.316 |
| content_streams | 6,149,339 | 13.172 |
| metadata | 18,287 | 0.039 |
| structural/unaccounted | 772,520 | 1.655 |

Image-codec share dan all-stream-codec share dibedakan di storage_summary.json. Flate pada content/object streams tidak sama dengan raster Flate.

## Batas interpretasi dan pemeriksaan

- 79 slot xref tidak dapat dibaca; bisa mencakup free/deleted slots. Disimpan sebagai warning/error rows, bukan diasumsikan seluruh object telah terbaca. Semua PDF/page berhasil diprofilkan; jumlah byte residual tetap disajikan.
- Font stream share 0%; 36 font objects tanpa embedded program ditemukan. Tidak ada duplicate embedded font program; kesamaan nama Helvetica tidak dihitung sebagai dedup byte.
- 0 vector-heavy pages. Flag map/table pada 728 halaman dipicu ukuran scan 400 DPI; **bukan bukti semua halaman berisi peta/tabel**. Tidak dilakukan semantic detection/OCR ulang.
- Pemeriksaan visual terpisah pada halaman 131 PDF 30093fb7... mengonfirmasi satu lampiran tabel hukum. Catatan ada di visual_sample_notes.json; temuan sampel ini bukan klasifikasi semantik seluruh korpus.
- JPEG reversible recompression tidak dijalankan karena Lepton tidak tersedia. Bahkan batas atas tidak realistis menghapus seluruh JPEG payload hanya menghemat 0.161% korpus; ini batas byte, bukan klaim rasio recompression.
- DjVu tidak tersedia; layered derivative menjadi perbandingan non-archival. Downsample mengganti image dengan JPEG, termasuk bitonal, sehingga bisa membesar. Kegagalan rasio ini tetap dicatat.
- Hasil derived berlaku untuk dua metode yang diuji saja. Downsampling dengan mempertahankan bilevel/CCITT atau codec lain belum diukur; tidak ada klaim bahwa seluruh derived representation pasti lebih besar.
- Structural derivative lolos perbandingan render 96 DPI dan text pada semua halaman; tidak membuktikan preservasi signature, metadata, accessibility atau seluruh fitur interaktif.
- Preview error untuk lossy derivative diukur grayscale 36 DPI, hanya diagnostik; tidak membuktikan akurasi huruf/angka/detail kecil. Preview 96 DPI disimpan untuk first/middle/last setiap PDF.
- Peak memory tidak diukur (null). Logical container bytes memasukkan manifest/frame, tidak memasukkan filesystem allocation.
- Validation checks: PASS. Hash original diperiksa lagi setelah seluruh benchmark.

## Reproduksi

```powershell
uv run --locked --extra profile --extra dev pytest tests/test_pdf_profile.py tests/test_pdf_benchmark.py -q
uv run --locked --extra profile python -u -m corpus_indomicus.pdf_profile --input data --output reports/reproduction --benchmark
uv run --locked --extra profile python scripts/summarize_storage_study.py reports/reproduction
```

Gunakan direktori output baru. Versi tools, source hashes, parameter, input SHA-256, compressed artifacts, portable dedup blobs, hasil rekonstruksi dan timings disimpan. Dataset lokal sudah content-addressed; duplicate download URL bukan duplicate file dalam studi ini.

Percobaan pendahuluan reports/local-study-20260916 memakai 7z lama pada PATH dan mencatat 10 kegagalan LZMA2. Hasil final ini menggunakan instalasi 7-Zip modern yang terdeteksi; pendahuluan tetap disimpan sebagai jejak diagnosis.
